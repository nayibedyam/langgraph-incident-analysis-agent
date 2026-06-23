"""Minimal synchronous LLM client (OpenAI-compatible gateway).

Uses only the Python standard library (``urllib``) so the console has no extra
runtime dependencies. Authenticates against an OAuth2 client-credentials
token endpoint, then calls a chat-completions deployment (e.g. ``gpt-5-nano``).

Credentials are read from the environment (optionally loaded from
``src/backend/api/.env``):

    LLM_PROVIDER=gateway
    OAUTH_CLIENT_ID=...
    OAUTH_CLIENT_SECRET=...
    LLM_MODEL=gpt-5-nano
    LLM_BASE_URL=https://your-llm-gateway.example.com
    OAUTH_TOKEN_URL=https://your-oauth-provider.example.com/oauth2/default/v1/token
    GATEWAY_APP_KEY=...

``generate()`` returns the assistant text, or raises ``LLMError`` on failure.
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path

_ENV_PATH = Path(__file__).resolve().parent / ".env"
# The Bedrock/gateway credentials live in the repo-root .env; load that too.
_ROOT_ENV_PATH = Path(__file__).resolve().parents[3] / ".env"


class LLMError(RuntimeError):
    """Raised when the LLM is unavailable or a request fails."""


def _load_env() -> None:
    """Best-effort load of the local + repo-root .env into os.environ (no overwrite)."""
    for env_path in (_ENV_PATH, _ROOT_ENV_PATH):
        if not env_path.is_file():
            continue
        try:
            for raw in env_path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                os.environ.setdefault(key, val)
        except OSError:
            pass


class GatewayLLM:
    """Synchronous client for a hosted OpenAI-compatible chat model."""

    _TOKEN_SKEW = 60.0

    def __init__(self) -> None:
        _load_env()
        self.provider = (
            os.getenv("FL_LLM_PROVIDER") or os.getenv("LLM_PROVIDER") or ""
        ).lower()
        self.client_id = os.getenv("OAUTH_CLIENT_ID", "")
        self.client_secret = os.getenv("OAUTH_CLIENT_SECRET", "")
        self.model = os.getenv("LLM_MODEL", "gpt-5-nano")
        self.base_url = os.getenv("LLM_BASE_URL", "https://your-llm-gateway.example.com")
        self.token_url = os.getenv(
            "OAUTH_TOKEN_URL", "https://your-oauth-provider.example.com/oauth2/default/v1/token"
        )
        self.app_key = os.getenv("GATEWAY_APP_KEY", "")
        self._token: str | None = None
        self._token_expiry = 0.0
        # ── AWS Bedrock (used when FL_LLM_PROVIDER=bedrock) ──
        self.bedrock_model = os.getenv(
            "BEDROCK_MODEL", "us.anthropic.claude-haiku-4-5-20251001-v1:0"
        )
        self.bedrock_region = os.getenv(
            "AWS_REGION", os.getenv("AWS_DEFAULT_REGION", "us-west-2")
        )
        self.bedrock_token = os.getenv("AWS_BEARER_TOKEN_BEDROCK", "")
        self._bedrock_client = None

    @property
    def available(self) -> bool:
        if self.provider == "bedrock":
            return bool(self.bedrock_token)
        return bool(self.client_id and self.client_secret)

    # ---- auth ---------------------------------------------------------------
    def _get_token(self) -> str:
        if self._token and time.monotonic() < self._token_expiry:
            return self._token
        basic = base64.b64encode(
            f"{self.client_id}:{self.client_secret}".encode("utf-8")
        ).decode("utf-8")
        req = urllib.request.Request(
            self.token_url,
            data=b"grant_type=client_credentials",
            headers={
                "Accept": "*/*",
                "Content-Type": "application/x-www-form-urlencoded",
                "Authorization": f"Basic {basic}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise LLMError(f"Token request failed: {exc}") from exc
        token = payload.get("access_token")
        if not token:
            raise LLMError("Token endpoint returned no access_token")
        self._token = token
        self._token_expiry = time.monotonic() + max(
            float(payload.get("expires_in", 3600)) - self._TOKEN_SKEW, 0.0
        )
        return token

    # ---- inference ----------------------------------------------------------
    def generate(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int = 4000,
        timeout: float = 120.0,
    ) -> str:
        if self.provider == "bedrock":
            return self._bedrock_generate(
                system, user, max_tokens=max_tokens, timeout=timeout
            )
        if not self.available:
            raise LLMError("LLM credentials not configured")
        body: dict = {
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
        }
        if self.app_key:
            body["user"] = json.dumps({"appkey": self.app_key})
        url = f"{self.base_url.rstrip('/')}/openai/deployments/{self.model}/chat/completions"
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "api-key": self._get_token(),
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise LLMError(f"Chat request failed: {exc}") from exc
        choices = payload.get("choices") or []
        if not choices:
            raise LLMError("LLM returned no choices")
        return choices[0].get("message", {}).get("content", "") or ""

    # ---- AWS Bedrock inference (Converse API, bearer-token auth) -------------
    def _bedrock_generate(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int = 4000,
        timeout: float = 120.0,
    ) -> str:
        if not self.bedrock_token:
            raise LLMError("AWS_BEARER_TOKEN_BEDROCK is not set")
        try:
            import boto3
            from botocore.config import Config as _BotoConfig
        except ImportError as exc:  # pragma: no cover
            raise LLMError(f"boto3 not installed: {exc}") from exc
        if self._bedrock_client is None:
            self._bedrock_client = boto3.client(
                "bedrock-runtime",
                region_name=self.bedrock_region,
                config=_BotoConfig(
                    read_timeout=int(timeout),
                    connect_timeout=30,
                    retries={"max_attempts": 3, "mode": "adaptive"},
                ),
            )
        kwargs: dict = {
            "modelId": self.bedrock_model,
            "messages": [{"role": "user", "content": [{"text": user}]}],
            "inferenceConfig": {"maxTokens": int(max_tokens), "temperature": 0.1},
        }
        if system:
            kwargs["system"] = [{"text": system}]
        try:
            resp = self._bedrock_client.converse(**kwargs)
        except Exception as exc:  # noqa: BLE001
            raise LLMError(f"Bedrock request failed: {exc}") from exc
        try:
            return resp["output"]["message"]["content"][0]["text"] or ""
        except (KeyError, IndexError) as exc:
            raise LLMError(f"Bedrock returned no content: {exc}") from exc
