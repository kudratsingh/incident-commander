"""Compare canned scenario fixtures against what the platform actually returns.

Nothing checked that canned ``canned_tool_responses`` were real recordings, and
``consumer_lag_high`` asserted ``lag: 1200`` against a live ``0`` for months. Kinds:
``live_only_field``/``canned_only_field`` (key sets disagree), ``value`` (a scalar
that cannot occur), ``not_live_reachable`` (a row value the platform never emits),
``no_live_rows``. Live half: ``tests/integration/test_canned_fixtures_match_live.py``.
"""

from __future__ import annotations

import json
import uuid
from collections import Counter
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
    # key-set diff still reports a fixture that was never re-recorded. v0.6.24 (plat #243)
    # adds `last_poll_at` and `last_poll_age_seconds`: a clock and an age, like the two
    # above, and frozen-or-fresh is the canned world's story, not something a reset
    # stack can match.
    "get_consumer_lag": frozenset(
        {
            "lag_known",
            "measured_at",
            "age_seconds",
            "recent_samples",
            "recent_samples.lag",
            "recent_samples.measured_at",
            "last_poll_at",
            "last_poll_age_seconds",
        }
    ),
    # v0.6.11 (plat #218, WO-R3-217) added twelve output fields and v0.6.12's
    # `slow_db_queries` is the first hook that moves two of them. Neither of those
    # two joins this map, and WO-R3-221 (WP-8.5, ADR 0066) made that call
    # deliberately after trying the other one, so the line is worth stating:
    # **_VOLATILE is for a field no canned value can be right about in ANY world;
    # a field a hook drives into a bounded range is LEDGERED post-fault instead.**
    # `used_memory_bytes` above is the first kind — Redis allocates what it
    # allocates and `saturate_redis` only adds to it. `longest_active_query_ms`
    # is the second: the hook holds two `query_ms` reads offset by half a chunk,
    # so a seeded world reads somewhere in 772..1973 ms (six observations) and a
    # quiet one reads null. That is a range, not an arbitrary gauge, and the
    # honest record of it is a ledger row saying which world each value belongs
    # to. Declaring it volatile also had a cost that decided it: it made
    # `postgres_slow`'s two ledger lines stale, and the only place that coverage
    # could move to is a graded EVIDENCE claim on the fault's own values — which
    # is a world-content claim on a scenario the read-only smoke pass runs
    # UNSEEDED, i.e. the fifth instance of the class INC-003 is about and
    # WO-R3-266 is filed to fix. It broke the committed re-grade of
    # `0db6fe722f7c` immediately, which is that gap answering for itself.
    # `pools` (plat #227, platform ADR 0033): each process rewrites its record every 10 s, so
    # `written_at` and `reported_age_s` say when you looked, like the breaker clocks below.
    "get_postgres_health": frozenset(
        {"ping_latency_ms", "active_connections", "pools.written_at", "pools.reported_age_s"}
    ),
    # v0.6.11's breaker reading (plat #218, ADR 0030), first used by WO-R3-221.
    # Two clocks and the two ages derived from them, plus `last_failure_at`, which
    # is stamped by whichever call last failed. `seconds_since_state_change` and
    # `reported_age_s` are both "how long ago you looked", so they cannot be
    # canned at all. DELIBERATELY OUT: `state`, `failure_threshold` and
    # `last_failure_reason_class` — `state` IS `api_latency_downstream`'s evidence,
    # the threshold is configured, and the reason class is a closed three-member
    # vocabulary rather than a reading. `failure_count` is IN, and it is the
    # judgement call here: it climbs for as long as the dependency stays down
    # (measured 3 -> 4 -> 5 -> 6 -> 9 across one fault), so no canned value can be
    # right — but it is the other half of the open-breaker evidence, so
    # `api_latency_downstream` grades it `at_least: 3`, the breaker's own
    # threshold, which is the number that means "this opened because calls failed"
    # rather than a sample of when you looked. Volatile as a value, load-bearing as
    # a floor, exactly like `longest_active_query_ms` above.
    "get_circuit_breakers": frozenset(
        {
            "measured_at",
            "breakers.recorded_at",
            "breakers.reported_age_s",
            "breakers.last_state_change_at",
            "breakers.seconds_since_state_change",
            "breakers.last_failure_at",
            "breakers.failure_count",
        }
    ),
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
    # One clock, `measured_at`. The counters are readings: the `api_latency` worlds declare
    # `objectives.total` volatile in their own YAML (WO-R3-309) and the cascade does not.
    "get_slo_status": frozenset({"measured_at"}),
    # v0.6.9 (plat #211, WO-R3-201), made from the four recordings under
    # `evals/recorded_worlds/jobs_not_progressing_*`. Two clocks (`measured_at`,
    # `relay_last_tick_at`), the two ages derived from them, `last_publish_at` (the
    # seeder writes no outbox rows), and the four `oldest_/newest_unpublished_*`
    # that flip to null with load. DELIBERATELY OUT: `unpublished_count` IS the
    # outbox family's evidence (the `api_latency` worlds declare it volatile in their
    # own YAML), and `unpublished_past_attempt_limit`, `relay_heartbeat_known` and
    # `relay_tick_interval_s` are stable.
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
    # v0.6.24's loop reading (plat #243, platform ADR 0041), first held by a recording or a
    # canned world in WO-R3-372. Two clocks (`measured_at`, `loops.last_run_at`), the age
    # derived from one, and the pause's countdown, which says how long ago the world was
    # seeded the way `get_cache_key_info.ttl_seconds` says how long the stack has been up.
    # DELIBERATELY OUT: `loops.paused` IS the evidence (a seeded pause is ledgered
    # post-fault, never forgiven), and `tick_interval_seconds`, `name` and `total` are config.
    "get_control_loops": frozenset(
        {
            "measured_at",
            "loops.last_run_at",
            "loops.last_run_age_seconds",
            "loops.paused_expires_in_seconds",
        }
    ),
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


