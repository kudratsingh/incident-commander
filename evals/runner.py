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
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any, Final, TypedDict
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
from evals.preconditions import unmet
from evals.recorded_client import RecordedMCPClient, matching_recordings
from evals.scenarios.loader import load_scenarios
from evals.scenarios.schema import ChaosHook, ChaosPlan, Scenario
from evals.tracing import JsonlTracer, TraceKind, tracer_for
from incident_commander.agent.accounting import RunAccounting, accrue_llm_error
from incident_commander.agent.briefing import EscalationBriefing, render_briefing
from incident_commander.agent.briefing_enrichment import enrich_briefing
from incident_commander.agent.factory import start_run
from incident_commander.agent.investigation import (
    _DEFAULT_MAX_ITERATIONS,
    make_llm_investigate,
)
from incident_commander.agent.loop import run_to_completion
from incident_commander.agent.orchestrator import TRANSITIONS, Transition
from incident_commander.agent.reflection import CRITIC_ROLE
from incident_commander.agent.remediation import (
    make_llm_plan,
    make_llm_verify,
    make_remediate,
)
from incident_commander.agent.selection import SELECTOR_ROLE
from incident_commander.agent.state import BudgetLedger, EvidenceEntry, IncidentState, RunState
from incident_commander.agent.strategies.knobs import StrategyKnobs
from incident_commander.agent.strategies.protocol import InvestigationStrategy
from incident_commander.agent.strategies.records import StepRecord, StepSink
from incident_commander.agent.strategies.registry import STRATEGIES
from incident_commander.config import ChaosTokenNotConfigured, ModelRole, Settings
from incident_commander.llm.client import LLMClient, LLMClientProtocol, LLMError, preflight_auth
from incident_commander.llm.fakes import CannedLLMClient
from incident_commander.llm.repair import (
    OUTPUT_INVALID_PREFIXES,
    PLANNER_OUTPUT_INVALID_CLASS,
)
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
# The three directories above hold VERSIONED files, never overwritten, every write
# exclusive-create (invariant 9 — the old flat "refreshable pointers" are withdrawn).
# There is no ``latest.json`` and no symlink: the newest version is resolved in exactly
# one place, ``evals/artifacts.py``, by the stamp in the FILENAME and never by mtime.
#
# ``evals/runs/<invocation_id>/`` is the append-only archive of a whole run, written
# INCREMENTALLY — each scenario's files land as it finishes (``archive_scenario``) and
# ``report.json`` is written LAST as the completion marker, so a directory without one
# is a killed run whose per-scenario files are still evidence (ADR 0017). Files are
# locked read-only (+``uchg``) as they land and the tree is sealed with the marker
# (ADR 0021): invariant 9, enforced by the filesystem.
_RUNS_DIR = _REPO_ROOT / "evals" / "runs"
_TRACE_DIR_ENV = "EVAL_TRACE_DIR"


_UNKNOWN: Final[str] = "unknown"
_COMPOSE_FILE = _REPO_ROOT / "demo" / "compose.yml"
# The compose service publishing the MCP surface the agent is evaluated through. Its
# ``image:`` line is where the digest is READ FROM, so there is no second copy to keep
# in step (C-10); that the other services move with it is pinned by
# ``test_demo_docs.py::test_every_repository_resolves_to_one_ref``.
_PLATFORM_SERVICE: Final[str] = "platform"
# How many scenario names the non-closing mark spells out before counting the
# rest (see ``RunReport.non_closing_reason``).
_NON_CLOSING_NAMES_SHOWN: Final[int] = 5
# Where a failed chaos teardown records that the shared world is dirty. On DISK
# because what it protects is the NEXT invocation, which would read a standing fault as
# its baseline — a flag that dies with the process protects nothing. Not under
# ``evals/runs/``: it is mutable operational state with two transitions, and the
# durable record of the same event is ``ScenarioOutcome.teardown_error``.
_CHAOS_BLOCK_PATH = _REPO_ROOT / "evals" / ".chaos-teardown-block.json"


class ExecutionMode(StrEnum):
    """How the world under a run was produced.

    ``RECORDED`` (WP-3.3) replays one moment of the live world from disk (ADR 0043)
    while the model calls stay real. Its own value, not ``LIVE`` with a flag (ADR 0013),
    and APPENDED, because archived reports are read back against this enum. Valid for
    diagnosis, plan, candidate metrics and calibration only (plan 02 § 189, 03 § 52).
    """

    CANNED = "canned"
    LIVE = "live"
    RECORDED = "recorded"


class RunProvenance(BaseModel):
    """Exactly what produced one run: code, world, models, role, budgets.

    ADR 0013's question ("real system or canned model of it?") extended to identity, so
    a saved run names its code, image, model, role, strategy and seeded budgets. Every
    string field is populated or explicitly ``"unknown"``, a claim a reader can act on.
    Per SCENARIO, because a scenario is the unit a leaderboard row is built from.
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
    # The run's OWN ledger: ``max_*`` are the budgets actually seeded, not the
    # documented defaults (ADR 0019's per-scenario cap, and the paid-run protocol's
    # .env-only ceilings), and ``*_used`` are ADR 0015's four meters.
    budget: BudgetLedger


@lru_cache(maxsize=1)
def commander_revision() -> str:
    """``git rev-parse HEAD``, or ``"unknown"`` outside a git checkout.

    Never omitted: a report that cannot name its revision is still about SOME revision,
    and saying so is the honest form of not knowing. Every failure shape is tolerated —
    the alternative is a run that cannot start over a metadata read.
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

    From the compose file, not a constant here, so the recorded digest is the one the
    stack came up on: a second copy goes stale two releases later (C-10), and a record
    naming the wrong platform is worse than one naming none.
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
        # Not pinned by digest at all. `tests/unit/test_demo_docs.py` refuses
        # that in CI; here it is reported rather than guessed at.
        return _UNKNOWN
    return image.split("@", 1)[1]


