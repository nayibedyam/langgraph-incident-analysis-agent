"""Generic agent loop used by every LLM-backed node in the pipeline.

Implements the standard tool-calling while loop:
1. Send messages to the LLM (with bound tools)
2. If response has no tool calls → it's the final answer
3. Otherwise execute each tool call, append results, loop

Each node provides its own system prompt and tool list. The loop is
identical regardless of which node is calling it.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, List, Sequence

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.tools import BaseTool
from langchain_openai import AzureChatOpenAI

logger = logging.getLogger(__name__)


@dataclass
class AgentRunResult:
    """Result envelope returned by :func:`run_agent_loop`."""

    final_text: str
    tool_log: List[dict] = field(default_factory=list)
    iterations: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    duration_seconds: float = 0.0


def _resolve_tool(name: str, tools: Sequence[BaseTool]) -> BaseTool:
    for tool in tools:
        if tool.name == name:
            return tool
    raise ValueError(f"Tool {name!r} not found in agent's tool set")


def _stringify(result: Any) -> str:
    if isinstance(result, (dict, list)):
        try:
            return json.dumps(result, default=str)
        except (TypeError, ValueError):
            return str(result)
    return str(result)


async def run_agent_loop(
    llm: AzureChatOpenAI,
    *,
    system_prompt: str,
    user_message: str,
    tools: Sequence[BaseTool],
    max_iterations: int = 12,
) -> AgentRunResult:
    """Drive a single LLM agent through its tool-calling loop.

    Parameters
    ----------
    llm:
        AzureChatOpenAI instance (typically from :func:`pipeline.llm.get_llm`).
    system_prompt:
        Stage-specific system prompt loaded from ``pipeline/prompts/*.md``.
    user_message:
        Initial user-facing prompt that frames the task.
    tools:
        LangChain tools the agent is allowed to call this turn.
    max_iterations:
        Hard cap on tool-call rounds. Prevents runaway loops.
    """
    started = time.monotonic()
    llm_with_tools = llm.bind_tools(list(tools)) if tools else llm

    messages: List[Any] = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_message),
    ]
    tool_log: List[dict] = []
    input_tokens = 0
    output_tokens = 0

    for iteration in range(1, max_iterations + 1):
        logger.debug("Agent loop iteration %d/%d", iteration, max_iterations)
        response: AIMessage = await llm_with_tools.ainvoke(messages)
        messages.append(response)

        usage = getattr(response, "usage_metadata", None) or {}
        input_tokens += int(usage.get("input_tokens", 0) or 0)
        output_tokens += int(usage.get("output_tokens", 0) or 0)

        tool_calls = getattr(response, "tool_calls", None) or []
        logger.debug(
            "Iteration %d: tool_calls=%d, response_length=%d, response_preview=%.500s",
            iteration, len(tool_calls),
            len(response.content) if isinstance(response.content, str) else 0,
            response.content if isinstance(response.content, str) else str(response.content)[:500],
        )
        if not tool_calls:
            return AgentRunResult(
                final_text=response.content if isinstance(response.content, str) else str(response.content),
                tool_log=tool_log,
                iterations=iteration,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                duration_seconds=time.monotonic() - started,
            )

        for call in tool_calls:
            tool_name = call.get("name", "")
            tool_args = call.get("args", {}) or {}
            tool_id = call.get("id", "")
            logger.debug("Tool call: %s(%s)", tool_name, json.dumps(tool_args, default=str)[:300])
            try:
                tool_fn = _resolve_tool(tool_name, tools)
                tool_started = time.monotonic()
                result = await tool_fn.ainvoke(tool_args)
                tool_duration = time.monotonic() - tool_started
                content = _stringify(result)
                tool_log.append({
                    "tool": tool_name,
                    "args": tool_args,
                    "duration": round(tool_duration, 3),
                    "ok": True,
                })
            except Exception as exc:  # noqa: BLE001
                logger.exception("Tool %s failed", tool_name)
                content = json.dumps({"error": str(exc), "tool": tool_name})
                tool_log.append({
                    "tool": tool_name,
                    "args": tool_args,
                    "ok": False,
                    "error": str(exc),
                })
            messages.append(ToolMessage(content=content, tool_call_id=tool_id))

    logger.warning("Agent loop hit max_iterations=%d without final answer", max_iterations)
    last = messages[-1]
    final = last.content if isinstance(getattr(last, "content", None), str) else str(last)
    return AgentRunResult(
        final_text=final,
        tool_log=tool_log,
        iterations=max_iterations,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        duration_seconds=time.monotonic() - started,
    )
