"""Wire-level idempotency contract test against the pinned platform.

**What this test enforces**

The platform's idempotency store dedupes by (idempotency_key, hash of
the call's arguments). Both sides of that hash must agree:

- **Same key + same args** → cached replay (no re-execute).
- **Same key + different args** → 409, treated as a contract violation
  ("caller changed the plan under an old key").
- **Fresh key** → fresh execute (regardless of args).

How much normalization the platform applies before hashing — whether key
ORDER counts — is not assumed here; it is what
``test_same_key_reordered_args_is_still_a_cache_hit`` below documents.

This test proves the agent's ``wire_arguments`` output stays inside
the tolerance window the platform allows, without importing anything
from the platform repo — the contract is verified over the wire against
the digest-pinned MCP endpoint. That way a platform-side refactor of
the internal args normalizer surfaces as a test failure here (in the
same CI job that already catches ``tools/list`` drift via
``test_contract_snapshot.py``), not as a silent 409 in production.

**Observation trick**

``restart_consumer_group`` is a good probe because its result field
``kill_key_cleared`` distinguishes fresh execute from cache replay:

- Fresh execute with the kill flag set  → ``kill_key_cleared: true``.
- Fresh execute with no kill flag set   → ``kill_key_cleared: false``.
- Cache replay of a previous ``true``   → ``kill_key_cleared: true``,
                                          even though the flag was
                                          already cleared by the
                                          original execute.

Mirroring the platform's own kill-key trick from the platform's
integration tests, from the client side.

**Where this runs**

``make test-idempotency``, wired into CI's ``contract`` job as its LAST
step (WO-R2-43). Before that it executed in no job at all: the ``test``
job runs it without ``PLATFORM_MCP_URL``/``PLATFORM_TOKEN`` so it
self-skipped, and the ``contract`` job — which has those credentials —
ran only the schema diff and the fixture-value drift walk. ADR 0008
deleted the client-side execute-once guard on the strength of this file,
so for that whole period Tier-1 crash-resume was defended by a contract
nothing checked.

Last in the job on purpose: this is the only step there that WRITES. It
fires the ``kill_consumer`` chaos hook and restarts a consumer group
several times, and the two steps before it read that same stack.

The ``TestRefusalShapeIsSpecific`` class below needs no live environment
and runs everywhere, including the offline ``test`` job.

**Which platform these assertions describe — 2026-08-30 (wave-9 re-pin)**

The error codes and shapes asserted here are the ones the **currently
pinned** image serves: ``ghcr.io/kudratsingh/incident-platform:v0.6.0
@sha256:d22c18fa…``, the v0.6.0 release index. (The ``demo/compose.yml``
comments that still said v0.4.9 were corrected in the same PR that moved
this pin; the digest remains the truth.)

This pin is the first that INCLUDES platform #154, "put tools/call's
post-execution and error paths inside one transaction envelope" — the
section below was written when the pin predated it. Re-checked against
the v0.6.0 image's own ``app/mcp/protocol.py`` and ``handlers.py`` at the
re-pin, as the closing note asks, rather than trusting the prediction:
``MCP_TOOL_ERROR = -32011`` is unchanged, and the assertions in this file
still hold. The reasoning that predicted that outcome, kept because it is
what makes the result checkable:

- **#154 is a DB-transaction envelope, not a wire envelope.** It changes
  which savepoint a handler unwinds to, not the JSON on the wire.
- **The idempotency-conflict shape is byte-identical before and after
  it.** ``-32011`` + ``data.error_code == "idempotency_key_reused"``
  holds on the pinned image and on platform master. #154 adds a *second*
  path that reaches the same refusal (a post-execution store collision
  resolved by read-back) and deliberately emits the same body.

So a re-pin past #154 should not move anything this file asserts — and
it did not. What it does change, none of which these tests touch today,
now VERIFIED live on the v0.6.0 image rather than predicted:

- Unhandled platform exceptions become HTTP 500 with a JSON-RPC body
  (``-32603``, ``"internal server error"`` — confirmed at
  ``handlers.py:216`` and ``standalone.py:160``) instead of escaping as
  Starlette's plain-text ``Internal Server Error``. Both are non-2xx, so
  ``_call_tool``'s ``raise_for_status()`` still turns them into an
  ``httpx.HTTPStatusError`` rather than a ``PlatformToolError`` — worth
  knowing when reading a failure, and worth revisiting if this file ever
  wants to assert on that path.
- The tool-crash message (``"internal tool error"``, also ``-32603``) is
  unchanged and remains distinct from the catch-all above.

On the next re-pin, re-check this section against the platform's
``backend/app/mcp/protocol.py`` and ``handlers.py`` rather than trusting
the date on it. (Done for v0.6.0 — see the header note.)

**Skipping**

The live classes require ``PLATFORM_MCP_URL`` + ``PLATFORM_TOKEN`` with
``chaos:invoke`` scope. They skip cleanly without them. Local dev:
``make bootstrap-token`` (the service account it mints carries
``actions:execute`` + ``chaos:invoke``) then ``make test-idempotency``.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator
from typing import Any, Final

import httpx
import pytest

_CONSUMER_GROUP = "worker-dispatcher"

# JSON-RPC error codes the pinned platform uses on this surface, from the
# platform's own ``backend/app/mcp/protocol.py``. Every refusal below
# arrives as HTTP **200** with the error in the payload's ``error`` key —
# the platform never maps these to a 4xx status, so status-code checks
# cannot see them.
#
# -32002 MCP_FORBIDDEN  — missing scope. Carries no ``data`` at all.
# -32011 MCP_TOOL_ERROR — the idempotency conflict AND every other
#                         application error a tool handler raises. The
#                         code alone is NOT a discriminator; the specific
#                         refusal is named by ``data.error_code``.
_MCP_FORBIDDEN: Final[int] = -32002
_MCP_TOOL_ERROR: Final[int] = -32011
_IDEMPOTENCY_KEY_REUSED: Final[str] = "idempotency_key_reused"


class PlatformToolError(RuntimeError):
    """A JSON-RPC error returned by a ``tools/call``, with its shape intact.

    The point of the class is ``code`` and ``data``. The previous helper
    formatted both into a message string and raised a bare ``RuntimeError``,
    which made every refusal the platform can produce indistinguishable to
    an ``pytest.raises`` matcher — a scope denial, an unknown tool, a
    missing argument and the idempotency conflict all read the same.
    """

    def __init__(self, tool_name: str, code: int, message: str, data: Any) -> None:
        super().__init__(f"tools/call {tool_name} failed: code={code} message={message}")
        self.tool_name = tool_name
        self.code = code
        self.message = message
        # ``data`` is absent (not null) on refusals that carry no detail —
        # the platform serializes with ``exclude_none=True``.
        self.data: dict[str, Any] = data if isinstance(data, dict) else {}

    @property
    def error_code(self) -> str | None:
        """The platform's application-level error name, e.g.
        ``idempotency_key_reused``. ``None`` when the refusal carries no
        ``data.error_code`` — which is itself a meaningful distinction."""
        value = self.data.get("error_code")
        return value if isinstance(value, str) else None


def _error_from_payload(tool_name: str, payload: dict[str, Any]) -> PlatformToolError:
    """Build a typed error from a JSON-RPC error payload."""
    err = payload["error"]
    return PlatformToolError(
        tool_name,
        int(err.get("code", 0)),
        str(err.get("message", "")),
        err.get("data"),
    )


def _live_env_available() -> bool:
    return bool(os.getenv("PLATFORM_MCP_URL") and os.getenv("PLATFORM_TOKEN"))


def _call_tool(
    client: httpx.Client,
    url: str,
    tool_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Send ``tools/call``. Returns the parsed content dict.

    Raises ``PlatformToolError`` — carrying the JSON-RPC code and ``data``
    — when the platform refuses, so callers can assert WHICH refusal they
    got rather than merely that something failed.

    This used to also return the raw response bytes, for a "byte-for-byte"
    replay comparison that no caller ever performed (the one that claimed
    it compared parsed dicts). The bytes are not brought back because the
    claim was not worth restoring: identical bytes are not evidence of a
    cache hit — a re-execute that happened to produce the same result
    would serialize identically too. The ``kill_key_cleared`` observation
    below is the real discriminator, and it is a semantic one.
    """
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    }
    r = client.post(url, json=body)
    r.raise_for_status()
    payload = r.json()
    if "error" in payload:
        raise _error_from_payload(tool_name, payload)
    result = payload["result"]
    content = result.get("content", [])
    parsed: dict[str, Any] = {}
    for block in content:
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            parsed = json.loads(block["text"])
            break
    return parsed


