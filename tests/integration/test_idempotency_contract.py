"""Wire-level idempotency contract test against the pinned platform.

WHAT IT ENFORCES. The platform dedupes by (idempotency_key, hash of the call's
arguments), and both sides of that hash must agree: same key and same args is a
cached replay, same key and different args is a 409 ("the caller changed the plan
under an old key"), and a fresh key always executes. How much normalization the
platform applies before hashing is not assumed — it is what
``test_same_key_reordered_args_is_still_a_cache_hit`` documents. So the agent's
``wire_arguments`` output is proved to stay inside the platform's tolerance window
over the wire, importing nothing from the platform repo.

THE OBSERVATION TRICK. ``restart_consumer_group``'s ``kill_key_cleared`` field
distinguishes a fresh execute from a cache replay: a fresh execute clears the flag
and reports what it found, while a replay of a previous ``true`` reports ``true``
again even though the flag is long gone. The platform's own kill-key trick, from the
client side.

WHERE IT RUNS. ``make test-idempotency``, wired into CI's ``contract`` job as its
LAST step (WO-R2-43) because it is the only step there that WRITES. Before that it
ran in no job at all — the ``test`` job self-skipped for want of credentials and the
``contract`` job ran only the schema diff — so for the whole period after ADR 0008
deleted the client-side execute-once guard, Tier-1 crash-resume was defended by a
contract nothing checked. ``TestRefusalShapeIsSpecific`` needs no live environment.

WHICH PLATFORM THESE ASSERTIONS DESCRIBE. The codes and shapes are the pinned
image's, re-checked at each re-pin rather than inherited: v0.6.2 moved schemas and
added ``bad_data_fixture_name_in_use``, v0.6.3 moved the tool count 29 → 30 and added
two more ``*_fixture_name_in_use`` refusals — all chaos-tool refusals nothing here
asserts, all ledgered in ``evals/chaos_hooks.py`` so a collision reads as "reset the
world" rather than flakiness. The pin also now INCLUDES platform #154, whose
transaction envelope is a DB envelope rather than a wire one: ``MCP_TOOL_ERROR =
-32011`` and the conflict body are byte-identical across it, verified on the image.
On the next re-pin, re-check this against the platform's ``protocol.py`` and
``handlers.py`` rather than trusting the date on it.

TWO PRINCIPALS SINCE v0.6.5. The Tier-1 calls replay under the AGENT's
``PLATFORM_TOKEN``; the ``kill_consumer`` hook that stages the world runs under the
EVALUATOR's ``PLATFORM_CHAOS_TOKEN``. The split is not cosmetic here — the agent
principal is REFUSED the hook on scope, so a file that kept firing it under that
token would report every chaos-dependent case as a skip: green, covering nothing.
The live classes skip cleanly without their credentials.
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

# JSON-RPC error codes the pinned platform uses on this surface, from its own
# ``protocol.py``. Every refusal below arrives as HTTP 200 with the error in the
# payload, so status-code checks cannot see them. -32002 MCP_FORBIDDEN is a missing
# scope and carries no ``data``; -32011 MCP_TOOL_ERROR is the idempotency conflict AND
# every other application error, so the code alone is NOT a discriminator —
# ``data.error_code`` names the specific refusal.
_MCP_FORBIDDEN: Final[int] = -32002
_MCP_TOOL_ERROR: Final[int] = -32011
_IDEMPOTENCY_KEY_REUSED: Final[str] = "idempotency_key_reused"


class PlatformToolError(RuntimeError):
    """A JSON-RPC error returned by a ``tools/call``, with its shape intact.

    The point of the class is ``code`` and ``data``. The previous helper formatted both
    into a message and raised a bare ``RuntimeError``, which made a scope denial, an
    unknown tool, a missing argument and the idempotency conflict all read the same to a
    ``pytest.raises`` matcher.
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

    Raises ``PlatformToolError`` carrying the JSON-RPC code and ``data``, so callers can
    assert WHICH refusal they got. It used to also return the raw bytes for a
    "byte-for-byte" replay comparison no caller performed: identical bytes are not
    evidence of a cache hit, since a re-execute producing the same result serializes
    identically too. ``kill_key_cleared`` is the real, semantic discriminator.
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

    The scope check is why this exists, and it used to look in two places the denial
    never appears: the platform answers a missing ``chaos:invoke`` with HTTP 200 and
    JSON-RPC ``-32002``, not HTTP 403 and not ``-32601``/``-32602``, so a scope-less
    token fell through to a hard failure that reads like platform breakage.

    Note the tension and accept it deliberately: a skip here is invisible, and the client
    passed in is the CHAOS client, whose token ``bootstrap_agent_token.py`` mints with
    ``chaos:invoke`` — so a skip in the contract job means the credential was minted or
    wired wrong, which is a job-configuration regression rather than something to fail
    this test on. It is also why the fixture skips LOUDLY on an absent
    ``PLATFORM_CHAOS_TOKEN`` rather than reusing ``PLATFORM_TOKEN``.
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
def chaos_client() -> Iterator[httpx.Client]:
    """The EVALUATOR's client: the only principal that can seed a world.

    Separate from ``live_client`` because they are separate service accounts, and the
    point of the split is that neither can do the other's job — the agent cannot fire a
    hook, and the chaos principal holds no ``actions:execute``.
    """
    if not _live_env_available():
        pytest.skip("PLATFORM_MCP_URL and PLATFORM_TOKEN required")
    token = os.getenv("PLATFORM_CHAOS_TOKEN", "")
    if not token.strip():
        pytest.skip(
            "PLATFORM_CHAOS_TOKEN required to stage this case: since platform "
            "v0.6.5 chaos:invoke is its own principal and PLATFORM_TOKEN cannot "
            "fire a hook. Run `make bootstrap-token`."
        )
    client = httpx.Client(
        headers={
            "Authorization": f"Bearer {token}",
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
        self, live_client: httpx.Client, chaos_client: httpx.Client, mcp_url: str
    ) -> None:
        # Set kill flag so the first restart has something to clear. Fired
        # under the CHAOS principal; the replay below is the agent's.
        _chaos_or_skip(chaos_client, mcp_url, "kill_consumer", {"consumer_group": _CONSUMER_GROUP})

        key = _fresh_key()
        args = {"consumer_group": _CONSUMER_GROUP, "idempotency_key": key}

        # First call: FRESH execute → kill_key_cleared=true (flag was set).
        first = _call_tool(live_client, mcp_url, "restart_consumer_group", args)
        assert first["kill_key_cleared"] is True

        # Second call with the SAME (key, args): a cached replay, checked semantically, which
        # is what this always actually did. The real tell is that kill_key_cleared is STILL
        # true — a fresh execute here would find the flag already cleared and return false.
        second = _call_tool(live_client, mcp_url, "restart_consumer_group", args)
        assert second == first, "cached replay should return the original result"
        assert second["kill_key_cleared"] is True

        # Sanity: a FRESH key over the same consumer_group returns kill_key_cleared=false,
        # which confirms the previous "true" was a cache replay rather than a re-execute.
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

        The assertion the whole file exists for: ADR 0008 removed the client-side
        execute-once guard because the platform refuses a reused key, and ``loop.py``
        re-invokes on a crash-resumed REMEDIATING run on the same strength (WO-R2-39).

        It used to assert only the message ``_call_tool`` stamped on EVERY refusal, so an
        unknown tool, a missing key, a revoked scope or an internal error all satisfied it —
        and the test would have stayed green through a platform that stopped enforcing
        idempotency and merely started rejecting the call for some other reason.
        """
        key = _fresh_key()
        # First call succeeds. No chaos needed — we only care about the
        # second call's rejection behavior.
        first_args = {"consumer_group": _CONSUMER_GROUP, "idempotency_key": key}
        _call_tool(live_client, mcp_url, "restart_consumer_group", first_args)

        # Same key, different consumer_group — must reject. The platform exposes 409 as an MCP
        # application error rather than a raw HTTP 409, per the JSON-RPC standard.
        second_args = {"consumer_group": "billing-consumer", "idempotency_key": key}
        with pytest.raises(PlatformToolError) as excinfo:
            _call_tool(live_client, mcp_url, "restart_consumer_group", second_args)

        err = excinfo.value
        # Both halves are load-bearing: -32011 is the generic "a tool handler raised" code
        # shared by every AppError, so it alone would admit a group-not-found or a rate-limit
        # refusal. ``data.error_code`` is the only field that names THIS refusal.
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
        self, live_client: httpx.Client, chaos_client: httpx.Client, mcp_url: str
    ) -> None:
        # JSON keys are order-independent semantically but the raw bytes differ, so hashing
        # over parsed args gives a cache hit and hashing over raw bytes gives a 409. This test
        # DOCUMENTS which — the assertion is the spec, not the reverse.
        _chaos_or_skip(chaos_client, mcp_url, "kill_consumer", {"consumer_group": _CONSUMER_GROUP})
        key = _fresh_key()

        first_args = {"consumer_group": _CONSUMER_GROUP, "idempotency_key": key}
        first = _call_tool(live_client, mcp_url, "restart_consumer_group", first_args)
        assert first["kill_key_cleared"] is True

        # Same fields, reversed insertion order → different serialized bytes.
        reordered = {"idempotency_key": key, "consumer_group": _CONSUMER_GROUP}
        second = _call_tool(live_client, mcp_url, "restart_consumer_group", reordered)
        # The platform's normalizer is order-independent (its ADR 0010): parsed args, not raw
        # bytes. If that ever flips, ``wire_arguments`` must sort keys before serialization and
        # this assertion changes with the ADR update.
        assert second["kill_key_cleared"] is True, (
            "reordered args should hit the cache (order-independent hash spec); "
            "if this fails the platform switched to raw-byte hashing — sort keys "
            "in wire_arguments and update ADR 0010"
        )


class TestRefusalShapeIsSpecific:
    """Offline proof that the live assertion above discriminates.

    No live environment and no fixtures, so this runs in the ordinary ``test`` job. It
    exists because the assertion it guards can only be exercised for real inside the
    ``contract`` job, and an assertion only ever observed passing is how the vacuous
    version survived. The payloads are the pinned platform's real wire bodies,
    transcribed from its handlers, and every one arrives as HTTP 200.
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

        Why the assertion checks ``data.error_code`` and not just the code: -32011 is the
        generic "tool handler raised an AppError", shared by every one of them.
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

        ``PlatformToolError`` still subclasses ``RuntimeError`` and still carries the old
        message, so this reproduces the previous matcher exactly — and every unrelated
        refusal satisfies it while none of them is an idempotency conflict.
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
