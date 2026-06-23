"""Pure-Python prescan node (no LLM)."""

from __future__ import annotations

import logging
import time
from typing import Any, Dict

from ..prescan import run_prescan
from ..state import FLAgentState
from ..utils import stage_trace, utc_now_iso

logger = logging.getLogger(__name__)


async def prescan_node(state: FLAgentState) -> Dict[str, Any]:
    started = time.monotonic()
    cdets_id = state["cdets_id"]
    result = run_prescan(cdets_id)

    duration = time.monotonic() - started
    if not result["cdets_lookup_ok"]:
        return {
            **result,
            "error": result.get("error", "CDETS lookup failed"),
            "stage_traces": {
                "prescan": stage_trace(
                    status="failed",
                    duration=duration,
                    error=result.get("error"),
                )
                | {"start_time": utc_now_iso()},
            },
        }

    logger.info(
        "prescan: cdets=%s component=%s ap=%s version=%s",
        cdets_id, result["component"], result["primary_ap"], result["version"],
    )

    return {
        **result,
        "stage_traces": {
            "prescan": stage_trace(
                status="ok",
                duration=duration,
            )
            | {
                "start_time": utc_now_iso(),
                "component": result["component"],
                "primary_ap": result["primary_ap"],
            },
        },
    }
