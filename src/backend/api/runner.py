"""Drive the FL LangGraph pipeline and stream per-node progress.

`run_pipeline_streamed()` is an async task that:
  1. Serves the run from on-disk cache when artifacts already exist
     (unless ``force`` / a per-run model override is requested).
  2. Refreshes the gateway JWT (best-effort).
  3. Builds the LangGraph (with a SQLite checkpointer so a ``human_review``
     interrupt can be persisted) + initial state.
  4. Iterates ``graph.astream(..., stream_mode="updates")``, converts each
     node's ``stage_traces`` entry into a :class:`NodeEvent`, and pushes it to
     the Job's subscribers via the registry.
  5. Marks the Job done / error, or pauses it as ``awaiting_human`` when the
     pipeline interrupts for human-in-the-loop review.

Per-node ``stage_traces`` are produced by every pipeline node (see
``pipeline/utils.py:stage_trace``), so we get status/duration/token usage
for free.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

from .jobs import Job, NodeEvent, registry

logger = logging.getLogger(__name__)

# The src/ root holds the importable packages (backend.*, eval.*); the repo
# root (one level above src/) holds cdets_data/ and .env.
_SRC_ROOT = Path(__file__).resolve().parents[2]
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

_REPO_ROOT = Path(__file__).resolve().parents[3]
_ARTIFACT_ROOT = Path(os.getenv("FL_ARTIFACTS_DIR", str(_REPO_ROOT / "cdets_data")))

# Canonical stage order, used to render a cached run as a completed trace.
_CACHE_STAGES = [
    "common_infra",
    "prescan",
    "rag_fetch_related_cdets",
    "cdets_tz_analyzer",
    "cdets_scoring",
    "cafy_rca_analyzer",
    "existing_test_scanner",
    "testcase_generator",
    "merge_coverage",
    "coverage_comparison",
    "email_report_generator",
    "delivery",
]


def _checkpoint_path_for(cdets_id: str, job_id: str) -> Path:
    """Per-job sqlite checkpoint under the defect's artifact dir."""
    p = _REPO_ROOT / "cdets_data" / cdets_id / "checkpoints"
    p.mkdir(parents=True, exist_ok=True)
    return p / f"{job_id}.sqlite"


def cached_result(cdets_id: str) -> Dict[str, Any] | None:
    """Return a result summary from on-disk artifacts, or None if not analyzed.

    A CDETS counts as "already analyzed" when its Scorecard exists. The summary
    mirrors what :func:`run_pipeline_streamed` builds after a live run, plus a
    ``from_cache`` flag and the stages that have artifacts on disk.
    """
    d = _ARTIFACT_ROOT / cdets_id
    scorecard = d / f"{cdets_id}-Scorecard.md"
    if not scorecard.is_file():
        return None

    def _p(name: str) -> str | None:
        fp = d / name
        return str(fp) if fp.is_file() else None

    present = {
        "cdets_tz_analyzer": _p(f"{cdets_id}_Cdets_Schema_Template.json"),
        "cdets_scoring": _p(f"{cdets_id}-Scorecard.md"),
        "cafy_rca_analyzer": _p(f"{cdets_id}_cafy_rca.json"),
        "testcase_generator": _p(f"AI-FL-{cdets_id}_TestCase.md"),
        "rag_fetch_related_cdets": _p(f"{cdets_id}_related_cdets.json"),
    }
    return {
        "from_cache": True,
        "elapsed_seconds": 0.0,
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "total_tokens": 0,
        "artifact_dir": str(d),
        "scorecard_path": _p(f"{cdets_id}-Scorecard.md"),
        "testcase_path": _p(f"AI-FL-{cdets_id}_TestCase.md"),
        "cdets_schema_path": _p(f"{cdets_id}_Cdets_Schema_Template.json"),
        "union_schema_path": _p(f"{cdets_id}_Union_Schema_Template.json"),
        "_stages_present": present,
    }


