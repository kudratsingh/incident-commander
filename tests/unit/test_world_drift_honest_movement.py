"""WO-R3-366 — the world-drift check stops reading honest movement as DRIFT.

8 of the 17 worlds re-recorded on 2026-10-10 read DRIFT against themselves minutes later, for
six reasons that are movement any two truthful readings of one world show. Each rule here is
proved twice on the COMMITTED recording and the committed table's own DRIFT lines: the check
before this change reproduces the recorded line, the check now reads it CLEAN, and a genuinely
changed value in the same field still reads DRIFT. Hermetic and free: no stack, no model.
"""

from __future__ import annotations

import copy
import json
import re
import uuid
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Final

import pytest

from evals import fixture_drift, recorder, world_drift, world_drift_table
from evals.fixture_drift import (
    HONEST_MOVEMENT,
    RULE_MEANING,
    CannedCall,
    Drift,
    compare,
)
from evals.recorded_client import matching_recordings
from evals.scenarios.loader import load_scenarios
from evals.scenarios.schema import Scenario
from incident_commander.tools.registry import TOOL_REGISTRY

_REPO: Final[Path] = Path(__file__).resolve().parents[2]
#: The table whose DRIFT lines this order exists for (cmd #355, WO-R3-294).
_TABLE: Final[Path] = (
    _REPO / "evals/reports/world-drift/world_drift_table.20261010T094237Z.899a671c44d0.json"
)
_LINE: Final[re.Pattern[str]] = re.compile(r"^  - [^:\s]+:(\w+)(?:\[\d+\])? (\S+) \[(\w+)\]")

Change = Callable[[dict[str, Any]], None]


@pytest.fixture(scope="module")
def corpus() -> dict[str, Scenario]:
    return {s.name: s for s in load_scenarios(_REPO / "evals" / "scenarios")}


def _world(scenario: str, invocation_id: str) -> recorder.RecordedWorld:
    found = matching_recordings(invocation_id, [scenario])
    assert scenario in found, f"no committed recording {invocation_id} of {scenario}"
    return recorder.load_recording(found[scenario])


def _payload(call: recorder.RecordedCall) -> dict[str, Any]:
    parsed = json.loads(call.result["content"][0]["text"])
    assert isinstance(parsed, dict)
    return parsed


def _with(call: recorder.RecordedCall, payload: Mapping[str, Any]) -> recorder.RecordedCall:
    content = [{"type": "text", "text": json.dumps(payload)}]
    return call.model_copy(update={"result": {**call.result, "content": content}})


def _full_page(call: recorder.RecordedCall) -> bool:
    """The unfiltered newest-50 listing, the one call per tool the table's lines are about."""
    return not any(call.arguments.get(k) for k in ("status", "job_type", "remediation_hint"))


def _live(world: recorder.RecordedWorld, **changes: Change) -> list[recorder.RecordedCall]:
    """The recording's own calls, as a second reading that moved only as ``changes`` say."""
    out = []
    for call in world.calls:
        change = changes.get(call.tool)
        if change is None or not _full_page(call):
            out.append(call)
            continue
        payload = copy.deepcopy(_payload(call))
        change(payload)
        out.append(_with(call, payload))
    return out


def _before_this_change(
    world: recorder.RecordedWorld, live: list[recorder.RecordedCall], scenario: Scenario
) -> set[tuple[str, str, str]]:
    """The check as it ran on 2026-10-10: the same walk, history only, no honest rules."""
    by_key = {call.key: call for call in live}
    found: set[tuple[str, str, str]] = set()
    for call in world.calls:
        drifts = compare(
            CannedCall(
                scenario=world.scenario,
                tool=call.tool,
                arguments=call.arguments,
                payload=_payload(call),
                volatile=scenario.volatile_paths(call.tool),
            ),
            _payload(by_key[call.key]),
            shape_only=world_drift.shape_only_paths(call.tool, scenario),
        )
        found |= {(d.tool, d.path, d.kind) for d in drifts}
    return found


def _now(
    world: recorder.RecordedWorld, live: list[recorder.RecordedCall], scenario: Scenario
) -> list[Drift]:
    return world_drift.drift_between(world, live, scenario=scenario)


def _table_lines(scenario: str) -> set[tuple[str, str, str]]:
    """``(tool, path, kind)`` of every DRIFT line the committed table holds for one world."""
    table = json.loads(_TABLE.read_text())
    row = next(r for r in table["rows"] if r["scenario"] == scenario)
    found = set()
    for line in row["output"].splitlines():
        match = _LINE.match(line)
        if match:
            found.add((match.group(1), match.group(2), match.group(3)))
    assert found, scenario
    return found


