"""Stage 02b — missing_info_request.

When ``cdets_scoring`` produces ``cdet_ai_score`` below the configured
threshold, this node asks Haiku to identify the minimum set of fields a
human reviewer must supply, persists the request to disk, and sends a
reviewer email with a link back to the UI.

The actual pause-for-human happens in :func:`human_review_node` via
``langgraph.types.interrupt``; this node only prepares the request and
notifies the reviewer.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict

_MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_MD_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)")
_MD_BACKTICK_RE = re.compile(r"`([^`]+)`")


def _strip_markdown(text: str) -> str:
    """Remove inline markdown emphasis that Haiku sometimes leaks into
    `summary_for_reviewer` despite the prompt forbidding it."""
    if not text:
        return text
    text = _MD_BOLD_RE.sub(r"\1", text)
    text = _MD_ITALIC_RE.sub(r"\1", text)
    text = _MD_BACKTICK_RE.sub(r"\1", text)
    return text.strip()

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..agent_loop import run_agent_loop
from ..llm import get_llm
from ..state import FLAgentState
from ..tools.email_tool import send_email
from ..tools.filesystem import read_file_text
from ..utils import backend_root, stage_trace, utc_now_iso
from ._llm_helpers import extract_json, format_user_message, load_prompt

logger = logging.getLogger(__name__)

_USER_TEMPLATE = """\
The defect **{cdets_id}** scored **{cdet_ai_score:.1f}** which is below
the acceptance threshold ({score_threshold}). Read the schema and the
scorecard's quality blockers, then return the JSON described in the
system prompt.

Inputs:
- CDETS schema      : {cdets_schema_path}
- Scorecard         : {scorecard_path}
- Quality blockers  : {quality_blockers}
- Headline          : {headline}
"""


def _render_email(template_dir: Path, **ctx: Any) -> str:
    env = Environment(
        loader=FileSystemLoader(str(template_dir)),
        autoescape=select_autoescape(["html"]),
    )
    tpl = env.get_template("missing_info_request.html.j2")
    return tpl.render(**ctx)


async def missing_info_request_node(state: FLAgentState) -> Dict[str, Any]:
    started = time.monotonic()
    config = state["config"]
    hil_cfg = config.get("human_in_loop", {}) or {}
    email_cfg = config.get("email", {}) or {}

    cdets_id = state["cdets_id"]
    score = float(state.get("cdet_ai_score", 0.0) or 0.0)
    threshold = float(hil_cfg.get("score_threshold", 60))

    llm = get_llm(config, stage="email")  # Haiku — cheap, structured JSON

    user_message = format_user_message(
        _USER_TEMPLATE,
        cdets_id=cdets_id,
        cdet_ai_score=score,
        score_threshold=threshold,
        cdets_schema_path=state.get("cdets_schema_path", ""),
        scorecard_path=state.get("scorecard_path", "") or "",
        quality_blockers=state.get("quality_blockers", []) or [],
        headline=state.get("headline", "") or "",
    )

    result = await run_agent_loop(
        llm,
        system_prompt=load_prompt("missing_info"),
        user_message=user_message,
        tools=[read_file_text],
        max_iterations=4,
    )

    parsed = extract_json(result.final_text) or {}
    missing_fields = parsed.get("missing_fields") or []
    free_form = parsed.get("free_form_questions") or []
    summary = _strip_markdown(parsed.get("summary_for_reviewer") or "")

    artifact_dir = Path(state.get("artifact_dir") or f"cdets_data/{cdets_id}")
    artifact_dir.mkdir(parents=True, exist_ok=True)
    request_payload = {
        "cdets_id": cdets_id,
        "cdet_ai_score": score,
        "score_threshold": threshold,
        "missing_fields": missing_fields,
        "free_form_questions": free_form,
        "summary_for_reviewer": summary,
        "headline": state.get("headline"),
        "generated_at": utc_now_iso(),
    }
    request_path = artifact_dir / f"{cdets_id}_missing_info_request.json"
    request_path.write_text(json.dumps(request_payload, indent=2), encoding="utf-8")

    # Build the UI link. job_id falls back to cdets_id when running outside the
    # FastAPI runner (CLI / pytest); the FE accepts either form.
    job_id = state.get("job_id") or cdets_id
    base = str(hil_cfg.get("review_url_base") or "http://localhost:5173").rstrip("/")
    review_url = f"{base}/?review={job_id}&cdets={cdets_id}"

    _template_dir = Path(email_cfg.get("template_dir", "pipeline/templates"))
    if not _template_dir.is_absolute():
        _template_dir = backend_root() / _template_dir
    body_html = _render_email(
        _template_dir,
        cdets_id=cdets_id,
        cdet_ai_score=score,
        score_threshold=int(threshold),
        headline=state.get("headline"),
        summary_for_reviewer=summary,
        missing_fields=missing_fields,
        free_form_questions=free_form,
        review_url=review_url,
        job_id=job_id,
        from_address=email_cfg.get("from_address", "reviewer@example.com"),
    )

    recipients = hil_cfg.get("recipients") or []
    email_result: Dict[str, Any] = {"ok": False, "error": "no recipients configured"}
    if recipients:
        email_result = send_email(
            subject=f"[AI-FL] Reviewer input needed — {cdets_id} (score {score:.0f})",
            body_html=body_html,
            to_addresses=recipients,
            from_address=email_cfg.get("from_address", "reviewer@example.com"),
            cc_addresses=email_cfg.get("cc") or [],
            attachment_paths=[str(request_path)],
            transport=email_cfg.get("transport", "sendmail"),
            sendmail_path=email_cfg.get("sendmail_path", "/usr/sbin/sendmail"),
            smtp_host=email_cfg.get("smtp_host", "localhost"),
            smtp_port=int(email_cfg.get("smtp_port", 25)),
            dry_run=bool(state.get("dry_run")),
        )

    duration = time.monotonic() - started
    logger.info(
        "missing_info_request: cdets=%s score=%.1f fields=%d email_ok=%s",
        cdets_id, score, len(missing_fields), email_result.get("ok"),
    )

    return {
        "needs_human_review": True,
        "missing_info_request": request_payload,
        "missing_info_request_path": str(request_path),
        "human_review_email_sent": bool(email_result.get("ok")),
        "review_url": review_url,
        "stage_traces": {
            "missing_info_request": stage_trace(
                status="ok" if email_result.get("ok") else "failed",
                duration=duration,
                iterations=result.iterations,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                error=None if email_result.get("ok") else email_result.get("error"),
            )
            | {
                "start_time": utc_now_iso(),
                "email": email_result,
                "missing_fields_count": len(missing_fields),
            },
        },
    }
