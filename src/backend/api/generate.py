"""LLM-backed generation of FL scorecard + test case artifacts.

Given the deterministic prescan context, the fresh CDETS raw text, and the
CaFy coverage verdict, this asks the configured LLM to author the two reasoning
artifacts the deterministic prescan cannot produce:

    <ID>-Scorecard.md          (confidence-weighted quality scorecard)
    AI-FL-<ID>_TestCase.md     (lab-executable test case + coverage gap)

The prompts encode the FL Agent's scoring method (v3) and the mandatory
Score Summary Box format so the console's box-parser can extract headline
metrics. Generation is best-effort: any failure leaves the deterministic
artifacts in place and is reported to the caller.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from .llm import GatewayLLM, LLMError

_SYSTEM = """You are the IOS-XR Feedback Loop Agent. You analyze a CDETS \
defect and produce precise, lab-grade engineering artifacts. You never \
hallucinate: every concrete claim must trace to the provided CDETS evidence. \
You write in clean GitHub-flavored Markdown and follow the requested section \
order and the exact ASCII Score Summary Box format. Use ASCII only in score \
boxes (no Unicode box-drawing)."""

_SCORECARD_PROMPT = """Produce a QUALITY SCORECARD markdown for CDETS {cdets_id}.

Use the confidence-weighted quality method (v3):
- Field weights: 1 (routing), 2 (context), 3 (automation-critical). Baseline total applicable = 38.
- Quality: 0=MISSING, 0.5=LOW_SPECIFICITY, 0.75=ASSERTED_NEGATIVE, 0.85=ASSERTED_NEGATIVE_CONSISTENT, 1.0=ACTIONABLE (must be citable).
- Confidence factor: HIGH=1.0, MEDIUM=0.8, LOW=0.5. Score = weight x quality x factor.
- Hard blockers (set AI eligible = NO): behavior.expected or behavior.actual missing; primary_ap missing; failure.category invalid.

CRITICAL SCORING CALIBRATION — do the math, do not guess:
- A typical well-documented defect (clear expected + actual behavior, valid component/AP, a
  reproduction trigger or exact syslog signature, severity present) scores in the 70-90% range.
- Reserve scores below 50% ONLY when expected/actual behavior is genuinely missing or a hard
  blocker fires. Do NOT output a low score for a defect that has a clear symptom and trigger.
- Final % = round(earned / 38 * 100). Compute earned by summing the per-field Score column.
  Verify earned/38 matches the headline percentage before you finalize.
- Grade bands: >=85 STRONG/GOOD, 70-84 MODERATE, 55-69 WEAK, <55 POOR.
- The Automation Readiness line must be one of:
  "READY FOR AUTOMATION", "AUTOMATABLE -- REVIEW RECOMMENDED", or "NOT READY FOR AUTOMATION".
  It is NOT a CaFy verdict — never put NEW_TEST_NEEDED there.

Required output structure, in order:
1. `# {cdets_id} — Quality Scorecard`
2. A line: `` `{cdets_id} : <headline>` ``
3. A fenced ``` block containing EXACTLY this Score Summary Box (fill real values):
+--------------------------------------------------------------------------+
| CDETS Defect Score:       <N>% (<GRADE>)                                 |
| AI Confidence:            <N>% / <HIGH|MED|LOW>                          |
| Automation Readiness:     <READY FOR AUTOMATION|AUTOMATABLE -- REVIEW RECOMMENDED|NOT READY FOR AUTOMATION> |
| TestCoverageConfidence:   <N>% / <Fully Covered|Partial|No Coverage>    |
| DT Testability:           OK                                            |
| Source:                   CDETS_ONLY                                    |
+--------------------------------------------------------------------------+
4. `## Required Fields Gate` (table: filled/total, percent; list any missing).
5. `## Confidence-Weighted Quality Score (v3)` — a markdown table with columns
   Field Path | Weight | Quality | Label | Confidence | Factor | Score, covering at least
   12 scored fields, then a line
   `Earned = X | Total applicable = 38 | Raw = X% | Final = N% (GRADE)`.
6. `## Component Mapping` (Component, Primary AP, Sub-AP, AP Confidence, DT-PT Manager).
7. `## Cross-Field Derivation Signals` (TRAFFIC/SOAK/PHYSICAL/SCOPE/SEVERITY/VERSION rows).
8. `## Evidence Citations` (cite specific CDETS description lines / syslog / fields).
9. `## Next Info Requests`.

Use these pre-resolved facts: Primary AP = {primary_ap}; Sub-AP = {sub_ap};
Component = {component}; CaFy verdict = {cafy_verdict}; TestCoverageConfidence
should reflect a {cafy_verdict} verdict (NEW_TEST_NEEDED implies low coverage / No Coverage).

CDETS EVIDENCE (authoritative — do not invent beyond this):
---
{cdets_raw}
---
Output ONLY the markdown, no preamble."""

_TESTCASE_PROMPT = """Produce a LAB-EXECUTABLE TEST CASE markdown for CDETS {cdets_id}.

