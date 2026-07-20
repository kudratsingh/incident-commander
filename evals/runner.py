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
from evals.graders.llm_judge import JudgeScore, judge_briefing
from evals.scenarios.loader import load_scenarios
from evals.scenarios.schema import Scenario
from incident_commander.agent.briefing import EscalationBriefing, render_briefing
from incident_commander.agent.briefing_enrichment import enrich_briefing
from incident_commander.agent.factory import start_run
from incident_commander.agent.investigation import make_llm_investigate
from incident_commander.agent.loop import run_to_completion
from incident_commander.agent.orchestrator import TRANSITIONS, Transition
from incident_commander.agent.state import IncidentState, RunState
from incident_commander.config import Settings
from incident_commander.llm.fakes import CannedLLMClient
from incident_commander.persistence.memory import InMemoryCheckpointer
from incident_commander.tools.mcp_client import MCPClient, MCPClientProtocol, make_client

_EVAL_PLACEHOLDER_HOST = "eval.local"

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCENARIOS_DIR = _REPO_ROOT / "evals" / "scenarios"
_REPORTS_DIR = _REPO_ROOT / "evals" / "reports"
_TRAJECTORIES_DIR = _REPO_ROOT / "evals" / "trajectories"
_BRIEFINGS_DIR = _REPO_ROOT / "evals" / "briefings"
_LATEST_REPORT = _REPORTS_DIR / "latest.json"


class ScenarioOutcome(BaseModel):
    """One scenario's run + grade, persisted in the aggregate report."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scenario: str
    final_state: IncidentState
    tool_calls_used: int
    report: GradeReport
    judge_score: JudgeScore | None = None


class RunReport(BaseModel):
    """Aggregate output written to ``evals/reports/latest.json``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    generated_at: datetime
    total: int
    passed: int
    failed: int
    judged_count: int = 0
    judge_useful_count: int = 0
    judge_mean_overall: float | None = None
    outcomes: tuple[ScenarioOutcome, ...]


