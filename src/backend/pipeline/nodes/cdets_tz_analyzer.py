"""Stage 01 — cdets_tz_analyzer (LLM agent)."""

from __future__ import annotations

import logging
import time
from typing import Any, Dict

from ..agent_loop import run_agent_loop
from ..llm import get_llm
from ..state import FLAgentState
from ..tools.cdets import lookup_cdets
from ..tools.filesystem import (
    file_exists,
    list_directory,
    read_file_text,
    write_artifact_json,
    write_artifact_text,
)
from ..utils import backend_root, stage_trace, utc_now_iso
from ._llm_helpers import extract_json, format_user_message, load_prompt

logger = logging.getLogger(__name__)

_USER_TEMPLATE = """\
Analyze CDETS defect **{cdets_id}** and produce schema artifacts.

Pre-resolved context (already verified):

```
component       : {component}
primary_ap      : {primary_ap}
version         : {version}
severity        : {severity}
dtpt_manager    : {dtpt_manager}
artifact_dir    : {artifact_dir}
schema_skeleton : {schema_skeleton}
```

Structured CDETS fields from prescan:

{cdets_fields}

The schema skeleton is provided inline below — do NOT call `read_file_text`
on it (that wastes a round). Fill it in from the CDETS fields above and write
it directly with `write_artifact_json`. Then end with the JSON response
described in the system prompt.

Schema skeleton (fill this in):

```json
{schema_skeleton_content}
```
"""


async def cdets_tz_analyzer_node(state: FLAgentState) -> Dict[str, Any]:
    started = time.monotonic()
    config = state["config"]
    llm = get_llm(config, stage="schema")

    schema_skeleton = backend_root() / "pipeline" / "schemas" / "Defect_Schema_Template_v1.0.json"
    try:
        skeleton_content = schema_skeleton.read_text(encoding="utf-8")
    except OSError:
        skeleton_content = ""

    user_message = format_user_message(
        _USER_TEMPLATE,
        cdets_id=state["cdets_id"],
        component=state.get("component", ""),
        primary_ap=state.get("primary_ap", ""),
        version=state.get("version", ""),
        severity=state.get("severity", ""),
        dtpt_manager=state.get("dtpt_manager", ""),
        artifact_dir=state.get("artifact_dir", ""),
        schema_skeleton=str(schema_skeleton),
        schema_skeleton_content=skeleton_content,
        cdets_fields=state.get("cdets_fields", {}),
    )

    result = await run_agent_loop(
        llm,
        system_prompt=load_prompt("cdets_analyzer"),
        user_message=user_message,
        tools=[
            lookup_cdets,
            read_file_text,
            list_directory,
            file_exists,
            write_artifact_json,
            write_artifact_text,
        ],
        max_iterations=12,
    )

    parsed = extract_json(result.final_text) or {}
    schema_path = parsed.get("cdets_schema_path")

    update: Dict[str, Any] = {
        "cdets_schema_path": schema_path,
        "tz_schema_path": parsed.get("tz_schema_path"),
        "union_schema_path": parsed.get("union_schema_path"),
        "has_techzone": bool(parsed.get("has_techzone", False)),
        "schema_data": parsed.get("schema_summary", {}) or {},
        "stage_traces": {
            "cdets_tz_analyzer": stage_trace(
                status="ok" if schema_path else "failed",
                duration=result.duration_seconds,
                iterations=result.iterations,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                tool_calls=result.tool_log,
                error=None if schema_path else "no schema path returned",
            )
            | {"start_time": utc_now_iso()},
        },
    }
    if not schema_path:
        update["error"] = "cdets_tz_analyzer did not produce a schema path"
    logger.info(
        "cdets_tz_analyzer: cdets=%s schema=%s tz=%s",
        state["cdets_id"], schema_path, update["has_techzone"],
    )
    return update
