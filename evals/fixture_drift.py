"""Compare canned scenario fixtures against what the platform actually returns.

Nothing checked that canned ``canned_tool_responses`` were real recordings, and
``consumer_lag_high`` asserted ``lag: 1200`` against a live ``0`` for months. Kinds:
``live_only_field``/``canned_only_field`` (key sets disagree), ``value`` (a scalar
that cannot occur), ``not_live_reachable`` (a row value the platform never emits),
``no_live_rows``. Live half: ``tests/integration/test_canned_fixtures_match_live.py``.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from evals.scenarios.schema import Scenario
from incident_commander.tools.mcp_client import ToolResult

# Fields that legitimately differ between two honest observations: checked for
# presence and JSON type, never for value. Per tool, dotted paths without list
# markers (``items.created_at``) — lookup strips them. The membership question is
# "does the fixture pack FIX this value?", and every entry is a check NOT being
# performed, so ``lag`` stays out: its value is the point of the lag scenarios.
_VOLATILE: Final[Mapping[str, frozenset[str]]] = {
    # Freshness metadata only: `lag_known` flips once the 60s loop first emits
    # (v0.6.0, plat #166), and v0.6.7's (plat #204, WO-R3-254) `measured_at`,
    # `age_seconds` and `recent_samples` are all properties of when you looked —
    # `recent_samples` even in its emptiness, which is why `compare` also exempts
    # a volatile list from `no_live_rows`. `lag` and `source` stay guarded, and the
    # key-set diff still reports a fixture that was never re-recorded.
    "get_consumer_lag": frozenset(
        {
            "lag_known",
            "measured_at",
            "age_seconds",
            "recent_samples",
            "recent_samples.lag",
            "recent_samples.measured_at",
        }
    ),
    "get_postgres_health": frozenset({"ping_latency_ms", "active_connections"}),
    # Gauges of a running server that nothing seeds: memory is whatever Redis
    # allocated, and the keyspace counters are monotonic over its lifetime, so no
    # canned value can be right and type is the only honourable claim.
    # `used_memory_human` is the bytes formatted by Redis, so it has to travel with
    # them — splitting the two is how "1.00G" sat in the ledger against "1.60M".
    "get_redis_health": frozenset(
        {
            "ping_latency_ms",
            "connected_clients",
            "used_memory_bytes",
            "used_memory_human",
            "keyspace_hits",
            "keyspace_misses",
        }
    ),
    # `ttl_seconds` is a countdown on a key seeded with a 24h expiry, so its value
    # says how long the stack has been up. `exists`, `type` and `size` stay guarded
    # (the pack fixes all three), and so do v0.6.8's (plat #209, WO-R3-267)
    # `records_referenced` / `records_found`: they are counts of SEEDED records, so
    # a fresh stack answers 3 / 3 every time and a recording can be right.
    "get_cache_key_info": frozenset({"ttl_seconds"}),
    # v0.6.11 (plat #218, WO-R3-217), read by WO-R3-229's cascade. One field, and it is a
    # clock by the tool's own description: `measured_at` is the answering process's time,
    # and every objective's window ends at it. Nothing else here is exempt — the counts,
    # the rates and both flags ARE the reading a scenario grades, so a canned world's
    # numbers stay guarded and its disagreement with an un-faulted stack is ledgered.
    "get_slo_status": frozenset({"measured_at"}),
    # v0.6.9 (plat #211, WO-R3-201), made from the four recordings under
    # `evals/recorded_worlds/jobs_not_progressing_*`. Two clocks (`measured_at`,
    # `relay_last_tick_at`), the two ages derived from them, `last_publish_at` (the
    # seeder writes no outbox rows), and the four `oldest_/newest_unpublished_*`
    # that flip to null with load. DELIBERATELY OUT: `unpublished_count` IS the
    # family's evidence, and `unpublished_past_attempt_limit`,
    # `relay_heartbeat_known` and `relay_tick_interval_s` are stable.
    "get_outbox_status": frozenset(
        {
            "measured_at",
            "last_publish_at",
            "seconds_since_last_publish",
            "relay_last_tick_at",
            "relay_heartbeat_age_s",
            "oldest_unpublished_at",
            "oldest_unpublished_age_s",
            "newest_unpublished_at",
            "newest_unpublished_age_s",
        }
    ),
    # Four clocks the seeder or an operator stamps fresh, so no recording matches:
    # `dead_lettered_at` (v0.6.0, plat #180, R2-53) and `fenced_at` (v0.6.2, plat
    # #198, WO-R2-158) beside the two originals. `fenced_by` is exempt for a
    # stronger reason — it carries a principal id minted by `make bootstrap-token`,
    # so it changes on every `down -v`. _VOLATILE is a LEAF-VALUE exemption, so
    # presence is still enforced and whether a fence LANDED stays a scenario claim
    # (`dlq_human_required_escalates` grades `items[].fenced_at is_null: false`).
    "list_dlq_messages": frozenset(
        {
            "items.created_at",
            "items.updated_at",
            "items.dead_lettered_at",
            "items.fenced_at",
            "items.fenced_by",
        }
    ),
    "list_active_alerts": frozenset({"alerts.fired_at", "alerts.created_at"}),
    "list_incidents": frozenset({"incidents.fired_at", "incidents.created_at"}),
    "list_audit_events": frozenset({"events.created_at", "events.request_id"}),
    "get_deploy_history": frozenset({"entries.deployed_at"}),
    "get_dag_state": frozenset({"nodes.created_at"}),
    # `jobs.updated_at` joins the two created_at clocks: the seeder stamps it fresh,
    # and it was already volatile through `list_dlq_messages` — the same field
    # cannot be a clock through one tool and a pinned value through another.
    "get_trace": frozenset({"jobs.created_at", "jobs.updated_at", "audit_events.created_at"}),
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
    # True when the fixture describes a world the scenario's hooks manufacture.
    # From ``Scenario.seeds_chaos``, not the legacy ``chaos_setup`` (``None`` on a
    # plan-declaring scenario, ADR 0037). One case needs it: "that entity does not
    # exist" against the UN-faulted world is an observation, not a probe failure —
    # see ``evals/fixture_probe.py``.
    chaos_seeded: bool = False

    @property
    def label(self) -> str:
        """The fixture named as ``scenario:tool[element]``, for reports."""
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
    # Which element of a sequenced fixture disagreed.
    index: int = 0

    @property
    def key(self) -> tuple[str, str, str, str, int]:
        """Identity for the known-drift ledger. Excludes the observed values.

        A fixture whose wrong value changes is the same unfixed drift, and
        re-blessing on every wobble would make the ledger a rubber stamp. The
        element counts: one path can need a pre-fault and a post-action reason.
        """
        return (self.scenario, self.tool, self.path, self.kind, self.index)

    def describe(self) -> str:
        """One line naming the fixture, the field, the kind, and both values."""
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

    ``canned_tool_responses`` is keyed by tool name only, so the fixture does not
    record what call it answers; the canned planner's scripted ``next_action`` does.
    Read from there, a scenario cannot drift away from what this check probes.
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
    """Every JSON object carried in a tool result's text blocks."""
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
                            chaos_seeded=scenario.seeds_chaos,
                        )
                    )
    return tuple(calls)