def _minted() -> str:
    return str(uuid.uuid4())


# --------------------------------------------------------------------------
# The moves each world made between its recording and its re-check (from the table)
# --------------------------------------------------------------------------


def _remint_raised_alerts(later: str, *, depth_delta: int = 0) -> Change:
    """A platform-raised alert fired again: new random id, later clock, summary quoting it.

    ``depth_delta``: the DLQ depth it fired at moved too (api_latency_downstream: 8 -> 7).
    """

    def change(payload: dict[str, Any]) -> None:
        key = "alerts" if "alerts" in payload else "incidents"
        for row in payload[key]:
            if uuid.UUID(row["id"]).version != 4:
                continue  # a seeded alert: version-5 id, untouched
            old = row["id"]
            row["id"] = _remints.setdefault(old, _minted())
            extra = row.get("extra_data")
            if extra:
                if extra.get("measured_at"):
                    extra["summary"] = extra["summary"].replace(extra["measured_at"], later)
                    extra["measured_at"] = later
                if "dlq_depth" in extra and depth_delta:
                    depth = extra["dlq_depth"] + depth_delta
                    extra["summary"] = extra["summary"].replace(
                        f"{extra['dlq_depth']} jobs", f"{depth} jobs"
                    )
                    extra["dlq_depth"] = depth

    return change


_remints: dict[str, str] = {}


def _window(**objectives: dict[str, Any]) -> Change:
    """The 24-hour objectives read later: per objective id, the fields that moved."""

    def change(payload: dict[str, Any]) -> None:
        for row in payload["objectives"]:
            row.update(objectives.get(row["id"], {}))

    return change


def _slide(arrivals: int, *, status: str = "pending", job_type: str = "bulk_api_sync") -> Change:
    """``arrivals`` newer jobs on top of a full newest-first page; the oldest fall off."""

    def change(payload: dict[str, Any]) -> None:
        rows = payload["matches"]
        new = [
            {
                "trace_id": _minted(),
                "job_id": _minted(),
                "job_type": job_type,
                "status": status,
                "created_at": f"2026-10-10T23:59:{59 - i:02d}.000000Z",
            }
            for i in range(arrivals)
        ]
        payload["matches"] = (new + rows)[: len(rows)]

    return change


def _remint_traffic_dead_letters(payload: dict[str, Any]) -> None:
    """The traffic's dead letters, made again by the check's own burst: new rows, new ids."""
    for row in payload["items"]:
        if uuid.UUID(row["id"]).version == 4:
            row["id"], row["trace_id"] = _minted(), _minted()


# --------------------------------------------------------------------------
# Rule 1 — platform-raised alert ids and clocks
# --------------------------------------------------------------------------


