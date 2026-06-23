"""Stage 06 — email_report_generator (LLM agent).

Composes the email subject + HTML body. Does NOT send — that happens in the
delivery node so dry-run mode can preview the payload.
"""

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
Compose the AI-FL summary email for **{cdets_id}**.

Defect facts:
- Component / AP / SubAP : {component} / {primary_ap} / {primary_subap}
- Version / Severity     : {version} / {severity}
- DTPT manager           : {dtpt_manager}

Verdict:
- AI score / confidence  : {cdet_ai_score} / {ai_confidence}
- Automation readiness   : {automation_readiness}
- Coverage grade / class : {test_coverage_grade} / {coverage_classification}
- CaFy verdict           : {cafy_coverage_verdict}

Artifacts (use these in attachment_paths):
- Schema   : {cdets_schema_path}
- Scorecard: {scorecard_path}
- TestCase : {testcase_path}
- RCA MD   : {cafy_rca_md_path}

You may read any artifact with `read_file_text` if you need quotes for the
RCA summary. Return the final JSON envelope.
"""


async def email_report_generator_node(state: FLAgentState) -> Dict[str, Any]:
    started = time.monotonic()
    config = state["config"]
    llm = get_llm(config, stage="email")

    user_message = format_user_message(
        _USER_TEMPLATE,
        cdets_id=state["cdets_id"],
        component=state.get("component", ""),
        primary_ap=state.get("primary_ap", ""),
        primary_subap=state.get("primary_subap", ""),
        version=state.get("version", ""),
        severity=state.get("severity", ""),
        dtpt_manager=state.get("dtpt_manager", ""),
        cdet_ai_score=state.get("cdet_ai_score", 0.0),
        ai_confidence=state.get("ai_confidence", 0.0),
        automation_readiness=state.get("automation_readiness", ""),
        test_coverage_grade=state.get("test_coverage_grade", ""),
        coverage_classification=state.get("coverage_classification", ""),
        cafy_coverage_verdict=state.get("cafy_coverage_verdict", ""),
        cdets_schema_path=state.get("cdets_schema_path", ""),
        scorecard_path=state.get("scorecard_path", ""),
        testcase_path=state.get("testcase_path", ""),
        cafy_rca_md_path=state.get("cafy_rca_md_path", ""),
    )

    result = await run_agent_loop(
        llm,
        system_prompt=load_prompt("email_report"),
        user_message=user_message,
        tools=[read_file_text],
        max_iterations=6,
    )

    parsed = extract_json(result.final_text) or {}

    payload = {
        "subject": parsed.get("subject", f"AI-FL {state['cdets_id']}"),
        "body_html": parsed.get("body_html", ""),
        "to": parsed.get("to") or [],
        "cc": parsed.get("cc") or [],
        "attachment_paths": parsed.get("attachment_paths") or [],
    }

    update: Dict[str, Any] = {
        "email_payload": payload,
        "email_subject": payload["subject"],
        "attachment_paths": payload["attachment_paths"],
        "stage_traces": {
            "email_report_generator": stage_trace(
                status="ok" if payload["body_html"] else "failed",
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
        "email_report_generator: cdets=%s subject=%s recipients=%d",
        state["cdets_id"],
        payload["subject"],
        len(payload["to"]) + len(payload["cc"]),
    )
    return update
