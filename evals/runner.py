"""Scenario runner. ``make eval`` calls the CLI at the bottom of this file."""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, SecretStr

from evals.fakes import CannedMCPClient
from evals.graders.deterministic import GradeReport, grade
from evals.scenarios.loader import load_scenarios
from evals.scenarios.schema import Scenario
from incident_commander.agent.factory import start_run
from incident_commander.agent.investigation import make_investigate
from incident_commander.agent.loop import run_to_completion
from incident_commander.agent.orchestrator import TRANSITIONS, Transition
from incident_commander.agent.state import IncidentState, RunState
from incident_commander.config import Settings
from incident_commander.persistence.memory import InMemoryCheckpointer

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCENARIOS_DIR = _REPO_ROOT / "evals" / "scenarios"
_REPORTS_DIR = _REPO_ROOT / "evals" / "reports"
_TRAJECTORIES_DIR = _REPO_ROOT / "evals" / "trajectories"
_LATEST_REPORT = _REPORTS_DIR / "latest.json"


class ScenarioOutcome(BaseModel):
    """One scenario's run + grade, persisted in the aggregate report."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scenario: str
    final_state: IncidentState
    tool_calls_used: int
    report: GradeReport


class RunReport(BaseModel):
    """Aggregate output written to ``evals/reports/latest.json``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    generated_at: datetime
    total: int
    passed: int
    failed: int
    outcomes: tuple[ScenarioOutcome, ...]


class Trajectory(BaseModel):
    """Per-run checkpoint log, written to ``evals/trajectories/<scenario>.json``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scenario: str
    incident_id: str
    checkpoints: tuple[RunState, ...]


@dataclass(frozen=True)
class ScenarioResult:
    """What ``run_scenario`` returns — outcome is aggregated; trajectory is per-run."""

    outcome: ScenarioOutcome
    trajectory: Trajectory


def run_scenario(
    scenario: Scenario,
    settings: Settings,
    clock: Callable[[], datetime] | None = None,
) -> ScenarioResult:
    """Drive one scenario end-to-end and grade the result."""
    tick = clock or (lambda: datetime.now(UTC))
    now = tick()

    client = CannedMCPClient(scenario.canned_tool_responses)
    transitions: dict[IncidentState, Transition] = dict(TRANSITIONS)
    transitions[IncidentState.INVESTIGATING] = make_investigate(client)

    checkpointer = InMemoryCheckpointer()
    run = start_run(scenario.alert.model_dump(), settings, now)
    final = run_to_completion(
        run,
        clock=tick,
        transitions=transitions,
        checkpointer=checkpointer,
    )
    report = grade(final, scenario.expectation)
    outcome = ScenarioOutcome(
        scenario=scenario.name,
        final_state=final.state,
        tool_calls_used=final.budget.tool_calls_used,
        report=report,
    )
    trajectory = Trajectory(
        scenario=scenario.name,
        incident_id=str(final.incident_id),
        checkpoints=tuple(checkpointer.history(final.incident_id)),
    )
    return ScenarioResult(outcome=outcome, trajectory=trajectory)


def run_all(
    scenarios: Iterable[Scenario],
    settings: Settings,
    clock: Callable[[], datetime] | None = None,
) -> tuple[RunReport, tuple[Trajectory, ...]]:
    results = tuple(run_scenario(s, settings, clock) for s in scenarios)
    outcomes = tuple(r.outcome for r in results)
    trajectories = tuple(r.trajectory for r in results)
    passed = sum(1 for o in outcomes if o.report.passed)
    failed = len(outcomes) - passed
    report = RunReport(
        generated_at=datetime.now(UTC),
        total=len(outcomes),
        passed=passed,
        failed=failed,
        outcomes=outcomes,
    )
    return report, trajectories


def write_report(report: RunReport, path: Path = _LATEST_REPORT) -> None:
    """Serialize ``report`` as JSON. Creates parent directories if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report.model_dump_json(indent=2))


def write_trajectories(
    trajectories: Iterable[Trajectory],
    directory: Path = _TRAJECTORIES_DIR,
) -> None:
    """Serialize each trajectory to ``<directory>/<scenario>.json``."""
    directory.mkdir(parents=True, exist_ok=True)
    for trajectory in trajectories:
        (directory / f"{trajectory.scenario}.json").write_text(trajectory.model_dump_json(indent=2))


def _eval_defaults() -> Settings:
    """Placeholder Settings for offline eval runs (budget is what actually matters)."""
    return Settings.model_validate(
        {
            "anthropic_api_key": SecretStr("eval"),
            "judge_model": "eval-judge",
            "platform_mcp_url": "https://eval.local",
            "platform_rest_url": "https://eval.local",
            "platform_token": SecretStr("eval"),
            "platform_webhook_secret": SecretStr("eval"),
            "database_url": "postgresql://eval:eval@localhost:5432/eval",
        }
    )


def _print_summary(report: RunReport) -> None:
    print(f"scenarios: {report.total}, passed: {report.passed}, failed: {report.failed}")
    for outcome in report.outcomes:
        mark = "PASS" if outcome.report.passed else "FAIL"
        print(f"  {mark} {outcome.scenario}")
        if not outcome.report.passed:
            for dim in outcome.report.dimensions:
                if not dim.passed:
                    print(f"    - {dim.dimension.value}: {dim.detail}")


def main() -> int:
    scenarios = load_scenarios(_SCENARIOS_DIR)
    settings = _eval_defaults()
    report, trajectories = run_all(scenarios, settings)
    write_report(report)
    write_trajectories(trajectories)
    _print_summary(report)
    return 0 if report.failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
