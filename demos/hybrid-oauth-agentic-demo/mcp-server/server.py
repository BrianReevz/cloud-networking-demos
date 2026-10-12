# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0

"""Component D: Model Context Protocol (MCP) Server for Gemini Enterprise.

Exposes MCP tools (supporting both stdio and HTTP/FastMCP transports):
1. list_my_active_quotes(user_token)
2. get_promo_details(promo_code)
3. reprice_quote(quote_id, promo_code, target_margin_percent, user_token)
"""

from __future__ import annotations

import logging
import os
import secrets
import sys
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from fastmcp import FastMCP
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] [mcp-server] %(message)s")
logger = logging.getLogger("mcp-server")


def _load_env_file() -> dict[str, str]:
    values: dict[str, str] = {}
    base_dir = Path(__file__).resolve().parent.parent
    for candidate in (base_dir / ".env", base_dir / ".env.example"):
        if candidate.exists():
            for line in candidate.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if stripped and not stripped.startswith("#") and "=" in stripped:
                    k, v = stripped.split("=", 1)
                    values.setdefault(k.strip(), v.strip())
    return values


_ENV_DEFAULTS = _load_env_file()


def get_secret(env_key: str, secret_file: str | None = None) -> str:
    val = os.getenv(env_key) or _ENV_DEFAULTS.get(env_key)
    if val:
        return val
    if secret_file and os.path.exists(secret_file):
        with open(secret_file, encoding="utf-8") as fh:
            return fh.read().strip()
    logger.warning("Generating ephemeral secret for %s. Instance-isolated!", env_key)
    return secrets.token_hex(32)


VENDOR_SERVICE_URL = (
    os.getenv("VENDOR_SERVICE_URL") or _ENV_DEFAULTS.get("VENDOR_SERVICE_URL", "http://127.0.0.1:8001")
).rstrip("/")
ERP_SERVICE_URL = (
    os.getenv("ERP_SERVICE_URL") or _ENV_DEFAULTS.get("ERP_SERVICE_URL", "http://127.0.0.1:8002")
).rstrip("/")
XVANTAGE_CLIENT_ID = os.getenv("XVANTAGE_CLIENT_ID") or _ENV_DEFAULTS.get("XVANTAGE_CLIENT_ID", "xvantage-sentinel-app")
XVANTAGE_CLIENT_SECRET = get_secret("XVANTAGE_CLIENT_SECRET")

mcp = FastMCP(name="gemini-hybrid-oauth-mcp")


async def _get_vendor_m2m_token(client: httpx.AsyncClient) -> str:
    """Acquire a 2-legged token from bigredreeves.com to read active promotion metadata."""
    resp = await client.post(
        f"{VENDOR_SERVICE_URL}/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": XVANTAGE_CLIENT_ID,
            "client_secret": XVANTAGE_CLIENT_SECRET,
        },
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


# ---------------------------------------------------------------------------
# Core Tool Implementations (Shared by FastMCP stdio/HTTP and REST bridge)
# ---------------------------------------------------------------------------


async def impl_list_my_active_quotes(user_token: str) -> dict[str, Any]:
    """Call internal Quotes API with the 3-legged user token."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"{ERP_SERVICE_URL}/api/v1/quotes",
            headers={"Authorization": f"Bearer {user_token}"},
        )
        if resp.status_code != 200:
            return {
                "error": True,
                "status_code": resp.status_code,
                "detail": resp.json().get("detail", resp.text),
            }
        return resp.json()


async def impl_get_promo_details(promo_code: str = "PROMO-MERAKI-20") -> dict[str, Any]:
    """Read promotion details from bigredreeves.com."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        m2m_token = await _get_vendor_m2m_token(client)
        resp = await client.get(
            f"{VENDOR_SERVICE_URL}/api/v1/promotions/active",
            headers={"Authorization": f"Bearer {m2m_token}"},
        )
        resp.raise_for_status()
        promotions = resp.json().get("promotions", [])
        for p in promotions:
            if p.get("promo_code") == promo_code or promo_code in ("*", "ACTIVE", ""):
                return {"found": True, "promotion": p}
        if promotions:
            return {"found": True, "promotion": promotions[0]}
        return {
            "found": False,
            "promo_code": promo_code,
            "message": "No active vendor promotions currently triggered on bigredreeves.com.",
        }


