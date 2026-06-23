"""Tests for ``backend.pipeline.utils`` helpers."""

from __future__ import annotations

from backend.pipeline.utils import (
    is_valid_cdets_id,
    resolve_env_in_value,
    stage_trace,
    utc_now_iso,
)


def test_valid_cdets_ids():
    assert is_valid_cdets_id("CSCwk35275")
    assert is_valid_cdets_id("CSCab12345")


def test_invalid_cdets_ids():
    for bad in ("", "csc12345", "CSC123", "CSCwk3527", "CSCwkx5275", "FOO12345"):
        assert not is_valid_cdets_id(bad), bad


def test_resolve_env_with_default(monkeypatch):
    monkeypatch.delenv("FL_TEST_VAR", raising=False)
    assert resolve_env_in_value("${FL_TEST_VAR:-fallback}") == "fallback"
    monkeypatch.setenv("FL_TEST_VAR", "real")
    assert resolve_env_in_value("${FL_TEST_VAR:-fallback}") == "real"


def test_resolve_env_nested():
    out = resolve_env_in_value({"a": ["${X:-1}", "${Y:-2}"], "b": "lit"})
    assert out == {"a": ["1", "2"], "b": "lit"}


def test_stage_trace_minimal():
    entry = stage_trace(status="ok", duration=0.123)
    assert entry["status"] == "ok"
    assert entry["duration_seconds"] == 0.123
    assert "end_time" in entry


def test_utc_now_iso_format():
    ts = utc_now_iso()
    assert ts.endswith("Z")
    assert "T" in ts
