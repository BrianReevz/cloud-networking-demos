#!/usr/bin/env bash
# Copyright 2026 Google LLC
#
# End-to-End Deployment Script for Multi-Agent Hybrid OAuth Demo on Gemini Enterprise
# Target Project: gemini-enterprise-demo-508317 (Org: 727532393300 / brianreevz.altostrat.com)
# Target Gemini Enterprise App ID: 92324c1c-dfb4-463b-8e83-f72467d417a5

set -euo pipefail

export PROJECT_ID="${PROJECT_ID:-gemini-enterprise-demo-508317}"
export REGION="${REGION:-us-central1}"
export ORG_ID="${ORG_ID:-727532393300}"
export GE_APP_ID="${GE_APP_ID:-92324c1c-dfb4-463b-8e83-f72467d417a5}"
export ADMIN_ACCOUNT="${ADMIN_ACCOUNT:-admin@brianreevz.altostrat.com}"

echo "================================================================================"
echo " Deploying Hybrid 2-Legged & 3-Legged Agents to Gemini Enterprise"
echo " Project: ${PROJECT_ID} | Region: ${REGION} | GE App: ${GE_APP_ID}"
echo "================================================================================"

# Step 0: Use ADC access token for all gcloud and Python calls
ADC_TOK="$(gcloud auth application-default print-access-token 2>/dev/null || true)"
if [[ -n "${ADC_TOK}" ]]; then
  export CLOUDSDK_AUTH_ACCESS_TOKEN="${ADC_TOK}"
fi

if ! gcloud projects describe "${PROJECT_ID}" >/dev/null 2>&1; then
  echo ""
  echo "[!] Current gcloud session does not have access to ${PROJECT_ID}."
  gcloud auth login "${ADMIN_ACCOUNT}" --update-adc --force
  export CLOUDSDK_AUTH_ACCESS_TOKEN="$(gcloud auth application-default print-access-token)"
fi

gcloud config set project "${PROJECT_ID}"

PROJECT_NUMBER="$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)')"
export PROJECT_NUMBER
echo "✓ Verified access to ${PROJECT_ID} (projectNumber=${PROJECT_NUMBER})"

# Step 1: Ensure staging bucket exists
STAGING_BUCKET="gs://${PROJECT_ID}-staging"
if ! gcloud storage buckets describe "${STAGING_BUCKET}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
  gcloud storage buckets create "${STAGING_BUCKET}" --project="${PROJECT_ID}" --location="${REGION}" --uniform-bucket-level-access
fi

# Step 2: Build & Push Docker images to Artifact Registry & Deploy to Cloud Run
AR_REPO="${REGION}-docker.pkg.dev/${PROJECT_ID}/gateway-docker"
echo ""
echo "Step 2: Building & pushing Cloud Run MCP images to ${AR_REPO}..."

echo "${CLOUDSDK_AUTH_ACCESS_TOKEN}" | docker login -u oauth2accesstoken --password-stdin "https://${REGION}-docker.pkg.dev"

docker build -t "${AR_REPO}/internal-quotes-mcp:latest" ./cloudrun-quotes-mcp
docker push "${AR_REPO}/internal-quotes-mcp:latest"

gcloud run deploy internal-quotes-mcp \
  --project="${PROJECT_ID}" \
  --region="${REGION}" \
  --image="${AR_REPO}/internal-quotes-mcp:latest" \
  --ingress=all \
  --no-allow-unauthenticated \
  --service-account="mcp-corporate-email@${PROJECT_ID}.iam.gserviceaccount.com" \
  --set-env-vars="GOOGLE_CLOUD_PROJECT=${PROJECT_ID},INTERNAL_DOMAIN=brianreevz.altostrat.com"

gcloud run services add-iam-policy-binding internal-quotes-mcp \
  --project="${PROJECT_ID}" \
  --region="${REGION}" \
  --member="serviceAccount:agent-mcp-invoker@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/run.invoker"

QUOTES_MCP_URL="$(gcloud run services describe internal-quotes-mcp --project="${PROJECT_ID}" --region="${REGION}" --format='value(status.url)')"
echo "✓ Deployed internal-quotes-mcp on Cloud Run: ${QUOTES_MCP_URL}"

# Step 3: Deploy Cloud Run relay/mirror for `xvantage-catalog-mcp` (synced from localhost:8001)
docker build -t "${AR_REPO}/xvantage-catalog-mcp:latest" ./bigredreeves-service
docker push "${AR_REPO}/xvantage-catalog-mcp:latest"

gcloud run deploy xvantage-catalog-mcp \
  --project="${PROJECT_ID}" \
  --region="${REGION}" \
  --image="${AR_REPO}/xvantage-catalog-mcp:latest" \
  --ingress=all \
  --no-allow-unauthenticated \
  --service-account="mcp-corporate-email@${PROJECT_ID}.iam.gserviceaccount.com" \
  --set-env-vars="BIND_HOST=0.0.0.0" \
  --port=8001

gcloud run services add-iam-policy-binding xvantage-catalog-mcp \
  --project="${PROJECT_ID}" \
  --region="${REGION}" \
  --member="serviceAccount:agent-mcp-invoker@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/run.invoker"

CATALOG_MCP_URL="$(gcloud run services describe xvantage-catalog-mcp --project="${PROJECT_ID}" --region="${REGION}" --format='value(status.url)')"
echo "✓ Deployed xvantage-catalog-mcp relay on Cloud Run: ${CATALOG_MCP_URL}"
echo "✓ Local Promo Trigger UI running on http://localhost:8001/demo-admin"

# Step 4: Register MCPs in Agent Registry & Deploy Both Agents to Agent Engine + Gemini Enterprise
echo ""
echo "Step 4: Registering MCPs in Agent Registry and deploying 2-Legged & 3-Legged Agents to Gemini Enterprise..."
uv run --directory ../agent-gateway/src/mortgage-agent python "${PWD}/deploy_hybrid_agents.py" \
  --project="${PROJECT_ID}" \
  --region="${REGION}" \
  --org-id="${ORG_ID}" \
  --app-id="${GE_APP_ID}" \
  --quotes-mcp-url="${QUOTES_MCP_URL}" \
  --catalog-mcp-url="${CATALOG_MCP_URL}"
