# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0

"""Component B: Internal Quote & Identity Service (brianreevz.altostrat.com).

Provides:
1. Mock 3-Legged OAuth / OIDC Identity Provider (/oauth/authorize & /oauth/token)
2. Quotes Repository API with strict RBAC & Tenant Isolation (GET/PATCH /api/v1/quotes)
3. 2-Legged Least Privilege enforcement (rejects M2M tokens on PATCH /api/v1/quotes/{id})
4. In-App Notification Queue & SSE Stream (/api/v1/notifications/stream)
5. Immutable Audit Log (/api/v1/audit-logs) recording updated_by: brian@brianreevz.altostrat.com
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import jwt
import uvicorn
from fastapi import FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] [internal-erp] %(message)s")
logger = logging.getLogger("internal-erp")

JWT_ALGORITHM = "HS256"
GCP_PROJECT_ID = os.getenv("GCP_PROJECT_ID", "gemini-enterprise-demo-508317")
INTERNAL_DOMAIN = os.getenv("INTERNAL_DOMAIN", "brianreevz.altostrat.com")


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
    """Resolve secret via Environment -> Local File -> Ephemeral CSPRNG fallback."""
    val = os.getenv(env_key) or _ENV_DEFAULTS.get(env_key)
    if val:
        return val
    if secret_file and os.path.exists(secret_file):
        with open(secret_file, encoding="utf-8") as fh:
            return fh.read().strip()
    logger.warning("Generating ephemeral secret for %s. Instance-isolated!", env_key)
    return secrets.token_hex(32)


ERP_JWT_SECRET = get_secret("ERP_JWT_SECRET")
SENTINEL_INTERNAL_CLIENT_ID = os.getenv("SENTINEL_INTERNAL_CLIENT_ID") or _ENV_DEFAULTS.get(
    "SENTINEL_INTERNAL_CLIENT_ID", "sentinel-internal-reader"
)
SENTINEL_INTERNAL_CLIENT_SECRET = get_secret("SENTINEL_INTERNAL_CLIENT_SECRET")
COPILOT_OAUTH_CLIENT_ID = os.getenv("COPILOT_OAUTH_CLIENT_ID") or _ENV_DEFAULTS.get(
    "COPILOT_OAUTH_CLIENT_ID", "gemini-enterprise-copilot-client"
)
COPILOT_OAUTH_CLIENT_SECRET = get_secret("COPILOT_OAUTH_CLIENT_SECRET")

ALLOWED_USERS = {
    "brian@brianreevz.altostrat.com": {
        "sub": "brian@brianreevz.altostrat.com",
        "name": "Brian Reeves",
        "title": "Enterprise Account Executive",
        "roles": ["sales_rep"],
        "scope": "quotes:read quotes:write",
    },
    "dave@brianreevz.altostrat.com": {
        "sub": "dave@brianreevz.altostrat.com",
        "name": "Dave Miller",
        "title": "Commercial Account Executive",
        "roles": ["sales_rep"],
        "scope": "quotes:read quotes:write",
    },
}

ALLOWED_REDIRECT_PREFIXES = (
    "http://brianreevz.altostrat.com",
    "http://127.0.0.1",
    "http://localhost",
)

# ---------------------------------------------------------------------------
# In-Memory Database State (Quotes, Auth Codes, Notifications, Audit Trail)
# ---------------------------------------------------------------------------

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

ERP_STATE: dict[str, Any] = {
    "quotes": {qid: dict(q) for qid, q in INITIAL_QUOTES.items()},
    "auth_codes": {},
    "notifications": [],
    "audit_logs": [],
}


def reset_erp_state() -> None:
    ERP_STATE["quotes"] = {qid: dict(q) for qid, q in INITIAL_QUOTES.items()}
    ERP_STATE["auth_codes"] = {}
    ERP_STATE["notifications"] = []
    ERP_STATE["audit_logs"] = []


def record_audit_event(
    action: str,
    actor: str,
    auth_type: str,
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
        "auth_type": auth_type,
        "quote_id": quote_id,
        "outcome": outcome,
        "details": details,
    }
    ERP_STATE["audit_logs"].append(entry)
    logger.info("AUDIT [%s] action=%s actor=%s auth=%s quote=%s", outcome, action, actor, auth_type, quote_id)
    return entry


# ---------------------------------------------------------------------------
# FastAPI Application & Security Headers
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Internal Quote & OIDC Identity Service (brianreevz.altostrat.com)",
    version="1.0.0",
)


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    if request.method not in ("GET", "POST", "PATCH", "OPTIONS", "HEAD"):
        return JSONResponse(status_code=405, content={"detail": "Method Not Allowed"})
    response: Response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = "default-src 'self'; frame-ancestors 'none';"
    return response


def decode_bearer_token(authorization: str | None) -> dict[str, Any]:
    """Decode and validate a Bearer JWT issued by brianreevz.altostrat.com."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization Bearer header",
        )
    raw_token = authorization.removeprefix("Bearer ").strip()

    # First check if a foreign/untrusted M2M token (e.g., from bigredreeves.com) or unsigned token was presented
    try:
        unverified_claims = jwt.decode(raw_token, options={"verify_signature": False})
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Malformed JWT bearer token: {exc}",
        ) from exc

    # If the caller presented a 2-legged token from bigredreeves.com or sentinel-agent,
    # flag it as a 2-legged M2M token so endpoints requiring 3-legged user delegation return 403 Forbidden!
    if unverified_claims.get("auth_type") == "2_legged_m2m" or unverified_claims.get("iss") == "https://bigredreeves.com":
        try:
            # Verify signature if issued by internal ERP; if issued by vendor, still classify as 2-legged M2M
            claims = jwt.decode(
                raw_token,
                ERP_JWT_SECRET,
                algorithms=[JWT_ALGORITHM],
                options={"require": ["exp", "iat", "sub"]},
            )
            return claims
        except jwt.PyJWTError:
            # Foreign 2-legged vendor token presented against internal ERP
            return {
                "sub": str(unverified_claims.get("sub", "xvantage-sentinel-app")),
                "iss": str(unverified_claims.get("iss", "https://bigredreeves.com")),
                "auth_type": "2_legged_m2m",
                "scope": str(unverified_claims.get("scope", "")),
                "roles": ["machine_sentinel"],
            }

    try:
        claims = jwt.decode(
            raw_token,
            ERP_JWT_SECRET,
            algorithms=[JWT_ALGORITHM],
            options={"require": ["exp", "iat", "sub", "scope"]},
        )
        return claims
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid or expired internal OAuth token: {exc}",
        ) from exc