def strategy_knobs(settings: Settings) -> StrategyKnobs:
    """The inference block the selected strategy is built with (WP-5.2).

    The edge configuration is wired at: nothing under ``agent/`` reads ``Settings``.
    One function for both builders, so the arm that ran and the arm the artifact names
    cannot differ. The budget multipliers are absent: ``factory.start_run`` applies them
    once, and ``test_budgets.py::TestNoOtherCallSiteScalesABudget`` refuses a second.
    """
    return StrategyKnobs(
        n=settings.best_of_n,
        sample_temperature=settings.sample_temperature,
        selector_generator=settings.selector_generator.value,
    )


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
) -> RunProvenance:
    """Assemble one run's provenance record from the run's own inputs.

    Takes the LEDGER rather than re-reading the settings: the record's budgets must be
    the seeded ones (ADR 0019, and the protocol's .env-only ceilings). ``strategy`` is
    the object that made the planner calls (WP-0.2), so the stamp comes from what ran;
    ``None`` is the crash path, where the configured strategy is the only honest answer.
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
        # "" reaches here from a direct ``run_scenario`` call. Recorded as "unknown"
        # for the reason the two reads above are: an empty string reads like a value.
        invocation_id=invocation_id or _UNKNOWN,
        recorded_at=recorded_at or datetime.now(UTC),
        execution_mode=execution_mode,
        budget=budget,
    )


class RoleAccounting(BaseModel):
    """One prompt role's share of a run's bill (plan 03 § 7.8).

    Role is the unit a strategy changes — a best-of-N arm spends its extra tokens in
    ``investigation_planner`` alone — so a run total cannot tell "the strategy cost
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

    Divergence D3: WP-0.3's ledger answers the total and not "on which role, over how
    many steps, carrying how much context?", which are the accuracy/cost frontier's
    columns. ``reconciled`` makes the rest usable — the row states both sides rather
    than asserting an agreement the reader cannot check.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    by_role: tuple[RoleAccounting, ...] = ()
    llm_calls: int = 0
    # From the run's own ledger, not counted again here.
    tool_calls: int = 0
    wall_seconds: float = 0.0
    # Time inside LLM calls, deliberately not ``wall_seconds``: the loop also probes,
    # waits out ADR 0009's window and grades, and conflating them would attribute a
    # 75-second sleep to the model.
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
    # The benchmark's grouping keys (WP-1.4) on the ROW: joining a report back to
    # ``evals/scenarios/`` reads today's classification against last month's run.
    # ``None`` predates the record, tolerated as ``provenance``'s absence is.
    template_id: str | None = None
    seed: int | None = None
    family: str | None = None
    difficulty: str | None = None
    benchmark_split: str | None = None
    final_state: IncidentState
    tool_calls_used: int
    report: GradeReport
    judge_score: JudgeScore | None = None
    # Five-bucket noise-source classification (docs/lessons/live-eval-noise-sources.md)
    # plus "passed" and "unclassified". Heuristic: a starting point for
    # bucket-before-you-debug, not a verdict.
    failure_class: str = "unclassified"
    # Why the bucket, in enough detail to act on without opening the trajectory: on
    # INC-001 the class was right and the whole trace still had to be read.
    failure_class_detail: str = ""
    # Set when the briefing judge call itself failed: the scenario result
    # stands (graded deterministically); only the judge column is missing.
    judge_error: str | None = None
    # Set when post-grade briefing enrichment failed: the deterministic briefing stands
    # and the grade holds; only the LLM-written findings/recommendation are missing.
    briefing_error: str | None = None
    # What the ChaosPlan did, setup then teardown, in firing order (WP-1.1).
    # Evaluator-only: the archive's answer to "what world was this graded in?", which
    # used to need a trace file. Empty on canned runs and live runs that seed nothing.
    chaos_hooks: tuple[ChaosHookRecord, ...] = ()
    # Set when a teardown hook failed — not a statement about THIS run, whose grade
    # stands, but about the SHARED environment the next run would inherit. The latch on
    # disk (``_CHAOS_BLOCK_PATH``) refuses that run; this is the durable record of why.
    teardown_error: str | None = None
    # Run provenance (ADR 0013): which legs ran live, and whether a declared-live leg
    # fell back to canned. The defaults are load-bearing — archived reports and the
    # committed baseline predate these fields and must keep parsing.
    live_mcp: bool = False
    live_llm: bool = False
    degraded: bool = False
    # What produced this row (WP-0.3): revision, digest, model and role, strategy,
    # seeded budgets and spend. ``None`` means "predates the record", which every
    # archived report does — tolerated on ADR 0013's own precedent above.
    provenance: RunProvenance | None = None
    # What the run cost, by role and by step (WP-2.3). ``None`` means "no measurement
    # exists" — every archived report, and a crash before the first call — never "this
    # run was free", which a zeroed record would have said.
    accounting: RunAccountingRecord | None = None
    # What the REPLAY did, on a recorded run only (WP-3.3), in
    # ``RecordedMCPClient.summary()``'s own shape — untyped, so that vocabulary has one
    # home. The miss count is load-bearing: it sets ``degraded``.
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

    Beside ``outcomes``, not inside it, and the placement IS the decision: there is no
    honest ``GradeReport`` for a run that never happened, and a red "crashed" row would
    enter the pass rate (`bb1fa70abb4c`). So the report says three things, and the third
    cannot be got by subtraction.
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
    # ``None`` = pre-schema ("unknown"), deliberately distinct from 0 ("verified fully
    # live"): the committed baseline was a 32/37-degraded canned run, and defaulting to
    # 0 would make that artifact assert a falsehood.
    degraded_count: int | None = None
    # Which --only filters produced this report, so filtered runs
    # self-describe in latest.json and in the archive. Empty = full suite.
    only_patterns: tuple[str, ...] = ()
    # Whether this report may CLOSE a phase (plan 03 § 14). Tri-state on
    # ``degraded_count``'s reasoning, and the validator below refuses a ``True`` over a
    # development run, so the mark cannot disagree with its own rows.
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
    def non_closing_reason(self) -> str:
        """Why this report cannot close a phase, or "" if it can.

        Reported as a sentence rather than a flag because the reader's next
        question is always "which runs?", and the answer is the list of
        scenarios that has to be re-run under the benchmark role.
        """
        if self.closing is None:
            return "predates model roles (no run in it records one)"
        if self.closing:
            return ""
        development = self.development_scenarios
        if development:
            # Named but bounded: 40 names is a sentence nobody finishes. The count is
            # exact, the list a sample, and ``development_scenarios`` has them all.
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
        """A report may not claim it can close a phase while carrying a
        development run.

        The one direction that has to be refused. The other direction is
        legitimate: a report with no development run in it can still be
        marked non-closing for a reason outside this model (a degraded leg,
        an incident under investigation). This is the ``degraded_count``
        lesson applied to a second field — a persisted number that can
        contradict the rows beneath it is worse than no number at all.
        """
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
    # Which invocation produced this, so a mixed-vintage directory self-describes even
    # with the filenames lost — how Run 001's live trajectories were erased (F-003).
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

    The crash row used to be built from the exception alone, so it hardcoded
    ``tool_calls_used=0`` and a scenario that crashed after nine calls reported none.
    ``checkpoints`` is the run's own history to the last completed transition — still a
    lower bound, but one derived from what the run did. Raised only from
    ``run_scenario``'s handler: seeding and precondition failures propagate unwrapped,
    because there is no run to describe.
    """

    def __init__(self, cause: BaseException, checkpoints: tuple[RunState, ...]) -> None:
        super().__init__(f"{type(cause).__name__}: {cause}")
        self.cause = cause
        self.checkpoints = checkpoints
        # Set when the teardown after a crashed run ALSO failed. Beside the cause, not
        # folded in: the crash is about the agent, this is about the world, and the
        # second decides whether the next live run may start.
        self.teardown_error: str | None = None
        # What the run had billed when it died, by role (WP-2.3): a crashed run's spend
        # is spend, and a row that cannot name it makes the cost columns a lower bound.
        self.accounting: RunAccounting | None = None
        # The ledger the spend was charged to, NOT ``final.budget``: the briefing
        # writer bills after the last checkpoint (WO-R3-260), so reconciling against
        # the checkpoint would report which object was read as a disagreement.
        self.ledger: BudgetLedger | None = None

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

    The pair's whole point: "the fault was never manufactured" is a claim about the
    world and needs the world to have answered, so reporting a dead platform as one
    sends the reader to seeding. "Answered" means the DECISIVE attempt — the one that
    ended the polling window — because an earlier readable reading is stale.
    """


def _assert_preconditions(
    scenario: Scenario,
    client: MCPClientProtocol,
    tracer: JsonlTracer | None,
) -> None:
    """Probe the world for the scenario's premise, polling where declared."""
    for probe in scenario.expected_precondition:
        # Only the DECISIVE attempt speaks for the world: `reading` is its verdict when
        # readable (empty list = met), `None` otherwise. Latching "did any attempt ever
        # answer" let a dead platform report as "the fault was never manufactured".
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


class ChaosHookRecord(BaseModel):
    """What one chaos invocation did, recorded evaluator-side.

    Evaluator-only by construction: reachable from ``ScenarioOutcome`` and the archive,
    never from ``RunState``, the evidence ledger or a prompt — telling the agent which
    faults were seeded would measure recall of the answer key (WP-1.3 tests that
    boundary). ``result`` holds the hook's own parsed response, so the archive can say
    what the world WAS without a trace file.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    phase: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    ok: bool = True
    result: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


#: ``ChaosHookRecord.phase`` for a hook that MANUFACTURED the world, as
#: opposed to one that put it back.
CHAOS_SETUP_PHASE: Final[str] = "setup"


def seeded_chaos(records: Iterable[ChaosHookRecord]) -> bool:
    """Did this run manufacture its own fault world?

    INC-003's rule needs it (``root_cause.label_describes_this_world`` is the rule).
    Successful SETUP records only — a teardown says the world was put BACK. Reads the
    RECORDS, not ``Scenario.seeds_chaos``: the question is what world this run was
    actually in, and a scenario file may have gained or lost a hook since.
    """
    return any(record.phase == CHAOS_SETUP_PHASE and record.ok for record in records)


def _invoke_plan_hook(
    scenario: Scenario,
    hook: ChaosHook,
    settings: Settings,
    tracer: JsonlTracer | None,
    *,
    phase: str,
) -> ChaosHookRecord:
    """Fire one hook under the chaos principal and record what happened.

    A literal second credential since v0.6.5: while the agent's own token carried
    ``chaos:invoke``, the chaos-audit filter was inert and ``list_audit_events`` handed
    the agent the hook (G3, O-4). Never raises for a hook failure — the callers disagree
    about what one MEANS. A MISSING credential DOES raise: nothing was attempted.
    """
    record = ChaosHookRecord(phase=phase, name=hook.name, arguments=dict(hook.arguments))
    chaos_token = settings.require_chaos_token()
    try:
        result = invoke_chaos_hook(
            str(settings.platform_mcp_url),
            chaos_token,
            hook.name,
            dict(hook.arguments),
        )
    except ChaosInvocationError as err:
        # ``err`` carries the platform's own refusal NAME (evals/chaos_hooks.py), which
        # is the difference between "poison_fixture_name_in_use: reset, do not retry"
        # and an anonymous -32011 that reads like flakiness. Verbatim for that reason.
        record = record.model_copy(update={"ok": False, "error": str(err)})
    else:
        record = record.model_copy(update={"result": result})
    if tracer is not None:
        tracer.write(
            {
                # One kind for both halves of a plan, with ``phase`` saying which: a
                # second TraceKind would need a renderer, and the record is the same
                # shape either way.
                "kind": TraceKind.CHAOS_SETUP,
                "scenario": scenario.name,
                "phase": phase,
                "hook": hook.name,
                "arguments": dict(hook.arguments),
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

    Order is the declaration, not an implementation detail: a cascade is only the world
    it claims to be if its second fault lands on the first — which is also why the
    hooks after a failure are not fired.
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

    Never raises and never stops early: this is the janitor on the way out of a run
    that may already carry an exception, so raising would replace the run's own cause
    and stopping early would skip compensators that might still work.
    ``ChaosTokenNotConfigured`` becomes the same text for that reason — the right
    outcome is the dirty-world latch the caller writes from it.
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

    Reads the latch a failed teardown wrote. A malformed file still BLOCKS: something
    wrote it, and "we cannot tell what went wrong" does not read as "carry on
    spending". The path resolves at CALL time so a test can redirect the constant.
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

    Not instead of ``make eval-reset``, which is what restores the world; this only
    records that it happened. The reset's LAST recipe line runs this, deliberately last:
    make abandons a recipe at the first failing line, so a reset that did not succeed
    never clears the latch.
    """
    path = path or _CHAOS_BLOCK_PATH
    reason = chaos_block_reason(path)
    if reason is None:
        return None
    path.unlink(missing_ok=True)
    return reason


