"""Scenario runner. ``make eval`` calls the CLI at the bottom of this file."""

from __future__ import annotations

import json
import os
import stat
import sys
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    PostgresDsn,
    SecretStr,
    ValidationError,
)

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
    assert_chaos_capable_principal,
    assert_no_tier1_successes,
    assert_read_only_principal,
    assert_write_capable_principal,
)
from evals.preconditions import unmet
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
    ToolResult,
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
#
# The archive is written INCREMENTALLY: main() creates
# runs/<invocation_id>/ before the suite starts, and each scenario's
# trajectory, briefing and trace slice land as that scenario finishes
# (archive_scenario). ``report.json`` is written LAST (finalize_archive)
# and is the completion marker — a run directory without it is a killed
# or crashed run whose per-scenario files are still first-class evidence
# (ADR 0017). Archived files are locked read-only (+``uchg`` on macOS)
# as they land, and the whole directory is sealed when the marker goes
# down (ADR 0021) — invariant 9, enforced by the filesystem.
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
    # Set when post-grade briefing enrichment failed: the deterministic
    # briefing stands and the run keeps its grade; only the LLM-written
    # findings/recommendation are missing.
    briefing_error: str | None = None
    # Run provenance (ADR 0013): which legs actually ran live, and whether a
    # declared-live leg silently fell back to canned. Defaults are load-
    # bearing — archived reports and the committed baseline predate these
    # fields and must keep parsing (same precedent as failure_class above).
    live_mcp: bool = False
    live_llm: bool = False
    degraded: bool = False


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
    # None = pre-schema report ("unknown"), deliberately distinct from 0
    # ("verified fully live"): the committed baseline was a 32/37-degraded
    # canned run, and defaulting to 0 would make that artifact assert a
    # falsehood — the exact honesty failure these fields exist to fix.
    degraded_count: int | None = None
    # Which --only filters produced this report, so filtered runs
    # self-describe in latest.json and in the archive. Empty = full suite.
    only_patterns: tuple[str, ...] = ()
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
    # Exact-host match, not substring: "https://eval.local.evil.example"
    # must count as live (S-09) — only the placeholder host itself, with
    # any port/path, stays offline.
    return urlparse(url).hostname == _EVAL_PLACEHOLDER_HOST


def _is_offline_api_key(key: str) -> bool:
    return key in {_EVAL_PLACEHOLDER_API_KEY, "placeholder", ""}


class ScenarioCrash(Exception):
    """A scenario's run raised, wrapped with the history it had accumulated.

    The crash row used to be built from the exception alone, which is why
    it hardcoded ``tool_calls_used=0``: nothing else was reachable from
    ``run_suite``'s handler. A scenario that crashed after spending nine
    tool calls therefore reported spending none, and the suite's cost and
    budget columns were a lower bound that read like a measurement.

    ``checkpoints`` is the run's own checkpoint history up to the last
    completed transition — the same tuple the success path puts on the
    trajectory. It is still a lower bound (work inside the transition that
    crashed is not checkpointed) but it is a bound derived from what the
    run did, not from a constant.

    Raised only from ``run_scenario``'s own handler. Failures that happen
    before the loop starts — chaos seeding, preconditions — propagate
    unwrapped, because there is no run to describe.
    """

    def __init__(self, cause: BaseException, checkpoints: tuple[RunState, ...]) -> None:
        super().__init__(f"{type(cause).__name__}: {cause}")
        self.cause = cause
        self.checkpoints = checkpoints

    @property
    def final(self) -> RunState | None:
        """The last checkpointed state, if the run got far enough to write one."""
        return self.checkpoints[-1] if self.checkpoints else None


class PreconditionFailure(RuntimeError):
    """Base: the run was abandoned before the agent started."""


class PreconditionNotMet(PreconditionFailure):
    """The world answered, and the scenario's premise is false.

    Deliberately NOT a graded failure. A run that never happened has nothing
    to say about the agent, and recording it as an agent failure is how
    `bb1fa70abb4c` came to report that the agent fixed the wrong thing when
    the right thing could not be made to exist.
    """


class PreconditionUnverifiable(PreconditionFailure):
    """The probe never returned a usable answer, so the premise is unknown.

    Distinct from ``PreconditionNotMet`` on purpose, and the distinction is
    the whole point of the pair. "The fault was never manufactured" is a
    claim about the world; it requires the world to have answered. A refused
    scope, a dead platform, or a malformed response tells you nothing about
    the fault, and reporting one as the other sends the reader to seeding
    when the problem is the platform.

    "Answered" means the DECISIVE attempt answered — the one that ended the
    polling window. A readable-but-unmet reading followed by a dead platform
    is Unverifiable, because the reading that was current when polling gave
    up is the transport error, and the earlier one is stale.
    """


