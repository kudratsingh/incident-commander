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
    drifts: tuple[Drift, ...]
    errors: tuple[ProbeError, ...]
    checked: int
    skipped_write_tier: int
    live_calls: int
    # The ``(scenario, tool)`` pairs whose canned payload was actually
    # compared against a live reading. This is the run's COVERAGE, and it is
    # what licenses the bless path to delete a ledger entry: an entry may
    # only be dropped when this run genuinely disproved it, which requires
    # having probed it. ``checked`` is a count and cannot answer that.
    compared: tuple[tuple[str, str], ...] = ()


def unregistered_calls(calls: Iterable[CannedCall]) -> tuple[CannedCall, ...]:
    """Canned fixtures keyed by a name no tool in ``TOOL_REGISTRY`` answers.

    A ``canned_tool_responses`` key is the name the offline run serves the
    fixture for, so a typo names a call the agent can never make: the
    fixture is dead weight that no run will ever use, and it is invisible
    because the offline suite only ever looks up keys it already has.

    Reported rather than raised. ``tier_of`` raises ``KeyError`` for an
    unregistered name, so this used to abort the whole 95-fixture check on
    one misspelling — the loudest possible failure aimed at the smallest
    possible defect, and it told you nothing about the other ninety-four.
    """
    return tuple(call for call in calls if call.tool not in TOOL_REGISTRY)


def read_tier_calls(calls: Iterable[CannedCall]) -> tuple[CannedCall, ...]:
    """The calls this check may make.

    Tier-1 fixtures are excluded by construction, not by care: probing
    ``replay_dlq_by_category`` to see what it returns would replay the DLQ.
    Their canned payloads are checked for SHAPE only, offline, against the
    committed tool snapshot (``evals.fixture_shape``) — no live call, so no
    write. Their values stay unvalidated by any check, which is the real
    remaining hole, and the reason this check also runs under the
    read-scoped principal rather than trusting this filter alone.

    Unregistered names are filtered out here rather than classified: they
    are reported by ``unregistered_calls`` and must not reach ``tier_of``,
    whose refusal to guess is correct and fatal.
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

    ``token`` must be the READ-SCOPED principal. Two independent guards, in
    the order that matters: the caller passes a token the platform refuses
    Tier-1 calls under (``-32002 missing required scope``), and
    ``read_tier_calls`` never asks for one. The scope check is the real
    boundary; the filter is so a bug fails loudly rather than at the
    platform.

    Calls are probed in sequence order — every element 0 before any element
    1 — so that a later element's snapshot is genuinely a later reading of
    the world, which is the only thing that makes comparing it to a later
    recording mean anything.
    """
    all_calls = tuple(calls)
    unregistered = unregistered_calls(all_calls)
    probed = sorted(read_tier_calls(all_calls), key=lambda call: call.index)
    owned = client is None
    http = client or httpx.Client(timeout=_TIMEOUT_SECONDS)
    cache: dict[tuple[str, str, int], tuple[Mapping[str, Any] | None, str | None]] = {}
    drifts: list[Drift] = []
    compared: dict[tuple[str, str], None] = {}
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
            # Keyed by POSITION as well as by call. A sequenced fixture is a
            # record of successive observations — element 1 is what the
            # platform said after the agent acted — so answering every
            # element from element 0's snapshot compares a post-action
            # recording against the pre-action world, which no correct
            # fixture can survive. One fresh read per position; elements at
            # the same position still share it, so the cache keeps paying.
            key = (call.tool, json.dumps(dict(call.arguments), sort_keys=True), call.index)
            if key not in cache:
                cache[key] = _call_tool(http, mcp_url, token, call.tool, dict(call.arguments))
            payload, error = cache[key]
            if (error is not None or payload is None) and call.chaos_seeded:
                # A fixture whose premise a chaos hook manufactures is
                # probed against the world BEFORE that hook fires, and some
                # of those probes name an entity that does not exist yet —
                # `create_stuck_dag` derives its chain ids from the
                # chain_name, so `get_dag_state` answers "job not found"
                # until the hook has run. That is an OBSERVATION of the
                # un-faulted world, not a failure to observe it, and the
                # difference matters in both directions.
                #
                # Treating it as a ProbeError failed
                # test_every_fixture_was_actually_reachable and, worse, took
                # the fixture out of the comparison entirely: its ledger
                # entries then went unobserved, which the ratchet reads as
                # "fixed" and the bless refuses to delete (split_for_bless
                # carries what the run never reached). The entries would
                # have been stuck — permanently reported stale and
                # permanently undeletable.
                #
                # Comparing against an empty live payload keeps the fixture
                # IN the walk, so its disagreement stays recorded, stays
                # classified (post-fault, in the ledger), and stays
                # discharge-able the day the world can answer. Scoped to
                # chaos-declaring scenarios so a genuine 502 or a typo'd
                # tool name anywhere else is still the loud error it was.
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
    )


def assert_seeded(client: httpx.Client, mcp_url: str, token: str) -> None:
    """Refuse to compare fixtures against a platform that carries no data.

    An unseeded platform answers every list tool with ``[]``, and comparing
    fixtures to an empty world produces a page of confident nonsense: every
    fixture looks wrong, and the one real signal is buried. It happened the
    first time this check ran in CI — the ``contract`` job waits for the REST
    app's ``/healthz`` but nothing waited for the seeder, and ``test-contract``
    never noticed because ``tools/list`` needs no data.

    So the precondition is asserted rather than assumed. This is the same
    rule the eval suite is missing at the scenario level (``BUILD_PLAN.md``
    3.4): a check whose premise was never established does not report a
    result, it reports that it could not run.
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

    Every way a call can fail returns an error STRING, because the caller's
    ``ProbeError`` channel is the whole point: "this fixture went unchecked"
    is a result, and one bad gateway is not a reason to learn nothing about
    the other ninety-four fixtures. Only the MCP-level JSON-RPC error ever
    reached that channel before; an HTTP status, a body that was not JSON,
    and a connection that never opened all escaped from here and took the
    run with them.
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