Mandatory order and headings:
1. First line: `{cdets_id} : <headline>`
2. `## Score Summary Box` with a fenced ``` block containing EXACTLY:
+--------------------------------------------------------------------------+
| CDETS Defect Score:       <N>% (<GRADE>)                                 |
| TestCoverageConfidence:   <N>% / <Fully Covered|Partial|No Coverage>    |
| Coverage Gap:             <FULL_COVERAGE|VERIFIER_ADDITION|STEP_ADDITION|TEST_EXTENSION|NEW_TEST_REQUIRED> |
| Source:                   CDETS_ONLY                                    |
| Primary AP:               {primary_ap}                                  |
| Sub-AP:                   {sub_ap}                                      |
| Blueprint Topology:       <PP|F3|F6>                                    |
+--------------------------------------------------------------------------+
3. `## Evidence Summary` (sources, failure category, broken observable, expected observable, triggers, RCA/mechanism).
4. `## Automation Feasibility` (verdict, existing code alignment, helper reuse analysis, new helper needs).
5. `## POD Topology` (POD type, router roles, adjacency, traffic path).
6. One or more `## Test Case N: <title>` blocks. Each has:
   - **TestType:** Functional|Negative|Regression|Stress|HA|Scale|Upgrade|Interop|Performance
   - **Preconditions:**
   - **Test Procedure:** numbered English steps (config, baseline capture, trigger, validate, cleanup), naming concrete CLI/verifier/helper paths where known.
   - **Pass/Fail:** deterministic PASS and FAIL criteria tied to the broken observable.
7. `## Coverage Gap Analysis` with `### TestCoverageConfidence: N% (grade)`, a comparison matrix table, `### Gap Classification: <class>`, an `### Automation Coverage Mapping`, and a `### DT Testability Alert`.

Map to a {cafy_verdict} CaFy verdict. Reuse real evidence-backed triggers from the CDETS data (syslog signatures, exact config sequences). Do not invent commands not implied by the evidence.

Pre-resolved: Primary AP = {primary_ap}; Sub-AP = {sub_ap}; Component = {component}.

CDETS EVIDENCE (authoritative):
---
{cdets_raw}
---
Output ONLY the markdown, no preamble."""


def _strip_code_fence(text: str) -> str:
    """Remove an outer ```markdown ... ``` wrapper if the model added one."""
    t = text.strip()
    m = re.match(r"^```[a-zA-Z]*\s*\n(.*)\n```$", t, re.DOTALL)
    return m.group(1).strip() if m else t


def generate_reasoning_artifacts(
    cdets_id: str,
    out_dir: Path,
    *,
    cdets_raw: str,
    component: str,
    primary_ap: str,
    sub_ap: str,
    cafy_verdict: str,
) -> dict[str, object]:
    """Generate scorecard + test case via LLM. Best-effort.

    Returns a dict: {"generated": [filenames], "error": Optional[str]}.
    """
    llm = GatewayLLM()
    if not llm.available:
        return {"generated": [], "error": "LLM not configured"}

    # Keep evidence within a sane prompt budget.
    evidence = (cdets_raw or "")[:14000]
    ctx = dict(
        cdets_id=cdets_id,
        component=component,
        primary_ap=primary_ap,
        sub_ap=sub_ap or "N/A",
        cafy_verdict=cafy_verdict or "NEW_TEST_NEEDED",
        cdets_raw=evidence,
    )

    generated: list[str] = []
    try:
        # Run the two LLM calls concurrently — each is an independent ~30-40s
        # network call, so parallelizing roughly halves total analyze latency.
        from concurrent.futures import ThreadPoolExecutor

        def _gen_scorecard() -> str:
            return _strip_code_fence(
                llm.generate(_SYSTEM, _SCORECARD_PROMPT.format(**ctx), max_tokens=4000)
            )

        def _gen_testcase() -> str:
            return _strip_code_fence(
                llm.generate(_SYSTEM, _TESTCASE_PROMPT.format(**ctx), max_tokens=4500)
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            sc_future = pool.submit(_gen_scorecard)
            tc_future = pool.submit(_gen_testcase)
            scorecard = sc_future.result()
            testcase = tc_future.result()

        if scorecard:
            (out_dir / f"{cdets_id}-Scorecard.md").write_text(scorecard, encoding="utf-8")
            generated.append(f"{cdets_id}-Scorecard.md")
        if testcase:
            (out_dir / f"AI-FL-{cdets_id}_TestCase.md").write_text(testcase, encoding="utf-8")
            generated.append(f"AI-FL-{cdets_id}_TestCase.md")
    except LLMError as exc:
        return {"generated": generated, "error": str(exc)}
    except OSError as exc:
        return {"generated": generated, "error": f"write failed: {exc}"}

    return {"generated": generated, "error": None}