def _assert_preconditions(
    scenario: Scenario,
    client: MCPClientProtocol,
    tracer: JsonlTracer | None,
) -> None:
    """Probe the world for the scenario's premise, polling where declared."""
    for probe in scenario.expected_precondition:
        # Only the DECISIVE attempt — the one that ended the polling window —
        # speaks for the world. `reading` holds that attempt's verdict when it
        # answered with something we could read (empty list = met), and is
        # None while the newest attempt was unreadable; `unreadable` holds why.
        # Latching "did any attempt ever answer" across the window is what let
        # a dead platform on the last attempt report as "the fault was never
        # manufactured" — a claim about the world, quoting a transport error.
        reading: list[str] | None = None
        unreadable: list[str] = []
        # Kept only to say, in the Unverifiable message, that a now-stale
        # reading exists. Always unmet: an attempt that reads MET breaks out.
        stale_reading: list[str] = []
        for attempt in range(probe.attempts):
            if attempt:
                time.sleep(probe.delay_seconds)
            try:
                result = client.call_tool(probe.tool, dict(probe.arguments))
            except MCPError as err:
                reading, unreadable = None, [f"{probe.tool}: probe failed: {err}"]
                continue
            if result.is_error:
                reading, unreadable = None, [f"{probe.tool}: probe returned is_error=True"]
                continue
            payload = _first_json_object(result)
            if payload is None:
                reading = None
                unreadable = [f"{probe.tool}: probe returned no readable JSON object"]
                continue
            reading, unreadable = unmet(probe, payload), []
            if not reading:
                break
            stale_reading = reading
        # The predicate the report turns on: did the DECISIVE attempt answer?
        answered = reading is not None
        failures = reading if reading is not None else unreadable
        if tracer is not None:
            tracer.write(
                {
                    "kind": "precondition",
                    "scenario": scenario.name,
                    "tool": probe.tool,
                    "arguments": dict(probe.arguments),
                    "met": not failures,
                    "answered": answered,
                    # Whether the platform answered at any point in the window.
                    # Diagnostic only — it must never decide Not-Met vs
                    # Unverifiable, which is exactly the bug this pair records.
                    "ever_answered": answered or bool(stale_reading),
                    "failures": failures,
                }
            )
        if failures and not answered:
            stale = (
                f" An earlier attempt did read the world ({'; '.join(stale_reading)}), "
                "but that reading is stale and did not decide."
                if stale_reading
                else ""
            )
            raise PreconditionUnverifiable(
                f"scenario {scenario.name!r} precondition could not be checked after "
                f"{probe.attempts} attempt(s): {'; '.join(failures)}. The platform "
                "never returned a readable answer to the deciding attempt, so whether "
                "the fault exists is UNKNOWN — look at the platform, not at the "
                f"seeding.{stale}"
            )
        if failures:
            raise PreconditionNotMet(
                f"scenario {scenario.name!r} precondition not met after "
                f"{probe.attempts} attempt(s): {'; '.join(failures)}. The fault this "
                "scenario asserts was never manufactured, so the run was abandoned "
                "before any model call — this says nothing about the agent."
            )


