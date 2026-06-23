"""Filesystem tools — safe, sandboxed read/write within ``cdets_data/``.

All write operations require the path to be inside the configured artifact
base directory. Reads are unrestricted but logged.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from langchain_core.tools import tool

from ..utils import repo_root

logger = logging.getLogger(__name__)


def _ensure_under_artifact_dir(path: str) -> Path:
    """Resolve *path* and require that it lives under ``<repo>/cdets_data/``."""
    target = Path(path).expanduser().resolve()
    base = (repo_root() / "cdets_data").resolve()
    base.mkdir(parents=True, exist_ok=True)
    try:
        target.relative_to(base)
    except ValueError as exc:
        raise PermissionError(
            f"Refusing to write outside artifact dir. base={base} target={target}"
        ) from exc
    return target


@tool
def read_file_text(path: str, max_bytes: int = 200_000) -> str:
    """Read a UTF-8 text file and return its contents (truncated to *max_bytes*).

    Use this to inspect existing artifacts, blueprints, or test code.
    """
    p = Path(path).expanduser().resolve()
    if not p.exists():
        return f"ERROR: file not found: {p}"
    if not p.is_file():
        return f"ERROR: not a file: {p}"
    data = p.read_bytes()[:max_bytes]
    return data.decode("utf-8", errors="replace")


@tool
def write_artifact_text(relative_path: str, content: str) -> str:
    """Write *content* to ``cdets_data/<relative_path>`` (creates dirs).

    Returns the absolute path written. Refuses paths outside ``cdets_data/``.
    """
    target = _ensure_under_artifact_dir(str(repo_root() / "cdets_data" / relative_path))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    logger.info("Wrote artifact %s (%d bytes)", target, len(content))
    return str(target)


@tool
def write_artifact_json(relative_path: str, data_json: str) -> str:
    """Write a JSON document to ``cdets_data/<relative_path>``.

    *data_json* is the JSON string the LLM produced (pretty-printed on disk).
    """
    try:
        parsed = json.loads(data_json)
    except json.JSONDecodeError as exc:
        return f"ERROR: invalid JSON: {exc}"
    target = _ensure_under_artifact_dir(str(repo_root() / "cdets_data" / relative_path))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(parsed, indent=2, default=str), encoding="utf-8")
    logger.info("Wrote JSON artifact %s", target)
    return str(target)


@tool
def list_directory(path: str, pattern: str = "*") -> str:
    """List entries under *path* matching glob *pattern*.

    Returns one entry per line. Useful for blueprint and existing-test discovery.
    """
    p = Path(path).expanduser().resolve()
    if not p.exists() or not p.is_dir():
        return f"ERROR: directory not found: {p}"
    entries = sorted(str(child.relative_to(p)) for child in p.glob(pattern))
    return "\n".join(entries) if entries else "(empty)"


@tool
def file_exists(path: str) -> bool:
    """Return True if *path* exists on the filesystem."""
    return Path(path).expanduser().exists()
