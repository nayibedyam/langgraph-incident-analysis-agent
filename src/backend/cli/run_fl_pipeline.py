#!/usr/bin/env python3
"""CLI entry point for the FL LangGraph Agent.

Usage:
    python src/backend/cli/run_fl_pipeline.py CSCwk35275
    python src/backend/cli/run_fl_pipeline.py CSCwk35275 CSCwk35276 --parallel 2
    python src/backend/cli/run_fl_pipeline.py CSCwk35275 --dry-run
    python src/backend/cli/run_fl_pipeline.py --bugs-file bugs.txt --parallel 5
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import List

# Ensure src/ is importable when run as `python src/backend/cli/run_fl_pipeline.py`.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from dotenv import load_dotenv

from backend.pipeline.batch import run_batch
from backend.pipeline.utils import is_valid_cdets_id, load_config
from eval.tracing import setup_phoenix_tracing, tracing_enabled_from_env


def _setup_logging(verbosity: int) -> None:
    level = logging.WARNING
    if verbosity == 1:
        level = logging.INFO
    elif verbosity >= 2:
        level = logging.DEBUG
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )


def _parse_bugs_file(path: str) -> List[str]:
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"bugs-file not found: {p}")
    ids = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        ids.append(line)
    return ids


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the FL LangGraph Agent on one or more CDETS defects.",
    )
    parser.add_argument(
        "cdets_ids",
        nargs="*",
        help="One or more CDETS defect IDs (e.g. CSCwk35275).",
    )
    parser.add_argument(
        "--bugs-file",
        help="Path to a text file with one CDETS ID per line (# comments allowed).",
    )
    parser.add_argument(
        "--parallel",
        type=int,
        default=1,
        help="Maximum number of defects processed concurrently. Default: 1.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip MongoDB / TFTP / SMTP side-effects (still writes local artifacts).",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to config.yaml (defaults to config/config.yaml in the project root).",
    )
    parser.add_argument(
        "--output-summary",
        help="Optional path: write the batch summary as JSON to this file.",
    )
    parser.add_argument(
        "--trace",
        action="store_true",
        help=(
            "Enable Arize Phoenix tracing. Launches a local Phoenix UI "
            "(http://localhost:6006) and captures every LangGraph node and LLM "
            "call as an OpenTelemetry trace. Also enabled via PHOENIX_TRACING=1."
        ),
    )
    parser.add_argument("-v", "--verbose", action="count", default=1)
    return parser.parse_args()


async def _main_async() -> int:
    load_dotenv()
    args = _parse_args()
    _setup_logging(args.verbose)

    if args.trace or tracing_enabled_from_env():
        setup_phoenix_tracing()

    ids: List[str] = list(args.cdets_ids)
    if args.bugs_file:
        ids.extend(_parse_bugs_file(args.bugs_file))

    if not ids:
        print("error: no CDETS IDs provided. Pass them as args or via --bugs-file.")
        return 2

    invalid = [i for i in ids if not is_valid_cdets_id(i)]
    if invalid:
        print(f"error: invalid CDETS IDs: {invalid}")
        return 2

    config = load_config(args.config)
    results = await run_batch(
        ids,
        config,
        parallel=args.parallel,
        dry_run=args.dry_run,
    )

    summary = {
        "total": len(results),
        "succeeded": sum(1 for r in results if r.get("ok")),
        "failed": sum(1 for r in results if not r.get("ok")),
        "results": [
            {
                "cdets_id": r["cdets_id"],
                "ok": r.get("ok", False),
                "delivery_status": (r.get("result") or {}).get("delivery_status"),
                "error": r.get("error"),
            }
            for r in results
        ],
    }

    print(json.dumps(summary, indent=2))

    if args.output_summary:
        Path(args.output_summary).write_text(
            json.dumps(summary, indent=2, default=str),
            encoding="utf-8",
        )

    return 0 if summary["failed"] == 0 else 1


def main() -> int:
    try:
        return asyncio.run(_main_async())
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
