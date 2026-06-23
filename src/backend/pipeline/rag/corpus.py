"""Corpus loader: turn the bug-list CSV into retrieval documents.

The source CSV (``1_bug_list_updated.csv``) has these columns::

    Identifier, eng_notes, scrub_notes, release_notes,
    cfd_analysis, regression_analysis, description

Each row becomes one :class:`CorpusDoc` keyed on ``Identifier``. The free-text
columns are concatenated (with a short field label) into a single ``text``
blob that the TF-IDF vectorizer indexes.
"""

from __future__ import annotations

import csv
import sys
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional

# CSV fields can contain very large note blobs; lift the field-size ceiling.
csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

ID_COLUMN = "Identifier"

# Text columns in priority order. Labels are prepended so the vectorizer can
# (weakly) distinguish where a term came from and humans can read snippets.
TEXT_COLUMNS: List[str] = [
    "description",
    "eng_notes",
    "scrub_notes",
    "release_notes",
    "cfd_analysis",
    "regression_analysis",
]


@dataclass
class CorpusDoc:
    """One indexed CDETS record."""

    identifier: str
    text: str
    fields: Dict[str, str] = field(default_factory=dict)

    def snippet(self, max_chars: int = 240) -> str:
        snip = " ".join(self.text.split())
        return snip[:max_chars] + ("…" if len(snip) > max_chars else "")


def build_doc_text(row: Dict[str, str], text_columns: List[str]) -> str:
    """Concatenate the configured text columns into a single blob."""
    parts: List[str] = []
    for col in text_columns:
        val = (row.get(col) or "").strip()
        if val:
            parts.append(f"[{col}] {val}")
    return "\n".join(parts)


def load_corpus(
    csv_path: str,
    *,
    id_column: str = ID_COLUMN,
    text_columns: Optional[List[str]] = None,
    skip_empty: bool = True,
) -> List[CorpusDoc]:
    """Load the CSV into a list of :class:`CorpusDoc`.

    Rows with a blank identifier are always skipped. When ``skip_empty`` is
    true, rows whose combined text is empty are skipped too.
    """
    cols = text_columns or TEXT_COLUMNS
    docs: List[CorpusDoc] = []
    seen: set[str] = set()

    for row in _iter_rows(csv_path):
        identifier = (row.get(id_column) or "").strip()
        if not identifier or identifier in seen:
            continue
        text = build_doc_text(row, cols)
        if skip_empty and not text.strip():
            continue
        seen.add(identifier)
        docs.append(
            CorpusDoc(
                identifier=identifier,
                text=text,
                fields={k: (row.get(k) or "") for k in row},
            )
        )
    return docs


def _iter_rows(csv_path: str) -> Iterator[Dict[str, str]]:
    with open(csv_path, newline="", encoding="utf-8", errors="replace") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            yield row
