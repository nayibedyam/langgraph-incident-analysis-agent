"""LangGraph pipeline wiring for the FL Agent.

The graph implements:

```
common_infra → prescan → rag_fetch_related_cdets
                              │  (high-confidence duplicate? → delivery)
                              ▼
                       cdets_tz_analyzer → cdets_scoring
                                                │
                  ┌──── low score + HITL enabled ┴── normal ────┐
                  ▼                                             ▼
        missing_info_request                            cafy_rca_analyzer
                  │                                             │
            human_review (interrupt)            ┌──── testcase_generator ──┐
                  │                              │                          ▼
          merge_human_input ─► cdets_tz_analyzer└─ existing_test_scanner ─► merge_coverage
                                                                            │
                                                                            ▼
                                                                coverage_comparison
                                                                            │
                                                                            ▼
                                                           email_report_generator
                                                                            │
                                                                            ▼
                                                                        delivery → END
```

Conditional edges abort the run when:
- `common_infra` rejects the CDETS ID format
- `prescan` cannot fetch CDETS
- `cdets_tz_analyzer` fails to produce a schema

The RAG node short-circuits straight to `delivery` on a high-confidence
duplicate. The human-in-the-loop detour (missing_info_request → human_review →
merge_human_input) only fires when ``human_in_loop.enabled`` is set AND the run
carries a ``job_id`` (i.e. it is driven by the console runner, which compiles
the graph with a checkpointer). CLI / batch / analyze runs never enter the
interrupt path, so they remain unaffected.
"""

from __future__ import annotations

import logging
from typing import Literal

from langgraph.graph import END, START, StateGraph

from .nodes.abort import abort_node
from .nodes.cafy_rca_analyzer import cafy_rca_analyzer_node
from .nodes.cdets_scoring import cdets_scoring_node
from .nodes.cdets_tz_analyzer import cdets_tz_analyzer_node
from .nodes.common_infra import common_infra_node
from .nodes.coverage_comparison import coverage_comparison_node
from .nodes.delivery import delivery_node
from .nodes.email_report_generator import email_report_generator_node
from .nodes.existing_test_scanner import existing_test_scanner_node
from .nodes.human_review import human_review_node
from .nodes.merge_coverage import merge_coverage_node
from .nodes.merge_human_input import merge_human_input_node
from .nodes.missing_info_request import missing_info_request_node
from .nodes.prescan import prescan_node
from .nodes.rag_fetch_related_cdets import rag_fetch_related_cdets_node
from .nodes.testcase_generator import testcase_generator_node
from .state import FLAgentState

logger = logging.getLogger(__name__)


def _route_after_common_infra(state: FLAgentState) -> Literal["prescan", "abort"]:
    return "prescan" if state.get("init_valid") else "abort"


def _route_after_prescan(
    state: FLAgentState,
) -> Literal["rag_fetch_related_cdets", "abort"]:
    return "rag_fetch_related_cdets" if state.get("cdets_lookup_ok") else "abort"


def _route_after_rag(
    state: FLAgentState,
) -> Literal["cdets_tz_analyzer", "delivery"]:
    """Short-circuit to delivery when a high-confidence duplicate is found."""
    return "delivery" if state.get("rag_short_circuit") else "cdets_tz_analyzer"


def _route_after_cdets_analyzer(
    state: FLAgentState,
) -> Literal["cdets_scoring", "abort"]:
    """Continue to scoring once the schema exists.

    Scoring runs sequentially (before RCA) so its ``cdet_ai_score`` can gate
    the human-in-the-loop detour.
    """
    return "cdets_scoring" if state.get("cdets_schema_path") else "abort"