class TestRaisedAlerts:
    @pytest.mark.parametrize(
        ("scenario", "recording"),
        [
            ("workflow_stuck_dead_lettered_root", "9d2272df85cf"),
            ("workflow_stuck_downstream_child_failed", "6aad209eb376"),
        ],
    )
    def test_the_recorded_drift_line_now_reads_clean(
        self, corpus: dict[str, Scenario], scenario: str, recording: str
    ) -> None:
        world = _world(scenario, recording)
        moved = _remint_raised_alerts("2026-10-10T08:34:46.571806+00:00")
        live = _live(world, list_active_alerts=moved, list_incidents=moved)
        assert _before_this_change(world, live, corpus[scenario]) == _table_lines(scenario)
        assert _now(world, live, corpus[scenario]) == []

    def test_a_changed_severity_still_drifts(self, corpus: dict[str, Scenario]) -> None:
        name = "workflow_stuck_dead_lettered_root"
        world = _world(name, "9d2272df85cf")
        moved = _remint_raised_alerts("2026-10-10T08:34:46.571806+00:00")

        def demoted(payload: dict[str, Any]) -> None:
            moved(payload)
            payload["alerts"][0]["severity"] = "warning"

        drifts = _now(world, _live(world, list_active_alerts=demoted), corpus[name])
        # The demoted alert is a different alert: the recorded one is missing, and the new
        # one is set aside and named rather than matched to it.
        assert ("alerts[]", "minted_row_missing") in {(d.path, d.kind) for d in drifts}

    def test_a_raised_alert_that_did_not_fire_still_drifts(
        self, corpus: dict[str, Scenario]
    ) -> None:
        name = "workflow_stuck_dead_lettered_root"
        world = _world(name, "9d2272df85cf")

        def silent(payload: dict[str, Any]) -> None:
            key = "alerts" if "alerts" in payload else "incidents"
            payload[key] = [r for r in payload[key] if uuid.UUID(r["id"]).version != 4]
            payload["total"] = len(payload[key])

        drifts = _now(world, _live(world, list_active_alerts=silent), corpus[name])
        kinds = {(d.path, d.kind) for d in drifts}
        assert ("total", "below_floor") in kinds
        assert ("alerts[]", "minted_row_missing") in kinds
        assert ("alerts[].id[]", "minted_rows_below_floor") in kinds

    def test_a_seeded_alerts_id_is_still_pinned(self, corpus: dict[str, Scenario]) -> None:
        """Seeded alerts carry version-5 ids; only random ids are matched by shape."""
        name = "workflow_stuck_dead_lettered_root"
        world = _world(name, "9d2272df85cf")

        def reseeded(payload: dict[str, Any]) -> None:
            seeded = next(r for r in payload["alerts"] if uuid.UUID(r["id"]).version == 5)
            seeded["id"] = str(uuid.uuid5(uuid.NAMESPACE_DNS, "another seed"))

        drifts = _now(world, _live(world, list_active_alerts=reseeded), corpus[name])
        assert [(d.path, d.kind) for d in drifts] == [("alerts[].id[]", "not_live_reachable")]

    def test_an_alert_raised_after_the_recording_is_set_aside_and_named(
        self, corpus: dict[str, Scenario]
    ) -> None:
        """api_latency_downstream: the completion fast-burn page fired between the two reads."""
        name = "api_latency_downstream"
        world = _world(name, "58ec70df7157")
        moved = _remint_raised_alerts("2026-10-10T08:48:13.582774+00:00", depth_delta=-1)
        slo_alert: dict[str, Any] = {
            "id": _minted(),
            "severity": "critical",
            "source": "slo:job_completion_rate",
            "title": "SLO fast burn: Job completion rate",
            "description": "burning",
            "fired_at": "2026-10-10T08:47:50.000000Z",
            "extra_data": {
                "slo_id": "job_completion_rate",
                "runbook_id": "rb-slo-job-completion",
                "burn_rate": 21.978,
                "threshold": 14.4,
                "target": 0.99,
                "current": 0.78022,
                "budget_remaining_pct": -100.0,
                "window_hours": 24,
                "total": 91,
                "failed": 20,
            },
        }

        def raised(payload: dict[str, Any]) -> None:
            moved(payload)
            key = "alerts" if "alerts" in payload else "incidents"
            row: dict[str, Any] = dict(slo_alert)
            if key == "incidents":
                row = {k: v for k, v in row.items() if k != "extra_data"}
                row |= {"resolved_at": None, "is_active": True}
            payload[key] = [row, *payload[key]]
            payload["total"] += 1

        live = _live(world, list_active_alerts=raised, list_incidents=raised)
        before = _before_this_change(world, live, corpus[name])
        recorded = _table_lines(name)
        alert_lines = {
            line for line in recorded if line[0] in ("list_active_alerts", "list_incidents")
        }
        assert {line for line in before if line[0] in ("list_active_alerts", "list_incidents")} == (
            alert_lines
        )
        now = [d for d in _now(world, live, corpus[name]) if d.tool.startswith("list_")]
        assert [d for d in now if d.tool != "list_dlq_messages"] == []
        notes = world_drift.set_aside_notes(world, live, scenario=corpus[name])
        assert any("slo:job_completion_rate (critical)" in note for note in notes)

    def test_fewer_alerts_than_recorded_still_drift(self, corpus: dict[str, Scenario]) -> None:
        name = "api_latency_downstream"
        world = _world(name, "58ec70df7157")

        def fewer(payload: dict[str, Any]) -> None:
            payload["total"] -= 1

        drifts = _now(world, _live(world, list_incidents=fewer), corpus[name])
        assert ("list_incidents", "total", "below_floor") in {
            (d.tool, d.path, d.kind) for d in drifts
        }


# --------------------------------------------------------------------------
# Rule 2 — the 24-hour job objectives grow with the check's own jobs
# --------------------------------------------------------------------------