# ---------------------------------------------------------------------------
# 1. Mock 3-Legged OAuth / OIDC Identity Provider (/oauth/authorize & /oauth/token)
# ---------------------------------------------------------------------------


@app.get("/oauth/authorize")
async def oauth_authorize(
    request: Request,
    response_type: str = Query(default="code"),
    client_id: str = Query(default=COPILOT_OAUTH_CLIENT_ID),
    redirect_uri: str | None = Query(default=None),
    state: str = Query(default=""),
    login_hint: str = Query(default="brian@brianreevz.altostrat.com"),
    scope: str = Query(default="quotes:read quotes:write"),
):
    """Simulate SSO OIDC Authorization Endpoint for brian@brianreevz.altostrat.com."""
    if response_type != "code":
        raise HTTPException(status_code=400, detail="Unsupported response_type. Expected 'code'.")

    user_email = login_hint.strip().lower()
    if user_email not in ALLOWED_USERS:
        raise HTTPException(
            status_code=403,
            detail=f"User {user_email} is not in internal directory {INTERNAL_DOMAIN}.",
        )

    auth_code = f"authcode_{secrets.token_urlsafe(24)}"
    ERP_STATE["auth_codes"][auth_code] = {
        "code": auth_code,
        "client_id": client_id,
        "user": ALLOWED_USERS[user_email],
        "scope": scope,
        "redirect_uri": redirect_uri,
        "expires_at": time.time() + 300,
    }
    logger.info("3-Legged OAuth /oauth/authorize issued code for sub=%s", user_email)

    accept = request.headers.get("accept", "")
    if "application/json" in accept or not redirect_uri:
        return {
            "code": auth_code,
            "state": state,
            "login_hint": user_email,
            "issuer": f"https://{INTERNAL_DOMAIN}",
        }

    if not any(redirect_uri.startswith(prefix) for prefix in ALLOWED_REDIRECT_PREFIXES):
        raise HTTPException(status_code=400, detail="Untrusted redirect_uri")

    sep = "&" if "?" in redirect_uri else "?"
    target = f"{redirect_uri}{sep}{urlencode({'code': auth_code, 'state': state})}"
    return RedirectResponse(url=target, status_code=status.HTTP_302_FOUND)


