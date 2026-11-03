import importlib.util
import json
import struct
import sys
from pathlib import Path
from typing import Any
from zipfile import ZipFile

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("package_teams", ROOT / "scripts/package_teams.py")
assert spec is not None and spec.loader is not None
packaging = importlib.util.module_from_spec(spec)
spec.loader.exec_module(packaging)
SUBSCRIPTION = "11111111-1111-4111-8111-111111111111"
BOT_ID = "22222222-2222-4222-8222-222222222222"
SCHEMA_PATH = ROOT / "tests/fixtures/MicrosoftTeams.v1.22.schema.json"


@pytest.fixture(scope="session")
def schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text())


@pytest.fixture
def manifest() -> dict[str, Any]:
    template = json.loads((ROOT / "teams/maf/manifest.template.json").read_text())
    return packaging.render(
        template,
        {
            "BOT_ID": BOT_ID,
            "APP_ID": packaging.app_id(SUBSCRIPTION, "rg", "demo", "dev", "maf"),
            "DOMAIN": "demo.azurecontainerapps.io",
            "VERSION": "1.1.0",
        },
    )


def test_stable_distinct_app_ids() -> None:
    args = (SUBSCRIPTION, "rg", "demo", "dev")
    assert packaging.app_id(*args, "maf") == packaging.app_id(*args, "maf")
    assert packaging.app_id(*args, "maf") != packaging.app_id(*args, "langchain")
    assert packaging.app_id(*args, "maf") != packaging.app_id(
        SUBSCRIPTION, "rg2", "demo", "dev", "maf"
    )


@pytest.mark.parametrize("agent", ["maf", "langchain"])
def test_archive_shape_and_icon_sizes(agent: str, tmp_path: Path, schema: dict[str, Any]) -> None:
    app_id = packaging.app_id(SUBSCRIPTION, "rg", "demo", "dev", agent)
    archive = packaging.package(
        agent, BOT_ID, "demo.azurecontainerapps.io", app_id, "1.1.0", tmp_path, schema
    )
    with ZipFile(archive) as bundle:
        assert set(bundle.namelist()) == {"manifest.json", "color.png", "outline.png"}
        manifest = json.loads(bundle.read("manifest.json"))
        assert manifest["bots"][0]["botId"] == BOT_ID
        assert manifest["id"] == app_id
        assert manifest["version"] == "1.1.0"
        assert manifest["manifestVersion"] == "1.22"
        assert manifest["$schema"] == packaging.SCHEMA_URL
        bot = manifest["bots"][0]
        assert bot["scopes"] == ["personal", "copilot"]
        assert bot["isNotificationOnly"] is False
        assert bot["supportsFiles"] is False
        assert bot["commandLists"][0]["scopes"] == bot["scopes"]
        assert len(bot["commandLists"][0]["commands"]) >= 3
        agents = manifest["copilotAgents"]
        assert "declarativeAgents" not in agents
        assert len(agents["customEngineAgents"]) == 1
        custom_agent = agents["customEngineAgents"][0]
        assert custom_agent["id"] == BOT_ID
        assert custom_agent["type"] == "bot"
        assert "AI-generated" in custom_agent["disclaimer"]["text"]
        for name, size in [("color.png", 192), ("outline.png", 32)]:
            data = bundle.read(name)
            assert data[:8] == b"\x89PNG\r\n\x1a\n"
            assert struct.unpack(">II", data[16:24]) == (size, size)


@pytest.mark.parametrize(
    "scopes",
    [
        ["personal"],
        ["copilot"],
        ["personal", "copilot", "team"],
        ["personal", "copilot", "groupChat"],
    ],
)
def test_unsupported_bot_scopes_rejected(manifest: dict[str, Any], scopes: list[str]) -> None:
    manifest["bots"][0]["scopes"] = scopes
    with pytest.raises(ValueError, match="Bot scopes"):
        packaging.validate_copilot_manifest(manifest, BOT_ID)


def test_command_list_scopes_must_match(manifest: dict[str, Any]) -> None:
    manifest["bots"][0]["commandLists"][0]["scopes"] = ["personal"]
    with pytest.raises(ValueError, match="Command lists"):
        packaging.validate_copilot_manifest(manifest, BOT_ID)
    del manifest["bots"][0]["commandLists"]
    with pytest.raises(ValueError, match="Command lists"):
        packaging.validate_copilot_manifest(manifest, BOT_ID)


@pytest.mark.parametrize("count", [0, 2])
def test_requires_one_bot_and_one_custom_engine_agent(manifest: dict[str, Any], count: int) -> None:
    bot = manifest["bots"][0]
    manifest["bots"] = [bot] * count
    with pytest.raises(ValueError, match="exactly one bot"):
        packaging.validate_copilot_manifest(manifest, BOT_ID)
    manifest["bots"] = [bot]
    agents = manifest["copilotAgents"]
    agents["customEngineAgents"] *= count
    with pytest.raises(ValueError, match="Exactly one custom engine agent"):
        packaging.validate_copilot_manifest(manifest, BOT_ID)


def test_declarative_agents_rejected(manifest: dict[str, Any]) -> None:
    manifest["copilotAgents"]["declarativeAgents"] = [{"id": "other", "file": "other.json"}]
    with pytest.raises(ValueError, match="must not contain declarative agents"):
        packaging.validate_copilot_manifest(manifest, BOT_ID)


@pytest.mark.parametrize("target", ["bot", "custom_agent"])
def test_mismatched_bot_ids_rejected_before_writing(
    manifest: dict[str, Any],
    schema: dict[str, Any],
    target: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if target == "bot":
        manifest["bots"][0]["botId"] = SUBSCRIPTION
    else:
        manifest["copilotAgents"]["customEngineAgents"][0]["id"] = manifest["id"]
    monkeypatch.setattr(packaging, "render", lambda *_args: manifest)
    with pytest.raises(ValueError, match="bot client ID"):
        packaging.package(
            "maf", BOT_ID, "demo.azurecontainerapps.io", manifest["id"], "1.1.0", tmp_path, schema
        )
    assert not (tmp_path / "maf-teams.zip").exists()


def test_official_schema_rejects_invalid_disclaimer(
    manifest: dict[str, Any],
    schema: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest["copilotAgents"]["customEngineAgents"][0]["disclaimer"]["text"] = "x" * 501
    monkeypatch.setattr(packaging, "render", lambda *_args: manifest)
    with pytest.raises(ValueError, match="manifest validation failed"):
        packaging.package(
            "maf", BOT_ID, "demo.azurecontainerapps.io", manifest["id"], "1.1.0", tmp_path, schema
        )
    assert not (tmp_path / "maf-teams.zip").exists()


def test_cli_defaults_to_updated_package_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "package_teams.py",
            "--agent",
            "maf",
            "--bot-id",
            BOT_ID,
            "--domain",
            "demo.azurecontainerapps.io",
            "--subscription",
            SUBSCRIPTION,
            "--resource-group",
            "rg",
            "--prefix",
            "demo",
            "--environment",
            "dev",
            "--output",
            str(tmp_path),
            "--schema",
            str(SCHEMA_PATH),
        ],
    )
    packaging.main()
    with ZipFile(tmp_path / "maf-teams.zip") as bundle:
        assert json.loads(bundle.read("manifest.json"))["version"] == "1.1.0"


def test_invalid_package_values_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="hostname"):
        packaging.package("maf", BOT_ID, "https://example", SUBSCRIPTION, "1.0.0", tmp_path, {})
    with pytest.raises(ValueError, match="distinct"):
        packaging.package("maf", BOT_ID, "example", BOT_ID, "1.0.0", tmp_path, {})
