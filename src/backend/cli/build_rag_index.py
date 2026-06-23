#!/usr/bin/env python3
"""CLI to build and query the CDETS RAG (TF-IDF) index.

Examples
--------
Build the index from the bug-list CSV (paths default from src/backend/config/config.yaml)::

    python src/backend/cli/build_rag_index.py build

Build from an explicit CSV / output dir::

    python src/backend/cli/build_rag_index.py build \
        --csv /path/to/bug_list.csv \
        --index-dir cdets_data/rag_index

Retrieve the top-5 related CDETS for the bug being analysed::

    python src/backend/cli/build_rag_index.py query --cdets-id CSCwr73685
    python src/backend/cli/build_rag_index.py query --text "GRE IPsec decap traffic drop on NCS5700"
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

# Ensure src/ is importable when run as `python src/backend/cli/build_rag_index.py`.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import yaml

from backend.pipeline.llm import _resolve_env
from backend.pipeline.rag import build_index, get_related_cdets
from backend.pipeline.utils import backend_root

DEFAULT_CSV = "/path/to/bug_list.csv"
DEFAULT_INDEX_DIR = "cdets_data/rag_index"


def _load_config(path: str | None = None) -> dict:
    cfg_path = path or str(backend_root() / "config" / "config.yaml")
    if os.path.isfile(cfg_path):
        with open(cfg_path, encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    return {}


def _rag_cfg(config: dict) -> dict:
    rag = dict(config.get("rag", {}) or {})
    # Resolve ${VAR:-default} placeholders in string values.
    for key, val in rag.items():
        if isinstance(val, str):
            rag[key] = _resolve_env(val)
    return rag


def cmd_build(args: argparse.Namespace) -> int:
    config = _load_config(args.config)
    rag = _rag_cfg(config)
    csv_path = args.csv or rag.get("corpus_csv") or DEFAULT_CSV
    index_dir = args.index_dir or rag.get("index_dir") or DEFAULT_INDEX_DIR

    if not os.path.isfile(csv_path):
        print(f"ERROR: corpus CSV not found: {csv_path}", file=sys.stderr)
        return 2

    summary = build_index(
        csv_path=csv_path,
        index_dir=index_dir,
        id_column=rag.get("id_column", "Identifier"),
        text_columns=rag.get("text_columns") or None,
    )
    print(json.dumps(summary, indent=2))
    return 0


def cmd_query(args: argparse.Namespace) -> int:
    config = _load_config(args.config)
    rag = _rag_cfg(config)
    index_dir = args.index_dir or rag.get("index_dir") or DEFAULT_INDEX_DIR
    k = args.k or int(rag.get("top_k", 5))

    if not args.cdets_id and not args.text:
        print("ERROR: provide --cdets-id and/or --text", file=sys.stderr)
        return 2

    # If we have a CDETS id that is NOT already in the corpus and no explicit
    # query text was given, pull the bug's fields via dumpcr so we can build a
    # query from its headline/summary/RCA.
    fields = None
    if args.cdets_id and not args.text and not args.no_lookup:
        from backend.pipeline.rag import CDETSRetriever
        from backend.pipeline.tools.cdets import lookup_cdets_impl

        try:
            retriever = CDETSRetriever(index_dir)
            in_corpus = retriever.has(args.cdets_id)
        except FileNotFoundError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        if not in_corpus:
            lookup = lookup_cdets_impl(args.cdets_id)
            if lookup.get("ok"):
                fields = lookup.get("fields") or {}
            else:
                print(
                    f"WARNING: {args.cdets_id} not in corpus and dumpcr lookup "
                    f"failed ({lookup.get('error')}); pass --text instead.",
                    file=sys.stderr,
                )

    results = get_related_cdets(
        index_dir=index_dir,
        cdets_id=args.cdets_id,
        text=args.text,
        fields=fields,
        k=k,
        exclude_self=True,
    )
    if args.json:
        print(json.dumps([r.to_dict() for r in results], indent=2))
    else:
        if not results:
            print("No related CDETS found.")
        for rank, r in enumerate(results, 1):
            print(f"{rank}. {r.identifier}  (score={r.score:.4f})")
            print(f"   {r.snippet}")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="CDETS RAG index builder / query tool")
    parser.add_argument("--config", default=None, help="Path to config.yaml (default: config/config.yaml)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_build = sub.add_parser("build", help="Build the TF-IDF index from the CSV corpus")
    p_build.add_argument("--csv", help="Source CSV (defaults from config / built-in)")
    p_build.add_argument("--index-dir", help="Output directory for index artifacts")
    p_build.set_defaults(func=cmd_build)

    p_query = sub.add_parser("query", help="Retrieve top-k related CDETS")
    p_query.add_argument("--cdets-id", help="CDETS id being analysed")
    p_query.add_argument("--text", help="Free-text query (headline/summary/etc.)")
    p_query.add_argument("--index-dir", help="Index directory to load")
    p_query.add_argument("-k", type=int, help="Number of results (default 5)")
    p_query.add_argument("--json", action="store_true", help="Emit JSON")
    p_query.add_argument(
        "--no-lookup",
        action="store_true",
        help="Do not auto-fetch CDETS fields via dumpcr when id is not in corpus",
    )
    p_query.set_defaults(func=cmd_query)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
