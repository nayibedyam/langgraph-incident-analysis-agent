#!/usr/bin/env python3
"""Run Phoenix LLM-as-judge evaluations over traced spans.

Tracing records spans; this script *evaluates* them and writes the verdicts
back to Phoenix as span annotations (the "Annotations" you see in the UI).

Prerequisites:
  1. A Phoenix server is running (./.venv/bin/phoenix serve) and reachable at
     PHOENIX_COLLECTOR_ENDPOINT (default http://localhost:6006).
  2. You have already run the pipeline at least once with tracing enabled, so
     there are LLM spans to score.

Usage:
    python src/eval/run_phoenix_evals.py
    python src/eval/run_phoenix_evals.py --eval quality --project fl-langgraph-agent
    python src/eval/run_phoenix_evals.py --eval hallucination --hours 6 --limit 50
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

# Ensure src/ is importable when run as `python src/eval/run_phoenix_evals.py`.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

from backend.pipeline.utils import load_config
from eval.evals import DEFAULT_PROJECT, EVAL_SPECS, run_phoenix_evals


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--eval",
        dest="eval_kind",
        default="quality",
        choices=sorted(EVAL_SPECS),
        help="Which LLM-as-judge dimension to score. Default: quality.",
    )
    parser.add_argument(
        "--project",
        default=DEFAULT_PROJECT,
        help=f"Phoenix project name. Default: {DEFAULT_PROJECT}.",
    )
    parser.add_argument(
        "--hours",
        type=float,
        default=24.0,
        help="Only evaluate spans from the last N hours. Default: 24.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Maximum number of spans to pull. Default: 100.",
    )
    parser.add_argument(
        "--judge-stage",
        default="scoring",
        help="Which configured model stage to use as the judge. Default: scoring.",
    )
    parser.add_argument(
        "--endpoint",
        default=None,
        help="Phoenix base URL (defaults to PHOENIX_COLLECTOR_ENDPOINT or localhost:6006).",
    )
    parser.add_argument("--config", default=None, help="Path to config.yaml.")
    parser.add_argument("-v", "--verbose", action="count", default=1)
    return parser.parse_args()


def main() -> int:
    load_dotenv()
    args = _parse_args()
    level = logging.INFO if args.verbose <= 1 else logging.DEBUG
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )

    config = load_config(args.config)
    summary = run_phoenix_evals(
        config=config,
        project_name=args.project,
        eval_kind=args.eval_kind,
        hours=args.hours,
        limit=args.limit,
        judge_stage=args.judge_stage,
        phoenix_endpoint=args.endpoint,
    )
    print(json.dumps(summary, indent=2))
    return 0 if summary.get("evaluated", 0) > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
