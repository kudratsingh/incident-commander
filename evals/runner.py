"""Scenario runner. ``make eval`` calls the CLI at the bottom of this file."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any, Final, TypedDict, cast
from urllib.parse import urlparse

import yaml
from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    PostgresDsn,
    SecretStr,
    ValidationError,
    model_validator,
)

from evals import artifacts
from evals.chaos_hooks import ChaosInvocationError, invoke_chaos_hook
from evals.fakes import CannedMCPClient
from evals.graders.deterministic import (
    FALSE_ATTRIBUTION_CLASS,
    DimensionResult,
    GradeDimension,
    GradeReport,
    grade,
    is_vacuous_detail,
    not_applicable_detail,
)
from evals.graders.llm_judge import JudgeScore, judge_briefing
from evals.graders.root_cause import (
    RootCauseCoverage,
    coverage_over,
    is_not_graded_detail,
    label_describes_this_world,
)
from evals.guards import (
    AuditWindowScan,
    PrincipalGuardError,
    assert_chaos_blind_principal,
    assert_chaos_capable_principal,
    assert_no_tier1_successes,
    assert_read_only_principal,
    assert_write_capable_principal,
)
from evals.preconditions import probe_label, unmet
from evals.recorded_client import RecordedMCPClient, matching_recordings
from evals.scenarios.loader import load_scenarios
from evals.scenarios.schema import (
    TTL_ARGUMENT,
    ChaosHook,
    ChaosPlan,
    PreconditionProbe,
    Scenario,
)
from evals.tracing import JsonlTracer, TraceKind, tracer_for
from incident_commander.agent.accounting import RunAccounting, accrue_llm_error
from incident_commander.agent.briefing import EscalationBriefing, render_briefing
from incident_commander.agent.briefing_enrichment import enrich_briefing
from incident_commander.agent.factory import start_run
from incident_commander.agent.investigation import (
    _DEFAULT_MAX_ITERATIONS,
    make_branch_prober,
    make_llm_investigate,
)
from incident_commander.agent.loop import run_to_completion
from incident_commander.agent.orchestrator import TRANSITIONS, Transition
from incident_commander.agent.planner_context import VERIFY_JUDGE_MARKER
from incident_commander.agent.reflection import CRITIC_ROLE
from incident_commander.agent.remediation import (
    make_llm_plan,
    make_llm_verify,
    make_remediate,
)
from incident_commander.agent.run_reporting import (
    ReportingCheckpointer,
    RunReporter,
    ToolCallLog,
)
from incident_commander.agent.run_reporting import (
    summarize as summarize_reporting,
)
from incident_commander.agent.search import SEARCH_IS_RECORDED_MODE_ONLY
from incident_commander.agent.selection import SELECTOR_ROLE
from incident_commander.agent.state import BudgetLedger, EvidenceEntry, IncidentState, RunState
from incident_commander.agent.strategies.knobs import StrategyKnobs
from incident_commander.agent.strategies.names import StrategyName
from incident_commander.agent.strategies.protocol import InvestigationStrategy
from incident_commander.agent.strategies.records import StepRecord, StepSink
from incident_commander.agent.strategies.registry import STRATEGIES
from incident_commander.agent.thinking import PlannerLog
from incident_commander.config import ChaosTokenNotConfigured, ModelRole, Settings
from incident_commander.llm.client import LLMClient, LLMClientProtocol, LLMError, preflight_auth
from incident_commander.llm.fakes import CannedLLMClient
from incident_commander.llm.repair import (
    OUTPUT_INVALID_PREFIXES,
    PLANNER_OUTPUT_INVALID_CLASS,
)
from incident_commander.persistence.memory import InMemoryCheckpointer
from incident_commander.tools.mcp_client import (
    LabProbeCapableClient,
    LabProbeClient,
    LabProbeRefused,
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
# The three directories above hold VERSIONED files, exclusive-create, never overwritten
# (invariant 9); ``evals/artifacts.py`` resolves the newest by the stamp in the FILENAME.
# ``evals/runs/<invocation_id>/`` archives a run incrementally, with ``report.json`` written
# LAST as the completion marker: without one it is a killed run whose files are still evidence
# (ADR 0017). Files are locked read-only, the tree sealed with the marker (ADR 0021).
_RUNS_DIR = _REPO_ROOT / "evals" / "runs"
_TRACE_DIR_ENV = "EVAL_TRACE_DIR"


_UNKNOWN: Final[str] = "unknown"
_COMPOSE_FILE = _REPO_ROOT / "demo" / "compose.yml"
# The compose service whose ``image:`` line the digest is READ FROM, so there is no second
# copy to keep in step (C-10).
_PLATFORM_SERVICE: Final[str] = "platform"
# How many scenario names ``RunReport.non_closing_reason`` spells out before counting the rest.
_NON_CLOSING_NAMES_SHOWN: Final[int] = 5
# Where a failed chaos teardown records that the shared world is dirty. On DISK because what
# it protects is the NEXT invocation, which would read a standing fault as its baseline. The
# durable record of the same event is ``ScenarioOutcome.teardown_error``.
_CHAOS_BLOCK_PATH = _REPO_ROOT / "evals" / ".chaos-teardown-block.json"


class ExecutionMode(StrEnum):
    """How the world under a run was produced.

    ``RECORDED`` replays one moment of the live world from disk with the model calls real
    (ADR 0043); ``REHEARSAL`` is the reverse — real platform, scripted planner (ADR 0069).
    Members are APPENDED and never borrowed, because every reader counting live rows asks
    ``== "live"`` and archived reports are read back against this enum.
    """

    CANNED = "canned"
    LIVE = "live"
    RECORDED = "recorded"
    REHEARSAL = "rehearsal"


#: Who seeded the fault when this runner did not (``--world-already-faulted``, ADR 0075).
#: Named once, because the archive field, the printed line and the grader must match on it.
EXTERNAL_CHAOS_SEEDER: Final[str] = "demo_live"

#: Who wrote the alert a run was paged with (owner decision O-36, platform ADR 0039).
#: ``"scenario"`` is the default and is true of every row ever archived: the YAML's
#: ``alert:`` block. ``"platform"`` says the run started from an alert row the platform
#: raised on its own metric, matched by fingerprint and subject. Two named values rather
#: than a boolean, because the question a reader asks of an old report is "where did this
#: brief come from", and ``alert_from_platform: false`` answers it only by implication.
SCENARIO_ALERT_SOURCE: Final[str] = "scenario"
PLATFORM_ALERT_SOURCE: Final[str] = "platform"


class RunProvenance(BaseModel):
    """Exactly what produced one run: code, world, models, role, budgets.

    Per SCENARIO, because that is the unit a leaderboard row is built from. Every string
    field is populated or explicitly ``"unknown"`` — a claim a reader can act on.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # --- the code and the world -------------------------------------------
    # ``git rev-parse HEAD`` of this checkout, or "unknown" outside a repo.
    commander_revision: str
    # The platform image digest read out of demo/compose.yml (see above).
    platform_image_digest: str
    # --- the models and the role ------------------------------------------
    agent_model: str
    model_role: ModelRole
    judge_model: str
    # --- the approach -----------------------------------------------------
    strategy: str
    strategy_config: dict[str, Any] = {}
    # --- the run ----------------------------------------------------------
    scenario: str
    invocation_id: str
    recorded_at: datetime
    execution_mode: ExecutionMode
    # A demo REHEARSAL row: real platform, scripted planner (ADR 0069). Redundant with
    # ``execution_mode`` and carried anyway, so readers predating the mode can refuse it.
    rehearsal: bool = False
    # Who manufactured the fault when it was NOT this runner (ADR 0075). ``"demo_live"`` says
    # the world's one ``chaos.tool_invoked`` row sits before this run rather than inside it.
    chaos_seeded_by: str | None = None
    # Who wrote the brief this run was paged with (O-36). ``"scenario"`` is the YAML's
    # ``alert:`` block — the default, and true of every row archived before this field
    # existed; ``"platform"`` is an alert row the platform raised on its own metric and this
    # runner took verbatim. Not nullable, unlike ``chaos_seeded_by``: every run has an alert
    # from somewhere, so there is no "unknown" to represent.
    alert_source: str = SCENARIO_ALERT_SOURCE
    # Which alert row, when it was the platform's. Carried so a report can be joined to the
    # ``alert.raised`` audit row that started it — the one thing a fingerprint cannot name.
    alert_id: str | None = None
    # The run's OWN ledger: ``max_*`` are the budgets actually seeded (ADR 0019's per-scenario
    # cap, the protocol's .env-only ceilings), ``*_used`` are ADR 0015's four meters.
    budget: BudgetLedger


@lru_cache(maxsize=1)
def commander_revision() -> str:
    """``git rev-parse HEAD``, or ``"unknown"`` outside a git checkout.

    Never omitted and never raising: the alternative is a run that cannot start
    over a metadata read.
    """
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", "rev-parse", "HEAD"],  # noqa: S607 - PATH lookup is intended
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return _UNKNOWN
    revision = completed.stdout.strip()
    return revision if completed.returncode == 0 and revision else _UNKNOWN


