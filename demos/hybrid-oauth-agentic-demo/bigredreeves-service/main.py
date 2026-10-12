# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0

"""Component A: Remote Vendor Platform (bigredreeves.com / Mock Xvantage).

Provides:
1. Mock OAuth 2.0 Authorization Server (POST /oauth/token, grant_type=client_credentials)
2. Catalog Price & Availability API (GET /api/v1/catalog/pna)
3. Active Vendor Promotions API (GET /api/v1/promotions/active)
4. Interactive Demo Control Web UI (GET /demo-admin) with "Trigger 20% Vendor Promo"
"""

from __future__ import annotations

import hmac
import logging
import os
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import jwt
import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] [bigredreeves-vendor] %(message)s")
logger = logging.getLogger("bigredreeves-vendor")

JWT_ALGORITHM = "HS256"


def _load_env_file() -> dict[str, str]:
    """Read key-value pairs from .env or .env.example if present."""
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
    """Resolve secret via Environment -> Local File -> Ephemeral CSPRNG fallback."""
    val = os.getenv(env_key) or _ENV_DEFAULTS.get(env_key)
    if val:
        return val
    if secret_file and os.path.exists(secret_file):
        with open(secret_file, encoding="utf-8") as fh:
            return fh.read().strip()
    logger.warning("Generating ephemeral secret for %s. Instance-isolated!", env_key)
    return secrets.token_hex(32)


VENDOR_JWT_SECRET = get_secret("VENDOR_JWT_SECRET")
EXPECTED_CLIENT_ID = os.getenv("XVANTAGE_CLIENT_ID") or _ENV_DEFAULTS.get("XVANTAGE_CLIENT_ID", "xvantage-sentinel-app")
EXPECTED_CLIENT_SECRET = get_secret("XVANTAGE_CLIENT_SECRET")
ADMIN_CSRF_TOKEN = secrets.token_hex(24)

# ---------------------------------------------------------------------------
# In-Memory Vendor Catalog & Promotions State
# ---------------------------------------------------------------------------

INITIAL_CATALOG: dict[str, dict[str, Any]] = {
    "SKU-NET-MERAKI-MR36": {
        "sku": "SKU-NET-MERAKI-MR36",
        "name": "Cisco Meraki MR36 Wi-Fi 6 Cloud-Managed Indoor AP",
        "category": "Enterprise Networking",
        "baseline_wholesale": 450.00,
        "current_wholesale": 450.00,
        "currency": "USD",
        "available_qty": 480,
        "promo_active": False,
        "promo_code": None,
    },
    "SKU-SRV-DELL-R750": {
        "sku": "SKU-SRV-DELL-R750",
        "name": "Dell PowerEdge R750 Dual-Socket 2U Rack Server",
        "category": "Data Center Compute",
        "baseline_wholesale": 3200.00,
        "current_wholesale": 3200.00,
        "currency": "USD",
        "available_qty": 42,
        "promo_active": False,
        "promo_code": None,
    },
}

STATE: dict[str, Any] = {
    "catalog": {k: dict(v) for k, v in INITIAL_CATALOG.items()},
    "active_promotions": [],
    "events": [],
}


def reset_vendor_state() -> None:
    """Reset catalog and promotions to baseline state."""
    STATE["catalog"] = {k: dict(v) for k, v in INITIAL_CATALOG.items()}
    STATE["active_promotions"] = []
    STATE["events"].append(
        {
            "event": "STATE_RESET",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "detail": "Catalog reset to baseline wholesale pricing ($450.00 / $3,200.00).",
        }
    )


