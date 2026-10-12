# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0

"""Component E: Gemini Enterprise Copilot Front-End & BFF (brianreevz.altostrat.com/copilot).

Provides:
1. Secure Backend-for-Frontend (BFF) managing 3-Legged OAuth sessions in server memory (HttpOnly cookies, zero localStorage).
2. Real-time Notification Toast polling/streaming from the 2-Legged Sentinel Agent.
3. Gemini Enterprise Copilot Chat Interface invoking MCP tools (list_my_active_quotes, get_promo_details, reprice_quote).
4. Reverse-proxy helper for /demo-admin so both bigredreeves.com and brianreevz.altostrat.com work via a single gateway port.
"""

from __future__ import annotations

import hmac
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] [copilot-web-ui] %(message)s")
logger = logging.getLogger("copilot-web-ui")


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

VENDOR_SERVICE_URL = (
    os.getenv("VENDOR_SERVICE_URL") or _ENV_DEFAULTS.get("VENDOR_SERVICE_URL", "http://127.0.0.1:8001")
).rstrip("/")
ERP_SERVICE_URL = (
    os.getenv("ERP_SERVICE_URL") or _ENV_DEFAULTS.get("ERP_SERVICE_URL", "http://127.0.0.1:8002")
).rstrip("/")
MCP_SERVICE_URL = (
    os.getenv("MCP_SERVICE_URL") or _ENV_DEFAULTS.get("MCP_SERVICE_URL", "http://127.0.0.1:8003")
).rstrip("/")
GCP_PROJECT_ID = os.getenv("GCP_PROJECT_ID", "gemini-enterprise-demo-508317")
TARGET_INTERNAL_USER = os.getenv("TARGET_INTERNAL_USER", "brian@brianreevz.altostrat.com")

# Server-side BFF session store (keeps 3-legged OAuth tokens off browser localStorage)
BFF_SESSIONS: dict[str, dict[str, Any]] = {}