def _fresh_key() -> str:
    return f"contract-test-{uuid.uuid4().hex}"


def _chaos_or_skip(client: httpx.Client, url: str, hook: str, args: dict[str, Any]) -> None:
    """Call a chaos hook; skip the test cleanly if the token lacks scope.

    The scope check is the whole reason this helper exists, and it used to
    look in two places the denial never appears. The platform answers a
    missing ``chaos:invoke`` scope with HTTP **200** and JSON-RPC
    ``-32002`` (``MCP_FORBIDDEN``) — not HTTP 403, and not ``-32601``/
    ``-32602``. So a scope-less token fell through to the ``RuntimeError``
    and the test reported a hard failure that reads like platform
    breakage. Skipping on ``-32002`` is what the guard always meant.

    Note the tension this creates and accept it deliberately: a skip here
    is invisible, and CI's token DOES carry ``chaos:invoke``
    (``scripts/bootstrap_agent_token.py``), so a skip in the contract job
    means the token was minted wrong. That is a job-configuration
    regression worth noticing rather than something to fail this test on.
    """
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": hook, "arguments": args},
    }
    r = client.post(url, json=body)
    # Kept for a platform that someday maps the denial onto a real status.
    if r.status_code == 403:
        pytest.skip(f"token lacks chaos:invoke; can't fire {hook!r}")
    r.raise_for_status()
    payload = r.json()
    if "error" in payload:
        err = _error_from_payload(hook, payload)
        if err.code == _MCP_FORBIDDEN:
            pytest.skip(f"token lacks the scope for {hook!r}: {err.message}")
        if err.code in (-32601, -32602) and "chaos" in err.message.lower():
            pytest.skip(f"chaos hook {hook!r} unavailable: {err.message}")
        raise err


