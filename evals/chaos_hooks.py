"""Invoke platform chaos hooks from the eval harness.

Chaos setup is an operator / eval concern, outside the agent's trust
boundary — the agent never fires chaos on itself. This module lives in
``evals/`` (not ``src/incident_commander/``) to keep that separation
enforced by import path.

Two entry points:

- ``ChaosClient`` — thin JSON-RPC wrapper used by both this module and
  ``scripts/chaos_setup.py`` (the operator CLI).
- ``invoke_chaos_hook(url, token, hook)`` — one-shot: build a client,
  fire the hook, close. Used by the runner when a ``Scenario`` declares
  its own ``chaos_setup``.

Failures raise ``ChaosInvocationError`` so callers can distinguish a
seeding failure ("we couldn't set up the world") from a scenario
failure ("agent behaved wrong in a valid world").
"""

from __future__ import annotations

import json
from typing import Any

import httpx

_DEFAULT_TIMEOUT_SECONDS = 15.0


class ChaosInvocationError(RuntimeError):
    """The platform rejected or errored on a chaos-hook invocation."""


class ChaosClient:
    """Minimal JSON-RPC caller for the platform's chaos endpoints.

    Deliberately not the agent's ``MCPClient``: chaos is an operator
    concern, and the agent's client carries auth/retry policies tuned
    for the agent's trust boundary. Keeping this separate keeps that
    separation honest.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._url = base_url.rstrip("/")
        self._client = httpx.Client(
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=timeout_seconds,
        )

    def call(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }
        response = self._client.post(self._url, json=body)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ChaosInvocationError(f"non-object JSON response: {type(payload).__name__}")
        if "error" in payload:
            err = payload["error"]
            raise ChaosInvocationError(
                f"platform returned MCP error {err.get('code')}: {err.get('message')}"
            )
        result = payload.get("result", {})
        if not isinstance(result, dict):
            return {}
        content = result.get("content", [])
        for block in content:
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                parsed = json.loads(block["text"])
                if isinstance(parsed, dict):
                    return parsed
        return {}

    def close(self) -> None:
        self._client.close()


def invoke_chaos_hook(
    mcp_url: str,
    token: str,
    hook_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """One-shot: build client, fire hook, close, return parsed result.

    Used by the eval runner when a scenario declares its own chaos
    setup, so operators no longer have to remember which `make chaos-*`
    target pairs with which scenario.
    """
    client = ChaosClient(mcp_url, token)
    try:
        return client.call(hook_name, arguments)
    finally:
        client.close()