@lru_cache(maxsize=1)
def platform_image_digest() -> str:
    """The platform image digest from ``demo/compose.yml``, or ``"unknown"``.

    Read from the compose file, never a constant here: a second copy goes stale two
    releases later (C-10), and naming the wrong platform is worse than naming none.
    """
    try:
        document: object = yaml.safe_load(_COMPOSE_FILE.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return _UNKNOWN
    if not isinstance(document, dict):
        return _UNKNOWN
    services = document.get("services")
    if not isinstance(services, dict):
        return _UNKNOWN
    service = services.get(_PLATFORM_SERVICE)
    if not isinstance(service, dict):
        return _UNKNOWN
    image = service.get("image")
    if not isinstance(image, str) or "@" not in image:
        # Not pinned by digest. `tests/unit/test_demo_docs.py` refuses that in CI; here it
        # is reported rather than guessed at.
        return _UNKNOWN
    return image.split("@", 1)[1]


def strategy_knobs(settings: Settings) -> StrategyKnobs:
    """The inference block the selected strategy is built with (WP-5.2).

    This is the edge where configuration is wired; nothing under ``agent/`` reads ``Settings``.
    No budget multipliers: ``factory.start_run`` applies them once and a test refuses a second.
    """
    return StrategyKnobs(
        n=settings.best_of_n,
        sample_temperature=settings.sample_temperature,
        selector_generator=settings.selector_generator.value,
        search_depth=settings.search_depth,
        search_branch=settings.search_branch,
        # The adaptive ladder's eight thresholds (WP-13.1, ADR 0061), ``None`` unless an
        # operator set one: the defaults live in the policy's own table.
        uncertainty_top1_confidence_floor=settings.uncertainty_top1_confidence_floor,
        uncertainty_top1_top2_margin_floor=settings.uncertainty_top1_top2_margin_floor,
        uncertainty_selector_uncertainty_ceiling=settings.uncertainty_selector_uncertainty_ceiling,
        uncertainty_candidate_disagreement_ceiling=(
            settings.uncertainty_candidate_disagreement_ceiling
        ),
        uncertainty_confidence_floor_after_probes=(
            settings.uncertainty_confidence_floor_after_probes
        ),
        uncertainty_contradictory_evidence_count=settings.uncertainty_contradictory_evidence_count,
        uncertainty_failed_attempt_count=settings.uncertainty_failed_attempt_count,
        uncertainty_probe_count_before_confidence_check=(
            settings.uncertainty_probe_count_before_confidence_check
        ),
    )


def search_mode_refusal(settings: Settings, *, recorded: bool) -> str | None:
    """Why this invocation may not run ``search``, or ``None`` when it may (WP-12.1).

    Checked at the edge, before a client exists or a token is spent: a strategy whose
    branches read the world can only be measured in a world that does not move (ADR 0060).
    """
    if settings.inference_strategy is not StrategyName.SEARCH or recorded:
        return None
    return SEARCH_IS_RECORDED_MODE_ONLY


def _execution_mode(*, recorded: bool, rehearsal: bool, live: bool) -> ExecutionMode:
    """Which mode produced a run, from what its legs DID — the one place that decides.

    The ORDER is the content: ``recorded`` is the narrowest true statement, ``rehearsal``
    comes ahead of ``live`` whose test its real platform leg would otherwise satisfy, and
    ``canned`` is what is left. Read by the row, the crash row and the trace record alike.
    """
    if recorded:
        return ExecutionMode.RECORDED
    if rehearsal:
        return ExecutionMode.REHEARSAL
    return ExecutionMode.LIVE if live else ExecutionMode.CANNED


def build_provenance(
    scenario_name: str,
    settings: Settings,
    *,
    model_role: ModelRole,
    invocation_id: str,
    execution_mode: ExecutionMode,
    budget: BudgetLedger,
    recorded_at: datetime | None = None,
    strategy: InvestigationStrategy | None = None,
    rehearsal: bool = False,
    chaos_seeded_by: str | None = None,
    platform_alert: PlatformAlert | None = None,
) -> RunProvenance:
    """Assemble one run's provenance record from the run's own inputs.

    Takes the LEDGER rather than re-reading settings, so the budgets are the seeded ones
    (ADR 0019). ``strategy`` is the object that made the planner calls (WP-0.2); ``None``
    is the crash path, where the configured strategy is the only honest answer.
    """
    configured = (
        STRATEGIES.create(settings.inference_strategy, strategy_knobs(settings))
        if strategy is None
        else strategy
    )
    return RunProvenance(
        commander_revision=commander_revision(),
        platform_image_digest=platform_image_digest(),
        agent_model=settings.agent_model,
        model_role=model_role,
        judge_model=settings.judge_model,
        strategy=configured.name,
        strategy_config=dict(configured.config),
        scenario=scenario_name,
        # "" reaches here from a direct ``run_scenario`` call, and an empty string reads
        # like a value, so it is recorded as "unknown" like the two reads above.
        invocation_id=invocation_id or _UNKNOWN,
        recorded_at=recorded_at or datetime.now(UTC),
        execution_mode=execution_mode,
        # Derived from the mode as well as the flag, so a caller passing one without the
        # other still produces a row that names itself a rehearsal.
        rehearsal=rehearsal or execution_mode is ExecutionMode.REHEARSAL,
        # Taken on trust, unlike ``rehearsal``: nothing here can see whether a hook fired.
        chaos_seeded_by=chaos_seeded_by,
        # Derived from the alert OBJECT rather than from a flag, so a run that asked for the
        # platform's page and did not get one cannot claim it did: there is one alert here,
        # and either it came from a row or it came from the file.
        alert_source=SCENARIO_ALERT_SOURCE if platform_alert is None else PLATFORM_ALERT_SOURCE,
        alert_id=None if platform_alert is None else platform_alert.id,
        budget=budget,
    )


class RoleAccounting(BaseModel):
    """One prompt role's share of a run's bill (plan 03 § 7.8).

    Role is the unit a strategy changes, so a run total cannot tell "the strategy cost
    more" from "the incident needed more remediation".
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: str
    # Whether this role reached the run's BudgetLedger. False for the EVALUATOR's roles
    # only: WHOSE money a call is decides this, not when it happened (WO-R3-260).
    charged_to_ledger: bool
    calls: int
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    discarded_output_tokens: int
    tokens_used: int
    usd_used: Decimal
    elapsed_ms: int


class RunAccountingRecord(BaseModel):
    """What one run cost, how long it took, and how much context it carried.

    WP-0.3's ledger gives the total only, not the accuracy/cost frontier's columns
    (divergence D3). ``reconciled`` states both sides rather than asserting an agreement.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    by_role: tuple[RoleAccounting, ...] = ()
    llm_calls: int = 0
    # From the run's own ledger, not counted again here.
    tool_calls: int = 0
    wall_seconds: float = 0.0
    # Time inside LLM calls, not ``wall_seconds``: the loop also probes, waits out ADR 0009's
    # window and grades, so conflating them would attribute a 75-second sleep to the model.
    llm_elapsed_ms: int = 0
    # Every role, the evaluator's own spend included.
    tokens_used: int = 0
    usd_used: Decimal = Decimal("0")
    # Only the roles the run's ledger was charged for.
    charged_tokens_used: int = 0
    charged_usd_used: Decimal = Decimal("0")
    ledger_tokens_used: int = 0
    ledger_usd_used: Decimal = Decimal("0")
    reconciled: bool = True
    # Zero for ``baseline``, WRITTEN rather than omitted: a reader comparing it to a
    # later strategy's row must not have to decide what a missing key meant.
    selector_calls: int = 0
    # Zero for every arm but ``reflection``, WRITTEN for ``selector_calls``'s reason.
    critic_calls: int = 0
    revised_steps: int = 0
    branch_count: int = 0
    # Zero for every arm but ``search`` (WP-12.1), WRITTEN for ``selector_calls``'s reason:
    # branches TAKEN, and the reads all branches made against the one ceiling.
    search_branches: int = 0
    search_branch_tool_calls: int = 0
    planner_steps: int = 0
    # Per step, in order, and the total beside it (plan 02 § 17).
    planner_input_tokens: tuple[int, ...] = ()
    planner_input_tokens_total: int = 0
    planner_context_chars: tuple[int, ...] = ()
    planner_context_chars_total: int = 0


def build_accounting(accounting: RunAccounting, budget: BudgetLedger) -> RunAccountingRecord:
    """Assemble one run's accounting row from the run's own measurements.

    Takes the ledger, for ``build_provenance``'s reason: the number the row reconciles
    against has to be the one the run finished with.
    """
    return RunAccountingRecord(
        by_role=tuple(
            RoleAccounting(
                role=role.role,
                charged_to_ledger=role.charged_to_ledger,
                calls=role.calls,
                input_tokens=role.input_tokens,
                output_tokens=role.output_tokens,
                cache_creation_tokens=role.cache_creation_tokens,
                cache_read_tokens=role.cache_read_tokens,
                discarded_output_tokens=role.discarded_output_tokens,
                tokens_used=role.tokens_used,
                usd_used=role.usd_used,
                elapsed_ms=role.elapsed_ms,
            )
            for role in accounting.roles
        ),
        llm_calls=accounting.llm_calls,
        tool_calls=budget.tool_calls_used,
        wall_seconds=budget.wall_seconds_used,
        llm_elapsed_ms=accounting.elapsed_ms,
        tokens_used=accounting.tokens_used,
        usd_used=accounting.usd_used,
        charged_tokens_used=accounting.charged_tokens_used,
        charged_usd_used=accounting.charged_usd_used,
        ledger_tokens_used=budget.tokens_used,
        ledger_usd_used=budget.usd_used,
        reconciled=accounting.reconciles_with(budget),
        selector_calls=accounting.selector_calls,
        critic_calls=accounting.critic_calls,
        revised_steps=accounting.revised_steps,
        branch_count=accounting.branch_count,
        search_branches=accounting.search_branches,
        search_branch_tool_calls=accounting.search_branch_tool_calls,
        planner_steps=len(accounting.steps),
        planner_input_tokens=accounting.planner_input_tokens,
        planner_input_tokens_total=sum(accounting.planner_input_tokens),
        planner_context_chars=accounting.planner_context_chars,
        planner_context_chars_total=sum(accounting.planner_context_chars),
    )


class ScenarioOutcome(BaseModel):
    """One scenario's run + grade, persisted in the aggregate report."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scenario: str
    # The benchmark's grouping keys (WP-1.4) on the ROW, because joining a report back to
    # ``evals/scenarios/`` would read today's classification against last month's run.
    template_id: str | None = None
    seed: int | None = None
    family: str | None = None
    difficulty: str | None = None
    benchmark_split: str | None = None
    final_state: IncidentState
    tool_calls_used: int
    report: GradeReport
    judge_score: JudgeScore | None = None
    # Five-bucket noise-source classification (docs/lessons/live-eval-noise-sources.md) plus
    # "passed" and "unclassified". Heuristic: where to start debugging, not a verdict.
    failure_class: str = "unclassified"
    # Why the bucket, in enough detail to act on without opening the trajectory: on INC-001
    # the class was right and the whole trace still had to be read.
    failure_class_detail: str = ""
    # Set when the briefing judge call failed: the grade stands, the judge column is missing.
    judge_error: str | None = None
    # Set when post-grade briefing enrichment failed: the deterministic briefing stands
    # and the grade holds; only the LLM-written findings/recommendation are missing.
    briefing_error: str | None = None
    # What the ChaosPlan did, setup then teardown, in firing order (WP-1.1). Evaluator-only:
    # the archive's answer to "what world was this graded in?".
    chaos_hooks: tuple[ChaosHookRecord, ...] = ()
    # A failed teardown hook: not about THIS run, whose grade stands, but about the SHARED
    # world the next run inherits. ``_CHAOS_BLOCK_PATH`` refuses that run; this says why.
    teardown_error: str | None = None
    # Run provenance (ADR 0013): which legs ran live, and whether a declared-live leg fell
    # back to canned. The defaults are load-bearing — archived reports must keep parsing.
    live_mcp: bool = False
    live_llm: bool = False
    degraded: bool = False
    # What produced this row (WP-0.3): revision, digest, model and role, strategy, budgets.
    # ``None`` means "predates the record", which every archived report does.
    provenance: RunProvenance | None = None
    # What the run cost, by role and by step (WP-2.3). ``None`` means "no measurement exists",
    # never "this run was free", which a zeroed record would have said.
    accounting: RunAccountingRecord | None = None
    # What the REPLAY did (WP-3.3), in ``RecordedMCPClient.summary()``'s own shape so that
    # vocabulary has one home. The miss count is load-bearing: it sets ``degraded``.
    replay: dict[str, Any] | None = None


class _GroupingKeys(TypedDict):
    """The five benchmark keys, typed so ``**`` unpacking stays checked."""

    template_id: str | None
    seed: int | None
    family: str | None
    difficulty: str | None
    benchmark_split: str | None


def _grouping_keys(scenario: Scenario) -> _GroupingKeys:
    """The benchmark keys a ``ScenarioOutcome`` carries, from the scenario.

    One function for the clean row and the crash row: a crash row missing its keys
    would drop out of exactly the per-family counts that show a family crashing.
    """
    return {
        "template_id": scenario.template_id,
        "seed": scenario.seed,
        "family": scenario.family.value if scenario.family else None,
        "difficulty": scenario.difficulty.value if scenario.difficulty else None,
        "benchmark_split": scenario.benchmark_split.value,
    }


class UngradedScenario(BaseModel):
    """A scenario that was abandoned before any grade could exist.

    Beside ``outcomes``, not inside it: there is no honest ``GradeReport`` for a run that
    never happened, and a red "crashed" row would enter the pass rate (`bb1fa70abb4c`).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    scenario: str
    #: What stage abandoned it. ``chaos_setup`` is the only value today.
    phase: str = "chaos_setup"
    reason: str


class RunReport(BaseModel):
    """Aggregate output, written to ``evals/reports/report.<stamp>.<invocation_id>.json``.

    Resolve the current one with ``evals.artifacts.newest("report")`` — never
    by globbing or by mtime.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    generated_at: datetime
    total: int
    passed: int
    failed: int
    judged_count: int = 0
    judge_useful_count: int = 0
    judge_mean_overall: float | None = None
    invocation_id: str = ""
    # ``None`` = pre-schema "unknown", distinct from 0 = "verified fully live": the committed
    # baseline was a 32/37-degraded canned run, which a 0 default would call fully live.
    degraded_count: int | None = None
    # Which --only filters produced this report, so filtered runs self-describe. Empty = full.
    only_patterns: tuple[str, ...] = ()
    # Whether this report may CLOSE a phase (plan 03 § 14). Tri-state for
    # ``degraded_count``'s reason, and the validator below refuses a ``True`` over its own rows.
    closing: bool | None = None
    outcomes: tuple[ScenarioOutcome, ...]
    # Scenarios whose fault world could not be built (WP-1.1). Defaulted for
    # ``degraded_count``'s reason: every archived report predates the field.
    ungraded: tuple[UngradedScenario, ...] = ()

    @property
    def contaminated_scenarios(self) -> tuple[str, ...]:
        """Rows whose chaos teardown failed — the shared world may be dirty."""
        return tuple(o.scenario for o in self.outcomes if o.teardown_error is not None)

    @property
    def development_scenarios(self) -> tuple[str, ...]:
        """Scenarios in this report that ran under the development role."""
        return tuple(
            outcome.scenario
            for outcome in self.outcomes
            if outcome.provenance is not None
            and outcome.provenance.model_role is ModelRole.DEVELOPMENT
        )

    @property
    def rehearsal_scenarios(self) -> tuple[str, ...]:
        """Rows produced by a demo rehearsal — the real platform, a scripted planner.

        Asked off the ROWS, never a report-level flag, like ``development_scenarios`` beside
        it: both answer "which rows in here are not the measurement they look like" (ADR 0069).
        """
        return tuple(
            outcome.scenario
            for outcome in self.outcomes
            if outcome.provenance is not None and outcome.provenance.rehearsal
        )

    @property
    def non_closing_reason(self) -> str:
        """Why this report cannot close a phase, or "" if it can.

        A sentence rather than a flag: the reader's next question is always "which runs?".
        """
        if self.closing is None:
            return "predates model roles (no run in it records one)"
        if self.closing:
            return ""
        if rehearsed := self.rehearsal_scenarios:
            # Ahead of the role: no model produced these rows at all (ADR 0069), so
            # "re-run under the benchmark role" would be the wrong instruction.
            shown = ", ".join(rehearsed[:_NON_CLOSING_NAMES_SHOWN])
            remainder = len(rehearsed) - _NON_CLOSING_NAMES_SHOWN
            if remainder > 0:
                shown = f"{shown} (+{remainder} more)"
            return (
                f"{len(rehearsed)} run(s) are demo rehearsals — the real platform under a "
                f"scripted planner, not a measurement of the agent: {shown}"
            )
        development = self.development_scenarios
        if development:
            # Bounded: 40 names is a sentence nobody finishes. The count is exact, the
            # list a sample, and ``development_scenarios`` has them all.
            shown = ", ".join(development[:_NON_CLOSING_NAMES_SHOWN])
            remainder = len(development) - _NON_CLOSING_NAMES_SHOWN
            if remainder > 0:
                shown = f"{shown} (+{remainder} more)"
            return (
                f"{len(development)} run(s) were made under the "
                f"{ModelRole.DEVELOPMENT.value} model role: {shown}"
            )
        return "marked non-closing"

    @model_validator(mode="after")
    def _closing_cannot_outrank_its_own_rows(self) -> RunReport:
        """No closing report may carry a development run or a rehearsal (ADR 0069).

        One direction only: a report with neither can still be non-closing for a reason
        outside this model. Backstop for a report assembled by hand or read from an archive.
        """
        if self.closing and self.rehearsal_scenarios:
            raise ValueError(
                "closing=True but these runs are demo rehearsals — the real platform "
                f"under a scripted planner: {', '.join(self.rehearsal_scenarios)}. No "
                "phase closes on a run the model did not make (ADR 0069)."
            )
        if self.closing and self.development_scenarios:
            raise ValueError(
                "closing=True but these runs were made under the "
                f"{ModelRole.DEVELOPMENT.value} model role: "
                f"{', '.join(self.development_scenarios)}. A phase report containing a "
                "development run is non-closing (plan 03 section 14) — re-run them under "
                "--model-role benchmark rather than marking the report closing."
            )
        return self


class Trajectory(BaseModel):
    """Per-run checkpoint log, written to ``evals/trajectories/<scenario>.<stamp>.<inv>.json``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scenario: str
    incident_id: str
    checkpoints: tuple[RunState, ...]
    # Which invocation produced this, so a mixed-vintage directory self-describes even with
    # the filenames lost — how Run 001's live trajectories were erased (F-003).
    invocation_id: str = ""


@dataclass(frozen=True)
class ScenarioResult:
    """What ``run_scenario`` returns — outcome is aggregated; trajectory + briefing are per-run."""

    outcome: ScenarioOutcome
    trajectory: Trajectory
    briefing: EscalationBriefing


def _is_offline_placeholder(url: str) -> bool:
    # Exact-host match, not substring: "https://eval.local.evil.example" counts as live (S-09).
    return urlparse(url).hostname == _EVAL_PLACEHOLDER_HOST


def _is_offline_api_key(key: str) -> bool:
    return key in {_EVAL_PLACEHOLDER_API_KEY, "placeholder", ""}


def _lab_probe_credential(settings: Settings) -> str | None:
    """The lab's own credential for labelling the principal guards' probes.

    The platform will not relabel a row without it (platform ADR 0038). Unset or blank
    means no label: the probes still run, their rows read as the agent's, and
    ``_lab_probe_note`` says so out loud.
    """
    token = settings.platform_chaos_token
    if token is None or not token.get_secret_value().strip():
        return None
    return token.get_secret_value()


def _lab_probe_note(lab_probe_token: str | None) -> str:
    """One line saying whether the guards' own audit rows are the lab's or the agent's."""
    if lab_probe_token is not None:
        return (
            "principal guard: every probe above is labelled lab.probe in the platform "
            "audit (the lab's own credential travels with it), so none of them reads "
            "as the agent's work"
        )
    return (
        "principal guard: probes NOT labelled — PLATFORM_CHAOS_TOKEN is unset, so the "
        "platform records them as agent.tool_invoked and the demo page will read them "
        "as a run nobody made (finding F4)"
    )


class ScenarioCrash(Exception):
    """A scenario's run raised, wrapped with the history it had accumulated.

    ``checkpoints`` is the run's own history to the last completed transition, so the crash
    row reports what it spent instead of a hardcoded zero. Raised only from
    ``run_scenario``'s handler: seeding and precondition failures propagate unwrapped.
    """

    def __init__(self, cause: BaseException, checkpoints: tuple[RunState, ...]) -> None:
        super().__init__(f"{type(cause).__name__}: {cause}")
        self.cause = cause
        self.checkpoints = checkpoints
        # The teardown after the crash ALSO failed. Beside the cause, not folded in: the
        # crash is about the agent, this decides whether the next live run may start.
        self.teardown_error: str | None = None
        # What the run had billed when it died, by role (WP-2.3).
        self.accounting: RunAccounting | None = None
        # The ledger the spend was charged to, NOT ``final.budget``: the briefing writer bills
        # after the last checkpoint (WO-R3-260), which would read as a disagreement.
        self.ledger: BudgetLedger | None = None
        # The alert row the PLATFORM paged this run with, when it did (O-36). Carried on the
        # crash rather than re-derived from the caller's flag, and that is the point: a run
        # that died during its seeding never took an alert, so its crash row says
        # ``alert_source: scenario`` truthfully instead of claiming a page it never received.
        self.platform_alert: PlatformAlert | None = None

    @property
    def final(self) -> RunState | None:
        """The last checkpointed state, if the run got far enough to write one."""
        return self.checkpoints[-1] if self.checkpoints else None


class PreconditionFailure(RuntimeError):
    """Base: the run was abandoned before the agent started."""


class PreconditionNotMet(PreconditionFailure):
    """The world answered, and the scenario's premise is false.

    NOT a graded failure: a run that never happened has nothing to say about the agent,
    and `bb1fa70abb4c` is what recording it as one produces.
    """


class PreconditionUnverifiable(PreconditionFailure):
    """The probe never returned a usable answer, so the premise is unknown.

    Distinct from ``PreconditionNotMet``, which is a claim about the world and needs the
    world to have answered. "Answered" means the attempt that ended the polling window.
    """


def _precondition_reader(
    client: MCPClientProtocol,
    probe: PreconditionProbe,
    lab_principal_token: str | None,
) -> MCPClientProtocol:
    """``client``, with this probe's reads labelled as the lab's own (ADR 0075).

    One wrapper per probe, because ``LabProbeClient`` carries one reason. ``None`` returns the
    client untouched. The cast is unavoidable: ``MCPClientProtocol`` has no lab parameter, which
    is what keeps the AGENT's own path from relabelling its reads.
    """
    if lab_principal_token is None:
        return client
    return LabProbeClient(
        cast("LabProbeCapableClient", client),
        reason=probe_label(probe),
        principal_token=lab_principal_token,
    )


def _assert_preconditions(
    scenario: Scenario,
    client: MCPClientProtocol,
    tracer: JsonlTracer | None,
    *,
    lab_principal_token: str | None = None,
) -> None:
    """Probe the world for the scenario's premise, polling where declared.

    ``None`` for ``lab_principal_token`` keeps the pre-ADR-0075 behaviour, which every
    offline caller takes: the probes run, but the platform records them as the agent's.
    """
    for probe in scenario.expected_precondition:
        reader = _precondition_reader(client, probe, lab_principal_token)
        # Only the DECISIVE attempt speaks for the world (empty list = met, `None` = no
        # answer): latching "did any attempt answer" let a dead platform read as "no fault".
        reading: list[str] | None = None
        unreadable: list[str] = []
        # Kept only to say, in the Unverifiable message, that a now-stale
        # reading exists. Always unmet: an attempt that reads MET breaks out.
        stale_reading: list[str] = []
        for attempt in range(probe.attempts):
            if attempt:
                time.sleep(probe.delay_seconds)
            try:
                result = reader.call_tool(probe.tool, dict(probe.arguments))
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
                    "kind": TraceKind.PRECONDITION,
                    "scenario": scenario.name,
                    "tool": probe.tool,
                    "arguments": dict(probe.arguments),
                    "met": not failures,
                    "answered": answered,
                    # Whether the platform answered at any point. Diagnostic only: it
                    # must never decide Not-Met vs Unverifiable.
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

    Tolerates unparseable text: a bare ``json.loads`` escaped the polling loop, so one
    malformed block ended the run as a crash bucketed "transport", losing both the
    failing probe and the fact that it was a precondition.
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


class ChaosSetupFailed(RuntimeError):
    """A setup hook did not fire, so the scenario's world was never built.

    NOT a graded failure and not a ``ScenarioCrash``: a crash is "the agent's run
    died", this is "there was no world to run the agent in". Hence ``run_all`` records
    it as UNGRADED (plan 01 § 4: setup failure → world invalid, do not grade the agent).
    """


# ---------------------------------------------------------------------------
# The alert the PLATFORM raised (owner decision O-36, platform ADR 0039)
# ---------------------------------------------------------------------------

#: What the poll below says about itself in the platform's audit log. Labelled for
#: ``evals/world_audit.py``'s reason: the read goes out on the SMOKE account, which is a
#: service account like the agent's, so an unlabelled one lands as ``agent.tool_invoked``
#: inside the take and the demo page counts it as a call the agent made and never reported
#: (finding F4). It is the EVALUATOR asking whether the platform has paged yet.
PLATFORM_ALERT_PROBE_REASON: Final[str] = "demo: has the platform raised this scenario's alert yet"

#: The bound on the wait, deliberately expressed in seconds rather than in ticks. The rules
#: are evaluated once per metrics pass, and the pass interval is a deployment setting since
#: platform v0.6.18 — 5 s on the demo stack, 60 s by default — so a wait counted in ticks
#: would be 30 seconds here and half an hour on a stack somebody misconfigured. 120 s is two
#: passes at the DEFAULT interval: enough that a correct world is never declared quiet, short
#: enough that a rule which is switched off is found out while somebody is still watching.
_PLATFORM_ALERT_TIMEOUT_SECONDS: Final[float] = 120.0
_PLATFORM_ALERT_POLL_SECONDS: Final[float] = 3.0


@dataclass(frozen=True)
class PlatformAlert:
    """One alert the platform raised for itself, and the brief a run starts from.

    ``payload`` is the alert row's own ``extra_data`` VERBATIM — not merged with the
    scenario's YAML block, not augmented with the row's id, not re-keyed. The whole point
    of O-36 is that the agent is paged by the platform rather than by the file that grades
    it, and a payload this harness had a hand in assembling would be the old arrangement
    with an extra step. The id and the time travel BESIDE it, for the record and for the
    line the runner prints, and reach no prompt.
    """

    id: str
    fired_at: str
    payload: dict[str, Any]

    @property
    def said(self) -> str:
        """The alert in the words the runner and the demo machine print."""
        fingerprint = self.payload.get("fingerprint")
        return f"{fingerprint} (alert {self.id}, raised {self.fired_at})"


def _alert_fingerprint(alert: Mapping[str, Any]) -> str | None:
    """The fingerprint of an alert, whether it is a scenario's block or a platform row.

    The corpus writes it at the top level and a platform row nests it under
    ``extra_data`` — the same two places ``investigation.alert_subject`` looks, and for the
    same reason: reading only one of them leaves the match inert on exactly one of the two
    shapes it has to compare.
    """
    for source in (alert, alert.get("extra_data")):
        if not isinstance(source, Mapping):
            continue
        raw = source.get("fingerprint")
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return None


def _matches_scenario_alert(candidate: Mapping[str, Any], declared: Mapping[str, Any]) -> bool:
    """Whether one platform alert is the page this scenario is about.

    FINGERPRINT plus SUBJECT, and **never** ``source``. The platform's rows read
    ``kafka:consumer_lag`` and ``dlq:threshold`` where the corpus writes ``platform.kafka``
    and ``platform.dlq``, so a match on source would find nothing and a run would wait out
    its whole bound against an alert sitting in front of it (platform ADR 0039's own
    divergence note, WO-R3-339's addendum).

    The subject is read with ``investigation.alert_subject`` rather than by comparing a
    hand-listed set of keys: that function is this repo's one declaration of what an alert
    is ABOUT, the agent's own subject guard (ADR 0031/0032) routes off it, and a second
    notion of "subject" here would be a second thing to keep true. A scenario whose alert
    names no mappable subject (a whole-queue depth page) matches on the fingerprint alone,
    which is the honest reading: there is nothing narrower to ask.

    What is compared is the RESOURCE the subject names — the read that observes it, the
    argument the value belongs in, and the value — and deliberately NOT which payload FIELD
    spelled it. The platform's lag alert carries ``consumer_group`` *and* ``group`` holding the
    same value, because the receiver reads whichever it finds, while the corpus writes one or
    the other; both route to ``get_consumer_lag(consumer_group=…)``, so on a field comparison
    the right alert sitting in front of the runner would never match and the wait would run
    out. Comparing the resource also keeps the DLQ pair distinct, which is the thing a looser
    comparison would break: ``remediation_hint: replay_safe`` and ``dlq_scope: unclassified``
    both route to ``list_dlq_messages(remediation_hint=…)`` and are different incidents, and
    they differ in the value and the match mode, which are both in the comparison.
    """
    from incident_commander.agent.investigation import alert_subject

    if _alert_fingerprint(candidate) != _alert_fingerprint(declared):
        return False
    wanted = alert_subject(declared)
    if wanted is None:
        return True
    found = alert_subject(candidate)
    if found is None:
        return False
    return (found.tool_name, found.argument_field, found.value, found.match) == (
        wanted.tool_name,
        wanted.argument_field,
        wanted.value,
        wanted.match,
    )


def _platform_alerts(client: MCPClientProtocol) -> list[dict[str, Any]]:
    """Every unresolved alert the caller's tenant can see, or an empty list.

    A failed read is "no alert yet" rather than a raise: this is a polling loop, and a
    single transport blip must not end a demo that is about to work. The bound is what
    turns "not yet" into a failure, once.
    """
    try:
        result = client.call_tool("list_active_alerts", {"limit": 50})
    except MCPError:
        return []
    payload = _first_json_object(result) or {}
    alerts = payload.get("alerts")
    if not isinstance(alerts, list):
        return []
    return [dict(item) for item in alerts if isinstance(item, Mapping)]


def _await_platform_alert(
    scenario: Scenario,
    settings: Settings,
    *,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> PlatformAlert:
    """Wait until the PLATFORM has raised this scenario's alert, then hand it over.

    Read under the SMOKE principal and labelled as the lab's own, for the two reasons the
    demo machine reads the world that way (ADR 0074, platform ADR 0038): the question is
    the evaluator's, and the token that asks it cannot change the answer. Never the agent's
    token — an agent that polled for its own page would put reads in the take that the
    console then has to explain.

    Raises ``ChaosSetupFailed`` on the bound, so the run is UNGRADED rather than crashed or
    scored: an alert the platform never raised is a world that does not hold the premise,
    which is the same class of event as a hook that did not fire, and grading the agent for
    it would describe it with a pre-run failure.
    """
    from incident_commander.config import SmokeTokenNotConfigured

    declared = scenario.alert.model_dump()
    fingerprint = _alert_fingerprint(declared)
    if fingerprint is None:
        # Refused rather than matched loosely: with no fingerprint the first alert in the
        # tenant would become this run's page, and the tenant is never empty (three seeded
        # fixtures). A scenario that wants the platform's alert has to name which one.
        raise ChaosSetupFailed(
            f"scenario {scenario.name!r} declares no alert `fingerprint`, and "
            f"{ALERT_FROM_PLATFORM_FLAG} matches the platform's alert stream on fingerprint "
            "plus subject. Nothing ran: there is no way to tell this scenario's page from "
            "the three the world is seeded with."
        )
    try:
        credential = settings.require_smoke_token()
    except SmokeTokenNotConfigured as err:
        raise ChaosSetupFailed(
            f"{ALERT_FROM_PLATFORM_FLAG} reads the platform's alert stream under the "
            f"read-only principal and will not fall back to the agent's own token: {err}"
        ) from err
    client = make_client(settings, token=credential)
    labelled = LabProbeClient(
        cast("LabProbeCapableClient", client),
        reason=PLATFORM_ALERT_PROBE_REASON,
        principal_token=credential,
    )
    subject = alert_subject_note(declared)
    try:
        deadline = monotonic() + _PLATFORM_ALERT_TIMEOUT_SECONDS
        seen = 0
        while True:
            candidates = _platform_alerts(labelled)
            seen = len(candidates)
            for candidate in candidates:
                if not _matches_scenario_alert(candidate, declared):
                    continue
                extra = candidate.get("extra_data")
                if not isinstance(extra, Mapping) or not extra:
                    # The row matched on what the SUMMARY carries but has no payload, so
                    # there is no brief to start a run from. Loud, because the alternative
                    # is a run whose alert is three fields wide.
                    raise ChaosSetupFailed(
                        f"the platform's {fingerprint!r} alert carries no payload "
                        "(`extra_data` absent or empty), so there is nothing for the run "
                        "to be paged with. Nothing ran."
                    )
                return PlatformAlert(
                    id=str(candidate.get("id", "")),
                    fired_at=str(candidate.get("fired_at", "")),
                    payload=dict(extra),
                )
            if monotonic() >= deadline:
                break
            print(
                f"  waiting for the platform to raise {fingerprint!r}{subject} — "
                f"{seen} active alert(s), none of them this one"
            )
            sleep(_PLATFORM_ALERT_POLL_SECONDS)
    finally:
        client.close()
    raise ChaosSetupFailed(
        f"the platform did not raise a {fingerprint!r} alert{subject} within "
        f"{_PLATFORM_ALERT_TIMEOUT_SECONDS:.0f}s ({seen} active alert(s) at the last look). "
        f"{ALERT_FROM_PLATFORM_FLAG} takes the run's page from the platform's own rule "
        "instead of the scenario file, so with no alert there is no run — check that the "
        "fault is really in the world, that `alert_rules_enabled` is on, and that the "
        "breach is past the rule's threshold. Nothing ran and nothing was graded; this says "
        "nothing about the agent."
    )


def alert_subject_note(alert: Mapping[str, Any]) -> str:
    """`` on <field>=<value>`` for the waits' output, or ``""`` when the alert names none."""
    from incident_commander.agent.investigation import alert_subject

    subject = alert_subject(alert)
    return "" if subject is None else f" on {subject.alert_field}={subject.value!r}"


class ChaosHookRecord(BaseModel):
    """What one chaos invocation did, recorded evaluator-side.

    Reachable from ``ScenarioOutcome`` and the archive, never from ``RunState``, the evidence
    ledger or a prompt: telling the agent which faults were seeded would measure recall of
    the answer key (WP-1.3 tests that boundary).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    phase: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    ok: bool = True
    result: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    # WP-14.1's evaluator timeline: when the hook was sent, and when the fault it seeded
    # recovers ON ITS OWN — what the false-attribution grade compares a verdict against.
    # Stamped only for a DERIVED TTL (``ttl_from_windows``); all three default for archives.
    fired_at: datetime | None = None
    ttl_seconds: int | None = None
    expires_at: datetime | None = None


#: ``ChaosHookRecord.phase`` for a hook that MANUFACTURED the world, as
#: opposed to one that put it back.
CHAOS_SETUP_PHASE: Final[str] = "setup"


def seeded_chaos(records: Iterable[ChaosHookRecord]) -> bool:
    """Did this run manufacture its own fault world? (INC-003's rule needs it.)

    Successful SETUP records only — a teardown says the world was put BACK. Reads the
    RECORDS, not ``Scenario.seeds_chaos``, which may have gained or lost a hook since.
    """
    return any(record.phase == CHAOS_SETUP_PHASE and record.ok for record in records)


def _timeline_of(hook: ChaosHook, arguments: Mapping[str, Any], *, phase: str) -> dict[str, Any]:
    """The evaluator's timeline for one hook that fired, or an empty update (WP-14.1).

    Stamped only for a SETUP hook whose TTL was derived. The clock is read AFTER the call
    returns, so the recorded expiry is at or after the real one and the grade errs toward
    NOT calling false attribution — a false red there is INC-001 in a new dimension.
    """
    if hook.ttl_from_windows is None or phase != CHAOS_SETUP_PHASE:
        return {}
    ttl = arguments.get(TTL_ARGUMENT)
    if not isinstance(ttl, int):  # pragma: no cover — the resolver always writes an int
        return {}
    fired_at = datetime.now(UTC)
    return {
        "fired_at": fired_at,
        "ttl_seconds": ttl,
        "expires_at": fired_at + timedelta(seconds=ttl),
    }


def temporal_timing_refusal(scenario: Scenario, settings: Settings) -> str | None:
    """Why this run's knobs cannot carry this temporal template — or ``None`` (WP-14.1).

    Two refusals. A TTL that does not outlast the run's own pre-agent cost (settle plus the
    precondition polling) measures the HARNESS, not the agent (LESSONS 2026-08-31, run B).
    An expiry positioned inside a window the knobs collapse to zero is a different
    experiment wearing the same name, so it is refused rather than warned about.
    """
    if not scenario.is_temporal:
        return None
    plan = scenario.chaos
    investigation_window = settings.investigation_reprobe_window_seconds
    verify_window = settings.verify_polling_window_seconds
    spent = plan.settle_seconds + scenario.precondition_window_seconds
    knobs = (
        f"INVESTIGATE_REPROBE_ATTEMPTS={settings.investigate_reprobe_attempts}, "
        f"INVESTIGATE_REPROBE_DELAY_SECONDS={settings.investigate_reprobe_delay_seconds}, "
        f"VERIFY_PROBE_ATTEMPTS={settings.verify_probe_attempts}, "
        f"VERIFY_PROBE_DELAY_SECONDS={settings.verify_probe_delay_seconds}"
    )
    for hook in plan.setup:
        derivation = hook.ttl_from_windows
        if derivation is None:
            continue
        collapsed = [
            name
            for name, multiple, window in (
                ("investigation", derivation.investigation_multiple, investigation_window),
                ("verify", derivation.verify_multiple, verify_window),
            )
            if multiple > 0.0 and window <= 0.0
        ]
        if collapsed:
            return (
                f"scenario {scenario.name!r} hook {hook.name!r} positions its expiry "
                f"inside the {', '.join(collapsed)} window, and this run's knobs "
                f"collapse that window to zero ({knobs}). The derived TTL would fall "
                "back to its floor and stop tracking the window it was written "
                "against, which makes this a different experiment wearing the same "
                "name. Set the live probe knobs (docs/runbook.md, environment variable "
                "knobs) or do not run this template."
            )
        ttl = derivation.seconds(
            precondition_window=scenario.precondition_window_seconds,
            investigation_window=investigation_window,
            verify_window=verify_window,
        )
        if ttl > spent:
            continue
        return (
            f"scenario {scenario.name!r} hook {hook.name!r} resolves to "
            f"ttl_seconds={ttl} under this run's knobs ({knobs}), and the run spends "
            f"{spent:g}s before the agent's first probe (settle "
            f"{plan.settle_seconds:g}s + preconditions "
            f"{scenario.precondition_window_seconds:g}s). The fault would be gone "
            "before the agent could observe it, so the run would grade the agent for "
            "missing something that had already expired. Raise the derivation's "
            "floor_seconds, shorten the precondition, or run with the live knobs this "
            "template was derived against (docs/runbook.md, environment variable knobs)."
        )
    return None


def self_recovery_at(records: Iterable[ChaosHookRecord]) -> datetime | None:
    """When this run's fault world starts putting itself back, or ``None`` (WP-14.1).

    The EARLIEST expiry among the setup hooks that fired: the first fault to expire is the
    first recovery an agent could mistake for its own work. Off the RECORDS, like ``seeded_chaos``.
    """
    expiries = [
        record.expires_at
        for record in records
        if record.phase == CHAOS_SETUP_PHASE and record.ok and record.expires_at is not None
    ]
    return min(expiries) if expiries else None


def _invoke_plan_hook(
    scenario: Scenario,
    hook: ChaosHook,
    settings: Settings,
    tracer: JsonlTracer | None,
    *,
    phase: str,
) -> ChaosHookRecord:
    """Fire one hook under the chaos principal and record what happened.

    A literal second credential since v0.6.5: on the agent's own token the chaos-audit
    filter was inert and ``list_audit_events`` handed the agent the hook (G3, O-4). Never
    raises for a hook failure, because the callers disagree about what one means; a
    MISSING credential does raise, because nothing was attempted.
    """
    # A derived TTL (WP-14.1) is resolved here, from this run's own knobs, so the seeded
    # value and the recorded timeline are the same number by construction.
    arguments = hook.seeded_arguments(
        precondition_window=scenario.precondition_window_seconds,
        investigation_window=settings.investigation_reprobe_window_seconds,
        verify_window=settings.verify_polling_window_seconds,
    )
    record = ChaosHookRecord(phase=phase, name=hook.name, arguments=arguments)
    chaos_token = settings.require_chaos_token()
    try:
        result = invoke_chaos_hook(
            str(settings.platform_mcp_url),
            chaos_token,
            hook.name,
            arguments,
        )
    except ChaosInvocationError as err:
        # Verbatim, because ``err`` carries the platform's refusal NAME (evals/chaos_hooks.py):
        # "poison_fixture_name_in_use: reset, do not retry" beats an anonymous -32011.
        record = record.model_copy(update={"ok": False, "error": str(err)})
    else:
        record = record.model_copy(
            update={"result": result, **_timeline_of(hook, arguments, phase=phase)}
        )
    if tracer is not None:
        tracer.write(
            {
                # One kind for both halves of a plan, with ``phase`` saying which: the
                # record is the same shape either way.
                "kind": TraceKind.CHAOS_SETUP,
                "scenario": scenario.name,
                "phase": phase,
                "hook": hook.name,
                "arguments": arguments,
                **(
                    {"expires_at": record.expires_at.isoformat()}
                    if record.expires_at is not None
                    else {}
                ),
                "result": record.result,
                **({"error": record.error} if record.error is not None else {}),
            }
        )
    return record


def _seed_chaos_plan(
    scenario: Scenario,
    plan: ChaosPlan,
    settings: Settings,
    tracer: JsonlTracer | None,
) -> tuple[ChaosHookRecord, ...]:
    """Run the plan's setup hooks in declared order; stop at the first failure.

    Order is part of the declaration: a cascade is only the world it claims to be if its
    second fault lands on the first, which is also why a failure stops the rest.
    """
    records: list[ChaosHookRecord] = []
    for position, hook in enumerate(plan.setup, start=1):
        record = _invoke_plan_hook(scenario, hook, settings, tracer, phase=CHAOS_SETUP_PHASE)
        records.append(record)
        if not record.ok:
            raise ChaosSetupFailed(
                f"scenario {scenario.name!r} chaos_setup {hook.name!r} "
                f"(setup hook {position} of {len(plan.setup)}) failed: {record.error}. "
                "The fault world was never manufactured, so the agent was not run and "
                "nothing was graded — this says nothing about the agent."
            )
    return tuple(records)


def _teardown_chaos_plan(
    scenario: Scenario,
    plan: ChaosPlan,
    settings: Settings,
    tracer: JsonlTracer | None,
) -> tuple[tuple[ChaosHookRecord, ...], str | None]:
    """Run every teardown hook; return the records and a combined failure text.

    Never raises and never stops early: the run may already carry an exception this would
    replace, and a later compensator may still work. ``ChaosTokenNotConfigured`` becomes the
    same text, because the right outcome is the dirty-world latch the caller writes from it.
    """
    records: list[ChaosHookRecord] = []
    failures: list[str] = []
    for hook in plan.teardown:
        try:
            record = _invoke_plan_hook(scenario, hook, settings, tracer, phase="teardown")
        except ChaosTokenNotConfigured as err:
            failures.append(f"{hook.name}: {err}")
            continue
        records.append(record)
        if not record.ok:
            failures.append(f"{hook.name}: {record.error}")
    if not failures:
        return tuple(records), None
    return tuple(records), (
        f"scenario {scenario.name!r} chaos teardown failed for "
        f"{len(failures)} of {len(plan.teardown)} hook(s): {'; '.join(failures)}"
    )


def chaos_block_reason(path: Path | None = None) -> str | None:
    """Why live runs are blocked, or ``None`` when the world is believed clean.

    Reads the latch a failed teardown wrote. A malformed file still BLOCKS: "we cannot tell
    what went wrong" does not read as "carry on spending".
    """
    path = path or _CHAOS_BLOCK_PATH
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text())
    except (OSError, ValueError):
        return f"{_repo_relative(path)} exists but could not be read"
    if isinstance(payload, dict) and isinstance(payload.get("reason"), str):
        scenario = payload.get("scenario")
        when = payload.get("recorded_at")
        prefix = f"{scenario} at {when}: " if isinstance(scenario, str) else ""
        return f"{prefix}{payload['reason']}"
    return f"{_repo_relative(path)} exists"


def write_chaos_block(
    scenario_name: str,
    reason: str,
    *,
    invocation_id: str = "",
    path: Path | None = None,
    recorded_at: datetime | None = None,
) -> Path:
    """Latch "the shared world is dirty" where the next invocation will see it."""
    path = path or _CHAOS_BLOCK_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "scenario": scenario_name,
                "invocation_id": invocation_id,
                "recorded_at": (recorded_at or datetime.now(UTC)).isoformat(),
                "reason": reason,
            },
            indent=2,
        )
    )
    return path


def clear_chaos_block(path: Path | None = None) -> str | None:
    """Drop the latch, returning what it said — or ``None`` if there was none.

    ``make eval-reset`` restores the world; this only records that it happened, and runs as
    that recipe's LAST line so a reset that failed part-way never clears the latch.
    """
    path = path or _CHAOS_BLOCK_PATH
    reason = chaos_block_reason(path)
    if reason is None:
        return None
    path.unlink(missing_ok=True)
    return reason


def _step_sink(tracer: JsonlTracer) -> StepSink:
    """Send each planner step's ``StepRecord`` to the trace store (WP-2.1).

    One append-only JSONL line per step, joinable to the invocation's ``llm`` and ``mcp``
    records by ``call_id`` (invariant 9, F-002). Wired for canned runs too: their DECISIONS
    are real research data (divergence D1).
    """

    def sink(record: StepRecord) -> None:
        tracer.write({"kind": TraceKind.STEP, **record.as_trace_record()})

    return sink


def _replay_record(
    replay_client: RecordedMCPClient | None,
    recorded_world: Path | None,
    handoff: RecordedHandoff,
    final: RunState,
    scenario: Scenario,
) -> dict[str, Any] | None:
    """The ``replay`` row on a recorded outcome, or ``None`` on every other run.

    Three things a reader cannot get elsewhere: WHICH WORLD, by fingerprint as well as
    path, because that is what ``make world-drift`` compares; HOW COMPLETE the replay
    was, since a miss makes the run incomparable; and THE PLAN when truncated, because
    ``ACTION`` stays not-applicable and "grade the plan" (04:117) needs somewhere.
    """
    if replay_client is None or recorded_world is None:
        return None
    record: dict[str, Any] = dict(replay_client.summary())
    record["recording"] = _repo_relative(recorded_world)
    record["world_label"] = replay_client.world.world.label
    record["findings"] = [
        {"kind": f.kind, "subject": f.subject, "detail": f.detail}
        for f in replay_client.world.findings
    ]
    record["truncated_at_planning_handoff"] = handoff.fired
    # ``RunState.remediation_plan`` is a plain mapping on the checkpoint, read by key: the
    # row needs the three values, not ``remediation.py``'s schema.
    plan = final.remediation_plan
    planned_tool = None if plan is None else plan.get("action_tool")
    expected = tuple(scenario.expectation.expected_action_tools)
    record["plan"] = {
        "expected_action_tools": list(expected),
        "planned_action_tool": planned_tool,
        "planned_action_arguments": None if plan is None else plan.get("action_arguments"),
        "planned_verify_tool": None if plan is None else plan.get("verify_tool"),
        # ``None``, not ``False``, when there is nothing to compare: "there was no plan"
        # is a different fact from "the plan was wrong".
        "matches_expected": (
            None if (planned_tool is None or not expected) else planned_tool in expected
        ),
    }
    return record


#: What a recorded run's evidence ledger records where it stopped. Named once, because the
#: trajectory, the ``replay`` row and the truncation test all compare against it.
RECORDED_HANDOFF_REASON: Final[str] = (
    "recorded mode: stopped at the PLANNING handoff. A recording has no state to "
    "change and no audit log to observe a change in, so the plan was made and not "
    "executed; OUTCOME, ACTION and SAFETY are not graded for this run."
)


class RecordedHandoff:
    """Ends a recorded run where the plan is made, before anything is executed.

    Installed for BOTH acting successors on EVERY recorded run: an agent reaching one
    anyway would hand the replay client a Tier-1 call, which is refused and crashes
    (ADR 0044). Stops at ``ESCALATED`` rather than inventing a state (ADR 0002).
    """

    def __init__(self) -> None:
        self.fired = False

    def __call__(self, run_state: RunState, at: datetime) -> RunState:
        self.fired = True
        entry = EvidenceEntry(
            tool_name="_recorded_handoff",
            arguments={},
            result_summary=RECORDED_HANDOFF_REASON,
            timestamp=at,
        )
        return run_state.model_copy(
            update={
                "state": IncidentState.ESCALATED,
                "updated_at": at,
                "evidence": (*run_state.evidence, entry),
            }
        )


def recorded_not_applicable(*, truncated: bool) -> dict[GradeDimension, str]:
    """Which dimensions a recorded run makes no claim about, and why (plan 02 §189).

    ``OUTCOME`` (the handoff chose the terminal state), ``ACTION`` (nothing executed) and
    ``SAFETY`` (invariant 6 grades from an audit log a recording has none of), plus
    ``EVIDENCE`` when truncated, because those claims read the action tool's own response.
    ``BUDGET`` and ``ROOT_CAUSE`` stay graded.
    """
    reasons = {
        GradeDimension.OUTCOME: not_applicable_detail(
            "recorded",
            "a replayed world's run is stopped by the harness at the handoff, so "
            "its terminal state is not the agent's outcome (plan 02 §189)",
        ),
        GradeDimension.ACTION: not_applicable_detail(
            "recorded", "no action is executed against a recording"
        ),
        GradeDimension.SAFETY: not_applicable_detail(
            "recorded",
            "safety is graded from the platform audit log as ground truth "
            "(invariant 6) and a recording has none",
        ),
    }
    if truncated:
        reasons[GradeDimension.EVIDENCE] = not_applicable_detail(
            "recorded",
            "the run stopped at the PLANNING handoff, so the action tool's own "
            "response — which this scenario's evidence claims read — was never "
            "produced",
        )
    return reasons


#: Namespace for deriving a run's reported id from its invocation id. A fixed constant, so
#: the derivation is reproducible by anyone holding the invocation id and the scenario name.
_RUN_ID_NAMESPACE: Final[uuid.UUID] = uuid.UUID("6f3f0f29-0d9e-5a1e-9a0a-1f1bd5a9a7c1")


def _reporting_run_id(invocation_id: str, scenario: str) -> uuid.UUID:
    """The ``run_id`` every report of one run shares (ADR 0068).

    A UUID5 over (invocation id, scenario), because ``invocation_id`` is twelve hex
    characters while ``report_agent_run.run_id`` is typed ``format: uuid`` — so it is valid,
    and recomputable from two facts the archive already records.
    """
    return uuid.uuid5(_RUN_ID_NAMESPACE, f"{invocation_id}:{scenario}")


def _briefing_prose(briefing: EscalationBriefing) -> str | None:
    """The written-out half of the briefing, for the console's own prose slot.

    ``findings`` and ``recommendation`` are the two strings the briefing writer fills; the
    rest of the briefing is deterministic and travels as structure. ``None`` when unenriched.
    """
    parts = [
        f"{label}: {text}"
        for label, text in (
            ("Findings", briefing.findings),
            ("Recommendation", briefing.recommendation),
        )
        if text
    ]
    return "\n\n".join(parts) if parts else None


def run_scenario(
    scenario: Scenario,
    settings: Settings,
    clock: Callable[[], datetime] | None = None,
    mcp_token: str | None = None,
    invocation_id: str = "",
    model_role: ModelRole = ModelRole.DEVELOPMENT,
    recorded_world: Path | None = None,
    rehearsal: bool = False,
    world_already_faulted: bool = False,
    alert_from_platform: bool = False,
) -> ScenarioResult:
    """Drive one scenario end-to-end and grade the result.

    Four narrow modes: ``recorded_world`` replays a recorded world (no seeding, stops at the
    PLANNING handoff), ``rehearsal`` keeps the platform live and scripts the model (ADR 0069),
    ``world_already_faulted`` skips seeding the hooks fired elsewhere (ADR 0075), and
    ``alert_from_platform`` takes the run's brief from the platform's own alert stream rather
    than the scenario's ``alert:`` block (O-36, ADR 0076) — live-only, because a canned world
    has no alert stream, and it moves no grade: the graders key on the terminal state, the
    audit log and the readings, never on the brief.
    """
    tick = clock or (lambda: datetime.now(UTC))
    now = tick()

    # The scenario's allow-list projection (WP-1.3): ``ground_truth`` and the other
    # evaluator-only fields are not on it, so they cannot leak into a run from here.
    agent_visible = scenario.agent_visible()

    # RECORDED mode, decided once: the world is a file, so seeding, settling,
    # preconditions, teardown and the real transport are all off below.
    recorded = recorded_world is not None

    # Refused here and not only at the CLI, because ``run_all`` is also driven from tests and
    # from ``world_drift``: a run claiming both modes would have neither leg real.
    if recorded and rehearsal:
        raise ValueError(
            f"a run cannot be both {ExecutionMode.RECORDED.value} and "
            f"{ExecutionMode.REHEARSAL.value}: a recording replaces the PLATFORM and keeps "
            "the model, a rehearsal keeps the platform and replaces the MODEL. Asking for "
            "both leaves no real leg for the row to be about."
        )

    # ``search`` refused before a client exists, a hook fires or a token is spent
    # (WP-12.1, ADR 0060). ``run_all`` refuses earlier; this catches a direct call.
    refusal = search_mode_refusal(settings, recorded=recorded)
    if refusal is not None:
        raise ValueError(refusal)

    # Refused HERE and not only at the CLI, like the pair above: the platform's alert stream
    # is a live read, so a recording has nothing to poll, and a flag that quietly did nothing
    # would let a row claim ``alert_source: platform`` over a brief the YAML wrote.
    if alert_from_platform and recorded:
        raise ValueError(
            f"{ALERT_FROM_PLATFORM_FLAG} cannot be combined with "
            f"{ExecutionMode.RECORDED.value} mode: a recording replays a world from a file "
            "and reaches no platform, so there is no alert stream to be paged by."
        )

    # WP-14.1 backstop to ``recordings_for``'s CLI refusal: a replayed temporal scenario
    # would claim a timeline the replay does not have.
    if recorded and (temporal := scenario.recorded_refusal) is not None:
        raise ChaosSetupFailed(temporal)

    # use_live_* means "prefer live if env is real, else canned" — nothing skips on a
    # placeholder. ``not recorded`` leads because a replay under a real ``PLATFORM_MCP_URL``
    # could seed the shared world while the row claimed to be a replay.
    live_mcp_available = (
        not recorded
        and scenario.use_live_mcp
        and not _is_offline_placeholder(str(settings.platform_mcp_url))
    )
    # ``not rehearsal`` leads for the same reason: the flag alone must be sufficient, or a
    # rehearsal under a real key spends money on camera.
    live_llm_available = (
        not rehearsal
        and scenario.use_live_llm
        and not _is_offline_api_key(settings.anthropic_api_key.get_secret_value())
    )

    # The other half of that guard, and the one that catches the QUIET case: an offline
    # ``PLATFORM_MCP_URL`` or a scenario with no live leg degrades to canned fixtures, where
    # the flag would simply not happen while the row claimed the platform had paged. Raised
    # before the tracer, the hooks and the first model call.
    if alert_from_platform and not live_mcp_available:
        raise ValueError(
            f"{ALERT_FROM_PLATFORM_FLAG} needs a live platform to be paged BY, and scenario "
            f"{scenario.name!r} would run against canned fixtures here "
            f"(use_live_mcp={scenario.use_live_mcp}, PLATFORM_MCP_URL offline="
            f"{_is_offline_placeholder(str(settings.platform_mcp_url))}). A canned world has "
            "no alert stream, so the flag would change nothing while the row claimed the "
            "platform had paged."
        )

    # Opt-in tracing: EVAL_TRACE_DIR captures every LLM + MCP call to JSONL. The hooks wire
    # into the live clients only; the per-step ``StepRecord``s (WP-2.1) are written on both.
    tracer: JsonlTracer | None = None
    trace_dir_env = os.environ.get(_TRACE_DIR_ENV)
    if trace_dir_env:
        tracer = tracer_for(scenario.name, Path(trace_dir_env))
        if invocation_id:
            # One id across the invocation, so a trace and its trajectory can be joined.
            tracer.invocation_id = invocation_id
        tracer.write(
            {
                "kind": TraceKind.SCENARIO_START,
                "scenario": scenario.name,
                "live_mcp": live_mcp_available,
                "live_llm": live_llm_available,
                "model": settings.agent_model,
                "model_role": model_role.value,
                "judge_model": settings.judge_model,
                # The same four-way answer the ROW carries below, from the same three facts, so
                # a trajectory and the row about it cannot disagree (WO-R3-287).
                "execution_mode": _execution_mode(
                    recorded=recorded,
                    rehearsal=rehearsal,
                    live=live_mcp_available or live_llm_available,
                ).value,
                "recorded_world_id": _repo_relative(recorded_world) if recorded_world else None,
            }
        )

    # The fault in one shape however the YAML spells it (a legacy ``chaos_setup``
    # normalizes to a one-hook plan), read once so setup and teardown share a tuple.
    chaos_plan = scenario.chaos
    chaos_records: tuple[ChaosHookRecord, ...] = ()
    teardown_error: str | None = None
    # The alert row the platform raised, when the caller asked to be paged by the platform
    # (O-36). Resolved inside the live branch below, after the premise is proved; ``None``
    # everywhere else, and it is the OBJECT rather than the flag that decides both the brief
    # the run starts from and what the provenance record says about it.
    platform_alert: PlatformAlert | None = None

    def _tear_down() -> str | None:
        """Compensate the plan, latch the world dirty if that failed.

        Called at each exit from the live path rather than from a ``finally``, because on the
        failing path the teardown's own failure is attached to the exception in flight.
        """
        nonlocal chaos_records
        if not live_mcp_available:
            return None
        if world_already_faulted:
            # Symmetry with the seeding this mode skipped (ADR 0075): compensating a hook that
            # never fired would add a `chaos.tool_invoked` row the console draws as a fault
            # (WO-R3-336 item 7). Whoever broke the world puts it back.
            return None
        records, error = _teardown_chaos_plan(scenario, chaos_plan, settings, tracer)
        chaos_records += records
        if error is not None:
            # Latched before any exception leaves: a killed process must still
            # find the world marked dirty.
            write_chaos_block(scenario.name, error, invocation_id=invocation_id)
        return error

    mcp_client: MCPClientProtocol
    live_mcp_client: MCPClient | None = None
    replay_client: RecordedMCPClient | None = None
    # What the reporter's steps are made of (ADR 0072): latency, outcome and the raw first
    # content block exist at the client seam and nowhere else. Built only when reporting is on.
    tool_log: ToolCallLog | None = None
    # Where the run's own reasoning is observed (ADR 0075): a planner ranking has no client
    # seam, so without this the console sees one only at the next transition.
    planner_log: PlannerLog | None = None
    if recorded_world is not None:
        # The replay clock is the run's own clock, so every age the agent computes is the one
        # the recorder observed (ADR 0044). Built first: a recording that will not load is free.
        replay_client = RecordedMCPClient.from_path(recorded_world, replay_clock=now)
        mcp_client = replay_client
        print(
            f"  replay: {_repo_relative(recorded_world)} — "
            f"{len(replay_client.world.calls)} recorded call(s), world "
            f"{str(replay_client.summary()['world_fingerprint'])[:12]}, "
            f"replay offset {int(replay_client.offset.total_seconds())}s"
        )
        # Record-time coherence findings travel inside the recording (ADR 0043) and are
        # printed on every replay. A finding is not a verdict and never refuses the run.
        for finding in replay_client.world.findings:
            print(f"  replay lint [{finding.kind}] {finding.subject}: {finding.detail}")
    elif live_mcp_available:
        # Setup hooks fire before the agent's client exists, so a seeding failure surfaces as
        # itself rather than as a downstream "read returned healthy". The teardown region opens
        # here, not after seeding succeeds: the hooks that already fired are what it compensates.
        try:
            # WP-14.1: a derived TTL that cannot outlast this run's own pre-agent cost would
            # measure the harness, so the world is not built at all.
            if (timing := temporal_timing_refusal(scenario, settings)) is not None:
                raise ChaosSetupFailed(timing)
            if world_already_faulted and chaos_plan.self_recovering:
                # The evaluator timeline (WP-14.1, ADR 0062) is read off the seeding record's
                # ``expires_at``, so a run that seeded nothing has none. Refused as a SETUP
                # failure, leaving the row ungraded rather than scored against no timeline.
                raise ChaosSetupFailed(
                    f"scenario {scenario.name!r} declares a derived TTL, and "
                    f"{WORLD_ALREADY_FAULTED_FLAG} skips the seeding its expiry is read "
                    "from. A self-expiring fault has to be fired by the run that grades "
                    "it, or the recovery clock is a number nobody measured."
                )
            if world_already_faulted:
                # ADR 0075: re-firing would add a second `chaos.tool_invoked` row and give every
                # audit-anchored reader the wrong moment for the fault. Nothing is settled
                # either — this fault landed minutes ago; the preconditions still check it.
                print(
                    f"  chaos: NOT seeded — --world-already-faulted "
                    f"({EXTERNAL_CHAOS_SEEDER} fired "
                    f"{', '.join(hook.name for hook in chaos_plan.setup) or 'no hook'} "
                    f"already); the premise is still checked"
                )
            else:
                chaos_records = _seed_chaos_plan(scenario, chaos_plan, settings, tracer)
                # One wait for the whole plan: a cascade's second-order effect is not instant.
                if chaos_plan.settle_seconds:
                    time.sleep(chaos_plan.settle_seconds)
            mcp_hook = tracer.mcp_hook() if tracer else None
            if settings.agent_run_reporting:
                tool_log = ToolCallLog()
                # Beside the JSONL tracer, never instead of it: telemetry does not get to
                # displace the run's own append-only record.
                mcp_hook = tool_log.tee(mcp_hook)
                planner_log = PlannerLog()
            live_mcp_client = make_client(
                settings,
                tracer=mcp_hook,
                token=mcp_token,
            )
            mcp_client = live_mcp_client
            # The premise, after seeding and before the first model call: a fault that was
            # never manufactured costs one read instead of a graded run (`bb1fa70abb4c`).
            if scenario.expected_precondition:
                try:
                    # On the AGENT's token, because the premise must hold for the world the
                    # agent will see — but labelled as the lab's own reads, or the console
                    # counts them as steps the agent never reported (ADR 0075, plat ADR 0038).
                    _assert_preconditions(
                        scenario,
                        live_mcp_client,
                        tracer,
                        lab_principal_token=_lab_probe_credential(settings),
                    )
                except PreconditionFailure:
                    live_mcp_client.close()
                    live_mcp_client = None
                    raise
            if alert_from_platform:
                # AFTER the premise, deliberately, and the order is the claim. The
                # precondition says the fault is in the world; this says the platform has
                # NOTICED it. An alert without the fault is a stale page and an alert before
                # the premise is a race, so the fault is established first and the page is
                # then waited for — which is also the order the demo narrates it in.
                platform_alert = _await_platform_alert(scenario, settings)
                print(f"  paged by the PLATFORM: {platform_alert.said}")
        except BaseException:
            # The world was touched, so it goes back even though nothing is graded. The return
            # value is dropped: no row carries it, and the latch is already written.
            _tear_down()
            raise
    else:
        mcp_client = CannedMCPClient(agent_visible.canned_tool_responses)

    investigation_llm: LLMClientProtocol
    selector_llm: LLMClientProtocol
    critic_llm: LLMClientProtocol
    remediation_planner_llm: LLMClientProtocol
    verification_judge_llm: LLMClientProtocol
    briefing_llm: LLMClientProtocol
    judge_llm: LLMClientProtocol
    if live_llm_available:
        api_key = settings.anthropic_api_key.get_secret_value()
        # One client per role, so each call's trace record carries its own hook label.
        investigation_llm = LLMClient(
            api_key=api_key,
            tracer=tracer.llm_hook("investigation_planner") if tracer else None,
        )
        # WP-6.2's role.
        selector_llm = LLMClient(
            api_key=api_key,
            tracer=tracer.llm_hook(SELECTOR_ROLE) if tracer else None,
        )
        # WP-9.1's role.
        critic_llm = LLMClient(
            api_key=api_key,
            tracer=tracer.llm_hook(CRITIC_ROLE) if tracer else None,
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
        selector_llm = CannedLLMClient(scenario.canned_llm_responses.get(SELECTOR_ROLE, []))
        critic_llm = CannedLLMClient(scenario.canned_llm_responses.get(CRITIC_ROLE, []))
        remediation_planner_llm = CannedLLMClient(
            scenario.canned_llm_responses.get("remediation_planner", [])
        )
        verification_judge_llm = CannedLLMClient(
            scenario.canned_llm_responses.get("verification_judge", [])
        )
        briefing_llm = CannedLLMClient(scenario.canned_llm_responses.get("briefing_writer", []))
        judge_llm = CannedLLMClient(scenario.canned_llm_responses.get("briefing_judge", []))

    # The two clients whose queues are read AFTER the run. Bound before the metering
    # wrappers, because ``has_remaining`` is the canned client's own question.
    briefing_canned = briefing_llm if isinstance(briefing_llm, CannedLLMClient) else None
    judge_canned = judge_llm if isinstance(judge_llm, CannedLLMClient) else None

    # Cost, latency and context accounting (WP-2.3): every reachable LLM client is wrapped, so
    # the split is complete by construction. ``charged_to_ledger`` draws the line at WHOSE money
    # a call is — the briefing WRITER is the agent's (ADR 0015 § 4), the JUDGE the evaluator's.
    accounting = RunAccounting()
    investigation_llm = accounting.meter(investigation_llm, "investigation_planner")
    # Charged: the selector decides the run's diagnosis, so it is the agent's own cost.
    selector_llm = accounting.meter(selector_llm, SELECTOR_ROLE)
    # Charged for the same reason: the critic decides whether the run re-plans.
    critic_llm = accounting.meter(critic_llm, CRITIC_ROLE)
    remediation_planner_llm = accounting.meter(remediation_planner_llm, "remediation_planner")
    verification_judge_llm = accounting.meter(verification_judge_llm, "verification_judge")
    briefing_llm = accounting.meter(briefing_llm, "briefing_writer")
    judge_llm = accounting.meter(judge_llm, "briefing_judge", charged_to_ledger=False)

    # The inference strategy (WP-0.2), resolved once and given to both the loop and the
    # provenance record. An unknown INFERENCE_STRATEGY raises here, before anything is spent.
    strategy = STRATEGIES.create(settings.inference_strategy, strategy_knobs(settings))
    transitions: dict[IncidentState, Transition] = dict(TRANSITIONS)
    transitions[IncidentState.INVESTIGATING] = make_llm_investigate(
        mcp_client,
        investigation_llm,
        model=settings.agent_model,
        strategy=strategy,
        # Per-step records always reach the accounting (WP-2.3 needs each step's context size)
        # and the trace store when one exists. The sink composes; it does not choose.
        record_step=accounting.step_sink(_step_sink(tracer) if tracer is not None else None),
        # Passed on every run, read by one arm each; the other arms never touch them.
        selector_llm_client=selector_llm,
        critic_llm_client=critic_llm,
        # How a ``search`` branch reads the world (WP-12.1), recorded mode only: a live world
        # moves between branches and a canned client would serve the first branch's answer
        # twice. ``None`` is what ``search`` refuses on; every other arm ignores it.
        branch_prober=(make_branch_prober(mcp_client) if recorded else None),
        # Freshness re-probe (ADR 0009) is live-only: a re-probe against canned responses
        # would eat an extra scripted answer for nothing.
        reprobe_attempts=(settings.investigate_reprobe_attempts if live_mcp_available else 0),
        reprobe_delay_seconds=settings.investigate_reprobe_delay_seconds,
        # WP-2.4's third knob at its only consumer (WO-R3-256). Unset names the loop's own
        # default rather than re-declaring it, so no second copy can drift.
        max_iterations=(
            _DEFAULT_MAX_ITERATIONS
            if settings.max_iterations_override is None
            else settings.max_iterations_override
        ),
        # Where the loop writes each ranking it accepts (ADR 0075); ``None`` unless reporting is on.
        planner_log=planner_log,
    )
    # Phase 6 remediation loop: PLANNING → REMEDIATING → VERIFYING, a client per role.
    transitions[IncidentState.PLANNING] = make_llm_plan(
        remediation_planner_llm,
        model=settings.agent_model,
        # One field for both transitions that read it (ADR 0056), so a run cannot be
        # capped at two numbers.
        max_attempts=settings.max_remediation_attempts,
    )
    transitions[IncidentState.REMEDIATING] = make_remediate(
        mcp_client,
        # Live actions outlast reads (kafka restart, DB write); canned responses are instant.
        action_timeout_seconds=(
            settings.action_tool_timeout_seconds if live_mcp_available else None
        ),
    )
    transitions[IncidentState.VERIFYING] = make_llm_verify(
        mcp_client,
        verification_judge_llm,
        # JUDGE_MODEL, not agent_model: this decides RESOLVED and was the one judgement left
        # on an unpinned model. It judges an expectation the agent wrote for itself.
        model=settings.judge_model,
        # Poll only against a real platform; one canned read is already authoritative.
        probe_attempts=settings.verify_probe_attempts if live_mcp_available else 1,
        probe_delay_seconds=settings.verify_probe_delay_seconds,
        # The loop's own clock, so each poll in a multi-minute window is stamped when it happened.
        clock=tick,
        max_attempts=settings.max_remediation_attempts,
        # Each poll's verdict, the moment the judge returns (ADR 0075).
        planner_log=planner_log,
    )
    # RECORDED mode stops where the plan is made, over BOTH transitions: an approval
    # against a recording is as meaningless as an action.
    handoff = RecordedHandoff()
    if recorded:
        transitions[IncidentState.REMEDIATING] = handoff
        transitions[IncidentState.AWAITING_APPROVAL] = handoff

    # Outside the try, so a crash can still read what the run had spent (see ScenarioCrash).
    checkpointer = InMemoryCheckpointer()
    # AGENT_RUN_REPORTING (ADR 0068): a reporter on the checkpoint seam, so a human at the
    # console can follow the run. Needs a LIVE platform and the AGENT's own client, never the
    # chaos one. The decorator checkpoints first, so this cannot cost the run a checkpoint.
    reporter: RunReporter | None = None
    reporting_checkpointer: ReportingCheckpointer | None = None
    if settings.agent_run_reporting and live_mcp_available and live_mcp_client is not None:
        reporter = RunReporter(
            live_mcp_client,
            # The run id the console keys on: `invocation_id` when the harness supplied one, so
            # the console record joins the same run's trace and trajectory.
            run_id=_reporting_run_id(invocation_id, scenario.name),
            # `run_label`, not `scenario`: the tool's input model forbids unknown fields, so a
            # wrong spelling is a refused report rather than an ignored argument.
            run_label=scenario.name,
            # Where the per-call steps come from; `None` reports everything except the steps.
            tool_log=tool_log,
            # And the rankings (ADR 0075), subscribed in the constructor.
            planner_log=planner_log,
        )
        if tool_log is not None:
            # The precondition probes went through the agent's client but are the EVALUATOR's
            # reads (ADR 0038); reporting them would credit the agent with steps it never took.
            tool_log.forget()
        reporting_checkpointer = ReportingCheckpointer(checkpointer, reporter)
    run: RunState | None = None
    # Also outside: the post-terminal briefing charge is in no checkpoint, so a crash after
    # enrichment would reconcile against a stale ledger and report a false ``COST UNRECONCILED``.
    run_ledger: BudgetLedger | None = None
    try:
        # The scenario's declared cap IS the run's ceiling (ADR 0019), not just the number it
        # is graded against: the tight-budget scenarios used to be told the fleet default.
        run = start_run(
            # The scenario's own block, unless the PLATFORM paged this run — and then the
            # alert row's payload verbatim (O-36). Resolved to one object above rather than
            # branched on here, so there is exactly one alert in this function and the
            # provenance record cannot disagree with the brief the agent read.
            agent_visible.alert if platform_alert is None else platform_alert.payload,
            settings,
            now,
            max_tool_calls=agent_visible.max_tool_calls,
        )
        final = run_to_completion(
            run,
            clock=tick,
            transitions=transitions,
            # The decorator when reporting is on, the bare store otherwise — same seam either way.
            checkpointer=(
                checkpointer if reporting_checkpointer is None else reporting_checkpointer
            ),
        )
        trajectory = Trajectory(
            invocation_id=invocation_id,
            scenario=scenario.name,
            incident_id=str(final.incident_id),
            checkpoints=tuple(checkpointer.history(final.incident_id)),
        )
        # Built before grading, because ``expect_briefing_contains`` grades the handoff as the
        # human receives it, enrichment included. Grading itself still makes no LLM call.
        briefing = render_briefing(final)
        briefing_error: str | None = None
        # The ledger after the post-terminal briefing charge (WO-R3-260), never put back on
        # ``final``: BUDGET grades from that, and a ceiling must not decide a finished outcome.
        run_ledger = final.budget
        if scenario.use_live_llm or (briefing_canned is not None and briefing_canned.has_remaining):
            try:
                briefing, run_ledger = enrich_briefing(
                    briefing, briefing_llm, model=settings.agent_model, budget=run_ledger
                )
            except (LLMError, ValidationError) as err:
                # Charge what the failed call billed, or the reconciliation breaks on exactly
                # the run that cost money for nothing.
                run_ledger = accrue_llm_error(run_ledger, err, settings.agent_model)
                # A decoration, like the judge: losing it must not void the run (ADR 0007).
                # ValidationError too, because ``CannedLLMClient`` never reaches ``_parse``.
                briefing_error = f"briefing enrichment failed: {err}"
        if reporter is not None:
            # After enrichment, so the console shows the handoff a human would receive; outside
            # the enrichment try, so a failed enrichment still reports the deterministic half.
            reporter.report_briefing(briefing, prose=_briefing_prose(briefing))
        # Which world this run was actually in (INC-003, ADR 0040). Only the runner knows:
        # ``grade()`` is a pure function of its arguments and must stay one.
        if replay_client is not None:
            # A recording states its own world (ADR 0043 § 4) and is the only authority here:
            # ``live_mcp_available`` is False, so the derivation below would strike out the one
            # dimension recorded mode exists to measure.
            replay_label = replay_client.world.world
            world_matches_ground_truth = label_describes_this_world(
                live_mcp=replay_label.live_mcp,
                chaos_seeded=replay_label.chaos_seeded,
            )
        else:
            # ``chaos_records`` holds the SETUP hooks here (teardown runs after grading), and
            # ``or world_already_faulted`` because INC-003's rule asks whether the world was
            # MANUFACTURED — it was, and the preconditions above verified it.
            world_matches_ground_truth = label_describes_this_world(
                live_mcp=live_mcp_available,
                chaos_seeded=seeded_chaos(chaos_records) or world_already_faulted,
            )
        report = grade(
            final,
            scenario.expectation,
            briefing=briefing,
            # The answer key, read here and nowhere else: the run was built from
            # ``agent_visible()`` (ADR 0038). ``None`` grades ROOT_CAUSE vacuously.
            ground_truth=(
                None if scenario.ground_truth is None else scenario.ground_truth.root_causes
            ),
            # Which world the run was in (INC-003), decided above because the answer differs
            # by mode and nobody reads a conditional inside an argument list.
            world_matches_ground_truth=world_matches_ground_truth,
            # WP-14.1's timeline, off the setup records: when the fault starts putting itself
            # back. ``None`` grades ATTRIBUTION vacuously.
            self_recovery_at=self_recovery_at(chaos_records),
            # Which dimensions this MODE cannot make a claim about (WP-3.3).
            not_applicable=(recorded_not_applicable(truncated=handoff.fired) if recorded else None),
        )
        judge_score: JudgeScore | None = None
        judge_error: str | None = None
        if scenario.use_live_llm or (judge_canned is not None and judge_canned.has_remaining):
            try:
                judge_score = judge_briefing(briefing, judge_llm, model=settings.judge_model)
            except (LLMError, ValidationError) as err:
                # A soft-quality column on an already-graded run, so losing it must not void
                # the run. ValidationError because decoding does not guarantee JudgeScore's bounds.
                judge_error = f"judge call failed: {err}"
    except Exception as exc:
        if tracer is not None:
            tracer.write(
                {
                    "kind": TraceKind.SCENARIO_END,
                    "scenario": scenario.name,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
        # The world goes back before the exception leaves, and a teardown failure rides ALONG
        # with the crash: "the agent crashed" and "the world is dirty" are two facts.
        crash = ScenarioCrash(
            exc,
            () if run is None else tuple(checkpointer.history(run.incident_id)),
        )
        if live_mcp_client is not None:
            live_mcp_client.close()
            live_mcp_client = None
        crash.teardown_error = _tear_down()
        # What the run had billed before it died — a crashed run's spend is spend, and the
        # crash row is the only place it reaches a report.
        crash.accounting = accounting
        # The ledger it was charged to; ``None`` falls back to the last checkpoint.
        crash.ledger = run_ledger
        # And which alert it was paged with, when the platform's own rule raised one: the
        # crash row names it or says nothing, never the flag's intention.
        crash.platform_alert = platform_alert
        raise crash from exc
    finally:
        if live_mcp_client is not None:
            live_mcp_client.close()
            live_mcp_client = None

    if reporter is not None:
        # Operator output, not a row field: reporting says nothing about the agent's behaviour
        # and belongs in no measurement (ADR 0068).
        print(f"  {summarize_reporting(reporter)}")
    # Before the row that reports it: the outcome carries ``teardown_error``.
    teardown_error = _tear_down()
    # ``run_ledger`` is bound by the time this path runs; the fallback is for the type checker.
    reported_ledger = final.budget if run_ledger is None else run_ledger
    failure_class, failure_class_detail = _classify_failure(report, final)
    outcome = ScenarioOutcome(
        scenario=scenario.name,
        **_grouping_keys(scenario),
        final_state=final.state,
        tool_calls_used=final.budget.tool_calls_used,
        report=report,
        judge_score=judge_score,
        failure_class=failure_class,
        failure_class_detail=failure_class_detail,
        judge_error=judge_error,
        briefing_error=briefing_error,
        chaos_hooks=chaos_records,
        teardown_error=teardown_error,
        # Provenance mirrors the tracer's scenario_start keys above.
        live_mcp=live_mcp_available,
        live_llm=live_llm_available,
        # "This row is not the measurement it looks like" (ADR 0044) — not merely "it replayed",
        # which is the mode ``execution_mode`` names. ``or rehearsal`` unconditionally, because
        # it is true of every rehearsal whatever the scenario declares (ADR 0069).
        degraded=rehearsal
        or (scenario.use_live_mcp and not live_mcp_available and not recorded)
        or (scenario.use_live_llm and not live_llm_available)
        or (replay_client is not None and replay_client.degraded),
        provenance=build_provenance(
            scenario.name,
            settings,
            model_role=model_role,
            invocation_id=invocation_id,
            # From what the legs ACTUALLY did, never from the --live flag, which says what
            # was asked for. Same function as the trace record above.
            execution_mode=_execution_mode(
                recorded=recorded,
                rehearsal=rehearsal,
                live=live_mcp_available or live_llm_available,
            ),
            # Differs from the graded ``final.budget`` by exactly the briefing writer's
            # post-terminal charge, which is the agent's cost (WO-R3-260).
            budget=reported_ledger,
            recorded_at=tick(),
            # The strategy object the loop above actually ran with.
            strategy=strategy,
            # Who fired the fault, when it was not this runner (ADR 0075): a grader counting
            # chaos rows needs this to read one row as "before the run", not "a hook missing".
            chaos_seeded_by=EXTERNAL_CHAOS_SEEDER if world_already_faulted else None,
            # Where the brief came from (O-36). The OBJECT, so ``alert_source: platform``
            # can only be written by a run that really was handed an alert row.
            platform_alert=platform_alert,
        ),
        # Reconciled against the SAME ledger the provenance carries (WP-2.3): the charged
        # split includes ``briefing_writer``, so a pre-briefing ledger reads as unreconciled.
        accounting=build_accounting(accounting, reported_ledger),
        replay=_replay_record(replay_client, recorded_world, handoff, final, scenario),
    )
    if tracer is not None:
        tracer.write(
            {
                "kind": TraceKind.SCENARIO_END,
                "scenario": scenario.name,
                "final_state": final.state.value,
                "tool_calls_used": final.budget.tool_calls_used,
                "passed": report.passed,
                "failure_class": failure_class,
            }
        )
    return ScenarioResult(outcome=outcome, trajectory=trajectory, briefing=briefing)


def _grader_drift_detail(report: GradeReport, evidence: Sequence[EvidenceEntry]) -> str:
    """Why an evidence-only red is probably the grader's fault, and where to look.

    Prints the call shapes for the tools the failing claims name — "the claim wants THIS,
    the agent used THAT" — so the reader need not open the trace, as ``4974811d236f`` did.
    """
    detail = next(
        (d.detail for d in report.dimensions if d.dimension == GradeDimension.EVIDENCE), ""
    )
    shapes: list[str] = []
    for entry in evidence:
        if entry.tool_name.startswith("_") or entry.tool_name not in detail:
            continue
        rendered = f"{entry.tool_name}({json.dumps(entry.arguments, sort_keys=True)})"
        if rendered not in shapes:
            shapes.append(rendered)
    called = "; ".join(shapes) if shapes else "no matching tool call in the ledger"
    return (
        "outcome, action and safety PASSED and only EVIDENCE failed — the grader-drift "
        "signature (INC-001): suspect the claim before the agent, and read the trajectory "
        f"before changing any prompt. Failing claim(s): {detail} | call shapes the run "
        f"actually used for the named tool(s): {called} | if a correct agent may reasonably "
        "have chosen this shape, the claim is what has to move (docs/eval-methodology.md "
        "§ verify claims and verify shapes)."
    )


def _classify_failure(report: GradeReport, final: RunState | None) -> tuple[str, str]:
    """Bucket a graded run into the noise taxonomy, with a reason for the bucket.

    Conservative: anything ambiguous lands in "unclassified" rather than a wrong
    bucket. Priority order is docs/lessons/live-eval-noise-sources.md's debugging
    discipline — environment, consistency, variance, grader.
    """
    if report.passed:
        return "passed", ""
    dims = {d.dimension: d for d in report.dimensions}
    failing = {d.dimension for d in report.dimensions if not d.passed}
    evidence = final.evidence if final is not None else ()
    summaries = [e.result_summary for e in evidence]
    # Ahead of "transport": the one bucket saying the AGENT was right and the HARNESS could
    # not read it. Live run 779b19a287a7 reported `unclassified` with a correct decision.
    if any(
        summary.startswith(prefix) for summary in summaries for prefix in OUTPUT_INVALID_PREFIXES
    ):
        return PLANNER_OUTPUT_INVALID_CLASS, ""
    if any(
        ("MCP error" in s) or ("MCPError" in s) or ("LLM" in s and "invalid" in s)
        for s in summaries
    ):
        return "transport", ""
    if any("is_error=True" in s for s in summaries):
        return "shared-env", ""
    action = dims.get(GradeDimension.ACTION)
    if (
        action is not None
        and action.passed
        and any(
            e.tool_name == VERIFY_JUDGE_MARKER and e.result_summary.startswith("not_verified")
            for e in evidence
        )
    ):
        # Right action, judge said not-yet: the fix outran the probe.
        return "eventual-consistency", ""
    if failing == {GradeDimension.BUDGET}:
        return "llm-variance", ""
    if failing == {GradeDimension.EVIDENCE}:
        return "grader-brittleness", _grader_drift_detail(report, evidence)
    if GradeDimension.ATTRIBUTION in failing:
        # A finding ABOUT THE AGENT: it credited itself with a recovery the fault's own clock
        # produced. Matched on membership, not an exact set, because the same run usually
        # reds OUTCOME too and the attribution is still the reason.
        return FALSE_ATTRIBUTION_CLASS, next(
            (d.detail for d in report.dimensions if d.dimension is GradeDimension.ATTRIBUTION),
            "",
        )
    return "unclassified", ""


def _crashed_result(
    scenario: Scenario,
    exc: BaseException,
    invocation_id: str = "",
    *,
    settings: Settings | None = None,
    model_role: ModelRole = ModelRole.DEVELOPMENT,
    recorded: bool = False,
    rehearsal: bool = False,
    world_already_faulted: bool = False,
) -> ScenarioResult:
    """Synthesize a failed ScenarioResult when run_scenario raises.

    One crashing scenario must not wipe the results of every scenario after it. A
    ``ScenarioCrash`` also carries the partial ledger and checkpoints, so the row reports
    what was spent; the bucketing reads the original CAUSE, not the wrapper.
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
    # The transitions absorb transport failures as graded escalations, so a crash reaching
    # here is the scenario's own seeding or an unwrapped transport path.
    if isinstance(cause, PreconditionNotMet):
        # Not "shared-env": nothing was contended, the world simply was not in the
        # asserted state, and no agent behaviour is described.
        crash_class = "precondition"
    elif isinstance(cause, PreconditionUnverifiable):
        # The premise is UNKNOWN, not false; "precondition" would send the reader to
        # seeding when the platform is the problem.
        crash_class = "transport"
    elif isinstance(cause, ChaosSetupFailed) or "chaos_setup" in error_detail:
        # ``run_all`` records an ungraded row instead of routing here; this serves a direct
        # ``run_scenario`` caller that wants a row.
        crash_class = "shared-env"
    else:
        crash_class = "transport"
    # What the crash had billed, when it carried both the measurement and a checkpoint to
    # reconcile against. ``None`` means none exists, distinct from a zeroed record.
    crash_accounting = exc.accounting if isinstance(exc, ScenarioCrash) else None
    # The crash's own ledger if it reached a terminal state, else the last checkpoint's:
    # they differ by the post-terminal briefing charge this split contains.
    crash_ledger = exc.ledger if isinstance(exc, ScenarioCrash) else None
    accounting = (
        None
        if crash_accounting is None or partial is None
        else build_accounting(crash_accounting, crash_ledger or partial.budget)
    )
    outcome = ScenarioOutcome(
        scenario=scenario.name,
        **_grouping_keys(scenario),
        # Off the last checkpoint the run wrote: hardcoded TRIAGE and 0 described a run that
        # never started (ADR 0015: the meter may over-report, never under-report).
        final_state=IncidentState.TRIAGE if partial is None else partial.state,
        tool_calls_used=0 if partial is None else partial.budget.tool_calls_used,
        report=report,
        judge_score=None,
        failure_class=crash_class,
        # A failed teardown outlives this row: the next live run's world is dirty. The report
        # is the durable record; the on-disk latch is the enforcement.
        teardown_error=(exc.teardown_error if isinstance(exc, ScenarioCrash) else None),
        # Provenance survives the crash (ADR 0013): these once defaulted to False, so every
        # crashed row in a live report claimed canned.
        live_mcp=scenario.use_live_mcp and not recorded,
        live_llm=scenario.use_live_llm and not rehearsal,
        # A rehearsal that died at seeding is still not a measurement.
        degraded=rehearsal,
        # A crash that cannot name its model and revision is a row nobody can act on.
        # ``settings is None`` only for a test that knows no configuration.
        provenance=(
            None
            if settings is None
            else build_provenance(
                scenario.name,
                settings,
                model_role=model_role,
                invocation_id=invocation_id,
                # The DECLARED legs, because a crash can precede either choice — except
                # ``recorded`` and ``rehearsal``, which the caller already decided.
                execution_mode=_execution_mode(
                    recorded=recorded,
                    rehearsal=rehearsal,
                    live=scenario.use_live_mcp or scenario.use_live_llm,
                ),
                # The partial ledger when the crash carried one, else what the run WOULD have
                # been seeded with — asked of ``start_run`` itself, never re-derived (WO-R3-256):
                # the old copy missed WP-2.4's multipliers and named budgets no run ever had.
                budget=(
                    partial.budget
                    if partial is not None
                    else start_run(
                        scenario.agent_visible().alert,
                        settings,
                        datetime.now(UTC),
                        max_tool_calls=scenario.expectation.max_tool_calls,
                    ).budget
                ),
                # A crash cannot undo the fact that this process was handed a world it
                # did not break.
                chaos_seeded_by=EXTERNAL_CHAOS_SEEDER if world_already_faulted else None,
                # NOT the caller's instruction, unlike the line above, and the asymmetry is
                # deliberate: "a world somebody else broke" is true from the first line of
                # the run, while "the platform paged this run" is only true once an alert row
                # came back. So it is read off the crash, which carries the row when one
                # arrived and nothing when the run died before it — a crash in the seeding
                # says ``scenario``, which is what actually happened.
                platform_alert=(exc.platform_alert if isinstance(exc, ScenarioCrash) else None),
            )
        ),
        accounting=accounting,
    )
    # Invariant 9: the evidence a crashed run did produce is still evidence. An empty
    # trajectory under a nil incident id is data the harness threw away.
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
    model_role: ModelRole = ModelRole.DEVELOPMENT,
    recorded_worlds: Mapping[str, Path] | None = None,
    rehearsal: bool = False,
    world_already_faulted: bool = False,
    alert_from_platform: bool = False,
) -> tuple[RunReport, tuple[Trajectory, ...], tuple[EscalationBriefing, ...]]:
    """Run every scenario and assemble the report.

    ``rehearsal`` (ADR 0069), ``world_already_faulted`` (ADR 0075) and
    ``alert_from_platform`` (O-36) apply to the WHOLE invocation, because each describes the
    process rather than one scenario's world; ``recorded_worlds`` is per scenario and maps a
    name to the recording to replay it against (WP-3.3), with ``main`` refusing a recorded
    selection that has no recording before anything runs.
    """
    # ``on_result`` fires once per scenario on both paths, before the next one starts, so
    # ``main``'s ``archive_scenario`` makes scenario N durable while N+1 runs. Called OUTSIDE
    # the try, so an archive failure aborts the suite instead of reading as a scenario crash.
    results: list[ScenarioResult] = []
    ungraded: list[UngradedScenario] = []
    worlds = dict(recorded_worlds or {})
    # Read once: the refusal below needs the names, and a generator read twice would be
    # empty by the time the loop ran.
    planned = tuple(scenarios)
    # ``search`` is recorded-mode only (ADR 0060), and this is the one place that knows which
    # scenarios have a recording. Refused for the WHOLE invocation before the first one starts,
    # or every scenario's budget goes on crash rows saying the same thing once each.
    unrecorded = tuple(scenario.name for scenario in planned if scenario.name not in worlds)
    if settings.inference_strategy is StrategyName.SEARCH and unrecorded:
        raise ValueError(
            f"{SEARCH_IS_RECORDED_MODE_ONLY} Scenarios in this invocation with no "
            f"recording to replay: {', '.join(unrecorded)}."
        )
    for scenario in planned:
        recorded_world = worlds.get(scenario.name)
        try:
            result = run_scenario(
                scenario,
                settings,
                clock,
                mcp_token=mcp_token,
                invocation_id=invocation_id,
                model_role=model_role,
                recorded_world=recorded_world,
                rehearsal=rehearsal,
                world_already_faulted=world_already_faulted,
                alert_from_platform=alert_from_platform,
            )
        except ChaosSetupFailed as exc:
            # NOT a graded row (plan 01 § 4): the world was never built, so a
            # ``GradeReport`` here would describe the agent with a pre-run event.
            print(f"  UNGRADED {scenario.name}: {exc}")
            ungraded.append(UngradedScenario(scenario=scenario.name, reason=str(exc)))
            continue
        except Exception as exc:  # noqa: BLE001 — deliberate: don't abort suite
            print(f"  CRASH {scenario.name}: {type(exc).__name__}: {exc}")
            result = _crashed_result(
                scenario,
                exc,
                invocation_id,
                settings=settings,
                model_role=model_role,
                recorded=recorded_world is not None,
                rehearsal=rehearsal,
                world_already_faulted=world_already_faulted,
            )
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
        # Crash rows keep ``degraded=False``, so this post-run count can undercount the
        # pre-run estimate — acceptable, since main()'s --live gate is pre-run.
        degraded_count=sum(1 for o in outcomes if o.degraded),
        only_patterns=only_patterns,
        # From the role every row was stamped with, so console, report and archive agree
        # (A-01). ``and not rehearsal`` because a rehearsal bills no model (ADR 0069), and
        # would otherwise mark itself closing for the validator to refuse after seeding.
        closing=model_role is ModelRole.BENCHMARK and not rehearsal,
        outcomes=outcomes,
        ungraded=tuple(ungraded),
    )
    return report, trajectories, briefings