@pytest.fixture(scope="module")
def live_client() -> Iterator[httpx.Client]:
    if not _live_env_available():
        pytest.skip("PLATFORM_MCP_URL and PLATFORM_TOKEN required")
    client = httpx.Client(
        headers={
            "Authorization": f"Bearer {os.environ['PLATFORM_TOKEN']}",
            "Content-Type": "application/json",
        },
        timeout=15.0,
    )
    yield client
    client.close()


@pytest.fixture(scope="module")
def mcp_url() -> str:
    return os.environ["PLATFORM_MCP_URL"]


class TestIdempotencyCache:
    """Prove the wire bytes we send match what the platform expects for dedup."""

    def test_same_key_same_bytes_replays_from_cache(
        self, live_client: httpx.Client, mcp_url: str
    ) -> None:
        # Set kill flag so the first restart has something to clear.
        _chaos_or_skip(live_client, mcp_url, "kill_consumer", {"consumer_group": _CONSUMER_GROUP})

        key = _fresh_key()
        args = {"consumer_group": _CONSUMER_GROUP, "idempotency_key": key}

        # First call: FRESH execute → kill_key_cleared=true (flag was set).
        first = _call_tool(live_client, mcp_url, "restart_consumer_group", args)
        assert first["kill_key_cleared"] is True

        # Second call with the SAME (key, args): cached replay. Semantic
        # equality, which is what this has always actually checked — the
        # "byte-for-byte" the old comment claimed was never performed, and
        # would not have been better evidence anyway. The real tell is that
        # kill_key_cleared is STILL true: a fresh execute at this point
        # would see the flag already cleared and return false.
        second = _call_tool(live_client, mcp_url, "restart_consumer_group", args)
        assert second == first, "cached replay should return the original result"
        assert second["kill_key_cleared"] is True

        # Sanity: a FRESH key over the same consumer_group at this point
        # returns kill_key_cleared=false (nothing to clear). Confirms the
        # previous "true" was cache replay, not a real re-execute.
        fresh_args = {"consumer_group": _CONSUMER_GROUP, "idempotency_key": _fresh_key()}
        third = _call_tool(live_client, mcp_url, "restart_consumer_group", fresh_args)
        assert third["kill_key_cleared"] is False, (
            "if this is True, the previous call re-executed instead of caching — "
            "the platform's arguments hash accepted our bytes but re-ran the tool"
        )

    def test_same_key_different_args_rejects_as_409(
        self, live_client: httpx.Client, mcp_url: str
    ) -> None:
        """Same key + different args → the idempotency conflict, specifically.

        This is the assertion the whole file exists for: ADR 0008 removed
        the client-side execute-once guard because the platform refuses a
        reused key, and ``loop.py`` re-invokes on a crash-resumed
        REMEDIATING run on the same strength (WO-R2-39).

        It used to assert only ``pytest.raises(RuntimeError, match=
        "restart_consumer_group failed")`` — the message ``_call_tool``
        stamped on EVERY refusal. An unknown tool (-32010), a missing
        ``idempotency_key`` (-32602), a revoked scope (-32002) or an
        internal error (-32603) all satisfied it, so the test would have
        stayed green through a platform that had stopped enforcing
        idempotency altogether and merely started rejecting the call for
        some other reason.
        """
        key = _fresh_key()
        # First call succeeds. No chaos needed — we only care about the
        # second call's rejection behavior.
        first_args = {"consumer_group": _CONSUMER_GROUP, "idempotency_key": key}
        _call_tool(live_client, mcp_url, "restart_consumer_group", first_args)

        # Same key, different consumer_group — must reject. The platform
        # exposes 409 as an MCP application error, not as a raw HTTP 409
        # (JSON-RPC standard: error surface is the payload's "error" key).
        second_args = {"consumer_group": "billing-consumer", "idempotency_key": key}
        with pytest.raises(PlatformToolError) as excinfo:
            _call_tool(live_client, mcp_url, "restart_consumer_group", second_args)

        err = excinfo.value
        # Both halves are load-bearing. -32011 is the platform's generic
        # "a tool handler raised an application error" code — every
        # AppError shares it — so the code alone would still admit a
        # consumer-group-not-found or a rate-limit refusal.
        # ``data.error_code`` is the only field that names THIS refusal.
        assert err.code == _MCP_TOOL_ERROR, (
            f"expected the tool-error code {_MCP_TOOL_ERROR} for a reused key, "
            f"got {err.code}: {err.message}"
        )
        assert err.error_code == _IDEMPOTENCY_KEY_REUSED, (
            f"expected data.error_code == {_IDEMPOTENCY_KEY_REUSED!r}, got "
            f"{err.error_code!r} (data={err.data!r}). The platform refused the "
            "call, but not as an idempotency conflict — the dedup guarantee "
            "ADR 0008 rests on may no longer be what refused it."
        )

    def test_same_key_reordered_args_is_still_a_cache_hit(
        self, live_client: httpx.Client, mcp_url: str
    ) -> None:
        # JSON keys are order-independent semantically but the raw bytes
        # differ. If the platform hashes over parsed args (order-
        # independent) we get a cache hit; if it hashes over raw bytes we
        # get a 409. This test DOCUMENTS which — the assertion is the
        # spec, not the reverse.
        _chaos_or_skip(live_client, mcp_url, "kill_consumer", {"consumer_group": _CONSUMER_GROUP})
        key = _fresh_key()

        first_args = {"consumer_group": _CONSUMER_GROUP, "idempotency_key": key}
        first = _call_tool(live_client, mcp_url, "restart_consumer_group", first_args)
        assert first["kill_key_cleared"] is True

        # Same fields, reversed insertion order → different serialized bytes.
        reordered = {"idempotency_key": key, "consumer_group": _CONSUMER_GROUP}
        second = _call_tool(live_client, mcp_url, "restart_consumer_group", reordered)
        # Platform's normalizer is order-independent (ADR 0010 on the
        # platform side): parsed args, not raw bytes. If this ever flips
        # to raw-bytes hashing, the wire_arguments helper must sort keys
        # before serialization — and this assertion will change with the
        # ADR update.
        assert second["kill_key_cleared"] is True, (
            "reordered args should hit the cache (order-independent hash spec); "
            "if this fails the platform switched to raw-byte hashing — sort keys "
            "in wire_arguments and update ADR 0010"
        )


