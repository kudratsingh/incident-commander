"""Compare canned scenario fixtures against what the platform actually returns.

Canned ``canned_tool_responses`` are supposed to be recordings of real
platform responses (``docs/eval-methodology.md``). Nothing ever checked
that. ``consumer_lag_high`` asserted ``lag: 1200`` against a live ``0`` for
months, in a repo that also committed the trace proving it, and the suite
stayed green the whole time — because the fixture was standing in for the
queue traffic nobody had built. That is the general shape of the failure:
**a hand-written fixture can silently impersonate a component that does not
exist**, and a green offline suite is then evidence of nothing.

Three checks, because the drift arrives in three different shapes:

``live_only_field`` / ``canned_only_field``
    The key sets disagree. Either the platform returns something no fixture
    models — so the agent has never been offered it offline — or the fixture
    invents a field the platform does not return, and any expectation
    reading it is grading the fixture rather than the agent.

``value``
    A top-level scalar disagrees. This is the lag-1200 class: same shape,
    same type, a number that cannot occur. Fields that legitimately move
    between observations (clocks, gauges, latencies) are declared volatile
    per tool and are checked for type only.

``not_live_reachable``
    A scalar inside a list element whose value appears nowhere in the live
    response's value domain for that path. Row-by-row equality would be the
    wrong test — a fixture models a different world state and its rows are
    allowed to differ — but a ``status`` of ``succeeded`` when the platform
    only ever emits ``completed``, or a pinned ``job_id`` that exists in no
    row, is a value the agent can never actually meet. The snapshot cannot
    settle this: its ``outputSchema`` types these fields as plain strings
    with no ``enum``, so the running platform is the only authority.

``no_live_rows``
    The fixture carries rows where the platform has none. Deliberately ONE
    finding rather than a ``canned_only_field`` per key: an empty live list
    supports exactly one conclusion and says nothing about row shape, so
    fanning it out would make the result depend on how wide the fixture
    happens to be rather than on what is wrong.

The comparison is pure and lives here so it can be unit-tested offline
against synthetic payloads; ``tests/integration/test_canned_fixtures_match_live.py``
supplies the live half.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from evals.scenarios.schema import Scenario
from incident_commander.tools.mcp_client import ToolResult

# Fields that legitimately differ between two honest observations. Checked
# for presence and JSON type, never for value. Keys are tool names; values
# are dotted paths within the parsed payload, written WITHOUT list markers
# (``items.created_at``, not ``items[].created_at``) — lookup strips them,
# so one entry covers a field however deeply it is nested in lists.
#
# Deliberately per tool and deliberately short: every entry here is a check
# the drift guard is NOT performing, so a broad one silently buys back the
# problem this module exists to catch. Note what is absent — ``lag`` is a
# cached gauge and still not volatile, because its value is the whole point
# of the lag scenarios.
_VOLATILE: Final[Mapping[str, frozenset[str]]] = {
    # Recomputed every 60s from real offsets and cached with a 90s TTL.
    # The VALUE is the whole point of the lag scenarios, so it is NOT
    # volatile — only the reading's freshness metadata would be.
    "get_consumer_lag": frozenset(),
    "get_postgres_health": frozenset({"ping_latency_ms", "active_connections"}),
    "get_redis_health": frozenset(
        {"ping_latency_ms", "connected_clients", "used_memory_bytes", "evicted_keys"}
    ),
    "list_dlq_messages": frozenset({"items.created_at", "items.updated_at"}),
    "list_active_alerts": frozenset({"alerts.fired_at", "alerts.created_at"}),
    "list_incidents": frozenset({"incidents.fired_at", "incidents.created_at"}),
    "list_audit_events": frozenset({"events.created_at", "events.request_id"}),
    "get_deploy_history": frozenset({"entries.deployed_at"}),
    "get_dag_state": frozenset({"nodes.created_at"}),
    "get_trace": frozenset({"jobs.created_at", "audit_events.created_at"}),
    "search_traces": frozenset({"matches.created_at"}),
}

# Free-text fields whose value domain is unbounded, so live-domain
# membership says nothing. Same per-tool shape as _VOLATILE.
_UNBOUNDED_TEXT: Final[Mapping[str, frozenset[str]]] = {
    "list_dlq_messages": frozenset(
        {"items.error_message", "items.triage.suggested_fix", "items.triage.summary"}
    ),
    "list_active_alerts": frozenset({"alerts.title", "alerts.description"}),
    "list_incidents": frozenset({"incidents.title", "incidents.description"}),
    "get_deploy_history": frozenset({"entries.notes"}),
}


@dataclass(frozen=True)
class CannedCall:
    """One canned response, and the call the offline run serves it for."""

    scenario: str
    tool: str
    arguments: Mapping[str, Any]
    payload: Mapping[str, Any]
    # Index within a sequenced fixture (``get_consumer_lag: [before, after]``).
    index: int = 0

    @property
    def label(self) -> str:
        suffix = f"[{self.index}]" if self.index else ""
        return f"{self.scenario}:{self.tool}{suffix}"


@dataclass(frozen=True)
class Drift:
    """One disagreement between a canned fixture and the live platform."""

    scenario: str
    tool: str
    path: str
    kind: str
    canned: Any = None
    live: Any = None
    # Which element of a sequenced fixture disagreed. Reported, never keyed
    # — see ``key``.
    index: int = 0

    @property
    def key(self) -> tuple[str, str, str, str]:
        """Identity for the known-drift ledger. Deliberately excludes the
        observed values: a fixture whose wrong value changes is still the
        same unfixed drift, and re-blessing on every value wobble would make
        the ledger a rubber stamp.

        It excludes ``index`` for the same reason, and the reason is worth
        stating because the opposite is tempting. The ledger's unit is the
        work: one line per recording that has to be corrected. A sequenced
        fixture's elements are two recordings of ONE field, and whoever fixes
        it opens one file and fixes the path — so keying by element would
        split one job across two lines, and a scenario that grows a third
        probe would silently regrow the ledger it is supposed to shrink.
        The index belongs in the report, which is where you look to find out
        which element to edit; ``describe`` carries it.
        """
        return (self.scenario, self.tool, self.path, self.kind)

    def describe(self) -> str:
        element = f"[{self.index}]" if self.index else ""
        return (
            f"{self.scenario}:{self.tool}{element} {self.path} [{self.kind}] "
            f"canned={_short(self.canned)} live={_short(self.live)}"
        )


def _short(value: Any, limit: int = 60) -> str:
    text = json.dumps(value, default=str)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _planner_arguments(scenario: Scenario) -> dict[str, dict[str, Any]]:
    """Tool → the arguments this scenario's canned planner calls it with.

    ``canned_tool_responses`` is keyed by tool NAME only — ``CannedMCPClient``
    looks up the queue by name and never reads the arguments — so the fixture
    itself does not record what call it stands for. The canned planner does:
    its scripted ``next_action`` is exactly the call the offline run makes, so
    it is the arguments the fixture is the answer to. Taking them from here
    rather than from a hand-written table means a scenario that changes what
    it probes cannot drift away from what this check probes.
    """
    found: dict[str, dict[str, Any]] = {}
    for step in scenario.canned_llm_responses.get("investigation_planner", []):
        action = step.get("next_action") or {}
        if action.get("kind") == "probe" and isinstance(action.get("tool_name"), str):
            found.setdefault(action["tool_name"], dict(action.get("arguments") or {}))
    for step in scenario.canned_llm_responses.get("remediation_planner", []):
        for tool_key, args_key in (
            ("action_tool", "action_arguments"),
            ("verify_tool", "verify_arguments"),
        ):
            tool = step.get(tool_key)
            if isinstance(tool, str):
                found.setdefault(tool, dict(step.get(args_key) or {}))
    return found


def _payloads(result: ToolResult) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for block in result.content:
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            parsed = json.loads(block["text"])
            if isinstance(parsed, dict):
                out.append(parsed)
    return out


def canned_calls(scenarios: Iterable[Scenario]) -> tuple[CannedCall, ...]:
    """Every canned response, paired with the call it answers."""
    calls: list[CannedCall] = []
    for scenario in scenarios:
        arguments_by_tool = _planner_arguments(scenario)
        for tool, canned in sorted(scenario.canned_tool_responses.items()):
            results = (canned,) if isinstance(canned, ToolResult) else tuple(canned)
            for index, result in enumerate(results):
                if result.is_error:
                    continue  # models a transport failure, not a payload
                for payload in _payloads(result):
                    calls.append(
                        CannedCall(
                            scenario=scenario.name,
                            tool=tool,
                            arguments=arguments_by_tool.get(tool, {}),
                            payload=payload,
                            index=index,
                        )
                    )
    return tuple(calls)


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int | float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, Mapping):
        return "object"
    if isinstance(value, Sequence):
        return "array"
    return type(value).__name__


def compare(call: CannedCall, live: Mapping[str, Any]) -> list[Drift]:
    """Every way ``call``'s canned payload disagrees with the live response.

    ``live`` must be the snapshot taken for THIS element's position in the
    sequence — see ``fixture_probe.probe_live``. Handing element 1 the
    snapshot element 0 was compared against is what made a deliberately
    post-action recording drift by construction.
    """
    volatile = _VOLATILE.get(call.tool, frozenset())
    unbounded = _UNBOUNDED_TEXT.get(call.tool, frozenset())
    drifts: list[Drift] = []

    def record(path: str, kind: str, canned: Any = None, live_value: Any = None) -> None:
        drifts.append(
            Drift(
                scenario=call.scenario,
                tool=call.tool,
                path=path,
                kind=kind,
                canned=canned,
                live=live_value,
                index=call.index,
            )
        )

    def walk(canned_node: Any, live_node: Any, path: str, in_list: bool) -> None:
        if isinstance(canned_node, Mapping) and isinstance(live_node, Mapping):
            for key in sorted(set(canned_node) - set(live_node)):
                record(_join(path, key), "canned_only_field", canned_node[key])
            for key in sorted(set(live_node) - set(canned_node)):
                record(_join(path, key), "live_only_field", None, live_node[key])
            for key in sorted(set(canned_node) & set(live_node)):
                walk(canned_node[key], live_node[key], _join(path, key), in_list)
            return

        if isinstance(canned_node, list) and isinstance(live_node, list):
            # Rows are allowed to differ — a fixture models a different world
            # state. What must hold is that the row SHAPE matches and that
            # every canned leaf value is one the platform can emit. Lists of
            # objects collapse to one representative row; lists of scalars go
            # straight to the leaf check (merging them again would recurse
            # forever, since the merge of a scalar list is a scalar list).
            if canned_node and not live_node:
                # ONE finding, not one per field. An empty live list supports
                # exactly one conclusion — the platform currently has no rows
                # here — and nothing at all about row shape. Reporting a
                # `canned_only_field` per key would turn that single fact into
                # a dozen, and make the result depend on how wide the fixture
                # happens to be rather than on what is wrong.
                record(f"{path}[]", "no_live_rows", len(canned_node), 0)
                return
            if _has_mappings(canned_node) or _has_mappings(live_node):
                walk(_merge_rows(canned_node), _merge_rows(live_node), f"{path}[]", True)
            else:
                walk_leaves(canned_node, live_node, f"{path}[]")
            return

        if _policy_path(path) in volatile:
            if _differs_in_type(canned_node, live_node):
                record(path, "type", canned_node, live_node)
            return

        if _differs_in_type(canned_node, live_node) and not in_list:
            record(path, "type", canned_node, live_node)
            return

        if in_list:
            walk_leaves(canned_node, live_node, path)
            return

        if canned_node != live_node:
            record(path, "value", canned_node, live_node)

    def walk_leaves(canned_node: Any, live_node: Any, path: str) -> None:
        """In-list leaves: every canned value must be one the platform emits.

        Membership is checked by JSON type first, then by value, mirroring
        the grader's ``FieldComparator.satisfied_by``. Plain ``in`` uses
        ``==``, and in Python ``True == 1`` — so a canned boolean counted as
        reachable whenever the platform emitted the corresponding integer,
        absorbing exactly the bool-vs-number contract drift the grader
        refuses to let pass. A type the platform never emits at this path is
        reported as ``type``, not as an unreachable value: the fixture is not
        modelling a row the platform lacks, it is modelling a field the
        platform does not have.
        """
        normalized = _policy_path(path)
        if normalized in volatile or normalized in unbounded:
            return
        domain = [v for v in _flatten(live_node) if v is not None]
        live_types = {_json_type(v) for v in domain}
        for value in (v for v in _flatten(canned_node) if v is not None):
            # An all-null live domain says nothing about the field's type,
            # so it stays an unreachable VALUE rather than becoming a
            # contract claim the observation cannot support.
            if live_types and _json_type(value) not in live_types:
                record(path, "type", value, sorted(live_types))
            elif not any(_json_type(v) == _json_type(value) and v == value for v in domain):
                record(path, "not_live_reachable", value, sorted({_short(v) for v in domain}))

    walk(dict(call.payload), dict(live), "", False)
    return drifts


def _differs_in_type(canned: Any, live: Any) -> bool:
    """A real contract type change — null on either side does not count.

    ``null`` is a legal VALUE of most fields here, not a different type: the
    platform returns ``lag: null`` for a group it cannot resolve, and that
    null contract is load-bearing enough to have its own scenario
    (``consumer_lag_null_unknown_state``). Calling number-vs-null a type
    change also made the drift kind depend on how long the stack had been
    up — a freshly booted platform reports ``null`` for worker-dispatcher
    until the 60s metrics loop first runs, then ``0`` — so the same fixture
    defect landed in the ledger under two different keys depending on
    timing, and the ratchet's stale check would flip one of them red on
    every other run.
    """
    if canned is None or live is None:
        return False
    return _json_type(canned) != _json_type(live)


def _policy_path(path: str) -> str:
    """Path with list markers stripped, for _VOLATILE / _UNBOUNDED_TEXT lookup."""
    return path.replace("[]", "")


def _join(path: str, key: str) -> str:
    return f"{path}.{key}" if path else key


def _flatten(node: Any) -> list[Any]:
    if isinstance(node, list):
        return [v for element in node for v in _flatten(element)]
    return [node]


def _has_mappings(elements: Sequence[Any]) -> bool:
    return any(isinstance(element, Mapping) for element in elements)


def _merge_rows(elements: Sequence[Any]) -> dict[str, list[Any]]:
    """Collapse a list of objects into one representative row.

    Keys are the union across elements — so a field only some rows carry is
    still compared — and each value is the list of that key's values, which
    the leaf check then treats as a value domain rather than positionally.
    """
    merged: dict[str, list[Any]] = {}
    for element in elements:
        if not isinstance(element, Mapping):
            continue
        for key, value in element.items():
            merged.setdefault(key, []).append(value)
    return merged