# --------------------------------------------------------------------------
# Honest movement between two readings of ONE world (WO-R3-366)
# --------------------------------------------------------------------------
# A recorded world re-read live is a second truthful observation of the same world, and a
# few fields can never agree between two of those even when nothing about the world has
# changed: 8 of the 17 worlds re-recorded on 2026-10-10 read DRIFT for these alone. Each
# rule below names one such movement and swaps equality for the comparison that still sees
# a real change in the same field. OFF by default: only ``evals/world_drift.py`` passes
# them (``compare(..., honest=...)``), so the canned-fixture check and its may-only-shrink
# ledger are untouched — the split ``world_drift._HISTORY`` already makes. Where a scenario
# grades one of these paths, ``world_drift`` asks the scenario's own claim of the live
# reading as well (minted ids excepted: their rule still pins every id that is not random).

#: A random (version-4) UUID is minted per creation, so no second reading can hold it; the
#: rows carrying one are matched by shape, and live must hold at least as many as recorded.
#: Every other id — the seeder's deterministic version-5 ones — is still compared by value.
RULE_MINTED_ID: Final[str] = "minted_id"
#: A raised alert's record of the moment its rule fired: compared by JSON type only.
RULE_FIRING_READING: Final[str] = "firing_reading"
#: A count of rows one reading can hold more of than the other: live may not be lower.
RULE_COUNT_FLOOR: Final[str] = "count_floor"
#: A 24-hour count: a number on the same side of zero (a window that still holds jobs).
RULE_WINDOW_COUNT: Final[str] = "window_count"
#: A value that follows from the 24-hour window: compared by JSON type only.
RULE_WINDOW_READING: Final[str] = "window_reading"
#: A reading with a threshold beside it in the same payload: both readings must sit on the
#: same side of their own threshold (both at or over it, or both under it / null).
RULE_THRESHOLD_SIDE: Final[str] = "threshold_side"

#: What each rule still compares, printed beside every path it covers.
RULE_MEANING: Final[Mapping[str, str]] = {
    RULE_MINTED_ID: "random ids by shape, at least as many as recorded; other ids by value",
    RULE_FIRING_READING: "type only — what the platform saw when its rule fired",
    RULE_COUNT_FLOOR: "at least the recorded count",
    RULE_WINDOW_COUNT: "a number on the same side of zero",
    RULE_WINDOW_READING: "type only — it follows from the 24-hour window",
    RULE_THRESHOLD_SIDE: "on the same side of the threshold the same reading carries",
}