def _route_after_scoring(
    state: FLAgentState,
) -> Literal["cafy_rca_analyzer", "missing_info_request"]:
    """Gate downstream stages on the AI quality score.

    If ``cdet_ai_score`` is below the configured threshold AND we haven't
    already gone around the human-review loop ``max_review_rounds`` times,
    detour through ``missing_info_request → human_review → merge_human_input``
    before resuming the normal flow.

    The detour requires an ``interrupt()``-capable run, which only the console
    runner provides (it compiles the graph with a checkpointer and sets a
    ``job_id`` on the state). Runs without a ``job_id`` (CLI / batch / the
    synchronous analyze endpoint) skip HITL entirely so they never block.
    """
    cfg = (state.get("config") or {}).get("human_in_loop") or {}
    if not cfg.get("enabled", False):
        return "cafy_rca_analyzer"
    if not state.get("job_id"):
        return "cafy_rca_analyzer"
    score = float(state.get("cdet_ai_score") or 0.0)
    threshold = float(cfg.get("score_threshold", 60))
    max_rounds = int(cfg.get("max_review_rounds", 1))
    rounds_done = int(state.get("human_review_count") or 0)
    if score < threshold and rounds_done < max_rounds:
        logger.info(
            "_route_after_scoring: score=%.1f < %.1f (round %d/%d) → missing_info_request",
            score, threshold, rounds_done, max_rounds,
        )
        return "missing_info_request"
    return "cafy_rca_analyzer"


def build_graph(checkpointer=None):
    """Assemble and compile the FL Agent LangGraph.

    Pass a ``checkpointer`` (e.g. ``AsyncSqliteSaver``) to enable
    ``interrupt()`` / ``Command(resume=...)`` semantics for the
    human-in-the-loop branch.
    """
    g = StateGraph(FLAgentState)

    # Sequential prelude
    g.add_node("common_infra", common_infra_node)
    g.add_node("prescan", prescan_node)
    g.add_node("rag_fetch_related_cdets", rag_fetch_related_cdets_node)
    g.add_node("cdets_tz_analyzer", cdets_tz_analyzer_node)
    g.add_node("cdets_scoring", cdets_scoring_node)
    g.add_node("cafy_rca_analyzer", cafy_rca_analyzer_node)

    # Human-in-the-loop branch (low-score gating)
    g.add_node("missing_info_request", missing_info_request_node)
    g.add_node("human_review", human_review_node)
    g.add_node("merge_human_input", merge_human_input_node)

    # Fan-out branches
    g.add_node("testcase_generator", testcase_generator_node)
    g.add_node("existing_test_scanner", existing_test_scanner_node)

    # Join + downstream
    g.add_node("merge_coverage", merge_coverage_node)
    g.add_node("coverage_comparison", coverage_comparison_node)
    g.add_node("email_report_generator", email_report_generator_node)
    g.add_node("delivery", delivery_node)

    # Abort terminal
    g.add_node("abort", abort_node)

    g.add_edge(START, "common_infra")

    g.add_conditional_edges(
        "common_infra",
        _route_after_common_infra,
        {"prescan": "prescan", "abort": "abort"},
    )
    g.add_conditional_edges(
        "prescan",
        _route_after_prescan,
        {"rag_fetch_related_cdets": "rag_fetch_related_cdets", "abort": "abort"},
    )
    # After RAG: short-circuit to delivery on a high-confidence duplicate,
    # otherwise continue into the normal analysis pipeline.
    g.add_conditional_edges(
        "rag_fetch_related_cdets",
        _route_after_rag,
        {"cdets_tz_analyzer": "cdets_tz_analyzer", "delivery": "delivery"},
    )
    g.add_conditional_edges(
        "cdets_tz_analyzer",
        _route_after_cdets_analyzer,
        {"cdets_scoring": "cdets_scoring", "abort": "abort"},
    )
    g.add_conditional_edges(
        "cdets_scoring",
        _route_after_scoring,
        {
            "cafy_rca_analyzer": "cafy_rca_analyzer",
            "missing_info_request": "missing_info_request",
        },
    )

    # HITL loop: request → pause → merge → re-analyze (which feeds scoring again)
    g.add_edge("missing_info_request", "human_review")
    g.add_edge("human_review", "merge_human_input")
    g.add_edge("merge_human_input", "cdets_tz_analyzer")

    # Fan-out: same source → two parallel targets
    g.add_edge("cafy_rca_analyzer", "testcase_generator")
    g.add_edge("cafy_rca_analyzer", "existing_test_scanner")

    # Join: both branches feed merge_coverage
    g.add_edge("testcase_generator", "merge_coverage")
    g.add_edge("existing_test_scanner", "merge_coverage")

    g.add_edge("merge_coverage", "coverage_comparison")
    g.add_edge("coverage_comparison", "email_report_generator")
    g.add_edge("email_report_generator", "delivery")

    g.add_edge("delivery", END)
    g.add_edge("abort", END)

    if checkpointer is not None:
        return g.compile(checkpointer=checkpointer)
    return g.compile()
