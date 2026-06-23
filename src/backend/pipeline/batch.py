"""Async batch runner — process N CDETS IDs in parallel with rate limiting."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Iterable, List

from .graph import build_graph
from .state import initial_state

logger = logging.getLogger(__name__)


async def _run_one(
    graph,
    cdets_id: str,
    config: Dict[str, Any],
    dry_run: bool,
    semaphore: asyncio.Semaphore,
) -> Dict[str, Any]:
    async with semaphore:
        logger.info("Starting pipeline for %s", cdets_id)
        state = initial_state(cdets_id, config, dry_run=dry_run)
        try:
            result = await graph.ainvoke(state)
            logger.info(
                "Finished %s: status=%s",
                cdets_id,
                result.get("delivery_status", "unknown"),
            )
            return {"cdets_id": cdets_id, "ok": True, "result": result}
        except Exception as exc:  # noqa: BLE001
            logger.exception("Pipeline crashed for %s", cdets_id)
            return {"cdets_id": cdets_id, "ok": False, "error": str(exc)}


async def run_batch(
    cdets_ids: Iterable[str],
    config: Dict[str, Any],
    *,
    parallel: int = 1,
    dry_run: bool = False,
) -> List[Dict[str, Any]]:
    """Run the pipeline for *cdets_ids* concurrently with a worker cap."""
    ids = [c.strip() for c in cdets_ids if c and c.strip()]
    if not ids:
        return []

    graph = build_graph()
    sem = asyncio.Semaphore(max(1, int(parallel)))
    tasks = [_run_one(graph, c, config, dry_run, sem) for c in ids]
    return await asyncio.gather(*tasks)