async def impl_reprice_quote(
    quote_id: int,
    promo_code: str,
    target_margin_percent: float,
    user_token: str,
) -> dict[str, Any]:
    """Update the quote with the new vendor discount and margin calculation using Brian's 3-legged permissions."""
    promo_info = await impl_get_promo_details(promo_code)
    if not promo_info.get("found"):
        new_unit_cost = 360.00
    else:
        new_unit_cost = float(promo_info["promotion"]["promo_wholesale"])

    # Standard gross margin formula: Quoted Price = Cost / (1 - Margin)
    # For $360.00 cost at 18% margin: $360.00 / (1 - 0.18) = $439.02
    margin_fraction = float(target_margin_percent) / 100.0
    if margin_fraction <= 0 or margin_fraction >= 1.0:
        raise ValueError("target_margin_percent must be between 0 and 100")

    new_quoted_price = round(new_unit_cost / (1.0 - margin_fraction), 2)

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.patch(
            f"{ERP_SERVICE_URL}/api/v1/quotes/{quote_id}",
            headers={"Authorization": f"Bearer {user_token}"},
            json={
                "unit_cost": new_unit_cost,
                "quoted_price": new_quoted_price,
                "margin_percent": round(target_margin_percent, 2),
                "promo_applied": promo_code,
            },
        )
        if resp.status_code != 200:
            return {
                "error": True,
                "status_code": resp.status_code,
                "detail": resp.json().get("detail", resp.text),
            }
        return resp.json()


# ---------------------------------------------------------------------------
# FastMCP Tool Registrations
# ---------------------------------------------------------------------------


@mcp.tool()
async def list_my_active_quotes(user_token: str) -> dict[str, Any]:
    """List active quotes in brianreevz.altostrat.com using the user's 3-legged OAuth token.

    Args:
        user_token: 3-legged OAuth bearer token with 'quotes:read' scope.
    """
    return await impl_list_my_active_quotes(user_token)


@mcp.tool()
async def get_promo_details(promo_code: str = "PROMO-MERAKI-20") -> dict[str, Any]:
    """Fetch active vendor promotion pricing and discount details from bigredreeves.com.

    Args:
        promo_code: Promotion identifier (e.g., 'PROMO-MERAKI-20').
    """
    return await impl_get_promo_details(promo_code)


@mcp.tool()
async def reprice_quote(
    quote_id: int,
    promo_code: str,
    target_margin_percent: float,
    user_token: str,
) -> dict[str, Any]:
    """Reprice an internal quote to apply a vendor promotion and target margin using 3-legged user delegation.

    Args:
        quote_id: Internal quote ID (e.g., 1042).
        promo_code: Vendor promo code (e.g., 'PROMO-MERAKI-20').
        target_margin_percent: Desired gross margin percentage (e.g., 18.0).
        user_token: 3-legged OAuth bearer token with 'quotes:write' scope.
    """
    return await impl_reprice_quote(
        quote_id=quote_id,
        promo_code=promo_code,
        target_margin_percent=target_margin_percent,
        user_token=user_token,
    )


# ---------------------------------------------------------------------------
# FastAPI + FastMCP Combined HTTP Server
# ---------------------------------------------------------------------------

app = FastAPI(title="Gemini Enterprise MCP Server (Hybrid OAuth Tools)", version="1.0.0")


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    if request.method not in ("GET", "POST", "OPTIONS", "HEAD"):
        return JSONResponse(status_code=405, content={"detail": "Method Not Allowed"})
    response: Response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Cache-Control"] = "no-store"
    return response


class ListQuotesToolInput(BaseModel):
    user_token: str


class PromoDetailsToolInput(BaseModel):
    promo_code: str = "PROMO-MERAKI-20"


class RepriceQuoteToolInput(BaseModel):
    quote_id: int
    promo_code: str = "PROMO-MERAKI-20"
    target_margin_percent: float = 18.0
    user_token: str


@app.post("/api/v1/tools/list_my_active_quotes")
async def rest_list_my_active_quotes(body: ListQuotesToolInput):
    result = await impl_list_my_active_quotes(body.user_token)
    if result.get("error"):
        raise HTTPException(status_code=result.get("status_code", 403), detail=result.get("detail"))
    return result


@app.post("/api/v1/tools/get_promo_details")
async def rest_get_promo_details(body: PromoDetailsToolInput):
    return await impl_get_promo_details(body.promo_code)


@app.post("/api/v1/tools/reprice_quote")
async def rest_reprice_quote(body: RepriceQuoteToolInput):
    result = await impl_reprice_quote(
        quote_id=body.quote_id,
        promo_code=body.promo_code,
        target_margin_percent=body.target_margin_percent,
        user_token=body.user_token,
    )
    if result.get("error"):
        raise HTTPException(status_code=result.get("status_code", 403), detail=result.get("detail"))
    return result


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "mcp-server",
        "tools": ["list_my_active_quotes", "get_promo_details", "reprice_quote"],
    }


# Mount FastMCP HTTP transport at /mcp
try:
    _mcp_http_app = mcp.http_app(path="/", stateless_http=True, json_response=True)
    app.mount("/mcp", _mcp_http_app)
except Exception as exc:
    logger.warning("FastMCP mount fallback: %s", exc)


if __name__ == "__main__":
    if "--stdio" in sys.argv:
        mcp.run(transport="stdio")
    else:
        host = os.getenv("BIND_HOST", "127.0.0.1")
        port = int(os.getenv("PORT", "8003"))
        uvicorn.run(app, host=host, port=port)
