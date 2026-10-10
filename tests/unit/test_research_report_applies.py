"""research_report leaves out a recorded run whose answer key does not describe its world.

WO-R3-365. Before it, ``_candidate_rows`` scored every enumerated run against the scenario's
label, even where the recording's own answer key says ``applies: false`` (ADR 0040). Hermetic:
a synthetic root with two copied scenarios, canned answer keys and hand-built step records.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

import pytest

from evals import artifacts
from evals import research_report as research
from evals.graders.deterministic import DimensionResult, GradeDimension, GradeReport
from evals.graders.root_cause import not_graded_detail
from evals.runner import ExecutionMode, RunProvenance, RunReport, ScenarioOutcome
from incident_commander.agent.state import BudgetLedger, IncidentState
from incident_commander.config import ModelRole

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
_AT: Final[datetime] = datetime(2026, 10, 10, 12, 0, 0, tzinfo=UTC)

#: The unseeded world (its key says applies: false) and a seeded one (applies: true).
_UNSEEDED: Final[tuple[str, str]] = ("dlq_backlog", "0a0a0a0a0a0a")
_SEEDED: Final[tuple[str, str]] = ("workflow_stuck_paused_dag", "0b0b0b0b0b0b")
_ARM: Final[str] = "candidate_selector/best_of_n_enumerated/n=2"

_RECORDED: Final[str] = "aaaaaaaaaaaa"
_LIVE: Final[str] = "cccccccccccc"


def _record(categories: Sequence[str]) -> dict[str, Any]:
    """One candidate_selector step: a two-candidate set, the selector taking the first."""
    return {
        "kind": "step",
        "iteration": 0,
        "strategy": "candidate_selector",
        "candidate_set": [
            {"candidate_id": f"c{i}", "category": category, "name": f"c{i}"}
            for i, category in enumerate(categories, start=1)
        ],
        "selector": {
            "selected_candidate_id": "c1",
            "scores": {"c1": 0.7, "c2": 0.3},
            "uncertainty": 0.3,
            "decision": "select",
        },
        "emitted_step": {"next_action": {"kind": "stop"}},
        "generation_rejections": [],
    }


#: Each scenario's own label first, so every counted run is a hit at k=1.
_SETS: Final[dict[str, tuple[str, str]]] = {
    _UNSEEDED[0]: ("poison_message", "stale_cache"),
    _SEEDED[0]: ("dag_paused", "resolver_stall"),
}


def _outcome(
    scenario: str, *, recording: str | None, root_cause_detail: str = "matched the label"
) -> ScenarioOutcome:
    """A recorded run of ``scenario`` replaying ``recording``, or a live one when it is None."""
    mode = ExecutionMode.LIVE if recording is None else ExecutionMode.RECORDED
    return ScenarioOutcome(
        scenario=scenario,
        final_state=IncidentState.ESCALATED,
        tool_calls_used=2,
        report=GradeReport(
            scenario=scenario,
            passed=True,
            dimensions=(
                DimensionResult(
                    dimension=GradeDimension.ROOT_CAUSE, passed=True, detail=root_cause_detail
                ),
            ),
        ),
        provenance=RunProvenance(
            commander_revision="0" * 40,
            platform_image_digest="sha256:" + "1" * 64,
            agent_model="claude-sonnet-4-6",
            model_role=ModelRole.BENCHMARK,
            judge_model="claude-haiku-4-5",
            strategy="candidate_selector",
            strategy_config={"generator": "best_of_n_enumerated", "n": 2},
            scenario=scenario,
            invocation_id="a" * 12,
            recorded_at=_AT,
            execution_mode=mode,
            budget=BudgetLedger(
                max_tool_calls=25,
                tool_calls_used=2,
                max_tokens=400_000,
                tokens_used=1000,
                max_wall_seconds=600,
                wall_seconds_used=10.0,
                max_usd=Decimal("1"),
                usd_used=Decimal("0.30"),
            ),
        ),
        replay=(
            None
            if recording is None
            else {
                "recording": (
                    f"evals/recorded_worlds/{scenario}/{scenario}.20261010T000000Z.{recording}.json"
                ),
                "world_fingerprint": f"fp-{recording}",
                "misses": 0,
                "refusals": [],
            }
        ),
    )


def _archive(root: Path, archive: str, outcomes: Sequence[ScenarioOutcome]) -> None:
    directory = root / "evals" / "runs" / archive
    (directory / "traces").mkdir(parents=True)
    report = RunReport(
        generated_at=_AT,
        total=len(outcomes),
        passed=len(outcomes),
        failed=0,
        invocation_id=archive,
        outcomes=tuple(outcomes),
    )
    (directory / "report.json").write_text(report.model_dump_json())
    for outcome in outcomes:
        record = _record(_SETS[outcome.scenario])
        (directory / "traces" / f"{outcome.scenario}.jsonl").write_text(json.dumps(record) + "\n")


def _answer_key(root: Path, scenario: str, recording: str, *, applies: bool) -> None:
    """A canned ``.truth.json`` beside a recording that need not exist: only the key is read."""
    artifacts.write_versioned(
        "recorded_world_truth",
        scenario,
        content=json.dumps({"scenario": scenario, "recording": recording, "applies": applies}),
        timestamp=_AT,
        invocation_id=recording,
        root=root,
    )


def _root(tmp_path: Path, *, unseeded_applies: bool = False, runner_said: str = "") -> Path:
    """Two scenarios, their answer keys, a recorded archive of both and a live run of one."""
    scenarios = tmp_path / "evals" / "scenarios"
    scenarios.mkdir(parents=True)
    for name in (_UNSEEDED[0], _SEEDED[0]):
        shutil.copy(_REPO_ROOT / "evals" / "scenarios" / f"{name}.yaml", scenarios)
    _answer_key(tmp_path, *_UNSEEDED, applies=unseeded_applies)
    _answer_key(tmp_path, *_SEEDED, applies=True)
    unseeded = _outcome(
        _UNSEEDED[0], recording=_UNSEEDED[1], root_cause_detail=runner_said or "matched the label"
    )
    _archive(tmp_path, _RECORDED, [unseeded, _outcome(_SEEDED[0], recording=_SEEDED[1])])
    # The same unseeded scenario run LIVE: the rule asks recorded runs only.
    _archive(tmp_path, _LIVE, [_outcome(_UNSEEDED[0], recording=None)])
    return tmp_path


def _sections(root: Path) -> dict[str, Any]:
    document = research.assemble(root, (_RECORDED, _LIVE))
    return dict(document["sections"])


def _scored(section: dict[str, Any]) -> set[tuple[str, str]]:
    return {(row["archive"], row["scenario"]) for row in section["value"]["rows"]}


# --------------------------------------------------------------------------


class TestAnUnseededRecordingIsExcluded:
    def test_its_run_leaves_pass_at_k_and_the_n_drops(self, tmp_path: Path) -> None:
        section = _sections(_root(tmp_path))["pass_at_k_vs_selected_at_k"]
        assert section["measurable"] is True
        assert _scored(section) == {(_RECORDED, _SEEDED[0]), (_LIVE, _UNSEEDED[0])}
        assert section["value"]["runs"] == 2

    def test_it_is_listed_with_its_reason_and_not_counted_as_a_miss(self, tmp_path: Path) -> None:
        section = _sections(_root(tmp_path))["pass_at_k_vs_selected_at_k"]
        (excluded,) = section["excluded"]
        assert (excluded["archive"], excluded["scenario"], excluded["recording"]) == (
            _RECORDED,
            *_UNSEEDED,
        )
        assert "applies: false" in excluded["reason"]
        assert excluded["selector_calls"] == 1

    def test_the_archives_own_not_graded_verdict_excludes_it_too(self, tmp_path: Path) -> None:
        """What the runner writes for that world: ROOT_CAUSE not graded. Same exclusion."""
        root = _root(tmp_path, runner_said=not_graded_detail("poison_message"))
        section = _sections(root)["pass_at_k_vs_selected_at_k"]
        assert section["value"]["runs"] == 2
        (excluded,) = section["excluded"]
        assert excluded["reason"].startswith("not graded: the label does not describe")

    def test_it_leaves_the_oracle_gap_and_the_paired_count_drops(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(research, "CALIBRATION_REPORTS", MappingProxyType({_ARM: "cal-1234"}))
        # A registered id opens the gate only with its report, made on the runs' model (WO-R3-364).
        artifacts.write_versioned(
            "judge_calibration",
            "candidate_selector",
            content=json.dumps({"model": "claude-sonnet-4-6"}),
            timestamp=_AT,
            invocation_id="cal-1234",
            root=tmp_path,
        )
        section = _sections(_root(tmp_path))["oracle_gap"]
        assert section["measurable"] is True
        worlds = {entry["group"]: entry for entry in section["value"]["by_world"]}
        assert not any(f"fp-{_UNSEEDED[1]}" in world for world in worlds)
        assert sum(entry["paired_runs"] for entry in worlds.values()) == 2
        by_family = section["value"]["by_family"]
        assert sum(entry["paired_runs"] for entry in by_family) == 2
        (excluded,) = section["excluded"]
        assert excluded["scenario"] == _UNSEEDED[0]

    def test_uncalibrated_it_still_says_how_many_rows_remain_and_which_left(
        self, tmp_path: Path
    ) -> None:
        section = _sections(_root(tmp_path))["oracle_gap"]
        assert section["measurable"] is False
        assert section["why"].startswith("2 selector row(s) are in scope")
        (excluded,) = section["excluded"]
        assert excluded["scenario"] == _UNSEEDED[0]

    def test_the_markdown_lists_it(self, tmp_path: Path) -> None:
        document = research.assemble(_root(tmp_path), (_RECORDED, _LIVE))
        rendered = research.render_markdown(document)
        assert "Excluded (1)" in rendered
        assert f"`{_RECORDED}` {_UNSEEDED[0]} (recording `{_UNSEEDED[1]}`)" in rendered
        assert "applies: false" in rendered


class TestASeededRecordingIsCounted:
    def test_a_key_that_applies_keeps_the_run(self, tmp_path: Path) -> None:
        section = _sections(_root(tmp_path, unseeded_applies=True))["pass_at_k_vs_selected_at_k"]
        assert section["value"]["runs"] == 3
        assert section["excluded"] == []
        assert (_RECORDED, _UNSEEDED[0]) in _scored(section)

    def test_with_every_run_excluded_the_section_says_why(self, tmp_path: Path) -> None:
        """Only the unseeded recorded run in scope: nothing left to score, and it says so."""
        root = _root(tmp_path)
        section = research.assemble(root, (_RECORDED,))["sections"]["pass_at_k_vs_selected_at_k"]
        assert section["value"]["runs"] == 1  # the seeded world is still there
        only = tmp_path / "only"
        shutil.copytree(root / "evals" / "scenarios", only / "evals" / "scenarios")
        _answer_key(only, *_UNSEEDED, applies=False)
        _archive(only, _RECORDED, [_outcome(_UNSEEDED[0], recording=_UNSEEDED[1])])
        section = research.assemble(only, (_RECORDED,))["sections"]["pass_at_k_vs_selected_at_k"]
        assert section["measurable"] is False
        assert "answer key does not describe" in section["why"]
        assert [entry["scenario"] for entry in section["excluded"]] == [_UNSEEDED[0]]
