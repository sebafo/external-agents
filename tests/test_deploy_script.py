import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def script_env(tmp_path: Path, apply: bool = False) -> tuple[Path, dict[str, str]]:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copy(ROOT / "scripts/deploy.sh", scripts / "deploy.sh")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_az = bin_dir / "az"
    fake_az.write_text(
        """#!/usr/bin/env bash
set -eu
printf '%s\\n' "$*" >> "$CALL_LOG"
case "$1 $2 $3" in
  "extension show --name") ;;
  "account show --subscription") echo 22222222-2222-4222-8222-222222222222 ;;
  "cognitiveservices account show")
    printf '%s\\n' "$MODEL_INFO_JSON" ;;
  "cognitiveservices account deployment") ;;
  "bicep build --file") ;;
  "group exists --name") printf '%s\\n' "$GROUP_EXISTS" ;;
  "group create --subscription") ;;
  "deployment group what-if") ;;
  "deployment group show")
    echo '{"registryName":{"value":"registry"},
      "registryServer":{"value":"registry.azurecr.io"},
      "environmentName":{"value":"environment"}}' ;;
  "deployment group create")
    if [[ "$*" == *foundation.bicep* ]]; then
      echo '{"registryName":{"value":"registry"},
        "registryServer":{"value":"registry.azurecr.io"},
        "environmentName":{"value":"environment"}}'
    else
      echo '{"agents":{"value":[{"name":"maf","fqdn":"maf.example.com",
        "botClientId":"33333333-3333-4333-8333-333333333333"},
        {"name":"langchain","fqdn":"langchain.example.com",
        "botClientId":"44444444-4444-4444-8444-444444444444"}]}}'
    fi ;;
  "acr repository list") echo '[]' ;;
  "acr repository show") printf 'sha256:%064d\\n' 0 ;;
  "acr build --registry") ;;
  "resource list --subscription")
    echo '[{"id":"/subscriptions/11111111-1111-4111-8111-111111111111/resourceGroups/models/providers/Microsoft.CognitiveServices/accounts/example","resourceGroup":"models"}]' ;;
  *) echo "Unexpected Azure command: $*" >&2; exit 88 ;;
esac
"""
    )
    fake_az.chmod(0o755)
    for name, body in [
        ("uv", 'printf \'%s\\n\' "$*" >> "$UV_LOG"'),
        (
            "curl",
            """if [[ "$*" == *health/ready* ]]; then
  echo '{"status":"ready"}'
else
  printf 401
fi""",
        ),
    ]:
        executable = bin_dir / name
        executable.write_text(f"#!/usr/bin/env bash\nset -eu\n{body}\n")
        executable.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "CALL_LOG": str(tmp_path / "az.log"),
        "UV_LOG": str(tmp_path / "uv.log"),
        "GROUP_EXISTS": "true",
        "AZURE_SUBSCRIPTION_ID": "11111111-1111-4111-8111-111111111111",
        "AZURE_TENANT_ID": "22222222-2222-4222-8222-222222222222",
        "AZURE_RESOURCE_GROUP": "demo-rg",
        "AZURE_LOCATION": "westeurope",
        "AZURE_FOUNDRY_PROJECT_ENDPOINT": (
            "https://example.services.ai.azure.com/api/projects/example-project"
        ),
        "AZURE_OPENAI_ENDPOINT": "https://example.services.ai.azure.com/openai/v1/",
        "AZURE_OPENAI_DEPLOYMENT_NAME": "demo",
        "PREFIX": "demo",
        "ENVIRONMENT": "dev",
        "IMAGE_TAG": "test-build",
        "MODEL_INFO_JSON": json.dumps(
            {
                "kind": "AIServices",
                "properties": {"endpoint": "https://example.services.ai.azure.com/"},
            }
        ),
    }
    return scripts / "deploy.sh", env


@pytest.mark.parametrize("group_exists", ["true", "false"])
def test_default_is_read_only_plan(tmp_path: Path, group_exists: str) -> None:
    script, env = script_env(tmp_path)
    env["GROUP_EXISTS"] = group_exists
    result = subprocess.run(
        ["bash", str(script)], env=env, capture_output=True, text=True, input="", timeout=30
    )
    assert result.returncode == 0, result.stderr
    assert "Plan only" in result.stdout
    calls = (tmp_path / "az.log").read_text()
    assert ("what-if" in calls) == (group_exists == "true")
    assert "group create" not in calls
    assert "deployment group create" not in calls
    assert "acr build" not in calls


