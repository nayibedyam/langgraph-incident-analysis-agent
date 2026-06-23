"""AP / Blueprint resolution tools.

The production FL agent ships a CSV of Component → AP mappings and a JSON
index of AP → SubAPs. We reuse the same data files so behavior matches the
existing skill set.
"""

from __future__ import annotations

import csv
import json
import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

from langchain_core.tools import tool

from ..utils import backend_root

logger = logging.getLogger(__name__)


# Same normalization map as production prescan.py
_AP_TO_BLUEPRINT_DIR = {
    "aaa": "aaa", "bfd": "bfd", "bng": "bng", "bundles": "bundlemgr",
    "counters": "counters", "evpn": "evpn", "flow monitor": "flow_monitor",
    "forwarding": "forwarding", "forwarding foundation": "forwarding",
    "gre": "gre", "install": "install", "ip infra": "ip_infra",
    "ipsla": "ipsla", "l2": "l2", "l2vpn": "l2vpn", "l3vpn": "l3vpn",
    "load balancing": "load_balancing", "lpts punt-inject": "lpts",
    "lpts, span": "lpts", "manageability": "mgbl", "mpls": "mpls",
    "mpls-te": "mpls_te", "multicast": "multicast",
    "optical platform mxp": "optical_platform_mxp",
    "optical platform ols": "optical_platform_ols",
    "optical_platform": "optical_platform", "pd - routing igp": "routing",
    "platform": "platform", "platform - sf": "platform", "pm": "pm",
    "qos": "qos", "security": "security", "slapi": "slapi",
    "smart license": "smart_licensing", "sr": "sr", "srv6": "srv6",
    "tcam": "tcam", "timing": "timing", "xr sw infra": "pi_infra",
    "routing": "routing", "routing_bgp": "routing_bgp",
    "macsec": "security", "pd - routing bgp": "routing_bgp",
}

_NORMALIZE_AP = {
    "platform - sf": "Platform",
    "platform - sf (needs review)": "Platform",
    "platform - 9k/dnx": "Platform",
}


def _ap_csv_path() -> Path:
    return backend_root() / "config" / "comp_ap_dtpt_mgr_mapping.csv"


def _ap_index_path() -> Path:
    return backend_root() / "config" / "ap_subap_index.json"


def _blueprint_root() -> Optional[Path]:
    candidates: List[Path] = []
    for env_name in (
        "FL_BLUEPRINT_ROOT",
        "AUTOMATION_BLUEPRINT_ROOT",
        "GENC_BLUEPRINT_ROOT",
        "COVERAGE_ORACLE_BLUEPRINT_ROOT",
    ):
        val = (os.getenv(env_name) or "").strip()
        if val:
            candidates.append(Path(val).expanduser())
    candidates.append(Path("/path/to/blueprints"))
    for c in candidates:
        if c.is_dir():
            return c
    return None


@lru_cache(maxsize=1)
def _load_csv() -> List[Dict[str, str]]:
    path = _ap_csv_path()
    if not path.exists():
        logger.warning("AP CSV not found at %s — AP resolution will return empty", path)
        return []
    with path.open("r", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def resolve_ap_impl(component: str) -> Dict[str, Any]:
    """Return AP/DTPT data for *component* (pure Python)."""
    component = (component or "").strip()
    if not component:
        return {"ap": "", "dtpt_manager": "", "blueprint_dir": "", "blueprint_path": ""}

    normalized = component.lower()
    rows = _load_csv()
    ap_name = ""
    dtpt = ""
    for row in rows:
        name = (row.get("Component", "") or row.get("Name", "")).strip().lower()
        if name == normalized:
            ap_name = (row.get("AP Name", "") or "").strip()
            dtpt = (row.get("DTPT Manager", "") or row.get("DTPT-manager", "") or "").strip()
            break

    ap_name = _NORMALIZE_AP.get(ap_name.lower(), ap_name)
    blueprint_dir = _AP_TO_BLUEPRINT_DIR.get(ap_name.lower(), "") if ap_name else ""

    blueprint_path = ""
    bp_root = _blueprint_root()
    if bp_root and blueprint_dir:
        candidate = bp_root / blueprint_dir / f"{blueprint_dir}_metadata.md"
        if candidate.exists():
            blueprint_path = str(candidate)

    return {
        "ap": ap_name,
        "dtpt_manager": dtpt,
        "blueprint_dir": blueprint_dir,
        "blueprint_path": blueprint_path,
    }


@tool
def resolve_ap_for_component(component: str) -> str:
    """Resolve a Component name to its AP, DTPT manager, and blueprint path.

    Returns a JSON string with keys: ``ap``, ``dtpt_manager``, ``blueprint_dir``,
    ``blueprint_path`` (absolute path to the AP metadata blueprint, or empty).
    """
    return json.dumps(resolve_ap_impl(component))


@tool
def list_subaps(ap: str) -> str:
    """List known SubAPs for an AP using the local AP/SubAP index.

    Returns a JSON list of SubAP names. Empty list if the index isn't available
    or the AP isn't found.
    """
    path = _ap_index_path()
    if not path.exists():
        return json.dumps([])
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": str(exc), "subaps": []})

    ap_norm = (ap or "").strip().lower()
    for key, val in data.items():
        if key.lower() == ap_norm:
            if isinstance(val, list):
                return json.dumps(val)
            if isinstance(val, dict):
                return json.dumps(list(val.keys()))
    return json.dumps([])


@tool
def read_blueprint(blueprint_path: str, max_bytes: int = 200_000) -> str:
    """Read an AP/SubAP blueprint markdown file.

    Use this to extract topology, supported features, and existing test
    references from the GenC-owned blueprint catalog.
    """
    p = Path(blueprint_path).expanduser()
    if not p.exists() or not p.is_file():
        return f"ERROR: blueprint not found: {p}"
    return p.read_bytes()[:max_bytes].decode("utf-8", errors="replace")