_DISPATCHER_STALL: Final[Change] = _window(
    job_completion_rate={"total": 153},
    job_dispatch_latency={
        "total": 153,
        "current_success_rate": 0.9215686274509804,
        "budget_remaining_pct": -56.86274509803908,
        "burn_rate": 1.5686274509803908,
    },
)
_OUTBOX_STALL: Final[Change] = _window(
    job_completion_rate={"total": 243},
    job_dispatch_latency={
        "total": 243,
        "current_success_rate": 0.9506172839506173,
        "budget_remaining_pct": 1.2345679012346622,
        "burn_rate": 0.9876543209876534,
        "healthy": True,
    },
)
_CONSUMER_LAG: Final[Change] = _window(
    job_completion_rate={"total": 197},
    job_dispatch_latency={
        "total": 197,
        "current_success_rate": 0.9390862944162437,
        "budget_remaining_pct": -21.82741116751259,
        "burn_rate": 1.218274111675126,
    },
)
_DEPLOY_NOISE: Final[Change] = _window(
    job_completion_rate={"total": 119},
    job_dispatch_latency={
        "total": 119,
        "current_success_rate": 0.8991596638655462,
        "burn_rate": 2.016806722689074,
    },
)


class TestTheJobWindow:
    @pytest.mark.parametrize(
        ("scenario", "recording", "slo", "alerts", "traces"),
        [
            ("jobs_not_progressing_dispatcher_stall", "f4d62624c19f", _DISPATCHER_STALL, True, 0),
            ("jobs_not_progressing_outbox_stall", "9d9d56a0eb69", _OUTBOX_STALL, False, 0),
            ("remediate_consumer_lag_success", "20c1ee52bb70", _CONSUMER_LAG, True, 0),
            (
                "jobs_not_progressing_outbox_stall_deploy_noise",
                "84779d386158",
                _DEPLOY_NOISE,
                False,
                1,
            ),
        ],
    )
    def test_the_recorded_drift_line_now_reads_clean(
        self,
        corpus: dict[str, Scenario],
        scenario: str,
        recording: str,
        slo: Change,
        alerts: bool,
        traces: int,
    ) -> None:
        world = _world(scenario, recording)
        changes: dict[str, Change] = {"get_slo_status": slo}
        if alerts:
            moved = _remint_raised_alerts("2026-10-10T09:13:40.696314+00:00")
            changes |= {"list_active_alerts": moved, "list_incidents": moved}
        if traces:
            changes["search_traces"] = _slide(traces)
        live = _live(world, **changes)
        assert _before_this_change(world, live, corpus[scenario]) == _table_lines(scenario)
        assert _now(world, live, corpus[scenario]) == []

    def test_a_window_that_emptied_still_drifts(self, corpus: dict[str, Scenario]) -> None:
        """Same sign class: a window that held jobs and now holds none is a different world."""
        name = "jobs_not_progressing_dispatcher_stall"
        world = _world(name, "f4d62624c19f")
        empty = _window(job_completion_rate={"total": 0}, job_dispatch_latency={"total": 0})
        drifts = _now(world, _live(world, get_slo_status=empty), corpus[name])
        assert {(d.path, d.kind) for d in drifts} == {("objectives[].total[]", "sign_class")}

    def test_a_reading_that_changed_type_still_drifts(self, corpus: dict[str, Scenario]) -> None:
        name = "jobs_not_progressing_dispatcher_stall"
        world = _world(name, "f4d62624c19f")
        retyped = _window(job_dispatch_latency={"budget_remaining_pct": "-56.9"})
        drifts = _now(world, _live(world, get_slo_status=retyped), corpus[name])
        assert {(d.path, d.kind) for d in drifts} == {
            ("objectives[].budget_remaining_pct[]", "type")
        }

    def test_a_world_that_grades_the_budget_still_drifts_when_it_is_spent(
        self, corpus: dict[str, Scenario]
    ) -> None:
        """api_latency_db_query claims the dispatch budget is whole: its own claim is re-asked."""
        name = "api_latency_db_query"
        world = _world(name, "8c4a32a50d4b")
        spent = _window(
            job_dispatch_latency={"total": 255, "failed": 12, "budget_remaining_pct": 5.88}
        )
        drifts = _now(world, _live(world, get_slo_status=spent), corpus[name])
        broken = {(d.path, d.kind) for d in drifts}
        assert ("objectives.budget_remaining_pct", world_drift.KIND_HISTORY_CLAIM) in broken
        assert ("objectives.failed", world_drift.KIND_HISTORY_CLAIM) in broken

    def test_a_scenarios_own_volatile_declaration_still_wins(
        self, corpus: dict[str, Scenario]
    ) -> None:
        """api_latency_downstream declares the held-query time volatile: no rule, no re-check."""
        assert "longest_active_query_ms" not in world_drift.honest_paths(
            "get_postgres_health", corpus["api_latency_downstream"]
        )
        assert "objectives.total" not in world_drift.honest_paths(
            "get_slo_status", corpus["api_latency_downstream"]
        )