@app.post("/oauth/token")
async def oauth_token(request: Request):
    """Exchange 3-Legged authorization_code (or internal M2M client_credentials) for a JWT."""
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        body = await request.json()
    else:
        form = await request.form()
        body = dict(form)

    grant_type = str(body.get("grant_type", ""))
    now = int(time.time())

    if grant_type == "authorization_code":
        code = str(body.get("code", ""))
        code_record = ERP_STATE["auth_codes"].pop(code, None)
        if not code_record or code_record["expires_at"] < time.time():
            raise HTTPException(status_code=400, detail="Invalid or expired authorization_code")

        user_info = code_record["user"]
        payload = {
            "iss": f"https://{INTERNAL_DOMAIN}",
            "sub": user_info["sub"],
            "name": user_info["name"],
            "roles": user_info["roles"],
            "scope": "quotes:read quotes:write",
            "auth_type": "3_legged_user_delegated",
            "gcp_project": GCP_PROJECT_ID,
            "org": INTERNAL_DOMAIN,
            "iat": now,
            "exp": now + 3600,
            "jti": secrets.token_hex(12),
        }
        token = jwt.encode(payload, ERP_JWT_SECRET, algorithm=JWT_ALGORITHM)
        logger.info("Issued 3-Legged user-delegated token for sub=%s", user_info["sub"])
        return {
            "access_token": token,
            "id_token": token,
            "token_type": "Bearer",
            "expires_in": 3600,
            "scope": payload["scope"],
            "sub": user_info["sub"],
            "roles": user_info["roles"],
            "auth_type": "3_legged_user_delegated",
        }

    if grant_type == "client_credentials":
        client_id = str(body.get("client_id", ""))
        client_secret = str(body.get("client_secret", ""))
        id_ok = hmac.compare_digest(client_id, SENTINEL_INTERNAL_CLIENT_ID)
        sec_ok = hmac.compare_digest(client_secret, SENTINEL_INTERNAL_CLIENT_SECRET)
        if not (id_ok and sec_ok):
            raise HTTPException(status_code=401, detail="Invalid internal M2M credentials")

        payload = {
            "iss": f"https://{INTERNAL_DOMAIN}",
            "sub": f"sentinel-agent@{GCP_PROJECT_ID}.iam.gserviceaccount.com",
            "roles": ["machine_sentinel"],
            "scope": "quotes:match_sku notifications:publish",
            "auth_type": "2_legged_m2m",
            "gcp_project": GCP_PROJECT_ID,
            "iat": now,
            "exp": now + 3600,
            "jti": secrets.token_hex(12),
        }
        token = jwt.encode(payload, ERP_JWT_SECRET, algorithm=JWT_ALGORITHM)
        return {
            "access_token": token,
            "token_type": "Bearer",
            "expires_in": 3600,
            "scope": payload["scope"],
            "sub": payload["sub"],
            "auth_type": "2_legged_m2m",
        }

    raise HTTPException(status_code=400, detail="Unsupported grant_type")