@dataclass(frozen=True)
class Movement:
    """One field's honest movement: the rule that compares it, and why equality cannot."""

    rule: str
    why: str
    #: ``RULE_THRESHOLD_SIDE`` only: the sibling field that holds the threshold.
    threshold: str | None = None


_ALERT_ID_WHY: Final[str] = (
    "a platform-raised alert's id is a random UUID minted when its rule fires, so every "
    "seeding raises the same alert under a new id (d0e1e74e… recorded, 4b38852a… live in "
    "api_latency_downstream). The seeded alerts carry version-5 ids, and those stay pinned."
)
_FIRING_WHY: Final[str] = (
    "written once, at the moment the platform's own rule fired (alert_rules.py, slo.py): "
    "two seedings fire it at different moments, so the reading (lag 22, dlq_depth 8 vs 7), "
    "its clock and the summary that quotes both differ. Which rule fired, at what threshold "
    "and on which subject (fingerprint, source, severity, group, slo_id) stay compared."
)
_WINDOW_WHY: Final[str] = (
    "computed over the platform's rolling 24-hour `jobs` window (slo.py, window_hours=24), "
    "which grows with every job any check submits and sheds jobs a day old; `make "
    "eval-reset` deliberately never deletes job rows (INC-007). Recorded 131 -> live 153 "
    "in jobs_not_progressing_dispatcher_stall, with the rates following."
)

#: Per tool, policy path (no list markers) -> its movement. Pinned by an exact-equality test
#: (``tests/unit/test_world_drift_honest_movement.py``), like ``world_drift._HISTORY``.
HONEST_MOVEMENT: Final[Mapping[str, Mapping[str, Movement]]] = {
    "list_active_alerts": {
        "alerts.id": Movement(RULE_MINTED_ID, _ALERT_ID_WHY),
        "total": Movement(
            RULE_COUNT_FLOOR,
            "a platform rule can fire between the recording's read and the re-read: "
            "api_latency_downstream's completion fast-burn alert had not fired at the "
            "recording (4 alerts) and had at the re-check (5). A recorded alert that is gone "
            "still reads as drift, through this floor and through its identity fields.",
        ),
        "alerts.extra_data.measured_at": Movement(RULE_FIRING_READING, _FIRING_WHY),
        "alerts.extra_data.summary": Movement(RULE_FIRING_READING, _FIRING_WHY),
        "alerts.extra_data.lag": Movement(RULE_FIRING_READING, _FIRING_WHY),
        "alerts.extra_data.dlq_depth": Movement(RULE_FIRING_READING, _FIRING_WHY),
        "alerts.extra_data.current": Movement(RULE_FIRING_READING, _FIRING_WHY),
        "alerts.extra_data.burn_rate": Movement(RULE_FIRING_READING, _FIRING_WHY),
        "alerts.extra_data.budget_remaining_pct": Movement(RULE_FIRING_READING, _FIRING_WHY),
        "alerts.extra_data.total": Movement(RULE_FIRING_READING, _FIRING_WHY),
        "alerts.extra_data.failed": Movement(RULE_FIRING_READING, _FIRING_WHY),
    },
    "list_incidents": {
        "incidents.id": Movement(
            RULE_MINTED_ID, "the same alert ids as `list_active_alerts.alerts.id`, as incidents."
        ),
        "total": Movement(
            RULE_COUNT_FLOOR, "as `list_active_alerts.total`: an incident is an alert row."
        ),
    },
    "list_dlq_messages": {
        "items.id": Movement(
            RULE_MINTED_ID,
            "a dead letter made by traffic is a job the traffic created, with a random id: "
            "api_latency_downstream's 20 bulk_api_sync dead letters are new rows on every "
            "seeding (20 of its 57 disagreements). The seeded rows keep their version-5 ids.",
        ),
        "items.trace_id": Movement(
            RULE_MINTED_ID,
            "minted with the job, so it is new on every seeding exactly like `items.id` "
            "(the other 20 of api_latency_downstream's 40 dead-letter disagreements).",
        ),
    },
    "get_slo_status": {
        "objectives.total": Movement(RULE_WINDOW_COUNT, _WINDOW_WHY),
        "objectives.failed": Movement(RULE_WINDOW_READING, _WINDOW_WHY),
        "objectives.current_success_rate": Movement(RULE_WINDOW_READING, _WINDOW_WHY),
        "objectives.budget_remaining_pct": Movement(RULE_WINDOW_READING, _WINDOW_WHY),
        "objectives.burn_rate": Movement(RULE_WINDOW_READING, _WINDOW_WHY),
        "objectives.healthy": Movement(RULE_WINDOW_READING, _WINDOW_WHY),
        "objectives.fast_burn": Movement(RULE_WINDOW_READING, _WINDOW_WHY),
    },
    "get_postgres_health": {
        "longest_active_query_ms": Movement(
            RULE_THRESHOLD_SIDE,
            "the held query's running time is how long it had been running when you "
            "looked (api_latency_db_query: 1148 ms recorded, 1196 ms live). What the world "
            "claims is that a query is held over the platform's slow-query threshold, which "
            "is the scenario's own rule (at_least 500 = slow_query_threshold_ms).",
            threshold="slow_query_threshold_ms",
        ),
    },
}