# --------------------------------------------------------------------------
# Rule 3 — dead-letter rows made by traffic
# --------------------------------------------------------------------------


class TestTrafficDeadLetters:
    def test_the_recorded_drift_lines_now_read_clean(self, corpus: dict[str, Scenario]) -> None:
        name = "api_latency_downstream"
        world = _world(name, "58ec70df7157")
        live = _live(world, list_dlq_messages=_remint_traffic_dead_letters)
        before = {line for line in _before_this_change(world, live, corpus[name])}
        recorded = {line for line in _table_lines(name) if line[0] == "list_dlq_messages"}
        assert (
            before
            == recorded
            == {
                ("list_dlq_messages", "items[].id[]", "not_live_reachable"),
                ("list_dlq_messages", "items[].trace_id[]", "not_live_reachable"),
            }
        )
        assert _now(world, live, corpus[name]) == []

    def test_a_seeded_dead_letter_is_still_pinned_by_id(self, corpus: dict[str, Scenario]) -> None:
        name = "dlq_backlog"
        world = _world(name, "f0ee5ea8d33d")

        def reseeded(payload: dict[str, Any]) -> None:
            seeded = next(r for r in payload["items"] if uuid.UUID(r["id"]).version == 5)
            seeded["id"] = str(uuid.uuid5(uuid.NAMESPACE_DNS, "another seed"))

        drifts = _now(world, _live(world, list_dlq_messages=reseeded), corpus[name])
        assert ("items[].id[]", "not_live_reachable") in {(d.path, d.kind) for d in drifts}

    def test_fewer_traffic_dead_letters_than_recorded_still_drift(
        self, corpus: dict[str, Scenario]
    ) -> None:
        name = "api_latency_downstream"
        world = _world(name, "58ec70df7157")

        def fewer(payload: dict[str, Any]) -> None:
            _remint_traffic_dead_letters(payload)
            minted = [r for r in payload["items"] if uuid.UUID(r["id"]).version == 4]
            payload["items"] = [r for r in payload["items"] if r not in minted[:5]]

        drifts = _now(world, _live(world, list_dlq_messages=fewer), corpus[name])
        assert ("items[].id[]", "minted_rows_below_floor") in {(d.path, d.kind) for d in drifts}

    def test_a_changed_category_on_a_traffic_row_still_drifts(
        self, corpus: dict[str, Scenario]
    ) -> None:
        name = "api_latency_downstream"
        world = _world(name, "58ec70df7157")

        def recategorised(payload: dict[str, Any]) -> None:
            _remint_traffic_dead_letters(payload)
            for row in payload["items"]:
                if uuid.UUID(row["id"]).version == 4:
                    row["type"] = "report_gen"

        drifts = _now(world, _live(world, list_dlq_messages=recategorised), corpus[name])
        # A seeded bulk_api_sync row keeps the old category in the merged domain; the
        # per-row identity floor is what sees the 20 traffic rows change.
        assert ("items[]", "minted_row_missing") in {(d.path, d.kind) for d in drifts}


# --------------------------------------------------------------------------
# Rule 4 — the newest-50 trace list
# --------------------------------------------------------------------------