def activate_meraki_promo() -> dict[str, Any]:
    """Activate the 20% discount promotion on SKU-NET-MERAKI-MR36 ($450.00 -> $360.00)."""
    now_iso = datetime.now(timezone.utc).isoformat()
    sku_item = STATE["catalog"]["SKU-NET-MERAKI-MR36"]
    sku_item["current_wholesale"] = 360.00
    sku_item["promo_active"] = True
    sku_item["promo_code"] = "PROMO-MERAKI-20"

    promo_obj = {
        "promo_code": "PROMO-MERAKI-20",
        "sku": "SKU-NET-MERAKI-MR36",
        "product_name": sku_item["name"],
        "discount_percent": 20.0,
        "baseline_wholesale": 450.00,
        "promo_wholesale": 360.00,
        "unit_savings": 90.00,
        "currency": "USD",
        "active": True,
        "triggered_at": now_iso,
        "description": "20% Off Vendor Channel Promotion on SKU-NET-MERAKI-MR36 ($450.00 -> $360.00)",
    }
    STATE["active_promotions"] = [promo_obj]
    STATE["events"].append(
        {
            "event": "PROMO_TRIGGERED",
            "timestamp": now_iso,
            "sku": "SKU-NET-MERAKI-MR36",
            "promo_code": "PROMO-MERAKI-20",
            "new_wholesale": 360.00,
        }
    )
    logger.info("Vendor Promo Activated: PROMO-MERAKI-20 on SKU-NET-MERAKI-MR36 ($450.00 -> $360.00)")
    return promo_obj


if os.getenv("DEFAULT_PROMO_ACTIVE", "true").lower() == "true":
    activate_meraki_promo()

import httpx
from fastmcp import FastMCP
from fastmcp.tools.base import ToolResult

CLOUD_CATALOG_MCP_URL = os.getenv(
    "CLOUD_CATALOG_MCP_URL", "https://xvantage-catalog-mcp-fntvjdmgca-uc.a.run.app"
).rstrip("/")

mcp_catalog = FastMCP(name="xvantage-catalog-mcp")


@mcp_catalog.tool(annotations={"readOnlyHint": True})
def get_catalog_pna(sku: str | None = None) -> ToolResult:
    """Retrieve baseline and current wholesale Price and Availability (PnA) for vendor SKUs."""
    items = list(STATE["catalog"].values())
    if sku:
        items = [i for i in items if i["sku"] == sku]
    return ToolResult(
        content=[],
        structured_content={
            "vendor": "bigredreeves.com (Mock Xvantage)",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "items": items,
        },
    )


@mcp_catalog.tool(annotations={"readOnlyHint": True})
async def get_active_promotions() -> ToolResult:
    """Retrieve currently active vendor promotions triggered on bigredreeves.com."""
    import asyncio
    # Long-poll up to 3.5s (checking every 0.25s) if no promotion is active yet,
    # waking up immediately the moment the user clicks "Trigger 20% Vendor Promo"
    for _ in range(14):
        if STATE["active_promotions"]:
            break
        await asyncio.sleep(0.25)
    return ToolResult(
        content=[],
        structured_content={
            "vendor": "bigredreeves.com (Mock Xvantage)",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "promotions": STATE["active_promotions"],
        },
    )


_catalog_mcp_app = mcp_catalog.http_app(path="/mcp", stateless_http=True, json_response=True)

# ---------------------------------------------------------------------------
# FastAPI App & Security Middleware
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Mock Xvantage Remote Vendor Platform (bigredreeves.com)",
    version="1.0.0",
    lifespan=_catalog_mcp_app.lifespan,
)


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    """Enforce strict HTTP security headers on all responses."""
    if request.url.path == "/mcp/":
        request.scope["path"] = "/mcp"
    if request.method not in ("GET", "POST", "OPTIONS", "HEAD", "DELETE"):
        return JSONResponse(status_code=405, content={"detail": "Method Not Allowed"})
    response: Response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self' http://127.0.0.1:* http://localhost:* "
        "http://bigredreeves.com:* http://brianreevz.altostrat.com:*; frame-ancestors 'none';"
    )
    return response


def verify_2legged_token(
    required_scope: str,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Validate OAuth 2.0 2-Legged M2M Bearer Token and required scope."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization Bearer header",
        )
    raw_token = authorization.removeprefix("Bearer ").strip()
    try:
        claims = jwt.decode(
            raw_token,
            VENDOR_JWT_SECRET,
            algorithms=[JWT_ALGORITHM],
            audience="https://bigredreeves.com/api/v1",
            options={"require": ["exp", "iat", "sub", "scope"]},
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid or expired 2-legged vendor token: {exc}",
        ) from exc

    scopes = str(claims.get("scope", "")).split()
    if required_scope not in scopes:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Insufficient scope. Required: {required_scope}",
        )
    return claims


# ---------------------------------------------------------------------------
# 1. Mock OAuth 2.0 Authorization Server (POST /oauth/token)
# ---------------------------------------------------------------------------


