"""Stage 07 — delivery (terminal Python node).

Three side-effects, each independently flagged:
  1. MongoDB upsert into ``cdetDB.orders``.
  2. TFTP copy of artifacts to the shared directory.
  3. SMTP send of the email composed by stage 06.

Failures in any one are logged but do not abort delivery — every step
records its own boolean. ``dry_run`` mode skips all side-effects and just
returns what would have happened.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List

from ..state import FLAgentState
from ..tools.email_tool import send_email
from ..tools.mongo import push_to_mongo
from ..tools.tftp import push_to_tftp
from ..utils import stage_trace, utc_now_iso
from ..utils_summary import build_run_summary

logger = logging.getLogger(__name__)


def _write_pipeline_traces(artifact_dir: str, traces: Dict[str, Any]) -> None:
    """Persist per-stage traces (incl. token usage) next to artifacts."""
    if not artifact_dir or not traces:
        return
    try:
        out = Path(artifact_dir) / "_pipeline_traces.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps({"saved_at": utc_now_iso(), "stages": traces}, indent=2, default=str),
            encoding="utf-8",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to persist _pipeline_traces.json: %s", exc)


def _write_run_summary(state: FLAgentState) -> None:
    """Persist the post-run summary (bug analysis + scorecard + coverage)."""
    artifact_dir = state.get("artifact_dir") or ""
    cdets_id = state.get("cdets_id") or ""
    if not artifact_dir or not cdets_id:
        return
    try:
        summary = build_run_summary(dict(state), model_used=state.get("model_used"))
        out = Path(artifact_dir) / f"{cdets_id}_summary.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(summary, indent=2, default=str),
            encoding="utf-8",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to persist run summary: %s", exc)


def _collect_artifacts(state: FLAgentState) -> List[str]:
    paths = [
        state.get("related_cdets_path"),
        state.get("cdets_schema_path"),
        state.get("tz_schema_path"),
        state.get("union_schema_path"),
        state.get("scorecard_path"),
        state.get("cafy_rca_json_path"),
        state.get("cafy_rca_md_path"),
        state.get("testcase_path"),
    ]
    return [p for p in paths if p]


async def delivery_node(state: FLAgentState) -> Dict[str, Any]:
    started = time.monotonic()
    cdets_id = state["cdets_id"]
    config = state["config"]
    dry_run = bool(state.get("dry_run", False))

    # 1. Mongo
    mongo_result = push_to_mongo(
        cdets_id=cdets_id,
        state=dict(state),
        config=config,
        dry_run=dry_run,
    )
    mongo_pushed = bool(mongo_result.get("ok"))

    # 2. TFTP
    tftp_root = config.get("tftp", {}).get("root", "/path/to/cdets_feedback")
    tftp_result = push_to_tftp(
        cdets_id=cdets_id,
        artifact_paths=_collect_artifacts(state),
        tftp_root=tftp_root,
        dry_run=dry_run,
    )
    tftp_delivered = bool(tftp_result.get("ok"))

    # 3. Email
    email_cfg = config.get("email", {})
    payload = state.get("email_payload") or {}
    email_result: Dict[str, Any] = {"ok": False, "skipped": True}
    if email_cfg.get("enabled", True) and payload.get("body_html"):
        email_result = send_email(
            subject=payload.get("subject", f"AI-FL {cdets_id}"),
            body_html=payload.get("body_html", ""),
            to_addresses=payload.get("to", []) or [],
            cc_addresses=(payload.get("cc", []) or []) + (email_cfg.get("cc") or []),
            from_address=email_cfg.get("from_address", "fl-agent@example.com"),
            attachment_paths=payload.get("attachment_paths", []) or [],
            transport=email_cfg.get("transport", "sendmail"),
            sendmail_path=email_cfg.get("sendmail_path", "/usr/sbin/sendmail"),
            smtp_host=email_cfg.get("smtp_host", "localhost"),
            smtp_port=int(email_cfg.get("smtp_port", 25)),
            dry_run=dry_run,
        )
    email_sent = bool(email_result.get("ok"))

    delivery_status = "ok" if (mongo_pushed and tftp_delivered) else "partial"
    if not (mongo_pushed or tftp_delivered or email_sent):
        delivery_status = "failed"
    # Preserve the RAG short-circuit outcome so downstream consumers know this
    # bug was delivered as a likely duplicate rather than a full analysis.
    if state.get("rag_short_circuit") and delivery_status != "failed":
        delivery_status = "duplicate_match"

    logger.info(
        "delivery: cdets=%s mongo=%s tftp=%s email=%s dry_run=%s",
        cdets_id, mongo_pushed, tftp_delivered, email_sent, dry_run,
    )

    # Persist the full stage_traces (with per-stage token usage) so the UI
    # can render token totals after the run, not just live.
    _write_pipeline_traces(state.get("artifact_dir", ""), state.get("stage_traces") or {})

    # Persist the post-run summary (bug analysis + scorecard + coverage).
    # Merge in the side-effect flags we just computed so the summary's
    # delivery block reflects this run, not the pre-delivery state.
    enriched_state = dict(state)
    enriched_state.update({
        "mongo_pushed": mongo_pushed,
        "tftp_delivered": tftp_delivered,
        "email_sent": email_sent,
        "delivery_status": delivery_status,
    })
    _write_run_summary(enriched_state)

    return {
        "mongo_pushed": mongo_pushed,
        "tftp_delivered": tftp_delivered,
        "email_sent": email_sent,
        "cdets_attached": False,  # CDETS attach is out of scope for the LangGraph rebuild
        "delivery_status": delivery_status,
        "stage_traces": {
            "delivery": stage_trace(
                status=delivery_status,
                duration=time.monotonic() - started,
            )
            | {
                "start_time": utc_now_iso(),
                "mongo": mongo_result,
                "tftp": tftp_result,
                "email": email_result,
                "dry_run": dry_run,
            },
        },
    }
