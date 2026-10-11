"""`--repeats N` and `--study` on the runner (WO-R3-375, owner O-55's pilot of 4 worlds x 3).

One runner invocation makes N passes over the same pinned worlds, so a pilot of three repeats is
one paid ask (O-45). Each pass is its own archive under its own invocation id: every archive keeps
one row per scenario, and every reader that pairs a run by (archive, scenario) is untouched. Each
row's provenance says which pass it was (`repeat`), which invocation it belongs to (`repeat_of`)
and which sample plan asked for it (`study`). Hermetic and free: the model leg is the canned planner
or a stub, the platform leg a committed recording.
"""

from __future__ import annotations

import os
import re
import stat
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import pytest
from pydantic import SecretStr

from evals import artifacts, runner
from evals.graders.deterministic import GradeReport
from evals.runner import RunProvenance, RunReport, ScenarioOutcome, ScenarioResult, Trajectory
from evals.scenarios.loader import load_scenarios
from evals.scenarios.schema import Scenario
from incident_commander.agent.briefing import EscalationBriefing
from incident_commander.agent.state import IncidentState

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
#: A read-only scenario with a committed recording: a canned-planner replay runs to the end.
_READ_ONLY: Final[str] = "dlq_backlog"
#: A scenario whose name is no other scenario's substring, so ``--only`` selects it alone.
_PINNABLE: Final[str] = "workflow_stuck_paused_dag"
_INVOCATION_ID: Final[re.Pattern[str]] = re.compile(r"\A[0-9a-f]{12}\Z")


@pytest.fixture(autouse=True)
def _archives_unlocked_for_cleanup(tmp_path: Path) -> Iterator[None]:
    """`main` locks every archive it seals (ADR 0021); unlock them so pytest can clean up."""
    yield
    paths = [tmp_path, *tmp_path.rglob("*")]
    for path in paths:
        if hasattr(os, "chflags"):
            try:
                os.chflags(path, getattr(path.stat(), "st_flags", 0) & ~stat.UF_IMMUTABLE)
            except OSError:
                continue
    for path in paths:
        try:
            path.chmod(path.stat().st_mode | (0o700 if path.is_dir() else 0o600))
        except OSError:
            continue


@pytest.fixture(scope="module")
def scenarios() -> dict[str, Scenario]:
    return {s.name: s for s in load_scenarios(_REPO_ROOT / "evals" / "scenarios")}


def _recording(scenario: str) -> Path:
    path = artifacts.newest_or_none("recorded_world", scenario)
    assert path is not None, f"no committed recording for {scenario}"
    return path


def _recording_id(scenario: str) -> str:
    return _recording(scenario).name.rsplit(".", 2)[1]


class TestTheFlags:
    def test_no_flag_is_one_pass(self) -> None:
        assert runner._parse_repeats(["--only", "x"]) == (1, "")

    @pytest.mark.parametrize(
        ("argv", "expected"),
        [(["--repeats", "3"], 3), (["--repeats=2"], 2), (["--repeats", "1"], 1)],
    )
    def test_a_whole_number_of_passes_is_read(self, argv: list[str], expected: int) -> None:
        assert runner._parse_repeats(argv) == (expected, "")

    @pytest.mark.parametrize(
        "argv",
        [
            ["--repeats", "0"],
            ["--repeats", "-1"],
            ["--repeats", "x"],
            ["--repeats=1.5"],
            ["--repeats"],
            ["--repeats", "2", "--repeats", "3"],
        ],
    )
    def test_anything_else_is_refused(self, argv: list[str]) -> None:
        repeats, refusal = runner._parse_repeats(argv)
        assert repeats is None
        assert refusal.startswith("REPEATS FAIL")

    def test_a_study_is_a_short_name(self) -> None:
        assert runner._parse_study([]) == (None, "")
        assert runner._parse_study(["--study", "oracle_gap_pilot"]) == ("oracle_gap_pilot", "")
        assert runner._parse_study(["--study=oracle_gap"]) == ("oracle_gap", "")
        for bad in (["--study", "Two Words"], ["--study"], ["--study", "a", "--study", "b"]):
            study, refusal = runner._parse_study(bad)
            assert study is None
            assert refusal.startswith("STUDY FAIL"), bad


