#!/usr/bin/env python3
"""Quick health check for an AWS Bedrock bearer token (API key).

Reads the token from the ``AWS_BEARER_TOKEN_BEDROCK`` environment variable and
calls the cheapest Bedrock control-plane API (``ListFoundationModels``) — this
verifies the token authenticates without incurring any model-inference cost.

Usage (set the token in your shell; do NOT hard-code it)::

    export AWS_BEARER_TOKEN_BEDROCK='<your-token>'
    export AWS_REGION=us-east-1          # optional, defaults to us-east-1
    python src/backend/cli/check_bedrock_token.py

    # Optionally also try a tiny model invocation (costs a few tokens):
    python src/backend/cli/check_bedrock_token.py --invoke
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import requests

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # dotenv is optional; env vars still work without it
    pass


def _region() -> str:
    return (
        os.getenv("AWS_REGION")
        or os.getenv("AWS_DEFAULT_REGION")
        or "us-east-1"
    ).strip()


def list_models(token: str, region: str) -> int:
    url = f"https://bedrock.{region}.amazonaws.com/foundation-models"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    try:
        resp = requests.get(url, headers=headers, timeout=30)
    except requests.RequestException as exc:
        print(f"✖ Network error contacting {url}: {exc}")
        return 2

    if resp.status_code == 200:
        try:
            models = resp.json().get("modelSummaries", [])
        except ValueError:
            models = []
        print(f"✔ Token is VALID — authenticated to Bedrock in {region}.")
        print(f"  ListFoundationModels returned {len(models)} models. Sample:")
        for m in models[:8]:
            print(f"    - {m.get('modelId')}")
        return 0

    if resp.status_code in (401, 403):
        print(f"✖ Token REJECTED ({resp.status_code}) in region {region}.")
        print(f"  {resp.text[:400]}")
        print("  Hints: token may be expired/invalid, scoped to a different")
        print("  region, or lack bedrock:ListFoundationModels permission.")
        return 1

    print(f"✖ Unexpected response {resp.status_code}: {resp.text[:400]}")
    return 1


def invoke_model(token: str, region: str, model_id: str) -> int:
    """Optional: minimal invocation to confirm inference access (costs tokens)."""
    url = f"https://bedrock-runtime.{region}.amazonaws.com/model/{model_id}/converse"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    body = {
        "messages": [{"role": "user", "content": [{"text": "Reply with: OK"}]}],
        "inferenceConfig": {"maxTokens": 8, "temperature": 0},
    }
    try:
        resp = requests.post(url, headers=headers, json=body, timeout=60)
    except requests.RequestException as exc:
        print(f"✖ Network error invoking {model_id}: {exc}")
        return 2

    if resp.status_code == 200:
        data = resp.json()
        text = (
            data.get("output", {})
            .get("message", {})
            .get("content", [{}])[0]
            .get("text", "")
        )
        print(f"✔ Invocation OK on {model_id}: {text!r}")
        return 0

    print(f"✖ Invocation failed ({resp.status_code}) on {model_id}: {resp.text[:400]}")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check an AWS Bedrock bearer token")
    parser.add_argument(
        "--invoke",
        action="store_true",
        help="Also run a tiny model invocation (incurs a small token cost)",
    )
    parser.add_argument(
        "--model-id",
        default="anthropic.claude-3-haiku-20240307-v1:0",
        help="Model id to use with --invoke",
    )
    args = parser.parse_args(argv)

    token = os.getenv("AWS_BEARER_TOKEN_BEDROCK", "").strip()
    if not token:
        print(
            "✖ AWS_BEARER_TOKEN_BEDROCK is not set.\n"
            "  Run:  export AWS_BEARER_TOKEN_BEDROCK='<your-token>'\n"
            "  (then re-run this script)."
        )
        return 2

    region = _region()
    print(f"→ Checking Bedrock token in region '{region}' "
          f"(token length {len(token)})…")

    rc = list_models(token, region)
    if rc == 0 and args.invoke:
        rc = invoke_model(token, region, args.model_id)
    return rc


if __name__ == "__main__":
    sys.exit(main())
