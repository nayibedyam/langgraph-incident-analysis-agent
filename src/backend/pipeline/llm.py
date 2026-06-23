"""Azure OpenAI LLM factory with per-stage deployment routing.

Wired for an OpenAI-compatible LLM gateway (``https://your-llm-gateway.example.com``):
the access token goes in ``openai_api_key`` and an extra ``user`` JSON
payload (containing ``appkey`` and ``user``) is passed via
``model_kwargs`` so the gateway can attribute the call.

Reads configuration from ``config.yaml`` (already loaded into the
FLAgentState) and creates an :class:`AzureChatOpenAI` instance per stage.
The model name can vary by stage to control cost — e.g., scoring uses
gpt-4o-mini while testcase generation uses gpt-4o.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
from functools import lru_cache
from typing import Any, Optional

import requests
from dotenv import load_dotenv
from langchain_openai import AzureChatOpenAI

load_dotenv()

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# OAuth token manager
#
# The LLM gateway authenticates with a short-lived JWT access
# token (~1 hour). Rather than reading a static token from the environment
# (which quickly expires and causes ``401 - The Token has expired``), we mint
# one on demand via the OAuth2 client-credentials grant and cache it until
# shortly before it expires.
# --------------------------------------------------------------------------- #
_TOKEN_LOCK = threading.Lock()
_TOKEN_CACHE: dict[str, float | str] = {"token": "", "expires_at": 0.0}
_TOKEN_SKEW_SECONDS = 120  # refresh this many seconds before actual expiry


def _fetch_access_token() -> tuple[str, float]:
    """Mint a fresh access token via the OAuth2 client-credentials grant.

    Returns ``(token, expires_at_epoch)``.
    """
    client_id = os.getenv("OAUTH_CLIENT_ID", "").strip()
    client_secret = os.getenv("OAUTH_CLIENT_SECRET", "").strip()
    token_url = os.getenv(
        "OAUTH_TOKEN_URL", "https://your-oauth-provider.example.com/oauth2/default/v1/token"
    ).strip()
    if not client_id or not client_secret:
        raise RuntimeError(
            "OAUTH_CLIENT_ID / OAUTH_CLIENT_SECRET are not set. Add them to .env "
            "so the gateway access token can be minted automatically."
        )

    basic = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode(
        "utf-8"
    )
    headers = {
        "Accept": "*/*",
        "Content-Type": "application/x-www-form-urlencoded",
        "Authorization": f"Basic {basic}",
    }
    response = requests.post(
        token_url,
        headers=headers,
        data="grant_type=client_credentials",
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    token = data.get("access_token", "")
    if not token:
        raise RuntimeError(f"Token endpoint returned no access_token: {data}")
    expires_in = float(data.get("expires_in", 3300))
    expires_at = time.time() + expires_in
    logger.info("Minted new gateway access token (expires in %ss)", int(expires_in))
    return token, expires_at


def _get_access_token() -> str:
    """Return a valid access token, refreshing it shortly before expiry.

    When OAuth client credentials are present they take precedence (auto-
    refreshing). Otherwise a static ``AZURE_OPENAI_API_KEY`` is used as a
    fallback so older setups keep working.
    """
    has_client_creds = bool(
        os.getenv("OAUTH_CLIENT_ID", "").strip()
        and os.getenv("OAUTH_CLIENT_SECRET", "").strip()
    )
    if not has_client_creds:
        static = os.getenv("AZURE_OPENAI_API_KEY", "").strip()
        if static:
            return static
        raise RuntimeError(
            "No gateway credentials available. Set OAUTH_CLIENT_ID / "
            "OAUTH_CLIENT_SECRET (preferred) or AZURE_OPENAI_API_KEY in .env."
        )

    now = time.time()
    with _TOKEN_LOCK:
        if _TOKEN_CACHE["token"] and now < float(_TOKEN_CACHE["expires_at"]) - _TOKEN_SKEW_SECONDS:
            return str(_TOKEN_CACHE["token"])
        token, expires_at = _fetch_access_token()
        _TOKEN_CACHE["token"] = token
        _TOKEN_CACHE["expires_at"] = expires_at
        return token


def _resolve_env(value: str) -> str:
    """Substitute ${VAR} or ${VAR:-default} from environment."""
    if not isinstance(value, str) or "${" not in value:
        return value
    import re
    pattern = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)(?::-([^}]*))?\}")

    def replace(match: re.Match) -> str:
        var_name = match.group(1)
        default = match.group(2) or ""
        return os.getenv(var_name, default)

    return pattern.sub(replace, value)


def _is_reasoning_model(model_name: str) -> bool:
    """Reasoning models (gpt-5*, o1*, o3*) need reasoning_effort and don't support temperature."""
    name = model_name.lower()
    return name.startswith(("gpt-5", "o1", "o3", "o4"))


