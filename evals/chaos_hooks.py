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
from collections.abc import Mapping
from typing import Any, Final

import httpx

_DEFAULT_TIMEOUT_SECONDS = 15.0


class ChaosInvocationError(RuntimeError):
    """The platform rejected or errored on a chaos-hook invocation."""


# The chaos refusals a seeding failure can legitimately be, and what each one
# MEANS — the ledger WO-R2-16 asked for, kept here because this is the only
# place a chaos refusal passes through.
#
# Why it earns its place. Every tool-level refusal the platform raises arrives
# as JSON-RPC ``-32011`` with the name in ``data.error_code``, so on the code
# alone a fixture-name collision and a Kafka outage are the same event. The
# runner buckets a failed seed as ``shared-env`` and the operator reads the
# message; without the name, the message reads like transport flakiness and the
# reflex is to re-run — which is precisely wrong for every entry below. Each of
# these says the WORLD is not what the scenario assumes, and re-running seeds
# the same refusal again.
#
# ``*_fixture_name_in_use`` / ``stuck_chain_name_in_use`` (409) all mean one
# thing: a previous run's row or chain is still there and has DRIFTED from what
# the hook declares, so the hook refuses to rewrite somebody's evidence. The fix
# is `make eval-reset PURGE_IDEMPOTENCY=1` and a re-audit, never a retry and
# never a second fixture_name.
#
# Deliberately a MESSAGE ledger and not a control: nothing here changes what is
# raised or what the runner does with it. A code absent from this table still
# surfaces with its own name attached (the name comes off the wire, not out of
# this dict) — the table only adds the sentence a reader would otherwise have to
# go and find. That is why an unledgered future code cannot go quiet.
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

    The hook name says which fault failed to seed; this says why, which is
    the difference between "re-run it" and "the consumer group is misnamed
    in the scenario YAML".
    """
    parts = [
        block["text"]
        for block in content
        if isinstance(block, dict) and isinstance(block.get("text"), str)
    ]
    return " ".join(parts)[:200]


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
        # Every transport-shaped failure becomes a ChaosInvocationError, so
        # the caller's `except ChaosInvocationError` is the whole story. It
        # was not: raise_for_status, .json() on an HTML error page, and the
        # json.loads below all raised httpx/ValueError straight past the
        # runner's handler, so a 502 during seeding surfaced as a bare
        # "transport" crash with the hook name nowhere in it — the one
        # detail that says which fault failed to seed.
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
            # isinstance-guard before `.get`: a non-object error member (a
            # bare string, a list) made `.get` raise AttributeError, which
            # sails straight past the runner's `except ChaosInvocationError`
            # and reproduces the untyped crash this comment block says was
            # eliminated. The guard is what makes the claim above true for
            # every response shape, not just the well-formed ones.
            if isinstance(err_body, dict):
                detail = f"{err_body.get('code')}: {err_body.get('message')}"
                # The JSON-RPC `code` is a transport-shaped integer
                # (-32011 for every tool-level refusal the platform raises),
                # so it says nothing about WHICH refusal this is. The name
                # lives one level down in `data.error_code`, and dropping it
                # is what made a fixture-name collision read as flakiness.
                # Prefixed, not appended, so it is the first thing in the
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
        # A tool-level failure rides on `result.isError`, not on the JSON-RPC
        # `error` member — a failed hook is a 200 with a success envelope.
        # Reading only the envelope reported an unseeded fault to the runner
        # as a successful seed, and the run then graded the agent on a world
        # nobody manufactured. Both spellings on purpose: the wire is
        # camelCase, canned fixtures and trajectories are snake — the exact
        # split that left the agent's own escalate-on-error guard dead
        # against the wire until C-02.
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
