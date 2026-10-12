#!/usr/bin/env python3
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0

"""Security Posture & End-to-End Hybrid OAuth Verification Suite (verify_security_posture.py).

Verifies:
1. 2-Legged Least Privilege:
   - Verifies the Sentinel Agent cannot call `PATCH /api/v1/quotes/{id}` using its M2M token
     (both the vendor M2M token and internal Sentinel M2M token must receive `403 Forbidden`).
2. 3-Legged Tenant Isolation:
   - Verifies that Brian's 3-legged token (`brian@brianreevz.altostrat.com`) cannot read or mutate
     Quote #9999 (`dave@brianreevz.altostrat.com`'s quote for Cyberdyne) — must receive `403 Forbidden`.
3. Audit Trail Verification & 2-Legged to 3-Legged Handoff:
   - Triggers the 20% promo on `SKU-NET-MERAKI-MR36` ($450 -> $360) on `bigredreeves.com`.
   - Verifies the 2-Legged Sentinel Agent detects the promo and dispatches the notification for Quote #1042.
   - Executes MCP `reprice_quote(1042, "PROMO-MERAKI-20", 18.0, brian_3l_token)` to reprice Quote #1042
     to $439.02 (18% margin).
   - Verifies the immutable audit log explicitly records `updated_by: brian@brianreevz.altostrat.com`
     rather than a generic machine ID.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import httpx

# Allow importing SentinelAgent directly for in-process verification
BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR / "sentinel-agent"))

from worker import SentinelAgent  # noqa: E402

VENDOR_URL = os.getenv("VENDOR_SERVICE_URL", "http://127.0.0.1:8001").rstrip("/")
ERP_URL = os.getenv("ERP_SERVICE_URL", "http://127.0.0.1:8002").rstrip("/")
MCP_URL = os.getenv("MCP_SERVICE_URL", "http://127.0.0.1:8003").rstrip("/")


def _load_secret(key: str, default: str) -> str:
    env_file = BASE_DIR / ".env.example"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith(f"{key}="):
                return line.split("=", 1)[1].strip()
    return os.getenv(key, default)


async def get_3legged_token_for_user(client: httpx.AsyncClient, user_email: str) -> str:
    """Complete the OAuth 2.0 3-Legged authorization_code flow for the specified user."""
    auth_resp = await client.get(
        f"{ERP_URL}/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": "gemini-enterprise-copilot-client",
            "login_hint": user_email,
            "scope": "quotes:read quotes:write",
            "state": "verify-posture",
        },
        headers={"Accept": "application/json"},
    )
    assert auth_resp.status_code == 200, f"Failed /oauth/authorize for {user_email}: {auth_resp.text}"
    auth_code = auth_resp.json()["code"]

    token_resp = await client.post(
        f"{ERP_URL}/oauth/token",
        json={"grant_type": "authorization_code", "code": auth_code},
    )
    assert token_resp.status_code == 200, f"Failed /oauth/token exchange: {token_resp.text}"
    token_data = token_resp.json()
    assert token_data["sub"] == user_email
    assert "quotes:read" in token_data["scope"] and "quotes:write" in token_data["scope"]
    return token_data["access_token"]


async def run_security_verification() -> None:
    print("=" * 82)
    print(" HYBRID OAUTH (2-LEGGED SENTINEL & 3-LEGGED COPILOT) SECURITY VERIFICATION SUITE")
    print(" GCP Project: gemini-enterprise-demo-508317 | Domain: brianreevz.altostrat.com")
    print("=" * 82)

    async with httpx.AsyncClient(timeout=10.0) as client:
        # Reset services to clean baseline
        await client.post(f"{VENDOR_URL}/api/v1/demo/reset")
        await client.post(f"{ERP_URL}/api/v1/demo/reset")

        # -----------------------------------------------------------------------
        # TEST 1: 2-Legged Least Privilege
        # -----------------------------------------------------------------------
        print("\n[TEST 1] Verifying 2-Legged Least Privilege (M2M Sentinel Cannot Mutate Quotes)...")
        sentinel = SentinelAgent(vendor_url=VENDOR_URL, erp_url=ERP_URL)
        vendor_m2m_token = await sentinel.fetch_vendor_2legged_token(client)
        internal_m2m_token = await sentinel.fetch_internal_2legged_token(client)

        # Attempt PATCH /api/v1/quotes/1042 with Vendor 2-Legged M2M Token
        res_vendor_m2m = await client.patch(
            f"{ERP_URL}/api/v1/quotes/1042",
            headers={"Authorization": f"Bearer {vendor_m2m_token}"},
            json={"quoted_price": 399.00},
        )
        assert res_vendor_m2m.status_code == 403, (
            f"Expected 403 Forbidden for Vendor 2L M2M token, got {res_vendor_m2m.status_code}"
        )
        print(f"  ✓ Vendor 2-Legged M2M Token rejected on PATCH /api/v1/quotes/1042 -> HTTP {res_vendor_m2m.status_code}")

        # Attempt PATCH /api/v1/quotes/1042 with Internal Sentinel 2-Legged M2M Token
        res_internal_m2m = await client.patch(
            f"{ERP_URL}/api/v1/quotes/1042",
            headers={"Authorization": f"Bearer {internal_m2m_token}"},
            json={"quoted_price": 399.00},
        )
        assert res_internal_m2m.status_code == 403, (
            f"Expected 403 Forbidden for Internal 2L Sentinel token, got {res_internal_m2m.status_code}"
        )
        print(
            f"  ✓ Sentinel Internal 2-Legged M2M Token rejected on PATCH /api/v1/quotes/1042 -> HTTP {res_internal_m2m.status_code}"
        )
        print("  [PASS] 2-Legged Least Privilege verified.")

        # -----------------------------------------------------------------------
        # TEST 2: 3-Legged Tenant Isolation (Brian vs. Dave's Quote #9999)
        # -----------------------------------------------------------------------
        print("\n[TEST 2] Verifying 3-Legged Tenant Isolation (RBAC across brian@ vs. dave@)...")
        brian_3l_token = await get_3legged_token_for_user(client, "brian@brianreevz.altostrat.com")

        # Verify GET /api/v1/quotes only returns Brian's Quote #1042 and NOT Dave's Quote #9999
        list_resp = await client.get(
            f"{ERP_URL}/api/v1/quotes",
            headers={"Authorization": f"Bearer {brian_3l_token}"},
        )
        assert list_resp.status_code == 200
        returned_ids = [q["quote_id"] for q in list_resp.json()["quotes"]]
        assert returned_ids == [1042], f"Expected only [1042] for Brian, got {returned_ids}"
        print(f"  ✓ GET /api/v1/quotes with Brian's 3-Legged Token returned strictly Quote IDs: {returned_ids}")

        # Attempt GET /api/v1/quotes/9999 (Dave's quote for Cyberdyne) using Brian's token
        read_9999 = await client.get(
            f"{ERP_URL}/api/v1/quotes/9999",
            headers={"Authorization": f"Bearer {brian_3l_token}"},
        )
        assert read_9999.status_code == 403, f"Expected 403 reading Quote #9999, got {read_9999.status_code}"
        print(f"  ✓ Brian reading Dave's Quote #9999 blocked -> HTTP {read_9999.status_code} Forbidden")

        # Attempt PATCH /api/v1/quotes/9999 using Brian's 3-legged token
        patch_9999 = await client.patch(
            f"{ERP_URL}/api/v1/quotes/9999",
            headers={"Authorization": f"Bearer {brian_3l_token}"},
            json={"quoted_price": 420.00},
        )
        assert patch_9999.status_code == 403, f"Expected 403 mutating Quote #9999, got {patch_9999.status_code}"
        print(f"  ✓ Brian mutating Dave's Quote #9999 blocked -> HTTP {patch_9999.status_code} Forbidden")
        print("  [PASS] 3-Legged Tenant Isolation verified.")

        # -----------------------------------------------------------------------
        # TEST 3: Live 2-Legged Promo Detection -> 3-Legged Reprice & Audit Trail
        # -----------------------------------------------------------------------
        print("\n[TEST 3] Verifying End-to-End Promo Trigger, Sentinel Handoff & Audit Trail...")
        trigger_resp = await client.post(f"{VENDOR_URL}/api/v1/demo/trigger-promo")
        assert trigger_resp.status_code == 200
        print("  ✓ Triggered 20% Vendor Promo on bigredreeves.com (SKU-NET-MERAKI-MR36: $450.00 -> $360.00)")

        dispatched = await sentinel.poll_once(client)
        assert len(dispatched) == 1
        notif = dispatched[0]
        assert notif["recipient"] == "brian@brianreevz.altostrat.com"
        assert notif["action_url"] == "http://brianreevz.altostrat.com/copilot?quoteId=1042"
        print(f"  ✓ 2-Legged Sentinel detected promo & dispatched notification: \"{notif['message']}\"")

        # Invoke MCP Server reprice_quote tool with Brian's 3-Legged token for 18% margin
        mcp_reprice_resp = await client.post(
            f"{MCP_URL}/api/v1/tools/reprice_quote",
            json={
                "quote_id": 1042,
                "promo_code": "PROMO-MERAKI-20",
                "target_margin_percent": 18.0,
                "user_token": brian_3l_token,
            },
        )
        assert mcp_reprice_resp.status_code == 200, f"MCP reprice_quote failed: {mcp_reprice_resp.text}"
        reprice_data = mcp_reprice_resp.json()
        updated_quote = reprice_data["quote"]
        audit_record = reprice_data["audit_record"]

        assert updated_quote["unit_cost"] == 360.00
        assert updated_quote["quoted_price"] == 439.02
        assert updated_quote["margin_percent"] == 18.0
        assert updated_quote["updated_by"] == "brian@brianreevz.altostrat.com"
        assert audit_record["updated_by"] == "brian@brianreevz.altostrat.com"
        assert audit_record["auth_type"] == "3_legged_user_delegated"

        print(
            f"  ✓ Quote #1042 repriced via MCP: Unit Cost=${updated_quote['unit_cost']:.2f}, "
            f"New Price=${updated_quote['quoted_price']:.2f} (Margin={updated_quote['margin_percent']:.1f}%)"
        )
        print(
            f"  ✓ Audit Trail verified: updated_by='{audit_record['updated_by']}' "
            f"(auth_type='{audit_record['auth_type']}', event_id='{audit_record['event_id']}')"
        )
        print("  [PASS] Audit Trail & 3-Legged User Attribution verified.")

    print("\n" + "=" * 82)
    print(" ALL 3 SECURITY POSTURE & HYBRID OAUTH HANDOFF SPECIFICATIONS PASSED (100%)")
    print("=" * 82)


if __name__ == "__main__":
    asyncio.run(run_security_verification())
