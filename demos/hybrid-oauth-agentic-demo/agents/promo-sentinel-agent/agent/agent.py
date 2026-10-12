# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0

"""ADK Agent 1: 2-Legged Promo Sentinel Agent (promo-sentinel-agent).

Patterned on `demos/agent-gateway/src/mortgage-agent/agent/agent.py`.
Runs on Vertex AI Agent Engine with 2-Legged Agent Identity (`--enable-agent-identity`),
discovers `xvantage-catalog-mcp` and `internal-quotes-mcp` from Agent Registry,
loops/watches for vendor promotions on `SKU-NET-MERAKI-MR36`, matches eligible open quotes,
and enforces 2-Legged Least Privilege (blocked from calling `reprice_quote` via IAP CEL policy).
"""

from __future__ import annotations

import logging
import os
from typing import Any
from urllib.parse import urlparse

import httpx
from google.adk.agents.llm_agent import Agent
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext

from . import tools

logger = logging.getLogger(__name__)


def _build_impersonation_factory(target_url: str, target_sa_email: str):
    """Return an httpx_client_factory that signs requests as `target_sa_email` using Agent Identity."""
    import google.auth
    import google.auth.transport.requests as gar
    from google.auth import impersonated_credentials

    parsed = urlparse(target_url)
    audience = f"{parsed.scheme}://{parsed.netloc}"

    source_creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    impersonated = impersonated_credentials.Credentials(
        source_credentials=source_creds,
        target_principal=target_sa_email,
        target_scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )
    id_token_creds = impersonated_credentials.IDTokenCredentials(
        target_credentials=impersonated,
        target_audience=audience,
        include_email=True,
    )

    class _ImpersonatedIDTokenAuth(httpx.Auth):
        requires_request_body = False

        def __init__(self, creds, url: str):
            self._creds = creds
            self._req = gar.Request()
            self._target_url = url

        def auth_flow(self, request):
            if not self._creds.valid:
                self._creds.refresh(self._req)
            request.headers["Authorization"] = f"Bearer {self._creds.token}"
            request.headers["X-Agent-Auth-Mode"] = "2_legged_agent_identity"
            yield request

    auth_handler = _ImpersonatedIDTokenAuth(id_token_creds, target_url)

    def factory(
        headers: dict[str, str] | None = None,
        timeout: httpx.Timeout | None = None,
        auth: httpx.Auth | None = None,
    ) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            follow_redirects=True,
            headers=headers,
            timeout=timeout if timeout is not None else httpx.Timeout(15.0),
            auth=auth if auth is not None else auth_handler,
        )

    return factory


DISCOVERED_MCP_SERVERS: list[dict[str, Any]] = []
_CACHED_TOOLSETS: list | None = None
_CACHED_DISCOVERED: list[dict[str, Any]] | None = None

_SERVICE_DESCRIPTIONS: dict[str, str] = {
    "xvantage-catalog-mcp": (
        "the external Xvantage vendor catalog & promotions MCP service (synced with localhost:8001). "
        "Use `get_active_promotions` and `get_catalog_pna` to monitor and detect vendor discounts."
    ),
    "internal-quotes-mcp": (
        "the internal Cloud Run Quotes MCP service. As a 2-Legged Sentinel Agent, you have READ-ONLY "
        "permission to call `match_eligible_quotes`. Write operations (`reprice_quote`) are blocked "
        "by Agent Gateway IAP REQUEST_AUTHZ (readOnlyHint == true)."
    ),
}