class TestOnlyARecordedRunRepeats:
    def _main(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *argv: str) -> int:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(sys, "argv", ["evals.runner", *argv])
        return runner.main()

    @pytest.mark.parametrize(
        "argv",
        [
            ("--repeats", "3", "--only", _READ_ONLY),
            ("--live", "--repeats", "2", "--only", _READ_ONLY),
        ],
    )
    def test_a_canned_or_live_repeat_is_refused_before_anything_runs(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        argv: tuple[str, ...],
    ) -> None:
        """A live pass acts on the shared world with no reset between; canned repeats a file."""
        monkeypatch.setattr(runner, "run_all", _must_not_run)
        assert self._main(monkeypatch, tmp_path, *argv) == 2
        out = capsys.readouterr().out
        assert "REPEATS FAIL" in out
        assert "nothing was spent" in out

    def test_a_malformed_count_is_refused_before_anything_runs(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(runner, "run_all", _must_not_run)
        argv = ("--mode", "recorded", "--only", _READ_ONLY, "--repeats", "0")
        assert self._main(monkeypatch, tmp_path, *argv) == 2
        assert "REPEATS FAIL" in capsys.readouterr().out


def _must_not_run(*_args: object, **_kwargs: object) -> Any:
    raise AssertionError("the suite was started")


def _finished(scenario: str) -> ScenarioResult:
    return ScenarioResult(
        outcome=ScenarioOutcome(
            scenario=scenario,
            final_state=IncidentState.ESCALATED,
            tool_calls_used=1,
            report=GradeReport(scenario=scenario, passed=True, dimensions=()),
        ),
        trajectory=Trajectory(scenario=scenario, incident_id="i", checkpoints=()),
        briefing=EscalationBriefing(
            incident_id="i", final_state=IncidentState.ESCALATED, alert_summary="a"
        ),
    )


class TestEveryPassIsItsOwnArchive:
    """One invocation, N passes; one archive per pass, one row per scenario in each."""

    def _main(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *argv: str
    ) -> tuple[int, list[dict[str, Any]]]:
        calls: list[dict[str, Any]] = []
        runs = tmp_path / "runs"

        def _stub_run_all(_scenarios: Any, _settings: Any, **kwargs: Any) -> Any:
            calls.append(kwargs)
            kwargs["on_result"](_finished(_PINNABLE))
            report = RunReport(
                generated_at=datetime.now(UTC),
                invocation_id=kwargs["invocation_id"],
                total=1,
                passed=1,
                failed=0,
                outcomes=(_finished(_PINNABLE).outcome,),
            )
            return report, (), ()

        monkeypatch.chdir(tmp_path)
        # A key that is not the placeholder, so `--mode recorded` passes its preflight; the
        # suite is stubbed, so nothing can reach a model with it.
        settings = runner._eval_defaults().model_copy(
            update={"anthropic_api_key": SecretStr("not-a-real-key")}
        )
        monkeypatch.setattr(runner, "_settings_for_mode", lambda *_a, **_kw: settings)
        monkeypatch.setattr(runner, "_RUNS_DIR", runs)
        monkeypatch.setattr(runner, "run_all", _stub_run_all)
        monkeypatch.setattr(runner, "write_report", lambda *_a, **_kw: tmp_path / "r.json")
        monkeypatch.setattr(runner, "write_trajectories", lambda *_a, **_kw: [])
        monkeypatch.setattr(runner, "write_briefings", lambda *_a, **_kw: [])
        monkeypatch.setattr(sys, "argv", ["evals.runner", *argv])
        return runner.main(), calls

    def test_three_passes_are_three_archives_with_distinct_repeat_indexes(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        argv = (
            "--mode",
            "recorded",
            "--only",
            _PINNABLE,
            "--world",
            _recording_id(_PINNABLE),
            "--repeats",
            "3",
            "--study",
            "oracle_gap_pilot",
        )
        code, calls = self._main(monkeypatch, tmp_path, *argv)
        assert code == 0
        assert [call["repeat"] for call in calls] == [0, 1, 2]
        ids = [call["invocation_id"] for call in calls]
        assert len(set(ids)) == 3
        assert all(_INVOCATION_ID.match(pass_id) for pass_id in ids)
        # Every pass names pass 0, so the three archives can be found from any one of them.
        assert [call["repeat_of"] for call in calls] == [ids[0]] * 3
        assert {call["study"] for call in calls} == {"oracle_gap_pilot"}
        # Every pass replays the same pinned world.
        assert len({str(call["recorded_worlds"][_PINNABLE]) for call in calls}) == 1
        for pass_id in ids:
            assert (tmp_path / "runs" / pass_id / "report.json").is_file()
            assert (tmp_path / "runs" / pass_id / "trajectories" / f"{_PINNABLE}.json").is_file()
        out = capsys.readouterr().out
        assert "pass 3 of 3" in out
        assert ", ".join(ids) in out

    def test_without_the_flag_it_is_one_pass_and_names_no_series(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        argv = ("--mode", "recorded", "--only", _PINNABLE, "--world", _recording_id(_PINNABLE))
        code, calls = self._main(monkeypatch, tmp_path, *argv)
        assert code == 0
        (only,) = calls
        assert (only["repeat"], only["repeat_of"], only["study"]) == (0, None, None)
        assert [path.name for path in (tmp_path / "runs").iterdir()] == [only["invocation_id"]]


class TestEachRowSaysWhichPassItWas:
    def test_every_row_carries_its_pass_its_invocation_and_its_study(
        self, scenarios: dict[str, Scenario]
    ) -> None:
        report, _, _ = runner.run_all(
            [scenarios[_READ_ONLY]],
            runner._eval_defaults(),
            invocation_id="bbbbbbbbbbbb",
            recorded_worlds={_READ_ONLY: _recording(_READ_ONLY)},
            repeat=2,
            repeat_of="aaaaaaaaaaaa",
            study="oracle_gap_pilot",
        )
        (outcome,) = report.outcomes
        provenance = outcome.provenance
        assert provenance is not None
        assert (provenance.repeat, provenance.repeat_of, provenance.study) == (
            2,
            "aaaaaaaaaaaa",
            "oracle_gap_pilot",
        )
        assert provenance.invocation_id == "bbbbbbbbbbbb"

    def test_a_crashed_row_is_stamped_too(
        self, scenarios: dict[str, Scenario], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(*_a: Any, **_kw: Any) -> Any:
            raise RuntimeError("boom")

        monkeypatch.setattr(runner, "run_scenario", _boom)
        report, _, _ = runner.run_all(
            [scenarios[_READ_ONLY]],
            runner._eval_defaults(),
            invocation_id="cccccccccccc",
            recorded_worlds={_READ_ONLY: _recording(_READ_ONLY)},
            repeat=1,
            repeat_of="aaaaaaaaaaaa",
        )
        (outcome,) = report.outcomes
        assert outcome.provenance is not None
        assert (outcome.provenance.repeat, outcome.provenance.repeat_of) == (1, "aaaaaaaaaaaa")

    def test_an_unrepeated_run_is_pass_zero_of_no_series(
        self, scenarios: dict[str, Scenario]
    ) -> None:
        report, _, _ = runner.run_all(
            [scenarios[_READ_ONLY]],
            runner._eval_defaults(),
            invocation_id="dddddddddddd",
            recorded_worlds={_READ_ONLY: _recording(_READ_ONLY)},
        )
        (outcome,) = report.outcomes
        assert outcome.provenance is not None
        assert (outcome.provenance.repeat, outcome.provenance.repeat_of) == (0, None)
        assert outcome.provenance.study is None

    def test_an_archive_written_before_the_fields_still_parses(self) -> None:
        """Locked archives predate the three fields; they read as pass 0 of no series."""
        fields = RunProvenance.model_fields
        assert fields["repeat"].default == 0
        assert fields["repeat_of"].default is None
        assert fields["study"].default is None


class TestAPinnedRunSelectsByFullName:
    """The pilot pins `jobs_not_progressing_outbox_stall` without its deploy-noise sibling.

    By substring that name also selects `jobs_not_progressing_outbox_stall_deploy_noise`, which
    the pilot does not pin, and the all-or-nothing rule then refused the whole batch — at paid
    time, after the free check had passed. A run that pins its worlds names them in full.
    """

    _PREFIX: Final[str] = "jobs_not_progressing_outbox_stall"

    def _main(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *argv: str
    ) -> tuple[int, list[list[str]]]:
        selected: list[list[str]] = []

        def _stub_run_all(scenarios: Any, _settings: Any, **kwargs: Any) -> Any:
            selected.append([scenario.name for scenario in scenarios])
            report = RunReport(
                generated_at=datetime.now(UTC),
                invocation_id=kwargs["invocation_id"],
                total=0,
                passed=0,
                failed=0,
                outcomes=(),
            )
            return report, (), ()

        monkeypatch.chdir(tmp_path)
        settings = runner._eval_defaults().model_copy(
            update={"anthropic_api_key": SecretStr("not-a-real-key")}
        )
        monkeypatch.setattr(runner, "_settings_for_mode", lambda *_a, **_kw: settings)
        monkeypatch.setattr(runner, "_RUNS_DIR", tmp_path / "runs")
        monkeypatch.setattr(runner, "run_all", _stub_run_all)
        monkeypatch.setattr(runner, "write_report", lambda *_a, **_kw: tmp_path / "r.json")
        monkeypatch.setattr(runner, "write_trajectories", lambda *_a, **_kw: [])
        monkeypatch.setattr(runner, "write_briefings", lambda *_a, **_kw: [])
        monkeypatch.setattr(sys, "argv", ["evals.runner", *argv])
        return runner.main(), selected

    def test_a_name_that_prefixes_another_selects_only_itself_when_pinned(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        argv = (
            "--mode",
            "recorded",
            "--only",
            self._PREFIX,
            "--world",
            _recording_id(self._PREFIX),
        )
        code, selected = self._main(monkeypatch, tmp_path, *argv)
        assert code == 0
        assert selected == [[self._PREFIX]]

    def test_a_pinned_run_refuses_a_pattern_that_is_not_a_name(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        argv = (
            "--mode",
            "recorded",
            "--only",
            "outbox_stall",
            "--world",
            _recording_id(self._PREFIX),
        )
        code, selected = self._main(monkeypatch, tmp_path, *argv)
        assert code == 2
        assert selected == []
        out = capsys.readouterr().out
        assert "SELECTION FAIL" in out
        assert "nothing was spent" in out