class ClientCredentialsRequest(BaseModel):
    grant_type: str = "client_credentials"
    client_id: str | None = None
    client_secret: str | None = None
    scope: str | None = None


@app.post("/oauth/token")
async def issue_oauth_token(request: Request):
    """Issue a 2-Legged OAuth M2M Bearer token for grant_type=client_credentials."""
    content_type = request.headers.get("content-type", "")
    grant_type = ""
    client_id = ""
    client_secret = ""

    if "application/json" in content_type:
        body = await request.json()
        grant_type = str(body.get("grant_type", ""))
        client_id = str(body.get("client_id", ""))
        client_secret = str(body.get("client_secret", ""))
    else:
        form = await request.form()
        grant_type = str(form.get("grant_type", ""))
        client_id = str(form.get("client_id", ""))
        client_secret = str(form.get("client_secret", ""))

    # Also support HTTP Basic Auth header if provided
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Basic ") and not client_id:
        import base64

        decoded = base64.b64decode(auth_header.removeprefix("Basic ").strip()).decode("utf-8")
        if ":" in decoded:
            client_id, client_secret = decoded.split(":", 1)

    if grant_type != "client_credentials":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unsupported grant_type. Only 'client_credentials' (2-legged M2M) is supported on bigredreeves.com.",
        )

    id_ok = hmac.compare_digest(client_id, EXPECTED_CLIENT_ID)
    secret_ok = hmac.compare_digest(client_secret, EXPECTED_CLIENT_SECRET)
    if not (id_ok and secret_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid client_id or client_secret for 2-legged OAuth.",
        )

    now = int(time.time())
    granted_scope = "catalog.read promotions.read"
    payload = {
        "iss": "https://bigredreeves.com",
        "sub": client_id,
        "aud": "https://bigredreeves.com/api/v1",
        "auth_type": "2_legged_m2m",
        "scope": granted_scope,
        "iat": now,
        "exp": now + 3600,
        "jti": secrets.token_hex(12),
    }
    access_token = jwt.encode(payload, VENDOR_JWT_SECRET, algorithm=JWT_ALGORITHM)
    logger.info("Issued 2-Legged M2M Bearer token to client_id=%s (scope=%s)", client_id, granted_scope)
    return {
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": 3600,
        "scope": granted_scope,
        "auth_type": "2_legged_m2m",
    }


# ---------------------------------------------------------------------------
# 2. Catalog & Promo APIs
# ---------------------------------------------------------------------------


@app.get("/api/v1/catalog/pna")
async def get_catalog_pna(authorization: str | None = Header(default=None)):
    """Return Price and Availability (PnA) for vendor SKUs."""
    verify_2legged_token("catalog.read", authorization)
    return {
        "vendor": "bigredreeves.com (Mock Xvantage)",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "items": list(STATE["catalog"].values()),
    }


@app.get("/api/v1/promotions/active")
async def get_active_promotions(authorization: str | None = Header(default=None)):
    """Return currently active vendor promotions (requires 2-legged promotions.read scope)."""
    verify_2legged_token("promotions.read", authorization)
    return {
        "vendor": "bigredreeves.com (Mock Xvantage)",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "promotions": STATE["active_promotions"],
    }


# ---------------------------------------------------------------------------
# 3. Demo Admin Control Endpoints & Web UI (/demo-admin)
# ---------------------------------------------------------------------------


_BG_TASKS: set = set()


@app.get("/api/v1/demo/state")
async def get_demo_state():
    """Return current catalog and promotion state for the Demo Control Web UI."""
    if CLOUD_CATALOG_MCP_URL and not os.getenv("K_SERVICE"):
        try:
            async with httpx.AsyncClient(timeout=2.5) as client:
                resp = await client.get(f"{CLOUD_CATALOG_MCP_URL}/api/v1/demo/state")
                if resp.status_code == 200:
                    cloud_state = resp.json()
                    # Keep local memory synced with the authoritative Cloud Run single-instance state
                    if cloud_state.get("active_promotions"):
                        activate_meraki_promo()
                    else:
                        reset_vendor_state()
                    return cloud_state
        except Exception as exc:
            logger.warning("Cloud Run state read fallback to local: %s", exc)
    return {
        "vendor_domain": "bigredreeves.com",
        "oauth_client_id": EXPECTED_CLIENT_ID,
        "catalog": list(STATE["catalog"].values()),
        "active_promotions": STATE["active_promotions"],
        "events": STATE["events"][-10:],
    }


