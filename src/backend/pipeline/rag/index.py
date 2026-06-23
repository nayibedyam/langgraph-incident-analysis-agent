"""Build and persist a TF-IDF retrieval index over the CDETS corpus.

Artifacts written to ``index_dir``:
    vectorizer.joblib   - the fitted :class:`TfidfVectorizer`
    matrix.npz          - the sparse document-term matrix (L2-normalized)
    meta.json           - ordered identifiers + per-doc snippets + build info

The matrix rows are L2-normalized by TfidfVectorizer, so cosine similarity at
query time reduces to a single sparse dot product (``linear_kernel``).
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import List, Optional

import joblib
from scipy import sparse
from sklearn.feature_extraction.text import TfidfVectorizer

from .corpus import TEXT_COLUMNS, CorpusDoc, load_corpus

logger = logging.getLogger(__name__)

VECTORIZER_FILE = "vectorizer.joblib"
MATRIX_FILE = "matrix.npz"
META_FILE = "meta.json"


def build_index(
    csv_path: str,
    index_dir: str,
    *,
    id_column: str = "Identifier",
    text_columns: Optional[List[str]] = None,
    max_features: int = 200_000,
    ngram_range: tuple[int, int] = (1, 2),
    min_df: int = 2,
    snippet_chars: int = 240,
) -> dict:
    """Fit a TF-IDF index over the CSV corpus and persist it to ``index_dir``.

    Returns a small summary dict (doc count, vocabulary size, paths).
    """
    cols = text_columns or TEXT_COLUMNS
    t0 = time.time()

    docs: List[CorpusDoc] = load_corpus(
        csv_path, id_column=id_column, text_columns=cols, skip_empty=True
    )
    if not docs:
        raise ValueError(f"No usable documents loaded from {csv_path}")
    logger.info("Loaded %d documents from %s", len(docs), csv_path)

    vectorizer = TfidfVectorizer(
        lowercase=True,
        stop_words="english",
        ngram_range=ngram_range,
        max_features=max_features,
        min_df=min_df,
        sublinear_tf=True,
        token_pattern=r"(?u)\b[a-zA-Z][a-zA-Z0-9_.\-]{1,}\b",
    )
    matrix = vectorizer.fit_transform(d.text for d in docs)
    logger.info(
        "Fitted TF-IDF: %d docs x %d terms", matrix.shape[0], matrix.shape[1]
    )

    os.makedirs(index_dir, exist_ok=True)
    joblib.dump(vectorizer, os.path.join(index_dir, VECTORIZER_FILE))
    sparse.save_npz(os.path.join(index_dir, MATRIX_FILE), matrix)

    meta = {
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "source_csv": os.path.abspath(csv_path),
        "id_column": id_column,
        "text_columns": cols,
        "doc_count": len(docs),
        "vocab_size": len(vectorizer.vocabulary_),
        "ngram_range": list(ngram_range),
        "identifiers": [d.identifier for d in docs],
        "snippets": [d.snippet(snippet_chars) for d in docs],
    }
    with open(os.path.join(index_dir, META_FILE), "w", encoding="utf-8") as fh:
        json.dump(meta, fh)

    summary = {
        "doc_count": len(docs),
        "vocab_size": len(vectorizer.vocabulary_),
        "index_dir": os.path.abspath(index_dir),
        "elapsed_sec": round(time.time() - t0, 2),
    }
    logger.info("Index build complete: %s", summary)
    return summary