def _refresh_token_best_effort() -> None:
    """Refresh AZURE_OPENAI_API_KEY in .env. Swallow all errors."""
    try:
        from scripts.refresh_token import fetch_token, update_env  # noqa: E402

        from dotenv import load_dotenv

        env_path = _REPO_ROOT / ".env"
        load_dotenv(env_path, override=True)
        cid = os.getenv("OAUTH_CLIENT_ID", "")
        csec = os.getenv("OAUTH_CLIENT_SECRET", "")
        turl = os.getenv("OAUTH_TOKEN_URL", "https://your-oauth-provider.example.com/oauth2/default/v1/token")
        if not (cid and csec):
            logger.info("token refresh skipped: client_id / client_secret not set")
            return
        token = fetch_token(cid, csec, turl)
        update_env(env_path, token)
        load_dotenv(env_path, override=True)
        logger.info("token refresh: rotated (ends ...%s)", token[-8:])
    except Exception as exc:  # noqa: BLE001
        logger.warning("token refresh failed: %s", exc)


def _apply_model_override(config: dict, job: Job, model_override: str) -> dict:
    """Resolve a UI alias (sonnet/opus) and apply it to every Bedrock stage.

    Returns a deep-copied config so concurrent jobs are unaffected. The alias
    is resolved from ``llm.bedrock.model_aliases`` and written into every entry
    of ``llm.bedrock.models`` (plus the top-level ``model``) so the whole run
    uses the requested model regardless of which stage-key a node asks for.
    """
    try:
        config = copy.deepcopy(config)
        bedrock = (config.get("llm") or {}).get("bedrock") or {}
        aliases = bedrock.get("model_aliases") or {}
        resolved = aliases.get(model_override.lower())
        if not resolved:
            logger.warning(
                "job %s: unknown model alias %r — using default models",
                job.job_id, model_override,
            )
            return config
        models = config["llm"]["bedrock"].setdefault("models", {})
        for key in list(models.keys()) or ["default"]:
            models[key] = resolved
        models.setdefault("default", resolved)
        config["llm"]["bedrock"]["model"] = resolved
        job.model_override = model_override.lower()
        logger.info(
            "job %s using model_override=%s -> %s (all stages)",
            job.job_id, model_override, resolved,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("job %s: model override failed: %s", job.job_id, exc)
    return config


def _event_from_trace(node: str, trace: Dict[str, Any]) -> NodeEvent:
    raw_status = (trace.get("status") or "ok").lower()
    if raw_status in ("ok", "success", "completed"):
        status = "ok"
    elif raw_status in ("failed", "error"):
        status = "failed"
    elif raw_status in ("skipped", "skip"):
        status = "skipped"
    else:
        status = "ok"
    return NodeEvent(
        node=node,
        status=status,  # type: ignore[arg-type]
        duration=trace.get("duration"),
        input_tokens=trace.get("input_tokens"),
        output_tokens=trace.get("output_tokens"),
        iterations=trace.get("iterations"),
        error=trace.get("error"),
    )


async def run_pipeline_streamed(
    job: Job,
    *,
    dry_run: bool = False,
    force: bool = False,
    model_override: Optional[str] = None,
) -> None:
    """Run the pipeline for ``job.cdets_id``, emitting events as it goes.

    When ``force`` is false, no model override is requested, and the CDETS
    already has on-disk artifacts, the run is served from cache: a completed
    trace is replayed instantly instead of re-invoking the (expensive)
    LangGraph pipeline.

    ``model_override`` is a UI-selectable alias (``"sonnet"`` / ``"opus"``)
    applied to this job only. The graph is compiled with an
    ``AsyncSqliteSaver`` checkpointer keyed by the job's id so a
    ``human_review`` interrupt can be persisted and later resumed by
    :func:`resume_pipeline_job`.
    """
    job.status = "running"
    await registry.push_event(
        job, NodeEvent(node="__start__", status="ok", duration=0.0)
    )

    # 0. Cache hit: replay a completed trace from existing artifacts. A model
    #    override forces a fresh run (the user explicitly asked to re-run).
    if not force and not model_override:
        cached = cached_result(job.cdets_id)
        if cached:
            present = cached.pop("_stages_present", {})
            for stage in _CACHE_STAGES:
                if stage in ("common_infra", "prescan", "merge_coverage",
                             "coverage_comparison", "email_report_generator",
                             "delivery") or present.get(stage):
                    await registry.push_event(
                        job, NodeEvent(node=stage, status="ok", duration=0.0)
                    )
            logger.info("served %s from cache", job.cdets_id)
            await registry.close_job(job, status="done", result=cached)
            return

    # 1. Refresh token (sync, fast) before kicking off the graph.
    await asyncio.to_thread(_refresh_token_best_effort)

    try:
        from backend.pipeline.state import initial_state
        from backend.pipeline.utils import load_config
    except Exception as exc:  # noqa: BLE001
        await registry.close_job(job, status="error", error=f"pipeline import failed: {exc}")
        return

    try:
        config = load_config(None)
    except Exception as exc:  # noqa: BLE001
        await registry.close_job(job, status="error", error=f"config load failed: {exc}")
        return

    # Apply per-job model override (alias → model ID) without mutating shared config.
    if model_override:
        config = _apply_model_override(config, job, model_override)

    state = initial_state(
        job.cdets_id,
        config,
        dry_run=dry_run,
        model_override=(model_override or None),
    )
    # Tag state with job_id so HITL nodes can build a UI link back to this job
    # (and so the score gate knows it is safe to pause for review).
    state["job_id"] = job.job_id

    # Stash everything needed to resume after a human_review interrupt.
    ckpt_path = _checkpoint_path_for(job.cdets_id, job.job_id)
    job.resume_context = {
        "config": config,
        "dry_run": dry_run,
        "model_override": (model_override or None),
        "checkpoint_path": str(ckpt_path),
    }

    await _drive_graph(job, initial_input=state, checkpoint_path=ckpt_path)


async def _drive_graph(
    job: Job,
    *,
    initial_input: Any,
    checkpoint_path: Path,
) -> None:
    """Open the checkpointer, stream the graph, handle interrupts.

    ``initial_input`` is either the initial state dict (first run) or a
    ``Command(resume=...)`` instance (resume after human review).
    """
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    seen_traces: set[str] = set(ev.node for ev in job.nodes)
    final_state: Dict[str, Any] | None = None
    interrupted = False
    interrupt_payload: Dict[str, Any] | None = None

    started = time.monotonic()
    try:
        from backend.pipeline.graph import build_graph  # local import to keep startup snappy

        async with AsyncSqliteSaver.from_conn_string(str(checkpoint_path)) as checkpointer:
            graph = build_graph(checkpointer=checkpointer)
            graph_config = {"configurable": {"thread_id": job.job_id}}

            async for chunk in graph.astream(
                initial_input, graph_config, stream_mode="updates"
            ):
                # `chunk` is {node_name: partial_state_update}. The LangGraph
                # interrupt mechanism shows up as a `__interrupt__` key.
                if "__interrupt__" in chunk:
                    interrupted = True
                    raw = chunk["__interrupt__"]
                    try:
                        first = raw[0] if isinstance(raw, (list, tuple)) else raw
                        interrupt_payload = getattr(first, "value", None) or {}
                    except Exception:  # noqa: BLE001
                        interrupt_payload = {}
                    continue

                for node, update in chunk.items():
                    if node.startswith("__"):
                        continue
                    final_state = update  # last write wins
                    traces = (update or {}).get("stage_traces") or {}
                    for trace_name, trace in traces.items():
                        if trace_name in seen_traces:
                            continue
                        seen_traces.add(trace_name)
                        event = _event_from_trace(trace_name, trace)
                        await registry.push_event(job, event)

        if interrupted:
            payload = interrupt_payload or {}
            mir = payload.get("missing_info_request")
            review_url = (
                (job.resume_context or {}).get("review_url")
                or _infer_review_url(job, payload)
            )
            await registry.push_event(
                job,
                NodeEvent(
                    node="__awaiting_human__",
                    status="awaiting_human",
                    duration=round(time.monotonic() - started, 2),
                ),
            )
            await registry.pause_for_human(
                job,
                missing_info_request=mir,
                review_url=review_url,
            )
            logger.info(
                "job %s paused for human review (cdets=%s)", job.job_id, job.cdets_id
            )
            return

        elapsed = time.monotonic() - started
        total_in = sum(int(n.input_tokens or 0) for n in job.nodes)
        total_out = sum(int(n.output_tokens or 0) for n in job.nodes)
        result_summary: Dict[str, Any] = {
            "elapsed_seconds": round(elapsed, 2),
            "total_input_tokens": total_in,
            "total_output_tokens": total_out,
            "total_tokens": total_in + total_out,
            "artifact_dir": (final_state or {}).get("artifact_dir"),
            "scorecard_path": (final_state or {}).get("scorecard_path"),
            "testcase_path": (final_state or {}).get("testcase_path"),
            "cdets_schema_path": (final_state or {}).get("cdets_schema_path"),
            "union_schema_path": (final_state or {}).get("union_schema_path"),
            "cdet_ai_score": (final_state or {}).get("cdet_ai_score"),
            "automation_readiness": (final_state or {}).get("automation_readiness"),
            "test_coverage_grade": (final_state or {}).get("test_coverage_grade"),
            "delivery_status": (final_state or {}).get("delivery_status"),
        }
        await registry.close_job(job, status="done", result=result_summary)
    except Exception as exc:  # noqa: BLE001
        logger.exception("pipeline crashed for %s", job.cdets_id)
        await registry.push_event(
            job, NodeEvent(node="__error__", status="failed", error=str(exc))
        )
        await registry.close_job(job, status="error", error=str(exc))


def _infer_review_url(job: Job, payload: Dict[str, Any]) -> Optional[str]:
    """Best-effort review URL derivation from config (fallback)."""
    try:
        from backend.pipeline.utils import load_config

        cfg = (load_config(None).get("human_in_loop") or {})
        base = str(cfg.get("review_url_base") or "").rstrip("/")
        if not base:
            return None
        return f"{base}/?review={job.job_id}&cdets={job.cdets_id}"
    except Exception:  # noqa: BLE001
        return None


def start_pipeline_job(
    cdets_id: str,
    *,
    dry_run: bool = False,
    force: bool = False,
    model_override: Optional[str] = None,
) -> Job:
    """Create a Job, schedule its pipeline task, and return immediately."""
    job = registry.create(cdets_id)
    if model_override:
        job.model_override = model_override.lower()
    loop = asyncio.get_event_loop()
    job._task = loop.create_task(
        run_pipeline_streamed(
            job, dry_run=dry_run, force=force, model_override=model_override
        )
    )
    return job


async def resume_pipeline_job(job_id: str, human_input: Dict[str, Any]) -> Job:
    """Resume a paused job with the reviewer's answers.

    Re-opens the same SQLite checkpointer + ``thread_id`` and feeds a
    :class:`langgraph.types.Command` with ``resume=human_input`` to continue
    execution from the ``human_review`` node.
    """
    from langgraph.types import Command

    job = registry.get(job_id)
    if job is None:
        raise KeyError(f"unknown job_id {job_id!r}")
    if job.status != "awaiting_human":
        raise RuntimeError(f"job {job_id} is not awaiting human input (status={job.status})")
    ctx = job.resume_context or {}
    ckpt = ctx.get("checkpoint_path")
    if not ckpt:
        raise RuntimeError(f"job {job_id} has no checkpoint to resume from")

    job.status = "running"
    job.finished_at = None
    await registry.push_event(
        job,
        NodeEvent(node="__resume__", status="ok", duration=0.0),
    )

    loop = asyncio.get_event_loop()
    job._task = loop.create_task(
        _drive_graph(
            job,
            initial_input=Command(resume=human_input),
            checkpoint_path=Path(ckpt),
        )
    )
    return job