class TestRefusalShapeIsSpecific:
    """Offline proof that the live assertion above discriminates.

    No live environment, no fixtures — this class runs in the ordinary
    ``test`` job. It exists because the assertion it guards can only be
    exercised for real inside the ``contract`` job against the pinned
    stack, and an assertion that is only ever observed passing is exactly
    how the vacuous version survived as long as it did.

    The payloads are the pinned platform's real wire bodies, transcribed
    from ``backend/app/mcp/handlers.py`` + ``protocol.py``. Every one of
    them arrives as HTTP 200.
    """

    # Same key, different args. The refusal the file is about.
    _CONFLICT = {
        "jsonrpc": "2.0",
        "id": 1,
        "error": {
            "code": -32011,
            "message": (
                "Idempotency key 'contract-test-abc' was previously used for tool "
                "'restart_consumer_group' with different arguments. Pick a fresh "
                "key or send the exact same arguments."
            ),
            "data": {"error_code": "idempotency_key_reused"},
        },
    }
    # A DIFFERENT application error from the same handler: identical code,
    # different data.error_code. The deliberately-wrong-error-code case.
    _OTHER_TOOL_ERROR = {
        "jsonrpc": "2.0",
        "id": 1,
        "error": {
            "code": -32011,
            "message": "consumer group 'billing-consumer' not found",
            "data": {"error_code": "consumer_group_not_found"},
        },
    }
    # Missing scope. No "data" key at all (the platform serializes with
    # exclude_none=True), so error_code reads None rather than raising.
    _SCOPE_DENIED = {
        "jsonrpc": "2.0",
        "id": 1,
        "error": {"code": -32002, "message": "missing required scope: chaos:invoke"},
    }
    # idempotency_key omitted entirely — schema validation, not the store.
    _MISSING_KEY = {
        "jsonrpc": "2.0",
        "id": 1,
        "error": {"code": -32602, "message": "idempotency_key is required"},
    }

    def test_the_conflict_payload_satisfies_the_live_assertion(self) -> None:
        err = _error_from_payload("restart_consumer_group", self._CONFLICT)
        assert err.code == _MCP_TOOL_ERROR
        assert err.error_code == _IDEMPOTENCY_KEY_REUSED

    def test_a_different_tool_error_does_not_satisfy_it(self) -> None:
        """The code matches and the assertion still refuses it.

        This is why the assertion checks ``data.error_code`` and not just
        the code: -32011 is the platform's generic "tool handler raised an
        AppError", shared by every one of them.
        """
        err = _error_from_payload("restart_consumer_group", self._OTHER_TOOL_ERROR)
        assert err.code == _MCP_TOOL_ERROR
        assert err.error_code != _IDEMPOTENCY_KEY_REUSED
        assert err.error_code == "consumer_group_not_found"

    @pytest.mark.parametrize(
        "payload_name",
        ["_OTHER_TOOL_ERROR", "_SCOPE_DENIED", "_MISSING_KEY"],
    )
    def test_the_old_assertion_accepted_every_refusal(self, payload_name: str) -> None:
        """The vacuous pass, pinned so it cannot come back.

        The previous assertion was ``pytest.raises(RuntimeError, match=
        "restart_consumer_group failed")``. ``PlatformToolError`` still
        subclasses ``RuntimeError`` and still carries that message, so this
        reproduces the old matcher exactly — and every unrelated refusal
        satisfies it, while none of them is an idempotency conflict.
        """
        payload: dict[str, Any] = getattr(self, payload_name)
        err = _error_from_payload("restart_consumer_group", payload)

        # What the old test asserted, verbatim in effect.
        assert isinstance(err, RuntimeError)
        assert "restart_consumer_group failed" in str(err)

        # What the new test asserts. Not the conflict.
        assert err.error_code != _IDEMPOTENCY_KEY_REUSED

    def test_scope_denial_carries_no_data_and_is_recognised(self) -> None:
        """``_chaos_or_skip`` skips on this; it used to raise instead.

        HTTP 200 with -32002 was invisible to a guard watching for HTTP 403
        and -32601/-32602.
        """
        err = _error_from_payload("kill_consumer", self._SCOPE_DENIED)
        assert err.code == _MCP_FORBIDDEN
        assert err.data == {}
        assert err.error_code is None