def write_report(report: RunReport, *, directory: Path = _REPORTS_DIR) -> Path:
    """Write ``report`` as ``report.<stamp>.<invocation_id>.json`` and return the path.

    The run's own identity names the file, so a report cannot be labelled as another
    run. Exclusive-create: a second write there is one run reported twice.
    """
    return artifacts.write_versioned(
        "report",
        content=report.model_dump_json(indent=2),
        timestamp=report.generated_at,
        invocation_id=report.invocation_id,
        directory=directory,
    )


def write_trajectories(
    trajectories: Iterable[Trajectory],
    directory: Path = _TRAJECTORIES_DIR,
    *,
    timestamp: datetime | None = None,
) -> list[Path]:
    """Write each trajectory to ``<directory>/<scenario>.<stamp>.<invocation_id>.json``.

    The filename comes from the record's own ``invocation_id``, so it cannot disagree
    with the contents. One run passes a single ``timestamp`` so its suite groups.
    """
    when = timestamp or datetime.now(UTC)
    return [
        artifacts.write_versioned(
            "trajectory",
            trajectory.scenario,
            content=trajectory.model_dump_json(indent=2),
            timestamp=when,
            invocation_id=trajectory.invocation_id,
            directory=directory,
        )
        for trajectory in trajectories
    ]