class TestTheNewestPage:
    def test_a_page_that_slid_by_one_reads_clean_and_says_so(
        self, corpus: dict[str, Scenario]
    ) -> None:
        name = "jobs_not_progressing_outbox_stall_deploy_noise"
        world = _world(name, "84779d386158")
        live = _live(world, search_traces=_slide(1))
        assert ("search_traces", "matches[].job_type[]", "not_live_reachable") in (
            _before_this_change(world, live, corpus[name])
        )
        assert [d for d in _now(world, live, corpus[name]) if d.tool == "search_traces"] == []
        notes = world_drift.set_aside_notes(world, live, scenario=corpus[name])
        assert any("slid off the newest-50 page" in note and "csv_upload" in note for note in notes)

    def test_a_page_that_slid_by_twenty_reads_clean(self, corpus: dict[str, Scenario]) -> None:
        """api_latency_downstream: 20 new dead letters pushed `report_gen` (row 31) off."""
        name = "api_latency_downstream"
        world = _world(name, "58ec70df7157")
        live = _live(world, search_traces=_slide(20, status="dead_letter"))
        recorded = {line for line in _table_lines(name) if line[0] == "search_traces"}
        before = {line for line in _before_this_change(world, live, corpus[name])}
        assert recorded <= before
        assert [d for d in _now(world, live, corpus[name]) if d.tool == "search_traces"] == []

    def test_a_row_gone_from_the_middle_of_the_page_still_drifts(
        self, corpus: dict[str, Scenario]
    ) -> None:
        name = "api_latency_downstream"
        world = _world(name, "58ec70df7157")

        def hole(payload: dict[str, Any]) -> None:
            rows = payload["matches"]
            middle = next(i for i, r in enumerate(rows) if r["job_type"] == "doc_analysis")
            new = {**rows[0], "trace_id": _minted(), "job_id": _minted()}
            payload["matches"] = [new, *rows[:middle], *rows[middle + 1 :]]

        drifts = _now(world, _live(world, search_traces=hole), corpus[name])
        assert ("matches[].job_type[]", "not_live_reachable") in {(d.path, d.kind) for d in drifts}

    def test_a_changed_status_on_the_page_still_drifts(self, corpus: dict[str, Scenario]) -> None:
        name = "jobs_not_progressing_outbox_stall_deploy_noise"
        world = _world(name, "84779d386158")

        def healed(payload: dict[str, Any]) -> None:
            for row in payload["matches"]:
                if row["status"] == "pending":
                    row["status"] = "completed"

        drifts = _now(world, _live(world, search_traces=healed), corpus[name])
        assert ("matches[].status[]", "not_live_reachable") in {(d.path, d.kind) for d in drifts}


# --------------------------------------------------------------------------
# Rule 5 — the slow query's running time
# --------------------------------------------------------------------------


class TestTheHeldQuery:
    def _held(self, ms: float | None) -> Change:
        def change(payload: dict[str, Any]) -> None:
            payload["longest_active_query_ms"] = ms

        return change

    def test_the_recorded_drift_line_now_reads_clean(self, corpus: dict[str, Scenario]) -> None:
        name = "api_latency_db_query"
        world = _world(name, "8c4a32a50d4b")
        live = _live(world, get_postgres_health=self._held(1195.707))
        assert _before_this_change(world, live, corpus[name]) == _table_lines(name)
        assert _now(world, live, corpus[name]) == []

    @pytest.mark.parametrize("ms", [312.5, None])
    def test_a_query_no_longer_held_over_the_threshold_still_drifts(
        self, corpus: dict[str, Scenario], ms: float | None
    ) -> None:
        name = "api_latency_db_query"
        world = _world(name, "8c4a32a50d4b")
        drifts = _now(world, _live(world, get_postgres_health=self._held(ms)), corpus[name])
        kinds = {(d.path, d.kind) for d in drifts}
        assert ("longest_active_query_ms", "threshold_side") in kinds
        # And the scenario's own claim (at_least 500) is asked of the live reading too.
        assert ("longest_active_query_ms", world_drift.KIND_HISTORY_CLAIM) in kinds


# --------------------------------------------------------------------------
# Rule 6 — the breaker's 24-hour state record
# --------------------------------------------------------------------------


_EXPIRED: Final[dict[str, Any]] = {
    "measured_at": "2026-10-11T09:00:00.000000Z",
    "breakers": [],
    "total": 0,
    "unknown_reason": (
        "no breaker has a state record: either this platform registers none, or none has "
        "reported for longer than the platform keeps"
    ),
}


def _breakers(payload: Mapping[str, Any]) -> Change:
    def change(live: dict[str, Any]) -> None:
        live.clear()
        live.update(copy.deepcopy(dict(payload)))

    return change


