import argparse
import json
import re
import struct
import zlib
from pathlib import Path
from string import Template
from typing import Any
from urllib.request import urlopen
from uuid import NAMESPACE_URL, UUID, uuid5
from zipfile import ZIP_DEFLATED, ZipFile

from jsonschema import Draft4Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_URL = (
    "https://developer.microsoft.com/en-us/json-schemas/teams/v1.22/MicrosoftTeams.schema.json"
)


def app_id(
    subscription: str, resource_group: str, prefix: str, environment: str, agent: str
) -> str:
    identity = (
        f"azure://{UUID(subscription)}/{resource_group.lower()}/{prefix}/{environment}/{agent}"
    )
    return str(uuid5(NAMESPACE_URL, identity))


def icon(size: int, agent: str, outline: bool = False) -> bytes:
    """Generate original M/L demo artwork, with a transparent white outline variant."""
    glyph = (
        ["10001", "11011", "10101", "10001", "10001", "10001", "10001"]
        if agent == "maf"
        else ["10000", "10000", "10000", "10000", "10000", "10000", "11111"]
    )
    background = (
        (0, 0, 0, 0) if outline else ((36, 87, 167, 255) if agent == "maf" else (36, 107, 71, 255))
    )
    cell = size // 10
    left, top = (size - cell * 5) // 2, (size - cell * 7) // 2
    rows = bytearray()
    for y in range(size):
        rows.append(0)
        for x in range(size):
            gx, gy = (x - left) // cell, (y - top) // cell
            active = 0 <= gx < 5 and 0 <= gy < 7 and glyph[gy][gx] == "1"
            rows.extend((255, 255, 255, 255) if active else background)

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(rows)))
        + chunk(b"IEND", b"")
    )


def render(value: Any, substitutions: dict[str, str]) -> Any:
    if isinstance(value, str):
        return Template(value).substitute(substitutions)
    if isinstance(value, list):
        return [render(item, substitutions) for item in value]
    if isinstance(value, dict):
        return {key: render(item, substitutions) for key, item in value.items()}
    return value


def validate_copilot_manifest(manifest: dict[str, Any], bot_id: str) -> None:
    bots = manifest.get("bots", [])
    if len(bots) != 1 or bots[0]["botId"] != bot_id:
        raise ValueError("App manifest must contain exactly one bot matching the bot client ID")
    bot = bots[0]
    scopes = set(bot["scopes"])
    if scopes != {"personal", "copilot"}:
        raise ValueError(
            "Bot scopes must be personal and copilot; group/channel chats are unsupported"
        )
    command_lists = bot.get("commandLists", [])
    if not command_lists or any(set(commands["scopes"]) != scopes for commands in command_lists):
        raise ValueError("Command lists must match the personal and copilot bot scopes")
    agents = manifest.get("copilotAgents", {})
    if "declarativeAgents" in agents:
        raise ValueError("App manifest must not contain declarative agents")
    custom_agents = agents.get("customEngineAgents", [])
    if (
        len(custom_agents) != 1
        or custom_agents[0]["type"] != "bot"
        or custom_agents[0]["id"] != bot_id
    ):
        raise ValueError("Exactly one custom engine agent must reference the bot client ID")


def package(
    agent: str,
    bot_id: str,
    domain: str,
    teams_app_id: str,
    version: str,
    output: Path,
    schema: dict[str, Any],
) -> Path:
    if agent not in {"maf", "langchain"}:
        raise ValueError("Unknown agent")
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", domain):
        raise ValueError("Domain must be a hostname without a scheme or path")
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("Teams app version must be major.minor.patch")
    substitutions = {
        "BOT_ID": str(UUID(bot_id)),
        "APP_ID": str(UUID(teams_app_id)),
        "DOMAIN": domain,
        "VERSION": version,
    }
    if substitutions["BOT_ID"] == substitutions["APP_ID"]:
        raise ValueError("Teams app ID must be distinct from bot client ID")
    template = json.loads((ROOT / "teams" / agent / "manifest.template.json").read_text())
    manifest = render(template, substitutions)
    Draft4Validator.check_schema(schema)
    errors = list(Draft4Validator(schema, format_checker=FormatChecker()).iter_errors(manifest))
    if errors:
        details = "; ".join(
            f"{'.'.join(str(part) for part in error.path) or 'manifest'}: {error.message}"
            for error in errors[:5]
        )
        raise ValueError(f"Teams/Copilot manifest validation failed: {details}")
    validate_copilot_manifest(manifest, substitutions["BOT_ID"])
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f"{agent}-teams.zip"
    with ZipFile(archive, "w", ZIP_DEFLATED) as bundle:
        bundle.writestr("manifest.json", json.dumps(manifest, indent=2))
        bundle.writestr("color.png", icon(192, agent))
        bundle.writestr("outline.png", icon(32, agent, outline=True))
    return archive


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Package an agent for personal Teams chat and Microsoft 365 Copilot."
    )
    parser.add_argument("--agent", required=True, choices=["maf", "langchain"])
    parser.add_argument("--bot-id", required=True)
    parser.add_argument("--domain", required=True)
    parser.add_argument("--subscription", required=True)
    parser.add_argument("--resource-group", required=True)
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--version", default="1.1.0")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--schema", type=Path, help="Cached official manifest schema for offline use"
    )
    args = parser.parse_args()
    if args.schema:
        schema = json.loads(args.schema.read_text())
    else:
        with urlopen(SCHEMA_URL, timeout=30) as response:
            schema = json.load(response)
    result = package(
        args.agent,
        args.bot_id,
        args.domain,
        app_id(args.subscription, args.resource_group, args.prefix, args.environment, args.agent),
        args.version,
        args.output,
        schema,
    )
    print(result)


if __name__ == "__main__":
    main()
