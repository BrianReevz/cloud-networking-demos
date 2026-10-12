# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0

"""Component C: The 2-Legged Sentinel Background Agent.

Headless Python worker that:
1. Uses OAuth 2.0 client_credentials against bigredreeves.com/oauth/token to obtain a 2-legged M2M token.
2. Polls /api/v1/promotions/active every 5 seconds.
3. Queries internal open quotes matching promoted SKUs using a read-only M2M token.
4. Dispatches an In-App Notification Queue payload and Google Chat Webhook JSON payload for Quote #1042.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
from pathlib import Path
from typing import Any

import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] [sentinel-2legged-agent] %(message)s")
logger = logging.getLogger("sentinel-2legged-agent")


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


VENDOR_SERVICE_URL = os.getenv("VENDOR_SERVICE_URL") or _ENV_DEFAULTS.get("VENDOR_SERVICE_URL", "http://127.0.0.1:8001")
ERP_SERVICE_URL = os.getenv("ERP_SERVICE_URL") or _ENV_DEFAULTS.get("ERP_SERVICE_URL", "http://127.0.0.1:8002")
XVANTAGE_CLIENT_ID = os.getenv("XVANTAGE_CLIENT_ID") or _ENV_DEFAULTS.get("XVANTAGE_CLIENT_ID", "xvantage-sentinel-app")
XVANTAGE_CLIENT_SECRET = get_secret("XVANTAGE_CLIENT_SECRET")
SENTINEL_INTERNAL_CLIENT_ID = os.getenv("SENTINEL_INTERNAL_CLIENT_ID") or _ENV_DEFAULTS.get(
    "SENTINEL_INTERNAL_CLIENT_ID", "sentinel-internal-reader"
)
SENTINEL_INTERNAL_CLIENT_SECRET = get_secret("SENTINEL_INTERNAL_CLIENT_SECRET")
POLL_INTERVAL_SECONDS = float(os.getenv("POLL_INTERVAL_SECONDS") or _ENV_DEFAULTS.get("POLL_INTERVAL_SECONDS", "5"))
GOOGLE_CHAT_WEBHOOK_URL = os.getenv("GOOGLE_CHAT_WEBHOOK_URL", "")


class SentinelAgent:
    """Autonomous 2-Legged M2M Sentinel Agent."""

    def __init__(
        self,
        vendor_url: str = VENDOR_SERVICE_URL,
        erp_url: str = ERP_SERVICE_URL,
    ) -> None:
        self.vendor_url = vendor_url.rstrip("/")
        self.erp_url = erp_url.rstrip("/")
        self.vendor_token: str | None = None
        self.internal_token: str | None = None

    async def fetch_vendor_2legged_token(self, client: httpx.AsyncClient) -> str:
        """Obtain 2-Legged M2M bearer token from bigredreeves.com/oauth/token."""
        resp = await client.post(
            f"{self.vendor_url}/oauth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": XVANTAGE_CLIENT_ID,
                "client_secret": XVANTAGE_CLIENT_SECRET,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        self.vendor_token = data["access_token"]
        logger.info(
            "Fetched 2-Legged M2M token from bigredreeves.com (scope='%s')",
            data.get("scope"),
        )
        return self.vendor_token

    async def fetch_internal_2legged_token(self, client: httpx.AsyncClient) -> str:
        """Obtain internal 2-Legged M2M read-only token from brianreevz.altostrat.com/oauth/token."""
        resp = await client.post(
            f"{self.erp_url}/oauth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": SENTINEL_INTERNAL_CLIENT_ID,
                "client_secret": SENTINEL_INTERNAL_CLIENT_SECRET,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        self.internal_token = data["access_token"]
        return self.internal_token

    @staticmethod
    def build_google_chat_webhook_payload(
        recipient: str,
        message: str,
        action_url: str,
        sku: str,
        quote_id: int,
        client_name: str,
    ) -> dict[str, Any]:
        """Build a Google Chat Webhook JSON payload with interactive handoff link."""
        return {
            "text": f"🔔 *Sentinel Agent Alert for {recipient}*: {message}",
            "cardsV2": [
                {
                    "cardId": f"promo-{sku}-{quote_id}",
                    "card": {
                        "header": {
                            "title": "2-Legged Sentinel: Vendor Promo Detected",
                            "subtitle": f"SKU: {sku} | Client: {client_name} (Quote #{quote_id})",
                        },
                        "sections": [
                            {
                                "widgets": [
                                    {"textParagraph": {"text": message}},
                                    {
                                        "buttonList": {
                                            "buttons": [
                                                {
                                                    "text": "Review & Reprice in Gemini Copilot (3-Legged Auth)",
                                                    "onClick": {"openLink": {"url": action_url}},
                                                }
                                            ]
                                        }
                                    },
                                ]
                            }
                        ],
                    },
                }
            ],
        }

    async def poll_once(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        """Execute a single poll cycle against bigredreeves.com and dispatch notifications if promos match."""
        if not self.vendor_token:
            await self.fetch_vendor_2legged_token(client)
        if not self.internal_token:
            await self.fetch_internal_2legged_token(client)

        promo_resp = await client.get(
            f"{self.vendor_url}/api/v1/promotions/active",
            headers={"Authorization": f"Bearer {self.vendor_token}"},
        )
        if promo_resp.status_code == 401:
            await self.fetch_vendor_2legged_token(client)
            promo_resp = await client.get(
                f"{self.vendor_url}/api/v1/promotions/active",
                headers={"Authorization": f"Bearer {self.vendor_token}"},
            )
        promo_resp.raise_for_status()
        promotions = promo_resp.json().get("promotions", [])

        dispatched_notifications: list[dict[str, Any]] = []
        for promo in promotions:
            if not promo.get("active"):
                continue
            sku = promo["sku"]
            discount_pct = int(promo.get("discount_percent", 20))
            promo_code = promo.get("promo_code", "PROMO-MERAKI-20")

            # Query internal Quote database for matching open quotes owned by brian@brianreevz.altostrat.com
            match_resp = await client.get(
                f"{self.erp_url}/api/v1/internal/eligible-quotes",
                params={"sku": sku, "owner": "brian@brianreevz.altostrat.com"},
                headers={"Authorization": f"Bearer {self.internal_token}"},
            )
            match_resp.raise_for_status()
            matching_quotes = match_resp.json().get("matching_quotes", [])

            for q in matching_quotes:
                quote_id = q["quote_id"]
                recipient = q["owner"]
                client_name = q["client"]
                message = (
                    f"Promo detected on {sku} ({discount_pct}% off). "
                    f"Your Quote #{quote_id} for {client_name} is eligible for repricing."
                )
                action_url = f"http://brianreevz.altostrat.com/copilot?quoteId={quote_id}"

                chat_payload = self.build_google_chat_webhook_payload(
                    recipient=recipient,
                    message=message,
                    action_url=action_url,
                    sku=sku,
                    quote_id=quote_id,
                    client_name=client_name,
                )

                notification_payload = {
                    "recipient": recipient,
                    "message": message,
                    "action_url": action_url,
                    "promo_code": promo_code,
                    "sku": sku,
                    "quote_id": quote_id,
                    "google_chat_webhook": chat_payload,
                }

                dispatch_resp = await client.post(
                    f"{self.erp_url}/api/v1/notifications/dispatch",
                    json=notification_payload,
                    headers={"Authorization": f"Bearer {self.internal_token}"},
                )
                dispatch_resp.raise_for_status()

                if GOOGLE_CHAT_WEBHOOK_URL:
                    try:
                        await client.post(GOOGLE_CHAT_WEBHOOK_URL, json=chat_payload, timeout=3.0)
                    except httpx.HTTPError as exc:
                        logger.warning("Optional Google Chat webhook delivery skipped: %s", exc)

                dispatched_notifications.append(notification_payload)

        return dispatched_notifications

    async def run_forever(self) -> None:
        """Continuously poll bigredreeves.com every POLL_INTERVAL_SECONDS."""
        logger.info(
            "Starting 2-Legged Sentinel Background Worker (polling %s every %.1fs)",
            self.vendor_url,
            POLL_INTERVAL_SECONDS,
        )
        async with httpx.AsyncClient(timeout=10.0) as client:
            while True:
                try:
                    await self.poll_once(client)
                except Exception as exc:
                    logger.warning("Sentinel poll cycle encountered transient error: %s", exc)
                await asyncio.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    agent = SentinelAgent()
    asyncio.run(agent.run_forever())
