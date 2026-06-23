"""In-memory job registry for live pipeline progress.

Each call to :func:`start_job` allocates a :class:`Job`, launches the
LangGraph pipeline in an asyncio task, and returns immediately with a
``job_id``. Per-node progress events are pushed to per-subscriber
queues so SSE handlers (or any other consumer) can fan them out.

This is a single-process, single-worker store. If the FastAPI server is
ever run with multiple workers, swap the dict for Redis/SQLite.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Dict, List, Literal, Optional

logger = logging.getLogger(__name__)

JobStatus = Literal["queued", "running", "done", "error", "awaiting_human"]
NodeStatus = Literal["running", "ok", "failed", "skipped", "awaiting_human"]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class NodeEvent:
    """One progress event emitted as a pipeline node finishes."""

    node: str
    status: NodeStatus
    duration: Optional[float] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    iterations: Optional[int] = None
    error: Optional[str] = None
    ts: str = field(default_factory=_utc_now)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "node": self.node,
            "status": self.status,
            "duration": self.duration,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "iterations": self.iterations,
            "error": self.error,
            "ts": self.ts,
        }


@dataclass
class Job:
    job_id: str
    cdets_id: str
    status: JobStatus = "queued"
    model_override: Optional[str] = None
    nodes: List[NodeEvent] = field(default_factory=list)
    started_at: str = field(default_factory=_utc_now)
    finished_at: Optional[str] = None
    error: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    # ─── Human-in-the-loop state (populated when pipeline emits interrupt) ───
    missing_info_request: Optional[Dict[str, Any]] = None
    review_url: Optional[str] = None
    # Snapshot of args needed to resume the graph from its checkpoint.
    resume_context: Optional[Dict[str, Any]] = field(default=None, repr=False)
    # Subscribers receive every NodeEvent + a terminal sentinel ``None``.
    _subscribers: List[asyncio.Queue] = field(default_factory=list, repr=False)
    _task: Optional[asyncio.Task] = field(default=None, repr=False)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "cdets_id": self.cdets_id,
            "status": self.status,
            "model_override": self.model_override,
            "nodes": [n.to_dict() for n in self.nodes],
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "result": self.result,
            "missing_info_request": self.missing_info_request,
            "review_url": self.review_url,
        }


class JobRegistry:
    """Thread-safe-ish (single event-loop) registry of running jobs."""

    def __init__(self, max_jobs: int = 100) -> None:
        self._jobs: Dict[str, Job] = {}
        self._max_jobs = max_jobs
        self._lock = asyncio.Lock()

    def create(self, cdets_id: str) -> Job:
        job_id = uuid.uuid4().hex[:12]
        job = Job(job_id=job_id, cdets_id=cdets_id)
        self._jobs[job_id] = job
        if len(self._jobs) > self._max_jobs:
            # Evict the oldest finished job. Never evict a running one.
            for jid, j in list(self._jobs.items()):
                if j.status in ("done", "error"):
                    self._jobs.pop(jid, None)
                    break
        return job

    def get(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    def all(self) -> List[Job]:
        return list(self._jobs.values())

    async def push_event(self, job: Job, event: NodeEvent) -> None:
        """Record an event on the job and fan-out to all SSE subscribers."""
        job.nodes.append(event)
        dead: List[asyncio.Queue] = []
        for q in job._subscribers:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            job._subscribers.remove(q)

    async def close_job(
        self,
        job: Job,
        *,
        status: JobStatus,
        error: Optional[str] = None,
        result: Optional[Dict[str, Any]] = None,
    ) -> None:
        job.status = status
        job.error = error
        job.result = result
        job.finished_at = _utc_now()
        # Send terminal sentinel to subscribers so SSE generators exit.
        for q in job._subscribers:
            try:
                q.put_nowait(None)
            except asyncio.QueueFull:
                pass

    async def pause_for_human(
        self,
        job: Job,
        *,
        missing_info_request: Optional[Dict[str, Any]] = None,
        review_url: Optional[str] = None,
    ) -> None:
        """Flip the job to ``awaiting_human`` and close current SSE streams.

        The job is NOT terminal — a later call to ``resume_pipeline_job``
        will create a fresh task and clients can re-subscribe.
        """
        job.status = "awaiting_human"
        if missing_info_request is not None:
            job.missing_info_request = missing_info_request
        if review_url is not None:
            job.review_url = review_url
        for q in job._subscribers:
            try:
                q.put_nowait(None)
            except asyncio.QueueFull:
                pass
        job._subscribers.clear()

    async def subscribe(self, job: Job) -> AsyncIterator[NodeEvent]:
        """Yield every NodeEvent until the job ends.

        Replays already-emitted events before tailing live ones, so a
        client that connects late still sees full history.
        """
        queue: asyncio.Queue = asyncio.Queue(maxsize=256)
        # Replay history first.
        for ev in list(job.nodes):
            queue.put_nowait(ev)
        if job.status in ("done", "error", "awaiting_human"):
            yield_terminal = True
        else:
            yield_terminal = False
            job._subscribers.append(queue)
        try:
            while True:
                if yield_terminal and queue.empty():
                    return
                item = await queue.get()
                if item is None:
                    return
                yield item
        finally:
            if queue in job._subscribers:
                job._subscribers.remove(queue)


# Singleton registry used by server.py
registry = JobRegistry()
