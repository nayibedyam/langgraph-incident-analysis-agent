"""Tests for AP / blueprint resolution (CSV-driven, no network)."""

from __future__ import annotations

from backend.pipeline.tools.blueprints import resolve_ap_impl


def test_resolve_unknown_component():
    out = resolve_ap_impl("definitely-not-a-real-component-xyz")
    assert out["ap"] == ""
    assert out["dtpt_manager"] == ""


def test_resolve_empty_component():
    out = resolve_ap_impl("")
    assert out == {"ap": "", "dtpt_manager": "", "blueprint_dir": "", "blueprint_path": ""}