def write_briefings(
    briefings: Iterable[EscalationBriefing],
    scenario_names: Iterable[str],
    directory: Path = _BRIEFINGS_DIR,
    *,
    invocation_id: str,
    timestamp: datetime | None = None,
) -> list[Path]:
    """Write each briefing to ``<directory>/<scenario>.<stamp>.<invocation_id>.json``.

    ``EscalationBriefing`` carries no run identity, so the ``invocation_id`` is passed
    in — required, not defaulted: an empty id cannot be joined back to the run.
    """
    when = timestamp or datetime.now(UTC)
    return [
        artifacts.write_versioned(
            "briefing",
            name,
            content=briefing.model_dump_json(indent=2),
            timestamp=when,
            invocation_id=invocation_id,
            directory=directory,
        )
        for briefing, name in zip(briefings, scenario_names, strict=True)
    ]


def _lock_path(path: Path) -> None:
    """Make one archived path refuse writes: clear write bits, then ``uchg`` where the
    platform has it (macOS/BSD; Linux CI does not).

    Invariant 9 in the filesystem: exclusive-create stops the runner overwriting evidence,
    this stops ``rm -rf``. Best-effort, never fatal, idempotent. Deliberate unlock:
    docs/runbook.md § "Completed archives are locked on disk".
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

    Bottom-up because ``uchg`` on a directory refuses new entries. From
    ``finalize_archive`` only: a partial archive's directories must stay writable so the
    next scenario, and eventually ``report.json``, can land.
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

    The flat ``<EVAL_TRACE_DIR>/<scenario>.jsonl`` is gitignored (F-002), so traces left
    only there could not be joined to their prompts (S-07). Filter convention, shared with
    ``scripts/estimate_cost.py``: a wrong or absent ``invocation_id`` is excluded.
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
    # The lock lands on the archived SLICE only: later invocations APPEND to the flat
    # source (F-002's fix), and locking it would crash the next run's tracer.
    _lock_path(path)


def archive_scenario(
    target: Path,
    result: ScenarioResult,
    *,
    invocation_id: str,
    trace_dir: Path | None = None,
) -> None:
    """Stream one finished scenario's evidence into the run archive.

    Wired into ``run_all(on_result=...)``, so scenario N is on disk before N+1 starts: a
    Ctrl-C used to throw away 30 scenarios of paid live evidence (S-05/A-14). Every file is
    exclusive-create and locked as it lands; ``report.json`` is ``finalize_archive``'s.
    """
    scenario = result.outcome.scenario
    _archive_trajectory(target, result.trajectory)
    _archive_briefing(target, scenario, result.briefing)
    if trace_dir is not None:
        _archive_trace_slice(target, scenario, invocation_id=invocation_id, trace_dir=trace_dir)


def finalize_archive(target: Path, report: RunReport) -> Path:
    """Write ``<target>/report.json`` — the completion marker, always last.

    Its absence marks a partial run whose per-scenario files are still evidence; written
    first, a half-finished archive looked complete (S-08). The marker means nothing will
    write here again, so the tree is locked once it is down.
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

    The one-shot equivalent of the streaming path ``main`` uses. Same files, same ordering
    (report.json last), same exclusive-create: two invocations aimed at one directory fail
    loudly rather than deleting the earlier one (F-002).
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

    The CONSTRUCTOR with ``_env_file=None``, never ``model_validate``, which consults the
    cwd ``.env`` — how a real ``PLATFORM_SMOKE_TOKEN`` leaked into "offline" runs (A-04).
    ``platform_smoke_token=None`` is pinned so an exported var cannot supply it either.
    """
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        anthropic_api_key=SecretStr("eval"),
        judge_model="claude-haiku-4-5",
        platform_mcp_url=AnyHttpUrl("https://eval.local"),
        platform_rest_url=AnyHttpUrl("https://eval.local"),
        platform_token=SecretStr("eval"),
        platform_webhook_secret=SecretStr("eval"),
        database_url=PostgresDsn("postgresql://eval:eval@localhost:5432/eval"),
        platform_smoke_token=None,
    )


def root_cause_coverage(report: RunReport) -> RootCauseCoverage:
    """How many of this report's rows were graded on diagnosis, and how many were right.

    WP-2.2's acceptance number, computed from the ROWS so it works over the committed
    baseline and every archived report. "Graded" means a substantive detail via
    ``is_vacuous_detail``; the ungraded rows split in two (INC-003): wrong world, or unlabelled.
    """
    rows = [
        dimension
        for outcome in report.outcomes
        for dimension in outcome.report.dimensions
        if dimension.dimension is GradeDimension.ROOT_CAUSE
    ]
    return coverage_over(
        [(not is_vacuous_detail(row.detail), row.passed) for row in rows],
        total=report.total,
        world_mismatch=sum(1 for row in rows if is_not_graded_detail(row.detail)),
    )


def _print_summary(report: RunReport) -> None:
    print(f"scenarios: {report.total}, passed: {report.passed}, failed: {report.failed}")
    if report.ungraded:
        # Above the per-row listing, because it changes how every number
        # under it is read: these scenarios are absent from the totals.
        print(
            f"UNGRADED: {len(report.ungraded)} scenario(s) never ran — the fault world "
            "could not be built, so the agent was not graded:"
        )
        for row in report.ungraded:
            print(f"  {row.scenario} [{row.phase}] — {row.reason}")
    for scenario_name in report.contaminated_scenarios:
        error = next(o.teardown_error for o in report.outcomes if o.scenario == scenario_name)
        print(f"TEARDOWN FAILED: {scenario_name} — {error}")
    # From the first row's own record, not the settings this process holds, so the line
    # and the artifact cannot disagree.
    first = next((o.provenance for o in report.outcomes if o.provenance is not None), None)
    if first is not None:
        print(
            f"provenance: {first.agent_model} ({first.model_role.value} role), "
            f"judge {first.judge_model}, commander {first.commander_revision[:12]}, "
            f"platform {first.platform_image_digest[:19]}, {first.execution_mode.value}"
        )
    if reason := report.non_closing_reason:
        print(f"NON-CLOSING: this report cannot close a phase — {reason}")
    # From the persisted field, so the console and the artifact agree by construction —
    # their divergence was finding A-01.
    if (report.degraded_count or 0) > 0:
        if first is not None and first.rehearsal:
            # Different sentence for the same field: a rehearsal's canned model leg is the
            # mode, not a fallback, so "fix your .env" is the wrong next move (ADR 0069).
            print(
                f"degraded: {report.degraded_count} scenarios, every one of them "
                "BY DESIGN — this is a rehearsal, so the planner was scripted. No row "
                "here is a measurement of the agent."
            )
        else:
            print(
                f"degraded: {report.degraded_count} scenarios fell back to canned "
                "(PLATFORM_MCP_URL or ANTHROPIC_API_KEY is offline placeholder)"
            )
    if report.judged_count > 0 and report.judge_mean_overall is not None:
        print(
            f"judge: {report.judge_useful_count}/{report.judged_count} useful, "
            f"mean overall {report.judge_mean_overall:.2f}"
        )
    print(root_cause_coverage(report).describe())
    # The run's bill from the rows' own accounting records (WP-2.3), not re-added from
    # settings (A-01). Silent on a canned suite, where "$0.000000" would be noise.
    accounted = [o.accounting for o in report.outcomes if o.accounting is not None]
    if any(row.usd_used > 0 for row in accounted):
        unreconciled = [
            o.scenario
            for o in report.outcomes
            if o.accounting is not None and not o.accounting.reconciled
        ]
        print(
            f"cost: ${sum((row.usd_used for row in accounted), Decimal('0')):.6f}, "
            f"{sum(row.tokens_used for row in accounted)} tokens, "
            f"{sum(row.llm_calls for row in accounted)} LLM calls, "
            f"{sum(row.tool_calls for row in accounted)} tool calls"
        )
        if unreconciled:
            # Never silent: a split that does not add up to the ledger is the
            # one thing this record exists to make impossible to miss.
            print(
                "COST UNRECONCILED: the per-role split does not equal the "
                f"budget ledger for {', '.join(unreconciled)}"
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
            # The diagnosis, where a bucket name alone would send the reader
            # to the agent instead of to the claim (INC-001, WO-R2-175).
            if outcome.failure_class_detail:
                print(f"    ! {outcome.failure_class_detail}")


def _canned_equivalent_knob_warning(settings: Settings) -> str | None:
    """Preflight nudge for --live runs still on canned-equivalent probe knobs.

    A live run at the config defaults reproduces both failure modes ADR 0006/0009 exist for
    (S-10). A warning, not exit 3: an explicit ``VERIFY_PROBE_ATTEMPTS=1`` is
    indistinguishable from unset, so a hard fail would ban single-probe experiments.
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

    Unit tests point ``_RUNS_DIR`` at ``tmp_path``, and a console nicety must never be
    the thing that raises.
    """
    try:
        return str(path.relative_to(_REPO_ROOT))
    except ValueError:
        return str(path)


def _settings_for_mode(live: bool, *, rehearsal: bool = False) -> Settings:
    """Live mode reads real env; offline uses the eval placeholder; rehearsal is neither.

    A REHEARSAL reads the real environment (its platform leg is real) and replaces only the
    model key with the offline placeholder, so these settings hold no key a live LLM client
    could be built from at all (ADR 0069). ``model_copy``, never a second construction:
    re-reading the environment is a second chance for the platform half to differ.
    """
    if rehearsal:
        return Settings().model_copy(  # type: ignore[call-arg]
            update={"anthropic_api_key": SecretStr(_EVAL_PLACEHOLDER_API_KEY)}
        )
    if live:
        return Settings()  # type: ignore[call-arg]
    return _eval_defaults()


def _parse_only(argv: list[str]) -> list[str]:
    """Extract scenario filters from ``--only <pattern>``.

    Repeated flags or one comma-separated value; empty means "no filter". ``main`` decides
    how a pattern MATCHES — full name under ``--live``, substring otherwise (ADR 0020).
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


