#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

usage() {
  cat <<'HELP'
Deploy two Teams and Microsoft 365 Copilot agents using Bicep, ACR builds, and uv.
Default: compile and show what-if only. No resource creation or container builds.

Inputs may be flags or environment variables:
  --subscription ID       AZURE_SUBSCRIPTION_ID
  --tenant ID             AZURE_TENANT_ID
  --resource-group NAME   AZURE_RESOURCE_GROUP
  --location REGION       AZURE_LOCATION
  --prefix NAME           PREFIX (default: teamsdemo)
  --environment NAME      ENVIRONMENT (default: dev)
  --foundry-project-endpoint URL
                           AZURE_FOUNDRY_PROJECT_ENDPOINT (Foundry project endpoint)
  --deployment NAME       AZURE_OPENAI_DEPLOYMENT_NAME
  --endpoint URL          AZURE_OPENAI_ENDPOINT (Foundry OpenAI inference endpoint)
  --model-subscription ID AZURE_MODEL_SUBSCRIPTION_ID (default: application subscription)
  --image-tag TAG         IMAGE_TAG (required for builds; must be new)
  --agent maf|langchain|all (default: all)
  --images-json FILE      Deploy existing digest images instead of building
  --app-version X.Y.Z     Teams/Copilot app package version (default: 1.1.0)
  --apply                 Deploy automatically without confirmation prompts
  --yes                   Compatibility flag; unnecessary with --apply (requires --apply)
  --help

Requires az, its containerapp extension, uv, jq, curl, and Bash.
Does not install or publish Teams/Copilot apps. See README.md for tenant publishing.
No Docker daemon is required for ACR builds.
HELP
}

fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
SUBSCRIPTION="${AZURE_SUBSCRIPTION_ID:-}"
TENANT="${AZURE_TENANT_ID:-}"
RESOURCE_GROUP="${AZURE_RESOURCE_GROUP:-}"
LOCATION="${AZURE_LOCATION:-}"
PREFIX="${PREFIX:-teamsdemo}"
ENVIRONMENT="${ENVIRONMENT:-dev}"
FOUNDRY_PROJECT_ENDPOINT="${AZURE_FOUNDRY_PROJECT_ENDPOINT:-}"
MODEL_DEPLOYMENT="${AZURE_OPENAI_DEPLOYMENT_NAME:-}"
MODEL_ENDPOINT="${AZURE_OPENAI_ENDPOINT:-}"
MODEL_SUBSCRIPTION="${AZURE_MODEL_SUBSCRIPTION_ID:-}"
IMAGE_TAG="${IMAGE_TAG:-}"
AGENT=all
IMAGES_JSON=""
APP_VERSION=1.1.0
APPLY=false
YES=false
while (($#)); do
  case "$1" in
    --help) usage; exit 0 ;;
    --apply) APPLY=true; shift ;;
    --yes) YES=true; shift ;;
    --subscription|--tenant|--resource-group|--location|--prefix|--environment|--foundry-project-endpoint|--deployment|--endpoint|--model-subscription|--image-tag|--agent|--images-json|--app-version)
      (($# >= 2)) || fail "$1 requires a value"
      case "$1" in
        --subscription) SUBSCRIPTION="$2" ;;
        --tenant) TENANT="$2" ;;
        --resource-group) RESOURCE_GROUP="$2" ;;
        --location) LOCATION="$2" ;;
        --prefix) PREFIX="$2" ;;
        --environment) ENVIRONMENT="$2" ;;
        --foundry-project-endpoint) FOUNDRY_PROJECT_ENDPOINT="$2" ;;
        --deployment) MODEL_DEPLOYMENT="$2" ;;
        --endpoint) MODEL_ENDPOINT="$2" ;;
        --model-subscription) MODEL_SUBSCRIPTION="$2" ;;
        --image-tag) IMAGE_TAG="$2" ;;
        --agent) AGENT="$2" ;;
        --images-json) IMAGES_JSON="$2" ;;
        --app-version) APP_VERSION="$2" ;;
      esac
      shift 2 ;;
    *) fail "Unknown option: $1" ;;
  esac
done

