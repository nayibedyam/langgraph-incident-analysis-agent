"""Terminal abort node — invoked when an upstream gate fails.

Records the abort reason in ``state['stage_traces']`` and surfaces a clean
error message instead of letting the failure cascade.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from ..state import FLAgentState
from ..utils import stage_trace, utc_now_iso

logger = logging.getLogger(__name__)


async def abort_node(state: FLAgentState) -> Dict[str, Any]:
    """Emit a short failure summary and stop. Never raises."""
    error = state.get("error") or "Pipeline aborted"
    cdets_id = state.get("cdets_id", "<unknown>")
    logger.error("Pipeline aborted for %s: %s", cdets_id, error)

    return {
        "delivery_status": "aborted",
        "stage_traces": {
            "abort": stage_trace(status="aborted", error=error)
            | {"end_time": utc_now_iso()},
        },
    }