def _first_json_object(result: ToolResult) -> dict[str, Any] | None:
    """First text block that parses as a JSON object, or None.

    Tolerates unparseable text rather than raising: a bare ``json.loads``
    here escaped the polling loop entirely, so one malformed block ended the
    run as an uncaught crash bucketed "transport" — losing both the probe
    that failed and the fact that it was a precondition at all.
    """
    for block in result.content:
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            try:
                parsed = json.loads(block["text"])
            except ValueError:
                continue
            if isinstance(parsed, dict):
                return parsed
    return None


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
        # Establish the scenario's premise before spending anything on it.
        # Runs after seeding and before the first model call, so a fault that
        # was never manufactured costs one read instead of a full graded run
        # (see evals/preconditions.py, and `bb1fa70abb4c` for the run that
        # paid for this lesson).
        if scenario.expected_precondition:
            try:
                _assert_preconditions(scenario, live_mcp_client, tracer)
            except PreconditionFailure:
                live_mcp_client.close()
                raise
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
        # JUDGE_MODEL, not agent_model. This verdict decides RESOLVED, and it
        # was the one judgement in the suite running on an unpinned model —
        # so a model-pin change could move every remediation outcome with
        # nothing in the report saying why. JUDGE_MODEL exists to be pinned
        # separately for exactly this reason (config.py, CLAUDE.md); the
        # briefing judge already used it and this one did not.
        #
        # It also judges an expectation the agent wrote for itself
        # (`plan.verify_expectation`), which is a weaker check than a
        # world-state assertion — `expected_evidence_fields` is the stronger
        # tool where a scenario can express the effect directly.
        model=settings.judge_model,
        # Poll the verify probe only against a real platform; canned
        # responses are instant-consistent so one read is authoritative.
        probe_attempts=settings.verify_probe_attempts if live_mcp_available else 1,
        probe_delay_seconds=settings.verify_probe_delay_seconds,
        # Same clock the loop runs on, so a multi-minute polling window
        # stamps each attempt with the time it actually happened rather
        # than reusing the transition's single entry read.
        clock=tick,
    )

    # Outside the try so a crash can still read what the run had spent when
    # it died — see ScenarioCrash.
    checkpointer = InMemoryCheckpointer()
    run: RunState | None = None
    try:
        # The scenario's declared cap IS the run's tool-call ceiling (ADR
        # 0019), not just the number it is graded against afterwards. Before
        # this, the two were different numbers and the planner was told the
        # fleet default of 25 in every scenario — including the ones whose
        # whole subject is behaviour under a tight budget.
        run = start_run(
            scenario.alert.model_dump(),
            settings,
            now,
            max_tool_calls=scenario.expectation.max_tool_calls,
        )
        final = run_to_completion(
            run,
            clock=tick,
            transitions=transitions,
            checkpointer=checkpointer,
        )
        trajectory = Trajectory(
            invocation_id=invocation_id,
            scenario=scenario.name,
            incident_id=str(final.incident_id),
            checkpoints=tuple(checkpointer.history(final.incident_id)),
        )
        # The briefing is built BEFORE grading, because ``ScenarioExpectation.
        # expect_briefing_contains`` grades the handoff artifact as the human
        # receives it — enrichment included, since ``findings`` and
        # ``recommendation`` are empty in the deterministic template. Grading
        # still makes no LLM call of its own; it reads a finished object.
        briefing = render_briefing(final)
        briefing_error: str | None = None
        if scenario.use_live_llm or (
            isinstance(briefing_llm, CannedLLMClient) and briefing_llm.has_remaining
        ):
            try:
                briefing = enrich_briefing(briefing, briefing_llm, model=settings.agent_model)
            except (LLMError, ValidationError) as err:
                # Enrichment is a decoration on the run, same as the judge
                # below. Losing the briefing writer must not void it (ADR
                # 0007: a crashed scenario is an eval-infrastructure bug by
                # definition) — the deterministic briefing stands and the run
                # is still graded. A scenario that asserts on briefing text
                # will grade that dimension red, which is the honest outcome:
                # the assertion was about a briefing we could not produce.
                # ValidationError is caught alongside LLMError deliberately
                # even though LLMClient._parse now wraps schema violations:
                # CannedLLMClient validates its payloads directly
                # (llm/fakes.py) and never goes through _parse, so the raw
                # pydantic error is still reachable.
                briefing_error = f"briefing enrichment failed: {err}"
        report = grade(final, scenario.expectation, briefing=briefing)
        judge_score: JudgeScore | None = None
        judge_error: str | None = None
        if scenario.use_live_llm or (
            isinstance(judge_llm, CannedLLMClient) and judge_llm.has_remaining
        ):
            try:
                judge_score = judge_briefing(briefing, judge_llm, model=settings.judge_model)
            except (LLMError, ValidationError) as err:
                # The judge is a soft-quality column on top of an already-
                # graded run. Losing the judge must not void the run.
                # ValidationError for the same canned-client reason as above;
                # JudgeScore's ge/le bounds are not guaranteed by constrained
                # decoding either.
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
        # Re-raise carrying the run's own history, so the crash row reports
        # what the scenario actually spent instead of a hardcoded zero.
        raise ScenarioCrash(
            exc,
            () if run is None else tuple(checkpointer.history(run.incident_id)),
        ) from exc
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
        briefing_error=briefing_error,
        # Provenance mirrors the tracer's scenario_start keys above.
        live_mcp=live_mcp_available,
        live_llm=live_llm_available,
        degraded=(scenario.use_live_mcp and not live_mcp_available)
        or (scenario.use_live_llm and not live_llm_available),
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
    if any(
        ("MCP error" in s) or ("MCPError" in s) or ("LLM" in s and "invalid" in s)
        for s in summaries
    ):
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

    A ``ScenarioCrash`` also carries the run's partial ledger and
    checkpoints, so the row reports what the scenario spent before it died
    rather than zero. The bucketing below reads the original cause, not the
    wrapper — the wrapper is a carrier, not a new failure mode.
    """
    cause = exc.cause if isinstance(exc, ScenarioCrash) else exc
    partial = exc.final if isinstance(exc, ScenarioCrash) else None
    checkpoints = exc.checkpoints if isinstance(exc, ScenarioCrash) else ()
    error_detail = f"{type(cause).__name__}: {cause}"
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
    if isinstance(cause, PreconditionNotMet):
        # Its own bucket on purpose. "shared-env" would be close but wrong:
        # nothing was contended, the world simply was not in the state the
        # scenario asserts, and no agent behaviour is being described.
        crash_class = "precondition"
    elif isinstance(cause, PreconditionUnverifiable):
        # The premise is UNKNOWN, not false. Bucketing this as "precondition"
        # would send the reader to seeding when the platform is the problem.
        crash_class = "transport"
    elif "chaos_setup" in error_detail:
        crash_class = "shared-env"
    else:
        crash_class = "transport"
    outcome = ScenarioOutcome(
        scenario=scenario.name,
        # Both read off the last checkpoint the run wrote. Hardcoding TRIAGE
        # and 0 described a run that never started, which is a different
        # failure from the one being reported — and it made every crashed
        # row's usage silently unusable for the cost columns (ADR 0015: the
        # meter may over-report, never under-report).
        final_state=IncidentState.TRIAGE if partial is None else partial.state,
        tool_calls_used=0 if partial is None else partial.budget.tool_calls_used,
        report=report,
        judge_score=None,
        failure_class=crash_class,
        # Provenance survives the crash (ADR 0013). These defaulted to False,
        # so every crashed row in a live report claimed it had run canned —
        # and `degraded` False alongside said that was intended. A row that
        # misdescribes how it ran is worse than a missing row: the report is
        # the artifact, and a reader counting live coverage counted wrong.
        live_mcp=scenario.use_live_mcp,
        live_llm=scenario.use_live_llm,
    )
    # Invariant 9: the evidence a crashed run did produce is still evidence.
    # An empty trajectory under a nil incident id is not "no data", it is
    # data the harness threw away on its way to writing the row.
    trajectory = Trajectory(
        invocation_id=invocation_id,
        scenario=scenario.name,
        incident_id=(
            "00000000-0000-0000-0000-000000000000" if partial is None else str(partial.incident_id)
        ),
        checkpoints=checkpoints,
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
    only_patterns: tuple[str, ...] = (),
    on_result: Callable[[ScenarioResult], None] | None = None,
) -> tuple[RunReport, tuple[Trajectory, ...], tuple[EscalationBriefing, ...]]:
    # run_scenario falls back to canned when env is placeholder; nothing
    # is skipped here. Per-scenario crashes are captured as failed outcomes
    # so the batch keeps running — see _crashed_result.
    #
    # ``on_result`` fires once per scenario, on both paths, before the next
    # scenario starts: main() passes archive_scenario there so the evidence
    # for scenario N is durable while scenario N+1 runs. It is called
    # OUTSIDE the try, so an archive failure (e.g. exclusive-create hitting
    # an existing file) aborts the suite loudly instead of being recorded as
    # a scenario crash.
    results: list[ScenarioResult] = []
    for scenario in scenarios:
        try:
            result = run_scenario(
                scenario, settings, clock, mcp_token=mcp_token, invocation_id=invocation_id
            )
        except Exception as exc:  # noqa: BLE001 — deliberate: don't abort suite
            print(f"  CRASH {scenario.name}: {type(exc).__name__}: {exc}")
            result = _crashed_result(scenario, exc, invocation_id)
        results.append(result)
        if on_result is not None:
            on_result(result)
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
        # _crashed_result outcomes keep degraded=False defaults: a crash is
        # already a failure, so this post-run count may undercount the
        # pre-run estimate when a live scenario crashes — acceptable, the
        # --live gate in main() is pre-run.
        degraded_count=sum(1 for o in outcomes if o.degraded),
        only_patterns=only_patterns,
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


def _lock_path(path: Path) -> None:
    """Make one archived path refuse writes: clear write bits, then set
    ``uchg`` where the platform has it (macOS/BSD; Linux CI does not).

    This is invariant 9 enforced by the filesystem instead of by
    discipline — the same convention ``context/pack.sh`` applies to
    session archives ("Archives cannot be deleted", context/README.md).
    Exclusive-create already stops the *runner* from overwriting evidence;
    this stops everything else: ``rm -rf``, ``git clean -fd``, a cleanup
    script pointed at the wrong checkout. Before this, 0 of the files
    under ``evals/runs/`` were protected on disk, and a routine cleanup
    of a retired checkout came within one command of destroying 195 run
    files that existed nowhere else.

    Best-effort, never fatal: by the time anything is locked its bytes
    are already durable, so a filesystem that refuses ``chmod`` or
    ``chflags`` (some network mounts) gets a logged warning, not a
    crashed run. Already-immutable paths are skipped, which makes the
    finalize-time sweep idempotent — ``chmod`` on a ``uchg`` file is
    itself EPERM.

    Deliberate unlock (rare, announced): ``chflags -R nouchg`` +
    ``chmod -R u+w`` — see docs/runbook.md §"Completed archives are
    locked on disk".
    """
    try:
        st = path.stat()
        if getattr(st, "st_flags", 0) & stat.UF_IMMUTABLE:
            return
        os.chmod(path, st.st_mode & ~0o222)
        if hasattr(os, "chflags"):
            os.chflags(path, getattr(st, "st_flags", 0) | stat.UF_IMMUTABLE)
    except OSError as exc:
        print(f"archive lock skipped ({path}): {exc}")


def _lock_archive_tree(target: Path) -> None:
    """Lock every path under ``target``, children before parents.

    Bottom-up because the directory locks must land last: ``uchg`` on a
    directory refuses new entries, and the per-scenario files (locked
    individually as they were archived) sit inside the directories this
    seals. Called from ``finalize_archive`` only — a partial archive's
    directories must stay writable so the next scenario, and eventually
    ``report.json``, can still land.
    """
    for child in sorted(target.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        _lock_path(child)
    _lock_path(target)


def _archive_trajectory(target: Path, trajectory: Trajectory) -> None:
    """Exclusive-create ``<target>/trajectories/<scenario>.json``."""
    directory = target / "trajectories"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{trajectory.scenario}.json"
    with path.open("x") as handle:
        handle.write(trajectory.model_dump_json(indent=2))
    _lock_path(path)


def _archive_briefing(target: Path, scenario: str, briefing: EscalationBriefing) -> None:
    """Exclusive-create ``<target>/briefings/<scenario>.json``."""
    directory = target / "briefings"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{scenario}.json"
    with path.open("x") as handle:
        handle.write(briefing.model_dump_json(indent=2))
    _lock_path(path)


def _archive_trace_slice(
    target: Path, scenario: str, *, invocation_id: str, trace_dir: Path
) -> None:
    """Copy this invocation's trace records into ``<target>/traces/<scenario>.jsonl``.

    The flat ``<EVAL_TRACE_DIR>/<scenario>.jsonl`` accumulates every
    invocation's records forever (never truncated — study/findings.md
    F-002) and is gitignored, so an archived run whose traces stayed only
    there could not be joined back to the prompts and responses that
    produced it (S-07). The archive therefore carries its own slice.

    Filter convention, shared with ``scripts/estimate_cost.py``: records
    whose ``invocation_id`` does not match are another run's, and records
    with no ``invocation_id`` at all are pre-invocation-id vintage — both
    are excluded. Unparseable lines are skipped rather than fatal; a
    truncated final line from a killed run must not block the archive.

    The flat file is opened READ-ONLY on every path. It is the canonical
    incremental record — the only evidence of a scenario killed mid-flight,
    before this function ever fires for it.
    """
    source = trace_dir / f"{scenario}.jsonl"
    if not source.exists():
        return
    kept: list[str] = []
    for line in source.read_text().splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and record.get("invocation_id") == invocation_id:
            kept.append(line)
    directory = target / "traces"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{scenario}.jsonl"
    with path.open("x") as handle:
        for line in kept:
            handle.write(line + "\n")
    # The lock lands on the archived SLICE only. The flat source file
    # stays writable forever: later invocations APPEND to it (F-002's
    # fix), and locking it would crash the next run's tracer.
    _lock_path(path)


def archive_scenario(
    target: Path,
    result: ScenarioResult,
    *,
    invocation_id: str,
    trace_dir: Path | None = None,
) -> None:
    """Stream one finished scenario's evidence into the run archive.

    Wired into ``run_all(on_result=...)``, so scenario N's trajectory,
    briefing and trace slice are on disk before scenario N+1 starts.
    Before this existed, every artifact but the JSONL traces lived in
    memory until the whole suite finished, and a Ctrl-C at scenario 30 of
    37 threw away 30 scenarios of paid live evidence (S-05/A-14) — the
    F-002/F-003 shape CLAUDE.md invariant 9 exists to prevent. A hard kill
    now costs at most the in-flight scenario.

    Every file is exclusive-create (``"x"``), as in ``archive_run``: a
    second write at a path that already holds evidence crashes instead of
    deleting it. And every file is LOCKED (``_lock_path``) the moment its
    write completes, so a killed run's partial rows are already protected
    on disk — evidence the instant they are durable, not the instant the
    suite happens to finish. ``report.json`` is NOT written here — see
    ``finalize_archive``.
    """
    scenario = result.outcome.scenario
    _archive_trajectory(target, result.trajectory)
    _archive_briefing(target, scenario, result.briefing)
    if trace_dir is not None:
        _archive_trace_slice(target, scenario, invocation_id=invocation_id, trace_dir=trace_dir)


def finalize_archive(target: Path, report: RunReport) -> Path:
    """Write ``<target>/report.json`` — the completion marker, always last.

    Presence of ``report.json`` is what makes an archive complete. Its
    absence marks the directory as a partial run (killed, crashed, or still
    in flight) whose per-scenario files remain first-class evidence — which
    is why the aggregate report moved from the FIRST archive write to the
    last (S-08): written first, a half-finished archive was
    indistinguishable from a complete one.

    Once the marker is down, the whole archive is locked on disk
    (``_lock_archive_tree``): every file and directory loses its write
    bits and, where the platform supports it, gains ``uchg``. The marker
    means "nothing will ever write here again", so this is the exact
    moment enforcement can start — locking any earlier would refuse the
    archive its own remaining writes. A killed run never reaches this
    call: its per-scenario files are individually locked already, and its
    directories stay writable, which costs nothing — no future invocation
    ever writes there (fresh ``invocation_id`` per run, exclusive-create
    on every file), so the writable window is only ever used by manual,
    deliberate hands.
    """
    target.mkdir(parents=True, exist_ok=True)
    with (target / "report.json").open("x") as handle:
        handle.write(report.model_dump_json(indent=2))
    _lock_archive_tree(target)
    return target


def archive_run(
    invocation_id: str,
    report: RunReport,
    trajectories: Iterable[Trajectory],
    briefings: Iterable[EscalationBriefing],
    scenario_names: Iterable[str],
    runs_dir: Path = _RUNS_DIR,
) -> Path:
    """Write a whole run's artifacts to an immutable per-run directory at once.

    The one-shot equivalent of the streaming path (``archive_scenario`` per
    scenario, then ``finalize_archive``) that ``main`` uses, kept for
    callers that already hold a complete run in memory. Same files, same
    ordering guarantee: report.json last.

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
    for trajectory in trajectories:
        _archive_trajectory(target, trajectory)
    for briefing, name in zip(briefings, scenario_names, strict=True):
        _archive_briefing(target, name, briefing)
    return finalize_archive(target, report)


