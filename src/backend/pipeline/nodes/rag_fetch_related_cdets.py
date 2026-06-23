"""RAG node — retrieve the top-k historically related CDETS.

Runs immediately after ``prescan`` (which has already fetched the bug's
``cdets_fields`` and ``component``). It queries the local TF-IDF index built
from the historical bug-list corpus and:

  * always attaches the top-k matches to the state (``related_cdets``) and
    writes them to ``<artifact_dir>/<ID>_related_cdets.json``;
  * if the best match similarity is at/above ``rag.high_match_threshold``,
    flags ``rag_short_circuit=True`` so the graph can emit those matches as the
    result and skip the expensive schema/scoring/RCA/testcase stages (the bug
    is a likely duplicate of an already-analysed one).

The node is best-effort: a missing index or empty corpus never aborts the run;
it simply leaves ``related_cdets`` empty and lets the normal pipeline continue.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, List

from ..rag import get_related_cdets
from ..state import FLAgentState
from ..utils import artifact_dir_for, stage_trace, utc_now_iso

logger = logging.getLogger(__name__)


def _rag_config(config: dict) -> dict:
    return config.get("rag", {}) or {}


def _write_related_artifact(
    state: FLAgentState, related: List[dict], top_score: float, short_circuit: bool
) -> str | None:
    """Persist the matches to a JSON artifact; return its path (or None)."""
    cdets_id = state["cdets_id"]
    art_dir = state.get("artifact_dir") or str(
        artifact_dir_for(cdets_id, state.get("config", {}).get("paths", {}).get("artifact_base"))
    )
    try:
        os.makedirs(art_dir, exist_ok=True)
        path = os.path.join(art_dir, f"{cdets_id}_related_cdets.json")
        payload = {
            "cdets_id": cdets_id,
            "generated_at": utc_now_iso(),
            "top_score": round(float(top_score), 4),
            "short_circuit": short_circuit,
            "related_cdets": related,
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        return path
    except OSError as exc:  # pragma: no cover - filesystem edge case
        logger.warning("rag: failed to write related-CDETS artifact: %s", exc)
        return None


async def rag_fetch_related_cdets_node(state: FLAgentState) -> Dict[str, Any]:
    started = time.monotonic()
    cdets_id = state["cdets_id"]
    config = state.get("config", {})
    rag_cfg = _rag_config(config)

    index_dir = rag_cfg.get("index_dir") or "cdets_data/rag_index"
    top_k = int(rag_cfg.get("top_k", 5))
    enabled = bool(rag_cfg.get("enabled", True))
    threshold = float(rag_cfg.get("high_match_threshold", 0.45))

    related: List[dict] = []
    top_score = 0.0
    error: str | None = None

    try:
        hits = get_related_cdets(
            index_dir=index_dir,
            cdets_id=cdets_id,
            fields=state.get("cdets_fields") or None,
            k=top_k,
            exclude_self=True,
        )
        related = [h.to_dict() for h in hits]
        top_score = hits[0].score if hits else 0.0
    except FileNotFoundError as exc:
        error = f"RAG index not found: {exc}"
        logger.warning("rag: %s — continuing pipeline without related-CDETS", exc)
    except Exception as exc:  # noqa: BLE001 - retrieval must never abort the run
        error = f"RAG retrieval failed: {exc}"
        logger.warning("rag: retrieval error: %s — continuing pipeline", exc)

    short_circuit = bool(enabled and related and top_score >= threshold)

    artifact_path = (
        _write_related_artifact(state, related, top_score, short_circuit)
        if related
        else None
    )

    logger.info(
        "rag: cdets=%s matches=%d top_score=%.4f threshold=%.2f short_circuit=%s",
        cdets_id, len(related), top_score, threshold, short_circuit,
    )

    update: Dict[str, Any] = {
        "related_cdets": related,
        "related_cdets_path": artifact_path,
        "rag_top_score": round(float(top_score), 4),
        "rag_short_circuit": short_circuit,
        "stage_traces": {
            "rag_fetch_related_cdets": stage_trace(
                status="ok" if not error else "warning",
                duration=time.monotonic() - started,
                error=error,
            )
            | {
                "start_time": utc_now_iso(),
                "match_count": len(related),
                "top_score": round(float(top_score), 4),
                "threshold": threshold,
                "short_circuit": short_circuit,
            },
        },
    }
    if short_circuit:
        # Surface a concise outcome on the state for delivery/output.
        update["delivery_status"] = "duplicate_match"
    return update
