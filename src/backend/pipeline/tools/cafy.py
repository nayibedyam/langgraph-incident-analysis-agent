"""CaFy AP test discovery tools.

Production FL agent runs ``analyze_cafy_coverage.py`` against a CaFy AP root
to enumerate existing test cases for the relevant component/AP. We expose
two tools to the existing-test-scanner agent:

1. ``scan_cafy_tests`` — list test files under an AP root.
2. ``grep_cafy_tests`` — search for keywords across those files.

When the CaFy AP root is unavailable (dev laptop), tools degrade gracefully.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from langchain_core.tools import tool

logger = logging.getLogger(__name__)


def _cafy_root() -> Optional[Path]:
    val = (os.getenv("FL_CAFY_AP_ROOT") or "").strip()
    if val:
        p = Path(val).expanduser()
        if p.is_dir():
            return p
    default = Path("/path/to/cafy/work-dir")
    return default if default.is_dir() else None


@tool
def scan_cafy_tests(ap: str, subap: str = "", max_files: int = 200) -> str:
    """List CaFy test files for a given AP / optional SubAP.

    Returns a JSON list of relative file paths under the CaFy AP root.
    """
    root = _cafy_root()
    if not root:
        return json.dumps({"ok": False, "error": "FL_CAFY_AP_ROOT not available", "files": []})

    ap_token = (ap or "").strip().lower()
    subap_token = (subap or "").strip().lower()
    matches: List[str] = []
    for path in root.rglob("*.py"):
        rel = str(path.relative_to(root)).lower()
        if ap_token and ap_token not in rel:
            continue
        if subap_token and subap_token not in rel:
            continue
        matches.append(str(path.relative_to(root)))
        if len(matches) >= max_files:
            break
    return json.dumps({"ok": True, "ap_root": str(root), "files": matches, "count": len(matches)})


@tool
def grep_cafy_tests(pattern: str, ap: str = "", max_hits: int = 50) -> str:
    """Search CaFy test files for *pattern* (regex). Returns hits as JSON."""
    root = _cafy_root()
    if not root:
        return json.dumps({"ok": False, "error": "FL_CAFY_AP_ROOT not available", "hits": []})

    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        return json.dumps({"ok": False, "error": f"invalid regex: {exc}"})

    ap_token = (ap or "").strip().lower()
    hits: List[Dict[str, Any]] = []
    for path in root.rglob("*.py"):
        rel = str(path.relative_to(root))
        if ap_token and ap_token not in rel.lower():
            continue
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                for lineno, line in enumerate(fh, start=1):
                    if regex.search(line):
                        hits.append({"file": rel, "line": lineno, "text": line.rstrip()[:200]})
                        if len(hits) >= max_hits:
                            return json.dumps({"ok": True, "hits": hits, "truncated": True})
        except (OSError, UnicodeDecodeError):
            continue
    return json.dumps({"ok": True, "hits": hits, "truncated": False})
