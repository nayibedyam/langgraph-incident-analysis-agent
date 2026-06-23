"""Build a compact post-run summary from final pipeline state + schema JSON.

The output of :func:`build_run_summary` is persisted to disk as
``<cdets_id>_summary.json`` by :mod:`pipeline.nodes.delivery` and surfaced
to the UI via ``GET /api/artifacts/{id}/summary``.

This is the single source of truth for the "Summary" tab in the Defects
view: bug analysis, scorecard rollups, and coverage rollups. All values
come from data already in the final ``FLAgentState`` and the schema JSON
written by the cdets_tz_analyzer node — no extra LLM calls.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from .utils import utc_now_iso

logger = logging.getLogger(__name__)

SUMMARY_VERSION = 1


def _safe_load_json(path: Optional[str]) -> Dict[str, Any]:
    if not path:
        return {}
    p = Path(path)
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return {}


def _g(d: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    """Safe nested ``get`` — walks dotted-key path through dicts."""
    cur: Any = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur


def _weakest_fields(schema: Dict[str, Any], top_n: int = 3) -> List[Dict[str, Any]]:
    """Lowest-impact fields by weight × quality, capped at top_n."""
    scores = _g(schema, "defect_score", "field_quality", "field_scores", default={}) or {}
    rows: List[Dict[str, Any]] = []
    for path, entry in scores.items():
        if not isinstance(entry, dict):
            continue
        weight = float(entry.get("weight") or 0)
        quality = float(entry.get("quality") or 0)
        rows.append({
            "path": path,
            "label": entry.get("label"),
            "weight": int(weight) if weight.is_integer() else weight,
            "quality": quality,
            "value": entry.get("value"),
            "citation": entry.get("citation"),
            "_gap": weight * (1.0 - quality),
        })
    rows.sort(key=lambda r: r["_gap"], reverse=True)
    return [{k: v for k, v in r.items() if k != "_gap"} for r in rows[:top_n] if r["_gap"] > 0]


def _bug_analysis(schema: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Any]:
    defect = schema.get("defect", {}) or {}
    meta = schema.get("meta", {}) or {}
    src = meta.get("source_system", {}) or {}
    repro = defect.get("repro", {}) or {}
    behavior = defect.get("behavior", {}) or {}
    impact = behavior.get("impact", {}) or {}
    rca = defect.get("rca_summary", {}) or {}
    gate = schema.get("qualification_gate", {}) or {}
    comp_tags = _g(defect, "component", "technology", "tags", default=[]) or []
    component = ""
    if isinstance(comp_tags, list) and comp_tags:
        component = (comp_tags[0] or {}).get("name", "") if isinstance(comp_tags[0], dict) else ""

    return {
        "headline": defect.get("summary") or state.get("headline") or "",
        "component": component or state.get("component", ""),
        "primary_ap": state.get("primary_ap", ""),
        "sub_ap": state.get("primary_subap", ""),
        "version": state.get("version", ""),
        "severity": impact.get("severity") or state.get("severity", ""),
        "status": src.get("status", ""),
        "engineer": src.get("engineer", ""),
        "submitted_on": src.get("submitted_on", ""),
        "issue_url": src.get("issue_url", ""),
        "repro": {
            "reproducibility": _g(repro, "reproducibility", "value", default=""),
            "triggers": repro.get("triggers", []) or [],
            "soak_required": _g(repro, "soak", "required"),
            "traffic_required": _g(repro, "traffic", "required"),
        },
        "behavior": {
            "expected": behavior.get("expected", ""),
            "actual": behavior.get("actual", ""),
            "impact_severity": impact.get("severity", ""),
            "impact_priority": impact.get("priority", ""),
        },
        "failure_category": _g(defect, "failure", "category", default=""),
        "rca": {
            "available": bool(rca.get("available")),
            "root_cause": rca.get("root_cause_description") or rca.get("description", ""),
            "fix_approach": rca.get("fix_approach", ""),
            "confidence": rca.get("confidence", ""),
            "sources": rca.get("sources", []) or [],
        },
        "qualification": {
            "completion_status": gate.get("completion_status", ""),
            "ai_eligible": bool(gate.get("ai_eligible")),
            "missing_fields": gate.get("missing_required_fields", []) or [],
            "blockers": gate.get("blockers", []) or [],
        },
    }


def _scorecard(schema: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Any]:
    ds = schema.get("defect_score", {}) or {}
    ai = schema.get("ai_confidence", {}) or {}
    ar = schema.get("automation_readiness", {}) or {}
    dt = schema.get("dt_testability", {}) or {}

    score_value = ds.get("score_value")
    if score_value is None:
        score_value = state.get("cdet_ai_score")

    return {
        "score_value": score_value,
        "grade": ds.get("grade"),
        "status": ds.get("status"),
        "required_fields": {
            "filled": _g(ds, "required_fields", "filled", default=0),
            "total": _g(ds, "required_fields", "total", default=0),
            "percent": _g(ds, "required_fields", "percent", default=0),
        },
        "weighted": {
            "earned": _g(ds, "weighted", "earned"),
            "total_applicable": _g(ds, "weighted", "total_applicable"),
            "final_percent": _g(ds, "weighted", "final_percent"),
        },
        "ai_confidence": {
            "overall_percent": ai.get("overall_percent") or state.get("ai_confidence"),
            "grade": ai.get("overall_grade"),
            "fields_at": {
                "high": ai.get("fields_at_high", 0),
                "medium": ai.get("fields_at_medium", 0),
                "low": ai.get("fields_at_low", 0),
                "none": ai.get("fields_at_none", 0),
            },
        },
        "automation_readiness": {
            "verdict": ar.get("verdict") or state.get("automation_readiness"),
            "fields_ready": ar.get("fields_ready", 0),
            "conditional": ar.get("fields_conditional", 0),
            "not_ready": ar.get("fields_not_ready", 0),
        },
        "dt_testability": {
            "alert": bool(dt.get("alert")),
            "alert_text": dt.get("alert_text", ""),
            "triggered_count": dt.get("triggered_count", 0),
            "triggered_list": dt.get("triggered_list", []) or [],
        },
        "weakest_fields": _weakest_fields(schema, top_n=3),
        "blockers": list(state.get("quality_blockers") or []),
    }


def _coverage(state: Dict[str, Any]) -> Dict[str, Any]:
    existing_tests = state.get("existing_tests") or []
    existing_verifiers = state.get("existing_verifiers") or []
    new_scenarios = state.get("test_scenarios") or []
    return {
        "cafy_verdict": state.get("cafy_coverage_verdict", ""),
        "coverage_gap": state.get("coverage_gap", ""),
        "gap_classification": state.get("gap_classification", ""),
        "test_coverage_confidence": state.get("test_coverage_confidence"),
        "test_coverage_grade": state.get("test_coverage_grade", ""),
        "coverage_classification": state.get("coverage_classification", ""),
        "existing_tests_count": len(existing_tests),
        "existing_verifiers_count": len(existing_verifiers),
        "new_scenarios_count": len(new_scenarios),
        "has_techzone": bool(state.get("has_techzone")),
    }


def _delivery(state: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "mongo_pushed": bool(state.get("mongo_pushed")),
        "tftp_delivered": bool(state.get("tftp_delivered")),
        "email_sent": bool(state.get("email_sent")),
        "delivery_status": state.get("delivery_status", ""),
        "scorecard_path": state.get("scorecard_path"),
        "testcase_path": state.get("testcase_path"),
        "rca_md_path": state.get("cafy_rca_md_path"),
        "schema_path": state.get("cdets_schema_path"),
    }


def build_run_summary(
    state: Dict[str, Any],
    *,
    model_used: Optional[str] = None,
) -> Dict[str, Any]:
    """Assemble the post-run summary dict to persist as ``<id>_summary.json``."""
    schema = _safe_load_json(state.get("cdets_schema_path"))
    return {
        "version": SUMMARY_VERSION,
        "saved_at": utc_now_iso(),
        "cdets_id": state.get("cdets_id"),
        "artifact_dir": state.get("artifact_dir"),
        "model_used": model_used,
        "bug_analysis": _bug_analysis(schema, state),
        "scorecard": _scorecard(schema, state),
        "coverage": _coverage(state),
        "delivery": _delivery(state),
    }


def derive_summary_from_disk(defect_dir: Path, cdets_id: str) -> Dict[str, Any]:
    """Best-effort summary for legacy defects without a persisted ``_summary.json``.

    Reads the schema JSON only — coverage block will be mostly empty.
    """
    schema_path = defect_dir / f"{cdets_id}_Cdets_Schema_Template.json"
    schema = _safe_load_json(str(schema_path))
    if not schema:
        return {}
    legacy_state: Dict[str, Any] = {"cdets_id": cdets_id}
    return {
        "version": SUMMARY_VERSION,
        "saved_at": None,
        "cdets_id": cdets_id,
        "artifact_dir": str(defect_dir),
        "model_used": None,
        "bug_analysis": _bug_analysis(schema, legacy_state),
        "scorecard": _scorecard(schema, legacy_state),
        "coverage": None,
        "delivery": None,
    }
