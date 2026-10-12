"""WO-R3-386 — the newest-50 trace page is compared by trace identity, not by position.

The seeder's traces carry version-5 ids that every reset keeps, and every reset re-stamps
their clocks to fixed offsets before the reset; real jobs keep both their random id and their
time. So after a reset and a burst the seeded rows sit somewhere else on the page than they
did in the recording, and the WO-R3-366 slide rule (missing rows must be the recording's
OLDEST) read the seeded rows that fell off as rows gone from the middle: WO-R3-368's first
check of `jobs_not_progressing_outbox_stall_deploy_noise` read five such lines. Each test
builds the live page from the committed recording the way the platform builds it, then
asks the check. Hermetic and free: no stack, no model.
"""

from __future__ import annotations

import copy
import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import pytest

from evals import fixture_drift, recorder, world_drift
from evals.fixture_drift import CannedCall, Drift, compare
from evals.recorded_client import matching_recordings
from evals.scenarios.loader import load_scenarios
from evals.scenarios.schema import Scenario

_REPO: Final[Path] = Path(__file__).resolve().parents[2]
#: WO-R3-368's recording; its first check right after recording read 5 trace-page lines.
_SCENARIO: Final[str] = "jobs_not_progressing_outbox_stall_deploy_noise"
_RECORDING: Final[str] = "07ce53de46ca"
#: The reset the check ran after: three hours on, so every recorded row keeps its own time
#: and the seeded block moves with the reset (`_rebaseline_timestamps`: shift, not flatten).
_SHIFT: Final[timedelta] = timedelta(hours=3)

Change = Callable[[dict[str, Any]], None]


@pytest.fixture(scope="module")
def scenario() -> Scenario:
    return {s.name: s for s in load_scenarios(_REPO / "evals" / "scenarios")}[_SCENARIO]


@pytest.fixture(scope="module")
def world() -> recorder.RecordedWorld:
    found = matching_recordings(_RECORDING, [_SCENARIO])
    assert _SCENARIO in found
    return recorder.load_recording(found[_SCENARIO])


def _seeded(row: dict[str, Any]) -> bool:
    return uuid.UUID(row["trace_id"]).version == 5


