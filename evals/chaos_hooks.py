"""Invoke platform chaos hooks from the eval harness.

Lives in ``evals/`` rather than ``src/incident_commander/`` so the import path
enforces the boundary: the agent never fires chaos on itself. ``ChaosClient`` is
the JSON-RPC wrapper (shared with ``scripts/chaos_setup.py``); ``invoke_chaos_hook``
is the one-shot the runner uses. Failures raise ``ChaosInvocationError``, so a
seeding failure is never read as a scenario failure.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Final

import httpx

_DEFAULT_TIMEOUT_SECONDS = 15.0


class ChaosInvocationError(RuntimeError):
    """The platform rejected or errored on a chaos-hook invocation."""


# The chaos refusals a seeding failure can legitimately be, and what each MEANS
# (WO-R2-16). Every tool-level refusal arrives as JSON-RPC ``-32011``, so on the
# code alone a fixture-name collision and a Kafka outage read the same — and the
# reflex to re-run is wrong for every entry here: each says the WORLD is not what
# the scenario assumes. ``*_name_in_use`` (409) means a previous run's row or chain
# has DRIFTED; the fix is `make eval-reset PURGE_IDEMPOTENCY=1`, never a retry.
# A message ledger only: an unledgered code still surfaces with its wire name.
_REFUSAL_MEANINGS: Final[dict[str, str]] = {
    "poison_fixture_name_in_use": (
        "a poison_message row under this fixture_name already exists and no longer "
        "matches the declared fixture — reset the world, do not retry"
    ),
    "mislabeled_fixture_name_in_use": (
        "a create_mislabeled_dlq_job row under this fixture_name already exists and "
        "has been re-classified — reset the world, do not retry"
    ),
    "bad_data_fixture_name_in_use": (
        "a create_bad_data_job row under this fixture_name already exists and has "
        "drifted — reset the world, do not retry"
    ),
    "stuck_chain_name_in_use": (
        "a create_stuck_dag chain under this chain_name already exists and has "
        "drifted — reset the world, do not retry"
    ),
}


def _refusal_code(err_body: Mapping[str, Any]) -> str | None:
    """The platform's own name for this refusal, out of ``error.data.error_code``."""
    data = err_body.get("data")
    if not isinstance(data, Mapping):
        return None
    code = data.get("error_code")
    return code if isinstance(code, str) and code else None


def _refusal_hint(err_body: object) -> str:
    """The one sentence that says what a ledgered refusal means for the operator."""
    if not isinstance(err_body, Mapping):
        return ""
    code = _refusal_code(err_body)
    meaning = _REFUSAL_MEANINGS.get(code) if code is not None else None
    return f" — {meaning}" if meaning else ""


def _error_text(content: list[Any]) -> str:
    """Join the text blocks of an errored tool result, for the raised message.

    The hook name says which fault failed to seed; this says why.
    """
    parts = [
        block["text"]
        for block in content
        if isinstance(block, dict) and isinstance(block.get("text"), str)
    ]
    return " ".join(parts)[:200]


class ChaosClient:
    """Minimal JSON-RPC caller for the platform's chaos endpoints.

    Deliberately not the agent's ``MCPClient`` — chaos is an operator concern.
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
        """Fire one hook and return its parsed result. Every failure raises ChaosInvocationError."""
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }
        # Every transport-shaped failure becomes a ChaosInvocationError: without
        # this, a 502 during seeding crashed past the runner's handler with the
        # hook name — which fault failed to seed — nowhere in it.
        try:
            response = self._client.post(self._url, json=body)
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as err:
            raise ChaosInvocationError(
                f"{tool_name}: platform returned HTTP {err.response.status_code}: "
                f"{err.response.text[:200]}"
            ) from err
        except httpx.HTTPError as err:
            raise ChaosInvocationError(
                f"{tool_name}: transport failure: {type(err).__name__}: {err}"
            ) from err
        except ValueError as err:
            raise ChaosInvocationError(f"{tool_name}: response was not JSON: {err}") from err
        if not isinstance(payload, dict):
            raise ChaosInvocationError(
                f"{tool_name}: non-object JSON response: {type(payload).__name__}"
            )
        if "error" in payload:
            err_body = payload["error"]
            # isinstance-guard before `.get`: a non-object error member made
            # `.get` raise AttributeError, straight past the runner's handler.
            if isinstance(err_body, dict):
                detail = f"{err_body.get('code')}: {err_body.get('message')}"
                # The JSON-RPC `code` is -32011 for every tool-level refusal, so
                # the name lives in `data.error_code`. Prefixed, so it leads the
                # message the runner puts on a failed seed.
                code = _refusal_code(err_body)
                if code is not None:
                    detail = f"{code} ({detail})"
            else:
                detail = repr(err_body)
            raise ChaosInvocationError(
                f"{tool_name}: platform returned MCP error {detail}{_refusal_hint(err_body)}"
            )
        result = payload.get("result", {})
        if not isinstance(result, dict):
            return {}
        content = result.get("content", [])
        if not isinstance(content, list):
            content = []
        # A tool-level failure rides on `result.isError`, not the JSON-RPC `error`
        # member — a failed hook is a 200 with a success envelope, and reading only
        # the envelope graded the agent on a world nobody manufactured. Both
        # spellings: the wire is camelCase, fixtures are snake (C-02).
        if result.get("isError") or result.get("is_error"):
            raise ChaosInvocationError(
                f"{tool_name}: hook failed at the tool level (isError): "
                f"{_error_text(content) or '<no content>'}"
            )
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                try:
                    parsed = json.loads(block["text"])
                except ValueError as err:
                    raise ChaosInvocationError(
                        f"{tool_name}: result content was not JSON: {err}"
                    ) from err
                if isinstance(parsed, dict):
                    return parsed
        return {}

    def close(self) -> None:
        """Release the underlying HTTP connection."""
        self._client.close()


def invoke_chaos_hook(
    mcp_url: str,
    token: str,
    hook_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """One-shot: build client, fire hook, close, return parsed result.

    Used by the runner when a scenario declares its own chaos setup.
    """
    client = ChaosClient(mcp_url, token)
    try:
        return client.call(hook_name, arguments)
    finally:
        client.close()