async def ensure_3legged_session(user_email: str = TARGET_INTERNAL_USER) -> dict[str, Any]:
    """Perform 3-Legged OAuth (authorization_code) flow against internal-erp-service for the user."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        auth_resp = await client.get(
            f"{ERP_SERVICE_URL}/oauth/authorize",
            params={
                "response_type": "code",
                "client_id": "gemini-enterprise-copilot-client",
                "login_hint": user_email,
                "scope": "quotes:read quotes:write",
                "state": secrets.token_hex(8),
            },
            headers={"Accept": "application/json"},
        )
        auth_resp.raise_for_status()
        code = auth_resp.json()["code"]

        token_resp = await client.post(
            f"{ERP_SERVICE_URL}/oauth/token",
            json={"grant_type": "authorization_code", "code": code},
        )
        token_resp.raise_for_status()
        token_data = token_resp.json()

        session_id = secrets.token_urlsafe(32)
        csrf_token = secrets.token_hex(24)
        session_record = {
            "session_id": session_id,
            "csrf_token": csrf_token,
            "user_email": token_data["sub"],
            "roles": token_data.get("roles", ["sales_rep"]),
            "scope": token_data.get("scope", "quotes:read quotes:write"),
            "access_token": token_data["access_token"],
            "created_at": time.time(),
            "expires_at": time.time() + 3600,
        }
        BFF_SESSIONS[session_id] = session_record
        return session_record


async def get_or_create_bff_session(request: Request) -> dict[str, Any]:
    """Retrieve active BFF session from HttpOnly cookie or provision a fresh 3-legged session for Brian."""
    cookie_sid = request.cookies.get("__Secure-ge_session") or request.cookies.get("ge_session")
    if cookie_sid and cookie_sid in BFF_SESSIONS:
        sess = BFF_SESSIONS[cookie_sid]
        if sess["expires_at"] > time.time():
            return sess
    return await ensure_3legged_session(TARGET_INTERNAL_USER)


app = FastAPI(
    title="Gemini Enterprise Copilot Front-End & BFF (brianreevz.altostrat.com)",
    version="1.0.0",
)


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    if request.method not in ("GET", "POST", "OPTIONS", "HEAD"):
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


@app.get("/")
async def root_redirect(request: Request):
    host = request.headers.get("host", "")
    if "bigredreeves.com" in host:
        return RedirectResponse(url="/demo-admin")
    return RedirectResponse(url="/copilot")


# ---------------------------------------------------------------------------
# Proxy routes for /demo-admin (when accessed via unified gateway port)
# ---------------------------------------------------------------------------


@app.get("/demo-admin", response_class=HTMLResponse)
async def proxy_demo_admin():
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"{VENDOR_SERVICE_URL}/demo-admin")
        return HTMLResponse(content=resp.text, status_code=resp.status_code)


@app.get("/api/v1/demo/state")
async def proxy_demo_state():
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"{VENDOR_SERVICE_URL}/api/v1/demo/state")
        return JSONResponse(content=resp.json(), status_code=resp.status_code)


@app.post("/api/v1/demo/trigger-promo")
async def proxy_trigger_promo():
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(f"{VENDOR_SERVICE_URL}/api/v1/demo/trigger-promo")
        return JSONResponse(content=resp.json(), status_code=resp.status_code)


@app.post("/api/v1/demo/reset")
async def proxy_reset_all():
    async with httpx.AsyncClient(timeout=10.0) as client:
        await client.post(f"{VENDOR_SERVICE_URL}/api/v1/demo/reset")
        await client.post(f"{ERP_SERVICE_URL}/api/v1/demo/reset")
        return {"status": "all_reset"}


# ---------------------------------------------------------------------------
# Copilot BFF Endpoints (3-Legged Session, Notifications, MCP Tool Execution)
# ---------------------------------------------------------------------------


@app.get("/api/v1/copilot/context")
async def get_copilot_context(request: Request):
    """Return current 3-legged identity context, active quotes, notifications, and audit trail."""
    sess = await get_or_create_bff_session(request)
    user_token = sess["access_token"]

    async with httpx.AsyncClient(timeout=10.0) as client:
        quotes_resp = await client.post(
            f"{MCP_SERVICE_URL}/api/v1/tools/list_my_active_quotes",
            json={"user_token": user_token},
        )
        quotes_data = quotes_resp.json() if quotes_resp.status_code == 200 else {"quotes": []}

        notif_resp = await client.get(
            f"{ERP_SERVICE_URL}/api/v1/notifications",
            params={"recipient": sess["user_email"]},
        )
        notifications = notif_resp.json().get("notifications", []) if notif_resp.status_code == 200 else []

        audit_resp = await client.get(f"{ERP_SERVICE_URL}/api/v1/audit-logs")
        audit_logs = audit_resp.json().get("audit_logs", []) if audit_resp.status_code == 200 else []

    return {
        "identity": {
            "email": sess["user_email"],
            "badge": f"Authenticated as: {sess['user_email']} (3-Legged Token Active)",
            "roles": sess["roles"],
            "scope": sess["scope"],
            "gcp_project": GCP_PROJECT_ID,
            "domain": "brianreevz.altostrat.com",
        },
        "quotes": quotes_data.get("quotes", []),
        "notifications": notifications,
        "audit_logs": audit_logs[-8:],
    }


class ChatPromptRequest(BaseModel):
    prompt: str
    quote_id: int = 1042
    target_margin_percent: float = 18.0


@app.post("/api/v1/copilot/analyze")
async def analyze_quote_impact(request: Request, body: ChatPromptRequest):
    """Execute Gemini Enterprise MCP tool chain (list_my_active_quotes + get_promo_details) using Brian's 3L token."""
    sess = await get_or_create_bff_session(request)
    csrf_header = request.headers.get("x-csrf-token", "")
    if csrf_header and not hmac.compare_digest(csrf_header, sess["csrf_token"]):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")

    user_token = sess["access_token"]
    async with httpx.AsyncClient(timeout=10.0) as client:
        quotes_resp = await client.post(
            f"{MCP_SERVICE_URL}/api/v1/tools/list_my_active_quotes",
            json={"user_token": user_token},
        )
        quotes_resp.raise_for_status()
        quotes = quotes_resp.json().get("quotes", [])

        promo_resp = await client.post(
            f"{MCP_SERVICE_URL}/api/v1/tools/get_promo_details",
            json={"promo_code": "PROMO-MERAKI-20"},
        )
        promo_resp.raise_for_status()
        promo_data = promo_resp.json()

    quote_1042 = next((q for q in quotes if q["quote_id"] == body.quote_id), quotes[0] if quotes else None)
    if not quote_1042:
        raise HTTPException(status_code=404, detail=f"Quote #{body.quote_id} not found in user's portfolio")

    promo = promo_data.get("promotion") or {
        "promo_code": "PROMO-MERAKI-20",
        "sku": "SKU-NET-MERAKI-MR36",
        "discount_percent": 20.0,
        "baseline_wholesale": 450.00,
        "promo_wholesale": 360.00,
    }

    qty = int(quote_1042["qty"])
    before_cost = float(quote_1042["baseline_cost"])
    before_price = float(quote_1042["quoted_price"])
    before_margin = round(((before_price - before_cost) / before_price) * 100.0, 2)
    before_total = round(before_price * qty, 2)

    after_cost = float(promo["promo_wholesale"])
    target_margin = float(body.target_margin_percent)
    after_price = round(after_cost / (1.0 - (target_margin / 100.0)), 2)
    after_total = round(after_price * qty, 2)
    customer_savings = round(before_total - after_total, 2)

    return {
        "agent_model": "gemini-2.5-pro (Vertex AI Agent Engine / FastMCP)",
        "delegated_identity": sess["user_email"],
        "mcp_tools_invoked": [
            "list_my_active_quotes(user_token=<3-Legged-Bearer>)",
            "get_promo_details(promo_code='PROMO-MERAKI-20')",
        ],
        "analysis": {
            "quote_id": quote_1042["quote_id"],
            "client": quote_1042["client"],
            "sku": quote_1042["sku"],
            "qty": qty,
            "promo_code": promo["promo_code"],
            "before": {
                "unit_cost": before_cost,
                "quoted_price": before_price,
                "margin_percent": before_margin,
                "total_quote": before_total,
            },
            "after": {
                "unit_cost": after_cost,
                "quoted_price": after_price,
                "margin_percent": target_margin,
                "total_quote": after_total,
                "customer_savings": customer_savings,
            },
        },
        "summary_text": (
            f"Vendor promo {promo['promo_code']} lowers SKU-NET-MERAKI-MR36 wholesale cost by 20% "
            f"(${before_cost:.2f} -> ${after_cost:.2f}). Repricing Quote #1042 for Acme Corp (Qty: {qty}) "
            f"to maintain an {target_margin:.1f}% gross margin sets the new unit price to ${after_price:.2f} "
            f"(down from ${before_price:.2f}), saving Acme Corp ${customer_savings:,.2f} while boosting "
            f"your margin from {before_margin:.2f}% to {target_margin:.2f}%."
        ),
    }