# ---------------------------------------------------------------------------
# 2. Quotes Repository API (Strict RBAC & 2-Legged vs 3-Legged Enforcement)
# ---------------------------------------------------------------------------


class QuotePatchRequest(BaseModel):
    unit_cost: float | None = Field(default=None, gt=0)
    quoted_price: float | None = Field(default=None, gt=0)
    margin_percent: float | None = Field(default=None, ge=0, lt=100)
    promo_applied: str | None = None


@app.get("/api/v1/quotes")
async def list_quotes(authorization: str | None = Header(default=None)):
    """Return quotes strictly filtered by the authenticated 3-legged user's identity."""
    claims = decode_bearer_token(authorization)
    if claims.get("auth_type") != "3_legged_user_delegated":
        record_audit_event(
            action="LIST_QUOTES_DENIED",
            actor=str(claims.get("sub", "unknown")),
            auth_type=str(claims.get("auth_type", "unknown")),
            quote_id=None,
            outcome="403_FORBIDDEN",
            details={"reason": "2-Legged M2M tokens cannot list user-scoped quote portfolios"},
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="403 Forbidden: Listing user quotes requires a 3-Legged user-delegated token.",
        )

    user_sub = str(claims["sub"])
    user_quotes = [q for q in ERP_STATE["quotes"].values() if q["owner"] == user_sub]
    return {
        "authenticated_user": user_sub,
        "auth_type": claims["auth_type"],
        "roles": claims.get("roles", []),
        "quotes": user_quotes,
    }


@app.get("/api/v1/quotes/{quote_id}")
async def get_quote_by_id(quote_id: int, authorization: str | None = Header(default=None)):
    """Fetch a specific quote by ID, strictly enforcing tenant/owner isolation."""
    claims = decode_bearer_token(authorization)
    if claims.get("auth_type") != "3_legged_user_delegated":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="403 Forbidden: Reading full quote details requires a 3-Legged user token.",
        )

    quote = ERP_STATE["quotes"].get(quote_id)
    if not quote:
        raise HTTPException(status_code=404, detail=f"Quote #{quote_id} not found")

    user_sub = str(claims["sub"])
    if quote["owner"] != user_sub:
        record_audit_event(
            action="READ_QUOTE_RBAC_VIOLATION",
            actor=user_sub,
            auth_type=str(claims.get("auth_type")),
            quote_id=quote_id,
            outcome="403_FORBIDDEN",
            details={"quote_owner": quote["owner"], "attempted_by": user_sub},
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"403 Forbidden: Tenant isolation check failed. {user_sub} cannot access Quote #{quote_id} owned by {quote['owner']}.",
        )

    return quote


