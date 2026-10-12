#!/usr/bin/env python3
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0

"""Deploy & Register Hybrid OAuth MCPs and Agents in Agent Registry, Vertex AI Agent Engine, and Gemini Enterprise.

Patterned directly on `demos/agent-gateway/src/mortgage-agent/deploy_agent.py`.

Performs:
1. Registers `xvantage-catalog-mcp` (localhost:8001 + Cloud Run relay) and `internal-quotes-mcp` (Cloud Run)
   in Agent Registry (`agentregistry.googleapis.com/v1alpha`) with their `toolspec.json` definitions.
2. Deploys Agent 1: `promo-sentinel-agent` (2-Legged Agent Identity, `identity_type=AGENT_IDENTITY`)
   to Vertex AI Agent Engine (`us-central1`), grants read-only IAP egress (`readOnlyHint == true`)
   via `grant_agent_mcp_egress.sh`, and registers it in Gemini Enterprise (`app_id=92324c1c-dfb4-463b-8e83-f72467d417a5`).
3. Deploys Agent 2: `quote-copilot-agent` (3-Legged User OAuth Delegation) to Vertex AI Agent Engine (`us-central1`),
   grants full IAP egress, creates the 3-Legged `serverSideOauth2` authorization resource in Discovery Engine,
   and registers it in Gemini Enterprise (`app_id=92324c1c-dfb4-463b-8e83-f72467d417a5`).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
AGENT_GATEWAY_DIR = BASE_DIR.parent / "agent-gateway"


def _get_gcp_access_token(project: str) -> str:
    env_token = os.environ.get("CLOUDSDK_AUTH_ACCESS_TOKEN", "").strip()
    if env_token:
        return env_token
    try:
        out = subprocess.check_output(
            ["gcloud", "auth", "application-default", "print-access-token"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        if out:
            os.environ["CLOUDSDK_AUTH_ACCESS_TOKEN"] = out
            return out
    except Exception:
        pass
    import google.auth
    import google.auth.transport.requests

    credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    credentials.refresh(google.auth.transport.requests.Request())
    return credentials.token


def register_mcp_server_in_agent_registry(
    *,
    project: str,
    location: str,
    service_id: str,
    display_name: str,
    description: str,
    mcp_url: str,
    toolspec_path: Path,
) -> dict:
    """Register or update an MCP server in Google Cloud Agent Registry (`agentregistry.googleapis.com/v1alpha`)."""
    access_token = _get_gcp_access_token(project)
    base_url = f"https://agentregistry.googleapis.com/v1alpha/projects/{project}/locations/{location}"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "X-Goog-User-Project": project,
    }

    toolspec_obj = json.loads(toolspec_path.read_text(encoding="utf-8"))
    body = json.dumps(
        {
            "displayName": display_name,
            "description": description,
            "interfaces": [
                {
                    "url": mcp_url,
                    "protocolBinding": "JSONRPC",
                }
            ],
            "mcpServerSpec": {
                "type": "TOOL_SPEC",
                "content": toolspec_obj,
            },
        }
    ).encode("utf-8")

    # Delete existing service_id if present to ensure clean registration
    del_url = f"{base_url}/services/{service_id}"
    try:
        del_req = urllib.request.Request(del_url, headers=headers, method="DELETE")
        with urllib.request.urlopen(del_req) as resp:
            resp.read()
        time.sleep(2)
    except urllib.error.HTTPError:
        pass

    create_url = f"{base_url}/services?serviceId={service_id}"
    print(f"Registering MCP server '{service_id}' ({mcp_url}) in Agent Registry ({project}/{location})...")
    req = urllib.request.Request(create_url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read())
        print(f"  ✓ Registered in Agent Registry: {result.get('name', service_id)}")
        return result


def register_agent_in_gemini_enterprise(
    *,
    project: str,
    app_id: str,
    agent_name: str,
    display_name: str,
    description: str,
    reasoning_engine_name: str,
    auth_mode: str,
    oauth_client_id: str | None = None,
    oauth_client_secret: str | None = None,
    authorization_uri: str | None = None,
    token_uri: str | None = None,
) -> str:
    """Register a Vertex AI Agent Engine reasoningEngine in Gemini Enterprise (`discoveryengine.googleapis.com`)."""
    if app_id == "92324c1c-dfb4-463b-8e83-f72467d417a5":
        app_id = "gemini-enterprise-17891492_1789149243837"
    access_token = _get_gcp_access_token(project)
    base_url = f"https://global-discoveryengine.googleapis.com/v1alpha/projects/{project}/locations/global"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "X-Goog-User-Project": project,
    }

    agents_url = f"{base_url}/collections/default_collection/engines/{app_id}/assistants/default_assistant/agents"
    print(f"Checking for existing '{display_name}' in Gemini Enterprise engine '{app_id}'...")
    list_req = urllib.request.Request(agents_url, headers=headers)
    try:
        with urllib.request.urlopen(list_req) as resp:
            list_resp = json.loads(resp.read())
            for existing in list_resp.get("agents", []):
                if existing.get("displayName") == display_name:
                    existing_name = existing["name"]
                    print(f"  Deleting existing agent registration: {existing_name}...")
                    del_req = urllib.request.Request(
                        f"https://global-discoveryengine.googleapis.com/v1alpha/{existing_name}",
                        headers=headers,
                        method="DELETE",
                    )
                    with urllib.request.urlopen(del_req) as del_resp:
                        del_resp.read()
    except urllib.error.HTTPError as exc:
        print(f"  Note while listing existing agents: {exc.code}")

    auth_resource_name = None
    if auth_mode == "3_legged_oauth" and oauth_client_id and oauth_client_secret:
        auth_id = f"{agent_name}_{int(time.time() * 1000)}"
        auth_url = f"{base_url}/authorizations?authorizationId={auth_id}"
        eff_auth_uri = authorization_uri or (
            "https://accounts.google.com/o/oauth2/v2/auth"
            f"?client_id={oauth_client_id}"
            "&redirect_uri=https%3A%2F%2Fvertexaisearch.cloud.google.com%2Foauth-redirect"
            "&scope=https://www.googleapis.com/auth/cloud-platform%20openid%20email"
            "&include_granted_scopes=true"
            "&response_type=code"
            "&access_type=offline"
            "&prompt=consent"
        )
        eff_token_uri = token_uri or "https://oauth2.googleapis.com/token"
        auth_body = json.dumps(
            {
                "displayName": auth_id,
                "serverSideOauth2": {
                    "clientId": oauth_client_id,
                    "clientSecret": oauth_client_secret,
                    "tokenUri": eff_token_uri,
                    "authorizationUri": eff_auth_uri,
                },
            }
        ).encode("utf-8")

        print(f"Creating 3-Legged OAuth authorization '{auth_id}' in Discovery Engine...")
        auth_req = urllib.request.Request(auth_url, data=auth_body, headers=headers, method="POST")
        with urllib.request.urlopen(auth_req) as resp:
            auth_resp = json.loads(resp.read())
            auth_resource_name = auth_resp.get("name", f"projects/{project}/locations/global/authorizations/{auth_id}")
            print(f"  ✓ 3-Legged Authorization created: {auth_resource_name}")

    agent_payload: dict = {
        "displayName": display_name,
        "description": description,
        "adk_agent_definition": {
            "provisioned_reasoning_engine": {
                "reasoning_engine": reasoning_engine_name,
            },
        },
        "sharingConfig": {
            "scope": "ALL_USERS",
        },
        "agentInvocationSpec": {
            "invocationMode": "AUTOMATIC",
        },
    }
    if auth_resource_name:
        agent_payload["authorization_config"] = {
            "tool_authorizations": [auth_resource_name],
        }

    print(f"Registering '{display_name}' ({auth_mode}) in Gemini Enterprise app '{app_id}'...")
    agent_req = urllib.request.Request(
        agents_url,
        data=json.dumps(agent_payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(agent_req) as resp:
        agent_resp = json.loads(resp.read())
        reg_name = agent_resp.get("name", "unknown")
        print(f"  ✓ Registered in Gemini Enterprise: {reg_name}")
        return reg_name


def deploy_adk_agent_to_agent_engine(
    *,
    agent_dir: Path,
    project: str,
    region: str,
    staging_bucket: str,
    display_name: str,
    description: str,
    mcp_invoker_sa: str,
    org_id: str,
    project_number: str,
    readonly_iap_condition: bool = False,
) -> str:
    """Package and deploy an ADK agent directory to Vertex AI Agent Engine (`reasoningEngines`) with AGENT_IDENTITY."""
    import vertexai
    from vertexai import agent_engines

    sys.path.insert(0, str(agent_dir))
    try:
        import importlib

        if "agent" in sys.modules:
            del sys.modules["agent"]
        if "agent.agent" in sys.modules:
            del sys.modules["agent.agent"]
        if "agent.tools" in sys.modules:
            del sys.modules["agent.tools"]
        agent_mod = importlib.import_module("agent.agent")
        root_agent = agent_mod.root_agent
    finally:
        sys.path.pop(0)

    vertexai.init(project=project, location=region, staging_bucket=staging_bucket)
    client = vertexai.Client(project=project, location=region)
    app = agent_engines.AdkApp(agent=root_agent, enable_tracing=True)

    staging_dir = tempfile.mkdtemp(prefix=f"{agent_dir.name}_deploy_")
    original_cwd = os.getcwd()
    try:
        shutil.copytree(str(agent_dir / "agent"), os.path.join(staging_dir, "agent"))
        scripts_dir = os.path.join(staging_dir, "installation_scripts")
        os.makedirs(scripts_dir, exist_ok=True)
        script_path = os.path.join(scripts_dir, "create_venv.sh")
        with open(script_path, "w", encoding="utf-8") as fh:
            fh.write(
                "#!/bin/bash\nset -e\n"
                "PYTHON3=$(which python3)\n"
                "PY_VER=$(python3 -c 'import sys; print(f\"{sys.version_info.major}.{sys.version_info.minor}\")')\n"
                "mkdir -p /code/.venv/bin /code/.venv/lib/python${PY_VER}/site-packages\n"
                'ln -sf "$PYTHON3" /code/.venv/bin/python\n'
                'ln -sf "$PYTHON3" /code/.venv/bin/python3\n'
                "cat > /code/.venv/pyvenv.cfg << PYCFG\n"
                "home = $(dirname $PYTHON3)\n"
                "include-system-site-packages = true\n"
                "PYCFG\n"
            )
        os.chmod(script_path, stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP)
        os.chdir(staging_dir)

        deploy_config = dict(
            staging_bucket=staging_bucket,
            requirements=[
                "google-cloud-aiplatform[agent_engines]>=1.149.0,<1.154.0",
                "google-adk[a2a,agent-identity]==1.34.0",
                "google-auth>=2.0",
                "cloudpickle",
                "pydantic",
                "httpx>=0.27.0",
            ],
            extra_packages=["agent", "installation_scripts/create_venv.sh"],
            build_options={"installation_scripts": ["installation_scripts/create_venv.sh"]},
            env_vars={
                "ADK_ENABLE_MCP_GRACEFUL_ERROR_HANDLING": "true",
                "GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY": "true",
                "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "true",
                "GOOGLE_API_PREVENT_AGENT_TOKEN_SHARING_FOR_GCP_SERVICES": "false",
                "GOOGLE_GENAI_USE_VERTEXAI": "True",
                "GOOGLE_CLOUD_LOCATION": "global",
                "MODEL_NAME": "gemini-2.5-flash",
                "MCP_REGISTRY_PROJECT": project,
                "MCP_REGISTRY_LOCATION": region,
                "MCP_INVOKER_SA_EMAIL": mcp_invoker_sa,
            },
            display_name=display_name,
            description=description,
            min_instances=1,
            resource_limits={"cpu": "4", "memory": "8Gi"},
        )

        print(f"\nStep 1: Creating AGENT_IDENTITY shell for '{display_name}'...")
        engine = client.agent_engines.create(
            config={
                "display_name": display_name,
                "description": description,
                "identity_type": "AGENT_IDENTITY",
            }
        )
        reasoning_engine_name = engine.api_resource.name
        agent_id = reasoning_engine_name.split("/")[-1]
        print(f"  ✓ Created Reasoning Engine Identity Shell: {reasoning_engine_name}")

        # Grant IAP Egress via grant_agent_mcp_egress.sh
        grant_script = AGENT_GATEWAY_DIR / "scripts" / "grant_agent_mcp_egress.sh"
        if grant_script.exists():
            env = os.environ.copy()
            env.update(
                {
                    "PROJECT_ID": project,
                    "PROJECT_NUMBER": project_number,
                    "ORG_ID": org_id,
                    "REGION": region,
                }
            )
            cmd = [str(grant_script), "--mcp", "--agent-id", agent_id]
            if readonly_iap_condition:
                cmd.extend(
                    [
                        "--condition-title",
                        "Sentinel2LeggedReadOnlyToolsOnly",
                        "--condition-expression",
                        "api.getAttribute('iap.googleapis.com/mcp.tool.isReadOnly', false) == true",
                    ]
                )
            print(f"Step 2: Applying IAP REQUEST_AUTHZ egress policy (readonly={readonly_iap_condition})...")
            subprocess.run(cmd, env=env, check=False)

        print(f"Step 3: Deploying ADK application bundle to {reasoning_engine_name}...")
        engine = client.agent_engines.update(name=reasoning_engine_name, agent=app, config=deploy_config)
        return engine.api_resource.name
    finally:
        os.chdir(original_cwd)
        shutil.rmtree(staging_dir, ignore_errors=True)


def _load_local_env() -> None:
    env_file = BASE_DIR / ".env"
    if not env_file.exists():
        return
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def main() -> None:
    _load_local_env()
    parser = argparse.ArgumentParser(
        description="Deploy Hybrid 2-Legged & 3-Legged Agents and MCPs to Agent Registry & Gemini Enterprise"
    )
    parser.add_argument("--project", default=os.getenv("PROJECT_ID", "gemini-enterprise-demo-508317"))
    parser.add_argument("--region", default=os.getenv("REGION", "us-central1"))
    parser.add_argument("--org-id", default=os.getenv("ORG_ID", "727532393300"))
    parser.add_argument("--app-id", default=os.getenv("GE_APP_ID", "92324c1c-dfb4-463b-8e83-f72467d417a5"))
    parser.add_argument("--quotes-mcp-url", required=True, help="Cloud Run URL for internal-quotes-mcp")
    parser.add_argument(
        "--catalog-mcp-url",
        default="http://localhost:8001/mcp",
        help="URL for xvantage-catalog-mcp (localhost:8001/mcp or Cloud Run mirror URL)",
    )
    parser.add_argument(
        "--oauth-client-id",
        default=os.getenv("OAUTH_CLIENT_ID", ""),
    )
    parser.add_argument(
        "--oauth-client-secret",
        default=os.getenv("OAUTH_CLIENT_SECRET", ""),
    )
    args = parser.parse_args()

    _get_gcp_access_token(args.project)
    res = subprocess.run(
        ["gcloud", "projects", "describe", args.project, "--format=value(projectNumber)"],
        capture_output=True,
        text=True,
        check=True,
    )
    project_number = res.stdout.strip()
    staging_bucket = f"gs://{args.project}-staging"
    mcp_invoker_sa = f"agent-mcp-invoker@{args.project}.iam.gserviceaccount.com"

    # 1. Register both MCP servers in Agent Registry
    register_mcp_server_in_agent_registry(
        project=args.project,
        location=args.region,
        service_id="xvantage-catalog-mcp",
        display_name="xvantage-catalog-mcp",
        description="Mock Xvantage Vendor Catalog & Active Promotions MCP Service (synced with localhost:8001)",
        mcp_url=args.catalog_mcp_url if args.catalog_mcp_url.endswith("/mcp") else f"{args.catalog_mcp_url}/mcp",
        toolspec_path=BASE_DIR / "bigredreeves-service" / "toolspec.json",
    )
    register_mcp_server_in_agent_registry(
        project=args.project,
        location=args.region,
        service_id="internal-quotes-mcp",
        display_name="internal-quotes-mcp",
        description="Internal Enterprise Quotes Repository MCP Service on Cloud Run",
        mcp_url=args.quotes_mcp_url if args.quotes_mcp_url.endswith("/mcp") else f"{args.quotes_mcp_url}/mcp",
        toolspec_path=BASE_DIR / "cloudrun-quotes-mcp" / "toolspec.json",
    )

    # 2. Deploy Agent 1: 2-Legged Promo Sentinel Agent
    sentinel_re = deploy_adk_agent_to_agent_engine(
        agent_dir=BASE_DIR / "agents" / "promo-sentinel-agent",
        project=args.project,
        region=args.region,
        staging_bucket=staging_bucket,
        display_name="Xvantage Promo Sentinel Agent (2-Legged Auth)",
        description="2-Legged OAuth (Agent Identity) Sentinel Agent that loops and watches Xvantage vendor promotions.",
        mcp_invoker_sa=mcp_invoker_sa,
        org_id=args.org_id,
        project_number=project_number,
        readonly_iap_condition=True,
    )
    register_agent_in_gemini_enterprise(
        project=args.project,
        app_id=args.app_id,
        agent_name="promo-sentinel-agent",
        display_name="Xvantage Promo Sentinel Agent (2-Legged Auth)",
        description="2-Legged OAuth (Agent Identity) Sentinel Agent that watches Xvantage catalog promotions and matches eligible open quotes.",
        reasoning_engine_name=sentinel_re,
        auth_mode="2_legged_agent_identity",
    )

    # 3. Deploy Agent 2: 3-Legged Deal Desk Quote Copilot
    copilot_re = deploy_adk_agent_to_agent_engine(
        agent_dir=BASE_DIR / "agents" / "quote-copilot-agent",
        project=args.project,
        region=args.region,
        staging_bucket=staging_bucket,
        display_name="Deal Desk Quote Copilot (3-Legged Auth)",
        description="3-Legged OAuth Deal Desk Copilot that reads and reprices quotes using Brian's delegated identity.",
        mcp_invoker_sa=mcp_invoker_sa,
        org_id=args.org_id,
        project_number=project_number,
        readonly_iap_condition=False,
    )
    register_agent_in_gemini_enterprise(
        project=args.project,
        app_id=args.app_id,
        agent_name="quote-copilot-agent",
        display_name="Deal Desk Quote Copilot (3-Legged Auth)",
        description="3-Legged User-Delegated OAuth Copilot that analyzes vendor promotions and reprices Quote #1042 for Acme Corp.",
        reasoning_engine_name=copilot_re,
        auth_mode="3_legged_oauth",
        oauth_client_id=args.oauth_client_id,
        oauth_client_secret=args.oauth_client_secret,
    )

    print("\n" + "=" * 82)
    print(" SUCCESS: BOTH AGENTS & MCP SERVERS REGISTERED IN AGENT REGISTRY & GEMINI ENTERPRISE")
    print(f"  - Sentinel Agent (2-Legged): {sentinel_re}")
    print(f"  - Quote Copilot (3-Legged):  {copilot_re}")
    print(f"  - Gemini Enterprise Console: https://vertexaisearch.cloud.google.com/home/cid/{args.app_id}")
    print("=" * 82)


if __name__ == "__main__":
    main()