#: Lists whose rows the platform raises on its own clock. A live row with a random id and
#: an identity no recorded row has was raised after the recording was read: it is set aside
#: and named in the report, never compared (``observed_pair``).
RAISED_ROWS: Final[Mapping[str, str]] = {
    "list_active_alerts": "alerts",
    "list_incidents": "incidents",
}

#: Newest-first pages capped by the call's ``limit``. Every job any check submits pushes the
#: oldest row off a full page, so a recorded row the live page no longer holds is not drift
#: when the rows missing are, as shapes, among the recording's oldest that many
#: (``observed_pair``). A row that vanished from the middle still reads as drift.
NEWEST_PAGES: Final[Mapping[str, str]] = {"search_traces": "matches"}

#: The platform's own words for a breaker store with no record (`breaker_state.py`,
#: BREAKERS_UNKNOWN_NONE_PUBLISHED). A breaker record lives 24 h after the breaker last
#: reported (BREAKER_STATE_TTL_SECONDS), and only a call through the breaker or the
#: environment reset rewrites it — so a stack with no traffic through `bulk-api-sync` for
#: a day reads no breaker at all. That is the stack's idle clock, not the recorded world.
BREAKER_RECORD_EXPIRED_REASON: Final[str] = "none has reported for longer than the platform keeps"


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
    # Paths the scenario's own ``volatile:`` list adds to ``_VOLATILE`` for this tool.
    volatile: frozenset[str] = frozenset()

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
                            volatile=scenario.volatile_paths(tool),
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
    honest: Mapping[str, Movement] | None = None,
) -> list[Drift]:
    """Every way ``call``'s canned payload disagrees with the live response.

    ``live`` must be the snapshot for THIS element's position in the sequence. A value is
    forgiven only by ``_VOLATILE`` (clocks, every scenario) or the scenario's own
    ``volatile:`` list (``call.volatile``). ``shape_only`` (type only, cut at that node) and
    ``honest`` (this tool's ``HONEST_MOVEMENT`` rules, path -> movement) are empty by
    default; ``evals/world_drift.py`` is their one caller.
    """
    volatile = _VOLATILE.get(call.tool, frozenset()) | call.volatile
    unbounded = _UNBOUNDED_TEXT.get(call.tool, frozenset())
    # A scenario's own volatile declaration is the weaker claim and it is the owner's: it wins.
    moves = {path: m for path, m in (honest or {}).items() if path not in volatile}
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
                child = _join(path, key)
                move = moves.get(_policy_path(child))
                if move is not None and move.rule == RULE_THRESHOLD_SIDE and not in_list:
                    threshold_side(child, move, canned_node, live_node)
                    continue
                walk(canned_node[key], live_node[key], child, in_list)
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
                id_move = moves.get(_join(_policy_path(path), "id"))
                if id_move is not None and id_move.rule == RULE_MINTED_ID:
                    minted_rows(path, canned_node, live_node)
                walk(_merge_rows(canned_node), _merge_rows(live_node), f"{path}[]", True)
            else:
                walk_leaves(canned_node, live_node, f"{path}[]")
            return

        if _policy_path(path) in volatile:
            if _differs_in_type(canned_node, live_node):
                record(path, "type", canned_node, live_node)
            return

        move = moves.get(_policy_path(path))
        if move is not None and not in_list:
            moved_scalar(path, move, canned_node, live_node)
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
        values = [v for v in _flatten(canned_node) if v is not None]
        move = moves.get(normalized)
        if move is not None:
            moved_leaves(path, move, values, domain)
            return
        member_of(path, values, domain)

    def minted_rows(path: str, canned_rows: Sequence[Any], live_rows: Sequence[Any]) -> None:
        """``RULE_MINTED_ID`` per row: each recorded row with a random id needs a live row
        with a random id and the same identity — everything the walk still compares by
        value — at least as many times as the recording held it. The merged walk below
        reads values as a domain, so without this a traffic row that changed category
        would hide behind a seeded row of the old one."""
        prefix = _policy_path(path)
        relaxed = volatile | unbounded | shape_only | frozenset(moves)

        def identities(rows: Sequence[Any]) -> Counter[str]:
            return Counter(
                _identity(row, prefix, relaxed)
                for row in rows
                if isinstance(row, Mapping) and _is_random_uuid(row.get("id"))
            )

        have = identities(live_rows)
        for identity, count in sorted((identities(canned_rows) - have).items()):
            record(f"{path}[]", "minted_row_missing", f"{count} x {identity}", have[identity])

    def member_of(path: str, values: Sequence[Any], domain: Sequence[Any]) -> None:
        live_types = {_json_type(v) for v in domain}
        for value in values:
            # An all-null live domain says nothing about the field's type, so this
            # stays an unreachable VALUE rather than a contract claim.
            if live_types and _json_type(value) not in live_types:
                record(path, "type", value, sorted(live_types))
            elif not any(_json_type(v) == _json_type(value) and v == value for v in domain):
                record(path, "not_live_reachable", value, sorted({_short(v) for v in domain}))

    def same_types(path: str, values: Sequence[Any], domain: Sequence[Any]) -> bool:
        """The same JSON types on both sides, nulls aside; False once it recorded a drift.

        Both ways round: the values are a merged domain, so a recorded type that is still
        present elsewhere must not hide a live value that changed type.
        """
        recorded_types = {_json_type(v) for v in values}
        live_types = {_json_type(v) for v in domain}
        if recorded_types and live_types and recorded_types != live_types:
            record(path, "type", sorted(recorded_types), sorted(live_types))
            return False
        return True

    def moved_leaves(
        path: str, move: Movement, values: Sequence[Any], domain: Sequence[Any]
    ) -> None:
        """One ``HONEST_MOVEMENT`` rule over a list field's values, read as a domain."""
        if move.rule == RULE_MINTED_ID:
            minted = {v for v in values if _is_random_uuid(v)}
            live_minted = {v for v in domain if _is_random_uuid(v)}
            if len(live_minted) < len(minted):
                record(path, "minted_rows_below_floor", len(minted), len(live_minted))
            member_of(path, [v for v in values if not _is_random_uuid(v)], domain)
            return
        if not same_types(path, values, domain):
            return
        if move.rule == RULE_WINDOW_COUNT:
            live_signs = {_sign(v) for v in domain if _is_number(v)}
            for value in values:
                if _is_number(value) and live_signs and _sign(value) not in live_signs:
                    record(path, "sign_class", value, sorted({_short(v) for v in domain}))
        elif move.rule == RULE_COUNT_FLOOR:
            numbers = [v for v in domain if _is_number(v)]
            for value in values:
                if _is_number(value) and numbers and max(numbers) < value:
                    record(path, "below_floor", value, max(numbers))

    def moved_scalar(path: str, move: Movement, canned_node: Any, live_node: Any) -> None:
        """One ``HONEST_MOVEMENT`` rule over a field outside any list."""
        if move.rule == RULE_MINTED_ID and _is_random_uuid(canned_node):
            if not _is_random_uuid(live_node):
                record(path, "minted_id", canned_node, live_node)
            return
        if _differs_in_type(canned_node, live_node):
            record(path, "type", canned_node, live_node)
            return
        if move.rule in (RULE_FIRING_READING, RULE_WINDOW_READING):
            return
        both_numbers = _is_number(canned_node) and _is_number(live_node)
        if move.rule == RULE_WINDOW_COUNT and both_numbers:
            if _sign(canned_node) != _sign(live_node):
                record(path, "sign_class", canned_node, live_node)
        elif move.rule == RULE_COUNT_FLOOR and both_numbers:
            if live_node < canned_node:
                record(path, "below_floor", canned_node, live_node)
        elif canned_node != live_node:
            record(path, "value", canned_node, live_node)

    def threshold_side(
        path: str, move: Movement, canned_parent: Mapping[str, Any], live_parent: Mapping[str, Any]
    ) -> None:
        """A reading against the threshold its own payload carries, both sides read alike."""
        key = path.rsplit(".", 1)[-1]
        canned_node, live_node = canned_parent[key], live_parent[key]
        if _differs_in_type(canned_node, live_node):
            record(path, "type", canned_node, live_node)
            return
        canned_limit = canned_parent.get(move.threshold or "")
        live_limit = live_parent.get(move.threshold or "")
        if not (_is_number(canned_limit) and _is_number(live_limit)):
            # No threshold to read the reading against: equality, as before the rule.
            if canned_node != live_node:
                record(path, "value", canned_node, live_node)
            return
        if _at_or_over(canned_node, canned_limit) != _at_or_over(live_node, live_limit):
            record(path, "threshold_side", canned_node, live_node)

    walk(dict(call.payload), dict(live), "", False)
    return drifts


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _sign(value: float) -> int:
    return (value > 0) - (value < 0)


