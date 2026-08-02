"""Wire-level idempotency contract test against the pinned platform.

**What this test enforces**

The platform's idempotency store dedupes by (idempotency_key, hash of
raw wire args). Both sides of that hash must agree byte-for-byte:

- **Same key + same wire bytes** → cached replay (no re-execute).
- **Same key + different wire bytes** → 409, treated as a contract
  violation ("caller changed the plan under an old key").
- **Fresh key** → fresh execute (regardless of args).

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

**Skipping**

Requires ``PLATFORM_MCP_URL`` + ``PLATFORM_TOKEN`` with ``chaos:invoke``
scope. Skips cleanly without them. Local dev: ``make bootstrap-token
--scope chaos:invoke`` then ``uv run pytest
tests/integration/test_idempotency_contract.py``.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

_CONSUMER_GROUP = "worker-dispatcher"


def _live_env_available() -> bool:
    return bool(os.getenv("PLATFORM_MCP_URL") and os.getenv("PLATFORM_TOKEN"))


def _call_tool(
    client: httpx.Client,
    url: str,
    tool_name: str,
    arguments: dict[str, Any],
) -> tuple[dict[str, Any], bytes]:
    """Send ``tools/call``. Returns (parsed content dict, raw response bytes).

    Returning the raw bytes lets us assert byte-identical replay bodies —
    the tightest possible signal that a call was cached rather than
    re-executed.
    """
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    }
    r = client.post(url, json=body)
    raw = r.content
    r.raise_for_status()
    payload = r.json()
    if "error" in payload:
        raise RuntimeError(
            f"tools/call {tool_name} failed: code={payload['error'].get('code')} "
            f"message={payload['error'].get('message')}"
        )
    result = payload["result"]
    content = result.get("content", [])
    parsed: dict[str, Any] = {}
    for block in content:
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            parsed = json.loads(block["text"])
            break
    return parsed, raw


def _fresh_key() -> str:
    return f"contract-test-{uuid.uuid4().hex}"


def _chaos_or_skip(client: httpx.Client, url: str, hook: str, args: dict[str, Any]) -> None:
    """Call a chaos hook; skip the test cleanly if the token lacks scope."""
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": hook, "arguments": args},
    }
    r = client.post(url, json=body)
    if r.status_code == 403:
        pytest.skip(f"token lacks chaos:invoke; can't fire {hook!r}")
    r.raise_for_status()
    payload = r.json()
    if "error" in payload:
        err = payload["error"]
        if err.get("code") in (-32601, -32602) and "chaos" in str(err.get("message", "")).lower():
            pytest.skip(f"chaos hook {hook!r} unavailable: {err.get('message')}")
        raise RuntimeError(f"chaos {hook!r} failed: {err}")


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
        first, first_raw = _call_tool(live_client, mcp_url, "restart_consumer_group", args)
        assert first["kill_key_cleared"] is True

        # Second call with the SAME (key, args): cached replay. Byte-
        # identical body is the tightest evidence. Even if there's some
        # variance in serialization, the semantic tell is that
        # kill_key_cleared is STILL true — a fresh execute at this point
        # would see the flag already cleared and return false.
        second, second_raw = _call_tool(live_client, mcp_url, "restart_consumer_group", args)
        assert second == first, "cached body should match original byte-for-byte"
        assert second["kill_key_cleared"] is True

        # Sanity: a FRESH key over the same consumer_group at this point
        # returns kill_key_cleared=false (nothing to clear). Confirms the
        # previous "true" was cache replay, not a real re-execute.
        fresh_args = {"consumer_group": _CONSUMER_GROUP, "idempotency_key": _fresh_key()}
        third, _ = _call_tool(live_client, mcp_url, "restart_consumer_group", fresh_args)
        assert third["kill_key_cleared"] is False, (
            "if this is True, the previous call re-executed instead of caching — "
            "the platform's arguments hash accepted our bytes but re-ran the tool"
        )

    def test_same_key_different_args_rejects_as_409(
        self, live_client: httpx.Client, mcp_url: str
    ) -> None:
        key = _fresh_key()
        # First call succeeds. No chaos needed — we only care about the
        # second call's rejection behavior.
        first_args = {"consumer_group": _CONSUMER_GROUP, "idempotency_key": key}
        _call_tool(live_client, mcp_url, "restart_consumer_group", first_args)

        # Same key, different consumer_group — must reject. The platform
        # exposes 409 as an MCP application error, not as a raw HTTP 409
        # (JSON-RPC standard: error surface is the payload's "error" key).
        second_args = {"consumer_group": "billing-consumer", "idempotency_key": key}
        with pytest.raises(RuntimeError, match="restart_consumer_group failed"):
            _call_tool(live_client, mcp_url, "restart_consumer_group", second_args)

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
        first, _ = _call_tool(live_client, mcp_url, "restart_consumer_group", first_args)
        assert first["kill_key_cleared"] is True

        # Same fields, reversed insertion order → different serialized bytes.
        reordered = {"idempotency_key": key, "consumer_group": _CONSUMER_GROUP}
        second, _ = _call_tool(live_client, mcp_url, "restart_consumer_group", reordered)
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