@app.patch("/api/v1/quotes/{quote_id}")
async def patch_quote(
    quote_id: int,
    patch_body: QuotePatchRequest,
    authorization: str | None = Header(default=None),
):
    """Mutate pricing/margins on a quote.

    Enforces:
    1. 2-Legged Least Privilege: Rejects M2M tokens with 403 Forbidden.
    2. 3-Legged Tenant Isolation: Rejects cross-user mutations (e.g., Brian -> Quote #9999) with 403 Forbidden.
    3. Audit Trail Verification: Explicitly logs updated_by = brian@brianreevz.altostrat.com.
    """
    claims = decode_bearer_token(authorization)
    actor_sub = str(claims.get("sub", "unknown"))
    auth_type = str(claims.get("auth_type", "unknown"))
    scopes = str(claims.get("scope", "")).split()

    # CRITICAL SECURITY CHECK 1: 2-Legged Least Privilege
    if auth_type != "3_legged_user_delegated" or "quotes:write" not in scopes:
        record_audit_event(
            action="PATCH_QUOTE_M2M_BLOCKED",
            actor=actor_sub,
            auth_type=auth_type,
            quote_id=quote_id,
            outcome="403_FORBIDDEN",
            details={
                "reason": "2-Legged M2M token attempted privileged state mutation (PATCH /api/v1/quotes)",
                "presented_scope": claims.get("scope"),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "403 Forbidden: 2-Legged M2M tokens lack authority to mutate quotes. "
                "Requires 3-Legged user-delegated token with 'quotes:write' scope."
            ),
        )

    quote = ERP_STATE["quotes"].get(quote_id)
    if not quote:
        raise HTTPException(status_code=404, detail=f"Quote #{quote_id} not found")

    # CRITICAL SECURITY CHECK 2: 3-Legged Tenant Isolation
    if quote["owner"] != actor_sub:
        record_audit_event(
            action="PATCH_QUOTE_TENANT_VIOLATION",
            actor=actor_sub,
            auth_type=auth_type,
            quote_id=quote_id,
            outcome="403_FORBIDDEN",
            details={"quote_owner": quote["owner"], "attempted_by": actor_sub},
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"403 Forbidden: Tenant RBAC isolation check failed. "
                f"User '{actor_sub}' is not authorized to modify Quote #{quote_id} (Owner: '{quote['owner']}')."
            ),
        )

    old_price = quote["quoted_price"]
    old_cost = quote["unit_cost"]
    old_margin = quote["margin_percent"]

    if patch_body.unit_cost is not None:
        quote["unit_cost"] = round(patch_body.unit_cost, 2)
    if patch_body.quoted_price is not None:
        quote["quoted_price"] = round(patch_body.quoted_price, 2)
    if patch_body.margin_percent is not None:
        quote["margin_percent"] = round(patch_body.margin_percent, 2)
    elif quote["quoted_price"] > 0:
        quote["margin_percent"] = round(
            ((quote["quoted_price"] - quote["unit_cost"]) / quote["quoted_price"]) * 100.0,
            2,
        )
    if patch_body.promo_applied is not None:
        quote["promo_applied"] = patch_body.promo_applied

    quote["total_cost"] = round(quote["unit_cost"] * quote["qty"], 2)
    quote["total_price"] = round(quote["quoted_price"] * quote["qty"], 2)
    quote["gross_profit"] = round(quote["total_price"] - quote["total_cost"], 2)
    quote["updated_by"] = actor_sub
    quote["updated_at"] = datetime.now(timezone.utc).isoformat()
    quote["status"] = "REPRICED_PROMO_APPLIED"

    audit_entry = record_audit_event(
        action="QUOTE_REPRICED",
        actor=actor_sub,
        auth_type=auth_type,
        quote_id=quote_id,
        outcome="200_UPDATED",
        details={
            "previous_unit_cost": old_cost,
            "new_unit_cost": quote["unit_cost"],
            "previous_quoted_price": old_price,
            "new_quoted_price": quote["quoted_price"],
            "previous_margin_percent": old_margin,
            "new_margin_percent": quote["margin_percent"],
            "promo_applied": quote["promo_applied"],
            "updated_by": actor_sub,
        },
    )

    return {
        "status": "updated",
        "quote": quote,
        "audit_record": audit_entry,
    }


# ---------------------------------------------------------------------------
# 3. Internal M2M Read-Only SKU Matcher & Notification Stream (Component C)
# ---------------------------------------------------------------------------


@app.get("/api/v1/internal/eligible-quotes")
async def find_eligible_quotes_for_sku(
    sku: str = Query(...),
    owner: str = Query(default="brian@brianreevz.altostrat.com"),
    authorization: str | None = Header(default=None),
):
    """Read-only M2M endpoint allowing the Sentinel Agent to identify open quotes matching a promoted SKU."""
    claims = decode_bearer_token(authorization)
    scopes = str(claims.get("scope", "")).split()
    if "quotes:match_sku" not in scopes and "quotes:read" not in scopes:
        raise HTTPException(status_code=403, detail="Insufficient scope for SKU quote matching")

    matches = [
        {
            "quote_id": q["quote_id"],
            "owner": q["owner"],
            "client": q["client"],
            "sku": q["sku"],
            "qty": q["qty"],
            "unit_cost": q["unit_cost"],
            "quoted_price": q["quoted_price"],
        }
        for q in ERP_STATE["quotes"].values()
        if q["sku"] == sku and (not owner or q["owner"] == owner)
    ]
    return {"sku": sku, "matching_quotes": matches}