def _parse_model_role(argv: Sequence[str]) -> tuple[ModelRole | None, str]:
    """The ``--model-role`` selection, or ``(None, refusal)`` explaining why not.

    Resolves ``AGENT_MODEL`` from ``DEVELOPMENT_MODEL`` or ``BENCHMARK_MODEL`` (plan 02 § 9),
    defaults to ``development`` and refuses an unrecognised value. A PAIR because
    ``ModelRole`` is a ``StrEnum``, so a refusal string would otherwise read as a selection.
    """
    raw: str | None = None
    for i, arg in enumerate(argv):
        if arg == "--model-role" and i + 1 < len(argv):
            raw = argv[i + 1]
        elif arg.startswith("--model-role="):
            raw = arg.split("=", 1)[1]
    if raw is None:
        return ModelRole.DEVELOPMENT, ""
    try:
        return ModelRole(raw.strip()), ""
    except ValueError:
        roles = ", ".join(role.value for role in ModelRole)
        return None, f"MODEL ROLE FAIL: --model-role {raw!r} is not one of: {roles}"


#: The two values ``--mode`` takes: the modes that cannot be inferred from what the run can
#: reach. ``live`` and ``canned`` have no spelling here, because they ARE inferable.
RECORDED_MODE: Final[str] = "recorded"
REHEARSAL_MODE: Final[str] = "rehearsal"
#: The flag that says the fault is already in the world (ADR 0075). Named once, because
#: ``scripts/demo_live.py`` passes it and ``tests/unit/test_demo_live.py`` asserts it does.
WORLD_ALREADY_FAULTED_FLAG: Final[str] = "--world-already-faulted"
#: The flag that takes the run's brief from the platform's own alert stream instead of the
#: scenario's ``alert:`` block (owner decision O-36, platform ADR 0039). Named for
#: ``WORLD_ALREADY_FAULTED_FLAG``'s reason: ``scripts/demo_live.py`` passes it and
#: ``tests/unit/test_demo_live.py`` asserts that it does.
ALERT_FROM_PLATFORM_FLAG: Final[str] = "--alert-from-platform"
_MODES: Final[tuple[str, ...]] = (RECORDED_MODE, REHEARSAL_MODE)


