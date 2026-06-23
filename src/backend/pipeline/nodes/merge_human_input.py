"""Stage 02d — merge_human_input.

Deep-merges the reviewer's answers into the CDETS schema JSON on disk so
that downstream stages (cdets_tz_analyzer, cafy_rca_analyzer, testcase
generator, coverage comparison) see the augmented payload exactly as
they would for any other defect.

Supports two shapes of ``human_input``:

1. ``{"fields": {"behavior.repro.steps": "...", "rca.root_cause": "..."}}``
   — dotted paths into the schema.
2. ``{"patch": {<arbitrary nested dict>}}``
   — direct deep-merge into the schema root.

A ``free_form_answers`` list, if present, is appended under
``human_input.free_form_answers`` so the LLM stages can read it.
"""

from __future__ import annotations

import copy
import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, Iterable

from ..state import FLAgentState
from ..utils import stage_trace, utc_now_iso

logger = logging.getLogger(__name__)


def _set_dotted(obj: Dict[str, Any], path: str, value: Any) -> None:
    parts = [p for p in path.split(".") if p]
    if not parts:
        return
    cursor: Any = obj
    for key in parts[:-1]:
        nxt = cursor.get(key)
        if not isinstance(nxt, dict):
            nxt = {}
            cursor[key] = nxt
        cursor = nxt
    cursor[parts[-1]] = value


def _deep_merge(dst: Dict[str, Any], src: Dict[str, Any]) -> None:
    for k, v in (src or {}).items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge(dst[k], v)
        else:
            dst[k] = v


async def merge_human_input_node(state: FLAgentState) -> Dict[str, Any]:
    started = time.monotonic()
    cdets_id = state["cdets_id"]
    schema_path_str = state.get("cdets_schema_path") or ""
    human_input: Dict[str, Any] = state.get("human_input") or {}

    if not schema_path_str:
        logger.warning("merge_human_input: no cdets_schema_path on state; skipping")
        return {
            "stage_traces": {
                "merge_human_input": stage_trace(
                    status="skipped",
                    duration=time.monotonic() - started,
                )
                | {"start_time": utc_now_iso(), "reason": "no schema path"},
            }
        }

    schema_path = Path(schema_path_str)
    try:
        original = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.exception("merge_human_input: failed to load schema")
        return {
            "stage_traces": {
                "merge_human_input": stage_trace(
                    status="failed",
                    duration=time.monotonic() - started,
                    error=str(exc),
                )
                | {"start_time": utc_now_iso()},
            }
        }

    merged = copy.deepcopy(original)

    fields: Dict[str, Any] = human_input.get("fields") or {}
    for dotted, value in fields.items():
        _set_dotted(merged, dotted, value)

    patch: Dict[str, Any] = human_input.get("patch") or {}
    if isinstance(patch, dict):
        _deep_merge(merged, patch)

    free_form: Iterable[str] = human_input.get("free_form_answers") or []
    if free_form:
        hi = merged.setdefault("human_input", {})
        existing = hi.get("free_form_answers") or []
        hi["free_form_answers"] = list(existing) + list(free_form)
        hi["reviewer_round"] = int(state.get("human_review_count", 0) or 0)

    # Persist augmented schema next to the original and update state to point
    # at it so downstream stages pick up the new content.
    augmented_path = schema_path.with_name(
        f"{cdets_id}_Cdets_Schema_Template_v{int(state.get('human_review_count', 0) or 0) + 1}.json"
    )
    augmented_path.write_text(json.dumps(merged, indent=2), encoding="utf-8")

    duration = time.monotonic() - started
    logger.info(
        "merge_human_input: cdets=%s wrote %s (fields=%d patch_keys=%d)",
        cdets_id, augmented_path, len(fields), len(patch) if isinstance(patch, dict) else 0,
    )

    return {
        "cdets_schema_path": str(augmented_path),
        "needs_human_review": False,
        "stage_traces": {
            "merge_human_input": stage_trace(
                status="ok",
                duration=duration,
            )
            | {
                "start_time": utc_now_iso(),
                "fields_applied": len(fields),
                "patch_keys": len(patch) if isinstance(patch, dict) else 0,
                "schema_path": str(augmented_path),
            },
        },
    }