MODEL_SUBSCRIPTION="${MODEL_SUBSCRIPTION:-$SUBSCRIPTION}"
[[ -n "$SUBSCRIPTION" && -n "$TENANT" && -n "$RESOURCE_GROUP" && -n "$LOCATION" \
   && -n "$FOUNDRY_PROJECT_ENDPOINT" && -n "$MODEL_ENDPOINT" && -n "$MODEL_DEPLOYMENT" ]] || {
  usage >&2; fail "Required deployment inputs are missing"
}
[[ "$PREFIX" =~ ^[a-z][a-z0-9]{1,11}$ ]] || fail "Prefix must be 2-12 lowercase alphanumerics"
[[ "$ENVIRONMENT" =~ ^[a-z][a-z0-9]{1,7}$ ]] || fail "Environment must be 2-8 lowercase alphanumerics"
UUID_PATTERN='^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
[[ "$SUBSCRIPTION" =~ $UUID_PATTERN && "$TENANT" =~ $UUID_PATTERN ]] \
  || fail "Subscription and tenant must be GUIDs"
[[ "$AGENT" == maf || "$AGENT" == langchain || "$AGENT" == all ]] || fail "Invalid agent"
[[ "$APP_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail "Invalid Teams app version"
[[ "$YES" == false || "$APPLY" == true ]] || fail "--yes requires --apply"
if [[ -z "$IMAGES_JSON" ]]; then
  [[ "$IMAGE_TAG" =~ ^[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,127}$ ]] || fail "Provide --image-tag"
else
  [[ -f "$IMAGES_JSON" ]] || fail "Images JSON does not exist"
fi
for tool in az uv jq curl; do command -v "$tool" >/dev/null || fail "Missing tool: $tool"; done
az extension show --name containerapp >/dev/null 2>&1 \
  || fail "Install the containerapp extension: az extension add --name containerapp"

ACTUAL_TENANT="$(az account show --subscription "$SUBSCRIPTION" --query tenantId -o tsv)"
TENANT="$(printf '%s' "$TENANT" | tr '[:upper:]' '[:lower:]')"
[[ "$(printf '%s' "$ACTUAL_TENANT" | tr '[:upper:]' '[:lower:]')" == "$TENANT" ]] \
  || fail "Azure tenant does not match --tenant"
[[ "$MODEL_SUBSCRIPTION" =~ $UUID_PATTERN ]] || fail "Model subscription must be a GUID"
[[ "$FOUNDRY_PROJECT_ENDPOINT" =~ ^https://([^./?#]+)\.services\.ai\.azure\.(com|us|cn)/api/projects/([^/?#]+)/?$ ]] \
  || fail "Provide a Foundry project endpoint such as https://<account>.services.ai.azure.com/api/projects/<project>"
MODEL_ACCOUNT="${BASH_REMATCH[1]}"
PROJECT_NAME="${BASH_REMATCH[3]}"
MODEL_TENANT="$(az account show --subscription "$MODEL_SUBSCRIPTION" --query tenantId -o tsv)"
[[ "$(printf '%s' "$MODEL_TENANT" | tr '[:upper:]' '[:lower:]')" == "$TENANT" ]] \
  || fail "Model account is in a different tenant"
MODEL_RESOURCES="$(az resource list --subscription "$MODEL_SUBSCRIPTION" \
  --resource-type Microsoft.CognitiveServices/accounts --name "$MODEL_ACCOUNT" -o json)"
[[ "$(jq 'length' <<<"$MODEL_RESOURCES")" == 1 ]] \
  || fail "Could not resolve exactly one Foundry account named '$MODEL_ACCOUNT' in model subscription '$MODEL_SUBSCRIPTION'"
MODEL_ID="$(jq -er '.[0].id' <<<"$MODEL_RESOURCES")"
MODEL_GROUP="$(jq -er '.[0].resourceGroup' <<<"$MODEL_RESOURCES")"
MODEL_INFO="$(az cognitiveservices account show --name "$MODEL_ACCOUNT" \
  --resource-group "$MODEL_GROUP" --subscription "$MODEL_SUBSCRIPTION" -o json)"
MODEL_KIND="$(jq -r .kind <<<"$MODEL_INFO")"
[[ "$MODEL_KIND" == AIServices ]] \
  || fail "Unsupported model account kind: $MODEL_KIND; expected AIServices (Foundry)"

# Store a resource root; both agents append /openai/v1/ for the Responses API.
normalize_endpoint() {
  local endpoint="$1"
  while [[ "$endpoint" == */ ]]; do endpoint="${endpoint%/}"; done
  endpoint="${endpoint%/openai/v1}"
  endpoint="${endpoint%/openai}"
  printf '%s' "$endpoint"
}

if [[ "$MODEL_ENDPOINT" == *"/api/projects/"* ]]; then
  fail "AZURE_OPENAI_ENDPOINT must be the Foundry OpenAI inference endpoint, not the project endpoint"
fi
MODEL_ENDPOINT="$(normalize_endpoint "$MODEL_ENDPOINT")"
[[ "$MODEL_ENDPOINT" =~ ^https://[^/?#[:space:]]+$ ]] \
  || fail "Provide an HTTPS Foundry OpenAI inference endpoint root or /openai/v1 URL"
[[ "$MODEL_ENDPOINT" =~ ^https://${MODEL_ACCOUNT}\.(services\.ai|openai)\.azure\.(com|us|cn)$ ]] \
  || fail "Foundry OpenAI endpoint must belong to the account in the project endpoint"
az cognitiveservices account deployment show --name "$MODEL_ACCOUNT" \
  --resource-group "$MODEL_GROUP" --subscription "$MODEL_SUBSCRIPTION" \
  --deployment-name "$MODEL_DEPLOYMENT" >/dev/null

OUT="$ROOT/dist/$PREFIX-$ENVIRONMENT"
mkdir -p "$OUT"
if [[ "$AGENT" == all ]]; then AGENTS=(maf langchain); else AGENTS=("$AGENT"); fi

validate_image() {
  local image="$1" agent="$2"
  [[ "$image" == "$SERVER/$agent@"* && "${image##*@}" =~ ^sha256:[a-f0-9]{64}$ ]] \
    || fail "Invalid registry/digest image: $agent"
}

write_application_parameters() {
  jq -n --arg prefix "$PREFIX" --arg environment "$ENVIRONMENT" --arg location "$LOCATION" \
    --arg agent "$AGENT" --arg endpoint "$MODEL_ENDPOINT" --arg deployment "$MODEL_DEPLOYMENT" \
    --argjson images "$IMAGES" \
    --slurpfile foundation "$OUT/foundation.json" '{
    "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
    contentVersion: "1.0.0.0",
    parameters: {
      prefix: {value: $prefix}, environment: {value: $environment}, location: {value: $location},
      agentSelection: {value: $agent}, images: {value: $images},
      environmentName: {value: $foundation[0].environmentName.value},
      registryServer: {value: $foundation[0].registryServer.value},
      openAiEndpoint: {value: $endpoint}, openAiDeployment: {value: $deployment}
    }}' > "$OUT/applications.parameters.json"
}

uv sync --locked
az bicep build --file infra/foundation.bicep --outfile "$OUT/foundation.arm.json"
az bicep build --file infra/applications.bicep --outfile "$OUT/applications.arm.json"
FOUNDATION_PARAMS=(
  "prefix=$PREFIX" "environment=$ENVIRONMENT" "location=$LOCATION"
  "openAiResourceId=$MODEL_ID"
)
GROUP_EXISTS="$(az group exists --name "$RESOURCE_GROUP" --subscription "$SUBSCRIPTION")"
printf 'Subscription: %s\nTenant: %s\nResource group: %s\nRegion: %s\nAgents: %s\n' \
  "$SUBSCRIPTION" "$TENANT" "$RESOURCE_GROUP" "$LOCATION" "$AGENT"
printf 'Foundry project: %s (account: %s, %s)\nInference endpoint: %s\nDeployment: %s\n' \
  "$FOUNDRY_PROJECT_ENDPOINT" "$MODEL_ACCOUNT" "$MODEL_KIND" "$MODEL_ENDPOINT" "$MODEL_DEPLOYMENT"
printf 'Azure permissions: deployment/resource writes and role-assignment writes on the app RG and model account.\n'
printf 'Teams permissions: custom app upload or administrator publication; handled separately.\n'

if [[ "$GROUP_EXISTS" == true ]]; then
  az deployment group what-if --subscription "$SUBSCRIPTION" --resource-group "$RESOURCE_GROUP" \
    --template-file infra/foundation.bicep --parameters "${FOUNDATION_PARAMS[@]}"
else
  printf 'Resource group does not exist. Bicep compiled; cloud what-if requires the group.\n'
fi
if [[ "$APPLY" == false ]]; then
  if [[ -n "$IMAGES_JSON" && "$GROUP_EXISTS" == true ]]; then
    az deployment group show --subscription "$SUBSCRIPTION" --resource-group "$RESOURCE_GROUP" \
      --name "$PREFIX-$ENVIRONMENT-foundation" --query properties.outputs -o json > "$OUT/foundation.json"
    SERVER="$(jq -er '.registryServer.value' "$OUT/foundation.json")"
    IMAGES='{}'
    for agent in "${AGENTS[@]}"; do
      image="$(jq -er --arg agent "$agent" '.[$agent]' "$IMAGES_JSON")"
      validate_image "$image" "$agent"
      IMAGES="$(jq -n --argjson images "$IMAGES" --arg agent "$agent" --arg image "$image" \
        '$images + {($agent): $image}')"
    done
    write_application_parameters
    az deployment group what-if --subscription "$SUBSCRIPTION" --resource-group "$RESOURCE_GROUP" \
      --template-file infra/applications.bicep --parameters "@$OUT/applications.parameters.json"
  else
    printf 'Application what-if needs an existing foundation and --images-json.\n'
  fi
  printf 'Plan only. No Azure resources were changed and no images were built.\n'
  exit 0
fi
printf 'Apply mode: proceeding with resource writes and builds without confirmation prompts.\n'
if [[ "$GROUP_EXISTS" != true ]]; then
  az group create --subscription "$SUBSCRIPTION" --name "$RESOURCE_GROUP" --location "$LOCATION" >/dev/null
  az deployment group what-if --subscription "$SUBSCRIPTION" --resource-group "$RESOURCE_GROUP" \
    --template-file infra/foundation.bicep --parameters "${FOUNDATION_PARAMS[@]}"
fi
az deployment group create --subscription "$SUBSCRIPTION" --resource-group "$RESOURCE_GROUP" \
  --name "$PREFIX-$ENVIRONMENT-foundation" --template-file infra/foundation.bicep \
  --parameters "${FOUNDATION_PARAMS[@]}" --query properties.outputs -o json > "$OUT/foundation.json"
REGISTRY="$(jq -er '.registryName.value' "$OUT/foundation.json")"
SERVER="$(jq -er '.registryServer.value' "$OUT/foundation.json")"
IMAGES='{}'
for agent in "${AGENTS[@]}"; do
  if [[ -n "$IMAGES_JSON" ]]; then
    image="$(jq -er --arg agent "$agent" '.[$agent]' "$IMAGES_JSON")"
    validate_image "$image" "$agent"
  else
    # Refuse to overwrite tags: tags are build identifiers, deployment uses immutable digests.
    tags="$(az acr repository list --name "$REGISTRY" --subscription "$SUBSCRIPTION" -o json)"
    if jq -e --arg agent "$agent" 'index($agent) != null' <<<"$tags" >/dev/null; then
      existing="$(az acr repository show-tags --name "$REGISTRY" --repository "$agent" \
        --subscription "$SUBSCRIPTION" -o json)"
      if jq -e --arg tag "$IMAGE_TAG" 'index($tag) != null' <<<"$existing" >/dev/null; then
        fail "Image tag $agent:$IMAGE_TAG already exists. Use a new tag or --images-json."
      fi
    fi
    az acr build --registry "$REGISTRY" --subscription "$SUBSCRIPTION" \
      --image "$agent:$IMAGE_TAG" --file "agents/$agent/Dockerfile" --platform linux/amd64 .
    digest="$(az acr repository show --name "$REGISTRY" --image "$agent:$IMAGE_TAG" \
      --subscription "$SUBSCRIPTION" --query digest -o tsv)"
    [[ "$digest" =~ ^sha256:[a-f0-9]{64}$ ]] || fail "Registry returned an invalid digest"
    image="$SERVER/$agent@$digest"
  fi
  IMAGES="$(jq -n --argjson images "$IMAGES" --arg agent "$agent" --arg image "$image" \
    '$images + {($agent): $image}')"
done
printf '%s\n' "$IMAGES" > "$OUT/images.json"

write_application_parameters
az deployment group what-if --subscription "$SUBSCRIPTION" --resource-group "$RESOURCE_GROUP" \
  --template-file infra/applications.bicep --parameters "@$OUT/applications.parameters.json"
PREVIOUS='{}'
for agent in "${AGENTS[@]}"; do
  name="$PREFIX-$ENVIRONMENT-$agent"
  resources="$(az resource list --subscription "$SUBSCRIPTION" --resource-group "$RESOURCE_GROUP" \
    --resource-type Microsoft.App/containerApps --query '[].name' -o json)"
  if jq -e --arg name "$name" 'index($name) != null' <<<"$resources" >/dev/null; then
    old="$(az containerapp show --subscription "$SUBSCRIPTION" --resource-group "$RESOURCE_GROUP" \
      --name "$name" --query 'properties.template.containers[0].image' -o tsv)"
    PREVIOUS="$(jq -n --argjson previous "$PREVIOUS" --arg agent "$agent" --arg image "$old" \
      '$previous + {($agent): $image}')"
  fi
done
printf '%s\n' "$PREVIOUS" > "$OUT/previous-images.json"

# Retry only recognized identity/RBAC propagation failures, never arbitrary deployment errors.
for attempt in 1 2 3; do
  if az deployment group create --subscription "$SUBSCRIPTION" --resource-group "$RESOURCE_GROUP" \
    --name "$PREFIX-$ENVIRONMENT-applications" --template-file infra/applications.bicep \
    --parameters "@$OUT/applications.parameters.json" --query properties.outputs -o json \
    > "$OUT/applications.json" 2> "$OUT/deploy-error.log"; then
    break
  fi
  cat "$OUT/deploy-error.log" >&2
  if [[ "$attempt" == 3 ]] || ! grep -Eq 'AcrPull|UNAUTHORIZED|AuthorizationFailed|InvalidIdentity' "$OUT/deploy-error.log"; then
    fail "Application deployment failed; inspect $OUT/deploy-error.log"
  fi
  printf 'Identity propagation not complete; retrying in 30 seconds.\n' >&2
  sleep 30
done
for agent in "${AGENTS[@]}"; do
  fqdn="$(jq -er --arg agent "$agent" '.agents.value[] | select(.name == $agent) | .fqdn' "$OUT/applications.json")"
  bot_id="$(jq -er --arg agent "$agent" '.agents.value[] | select(.name == $agent) | .botClientId' "$OUT/applications.json")"
  healthy=false
  printf 'Checking %s readiness: https://%s/health/ready (up to 60 attempts).\n' "$agent" "$fqdn"
  for attempt in {1..60}; do
    if curl -fsS --connect-timeout 5 --max-time 10 "https://$fqdn/health/ready" > "$OUT/$agent-health.json"; then
      if jq -e '.status == "ready"' "$OUT/$agent-health.json" >/dev/null; then healthy=true; break; fi
    fi
    printf '%s readiness attempt %s/60 failed; retrying in 5 seconds.\n' "$agent" "$attempt" >&2
    sleep 5
  done
  [[ "$healthy" == true ]] || fail "$agent failed readiness: inspect ACA revision/logs"
  printf '%s is ready. Checking anonymous messaging requests are rejected.\n' "$agent"
  status="$(curl -sS --connect-timeout 5 --max-time 10 -o /dev/null -w '%{http_code}' \
    -X POST -H 'Content-Type: application/json' -d '{}' "https://$fqdn/api/messages")"
  [[ "$status" == 401 ]] || fail "$agent messaging endpoint must reject anonymous requests (got $status)"
  uv run --locked scripts/package_teams.py --agent "$agent" --bot-id "$bot_id" \
    --domain "$fqdn" --subscription "$SUBSCRIPTION" --resource-group "$RESOURCE_GROUP" \
    --prefix "$PREFIX" --environment "$ENVIRONMENT" --version "$APP_VERSION" --output "$OUT"
  printf '%s messaging endpoint: https://%s/api/messages\n' "$agent" "$fqdn"
done
printf 'Deployment and packaging completed. Teams and Copilot round-trips still require app installation.\n'
printf 'ZIPs in %s support both personal Teams chat and Microsoft 365 Copilot.\n' "$OUT"
printf 'Publish through Microsoft 365 admin center > Agents > All agents > Add agent (or Upload custom agent).\n'
printf 'For permitted Teams sideloading and organization app publishing, see README.md sections "Install in Teams" and "Publish in Microsoft 365 Copilot".\n'
