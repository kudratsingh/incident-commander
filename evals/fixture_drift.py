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
    # volatile — only the reading's freshness metadata would be. v0.6.0
    # (plat #166, R2-17) added exactly that metadata, and `lag_known` takes
    # the slot this note reserved for it: `worker-dispatcher` is the one
    # continuously-refreshed group, so within roughly the first minute of a
    # freshly booted stack it has no measurement yet and reports
    # lag_known=false, then flips to true once the loop emits. CI's contract
    # job reads inside that window and a developer stack reads outside it —
    # the same fixture is "wrong" in one and right in the other, which is
    # timing, not contract. `lag` itself stays out, so the number the lag
    # scenarios rest on is still guarded, and so does `source`, which is a
    # stable property of the group rather than of when you looked.
    #
    # v0.6.7 (plat #204, WO-R3-254) shipped the freshness metadata the note
    # above reserved this slot for, and all of it is a property of when you
    # looked rather than of the fixture pack:
    #
    #   `measured_at` is the platform's clock at the moment the metrics loop
    #   took the reading. No recording can match it, exactly like the four
    #   DLQ clocks below — declared here rather than blessed as four
    #   known-drift entries, the `dead_lettered_at` call made on the way in.
    #
    #   `age_seconds` is `now - measured_at` and lands anywhere in 0..~60 as
    #   the loop's cadence rolls. It is the `ttl_seconds` case: a countdown
    #   nothing fixes. Pinning one reading also makes the ledger FLAP rather
    #   than merely disagree — on the re-record run that produced this entry a
    #   canned 12 happened to equal a live 12, so the drift key vanished for
    #   that one fixture and would have come back on the next run, which is
    #   the stale-entry red the ledger exists to avoid.
    #
    #   `recent_samples` is the rolling window of those same readings, and it
    #   fails the membership test twice over. Its VALUES are what the loop
    #   measured while a fault was or was not running — the seeder writes no
    #   window at all (it deliberately skips worker-dispatcher, the only
    #   group that has one), so there is nothing for a recording to match.
    #   Its EMPTINESS is a function of how long the stack has been up: the
    #   loop sleeps 60s before its first measurement, so a freshly booted or
    #   freshly reset platform reports `[]` and a minute later reports rows.
    #   That is the `_differs_in_type` lesson — the same fixture landing in
    #   the ledger under two different keys (`no_live_rows` one run,
    #   `not_live_reachable` the next) depending on timing, with the ratchet
    #   reddening on whichever one it did not see. Both halves are silenced
    #   together, by the entry here and by the `no_live_rows` exemption in
    #   `compare`.
    #
    # What none of this silences: the key-set diff runs first, so a fixture
    # that has not been re-recorded is still reported as `live_only_field` on
    # all three fields. That is exactly how this re-pin found the fourteen
    # canned lag responses it had to re-record, and it is what keeps
    # `tool_output_schema_mismatch`'s deliberate omission in the ledger.
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
    # Every field here is a gauge of a running server, and the line that
    # decides membership is whether the fixture pack FIXES the value.
    # `lag` above is seeded to an exact number by the platform's
    # seed_eval_fixtures `_CONSUMER_LAGS`, so a recording can match it and
    # must — which is why it stays out. Nothing seeds Redis memory or the
    # keyspace counters: `used_memory_bytes` is whatever the server has
    # allocated, and `keyspace_hits` / `keyspace_misses` are monotonic
    # counters over its lifetime, so their values are a property of how many
    # reads happened before the probe rather than of the fixture. No canned
    # value can be right about them, and pinning one to a single reading
    # makes the check flip red on the next run — the rubber stamp the ledger
    # exists to avoid. Type is the claim a recording can actually honour.
    #
    # `used_memory_human` is `used_memory_bytes` formatted by Redis itself.
    # Declaring the bytes volatile and the string not made the same
    # observation unfalsifiable through one field and mandatory through the
    # other, which is how "1.00G" sat in the ledger against a live "1.60M".
    #
    # `evicted_keys` was in this set and is not in the v0.5.0 output model at
    # all, so it silenced nothing and only implied a field the tool returns.
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
    # `ttl_seconds` is a countdown on a key the fixture pack seeds with a
    # 24h expiry, so its value is a property of how long the stack has been
    # up when you looked — the same test this file applies to the redis
    # gauges above ("does the fixture pack FIX the value"), and the answer
    # is no. `exists`, `type` and `size` DO stay guarded: the pack fixes all
    # three for cache:jobs:worker-dispatcher:hot_set, so a recording can
    # match them and must.
    #
    # v0.6.8 (plat #209, WO-R3-267) adds `records_referenced` and
    # `records_found`, and they stay OUT of this set — the opposite call from
    # the one `get_consumer_lag`'s v0.6.7 fields got, and for the reason that
    # decides membership rather than by family resemblance. Those were a
    # clock (`measured_at`), a countdown (`age_seconds`) and a window whose
    # very emptiness depends on stack uptime (`recent_samples`). These two
    # are COUNTS OF SEEDED RECORDS: seed_eval_fixtures writes the hot-set
    # entry as a list of three job ids and seeds those three jobs, so a live
    # read of a fresh stack answers 3 / 3 every time, and the canned
    # `no_fault_healthy_cache` recording says 3 / 3 and matches with no
    # ledger entry at all. A recording can be right about them, so it must
    # be. Nothing here flaps: the only disagreements left are the two the
    # ledger records, and both are a fixture describing a LATER world than
    # the walk can probe (the seeded fault, and the post-delete read).
    "get_cache_key_info": frozenset({"ttl_seconds"}),
    # `get_outbox_status` arrived with the v0.6.9 re-pin (plat #211, WO-R3-201)
    # and had no canned fixture until WO-R3-202 wrote the `jobs_not_progressing`
    # family. The re-pin recorded the volatility call from two live readings 25s
    # apart and left it for whoever wrote the first fixture; this is that entry,
    # and the readings it is made from are the four recordings under
    # `evals/recorded_worlds/jobs_not_progressing_*`.
    #
    # Nine fields, and they divide on the one question this file asks: does the
    # fixture pack FIX the value?
    #
    #   `measured_at` is the database server's clock at call time, and
    #   `relay_last_tick_at` is the worker's clock at the relay's last pass. No
    #   recording can match either — the `get_consumer_lag.measured_at` case,
    #   one tool along.
    #
    #   `relay_heartbeat_age_s` and `seconds_since_last_publish` are those two
    #   clocks minus `measured_at`, so they move for the same reason and land
    #   anywhere. `age_seconds`'s note above applies verbatim: pinning one
    #   reading makes the ledger FLAP rather than merely disagree.
    #
    #   `last_publish_at` is when the relay last delivered, which is a fact
    #   about when the platform last had traffic, not about the fixture pack.
    #   The seeder writes no outbox rows at all.
    #
    #   The four `oldest_/newest_unpublished_{at,age_s}` fields exist only while
    #   a backlog exists, so they flip between a value and `null` with load
    #   rather than with contract. `_differs_in_type` already treats null as a
    #   legal value rather than a type change, so the entry silences the value
    #   and keeps the type claim — which is what a recording can honour.
    #
    # DELIBERATELY OUT, and this is the load-bearing half: `unpublished_count`,
    # `unpublished_past_attempt_limit`, `relay_heartbeat_known` and
    # `relay_tick_interval_s`. The count IS the outbox family's evidence — the
    # whole contrast is "the queue grew while lag stayed flat" — so silencing it
    # would silence the measurement. It disagrees with the un-faulted world by
    # construction (the fault is what fills the queue), and that disagreement is
    # a `post-fault` ledger entry, which is a statement someone wrote down. The
    # other three are stable: 0 on a drainable backlog, true on any stack with a
    # relay, and 1.0 from configuration.
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
    # `items.dead_lettered_at` arrived with the v0.6.0 re-pin (plat #180,
    # R2-53) and is the third clock on this tool, not a new species: the
    # seeder stamps it fresh at seed time, exactly like the two beside it,
    # so no recording can ever match it. Declared here rather than blessed
    # as six known-drift entries -- one per DLQ fixture -- which is the
    # `jobs.updated_at` lesson from the batch-4 re-record applied on the
    # way in instead of after the fact. The field's ORDERING contract (the
    # list is now sorted by it) is asserted by the scenarios, not here.
    # `items.fenced_at` and `items.fenced_by` arrived with the v0.6.2 re-pin
    # (plat #198, WO-R2-158) and join the three clocks above for the same
    # reason, applied on the way in rather than after the fact — the same call
    # the `dead_lettered_at` note describes ("declared here rather than blessed
    # as six known-drift entries").
    #
    # `fenced_at` is the fourth clock on this row and fails the only test that
    # matters here — does the fixture pack FIX the value? It cannot: it is
    # stamped by the platform at the moment an operator fences, so it exists
    # only in a post-action reading and its value is whenever that happened.
    #
    # `fenced_by` is not a clock and is exempt for a stronger reason: it is
    # `"{principal_type}:{principal_id}"`, and the principal id is minted by
    # `make bootstrap-token`, so it changes on every `down -v`. A recording
    # that pinned it would drift on every fresh stack — a ratchet reddening
    # for something that is not drift — and no scenario asserts it.
    #
    # What this does NOT silence, and the distinction is the whole reason this
    # is safe: _VOLATILE is a LEAF-VALUE exemption, checked inside `walk`'s
    # leaf branch and `walk_leaves`. The key-set diff above it runs first and
    # is untouched, so a fixture that omitted either field is still reported as
    # `live_only_field` — which is exactly how this re-pin found the 30 rows it
    # had to re-record. Presence is enforced; the value is not asserted.
    # Whether a fence LANDED is a scenario claim (`dlq_human_required_escalates`
    # grades `items[].fenced_at is_null: false` on the row it fenced), not a
    # ratchet claim.
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
    # `jobs.updated_at` joins the two created_at clocks for the same reason
    # they are here, and its absence was the same species of gap as
    # used_memory_human's: `items.updated_at` is already volatile for
    # list_dlq_messages, so the identical field was a clock through one tool
    # and a pinned value through another. The seeder writes it as a fresh
    # timestamp at seed time, so no recording can match it -- and until now
    # nothing noticed, because get_trace's fixture drifted as `no_live_rows`
    # (one finding for the whole empty list) and the row shape underneath was
    # never actually compared.
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
    # True when the scenario seeds chaos at all, i.e. its fixture describes a
    # world its hooks manufacture. Read off ``Scenario.seeds_chaos`` rather
    # than the legacy ``chaos_setup`` field, which is ``None`` on a
    # plan-declaring scenario (ADR 0037). Load-bearing for one case
    # only: a tool that answers "that entity does not exist" when probed
    # against the UN-faulted world has made an observation, not failed. See
    # ``evals/fixture_probe.py`` for why that distinction has to be drawn
    # there rather than swallowed as a probe error.
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
        """Identity for the known-drift ledger. Deliberately excludes the
        observed values: a fixture whose wrong value changes is still the
        same unfixed drift, and re-blessing on every value wobble would make
        the ledger a rubber stamp.

        The element is part of the identity. A sequenced fixture can record
        different worlds at different points in a run, so one path can need
        distinct explanations for its pre-fault and post-action readings.
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

    ``live`` must be the snapshot taken for THIS element's position in the
    sequence — see ``fixture_probe.probe_live``. Handing element 1 the
    snapshot element 0 was compared against is what made a deliberately
    post-action recording drift by construction.

    ``shape_only`` is the CALLER's additional declaration: normalized paths
    (no ``[]`` markers, as ``_policy_path`` writes them) whose values are
    compared for JSON type and nothing else, cut at that node so an open map
    underneath is not descended into. It is **empty by default**, which is the
    load-bearing half: ``make test-drift`` and
    ``tests/integration/test_canned_fixtures_match_live.py`` pass nothing, so
    the canned-fixture walk is byte-for-byte the walk it was, and widening
    ``_VOLATILE`` is still the only way to forgive a canned fixture.

    The one caller is ``evals/world_drift.py``, whose question is a different
    one. ``_VOLATILE`` asks "does the fixture pack FIX this value?"; a recorded
    world also has to ask "can anything in the lab PUT this value BACK?" — and
    for the platform's audit log the answer is no, because the log is immutable
    and every read the harness makes is an entry in it. That judgement is
    declared there, per tool and per path with a reason, and deliberately not
    here: a path added to this module's tables weakens a check on 41 committed
    fixtures, and the two questions must not share one table.
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
            # A caller-declared shape-only path: type, then stop. Checked FIRST
            # so the cut applies to containers too — ``extra_data`` on an audit
            # row is ``dict[str, Any]``, an open map whose keys are whatever the
            # action wrote, so an exact list of its leaves is unwriteable and
            # any attempt at one would fail in the direction of noise. Type is
            # compared over the flattened values so a merged row's list of ids
            # is still checked for "these are still strings"; an all-null side
            # yields nothing and says nothing, the ``walk_leaves`` rule.
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
                #
                # Unless the list itself is declared volatile, in which case
                # there is no conclusion to draw at all: a volatile list is one
                # whose CONTENTS nothing fixes, and an empty reading of it is
                # the same observation as a full one — `get_consumer_lag`'s
                # `recent_samples` is empty for the first minute of a stack's
                # life and populated after it. Without this the finding's KIND
                # would depend on when the check ran (`no_live_rows` inside
                # that minute, `not_live_reachable` outside it), and the
                # ledger's stale check reddens on whichever key it did not
                # see. Presence and type are still guarded by the key-set diff
                # above, which runs before this branch.
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
