"""Probe the live platform for the calls the canned fixtures answer.

Split from ``fixture_drift`` so the comparison stays pure and offline-
testable and only this module touches the network. Read tools only, under
the read-scoped principal — see ``probe_live``.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final

import httpx

from evals.fixture_drift import CannedCall, Drift, compare
from incident_commander.tools.policies import Tier, tier_of
from incident_commander.tools.registry import TOOL_REGISTRY

_TIMEOUT_SECONDS = 20.0

# The MCP server allows 120 calls a minute per principal. A 429 is retried after each of these
# waits in turn, and the whole run sleeps at most _RATE_LIMIT_BUDGET_SECONDS on them.
_RATE_LIMIT_BACKOFF_SECONDS: Final[tuple[float, ...]] = (2.0, 4.0, 8.0, 16.0)
_RATE_LIMIT_BUDGET_SECONDS: Final[float] = 60.0
_RATE_LIMITED: Final[str] = "HTTP 429 from the platform"
_NO_LAG_READING: Final[str] = "no get_consumer_lag('worker-dispatcher') reading was compared"

# Read tools whose non-empty result proves the eval fixture pack is loaded.
# Both are seeded unconditionally by the platform's seed_eval_fixtures.py.
_SEED_WITNESSES: tuple[tuple[str, str], ...] = (
    ("list_dlq_messages", "items"),
    ("list_active_alerts", "alerts"),
)


class UnseededPlatformError(RuntimeError):
    """The platform is up but carries no fixture pack, so there is nothing to compare to."""


@dataclass(frozen=True)
class ProbeError:
    """A live call that could not be made, so its fixture went unchecked."""

    scenario: str
    tool: str
    detail: str


@dataclass(frozen=True)
class ProbeResult:
    """What one probing run checked, what it could not, and what it found."""

    drifts: tuple[Drift, ...]
    errors: tuple[ProbeError, ...]
    checked: int
    skipped_write_tier: int
    live_calls: int
    # The ``(scenario, tool)`` pairs actually compared against a live reading —
    # the run's COVERAGE, which is what licenses the bless path to delete a
    # ledger entry. ``checked`` is a count and cannot answer that.
    compared: tuple[tuple[str, str], ...] = ()
    # `warm` means the metrics loop measured worker-dispatcher's lag within its last window,
    # `cold` that it has not (just booted, or just after `make eval-reset`) — not the volume's age.
    stack_context: str = "unknown"
    stack_context_reason: str = _NO_LAG_READING
    # Pairs a 429 kept this run from reading, after the back-off (WO-R3-307). Not errors and
    # not compared: their ledger rows are neither new nor stale this run.
    rate_limited: tuple[tuple[str, str], ...] = ()
    # Distinct live calls still refused with a 429 after the back-off.
    rate_limited_calls: int = 0


def unregistered_calls(calls: Iterable[CannedCall]) -> tuple[CannedCall, ...]:
    """Canned fixtures keyed by a name no tool in ``TOOL_REGISTRY`` answers.

    A typo names a call the agent can never make, invisible because the offline
    suite only looks up keys it already has. Reported rather than raised: through
    ``tier_of`` one misspelling aborted the whole fixture check.
    """
    return tuple(call for call in calls if call.tool not in TOOL_REGISTRY)


def read_tier_calls(calls: Iterable[CannedCall]) -> tuple[CannedCall, ...]:
    """The calls this check may make.

    Tier-1 fixtures are excluded by construction: probing ``replay_dlq_by_category``
    would replay the DLQ. They get a SHAPE-only offline check
    (``evals.fixture_shape``); their values stay unvalidated, which is why the
    read-scoped principal — not this filter — is the real boundary.
    """
    return tuple(
        call for call in calls if call.tool in TOOL_REGISTRY and tier_of(call.tool) is Tier.READ
    )


def probe_live(
    calls: Iterable[CannedCall],
    *,
    mcp_url: str,
    token: str,
    client: httpx.Client | None = None,
) -> ProbeResult:
    """Compare every read-tier canned fixture against the live platform.

    ``token`` must be the READ-SCOPED principal: the platform's scope refusal
    (``-32002``) is the real boundary and ``read_tier_calls`` is the loud filter
    ahead of it. Probed in sequence order, so element 1 really is a later reading.
    """
    all_calls = tuple(calls)
    unregistered = unregistered_calls(all_calls)
    probed = sorted(read_tier_calls(all_calls), key=lambda call: call.index)
    owned = client is None
    http = client or httpx.Client(timeout=_TIMEOUT_SECONDS)
    cache: dict[tuple[str, str, int], tuple[Mapping[str, Any] | None, str | None]] = {}
    drifts: list[Drift] = []
    compared: dict[tuple[str, str], None] = {}
    rate_limited: dict[tuple[str, str], None] = {}
    backoff = _Backoff()
    stack_context = "unknown"
    stack_context_reason = _NO_LAG_READING
    errors: list[ProbeError] = [
        ProbeError(
            scenario=call.scenario,
            tool=call.tool,
            detail=(
                f"{call.tool!r} is in no TOOL_REGISTRY entry, so no run can ever serve this "
                "fixture — check the canned_tool_responses key for a typo"
            ),
        )
        for call in {(c.scenario, c.tool): c for c in unregistered}.values()
    ]
    try:
        assert_seeded(http, mcp_url, token, backoff=backoff)
        for call in probed:
            # Keyed by POSITION as well as by call: element 1 is what the platform
            # said after the agent acted, so answering it from element 0's snapshot
            # compares a post-action recording against the pre-action world.
            key = (call.tool, json.dumps(dict(call.arguments), sort_keys=True), call.index)
            if key not in cache:
                cache[key] = backoff.call(http, mcp_url, token, call.tool, dict(call.arguments))
            payload, error = cache[key]
            if error == _RATE_LIMITED:
                # Still refused after the back-off: no reading, so no verdict on its rows.
                rate_limited[call.scenario, call.tool] = None
                continue
            if (error is not None or payload is None) and call.chaos_seeded:
                # A chaos-seeded fixture is probed BEFORE its hook fires, so
                # `get_dag_state` answering "job not found" is an OBSERVATION of
                # the un-faulted world, not a failure to observe it. As a
                # ProbeError the fixture left the comparison entirely, so its
                # ledger entries went unobserved — which the ratchet reads as
                # "fixed" and the bless refuses to delete: permanently stuck.
                # Comparing against an empty payload keeps it in the walk.
                # Scoped to chaos-declaring scenarios, so a 502 elsewhere is
                # still the loud error it was.
                drifts.extend(compare(call, {}))
                compared[call.scenario, call.tool] = None
                continue
            if error is not None or payload is None:
                errors.append(
                    ProbeError(
                        scenario=call.scenario,
                        tool=call.tool,
                        detail=error or "no text content block in result",
                    )
                )
                continue
            drifts.extend(compare(call, payload))
            if (
                call.tool == "get_consumer_lag"
                and call.arguments.get("consumer_group") == "worker-dispatcher"
            ):
                measured = payload.get("lag_known") is True and bool(payload.get("measured_at"))
                stack_context = "warm" if measured else "cold"
                stack_context_reason = (
                    f"get_consumer_lag('worker-dispatcher') answered "
                    f"lag_known={json.dumps(payload.get('lag_known'))}, "
                    f"measured_at={json.dumps(payload.get('measured_at'))}: the metrics loop "
                    + ("has" if measured else "has not")
                    + " measured the lag within its last window"
                )
            compared[call.scenario, call.tool] = None
    finally:
        if owned:
            http.close()
    return ProbeResult(
        drifts=tuple(drifts),
        errors=tuple(errors),
        checked=len(probed),
        skipped_write_tier=len(all_calls) - len(probed) - len(unregistered),
        live_calls=len(cache),
        # A pair with one element read and another refused was not fully read.
        compared=tuple(pair for pair in compared if pair not in rate_limited),
        stack_context=stack_context,
        stack_context_reason=stack_context_reason,
        rate_limited=tuple(rate_limited),
        rate_limited_calls=sum(1 for _, error in cache.values() if error == _RATE_LIMITED),
    )


class _Backoff:
    """Retries a 429 after each of ``_RATE_LIMIT_BACKOFF_SECONDS``, within one run-wide budget."""

    def __init__(self) -> None:
        self.remaining = _RATE_LIMIT_BUDGET_SECONDS

    def call(
        self,
        client: httpx.Client,
        mcp_url: str,
        token: str,
        name: str,
        arguments: dict[str, Any],
    ) -> tuple[Mapping[str, Any] | None, str | None]:
        """``_call_tool``, retried while the platform answers 429 and the budget lasts."""
        for delay in _RATE_LIMIT_BACKOFF_SECONDS:
            payload, error = _call_tool(client, mcp_url, token, name, arguments)
            if error != _RATE_LIMITED or delay > self.remaining:
                return payload, error
            self.remaining -= delay
            time.sleep(delay)
        return _call_tool(client, mcp_url, token, name, arguments)


def assert_seeded(
    client: httpx.Client, mcp_url: str, token: str, *, backoff: _Backoff | None = None
) -> None:
    """Refuse to compare fixtures against a platform that carries no data.

    An unseeded platform answers every list tool with ``[]``, so every fixture
    looks wrong and the one real signal is buried. A check whose premise was never
    established reports that it could not run, not a result.
    """
    patient = backoff or _Backoff()
    for tool, collection in _SEED_WITNESSES:
        payload, error = patient.call(client, mcp_url, token, tool, {})
        if error is not None:
            raise UnseededPlatformError(f"seed witness {tool} failed: {error}")
        if payload and payload.get(collection):
            return
    witnesses = ", ".join(tool for tool, _ in _SEED_WITNESSES)
    raise UnseededPlatformError(
        f"the platform returned no rows from any of: {witnesses}. It is up but not "
        "seeded, so every fixture would compare against an empty world. Boot it with "
        "SEED_EVAL_FIXTURES=true and wait for the seeder to finish before probing."
    )


def _call_tool(
    client: httpx.Client,
    mcp_url: str,
    token: str,
    name: str,
    arguments: dict[str, Any],
) -> tuple[Mapping[str, Any] | None, str | None]:
    """``(payload, error)`` for one live call. Never raises on a failed call.

    Every failure returns an error STRING so it reaches the caller's ``ProbeError``
    channel: one bad gateway must not cost the other ninety-four fixtures.
    """
    try:
        response = client.post(
            mcp_url,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as err:
        if err.response.status_code == httpx.codes.TOO_MANY_REQUESTS:
            return None, _RATE_LIMITED
        return None, f"HTTP {err.response.status_code} from the platform"
    except httpx.HTTPError as err:
        # Connect, read, write, timeout, protocol — the platform was not
        # reachable, which is also what a platform still booting looks like.
        return None, f"transport failure: {type(err).__name__}: {err}"
    try:
        payload = response.json()
    except ValueError as err:
        # A proxy's HTML error page, a truncated body, an empty 200.
        return None, f"response body is not JSON: {err}"
    if not isinstance(payload, dict):
        return None, f"non-object JSON response: {type(payload).__name__}"
    if "error" in payload:
        error = payload["error"]
        return None, f"MCP error {error.get('code')}: {error.get('message')}"
    result = payload.get("result") or {}
    for block in result.get("content", []):
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            try:
                parsed = json.loads(block["text"])
            except ValueError as err:
                return None, f"tool result text is not JSON: {err}"
            if isinstance(parsed, dict):
                return parsed, None
    return None, "no text content block in result"
