# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0

"""Cloud Run Internal Quotes MCP Server (internal-quotes-mcp).

Patterned on `demos/agent-gateway/src/corporate-email/main.py`.
Runs on Cloud Run behind Agent Gateway and is registered in Agent Registry.

Enforces:
1. 2-Legged Least Privilege (`match_eligible_quotes` is readOnlyHint=true; `reprice_quote` is readOnlyHint=false and rejects 2-legged callers with 403).
2. 3-Legged Tenant Isolation (`brian@brianreevz.altostrat.com` can read/reprice Quote #1042 for Acme Corp, but receives 403 Forbidden on Quote #9999 owned by `dave@brianreevz.altostrat.com`).
3. Audit Trail Verification (`updated_by: brian@brianreevz.altostrat.com`).
"""

from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime, timezone
from typing import Any

import uvicorn
from fastmcp import FastMCP
from fastmcp.tools.base import ToolResult
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] [internal-quotes-mcp] %(message)s")
logger = logging.getLogger("internal-quotes-mcp")

GCP_PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT", "gemini-enterprise-demo-508317")
INTERNAL_DOMAIN = os.getenv("INTERNAL_DOMAIN", "brianreevz.altostrat.com")

INITIAL_QUOTES: dict[int, dict[str, Any]] = {
    1042: {
        "quote_id": 1042,
        "owner": "brian@brianreevz.altostrat.com",
        "client": "Acme Corp",
        "sku": "SKU-NET-MERAKI-MR36",
        "product_name": "Cisco Meraki MR36 Wi-Fi 6 Cloud-Managed Indoor AP",
        "qty": 20,
        "baseline_cost": 450.00,
        "unit_cost": 450.00,
        "quoted_price": 540.00,
        "margin_percent": 16.67,
        "total_cost": 9000.00,
        "total_price": 10800.00,
        "gross_profit": 1800.00,
        "promo_applied": None,
        "status": "OPEN",
        "updated_by": "system-seed@brianreevz.altostrat.com",
        "updated_at": "2026-09-22T20:00:00Z",
    },
    9999: {
        "quote_id": 9999,
        "owner": "dave@brianreevz.altostrat.com",
        "client": "Cyberdyne",
        "sku": "SKU-NET-MERAKI-MR36",
        "product_name": "Cisco Meraki MR36 Wi-Fi 6 Cloud-Managed Indoor AP",
        "qty": 50,
        "baseline_cost": 450.00,
        "unit_cost": 450.00,
        "quoted_price": 540.00,
        "margin_percent": 16.67,
        "total_cost": 22500.00,
        "total_price": 27000.00,
        "gross_profit": 4500.00,
        "promo_applied": None,
        "status": "OPEN",
        "updated_by": "system-seed@brianreevz.altostrat.com",
        "updated_at": "2026-09-22T20:00:00Z",
    },
}

QUOTES_DB: dict[int, dict[str, Any]] = {k: dict(v) for k, v in INITIAL_QUOTES.items()}
NOTIFICATIONS_QUEUE: list[dict[str, Any]] = []
AUDIT_LOGS: list[dict[str, Any]] = []


def _record_audit(
    action: str,
    actor: str,
    auth_mode: str,
    quote_id: int | None,
    outcome: str,
    details: dict[str, Any],
) -> dict[str, Any]:
    entry = {
        "event_id": f"aud-{secrets.token_hex(6)}",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "gcp_project": GCP_PROJECT_ID,
        "domain": INTERNAL_DOMAIN,
        "action": action,
        "updated_by": actor,
        "auth_mode": auth_mode,
        "quote_id": quote_id,
        "outcome": outcome,
        "details": details,
    }
    AUDIT_LOGS.append(entry)
    logger.info("AUDIT [%s] action=%s updated_by=%s auth_mode=%s quote_id=%s", outcome, action, actor, auth_mode, quote_id)
    return entry


mcp = FastMCP(name="internal-quotes-mcp")