def _eval_defaults() -> Settings:
    """Placeholder Settings for offline eval runs (budget is what actually matters).

    Constructor with ``_env_file=None``, NOT ``model_validate``: the latter
    treats ``_env_file`` as data (``extra="ignore"`` drops it silently) and
    consults the cwd ``.env`` for any field absent from the dict — which is
    how a real ``PLATFORM_SMOKE_TOKEN`` leaked into "offline" runs (A-04).
    ``platform_smoke_token=None`` is pinned explicitly so an exported shell
    var cannot supply it either. Fields left unpinned here (``agent_model``,
    budget knobs) can still absorb exported shell env vars — the dotenv leak
    is the one closed here.
    """
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        anthropic_api_key=SecretStr("eval"),
        judge_model="eval-judge",
        platform_mcp_url=AnyHttpUrl("https://eval.local"),
        platform_rest_url=AnyHttpUrl("https://eval.local"),
        platform_token=SecretStr("eval"),
        platform_webhook_secret=SecretStr("eval"),
        database_url=PostgresDsn("postgresql://eval:eval@localhost:5432/eval"),
        platform_smoke_token=None,
    )


def _print_summary(report: RunReport) -> None:
    print(f"scenarios: {report.total}, passed: {report.passed}, failed: {report.failed}")
    # Print from the persisted field so the console number and the artifact
    # number are the same value by construction — their divergence (stdout
    # said "degraded", latest.json said nothing) was finding A-01.
    if (report.degraded_count or 0) > 0:
        print(
            f"degraded: {report.degraded_count} scenarios fell back to canned "
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
        briefing_hint = ""
        if outcome.briefing_error is not None:
            briefing_hint = f"  (briefing unenriched: {outcome.briefing_error})"
        class_hint = ""
        if not outcome.report.passed:
            class_hint = f"  [{outcome.failure_class}]"
        print(f"  {mark} {outcome.scenario}{class_hint}{judge_hint}{briefing_hint}")
        if not outcome.report.passed:
            for dim in outcome.report.dimensions:
                if not dim.passed:
                    print(f"    - {dim.dimension.value}: {dim.detail}")


def _canned_equivalent_knob_warning(settings: Settings) -> str | None:
    """Preflight nudge for --live runs still on canned-equivalent probe knobs.

    The config.py defaults (verify_probe_attempts=1,
    investigate_reprobe_attempts=0) deliberately keep canned runs
    byte-identical (ADR 0006/0009), but a live run at those values
    reproduces both documented live failure modes the mitigations exist
    for (S-10). A warning, not exit 3: through Settings an explicit
    VERIFY_PROBE_ATTEMPTS=1 is indistinguishable from unset, so a hard
    fail would ban deliberate single-probe live experiments with no
    escape hatch.
    """
    if settings.verify_probe_attempts <= 1 or settings.investigate_reprobe_attempts == 0:
        return (
            "WARNING: live run with canned-equivalent probe knobs "
            f"(VERIFY_PROBE_ATTEMPTS={settings.verify_probe_attempts}, "
            f"INVESTIGATE_REPROBE_ATTEMPTS={settings.investigate_reprobe_attempts}); "
            "the ADR 0006/0009 live mitigations are OFF — see docs/runbook.md "
            '"Environment variable knobs"'
        )
    return None


def _repo_relative(path: Path) -> str:
    """Display form for a path: repo-relative when it is inside the repo.

    Unit tests point ``_RUNS_DIR`` at ``tmp_path``, which has no repo-root
    prefix, and a console nicety must never be the thing that raises.
    """
    try:
        return str(path.relative_to(_REPO_ROOT))
    except ValueError:
        return str(path)


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
    if smoke and not live:
        # Before settings load, so this needs no env at all. Erroring beats
        # silently implying --live: that would flip the settings source from
        # placeholder to real .env behind the operator's back (A-04).
        print(
            "SMOKE FAIL: --smoke requires --live (smoke without live would "
            "run the whole suite canned under placeholder settings)"
        )
        return 3
    # One identity per invocation, shared by the tracer, the trajectories,
    # the report, and the archive directory — so every artifact this run
    # produces can be joined, and none of them can collide with another
    # run's (CLAUDE.md invariant 9).
    invocation_id = uuid.uuid4().hex[:12]
    only_patterns = _parse_only(sys.argv[1:])
    try:
        settings = _settings_for_mode(live)
    except ValidationError as err:
        # Exit 3 is the preflight/env code (see the smoke-token and LLM-auth
        # exits below); a raw traceback would exit 1, the code reserved for
        # "a scenario failed" (A-15).
        fields = ", ".join(
            ".".join(str(part) for part in detail["loc"]) or "(settings)" for detail in err.errors()
        )
        print(f"PREFLIGHT FAIL (env): invalid or missing settings — {fields}")
        return 3
    # Live-only, immediately after the settings load and before any spend:
    # surface canned-equivalent probe knobs even on runs that go on to be
    # refused. Never affects exit codes.
    if live and (msg := _canned_equivalent_knob_warning(settings)) is not None:
        print(msg)
    mcp_token: str | None = None
    if smoke:
        # An empty secret is an UNSET one, not a token. `is None` alone let
        # SecretStr("") through, mcp_token became "", and make_client's old
        # `token or ...` then selected the FULL principal for every client
        # in the stage — guard client included (S-04).
        if settings.platform_smoke_token is None or not (
            settings.platform_smoke_token.get_secret_value().strip()
        ):
            print("SMOKE FAIL: PLATFORM_SMOKE_TOKEN is not set in .env")
            print("run `make bootstrap-token` and add the read-scoped token")
            return 3
        mcp_token = settings.platform_smoke_token.get_secret_value()
    scenarios = load_scenarios(_SCENARIOS_DIR)
    if only_patterns:
        # OR-match: scenario keeps if any pattern is a substring of its name.
        # Accounted PER PATTERN, not just over the union. Refusing only when
        # the whole selection came back empty is what let one dead pattern
        # hide among the nineteen in SMOKE_ONLY: rename a read-only scenario
        # and its pattern quietly matches nothing, the other eighteen still
        # select scenarios, and the smoke pass reports green over a smaller
        # suite than the reader believes (WO-R2-41). A pattern that matches
        # nothing is a name that no longer exists, and the honest reading of
        # that is "the selection you asked for is not there" — not "run the
        # remainder". The per-pattern counts print on every filtered run so
        # the smoke log carries its own coverage evidence.
        matched: dict[str, list[str]] = {
            pattern: [s.name for s in scenarios if pattern in s.name] for pattern in only_patterns
        }
        for pattern, names in matched.items():
            print(f"  --only {pattern} → {len(names)} scenario(s)")
        dead = [pattern for pattern, names in matched.items() if not names]
        if dead:
            # Subsumes the older "no scenarios matched at all" case: if every
            # pattern is dead the whole selection is empty, and this names
            # which patterns to fix instead of only reporting the total.
            print(
                f"SELECTION FAIL: {len(dead)} --only pattern(s) matched no "
                f"scenario: {', '.join(dead)}"
            )
            print(
                "A pattern that matches nothing is a scenario that was renamed "
                "or deleted with the pattern left behind. Fix the pattern (or "
                "restore the name) — do not run the remainder and call it a pass."
            )
            print("no scenarios ran, nothing was spent")
            return 2
        scenarios = [s for s in scenarios if any(p in s.name for p in only_patterns)]
        print(f"filter --only={only_patterns} → {len(scenarios)} scenario(s)")
    if smoke:
        # A read-only stage does not seed chaos. run_scenario fires
        # chaos_setup under settings.platform_token — the full write+chaos
        # principal — which is exactly the claim --smoke exists to disprove.
        # The #80 principal guard only inspects the AGENT client's token and
        # the exit-5 post-stage audit sees the write after it lands, so the
        # only prevention is refusing the selection outright, here: after
        # --only (the reachable channel, since SMOKE_ONLY is .env-overridable)
        # and before preflight, guard, and any spend. There is no opt-out
        # flag: a scenario that seeds chaos is not a smoke scenario (S-03).
        chaos_scenarios = [s.name for s in scenarios if s.chaos_setup is not None]
        if chaos_scenarios:
            print(
                f"SMOKE FAIL: scenario(s) {', '.join(chaos_scenarios)} declare "
                "chaos_setup — a read-only stage does not seed chaos (chaos runs "
                "under the full write+chaos principal)"
            )
            print("no scenarios ran, nothing was spent")
            return 6
    if live and not smoke:
        # A canned-only scenario (no live leg declared) cannot run against
        # the live platform: the platform cannot manufacture or expose its
        # fault, so run_scenario would silently serve the canned fixtures
        # and the row would land in the live report's pass count as if the
        # world had been graded. Refusing the selection is the only honest
        # outcome — a canned green here is a statement about fixtures, not
        # the agent. Each such scenario's YAML says why it is canned-only
        # and what platform change unblocks it. --smoke is exempt by
        # design: its stage deliberately mixes canned harness-sanity
        # scenarios (noise_*, tool_*, ...) with live reads, and its report
        # is read that way (docs/eval-methodology.md, "The read-only smoke
        # pass"). Runs before the ADR 0020 mutating gate so its "run each
        # one alone" advice is only ever issued for scenarios that CAN run.
        canned_only = sorted(s.name for s in scenarios if s.canned_only)
        if canned_only:
            print(
                f"LIVE FAIL: {len(canned_only)} canned-only scenario(s) selected — "
                f"{', '.join(canned_only)}. Each declares use_live_mcp/use_live_llm "
                "false because the live platform cannot manufacture its fault; the "
                "comment above those flags in the scenario YAML says why, and what "
                "platform change unblocks it."
            )
            print(
                "Run them offline instead:\n"
                + "\n".join(f"  make eval ONLY={name}" for name in canned_only)
            )
            print("no scenarios ran, nothing was spent")
            return 8
    if live:
        # One state-mutating scenario per invocation, enforced rather than
        # remembered (ADR 0020). Nine remediation scenarios share ONE platform,
        # and the seeded pool has exactly one replay_safe row that two of them
        # both consume — so in a single invocation a CORRECT agent greens one
        # and reds the other, and the report blames the agent. The runner has
        # no reset between scenarios; the reset lives outside it, which is why
        # this is a selection refusal and not a scheduling fix.
        mutating = [
            s.name for s in scenarios if s.expectation.expected_action_tools or s.chaos_setup
        ]
        if len(mutating) > 1:
            print(
                f"LIVE FAIL: {len(mutating)} state-mutating scenarios selected — "
                f"{', '.join(sorted(mutating))}. Each of these changes the world the "
                "next one reads, and nothing resets between them inside one run."
            )
            print(
                "Run them one at a time, resetting in between:\n"
                + "\n".join(
                    f"  make eval-live ONLY={name} && make eval-reset" for name in sorted(mutating)
                )
            )
            print("no scenarios ran, nothing was spent")
            return 7
    offline_mcp = _is_offline_placeholder(str(settings.platform_mcp_url))
    offline_llm = _is_offline_api_key(settings.anthropic_api_key.get_secret_value())
    degraded_to_canned = sum(
        1 for s in scenarios if (s.use_live_mcp and offline_mcp) or (s.use_live_llm and offline_llm)
    )
    if live and degraded_to_canned > 0:
        # Fail BEFORE run_all: a misconfigured --live run costs zero tool
        # calls and zero dollars, and can never produce a latest.json that
        # is indistinguishable from a live-green run (A-01/S-09) — nor the
        # worse chimera of real MCP+chaos driven by a canned LLM.
        legs = []
        if offline_mcp:
            legs.append("PLATFORM_MCP_URL is the offline placeholder")
        if offline_llm:
            legs.append(
                "ANTHROPIC_API_KEY is empty or a placeholder "
                "(.env.example ships ANTHROPIC_API_KEY= empty — the exact trigger)"
            )
        print(
            f"PREFLIGHT FAIL (env): --live but {degraded_to_canned}/{len(scenarios)} "
            f"scenario(s) would degrade to canned — {'; '.join(legs)}"
        )
        print("no scenarios ran, nothing was spent")
        return 3
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
        # Defense-in-depth behind the degraded fail-fast above (reachable
        # when the --only selection contains no use_live scenario): a smoke
        # pass exists to verify the live read-scoped principal, and a
        # placeholder platform has no principal to verify (A-04).
        print(
            "SMOKE FAIL: smoke mode against a placeholder platform — "
            "there is no live principal to guard"
        )
        return 3
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
    # The mirror, for the stage that must be able to act. Until now every
    # principal check was gated on `smoke`, so the remediation stage — the
    # only one that spends money AND mutates — was the one stage running
    # unguarded. A read-scoped token there does not fail fast: each scenario
    # investigates, plans, attempts its action, is refused, and grades red
    # after full spend.
    #
    # Each half asks about the scope its half of the selection needs, keyed
    # off the same two fields the ADR 0020 mutating gate above is keyed off.
    # `expected_action_tools` alone was not enough: a scenario that mutates
    # the platform solely through `chaos_setup` declares none, so it skipped
    # the guard entirely and discovered its wrong token inside run_scenario,
    # mid-invocation. And `actions:execute` is not the scope it needs —
    # seeding is `chaos:invoke`, so probing for write scope would refuse
    # tokens that can seed and pass tokens that cannot.
    live_platform = (
        live and not smoke and not _is_offline_placeholder(str(settings.platform_mcp_url))
    )
    write_guard_required = live_platform and any(
        s.expectation.expected_action_tools for s in scenarios
    )
    chaos_guard_required = live_platform and any(s.chaos_setup is not None for s in scenarios)
    if write_guard_required or chaos_guard_required:
        # One client, both probes: in a live non-smoke run `mcp_token` is None,
        # so this is `settings.platform_token` — the same principal
        # `run_scenario` fires `chaos_setup` under.
        try:
            guard_client = make_client(settings, token=mcp_token)
            try:
                if write_guard_required:
                    assert_write_capable_principal(guard_client)
                if chaos_guard_required:
                    assert_chaos_capable_principal(guard_client)
            finally:
                guard_client.close()
        except PrincipalGuardError as err:
            print(f"PRINCIPAL GUARD FAIL: {err}")
            print("no scenarios ran, nothing was spent")
            return 4
        if write_guard_required:
            print("principal guard: token can act (negative probe refused on arguments, not scope)")
        if chaos_guard_required:
            print(
                "principal guard: token can seed chaos (negative probe refused "
                "on arguments, not scope)"
            )

    # Create the archive directory BEFORE the suite runs: from here on every
    # scenario streams its own evidence into it as it finishes, so a Ctrl-C
    # or a crash costs at most the in-flight scenario (ADR 0017).
    # exist_ok=False on all three: an invocation-id collision must fail
    # loudly, before any scenario is paid for, rather than land two runs in
    # one directory.
    trace_dir_setting = os.environ.get(_TRACE_DIR_ENV)
    trace_dir = Path(trace_dir_setting) if trace_dir_setting else None
    target = _RUNS_DIR / invocation_id
    target.mkdir(parents=True, exist_ok=False)
    (target / "trajectories").mkdir(exist_ok=False)
    (target / "briefings").mkdir(exist_ok=False)
    if trace_dir is not None:
        (target / "traces").mkdir(exist_ok=False)

    report, trajectories, briefings = run_all(
        scenarios,
        settings,
        mcp_token=mcp_token,
        invocation_id=invocation_id,
        only_patterns=tuple(only_patterns),
        on_result=lambda result: archive_scenario(
            target, result, invocation_id=invocation_id, trace_dir=trace_dir
        ),
    )
    ran_names = [o.scenario for o in report.outcomes]
    # The per-scenario archive writes already happened, inside run_all.
    # report.json goes down next — the completion marker, and the last write
    # into the archive — and only then are the flat pointers refreshed.
    # Order matters: if the pointer writes ever fail, the durable record is
    # already complete on disk. Until 2026-08-08 only the pointers existed,
    # so a routine offline `make eval` erased Run 001's paid live
    # trajectories (study/findings.md F-003).
    archived = finalize_archive(target, report)
    write_report(report)
    write_trajectories(trajectories)
    write_briefings(briefings, ran_names)
    _print_summary(report)
    print(f"run archived: {_repo_relative(archived)} (immutable; flat paths are pointers)")

    # Post-stage assertion, graded from the platform audit log rather than
    # the agent's own trajectory (CLAUDE.md invariant 6). This is the exact
    # evidence that exposed the token bug — now automatic.
    if guard_required:
        # Attribute violations to the principals this stage owns, so a
        # shared platform's other service accounts cannot fail (or mask)
        # this stage (A-13). Both ids or nothing: the failure mode this
        # guard exists for — the "read-scoped" stage silently holding the
        # full token — writes its audit rows under the AGENT principal, so
        # a half-configured env naming only the smoke id would blind the
        # guard to its own reason for existing. Unfiltered is over-broad,
        # which is the safe side to fall back to.
        agent_principal = settings.platform_agent_principal_id
        smoke_principal = settings.platform_smoke_principal_id
        principal_ids = (
            frozenset({agent_principal, smoke_principal})
            if agent_principal and smoke_principal
            else None
        )
        if principal_ids is None and (agent_principal or smoke_principal):
            print(
                "post-stage audit: only one of PLATFORM_AGENT_PRINCIPAL_ID / "
                "PLATFORM_SMOKE_PRINCIPAL_ID is set — ignoring both and failing "
                "on ANY service account's Tier-1 success (see docs/runbook.md)"
            )
        try:
            audit_client = make_client(settings, token=mcp_token)
            try:
                assert_no_tier1_successes(
                    audit_client, stage_started_at, principal_ids=principal_ids
                )
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