class NotificationPayload(BaseModel):
    recipient: str
    message: str
    action_url: str
    promo_code: str = "PROMO-MERAKI-20"
    sku: str = "SKU-NET-MERAKI-MR36"
    quote_id: int = 1042
    google_chat_webhook: dict[str, Any] | None = None


@app.post("/api/v1/notifications/dispatch")
async def dispatch_notification(
    payload: NotificationPayload,
    authorization: str | None = Header(default=None),
):
    """Receive notification from the 2-Legged Sentinel Agent and enqueue for SSE & Google Chat."""
    if authorization:
        decode_bearer_token(authorization)

    # Avoid duplicate spamming for the same promo + quote_id if already queued
    for existing in ERP_STATE["notifications"]:
        if existing.get("quote_id") == payload.quote_id and existing.get("promo_code") == payload.promo_code:
            return {"status": "already_dispatched", "notification": existing}

    item = {
        "id": f"notif-{secrets.token_hex(4)}",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "recipient": payload.recipient,
        "message": payload.message,
        "action_url": payload.action_url,
        "promo_code": payload.promo_code,
        "sku": payload.sku,
        "quote_id": payload.quote_id,
        "google_chat_webhook": payload.google_chat_webhook
        or {
            "cardsV2": [
                {
                    "cardId": "sentinel-promo-alert",
                    "card": {
                        "header": {
                            "title": "Sentinel Agent: Vendor Promo Detected",
                            "subtitle": f"SKU: {payload.sku} (20% Off Wholesale)",
                        },
                        "sections": [
                            {
                                "widgets": [
                                    {"textParagraph": {"text": payload.message}},
                                    {
                                        "buttonList": {
                                            "buttons": [
                                                {
                                                    "text": "Open in Gemini Enterprise Copilot",
                                                    "onClick": {"openLink": {"url": payload.action_url}},
                                                }
                                            ]
                                        }
                                    },
                                ]
                            }
                        ],
                    },
                }
            ]
        },
    }
    ERP_STATE["notifications"].append(item)
    logger.info("Notification dispatched for recipient=%s quote_id=%s", payload.recipient, payload.quote_id)
    return {"status": "dispatched", "notification": item}


@app.get("/api/v1/notifications")
async def get_notifications(recipient: str = Query(default="brian@brianreevz.altostrat.com")):
    """Return active notifications for the specified recipient."""
    items = [n for n in ERP_STATE["notifications"] if n["recipient"] == recipient]
    return {"recipient": recipient, "notifications": items}


@app.get("/api/v1/notifications/stream")
async def stream_notifications(
    request: Request,
    recipient: str = Query(default="brian@brianreevz.altostrat.com"),
):
    """Server-Sent Events (SSE) stream for real-time In-App Notification Toasts."""

    async def event_generator():
        sent_ids: set[str] = set()
        while True:
            if await request.is_disconnected():
                break
            for notif in ERP_STATE["notifications"]:
                if notif["recipient"] == recipient and notif["id"] not in sent_ids:
                    sent_ids.add(notif["id"])
                    yield f"data: {json.dumps(notif)}\n\n"
            await asyncio.sleep(1.0)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.get("/api/v1/audit-logs")
async def get_audit_logs():
    """Return immutable security & quote mutation audit trail."""
    return {"audit_logs": ERP_STATE["audit_logs"]}


@app.post("/api/v1/demo/reset")
async def reset_erp():
    """Reset quotes, notifications, and audit logs for repeat runs."""
    reset_erp_state()
    return {"status": "reset"}


@app.get("/health")
async def health():
    return {"status": "ok", "service": "internal-erp-service", "domain": INTERNAL_DOMAIN}


if __name__ == "__main__":
    host = os.getenv("BIND_HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8002"))
    uvicorn.run(app, host=host, port=port)
