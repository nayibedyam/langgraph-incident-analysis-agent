"""Unit tests for the rag_fetch_related_cdets node and short-circuit routing."""

from __future__ import annotations

import pytest

from backend.pipeline.graph import _route_after_prescan, _route_after_rag
from backend.pipeline.nodes import rag_fetch_related_cdets as rag_node
from backend.pipeline.rag.retriever import RelatedCDETS


def test_route_after_prescan_to_rag():
    assert _route_after_prescan({"cdets_lookup_ok": True}) == "rag_fetch_related_cdets"
    assert _route_after_prescan({"cdets_lookup_ok": False}) == "abort"


def test_route_after_rag_short_circuit():
    assert _route_after_rag({"rag_short_circuit": True}) == "delivery"
    assert _route_after_rag({"rag_short_circuit": False}) == "cdets_tz_analyzer"
    assert _route_after_rag({}) == "cdets_tz_analyzer"


def _state(tmp_path, threshold=0.45, enabled=True):
    return {
        "cdets_id": "CSCxx00001",
        "artifact_dir": str(tmp_path),
        "cdets_fields": {"Headline": "h", "Summary": "s"},
        "config": {"rag": {"index_dir": "x", "top_k": 5,
                            "enabled": enabled, "high_match_threshold": threshold}},
    }


@pytest.mark.asyncio
async def test_high_match_short_circuits(tmp_path, monkeypatch):
    hits = [
        RelatedCDETS("CSCaa11111", 0.92, "near duplicate"),
        RelatedCDETS("CSCbb22222", 0.40, "related"),
    ]
    monkeypatch.setattr(rag_node, "get_related_cdets", lambda **kw: hits)

    out = await rag_node.rag_fetch_related_cdets_node(_state(tmp_path, threshold=0.45))

    assert out["rag_short_circuit"] is True
    assert out["rag_top_score"] == pytest.approx(0.92)
    assert len(out["related_cdets"]) == 2
    assert out["related_cdets_path"] is not None
    assert out["delivery_status"] == "duplicate_match"


@pytest.mark.asyncio
async def test_low_match_continues_pipeline(tmp_path, monkeypatch):
    hits = [RelatedCDETS("CSCaa11111", 0.12, "weakly related")]
    monkeypatch.setattr(rag_node, "get_related_cdets", lambda **kw: hits)

    out = await rag_node.rag_fetch_related_cdets_node(_state(tmp_path, threshold=0.45))

    assert out["rag_short_circuit"] is False
    assert "delivery_status" not in out
    assert len(out["related_cdets"]) == 1


@pytest.mark.asyncio
async def test_disabled_never_short_circuits(tmp_path, monkeypatch):
    hits = [RelatedCDETS("CSCaa11111", 0.99, "duplicate")]
    monkeypatch.setattr(rag_node, "get_related_cdets", lambda **kw: hits)

    out = await rag_node.rag_fetch_related_cdets_node(
        _state(tmp_path, threshold=0.45, enabled=False)
    )

    assert out["rag_short_circuit"] is False


@pytest.mark.asyncio
async def test_missing_index_is_non_fatal(tmp_path, monkeypatch):
    def boom(**kw):
        raise FileNotFoundError("no index")

    monkeypatch.setattr(rag_node, "get_related_cdets", boom)

    out = await rag_node.rag_fetch_related_cdets_node(_state(tmp_path))

    assert out["related_cdets"] == []
    assert out["rag_short_circuit"] is False
    assert out["related_cdets_path"] is None