def _step_sink(tracer: JsonlTracer) -> StepSink:
    """Send each planner step's ``StepRecord`` to the trace store (WP-2.1).

    One JSONL line per step, beside the same invocation's ``llm`` and ``mcp`` records
    and joinable by ``call_id``, append-only (invariant 9, F-002). Wired whenever a
    tracer exists, canned runs included: a canned run's DECISIONS are real research data
    (divergence D1). The kind is stamped here, so it comes from ``TraceKind``.
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
    # ``RunState.remediation_plan`` is a plain mapping on the checkpoint, so it is read
    # by key: a report row needs the three values, not ``remediation.py``'s schema.
    plan = final.remediation_plan
    planned_tool = None if plan is None else plan.get("action_tool")
    expected = tuple(scenario.expectation.expected_action_tools)
    record["plan"] = {
        "expected_action_tools": list(expected),
        "planned_action_tool": planned_tool,
        "planned_action_arguments": None if plan is None else plan.get("action_arguments"),
        "planned_verify_tool": None if plan is None else plan.get("verify_tool"),
        # ``None``, not ``False``, when there is nothing to compare: a boolean would
        # read as "the plan was wrong", and "there was no plan" is a different fact.
        "matches_expected": (
            None if (planned_tool is None or not expected) else planned_tool in expected
        ),
    }
    return record


#: What a recorded run's evidence ledger records where it stopped. Once, because three
#: readers compare against it: the trajectory, the ``replay`` row, and the test that
#: proves the run was truncated rather than merely unlucky.
RECORDED_HANDOFF_REASON: Final[str] = (
    "recorded mode: stopped at the PLANNING handoff. A recording has no state to "
    "change and no audit log to observe a change in, so the plan was made and not "
    "executed; OUTCOME, ACTION and SAFETY are not graded for this run."
)


class RecordedHandoff:
    """Ends a recorded run where the plan is made, before anything is executed.

    Installed for BOTH acting successors on EVERY recorded run, not only those declaring
    ``expected_action_tools``: an agent that reaches the handoff anyway would hand the
    replay client a Tier-1 call, which is refused (ADR 0044) and is also a crash. Stops
    at ``ESCALATED`` rather than inventing a state (ADR 0002); ``fired`` says truncated.
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

    ``OUTCOME``, because the handoff partly chose the terminal state; ``ACTION``, because
    nothing executed; ``SAFETY``, because invariant 6 grades it from an audit log a
    recording has none of. Plus ``EVIDENCE`` when truncated — a DIVERGENCE from
    WO-R3-198's three, forced because those claims read the action tool's own response
    and the handoff writes an entry that would satisfy a substring claim. ``BUDGET`` and
    ``ROOT_CAUSE`` stay graded, and ``MODE_APPLICABLE_DIMENSIONS`` refuses the latter.
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


