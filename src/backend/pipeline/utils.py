"""Shared utilities used across nodes and tools."""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

CDETS_ID_PATTERN = re.compile(r"^CSC[a-zA-Z]{2}[0-9]{5}$")


def is_valid_cdets_id(value: str) -> bool:
    """Return True if *value* matches the CDETS ID format."""
    return bool(value and CDETS_ID_PATTERN.match(value.strip()))


def utc_now_iso() -> str:
    """ISO-8601 UTC timestamp with 'Z' suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def repo_root() -> Path:
    """Absolute path to the repository root (contains src/, tst/, cdets_data/)."""
    return Path(__file__).resolve().parents[3]


def backend_root() -> Path:
    """Absolute path to the backend package root (``src/backend``).

    Home of the ``config/`` directory and the ``pipeline/`` package; used to
    locate config.yaml, the blueprint CSV/JSON, schema templates, and email
    templates regardless of the current working directory.
    """
    return Path(__file__).resolve().parents[1]


def artifact_dir_for(cdets_id: str, base: Optional[str] = None) -> Path:
    """Return the per-defect artifact directory path."""
    base_path = Path(base) if base else repo_root() / "cdets_data"
    return base_path / cdets_id


def resolve_env_in_value(value):
    """Recursively substitute ${VAR} / ${VAR:-default} in config values."""
    if isinstance(value, str) and "${" in value:
        pattern = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)(?::-([^}]*))?\}")

        def replace(match: re.Match) -> str:
            var_name = match.group(1)
            default = match.group(2) or ""
            return os.getenv(var_name, default)

        return pattern.sub(replace, value)
    if isinstance(value, dict):
        return {k: resolve_env_in_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_env_in_value(v) for v in value]
    return value


def load_config(path: Optional[str] = None) -> dict:
    """Load and env-expand the ``config.yaml`` file."""
    import yaml

    config_path = Path(path) if path else backend_root() / "config" / "config.yaml"
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with config_path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    return resolve_env_in_value(raw)


def stage_trace(
    *,
    status: str,
    duration: float = 0.0,
    iterations: int = 0,
    input_tokens: int = 0,
    output_tokens: int = 0,
    tool_calls: Optional[list] = None,
    error: Optional[str] = None,
) -> dict:
    """Build a stage trace entry for ``state['stage_traces']``."""
    entry = {
        "end_time": utc_now_iso(),
        "duration_seconds": round(duration, 3),
        "status": status,
    }
    if iterations:
        entry["llm_iterations"] = iterations
    if input_tokens or output_tokens:
        entry["input_tokens"] = input_tokens
        entry["output_tokens"] = output_tokens
    if tool_calls is not None:
        entry["tool_calls"] = tool_calls
    if error:
        entry["error"] = error
    return entry
