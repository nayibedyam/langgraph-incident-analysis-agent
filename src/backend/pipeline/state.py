"""Shared state contract for the FL LangGraph pipeline.

Every node receives the full :class:`FLAgentState` and returns a partial
dict with only the fields it owns. LangGraph merges that partial update
back into the state before the next node runs.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, List, Optional, TypedDict


class FLAgentState(TypedDict, total=False):
    # ─── Input (set once at invocation) ───
    cdets_id: str
    invocation_mode: str
    config: dict
    dry_run: bool
    job_id: Optional[str]   # Set by the FastAPI runner so HITL nodes can build review URLs.
    model_override: Optional[str]   # UI-selected model alias (sonnet/opus) for this run.
    model_used: Optional[str]

    # ─── Stage 00: common_infra ───
    artifact_dir: str
    init_valid: bool
    error: Optional[str]

    # ─── Prescan (pre-LLM enrichment) ───
    cdets_fields: dict
    component: str
    primary_ap: str
    primary_subap: str
    blueprint_dir: Optional[str]
    topology: str
    version: str
    severity: str
    dtpt_manager: str
    prescan_coverage: Optional[dict]

    # ─── RAG: related-CDETS retrieval (runs next to prescan) ───
    related_cdets: list
    related_cdets_path: Optional[str]
    rag_top_score: float
    rag_short_circuit: bool

    # ─── Stage 01: cdets_tz_analyzer ───
    cdets_schema_path: Optional[str]
    tz_schema_path: Optional[str]
    union_schema_path: Optional[str]
    cdets_lookup_ok: bool
    has_techzone: bool
    schema_data: dict

    # ─── Stage 02: cdets_scoring ───
    scorecard_path: Optional[str]
    cdet_ai_score: float
    ai_confidence: float
    automation_readiness: str
    quality_blockers: list

    # ─── Stage 02b: missing_info_request + human_review (HITL) ───
    needs_human_review: bool
    missing_info_request: Optional[dict]
    missing_info_request_path: Optional[str]
    human_review_email_sent: bool
    review_url: Optional[str]
    human_input: Optional[dict]
    human_review_count: int

    # ─── Stage 03: cafy_rca_analyzer ───
    cafy_rca_json_path: Optional[str]
    cafy_rca_md_path: Optional[str]
    automation_mapping: Optional[dict]
    genc_handoff: Optional[dict]
    coverage_gap: str
    gap_classification: str
    cafy_coverage_verdict: str

    # ─── Stage 04a: testcase_generator (fan-out branch 1) ───
    testcase_path: Optional[str]
    test_scenarios: list

    # ─── Stage 04b: existing_test_scanner (fan-out branch 2) ───
    existing_tests: list
    existing_verifiers: list
    existing_helpers: list
    test_file_map: dict

    # ─── Stage 04c: merge_coverage (join point) ───
    merged_coverage_input: dict

    # ─── Stage 05: coverage_comparison ───
    test_coverage_confidence: float
    test_coverage_grade: str
    coverage_classification: str

    # ─── Stage 06: email_report_generator ───
    email_payload: Optional[dict]
    email_subject: str
    attachment_paths: list

    # ─── Stage 07: delivery ───
    mongo_pushed: bool
    tftp_delivered: bool
    email_sent: bool
    cdets_attached: bool
    delivery_status: str

    # ─── Pipeline metadata ───
    stage_traces: Annotated[dict, lambda a, b: {**a, **b}]
    messages: Annotated[List[Any], operator.add]


def initial_state(
    cdets_id: str,
    config: dict,
    *,
    invocation_mode: str = "LANGGRAPH",
    dry_run: bool = False,
    model_override: Optional[str] = None,
) -> FLAgentState:
    """Build a fresh initial state for a single pipeline invocation."""
    return {
        "cdets_id": cdets_id,
        "invocation_mode": invocation_mode,
        "config": config,
        "dry_run": dry_run,
        "init_valid": False,
        "cdets_lookup_ok": False,
        "has_techzone": False,
        "model_override": model_override,
        "model_used": model_override,
        "needs_human_review": False,
        "human_review_email_sent": False,
        "human_review_count": 0,
        "stage_traces": {},
        "messages": [],
    }