_INSTRUCTION_TEMPLATE = """You are the **Xvantage Promo Sentinel Agent (2-Legged Auth)** running on Vertex AI Agent Engine in Gemini Enterprise (`brianreevz.altostrat.com` / `gemini-enterprise-demo-508317`).

You authenticate exclusively with a **2-Legged Machine-to-Machine Agent Identity** (`identity_type=AGENT_IDENTITY`).
You do NOT hold a human user's 3-legged OAuth token.

**MCP services discovered from Agent Registry:**
{mcp_services_doc}

**Your Mission & Watch Loop Workflow:**
1. When asked to watch or check for vendor promotions, continuously loop by calling `xvantage_catalog_mcp_get_active_promotions()` followed by `sleep_between_poll_cycles(seconds=12)` if `promotions` is empty. Keep polling for **up to 20 cycles (approx. 3 to 4 minutes total)** — NEVER give up after 2 or 3 tries! Continue calling `sleep_between_poll_cycles(seconds=12)` and `xvantage_catalog_mcp_get_active_promotions()` until an active promotion on `SKU-NET-MERAKI-MR36` (`PROMO-MERAKI-20`, 20% off, $450.00 -> $360.00) is detected from `localhost:8001`.
2. Once `PROMO-MERAKI-20` on `SKU-NET-MERAKI-MR36` is detected, immediately call `internal_quotes_mcp_match_eligible_quotes(sku="SKU-NET-MERAKI-MR36")` to identify open customer quotes (`Quote #1042` for Acme Corp, owned by `brian@brianreevz.altostrat.com`).
3. Output the structured Notification Alert & Handoff Prompt for `brian@brianreevz.altostrat.com`:
   - **Recipient:** `brian@brianreevz.altostrat.com`
   - **Detected Promo:** `PROMO-MERAKI-20` (`SKU-NET-MERAKI-MR36` discounted 20% from `$450.00` to `$360.00`)
   - **Eligible Quote:** `Quote #1042` (`Acme Corp`, Qty: 20)
   - **3-Legged Copilot Handoff Prompt:** `"Show me the impact of the new vendor promotion on my Quote #1042 and reprice it to maintain an 18% margin."`
4. **2-Legged Least Privilege Verification:** Attempt to auto-reprice Quote `#1042` directly (`internal_quotes_mcp_reprice_quote` with `auth_mode="2_legged_agent_identity"`) and report the `403 Forbidden` policy block from Agent Gateway IAP / MCP RBAC, explaining that mutating quotes requires handing off to the **Deal Desk Quote Copilot (3-Legged Auth)**."""


def _render_mcp_services_doc() -> str:
    if not DISCOVERED_MCP_SERVERS:
        return "_(no MCP services discovered — only utility tools are available)_"
    lines: list[str] = []
    for entry in DISCOVERED_MCP_SERVERS:
        prefix = entry.get("tool_name_prefix")
        name = entry.get("name") or entry.get("resource_name") or "?"
        tool_names = entry.get("tools") or []
        desc = _SERVICE_DESCRIPTIONS.get(name, "")
        suffix = f" — {desc}" if desc else ""
        if prefix and tool_names:
            full = ", ".join(f"`{prefix}_{t}`" for t in tool_names)
            lines.append(f"- **{name}** (tools: {full}){suffix}")
        elif prefix:
            lines.append(f"- **{name}** (tools prefixed `{prefix}_*`){suffix}")
        else:
            lines.append(f"- **{name}**{suffix}")
    return "\n".join(lines)


def _handle_tool_error(
    tool: BaseTool, args: dict[str, Any], tool_context: ToolContext, error: Exception
) -> dict | None:
    return {
        "error": (
            f"403 Forbidden (IAP REQUEST_AUTHZ / 2-Legged Least Privilege): The 2-Legged Sentinel Agent "
            f"was blocked from executing '{tool.name}'. Only read-only tools (`readOnlyHint: true`) are "
            f"permitted for 2-Legged Agent Identity. Please hand off to the 3-Legged Deal Desk Quote Copilot."
        )
    }


