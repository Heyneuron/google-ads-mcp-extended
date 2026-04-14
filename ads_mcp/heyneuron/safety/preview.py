# Copyright 2026 HeyNeuron (BIG GROWTH Sp. z o.o.)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0

"""Dry-run helpers: let the model preview a mutation before applying it."""

from __future__ import annotations

import os
from typing import Any, Dict


def is_dry_run(explicit: bool | None) -> bool:
    """Resolves whether a mutation should run in dry-run mode.

    The explicit tool argument wins; otherwise falls back to the
    GOOGLE_ADS_MCP_DEFAULT_DRY_RUN env var (defaults to False).
    """
    if explicit is not None:
        return bool(explicit)
    return os.environ.get("GOOGLE_ADS_MCP_DEFAULT_DRY_RUN", "").lower() in {
        "1",
        "true",
        "yes",
    }


def dry_run_response(tool: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Returns a structured preview of the mutation without executing it."""
    return {
        "dry_run": True,
        "tool": tool,
        "would_apply": payload,
        "note": (
            "Re-run with dry_run=False to execute. Every mutation defaults "
            "to PAUSED status where applicable."
        ),
    }
