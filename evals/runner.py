"""Scenario runner. ``make eval`` calls the CLI at the bottom of this file."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, SecretStr

from evals.fakes import CannedMCPClient
from evals.graders.deterministic import (
    DimensionResult,
    GradeDimension,
    GradeReport,
    grade,
)
from evals.graders.llm_judge import JudgeScore, judge_briefing
from evals.scenarios.loader import load_scenarios
from evals.scenarios.schema import Scenario
from evals.tracing import JsonlTracer, tracer_for
from incident_commander.agent.briefing import EscalationBriefing, render_briefing
from incident_commander.agent.briefing_enrichment import enrich_briefing
from incident_commander.agent.factory import start_run
from incident_commander.agent.investigation import make_llm_investigate
from incident_commander.agent.loop import run_to_completion
from incident_commander.agent.orchestrator import TRANSITIONS, Transition
from incident_commander.agent.remediation import (
    make_llm_plan,
    make_llm_verify,
    make_remediate,
)
from incident_commander.agent.state import IncidentState, RunState
from incident_commander.config import Settings
from incident_commander.llm.client import LLMClient, LLMClientProtocol
from incident_commander.llm.fakes import CannedLLMClient
from incident_commander.persistence.memory import InMemoryCheckpointer
from incident_commander.tools.mcp_client import MCPClient, MCPClientProtocol, make_client

_EVAL_PLACEHOLDER_HOST = "eval.local"
_EVAL_PLACEHOLDER_API_KEY = "eval"

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCENARIOS_DIR = _REPO_ROOT / "evals" / "scenarios"
_REPORTS_DIR = _REPO_ROOT / "evals" / "reports"
_TRAJECTORIES_DIR = _REPO_ROOT / "evals" / "trajectories"
_BRIEFINGS_DIR = _REPO_ROOT / "evals" / "briefings"
_LATEST_REPORT = _REPORTS_DIR / "latest.json"
_TRACE_DIR_ENV = "EVAL_TRACE_DIR"


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


def _is_offline_placeholder(url: str) -> bool:
    return _EVAL_PLACEHOLDER_HOST in url


def _is_offline_api_key(key: str) -> bool:
    return key in {_EVAL_PLACEHOLDER_API_KEY, "placeholder", ""}


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

    # use_live_* means "prefer live if env is real, else fall back to canned."
    # Nothing skips just because env is placeholder — canned data is the
    # deterministic offline fallback for `make eval` / CI.
    live_mcp_available = scenario.use_live_mcp and not _is_offline_placeholder(
        str(settings.platform_mcp_url)
    )
    live_llm_available = scenario.use_live_llm and not _is_offline_api_key(
        settings.anthropic_api_key.get_secret_value()
    )

    # Tracing (opt-in): when EVAL_TRACE_DIR is set, capture every LLM +
    # MCP call for this scenario into a JSONL file. Only wires into the
    # live clients — canned clients are already deterministic.
    tracer: JsonlTracer | None = None
    trace_dir_env = os.environ.get(_TRACE_DIR_ENV)
    if trace_dir_env:
        tracer = tracer_for(scenario.name, Path(trace_dir_env))
        tracer.write(
            {
                "kind": "scenario_start",
                "scenario": scenario.name,
                "live_mcp": live_mcp_available,
                "live_llm": live_llm_available,
                "model": settings.agent_model,
                "judge_model": settings.judge_model,
            }
        )

    mcp_client: MCPClientProtocol
    live_mcp_client: MCPClient | None = None
    if live_mcp_available:
        live_mcp_client = make_client(
            settings,
            tracer=tracer.mcp_hook() if tracer else None,
        )
        mcp_client = live_mcp_client
    else:
        mcp_client = CannedMCPClient(scenario.canned_tool_responses)

    investigation_llm: LLMClientProtocol
    remediation_planner_llm: LLMClientProtocol
    verification_judge_llm: LLMClientProtocol
    briefing_llm: LLMClientProtocol
    judge_llm: LLMClientProtocol
    if live_llm_available:
        api_key = settings.anthropic_api_key.get_secret_value()
        # One underlying HTTP client per role so each gets its own tracer
        # hook label — that's what makes the JSONL readable per role.
        investigation_llm = LLMClient(
            api_key=api_key,
            tracer=tracer.llm_hook("investigation_planner") if tracer else None,
        )
        remediation_planner_llm = LLMClient(
            api_key=api_key,
            tracer=tracer.llm_hook("remediation_planner") if tracer else None,
        )
        verification_judge_llm = LLMClient(
            api_key=api_key,
            tracer=tracer.llm_hook("verification_judge") if tracer else None,
        )
        briefing_llm = LLMClient(
            api_key=api_key,
            tracer=tracer.llm_hook("briefing_writer") if tracer else None,
        )
        judge_llm = LLMClient(
            api_key=api_key,
            tracer=tracer.llm_hook("briefing_judge") if tracer else None,
        )
    else:
        investigation_llm = CannedLLMClient(
            scenario.canned_llm_responses.get("investigation_planner", [])
        )
        remediation_planner_llm = CannedLLMClient(
            scenario.canned_llm_responses.get("remediation_planner", [])
        )
        verification_judge_llm = CannedLLMClient(
            scenario.canned_llm_responses.get("verification_judge", [])
        )
        briefing_llm = CannedLLMClient(scenario.canned_llm_responses.get("briefing_writer", []))
        judge_llm = CannedLLMClient(scenario.canned_llm_responses.get("briefing_judge", []))

    transitions: dict[IncidentState, Transition] = dict(TRANSITIONS)
    transitions[IncidentState.INVESTIGATING] = make_llm_investigate(
        mcp_client, investigation_llm, model=settings.agent_model
    )
    # Phase 6 remediation loop: PLANNING → REMEDIATING → VERIFYING. Each
    # role gets its own LLM client so canned queues stay role-partitioned
    # and live tracer records label each call by role.
    transitions[IncidentState.PLANNING] = make_llm_plan(
        remediation_planner_llm, model=settings.agent_model
    )
    transitions[IncidentState.REMEDIATING] = make_remediate(mcp_client)
    transitions[IncidentState.VERIFYING] = make_llm_verify(
        mcp_client, verification_judge_llm, model=settings.agent_model
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
        if scenario.use_live_llm or (
            isinstance(briefing_llm, CannedLLMClient) and briefing_llm.has_remaining
        ):
            briefing = enrich_briefing(briefing, briefing_llm, model=settings.agent_model)
        judge_score: JudgeScore | None = None
        if scenario.use_live_llm or (
            isinstance(judge_llm, CannedLLMClient) and judge_llm.has_remaining
        ):
            judge_score = judge_briefing(briefing, judge_llm, model=settings.judge_model)
    except Exception as exc:
        if tracer is not None:
            tracer.write(
                {
                    "kind": "scenario_end",
                    "scenario": scenario.name,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
        raise
    finally:
        if live_mcp_client is not None:
            live_mcp_client.close()

    outcome = ScenarioOutcome(
        scenario=scenario.name,
        final_state=final.state,
        tool_calls_used=final.budget.tool_calls_used,
        report=report,
        judge_score=judge_score,
    )
    if tracer is not None:
        tracer.write(
            {
                "kind": "scenario_end",
                "scenario": scenario.name,
                "final_state": final.state.value,
                "tool_calls_used": final.budget.tool_calls_used,
                "passed": report.passed,
            }
        )
    return ScenarioResult(outcome=outcome, trajectory=trajectory, briefing=briefing)


def _crashed_result(scenario: Scenario, exc: BaseException) -> ScenarioResult:
    """Synthesize a failed ScenarioResult when run_scenario raises.

    One crashing scenario should not take out the whole suite — live-eval
    runs across dozens of scenarios and a single flaky platform call
    (network blip, unseeded fixture) would otherwise wipe every result
    that hadn't run yet. The synthesized report carries the error string
    so it's visible in the summary + written to disk.
    """
    error_detail = f"{type(exc).__name__}: {exc}"
    report = GradeReport(
        scenario=scenario.name,
        passed=False,
        dimensions=(
            DimensionResult(
                dimension=GradeDimension.OUTCOME,
                passed=False,
                detail=f"scenario crashed: {error_detail}",
            ),
        ),
    )
    outcome = ScenarioOutcome(
        scenario=scenario.name,
        final_state=IncidentState.TRIAGE,
        tool_calls_used=0,
        report=report,
        judge_score=None,
    )
    trajectory = Trajectory(
        scenario=scenario.name,
        incident_id="00000000-0000-0000-0000-000000000000",
        checkpoints=(),
    )
    briefing = EscalationBriefing(
        incident_id="00000000-0000-0000-0000-000000000000",
        final_state=IncidentState.TRIAGE,
        alert_summary=f"crashed: {error_detail}",
    )
    return ScenarioResult(outcome=outcome, trajectory=trajectory, briefing=briefing)


def run_all(
    scenarios: Iterable[Scenario],
    settings: Settings,
    clock: Callable[[], datetime] | None = None,
) -> tuple[RunReport, tuple[Trajectory, ...], tuple[EscalationBriefing, ...]]:
    # run_scenario falls back to canned when env is placeholder; nothing
    # is skipped here. Per-scenario crashes are captured as failed outcomes
    # so the batch keeps running — see _crashed_result.
    results: list[ScenarioResult] = []
    for scenario in scenarios:
        try:
            results.append(run_scenario(scenario, settings, clock))
        except Exception as exc:  # noqa: BLE001 — deliberate: don't abort suite
            print(f"  CRASH {scenario.name}: {type(exc).__name__}: {exc}")
            results.append(_crashed_result(scenario, exc))
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


def _print_summary(report: RunReport, degraded_to_canned: int = 0) -> None:
    print(f"scenarios: {report.total}, passed: {report.passed}, failed: {report.failed}")
    if degraded_to_canned > 0:
        print(
            f"degraded: {degraded_to_canned} scenarios fell back to canned "
            "(PLATFORM_MCP_URL or ANTHROPIC_API_KEY is offline placeholder)"
        )
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


def _parse_only(argv: list[str]) -> list[str]:
    """Extract scenario name-substring filters from ``--only <pattern>``.

    Accepts multiple patterns via repeated ``--only`` flags or as a
    comma-separated single value. A scenario matches if any pattern
    appears in its name. Empty list means "no filter" (run all).

    Examples:
        --only remediate_               → single filter
        --only remediate_,dlq_          → two filters (comma-separated)
        --only remediate_ --only dlq_   → two filters (repeated flag)
    """
    patterns: list[str] = []
    for i, arg in enumerate(argv):
        raw = None
        if arg == "--only" and i + 1 < len(argv):
            raw = argv[i + 1]
        elif arg.startswith("--only="):
            raw = arg.split("=", 1)[1]
        if raw is not None:
            patterns.extend(p.strip() for p in raw.split(",") if p.strip())
    return patterns


def main() -> int:
    live = "--live" in sys.argv[1:]
    only_patterns = _parse_only(sys.argv[1:])
    settings = _settings_for_mode(live)
    scenarios = load_scenarios(_SCENARIOS_DIR)
    if only_patterns:
        # OR-match: scenario keeps if any pattern is a substring of its name.
        scenarios = [s for s in scenarios if any(p in s.name for p in only_patterns)]
        if not scenarios:
            print(f"no scenarios matched --only={only_patterns}")
            return 2
        print(f"filter --only={only_patterns} → {len(scenarios)} scenario(s)")
    offline_mcp = _is_offline_placeholder(str(settings.platform_mcp_url))
    offline_llm = _is_offline_api_key(settings.anthropic_api_key.get_secret_value())
    degraded_to_canned = sum(
        1 for s in scenarios if (s.use_live_mcp and offline_mcp) or (s.use_live_llm and offline_llm)
    )
    report, trajectories, briefings = run_all(scenarios, settings)
    write_report(report)
    write_trajectories(trajectories)
    ran_names = [o.scenario for o in report.outcomes]
    write_briefings(briefings, ran_names)
    _print_summary(report, degraded_to_canned=degraded_to_canned)
    return 0 if report.failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
