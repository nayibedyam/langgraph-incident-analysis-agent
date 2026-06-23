"""Pre-LLM enrichment: dumpcr lookup + AP resolution + version normalization.

Run before the cdets_tz_analyzer LLM agent so:
  1. The pipeline aborts early on CDETS lookup failure (cheap, no tokens).
  2. The LLM gets pre-resolved component → AP → blueprint context (saves tokens).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict

from .tools.blueprints import resolve_ap_impl
from .tools.cdets import lookup_cdets_impl

logger = logging.getLogger(__name__)


def _normalize_version(raw: str) -> str:
    """Pick the lowest version-like token, return canonical N.M.P form."""
    if not raw:
        return ""
    cleaned = re.sub(r"[\[\]\"'`]", " ", str(raw))
    tokens = re.findall(
        r"(?:SDK[-_])?\d+(?:\.\d+){1,3}(?:[A-Za-z]\w*)?(?:\.BASE)?",
        cleaned,
        flags=re.IGNORECASE,
    )
    if not tokens:
        return ""

    def sort_key(tok: str):
        tok = re.sub(r"^SDK[-_]", "", tok.strip(), flags=re.IGNORECASE)
        parts = []
        for chunk in tok.split("."):
            m = re.match(r"^(\d+)([A-Za-z]\w*)?$", chunk)
            if m:
                parts.append((int(m.group(1)), m.group(2) or ""))
        return parts

    tokens.sort(key=sort_key)
    chosen = re.sub(r"^SDK[-_]", "", tokens[0], flags=re.IGNORECASE).split(".")
    nums = []
    for chunk in chosen[:3]:
        m = re.match(r"^(\d+)", chunk)
        if m:
            nums.append(int(m.group(1)))
    while len(nums) < 3:
        nums.append(0)
    return ".".join(str(n) for n in nums[:3])


def run_prescan(cdets_id: str) -> Dict[str, Any]:
    """Execute the prescan and return a dict ready to merge into the state.

    Always returns a dict with the same keys; ``cdets_lookup_ok`` indicates
    whether the CDETS query succeeded.
    """
    result: Dict[str, Any] = {
        "cdets_lookup_ok": False,
        "cdets_fields": {},
        "component": "",
        "primary_ap": "",
        "primary_subap": "",
        "blueprint_dir": None,
        "version": "",
        "severity": "",
        "dtpt_manager": "",
        "topology": "",
    }

    lookup = lookup_cdets_impl(cdets_id)
    if not lookup.get("ok"):
        result["error"] = lookup.get("error", "CDETS lookup failed")
        return result

    fields = lookup.get("fields", {}) or {}
    result["cdets_fields"] = fields
    result["cdets_lookup_ok"] = True

    component = (fields.get("Component") or "").strip()
    result["component"] = component

    ap_data = resolve_ap_impl(component) if component else {}
    result["primary_ap"] = ap_data.get("ap", "")
    result["primary_subap"] = ""
    result["blueprint_dir"] = ap_data.get("blueprint_path") or None

    if not result["dtpt_manager"]:
        result["dtpt_manager"] = ap_data.get("dtpt_manager", "") or fields.get("DTPT-manager", "")

    result["version"] = _normalize_version(fields.get("Version", "") or fields.get("Found-in", ""))
    result["severity"] = (fields.get("Severity") or fields.get("Sev") or "").strip()
    result["topology"] = (fields.get("Test-config") or "").strip()

    return result