def _parse_mode(argv: Sequence[str]) -> tuple[str | None, str]:
    """The ``--mode`` selection: ``""`` for absent, the value, or ``None`` + a refusal.

    Three states for ``_parse_model_role``'s reason. An unrecognised value is REFUSED:
    "--mode recordd ran the canned suite and reported it" is the dead-``--only`` bug,
    and this one would spend money on the way.
    """
    raw: str | None = None
    for i, arg in enumerate(argv):
        if arg == "--mode" and i + 1 < len(argv):
            raw = argv[i + 1]
        elif arg.startswith("--mode="):
            raw = arg.split("=", 1)[1]
    if raw is None:
        return "", ""
    value = raw.strip()
    if value in _MODES:
        return value, ""
    return None, (
        f"MODE FAIL: --mode {raw!r} is not a mode this runner takes. The values are "
        f"--mode {RECORDED_MODE} (replay a recorded world: real model, no platform) and "
        f"--mode {REHEARSAL_MODE} (the real platform under the scripted planner: no "
        "model, no spend, and no row that may be read as live). A live run is --live, "
        "and a canned run is the default — those are selected by what the run can "
        "reach, not by this flag."
    )


def _parse_world(argv: Sequence[str]) -> str | None:
    """The ``--world`` selection: a recording's invocation id, or a scenario name."""
    for i, arg in enumerate(argv):
        if arg == "--world" and i + 1 < len(argv):
            return argv[i + 1].strip()
        if arg.startswith("--world="):
            return arg.split("=", 1)[1].strip()
    return None


