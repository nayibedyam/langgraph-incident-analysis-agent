"""Optional Arize Phoenix tracing for the FL LangGraph Agent.

Phoenix runs entirely on your machine — no account or API key needed. Because
this pipeline drives every LLM call through LangChain (``AzureChatOpenAI`` for
the LLM gateway and ``ChatBedrockConverse`` for AWS Bedrock), the
``LangChainInstrumentor`` from OpenInference captures the whole run as an
OpenTelemetry trace tree:

* each LangGraph node (``common_infra``, ``prescan``, ``cdets_scoring``, ...)
* every LLM call within a node — the exact messages sent, tool calls, and
  token counts
* latency for each span

This is the LangChain analogue of the Anthropic-SDK ``AnthropicInstrumentor``
shown in the block5 Phoenix example: same OpenTelemetry plumbing, but wired to
the framework this project actually uses.

Usage
-----
Enable from the CLI::

    python src/backend/cli/run_fl_pipeline.py CSCwk35275 --trace

or via environment variable (useful for the web console / batch jobs)::

    PHOENIX_TRACING=1 python src/backend/cli/run_fl_pipeline.py CSCwk35275

Behaviour
---------
* If ``PHOENIX_COLLECTOR_ENDPOINT`` is set, spans are exported to that already
  running Phoenix collector (e.g. a shared ``phoenix serve`` instance) and no
  local UI is launched.
* Otherwise an embedded Phoenix app is launched in-process and its URL is
  printed (default http://localhost:6006).

The whole module degrades gracefully: if Phoenix or the instrumentation
packages are not installed, tracing is skipped with a one-line hint instead of
crashing the pipeline.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_INSTRUMENTED = False
_PHOENIX_URL: str | None = None

_INSTALL_HINT = (
    "Phoenix tracing requested but packages are missing. Install with:\n"
    "  pip install arize-phoenix openinference-instrumentation-langchain"
)


def setup_phoenix_tracing(
    *,
    launch_ui: bool = True,
    project_name: str = "fl-langgraph-agent",
) -> str | None:
    """Launch/connect Phoenix and instrument LangChain.

    Safe to call more than once — instrumentation is applied only on the first
    successful call. Returns the Phoenix UI URL when a local app is launched,
    or ``None`` when exporting to an external collector or when tracing could
    not be enabled.
    """
    global _INSTRUMENTED, _PHOENIX_URL

    if _INSTRUMENTED:
        return _PHOENIX_URL

    try:
        from openinference.instrumentation.langchain import LangChainInstrumentor
    except ImportError:
        logger.warning(_INSTALL_HINT)
        return None

    collector_endpoint = os.getenv("PHOENIX_COLLECTOR_ENDPOINT", "").strip()

    # Launch an embedded Phoenix app only when no external collector is
    # configured and a UI was requested.
    if launch_ui and not collector_endpoint:
        try:
            import phoenix as px

            session = px.launch_app()
            _PHOENIX_URL = session.url
            logger.info("Phoenix UI launched at %s", _PHOENIX_URL)
            print(f"[Phoenix] Traces -> {_PHOENIX_URL}")
        except ImportError:
            logger.warning(_INSTALL_HINT)
            return None
        except Exception as exc:  # noqa: BLE001 - never let tracing break a run
            logger.warning("Could not launch Phoenix UI: %s", exc)
    elif collector_endpoint:
        logger.info("Phoenix exporting spans to collector %s", collector_endpoint)
        print(f"[Phoenix] Exporting traces -> {collector_endpoint}")

    # Register an OpenTelemetry tracer provider that points at the Phoenix
    # collector, then attach the LangChain instrumentor to it.
    tracer_provider = None
    try:
        from phoenix.otel import register

        tracer_provider = register(
            project_name=project_name,
            set_global_tracer_provider=False,
            batch=True,
        )
    except Exception as exc:  # noqa: BLE001
        # phoenix.otel may be unavailable on older Phoenix; fall back to the
        # instrumentor's default provider, which still works with launch_app().
        logger.debug("phoenix.otel.register unavailable (%s); using default provider", exc)

    try:
        if tracer_provider is not None:
            LangChainInstrumentor().instrument(tracer_provider=tracer_provider)
        else:
            LangChainInstrumentor().instrument()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not instrument LangChain for Phoenix: %s", exc)
        return None

    _INSTRUMENTED = True
    logger.info("Phoenix LangChain instrumentation enabled (project=%s)", project_name)
    return _PHOENIX_URL


def tracing_enabled_from_env() -> bool:
    """Return True when tracing should be auto-enabled from the environment."""
    value = os.getenv("PHOENIX_TRACING", "").strip().lower()
    return value in {"1", "true", "yes", "on"}
