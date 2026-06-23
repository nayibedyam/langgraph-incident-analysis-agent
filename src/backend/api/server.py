#!/usr/bin/env python3
"""FL Agent Console — self-contained backend.

A small FastAPI service that powers the FL Agent web console. It reads the
per-CDETS Feedback Loop artifacts written by the agent
(``cdets_data/<CDETS-ID>/``) directly from disk, parses the Score
Summary Box for headline metrics, and can trigger the deterministic FL prescan
(CDETS fetch + AP/SubAP resolution + CaFy coverage) for a new CDETS ID.

Run with ``src/`` on the import path so ``backend`` is importable::

    .venv/bin/python -m uvicorn backend.api.server:app --app-dir src --port 8800

Endpoints (all under /api):
    GET  /api/stats                      aggregate overview metrics
    GET  /api/artifacts                  list analyzed defects (summaries)
    GET  /api/artifacts/{id}             one defect: summary + file list
    GET  /api/artifacts/{id}/file?name=  raw content of one artifact file
    POST /api/artifacts/analyze          run prescan for a CDETS ID
    GET  /api/health                     liveness probe
"""

from __future__ import annotations

import os
import re
import shutil
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
REPO_ROOT = Path(__file__).resolve().parents[3]
ARTIFACT_ROOT = Path(
    os.getenv("FL_ARTIFACTS_DIR", str(REPO_ROOT / "cdets_data"))
)

_CDETS_RE = re.compile(r"^CSC[a-zA-Z]{2}[0-9]{5}$")
_HIDDEN_FILES = {"cdets_raw.txt"}
_HIDDEN_SUFFIXES = ("_trace_index.json",)
ArtifactKind = Literal["markdown", "json", "text"]


def _canonical_cdets_id(raw: str) -> str:
    """Normalize a CDETS ID to canonical casing.

    CDETS IDs are case-sensitive: ``CSC`` is upper-case, the two product
    letters that follow are lower-case, then five digits (e.g. ``CSCwu19710``).
    Force-upper-casing the whole token breaks the CDETS lookup, so only the
    ``CSC`` prefix is upper-cased and the product letters are lower-cased.
    """
    s = (raw or "").strip()
    if len(s) >= 5 and s[:3].upper() == "CSC":
        return "CSC" + s[3:5].lower() + s[5:]
    return s


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class ArtifactScores(BaseModel):
    cdets_defect_score: Optional[str] = None
    ai_confidence: Optional[str] = None
    automation_readiness: Optional[str] = None
    test_coverage_confidence: Optional[str] = None
    coverage_gap: Optional[str] = None
    source: Optional[str] = None
    primary_ap: Optional[str] = None
    sub_ap: Optional[str] = None


class ArtifactFile(BaseModel):
    name: str
    kind: ArtifactKind
    size: int


class Priority(BaseModel):
    priority: str = "P4"
    score: int = 0
    rationale: str = ""


class ArtifactSummary(BaseModel):
    cdets_id: str
    headline: Optional[str] = None
    file_count: int = 0
    updated_at: Optional[str] = None
    scores: ArtifactScores = Field(default_factory=ArtifactScores)
    severity: Optional[str] = None
    regression: bool = False
    priority: Priority = Field(default_factory=Priority)


class ArtifactDetail(ArtifactSummary):
    files: list[ArtifactFile] = Field(default_factory=list)


class ArtifactFileContent(BaseModel):
    cdets_id: str
    name: str
    kind: ArtifactKind
    content: str


class AnalyzeRequest(BaseModel):
    cdets_id: str = Field(..., min_length=8)


class AnalyzeResult(BaseModel):
    cdets_id: str
    status: Literal["ok", "error"]
    message: str
    component: Optional[str] = None
    primary_ap: Optional[str] = None
    sub_ap: Optional[str] = None
    cafy_verdict: Optional[str] = None
    file_count: int = 0


class Stats(BaseModel):
    total_defects: int
    analyzed_with_scores: int
    avg_cdets_score: Optional[float] = None
    score_grades: dict[str, int] = Field(default_factory=dict)
    coverage_buckets: dict[str, int] = Field(default_factory=dict)
    ap_distribution: dict[str, int] = Field(default_factory=dict)
    coverage_gaps: dict[str, int] = Field(default_factory=dict)
    priority_buckets: dict[str, int] = Field(default_factory=dict)


class SimilarDefect(BaseModel):
    cdets_id: str
    headline: Optional[str] = None
    component: Optional[str] = None
    similarity: int = 0
    primary_ap: Optional[str] = None
    cdets_defect_score: Optional[str] = None


class SimilarResponse(BaseModel):
    cdets_id: str
    neighbors: list[SimilarDefect] = Field(default_factory=list)


class MarkdownResponse(BaseModel):
    cdets_id: str
    markdown: str
    error: Optional[str] = None


class ChatRequest(BaseModel):
    cdets_id: str
    question: str = Field(..., min_length=1)


class ChatResponse(BaseModel):
    cdets_id: str
    answer: str
    error: Optional[str] = None


