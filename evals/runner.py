"""Scenario runner. ``make eval`` calls the CLI at the bottom of this file."""

from __future__ import annotations

import os
import sys
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, SecretStr

from evals.chaos_hooks import ChaosInvocationError, invoke_chaos_hook
from evals.fakes import CannedMCPClient
from evals.graders.deterministic import (
    DimensionResult,
    GradeDimension,
    GradeReport,
    grade,
)
from evals.graders.llm_judge import JudgeScore, judge_briefing
from evals.guards import (
    PrincipalGuardError,
    assert_no_tier1_successes,
    assert_read_only_principal,
)
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
from incident_commander.llm.client import LLMClient, LLMClientProtocol, LLMError, preflight_auth
from incident_commander.llm.fakes import CannedLLMClient
from incident_commander.persistence.memory import InMemoryCheckpointer
from incident_commander.tools.mcp_client import (
    MCPClient,
    MCPClientProtocol,
    MCPError,
    make_client,
)

_EVAL_PLACEHOLDER_HOST = "eval.local"
_EVAL_PLACEHOLDER_API_KEY = "eval"

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCENARIOS_DIR = _REPO_ROOT / "evals" / "scenarios"
_REPORTS_DIR = _REPO_ROOT / "evals" / "reports"
_TRAJECTORIES_DIR = _REPO_ROOT / "evals" / "trajectories"
_BRIEFINGS_DIR = _REPO_ROOT / "evals" / "briefings"
_LATEST_REPORT = _REPORTS_DIR / "latest.json"
# Immutable per-invocation archive. The flat files above are POINTERS to
# the most recent run and are refreshed in place; this directory is the
# append-only record (CLAUDE.md invariant 9). Writing the archive with
# exclusive-create means a path collision fails loudly instead of
# deleting a prior run.
_RUNS_DIR = _REPO_ROOT / "evals" / "runs"
_TRACE_DIR_ENV = "EVAL_TRACE_DIR"