def run_scenario(
    scenario: Scenario,
    settings: Settings,
    clock: Callable[[], datetime] | None = None,
    mcp_token: str | None = None,
    invocation_id: str = "",
    model_role: ModelRole = ModelRole.DEVELOPMENT,
    recorded_world: Path | None = None,
) -> ScenarioResult:
    """Drive one scenario end-to-end and grade the result.

    Offline: ``CannedMCPClient`` plus ``CannedLLMClient`` per role, each with its own
    ``canned_llm_responses`` queue. ``recorded_world`` IS the RECORDED mode selector
    (WP-3.3) — a path, so no flag can disagree — and it changes four things: a
    ``RecordedMCPClient`` reaches no platform; nothing is seeded, settled, polled or torn
    down; the run stops at the ``PLANNING`` handoff (04:117); and OUTCOME, ACTION and
    SAFETY become not-applicable. NOT the model: its planner calls are real and cost
    live rates. ``model_role`` is recorded, not resolved, and defaults to ``DEVELOPMENT``.
    """
    tick = clock or (lambda: datetime.now(UTC))
    now = tick()

    # Everything this function is allowed to hand the agent, read once, from
    # the scenario's own allow-list projection (WP-1.3). The evaluator-only
    # fields — ``ground_truth`` above all — are not on it and cannot be
    # reached through it, so a future field on ``Scenario`` cannot leak into
    # a run by a call site here forgetting to leave it out.
    agent_visible = scenario.agent_visible()

    # RECORDED mode, decided once and read everywhere below. The world is a
    # file, so every live-platform behaviour this function has — seeding,
    # settling, preconditions, teardown, the real transport — is off.
    recorded = recorded_world is not None

    # use_live_* means "prefer live if env is real, else fall back to canned."
    # Nothing skips just because env is placeholder — canned data is the
    # deterministic offline fallback for `make eval` / CI.
    #
    # ``not recorded`` comes FIRST and is not an ``and`` at the end by accident:
    # a recorded run under a real ``PLATFORM_MCP_URL`` is the one combination
    # that could silently seed chaos into the shared world and read it live
    # while the row claimed to be a replay. The recording is the world, or there
    # is no run.
    live_mcp_available = (
        not recorded
        and scenario.use_live_mcp
        and not _is_offline_placeholder(str(settings.platform_mcp_url))
    )
    live_llm_available = scenario.use_live_llm and not _is_offline_api_key(
        settings.anthropic_api_key.get_secret_value()
    )

    # Tracing (opt-in): when EVAL_TRACE_DIR is set, capture every LLM +
    # MCP call for this scenario into a JSONL file. The call hooks only wire
    # into the live clients — canned clients are already deterministic and
    # have no raw request/response to capture. The per-step ``StepRecord``s
    # (WP-2.1) are written on both paths: what a canned run decided is as
    # real as what a live run decided, and it is the offline half of every
    # strategy comparison.
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
                "kind": TraceKind.SCENARIO_START,
                "scenario": scenario.name,
                "live_mcp": live_mcp_available,
                "live_llm": live_llm_available,
                "model": settings.agent_model,
                "model_role": model_role.value,
                "judge_model": settings.judge_model,
            }
        )

    # The fault in one shape however the YAML spells it (a legacy ``chaos_setup``
    # normalizes to a one-hook plan). Read once, so setup and teardown share a tuple.
    chaos_plan = scenario.chaos
    chaos_records: tuple[ChaosHookRecord, ...] = ()
    teardown_error: str | None = None

    def _tear_down() -> str | None:
        """Compensate the plan, latch the world dirty if that failed.

        Called on every way out of the live path — clean return, agent
        crash, unmet precondition, failed seeding — which is what "teardown
        in ``finally``" means here. It is a named call at each exit rather
        than a literal ``finally`` for one reason: on the failing path the
        teardown's own failure has to be attached to the exception that is
        already in flight, and a ``finally`` block cannot see it without
        reaching into ``sys.exc_info()``.

        A canned run tears nothing down because it seeded nothing.
        """
        nonlocal chaos_records
        if not live_mcp_available:
            return None
        records, error = _teardown_chaos_plan(scenario, chaos_plan, settings, tracer)
        chaos_records += records
        if error is not None:
            # The latch, before the exception (if any) leaves this function:
            # a killed process must still find the world marked dirty.
            write_chaos_block(scenario.name, error, invocation_id=invocation_id)
        return error

    mcp_client: MCPClientProtocol
    live_mcp_client: MCPClient | None = None
    replay_client: RecordedMCPClient | None = None
    if recorded_world is not None:
        # The replay clock is the run's OWN clock, so every age the agent computes is
        # the one the recorder observed (ADR 0044). Built first, so a recording that
        # will not load costs nothing.
        replay_client = RecordedMCPClient.from_path(recorded_world, replay_clock=now)
        mcp_client = replay_client
        print(
            f"  replay: {_repo_relative(recorded_world)} — "
            f"{len(replay_client.world.calls)} recorded call(s), world "
            f"{str(replay_client.summary()['world_fingerprint'])[:12]}, "
            f"replay offset {int(replay_client.offset.total_seconds())}s"
        )
        # The coherence lints ran at record time and their findings travel
        # INSIDE the recording (ADR 0043). Printed here, on every recorded run,
        # because that is the point of carrying them: "a recorded world that
        # contradicts itself is a fixture defect, not a benchmark" (plan 02
        # § 187), and a finding nobody sees is a finding nobody acts on. A
        # finding is not a verdict and does not refuse the run — the dossier's
        # own rule, for the reason its lint docstring gives: a lint that returns
        # a verdict becomes a gate somebody tunes to green.
        for finding in replay_client.world.findings:
            print(f"  replay lint [{finding.kind}] {finding.subject}: {finding.detail}")
    elif live_mcp_available:
        # Fire the scenario's declared setup hooks BEFORE building the
        # agent's client so a seeding failure surfaces immediately with a
        # clear reason, not as a downstream "read returned healthy" bug.
        # Canned runs skip this — the canned tool responses already encode
        # the broken state.
        #
        # The teardown region opens HERE, before the first setup hook is
        # attempted, not after seeding succeeds: ``_seed_chaos_plan`` stops
        # at the first failed hook and raises, and the hooks that already
        # fired are exactly what ``plan.teardown`` compensates.
        try:
            chaos_records = _seed_chaos_plan(scenario, chaos_plan, settings, tracer)
            # One wait for the whole plan, ahead of the preconditions' per-probe
            # polling: a cascade's second-order effect is not visible instantly.
            if chaos_plan.settle_seconds:
                time.sleep(chaos_plan.settle_seconds)
            live_mcp_client = make_client(
                settings,
                tracer=tracer.mcp_hook() if tracer else None,
                token=mcp_token,
            )
            mcp_client = live_mcp_client
            # The premise, after seeding and before the first model call, so a fault
            # that was never manufactured costs one read instead of a graded run
            # (`bb1fa70abb4c` paid for this lesson).
            if scenario.expected_precondition:
                try:
                    _assert_preconditions(scenario, live_mcp_client, tracer)
                except PreconditionFailure:
                    live_mcp_client.close()
                    live_mcp_client = None
                    raise
        except BaseException:
            # The world was touched, so it goes back even though nothing is graded.
            # The return value is dropped — there is no row to carry it on, and
            # ``_tear_down`` has already written the latch.
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
        # One underlying HTTP client per role so each gets its own tracer
        # hook label — that's what makes the JSONL readable per role.
        investigation_llm = LLMClient(
            api_key=api_key,
            tracer=tracer.llm_hook("investigation_planner") if tracer else None,
        )
        # WP-6.2's new role, with its own client so its trace records carry their own
        # hook label — what makes the JSONL readable per role.
        selector_llm = LLMClient(
            api_key=api_key,
            tracer=tracer.llm_hook(SELECTOR_ROLE) if tracer else None,
        )
        # WP-9.1's new role, its own client for the same reason.
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

    # Cost, latency and context accounting (WP-2.3). Every reachable LLM client is
    # wrapped, so the split is complete by construction and covers the canned suite,
    # where no tracer exists to derive one from (divergence D1).
    #
    # ``charged_to_ledger`` is declared HERE, the only place that knows it, and it draws
    # the line at WHOSE money a call is rather than when it happened. Both post-run
    # roles bill after the terminal state and part company there: ``briefing_writer`` is
    # the AGENT's own cost, so it is charged (ADR 0015 § 4 as amended — metered, never
    # gating, since no budget check is left); ``briefing_judge`` is the EVALUATOR's, so
    # it stays out of the ledger and the reconciliation. Both are RECORDED either way.
    accounting = RunAccounting()
    investigation_llm = accounting.meter(investigation_llm, "investigation_planner")
    # The selector is the AGENT's own cost — it decides the run's diagnosis — so
    # it is charged to the ledger like the planner and unlike the briefing judge.
    selector_llm = accounting.meter(selector_llm, SELECTOR_ROLE)
    # The critic is the AGENT's own cost too: it decides whether the run re-plans.
    critic_llm = accounting.meter(critic_llm, CRITIC_ROLE)
    remediation_planner_llm = accounting.meter(remediation_planner_llm, "remediation_planner")
    verification_judge_llm = accounting.meter(verification_judge_llm, "verification_judge")
    briefing_llm = accounting.meter(briefing_llm, "briefing_writer")
    judge_llm = accounting.meter(judge_llm, "briefing_judge", charged_to_ledger=False)

    # The inference strategy (WP-0.2), resolved at the edge that owns configuration and
    # passed to both the loop and the provenance record, so the two cannot disagree. An
    # unknown INFERENCE_STRATEGY raises here, before anything is spent.
    strategy = STRATEGIES.create(settings.inference_strategy, strategy_knobs(settings))
    transitions: dict[IncidentState, Transition] = dict(TRANSITIONS)
    transitions[IncidentState.INVESTIGATING] = make_llm_investigate(
        mcp_client,
        investigation_llm,
        model=settings.agent_model,
        strategy=strategy,
        # Per-step research records: always to the run's accounting (WP-2.3 needs every
        # step's context size), and on to the trace store when one was built. The sink
        # composes, it does not choose.
        record_step=accounting.step_sink(_step_sink(tracer) if tracer is not None else None),
        # Passed on every run, read by one arm each: the strategies that make no selector or
        # critic call never touch these, and the ones that do refuse rather than borrowing.
        selector_llm_client=selector_llm,
        critic_llm_client=critic_llm,
        # Freshness re-probe (ADR 0009) is live-only: canned responses are
        # instant-consistent, and a re-probe would eat an extra scripted response.
        reprobe_attempts=(settings.investigate_reprobe_attempts if live_mcp_available else 0),
        reprobe_delay_seconds=settings.investigate_reprobe_delay_seconds,
        # WP-2.4's third knob at its only consumer (WO-R3-256). Unset means the loop's
        # own default, NAMED rather than re-declared, so no second copy can drift.
        max_iterations=(
            _DEFAULT_MAX_ITERATIONS
            if settings.max_iterations_override is None
            else settings.max_iterations_override
        ),
    )
    # Phase 6 remediation loop: PLANNING → REMEDIATING → VERIFYING. A client per role,
    # so canned queues stay partitioned and tracer records label each call.
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
        # JUDGE_MODEL, not agent_model: this decides RESOLVED and was the one judgement
        # on an unpinned model. It judges an expectation the agent wrote for itself,
        # which is weaker than an `expected_evidence_fields` claim.
        model=settings.judge_model,
        # Poll the verify probe only against a real platform; canned
        # responses are instant-consistent so one read is authoritative.
        probe_attempts=settings.verify_probe_attempts if live_mcp_available else 1,
        probe_delay_seconds=settings.verify_probe_delay_seconds,
        # The loop's own clock, so a multi-minute polling window stamps each attempt
        # with when it happened rather than reusing the transition's entry read.
        clock=tick,
    )
    # RECORDED mode stops where the plan is made (04:117). AFTER the two transitions it
    # replaces, and over BOTH: an approval against a recording is as meaningless as an
    # action.
    handoff = RecordedHandoff()
    if recorded:
        transitions[IncidentState.REMEDIATING] = handoff
        transitions[IncidentState.AWAITING_APPROVAL] = handoff

    # Outside the try so a crash can still read what the run had spent when
    # it died — see ScenarioCrash.
    checkpointer = InMemoryCheckpointer()
    run: RunState | None = None
    # Same reason one field further: the post-terminal briefing charge is in no
    # checkpoint, so a crash after enrichment would reconcile a split containing that
    # call against a ledger predating it and report a false ``COST UNRECONCILED``.
    run_ledger: BudgetLedger | None = None
    try:
        # The scenario's declared cap IS the run's ceiling (ADR 0019), not only the
        # number it is graded against: the planner used to be told the fleet default of
        # 25 even in the scenarios about behaviour under a tight budget.
        run = start_run(
            agent_visible.alert,
            settings,
            now,
            max_tool_calls=agent_visible.max_tool_calls,
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
        # Built BEFORE grading, because ``expect_briefing_contains`` grades the handoff
        # as the human receives it, enrichment included. Grading still makes no LLM call
        # of its own; it reads a finished object.
        briefing = render_briefing(final)
        briefing_error: str | None = None
        # The ledger AFTER the post-terminal briefing charge (WO-R3-260), never put back
        # on ``final``: BUDGET is graded from that, so the charge would let a ceiling
        # decide a finished outcome. Metered, never gating (ADR 0015 § 4 as amended).
        run_ledger = final.budget
        if scenario.use_live_llm or (briefing_canned is not None and briefing_canned.has_remaining):
            try:
                briefing, run_ledger = enrich_briefing(
                    briefing, briefing_llm, model=settings.agent_model, budget=run_ledger
                )
            except (LLMError, ValidationError) as err:
                # Charge what the failed call billed, as the investigation loop does:
                # leaving it would break the reconciliation on exactly the run that
                # cost money for nothing.
                run_ledger = accrue_llm_error(run_ledger, err, settings.agent_model)
                # A decoration, like the judge: losing it must not void the run
                # (ADR 0007), and a scenario asserting on briefing text grades red,
                # which is honest. ValidationError because ``CannedLLMClient``
                # validates its own payloads and never reaches ``_parse``.
                briefing_error = f"briefing enrichment failed: {err}"
        # Which world this run was actually in (INC-003, ADR 0040). Only the runner
        # knows: ``grade()`` is a pure function of its arguments and must stay one.
        if replay_client is not None:
            # A recording states which world it is (ADR 0043 § 4) and is the only
            # authority here: ``live_mcp_available`` is False, so the derivation below
            # would strike out the one dimension recorded mode exists to measure.
            replay_label = replay_client.world.world
            world_matches_ground_truth = label_describes_this_world(
                live_mcp=replay_label.live_mcp,
                chaos_seeded=replay_label.chaos_seeded,
            )
        else:
            # ``chaos_records`` holds the SETUP hooks here (teardown runs after
            # grading); a canned run seeded nothing and is in the label's world anyway.
            world_matches_ground_truth = label_describes_this_world(
                live_mcp=live_mcp_available,
                chaos_seeded=seeded_chaos(chaos_records),
            )
        report = grade(
            final,
            scenario.expectation,
            briefing=briefing,
            # The answer key, read here and nowhere else: the run was built from
            # ``agent_visible()`` (ADR 0038), so this is the only point where the two
            # sides of the boundary meet. ``None`` grades ROOT_CAUSE vacuously.
            ground_truth=(
                None if scenario.ground_truth is None else scenario.ground_truth.root_causes
            ),
            # Which world the run was in (INC-003), decided above because the answer
            # differs by mode and nobody reads a conditional inside an argument list.
            world_matches_ground_truth=world_matches_ground_truth,
            # Which dimensions this MODE cannot make a claim about (WP-3.3).
            # ``None`` for every other mode, so nothing else changes.
            not_applicable=(recorded_not_applicable(truncated=handoff.fired) if recorded else None),
        )
        judge_score: JudgeScore | None = None
        judge_error: str | None = None
        if scenario.use_live_llm or (judge_canned is not None and judge_canned.has_remaining):
            try:
                judge_score = judge_briefing(briefing, judge_llm, model=settings.judge_model)
            except (LLMError, ValidationError) as err:
                # A soft-quality column on an already-graded run, so losing it must not
                # void the run. ValidationError for the canned-client reason above, and
                # because constrained decoding does not guarantee JudgeScore's bounds.
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
        # The run is over, so the world goes back before the exception leaves. A
        # teardown failure rides ALONG with the crash: "the agent crashed" and "the
        # world is dirty" are two facts, and collapsing them loses one.
        crash = ScenarioCrash(
            exc,
            () if run is None else tuple(checkpointer.history(run.incident_id)),
        )
        if live_mcp_client is not None:
            live_mcp_client.close()
            live_mcp_client = None
        crash.teardown_error = _tear_down()
        # What the run had billed before it died: the crash row is the only place this
        # reaches a report, and a crashed run's spend is spend.
        crash.accounting = accounting
        # And the ledger it was charged to, when the run got far enough to have one:
        # ``None`` falls back to the last checkpoint.
        crash.ledger = run_ledger
        # Re-raise carrying the run's own history, so the crash row reports
        # what the scenario actually spent instead of a hardcoded zero.
        raise crash from exc
    finally:
        if live_mcp_client is not None:
            live_mcp_client.close()
            live_mcp_client = None

    # After the run, before the row that reports it: the outcome carries
    # ``teardown_error``, so the teardown has to have happened by now.
    teardown_error = _tear_down()
    # ``run_ledger`` is bound the moment the loop returns, which this path implies; the
    # fallback makes that visible to the type checker.
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
        # A recorded run is degraded when the recording missed or refused a call: the
        # field means "this row is not the measurement it looks like" (ADR 0044). NOT
        # true merely for replaying — that is the mode, which ``execution_mode`` says.
        degraded=(scenario.use_live_mcp and not live_mcp_available and not recorded)
        or (scenario.use_live_llm and not live_llm_available)
        or (replay_client is not None and replay_client.degraded),
        provenance=build_provenance(
            scenario.name,
            settings,
            model_role=model_role,
            invocation_id=invocation_id,
            # From what the legs ACTUALLY did, never from the --live flag, which says
            # what was asked for. ``recorded`` is first because it is the narrowest true
            # statement, whatever the model leg did.
            execution_mode=(
                ExecutionMode.RECORDED
                if recorded
                else (
                    ExecutionMode.LIVE
                    if (live_mcp_available or live_llm_available)
                    else ExecutionMode.CANNED
                )
            ),
            # The final ledger: seeded maxima, all four meters, and (WO-R3-260) the
            # briefing writer's post-terminal call, which is the agent's cost. It
            # differs from the graded ``final.budget`` by exactly that charge.
            budget=reported_ledger,
            recorded_at=tick(),
            # The strategy object the loop above actually ran with.
            strategy=strategy,
        ),
        # What it cost, by role and by step, reconciled against the SAME ledger the
        # provenance carries (WP-2.3): the charged split includes ``briefing_writer``,
        # so the pre-briefing ledger would report every enriched run as unreconciled.
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

    On live run ``4974811d236f`` the bucket was right and a full trace read was still
    needed to learn the claim wanted an unfiltered listing and the agent verified with a
    filtered one. Both halves are in the graded artifacts already. Call shapes per tool,
    only for the tools the failing claims name: "the claim wants THIS, the agent used THAT".
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
    # Ahead of "transport": it is the one bucket saying the AGENT was right and the
    # HARNESS could not read it. Live run 779b19a287a7 reported `unclassified` with a
    # correct decision in hand, and would otherwise have read as a network problem.
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
            e.tool_name == "_verify_judge" and e.result_summary.startswith("not_verified")
            for e in evidence
        )
    ):
        # Right action, judge said not-yet: the fix outran the probe.
        return "eventual-consistency", ""
    if failing == {GradeDimension.BUDGET}:
        return "llm-variance", ""
    if failing == {GradeDimension.EVIDENCE}:
        return "grader-brittleness", _grader_drift_detail(report, evidence)
    return "unclassified", ""