def _at_or_over(value: Any, threshold: Any) -> bool:
    """``RULE_THRESHOLD_SIDE``'s two sides: at or over it, or under it / no reading at all."""
    return _is_number(value) and value >= threshold


def _is_random_uuid(value: Any) -> bool:
    """A version-4 UUID: random by definition (RFC 9562), so no second reading repeats it."""
    if not isinstance(value, str):
        return False
    try:
        return uuid.UUID(value).version == 4
    except ValueError:
        return False


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


@dataclass(frozen=True)
class ObservedPair:
    """Two readings of one call with the rows only one of them can hold set aside, and why."""

    recorded: dict[str, Any]
    live: dict[str, Any]
    notes: tuple[str, ...] = ()


def observed_pair(
    tool: str,
    arguments: Mapping[str, Any],
    recorded: Mapping[str, Any],
    live: Mapping[str, Any],
    *,
    relaxed: frozenset[str] = frozenset(),
) -> ObservedPair:
    """``RAISED_ROWS`` and ``NEWEST_PAGES``: rows that are not drift, removed before the walk.

    ``relaxed`` is every path the caller compares by less than value (history, a scenario's
    own volatile list); with ``_VOLATILE``, ``_UNBOUNDED_TEXT`` and ``HONEST_MOVEMENT`` it is
    what a row's IDENTITY leaves out, so identity is exactly "what the walk still compares".
    Pure, and the only thing ``world_drift`` adds to the walk: the walk itself is unchanged.
    """
    rows_relaxed = (
        relaxed
        | _VOLATILE.get(tool, frozenset())
        | _UNBOUNDED_TEXT.get(tool, frozenset())
        | frozenset(HONEST_MOVEMENT.get(tool, {}))
    )
    recorded_out, live_out = dict(recorded), dict(live)
    notes: list[str] = []

    key = RAISED_ROWS.get(tool)
    if key is not None and _rows(recorded, key) is not None and _rows(live, key) is not None:
        known = {_identity(row, key, rows_relaxed) for row in _rows(recorded, key) or ()}
        kept, raised = [], []
        for row in _rows(live, key) or ():
            minted = isinstance(row, Mapping) and _is_random_uuid(row.get("id"))
            if minted and _identity(row, key, rows_relaxed) not in known:
                raised.append(row)
            else:
                kept.append(row)
        if raised:
            live_out[key] = kept
            names = ", ".join(sorted(f"{r.get('source')} ({r.get('severity')})" for r in raised))
            notes.append(
                f"{tool}: {len(raised)} row(s) the platform raised after the recording was "
                f"read, set aside — {names}"
            )

    key = NEWEST_PAGES.get(tool)
    limit = arguments.get("limit")
    recorded_rows, live_rows = _rows(recorded, key or ""), _rows(live, key or "")
    if (
        key is not None
        and isinstance(limit, int)
        and recorded_rows is not None
        and live_rows is not None
        and len(recorded_rows) == len(live_rows) == limit
    ):
        slid = _slid_off(recorded_rows, live_rows, key, rows_relaxed)
        if slid:
            recorded_out[key] = [row for i, row in enumerate(recorded_rows) if i not in slid]
            shapes = Counter(_identity(recorded_rows[i], key, rows_relaxed) for i in slid)
            listed = ", ".join(f"{shape} x{n}" for shape, n in sorted(shapes.items()))
            notes.append(
                f"{tool}: {len(slid)} recorded row(s) slid off the newest-{limit} page as "
                f"newer rows arrived, set aside — {listed}"
            )
    return ObservedPair(recorded_out, live_out, tuple(notes))