class LogTriageRequest(BaseModel):
    log_text: str = Field(..., min_length=1)


class LogTriageResponse(BaseModel):
    markdown: str
    matched_ids: list[str] = Field(default_factory=list)
    error: Optional[str] = None


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #
def _kind_for(name: str) -> ArtifactKind:
    lower = name.lower()
    if lower.endswith(".md"):
        return "markdown"
    if lower.endswith(".json"):
        return "json"
    return "text"


def _is_visible(path: Path) -> bool:
    if not path.is_file():
        return False
    if path.name in _HIDDEN_FILES:
        return False
    if any(path.name.endswith(suffix) for suffix in _HIDDEN_SUFFIXES):
        return False
    return path.suffix.lower() in {".md", ".json", ".txt"}


def _defect_dir(cdets_id: str) -> Path:
    if not _CDETS_RE.match(cdets_id):
        raise HTTPException(status_code=400, detail="Invalid CDETS ID format")
    root = ARTIFACT_ROOT.resolve()
    candidate = (root / cdets_id).resolve()
    if root not in candidate.parents and candidate != root:
        raise HTTPException(status_code=403, detail="Path outside artifacts root")
    if not candidate.is_dir():
        raise HTTPException(status_code=404, detail=f"No artifacts for {cdets_id}")
    return candidate


def _parse_score_box(text: str) -> tuple[Optional[str], ArtifactScores]:
    headline: Optional[str] = None
    title_match = re.search(
        r"^\s*`?CSC[a-zA-Z]{2}[0-9]{5}\s*:\s*(.+?)`?\s*$", text, re.MULTILINE
    )
    if title_match:
        headline = title_match.group(1).strip().strip("`").strip()

    def grab(label: str) -> Optional[str]:
        m = re.search(rf"{re.escape(label)}\s*:\s*(.+?)\s*\|", text)
        return m.group(1).strip() if m else None

    return headline, ArtifactScores(
        cdets_defect_score=grab("CDETS Defect Score"),
        ai_confidence=grab("AI Confidence"),
        automation_readiness=grab("Automation Readiness"),
        test_coverage_confidence=grab("TestCoverageConfidence"),
        coverage_gap=grab("Coverage Gap"),
        source=grab("Source"),
        primary_ap=grab("Primary AP"),
        sub_ap=grab("Sub-AP"),
    )


