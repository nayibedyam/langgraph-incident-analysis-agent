"""LLM-powered debugging agents for the FL Console.

Each function is a focused "agent" that calls the hosted LLM
(``backend.api.llm.GatewayLLM``) with a purpose-built system prompt:

    remediation_agent  — RCA + remediation grounded in similar past defects (RAG)
    log_triage_agent   — paste-a-log triage: signatures, probable cause, matches
    chat_agent         — conversational Q&A about a specific analyzed defect
    enrichment_agent   — CDETS ticket-enrichment note (metadata + next steps)

All agents are best-effort and raise ``LLMError`` on failure so callers can
degrade gracefully. Deterministic helpers (priority scoring, similarity) live
here too so the API layer stays thin.
"""

from __future__ import annotations

import re
from typing import Optional

from .llm import GatewayLLM, LLMError

# --------------------------------------------------------------------------- #
# Deterministic triage priority (no LLM — fast, explainable)
# --------------------------------------------------------------------------- #
_SEV_WEIGHT = {"1": 45, "2": 38, "3": 28, "4": 16, "5": 8, "6": 6}


def compute_priority(scores: dict, *, severity: str = "", regression: bool = False) -> dict:
    """Compute a P1-P4 triage priority from impact + urgency signals.

    Returns {"priority": "P1".."P4", "score": int, "rationale": str}.
    Inputs are strings parsed from the Score Summary Box plus optional CDETS
    severity / regression flags.
    """
    pts = 0
    reasons: list[str] = []

    sev_digit = ""
    m = re.search(r"(\d)", severity or "")
    if m:
        sev_digit = m.group(1)
    pts += _SEV_WEIGHT.get(sev_digit, 14)
    if sev_digit:
        reasons.append(f"severity {sev_digit}")

    cov = (scores.get("test_coverage_confidence") or "").lower()
    gap = (scores.get("coverage_gap") or "").upper()
    if "no coverage" in cov or "NEW_TEST_REQUIRED" in gap:
        pts += 18
        reasons.append("no existing test coverage")
    elif "partial" in cov:
        pts += 10
        reasons.append("partial coverage")

    cdets = scores.get("cdets_defect_score") or ""
    sm = re.search(r"(\d+)", cdets)
    if sm and int(sm.group(1)) >= 75:
        pts += 8
        reasons.append("well-documented (high CDETS score)")

    if regression:
        pts += 14
        reasons.append("regression of a prior fix")

    if pts >= 70:
        prio = "P1"
    elif pts >= 52:
        prio = "P2"
    elif pts >= 34:
        prio = "P3"
    else:
        prio = "P4"

    return {
        "priority": prio,
        "score": min(pts, 100),
        "rationale": "; ".join(reasons) or "baseline",
    }


# --------------------------------------------------------------------------- #
# Deterministic similarity (token overlap over component/AP/headline/signature)
# --------------------------------------------------------------------------- #
_STOP = {
    "the", "a", "an", "to", "of", "in", "on", "for", "and", "or", "is", "are",
    "when", "with", "during", "seen", "issue", "failure", "error",
}


def _tokens(*parts: str) -> set[str]:
    text = " ".join(p for p in parts if p).lower()
    toks = re.findall(r"[a-z0-9_%\-]{3,}", text)
    return {t for t in toks if t not in _STOP}


def similarity_score(a: dict, b: dict) -> int:
    """Jaccard-style similarity (0-100) between two defect summary dicts."""
    ta = _tokens(
        a.get("component", ""), a.get("headline", ""),
        (a.get("scores") or {}).get("primary_ap", ""),
        (a.get("scores") or {}).get("sub_ap", ""),
    )
    tb = _tokens(
        b.get("component", ""), b.get("headline", ""),
        (b.get("scores") or {}).get("primary_ap", ""),
        (b.get("scores") or {}).get("sub_ap", ""),
    )
    if not ta or not tb:
        return 0
    inter = len(ta & tb)
    union = len(ta | tb)
    base = round(100 * inter / union) if union else 0
    # Component match is a strong signal — boost it.
    if a.get("component") and a.get("component") == b.get("component"):
        base = min(100, base + 25)
    return base


# --------------------------------------------------------------------------- #
# LLM agents
# --------------------------------------------------------------------------- #
_REMEDIATION_SYS = """You are an IOS-XR production debugging agent. You produce \
concise, actionable root-cause analysis and remediation steps for a CDETS \
defect, grounded ONLY in the provided defect evidence and the listed similar \
past defects. Never invent CDETS IDs, fixes, or commands. Prefer remediation \
patterns that worked in the similar past defects. Output clean Markdown."""


