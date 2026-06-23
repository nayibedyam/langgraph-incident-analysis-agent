"""Query the TF-IDF index for the top-k CDETS most related to a query.

Typical use inside the pipeline::

    from backend.pipeline.rag import get_related_cdets
    related = get_related_cdets(cdets_id="CSCwr73685", fields=state["cdets_fields"], k=5)

``related`` is a list of :class:`RelatedCDETS` sorted by descending similarity.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Dict, List, Optional

import joblib
from scipy import sparse
from sklearn.metrics.pairwise import linear_kernel

from .index import MATRIX_FILE, META_FILE, VECTORIZER_FILE

logger = logging.getLogger(__name__)

# Fields from a CDETS lookup (prescan ``cdets_fields``) used to build a query
# when the bug is not already present in the corpus.
_QUERY_FIELDS = [
    "Headline",
    "Summary",
    "Component",
    "Product",
    "Root-cause-analysis",
    "description",
    "eng_notes",
]


@dataclass
class RelatedCDETS:
    """One retrieval hit."""

    identifier: str
    score: float
    snippet: str

    def to_dict(self) -> dict:
        return {
            "identifier": self.identifier,
            "score": round(float(self.score), 4),
            "snippet": self.snippet,
        }


class CDETSRetriever:
    """Loads a persisted TF-IDF index and answers top-k queries."""

    def __init__(self, index_dir: str) -> None:
        self.index_dir = index_dir
        vec_path = os.path.join(index_dir, VECTORIZER_FILE)
        mat_path = os.path.join(index_dir, MATRIX_FILE)
        meta_path = os.path.join(index_dir, META_FILE)
        for p in (vec_path, mat_path, meta_path):
            if not os.path.isfile(p):
                raise FileNotFoundError(
                    f"Index artifact missing: {p}. Build it first with "
                    f"src/backend/cli/build_rag_index.py."
                )
        self.vectorizer = joblib.load(vec_path)
        self.matrix = sparse.load_npz(mat_path)
        with open(meta_path, encoding="utf-8") as fh:
            self.meta = json.load(fh)
        self.identifiers: List[str] = self.meta["identifiers"]
        self.snippets: List[str] = self.meta.get("snippets", [""] * len(self.identifiers))
        self._id_to_row: Dict[str, int] = {
            cid: i for i, cid in enumerate(self.identifiers)
        }

    @property
    def doc_count(self) -> int:
        return len(self.identifiers)

    def has(self, cdets_id: str) -> bool:
        return cdets_id in self._id_to_row

    def query_text(
        self, text: str, k: int = 5, *, exclude_ids: Optional[set[str]] = None
    ) -> List[RelatedCDETS]:
        """Return the top-k documents most similar to ``text``."""
        if not text or not text.strip():
            return []
        q = self.vectorizer.transform([text])
        sims = linear_kernel(q, self.matrix).ravel()
        return self._top_k(sims, k, exclude_ids or set())

    def query_by_id(
        self, cdets_id: str, k: int = 5, *, exclude_self: bool = True
    ) -> List[RelatedCDETS]:
        """Return the top-k documents most similar to an in-corpus CDETS row."""
        row = self._id_to_row.get(cdets_id)
        if row is None:
            return []
        sims = linear_kernel(self.matrix[row], self.matrix).ravel()
        exclude = {cdets_id} if exclude_self else set()
        return self._top_k(sims, k, exclude)

    def query(
        self,
        *,
        cdets_id: Optional[str] = None,
        text: Optional[str] = None,
        fields: Optional[Dict[str, str]] = None,
        k: int = 5,
        exclude_self: bool = True,
    ) -> List[RelatedCDETS]:
        """Flexible entry point.

        Resolution order:
          1. If ``cdets_id`` is already indexed, use its own vector (best signal).
          2. Else build query text from ``text`` and/or ``fields`` and search.
        """
        if cdets_id and self.has(cdets_id):
            return self.query_by_id(cdets_id, k, exclude_self=exclude_self)

        query_str = build_query_text(text=text, fields=fields)
        exclude = {cdets_id} if (cdets_id and exclude_self) else set()
        return self.query_text(query_str, k, exclude_ids=exclude)

    def _top_k(
        self, sims, k: int, exclude_ids: set[str]
    ) -> List[RelatedCDETS]:
        import numpy as np

        n = sims.shape[0]
        if n == 0:
            return []
        # Over-fetch a little so excluded ids don't shrink the result set.
        fetch = min(n, k + len(exclude_ids) + 5)
        cand = np.argpartition(-sims, fetch - 1)[:fetch]
        cand = cand[np.argsort(-sims[cand])]
        out: List[RelatedCDETS] = []
        for idx in cand:
            cid = self.identifiers[idx]
            if cid in exclude_ids:
                continue
            score = float(sims[idx])
            if score <= 0.0:
                continue
            out.append(RelatedCDETS(identifier=cid, score=score, snippet=self.snippets[idx]))
            if len(out) >= k:
                break
        return out


def build_query_text(
    *, text: Optional[str] = None, fields: Optional[Dict[str, str]] = None
) -> str:
    """Assemble a query string from free text and/or CDETS fields."""
    parts: List[str] = []
    if text and text.strip():
        parts.append(text.strip())
    if fields:
        for key in _QUERY_FIELDS:
            val = (fields.get(key) or "").strip()
            if val:
                parts.append(val)
    return "\n".join(parts)


@lru_cache(maxsize=4)
def _cached_retriever(index_dir: str) -> CDETSRetriever:
    return CDETSRetriever(index_dir)


def get_related_cdets(
    *,
    index_dir: str,
    cdets_id: Optional[str] = None,
    text: Optional[str] = None,
    fields: Optional[Dict[str, str]] = None,
    k: int = 5,
    exclude_self: bool = True,
) -> List[RelatedCDETS]:
    """Convenience wrapper: load (cached) retriever and run a query."""
    retriever = _cached_retriever(index_dir)
    return retriever.query(
        cdets_id=cdets_id, text=text, fields=fields, k=k, exclude_self=exclude_self
    )