def _read_summary_text(defect_dir: Path, cdets_id: str) -> str:
    parts: list[str] = []
    for candidate in (
        defect_dir / f"{cdets_id}-Scorecard.md",
        defect_dir / f"AI-FL-{cdets_id}_TestCase.md",
    ):
        if candidate.is_file():
            try:
                parts.append(candidate.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
    return "\n".join(parts)


def _read_schema_meta(defect_dir: Path, cdets_id: str) -> tuple[str, str, bool]:
    """Return (component, severity, regression) from the defect schema JSON."""
    schema_path = defect_dir / f"{cdets_id}_Cdets_Schema_Template.json"
    if not schema_path.is_file():
        return "", "", False
    try:
        import json

        data = json.loads(schema_path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return "", "", False
    defect = data.get("defect", {}) or {}
    comp = ""
    try:
        comp = defect["component"]["technology"]["tags"][0]["name"]
    except (KeyError, IndexError, TypeError):
        comp = ""
    severity = ""
    try:
        severity = defect["behavior"]["impact"]["severity"]
    except (KeyError, TypeError):
        severity = ""
    blob = json.dumps(data).lower()
    regression = "regression" in blob or "related_cfd" in blob
    return comp or "", severity or "", regression


def _load_summary_json(defect_dir: Path, cdets_id: str) -> dict:
    """Load the structured ``<id>_summary.json`` written by the delivery node.

    Returns ``{}`` when absent or unreadable (legacy defects).
    """
    import json

    path = defect_dir / f"{cdets_id}_summary.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _scores_from_summary(
    summary: dict,
) -> tuple[Optional[str], ArtifactScores, str, str]:
    """Map a structured run summary into (headline, scores, component, severity).

    This is the authoritative source for the artifacts list and the overview
    stats — it carries the real numbers (score, AI confidence, coverage) that
    the free-text Scorecard markdown box does not reliably expose.
    """
    sc = summary.get("scorecard") or {}
    cov = summary.get("coverage") or {}
    ba = summary.get("bug_analysis") or {}

    def pct(v: object) -> Optional[str]:
        if v is None:
            return None
        try:
            return f"{float(v):.1f}%"
        except (TypeError, ValueError):
            return str(v)

    grade = sc.get("grade")
    cdets_defect_score = None
    if sc.get("score_value") is not None:
        cdets_defect_score = pct(sc.get("score_value"))
        if grade:
            cdets_defect_score = f"{cdets_defect_score} ({grade})"

    ai = sc.get("ai_confidence") or {}
    ai_conf = None
    if ai.get("overall_percent") is not None:
        ai_conf = pct(ai.get("overall_percent"))
        if ai.get("grade"):
            ai_conf = f"{ai_conf} / {ai.get('grade')}"

    automation = (sc.get("automation_readiness") or {}).get("verdict")

    # Build a coverage label the stats bucketer + priority scorer understand.
    existing = cov.get("existing_tests_count")
    new_sc = cov.get("new_scenarios_count") or 0
    tcg = (cov.get("test_coverage_grade") or "").upper()
    tcc = cov.get("test_coverage_confidence")
    base: Optional[str] = None
    if existing == 0 and new_sc > 0:
        base = "No Coverage"
    elif tcg in ("A", "B"):
        base = "Fully Covered"
    elif tcg in ("C", "D"):
        base = "Partial"
    elif tcg == "F":
        base = "No Coverage"
    cov_label: Optional[str] = base
    if tcc is not None:
        cov_label = f"{base} ({pct(tcc)})" if base else pct(tcc)

    headline = ba.get("headline") or None
    component = ba.get("component") or ""
    severity = str(ba.get("severity") or "")

    scores = ArtifactScores(
        cdets_defect_score=cdets_defect_score,
        ai_confidence=ai_conf,
        automation_readiness=automation or None,
        test_coverage_confidence=cov_label,
        coverage_gap=(cov.get("coverage_gap") or cov.get("gap_classification") or None),
        source=summary.get("model_used") or None,
        primary_ap=(ba.get("primary_ap") or component or None),
        sub_ap=(ba.get("sub_ap") or None),
    )
    return headline, scores, component, severity


def _build_summary(defect_dir: Path, cdets_id: str) -> ArtifactSummary:
    files = [p for p in defect_dir.iterdir() if _is_visible(p)]
    summary_json = _load_summary_json(defect_dir, cdets_id)
    if summary_json:
        headline, scores, component, severity = _scores_from_summary(summary_json)
        # Regression flag still comes from the raw schema (not in the summary).
        _, _, regression = _read_schema_meta(defect_dir, cdets_id)
    else:
        # Legacy defects (analyzed before the structured summary existed):
        # fall back to parsing the Scorecard markdown + schema.
        headline, scores = _parse_score_box(_read_summary_text(defect_dir, cdets_id))
        component, severity, regression = _read_schema_meta(defect_dir, cdets_id)
    updated_at = None
    if files:
        newest = max(p.stat().st_mtime for p in files)
        updated_at = datetime.fromtimestamp(newest, tz=timezone.utc).isoformat()
    from .agents import compute_priority

    prio = compute_priority(scores.model_dump(), severity=severity, regression=regression)
    return ArtifactSummary(
        cdets_id=cdets_id,
        headline=headline,
        file_count=len(files),
        updated_at=updated_at,
        scores=scores,
        severity=severity or None,
        regression=regression,
        priority=Priority(**prio),
    )


def _all_summaries() -> list[ArtifactSummary]:
    if not ARTIFACT_ROOT.is_dir():
        return []
    out: list[ArtifactSummary] = []
    for child in ARTIFACT_ROOT.iterdir():
        if not child.is_dir() or not _CDETS_RE.match(child.name):
            continue
        if not any(_is_visible(p) for p in child.iterdir()):
            continue
        out.append(_build_summary(child, child.name))
    out.sort(key=lambda s: s.updated_at or "", reverse=True)
    return out


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #
app = FastAPI(title="FL Agent Console API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def _maybe_enable_phoenix_tracing() -> None:
    """Enable Arize Phoenix tracing when PHOENIX_TRACING is set.

    Captures every pipeline run triggered from the console as an OpenTelemetry
    trace. Set PHOENIX_COLLECTOR_ENDPOINT to export to a shared collector, or
    leave it unset to launch a local Phoenix UI (http://localhost:6006).
    """
    try:
        from dotenv import load_dotenv
        from eval.tracing import setup_phoenix_tracing, tracing_enabled_from_env

        load_dotenv()
        if tracing_enabled_from_env():
            setup_phoenix_tracing()
    except Exception:  # noqa: BLE001 - tracing must never block the API
        pass


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "artifacts_root": str(ARTIFACT_ROOT)}


@app.get("/api/stats", response_model=Stats)
async def stats() -> Stats:
    summaries = _all_summaries()
    grades: Counter[str] = Counter()
    coverage: Counter[str] = Counter()
    aps: Counter[str] = Counter()
    gaps: Counter[str] = Counter()
    priorities: Counter[str] = Counter()
    scored = 0
    score_total = 0.0

    for s in summaries:
        sc = s.scores
        priorities[s.priority.priority] += 1
        if sc.cdets_defect_score:
            m = re.search(r"(\d+)", sc.cdets_defect_score)
            grade = re.search(r"\(([^)]+)\)", sc.cdets_defect_score)
            if m:
                score_total += int(m.group(1))
                scored += 1
            if grade:
                grades[grade.group(1)] += 1
        if sc.test_coverage_confidence:
            low = sc.test_coverage_confidence.lower()
            if "fully" in low or "(a)" in low or "(b)" in low:
                coverage["Fully Covered"] += 1
            elif "no coverage" in low or "(f)" in low:
                coverage["No Coverage"] += 1
            elif "partial" in low or "needs review" in low or "(c)" in low or "(d)" in low:
                coverage["Partial"] += 1
            else:
                coverage["Other"] += 1
        if sc.primary_ap:
            aps[sc.primary_ap] += 1
        if sc.coverage_gap:
            gaps[sc.coverage_gap] += 1

    return Stats(
        total_defects=len(summaries),
        analyzed_with_scores=scored,
        avg_cdets_score=round(score_total / scored, 1) if scored else None,
        score_grades=dict(grades),
        coverage_buckets=dict(coverage),
        ap_distribution=dict(aps),
        coverage_gaps=dict(gaps),
        priority_buckets=dict(priorities),
    )


@app.get("/api/artifacts", response_model=list[ArtifactSummary])
async def list_artifacts() -> list[ArtifactSummary]:
    return _all_summaries()


@app.get("/api/artifacts/{cdets_id}", response_model=ArtifactDetail)
async def get_artifact(cdets_id: str) -> ArtifactDetail:
    defect_dir = _defect_dir(cdets_id)
    summary = _build_summary(defect_dir, cdets_id)
    files = sorted(
        (
            ArtifactFile(name=p.name, kind=_kind_for(p.name), size=p.stat().st_size)
            for p in defect_dir.iterdir()
            if _is_visible(p)
        ),
        key=lambda f: f.name,
    )
    return ArtifactDetail(**summary.model_dump(), files=files)


@app.delete("/api/artifacts/{cdets_id}")
async def delete_artifact(cdets_id: str) -> dict:
    """Delete all on-disk artifacts for a CDETS (removes its cache too)."""
    cdets_id = _canonical_cdets_id(cdets_id)
    defect_dir = _defect_dir(cdets_id)  # validates format + path safety + existence
    try:
        shutil.rmtree(defect_dir)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to delete: {exc}") from exc
    return {"ok": True, "cdets_id": cdets_id, "deleted": True}


@app.get("/api/artifacts/{cdets_id}/file", response_model=ArtifactFileContent)
async def get_artifact_file(
    cdets_id: str,
    name: str = Query(..., description="Artifact file name within the defect folder"),
) -> ArtifactFileContent:
    defect_dir = _defect_dir(cdets_id)
    if "/" in name or "\\" in name or name in {"", ".", ".."}:
        raise HTTPException(status_code=400, detail="Invalid file name")
    target = (defect_dir / name).resolve()
    if target.parent != defect_dir.resolve():
        raise HTTPException(status_code=403, detail="Path outside defect folder")
    if not _is_visible(target):
        raise HTTPException(status_code=404, detail="File not found")
    try:
        content = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return ArtifactFileContent(
        cdets_id=cdets_id, name=name, kind=_kind_for(name), content=content
    )


def _fetch_cdets_raw(cdets_id: str) -> str:
    """Fetch raw CDETS field + note text via dumpcr for LLM evidence.

    Best-effort; returns an empty string if the CLI is unavailable. The
    submitter description and engineering notes carry the RCA the LLM needs.
    """
    import subprocess

    dumpcr = "/path/to/dumpcr"
    if not Path(dumpcr).exists():
        return ""
    try:
        proc = subprocess.run(
            [dumpcr, "-d", cdets_id],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            stdin=subprocess.DEVNULL,
        )
    except Exception:  # noqa: BLE001
        return ""
    return proc.stdout if proc.returncode == 0 else ""


def _ensure_cafy_rca(out_dir, cdets_id: str, ps: dict, cf: dict) -> None:
    """Guarantee CaFy RCA artifacts exist for every analyzed defect.

    ``run_cafy_prescan`` returns early without writing anything when the defect
    component cannot be mapped to an AP (or the CaFy coverage script fails). In
    that case we still want a JSON + Markdown RCA so the console renders a
    consistent set of artifacts. Existing CaFy output (when the script ran) is
    never overwritten.
    """
    import json

    json_path = out_dir / f"{cdets_id}_cafy_rca.json"
    md_path = out_dir / f"AI-FL-{cdets_id}_cafy_rca.md"
    if json_path.exists() and md_path.exists():
        return

    ap = ps.get("ap", "") or ""
    subap = ps.get("subap", "") or ""
    component = ps.get("component", "") or ""
    headline = ps.get("headline", "") or ""
    verdict = cf.get("verdict", "") or "UNDETERMINED"
    reason = (
        "AP could not be resolved from the defect component; CaFy coverage "
        "analysis was skipped."
        if not ap
        else "CaFy coverage analysis did not produce a result."
    )

    if not json_path.exists():
        data = {
            "bug_id": cdets_id,
            "ap": ap,
            "subap": subap,
            "component": component,
            "headline": headline,
            "verdict": verdict,
            "confidence": cf.get("confidence", "") or "LOW",
            "recommendation": cf.get("recommendation", "") or reason,
            "total_methods_scanned": cf.get("methods_scanned", 0) or 0,
            "predicted_subap": cf.get("predicted_subap", "") or "",
            "subap_rationale": cf.get("subap_rationale", "") or "",
            "ran": bool(cf.get("ran")),
            "fallback": True,
            "fallback_reason": reason,
        }
        json_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    if not md_path.exists():
        md = (
            f"# CaFy RCA — {cdets_id}\n\n"
            f"**Verdict:** {verdict}\n\n"
            f"**AP:** {ap or '_unresolved_'}\n\n"
            f"**Sub-AP:** {subap or '_n/a_'}\n\n"
            f"**Component:** {component or '_unknown_'}\n\n"
            f"**Headline:** {headline or '_n/a_'}\n\n"
            "## Coverage Analysis\n\n"
            f"{reason}\n\n"
            "> This is a fallback RCA generated because automated CaFy "
            "coverage analysis could not run for this defect. Map the "
            "component to an AP to enable full coverage analysis.\n"
        )
        md_path.write_text(md, encoding="utf-8")


def _run_prescan_sync(cdets_id: str) -> AnalyzeResult:
    """Run the deterministic FL prescan in-process (called via threadpool)."""
    try:
        from FL_agent.core.prescan import run_prescan, run_cafy_prescan
    except Exception as exc:  # pragma: no cover - import/runtime guard
        return AnalyzeResult(
            cdets_id=cdets_id,
            status="error",
            message=f"FL Agent prescan unavailable: {exc}",
        )

    ps = run_prescan(cdets_id)
    if ps.get("prescan_error"):
        return AnalyzeResult(cdets_id=cdets_id, status="error", message=ps["prescan_error"])
    if not ps.get("cdets_fields_available"):
        return AnalyzeResult(
            cdets_id=cdets_id,
            status="error",
            message="CDETS lookup failed or returned no fields",
        )

    cf = run_cafy_prescan(
        cdets_id,
        ps.get("ap", ""),
        ps.get("subap", ""),
        component=ps.get("component", ""),
        headline=ps.get("headline", ""),
    )

    out_dir = ARTIFACT_ROOT / cdets_id
    out_dir.mkdir(parents=True, exist_ok=True)

    # Fallback: run_cafy_prescan skips silently when the component does not map
    # to an AP (or the CaFy script fails), leaving no RCA artifacts. Always
    # emit a minimal CaFy RCA JSON + Markdown so the console can render them.
    _ensure_cafy_rca(out_dir, cdets_id, ps, cf)
    schema_path = out_dir / f"{cdets_id}_Cdets_Schema_Template.json"
    if not schema_path.exists():
        import json

        schema = {
            "schema_version": "1.1",
            "template_name": "IOS-XR_Feedback_Loop_Defect_Intake",
            "meta": {"source_system": {"type": "BUG_TRACKER", "name": "CDETS",
                                       "issue_id": cdets_id}},
            "defect": {
                "summary": ps.get("headline", ""),
                "component": {"technology": {"tags": [
                    {"name": ps.get("component", ""), "class": "PRIMARY"}]}},
                "ap_selection": {"primary_ap": ps.get("ap", ""),
                                 "sub_ap": ps.get("subap", "")},
                "platform": {"family": ps.get("product", ""),
                             "topology": {"pod_type": ps.get("topology", "")}},
                "versions": {"submitted": {"release_train": ps.get("version", "")},
                             "fixed": {"to_be_fixed": ps.get("to_be_fixed", "")}},
                "behavior": {"impact": {"severity": ps.get("severity", ""),
                                        "priority": ps.get("priority", "")}},
            },
        }
        schema_path.write_text(json.dumps(schema, indent=2), encoding="utf-8")

    file_count = (
        sum(1 for p in out_dir.iterdir() if _is_visible(p)) if out_dir.is_dir() else 0
    )
    return AnalyzeResult(
        cdets_id=cdets_id,
        status="ok",
        message="Prescan complete. Generating scorecard + test case…",
        component=ps.get("component", ""),
        primary_ap=ps.get("ap", ""),
        sub_ap=ps.get("subap", ""),
        cafy_verdict=cf.get("verdict", ""),
        file_count=file_count,
    )


# In-progress LLM generation tracking (cdets_id -> running) so the UI can show
# a "generating" state while the slow LLM step finishes in the background.
_GENERATING: set[str] = set()


def _generate_reasoning_bg(cdets_id: str) -> None:
    """Background worker: author scorecard + test case via the LLM."""
    try:
        from .generate import generate_reasoning_artifacts

        out_dir = ARTIFACT_ROOT / cdets_id
        ps = {}
        cf_verdict = ""
        cafy_path = out_dir / f"{cdets_id}_cafy_rca.json"
        if cafy_path.is_file():
            try:
                import json

                cf_verdict = json.loads(cafy_path.read_text(encoding="utf-8")).get("verdict", "")
            except (OSError, ValueError):
                cf_verdict = ""
        # Recover AP/component/subap from the schema written during prescan.
        comp, _sev, _reg = _read_schema_meta(out_dir, cdets_id)
        schema_path = out_dir / f"{cdets_id}_Cdets_Schema_Template.json"
        primary_ap = sub_ap = ""
        if schema_path.is_file():
            try:
                import json

                data = json.loads(schema_path.read_text(encoding="utf-8"))
                sel = data.get("defect", {}).get("ap_selection", {})
                primary_ap = sel.get("primary_ap", "")
                sub_ap = sel.get("sub_ap", "")
            except (OSError, ValueError):
                pass
        cdets_raw = _fetch_cdets_raw(cdets_id)
        generate_reasoning_artifacts(
            cdets_id,
            out_dir,
            cdets_raw=cdets_raw,
            component=comp,
            primary_ap=primary_ap,
            sub_ap=sub_ap,
            cafy_verdict=cf_verdict,
        )
    except Exception:  # noqa: BLE001 - background task must never raise
        pass
    finally:
        _GENERATING.discard(cdets_id)


@app.post("/api/artifacts/analyze", response_model=AnalyzeResult)
async def analyze(request: AnalyzeRequest, background: BackgroundTasks) -> AnalyzeResult:
    cdets_id = _canonical_cdets_id(request.cdets_id)
    if not _CDETS_RE.match(cdets_id):
        raise HTTPException(status_code=400, detail="Invalid CDETS ID format")
    # Fast path: run the deterministic prescan and return immediately.
    result = await run_in_threadpool(_run_prescan_sync, cdets_id)
    # Slow path: author scorecard + test case in the background; the UI polls.
    if result.status == "ok":
        _GENERATING.add(cdets_id)
        background.add_task(_generate_reasoning_bg, cdets_id)
    return result


@app.get("/api/artifacts/{cdets_id}/status")
async def generation_status(cdets_id: str) -> dict:
    cdets_id = _canonical_cdets_id(cdets_id)
    return {"cdets_id": cdets_id, "generating": cdets_id in _GENERATING}


# --------------------------------------------------------------------------- #
# LLM agent endpoints
# --------------------------------------------------------------------------- #
def _neighbors_for(cdets_id: str, limit: int = 5) -> list[SimilarDefect]:
    """Rank all other analyzed defects by similarity to the target."""
    from .agents import similarity_score

    target_dir = _defect_dir(cdets_id)
    target = _build_summary(target_dir, cdets_id)
    tcomp, _, _ = _read_schema_meta(target_dir, cdets_id)
    target_d = {
        "component": tcomp,
        "headline": target.headline or "",
        "scores": target.scores.model_dump(),
    }
    out: list[SimilarDefect] = []
    for s in _all_summaries():
        if s.cdets_id == cdets_id:
            continue
        comp, _, _ = _read_schema_meta(ARTIFACT_ROOT / s.cdets_id, s.cdets_id)
        cand_d = {
            "component": comp,
            "headline": s.headline or "",
            "scores": s.scores.model_dump(),
        }
        sim = similarity_score(target_d, cand_d)
        if sim > 0:
            out.append(
                SimilarDefect(
                    cdets_id=s.cdets_id,
                    headline=s.headline,
                    component=comp or None,
                    similarity=sim,
                    primary_ap=s.scores.primary_ap,
                    cdets_defect_score=s.scores.cdets_defect_score,
                )
            )
    out.sort(key=lambda n: n.similarity, reverse=True)
    return out[:limit]


@app.get("/api/artifacts/{cdets_id}/similar", response_model=SimilarResponse)
async def similar(cdets_id: str) -> SimilarResponse:
    cdets_id = _canonical_cdets_id(cdets_id)
    neighbors = await run_in_threadpool(_neighbors_for, cdets_id, 6)
    return SimilarResponse(cdets_id=cdets_id, neighbors=neighbors)


def _defect_context(cdets_id: str) -> str:
    """Concatenate a defect's artifacts as LLM context."""
    defect_dir = _defect_dir(cdets_id)
    parts: list[str] = []
    for p in sorted(defect_dir.iterdir()):
        if _is_visible(p):
            try:
                parts.append(f"### {p.name}\n" + p.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
    return "\n\n".join(parts)


def _remediation_sync(cdets_id: str) -> MarkdownResponse:
    from .agents import remediation_agent
    from .llm import GatewayLLM, LLMError

    llm = GatewayLLM()
    if not llm.available:
        return MarkdownResponse(cdets_id=cdets_id, markdown="", error="LLM not configured")
    summary = _build_summary(_defect_dir(cdets_id), cdets_id)
    neighbors = [n.model_dump() for n in _neighbors_for(cdets_id, 5)]
    cdets_raw = _fetch_cdets_raw(cdets_id)
    try:
        md = remediation_agent(
            llm,
            cdets_id=cdets_id,
            headline=summary.headline or "",
            cdets_raw=cdets_raw,
            neighbors=neighbors,
        )
    except LLMError as exc:
        return MarkdownResponse(cdets_id=cdets_id, markdown="", error=str(exc))
    return MarkdownResponse(cdets_id=cdets_id, markdown=md)


@app.get("/api/artifacts/{cdets_id}/remediation", response_model=MarkdownResponse)
async def remediation(cdets_id: str) -> MarkdownResponse:
    cdets_id = _canonical_cdets_id(cdets_id)
    _defect_dir(cdets_id)
    return await run_in_threadpool(_remediation_sync, cdets_id)


def _enrich_sync(cdets_id: str) -> MarkdownResponse:
    from .agents import enrichment_agent
    from .llm import GatewayLLM, LLMError

    llm = GatewayLLM()
    if not llm.available:
        return MarkdownResponse(cdets_id=cdets_id, markdown="", error="LLM not configured")
    summary = _build_summary(_defect_dir(cdets_id), cdets_id)
    cdets_raw = _fetch_cdets_raw(cdets_id)
    cf_verdict = ""
    cafy_path = _defect_dir(cdets_id) / f"{cdets_id}_cafy_rca.json"
    if cafy_path.is_file():
        try:
            import json

            cf_verdict = json.loads(cafy_path.read_text(encoding="utf-8")).get("verdict", "")
        except (OSError, ValueError):
            cf_verdict = ""
    try:
        note = enrichment_agent(
            llm,
            cdets_id=cdets_id,
            scores=summary.scores.model_dump(),
            cafy_verdict=cf_verdict,
            cdets_raw=cdets_raw,
        )
    except LLMError as exc:
        return MarkdownResponse(cdets_id=cdets_id, markdown="", error=str(exc))
    return MarkdownResponse(cdets_id=cdets_id, markdown=note)


@app.get("/api/artifacts/{cdets_id}/enrichment", response_model=MarkdownResponse)
async def enrichment(cdets_id: str) -> MarkdownResponse:
    cdets_id = _canonical_cdets_id(cdets_id)
    _defect_dir(cdets_id)
    return await run_in_threadpool(_enrich_sync, cdets_id)


def _chat_sync(cdets_id: str, question: str) -> ChatResponse:
    from .agents import chat_agent
    from .llm import GatewayLLM, LLMError

    llm = GatewayLLM()
    if not llm.available:
        return ChatResponse(cdets_id=cdets_id, answer="", error="LLM not configured")
    context = _defect_context(cdets_id)
    try:
        answer = chat_agent(llm, question=question, context=context)
    except LLMError as exc:
        return ChatResponse(cdets_id=cdets_id, answer="", error=str(exc))
    return ChatResponse(cdets_id=cdets_id, answer=answer)


@app.post("/api/artifacts/{cdets_id}/chat", response_model=ChatResponse)
async def chat(cdets_id: str, request: ChatRequest) -> ChatResponse:
    cdets_id = _canonical_cdets_id(cdets_id)
    _defect_dir(cdets_id)
    return await run_in_threadpool(_chat_sync, cdets_id, request.question)


_SIGNATURE_RE = re.compile(r"%[A-Z0-9_]+-\d-[A-Z0-9_]+")


def _log_triage_sync(log_text: str) -> LogTriageResponse:
    from .agents import log_triage_agent, _tokens
    from .llm import GatewayLLM, LLMError

    llm = GatewayLLM()
    if not llm.available:
        return LogTriageResponse(markdown="", error="LLM not configured")

    # Find analyzed defects whose text overlaps the pasted log.
    log_toks = _tokens(log_text)
    sigs = set(_SIGNATURE_RE.findall(log_text))
    candidates: list[dict] = []
    for s in _all_summaries():
        ctx_dir = ARTIFACT_ROOT / s.cdets_id
        hay = (s.headline or "").lower()
        try:
            sc_file = ctx_dir / f"{s.cdets_id}-Scorecard.md"
            if sc_file.is_file():
                hay += " " + sc_file.read_text(encoding="utf-8", errors="replace").lower()
        except OSError:
            pass
        overlap = len(log_toks & _tokens(hay))
        sig_hit = any(sig.lower() in hay for sig in sigs)
        if sig_hit or overlap >= 3:
            candidates.append({"cdets_id": s.cdets_id, "headline": s.headline or "",
                               "_rank": (10 if sig_hit else 0) + overlap})
    candidates.sort(key=lambda c: c["_rank"], reverse=True)
    candidates = candidates[:6]
    try:
        md = log_triage_agent(llm, log_text=log_text, candidates=candidates)
    except LLMError as exc:
        return LogTriageResponse(markdown="", error=str(exc))
    return LogTriageResponse(markdown=md, matched_ids=[c["cdets_id"] for c in candidates])


@app.post("/api/triage/log", response_model=LogTriageResponse)
async def triage_log(request: LogTriageRequest) -> LogTriageResponse:
    return await run_in_threadpool(_log_triage_sync, request.log_text)


# --------------------------------------------------------------------------- #
# LangGraph pipeline jobs with live progress (SSE)
# --------------------------------------------------------------------------- #
class StartJobRequest(BaseModel):
    cdets_id: str = Field(..., min_length=8)
    dry_run: bool = False
    force: bool = False
    model: Optional[Literal["sonnet", "opus"]] = None


class StartJobResponse(BaseModel):
    job_id: str
    cdets_id: str
    status: str
    cached: bool = False
    model: Optional[str] = None


@app.post("/api/jobs", response_model=StartJobResponse)
async def start_job(request: StartJobRequest) -> StartJobResponse:
    """Kick off the LangGraph pipeline for one CDETS ID and return a job_id.

    If the CDETS has already been analyzed (artifacts on disk) and neither
    ``force`` nor a per-run ``model`` override is set, the run is served from
    the local cache instead of re-running.
    """
    cdets_id = _canonical_cdets_id(request.cdets_id)
    if not _CDETS_RE.match(cdets_id):
        raise HTTPException(status_code=400, detail="Invalid CDETS ID format")
    from .runner import cached_result, start_pipeline_job

    is_cached = (
        not request.force
        and not request.model
        and cached_result(cdets_id) is not None
    )
    job = start_pipeline_job(
        cdets_id,
        dry_run=request.dry_run,
        force=request.force,
        model_override=request.model,
    )
    return StartJobResponse(
        job_id=job.job_id,
        cdets_id=cdets_id,
        status=job.status,
        cached=is_cached,
        model=job.model_override,
    )


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str) -> dict:
    from .jobs import registry

    job = registry.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job.to_dict()


@app.get("/api/jobs")
async def list_jobs() -> list[dict]:
    from .jobs import registry

    return [j.to_dict() for j in registry.all()]


@app.get("/api/jobs/{job_id}/events")
async def stream_job_events(job_id: str) -> StreamingResponse:
    """Server-Sent Events stream of NodeEvent JSON, one per pipeline node."""
    from .jobs import registry
    import asyncio
    import json

    job = registry.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")

    async def event_gen():
        # Send an initial snapshot so the client can render immediately.
        yield f"event: snapshot\ndata: {json.dumps(job.to_dict())}\n\n"
        async for ev in registry.subscribe(job):
            yield f"data: {json.dumps(ev.to_dict())}\n\n"
        # Final "done" frame carries the result summary.
        yield (
            "event: done\n"
            f"data: {json.dumps({'status': job.status, 'error': job.error, 'result': job.result})}\n\n"
        )

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# --------------------------------------------------------------------------- #
# Post-run summary (bug analysis + scorecard + coverage rollups)
# --------------------------------------------------------------------------- #
@app.get("/api/artifacts/{cdets_id}/summary")
async def get_artifact_summary(cdets_id: str) -> dict:
    """Structured post-run summary: bug analysis + scorecard + coverage.

    Reads ``<cdets_id>_summary.json`` if present. Falls back to deriving a
    thin summary from the schema JSON (for older defects).
    """
    import json

    cdets_id = _canonical_cdets_id(cdets_id)
    defect_dir = _defect_dir(cdets_id)
    summary_path = defect_dir / f"{cdets_id}_summary.json"
    if summary_path.is_file():
        try:
            return json.loads(summary_path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=500, detail=f"failed to read summary: {exc}") from exc

    # Fall back to deriving on the fly from the schema JSON.
    import sys
    src_root = Path(__file__).resolve().parents[2]
    if str(src_root) not in sys.path:
        sys.path.insert(0, str(src_root))
    try:
        from backend.pipeline.utils_summary import derive_summary_from_disk
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"summary module load failed: {exc}") from exc
    fallback = derive_summary_from_disk(defect_dir, cdets_id)
    if not fallback:
        raise HTTPException(status_code=404, detail="No summary available for this defect")
    return fallback


