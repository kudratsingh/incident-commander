"""The flat eval outputs are versioned, and nothing overwrites a prior file.

Invariant 9 shipped with a documented exception: four "refreshable pointers" every run
rewrote in place, on the argument that ``evals/runs/<invocation_id>/`` held a durable
copy. An artifact that erases its own history cannot be evidence, so the exception is
withdrawn, and a second write must leave the first readable. Everything writes to ``tmp_path``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import pytest

from evals import artifacts
from evals.graders.deterministic import GradeReport
from evals.runner import (
    RunReport,
    ScenarioOutcome,
    Trajectory,
    write_briefings,
    write_report,
    write_trajectories,
)
from incident_commander.agent.briefing import EscalationBriefing
from incident_commander.agent.state import IncidentState

_T1: Final[datetime] = datetime(2026, 9, 6, 10, 11, 12, tzinfo=UTC)
_T2: Final[datetime] = datetime(2026, 9, 6, 14, 30, 0, tzinfo=UTC)
_INV1: Final[str] = "aaaaaaaa0001"
_INV2: Final[str] = "bbbbbbbb0002"


def _trajectory(
    incident_id: str, invocation_id: str, scenario: str = "consumer_lag_pass"
) -> Trajectory:
    return Trajectory(
        scenario=scenario, incident_id=incident_id, checkpoints=(), invocation_id=invocation_id
    )


def _briefing(summary: str) -> EscalationBriefing:
    return EscalationBriefing(
        incident_id="i", final_state=IncidentState.ESCALATED, alert_summary=summary
    )


def _report(invocation_id: str, generated_at: datetime, total: int = 1) -> RunReport:
    return RunReport(
        generated_at=generated_at,
        invocation_id=invocation_id,
        total=total,
        passed=total,
        failed=0,
        outcomes=(
            ScenarioOutcome(
                scenario="consumer_lag_pass",
                final_state=IncidentState.ESCALATED,
                tool_calls_used=1,
                report=GradeReport(scenario="consumer_lag_pass", passed=True, dimensions=()),
            ),
        ),
    )


class TestFlatOutputsAreVersioned:
    """Two runs, two files — for every flat output the harness writes.

    Before this rule the second run of a scenario left exactly ONE file in
    each flat directory and the first run's copy was gone. That is the
    assertion each test here inverts.
    """

    def test_two_runs_leave_two_trajectory_files(self, tmp_path: Path) -> None:
        write_trajectories([_trajectory("first", _INV1)], directory=tmp_path)
        write_trajectories([_trajectory("second", _INV2)], directory=tmp_path)

        files = sorted(p.name for p in tmp_path.iterdir())
        assert len(files) == 2, f"a re-run replaced the first trajectory: {files}"
        # Both readable — not just present.
        bodies = {json.loads((tmp_path / name).read_text())["incident_id"] for name in files}
        assert bodies == {"first", "second"}
        # Newest wins: within a second by invocation_id, across one by stamp.
        newest = artifacts.newest("trajectory", "consumer_lag_pass", directory=tmp_path)
        assert json.loads(newest.read_text())["incident_id"] == "second"

    def test_two_runs_leave_two_briefing_files(self, tmp_path: Path) -> None:
        write_briefings(
            [_briefing("first")], ["consumer_lag_pass"], directory=tmp_path, invocation_id=_INV1
        )
        write_briefings(
            [_briefing("second")], ["consumer_lag_pass"], directory=tmp_path, invocation_id=_INV2
        )

        files = sorted(p.name for p in tmp_path.iterdir())
        assert len(files) == 2, f"a re-run replaced the first briefing: {files}"
        bodies = {json.loads((tmp_path / name).read_text())["alert_summary"] for name in files}
        assert bodies == {"first", "second"}
        newest = artifacts.newest("briefing", "consumer_lag_pass", directory=tmp_path)
        assert json.loads(newest.read_text())["alert_summary"] == "second"

    def test_two_runs_leave_two_reports(self, tmp_path: Path) -> None:
        write_report(_report(_INV1, _T1, total=1), directory=tmp_path)
        write_report(_report(_INV2, _T2, total=2), directory=tmp_path)

        files = sorted(p.name for p in tmp_path.rglob("*.json"))
        assert len(files) == 2, f"a re-run replaced the first report: {files}"
        newest = artifacts.newest("report", directory=tmp_path)
        assert RunReport.model_validate_json(newest.read_text()).invocation_id == _INV2

    def test_a_collision_raises_and_keeps_the_first_file(self, tmp_path: Path) -> None:
        """Exclusive-create is the load-bearing half — same as the archive writes.

        Nothing legitimate replays one run's exact identity, so it must crash.
        """
        write_trajectories([_trajectory("first", _INV1)], directory=tmp_path, timestamp=_T1)

        with pytest.raises(FileExistsError):
            write_trajectories([_trajectory("second", _INV1)], directory=tmp_path, timestamp=_T1)

        surviving = artifacts.newest("trajectory", "consumer_lag_pass", directory=tmp_path)
        assert json.loads(surviving.read_text())["incident_id"] == "first"

    def test_briefing_collision_raises(self, tmp_path: Path) -> None:
        write_briefings(
            [_briefing("first")],
            ["consumer_lag_pass"],
            directory=tmp_path,
            invocation_id=_INV1,
            timestamp=_T1,
        )
        with pytest.raises(FileExistsError):
            write_briefings(
                [_briefing("second")],
                ["consumer_lag_pass"],
                directory=tmp_path,
                invocation_id=_INV1,
                timestamp=_T1,
            )

    def test_report_collision_raises(self, tmp_path: Path) -> None:
        write_report(_report(_INV1, _T1), directory=tmp_path)
        with pytest.raises(FileExistsError):
            write_report(_report(_INV1, _T1), directory=tmp_path)

    def test_writers_never_touch_a_legacy_flat_file(self, tmp_path: Path) -> None:
        """Migration rule: the pre-versioning file is evidence and stays put.

        Never deleted, never renamed in place. The first versioned write
        lands beside it and immediately outranks it.
        """
        legacy = tmp_path / "consumer_lag_pass.json"
        legacy.write_text(json.dumps({"incident_id": "legacy"}))

        write_trajectories([_trajectory("fresh", _INV1)], directory=tmp_path)

        assert legacy.exists(), "the legacy flat file is evidence; it must survive"
        assert json.loads(legacy.read_text())["incident_id"] == "legacy"
        newest = artifacts.newest("trajectory", "consumer_lag_pass", directory=tmp_path)
        assert newest != legacy
        assert json.loads(newest.read_text())["incident_id"] == "fresh"


class TestHumanReportsAreVersioned:
    """``scripts/format_traces.py`` renders beside its predecessors, never over them."""

    @staticmethod
    def _trace(path: Path, invocation_id: str, scenario: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        records = [
            {
                "kind": "scenario_start",
                "invocation_id": invocation_id,
                "invocation_started_at": "2026-09-06T10:00:00+00:00",
                "timestamp": "2026-09-06T10:00:00+00:00",
                "scenario": scenario,
                "live_mcp": False,
                "live_llm": False,
                "model": "claude-sonnet-4-6",
                "judge_model": "claude-sonnet-4-6",
            },
            {
                "kind": "scenario_end",
                "invocation_id": invocation_id,
                "timestamp": "2026-09-06T10:01:00+00:00",
                "scenario": scenario,
                "passed": True,
                "final_state": "resolved",
            },
        ]
        path.write_text("".join(json.dumps(r) + "\n" for r in records))

    def test_two_renders_leave_two_reports(self, tmp_path: Path) -> None:
        from scripts.format_traces import main

        trace_dir = tmp_path / "traces"
        out_dir = tmp_path / "human"
        self._trace(trace_dir / "redis_saturation.jsonl", "inv0000000001", "redis_saturation")

        assert main(["--trace-dir", str(trace_dir), "--out-dir", str(out_dir)]) == 0
        # The second pass is INCREMENTAL (WO-R3-257), so ask for the re-render explicitly.
        assert main(["--trace-dir", str(trace_dir), "--out-dir", str(out_dir), "--force"]) == 0

        files = sorted(p.name for p in out_dir.rglob("*.txt"))
        assert len(files) == 2, f"a second render replaced the first report: {files}"
        assert all(f.endswith(".txt") for f in files)
        newest = artifacts.newest("human", "redis_saturation", directory=out_dir)
        assert "INCIDENT TRAJECTORY: redis_saturation" in newest.read_text()

    def test_render_collision_raises(self, tmp_path: Path) -> None:
        """A manufactured collision must raise, not replace."""
        from scripts.format_traces import render_to

        trace_dir = tmp_path / "traces"
        out_dir = tmp_path / "human"
        self._trace(trace_dir / "redis_saturation.jsonl", "inv0000000001", "redis_saturation")

        render_to(
            trace_dir / "redis_saturation.jsonl",
            out_dir,
            timestamp=_T1,
            render_id=_INV1,
        )
        with pytest.raises(FileExistsError):
            render_to(
                trace_dir / "redis_saturation.jsonl",
                out_dir,
                timestamp=_T1,
                render_id=_INV1,
            )


class TestNewestResolution:
    """One resolver, one ordering rule: stamp then invocation_id, never mtime."""

    @staticmethod
    def _touch(directory: Path, name: str, body: str = "{}") -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        path.write_text(body)
        return path

    def test_orders_by_stamp_not_by_mtime(self, tmp_path: Path) -> None:
        """The older run is written to disk LAST. Filename ordering must win.

        A resolver a backup tool can re-order is not a resolver.
        """
        newer = self._touch(tmp_path, "s.20260906T140000Z.bbbbbbbb0002.json", '{"n": 2}')
        older = self._touch(tmp_path, "s.20260906T101112Z.aaaaaaaa0001.json", '{"n": 1}')
        assert older.stat().st_mtime >= newer.stat().st_mtime

        assert artifacts.newest("trajectory", "s", directory=tmp_path) == newer

    def test_same_stamp_breaks_ties_on_invocation_id(self, tmp_path: Path) -> None:
        self._touch(tmp_path, "s.20260906T101112Z.aaaaaaaa0001.json")
        second = self._touch(tmp_path, "s.20260906T101112Z.bbbbbbbb0002.json")
        assert artifacts.newest("trajectory", "s", directory=tmp_path) == second

    def test_legacy_file_is_the_oldest_version(self, tmp_path: Path) -> None:
        legacy = self._touch(tmp_path, "s.json", '{"vintage": "legacy"}')
        assert artifacts.newest("trajectory", "s", directory=tmp_path) == legacy

        fresh = self._touch(tmp_path, "s.20260101T000000Z.aaaaaaaa0001.json")
        assert artifacts.versions("trajectory", "s", directory=tmp_path) == [legacy, fresh]
        assert artifacts.newest("trajectory", "s", directory=tmp_path) == fresh

    def test_other_scenarios_and_strays_are_not_members(self, tmp_path: Path) -> None:
        mine = self._touch(tmp_path, "s.20260906T101112Z.aaaaaaaa0001.json")
        self._touch(tmp_path, "other.20260907T101112Z.aaaaaaaa0001.json")
        self._touch(tmp_path, "s.20260906T101112Z.aaaaaaaa0001.json.bak")
        self._touch(tmp_path, "s.not-a-stamp.aaaaaaaa0001.json")

        assert artifacts.versions("trajectory", "s", directory=tmp_path) == [mine]

    def test_baseline_is_not_a_report_version(self, tmp_path: Path) -> None:
        """``evals/reports/`` also holds the committed baseline. It is not a run."""
        self._touch(tmp_path, "baseline.json")
        latest = self._touch(tmp_path, "latest.json")
        assert artifacts.versions("report", directory=tmp_path) == [latest]

        fresh = self._touch(tmp_path, "report.20260906T101112Z.aaaaaaaa0001.json")
        assert artifacts.versions("report", directory=tmp_path) == [latest, fresh]

    def test_missing_artifact_raises_rather_than_defaulting(self, tmp_path: Path) -> None:
        assert artifacts.newest_or_none("trajectory", "s", directory=tmp_path) is None
        with pytest.raises(FileNotFoundError, match="trajectory"):
            artifacts.newest("trajectory", "s", directory=tmp_path)

    def test_naive_timestamps_are_refused(self) -> None:
        with pytest.raises(ValueError, match="aware datetime"):
            artifacts.stamp(datetime(2026, 9, 6, 10, 11, 12))  # noqa: DTZ001

    def test_stamp_is_utc_regardless_of_input_zone(self) -> None:
        from datetime import timedelta, timezone

        plus_two = datetime(2026, 9, 6, 12, 11, 12, tzinfo=timezone(timedelta(hours=2)))
        assert artifacts.stamp(plus_two) == "20260906T101112Z"

    def test_invocation_id_must_be_filename_safe(self) -> None:
        with pytest.raises(ValueError, match="filename-safe"):
            artifacts.version_name("trajectory", "s", timestamp=_T1, invocation_id="../../etc")
        with pytest.raises(ValueError, match="invocation_id"):
            artifacts.version_name("trajectory", "s", timestamp=_T1, invocation_id="")


class TestTheLayoutIsOneFamilyPerFolder:
    """WO-R3-257: where each family lives, and that reads still find the old place.

    The layout is data in ``KINDS``, so these tests write an artifact and ask where it went.
    """

    _EXPECTED: Final[dict[str, tuple[str, ...]]] = {
        "report": ("evals", "reports", "runs", "2026-09"),
        "baseline_report": ("evals", "reports", "baseline"),
        "baseline_report_md": ("evals", "reports", "baseline"),
        "phase_close_report": ("evals", "reports", "phase-close"),
        "phase_close_report_md": ("evals", "reports", "phase-close"),
        "research_report": ("evals", "reports", "research"),
        "research_report_md": ("evals", "reports", "research"),
        "regrade_report": ("evals", "reports", "regrades"),
        "regrade_report_md": ("evals", "reports", "regrades"),
        "human": ("evals", "reports", "human", "consumer_lag_pass"),
        "dossier": ("evals", "reports", "dossiers", "consumer_lag_pass"),
        # WP-6.3. Per-JUDGE, not per-scenario: it rides the per-scenario mechanism because
        # the subject is a judge role name.
        "judge_calibration": ("evals", "reports", "judge-calibration", "consumer_lag_pass"),
        "trajectory": ("evals", "trajectories"),
        "briefing": ("evals", "briefings"),
        # WP-3.1, outside `evals/reports/`: a recording is an input, not a document.
        "recorded_world": ("evals", "recorded_worlds", "consumer_lag_pass"),
        "recorded_world_truth": ("evals", "recorded_worlds", "consumer_lag_pass"),
        # WP-15.1, outside `evals/reports/` for the same reason. Three families in one folder:
        # the training data, the evaluator labels a training path must not load, and the manifest.
        "training_export": ("evals", "exports"),
        "training_export_labels": ("evals", "exports"),
        "training_export_manifest": ("evals", "exports"),
    }

    @staticmethod
    def _scenario_for(kind: str) -> str | None:
        return "consumer_lag_pass" if artifacts.KINDS[kind].per_scenario else None

    def test_every_kind_writes_where_the_layout_says(self, tmp_path: Path) -> None:
        assert set(self._EXPECTED) == set(artifacts.KINDS), (
            "a new artifact kind was added without saying where in the tree it lives"
        )
        for kind, expected in self._EXPECTED.items():
            scenario = self._scenario_for(kind)
            written = artifacts.write_versioned(
                kind,
                scenario,
                content="{}",
                timestamp=_T1,
                invocation_id=_INV1,
                root=tmp_path,
            )
            assert written.parent.relative_to(tmp_path).parts == expected, kind

    def test_every_kind_resolves_from_the_new_layout(self, tmp_path: Path) -> None:
        """The other half: written there, found there, by ``newest``."""
        for kind in self._EXPECTED:
            scenario = self._scenario_for(kind)
            written = artifacts.write_versioned(
                kind,
                scenario,
                content="{}",
                timestamp=_T1,
                invocation_id=_INV1,
                root=tmp_path,
            )
            assert artifacts.newest(kind, scenario, root=tmp_path) == written, kind

    def test_every_kind_still_finds_a_file_left_in_the_old_flat_place(self, tmp_path: Path) -> None:
        """The transition window: merged, not yet migrated, and nothing breaks.

        Until ``scripts/migrate_reports_layout.py`` runs, every artifact is still flat; a
        new-folder-only resolver would report an empty reports folder.
        """
        for kind, expected in self._EXPECTED.items():
            scenario = self._scenario_for(kind)
            container = artifacts.directory_for(kind, root=tmp_path)
            container.mkdir(parents=True, exist_ok=True)
            flat = container / artifacts.version_name(
                kind, scenario, timestamp=_T1, invocation_id=_INV1
            )
            flat.write_text("{}")
            assert artifacts.newest(kind, scenario, root=tmp_path) == flat, kind
            assert artifacts.versions(kind, scenario, root=tmp_path) == [flat], kind
            # …and the new place still wins once something lands there.
            fresh = artifacts.write_versioned(
                kind,
                scenario,
                content="{}",
                timestamp=_T2,
                invocation_id=_INV2,
                root=tmp_path,
            )
            assert expected[-1] in fresh.parts
            assert artifacts.newest(kind, scenario, root=tmp_path) == fresh, kind

    def test_reports_group_by_the_month_they_were_generated_in(self, tmp_path: Path) -> None:
        """The folder comes from the report's own stamp, not from the clock."""
        december = datetime(2026, 12, 31, 23, 59, 59, tzinfo=UTC)
        written = write_report(_report(_INV1, december), directory=tmp_path)
        assert written.parent == tmp_path / "runs" / "2026-12"
        assert artifacts.newest("report", directory=tmp_path) == written

    def test_a_superseded_render_is_still_resolved(self, tmp_path: Path) -> None:
        """Moved aside for readability is not moved out of the record.

        ``human/_superseded/`` renders are evidence, so ``versions()`` returns them; being
        older, none can become ``newest()``.
        """
        kept = artifacts.write_versioned(
            "human",
            "consumer_lag_pass",
            content="new render",
            timestamp=_T2,
            invocation_id=_INV2,
            directory=tmp_path,
        )
        aside = tmp_path / artifacts.SUPERSEDED_DIR / "consumer_lag_pass"
        aside.mkdir(parents=True)
        older = aside / artifacts.version_name(
            "human", "consumer_lag_pass", timestamp=_T1, invocation_id=_INV1
        )
        older.write_text("old render")

        assert artifacts.versions("human", "consumer_lag_pass", directory=tmp_path) == [
            older,
            kept,
        ]
        assert artifacts.newest("human", "consumer_lag_pass", directory=tmp_path) == kept

    def test_the_two_pinned_files_are_not_members_of_any_family(self, tmp_path: Path) -> None:
        """``baseline.json`` and ``latest.json`` stay at the top and stay themselves.

        The gate reads ``baseline.json`` by a literal path, so it must not acquire a folder.
        """
        (tmp_path / "baseline.json").write_text("{}")
        latest = tmp_path / "latest.json"
        latest.write_text("{}")
        assert artifacts.versions("report", directory=tmp_path) == [latest]
        assert artifacts.versions("baseline_report", directory=tmp_path) == []

    def test_a_scenario_name_cannot_steer_a_write_out_of_its_folder(self) -> None:
        """A scenario name is a directory name now; it gets the same check an id does."""
        for bad in ("../../etc", "a/b", ".hidden", "..", ""):
            with pytest.raises(ValueError):
                artifacts.write_directory("human", bad)

    def test_parse_version_name_is_the_one_reading_of_a_filename(self) -> None:
        parsed = artifacts.parse_version_name(
            "consumer_lag_pass.20260906T101112Z.aaaaaaaa0001.txt", suffix=".txt"
        )
        assert parsed is not None
        assert (parsed.stem, parsed.stamp, parsed.invocation_id) == (
            "consumer_lag_pass",
            "20260906T101112Z",
            "aaaaaaaa0001",
        )
        assert parsed.timestamp() == datetime(2026, 9, 6, 10, 11, 12, tzinfo=UTC)
        assert artifacts.parse_version_name("consumer_lag_pass.txt", suffix=".txt") is None
        assert artifacts.parse_version_name("s.not-a-stamp.id.txt", suffix=".txt") is None