@mcp.tool()
def match_eligible_quotes(
    sku: str = "SKU-NET-MERAKI-MR36",
    discount_percent: float = 20.0,
    promo_wholesale: float = 360.0,
) -> ToolResult:
    """Read-only tool for the 2-Legged Sentinel Agent to find open quotes matching a promoted SKU."""
    matching = [
        {
            "quote_id": q["quote_id"],
            "owner": q["owner"],
            "client": q["client"],
            "sku": q["sku"],
            "qty": q["qty"],
            "baseline_cost": q["baseline_cost"],
            "current_quoted_price": q["quoted_price"],
        }
        for q in QUOTES_DB.values()
        if q["sku"] == sku and q["owner"] == "brian@brianreevz.altostrat.com"
    ]
    notifications_emitted = []
    for m in matching:
        payload = {
            "recipient": m["owner"],
            "message": (
                f"Promo detected on {sku} ({int(discount_percent)}% off, new wholesale ${promo_wholesale:.2f}). "
                f"Your Quote #{m['quote_id']} for {m['client']} is eligible for repricing."
            ),
            "action_url": f"http://brianreevz.altostrat.com/copilot?quoteId={m['quote_id']}",
            "recommended_copilot_prompt": (
                f"Show me the impact of the new vendor promotion on my Quote #{m['quote_id']} "
                "and reprice it to maintain an 18% margin."
            ),
        }
        NOTIFICATIONS_QUEUE.append(payload)
        notifications_emitted.append(payload)

    _record_audit(
        action="SENTINEL_MATCH_ELIGIBLE_QUOTES",
        actor=f"promo-sentinel-agent@{GCP_PROJECT_ID}.iam.gserviceaccount.com",
        auth_mode="2_legged_agent_identity",
        quote_id=1042,
        outcome="200_OK_READ_ONLY",
        details={"sku": sku, "matched_quote_ids": [m["quote_id"] for m in matching]},
    )
    return ToolResult(
        content=[],
        structured_content={
            "auth_mode": "2_legged_agent_identity",
            "sku": sku,
            "matching_quotes": matching,
            "notifications_dispatched": notifications_emitted,
        },
    )


@mcp.tool()
def list_my_active_quotes(user_email: str = "brian@brianreevz.altostrat.com") -> ToolResult:
    """Read-only tool for the 3-Legged Copilot to list quotes strictly owned by the authenticated user."""
    normalized = user_email.strip().lower()
    user_quotes = [q for q in QUOTES_DB.values() if q["owner"] == normalized]
    return ToolResult(
        content=[],
        structured_content={
            "authenticated_user": normalized,
            "auth_mode": "3_legged_oauth_user_delegated",
            "rbac_tenant_filter": f"owner == '{normalized}' (Quote #9999 owned by dave@brianreevz.altostrat.com is excluded)",
            "quotes": user_quotes,
        },
    )


