"""Stage 00 — common_infra.

Validates the CDETS ID format, prepares the per-defect artifact directory,
and emits a stage trace. This is the only node that runs unconditionally
before every other stage.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Dict

from ..state import FLAgentState
from ..utils import (
    artifact_dir_for,
    is_valid_cdets_id,
    repo_root,
    stage_trace,
    utc_now_iso,
)

logger = logging.getLogger(__name__)


async def common_infra_node(state: FLAgentState) -> Dict[str, Any]:
    """Initialize artifact directory and validate the input ID."""
    started = time.monotonic()
    cdets_id = (state.get("cdets_id") or "").strip()

    if not is_valid_cdets_id(cdets_id):
        return {
            "init_valid": False,
            "error": f"Invalid CDETS ID format: {cdets_id!r}. Expected CSCxxNNNNN.",
            "stage_traces": {
                "common_infra": stage_trace(
                    status="failed",
                    duration=time.monotonic() - started,
                    error="invalid_cdets_id",
                ),
            },
        }

    base = state.get("config", {}).get("paths", {}).get("artifact_base", "cdets_data")
    base_path = Path(base)
    if not base_path.is_absolute():
        base_path = repo_root() / base_path
    artifact_dir = artifact_dir_for(cdets_id, base=str(base_path))
    artifact_dir.mkdir(parents=True, exist_ok=True)

    debug_dir = artifact_dir / "debug"
    debug_dir.mkdir(exist_ok=True)

    logger.info("common_infra: ready cdets=%s dir=%s", cdets_id, artifact_dir)

    return {
        "cdets_id": cdets_id,
        "init_valid": True,
        "artifact_dir": str(artifact_dir),
        "stage_traces": {
            "common_infra": stage_trace(
                status="ok",
                duration=time.monotonic() - started,
            )
            | {"start_time": utc_now_iso(), "artifact_dir": str(artifact_dir)},
        },
    }