@pytest.mark.parametrize("compatibility_flags", [[], ["--yes"]])
def test_apply_existing_digest_only_updates_selected_agent(
    tmp_path: Path, compatibility_flags: list[str]
) -> None:
    script, env = script_env(tmp_path)
    images = tmp_path / "rollback.json"
    images.write_text('{"maf":"registry.azurecr.io/maf@sha256:' + "a" * 64 + '"}')
    result = subprocess.run(
        [
            "bash",
            str(script),
            "--apply",
            *compatibility_flags,
            "--agent",
            "maf",
            "--app-version",
            "1.1.3",
            "--images-json",
            str(images),
        ],
        env=env,
        capture_output=True,
        text=True,
        input="",
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "Deployment and packaging completed" in result.stdout
    assert "without confirmation prompts" in result.stdout
    assert "support both personal Teams chat and Microsoft 365 Copilot" in result.stdout
    assert "Add agent (or Upload custom agent)" in result.stdout
    calls = (tmp_path / "az.log").read_text()
    assert "deployment group create" in calls
    assert "acr build" not in calls
    assert "containerapp delete" not in calls
    params = (tmp_path / "dist/demo-dev/applications.parameters.json").read_text()
    assert '"agentSelection": {\n      "value": "maf"' in params
    assert "langchain" not in params
    assert "storageUrl" not in params
    assert "openAiApiVersion" not in params
    assert "--agent maf" in (tmp_path / "uv.log").read_text()
    assert "--version 1.1.3" in (tmp_path / "uv.log").read_text()


@pytest.mark.parametrize("group_exists", ["true", "false"])
def test_apply_builds_and_deploys_both_agents_without_input(
    tmp_path: Path, group_exists: str
) -> None:
    script, env = script_env(tmp_path)
    env["GROUP_EXISTS"] = group_exists
    result = subprocess.run(
        ["bash", str(script), "--apply"],
        env=env,
        capture_output=True,
        text=True,
        input="",
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert f"Subscription: {env['AZURE_SUBSCRIPTION_ID']}" in result.stdout
    assert "Agents: all" in result.stdout
    assert "Deployment and packaging completed" in result.stdout
    assert "Type deploy" not in result.stdout + result.stderr
    calls = (tmp_path / "az.log").read_text()
    assert any(call.startswith("group create ") for call in calls.splitlines()) == (
        group_exists == "false"
    )
    assert calls.count("deployment group create") == 2
    assert calls.count("acr build --registry") == 2
    assert calls.index("deployment group what-if") < calls.index("deployment group create")
    assert calls.rindex("deployment group what-if") < calls.rindex("deployment group create")
    images = json.loads((tmp_path / "dist/demo-dev/images.json").read_text())
    assert set(images) == {"maf", "langchain"}
    package_calls = (tmp_path / "uv.log").read_text()
    for agent in ("maf", "langchain"):
        assert f"--image {agent}:test-build" in calls
        assert any(
            call.startswith("acr build ")
            and f"--image {agent}:test-build" in call
            and call.endswith(f"--file Dockerfile --platform linux/amd64 agents/{agent}")
            for call in calls.splitlines()
        )
        assert images[agent] == f"registry.azurecr.io/{agent}@sha256:" + "0" * 64
        assert f"--agent {agent}" in package_calls


def test_yes_without_apply_fails_before_cloud_calls(tmp_path: Path) -> None:
    script, env = script_env(tmp_path)
    result = subprocess.run(["bash", str(script), "--yes"], env=env, capture_output=True, text=True)
    assert result.returncode != 0
    assert "--yes requires --apply" in result.stderr
    assert not (tmp_path / "az.log").exists()


def test_help_describes_dual_surface_packages_without_cloud_calls(tmp_path: Path) -> None:
    script, env = script_env(tmp_path)
    result = subprocess.run(
        ["bash", str(script), "--help"], env=env, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert "Teams and Microsoft 365 Copilot" in result.stdout
    assert "default: 1.1.0" in result.stdout
    assert "Does not install or publish Teams/Copilot apps" in result.stdout
    assert "Deploy automatically without confirmation prompts" in result.stdout
    assert not (tmp_path / "az.log").exists()


def test_tenant_mismatch_fails_before_resource_writes(tmp_path: Path) -> None:
    script, env = script_env(tmp_path)
    env["AZURE_TENANT_ID"] = "44444444-4444-4444-8444-444444444444"
    result = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True)
    assert result.returncode != 0
    assert "tenant does not match" in result.stderr
    assert "create" not in (tmp_path / "az.log").read_text()


def test_plan_existing_images_includes_application_whatif(tmp_path: Path) -> None:
    script, env = script_env(tmp_path)
    images = tmp_path / "images.json"
    images.write_text('{"maf":"registry.azurecr.io/maf@sha256:' + "a" * 64 + '"}')
    result = subprocess.run(
        ["bash", str(script), "--agent", "maf", "--images-json", str(images)],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "az.log").read_text()
    assert "deployment group show" in calls
    assert "--template-file infra/applications.bicep" in calls
    assert "deployment group create" not in calls


def test_foreign_registry_digest_is_rejected(tmp_path: Path) -> None:
    script, env = script_env(tmp_path)
    images = tmp_path / "images.json"
    images.write_text('{"maf":"registryXazurecr.io/maf@sha256:' + "a" * 64 + '"}')
    result = subprocess.run(
        ["bash", str(script), "--agent", "maf", "--images-json", str(images)],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "Invalid registry/digest image" in result.stderr
    assert "deployment group create" not in (tmp_path / "az.log").read_text()


@pytest.mark.parametrize("endpoint_suffix", ["/", "/openai/", "/openai/v1/"])
@pytest.mark.parametrize("host", ["example.services.ai.azure.com"])
def test_foundry_resolves_account_and_inference_endpoint(
    tmp_path: Path, endpoint_suffix: str, host: str
) -> None:
    script, env = script_env(tmp_path)
    env["MODEL_INFO_JSON"] = json.dumps(
        {
            "kind": "AIServices",
            "properties": {"endpoint": "https://example.cognitiveservices.azure.com/"},
        }
    )
    env["AZURE_OPENAI_ENDPOINT"] = f"https://{host}" + endpoint_suffix
    images = tmp_path / "images.json"
    images.write_text('{"maf":"registry.azurecr.io/maf@sha256:' + "a" * 64 + '"}')
    result = subprocess.run(
        ["bash", str(script), "--agent", "maf", "--images-json", str(images)],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "az.log").read_text()
    assert (
        "openAiResourceId=/subscriptions/11111111-1111-4111-8111-111111111111/resourceGroups/models/providers/Microsoft.CognitiveServices/accounts/example"
        in calls
    )
    assert "/projects/" not in calls
    assert "deployment group create" not in calls
    params = json.loads((tmp_path / "dist/demo-dev/applications.parameters.json").read_text())
    assert params["parameters"]["openAiEndpoint"]["value"] == f"https://{host}"


@pytest.mark.parametrize(
    ("kind", "endpoint", "message"),
    [
        (
            "TextAnalytics",
            "https://example.services.ai.azure.com/openai/v1",
            "Unsupported model account kind",
        ),
        (
            "AIServices",
            "https://other.services.ai.azure.com/openai/v1",
            "must belong to the account",
        ),
        (
            "AIServices",
            "https://example.services.ai.azure.com/api/projects/demo",
            "project endpoint",
        ),
    ],
)
def test_invalid_model_account_or_endpoint_fails_before_writes(
    tmp_path: Path, kind: str, endpoint: str, message: str
) -> None:
    script, env = script_env(tmp_path)
    env["MODEL_INFO_JSON"] = json.dumps(
        {"kind": kind, "properties": {"endpoint": "https://example.services.ai.azure.com/"}}
    )
    env["AZURE_OPENAI_ENDPOINT"] = endpoint
    result = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True)
    assert result.returncode != 0
    assert message in result.stderr
    calls = (tmp_path / "az.log").read_text()
    assert "create" not in calls
    assert "bicep build" not in calls