async def _sync_to_cloud_mirror(endpoint_path: str) -> None:
    """Forward local promo trigger/reset state from localhost:8001 to the Cloud Run mirror if configured."""
    if not CLOUD_CATALOG_MCP_URL or os.getenv("K_SERVICE"):
        return
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(f"{CLOUD_CATALOG_MCP_URL}{endpoint_path}")
            logger.info("Synced %s to Cloud Run mirror %s", endpoint_path, CLOUD_CATALOG_MCP_URL)
    except Exception as exc:
        logger.warning("Cloud Run mirror sync skipped (%s): %s", CLOUD_CATALOG_MCP_URL, exc)


@app.post("/api/v1/demo/trigger-promo")
async def trigger_vendor_promo(request: Request):
    """Trigger the 20% Vendor Promo on SKU-NET-MERAKI-MR36 ($450.00 -> $360.00)."""
    promo = activate_meraki_promo()
    await _sync_to_cloud_mirror("/api/v1/demo/trigger-promo")
    return {
        "status": "promo_triggered",
        "promotion": promo,
        "catalog_item": STATE["catalog"]["SKU-NET-MERAKI-MR36"],
    }


async def _background_oauth_reset() -> None:
    if not os.getenv("K_SERVICE"):
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post("https://internal-quotes-mcp-fntvjdmgca-uc.a.run.app/api/v1/demo/reset")
        except Exception as exc:
            logger.warning("Quotes reset notice: %s", exc)
        try:
            import asyncio
            script_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "reset_demo.sh")
            if os.path.exists(script_path):
                proc = await asyncio.create_subprocess_exec("bash", script_path, "--oauth-only")
                await proc.wait()
        except Exception as exc:
            logger.warning("OAuth rotation notice: %s", exc)


@app.post("/api/v1/demo/reset")
async def reset_demo_endpoint(request: Request):
    """Reset the vendor promo state back to baseline ($450.00), reset Quote #1042, and rotate 3-legged OAuth."""
    import asyncio
    reset_vendor_state()
    await _sync_to_cloud_mirror("/api/v1/demo/reset")
    t = asyncio.create_task(_background_oauth_reset())
    _BG_TASKS.add(t)
    t.add_done_callback(_BG_TASKS.discard)
    return {"status": "reset", "catalog": list(STATE["catalog"].values())}


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "bigredreeves-service",
        "mcp_server": "xvantage-catalog-mcp",
        "domain": "bigredreeves.com",
    }


