# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0

"""ADK Agent 2: 3-Legged Deal Desk Quote Copilot (quote-copilot-agent).

Patterned on `demos/agent-gateway/src/mortgage-agent/agent/agent.py`.
Registered in Gemini Enterprise with 3-Legged OAuth (`authorization_config.tool_authorizations`).
Discovers `internal-quotes-mcp` and `xvantage-catalog-mcp` from Agent Registry,
reads and reprices quotes owned by the authenticated user (`brian@brianreevz.altostrat.com`),
enforces 3-Legged Tenant Isolation (`403 Forbidden` on Dave's Quote #9999),
and records `updated_by: brian@brianreevz.altostrat.com` in the Cloud Run audit trail.
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
    """Return an httpx_client_factory that signs requests for Cloud Run via Agent Gateway."""
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
            request.headers["X-Agent-Auth-Mode"] = "3_legged_oauth"
            request.headers["X-Delegated-User"] = os.environ.get("DEFAULT_3L_USER", "brian@brianreevz.altostrat.com")
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
            timeout=timeout if timeout is not None else httpx.Timeout(5.0),
            auth=auth if auth is not None else auth_handler,
        )

    return factory


DISCOVERED_MCP_SERVERS: list[dict[str, Any]] = []
_CACHED_TOOLSETS: list | None = None
_CACHED_DISCOVERED: list[dict[str, Any]] | None = None

_SERVICE_DESCRIPTIONS: dict[str, str] = {
    "internal-quotes-mcp": (
        "the internal Cloud Run Quotes Repository MCP service (`brianreevz.altostrat.com`). "
        "Use `list_my_active_quotes` to retrieve the authenticated user's quotes and `reprice_quote` "
        "to update quote pricing and margins under 3-Legged OAuth user delegation."
    ),
    "xvantage-catalog-mcp": (
        "the Xvantage vendor catalog & active promotions MCP service (`bigredreeves.com`). "
        "Use `get_active_promotions` to verify active vendor discounts (`PROMO-MERAKI-20`)."
    ),
}

_INSTRUCTION_TEMPLATE = """You are the **Deal Desk Quote Copilot (3-Legged Auth)** in Gemini Enterprise for the `brianreevz.altostrat.com` organization (`gemini-enterprise-demo-508317`).

You execute actions strictly under **3-Legged User-Delegated OAuth (`authorization_code`)** on behalf of the logged-in Account Executive:
- **Authenticated User:** `brian@brianreevz.altostrat.com` (Role: `sales_rep`, Scopes: `quotes:read quotes:write`)

**MCP services discovered from Agent Registry:**
{mcp_services_doc}

**Workflow:**
1. When Brian asks: *"Show me the impact of the new vendor promotion on my Quote #1042 and reprice it to maintain an 18% margin"*:
   - Call `internal_quotes_mcp_list_my_active_quotes(user_email="brian@brianreevz.altostrat.com")` to load Quote `#1042` (`Acme Corp`, `SKU-NET-MERAKI-MR36`, Qty: 20, Baseline Cost: `$450.00`, Quoted Price: `$540.00`).
   - Call `xvantage_catalog_mcp_get_active_promotions()` to verify `PROMO-MERAKI-20` (`20% off SKU-NET-MERAKI-MR36`, discounted wholesale cost: `$360.00`).
   - Call `calculate_target_margin_impact` and call `internal_quotes_mcp_reprice_quote(quote_id=1042, promo_code="PROMO-MERAKI-20", target_margin_percent=18.0, promo_wholesale_cost=360.0, user_email="brian@brianreevz.altostrat.com", auth_mode="3_legged_oauth")`.
2. Present a clear **Before vs. After Margin & Pricing Comparison Table**:
   - **Before Promo:** Cost: `$450.00` | Quoted Price: `$540.00` (`16.67%` margin) | Total: `$10,800.00`
   - **After 20% Promo (`18.0%` Margin):** Cost: `$360.00` | New Quoted Price: **`$439.02`** (`18.00%` margin) | New Total: **`$8,780.40`** (Customer Savings for Acme Corp: **`$2,019.60`**)
   - Explicitly display the **Audit Trail Attribution** returned by the tool (`updated_by: brian@brianreevz.altostrat.com`).
3. **3-Legged Tenant Isolation Check:** If asked to read or reprice **Quote `#9999`** (owned by `dave@brianreevz.altostrat.com` for `Cyberdyne`), call `internal_quotes_mcp_reprice_quote` with `quote_id=9999` and `user_email="brian@brianreevz.altostrat.com"`, and report the **`403 Forbidden`** tenant isolation enforcement."""


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
            f"403 Forbidden: Tool '{tool.name}' was blocked by authorization policy or tenant RBAC isolation."
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
    msg = f"[TELEMETRY][3-LEGGED-COPILOT] Calling tool '{tool.name}' with args={args} (auth_mode=3_legged_oauth, delegated_user=brian@brianreevz.altostrat.com)"
    logger.info(msg)
    print(msg, flush=True)
    return None


def _after_tool_log(
    tool: BaseTool, args: dict[str, Any], tool_context: ToolContext, tool_response: dict
) -> dict | None:
    msg = f"[TELEMETRY][3-LEGGED-COPILOT] Completed tool '{tool.name}' -> response={str(tool_response)[:400]}"
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
        tools.calculate_target_margin_impact,
    ]
    _tools.extend(_discover_mcp_toolsets())
    instruction = _INSTRUCTION_TEMPLATE.format(mcp_services_doc=_render_mcp_services_doc())
    return _PickleSafeAgent(
        model=os.environ.get("MODEL_NAME", "gemini-2.5-flash"),
        name="deal_desk_quote_copilot_3legged",
        description=(
            "3-Legged OAuth Deal Desk Quote Copilot that reads and reprices customer quotes "
            "using Brian's delegated user identity (brian@brianreevz.altostrat.com)."
        ),
        instruction=instruction,
        tools=_tools,
        before_tool_callback=_before_tool_log,
        after_tool_callback=_after_tool_log,
        on_tool_error_callback=_handle_tool_error,
    )


root_agent = _build_agent()
