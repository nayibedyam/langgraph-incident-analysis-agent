"""Tests for prescan version normalization (no-network paths only)."""

from __future__ import annotations

from backend.pipeline.prescan import _normalize_version


def test_version_simple():
    assert _normalize_version("7.10.2") == "7.10.2"


def test_version_picks_lowest_token():
    # Multiple tokens — should pick the lowest
    assert _normalize_version("7.10.2 and 8.0.0") == "7.10.2"


def test_version_strips_brackets():
    assert _normalize_version("[7.10.2]") == "7.10.2"


def test_version_pads_to_three():
    assert _normalize_version("7.10") == "7.10.0"
    # Bare integers like "7" need at least N.M form to be picked up — same as
    # the production prescan regex.
    assert _normalize_version("7.10.2.1") == "7.10.2"


def test_version_empty():
    assert _normalize_version("") == ""
    assert _normalize_version("not-a-version") == ""


def test_version_handles_sdk_prefix():
    assert _normalize_version("SDK-7.10.2") == "7.10.2"
