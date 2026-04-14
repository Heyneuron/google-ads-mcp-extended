# Copyright 2026 HeyNeuron (BIG GROWTH Sp. z o.o.)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Append-only JSON Lines audit log for every mutation issued by the MCP server.

Every write operation MUST call `record()` before returning to the caller.
The default log file lives under `$GOOGLE_ADS_MCP_AUDIT_LOG` or
`~/.google-ads-mcp/audit.log.jsonl` if the env var is unset.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

_DEFAULT_PATH = Path.home() / ".google-ads-mcp" / "audit.log.jsonl"


def _log_path() -> Path:
    override = os.environ.get("GOOGLE_ADS_MCP_AUDIT_LOG")
    path = Path(override).expanduser() if override else _DEFAULT_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def record(
    tool: str,
    customer_id: str,
    payload: Dict[str, Any],
    result: Dict[str, Any] | None = None,
    dry_run: bool = False,
) -> None:
    """Appends a single audit entry to the mutation log.

    Args:
        tool: Name of the MCP tool that was invoked (e.g. ``create_campaign``).
        customer_id: Target Google Ads customer id (10 digits, no dashes).
        payload: The arguments the tool was called with.
        result: Optional structured result returned by the Google Ads API.
        dry_run: True when the tool ran in preview mode and did not mutate.
    """
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "tool": tool,
        "customer_id": customer_id,
        "dry_run": dry_run,
        "payload": payload,
        "result": result,
    }
    with _log_path().open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(entry, default=str) + "\n")
