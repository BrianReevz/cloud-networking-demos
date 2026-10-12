# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0

"""Utility & watch-loop tool functions for the 2-Legged Promo Sentinel Agent."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


def get_current_time(timezone_name: str = "UTC") -> dict:
    """Return the current time in the specified timezone."""
    try:
        tz = ZoneInfo(timezone_name)
        now = datetime.now(tz)
        return {"timezone": timezone_name, "datetime": now.isoformat(), "utc_offset": now.strftime("%z")}
    except KeyError:
        now = datetime.now(timezone.utc)
        return {"timezone": "UTC", "datetime": now.isoformat(), "utc_offset": "+0000"}


def list_mcp_connections() -> dict:
    """List MCP servers discovered from the Agent Registry at startup."""
    from . import agent as _agent

    servers = list(_agent.DISCOVERED_MCP_SERVERS)
    return {"connections": servers, "count": len(servers)}


def sleep_between_poll_cycles(seconds: int = 12) -> dict:
    """Pause between 2-Legged Sentinel polling cycles (up to 20s per cycle), waking up immediately as soon as a vendor promo is triggered."""
    import httpx

    capped = max(5, min(int(seconds), 20))
    slept = 0
    promo_detected_early = False
    catalog_url = "https://xvantage-catalog-mcp-fntvjdmgca-uc.a.run.app/api/v1/demo/state"

    for _ in range(capped):
        time.sleep(1)
        slept += 1
        try:
            resp = httpx.get(catalog_url, timeout=1.5)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("active_promotions"):
                    promo_detected_early = True
                    break
        except Exception:
            pass

    return {
        "slept_seconds": slept,
        "promo_detected_early": promo_detected_early,
        "resumed_at": datetime.now(timezone.utc).isoformat(),
    }
