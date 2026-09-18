"""Probe the live platform for the calls the canned fixtures answer.

Split from ``fixture_drift`` so the comparison stays pure and offline-
testable and only this module touches the network. Read tools only, under
the read-scoped principal — see ``probe_live``.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from evals.fixture_drift import CannedCall, Drift, compare
from incident_commander.tools.policies import Tier, tier_of
from incident_commander.tools.registry import TOOL_REGISTRY

_TIMEOUT_SECONDS = 20.0

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
    stack_context: str = "unknown"


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
    stack_context = "unknown"
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
        assert_seeded(http, mcp_url, token)
        for call in probed:
            # Keyed by POSITION as well as by call: element 1 is what the platform
            # said after the agent acted, so answering it from element 0's snapshot
            # compares a post-action recording against the pre-action world.
            key = (call.tool, json.dumps(dict(call.arguments), sort_keys=True), call.index)
            if key not in cache:
                cache[key] = _call_tool(http, mcp_url, token, call.tool, dict(call.arguments))
            payload, error = cache[key]
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
                stack_context = (
                    "warm"
                    if payload.get("lag_known") is True and payload.get("measured_at")
                    else "cold"
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
        compared=tuple(compared),
        stack_context=stack_context,
    )


def assert_seeded(client: httpx.Client, mcp_url: str, token: str) -> None:
    """Refuse to compare fixtures against a platform that carries no data.

    An unseeded platform answers every list tool with ``[]``, so every fixture
    looks wrong and the one real signal is buried. A check whose premise was never
    established reports that it could not run, not a result.
    """
    for tool, collection in _SEED_WITNESSES:
        payload, error = _call_tool(client, mcp_url, token, tool, {})
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