def _json_type(value: Any) -> str:
    """The JSON type name a Python value serializes to."""
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


def compare(
    call: CannedCall,
    live: Mapping[str, Any],
    *,
    shape_only: frozenset[str] = frozenset(),
) -> list[Drift]:
    """Every way ``call``'s canned payload disagrees with the live response.

    ``live`` must be the snapshot for THIS element's position in the sequence
    (``fixture_probe.probe_live``). ``shape_only`` — normalized paths compared for
    JSON type only, cut at that node — is EMPTY by default, so widening
    ``_VOLATILE`` stays the only way to forgive a canned fixture. Its one caller is
    ``evals/world_drift.py``, which asks a different question and declares its own.
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
        """Compare one canned node against its live counterpart, descending as it goes."""
        if path and _policy_path(path) in shape_only:
            # A caller-declared shape-only path: type, then stop. Checked FIRST so
            # the cut reaches containers too — ``extra_data`` is an open map whose
            # leaves cannot be listed. Type is compared over the flattened values;
            # an all-null side says nothing, the ``walk_leaves`` rule.
            canned_types = {_json_type(v) for v in _flatten(canned_node) if v is not None}
            live_types = {_json_type(v) for v in _flatten(live_node) if v is not None}
            if canned_types and live_types and not canned_types & live_types:
                record(path, "type", sorted(canned_types), sorted(live_types))
            return

        if isinstance(canned_node, Mapping) and isinstance(live_node, Mapping):
            for key in sorted(set(canned_node) - set(live_node)):
                record(_join(path, key), "canned_only_field", canned_node[key])
            for key in sorted(set(live_node) - set(canned_node)):
                record(_join(path, key), "live_only_field", None, live_node[key])
            for key in sorted(set(canned_node) & set(live_node)):
                walk(canned_node[key], live_node[key], _join(path, key), in_list)
            return

        if isinstance(canned_node, list) and isinstance(live_node, list):
            # Rows may differ — a fixture models another world state — but the row
            # SHAPE must match and every canned leaf must be a value the platform
            # can emit. Lists of objects collapse to one representative row; lists
            # of scalars go straight to the leaf check, or merging would recurse.
            if canned_node and not live_node:
                # ONE finding, not one per field: an empty live list supports one
                # conclusion and says nothing about row shape. Unless the list is
                # volatile, where there is no conclusion at all — `recent_samples`
                # is empty for a stack's first minute, so the finding's KIND would
                # otherwise depend on when the check ran. The key-set diff above
                # still guards presence and type.
                if _policy_path(path) not in volatile:
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

        Type first, then value, mirroring the grader's
        ``FieldComparator.satisfied_by`` — plain ``in`` uses ``==`` and ``True == 1``
        in Python, which would absorb bool-vs-number drift. A type the platform
        never emits here is reported as ``type``, not as an unreachable value.
        """
        normalized = _policy_path(path)
        if normalized in volatile or normalized in unbounded:
            return
        domain = [v for v in _flatten(live_node) if v is not None]
        live_types = {_json_type(v) for v in domain}
        for value in (v for v in _flatten(canned_node) if v is not None):
            # An all-null live domain says nothing about the field's type, so this
            # stays an unreachable VALUE rather than a contract claim.
            if live_types and _json_type(value) not in live_types:
                record(path, "type", value, sorted(live_types))
            elif not any(_json_type(v) == _json_type(value) and v == value for v in domain):
                record(path, "not_live_reachable", value, sorted({_short(v) for v in domain}))

    walk(dict(call.payload), dict(live), "", False)
    return drifts


def _differs_in_type(canned: Any, live: Any) -> bool:
    """A real contract type change — null on either side does not count.

    ``null`` is a legal VALUE here, not a type: ``lag: null`` has its own scenario
    (``consumer_lag_null_unknown_state``). Counting number-vs-null also made the
    drift KIND depend on stack uptime, so one defect flapped between ledger keys.
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

    Keys are the union across elements, so a field only some rows carry is still
    compared; each value is that key's values, which the leaf check reads as a
    value domain rather than positionally.
    """
    merged: dict[str, list[Any]] = {}
    for element in elements:
        if not isinstance(element, Mapping):
            continue
        for key, value in element.items():
            merged.setdefault(key, []).append(value)
    return merged