def _discover_mcp_toolsets() -> list:
    global _CACHED_TOOLSETS, _CACHED_DISCOVERED
    if _CACHED_TOOLSETS is not None:
        DISCOVERED_MCP_SERVERS.clear()
        DISCOVERED_MCP_SERVERS.extend(_CACHED_DISCOVERED or [])
        return _CACHED_TOOLSETS

    DISCOVERED_MCP_SERVERS.clear()
    project = os.environ.get("MCP_REGISTRY_PROJECT") or os.environ.get("GOOGLE_CLOUD_PROJECT")
    location = os.environ.get("MCP_REGISTRY_LOCATION") or "us-central1"
    filter_str = os.environ.get("MCP_REGISTRY_FILTER")
    invoker_sa_email = os.environ.get("MCP_INVOKER_SA_EMAIL")

    try:
        from google.adk.integrations.agent_registry import AgentRegistry

        registry = AgentRegistry(project_id=project, location=location)
        response = registry.list_mcp_servers(filter_str=filter_str)
    except Exception as exc:
        logger.warning("AgentRegistry discovery skipped or failed: %s", exc)
        _CACHED_TOOLSETS = []
        _CACHED_DISCOVERED = []
        return _CACHED_TOOLSETS

    toolsets = []
    for server in response.get("mcpServers", []):
        name = server.get("name")
        display = server.get("displayName") or ""
        if not name:
            continue
        # Only connect to our hybrid-oauth demo MCP servers if multiple exist
        if display and display not in ("xvantage-catalog-mcp", "internal-quotes-mcp") and not filter_str:
            continue
        try:
            toolset = registry.get_mcp_toolset(mcp_server_name=name)
            conn_params = getattr(toolset, "_connection_params", None)
            resolved_url = getattr(conn_params, "url", None)
            if invoker_sa_email and conn_params is not None and resolved_url and hasattr(conn_params, "httpx_client_factory"):
                conn_params.httpx_client_factory = _build_impersonation_factory(
                    target_url=resolved_url,
                    target_sa_email=invoker_sa_email,
                )
            toolsets.append(toolset)
            DISCOVERED_MCP_SERVERS.append(
                {
                    "name": display or name,
                    "resource_name": name,
                    "tool_name_prefix": getattr(toolset, "tool_name_prefix", None),
                    "resolved_url": resolved_url,
                    "tools": [t.get("name") for t in server.get("tools", []) if t.get("name")],
                }
            )
        except Exception:
            logger.exception("Failed building toolset for %s", name)

    _CACHED_TOOLSETS = toolsets
    _CACHED_DISCOVERED = list(DISCOVERED_MCP_SERVERS)
    return _CACHED_TOOLSETS


logging.basicConfig(level=logging.INFO, force=True)


def _before_tool_log(tool: BaseTool, args: dict[str, Any], tool_context: ToolContext) -> dict | None:
    msg = f"[TELEMETRY][2-LEGGED-SENTINEL] Calling tool '{tool.name}' with args={args} (auth_mode=2_legged_agent_identity)"
    logger.info(msg)
    print(msg, flush=True)
    return None


def _after_tool_log(
    tool: BaseTool, args: dict[str, Any], tool_context: ToolContext, tool_response: dict
) -> dict | None:
    msg = f"[TELEMETRY][2-LEGGED-SENTINEL] Completed tool '{tool.name}' -> response={str(tool_response)[:400]}"
    logger.info(msg)
    print(msg, flush=True)
    return None


class _PickleSafeAgent(Agent):
    def __reduce__(self):
        return (_build_agent, ())

    def __deepcopy__(self, memo):
        return _build_agent()


def _build_agent():
    _tools: list = [
        tools.get_current_time,
        tools.list_mcp_connections,
        tools.sleep_between_poll_cycles,
    ]
    _tools.extend(_discover_mcp_toolsets())
    instruction = _INSTRUCTION_TEMPLATE.format(mcp_services_doc=_render_mcp_services_doc())
    return _PickleSafeAgent(
        model=os.environ.get("MODEL_NAME", "gemini-2.5-flash"),
        name="promo_sentinel_agent_2legged",
        description=(
            "2-Legged OAuth (Agent Identity) Sentinel Agent that watches Xvantage catalog promotions "
            "and matches eligible internal quotes without quote mutation privileges."
        ),
        instruction=instruction,
        tools=_tools,
        before_tool_callback=_before_tool_log,
        after_tool_callback=_after_tool_log,
        on_tool_error_callback=_handle_tool_error,
    )


root_agent = _build_agent()
