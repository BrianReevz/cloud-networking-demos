#!/usr/bin/env bash
# ============================================================================
# Full Demo Reset Script:
# 1. Resets Vendor Catalog (localhost:8001 + Cloud Run xvantage-catalog-mcp) to MSRP ($450.00, no active promo)
# 2. Resets Internal Quotes DB (Cloud Run internal-quotes-mcp) so Quote #1042 is back to OPEN ($450 cost / $540 price)
# 3. Wipes 3-Legged OAuth Authorization in Gemini Enterprise by DELETING the old Agent registration
#    and all old Authorization resources, then creating a fresh Authorization + Agent registration.
# ============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "${SCRIPT_DIR}/.env" ]]; then
  # shellcheck disable=SC1091
  source "${SCRIPT_DIR}/.env"
fi

PROJECT_ID="${PROJECT_ID:-gemini-enterprise-demo-508317}"
PROJECT_NUMBER="${PROJECT_NUMBER:-229571756001}"
ENGINE_ID="${ENGINE_ID:-gemini-enterprise-17891492_1789149243837}"
ASSISTANTS_BASE="https://discoveryengine.googleapis.com/v1alpha/projects/${PROJECT_NUMBER}/locations/global/collections/default_collection/engines/${ENGINE_ID}/assistants/default_assistant"
COPILOT_RE_NAME="projects/${PROJECT_NUMBER}/locations/us-central1/reasoningEngines/1986847747960995840"
OAUTH_CLIENT_ID="${OAUTH_CLIENT_ID:?Set OAUTH_CLIENT_ID in environment or demos/hybrid-oauth-agentic-demo/.env}"
OAUTH_CLIENT_SECRET="${OAUTH_CLIENT_SECRET:?Set OAUTH_CLIENT_SECRET in environment or demos/hybrid-oauth-agentic-demo/.env}"

if [[ "${1:-}" != "--oauth-only" ]]; then
  echo "=== 1. Resetting Vendor Catalog (localhost:8001 & Cloud Run xvantage-catalog-mcp) to MSRP (\$450.00) ==="
  curl -s -X POST "http://localhost:8001/api/v1/demo/reset" >/dev/null || true
  curl -s -X POST "https://xvantage-catalog-mcp-fntvjdmgca-uc.a.run.app/api/v1/demo/reset" >/dev/null || true
  echo "✔ Vendor Catalog reset to \$450.00 MSRP (0 active promotions)."

  echo "=== 2. Resetting Quote #1042 on Cloud Run internal-quotes-mcp back to OPEN ==="
  curl -s -X POST "https://internal-quotes-mcp-fntvjdmgca-uc.a.run.app/api/v1/demo/reset" >/dev/null || true
  echo "✔ Quote #1042 reset to OPEN (\$450.00 wholesale / \$540.00 unit sell price)."
fi

echo "=== 3. Wiping & Recreating 3-Legged OAuth Authorization on Deal Desk Quote Copilot in Gemini Enterprise ==="
TOKEN="$(gcloud auth application-default print-access-token 2>/dev/null || gcloud auth print-access-token)"