def _rows(payload: Mapping[str, Any], key: str) -> list[Any] | None:
    rows = payload.get(key) if key else None
    return rows if isinstance(rows, list) else None


def _identity(row: Any, prefix: str, relaxed: frozenset[str]) -> str:
    """A row with every relaxed field removed, as canonical JSON: what still identifies it."""

    def keep(node: Any, path: str) -> Any:
        if isinstance(node, Mapping):
            return {
                key: keep(value, _join(path, key))
                for key, value in node.items()
                if _join(path, key) not in relaxed
            }
        return node

    return json.dumps(keep(row, prefix), sort_keys=True, default=str)


def _slid_off(
    recorded: Sequence[Any], live: Sequence[Any], key: str, relaxed: frozenset[str]
) -> set[int]:
    """Indexes of the recorded rows a full page lost to newer rows, or nothing.

    Two full pages of one size: as a multiset of shapes, what the recording holds and live
    does not is exactly as many rows as live holds and the recording does not. If those
    missing shapes are all among the recording's oldest that many rows, the page slid; the
    set returned is those rows. Anything missing from further up means a row went away.
    """
    recorded_shapes = [_identity(row, key, relaxed) for row in recorded]
    missing = Counter(recorded_shapes) - Counter(_identity(row, key, relaxed) for row in live)
    count = sum(missing.values())
    if not count:
        return set()
    order = sorted(range(len(recorded)), key=lambda i: _created_at(recorded[i]), reverse=True)
    tail = order[-count:]
    if Counter(recorded_shapes[i] for i in tail) & missing != missing:
        return set()
    slid: set[int] = set()
    left = Counter(missing)
    for i in reversed(tail):  # oldest first
        if left[recorded_shapes[i]] > 0:
            left[recorded_shapes[i]] -= 1
            slid.add(i)
    return slid


def _created_at(row: Any) -> str:
    """The newest-first order key; a row without one sorts as the oldest."""
    value = row.get("created_at") if isinstance(row, Mapping) else None
    return value if isinstance(value, str) else ""


def breaker_record_expired(recorded: Mapping[str, Any], live: Mapping[str, Any]) -> bool:
    """``get_circuit_breakers`` read records once and none now, for the reason expiry gives.

    Not drift and not a match: the world cannot be compared on this tool until traffic
    through the breaker re-publishes its record (``BREAKER_RECORD_EXPIRED_REASON``).
    ``world_drift`` refuses the verdict and says so. Any other empty reading (the store
    unreachable or unreadable) is left to the walk, which reports it as drift.
    """
    reason = live.get("unknown_reason")
    return (
        bool(_rows(recorded, "breakers"))
        and _rows(live, "breakers") == []
        and isinstance(reason, str)
        and BREAKER_RECORD_EXPIRED_REASON in reason
    )