class ConfirmRepriceRequest(BaseModel):
    quote_id: int = 1042
    promo_code: str = "PROMO-MERAKI-20"
    target_margin_percent: float = 18.0


@app.post("/api/v1/copilot/confirm-reprice")
async def confirm_reprice_quote(request: Request, body: ConfirmRepriceRequest):
    """Execute MCP tool reprice_quote(...) using Brian's 3-Legged token upon explicit human confirmation."""
    sess = await get_or_create_bff_session(request)
    csrf_header = request.headers.get("x-csrf-token", "")
    if csrf_header and not hmac.compare_digest(csrf_header, sess["csrf_token"]):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            f"{MCP_SERVICE_URL}/api/v1/tools/reprice_quote",
            json={
                "quote_id": body.quote_id,
                "promo_code": body.promo_code,
                "target_margin_percent": body.target_margin_percent,
                "user_token": sess["access_token"],
            },
        )
        if resp.status_code != 200:
            raise HTTPException(status_code=resp.status_code, detail=resp.json().get("detail", resp.text))
        return resp.json()


@app.post("/api/v1/copilot/test-rbac-violation")
async def test_rbac_violation(request: Request):
    """Demonstrate 3-Legged Tenant Isolation: attempt to mutate Dave's Quote #9999 using Brian's 3-legged token."""
    sess = await get_or_create_bff_session(request)
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.patch(
            f"{ERP_SERVICE_URL}/api/v1/quotes/9999",
            headers={"Authorization": f"Bearer {sess['access_token']}"},
            json={"quoted_price": 400.00},
        )
        return {
            "attempted_quote_id": 9999,
            "attempted_by": sess["user_email"],
            "http_status": resp.status_code,
            "detail": resp.json().get("detail"),
        }