def _crashed_result(
    scenario: Scenario,
    exc: BaseException,
    invocation_id: str = "",
    *,
    settings: Settings | None = None,
    model_role: ModelRole = ModelRole.DEVELOPMENT,
    recorded: bool = False,
) -> ScenarioResult:
    """Synthesize a failed ScenarioResult when run_scenario raises.

    One crashing scenario must not take out the suite: a single flaky platform call
    would otherwise wipe every result that had not run yet. The error string travels on
    the row, and a ``ScenarioCrash`` also carries the partial ledger and checkpoints so
    the row reports what was spent rather than zero. The bucketing reads the original
    CAUSE — the wrapper is a carrier, not a failure mode.
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
    # The transitions absorb transport failures as graded escalations, so a crash that
    # reaches here is the scenario's own seeding or an unwrapped transport path.
    if isinstance(cause, PreconditionNotMet):
        # Its own bucket: "shared-env" is close but wrong — nothing was contended, the
        # world simply was not in the asserted state, and no agent behaviour is described.
        crash_class = "precondition"
    elif isinstance(cause, PreconditionUnverifiable):
        # The premise is UNKNOWN, not false. Bucketing this as "precondition"
        # would send the reader to seeding when the platform is the problem.
        crash_class = "transport"
    elif isinstance(cause, ChaosSetupFailed) or "chaos_setup" in error_detail:
        # ``run_all`` records an ungraded row for a setup failure instead of routing
        # here. This branch survives for the direct ``run_scenario`` caller that wants a
        # row, and the substring half for a caller raising the old plain RuntimeError.
        crash_class = "shared-env"
    else:
        crash_class = "transport"
    # What the crash had billed, when it carried the measurement AND a checkpoint to
    # reconcile against. ``None`` means none exists, distinct from a zeroed record.
    crash_accounting = exc.accounting if isinstance(exc, ScenarioCrash) else None
    # The crash's own ledger if it reached a terminal state, else the last
    # checkpoint's: they differ by the post-terminal briefing charge, which the split
    # being reconciled here contains.
    crash_ledger = exc.ledger if isinstance(exc, ScenarioCrash) else None
    accounting = (
        None
        if crash_accounting is None or partial is None
        else build_accounting(crash_accounting, crash_ledger or partial.budget)
    )
    outcome = ScenarioOutcome(
        scenario=scenario.name,
        **_grouping_keys(scenario),
        # Both off the last checkpoint the run wrote: hardcoding TRIAGE and 0 described
        # a run that never started and made every crashed row unusable for the cost
        # columns (ADR 0015: the meter may over-report, never under-report).
        final_state=IncidentState.TRIAGE if partial is None else partial.state,
        tool_calls_used=0 if partial is None else partial.budget.tool_calls_used,
        report=report,
        judge_score=None,
        failure_class=crash_class,
        # A crashed run whose teardown ALSO failed says a second thing that outlives
        # this row: the next live run's world is dirty. On the row because the report is
        # the durable record; the on-disk latch is the enforcement.
        teardown_error=(exc.teardown_error if isinstance(exc, ScenarioCrash) else None),
        # Provenance survives the crash (ADR 0013): these defaulted to False, so every
        # crashed row in a live report claimed canned, and a row that misdescribes how it
        # ran is worse than none. ``recorded`` overrides the declared MCP leg likewise.
        live_mcp=scenario.use_live_mcp and not recorded,
        live_llm=scenario.use_live_llm,
        # Same reasoning one field up: a crash that cannot name its model and revision
        # is a row nobody can act on. ``settings is None`` only for a test that knows no
        # configuration, where the record is absent rather than invented.
        provenance=(
            None
            if settings is None
            else build_provenance(
                scenario.name,
                settings,
                model_role=model_role,
                invocation_id=invocation_id,
                # The DECLARED legs, like the flags above: a crash can precede either
                # choice. Except ``recorded``, which is the caller's instruction — the
                # platform leg WAS a replay however early the crash came.
                execution_mode=(
                    ExecutionMode.RECORDED
                    if recorded
                    else (
                        ExecutionMode.LIVE
                        if (scenario.use_live_mcp or scenario.use_live_llm)
                        else ExecutionMode.CANNED
                    )
                ),
                # The partial ledger when the crash carried one (what the
                # run had actually spent), else the ledger it would have
                # been seeded with — never zeros standing in for unknowns.
                #
                # "Would have been seeded with" is asked of the seed itself
                # (WO-R3-256). This used to be a second copy of ``start_run``'s
                # body, and it had already drifted: WP-2.4 made the token and
                # dollar ceilings per-strategy multiples of the configured
                # ones, the copy went on reading the configured ones, and under
                # a non-1.0 multiplier a crash row named budgets no run would
                # ever have had. One seam — the same argument WP-2.4 makes for
                # the multipliers themselves, and the reason
                # ``tests/unit/test_budgets.py`` reads the source to keep the
                # scaled ceilings out of every file but ``config.py`` and
                # ``factory.py``.
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
            )
        ),
        accounting=accounting,
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
    model_role: ModelRole = ModelRole.DEVELOPMENT,
    recorded_worlds: Mapping[str, Path] | None = None,
) -> tuple[RunReport, tuple[Trajectory, ...], tuple[EscalationBriefing, ...]]:
    """Run every scenario and assemble the report.

    ``recorded_worlds`` maps a scenario name to the recording to replay it
    against (WP-3.3). A scenario absent from the mapping runs in whatever mode it
    would have run in anyway, which is what keeps this one loop serving all three
    modes; ``main`` refuses a recorded selection with a scenario that has no
    recording, before anything runs, so the silent-canned-fallback case cannot be
    reached from the CLI.
    """
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
    ungraded: list[UngradedScenario] = []
    worlds = dict(recorded_worlds or {})
    for scenario in scenarios:
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
        # From the role every row was stamped with, so the console line, the report and
        # the archive agree by construction (``degraded_count``'s discipline, A-01).
        closing=model_role is ModelRole.BENCHMARK,
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

    Invariant 9 enforced by the filesystem rather than by discipline: exclusive-create
    stops the RUNNER overwriting evidence, this stops ``rm -rf`` and a cleanup script
    aimed at the wrong checkout — one came within a command of destroying 195 run files.
    Best-effort, never fatal, and idempotent (immutable paths are skipped). Deliberate
    unlock: docs/runbook.md § "Completed archives are locked on disk".
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

    The flat ``<EVAL_TRACE_DIR>/<scenario>.jsonl`` is gitignored (F-002), so an archive
    whose traces stayed only there could not be joined to its prompts (S-07). Filter
    convention, shared with ``scripts/estimate_cost.py``: a mismatched or absent
    ``invocation_id`` is excluded, and an unparseable line skipped. READ-ONLY throughout.
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
    Ctrl-C used to throw away 30 scenarios of paid live evidence (S-05/A-14), and a hard
    kill now costs at most the in-flight one. Every file is exclusive-create and LOCKED
    as its write completes — evidence the instant it is durable. ``report.json`` is NOT
    written here; see ``finalize_archive``.
    """
    scenario = result.outcome.scenario
    _archive_trajectory(target, result.trajectory)
    _archive_briefing(target, scenario, result.briefing)
    if trace_dir is not None:
        _archive_trace_slice(target, scenario, invocation_id=invocation_id, trace_dir=trace_dir)


