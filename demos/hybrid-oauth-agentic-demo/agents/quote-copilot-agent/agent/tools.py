# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0

"""Utility & margin calculation tools for the 3-Legged Deal Desk Quote Copilot Agent."""

from __future__ import annotations

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


def calculate_target_margin_impact(
    baseline_cost: float = 450.0,
    current_quoted_price: float = 540.0,
    promo_wholesale_cost: float = 360.0,
    target_margin_percent: float = 18.0,
    qty: int = 20,
) -> dict:
    """Calculate the exact before/after unit price, total price, and customer savings for a target gross margin."""
    before_margin = round(((current_quoted_price - baseline_cost) / current_quoted_price) * 100.0, 2)
    before_total = round(current_quoted_price * qty, 2)
    margin_fraction = float(target_margin_percent) / 100.0
    new_unit_price = round(float(promo_wholesale_cost) / (1.0 - margin_fraction), 2)
    after_total = round(new_unit_price * qty, 2)
    customer_savings = round(before_total - after_total, 2)

    return {
        "qty": qty,
        "before": {
            "unit_cost": baseline_cost,
            "quoted_price": current_quoted_price,
            "margin_percent": before_margin,
            "total_quote": before_total,
        },
        "after": {
            "unit_cost": promo_wholesale_cost,
            "quoted_price": new_unit_price,
            "margin_percent": target_margin_percent,
            "total_quote": after_total,
            "customer_savings": customer_savings,
        },
    }