# --------------------------------------------------------------------------- #
# Human-in-the-loop: review request + resume
# --------------------------------------------------------------------------- #
class ResumeJobRequest(BaseModel):
    human_input: dict = Field(default_factory=dict)


@app.get("/api/jobs/{job_id}/review")
async def get_review_request(job_id: str) -> dict:
    """Return the missing-info request payload for a paused job."""
    from .jobs import registry

    job = registry.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    if job.status != "awaiting_human" or not job.missing_info_request:
        raise HTTPException(status_code=404, detail="job is not awaiting human input")
    return {
        "job_id": job.job_id,
        "cdets_id": job.cdets_id,
        "status": job.status,
        "review_url": job.review_url,
        "missing_info_request": job.missing_info_request,
    }


@app.post("/api/jobs/{job_id}/resume", response_model=StartJobResponse)
async def resume_job(job_id: str, request: ResumeJobRequest) -> StartJobResponse:
    """Resume a paused job after a reviewer has supplied missing details."""
    from .runner import resume_pipeline_job

    try:
        job = await resume_pipeline_job(job_id, request.human_input or {})
    except KeyError:
        raise HTTPException(status_code=404, detail="job not found")
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return StartJobResponse(
        job_id=job.job_id,
        cdets_id=job.cdets_id,
        status=job.status,
        model=job.model_override,
    )


@app.get("/api/artifacts/{cdets_id}/missing_info")
async def get_missing_info(cdets_id: str) -> dict:
    """Read the most recent ``<cdets>_missing_info_request.json`` from disk."""
    import json

    cdets_id = _canonical_cdets_id(cdets_id)
    candidate = _defect_dir(cdets_id) / f"{cdets_id}_missing_info_request.json"
    if not candidate.exists():
        raise HTTPException(status_code=404, detail="no missing-info request on disk")
    try:
        return json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=500, detail=f"failed to read: {exc}")