def finalize_archive(target: Path, report: RunReport) -> Path:
    """Write ``<target>/report.json`` — the completion marker, always last.

    Its presence makes an archive complete and its absence marks a partial run whose
    per-scenario files are still evidence: written FIRST, as until S-08, a half-finished
    archive looked complete. The marker means nothing will write here again, so the tree
    is locked once it is down. A killed run's directories stay writable at no cost.
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

    The one-shot equivalent of the streaming path ``main`` uses, for callers holding a
    complete run in memory. Same files, same ordering: report.json last. Every file is
    exclusive-create, which is the load-bearing half — two invocations aimed at one
    directory fail loudly rather than deleting the earlier one (F-002).
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

    The CONSTRUCTOR with ``_env_file=None``, not ``model_validate``, which drops that
    key and consults the cwd ``.env`` — how a real ``PLATFORM_SMOKE_TOKEN`` leaked into
    "offline" runs (A-04). ``platform_smoke_token=None`` is pinned so an exported var
    cannot supply it either; unpinned fields can still absorb shell env vars.
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

    WP-2.2's acceptance number, from the ROWS rather than stored, so it computes over
    the committed baseline and every archived report. The denominator is the corpus the
    loader produced, never a literal, and "graded" means a substantive detail via
    ``is_vacuous_detail``, so this and the gate's vacated-assertion check move together.
    The ungraded rows split in two (INC-003): wrong world, or unlabelled.
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
    # settings — the A-01 rule again. Silent on a canned suite, where every fake bills
    # nothing and "$0.000000" would be noise.
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

    The config defaults keep canned runs byte-identical (ADR 0006/0009), but a live run
    at those values reproduces both failure modes the mitigations exist for (S-10). A
    warning, not exit 3: an explicit ``VERIFY_PROBE_ATTEMPTS=1`` is indistinguishable
    from unset, so a hard fail would ban single-probe live experiments outright.
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


def _settings_for_mode(live: bool) -> Settings:
    """Live mode reads real env; offline uses the eval placeholder."""
    if live:
        return Settings()  # type: ignore[call-arg]
    return _eval_defaults()


def _parse_only(argv: list[str]) -> list[str]:
    """Extract scenario filters from ``--only <pattern>``.

    Repeated flags or one comma-separated value; empty means "no filter". ``main``
    decides how a pattern MATCHES: a scenario's FULL NAME under ``--live`` without
    ``--smoke`` (a substring there widens the selection past the ADR 0020 gate), a
    substring under ``--smoke`` and offline. Parsing is the same either way.
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

    Resolves ``AGENT_MODEL`` from ``DEVELOPMENT_MODEL`` or ``BENCHMARK_MODEL`` (plan 02
    § 9). Defaults to ``development`` (a run that did not name a role is not a benchmark
    run) and REFUSES an unrecognised value. Returns a PAIR because ``ModelRole`` is a
    ``StrEnum``, so a refusal string would otherwise read as a selection.
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


