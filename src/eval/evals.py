"""Phoenix LLM-as-judge evaluations for the FL LangGraph Agent.

Tracing (``eval.tracing``) records *what happened* — every LangGraph node
and LLM call becomes a span in Phoenix. Evaluation answers *how good was it*:
each span is scored by an LLM judge and the verdict is written back to Phoenix
as a **span annotation** (label + score + explanation). Annotations are what
populate the "Annotations" column/tab in the Phoenix UI — without an eval pass
that column is empty, which is expected.

This mirrors the LLM-as-judge step in the block5 Phoenix example, but instead
of the Anthropic SDK it reuses this project's own LangChain model factory
(:func:`backend.pipeline.llm.get_llm`) as the judge, so the existing gateway /
Bedrock authentication is reused and no extra credentials are needed.

Flow
----
1. Pull the LLM spans for a project from Phoenix.
2. For each span, ask the judge model to rate the response.
3. Log the verdict back to the span via the Phoenix annotations API.

Usage
-----
    python src/eval/run_phoenix_evals.py                       # eval recent LLM spans
    python src/eval/run_phoenix_evals.py --project fl-langgraph-agent --hours 24
    python src/eval/run_phoenix_evals.py --eval coherence --limit 50
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

DEFAULT_PROJECT = "fl-langgraph-agent"


# --------------------------------------------------------------------------- #
# Judge prompt templates. Each returns a strict JSON verdict so the result can
# be logged as a Phoenix annotation (label + score + explanation).
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class EvalSpec:
    """A single LLM-as-judge evaluation dimension."""

    name: str
    instructions: str
    labels: dict[str, float]  # label -> numeric score (higher = better)

    @property
    def label_list(self) -> str:
        return ", ".join(self.labels)


EVAL_SPECS: dict[str, EvalSpec] = {
    "quality": EvalSpec(
        name="response_quality",
        instructions=(
            "You are grading the QUALITY of an AI assistant's response to the "
            "input it was given. A good response is relevant, complete, "
            "well-structured, and free of obvious errors or hallucinations."
        ),
        labels={"good": 1.0, "fair": 0.5, "poor": 0.0},
    ),
    "coherence": EvalSpec(
        name="coherence",
        instructions=(
            "You are grading the COHERENCE of an AI assistant's response: is it "
            "logically organised, internally consistent, and easy to follow?"
        ),
        labels={"coherent": 1.0, "incoherent": 0.0},
    ),
    "hallucination": EvalSpec(
        name="hallucination",
        instructions=(
            "You are checking the response for HALLUCINATION: content that is "
            "fabricated or not grounded in the input/context. Answer 'factual' "
            "if the response is grounded, 'hallucinated' if it invents facts."
        ),
        labels={"factual": 1.0, "hallucinated": 0.0},
    ),
}

_JUDGE_TEMPLATE = """{instructions}

[BEGIN INPUT]
{input}
[END INPUT]

[BEGIN RESPONSE]
{output}
[END RESPONSE]

Choose exactly one label from: {labels}.
Respond with ONLY a JSON object on a single line, no markdown, of the form:
{{"label": "<one of: {labels}>", "explanation": "<one short sentence>"}}"""


# --------------------------------------------------------------------------- #
# Span field extraction (OpenInference attributes)
# --------------------------------------------------------------------------- #
def _first_present(row: dict[str, Any], *keys: str) -> str:
    for key in keys:
        val = row.get(key)
        if val is None:
            continue
        if isinstance(val, (dict, list)):
            return json.dumps(val, default=str)[:6000]
        text = str(val).strip()
        if text and text.lower() != "nan":
            return text[:6000]
    return ""


def _extract_io(row: dict[str, Any]) -> tuple[str, str]:
    """Pull (input, output) text from an OpenInference LLM span row."""
    input_text = _first_present(
        row,
        "attributes.input.value",
        "attributes.llm.input_messages",
        "attributes.llm.prompts",
    )
    output_text = _first_present(
        row,
        "attributes.output.value",
        "attributes.llm.output_messages",
    )
    return input_text, output_text


# --------------------------------------------------------------------------- #
# Judge
# --------------------------------------------------------------------------- #
def _parse_verdict(raw: str, spec: EvalSpec) -> tuple[str, Optional[float], str]:
    """Parse the judge's JSON reply into (label, score, explanation)."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{") :] if "{" in text else text
    try:
        start, end = text.find("{"), text.rfind("}")
        data = json.loads(text[start : end + 1]) if start != -1 and end != -1 else {}
    except (json.JSONDecodeError, ValueError):
        data = {}

    label = str(data.get("label", "")).strip().lower()
    explanation = str(data.get("explanation", "")).strip()
    if label not in spec.labels:
        # Best-effort recovery: match any known label mentioned in the text.
        for known in spec.labels:
            if known in text.lower():
                label = known
                break
    score = spec.labels.get(label)
    return label or "unknown", score, explanation or raw.strip()[:200]


