"""Stage 04b — existing_test_scanner (pure Python, fan-out branch 2).

Independent of testcase_generator: walks the configured CaFy AP root and
collects file paths, helper/verifier names, and a coarse file-map keyed
by feature directory. The output feeds the merge_coverage join node.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List

from ..state import FLAgentState
from ..utils import stage_trace, utc_now_iso

logger = logging.getLogger(__name__)

_HELPER_PATTERNS = (
    re.compile(r"^def\s+(test_[A-Za-z0-9_]+)\b"),
    re.compile(r"^def\s+(verify_[A-Za-z0-9_]+)\b"),
    re.compile(r"^def\s+(check_[A-Za-z0-9_]+)\b"),
)


def _cafy_root() -> Path | None:
    val = (os.getenv("FL_CAFY_AP_ROOT") or "").strip()
    if val:
        p = Path(val).expanduser()
        if p.is_dir():
            return p
    default = Path("/path/to/cafy/work-dir")
    return default if default.is_dir() else None


async def existing_test_scanner_node(state: FLAgentState) -> Dict[str, Any]:
    started = time.monotonic()
    ap = (state.get("primary_ap") or "").strip().lower()
    subap = (state.get("primary_subap") or "").strip().lower()
    root = _cafy_root()

    existing_tests: List[str] = []
    existing_verifiers: List[str] = []
    existing_helpers: List[str] = []
    file_map: Dict[str, List[str]] = defaultdict(list)
    error: str | None = None

    if root is None:
        error = "FL_CAFY_AP_ROOT not available — skipping existing test scan"
        logger.info("existing_test_scanner: %s", error)
    else:
        max_files = 400
        scanned = 0
        for path in root.rglob("*.py"):
            rel = str(path.relative_to(root))
            rel_lower = rel.lower()
            if ap and ap not in rel_lower:
                continue
            if subap and subap not in rel_lower:
                continue

            existing_tests.append(rel)
            top = rel.split(os.sep, 1)[0]
            file_map[top].append(rel)

            try:
                with path.open("r", encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        for pat in _HELPER_PATTERNS:
                            m = pat.match(line)
                            if not m:
                                continue
                            name = m.group(1)
                            if name.startswith("test_"):
                                existing_helpers.append(f"{rel}::{name}")
                            elif name.startswith("verify_") or name.startswith("check_"):
                                existing_verifiers.append(f"{rel}::{name}")
            except (OSError, UnicodeDecodeError):
                continue

            scanned += 1
            if scanned >= max_files:
                break

    duration = time.monotonic() - started
    logger.info(
        "existing_test_scanner: ap=%s tests=%d verifiers=%d helpers=%d duration=%.2fs",
        ap, len(existing_tests), len(existing_verifiers), len(existing_helpers), duration,
    )

    return {
        "existing_tests": existing_tests,
        "existing_verifiers": existing_verifiers,
        "existing_helpers": existing_helpers,
        "test_file_map": dict(file_map),
        "stage_traces": {
            "existing_test_scanner": stage_trace(
                status="ok" if not error else "skipped",
                duration=duration,
                error=error,
            )
            | {
                "start_time": utc_now_iso(),
                "tests_found": len(existing_tests),
                "ap_root": str(root) if root else None,
            },
        },
    }
