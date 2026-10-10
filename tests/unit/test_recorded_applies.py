"""One rule for whether a recording's answer key describes its world (WO-R3-365, ADR 0040).

``oracle_gap`` and ``research_report`` both ask ``evals/recorded_applies.py``. The committed
recordings are read as they are; every other answer key is canned under a temporary root.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

import pytest

from evals import artifacts, oracle_gap, recorded_applies, research_report
from evals.graders.deterministic import DimensionResult, GradeDimension, GradeReport
from evals.graders.root_cause import not_graded_detail
from evals.runner import ExecutionMode, RunProvenance, ScenarioOutcome
from incident_commander.agent.state import BudgetLedger, IncidentState
from incident_commander.config import ModelRole

_AT: Final[datetime] = datetime(2026, 10, 10, 12, 0, 0, tzinfo=UTC)
_SCENARIO: Final[str] = "dlq_backlog"
_RECORDING: Final[str] = "0a0a0a0a0a0a"

#: The worlds whose newest committed recording was taken with no fault seeded (WO-R3-294).
_UNSEEDED_TODAY: Final[frozenset[str]] = frozenset(
    {"api_latency_healthy_control", "jobs_not_progressing_healthy_backlog_spike", "dlq_backlog"}
)


def _outcome(
    *,
    mode: ExecutionMode = ExecutionMode.RECORDED,
    replay: dict[str, Any] | None = None,
    root_cause_detail: str = "matched the label",
) -> ScenarioOutcome:
    if replay is None and mode is ExecutionMode.RECORDED:
        replay = {
            "recording": (
                f"evals/recorded_worlds/{_SCENARIO}/{_SCENARIO}.20261010T000000Z.{_RECORDING}.json"
            ),
            "world_fingerprint": "fp",
        }
    return ScenarioOutcome(
        scenario=_SCENARIO,
        final_state=IncidentState.ESCALATED,
        tool_calls_used=1,
        report=GradeReport(
            scenario=_SCENARIO,
            passed=True,
            dimensions=(
                DimensionResult(
                    dimension=GradeDimension.ROOT_CAUSE, passed=True, detail=root_cause_detail
                ),
            ),
        ),
        provenance=RunProvenance(
            commander_revision="rev",
            platform_image_digest="sha256:0",
            agent_model="claude-sonnet-4-6",
            model_role=ModelRole.BENCHMARK,
            judge_model="j",
            strategy="candidate_selector",
            strategy_config={"generator": "best_of_n_enumerated", "n": 8},
            scenario=_SCENARIO,
            invocation_id="inv",
            recorded_at=_AT,
            execution_mode=mode,
            budget=BudgetLedger(
                max_tool_calls=25, max_tokens=1000, max_wall_seconds=60, max_usd=Decimal("1")
            ),
        ),
        replay=replay,
    )


def _key(root: Path, *, applies: object) -> None:
    artifacts.write_versioned(
        "recorded_world_truth",
        _SCENARIO,
        content=json.dumps({"scenario": _SCENARIO, "applies": applies}),
        timestamp=_AT,
        invocation_id=_RECORDING,
        root=root,
    )


# --------------------------------------------------------------------------


class TestTheCommittedAnswerKeys:
    def test_every_committed_recording_has_an_answer_key_beside_it(self) -> None:
        """So "no key" never decides a committed world: each recording names its own."""
        for directory in sorted((artifacts.REPO_ROOT / "evals" / "recorded_worlds").iterdir()):
            if not directory.is_dir():
                continue
            for path in artifacts.versions("recorded_world", directory.name):
                parsed = artifacts.parse_version_name(path.name, suffix=".json")
                assert parsed is not None, path
                applies = recorded_applies.label_applies(directory.name, parsed.invocation_id)
                assert isinstance(applies, bool), path

    def test_three_worlds_newest_keys_say_applies_false(self) -> None:
        """The three the oracle-gap sample leaves out; a fourth would change the sample."""
        unseeded = set()
        for directory in sorted((artifacts.REPO_ROOT / "evals" / "recorded_worlds").iterdir()):
            if not directory.is_dir():
                continue
            newest = artifacts.newest("recorded_world", directory.name)
            parsed = artifacts.parse_version_name(newest.name, suffix=".json")
            assert parsed is not None
            if recorded_applies.label_applies(directory.name, parsed.invocation_id) is False:
                unseeded.add(directory.name)
        assert unseeded == _UNSEEDED_TODAY

    def test_an_unknown_recording_has_no_key(self) -> None:
        assert recorded_applies.label_applies(_SCENARIO, "deadbeefcafe") is None


class TestWhichRecording:
    def test_the_id_comes_off_the_replayed_path(self) -> None:
        assert recorded_applies.recording_of(_outcome()) == _RECORDING

    @pytest.mark.parametrize("replay", [{}, {"recording": "evals/recorded_worlds/x/not-versioned"}])
    def test_no_parseable_path_is_no_id(self, replay: dict[str, Any]) -> None:
        assert recorded_applies.recording_of(_outcome(replay=replay)) is None


class TestTheRule:
    def test_a_key_that_applies_lets_the_run_count(self, tmp_path: Path) -> None:
        _key(tmp_path, applies=True)
        assert recorded_applies.why_key_does_not_apply(_outcome(), root=tmp_path) == ""

    def test_a_key_that_says_false_excludes_it_and_names_the_recording(
        self, tmp_path: Path
    ) -> None:
        _key(tmp_path, applies=False)
        reason = recorded_applies.why_key_does_not_apply(_outcome(), root=tmp_path)
        assert reason.startswith("not graded:")
        assert f"recording {_RECORDING} says applies: false" in reason

    @pytest.mark.parametrize("applies", [None, "yes", 1])
    def test_no_key_or_a_non_boolean_one_is_not_a_yes(
        self, tmp_path: Path, applies: object
    ) -> None:
        if applies is not None:
            _key(tmp_path, applies=applies)
        reason = recorded_applies.why_key_does_not_apply(_outcome(), root=tmp_path)
        assert "no answer key beside recording" in reason

    def test_the_archives_not_graded_verdict_is_read_first(self, tmp_path: Path) -> None:
        """The runner graded ROOT_CAUSE off the recording's label; the key is not consulted."""
        _key(tmp_path, applies=True)
        outcome = _outcome(root_cause_detail=not_graded_detail("poison_message"))
        reason = recorded_applies.why_key_does_not_apply(outcome, root=tmp_path)
        assert reason == recorded_applies.NOT_GRADED

    def test_a_recorded_run_that_names_no_recording_is_excluded(self, tmp_path: Path) -> None:
        reason = recorded_applies.why_key_does_not_apply(_outcome(replay={}), root=tmp_path)
        assert "names no recording" in reason

    @pytest.mark.parametrize("mode", [ExecutionMode.LIVE, ExecutionMode.CANNED])
    def test_live_and_canned_runs_are_not_asked(self, tmp_path: Path, mode: ExecutionMode) -> None:
        """Canned is the label's world by definition; a live run keeps its own grade."""
        outcome = _outcome(mode=mode, root_cause_detail=not_graded_detail("poison_message"))
        assert recorded_applies.why_key_does_not_apply(outcome, root=tmp_path) == ""

    def test_with_no_root_it_reads_the_committed_keys(self) -> None:
        """dlq_backlog's newest recording (f0ee5ea8d33d) was taken unseeded."""
        replay = {"recording": f"x/{_SCENARIO}.20261010T082308Z.f0ee5ea8d33d.json"}
        reason = recorded_applies.why_key_does_not_apply(_outcome(replay=replay))
        assert "recording f0ee5ea8d33d says applies: false" in reason


class TestBothReportsAskTheSameRule:
    """A spy in place of the rule: each report must ask it, and take its answer as given."""

    @pytest.fixture
    def asked(self, monkeypatch: pytest.MonkeyPatch) -> list[str]:
        calls: list[str] = []

        def spy(outcome: ScenarioOutcome, *, root: Path | None = None) -> str:
            calls.append(outcome.scenario)
            return "spy: left out"

        monkeypatch.setattr(recorded_applies, "why_key_does_not_apply", spy)
        return calls

    def test_oracle_gap_asks_it_and_prints_its_reason(self, asked: list[str]) -> None:
        plan = oracle_gap.load_plan()
        plan = plan.model_copy(
            update={"worlds": (oracle_gap.SampleWorld(scenario=_SCENARIO, recording=_RECORDING),)}
        )
        reason = oracle_gap.why_not_scored(_outcome(), plan, {_SCENARIO: _RECORDING}, {})
        assert (reason, asked) == ("spy: left out", [_SCENARIO])

    def test_research_report_asks_it_for_every_run_with_step_records(
        self, asked: list[str]
    ) -> None:
        root = research_report.REPO_ROOT
        research_report._candidate_exclusions(root, research_report.read_scope(root))
        assert asked  # three archives in today's scope carry step records