def _make_judge(config: dict, stage: Optional[str]) -> Callable[[str], str]:
    """Return a callable that sends a prompt to the project's LLM judge."""
    from backend.pipeline.llm import get_llm

    llm = get_llm(config, stage=stage)

    def judge(prompt: str) -> str:
        result = llm.invoke(prompt)
        return getattr(result, "content", str(result))

    return judge


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def run_phoenix_evals(
    *,
    config: dict,
    project_name: str = DEFAULT_PROJECT,
    eval_kind: str = "quality",
    hours: float = 24.0,
    limit: int = 100,
    judge_stage: Optional[str] = "scoring",
    phoenix_endpoint: Optional[str] = None,
) -> dict[str, Any]:
    """Score LLM spans in *project_name* and log Phoenix span annotations.

    Returns a summary dict with counts and the label distribution.
    """
    try:
        from phoenix.client import Client
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "phoenix client not installed. Run: uv pip install arize-phoenix"
        ) from exc

    spec = EVAL_SPECS.get(eval_kind)
    if spec is None:
        raise ValueError(
            f"unknown eval '{eval_kind}'. Choices: {', '.join(EVAL_SPECS)}"
        )

    endpoint = (
        phoenix_endpoint
        or os.getenv("PHOENIX_COLLECTOR_ENDPOINT")
        or "http://localhost:6006"
    )
    client = Client(base_url=endpoint)

    start_time = datetime.now(timezone.utc) - timedelta(hours=hours)
    df = client.spans.get_spans_dataframe(
        project_identifier=project_name,
        start_time=start_time,
        limit=limit,
    )
    if df is None or len(df) == 0:
        logger.warning("No spans found in project '%s' in the last %sh.", project_name, hours)
        return {"project": project_name, "eval": spec.name, "evaluated": 0, "labels": {}}

    # Restrict to LLM spans where we can read both input and output.
    if "span_kind" in df.columns:
        df = df[df["span_kind"].astype(str).str.upper() == "LLM"]

    judge = _make_judge(config, judge_stage)
    labels: dict[str, int] = {}
    evaluated = 0
    skipped = 0

    for _, row in df.iterrows():
        record = row.to_dict()
        span_id = record.get("context.span_id") or record.get("span_id")
        if not span_id:
            skipped += 1
            continue
        input_text, output_text = _extract_io(record)
        if not input_text or not output_text:
            skipped += 1
            continue

        prompt = _JUDGE_TEMPLATE.format(
            instructions=spec.instructions,
            input=input_text,
            output=output_text,
            labels=spec.label_list,
        )
        try:
            raw = judge(prompt)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Judge failed for span %s: %s", span_id, exc)
            skipped += 1
            continue

        label, score, explanation = _parse_verdict(raw, spec)
        try:
            client.spans.add_span_annotation(
                span_id=str(span_id),
                annotation_name=spec.name,
                annotator_kind="LLM",
                label=label,
                score=score,
                explanation=explanation,
                sync=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not log annotation for span %s: %s", span_id, exc)
            skipped += 1
            continue

        labels[label] = labels.get(label, 0) + 1
        evaluated += 1
        logger.info("span %s -> %s (%s)", span_id, label, score)

    summary = {
        "project": project_name,
        "eval": spec.name,
        "evaluated": evaluated,
        "skipped": skipped,
        "labels": labels,
        "phoenix": endpoint,
    }
    logger.info("Eval complete: %s", summary)
    return summary
