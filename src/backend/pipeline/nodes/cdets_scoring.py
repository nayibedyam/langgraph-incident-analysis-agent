"""Stage 02 — cdets_scoring (LLM agent)."""

from __future__ import annotations

import logging
import time
from typing import Any, Dict

from ..agent_loop import run_agent_loop
from ..llm import get_llm
from ..state import FLAgentState
from ..tools.filesystem import read_file_text, write_artifact_text
from ..utils import stage_trace, utc_now_iso
from ._llm_helpers import extract_json, format_user_message, load_prompt

logger = logging.getLogger(__name__)

_USER_TEMPLATE = """\
Produce the AI-FL Quality Scorecard for **{cdets_id}**.

Inputs:
- CDETS schema  : {cdets_schema_path}
- Union schema  : {union_schema_path}
- Artifact dir  : {artifact_dir}
- AP / SubAP    : {primary_ap} / {primary_subap}
- Severity      : {severity}

Read the schema with `read_file_text`, score each dimension per the rubric
in the system prompt, write the markdown to
``{cdets_id}/{cdets_id}-Scorecard.md`` using `write_artifact_text`, and
return the final JSON.
"""


async def cdets_scoring_node(state: FLAgentState) -> Dict[str, Any]:
    started = time.monotonic()
    config = state["config"]
    llm = get_llm(config, stage="scoring")

    user_message = format_user_message(
        _USER_TEMPLATE,
        cdets_id=state["cdets_id"],
        cdets_schema_path=state.get("cdets_schema_path", ""),
        union_schema_path=state.get("union_schema_path") or "",
        artifact_dir=state.get("artifact_dir", ""),
        primary_ap=state.get("primary_ap", ""),
        primary_subap=state.get("primary_subap", ""),
        severity=state.get("severity", ""),
    )

    result = await run_agent_loop(
        llm,
        system_prompt=load_prompt("scoring"),
        user_message=user_message,
        tools=[read_file_text, write_artifact_text],
        max_iterations=8,
    )

    parsed = extract_json(result.final_text) or {}
    scorecard_path = parsed.get("scorecard_path")

    update: Dict[str, Any] = {
        "scorecard_path": scorecard_path,
        "cdet_ai_score": float(parsed.get("cdet_ai_score", 0.0) or 0.0),
        "ai_confidence": float(parsed.get("ai_confidence", 0.0) or 0.0),
        "automation_readiness": parsed.get("automation_readiness", "Unknown"),
        "quality_blockers": parsed.get("quality_blockers", []) or [],
        "stage_traces": {
            "cdets_scoring": stage_trace(
                status="ok" if scorecard_path else "failed",
                duration=result.duration_seconds,
                iterations=result.iterations,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                tool_calls=result.tool_log,
            )
            | {"start_time": utc_now_iso()},
        },
    }
    logger.info(
        "cdets_scoring: cdets=%s score=%.1f conf=%.2f readiness=%s",
        state["cdets_id"],
        update["cdet_ai_score"],
        update["ai_confidence"],
        update["automation_readiness"],
    )
    return update