@app.get("/health")
async def health():
    return {"status": "ok", "service": "copilot-web-ui"}


COPILOT_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Gemini Enterprise Copilot | brianreevz.altostrat.com</title>
  <style>
    :root {
      --bg: #0b0f19;
      --surface: #111827;
      --surface-2: #1f2937;
      --border: #374151;
      --text: #f9fafb;
      --muted: #9ca3af;
      --primary: #3b82f6;
      --primary-hover: #2563eb;
      --emerald: #10b981;
      --amber: #f59e0b;
      --danger: #ef4444;
    }
    * { box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
    body { margin: 0; background: var(--bg); color: var(--text); min-height: 100vh; display: flex; flex-direction: column; }
    .topbar {
      display: flex; justify-content: space-between; align-items: center;
      padding: 14px 28px; background: var(--surface); border-bottom: 1px solid var(--border);
    }
    .brand { display: flex; align-items: center; gap: 12px; }
    .sparkle {
      width: 28px; height: 28px; border-radius: 8px;
      background: linear-gradient(135deg, #3b82f6, #8b5cf6);
      display: flex; align-items: center; justify-content: center; font-weight: 800;
    }
    .brand h1 { margin: 0; font-size: 17px; font-weight: 700; }
    .brand span { font-size: 12px; color: var(--muted); }
    .identity-badge {
      background: rgba(16, 185, 129, 0.14); border: 1px solid rgba(16, 185, 129, 0.45);
      color: #6ee7b7; padding: 8px 16px; border-radius: 999px; font-size: 13px; font-weight: 600;
    }
    .toast-bar {
      margin: 16px 28px 0 28px; padding: 14px 20px; border-radius: 10px;
      background: linear-gradient(90deg, rgba(245, 158, 11, 0.18), rgba(59, 130, 246, 0.18));
      border: 1px solid var(--amber); display: none; justify-content: space-between;
      align-items: center; cursor: pointer; transition: transform 0.12s ease;
    }
    .toast-bar:hover { transform: translateY(-1px); }
    .toast-title { font-weight: 700; color: #fde68a; font-size: 14px; margin-bottom: 4px; }
    .toast-msg { font-size: 13px; color: var(--text); }
    .toast-cta {
      background: var(--amber); color: #111827; font-weight: 700; font-size: 12px;
      padding: 8px 14px; border-radius: 6px; white-space: nowrap; margin-left: 16px;
    }
    .workspace {
      display: grid; grid-template-columns: 380px 1fr; gap: 20px;
      padding: 20px 28px; flex: 1; max-width: 1440px; width: 100%; margin: 0 auto;
    }
    .card {
      background: var(--surface); border: 1px solid var(--border);
      border-radius: 12px; padding: 18px; margin-bottom: 16px;
    }
    .card h2 { margin: 0 0 12px 0; font-size: 14px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); }
    .quote-box {
      background: var(--surface-2); border: 1px solid var(--border);
      border-radius: 8px; padding: 14px; font-size: 13px;
    }
    .quote-row { display: flex; justify-content: space-between; margin-bottom: 8px; }
    .quote-val { font-weight: 700; }
    .chat-container {
      background: var(--surface); border: 1px solid var(--border);
      border-radius: 12px; display: flex; flex-direction: column; height: 680px;
    }
    .chat-messages { flex: 1; padding: 20px; overflow-y: auto; display: flex; flex-direction: column; gap: 16px; }
    .msg { max-width: 85%; padding: 14px 18px; border-radius: 12px; font-size: 14px; line-height: 1.5; }
    .msg-user { align-self: flex-end; background: var(--primary); color: #fff; }
    .msg-agent { align-self: flex-start; background: var(--surface-2); border: 1px solid var(--border); }
    .tool-pill {
      display: inline-block; background: #1e1b4b; color: #a5b4fc; border: 1px solid #4338ca;
      padding: 4px 10px; border-radius: 6px; font-family: monospace; font-size: 11px; margin: 4px 6px 8px 0;
    }
    .delta-grid {
      display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin: 12px 0;
      background: #0f172a; padding: 12px; border-radius: 8px; border: 1px solid var(--border);
    }
    .btn-confirm {
      background: var(--emerald); color: #052e16; border: none; padding: 10px 18px;
      border-radius: 8px; font-weight: 700; font-size: 13px; cursor: pointer; margin-top: 8px;
    }
    .btn-confirm:hover { opacity: 0.92; }
    .btn-confirm:disabled { background: #374151; color: var(--muted); cursor: not-allowed; }
    .chat-input-bar {
      padding: 14px 18px; border-top: 1px solid var(--border); display: flex; gap: 10px;
    }
    .chat-input {
      flex: 1; background: var(--surface-2); border: 1px solid var(--border);
      color: var(--text); padding: 12px 14px; border-radius: 8px; font-size: 14px;
    }
    .btn-send {
      background: var(--primary); color: #fff; border: none; padding: 0 20px;
      border-radius: 8px; font-weight: 600; cursor: pointer;
    }
    .btn-outline {
      width: 100%; background: transparent; border: 1px solid var(--border);
      color: var(--muted); padding: 8px 12px; border-radius: 6px; font-size: 12px; cursor: pointer; margin-top: 8px;
    }
    .btn-outline:hover { color: var(--text); border-color: var(--text); }
    .audit-list { list-style: none; padding: 0; margin: 0; font-family: monospace; font-size: 11px; color: var(--muted); }
    .audit-list li { padding: 6px 0; border-bottom: 1px dashed var(--border); }
  </style>
</head>
<body>
  <div class="topbar">
    <div class="brand">
      <div class="sparkle">G</div>
      <div>
        <h1>Gemini Enterprise Copilot &mdash; Deal Desk Assistant</h1>
        <span>GCP Project: gemini-enterprise-demo-508317 | Domain: brianreevz.altostrat.com</span>
      </div>
    </div>
    <div id="identity-badge" class="identity-badge">
      Authenticated as: brian@brianreevz.altostrat.com (3-Legged Token Active)
    </div>
  </div>

  <div id="sentinel-toast" class="toast-bar">
    <div>
      <div class="toast-title">2-Legged Sentinel Agent Notification (Handoff to 3-Legged Copilot)</div>
      <div id="toast-message" class="toast-msg"></div>
    </div>
    <div class="toast-cta">Click to Analyze &amp; Reprice in Copilot &rarr;</div>
  </div>

  <div class="workspace">
    <div>
      <div class="card">
        <h2>My Active Quotes (3-Legged RBAC Filtered)</h2>
        <div id="quotes-panel"></div>
        <button id="rbac-test-btn" class="btn-outline" type="button">
          Test RBAC Isolation: Attempt to Mutate Dave's Quote #9999
        </button>
      </div>

      <div class="card">
        <h2>Immutable Security &amp; Quote Audit Trail</h2>
        <ul id="audit-panel" class="audit-list"></ul>
      </div>
    </div>

    <div class="chat-container">
      <div id="chat-messages" class="chat-messages">
        <div class="msg msg-agent">
          Welcome, Brian! Your 3-Legged OAuth session is active with scopes
          <code>quotes:read quotes:write</code>. When the headless 2-Legged Sentinel Agent detects a
          vendor promotion on <code>bigredreeves.com</code>, click the notification toast above to evaluate
          and reprice your eligible quotes.
        </div>
      </div>
      <div class="chat-input-bar">
        <input
          id="prompt-input"
          class="chat-input"
          type="text"
          placeholder="Ask Gemini Copilot to analyze vendor promotions or reprice Quote #1042..."
        />
        <button id="send-btn" class="btn-send" type="button">Send</button>
      </div>
    </div>
  </div>

  <script>
    const CSRF_TOKEN = "__CSRF_TOKEN__";
    const TARGET_PROMPT = "Show me the impact of the new vendor promotion on my Quote #1042 and reprice it to maintain an 18% margin.";

    function renderQuotes(quotes) {
      const container = document.getElementById("quotes-panel");
      container.replaceChildren();
      quotes.forEach((q) => {
        const box = document.createElement("div");
        box.className = "quote-box";

        const rows = [
          ["Quote ID", "#" + q.quote_id + " (" + q.status + ")"],
          ["Owner (3L Sub)", q.owner],
          ["Customer", q.client],
          ["Line Item", q.sku + " (Qty: " + q.qty + ")"],
          ["Wholesale Cost", "$" + Number(q.unit_cost).toFixed(2)],
          ["Quoted Unit Price", "$" + Number(q.quoted_price).toFixed(2)],
          ["Gross Margin", Number(q.margin_percent).toFixed(2) + "%"],
          ["Total Quote", "$" + Number(q.total_price).toLocaleString("en-US", { minimumFractionDigits: 2 })],
          ["Last Updated By", q.updated_by]
        ];
        rows.forEach(([label, val]) => {
          const r = document.createElement("div");
          r.className = "quote-row";
          const s1 = document.createElement("span");
          s1.textContent = label;
          const s2 = document.createElement("span");
          s2.className = "quote-val";
          s2.textContent = String(val);
          r.appendChild(s1);
          r.appendChild(s2);
          box.appendChild(r);
        });
        container.appendChild(box);
      });
    }

    function renderAuditLogs(logs) {
      const ul = document.getElementById("audit-panel");
      ul.replaceChildren();
      if (!logs || logs.length === 0) {
        const li = document.createElement("li");
        li.textContent = "No quote mutations recorded yet.";
        ul.appendChild(li);
        return;
      }
      logs.slice().reverse().forEach((entry) => {
        const li = document.createElement("li");
        li.textContent = "[" + entry.outcome + "] " + entry.action + " | Quote #" +
          (entry.quote_id || "-") + " | updated_by: " + entry.updated_by;
        ul.appendChild(li);
      });
    }

    async function refreshContext() {
      const resp = await fetch("/api/v1/copilot/context");
      if (!resp.ok) return;
      const data = await resp.json();
      document.getElementById("identity-badge").textContent = data.identity.badge;
      renderQuotes(data.quotes || []);
      renderAuditLogs(data.audit_logs || []);

      const toast = document.getElementById("sentinel-toast");
      const toastMsg = document.getElementById("toast-message");
      if (data.notifications && data.notifications.length > 0) {
        const latest = data.notifications[data.notifications.length - 1];
        toastMsg.textContent = latest.message;
        toast.style.display = "flex";
      } else {
        toast.style.display = "none";
      }
    }

    function appendUserMessage(text) {
      const chat = document.getElementById("chat-messages");
      const div = document.createElement("div");
      div.className = "msg msg-user";
      div.textContent = text;
      chat.appendChild(div);
      chat.scrollTop = chat.scrollHeight;
    }

    function appendAnalysisCard(result) {
      const chat = document.getElementById("chat-messages");
      const msg = document.createElement("div");
      msg.className = "msg msg-agent";

      result.mcp_tools_invoked.forEach((toolName) => {
        const pill = document.createElement("span");
        pill.className = "tool-pill";
        pill.textContent = "MCP: " + toolName;
        msg.appendChild(pill);
      });

      const p = document.createElement("p");
      p.textContent = result.summary_text;
      msg.appendChild(p);

      const grid = document.createElement("div");
      grid.className = "delta-grid";

      const beforeCol = document.createElement("div");
      const bTitle = document.createElement("strong");
      bTitle.textContent = "Before Promo (Baseline)";
      const bBody = document.createElement("div");
      bBody.textContent = "Cost: $" + result.analysis.before.unit_cost.toFixed(2) +
        " | Price: $" + result.analysis.before.quoted_price.toFixed(2) +
        " | Margin: " + result.analysis.before.margin_percent.toFixed(2) + "%";
      beforeCol.appendChild(bTitle);
      beforeCol.appendChild(bBody);

      const afterCol = document.createElement("div");
      const aTitle = document.createElement("strong");
      aTitle.style.color = "#10b981";
      aTitle.textContent = "After 20% Promo (18% Target Margin)";
      const aBody = document.createElement("div");
      aBody.textContent = "Cost: $" + result.analysis.after.unit_cost.toFixed(2) +
        " | New Price: $" + result.analysis.after.quoted_price.toFixed(2) +
        " | Margin: " + result.analysis.after.margin_percent.toFixed(2) + "%";
      afterCol.appendChild(aTitle);
      afterCol.appendChild(aBody);

      grid.appendChild(beforeCol);
      grid.appendChild(afterCol);
      msg.appendChild(grid);

      const confirmBtn = document.createElement("button");
      confirmBtn.type = "button";
      confirmBtn.className = "btn-confirm";
      confirmBtn.textContent = "[Confirm & Update Quote #1042]";
      confirmBtn.addEventListener("click", async () => {
        confirmBtn.disabled = true;
        confirmBtn.textContent = "Updating Quote via 3-Legged Token...";
        const patchResp = await fetch("/api/v1/copilot/confirm-reprice", {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-CSRF-Token": CSRF_TOKEN },
          body: JSON.stringify({
            quote_id: result.analysis.quote_id,
            promo_code: result.analysis.promo_code,
            target_margin_percent: result.analysis.after.margin_percent
          })
        });
        const patchData = await patchResp.json();
        confirmBtn.textContent = "Quote #1042 Updated (Audit: " + patchData.audit_record.updated_by + ")";

        const confirmNotice = document.createElement("div");
        confirmNotice.className = "msg msg-agent";
        confirmNotice.textContent = "MCP Tool reprice_quote(1042, 'PROMO-MERAKI-20', 18.0) succeeded! " +
          "Quote #1042 unit price is now $" + Number(patchData.quote.quoted_price).toFixed(2) +
          " (18.0% margin). Audit log recorded updated_by: " + patchData.audit_record.updated_by + ".";
        chat.appendChild(confirmNotice);
        chat.scrollTop = chat.scrollHeight;
        await refreshContext();
      });
      msg.appendChild(confirmBtn);

      chat.appendChild(msg);
      chat.scrollTop = chat.scrollHeight;
    }

    async function sendPrompt() {
      const input = document.getElementById("prompt-input");
      const text = input.value.trim() || TARGET_PROMPT;
      input.value = "";
      appendUserMessage(text);

      const resp = await fetch("/api/v1/copilot/analyze", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": CSRF_TOKEN },
        body: JSON.stringify({ prompt: text, quote_id: 1042, target_margin_percent: 18.0 })
      });
      if (resp.ok) {
        const data = await resp.json();
        appendAnalysisCard(data);
      }
    }

    document.getElementById("sentinel-toast").addEventListener("click", () => {
      const input = document.getElementById("prompt-input");
      input.value = TARGET_PROMPT;
      input.focus();
    });

    document.getElementById("send-btn").addEventListener("click", sendPrompt);
    document.getElementById("prompt-input").addEventListener("keydown", (e) => {
      if (e.key === "Enter") sendPrompt();
    });

    document.getElementById("rbac-test-btn").addEventListener("click", async () => {
      const resp = await fetch("/api/v1/copilot/test-rbac-violation", {
        method: "POST",
        headers: { "X-CSRF-Token": CSRF_TOKEN }
      });
      const data = await resp.json();
      const chat = document.getElementById("chat-messages");
      const div = document.createElement("div");
      div.className = "msg msg-agent";
      div.textContent = "[RBAC Security Check] Attempted PATCH /api/v1/quotes/9999 (Dave's Cyberdyne quote) using Brian's 3-Legged token -> HTTP " +
        data.http_status + ": " + data.detail;
      chat.appendChild(div);
      chat.scrollTop = chat.scrollHeight;
      await refreshContext();
    });

    refreshContext();
    setInterval(refreshContext, 2500);
  </script>
</body>
</html>
"""


@app.get("/copilot", response_class=HTMLResponse)
async def copilot_ui(request: Request):
    """Serve the Gemini Enterprise Copilot Front-End (http://brianreevz.altostrat.com/copilot)."""
    sess = await get_or_create_bff_session(request)
    html_content = COPILOT_HTML.replace("__CSRF_TOKEN__", sess["csrf_token"])
    response = HTMLResponse(content=html_content)
    response.set_cookie(
        key="ge_session",
        value=sess["session_id"],
        httponly=True,
        samesite="lax",
        max_age=3600,
    )
    return response


if __name__ == "__main__":
    host = os.getenv("BIND_HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8080"))
    uvicorn.run(app, host=host, port=port)
