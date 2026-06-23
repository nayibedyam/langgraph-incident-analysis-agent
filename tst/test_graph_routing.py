"""Smoke tests for graph compilation and abort routing."""

from __future__ import annotations

import pytest

from backend.pipeline.graph import build_graph
from backend.pipeline.state import initial_state
from backend.pipeline.utils import load_config


def test_graph_compiles():
    graph = build_graph()
    expected = {
        "common_infra",
        "prescan",
        "cdets_tz_analyzer",
        "cdets_scoring",
        "cafy_rca_analyzer",
        "testcase_generator",
        "existing_test_scanner",
        "merge_coverage",
        "coverage_comparison",
        "email_report_generator",
        "delivery",
        "abort",
    }
    assert expected.issubset(set(graph.nodes.keys()))


@pytest.mark.asyncio
async def test_invalid_cdets_id_routes_to_abort():
    graph = build_graph()
    cfg = load_config()
    state = initial_state("BOGUS", cfg, dry_run=True)
    result = await graph.ainvoke(state)
    assert result["init_valid"] is False
    assert result["delivery_status"] == "aborted"
    assert "common_infra" in result["stage_traces"]
    assert "abort" in result["stage_traces"]
    assert "cdets_tz_analyzer" not in result["stage_traces"]


@pytest.mark.asyncio
async def test_prescan_failure_aborts(monkeypatch):
    """When dumpcr is unavailable, prescan must route to abort."""
    from backend.pipeline.tools import cdets as cdets_tool

    def fake_lookup(_id):
        return {"ok": False, "error": "dumpcr not available"}

    monkeypatch.setattr(cdets_tool, "lookup_cdets_impl", fake_lookup)
    # Also patch the import inside prescan.py
    from backend.pipeline import prescan as prescan_mod
    monkeypatch.setattr(prescan_mod, "lookup_cdets_impl", fake_lookup)

    graph = build_graph()
    cfg = load_config()
    state = initial_state("CSCwk35275", cfg, dry_run=True)
    result = await graph.ainvoke(state)

    assert result["init_valid"] is True
    assert result["cdets_lookup_ok"] is False
    assert result["delivery_status"] == "aborted"
    assert "prescan" in result["stage_traces"]
    assert "cdets_tz_analyzer" not in result["stage_traces"]