class ScenarioOutcome(BaseModel):
    """One scenario's run + grade, persisted in the aggregate report."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scenario: str
    final_state: IncidentState
    tool_calls_used: int
    report: GradeReport
    judge_score: JudgeScore | None = None
    # Five-bucket noise-source classification (docs/lessons/
    # live-eval-noise-sources.md) + "passed" + "unclassified". Heuristic,
    # derived from the grade report and the run's evidence — a starting
    # point for bucket-before-you-debug, not a verdict.
    failure_class: str = "unclassified"
    # Set when the briefing judge call itself failed: the scenario result
    # stands (graded deterministically); only the judge column is missing.
    judge_error: str | None = None


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
    invocation_id: str = ""
    outcomes: tuple[ScenarioOutcome, ...]


class Trajectory(BaseModel):
    """Per-run checkpoint log, written to ``evals/trajectories/<scenario>.json``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scenario: str
    incident_id: str
    checkpoints: tuple[RunState, ...]
    # Which runner invocation produced this. Mixed-vintage directories
    # self-describe even if filenames are lost — the trajectory writer
    # overwrote by scenario name until 2026-08-08 and Run 001's live
    # trajectories were erased by a later offline `make eval`.
    invocation_id: str = ""


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
    mcp_token: str | None = None,
    invocation_id: str = "",
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
        if invocation_id:
            # Share one id across the whole invocation so a scenario's
            # trace and its trajectory can be joined after the fact.
            tracer.invocation_id = invocation_id
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
        # Fire the scenario's declared chaos hook (if any) BEFORE building
        # the agent's client so a seeding failure surfaces immediately with
        # a clear reason, not as a downstream "read returned healthy" bug.
        # Canned runs skip this — the canned tool responses already encode
        # the broken state.
        if scenario.chaos_setup is not None:
            try:
                seed_result = invoke_chaos_hook(
                    str(settings.platform_mcp_url),
                    settings.platform_token.get_secret_value(),
                    scenario.chaos_setup.name,
                    dict(scenario.chaos_setup.arguments),
                )
            except ChaosInvocationError as err:
                raise RuntimeError(
                    f"scenario {scenario.name!r} chaos_setup "
                    f"{scenario.chaos_setup.name!r} failed: {err}"
                ) from err
            if tracer is not None:
                tracer.write(
                    {
                        "kind": "chaos_setup",
                        "scenario": scenario.name,
                        "hook": scenario.chaos_setup.name,
                        "arguments": dict(scenario.chaos_setup.arguments),
                        "result": seed_result,
                    }
                )
        live_mcp_client = make_client(
            settings,
            tracer=tracer.mcp_hook() if tracer else None,
            token=mcp_token,
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
        mcp_client,
        investigation_llm,
        model=settings.agent_model,
        # Freshness re-probe (ADR 0009) is live-only: canned tool responses
        # are instant-consistent, and a re-probe would consume an extra
        # scripted planner response, breaking every canned scenario.
        reprobe_attempts=(settings.investigate_reprobe_attempts if live_mcp_available else 0),
        reprobe_delay_seconds=settings.investigate_reprobe_delay_seconds,
    )
    # Phase 6 remediation loop: PLANNING → REMEDIATING → VERIFYING. Each
    # role gets its own LLM client so canned queues stay role-partitioned
    # and live tracer records label each call by role.
    transitions[IncidentState.PLANNING] = make_llm_plan(
        remediation_planner_llm, model=settings.agent_model
    )
    transitions[IncidentState.REMEDIATING] = make_remediate(
        mcp_client,
        # Live actions can take longer than reads (kafka restart, DB write,
        # etc.); canned responses are instant so the override is a no-op.
        action_timeout_seconds=(
            settings.action_tool_timeout_seconds if live_mcp_available else None
        ),
    )
    transitions[IncidentState.VERIFYING] = make_llm_verify(
        mcp_client,
        verification_judge_llm,
        model=settings.agent_model,
        # Poll the verify probe only against a real platform; canned
        # responses are instant-consistent so one read is authoritative.
        probe_attempts=settings.verify_probe_attempts if live_mcp_available else 1,
        probe_delay_seconds=settings.verify_probe_delay_seconds,
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
            invocation_id=invocation_id,
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
        judge_error: str | None = None
        if scenario.use_live_llm or (
            isinstance(judge_llm, CannedLLMClient) and judge_llm.has_remaining
        ):
            try:
                judge_score = judge_briefing(briefing, judge_llm, model=settings.judge_model)
            except LLMError as err:
                # The judge is a soft-quality column on top of an already-
                # graded run. Losing the judge must not void the run.
                judge_error = f"judge call failed: {err}"
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

    failure_class = _classify_failure(report, final)
    outcome = ScenarioOutcome(
        scenario=scenario.name,
        final_state=final.state,
        tool_calls_used=final.budget.tool_calls_used,
        report=report,
        judge_score=judge_score,
        failure_class=failure_class,
        judge_error=judge_error,
    )
    if tracer is not None:
        tracer.write(
            {
                "kind": "scenario_end",
                "scenario": scenario.name,
                "final_state": final.state.value,
                "tool_calls_used": final.budget.tool_calls_used,
                "passed": report.passed,
                "failure_class": failure_class,
            }
        )
    return ScenarioResult(outcome=outcome, trajectory=trajectory, briefing=briefing)


def _classify_failure(report: GradeReport, final: RunState | None) -> str:
    """Bucket a graded run into the five-bucket noise taxonomy.

    Heuristic and deliberately conservative: anything ambiguous lands in
    "unclassified" rather than a wrong bucket. Priority order mirrors the
    debugging discipline in docs/lessons/live-eval-noise-sources.md —
    environment before consistency before variance before grader.
    """
    if report.passed:
        return "passed"
    dims = {d.dimension: d for d in report.dimensions}
    failing = {d.dimension for d in report.dimensions if not d.passed}
    evidence = final.evidence if final is not None else ()
    summaries = [e.result_summary for e in evidence]
    if any("MCPError" in s or "LLM" in s and "invalid" in s for s in summaries):
        return "transport"
    if any("is_error=True" in s for s in summaries):
        return "shared-env"
    action = dims.get(GradeDimension.ACTION)
    if (
        action is not None
        and action.passed
        and any(
            e.tool_name == "_verify_judge" and e.result_summary.startswith("not_verified")
            for e in evidence
        )
    ):
        # Right action, judge said not-yet: the fix outran the probe.
        return "eventual-consistency"
    if failing == {GradeDimension.BUDGET}:
        return "llm-variance"
    if failing == {GradeDimension.EVIDENCE}:
        return "grader-brittleness"
    return "unclassified"


def _crashed_result(
    scenario: Scenario, exc: BaseException, invocation_id: str = ""
) -> ScenarioResult:
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
    # Post-#48 the transitions absorb transport failures as graded
    # escalations, so a crash that still reaches here is either the
    # scenario's own seeding (environment) or an unwrapped transport path.
    crash_class = "shared-env" if "chaos_setup" in error_detail else "transport"
    outcome = ScenarioOutcome(
        scenario=scenario.name,
        final_state=IncidentState.TRIAGE,
        tool_calls_used=0,
        report=report,
        judge_score=None,
        failure_class=crash_class,
    )
    trajectory = Trajectory(
        invocation_id=invocation_id,
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
    mcp_token: str | None = None,
    invocation_id: str = "",
) -> tuple[RunReport, tuple[Trajectory, ...], tuple[EscalationBriefing, ...]]:
    # run_scenario falls back to canned when env is placeholder; nothing
    # is skipped here. Per-scenario crashes are captured as failed outcomes
    # so the batch keeps running — see _crashed_result.
    results: list[ScenarioResult] = []
    for scenario in scenarios:
        try:
            results.append(
                run_scenario(
                    scenario, settings, clock, mcp_token=mcp_token, invocation_id=invocation_id
                )
            )
        except Exception as exc:  # noqa: BLE001 — deliberate: don't abort suite
            print(f"  CRASH {scenario.name}: {type(exc).__name__}: {exc}")
            results.append(_crashed_result(scenario, exc, invocation_id))
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
        invocation_id=invocation_id,
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


def archive_run(
    invocation_id: str,
    report: RunReport,
    trajectories: Iterable[Trajectory],
    briefings: Iterable[EscalationBriefing],
    scenario_names: Iterable[str],
    runs_dir: Path = _RUNS_DIR,
) -> Path:
    """Write this invocation's artifacts to an immutable per-run directory.

    The flat ``evals/{reports,trajectories,briefings}`` paths are pointers
    to the latest run and are refreshed in place — convenient, and the
    thing every existing consumer reads. This archive is the durable
    record required by CLAUDE.md invariant 9.

    Every file is opened with exclusive-create (``"x"``). That is the
    load-bearing half: if a future refactor ever routes two invocations at
    one directory, the run fails loudly instead of silently deleting the
    earlier one — which is exactly how Run 001's live trajectories were
    lost (study/findings.md F-002).
    """
    target = runs_dir / invocation_id
    (target / "trajectories").mkdir(parents=True, exist_ok=True)
    (target / "briefings").mkdir(parents=True, exist_ok=True)
    with (target / "report.json").open("x") as handle:
        handle.write(report.model_dump_json(indent=2))
    for trajectory in trajectories:
        with (target / "trajectories" / f"{trajectory.scenario}.json").open("x") as handle:
            handle.write(trajectory.model_dump_json(indent=2))
    for briefing, name in zip(briefings, scenario_names, strict=True):
        with (target / "briefings" / f"{name}.json").open("x") as handle:
            handle.write(briefing.model_dump_json(indent=2))
    return target


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
        elif outcome.judge_error is not None:
            judge_hint = f"  (judge unavailable: {outcome.judge_error})"
        class_hint = ""
        if not outcome.report.passed:
            class_hint = f"  [{outcome.failure_class}]"
        print(f"  {mark} {outcome.scenario}{class_hint}{judge_hint}")
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
    # Smoke mode selects the read-scoped principal from Settings directly.
    # It is NOT plumbed through the shell: exporting PLATFORM_TOKEN from a
    # make recipe was silently overridden by `-include .env` (PR #62 vs
    # #69), so every "read-scoped" smoke run before 2026-08-07 actually
    # held write scope. Config in, guard at point of use.
    smoke = "--smoke" in sys.argv[1:]
    # One identity per invocation, shared by the tracer, the trajectories,
    # the report, and the archive directory — so every artifact this run
    # produces can be joined, and none of them can collide with another
    # run's (CLAUDE.md invariant 9).
    invocation_id = uuid.uuid4().hex[:12]
    only_patterns = _parse_only(sys.argv[1:])
    settings = _settings_for_mode(live)
    mcp_token: str | None = None
    if smoke:
        if settings.platform_smoke_token is None:
            print("SMOKE FAIL: PLATFORM_SMOKE_TOKEN is not set in .env")
            print("run `make bootstrap-token` and add the read-scoped token")
            return 3
        mcp_token = settings.platform_smoke_token.get_secret_value()
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
    if live and not offline_llm and any(s.use_live_llm for s in scenarios):
        # One free authenticated call before anything runs: an expired key
        # otherwise surfaces as N identical per-scenario crash rows (the
        # 2026-08-03 campaign burned a whole smoke pass discovering this).
        try:
            preflight_auth(settings.anthropic_api_key.get_secret_value())
        except LLMError as err:
            print(f"PREFLIGHT FAIL (LLM auth): {err}")
            print("fix ANTHROPIC_API_KEY in .env — no scenarios ran, nothing was spent")
            return 3
    # Guard the principal at point of use, against the live platform,
    # before a single scenario (or dollar) is spent. v0.4.9 exposes no
    # whoami/introspection tool, so this is a negative probe: a Tier-1
    # call with invalid arguments must be refused on SCOPE. The handler
    # checks scope before parsing arguments, so it cannot execute under
    # either token — the two outcomes are distinguishable and safe.
    stage_started_at = datetime.now(UTC)
    # Unconditional in smoke mode whenever a real platform is reachable.
    # Derived from the platform URL, NOT from the --live flag: the guard
    # must not depend on a second mechanism (flag parsing) to decide
    # whether the first mechanism (scope) needs checking. There is no
    # opt-out — no env var, no flag, no config key disables this.
    guard_required = smoke and not _is_offline_placeholder(str(settings.platform_mcp_url))
    if smoke and not guard_required:
        print("smoke mode against a placeholder platform: canned run, no live principal to guard")
    if guard_required:
        try:
            guard_client = make_client(settings, token=mcp_token)
            try:
                assert_read_only_principal(guard_client)
            finally:
                guard_client.close()
        except PrincipalGuardError as err:
            print(f"PRINCIPAL GUARD FAIL: {err}")
            print("no scenarios ran, nothing was spent")
            return 4
        print("principal guard: token is read-scoped (negative probe refused on scope)")

    report, trajectories, briefings = run_all(
        scenarios, settings, mcp_token=mcp_token, invocation_id=invocation_id
    )
    ran_names = [o.scenario for o in report.outcomes]
    # Archive FIRST, into an immutable per-invocation directory, then
    # refresh the flat pointers. Order matters: if the pointer writes ever
    # fail, the durable record is already on disk. Until 2026-08-08 only
    # the pointers existed, so a routine offline `make eval` erased Run
    # 001's paid live trajectories (study/findings.md F-003).
    archived = archive_run(invocation_id, report, trajectories, briefings, ran_names)
    write_report(report)
    write_trajectories(trajectories)
    write_briefings(briefings, ran_names)
    _print_summary(report, degraded_to_canned=degraded_to_canned)
    print(f"run archived: {archived.relative_to(_REPO_ROOT)} (immutable; flat paths are pointers)")

    # Post-stage assertion, graded from the platform audit log rather than
    # the agent's own trajectory (CLAUDE.md invariant 6). This is the exact
    # evidence that exposed the token bug — now automatic.
    if guard_required:
        try:
            audit_client = make_client(settings, token=mcp_token)
            try:
                assert_no_tier1_successes(audit_client, stage_started_at)
            finally:
                audit_client.close()
        except PrincipalGuardError as err:
            print(f"POST-STAGE AUDIT FAIL: {err}")
            return 5
        except MCPError as err:
            print(f"POST-STAGE AUDIT INCONCLUSIVE (audit read failed): {err}")
            return 5
        print("post-stage audit: zero successful Tier-1 actions during the smoke stage")

    return 0 if report.failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
