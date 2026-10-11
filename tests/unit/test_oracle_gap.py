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

    def test_selector_numbers_are_withheld_without_a_calibration_report(
        self, batch: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The register holds the arm's real calibration (36e12b915900) since 2026-10-11;
        # this test is about the EMPTY register, so empty it here.
        monkeypatch.setattr(research_report, "CALIBRATION_REPORTS", {})
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


# --------------------------------------------------------------------------
# WO-R3-375 (owner O-55): the pilot plan, REPEATS=N in one invocation, and the calibration
# report id beside every selector number.

_PILOT_WORLDS: Final[tuple[str, ...]] = (
    "api_latency_db_query",
    "dlq_mislabeled_replay_safe",
    "jobs_not_progressing_outbox_stall",
    "workflow_stuck_resolver_stall",
)
_CAL: Final[str] = "abcabcabcabc"


def _pilot(*worlds: tuple[str, str]) -> SamplePlan:
    """The committed pilot plan, or a copy of it pinned to committed recordings."""
    pilot = oracle_gap.load_plan(oracle_gap.PILOT_PLAN_PATH)
    if not worlds:
        return pilot
    return pilot.model_copy(
        update={"worlds": tuple(SampleWorld(scenario=s, recording=r) for s, r in worlds)}
    )


def _stamped(
    outcome: ScenarioOutcome,
    *,
    study: str | None = None,
    repeat: int = 0,
    repeat_of: str | None = None,
) -> ScenarioOutcome:
    """An outcome whose provenance names its study and its pass, as the runner stamps them."""
    assert outcome.provenance is not None
    provenance = outcome.provenance.model_copy(
        update={"study": study, "repeat": repeat, "repeat_of": repeat_of}
    )
    return outcome.model_copy(update={"provenance": provenance})


def _calibrate(
    root: Path, monkeypatch: pytest.MonkeyPatch, model: str = "claude-sonnet-4-6"
) -> None:
    """Register a calibration report for the arm, made on ``model``."""
    artifacts.write_versioned(
        "judge_calibration",
        "candidate_selector",
        content=json.dumps({"model": model}),
        timestamp=_AT,
        invocation_id=_CAL,
        root=root,
    )
    monkeypatch.setattr(
        research_report,
        "CALIBRATION_REPORTS",
        MappingProxyType({oracle_gap.load_plan().arm: _CAL}),
    )


def _passes(root: Path, plan: SamplePlan, picks: Sequence[str]) -> list[str]:
    """One archive per pass of a repeated invocation over ``_PAUSED``, as the runner writes it.

    ``picks`` is the candidate the selector took in each pass (c1 is right, c2 is not).
    """
    archives = [(f"{index:x}" * 12)[:12] for index in range(10, 10 + len(picks))]
    for repeat, (archive, pick) in enumerate(zip(archives, picks, strict=True)):
        outcome = _stamped(
            _outcome(*_PAUSED), study=plan.study, repeat=repeat, repeat_of=archives[0]
        )
        _archive(root, archive, [outcome], {_PAUSED[0]: [_record(_SET, selected=pick)]})
    return archives


class TestThePilotPlan:
    def test_the_pilot_is_four_worlds_on_the_main_arm_and_never_pooled(self) -> None:
        pilot, main = _pilot(), oracle_gap.load_plan()
        assert tuple(world.scenario for world in pilot.worlds) == _PILOT_WORLDS
        assert set(_PILOT_WORLDS) <= {world.scenario for world in main.worlds}
        assert (pilot.arm, pilot.model_role) == (main.arm, main.model_role)
        assert (pilot.token_multiplier, pilot.usd_multiplier) == (
            main.token_multiplier,
            main.usd_multiplier,
        )
        assert pilot.pooled_with_main is False
        assert main.pooled_with_main is True
        assert pilot.study != main.study

    def test_the_pilot_placeholder_ids_are_refused(self) -> None:
        pilot = _pilot()
        refusals = oracle_gap.plan_refusals(pilot, pilot.worlds)
        assert len(refusals) == len(_PILOT_WORLDS)
        assert all("is not a recording id" in line for line in refusals)
        assert all("WO-R3-368" in line for line in refusals)

    def test_the_pilot_never_reaches_the_runner_with_placeholders(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(subprocess, "run", _must_not_run)
        assert oracle_gap.run_batch(_pilot(), yes_spend=True, repeats=3) == 2


class TestRepeatsAreOneInvocation:
    def test_the_runner_is_asked_for_n_passes_and_the_plans_study(self) -> None:
        plan = _plan(_PAUSED, _LAG)
        argv, _env = oracle_gap.batch_invocation(plan, plan.worlds, repeats=3)
        assert argv[argv.index("--repeats") + 1] == "3"
        assert argv[argv.index("--study") + 1] == plan.study
        # Each world is still pinned once: the passes repeat the batch, not the pins.
        assert [argv[i + 1] for i, arg in enumerate(argv) if arg == "--world"] == [
            _PAUSED[1],
            _LAG[1],
        ]

    def test_one_repeat_asks_for_no_repeats_flag(self) -> None:
        plan = _plan(_PAUSED, _LAG)
        argv, _env = oracle_gap.batch_invocation(plan, plan.worlds)
        assert "--repeats" not in argv

    def test_the_free_check_prints_the_repeat_count_and_the_total(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(subprocess, "run", _must_not_run)
        assert oracle_gap.run_batch(_plan(_PAUSED, _LAG), yes_spend=False, repeats=3) == 0
        out = capsys.readouterr().out
        assert "2 world(s) x 3 repeat(s) = 6 recorded runs in ONE runner invocation" in out
        assert "--repeats 3" in out
        assert "Nothing was spent" in out

    def test_zero_repeats_are_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(subprocess, "run", _must_not_run)
        assert oracle_gap.run_batch(_plan(_PAUSED), yes_spend=True, repeats=0) == 2

    @pytest.mark.skipif(shutil.which("make") is None, reason="make not available")
    def test_the_make_target_passes_plan_and_repeats_through(self) -> None:
        def recipe(*args: str) -> str:
            return subprocess.run(
                ["make", "-n", "-C", str(_REPO_ROOT), "oracle-gap-batch", *args],
                capture_output=True,
                text=True,
                check=True,
            ).stdout

        assert "--repeats" not in recipe()
        assert "--repeats 3" in recipe("REPEATS=3")
        pilot = "evals/samples/oracle_gap_pilot.json"
        assert f"--plan {pilot}" in recipe(f"PLAN={pilot}")

    def test_a_paid_batch_reports_every_pass_with_its_repeat(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The runner is faked; the driver must find all three passes and report them as one."""
        plan = _plan(_PAUSED)
        written: list[str] = []

        def _runner(command: Sequence[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
            assert command[command.index("--repeats") + 1] == "3"
            written.extend(_passes(tmp_path, plan, ["c1", "c2", "c1"]))
            for second, archive in enumerate(written):
                report = research_report.read_source(tmp_path, archive).report
                artifacts.write_versioned(
                    "report",
                    content=report.model_dump_json(),
                    timestamp=_AT.replace(second=second),
                    invocation_id=archive,
                    root=tmp_path,
                )
            return subprocess.CompletedProcess(command, returncode=0)

        monkeypatch.setattr(subprocess, "run", _runner)
        assert oracle_gap.run_batch(plan, yes_spend=True, repeats=3, root=tmp_path) == 0
        document = json.loads(artifacts.newest("oracle_gap_report", root=tmp_path).read_text())
        assert [entry["archive"] for entry in document["scope"]["archives"]] == written
        assert [entry["repeat"] for entry in document["scope"]["archives"]] == [0, 1, 2]
        (world,) = document["summary"]["per_world"]
        assert world["runs"] == 3
        assert [run["repeat"] for run in world["per_run"]] == [0, 1, 2]
        assert [run["archive"] for run in world["per_run"]] == written

    def test_a_missing_pass_is_named_and_nothing_is_assembled(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        plan = _plan(_PAUSED)

        def _runner(command: Sequence[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
            for second, archive in enumerate(_passes(tmp_path, plan, ["c1", "c2"])):
                report = research_report.read_source(tmp_path, archive).report
                artifacts.write_versioned(
                    "report",
                    content=report.model_dump_json(),
                    timestamp=_AT.replace(second=second),
                    invocation_id=archive,
                    root=tmp_path,
                )
            return subprocess.CompletedProcess(command, returncode=0)

        monkeypatch.setattr(subprocess, "run", _runner)
        assert oracle_gap.run_batch(plan, yes_spend=True, repeats=3, root=tmp_path) == 2
        assert "2 of 3 passes" in capsys.readouterr().out
        assert artifacts.newest_or_none("oracle_gap_report", root=tmp_path) is None


class TestAWorldsRepeatsAreReadTogether:
    def test_each_world_lists_its_runs_by_repeat_and_whether_they_agree(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _calibrate(tmp_path, monkeypatch)
        plan = _plan(_PAUSED)
        archives = _passes(tmp_path, plan, ["c1", "c2", "c1"])
        document = oracle_gap.assemble(archives, plan, root=tmp_path)
        (world,) = document["summary"]["per_world"]
        assert [(run["repeat"], run["selected_hit"]) for run in world["per_run"]] == [
            (0, True),
            (1, False),
            (2, True),
        ]
        agreement = world["agreement"]
        assert agreement["pass_at_k_agree"] is True
        assert agreement["selected_at_k_agree"] is False
        assert agreement["calibration_report_id"] == _CAL


class TestThePilotIsNeverPooled:
    def _archive_with_one_pilot_run(self, root: Path) -> None:
        _archive(
            root,
            "dddddddddddd",
            [_stamped(_outcome(*_PAUSED), study=_pilot().study), _outcome(*_LAG)],
            {
                _PAUSED[0]: [_record(_SET, selected="c1")],
                _LAG[0]: [_record(("consumer_saturation",), selected="c1")],
            },
        )

    def test_a_pilot_run_is_left_out_of_the_main_sample(self, tmp_path: Path) -> None:
        """Same arm, same pinned recording — and still not pooled: it was made for the pilot."""
        self._archive_with_one_pilot_run(tmp_path)
        document = oracle_gap.assemble(["dddddddddddd"], _plan(_PAUSED, _LAG), root=tmp_path)
        reasons = {e["scenario"]: e["reason"] for e in document["excluded"]}
        assert _PAUSED[0] in reasons
        assert "oracle_gap_pilot" in reasons[_PAUSED[0]]
        assert "never pooled" in reasons[_PAUSED[0]]
        assert document["summary"]["pooled"]["runs"] == 1  # the unstamped run only

    def test_the_pilot_reports_only_its_own_runs_on_their_own(self, tmp_path: Path) -> None:
        self._archive_with_one_pilot_run(tmp_path)
        pilot = _pilot(_PAUSED, _LAG)
        document = oracle_gap.assemble(["dddddddddddd"], pilot, root=tmp_path)
        assert document["pooled_with_main"] is False
        assert document["summary"]["pooled"]["runs"] == 1  # the pilot-stamped run only
        reasons = {e["scenario"]: e["reason"] for e in document["excluded"]}
        assert "not made for oracle_gap_pilot" in reasons[_LAG[0]]
        assert "reported on its own" in oracle_gap.render_markdown(document)


#: Markdown cells that carry a selector number, by the label the shared renderer gives them.
_SELECTOR_LABELS: Final[tuple[str, ...]] = (
    "selected@",
    "oracle gap@",
    "gap interval@",
    "gap by decision@",
    "selector vs top candidate@",
    "selector decision@",
)


def _selector_cells(markdown: str) -> list[str]:
    """Every cell (or line) that states a selector number; a column header states none."""
    cells = [cell.strip() for line in markdown.splitlines() for cell in line.split("|")]
    return [cell for cell in cells if cell.startswith(_SELECTOR_LABELS) and not cell.endswith("@k")]


class TestTheCalibrationIdIsBesideEverySelectorNumber:
    def test_the_shared_renderer(self) -> None:
        assert (
            research_report.selector_number("selected@8", "9/12", report_ids=("4fc2722d286e",))
            == "selected@8 = 9/12 [cal 4fc2722d286e]"
        )
        assert (
            research_report.selector_number("selected@8", "9/12", report_ids=(), why_withheld="x")
            == "selected@8 withheld [x]"
        )

    def test_an_open_gate_puts_the_id_in_every_row(
        self, batch: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _calibrate(batch, monkeypatch)
        document = oracle_gap.assemble(["bbbbbbbbbbbb"], _batch_plan(), root=batch)
        pooled = document["summary"]["pooled"]
        (world,) = document["summary"]["per_world"]
        rows = [
            *pooled["at_k"],
            *world["at_k"],
            *world["per_run"],
            world["agreement"],
            pooled["gap_by_decision"],
            pooled["selector_vs_top_candidate"],
        ]
        assert all(row["calibration_report_id"] == _CAL for row in rows)
        markdown = oracle_gap.render_markdown(document)
        cells = _selector_cells(markdown)
        assert {cell.split(" ")[0].split("@")[0] for cell in cells} >= {"selected", "oracle"}
        assert cells and all(cell.endswith(f"[cal {_CAL}]") for cell in cells), cells
        assert f"selected@8 = 0/1 (0.0) [cal {_CAL}]" in cells

    def test_withheld_rows_say_why_in_the_same_row(self, batch: Path) -> None:
        document = oracle_gap.assemble(["bbbbbbbbbbbb"], _batch_plan(), root=batch)
        why = document["calibration"]["why_not"]
        pooled = document["summary"]["pooled"]
        (world,) = document["summary"]["per_world"]
        rows = [
            *pooled["at_k"],
            *world["at_k"],
            *world["per_run"],
            world["agreement"],
            pooled["gap_by_decision"],
            pooled["selector_vs_top_candidate"],
        ]
        assert all(row["calibration_report_id"] is None for row in rows)
        assert all(row["withheld_because"] == why for row in rows)
        cells = _selector_cells(oracle_gap.render_markdown(document))
        assert cells and all(cell.endswith(f"withheld [{why}]") for cell in cells), cells

    def test_a_report_on_another_model_is_named_beside_each_number(
        self, batch: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _calibrate(batch, monkeypatch, model="claude-haiku-4-5")
        document = oracle_gap.assemble(["bbbbbbbbbbbb"], _batch_plan(), root=batch)
        cells = _selector_cells(oracle_gap.render_markdown(document))
        assert cells and all("measured claude-haiku-4-5" in cell for cell in cells)

    def test_the_research_reports_section_renders_through_the_same_function(self) -> None:
        rows: list[dict[str, Any]] = [
            {
                "world": "w1",
                "group": {"family": "workflow_stuck", "difficulty": "single"},
                "calibration_report_id": _CAL,
                "oracle_gap_at_k": [
                    {"k": 8, "pass_hit": True, "selected_hit": False, "gap": 1},
                ],
            }
        ]
        value: dict[str, Any] = {
            "calibration_reports": {"arm": _CAL},
            "by_world": research_report._gap_by(rows, lambda row: row["world"]),
            "by_family": research_report._gap_by(rows, lambda row: row["group"]["family"]),
            "by_difficulty": research_report._gap_by(rows, lambda row: row["group"]["difficulty"]),
        }
        assert value["by_world"][0]["calibration_report_ids"] == [_CAL]
        cells = _selector_cells("\n".join(research_report._render_oracle_gap(value)))
        assert cells and all(cell.endswith(f"[cal {_CAL}]") for cell in cells), cells
        assert f"selected@8 = 0.000 [cal {_CAL}]" in cells