#: The one value ``--mode`` takes. The other two are selected by what the run can
#: reach, and flag spellings for them would be two ways to ask for one thing.
RECORDED_MODE: Final[str] = "recorded"


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
    if value == RECORDED_MODE:
        return value, ""
    return None, (
        f"MODE FAIL: --mode {raw!r} is not a mode this runner takes. The only value "
        f"is --mode {RECORDED_MODE} (replay a recorded world). A live run is "
        "--live, and a canned run is the default — those are selected by what the "
        "run can reach, not by this flag."
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

    Without ``--world``, each scenario replays its NEWEST recording through
    ``artifacts.newest_or_none`` — never a glob, never ``ls -t`` — and no recording is a
    REFUSAL rather than a canned fallback. With ``--world <id>`` one recording is pinned,
    which is what makes a number reproducible; a scenario NAME means its newest, and the
    two cannot collide. Either way: exactly one recording, exactly that scenario.
    """
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

    Per scenario, because the three causes have three different repairs: a chaos or
    write scenario belongs to the remediation stage, a ``smoke_exclusion`` is a
    deliberate hold-back. The first two can both be true and both are reported;
    ``smoke_exclusion`` cannot coexist with either, so it is reported alone.
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
    # Unreachable from a caller filtering on `not in_smoke_pass`, and stated anyway: an
    # empty reason would turn the refusal into a bare list of names.
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
    if live and (blocked := chaos_block_reason()) is not None:
        # A previous teardown did not complete, so the next live run's baseline carries
        # a fault nobody intended. Refused before settings, guards and spend: a refusal
        # that arrives after the money is gone is a report. Offline runs share no world.
        print(f"CONTAMINATED WORLD: live runs are blocked — {blocked}")
        print(
            "Restore the world — the reset clears the block on success:\n"
            "  make eval-reset PURGE_IDEMPOTENCY=1\n"
            "Only where there is no stack left to reset, clear it alone:\n"
            "  uv run python -m evals.runner --clear-chaos-block"
        )
        print("no scenarios ran, nothing was spent")
        return 10
    # Before the settings load, like the refusals around it: a mistyped mode
    # must cost nothing, and this parse depends on no environment.
    mode, mode_refusal = _parse_mode(sys.argv[1:])
    if mode is None:
        print(mode_refusal)
        print("no scenarios ran, nothing was spent")
        return 2
    recorded = mode == RECORDED_MODE
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
    # Before the settings load, like the two refusals around it: a mistyped
    # role must cost nothing, and this parse depends on no environment.
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
        # A bare `--live` is the whole suite against one shared platform with real
        # spend and no reset. It was refused only INCIDENTALLY, by the exit-8 canned-only
        # gate, which is a fact about the scenario DIRECTORY: give every scenario a live
        # leg and an unfiltered `--live` starts spending with nothing here changed. So
        # the missing filter is refused structurally, before the settings load.
        # `--smoke` is exempt (it derives its own read-scoped selection, WO-R2-123), and
        # the Makefile's `ifndef ONLY` says the same thing for the `make` path only.
        print(
            "LIVE FAIL: --live requires --only <scenario_name> (or --smoke). "
            "An unfiltered live selection is the whole suite against one shared "
            "platform — real spend, no reset between scenarios."
        )
        print("Name exactly one scenario, e.g. make eval-live ONLY=remediate_dlq_backlog_success")
        print("no scenarios ran, nothing was spent")
        return 2
    try:
        # A recorded run reads the real environment, which is why it costs money: the
        # platform is a replay, the MODEL is not. ``PLATFORM_MCP_URL`` is read and
        # ignored — ``run_scenario`` forces the live MCP leg off for any recording.
        settings = _settings_for_mode(live or recorded)
    except ValidationError as err:
        # Exit 3 is the preflight/env code; a raw traceback would exit 1, which is
        # reserved for "a scenario failed" (A-15).
        fields = ", ".join(
            ".".join(str(part) for part in detail["loc"]) or "(settings)" for detail in err.errors()
        )
        print(f"PREFLIGHT FAIL (env): invalid or missing settings — {fields}")
        return 3
    # The role resolves the billed model once, before any scenario. ``model_copy``, not
    # a second Settings construction: the id is already guaranteed priced, and
    # re-reading the environment would be a second chance for it to differ.
    settings = settings.model_copy(update={"agent_model": settings.model_for_role(model_role)})
    print(f"model role: {model_role.value} → AGENT_MODEL={settings.agent_model}")
    # Live-only, after the settings load and before any spend, so canned-equivalent
    # probe knobs surface even on a run that goes on to be refused. Never an exit code.
    if live and (msg := _canned_equivalent_knob_warning(settings)) is not None:
        print(msg)
    mcp_token: str | None = None
    if smoke:
        # An empty secret is UNSET, not a token: `is None` alone let `SecretStr("")`
        # through and `make_client`'s `token or ...` then selected the FULL principal
        # for every client in the stage, guard client included (S-04).
        if settings.platform_smoke_token is None or not (
            settings.platform_smoke_token.get_secret_value().strip()
        ):
            print("SMOKE FAIL: PLATFORM_SMOKE_TOKEN is not set in .env")
            print("run `make bootstrap-token` and add the read-scoped token")
            return 3
        mcp_token = settings.platform_smoke_token.get_secret_value()
    scenarios = load_scenarios(_SCENARIOS_DIR)
    if smoke and not only_patterns:
        # The smoke pass selects itself through `in_smoke_pass` (WO-R2-123): the old
        # hand-written Makefile list let a renamed scenario fall out of the pass
        # silently. `SMOKE_ONLY=` survives as an override and arrives through --only.
        held_back = sorted(
            (s.name, s.smoke_exclusion) for s in scenarios if s.smoke_exclusion is not None
        )
        scenarios = [s for s in scenarios if s.in_smoke_pass]
        print(f"smoke selection: {len(scenarios)} scenario(s) derived from {_SCENARIOS_DIR}")
        for name, reason in held_back:
            # Printed for the per-pattern counts' reason: the smoke log carries its own
            # record of what it covered, and here of what it knowingly did not.
            print(f"  held back (smoke_exclusion): {name} — {reason}")
        if not scenarios:
            # Unreachable from a healthy tree, and checked precisely for that: a
            # derivation that comes back empty is a green smoke pass over nothing.
            print(
                "SELECTION FAIL: no scenario is in the smoke pass — every scenario "
                "either declares chaos_setup/expected_action_tools or carries a "
                "smoke_exclusion"
            )
            print("no scenarios ran, nothing was spent")
            return 2
    if only_patterns:
        # OR-match, accounted PER PATTERN: refusing only an empty selection let one dead
        # pattern hide among SMOKE_ONLY's nineteen and report green over a smaller suite
        # than the reader believed (WO-R2-41).
        matched: dict[str, list[str]] = {
            pattern: [s.name for s in scenarios if pattern in s.name] for pattern in only_patterns
        }
        for pattern, names in matched.items():
            print(f"  --only {pattern} → {len(names)} scenario(s)")
        dead = [pattern for pattern, names in matched.items() if not names]
        if dead:
            # Subsumes the older "nothing matched" case, and names which patterns to
            # fix instead of only reporting the total.
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
        if live and not smoke:
            # On the spend path a pattern must be a scenario's FULL NAME.
            # `ONLY=dlq_backlog` used to take `remediate_dlq_backlog_success` too, the
            # read-only one drained the seeded replay_safe pool, and the report blamed
            # the agent — and the ADR 0020 gate misses it, since only one of the two is
            # mutating. Exact match FIRST, so a name that is a prefix of another stays
            # runnable. `--smoke` and offline keep substring matching: no spend, no
            # shared platform, and `make eval-reg`/`baseline` refuse ONLY outright.
            known = {s.name for s in scenarios}
            widened = [p for p in only_patterns if p not in known]
            if widened:
                print(
                    f"SELECTION FAIL: {len(widened)} --only pattern(s) are not "
                    f"scenario names: {', '.join(widened)}"
                )
                print(
                    "A live run selects by full scenario name — one named scenario "
                    "per pattern — because a substring silently widens the selection "
                    "past the ADR 0020 one-mutating-scenario gate. Did you mean:"
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
        # A read-only stage runs the DERIVED smoke set and nothing else. Seeding fires
        # under PLATFORM_CHAOS_TOKEN regardless of --smoke, so a read-scoped AGENT token
        # cannot prevent it, the #80 guard only inspects the agent's client, and the
        # exit-5 audit sees the write after it lands — so the only prevention is
        # refusing the selection here, after --only and before any spend (S-03).
        # Checking `chaos_setup` alone was half the door: `--only` bypasses the
        # derivation, so an override could re-admit a scenario dropped for
        # `expected_action_tools` — a graded Tier-1 write inside the stage that exists to
        # prove the smoke token cannot write. The override may NARROW the set, not widen it.
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
        # A canned-only scenario cannot run live: the platform cannot manufacture its
        # fault, so `run_scenario` would serve the canned fixtures and the row would
        # land in the live report's pass count. A canned green there is a statement
        # about fixtures, not the agent, and each such YAML says what would unblock it.
        # --smoke is exempt by design (its stage mixes canned harness-sanity scenarios
        # with live reads). Before the ADR 0020 gate, so "run each one alone" is only
        # ever advised for scenarios that CAN run.
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
        # One state-mutating scenario per invocation, enforced rather than remembered
        # (ADR 0020): the remediation scenarios share ONE platform and one seeded
        # replay_safe row, so in a single invocation a CORRECT agent greens one and reds
        # the other. The reset lives outside the runner, which is why this is a selection
        # refusal. ``seeds_chaos``, not ``chaos_setup``: the legacy field is ``None`` on
        # a plan-declaring scenario, which would stop a two-hook scenario counting as
        # mutating.
        mutating = [
            s.name for s in scenarios if s.expectation.expected_action_tools or s.seeds_chaos
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
            # The mirror of the --live degradation refusal below: a recorded run on the
            # canned planner is a CANNED run wearing a recorded label. Also why the
            # offline proof of this mode drives ``run_all`` from tests.
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
    # The principal, guarded at point of use before a dollar is spent. No
    # whoami exists, so this is a negative probe: an invalid Tier-1 call must be refused
    # on SCOPE, and the handler checks scope before arguments, so nothing can execute.
    stage_started_at = datetime.now(UTC)
    # Unconditional in smoke mode against a real platform, derived from the URL and NOT
    # the --live flag: the guard must not depend on flag parsing to decide whether scope
    # needs checking. There is no opt-out.
    guard_required = smoke and not _is_offline_placeholder(str(settings.platform_mcp_url))
    if smoke and not guard_required:
        # Defence in depth behind the degraded fail-fast above: a smoke pass exists to
        # verify the live read-scoped principal, and a placeholder has none (A-04).
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
    # The mirror, for the stage that must be able to ACT. Every principal check used to
    # be gated on `smoke`, leaving the one stage that spends AND mutates unguarded — and
    # a read-scoped token there does not fail fast, it grades every scenario red after
    # full spend. Each half asks about the scope its half of the selection needs, keyed
    # off the ADR 0020 gate's two fields: `expected_action_tools` alone missed a
    # chaos-only scenario, and `actions:execute` is the wrong scope for seeding.
    live_platform = (
        live and not smoke and not _is_offline_placeholder(str(settings.platform_mcp_url))
    )
    write_guard_required = live_platform and any(
        s.expectation.expected_action_tools for s in scenarios
    )
    chaos_guard_required = live_platform and any(s.seeds_chaos for s in scenarios)
    if write_guard_required or chaos_guard_required:
        # TWO clients, because there are two principals (v0.6.5, O-4). The agent's is
        # `mcp_token`, and `assert_write_capable_principal` asks both halves about it: it
        # must act, and it must NOT seed (a token that can seed reads the chaos audit
        # rows, which are the answer key). The chaos client is `PLATFORM_CHAOS_TOKEN`,
        # guarded where it is used rather than where it is configured. Resolved HERE, so
        # an unset token costs one refusal instead of a crash mid-invocation.
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
                    assert_write_capable_principal(guard_client)
                else:
                    # A chaos-only selection declares no Tier-1 action, so there is no
                    # write scope to assert — but there is still an agent, and the leak
                    # does not care whether anything was remediated.
                    assert_chaos_blind_principal(guard_client)
            finally:
                guard_client.close()
            if chaos_token is not None:
                chaos_guard_client = make_client(settings, token=chaos_token)
                try:
                    assert_chaos_capable_principal(chaos_guard_client)
                finally:
                    chaos_guard_client.close()
        except PrincipalGuardError as err:
            print(f"PRINCIPAL GUARD FAIL: {err}")
            print("no scenarios ran, nothing was spent")
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

    # Created BEFORE the suite runs, so each scenario streams its evidence in as it
    # finishes and a Ctrl-C costs at most the in-flight one (ADR 0017). ``exist_ok=False``
    # on all three: an invocation-id collision must fail loudly, before any spend.
    trace_dir_setting = os.environ.get(_TRACE_DIR_ENV)
    trace_dir = Path(trace_dir_setting) if trace_dir_setting else None
    target = _RUNS_DIR / invocation_id
    target.mkdir(parents=True, exist_ok=False)
    (target / "trajectories").mkdir(exist_ok=False)
    (target / "briefings").mkdir(exist_ok=False)
    if trace_dir is not None:
        (target / "traces").mkdir(exist_ok=False)

    # The post-stage assertion can only see the newest 200 rows — `list_audit_events`
    # has no offset and no created_after — so rows that scroll past the cap are gone the
    # moment the stage ends. The one window the guard CAN cover is the one it watches
    # while it happens: a page after every scenario, and the assertion takes the union.
    # Without it a stage louder than 200 rows exits 5 "inconclusive" on a paid run.
    audit_scan = AuditWindowScan(stage_started_at) if guard_required else None
    scan_client = make_client(settings, token=mcp_token) if guard_required else None

    def _after_scenario(result: ScenarioResult) -> None:
        archive_scenario(target, result, invocation_id=invocation_id, trace_dir=trace_dir)
        if audit_scan is None or scan_client is None:
            return
        try:
            audit_scan.checkpoint(scan_client)
        except Exception as err:  # noqa: BLE001 — a checkpoint is best-effort
            # Not fatal: a missed checkpoint only narrows coverage, which fails closed
            # on its own. Printed so a systematically broken one stays visible.
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
        )
    finally:
        if scan_client is not None:
            scan_client.close()
    ran_names = [o.scenario for o in report.outcomes]
    # The per-scenario writes already happened inside run_all; report.json goes down
    # next as the completion marker, and only then the top-level copies — so a failure
    # there leaves the durable record already complete. When only the flat files
    # existed, a routine offline `make eval` erased Run 001's paid trajectories (F-003).
    # One stamp for the whole run, so its artifacts share a prefix and sort together.
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
        # Attributed to the principals this stage owns, so another service account
        # cannot fail or mask it (A-13). BOTH ids or nothing: the failure this exists for
        # writes under the AGENT principal. Unfiltered is over-broad, the safe side.
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

    # Order is the blast radius, widest first: a contaminated world is the only one of
    # the three that reaches the NEXT invocation, and an ungraded scenario outranks a
    # failed one because "the agent failed" is exactly what did not happen. All of them
    # land AFTER the archive and report are on disk — evidence first, verdict second.
    # The LATCH, not only the report rows: a scenario abandoned at SEEDING has no row to
    # carry ``teardown_error``. Live only, like the pre-run refusal.
    if live and (report.contaminated_scenarios or chaos_block_reason() is not None):
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