class TestTheExpiredBreakerRecord:
    def test_an_expired_record_is_named_not_compared(self) -> None:
        world = _world("workflow_stuck_healthy_chain", "027f170a4cb1")
        live = _live(world, get_circuit_breakers=_breakers(_EXPIRED))
        expired = world_drift.expired_records(world, live)
        assert len(expired) == 1
        assert "the recording read 1 breaker(s)" in expired[0]

    def test_an_unreachable_store_is_not_expiry_and_still_drifts(
        self, corpus: dict[str, Scenario]
    ) -> None:
        name = "workflow_stuck_healthy_chain"
        world = _world(name, "027f170a4cb1")
        unreachable = dict(_EXPIRED, unknown_reason="the platform could not reach the store")
        live = _live(world, get_circuit_breakers=_breakers(unreachable))
        assert world_drift.expired_records(world, live) == ()
        assert {d.tool for d in _now(world, live, corpus[name])} == {"get_circuit_breakers"}

    def test_a_breaker_that_changed_state_still_drifts(self, corpus: dict[str, Scenario]) -> None:
        name = "workflow_stuck_healthy_chain"
        world = _world(name, "027f170a4cb1")

        def opened(payload: dict[str, Any]) -> None:
            payload["breakers"][0]["state"] = "open"

        live = _live(world, get_circuit_breakers=opened)
        assert world_drift.expired_records(world, live) == ()
        drifts = _now(world, live, corpus[name])
        assert ("breakers[].state[]", "not_live_reachable") in {(d.path, d.kind) for d in drifts}

    def test_the_check_says_send_warm_up_traffic_instead_of_drift(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The live half, with the stack faked: exit 8, no verdict, the world put back."""
        world = _world("workflow_stuck_healthy_chain", "027f170a4cb1")
        live = _live(world, get_circuit_breakers=_breakers(_EXPIRED))
        # The reachability read, then the recording's own calls.
        replies: Iterator[tuple[list[Any], list[Any], list[Any]]] = iter(
            [([], [SimpleNamespace(error=None)], []), (live, [], [])]
        )
        resets: list[int] = []

        def reset(_client: object) -> tuple[int, bool]:
            resets.append(1)
            return 0, True

        settings = SimpleNamespace(
            platform_smoke_token=SimpleNamespace(get_secret_value=lambda: "read"),
            platform_mcp_url="http://platform.invalid:8001/mcp",
            require_chaos_token=lambda: "chaos",
        )
        monkeypatch.setattr(world_drift, "Settings", lambda: settings)
        monkeypatch.setattr(world_drift, "make_client", lambda _s, token: SimpleNamespace())
        monkeypatch.setattr(world_drift, "record_calls", lambda _c, _p: next(replies))
        monkeypatch.setattr(world_drift, "seed_chaos", lambda *_a: SimpleNamespace(failed=False))
        monkeypatch.setattr(world_drift, "establish_preconditions", lambda *_a: [])
        monkeypatch.setattr(world_drift, "_reset_and_audit", reset)

        code, report = world_drift.check_world("027f170a4cb1")

        out = capsys.readouterr().out
        assert (code, report) == (world_drift.EXIT_RECORD_EXPIRED, None)
        assert f"DRIFT FAIL (record expired): {world_drift.RECORD_EXPIRED_MESSAGE}" in out
        assert "make traffic COUNT=5" in out
        assert "disagreement(s)" not in out and "DRIFT: none" not in out
        assert resets == ([1] if world.world.chaos_seeded else [])
        row = world_drift_table.row_from_log(
            f"uv run python -m evals.world_drift --world 027f170a4cb1\n{out}",
            checked_at="2026-10-11T09:00:00+00:00",
        )
        assert row.verdict == world_drift_table.REFUSED
        assert row.detail.startswith(f"record expired: {world_drift.RECORD_EXPIRED_MESSAGE}")


# --------------------------------------------------------------------------
# It cannot become a blanket exemption
# --------------------------------------------------------------------------


def _model_paths(model: type[Any], prefix: str = "") -> dict[str, Any]:
    """Every field path of one output model, with its annotation, nested models descended."""
    from pydantic import BaseModel

    found: dict[str, Any] = {}
    for name, field in model.model_fields.items():
        path = f"{prefix}{name}"
        found[path] = field.annotation
        for inner in _models_in(field.annotation):
            if isinstance(inner, type) and issubclass(inner, BaseModel):
                found |= _model_paths(inner, f"{path}.")
    return found


def _models_in(annotation: Any) -> list[Any]:
    from typing import get_args, get_origin

    stack, found = [annotation], []
    while stack:
        current = stack.pop()
        if get_origin(current) is None:
            found.append(current)
        stack.extend(get_args(current))
    return found


def _open_map(annotation: Any) -> bool:
    from typing import get_origin

    return any(get_origin(a) is dict or a is dict for a in [annotation, *_args(annotation)])


def _args(annotation: Any) -> list[Any]:
    from typing import get_args

    return list(get_args(annotation))


class TestTheTableIsNarrow:
    def test_the_declared_set_is_pinned(self) -> None:
        """Widening it is a reviewed edit to this list, never a side effect."""
        assert {
            tool: {p: m.rule for p, m in paths.items()} for tool, paths in HONEST_MOVEMENT.items()
        } == {
            "list_active_alerts": {
                "alerts.id": "minted_id",
                "total": "count_floor",
                "alerts.extra_data.measured_at": "firing_reading",
                "alerts.extra_data.summary": "firing_reading",
                "alerts.extra_data.lag": "firing_reading",
                "alerts.extra_data.dlq_depth": "firing_reading",
                "alerts.extra_data.current": "firing_reading",
                "alerts.extra_data.burn_rate": "firing_reading",
                "alerts.extra_data.budget_remaining_pct": "firing_reading",
                "alerts.extra_data.total": "firing_reading",
                "alerts.extra_data.failed": "firing_reading",
            },
            "list_incidents": {"incidents.id": "minted_id", "total": "count_floor"},
            "list_dlq_messages": {"items.id": "minted_id", "items.trace_id": "minted_id"},
            "get_slo_status": {
                "objectives.total": "window_count",
                "objectives.failed": "window_reading",
                "objectives.current_success_rate": "window_reading",
                "objectives.budget_remaining_pct": "window_reading",
                "objectives.burn_rate": "window_reading",
                "objectives.healthy": "window_reading",
                "objectives.fast_burn": "window_reading",
            },
            "get_postgres_health": {"longest_active_query_ms": "threshold_side"},
        }

    def test_every_path_is_a_field_of_that_tools_output_or_inside_an_open_map(self) -> None:
        for tool, paths in HONEST_MOVEMENT.items():
            modelled = _model_paths(TOOL_REGISTRY[tool].output_model)
            for path in paths:
                if path in modelled:
                    continue
                parent = path.rsplit(".", 1)[0]
                assert parent in modelled and _open_map(modelled[parent]), (tool, path)

    def test_no_tool_is_exempted_wholesale(self) -> None:
        """A strict subset of every tool's fields, always: a whole tool is the blanket."""
        for tool, paths in HONEST_MOVEMENT.items():
            modelled = set(_model_paths(TOOL_REGISTRY[tool].output_model))
            assert set(paths) & modelled < modelled, tool

    def test_every_path_says_why_and_every_rule_says_what_it_still_compares(self) -> None:
        for tool, paths in HONEST_MOVEMENT.items():
            for path, move in paths.items():
                assert len(move.why) > 40, (tool, path)
                assert move.rule in RULE_MEANING, (tool, path)
                assert (move.threshold is not None) == (move.rule == "threshold_side")

    def test_the_canned_fixture_walk_is_untouched(self) -> None:
        """`make test-drift` and its may-only-shrink ledger: no rule unless a caller passes it."""
        recorded = {"total": 4, "alerts": [{"id": _minted(), "severity": "critical"}]}
        live = {"total": 5, "alerts": [{"id": _minted(), "severity": "critical"}]}
        call = CannedCall(scenario="s", tool="list_active_alerts", arguments={}, payload=recorded)
        assert {(d.path, d.kind) for d in compare(call, live)} == {
            ("total", "value"),
            ("alerts[].id[]", "not_live_reachable"),
        }
        honest = HONEST_MOVEMENT["list_active_alerts"]
        assert compare(call, live, honest=honest) == []

    def test_the_report_names_every_rule_it_applied(self, corpus: dict[str, Scenario]) -> None:
        """A clean walk must never be readable as "everything was compared by value"."""
        name = "jobs_not_progressing_dispatcher_stall"
        world = _world(name, "f4d62624c19f")
        live = world.model_copy(
            update={"calls": tuple(_live(world, get_slo_status=_DISPATCHER_STALL))}
        )
        report = world_drift.build_report(
            world="f4d62624c19f",
            recording_path=Path("evals/recorded_worlds/x/x.json"),
            recording=world,
            live_world=live,
            scenario=corpus[name],
        )
        rendered = world_drift.render(report)
        assert "DRIFT: none" in rendered
        assert "DRIFT: honest movement" in rendered
        assert "get_slo_status.objectives.total [window_count" in rendered


def test_every_rule_world_drift_prints_has_a_meaning() -> None:
    rules = {move.rule for paths in HONEST_MOVEMENT.values() for move in paths.values()}
    assert rules == set(fixture_drift.RULE_MEANING)
