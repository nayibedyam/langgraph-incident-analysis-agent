"""Stage 02c — human_review.

Pauses the LangGraph run via :func:`langgraph.types.interrupt`. The
runner detects the interrupt, persists checkpoint state to disk, and
flips the job status to ``awaiting_human``. When the reviewer submits
their answers, the runner resumes the graph with a
``Command(resume={...})`` and execution continues from this node.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from langgraph.types import interrupt

from ..state import FLAgentState

logger = logging.getLogger(__name__)


async def human_review_node(state: FLAgentState) -> Dict[str, Any]:
    request = state.get("missing_info_request") or {}
    logger.info(
        "human_review: cdets=%s pausing for reviewer (%d fields)",
        state.get("cdets_id"),
        len(request.get("missing_fields") or []),
    )

    # interrupt() raises GraphInterrupt on first execution and returns the
    # value supplied by Command(resume=...) on the second execution.
    value: Dict[str, Any] = interrupt(
        {
            "cdets_id": state.get("cdets_id"),
            "job_id": state.get("job_id"),
            "missing_info_request": request,
        }
    )

    return {
        "human_input": value or {},
        "human_review_count": int(state.get("human_review_count", 0) or 0) + 1,
    }