DEMO_ADMIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>bigredreeves.com | Xvantage Vendor Distribution Portal (Demo Admin)</title>
  <style>
    :root {
      --bg: #0f172a;
      --card: #1e293b;
      --border: #334155;
      --text: #f8fafc;
      --muted: #94a3b8;
      --accent: #ef4444;
      --accent-hover: #dc2626;
      --success: #10b981;
      --badge-bg: #312e81;
      --badge-text: #c7d2fe;
    }
    * { box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
    body { margin: 0; background: var(--bg); color: var(--text); padding: 28px; }
    .container { max-width: 1040px; margin: 0 auto; }
    .header {
      display: flex; justify-content: space-between; align-items: center;
      background: var(--card); border: 1px solid var(--border);
      padding: 20px 24px; border-radius: 12px; margin-bottom: 24px;
    }
    .brand { display: flex; align-items: center; gap: 14px; }
    .brand-dot { width: 14px; height: 14px; border-radius: 50%; background: var(--accent); box-shadow: 0 0 12px var(--accent); }
    .brand h1 { margin: 0; font-size: 20px; font-weight: 700; }
    .brand p { margin: 4px 0 0 0; font-size: 13px; color: var(--muted); }
    .oauth-pill {
      background: var(--badge-bg); color: var(--badge-text);
      padding: 8px 14px; border-radius: 999px; font-size: 12px; font-weight: 600;
    }
    .panel {
      background: var(--card); border: 1px solid var(--border);
      border-radius: 12px; padding: 24px; margin-bottom: 24px;
    }
    .panel-title { margin: 0 0 16px 0; font-size: 16px; font-weight: 600; display: flex; justify-content: space-between; align-items: center; }
    table { width: 100%; border-collapse: collapse; }
    th, td { text-align: left; padding: 14px 12px; border-bottom: 1px solid var(--border); font-size: 14px; }
    th { color: var(--muted); font-weight: 600; font-size: 12px; text-transform: uppercase; letter-spacing: 0.05em; }
    .sku-code { font-family: monospace; font-weight: 700; color: #38bdf8; }
    .price-strike { text-decoration: line-through; color: var(--muted); margin-right: 8px; }
    .price-promo { color: var(--success); font-weight: 700; font-size: 15px; }
    .btn {
      background: var(--accent); color: #fff; border: none; padding: 10px 16px;
      border-radius: 8px; font-weight: 600; font-size: 13px; cursor: pointer;
      transition: background 0.15s ease;
    }
    .btn:hover { background: var(--accent-hover); }
    .btn:disabled { background: #475569; cursor: not-allowed; }
    .btn-secondary {
      background: transparent; border: 1px solid var(--border); color: var(--muted);
      padding: 8px 14px; border-radius: 8px; font-size: 12px; cursor: pointer;
    }
    .btn-secondary:hover { color: var(--text); border-color: var(--muted); }
    .status-banner {
      padding: 14px 18px; border-radius: 8px; margin-top: 16px;
      background: rgba(16, 185, 129, 0.12); border: 1px solid rgba(16, 185, 129, 0.4);
      color: #6ee7b7; font-size: 14px; display: none;
    }
    .event-list { list-style: none; padding: 0; margin: 0; font-family: monospace; font-size: 12px; color: var(--muted); }
    .event-list li { padding: 6px 0; border-bottom: 1px dashed var(--border); }
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <div class="brand">
        <div class="brand-dot"></div>
        <div>
          <h1>bigredreeves.com &mdash; Mock Xvantage Vendor Portal</h1>
          <p>External Partner Wholesale Catalog &amp; 2-Legged OAuth (client_credentials) Endpoint</p>
        </div>
      </div>
      <div class="oauth-pill">OAuth2 M2M Client: xvantage-sentinel-app | Scopes: catalog.read promotions.read</div>
    </div>

    <div class="panel">
      <div class="panel-title">
        <span>Wholesale Inventory &amp; Promotional Pricing Control</span>
        <button id="reset-btn" class="btn-secondary" type="button">Reset Demo State</button>
      </div>
      <table>
        <thead>
          <tr>
            <th>SKU Identifier</th>
            <th>Product Description</th>
            <th>Baseline Wholesale</th>
            <th>Active Wholesale</th>
            <th>Available Stock</th>
            <th>Vendor Promo Action</th>
          </tr>
        </thead>
        <tbody id="catalog-tbody"></tbody>
      </table>
      <div id="promo-banner" class="status-banner"></div>
    </div>

    <div class="panel">
      <div class="panel-title"><span>Live Vendor API Telemetry Log (/api/v1/promotions/active)</span></div>
      <ul id="event-log" class="event-list"></ul>
    </div>
  </div>

  <script>
    const CSRF_TOKEN = "__CSRF_TOKEN__";

    function renderCatalog(state) {
      const tbody = document.getElementById("catalog-tbody");
      tbody.replaceChildren();

      state.catalog.forEach((item) => {
        const tr = document.createElement("tr");

        const tdSku = document.createElement("td");
        const skuSpan = document.createElement("span");
        skuSpan.className = "sku-code";
        skuSpan.textContent = item.sku;
        tdSku.appendChild(skuSpan);

        const tdName = document.createElement("td");
        tdName.textContent = item.name;

        const tdBase = document.createElement("td");
        tdBase.textContent = "$" + Number(item.baseline_wholesale).toLocaleString("en-US", { minimumFractionDigits: 2 });

        const tdCurrent = document.createElement("td");
        if (item.promo_active) {
          const strike = document.createElement("span");
          strike.className = "price-strike";
          strike.textContent = "$" + Number(item.baseline_wholesale).toFixed(2);
          const promoPrice = document.createElement("span");
          promoPrice.className = "price-promo";
          promoPrice.textContent = "$" + Number(item.current_wholesale).toFixed(2) + " (20% OFF)";
          tdCurrent.appendChild(strike);
          tdCurrent.appendChild(promoPrice);
        } else {
          tdCurrent.textContent = "$" + Number(item.current_wholesale).toLocaleString("en-US", { minimumFractionDigits: 2 });
        }

        const tdStock = document.createElement("td");
        tdStock.textContent = String(item.available_qty) + " units";

        const tdAction = document.createElement("td");
        if (item.sku === "SKU-NET-MERAKI-MR36") {
          const btn = document.createElement("button");
          btn.type = "button";
          btn.id = "trigger-promo-btn";
          btn.className = "btn";
          if (item.promo_active) {
            btn.textContent = "20% Promo Active (PROMO-MERAKI-20)";
            btn.disabled = true;
          } else {
            btn.textContent = "Trigger 20% Vendor Promo";
            btn.addEventListener("click", triggerPromo);
          }
          tdAction.appendChild(btn);
        } else {
          const span = document.createElement("span");
          span.style.color = "#64748b";
          span.textContent = "Standard Baseline Pricing";
          tdAction.appendChild(span);
        }

        tr.appendChild(tdSku);
        tr.appendChild(tdName);
        tr.appendChild(tdBase);
        tr.appendChild(tdCurrent);
        tr.appendChild(tdStock);
        tr.appendChild(tdAction);
        tbody.appendChild(tr);
      });

      const banner = document.getElementById("promo-banner");
      if (state.active_promotions && state.active_promotions.length > 0) {
        const p = state.active_promotions[0];
        banner.style.display = "block";
        banner.textContent = "ACTIVE PROMO EMITTED: " + p.promo_code + " | SKU: " + p.sku +
          " discounted from $" + Number(p.baseline_wholesale).toFixed(2) + " to $" +
          Number(p.promo_wholesale).toFixed(2) +
          ". The 2-Legged Sentinel Agent will detect this on its next 5-second poll!";
      } else {
        banner.style.display = "none";
        banner.textContent = "";
      }

      const logEl = document.getElementById("event-log");
      logEl.replaceChildren();
      (state.events || []).slice().reverse().forEach((ev) => {
        const li = document.createElement("li");
        li.textContent = "[" + ev.timestamp + "] " + ev.event + " — " + (ev.detail || (ev.sku + " -> $" + ev.new_wholesale));
        logEl.appendChild(li);
      });
    }

    async function loadState() {
      const resp = await fetch("/api/v1/demo/state");
      const data = await resp.json();
      renderCatalog(data);
    }

    async function triggerPromo() {
      const resp = await fetch("/api/v1/demo/trigger-promo", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": CSRF_TOKEN }
      });
      if (resp.ok) {
        await loadState();
      }
    }

    document.getElementById("reset-btn").addEventListener("click", async () => {
      const resetBtn = document.getElementById("reset-btn");
      const origText = resetBtn.textContent;
      resetBtn.textContent = "Resetting Catalog, Quotes & OAuth...";
      resetBtn.disabled = true;
      try {
        await fetch("/api/v1/demo/reset", {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-CSRF-Token": CSRF_TOKEN }
        });
        await loadState();
        resetBtn.textContent = "✔ Reset Complete (Baseline MSRP)";
        setTimeout(() => { resetBtn.textContent = origText; resetBtn.disabled = false; }, 2000);
      } catch (e) {
        resetBtn.textContent = origText;
        resetBtn.disabled = false;
      }
    });

    loadState();
    setInterval(loadState, 3000);
  </script>
</body>
</html>
"""


@app.get("/demo-admin", response_class=HTMLResponse)
async def demo_admin_ui():
    """Serve the Demo Control Web UI (http://bigredreeves.com/demo-admin)."""
    return HTMLResponse(content=DEMO_ADMIN_HTML.replace("__CSRF_TOKEN__", ADMIN_CSRF_TOKEN))


app.mount("/", _catalog_mcp_app)


if __name__ == "__main__":
    import socket

    port = int(os.getenv("PORT", "8001"))
    sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
    sock.bind(("::", port))
    uvicorn.run(app, fd=sock.fileno())



