"""Helpers shared by LLM-backed stage nodes."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_JSON_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def load_prompt(name: str) -> str:
    """Load a prompt template from ``pipeline/prompts/<name>.md``."""
    here = Path(__file__).resolve().parent.parent / "prompts" / f"{name}.md"
    if not here.exists():
        raise FileNotFoundError(f"Prompt not found: {here}")
    return here.read_text(encoding="utf-8")


def extract_json(text: str) -> Optional[Dict[str, Any]]:
    """Pull the last JSON object out of *text* (handles ```json fences)."""
    if not text:
        return None
    m = list(_JSON_FENCE.finditer(text))
    if m:
        candidate = m[-1].group(1)
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

    text = text.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    return None


def format_user_message(template: str, **kwargs: Any) -> str:
    """Format a user message with named placeholders, escaping JSON values."""
    rendered = template
    for key, value in kwargs.items():
        if isinstance(value, (dict, list)):
            value = json.dumps(value, indent=2, default=str)
        rendered = rendered.replace("{" + key + "}", str(value))
    return rendered
