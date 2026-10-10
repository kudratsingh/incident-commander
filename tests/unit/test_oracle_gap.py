"""The oracle-gap sample driver (WO-R3-347): the plan, the batch, the numbers, the report.

Hermetic and free: the runner is never started for real, step records are hand-built, and
archives are written to a temporary directory. Only committed recordings are read.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

import pytest

from evals import artifacts, oracle_gap, research_report
from evals.candidate_metrics import world_key
from evals.graders.deterministic import DimensionResult, GradeDimension, GradeReport
from evals.graders.root_cause import not_graded_detail
from evals.oracle_gap import RunRow, SamplePlan, SampleWorld
from evals.runner import ExecutionMode, RunProvenance, RunReport, ScenarioOutcome
from evals.scenarios.loader import load_scenarios
from evals.scenarios.schema import ScenarioDifficulty
from incident_commander.agent.hypothesis import HypothesisCategory
from incident_commander.agent.state import BudgetLedger, IncidentState
from incident_commander.config import ModelRole

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
_AT: Final[datetime] = datetime(2026, 10, 10, 12, 0, 0, tzinfo=UTC)

#: Two committed recordings whose answer keys apply, and one whose key does not (unseeded).
_PAUSED: Final[tuple[str, str]] = ("workflow_stuck_paused_dag", "93673b198b72")
_LAG: Final[tuple[str, str]] = ("remediate_consumer_lag_success", "c8c4f0119dcd")
_UNSEEDED: Final[tuple[str, str]] = ("dlq_backlog", "1800b3ddafb5")

_ARM_CONFIG: Final[dict[str, Any]] = {
    "n": 8,
    "evidence_ids_rendered": True,
    "generator": "best_of_n_enumerated",
    "selector": "candidate_selector",
}


def _plan(*worlds: tuple[str, str]) -> SamplePlan:
    committed = oracle_gap.load_plan()
    return committed.model_copy(
        update={"worlds": tuple(SampleWorld(scenario=s, recording=r) for s, r in worlds)}
    )


def _record(
    categories: Sequence[str], *, selected: str | None, decision: str = "select"
) -> dict[str, Any]:
    """One candidate_selector step record, in the JSON shape a trace holds."""
    return {
        "kind": "step",
        "iteration": 0,
        "strategy": "candidate_selector",
        "candidate_set": [
            {"candidate_id": f"c{i}", "category": category, "name": f"c{i}"}
            for i, category in enumerate(categories, start=1)
        ],
        "selector": {
            "selected_candidate_id": selected,
            "scores": {f"c{i}": 0.5 for i in range(1, len(categories) + 1)},
            "uncertainty": 0.3,
            "decision": decision,
        },
        "emitted_step": {"next_action": {"kind": "stop"}},
        "generation_rejections": [],
    }


def _row(
    world: str, categories: Sequence[str], *, selected: str | None, decision: str = "select"
) -> RunRow:
    return oracle_gap.run_row(
        archive="aaaaaaaaaaaa",
        scenario=world,
        recording="0123456789ab",
        world=world_key(
            scenario=world, execution_mode="recorded", archive="a", world_fingerprint=world
        ),
        agent_model="claude-sonnet-4-6",
        records=[_record(categories, selected=selected, decision=decision)],
        expected=[HypothesisCategory.DAG_PAUSED],
    )


#: The truth is dag_paused; c1 is it, c2 is not.
_SET: Final[tuple[str, ...]] = ("dag_paused", "resolver_stall")


# --------------------------------------------------------------------------


class TestThePlan:
    def test_the_committed_plan_is_thirteen_fault_worlds_on_one_arm(self) -> None:
        plan = oracle_gap.load_plan()
        assert plan.arm == "candidate_selector/best_of_n_enumerated/n=8"
        assert plan.model_role is ModelRole.BENCHMARK
        assert len(plan.worlds) == 13
        corpus = {s.name: s for s in load_scenarios(_REPO_ROOT / "evals" / "scenarios")}
        for world in plan.worlds:
            scenario = corpus[world.scenario]
            assert scenario.ground_truth is not None, world.scenario
            # A control's right answer is "nothing is wrong", not a diagnosis of a fault.
            assert scenario.difficulty is not ScenarioDifficulty.CONTROL, world.scenario

    def test_placeholder_ids_are_refused_before_anything_runs(self) -> None:
        plan = oracle_gap.load_plan()
        refusals = oracle_gap.plan_refusals(plan, plan.worlds)
        assert len(refusals) == len(plan.worlds)
        assert all("is not a recording id" in line for line in refusals)

    def test_filled_ids_of_committed_recordings_pass(self) -> None:
        plan = _plan(_PAUSED, _LAG)
        assert oracle_gap.plan_refusals(plan, plan.worlds) == []

    def test_a_world_whose_answer_key_does_not_apply_is_refused(self) -> None:
        plan = _plan(_UNSEEDED)
        (refusal,) = oracle_gap.plan_refusals(plan, plan.worlds)
        assert "applies=False" in refusal

    def test_an_unknown_recording_is_refused(self) -> None:
        plan = _plan((_PAUSED[0], "deadbeefcafe"))
        (refusal,) = oracle_gap.plan_refusals(plan, plan.worlds)
        assert "no recording deadbeefcafe" in refusal

    def test_a_scenario_listed_twice_is_refused(self) -> None:
        plan = _plan(_PAUSED, _PAUSED)
        assert any("listed twice" in line for line in oracle_gap.plan_refusals(plan, plan.worlds))


class TestTheBatch:
    def test_one_invocation_pins_one_world_per_scenario(self) -> None:
        plan = _plan(_PAUSED, _LAG)
        argv, env = oracle_gap.batch_invocation(plan, plan.worlds)
        assert argv[:4] == ["--mode", "recorded", "--model-role", "benchmark"]
        assert argv[argv.index("--only") + 1] == f"{_PAUSED[0]},{_LAG[0]}"
        assert [argv[i + 1] for i, arg in enumerate(argv) if arg == "--world"] == [
            _PAUSED[1],
            _LAG[1],
        ]
        assert env == {
            "INFERENCE_STRATEGY": "candidate_selector",
            "SELECTOR_GENERATOR": "best_of_n_enumerated",
            "BEST_OF_N": "8",
            "TOKEN_BUDGET_MULTIPLIER": "2",
            "USD_BUDGET_MULTIPLIER": "1",
            "EVAL_TRACE_DIR": "evals/traces",
        }

    def test_without_yes_spend_it_prints_the_command_and_runs_nothing(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(subprocess, "run", _must_not_run)
        assert oracle_gap.run_batch(_plan(_PAUSED, _LAG), yes_spend=False) == 0
        out = capsys.readouterr().out
        assert "uv run python -m evals.runner --mode recorded" in out
        assert "Nothing was spent" in out

    def test_a_plan_that_cannot_run_never_reaches_the_runner(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(subprocess, "run", _must_not_run)
        assert oracle_gap.run_batch(oracle_gap.load_plan(), yes_spend=True) == 2

    def test_a_subset_outside_the_plan_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(subprocess, "run", _must_not_run)
        assert oracle_gap.run_batch(_plan(_PAUSED), only=["nope"], yes_spend=True) == 2

    def test_a_runner_refusal_writes_no_report(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(
            subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, returncode=3)
        )
        assert oracle_gap.run_batch(_plan(_PAUSED), yes_spend=True, root=tmp_path) == 3
        assert not (tmp_path / "evals" / "reports").exists()

    @pytest.mark.skipif(shutil.which("make") is None, reason="make not available")
    def test_the_make_target_spends_only_on_yes_spend_exactly_one(self) -> None:
        def recipe(*args: str) -> str:
            return subprocess.run(
                ["make", "-n", "-C", str(_REPO_ROOT), "oracle-gap-batch", *args],
                capture_output=True,
                text=True,
                check=True,
            ).stdout

        assert "--yes-spend" not in recipe()
        assert "--yes-spend" not in recipe("YES_SPEND=0")
        assert "--yes-spend" in recipe("YES_SPEND=1")


def _must_not_run(*_args: object, **_kwargs: object) -> None:
    raise AssertionError("the runner was started")


class TestTheNumbers:
    def test_a_correct_candidate_not_taken_is_a_gap_of_one(self) -> None:
        row = _row("w1", _SET, selected="c2")
        assert (row.pass_hit[8], row.selected_hit[8], row.gap[8]) == (True, False, 1)

    def test_taking_the_correct_candidate_is_no_gap(self) -> None:
        row = _row("w1", _SET, selected="c1")
        assert (row.pass_hit[8], row.selected_hit[8], row.gap[8]) == (True, True, 0)

    def test_counts_carry_their_n(self) -> None:
        rows = [_row("w1", _SET, selected="c1"), _row("w1", _SET, selected="c2")]
        entry = oracle_gap.at_k(rows, 8)
        assert entry["n"] == 2
        assert (entry["pass_at_k"], entry["selected_at_k"], entry["oracle_gap"]) == (2, 1, 1)
        assert entry["gap_rate"] == 0.5

    def test_the_interval_resamples_worlds_and_needs_two(self) -> None:
        one_world = [_row("w1", _SET, selected="c2"), _row("w1", _SET, selected="c1")]
        assert oracle_gap.gap_interval(one_world, 8) is None
        rows = one_world + [_row("w2", _SET, selected="c1"), _row("w3", _SET, selected="c1")]
        interval = oracle_gap.gap_interval(rows, 8)
        assert interval is not None
        low, high = interval
        assert low <= oracle_gap.at_k(rows, 8)["gap_rate"] <= high
        assert oracle_gap.gap_interval(rows, 8) == interval  # fixed seed

    def test_a_gap_is_split_by_what_the_selector_did(self) -> None:
        rows = [
            _row("w1", _SET, selected="c2"),
            _row("w2", _SET, selected=None, decision="escalate"),
            _row("w3", _SET, selected="c1"),
        ]
        assert oracle_gap.gap_by_decision(rows, 8) == {"escalate": 1, "select": 1}

    def test_the_selector_is_compared_with_the_top_candidate_both_ways(self) -> None:
        # c1 is ranked first, so the generator's own top candidate is right in both runs.
        rows = [_row("w1", _SET, selected="c1"), _row("w2", _SET, selected="c2")]
        assert oracle_gap.selector_vs_top_candidate(rows, 8) == {
            "n": 2,
            "both_right": 1,
            "only_selector_right": 0,
            "only_top_candidate_right": 1,
            "both_wrong": 0,
        }


# --------------------------------------------------------------------------
# A batch archive on disk


def _outcome(
    scenario: str,
    recording: str,
    *,
    dimensions: Sequence[DimensionResult] | None = None,
    degraded: bool = False,
    model: str = "claude-sonnet-4-6",
) -> ScenarioOutcome:
    path = f"evals/recorded_worlds/{scenario}/{scenario}.20261010T000000Z.{recording}.json"
    return ScenarioOutcome(
        scenario=scenario,
        final_state=IncidentState.ESCALATED,
        tool_calls_used=1,
        report=GradeReport(
            scenario=scenario,
            passed=True,
            dimensions=tuple(
                dimensions
                if dimensions is not None
                else (
                    DimensionResult(dimension=GradeDimension.ROOT_CAUSE, passed=True, detail="ok"),
                )
            ),
        ),
        degraded=degraded,
        provenance=RunProvenance(
            commander_revision="rev",
            platform_image_digest="sha256:0",
            agent_model=model,
            model_role=ModelRole.BENCHMARK,
            judge_model="j",
            strategy="candidate_selector",
            strategy_config=dict(_ARM_CONFIG),
            scenario=scenario,
            invocation_id="inv",
            recorded_at=_AT,
            execution_mode=ExecutionMode.RECORDED,
            budget=BudgetLedger(
                max_tool_calls=25,
                max_tokens=400_000,
                max_wall_seconds=600,
                max_usd=Decimal("1"),
                usd_used=Decimal("0.30"),
            ),
        ),
        replay={
            "recording": path,
            "world_fingerprint": f"fp-{scenario}",
            "misses": 1 if degraded else 0,
            "refusals": [],
        },
    )


def _archive(
    root: Path,
    archive: str,
    outcomes: Sequence[ScenarioOutcome],
    traces: Mapping[str, Sequence[dict[str, Any]]],
) -> None:
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
    for scenario, records in traces.items():
        (directory / "traces" / f"{scenario}.jsonl").write_text(
            "".join(json.dumps(record) + "\n" for record in records)
        )


@pytest.fixture
def batch(tmp_path: Path) -> Path:
    """One scored run, and one each of the four ways a run is left out."""
    crashed = DimensionResult(
        dimension=GradeDimension.OUTCOME, passed=False, detail="scenario crashed: boom"
    )
    not_graded = DimensionResult(
        dimension=GradeDimension.ROOT_CAUSE, passed=True, detail=not_graded_detail("x")
    )
    _archive(
        tmp_path,
        "bbbbbbbbbbbb",
        [
            _outcome(*_PAUSED),
            _outcome(*_LAG, degraded=True),
            _outcome("api_latency_redis", "7471a62bdb56", dimensions=[crashed]),
            _outcome("workflow_stuck_resolver_stall", "e7e5393d8cf4", dimensions=[not_graded]),
            _outcome("workflow_stuck_dead_lettered_root", "000000000000"),
        ],
        {_PAUSED[0]: [_record(_SET, selected="c2")]},
    )
    return tmp_path


def _batch_plan() -> SamplePlan:
    return _plan(
        _PAUSED,
        _LAG,
        ("api_latency_redis", "7471a62bdb56"),
        ("workflow_stuck_resolver_stall", "e7e5393d8cf4"),
        ("workflow_stuck_dead_lettered_root", "f5b5f50dff61"),
    )


class TestTheReport:
    def test_every_run_is_scored_or_listed_with_its_reason(self, batch: Path) -> None:
        document = oracle_gap.assemble(["bbbbbbbbbbbb"], _batch_plan(), root=batch)
        assert document["summary"]["pooled"]["runs"] == 1
        reasons = {e["scenario"]: e["reason"] for e in document["excluded"]}
        assert reasons[_LAG[0]].startswith("not comparable")
        assert reasons["api_latency_redis"].startswith("harness failure")
        assert reasons["workflow_stuck_resolver_stall"].startswith("not graded")
        assert "the plan pins f5b5f50dff61" in reasons["workflow_stuck_dead_lettered_root"]
        # Every run was billed, scored or not.
        assert document["spend"]["runs"] == 5
        assert document["spend"]["usd_total"] == "1.50"

    def test_selector_numbers_are_withheld_without_a_calibration_report(self, batch: Path) -> None:
        document = oracle_gap.assemble(["bbbbbbbbbbbb"], _batch_plan(), root=batch)
        assert document["calibration"]["report_id"] is None
        assert document["calibration"]["opens"] is False
        headline = document["summary"]["pooled"]["at_k"][-1]
        assert headline["k"] == 8
        assert headline["pass_at_k"] == 1  # generation is not gated
        assert headline["selected_at_k"] == research_report.WITHHELD
        assert headline["oracle_gap"] == research_report.WITHHELD
        assert "withheld" in oracle_gap.render_markdown(document)

    @pytest.mark.parametrize(
        ("calibrated_on", "opens"),
        [("claude-sonnet-4-6", True), ("claude-haiku-4-5", False)],
    )
    def test_a_calibration_opens_the_gate_only_for_the_model_the_selector_ran_on(
        self, batch: Path, monkeypatch: pytest.MonkeyPatch, calibrated_on: str, opens: bool
    ) -> None:
        """The harness calibrates on JUDGE_MODEL; a run's selector runs on the agent's model."""
        plan = _batch_plan()
        artifacts.write_versioned(
            "judge_calibration",
            "candidate_selector",
            content=json.dumps({"model": calibrated_on}),
            timestamp=_AT,
            invocation_id="abcabcabcabc",
            root=batch,
        )
        monkeypatch.setattr(
            research_report, "CALIBRATION_REPORTS", MappingProxyType({plan.arm: "abcabcabcabc"})
        )
        document = oracle_gap.assemble(["bbbbbbbbbbbb"], plan, root=batch)
        assert document["calibration"]["opens"] is opens
        headline = document["summary"]["pooled"]["at_k"][-1]
        if opens:
            assert (headline["n"], headline["pass_at_k"], headline["oracle_gap"]) == (1, 1, 1)
            (world,) = document["summary"]["per_world"]
            assert (world["scenario"], world["recording"]) == _PAUSED
        else:
            assert headline["oracle_gap"] == research_report.WITHHELD
            assert "measured claude-haiku-4-5" in document["calibration"]["why_not"]

    def test_two_models_in_one_report_are_refused(self, tmp_path: Path) -> None:
        _archive(
            tmp_path,
            "cccccccccccc",
            [_outcome(*_PAUSED), _outcome(*_LAG, model="claude-haiku-4-5")],
            {
                _PAUSED[0]: [_record(_SET, selected="c1")],
                _LAG[0]: [_record(("consumer_saturation",), selected="c1")],
            },
        )
        with pytest.raises(ValueError, match="two agent models"):
            oracle_gap.assemble(["cccccccccccc"], _plan(_PAUSED, _LAG), root=tmp_path)

    def test_a_scope_is_written_once_and_never_over(self, batch: Path) -> None:
        document = oracle_gap.assemble(["bbbbbbbbbbbb"], _batch_plan(), root=batch)
        json_path, md_path = oracle_gap.write(document, root=batch)
        assert json_path.parent == batch / "evals" / "reports" / "oracle-gap"
        assert artifacts.newest("oracle_gap_report", root=batch) == json_path
        assert md_path.read_text().startswith("# Oracle gap")
        with pytest.raises(FileExistsError):
            oracle_gap.write(document, root=batch)