class Trajectory(BaseModel):
    """Per-run checkpoint log, written to ``evals/trajectories/<scenario>.json``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scenario: str
    incident_id: str
    checkpoints: tuple[RunState, ...]


@dataclass(frozen=True)
class ScenarioResult:
    """What ``run_scenario`` returns — outcome is aggregated; trajectory + briefing are per-run."""

    outcome: ScenarioOutcome
    trajectory: Trajectory
    briefing: EscalationBriefing


class LiveMCPUnavailable(RuntimeError):
    """Scenario needs live MCP but PLATFORM_MCP_URL is still the eval placeholder."""


def _is_offline_placeholder(url: str) -> bool:
    return _EVAL_PLACEHOLDER_HOST in url


def run_scenario(
    scenario: Scenario,
    settings: Settings,
    clock: Callable[[], datetime] | None = None,
) -> ScenarioResult:
    """Drive one scenario end-to-end and grade the result.

    Uses ``CannedMCPClient`` for tool calls and ``CannedLLMClient`` for both
    the investigation planner and the briefing writer, each with its own
    per-scenario response queue keyed under ``canned_llm_responses``.
    """
    tick = clock or (lambda: datetime.now(UTC))
    now = tick()

    mcp_client: MCPClientProtocol
    live_client: MCPClient | None = None
    if scenario.use_live_mcp:
        if _is_offline_placeholder(str(settings.platform_mcp_url)):
            raise LiveMCPUnavailable(
                f"scenario {scenario.name} requires live MCP but "
                f"PLATFORM_MCP_URL is the offline placeholder"
            )
        live_client = make_client(settings)
        mcp_client = live_client
    else:
        mcp_client = CannedMCPClient(scenario.canned_tool_responses)

    investigation_llm = CannedLLMClient(
        scenario.canned_llm_responses.get("investigation_planner", [])
    )
    briefing_llm = CannedLLMClient(scenario.canned_llm_responses.get("briefing_writer", []))
    judge_llm = CannedLLMClient(scenario.canned_llm_responses.get("briefing_judge", []))

    transitions: dict[IncidentState, Transition] = dict(TRANSITIONS)
    transitions[IncidentState.INVESTIGATING] = make_llm_investigate(
        mcp_client, investigation_llm, model=settings.agent_model
    )

    try:
        checkpointer = InMemoryCheckpointer()
        run = start_run(scenario.alert.model_dump(), settings, now)
        final = run_to_completion(
            run,
            clock=tick,
            transitions=transitions,
            checkpointer=checkpointer,
        )
        report = grade(final, scenario.expectation)
        trajectory = Trajectory(
            scenario=scenario.name,
            incident_id=str(final.incident_id),
            checkpoints=tuple(checkpointer.history(final.incident_id)),
        )
        briefing = render_briefing(final)
        if briefing_llm.has_remaining:
            briefing = enrich_briefing(briefing, briefing_llm, model=settings.agent_model)
        judge_score: JudgeScore | None = None
        if judge_llm.has_remaining:
            judge_score = judge_briefing(briefing, judge_llm, model=settings.judge_model)
    finally:
        if live_client is not None:
            live_client.close()

    outcome = ScenarioOutcome(
        scenario=scenario.name,
        final_state=final.state,
        tool_calls_used=final.budget.tool_calls_used,
        report=report,
        judge_score=judge_score,
    )
    return ScenarioResult(outcome=outcome, trajectory=trajectory, briefing=briefing)


def run_all(
    scenarios: Iterable[Scenario],
    settings: Settings,
    clock: Callable[[], datetime] | None = None,
) -> tuple[RunReport, tuple[Trajectory, ...], tuple[EscalationBriefing, ...]]:
    offline = _is_offline_placeholder(str(settings.platform_mcp_url))
    results: list[ScenarioResult] = []
    for scenario in scenarios:
        if scenario.use_live_mcp and offline:
            # Skip cleanly; the summary line notes the count.
            continue
        results.append(run_scenario(scenario, settings, clock))
    outcomes = tuple(r.outcome for r in results)
    trajectories = tuple(r.trajectory for r in results)
    briefings = tuple(r.briefing for r in results)
    passed = sum(1 for o in outcomes if o.report.passed)
    failed = len(outcomes) - passed
    judged = tuple(o for o in outcomes if o.judge_score is not None)
    judged_count = len(judged)
    judge_useful_count = sum(
        1 for o in judged if o.judge_score is not None and o.judge_score.is_useful
    )
    judge_mean_overall: float | None
    if judged_count == 0:
        judge_mean_overall = None
    else:
        judge_mean_overall = (
            sum(o.judge_score.overall for o in judged if o.judge_score is not None) / judged_count
        )
    report = RunReport(
        generated_at=datetime.now(UTC),
        total=len(outcomes),
        passed=passed,
        failed=failed,
        judged_count=judged_count,
        judge_useful_count=judge_useful_count,
        judge_mean_overall=judge_mean_overall,
        outcomes=outcomes,
    )
    return report, trajectories, briefings


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


def write_briefings(
    briefings: Iterable[EscalationBriefing],
    scenario_names: Iterable[str],
    directory: Path = _BRIEFINGS_DIR,
) -> None:
    """Serialize each briefing to ``<directory>/<scenario>.json``."""
    directory.mkdir(parents=True, exist_ok=True)
    for briefing, name in zip(briefings, scenario_names, strict=True):
        (directory / f"{name}.json").write_text(briefing.model_dump_json(indent=2))


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


def _print_summary(report: RunReport, skipped_live: int = 0) -> None:
    print(f"scenarios: {report.total}, passed: {report.passed}, failed: {report.failed}")
    if skipped_live > 0:
        print(f"skipped: {skipped_live} live scenarios (PLATFORM_MCP_URL is offline placeholder)")
    if report.judged_count > 0 and report.judge_mean_overall is not None:
        print(
            f"judge: {report.judge_useful_count}/{report.judged_count} useful, "
            f"mean overall {report.judge_mean_overall:.2f}"
        )
    for outcome in report.outcomes:
        mark = "PASS" if outcome.report.passed else "FAIL"
        judge_hint = ""
        if outcome.judge_score is not None:
            judge_hint = f"  (judge: {outcome.judge_score.overall:.2f})"
        print(f"  {mark} {outcome.scenario}{judge_hint}")
        if not outcome.report.passed:
            for dim in outcome.report.dimensions:
                if not dim.passed:
                    print(f"    - {dim.dimension.value}: {dim.detail}")


def _settings_for_mode(live: bool) -> Settings:
    """Live mode reads real env; offline uses the eval placeholder."""
    if live:
        return Settings()  # type: ignore[call-arg]
    return _eval_defaults()


def main() -> int:
    live = "--live" in sys.argv[1:]
    settings = _settings_for_mode(live)
    scenarios = load_scenarios(_SCENARIOS_DIR)
    offline = _is_offline_placeholder(str(settings.platform_mcp_url))
    skipped_live = sum(1 for s in scenarios if s.use_live_mcp and offline)
    # run_all skips live scenarios internally when offline.
    report, trajectories, briefings = run_all(scenarios, settings)
    write_report(report)
    write_trajectories(trajectories)
    # Write briefings only for scenarios that actually ran.
    ran_names = [o.scenario for o in report.outcomes]
    write_briefings(briefings, ran_names)
    _print_summary(report, skipped_live=skipped_live)
    return 0 if report.failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