# 3a. Delete existing Deal Desk Quote Copilot agent registration(s) so Discovery Engine drops the cached user-agent session
EXISTING_AGENTS=$(curl -s -H "Authorization: Bearer ${TOKEN}" -H "X-Goog-User-Project: ${PROJECT_ID}" "${ASSISTANTS_BASE}/agents" | python3 -c '
import sys, json
data = json.load(sys.stdin)
for a in data.get("agents", []):
    if "Deal Desk Quote Copilot" in a.get("displayName", ""):
        print(a["name"])
')
for AGENT_NAME in $EXISTING_AGENTS; do
  curl -s -X DELETE -H "Authorization: Bearer ${TOKEN}" -H "X-Goog-User-Project: ${PROJECT_ID}" \
    "https://discoveryengine.googleapis.com/v1alpha/${AGENT_NAME}" >/dev/null || true
done

# 3b. Delete all old quote-copilot-agent_* Authorization resources to wipe stored OAuth tokens in Discovery Engine
OLD_AUTHS=$(curl -s -H "Authorization: Bearer ${TOKEN}" -H "X-Goog-User-Project: ${PROJECT_ID}" \
  "https://discoveryengine.googleapis.com/v1alpha/projects/${PROJECT_NUMBER}/locations/global/authorizations" | python3 -c '
import sys, json
data = json.load(sys.stdin)
for auth in data.get("authorizations", []):
    if "quote-copilot-agent" in auth.get("name", ""):
        print(auth["name"])
')
for AUTH_NAME in $OLD_AUTHS; do
  curl -s -X DELETE -H "Authorization: Bearer ${TOKEN}" -H "X-Goog-User-Project: ${PROJECT_ID}" \
    "https://discoveryengine.googleapis.com/v1alpha/${AUTH_NAME}" >/dev/null || true
done

# 3c. Create fresh Authorization resource with prompt=consent and recreate the Deal Desk Quote Copilot agent
NEW_AUTH_ID="quote-copilot-agent_$(date +%s)"
NEW_AUTH_RESOURCE="projects/${PROJECT_NUMBER}/locations/global/authorizations/${NEW_AUTH_ID}"
AUTH_URI="https://accounts.google.com/o/oauth2/v2/auth?client_id=${OAUTH_CLIENT_ID}&redirect_uri=https%3A%2F%2Fvertexaisearch.cloud.google.com%2Foauth-redirect&scope=https://www.googleapis.com/auth/cloud-platform%20openid%20email&include_granted_scopes=true&response_type=code&access_type=offline&prompt=consent"

curl -s -X POST \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -H "X-Goog-User-Project: ${PROJECT_ID}" \
  "https://discoveryengine.googleapis.com/v1alpha/projects/${PROJECT_NUMBER}/locations/global/authorizations?authorizationId=${NEW_AUTH_ID}" \
  -d "{
    \"name\": \"${NEW_AUTH_RESOURCE}\",
    \"displayName\": \"${NEW_AUTH_ID}\",
    \"serverSideOauth2\": {
      \"clientId\": \"${OAUTH_CLIENT_ID}\",
      \"clientSecret\": \"${OAUTH_CLIENT_SECRET}\",
      \"authorizationUri\": \"${AUTH_URI}\",
      \"tokenUri\": \"https://oauth2.googleapis.com/token\"
    }
  }" >/dev/null

NEW_AGENT=$(curl -s -X POST \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -H "X-Goog-User-Project: ${PROJECT_ID}" \
  "${ASSISTANTS_BASE}/agents" \
  -d "{
    \"displayName\": \"Deal Desk Quote Copilot (3-Legged Auth)\",
    \"description\": \"3-Legged OAuth User-Delegated Deal Desk Copilot. Enforces tenant isolation and requires interactive human confirmation before repricing quotes.\",
    \"adkAgentDefinition\": {
      \"toolSettings\": {
        \"toolDescription\": \"3-Legged OAuth Deal Desk Copilot for reading and repricing internal customer quotes\"
      },
      \"provisionedReasoningEngine\": {
        \"reasoningEngine\": \"${COPILOT_RE_NAME}\"
      }
    },
    \"authorizationConfig\": {
      \"toolAuthorizations\": [
        \"${NEW_AUTH_RESOURCE}\"
      ]
    }
  }" | python3 -c 'import sys, json; print(json.load(sys.stdin).get("name", ""))')

echo "✔ Wiped old OAuth tokens and recreated Deal Desk Quote Copilot (${NEW_AGENT}) with Authorization ${NEW_AUTH_ID}."
echo "👉 IMPORTANT: Hard-refresh (Cmd+R / F5) your Gemini Enterprise browser tab so the UI loads the new agent registration!"