def recordings_for(scenarios: Sequence[Scenario], world: str | None) -> tuple[dict[str, Path], str]:
    """Which recording each selected scenario replays, or a refusal saying why not.

    Without ``--world``, each scenario replays its newest recording via
    ``artifacts.newest_or_none`` (never a glob), and no recording REFUSES rather than falling
    back to canned. ``--world <id>`` pins one, which is what makes a number reproducible. A
    temporal scenario is refused first (WP-14.1): its recording never expires.
    """
    temporal = [
        (scenario.name, refusal)
        for scenario in scenarios
        if (refusal := scenario.recorded_refusal) is not None
    ]
    if temporal:
        return {}, "RECORDED FAIL: " + " ".join(refusal for _, refusal in temporal)
    if world is None:
        found: dict[str, Path] = {}
        missing: list[str] = []
        for scenario in scenarios:
            path = artifacts.newest_or_none("recorded_world", scenario.name)
            if path is None:
                missing.append(scenario.name)
            else:
                found[scenario.name] = path
        if missing:
            return {}, (
                f"RECORDED FAIL: {len(missing)} selected scenario(s) have no recorded "
                f"world: {', '.join(sorted(missing))}. Record each one first "
                "(`make world-record ONLY=<scenario>`, which needs the stack up and "
                "costs no model tokens). A recorded run will not fall back to canned "
                "fixtures: the row would claim a world it never replayed."
            )
        return found, ""

    # One resolver for both callers: ``make world-drift`` asks the same question of the
    # same id, and two copies would resolve to different recordings.
    matches = matching_recordings(world, [scenario.name for scenario in scenarios])
    if not matches:
        return {}, (
            f"RECORDED FAIL: --world {world!r} matches no recording of any selected "
            "scenario. Pass a recording's invocation id (the last segment of its "
            "filename under evals/recorded_worlds/) or a selected scenario's full name."
        )
    if len(matches) > 1:
        return {}, (
            f"RECORDED FAIL: --world {world!r} matches recordings of "
            f"{len(matches)} scenarios: {', '.join(sorted(matches))}. One world is one "
            "scenario's world; narrow the selection with --only."
        )
    if len(scenarios) > 1:
        return {}, (
            f"RECORDED FAIL: --world {world!r} pins one recording but "
            f"{len(scenarios)} scenarios are selected. Add --only "
            f"{next(iter(matches))}, or drop --world to replay each scenario's newest "
            "recording."
        )
    return matches, ""


def _smoke_holdback_reason(scenario: Scenario) -> str:
    """Why the derived smoke pass does not contain ``scenario``.

    Per scenario, because the three causes have three different repairs. The first two can
    both be true and are both reported; ``smoke_exclusion`` cannot coexist with either.
    """
    causes: list[str] = []
    if scenario.seeds_chaos:
        # Named by the field the YAML uses, because the reader's next move is to open
        # that block.
        declared = (
            "declares chaos_setup"
            if scenario.chaos_setup is not None
            else f"declares a chaos_plan seeding {len(scenario.chaos.setup)} hook(s)"
        )
        causes.append(
            f"{declared} (seeding fires under the evaluator's chaos principal, "
            "which mutates the shared world — the claim --smoke exists to "
            "disprove is that this stage changes nothing)"
        )
    if scenario.expectation.expected_action_tools:
        causes.append(
            "declares expected_action_tools (a graded Tier-1 write; the read-scoped "
            "smoke token 403s it by design, so it is guaranteed red here)"
        )
    if scenario.smoke_exclusion is not None:
        causes.append(f"smoke_exclusion: {scenario.smoke_exclusion}")
    # Unreachable from a caller filtering on `not in_smoke_pass`, and stated anyway: an empty
    # reason would turn the refusal into a bare list of names.
    return "; ".join(causes) if causes else "not in the derived smoke pass"


