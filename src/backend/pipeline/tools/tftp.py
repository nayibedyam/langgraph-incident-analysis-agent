"""TFTP / shared filesystem delivery for FL Agent artifacts.

The legacy IDE workflow copies generated artifacts to a shared TFTP root
(``/path/to/cdets_feedback/<CDETS-ID>/``) so they're reachable
from lab gear and the dashboard.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Dict, Iterable, List

logger = logging.getLogger(__name__)


def push_to_tftp(
    *,
    cdets_id: str,
    artifact_paths: Iterable[str],
    tftp_root: str,
    dry_run: bool = False,
) -> Dict[str, object]:
    """Copy *artifact_paths* under ``<tftp_root>/<cdets_id>/``.

    Returns a dict with ``ok``, ``destination``, and ``copied`` (list of
    files actually copied).
    """
    target_dir = Path(tftp_root).expanduser() / cdets_id
    copied: List[str] = []
    skipped: List[str] = []

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "destination": str(target_dir),
            "would_copy": [str(p) for p in artifact_paths if p],
        }

    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except (OSError, PermissionError) as exc:
        return {"ok": False, "error": f"mkdir failed: {exc}", "destination": str(target_dir)}

    for src in artifact_paths:
        if not src:
            continue
        src_path = Path(src)
        if not src_path.exists():
            skipped.append(src)
            continue
        try:
            shutil.copy2(src_path, target_dir / src_path.name)
            copied.append(str(target_dir / src_path.name))
        except (OSError, PermissionError) as exc:
            logger.warning("TFTP copy failed for %s: %s", src, exc)
            skipped.append(src)

    return {
        "ok": bool(copied),
        "destination": str(target_dir),
        "copied": copied,
        "skipped": skipped,
    }