@lru_cache(maxsize=8)
def _build_llm(
    model_name: str,
    endpoint: str,
    api_version: str,
    temperature: float,
    max_tokens: int,
    timeout: int,
    reasoning_effort: str,
    access_token: str = "",
) -> AzureChatOpenAI:
    """Cache LLM instances by (model, endpoint, params, token) tuple.

    ``access_token`` is part of the cache key so a refreshed token transparently
    produces a new client instead of reusing one bound to an expired token.
    """
    app_key = os.getenv("GATEWAY_APP_KEY", "").strip()
    user_id = os.getenv("GATEWAY_USER_ID", "").strip()

    if not access_token:
        raise RuntimeError(
            "No gateway access token available. Set OAUTH_CLIENT_ID / "
            "OAUTH_CLIENT_SECRET (preferred) or AZURE_OPENAI_API_KEY in .env."
        )
    if not app_key:
        raise RuntimeError(
            "GATEWAY_APP_KEY is not set. Set your gateway app key in .env."
        )
    if not user_id:
        raise RuntimeError(
            "GATEWAY_USER_ID is not set. Set your gateway user ID in .env."
        )
    if not endpoint:
        raise RuntimeError("AZURE_OPENAI_ENDPOINT is not set.")

    user_payload = json.dumps({"appkey": app_key, "user": user_id})
    model_kwargs = {"user": user_payload}

    kwargs = dict(
        model=model_name,
        azure_endpoint=endpoint,
        api_version=api_version,
        openai_api_key=access_token,
        timeout=timeout,
        model_kwargs=model_kwargs,
    )

    if _is_reasoning_model(model_name):
        model_kwargs["reasoning_effort"] = reasoning_effort
        # Reasoning models consume tokens internally for their chain-of-thought.
        # Give them ample headroom so visible output isn't starved.
        kwargs["max_tokens"] = max(max_tokens, 32768)
        logger.debug(
            "Building AzureChatOpenAI: model=%s endpoint=%s reasoning_effort=%s max_tokens=%s",
            model_name, endpoint, reasoning_effort, kwargs["max_tokens"],
        )
    else:
        kwargs["temperature"] = temperature
        kwargs["max_tokens"] = max_tokens
        logger.debug(
            "Building AzureChatOpenAI: model=%s endpoint=%s temperature=%s",
            model_name, endpoint, temperature,
        )

    return AzureChatOpenAI(**kwargs)


@lru_cache(maxsize=8)
def _build_bedrock_llm(
    model_id: str,
    region: str,
    temperature: float,
    max_tokens: int,
    timeout: int,
) -> Any:
    """Build a cached :class:`ChatBedrockConverse` for AWS Bedrock.

    Authenticates with the Bedrock API key in ``AWS_BEARER_TOKEN_BEDROCK``
    (picked up automatically by botocore). Bedrock's Converse API exposes the
    same LangChain chat interface (``bind_tools``, ``tool_calls``,
    ``usage_metadata``) used by the agent loop, so it is a drop-in for the
    Azure client. Claude models are strong tool-callers.
    """
    try:
        from langchain_aws import ChatBedrockConverse
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "langchain-aws is not installed. Run: pip install langchain-aws boto3"
        ) from exc

    if not os.getenv("AWS_BEARER_TOKEN_BEDROCK", "").strip():
        raise RuntimeError(
            "AWS_BEARER_TOKEN_BEDROCK is not set. Add your Bedrock API key to .env."
        )

    # Large artifact-writing stages generate for well over botocore's default
    # 60s read timeout, so raise it and add adaptive retries for throttling.
    from botocore.config import Config as BotoConfig

    boto_config = BotoConfig(
        read_timeout=timeout,
        connect_timeout=30,
        retries={"max_attempts": 4, "mode": "adaptive"},
    )

    logger.debug("Building ChatBedrockConverse: model=%s region=%s", model_id, region)
    return ChatBedrockConverse(
        model=model_id,
        region_name=region,
        temperature=temperature,
        max_tokens=max_tokens,
        config=boto_config,
    )


def get_llm(config: dict, stage: Optional[str] = None) -> Any:
    """Return a configured chat model for the given stage.

    Provider is selected via ``llm.provider`` in config or the
    ``FL_LLM_PROVIDER`` env var (``azure`` [default] or ``bedrock``). Falls
    back to the ``default`` deployment/model if the stage has no specific entry.
    """
    llm_cfg = config.get("llm", {}) or {}
    provider = (_resolve_env(llm_cfg.get("provider", "")) or "azure").strip().lower()

    if provider == "bedrock":
        bedrock_cfg = llm_cfg.get("bedrock", {}) or {}
        models = bedrock_cfg.get("models", {}) or {}
        model_id = (models.get(stage) if stage else None) or models.get(
            "default", _resolve_env(bedrock_cfg.get("model", ""))
        )
        return _build_bedrock_llm(
            model_id=_resolve_env(model_id),
            region=_resolve_env(bedrock_cfg.get("region", "us-west-2")),
            temperature=float(bedrock_cfg.get("temperature", 0.1)),
            max_tokens=int(bedrock_cfg.get("max_tokens", 8192)),
            timeout=int(bedrock_cfg.get("request_timeout", 300)),
        )

    azure_cfg = config.get("azure_openai", {})
    deployments = azure_cfg.get("deployments", {})

    model_name = deployments.get(stage) if stage else None
    if not model_name:
        model_name = deployments.get("default", "gpt-5-nano")

    return _build_llm(
        model_name=_resolve_env(model_name),
        endpoint=_resolve_env(azure_cfg.get("endpoint", "https://your-llm-gateway.example.com")),
        api_version=_resolve_env(azure_cfg.get("api_version", "2025-04-01-preview")),
        temperature=float(azure_cfg.get("temperature", 0.1)),
        max_tokens=int(azure_cfg.get("max_tokens", 4096)),
        timeout=int(azure_cfg.get("request_timeout", 120)),
        reasoning_effort=str(azure_cfg.get("reasoning_effort", "medium")),
        access_token=_get_access_token(),
    )