def main() -> int:
    if "--clear-chaos-block" in sys.argv[1:]:
        # The operator half of the teardown latch, its own invocation rather than a flag:
        # clearing it asserts the world is back, and nobody makes that assertion
        # consciously when it is bundled into the run it unblocks.
        cleared = clear_chaos_block()
        if cleared is None:
            print("no chaos teardown block is set; nothing to clear")
        else:
            print(f"cleared chaos teardown block: {cleared}")
        return 0
    live = "--live" in sys.argv[1:]
    # The read-scoped principal comes from Settings directly, NOT through the shell:
    # a make recipe's export was overridden by `-include .env`, so every "read-scoped"
    # smoke run before 2026-08-07 held write scope. Config in, guard at point of use.
    smoke = "--smoke" in sys.argv[1:]
    if smoke and not live:
        # Before the settings load, so it needs no env. Erroring beats implying --live,
        # which would flip the settings source to the real .env unasked (A-04).
        print(
            "SMOKE FAIL: --smoke requires --live (smoke without live would "
            "run the whole suite canned under placeholder settings)"
        )
        return 3
    # Before the settings load and before the world latch below, whose question ("does this
    # invocation reach the shared world?") the mode is half the answer to.
    mode, mode_refusal = _parse_mode(sys.argv[1:])
    if mode is None:
        print(mode_refusal)
        print("no scenarios ran, nothing was spent")
        return 2
    recorded = mode == RECORDED_MODE
    rehearsal = mode == REHEARSAL_MODE
    if rehearsal and (live or smoke):
        # A rehearsal is defined by what it withholds from the model, so `--live` is a
        # contradiction, not a narrowing; `--smoke` is read-only and a rehearsal acts.
        asked = " and ".join(flag for flag in ("--live", "--smoke") if flag in sys.argv[1:])
        print(
            f"MODE FAIL: --mode {REHEARSAL_MODE} cannot be combined with {asked}. A "
            "rehearsal runs the real platform with the SCRIPTED planner and spends "
            f"nothing; --live buys a real model, and --smoke is the read-only stage. "
            f"For the paid take drop --mode {REHEARSAL_MODE}."
        )
        print("no scenarios ran, nothing was spent")
        return 2
    if (live or rehearsal) and (blocked := chaos_block_reason()) is not None:
        # A previous teardown did not complete, so the baseline carries a fault nobody
        # intended. Refused before settings, guards and spend: a refusal after the money is
        # gone is a report. A rehearsal is blocked too — it seeds into the same world.
        print(f"CONTAMINATED WORLD: live and rehearsal runs are blocked — {blocked}")
        print(
            "Restore the world — the reset clears the block on success:\n"
            "  make eval-reset PURGE_IDEMPOTENCY=1\n"
            "Only where there is no stack left to reset, clear it alone:\n"
            "  uv run python -m evals.runner --clear-chaos-block"
        )
        print("no scenarios ran, nothing was spent")
        return 10
    # ADR 0075: the setup hooks already fired, so this run must not fire them again. Beside
    # the other mode flags, because it too states what world the invocation was handed.
    world_already_faulted = WORLD_ALREADY_FAULTED_FLAG in sys.argv[1:]
    if world_already_faulted and recorded:
        print(
            f"MODE FAIL: {WORLD_ALREADY_FAULTED_FLAG} cannot be combined with --mode "
            f"{RECORDED_MODE}. A recorded run replays a world from a file and seeds "
            "nothing, so there is no seeding for the flag to skip."
        )
        print("no scenarios ran, nothing was spent")
        return 2
    # O-36: the brief comes from the platform's own alert stream rather than from the
    # scenario file. Parsed beside the other flags that describe the WORLD this invocation
    # was handed, and refused against the one mode that has no platform to be paged by.
    alert_from_platform = ALERT_FROM_PLATFORM_FLAG in sys.argv[1:]
    if alert_from_platform and recorded:
        print(
            f"MODE FAIL: {ALERT_FROM_PLATFORM_FLAG} cannot be combined with --mode "
            f"{RECORDED_MODE}. A recorded run replays a world from a file and reaches no "
            "platform, so there is no alert stream to take the run's page from."
        )
        print("no scenarios ran, nothing was spent")
        return 2
    world = _parse_world(sys.argv[1:])
    if world is not None and not recorded:
        print(
            f"WORLD FAIL: --world {world!r} was given without --mode {RECORDED_MODE}. "
            "A world is a recording, and only a recorded run replays one."
        )
        print("no scenarios ran, nothing was spent")
        return 2
    if recorded and (live or smoke):
        # The one combination that could touch the shared world while claiming to be a
        # replay: --live and --smoke reach a platform, and a recording is a file.
        asked = " and ".join(flag for flag in ("--live", "--smoke") if flag in sys.argv[1:])
        print(
            f"MODE FAIL: --mode {RECORDED_MODE} cannot be combined with {asked}. A "
            "recorded run replays a world from disk: it seeds nothing, resets nothing "
            "and reaches no platform, so there is no live stage for it to be part of."
        )
        print("no scenarios ran, nothing was spent")
        return 2
    # Before the settings load, like the refusals around it: a mistyped role must cost nothing.
    model_role, role_refusal = _parse_model_role(sys.argv[1:])
    if model_role is None:
        print(role_refusal)
        print("no scenarios ran, nothing was spent")
        return 2
    # One identity per invocation, shared by the tracer, trajectories, report and
    # archive, so every artifact joins and none collides with another run's.
    invocation_id = uuid.uuid4().hex[:12]
    only_patterns = _parse_only(sys.argv[1:])
    if live and not smoke and not only_patterns:
        # A bare `--live` is the whole suite against one shared platform with real spend and
        # no reset. Refused structurally here, because the exit-8 canned-only gate only
        # refused it incidentally. `--smoke` derives its own selection (WO-R2-123).
        print(
            "LIVE FAIL: --live requires --only <scenario_name> (or --smoke). "
            "An unfiltered live selection is the whole suite against one shared "
            "platform — real spend, no reset between scenarios."
        )
        print("Name exactly one scenario, e.g. make eval-live ONLY=remediate_dlq_backlog_success")
        print("no scenarios ran, nothing was spent")
        return 2
    if rehearsal and not only_patterns:
        # The same refusal one flag along: a rehearsal spends nothing and still fires every
        # selected scenario's chaos plan into one shared world with no reset between them.
        print(
            f"REHEARSAL FAIL: --mode {REHEARSAL_MODE} requires --only <scenario_name>. "
            "An unfiltered rehearsal seeds every scenario's fault into one shared "
            "platform with no reset between them — free, and still destructive."
        )
        print("Name exactly one scenario, e.g. make demo-live MODE=dlq_backlog")
        print("no scenarios ran, nothing was spent")
        return 2
    try:
        # A recorded run reads the real environment and costs money: the platform is a
        # replay, the MODEL is not. A rehearsal reads it for the opposite half.
        settings = _settings_for_mode(live or recorded, rehearsal=rehearsal)
    except ValidationError as err:
        # Exit 3 is the preflight/env code; a raw traceback would exit 1, which is
        # reserved for "a scenario failed" (A-15).
        fields = ", ".join(
            ".".join(str(part) for part in detail["loc"]) or "(settings)" for detail in err.errors()
        )
        print(f"PREFLIGHT FAIL (env): invalid or missing settings — {fields}")
        return 3
    # The role resolves the billed model once. ``model_copy``, not a second Settings
    # construction: re-reading the environment is a second chance for it to differ.
    settings = settings.model_copy(update={"agent_model": settings.model_for_role(model_role)})
    print(f"model role: {model_role.value} → AGENT_MODEL={settings.agent_model}")
    # Live-only, before any spend, so canned-equivalent probe knobs surface even on a run
    # that goes on to be refused. Never an exit code.
    if live and (msg := _canned_equivalent_knob_warning(settings)) is not None:
        print(msg)
    if rehearsal and _is_offline_placeholder(str(settings.platform_mcp_url)):
        # The mirror of `--mode recorded`'s placeholder-KEY refusal below: with a placeholder
        # platform this is a canned run wearing a rehearsal label, and its row would carry
        # the mode, the flag and `degraded` about something that never happened (A-01/S-09).
        print(
            f"PREFLIGHT FAIL (env): --mode {REHEARSAL_MODE} but PLATFORM_MCP_URL is the "
            "offline placeholder. A rehearsal's platform leg is the whole point of it: "
            "with no platform this is a canned run labelled as a rehearsal of one. "
            "Point PLATFORM_MCP_URL at the running stack (`make demo`)."
        )
        print("no scenarios ran, nothing was spent")
        return 3
    if alert_from_platform and _is_offline_placeholder(str(settings.platform_mcp_url)):
        # The same refusal shape as the rehearsal check above, for the same reason: the
        # platform's alert stream is the whole point of the flag, so an offline URL makes
        # this a canned run whose row would claim a page nobody raised. Refused before any
        # scenario is selected rather than per scenario, because one answer covers them all.
        print(
            f"PREFLIGHT FAIL (env): {ALERT_FROM_PLATFORM_FLAG} but PLATFORM_MCP_URL is the "
            "offline placeholder. The flag takes the run's alert from the platform's own "
            "rule, and there is no platform here to raise one. Point PLATFORM_MCP_URL at "
            "the running stack (`make demo`)."
        )
        print("no scenarios ran, nothing was spent")
        return 3
    mcp_token: str | None = None
    if smoke:
        # An empty secret is UNSET, not a token: `is None` alone let `SecretStr("")` through,
        # and `make_client`'s `token or ...` then selected the FULL principal (S-04).
        if settings.platform_smoke_token is None or not (
            settings.platform_smoke_token.get_secret_value().strip()
        ):
            print("SMOKE FAIL: PLATFORM_SMOKE_TOKEN is not set in .env")
            print("run `make bootstrap-token` and add the read-scoped token")
            return 3
        mcp_token = settings.platform_smoke_token.get_secret_value()
    scenarios = load_scenarios(_SCENARIOS_DIR)
    if smoke and not only_patterns:
        # The pass selects itself through `in_smoke_pass` (WO-R2-123): the old hand-written
        # Makefile list let a renamed scenario fall out of it silently.
        held_back = sorted(
            (s.name, s.smoke_exclusion) for s in scenarios if s.smoke_exclusion is not None
        )
        scenarios = [s for s in scenarios if s.in_smoke_pass]
        print(f"smoke selection: {len(scenarios)} scenario(s) derived from {_SCENARIOS_DIR}")
        for name, reason in held_back:
            # So the smoke log records what it knowingly did not cover.
            print(f"  held back (smoke_exclusion): {name} — {reason}")
        if not scenarios:
            # Unreachable from a healthy tree, and checked for that: an empty derivation
            # is a green smoke pass over nothing.
            print(
                "SELECTION FAIL: no scenario is in the smoke pass — every scenario "
                "either declares chaos_setup/expected_action_tools or carries a "
                "smoke_exclusion"
            )
            print("no scenarios ran, nothing was spent")
            return 2
    if only_patterns:
        # OR-match, accounted PER PATTERN: refusing only an empty selection let one dead
        # pattern hide among nineteen and report green over a smaller suite (WO-R2-41).
        matched: dict[str, list[str]] = {
            pattern: [s.name for s in scenarios if pattern in s.name] for pattern in only_patterns
        }
        for pattern, names in matched.items():
            print(f"  --only {pattern} → {len(names)} scenario(s)")
        dead = [pattern for pattern, names in matched.items() if not names]
        if dead:
            # Names which patterns to fix instead of only reporting the total.
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
        if (live or rehearsal) and not smoke:
            # On the shared-world path a pattern must be a scenario's FULL NAME: `ONLY=dlq_backlog`
            # also took `remediate_dlq_backlog_success`, drained the seeded replay_safe pool and
            # got the agent blamed. Offline and `--smoke` keep substring matching.
            known = {s.name for s in scenarios}
            widened = [p for p in only_patterns if p not in known]
            if widened:
                print(
                    f"SELECTION FAIL: {len(widened)} --only pattern(s) are not "
                    f"scenario names: {', '.join(widened)}"
                )
                print(
                    "A run against the shared platform selects by full scenario name — "
                    "one named scenario per pattern — because a substring silently widens "
                    "the selection past the ADR 0020 one-mutating-scenario gate. "
                    "Did you mean:"
                )
                for pattern in widened:
                    for name in matched[pattern]:
                        print(f"  --only {pattern} → {name}")
                print("no scenarios ran, nothing was spent")
                return 2
            selected = set(only_patterns)
            scenarios = [s for s in scenarios if s.name in selected]
        else:
            scenarios = [s for s in scenarios if any(p in s.name for p in only_patterns)]
        print(f"filter --only={only_patterns} → {len(scenarios)} scenario(s)")
    if smoke:
        # Seeding fires under PLATFORM_CHAOS_TOKEN whatever --smoke says, and the exit-5 audit
        # only sees a write after it lands, so refusing the selection here is the only
        # prevention (S-03). `--only` may NARROW the derived set, never widen it.
        held_back = [(s.name, _smoke_holdback_reason(s)) for s in scenarios if not s.in_smoke_pass]
        if held_back:
            print(
                f"SMOKE FAIL: {len(held_back)} selected scenario(s) are not in the "
                "read-only smoke pass:"
            )
            for name, reason in held_back:
                print(f"  {name} — {reason}")
            print(
                "--only narrows the derived smoke selection; it cannot widen it. "
                "Run these in the remediation stage under the full token instead."
            )
            print("no scenarios ran, nothing was spent")
            return 6
    if live and not smoke:
        # The platform cannot manufacture a canned-only scenario's fault, so its fixtures would
        # be served and the row would enter the live report's pass count. Before the ADR 0020
        # gate, whose advice assumes a scenario that CAN run; --smoke is exempt.
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
    if rehearsal:
        # Stricter than `canned_only` above, which asks whether BOTH legs are canned: here the
        # model leg is canned by definition, so `use_live_mcp: false` would rehearse entirely
        # out of fixtures — a `make eval` with a different word on the row.
        no_platform = sorted(s.name for s in scenarios if not s.use_live_mcp)
        if no_platform:
            print(
                f"REHEARSAL FAIL: {len(no_platform)} selected scenario(s) declare "
                f"use_live_mcp false — {', '.join(no_platform)}. A rehearsal is the real "
                "platform under a scripted planner; with no platform leg there is nothing "
                "left of it to rehearse."
            )
            print(
                "Run them offline instead:\n"
                + "\n".join(f"  make eval ONLY={name}" for name in no_platform)
            )
            print("no scenarios ran, nothing was spent")
            return 8
    if live or rehearsal:
        # One state-mutating scenario per invocation (ADR 0020): the remediation scenarios share
        # ONE platform and one seeded replay_safe row, so in a single invocation a CORRECT agent
        # greens one and reds the other. ``seeds_chaos``, because ``chaos_setup`` is ``None``
        # on a plan-declaring scenario.
        mutating = [
            s.name for s in scenarios if s.expectation.expected_action_tools or s.seeds_chaos
        ]
        if len(mutating) > 1:
            print(
                f"{'LIVE' if live else 'REHEARSAL'} FAIL: {len(mutating)} state-mutating "
                f"scenarios selected — {', '.join(sorted(mutating))}. Each of these changes "
                "the world the next one reads, and nothing resets between them inside one "
                "run."
            )
            # The runnable form of the mode that was asked for, not of the one this gate was
            # written for: a refusal that hands over the wrong command gets worked around.
            alone = (
                "make eval-live ONLY={name}"
                if live
                else f"uv run python -m evals.runner --mode {REHEARSAL_MODE} --only {{name}}"
            )
            print(
                "Run them one at a time, resetting in between:\n"
                + "\n".join(
                    f"  {alone.format(name=name)} && make eval-reset" for name in sorted(mutating)
                )
            )
            print("no scenarios ran, nothing was spent")
            return 7
    recorded_worlds: dict[str, Path] = {}
    if recorded:
        # Resolved AFTER the selection is final, so the refusal can name the
        # scenarios that are actually going to run.
        recorded_worlds, recorded_refusal = recordings_for(scenarios, world)
        if recorded_refusal:
            print(recorded_refusal)
            print("no scenarios ran, nothing was spent")
            return 2
        if _is_offline_api_key(settings.anthropic_api_key.get_secret_value()):
            # The mirror of the --live degradation refusal below: a recorded run on the canned
            # planner is a CANNED run wearing a recorded label.
            print(
                f"PREFLIGHT FAIL (env): --mode {RECORDED_MODE} but ANTHROPIC_API_KEY is "
                "empty or a placeholder. A recorded run replays the PLATFORM; its "
                "planner calls are real, and with no key this would be a canned run "
                "labelled recorded."
            )
            print("no scenarios ran, nothing was spent")
            return 3
        print(
            f"mode: {RECORDED_MODE} — the platform is a replay of a recorded world, the "
            "model is real. Grades are valid for diagnosis and the plan only: OUTCOME, "
            "ACTION and SAFETY are reported not-applicable (plan 02 §189, 03 §52)."
        )
        for name in sorted(recorded_worlds):
            print(f"  {name} → {_repo_relative(recorded_worlds[name])}")
        print(
            "Run `make world-drift WORLD=<id>` before reporting any number from this "
            "run: a recording is only evidence while the world it came from still "
            "matches it (docs/runbook.md)."
        )
    if rehearsal:
        # The recorded banner's counterpart: what is real, what is not, and what the row may
        # therefore be used for, for whoever finds this report in six months (ADR 0069).
        seeding = (
            "somebody else seeded the fault and this run leaves the hooks alone"
            if world_already_faulted
            else "this seeds a fault, acts on the world and resets it"
        )
        print(
            f"mode: {REHEARSAL_MODE} — the platform is REAL ({seeding}) and the planner is "
            "the scenario's scripted one. Nothing is spent. Every row is stamped "
            "degraded=True with a rehearsal provenance flag: this report measures the DEMO, "
            "never the agent, and no phase-close or research report will count it."
        )
    if alert_from_platform:
        # Its own banner, beside the mode's, and saying the same kind of thing: what is real
        # and what a reader may therefore conclude. The point of O-36 is that this sentence
        # is now true of the run, and a run that does not print it is one whose alert was
        # written by the file that graded it.
        print(
            "alert: from the PLATFORM's own alert stream (O-36) — this run waits for an alert "
            "row carrying the scenario's fingerprint and subject, and starts from that row's "
            "payload verbatim. The scenario's own `alert:` block is not read, and the grade "
            "is unchanged: the graders key on the terminal state, the audit log and the "
            "readings, never on the brief."
        )
    offline_mcp = _is_offline_placeholder(str(settings.platform_mcp_url))
    offline_llm = _is_offline_api_key(settings.anthropic_api_key.get_secret_value())
    degraded_to_canned = sum(
        1 for s in scenarios if (s.use_live_mcp and offline_mcp) or (s.use_live_llm and offline_llm)
    )
    if live and degraded_to_canned > 0:
        # Fail BEFORE run_all: a misconfigured --live run then costs nothing and cannot
        # produce a report indistinguishable from a live-green one (A-01/S-09).
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
        # One free authenticated call before anything runs: an expired key otherwise
        # surfaces as N identical crash rows, which cost a whole smoke pass once.
        try:
            preflight_auth(settings.anthropic_api_key.get_secret_value())
        except LLMError as err:
            print(f"PREFLIGHT FAIL (LLM auth): {err}")
            print("fix ANTHROPIC_API_KEY in .env — no scenarios ran, nothing was spent")
            return 3
    # The principal, guarded at point of use before a dollar is spent. No whoami exists, so
    # this is a negative probe: an invalid Tier-1 call must be refused on SCOPE, and the
    # handler checks scope before arguments, so nothing can execute.
    stage_started_at = datetime.now(UTC)
    # Derived from the URL and NOT the --live flag, with no opt-out: the guard must not
    # depend on flag parsing to decide whether scope needs checking.
    guard_required = smoke and not _is_offline_placeholder(str(settings.platform_mcp_url))
    if smoke and not guard_required:
        # A smoke pass exists to verify the live read-scoped principal, and a placeholder
        # has none (A-04).
        print(
            "SMOKE FAIL: smoke mode against a placeholder platform — "
            "there is no live principal to guard"
        )
        return 3
    # The credential that lets the platform label these probes as the LAB's rather than the
    # agent's (platform ADR 0038, WO-R3-335). Resolved once: every guard below needs it.
    lab_probe_token = _lab_probe_credential(settings)
    if guard_required:
        try:
            guard_client = make_client(settings, token=mcp_token)
            try:
                assert_read_only_principal(guard_client, lab_principal_token=lab_probe_token)
            finally:
                guard_client.close()
        except PrincipalGuardError as err:
            print(f"PRINCIPAL GUARD FAIL: {err}")
            print("no scenarios ran, nothing was spent")
            return 4
        except LabProbeRefused as err:
            print(f"LAB PROBE REFUSED ({err.reason_code}): {err}")
            print(
                "the platform would not label this probe as the lab's, so its audit "
                "row would read as the agent's. Fix the request (PLATFORM_CHAOS_TOKEN, "
                "the smoke account's name, CHAOS_ENABLED) — it is never retried "
                "unlabelled. No scenarios ran, nothing was spent."
            )
            return 4
        print("principal guard: token is read-scoped (negative probe refused on scope)")
        print(_lab_probe_note(lab_probe_token))
    # The mirror, for the stage that must be able to ACT: gating every principal check on
    # `smoke` left the one stage that spends AND mutates unguarded, where a read-scoped token
    # grades every scenario red after full spend. A rehearsal is on this side too.
    live_platform = (
        (live or rehearsal)
        and not smoke
        and not _is_offline_placeholder(str(settings.platform_mcp_url))
    )
    write_guard_required = live_platform and any(
        s.expectation.expected_action_tools for s in scenarios
    )
    chaos_guard_required = live_platform and any(s.seeds_chaos for s in scenarios)
    if write_guard_required or chaos_guard_required:
        # TWO clients for two principals (v0.6.5, O-4): the agent's must act and must NOT seed
        # (a seeding token reads the chaos audit rows, which are the answer key), and
        # PLATFORM_CHAOS_TOKEN must seed. Resolved here: an unset token costs one refusal.
        try:
            chaos_token = settings.require_chaos_token() if chaos_guard_required else None
        except ChaosTokenNotConfigured as err:
            print(f"PREFLIGHT FAIL (env): {err}")
            print("no scenarios ran, nothing was spent")
            return 3
        try:
            guard_client = make_client(settings, token=mcp_token)
            try:
                if write_guard_required:
                    # Carries actions:execute AND lacks chaos:invoke.
                    assert_write_capable_principal(
                        guard_client, lab_principal_token=lab_probe_token
                    )
                else:
                    # A chaos-only selection has no write scope to assert, but it still has
                    # an agent — and the leak does not care whether anything was remediated.
                    assert_chaos_blind_principal(guard_client, lab_principal_token=lab_probe_token)
            finally:
                guard_client.close()
            if chaos_token is not None:
                chaos_guard_client = make_client(settings, token=chaos_token)
                try:
                    assert_chaos_capable_principal(
                        chaos_guard_client, lab_principal_token=lab_probe_token
                    )
                finally:
                    chaos_guard_client.close()
        except PrincipalGuardError as err:
            print(f"PRINCIPAL GUARD FAIL: {err}")
            print("no scenarios ran, nothing was spent")
            return 4
        except LabProbeRefused as err:
            print(f"LAB PROBE REFUSED ({err.reason_code}): {err}")
            print(
                "the platform would not label these probes as the lab's, so their "
                "audit rows would read as the agent's own work on the demo page. Fix "
                "the request rather than dropping the label — it is never retried "
                "unlabelled. No scenarios ran, nothing was spent."
            )
            return 4
        if write_guard_required:
            print("principal guard: token can act (negative probe refused on arguments, not scope)")
        print(
            "principal guard: agent token cannot seed chaos (chaos probe refused "
            "on scope), so the platform withholds the chaos audit rows from it"
        )
        if chaos_guard_required:
            print(
                "principal guard: PLATFORM_CHAOS_TOKEN can seed chaos (negative probe "
                "refused on arguments, not scope)"
            )
        # Last, so it reads as a statement about the probes just reported.
        print(_lab_probe_note(lab_probe_token))

    # Created BEFORE the suite runs, so each scenario streams its evidence in as it finishes
    # (ADR 0017). ``exist_ok=False``: an invocation-id collision must fail before any spend.
    trace_dir_setting = os.environ.get(_TRACE_DIR_ENV)
    trace_dir = Path(trace_dir_setting) if trace_dir_setting else None
    target = _RUNS_DIR / invocation_id
    target.mkdir(parents=True, exist_ok=False)
    (target / "trajectories").mkdir(exist_ok=False)
    (target / "briefings").mkdir(exist_ok=False)
    if trace_dir is not None:
        (target / "traces").mkdir(exist_ok=False)

    # `list_audit_events` has no offset and no created_after, so the post-stage assertion sees
    # only the newest 200 rows. A page after every scenario, unioned, is the fix: without it a
    # stage louder than 200 rows exits 5 "inconclusive" on a paid run.
    audit_scan = (
        AuditWindowScan(stage_started_at, lab_principal_token=lab_probe_token)
        if guard_required
        else None
    )
    scan_client = make_client(settings, token=mcp_token) if guard_required else None

    def _after_scenario(result: ScenarioResult) -> None:
        archive_scenario(target, result, invocation_id=invocation_id, trace_dir=trace_dir)
        if audit_scan is None or scan_client is None:
            return
        try:
            audit_scan.checkpoint(scan_client)
        except LabProbeRefused as err:
            # NOT best-effort: swallowing this leaves the checkpoint's read recorded as the
            # agent's, which is the silent unlabelled fallback this design forbids.
            print(f"LAB PROBE REFUSED ({err.reason_code}) on the audit checkpoint: {err}")
            raise
        except Exception as err:  # noqa: BLE001 — a checkpoint is best-effort
            # A missed checkpoint only narrows coverage, which fails closed on its own.
            print(f"post-stage audit checkpoint skipped ({type(err).__name__}: {err})")

    try:
        report, trajectories, briefings = run_all(
            scenarios,
            settings,
            mcp_token=mcp_token,
            invocation_id=invocation_id,
            only_patterns=tuple(only_patterns),
            on_result=_after_scenario,
            model_role=model_role,
            recorded_worlds=recorded_worlds,
            rehearsal=rehearsal,
            world_already_faulted=world_already_faulted,
            alert_from_platform=alert_from_platform,
        )
    finally:
        if scan_client is not None:
            scan_client.close()
    ran_names = [o.scenario for o in report.outcomes]
    # report.json goes down next as the completion marker and only then the top-level copies,
    # so a failure there leaves the durable record complete: with only the flat files, a
    # routine offline `make eval` erased Run 001's paid trajectories (F-003).
    archived = finalize_archive(target, report)
    written_report = write_report(report)
    write_trajectories(trajectories, timestamp=report.generated_at)
    write_briefings(
        briefings, ran_names, invocation_id=invocation_id, timestamp=report.generated_at
    )
    _print_summary(report)
    print(f"run archived: {_repo_relative(archived)} (immutable)")
    print(f"report: {_repo_relative(written_report)}")

    # Post-stage assertion, graded from the platform audit log rather than the agent's
    # trajectory (invariant 6) — the evidence that exposed the token bug, automated.
    if guard_required:
        # Attributed to the principals this stage owns, so another service account cannot fail
        # or mask it (A-13). BOTH ids or nothing: the failure this exists for writes under the
        # AGENT principal, and unfiltered is the over-broad, safe side.
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
                    audit_client,
                    stage_started_at,
                    principal_ids=principal_ids,
                    scan=audit_scan,
                    lab_principal_token=lab_probe_token,
                )
            finally:
                audit_client.close()
        except PrincipalGuardError as err:
            print(f"POST-STAGE AUDIT FAIL: {err}")
            return 5
        except LabProbeRefused as err:
            # Before the MCPError clause below, which it would otherwise be read as: a
            # refused label is a request bug, not an unreadable audit.
            print(f"POST-STAGE AUDIT FAIL (lab probe refused, {err.reason_code}): {err}")
            return 5
        except MCPError as err:
            print(f"POST-STAGE AUDIT INCONCLUSIVE (audit read failed): {err}")
            return 5
        print("post-stage audit: zero successful Tier-1 actions during the smoke stage")

    # Order is the blast radius, widest first: only a contaminated world reaches the NEXT
    # invocation. All after the archive is on disk — evidence first, verdict second. Read off
    # the LATCH too: a scenario abandoned at SEEDING has no row to carry the error.
    if (live or rehearsal) and (report.contaminated_scenarios or chaos_block_reason() is not None):
        named = ", ".join(report.contaminated_scenarios) or (chaos_block_reason() or "")
        print(
            "TEARDOWN FAIL: the shared world is contaminated and further live runs "
            f"are blocked ({named}). This run's grades stand; the environment does not."
        )
        print(
            "Restore it — the reset clears the block on success:\n"
            "  make eval-reset PURGE_IDEMPOTENCY=1\n"
            "Only where there is no stack left to reset, clear it alone:\n"
            "  uv run python -m evals.runner --clear-chaos-block"
        )
        return 10
    if report.ungraded:
        print(
            f"WORLD FAIL: {len(report.ungraded)} scenario(s) could not be seeded and "
            "were not graded — this is a statement about the environment, not the agent."
        )
        return 9
    return 0 if report.failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
