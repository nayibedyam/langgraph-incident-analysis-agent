"""Lightweight RAG retrieval over the CDETS bug corpus.

The gateway appkey used by this project is scoped to ``gpt-5-nano`` chat
only — the embedding deployments return 401. So retrieval here is built on a
local TF-IDF vector space (scikit-learn) instead of a hosted embedding model.
This keeps the index fully offline, deterministic, and fast over ~10k bugs.

Public API:
    build_index(...)        -> build & persist the TF-IDF index from the CSV
    CDETSRetriever          -> load the index and query top-k related CDETS
    get_related_cdets(...)  -> convenience top-level query helper
"""

from __future__ import annotations

from .corpus import CorpusDoc, load_corpus
from .index import build_index
from .retriever import CDETSRetriever, RelatedCDETS, get_related_cdets

__all__ = [
    "CorpusDoc",
    "load_corpus",
    "build_index",
    "CDETSRetriever",
    "RelatedCDETS",
    "get_related_cdets",
]
