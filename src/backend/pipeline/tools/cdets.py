"""CDETS lookup tools — wrap ``dumpcr`` / ``cbugval`` shell commands.

These are macOS/dev-machine safe: when the binaries are unavailable (e.g. on
a developer laptop), the tools return a structured ``unavailable`` response
instead of raising, so the pipeline can still be exercised in dry-run.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from typing import Any, Dict

from langchain_core.tools import tool

logger = logging.getLogger(__name__)

DEFAULT_DUMPCR = "/path/to/dumpcr"
DEFAULT_CBUGVAL = "/path/to/cbugval"
DEFAULT_TIMEOUT = 20


def _resolve(bin_path: str, fallback_name: str) -> str:
    if os.path.isfile(bin_path):
        return bin_path
    return shutil.which(fallback_name) or ""


def _parse_dumpcr_output(raw: str) -> Dict[str, Any]:
    """Parse ``dumpcr -d`` text into a flat dict of structured fields."""
    fields: Dict[str, Any] = {}
    for line in raw.splitlines():
        if ":\t" not in line:
            continue
        key, _, value = line.partition(":\t")
        key = key.strip()
        value = value.strip()
        if key:
            fields[key] = value
    return fields


def lookup_cdets_impl(cdets_id: str) -> Dict[str, Any]:
    """Pure-Python implementation (re-used by tool wrapper and prescan)."""
    cdets_id = (cdets_id or "").strip()
    if not cdets_id:
        return {"ok": False, "error": "empty cdets_id"}

    dumpcr = _resolve(DEFAULT_DUMPCR, "dumpcr")
    if not dumpcr:
        return {
            "ok": False,
            "error": "dumpcr binary not available on this host",
            "fields": {},
            "available": False,
        }

    try:
        proc = subprocess.run(
            [dumpcr, "-d", cdets_id],
            capture_output=True,
            check=False,
            timeout=DEFAULT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"dumpcr timed out after {DEFAULT_TIMEOUT}s"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"dumpcr exec failed: {exc}"}

    if proc.returncode != 0:
        return {
            "ok": False,
            "error": f"dumpcr returncode={proc.returncode}",
            "stderr": proc.stderr.decode("utf-8", errors="replace")[:1000],
        }

    raw = proc.stdout.decode("utf-8", errors="replace")
    fields = _parse_dumpcr_output(raw)

    if not fields.get("DTPT-manager", "").strip():
        cbugval = _resolve(DEFAULT_CBUGVAL, "cbugval")
        if cbugval:
            try:
                cb = subprocess.run(
                    [cbugval, "-i", cdets_id, "DTPT-manager"],
                    capture_output=True, check=False, timeout=15,
                )
                if cb.returncode == 0:
                    val = cb.stdout.decode("utf-8", errors="replace").strip()
                    if val:
                        fields["DTPT-manager"] = val
            except Exception as exc:  # noqa: BLE001
                logger.debug("cbugval fallback failed: %s", exc)

    return {"ok": True, "fields": fields, "raw_length": len(raw)}


@tool
def lookup_cdets(cdets_id: str) -> str:
    """Fetch structured CDETS fields via ``dumpcr -d <id>``.

    Returns a JSON string with keys: ``ok`` (bool), ``fields`` (dict of
    CDETS structured fields like Headline, Component, Severity, etc.), and
    ``error`` if the lookup failed.
    """
    return json.dumps(lookup_cdets_impl(cdets_id), default=str)


@tool
def fetch_cdets_field(cdets_id: str, field_name: str) -> str:
    """Query a single CDETS field via ``cbugval -i <id> <field>``.

    Use this for narrow lookups (e.g., DTPT-manager) when you don't need the
    full record.
    """
    cbugval = _resolve(DEFAULT_CBUGVAL, "cbugval")
    if not cbugval:
        return json.dumps({"ok": False, "error": "cbugval not available"})
    try:
        proc = subprocess.run(
            [cbugval, "-i", cdets_id, field_name],
            capture_output=True, check=False, timeout=15,
        )
        if proc.returncode != 0:
            return json.dumps({"ok": False, "returncode": proc.returncode})
        value = proc.stdout.decode("utf-8", errors="replace").strip()
        return json.dumps({"ok": True, "field": field_name, "value": value})
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"ok": False, "error": str(exc)})