@mcp.tool()
def reprice_quote(
    quote_id: int,
    promo_code: str,
    target_margin_percent: float,
    promo_wholesale_cost: float = 360.0,
    user_email: str = "brian@brianreevz.altostrat.com",
    auth_mode: str = "3_legged_oauth",
) -> ToolResult:
    """Privileged write tool requiring 3-Legged user delegation to update quote pricing and margin."""
    # CRITICAL SECURITY CHECK 1: 2-Legged Least Privilege
    if auth_mode != "3_legged_oauth" or "sentinel" in user_email.lower():
        audit = _record_audit(
            action="REPRICE_QUOTE_2LEGGED_DENIED",
            actor=user_email,
            auth_mode=auth_mode,
            quote_id=quote_id,
            outcome="403_FORBIDDEN",
            details={"reason": "2-Legged Sentinel Agent lacks write authority to mutate quotes"},
        )
        return ToolResult(
            content=[],
            structured_content={
                "error": True,
                "http_status": 403,
                "status": "403 Forbidden",
                "detail": (
                    "403 Forbidden: 2-Legged Sentinel Agent (Agent Identity) is strictly read-only "
                    "and cannot call reprice_quote. Handoff to the 3-Legged Deal Desk Copilot is required."
                ),
                "audit_record": audit,
            },
        )

    quote = QUOTES_DB.get(int(quote_id))
    if not quote:
        return ToolResult(
            content=[],
            structured_content={"error": True, "http_status": 404, "detail": f"Quote #{quote_id} not found"},
        )

    # CRITICAL SECURITY CHECK 2: 3-Legged Tenant Isolation (Brian vs Dave's Quote #9999)
    normalized_user = user_email.strip().lower()
    if quote["owner"] != normalized_user:
        audit = _record_audit(
            action="REPRICE_QUOTE_TENANT_ISOLATION_DENIED",
            actor=normalized_user,
            auth_mode=auth_mode,
            quote_id=quote_id,
            outcome="403_FORBIDDEN",
            details={"quote_owner": quote["owner"], "attempted_by": normalized_user},
        )
        return ToolResult(
            content=[],
            structured_content={
                "error": True,
                "http_status": 403,
                "status": "403 Forbidden",
                "detail": (
                    f"403 Forbidden: Tenant isolation violation. User '{normalized_user}' cannot access or mutate "
                    f"Quote #{quote_id} owned by '{quote['owner']}' (Client: {quote['client']})."
                ),
                "audit_record": audit,
            },
        )

    old_cost = quote["unit_cost"]
    old_price = quote["quoted_price"]
    old_margin = quote["margin_percent"]

    margin_fraction = float(target_margin_percent) / 100.0
    new_price = round(float(promo_wholesale_cost) / (1.0 - margin_fraction), 2)

    quote["unit_cost"] = round(float(promo_wholesale_cost), 2)
    quote["quoted_price"] = new_price
    quote["margin_percent"] = round(float(target_margin_percent), 2)
    quote["total_cost"] = round(quote["unit_cost"] * quote["qty"], 2)
    quote["total_price"] = round(quote["quoted_price"] * quote["qty"], 2)
    quote["gross_profit"] = round(quote["total_price"] - quote["total_cost"], 2)
    quote["promo_applied"] = promo_code
    quote["status"] = "REPRICED_PROMO_APPLIED"
    quote["updated_by"] = normalized_user
    quote["updated_at"] = datetime.now(timezone.utc).isoformat()

    audit = _record_audit(
        action="QUOTE_REPRICED",
        actor=normalized_user,
        auth_mode="3_legged_user_delegated",
        quote_id=quote_id,
        outcome="200_UPDATED",
        details={
            "previous_unit_cost": old_cost,
            "new_unit_cost": quote["unit_cost"],
            "previous_quoted_price": old_price,
            "new_quoted_price": quote["quoted_price"],
            "previous_margin_percent": old_margin,
            "new_margin_percent": quote["margin_percent"],
            "promo_applied": promo_code,
            "updated_by": normalized_user,
        },
    )

    return ToolResult(
        content=[],
        structured_content={
            "status": "updated",
            "http_status": 200,
            "quote": quote,
            "audit_record": audit,
        },
    )


async def health(request: Request):
    return JSONResponse({"status": "ok", "service": "internal-quotes-mcp", "audit_entries": len(AUDIT_LOGS)})


async def get_audit_logs(request: Request):
    return JSONResponse({"audit_logs": AUDIT_LOGS, "quotes": list(QUOTES_DB.values())})


async def reset_quotes(request: Request):
    import copy
    QUOTES_DB.clear()
    QUOTES_DB.update(copy.deepcopy(INITIAL_QUOTES))
    AUDIT_LOGS.clear()
    logger.info("Internal Quotes DB and Audit Logs reset to initial baseline state.")
    return JSONResponse({"status": "reset", "quotes": list(QUOTES_DB.values())})


_mcp_app = mcp.http_app(path="/mcp", stateless_http=True, json_response=True)

app = Starlette(
    routes=[
        Route("/health", health),
        Route("/api/v1/audit-logs", get_audit_logs),
        Route("/api/v1/demo/reset", reset_quotes, methods=["POST"]),
        Mount("/", app=_mcp_app),
    ],
    lifespan=_mcp_app.lifespan,
)

if __name__ == "__main__":
    host = os.getenv("BIND_HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8080"))
    uvicorn.run(app, host=host, port=port)
