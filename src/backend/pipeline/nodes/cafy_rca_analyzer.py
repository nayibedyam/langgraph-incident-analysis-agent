"""Stage 03 — cafy_rca_analyzer (LLM agent)."""

from __future__ import annotations

import logging
import time
from typing import Any, Dict

from ..agent_loop import run_agent_loop
from ..llm import get_llm
from ..state import FLAgentState
from ..tools.blueprints import list_subaps, read_blueprint, resolve_ap_for_component
from ..tools.cafy import grep_cafy_tests, scan_cafy_tests
from ..tools.filesystem import (
    list_directory,
    read_file_text,
    write_artifact_json,
    write_artifact_text,
)
from ..utils import stage_trace, utc_now_iso
from ._llm_helpers import extract_json, format_user_message, load_prompt

logger = logging.getLogger(__name__)

_USER_TEMPLATE = """\
Run CaFy RCA analysis for **{cdets_id}**.

Inputs:
- CDETS schema  : {cdets_schema_path}
- Component     : {component}
- AP            : {primary_ap}
- SubAP         : {primary_subap}
- Blueprint     : {blueprint_dir}
- Version       : {version}
- Severity      : {severity}

Procedure:
1. Read the schema with `read_file_text`.
2. If a blueprint path is provided, use `read_blueprint`.
3. Use `scan_cafy_tests` and `grep_cafy_tests` to find existing coverage.
4. Write the JSON and markdown RCA outputs.
5. Return the final JSON envelope as described in the system prompt.
"""


async def cafy_rca_analyzer_node(state: FLAgentState) -> Dict[str, Any]:
    started = time.monotonic()
    config = state["config"]
    llm = get_llm(config, stage="rca")

    user_message = format_user_message(
        _USER_TEMPLATE,
        cdets_id=state["cdets_id"],
        cdets_schema_path=state.get("cdets_schema_path", ""),
        component=state.get("component", ""),
        primary_ap=state.get("primary_ap", ""),
        primary_subap=state.get("primary_subap", ""),
        blueprint_dir=state.get("blueprint_dir") or "",
        version=state.get("version", ""),
        severity=state.get("severity", ""),
    )

    result = await run_agent_loop(
        llm,
        system_prompt=load_prompt("cafy_rca"),
        user_message=user_message,
        tools=[
            read_file_text,
            list_directory,
            read_blueprint,
            list_subaps,
            resolve_ap_for_component,
            scan_cafy_tests,
            grep_cafy_tests,
            write_artifact_json,
            write_artifact_text,
        ],
        max_iterations=14,
    )

    parsed = extract_json(result.final_text) or {}

    update: Dict[str, Any] = {
        "cafy_rca_json_path": parsed.get("cafy_rca_json_path"),
        "cafy_rca_md_path": parsed.get("cafy_rca_md_path"),
        "automation_mapping": parsed.get("automation_mapping") or {},
        "coverage_gap": parsed.get("coverage_gap", "Unknown"),
        "gap_classification": parsed.get("gap_classification", "unknown"),
        "cafy_coverage_verdict": parsed.get("cafy_coverage_verdict", ""),
        "genc_handoff": parsed.get("genc_handoff") or {},
        "stage_traces": {
            "cafy_rca_analyzer": stage_trace(
                status="ok" if parsed.get("cafy_rca_json_path") else "failed",
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
        "cafy_rca_analyzer: cdets=%s gap=%s class=%s",
        state["cdets_id"],
        update["coverage_gap"],
        update["gap_classification"],
    )
    return update
