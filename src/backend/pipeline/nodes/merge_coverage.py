"""Stage 04c — merge_coverage (join after fan-out).

Pure transformation: gathers outputs from `testcase_generator` and
`existing_test_scanner` into a single ``merged_coverage_input`` dict
that feeds `coverage_comparison`.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict

from ..state import FLAgentState
from ..utils import stage_trace, utc_now_iso

logger = logging.getLogger(__name__)


async def merge_coverage_node(state: FLAgentState) -> Dict[str, Any]:
    started = time.monotonic()
    merged = {
        "testcase_path": state.get("testcase_path"),
        "test_scenarios": state.get("test_scenarios", []) or [],
        "existing_tests": state.get("existing_tests", []) or [],
        "existing_verifiers": state.get("existing_verifiers", []) or [],
        "existing_helpers": state.get("existing_helpers", []) or [],
        "cafy_rca_json_path": state.get("cafy_rca_json_path"),
        "automation_mapping": state.get("automation_mapping") or {},
    }
    logger.info(
        "merge_coverage: tc=%s existing_tests=%d scenarios=%d",
        merged["testcase_path"],
        len(merged["existing_tests"]),
        len(merged["test_scenarios"]),
    )
    return {
        "merged_coverage_input": merged,
        "stage_traces": {
            "merge_coverage": stage_trace(
                status="ok",
                duration=time.monotonic() - started,
            )
            | {"start_time": utc_now_iso()},
        },
    }