def remediation_agent(
    llm: GatewayLLM,
    *,
    cdets_id: str,
    headline: str,
    cdets_raw: str,
    neighbors: list[dict],
) -> str:
    """RAG remediation: RCA + steps grounded in similar past defects."""
    neighbor_block = "\n".join(
        f"- {n['cdets_id']} ({n.get('similarity', 0)}% similar): {n.get('headline', '')}"
        for n in neighbors[:5]
    ) or "- (no closely related past defects found)"
    prompt = f"""Defect {cdets_id}: {headline}

SIMILAR PAST DEFECTS (use their resolution patterns as priors):
{neighbor_block}

CURRENT DEFECT EVIDENCE:
---
{cdets_raw[:9000]}
---

Produce Markdown with these sections:
## Probable Root Cause
(2-4 sentences, evidence-anchored)
## Recommended Remediation Steps
(numbered, concrete; name CLI/process/config when implied by evidence)
## Verification
(how to confirm the fix worked)
## Confidence
(HIGH/MEDIUM/LOW + one-line why)
Output ONLY the Markdown."""
    return llm.generate(_REMEDIATION_SYS, prompt, max_tokens=1600)


_LOG_TRIAGE_SYS = """You are a first-line SRE triage agent for IOS-XR. \
Given raw logs / error traces / syslog, you extract the key failure signatures, \
infer the probable subsystem and root cause, and recommend immediate next steps. \
You are fast and decisive but never fabricate specific CDETS IDs. Output Markdown."""


def log_triage_agent(llm: GatewayLLM, *, log_text: str, candidates: list[dict]) -> str:
    """Paste-a-log triage. ``candidates`` are possibly-related analyzed defects."""
    cand_block = "\n".join(
        f"- {c['cdets_id']}: {c.get('headline', '')}" for c in candidates[:6]
    ) or "- (no analyzed defects matched the signatures)"
    prompt = f"""RAW INPUT (logs / trace / symptom):
---
{log_text[:9000]}
---

POSSIBLY-RELATED ANALYZED DEFECTS (only reference these IDs if relevant):
{cand_block}

Produce Markdown:
## Extracted Signatures
(syslog %FAC-SEV-MNEMONIC codes, exceptions, key error lines)
## Probable Subsystem / Component
## Probable Root Cause
## Immediate Next Steps
(numbered, what an on-call engineer should do now)
## Related Defects
(only the candidate IDs above that genuinely match, with one-line why; or "None")
Output ONLY the Markdown."""
    return llm.generate(_LOG_TRIAGE_SYS, prompt, max_tokens=1500)


_CHAT_SYS = """You are a debugging assistant answering questions about ONE \
specific CDETS defect. Answer concisely and only from the provided defect \
context (CDETS fields, scorecard, test case, CaFy coverage). If the context does \
not contain the answer, say so plainly. Output Markdown."""


def chat_agent(llm: GatewayLLM, *, question: str, context: str) -> str:
    """Conversational Q&A grounded in one defect's artifacts."""
    prompt = f"""DEFECT CONTEXT:
---
{context[:11000]}
---

QUESTION: {question}

Answer from the context above. Be specific and brief."""
    return llm.generate(_CHAT_SYS, prompt, max_tokens=1200)


_ENRICH_SYS = """You are a CDETS ticket-enrichment agent. You draft a concise \
enrichment note for a defect: suspected component/AP, failure category, \
testability, and recommended next steps. The note is for engineers; keep it \
factual and grounded in the evidence. Output plain text suitable for a CDETS note."""


def enrichment_agent(
    llm: GatewayLLM, *, cdets_id: str, scores: dict, cafy_verdict: str, cdets_raw: str
) -> str:
    """Draft a CDETS enrichment note (returned as text; write-back is separate)."""
    prompt = f"""Draft an AI-FL enrichment note for {cdets_id}.

Pre-resolved analysis:
- Primary AP: {scores.get('primary_ap', 'N/A')}
- Sub-AP: {scores.get('sub_ap', 'N/A')}
- CDETS quality score: {scores.get('cdets_defect_score', 'N/A')}
- Coverage: {scores.get('test_coverage_confidence', 'N/A')} ({cafy_verdict})

Evidence:
---
{cdets_raw[:6000]}
---

Write a note (<= 200 words) with: Suspected Component/AP, Failure Summary,
Test Coverage status, and 2-4 Recommended Next Steps. Plain text only."""
    return llm.generate(_ENRICH_SYS, prompt, max_tokens=900)
