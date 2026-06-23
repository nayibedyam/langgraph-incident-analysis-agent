"""MongoDB delivery tool — upserts the FL Agent record into ``cdetDB.orders``.

Reuses the same schema as the production ``push_to_dashboard.py``: the
collection key is the CDETS defect ID, and the document carries the analyzer
output, scorecard, and timestamps so the Agent Metrics dashboard can render
the run.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_optional(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        return None
    return p.read_text(encoding="utf-8", errors="replace")


def _read_optional_json(path: Optional[str]) -> Optional[Dict[str, Any]]:
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def push_to_mongo(
    *,
    cdets_id: str,
    state: Dict[str, Any],
    config: Dict[str, Any],
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Upsert the pipeline result for *cdets_id* into MongoDB.

    Reads the artifact files referenced in *state* and assembles a single
    document per the existing dashboard schema. In dry-run mode, returns
    the payload without connecting.
    """
    mongo_cfg = config.get("mongodb", {})
    uri = (os.getenv("MONGO_URI") or "").strip() or mongo_cfg.get("uri", "")
    db_name = mongo_cfg.get("database", "cdetDB")
    coll_name = mongo_cfg.get("collection", "orders")

    schema_data = _read_optional_json(state.get("cdets_schema_path"))
    cafy_rca = _read_optional_json(state.get("cafy_rca_json_path"))
    scorecard_md = _read_optional(state.get("scorecard_path"))
    testcase_md = _read_optional(state.get("testcase_path"))
    rca_md = _read_optional(state.get("cafy_rca_md_path"))

    document = {
        "cdets_id": cdets_id,
        "updated_at": _utc_now(),
        "invocation_mode": state.get("invocation_mode", "LANGGRAPH"),
        "component": state.get("component", ""),
        "ap": state.get("primary_ap", ""),
        "subap": state.get("primary_subap", ""),
        "version": state.get("version", ""),
        "severity": state.get("severity", ""),
        "dtpt_manager": state.get("dtpt_manager", ""),
        "schema_data": schema_data or {},
        "scorecard_md": scorecard_md or "",
        "testcase_md": testcase_md or "",
        "cafy_rca": cafy_rca or {},
        "cafy_rca_md": rca_md or "",
        "scoring": {
            "cdet_ai_score": state.get("cdet_ai_score", 0.0),
            "ai_confidence": state.get("ai_confidence", 0.0),
            "automation_readiness": state.get("automation_readiness", ""),
            "quality_blockers": state.get("quality_blockers", []),
        },
        "coverage": {
            "test_coverage_confidence": state.get("test_coverage_confidence", 0.0),
            "test_coverage_grade": state.get("test_coverage_grade", ""),
            "coverage_classification": state.get("coverage_classification", ""),
            "cafy_coverage_verdict": state.get("cafy_coverage_verdict", ""),
        },
        "artifacts": {
            "cdets_schema_path": state.get("cdets_schema_path"),
            "tz_schema_path": state.get("tz_schema_path"),
            "union_schema_path": state.get("union_schema_path"),
            "scorecard_path": state.get("scorecard_path"),
            "cafy_rca_json_path": state.get("cafy_rca_json_path"),
            "cafy_rca_md_path": state.get("cafy_rca_md_path"),
            "testcase_path": state.get("testcase_path"),
        },
        "stage_traces": state.get("stage_traces", {}),
    }

    if dry_run:
        return {"ok": True, "dry_run": True, "doc_keys": list(document.keys())}

    if not uri:
        return {"ok": False, "error": "MONGO_URI not configured"}

    try:
        from pymongo import MongoClient
    except ImportError:
        return {"ok": False, "error": "pymongo is not installed"}

    try:
        client = MongoClient(uri, serverSelectionTimeoutMS=10_000)
        coll = client[db_name][coll_name]
        result = coll.update_one(
            {"cdets_id": cdets_id},
            {"$set": document, "$setOnInsert": {"created_at": _utc_now()}},
            upsert=True,
        )
        return {
            "ok": True,
            "matched": result.matched_count,
            "modified": result.modified_count,
            "upserted_id": str(result.upserted_id) if result.upserted_id else None,
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("Mongo upsert failed")
        return {"ok": False, "error": str(exc)}
    finally:
        try:
            client.close()  # type: ignore[union-attr]
        except Exception:
            pass
