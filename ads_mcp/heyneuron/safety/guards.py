# Copyright 2026 HeyNeuron (BIG GROWTH Sp. z o.o.)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0

"""Hard guards that refuse dangerous mutations.

These are intentionally conservative; the goal is to stop an agent-driven
mistake from spending client money before a human notices.
"""

from __future__ import annotations

import os
from typing import Iterable

from fastmcp.exceptions import ToolError

# Hard caps. Any mutation that would exceed these is refused unless the user
# sets GOOGLE_ADS_MCP_GUARDS_DISABLED=1, which should only be used knowingly.
_MAX_CAMPAIGN_BUDGET_MICROS = int(
    os.environ.get("GOOGLE_ADS_MCP_MAX_CAMPAIGN_BUDGET_MICROS", 500_000_000)
)  # 500 units in account currency (e.g. 500 PLN/USD/EUR)

_MAX_BID_MICROS = int(
    os.environ.get("GOOGLE_ADS_MCP_MAX_BID_MICROS", 50_000_000)
)  # 50 units in account currency


def _guards_enabled() -> bool:
    return os.environ.get("GOOGLE_ADS_MCP_GUARDS_DISABLED") != "1"


def ensure_budget_under_cap(amount_micros: int) -> None:
    """Raises ToolError if amount_micros exceeds the configured hard cap."""
    if not _guards_enabled():
        return
    if amount_micros > _MAX_CAMPAIGN_BUDGET_MICROS:
        raise ToolError(
            f"Refusing to set campaign budget to {amount_micros} micros: "
            f"exceeds configured hard cap of {_MAX_CAMPAIGN_BUDGET_MICROS} "
            f"micros. Set GOOGLE_ADS_MCP_MAX_CAMPAIGN_BUDGET_MICROS to raise "
            f"the cap or GOOGLE_ADS_MCP_GUARDS_DISABLED=1 to disable guards."
        )


def ensure_bid_under_cap(amount_micros: int) -> None:
    """Raises ToolError if a keyword/ad-group bid exceeds the hard cap."""
    if not _guards_enabled():
        return
    if amount_micros > _MAX_BID_MICROS:
        raise ToolError(
            f"Refusing to set bid to {amount_micros} micros: exceeds "
            f"configured hard cap of {_MAX_BID_MICROS} micros. Set "
            f"GOOGLE_ADS_MCP_MAX_BID_MICROS to raise the cap."
        )


def ensure_customer_id(customer_id: str) -> str:
    """Validates a customer id is 10 digits and returns it normalised."""
    cleaned = customer_id.replace("-", "").strip()
    if not cleaned.isdigit() or len(cleaned) != 10:
        raise ToolError(
            f"Invalid customer_id '{customer_id}'. Expected 10 digits, "
            f"optionally separated by dashes (e.g. 123-456-7890)."
        )
    return cleaned


def ensure_non_empty(name: str, values: Iterable) -> None:
    values_list = list(values)
    if not values_list:
        raise ToolError(f"'{name}' must not be empty.")
