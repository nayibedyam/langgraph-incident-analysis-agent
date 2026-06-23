"""Stage 04a — testcase_generator (LLM agent, fan-out branch 1)."""

from __future__ import annotations

import logging
import time
from typing import Any, Dict

from ..agent_loop import run_agent_loop
from ..llm import get_llm
from ..state import FLAgentState
from ..tools.blueprints import read_blueprint
from ..tools.filesystem import read_file_text, write_artifact_text
from ..utils import stage_trace, utc_now_iso
from ._llm_helpers import extract_json, format_user_message, load_prompt

logger = logging.getLogger(__name__)

_USER_TEMPLATE = """\
Draft an executable test case markdown for **{cdets_id}**.

Inputs:
- CDETS schema      : {cdets_schema_path}
- Union schema      : {union_schema_path}
- CaFy RCA JSON     : {cafy_rca_json_path}
- AP / SubAP        : {primary_ap} / {primary_subap}
- Blueprint         : {blueprint_dir}
- Version, Severity : {version}, {severity}

Write the markdown to ``{cdets_id}/AI-FL-{cdets_id}_TestCase.md`` and
return the final JSON described in the system prompt.
"""


async def testcase_generator_node(state: FLAgentState) -> Dict[str, Any]:
    started = time.monotonic()
    config = state["config"]
    llm = get_llm(config, stage="testcase")

    user_message = format_user_message(
        _USER_TEMPLATE,
        cdets_id=state["cdets_id"],
        cdets_schema_path=state.get("cdets_schema_path", ""),
        union_schema_path=state.get("union_schema_path") or "",
        cafy_rca_json_path=state.get("cafy_rca_json_path", ""),
        primary_ap=state.get("primary_ap", ""),
        primary_subap=state.get("primary_subap", ""),
        blueprint_dir=state.get("blueprint_dir") or "",
        version=state.get("version", ""),
        severity=state.get("severity", ""),
    )

    result = await run_agent_loop(
        llm,
        system_prompt=load_prompt("testcase_gen"),
        user_message=user_message,
        tools=[read_file_text, read_blueprint, write_artifact_text],
        max_iterations=10,
    )

    parsed = extract_json(result.final_text) or {}

    update: Dict[str, Any] = {
        "testcase_path": parsed.get("testcase_path"),
        "test_scenarios": parsed.get("test_scenarios", []) or [],
        "stage_traces": {
            "testcase_generator": stage_trace(
                status="ok" if parsed.get("testcase_path") else "failed",
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
        "testcase_generator: cdets=%s path=%s scenarios=%d",
        state["cdets_id"],
        update["testcase_path"],
        len(update["test_scenarios"]),
    )
    return update