def _at(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _page(call: recorder.RecordedCall) -> dict[str, Any]:
    parsed = json.loads(call.result["content"][0]["text"])
    assert isinstance(parsed, dict)
    return parsed


def _the_page(world: recorder.RecordedWorld) -> recorder.RecordedCall:
    """The unfiltered newest-50 listing."""
    return next(
        call
        for call in world.calls
        if call.tool == "search_traces"
        and not any(call.arguments.get(k) for k in ("status", "job_type"))
    )


def _live(world: recorder.RecordedWorld, change: Change) -> list[recorder.RecordedCall]:
    page = _the_page(world)
    out = []
    for call in world.calls:
        if call.key != page.key:
            out.append(call)
            continue
        payload = copy.deepcopy(_page(call))
        change(payload)
        content = [{"type": "text", "text": json.dumps(payload)}]
        out.append(call.model_copy(update={"result": {**call.result, "content": content}}))
    return out


def _job(moment: datetime, status: str, job_type: str = "bulk_api_sync") -> dict[str, Any]:
    return {
        "trace_id": str(uuid.uuid4()),
        "job_id": str(uuid.uuid4()),
        "job_type": job_type,
        "status": status,
        "created_at": _stamp(moment),
    }


def _reset_then_traffic(*, recent: int, burst: int, every: float = 10.0) -> Change:
    """The page as the platform serves it after a reset and the check's own burst.

    The seeded rows keep their ids and move ``_SHIFT`` later (the reset); the recording's own
    traffic rows keep their time and have run (pending -> completed); ``recent`` jobs ran in
    the minutes before the reset, one every ``every`` seconds; ``burst`` jobs are the check's
    own, still pending three minutes after it. Newest first, cut at the page's limit.
    """

    def change(payload: dict[str, Any]) -> None:
        rows = payload["matches"]
        limit = len(rows)
        seeded = [
            {**r, "created_at": _stamp(_at(r["created_at"]) + _SHIFT)} for r in rows if _seeded(r)
        ]
        reset = max(_at(r["created_at"]) for r in seeded) + timedelta(minutes=1)
        traffic = [{**r, "status": "completed"} for r in rows if not _seeded(r)]
        traffic += [
            _job(reset - timedelta(seconds=every * (i + 1)), "completed") for i in range(recent)
        ]
        traffic += [
            _job(reset + timedelta(minutes=3, seconds=0.25 * i), "pending") for i in range(burst)
        ]
        everything = sorted(seeded + traffic, key=lambda r: r["created_at"], reverse=True)
        payload["matches"] = everything[:limit]

    return change


def _walk_alone(
    world: recorder.RecordedWorld, live: list[recorder.RecordedCall], scenario: Scenario
) -> set[tuple[str, str]]:
    """The trace page through the walk with nothing set aside: what the rows really differ in."""
    page = _the_page(world)
    live_page = next(call for call in live if call.key == page.key)
    drifts = compare(
        CannedCall(
            scenario=world.scenario,
            tool=page.tool,
            arguments=page.arguments,
            payload=_page(page),
            volatile=scenario.volatile_paths(page.tool),
        ),
        _page(live_page),
        shape_only=world_drift.shape_only_paths(page.tool, scenario),
    )
    return {(d.path, d.kind) for d in drifts}


def _traces(
    world: recorder.RecordedWorld, live: list[recorder.RecordedCall], scenario: Scenario
) -> list[Drift]:
    return [
        d
        for d in world_drift.drift_between(world, live, scenario=scenario)
        if d.tool == "search_traces"
    ]


def _notes(
    world: recorder.RecordedWorld, live: list[recorder.RecordedCall], scenario: Scenario
) -> tuple[str, ...]:
    return world_drift.set_aside_notes(world, live, scenario=scenario)


class TestSeededRowsThatMoved:
    def test_the_canned_recording_holds_both_kinds_of_row(
        self, world: recorder.RecordedWorld
    ) -> None:
        rows = _page(_the_page(world))["matches"]
        assert len(rows) == 50
        assert sum(_seeded(r) for r in rows) == 7
        # The seeded block sits in the MIDDLE of the recording's time order: 36 newer traffic
        # rows above it, 7 older below it.
        order = sorted(rows, key=lambda r: r["created_at"], reverse=True)
        assert [i for i, r in enumerate(order) if _seeded(r)] == list(range(36, 43))

    def test_seeded_rows_that_fell_off_after_a_reset_and_a_burst_read_clean_and_say_so(
        self, world: recorder.RecordedWorld, scenario: Scenario
    ) -> None:
        """WO-R3-368's first check: recent traffic outnumbered the page above the older
        seeded rows, so `csv_upload`, `doc_analysis`, `report_gen` and both `failed` rows
        were below the page while two newer seeded rows were still on it."""
        live = _live(world, _reset_then_traffic(recent=44, burst=12))
        alone = _walk_alone(world, live, scenario)
        assert ("matches[].job_type[]", "not_live_reachable") in alone
        assert ("matches[].status[]", "not_live_reachable") in alone
        assert _traces(world, live, scenario) == []
        notes = _notes(world, live, scenario)
        seeded = next(n for n in notes if "seeded row(s) slid off the newest-50 page" in n)
        for job_type in ("csv_upload", "doc_analysis", "report_gen"):
            assert job_type in seeded
        assert any("recorded traffic row(s) slid off the newest-50 page" in n for n in notes)

    @pytest.mark.parametrize(("recent", "burst"), [(0, 12), (10, 12), (30, 22), (36, 22), (60, 22)])
    def test_wherever_the_reset_puts_the_seeded_block_the_page_reads_clean(
        self, world: recorder.RecordedWorld, scenario: Scenario, recent: int, burst: int
    ) -> None:
        """Position and slide no longer matter: from nothing slid to every seeded row gone.

        Every check of this world sends its own burst (the dispatcher holds it pending), as
        the recording did; without one the recording's pending rows would have no pending
        counterpart (`test_a_traffic_row_inside_the_live_pages_time_still_counts`)."""
        live = _live(world, _reset_then_traffic(recent=recent, burst=burst))
        assert _traces(world, live, scenario) == []

    def test_a_seeded_trace_whose_status_changed_drifts_even_when_its_shape_is_elsewhere(
        self, world: recorder.RecordedWorld, scenario: Scenario
    ) -> None:
        """`1bfc7482` dead_letter -> failed: `failed` is already on the page (two other seeded
        rows), so the merged value domain cannot see it; the trace's own id can."""
        quiet = _reset_then_traffic(recent=0, burst=12)

        def changed(payload: dict[str, Any]) -> None:
            quiet(payload)
            for row in payload["matches"]:
                if row["trace_id"].startswith("1bfc7482"):
                    row["status"] = "failed"

        live = _live(world, changed)
        assert _walk_alone(world, live, scenario) == set()
        drifts = _traces(world, live, scenario)
        assert [(d.path, d.kind) for d in drifts] == [("matches[]", "seeded_row_changed")]
        assert drifts[0].canned.startswith("1bfc7482")
        assert '"status": "failed"' in drifts[0].live

    def test_a_seeded_trace_gone_while_an_older_one_stays_drifts(
        self, world: recorder.RecordedWorld, scenario: Scenario
    ) -> None:
        """`89609789` (csv_upload) removed; the four older seeded rows are still on the page,
        so it cannot have slid off: a row went away from the middle."""
        quiet = _reset_then_traffic(recent=0, burst=1)

        def hole(payload: dict[str, Any]) -> None:
            quiet(payload)
            payload["matches"] = [
                r for r in payload["matches"] if not r["trace_id"].startswith("89609789")
            ]
            payload["matches"].insert(0, _job(datetime.now(UTC), "pending"))

        drifts = _traces(world, _live(world, hole), scenario)
        assert ("matches[]", "seeded_row_missing") in {(d.path, d.kind) for d in drifts}
        missing = next(d for d in drifts if d.kind == "seeded_row_missing")
        assert missing.canned.startswith("89609789") and "csv_upload" in missing.canned

    def test_a_seeded_trace_absent_from_a_page_that_is_not_full_drifts(
        self, world: recorder.RecordedWorld, scenario: Scenario
    ) -> None:
        """Nothing slides off a page with room on it."""

        def short(payload: dict[str, Any]) -> None:
            rows = payload["matches"]
            payload["matches"] = [r for r in rows if not r["trace_id"].startswith("0e24ca29")]

        drifts = _traces(world, _live(world, short), scenario)
        kinds = {(d.path, d.kind) for d in drifts}
        assert ("matches[]", "page_below_floor") in kinds
        assert ("matches[]", "seeded_row_missing") in kinds

    def test_a_page_that_lost_rows_is_below_the_floor_and_sets_nothing_aside(
        self, world: recorder.RecordedWorld, scenario: Scenario
    ) -> None:
        moved = _reset_then_traffic(recent=44, burst=12)

        def thinner(payload: dict[str, Any]) -> None:
            moved(payload)
            payload["matches"] = payload["matches"][:40]

        live = _live(world, thinner)
        drifts = _traces(world, live, scenario)
        assert ("matches[]", "page_below_floor") in {(d.path, d.kind) for d in drifts}
        floor = next(d for d in drifts if d.kind == "page_below_floor")
        assert (floor.canned, floor.live) == (50, 40)
        assert not any("slid off" in n for n in _notes(world, live, scenario))

    def test_a_traffic_row_inside_the_live_pages_time_still_counts(
        self, world: recorder.RecordedWorld, scenario: Scenario
    ) -> None:
        """Traffic rows keep their time, so one newer than the live page's oldest row is on
        the page if it exists: the recording's pending rows read back with no pending at all
        is the world that changed (no check burst, every recorded job ran)."""

        def ran(payload: dict[str, Any]) -> None:
            for row in payload["matches"]:
                if row["status"] == "pending":
                    row["status"] = "completed"

        drifts = _traces(world, _live(world, ran), scenario)
        assert ("matches[].status[]", "not_live_reachable") in {(d.path, d.kind) for d in drifts}


def test_a_page_whose_every_recorded_row_slid_off_reads_clean_and_says_so(
    scenario: Scenario,
) -> None:
    """remediate_consumer_lag_success fe0a6b1cf71e holds one seeded row and 49 traffic rows.
    Checked after three other burst worlds (WO-R3-386's table, 09:42 UTC), the live page held
    only rows newer than all fifty, and the first version of this rule compared an EMPTY
    recorded list against a full live one: five `live_only_field` lines, one per row field."""
    name = "remediate_consumer_lag_success"
    found = matching_recordings("fe0a6b1cf71e", [name])
    world = recorder.load_recording(found[name])
    corpus = {s.name: s for s in load_scenarios(_REPO / "evals" / "scenarios")}
    rows = _page(_the_page(world))["matches"]
    assert (len(rows), sum(_seeded(r) for r in rows)) == (50, 1)

    def all_new(payload: dict[str, Any]) -> None:
        newest = max(_at(r["created_at"]) for r in payload["matches"]) + timedelta(hours=4)
        payload["matches"] = [
            _job(newest - timedelta(seconds=i), "pending" if i < 22 else "completed")
            for i in range(len(payload["matches"]))
        ]

    live = _live(world, all_new)
    assert _traces(world, live, corpus[name]) == []
    notes = _notes(world, live, corpus[name])
    assert any("no recorded row is still on the newest-50 page" in n for n in notes)


def test_the_rule_is_pure_and_only_for_newest_pages() -> None:
    """`observed_pair` on a tool with no newest page returns its inputs untouched."""
    recorded = {"matches": [{"trace_id": str(uuid.uuid5(uuid.NAMESPACE_URL, "x")), "status": "a"}]}
    pair = fixture_drift.observed_pair("get_trace", {"limit": 1}, recorded, {"matches": []})
    assert (pair.recorded, pair.live, pair.notes, pair.disagreements) == (
        recorded,
        {"matches": []},
        (),
        (),
    )
