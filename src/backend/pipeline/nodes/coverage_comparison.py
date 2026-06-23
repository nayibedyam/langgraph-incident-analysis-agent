"""Stage 05 — coverage_comparison (LLM agent)."""

from __future__ import annotations

import logging
import time
from typing import Any, Dict

from ..agent_loop import run_agent_loop
from ..llm import get_llm
from ..state import FLAgentState
from ..tools.filesystem import read_file_text
from ..utils import stage_trace, utc_now_iso
from ._llm_helpers import extract_json, format_user_message, load_prompt

logger = logging.getLogger(__name__)

_USER_TEMPLATE = """\
Compare the proposed test case to the existing CaFy test inventory for
**{cdets_id}**.

- New test case markdown : {testcase_path}
- CaFy RCA JSON          : {cafy_rca_json_path}
- Existing tests (truncated): {existing_tests}
- Existing verifiers     : {existing_verifiers}
- Existing helpers       : {existing_helpers}

Read the new test case with `read_file_text` (and the RCA JSON if useful).
Then return the final JSON verdict described in the system prompt.
"""


def _truncate(items: list, limit: int = 40) -> list:
    return list(items)[:limit]


async def coverage_comparison_node(state: FLAgentState) -> Dict[str, Any]:
    started = time.monotonic()
    config = state["config"]
    llm = get_llm(config, stage="coverage")
    merged = state.get("merged_coverage_input") or {}

    user_message = format_user_message(
        _USER_TEMPLATE,
        cdets_id=state["cdets_id"],
        testcase_path=merged.get("testcase_path") or "",
        cafy_rca_json_path=merged.get("cafy_rca_json_path") or "",
        existing_tests=_truncate(merged.get("existing_tests", [])),
        existing_verifiers=_truncate(merged.get("existing_verifiers", [])),
        existing_helpers=_truncate(merged.get("existing_helpers", [])),
    )

    result = await run_agent_loop(
        llm,
        system_prompt=load_prompt("coverage"),
        user_message=user_message,
        tools=[read_file_text],
        max_iterations=6,
    )

    parsed = extract_json(result.final_text) or {}

    update: Dict[str, Any] = {
        "test_coverage_confidence": float(parsed.get("test_coverage_confidence", 0.0) or 0.0),
        "test_coverage_grade": parsed.get("test_coverage_grade", "F"),
        "coverage_classification": parsed.get("coverage_classification", "none"),
        "stage_traces": {
            "coverage_comparison": stage_trace(
                status="ok" if parsed else "failed",
                duration=result.duration_seconds,
                iterations=result.iterations,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                tool_calls=result.tool_log,
            )
            | {
                "start_time": utc_now_iso(),
                "rationale": parsed.get("rationale", ""),
                "duplicate_with": parsed.get("duplicate_with", []),
                "missing_aspects": parsed.get("missing_aspects", []),
            },
        },
    }
    logger.info(
        "coverage_comparison: cdets=%s grade=%s class=%s conf=%.2f",
        state["cdets_id"],
        update["test_coverage_grade"],
        update["coverage_classification"],
        update["test_coverage_confidence"],
    )
    return update
