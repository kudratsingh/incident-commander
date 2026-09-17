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
from incident_commander.agent.remediation import (
    make_llm_plan,
    make_llm_verify,
    make_remediate,
)
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
# The three directories above hold VERSIONED files, never overwritten:
# ``<scenario>.<YYYYMMDDTHHMMSSZ>.<invocation_id>.json``, and
# ``report.<stamp>.<invocation_id>.json`` for the aggregate. They used to
# be flat "refreshable pointers" that every run rewrote in place — a
# documented exception to CLAUDE.md invariant 9, now withdrawn: nothing in
# the eval output tree overwrites a prior file. Every write here is
# exclusive-create, the same convention as the archive writes below.
#
# There is no ``latest.json`` and no symlink standing in for one. The
# newest version is resolved in exactly one place — ``evals/artifacts.py``
# (``artifacts.newest(kind, scenario)``), ordering by the stamp in the
# FILENAME and then by invocation_id, never by mtime — and every reader in
# the repo goes through it. Pre-versioning flat files still on disk are
# left exactly where they are (they are evidence) and resolve as the
# oldest version. Cost: a few KB per scenario per run that is never
# reclaimed; see the ``evals/artifacts.py`` docstring.
#
# Immutable per-invocation archive. This directory is the append-only
# record of a whole run (CLAUDE.md invariant 9). Writing the archive with
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


_UNKNOWN: Final[str] = "unknown"
_COMPOSE_FILE = _REPO_ROOT / "demo" / "compose.yml"
# The compose service that publishes the MCP surface the agent is evaluated
# through. Its ``image:`` line is where the platform digest is READ FROM, so
# the recorded digest cannot drift from the stack that ran: there is no second
# copy to keep in step (C-10). The other two platform services carry the same
# digest, and that they move together is pinned by
# ``tests/unit/test_demo_docs.py::test_every_repository_resolves_to_one_ref``
# rather than re-checked here.
_PLATFORM_SERVICE: Final[str] = "platform"
# How many scenario names the non-closing mark spells out before counting the
# rest (see ``RunReport.non_closing_reason``).
_NON_CLOSING_NAMES_SHOWN: Final[int] = 5
# Where a failed chaos teardown records that the shared world is dirty.
#
# On DISK and not in memory, because the thing it protects is the NEXT
# invocation: a teardown compensator that did not fire leaves a fault
# standing in a platform the next live run will read as its baseline, and
# that run is the one that pays for it. A flag that dies with the process
# protects nothing.
#
# Not evidence, so not under ``evals/runs/`` — it is mutable operational
# state with exactly two transitions (a teardown failed; an operator reset
# the world), and invariant 9 covers the append-only record, not the latch.
# The durable record of the same event is the run's own report row
# (``ScenarioOutcome.teardown_error``), which is append-only like everything
# else in the archive.
_CHAOS_BLOCK_PATH = _REPO_ROOT / "evals" / ".chaos-teardown-block.json"


class ExecutionMode(StrEnum):
    """How the world under a run was produced.

    ``RECORDED`` arrived with WP-3.3 and is the third: the platform is a
    recording of one moment of the live world (ADR 0043), replayed from disk,
    while the agent's model calls are real. It is appended rather than
    interleaved for the reason ``GradeDimension`` gives — archived reports and
    the committed baseline are read back against this enum — and it is a
    distinct value rather than ``LIVE`` with a flag because the whole of
    ADR 0013 is that a run must never be mistakable for a more-real run than it
    is. A recorded row's grades are valid for diagnosis, plan, candidate metrics
    and calibration, and for nothing else (plan 02 § 189, 03 § 52).
    """

    CANNED = "canned"
    LIVE = "live"
    RECORDED = "recorded"


class RunProvenance(BaseModel):
    """Exactly what produced one run: code, world, models, role, budgets.

    ADR 0013 made provenance part of the eval result and answered one
    question with it — "was this measured against the real system or the
    canned model of it?". This record extends that same logic to identity:
    a saved run has to answer "which code, which platform image, which
    model under which role, which strategy, and what was it allowed to
    spend?" without anyone reconstructing the invocation's environment
    afterwards. Before it existed, a report carried no model id, no
    commander revision, no platform digest and none of the four budget
    meters, so the Phase 0 baseline would have been un-attributable the day
    it was written.

    Every string field is populated or explicitly ``"unknown"`` — never
    omitted and never silently empty. ``unknown`` is a claim a reader can
    act on ("this ran outside a git checkout"); a missing key is one they
    have to interpret.

    Attached per scenario rather than once per invocation: a scenario is the
    unit a leaderboard row, a cost column and a phase report are all built
    from, so it is the unit that has to be able to name its own model. The
    duplication across a 40-scenario offline report is a few kilobytes and
    buys rows that stay attributable when they are read one at a time.
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
    # The run's OWN ledger: max_* are the budgets actually seeded (which are
    # not the documented defaults — a scenario's declared cap overrides
    # BUDGET_MAX_TOOL_CALLS per ADR 0019, and the paid-run protocol's
    # budgets live only in the operator's .env), and the *_used fields are
    # the four meters ADR 0015 tracks and the report used to drop.
    budget: BudgetLedger


@lru_cache(maxsize=1)
def commander_revision() -> str:
    """``git rev-parse HEAD``, or ``"unknown"`` outside a git checkout.

    Recorded, never inferred, and never omitted: a report that cannot name
    the revision it ran is still a report about *some* revision, and saying
    so is the honest form of not knowing. Failure is tolerated in every
    shape it comes in (no git binary, no repository, a broken index) because
    the alternative is an eval run that cannot start over a metadata read.
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

    Read from the compose file rather than from a constant in this repo, so
    the recorded digest is the one the stack was actually brought up on.
    A second copy of a pinned digest is a copy that goes stale two releases
    later (C-10, demo/README.md's inlined digest), and a run whose record
    names the wrong platform is worse than one that names none.
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

    This is the edge configuration is wired at: nothing under
    ``src/incident_commander/agent/`` reads ``Settings``, so N and the sampling
    temperature reach a strategy from here or not at all. One function, called by
    both places that build a strategy — the run itself and the provenance record
    — so the arm that ran and the arm the artifact names cannot be built from
    different knobs.

    The budget multipliers are deliberately absent: they are read in
    ``config.py`` and applied once, where ``agent/factory.py::start_run`` seeds
    the ledger (``tests/unit/test_budgets.py::TestNoOtherCallSiteScalesABudget``
    refuses a second reader). A BUDGET result for an N-arm is read beside the
    ``n`` the strategy stamps and the seeded ledger already in the provenance
    record.
    """
    return StrategyKnobs(
        n=settings.best_of_n,
        sample_temperature=settings.sample_temperature,
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

    Deliberately takes the ledger rather than reading the settings twice:
    the budgets in the record must be the ones the run was seeded with, not
    the ones the configuration documents (ADR 0019's per-scenario cap, and
    the paid-run protocol's .env-only ceilings, both make those different
    numbers).

    ``strategy`` is the object that actually made the run's planner calls
    (WP-0.2), so the stamped name and config come from the thing that ran
    rather than from a second read of the configuration. ``None`` is the crash
    path: a scenario can crash before a strategy is built, and there the
    configured one is the only honest answer available — the same reasoning the
    crash row's ``execution_mode`` already uses.
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
        # "" is how a run with no invocation id reaches here (a direct
        # ``run_scenario`` call; the runner's own entry point always has
        # one). Recorded as "unknown" for the same reason the two reads
        # above are: an empty string in a provenance field reads like a
        # value, and it is not one.
        invocation_id=invocation_id or _UNKNOWN,
        recorded_at=recorded_at or datetime.now(UTC),
        execution_mode=execution_mode,
        budget=budget,
    )


class RoleAccounting(BaseModel):
    """One prompt role's share of a run's bill (plan 03 § 7.8).

    Role is the unit because it is the one a strategy changes: a best-of-N
    arm spends its extra tokens in ``investigation_planner`` and nowhere
    else, so a run total alone cannot tell "the strategy cost more" from
    "the incident needed more remediation".
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: str
    # Whether this role's calls reached the run's own BudgetLedger. False for
    # the EVALUATOR's roles only: the briefing judge grades the run and is not
    # part of it, so folding it in would fail the reconciliation on every run.
    # The briefing writer is also billed after the terminal state and IS
    # charged (WO-R3-260) — it is the agent's own prose, and whose money a
    # call is decides this flag, not when the call happened.
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

    Divergence D3, the half WP-0.3 left open. WP-0.3 put the run's own ledger
    on the row, which answers "what did this run spend in total?". It cannot
    answer "on which role?", "over how many planner steps?" or "how much
    context did each step carry?" — and those are the columns the
    accuracy/cost frontier in Phases 6, 9, 12 and 13 is drawn from. Divergence
    D4 is the same gap one layer out: the campaign's cost figures live in
    prose because no artifact carried them.

    ``reconciled`` is the field that makes the rest usable. The charged split
    is built from the same ``LLMUsage`` objects the ledger was charged with,
    so it should equal the ledger exactly; the row states both sides and
    whether they matched, rather than asserting agreement a reader cannot
    check (``agent/accounting.py::RunAccounting.reconciles_with`` names the
    one path that can legitimately differ, and in which direction).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    by_role: tuple[RoleAccounting, ...] = ()
    llm_calls: int = 0
    # From the run's own ledger, not counted again here.
    tool_calls: int = 0
    wall_seconds: float = 0.0
    # Time spent inside LLM calls. Deliberately not the same number as
    # ``wall_seconds``: the loop also probes, waits out ADR 0009's freshness
    # window and grades, and a latency column that conflated the two would
    # attribute a 75-second sleep to the model.
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
    # Zero for ``baseline``, and WRITTEN as zero rather than omitted: a
    # later strategy's row carries real numbers here, and a reader comparing
    # the two must not have to decide what a missing key meant.
    selector_calls: int = 0
    branch_count: int = 0
    planner_steps: int = 0
    # Per step, in order, and the total beside it (plan 02 § 17).
    planner_input_tokens: tuple[int, ...] = ()
    planner_input_tokens_total: int = 0
    planner_context_chars: tuple[int, ...] = ()
    planner_context_chars_total: int = 0


def build_accounting(accounting: RunAccounting, budget: BudgetLedger) -> RunAccountingRecord:
    """Assemble one run's accounting row from the run's own measurements.

    Takes the ledger rather than reading it back off the report for the same
    reason ``build_provenance`` does: the number the row reconciles against
    has to be the one the run actually finished with.
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
    # The benchmark's grouping keys (WP-1.4), carried on the row rather than
    # re-derived by the reader. From Phase 2 onward every report slices by
    # family, difficulty and split, and a reader that joins a report back to
    # ``evals/scenarios/`` to get them is reading TODAY's classification of a
    # scenario against LAST MONTH's run — which silently re-labels history
    # every time a scenario is reclassified, and is the same shape as
    # invariant 9's "a derived metric computed from a mutable artifact is a
    # lower bound". Present here, the row says what the scenario was when it
    # ran.
    #
    # ``None`` means "predates the record", which every archived report and
    # the committed baseline do — they are append-only evidence and are never
    # rewritten, so the reader tolerates their absence exactly as it does for
    # ``provenance`` below (ADR 0013's precedent). WP-2.5 groups on these; it
    # needs no schema change to do it.
    template_id: str | None = None
    seed: int | None = None
    family: str | None = None
    difficulty: str | None = None
    benchmark_split: str | None = None
    final_state: IncidentState
    tool_calls_used: int
    report: GradeReport
    judge_score: JudgeScore | None = None
    # Five-bucket noise-source classification (docs/lessons/
    # live-eval-noise-sources.md) + "passed" + "unclassified". Heuristic,
    # derived from the grade report and the run's evidence — a starting
    # point for bucket-before-you-debug, not a verdict.
    failure_class: str = "unclassified"
    # Why the bucket, in enough detail to act on without opening the
    # trajectory. Empty on a pass and on buckets whose name already says
    # everything. `grader-brittleness` is the one that earns it: WO-R2-175
    # (INC-001) — the class was correct on live run 4974811d236f and the
    # coordinator still had to read the whole trace to learn WHICH claim and
    # WHICH call shape disagreed.
    failure_class_detail: str = ""
    # Set when the briefing judge call itself failed: the scenario result
    # stands (graded deterministically); only the judge column is missing.
    judge_error: str | None = None
    # Set when post-grade briefing enrichment failed: the deterministic
    # briefing stands and the run keeps its grade; only the LLM-written
    # findings/recommendation are missing.
    briefing_error: str | None = None
    # What the scenario's ChaosPlan did, setup then teardown, in the order
    # the hooks fired (WP-1.1). Evaluator-only: the archive's answer to
    # "what world was this run actually graded in?", which used to be
    # answerable only from a trace file, and only when tracing was on.
    # Empty on every canned run, and on every live run seeding nothing.
    chaos_hooks: tuple[ChaosHookRecord, ...] = ()
    # Set when a teardown hook failed. DISTINCT from every field above,
    # because it is not a statement about this run at all: the grade stands
    # exactly as reported, and what is wrong is the SHARED environment the
    # next run would inherit. It travels with the latch on disk
    # (``_CHAOS_BLOCK_PATH``), which is what actually refuses the next live
    # invocation — this is the durable record of why.
    teardown_error: str | None = None
    # Run provenance (ADR 0013): which legs actually ran live, and whether a
    # declared-live leg silently fell back to canned. Defaults are load-
    # bearing — archived reports and the committed baseline predate these
    # fields and must keep parsing (same precedent as failure_class above).
    live_mcp: bool = False
    live_llm: bool = False
    degraded: bool = False
    # What produced this row (WP-0.3): code revision, platform digest, model
    # + role, strategy, seeded budgets and what was spent. ``None`` means
    # "predates the record", which every archived report and the committed
    # baseline do — they are append-only evidence and are never rewritten, so
    # the reader tolerates its absence (ADR 0013's own precedent for
    # ``live_mcp``/``live_llm`` above).
    provenance: RunProvenance | None = None
    # What the run cost, by role and by step (WP-2.3). ``None`` means "no
    # measurement exists", which is every archived report, the committed
    # baseline, and a crash that died before a single call was made — never
    # "this run was free", which is what a zeroed record would have said.
    accounting: RunAccountingRecord | None = None
    # What the REPLAY did, on a recorded run only (WP-3.3): which recording,
    # its world fingerprint, the replay clock and offset, how many calls it
    # answered, how many it MISSED, whether it refused anything, the coherence
    # findings the recording carries, and the plan a truncated run stopped at.
    # ``None`` on every other run and on every archived report, which is the
    # same default-bearing reasoning as the two fields above.
    #
    # An untyped mapping, exactly like ``RunProvenance.strategy_config``: its
    # shape is ``RecordedMCPClient.summary()``'s plus the plan block, it exists
    # to be read by a person and grouped by a report, and nothing grades on it.
    # A second schema for it here would be a second place for the replay's own
    # vocabulary to drift from the module that produces it.
    #
    # The miss count is the load-bearing number: a recorded run that asked the
    # recording for something it does not hold is not a comparable run, and
    # ``degraded`` above is set from it for exactly that reason.
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

    One function for both construction sites — the clean row and the crash
    row — because a crashed run is still a run of a scenario in a family, and
    a crash row missing its keys would drop out of exactly the per-family
    counts that would have shown the family was crashing.
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

    Its own collection, beside ``outcomes`` rather than inside it, and that
    placement IS the decision: a ``ScenarioOutcome`` carries a
    ``GradeReport``, and there is no honest ``GradeReport`` for a run that
    never happened. A red row saying "crashed" would enter the failed count
    and the pass rate, and every derived number would then quietly describe
    the agent using an event that had nothing to do with it — the same
    mistake `bb1fa70abb4c` made one layer up (`PreconditionNotMet`'s
    docstring).

    So the report says three things instead of two: how many scenarios
    passed, how many failed, and how many never ran. A reader who wants the
    third number can no longer get it by subtraction, which is the point.
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
    # None = pre-schema report ("unknown"), deliberately distinct from 0
    # ("verified fully live"): the committed baseline was a 32/37-degraded
    # canned run, and defaulting to 0 would make that artifact assert a
    # falsehood — the exact honesty failure these fields exist to fix.
    degraded_count: int | None = None
    # Which --only filters produced this report, so filtered runs
    # self-describe in latest.json and in the archive. Empty = full suite.
    only_patterns: tuple[str, ...] = ()
    # Whether this report may be used to CLOSE a phase (plan 03 § 14: "a
    # phase report generated with any development run in it is marked
    # non-closing"). Tri-state on the same reasoning as ``degraded_count``
    # above: ``None`` = predates model roles, deliberately distinct from
    # ``True`` = "every run in it was made under the benchmark role". The
    # validator below refuses a report that claims ``True`` while carrying a
    # development run, so the mark cannot disagree with the rows it
    # summarizes — it is written into the artifact because artifacts outlive
    # the scrollback (ADR 0013).
    closing: bool | None = None
    outcomes: tuple[ScenarioOutcome, ...]
    # Scenarios whose fault world could not be built (WP-1.1). Defaulted for
    # the same back-compatibility reason as ``degraded_count`` and
    # ``closing`` above: every archived report and the committed baseline
    # predate the field and must keep parsing.
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
            # Named, but bounded: a full-suite offline run puts 40 names in
            # this line, and a sentence nobody finishes reading is a mark
            # nobody acts on. The count is exact; the list is a sample, and
            # ``development_scenarios`` has all of them for a caller that
            # needs the rest.
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
        # Set by ``run_scenario`` when the chaos teardown that follows a
        # crashed run ALSO failed. Carried beside the cause rather than
        # folded into it: the crash says what happened to the agent, this
        # says what happened to the world, and the second one decides
        # whether the next live run may start at all.
        self.teardown_error: str | None = None
        # What the run had billed when it died, by role (WP-2.3). Same
        # reasoning as ``checkpoints``: a crashed run's spend is still spend,
        # and a crash row that cannot name it leaves the cost columns a lower
        # bound that reads like a measurement.
        self.accounting: RunAccounting | None = None
        # The ledger that spend was charged to, when the run reached a
        # terminal state before dying. It is NOT ``final.budget``: the
        # briefing writer's post-terminal call is charged after the last
        # checkpoint is written (WO-R3-260), so reconciling against the
        # checkpoint would report a disagreement that is an artefact of
        # which object was read, not of anything the run got wrong.
        self.ledger: BudgetLedger | None = None

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
                    "kind": TraceKind.PRECONDITION,
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


class ChaosSetupFailed(RuntimeError):
    """A setup hook did not fire, so the scenario's world was never built.

    Deliberately NOT a graded failure, and deliberately not a
    ``ScenarioCrash``. The two say different things: a crash is "the agent's
    run died", and this is "there was no world to run the agent in". Grading
    a run whose fault was never manufactured is how a report comes to
    describe the agent when it is describing the environment — the same
    mistake ``PreconditionNotMet`` exists to avoid one step later, and the
    reason ``run_all`` records this as *ungraded* rather than as a failed
    scenario (plan 01 § 4: "setup failure → benchmark world invalid; do not
    grade the agent").
    """


class ChaosHookRecord(BaseModel):
    """What one chaos invocation did, recorded evaluator-side.

    Evaluator-only by construction: it is reachable from the grader's
    ``ScenarioOutcome`` and the run archive, and from nowhere the agent
    reads. Nothing here is ever put on ``RunState``, on the evidence ledger,
    or into a prompt — a scenario that told the agent which faults were
    seeded would be measuring recall of the answer key (WP-1.3 tests that
    boundary; this packet must not be the thing that breaches it).

    ``result`` holds the hook's own parsed response (seeded ids, fixture
    names, TTLs), which is what makes a post-hoc reading of the archive able
    to say what the world was — the question "what exactly was seeded?" was
    previously answerable only from the trace file, and only when tracing
    happened to be on.
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

    The second half of INC-003's rule (``root_cause.label_describes_this_world``
    is the rule itself): a live run whose scenario planted the fault is in the
    world its ground truth describes, and a live run that planted nothing is
    not. Setup records only — a teardown record says the world was put BACK,
    which is the opposite claim — and successful ones only, since a failed
    setup hook abandons the scenario ungraded anyway.

    Reads the records rather than ``Scenario.seeds_chaos`` on purpose: this is
    the question "what world was this run actually in?", and the archive
    answers it for a run that finished months ago, whose scenario file may
    since have gained or lost a hook. ``scripts/regrade_archive.py`` asks it
    of ``ScenarioOutcome.chaos_hooks`` for exactly that reason.
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

    "The chaos principal" is a literal second credential since platform
    v0.6.5: ``PLATFORM_CHAOS_TOKEN``, the ``incident-commander-chaos`` service
    account. It used to be ``settings.platform_token`` — the agent's own token,
    which then carried ``chaos:invoke`` — and that is exactly why the
    platform's chaos-audit filter was inert: the principal that seeded the
    fault was the principal being graded, so ``list_audit_events`` handed the
    agent the hook name and its arguments (divergence G3, owner decision O-4).

    Never raises for a hook failure: both callers want the record, and they
    disagree about what a failure MEANS — a failed setup abandons the
    scenario, a failed teardown lets the graded run stand and latches the
    environment dirty. Deciding that here would put both policies in the wrong
    place. A MISSING chaos credential is not that kind of failure and is not
    recorded as one: ``require_chaos_token`` raises, because nothing was
    attempted and no world was touched, and a run that silently recorded
    "seeding failed" would send the reader to the platform.
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
        # ``err`` already carries the platform's own refusal NAME out of
        # ``error.data.error_code`` (evals/chaos_hooks.py) — the difference
        # between "poison_fixture_name_in_use: reset, do not retry" and an
        # anonymous JSON-RPC -32011 that reads like flakiness. It is carried
        # verbatim rather than re-summarized for that reason.
        record = record.model_copy(update={"ok": False, "error": str(err)})
    else:
        record = record.model_copy(update={"result": result})
    if tracer is not None:
        tracer.write(
            {
                # One kind for both halves of a plan, with ``phase`` saying
                # which. A second TraceKind would have to land in
                # evals/tracing.py and gain a renderer in the trace
                # formatter, neither of which this packet owns — and the
                # record is the same shape either way.
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

    Order is the declaration, not an implementation detail: a cascading world
    is only the world it claims to be if its second fault lands on top of the
    first. Stopping at the first failure follows from the same reading — the
    hooks after it would be seeding into a world that does not exist yet.
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

    Never raises, and never stops early. Both are deliberate: this runs on
    the way out of a run that may already be carrying an exception, so
    raising would replace the run's own cause with the janitor's, and
    stopping at the first failure would skip compensators that might still
    have worked. Every hook gets its turn, and the caller decides what a
    failure means.

    ``ChaosTokenNotConfigured`` is the one non-hook failure that can reach
    here, and it is turned into the same combined text rather than raised, for
    the reason the paragraph above gives: this is the janitor. A run that
    reached teardown with no chaos credential cannot compensate anything, so
    the right outcome is the dirty-world latch the caller writes from this
    text, not an exception that replaces the run's own cause.
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

    Reads the latch a failed teardown wrote. An unreadable or malformed file
    still blocks — it can only exist because something wrote it, and the safe
    reading of "we cannot tell what went wrong" is not "carry on spending".

    The path is resolved at CALL time rather than bound as a default, so the
    module constant is one thing a test can redirect — the alternative is a
    unit suite that latches the real checkout's world dirty.
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

    Not instead of ``make eval-reset``: the reset is what actually restores
    the world, and this only records that it happened. They are separate
    entry points because the reset runs inside the platform's container and
    this file lives in the commander's checkout — but since cmd #233 the
    reset's last recipe line runs this itself, so the operator's normal path
    is one command. Invoking it alone is for the case with no world left to
    reset. Last line deliberately: make abandons a recipe at the first
    failing line, so a reset that did not succeed never clears the latch.
    """
    path = path or _CHAOS_BLOCK_PATH
    reason = chaos_block_reason(path)
    if reason is None:
        return None
    path.unlink(missing_ok=True)
    return reason


def _step_sink(tracer: JsonlTracer) -> StepSink:
    """Send each planner step's ``StepRecord`` to the trace store (WP-2.1).

    The strategy produces a record every step whether or not anyone is
    listening; this is what gives the records somewhere to land. One line of
    JSONL per planner step, beside the ``llm`` and ``mcp`` records of the same
    invocation, joinable to them by ``call_id`` — and append-only like every
    other record, so a re-run of a scenario adds its steps rather than
    replacing the earlier attempt's (invariant 9, F-002).

    Wired whenever a tracer exists, which includes a canned run with
    ``EVAL_TRACE_DIR`` set: the fake clients are not traced (they have no
    request or response to capture) but the *decisions* a canned run makes are
    the research data, and they are real. That is the offline landing place
    divergence D1 says WP-2.1 needs.

    The kind is stamped here rather than inside the record, so every trace
    record's kind still comes from the ``TraceKind`` enumeration the human
    renderer is held to.
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

    Three things a reader of a recorded result needs and cannot get anywhere
    else:

    * **which world**, by fingerprint and by path — the fingerprint because that
      is what ``make world-drift`` compares, and a row naming only a file would
      not say whether the world in it had moved;
    * **how complete the replay was** — answered, missed, refused. A miss means
      the agent asked the recording for something it does not hold, so the run is
      not comparable with another run of the same world, and the count is the
      only place that fact survives;
    * **the plan**, when the run was truncated. ``ACTION`` stays not-applicable
      (nothing executed), so "grade the plan" (04:117) is reported HERE rather
      than as a dimension: the planned tool, its arguments, and whether it is one
      the scenario expected. A paired comparison reads this field; nothing gates
      on it.
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
    # ``RunState.remediation_plan`` is a plain mapping on the checkpoint
    # (``state.py``: ``dict[str, object] | None``), so it is read by key. The
    # plan's own model lives in ``agent/remediation.py`` and is not imported for
    # this — a report row needs the three values, not the schema.
    plan = final.remediation_plan
    planned_tool = None if plan is None else plan.get("action_tool")
    expected = tuple(scenario.expectation.expected_action_tools)
    record["plan"] = {
        "expected_action_tools": list(expected),
        "planned_action_tool": planned_tool,
        "planned_action_arguments": None if plan is None else plan.get("action_arguments"),
        "planned_verify_tool": None if plan is None else plan.get("verify_tool"),
        # ``None`` — not ``False`` — when there is nothing to compare: no plan was
        # made, or the scenario expects no action. A boolean there would read as
        # "the plan was wrong", and "there was no plan" is a different fact.
        "matches_expected": (
            None if (planned_tool is None or not expected) else planned_tool in expected
        ),
    }
    return record


#: What a recorded run's evidence ledger records where it stopped. Written down
#: once because three readers compare against it: the trajectory a person opens,
#: the ``replay`` row on the outcome, and the test that proves the run really was
#: truncated rather than merely unlucky.
RECORDED_HANDOFF_REASON: Final[str] = (
    "recorded mode: stopped at the PLANNING handoff. A recording has no state to "
    "change and no audit log to observe a change in, so the plan was made and not "
    "executed; OUTCOME, ACTION and SAFETY are not graded for this run."
)


class RecordedHandoff:
    """Ends a recorded run where the plan is made, before anything is executed.

    Installed for ``REMEDIATING`` **and** ``AWAITING_APPROVAL`` on every recorded
    run — not only on the scenarios that declare ``expected_action_tools``. The
    declaration says what a run is GRADED on; what a run DOES is decided by the
    agent, and an agent that reaches the remediate handoff on a read-only
    scenario would otherwise hand a Tier-1 call to the replay client. That call
    is refused (ADR 0044), which is correct and is also a crashed scenario. This
    makes the refusal unreachable rather than merely right: in recorded mode
    there is no transition that could execute anything.

    It stops at ``ESCALATED`` rather than inventing a state, because the state
    machine's terminal set is an ADR-0002 decision and a new one would need an
    ADR of its own for no measurement gained. The plan itself is kept — it is
    what "grade the plan" (04:117) reads — and ``fired`` is what tells the caller
    the run was truncated, which is a different question from what the terminal
    state says.
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

    Three always, and a fourth when the run was truncated at the handoff:

    ``OUTCOME``
        A recorded run's terminal state is partly the harness's own choice — the
        handoff transition above ends it — so an outcome claim would be a claim
        about this function. Plan 02 § 189 says recorded mode is not for outcome
        claims at all, and 03 § 52 lists what it IS for: diagnosis, plan
        selection, candidate metrics, calibration.
    ``ACTION``
        Nothing was executed, so there is no action to be the right one.
    ``SAFETY``
        Safety is graded from the platform's audit log as ground truth
        (invariant 6) and a recording has none. A green SAFETY here would be the
        most dangerous number in the repo: "zero unauthorized actions" asserted
        by a run that could not have taken one.
    ``EVIDENCE``, only when truncated
        This one is a DIVERGENCE from WO-R3-198's stated scope (it names the
        three above) and it is reported as one. Two facts force it. A truncated
        run never calls the action tool, and these scenarios' evidence claims
        read that tool's own response — ``remediate_consumer_lag_success``
        asserts ``kill_key_cleared`` on ``restart_consumer_group``, which no
        replay can produce. And the handoff transition adds an evidence entry of
        its own, so leaving EVIDENCE graded would let harness prose satisfy a
        scenario's substring claim. Grading it would therefore be red for a
        reason that is the mode rather than the agent, which is the
        mis-attribution INCIDENTS.md exists to record.

    ``BUDGET`` and ``ROOT_CAUSE`` stay graded, and ``MODE_APPLICABLE_DIMENSIONS``
    refuses any attempt to mark ROOT_CAUSE — recorded mode exists to measure
    diagnosis. BUDGET is a real ceiling check over the calls that were actually
    made; a truncated run cannot fail it, which is equally true of any run that
    escalates early, so it is a weak claim rather than a false one.
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

    Uses ``CannedMCPClient`` for tool calls and ``CannedLLMClient`` for both
    the investigation planner and the briefing writer, each with its own
    per-scenario response queue keyed under ``canned_llm_responses``.

    ``recorded_world`` selects RECORDED mode (WP-3.3): the path to one
    recording, and the path IS the mode — there is no separate flag that could
    disagree with it. It changes four things and nothing else:

    * the agent's client is a ``RecordedMCPClient`` over that recording, so no
      platform is reached at all;
    * nothing is seeded, nothing settles, no precondition is polled and nothing
      is torn down — the world is a file, and a reset would be a reset of
      something this run never touched;
    * ``REMEDIATING`` and ``AWAITING_APPROVAL`` are replaced by a transition
      that stops the run at the ``PLANNING`` handoff, so no Tier-1 call is ever
      attempted (04:117). The replay client would refuse one anyway; this is
      what makes the refusal unreachable rather than merely correct;
    * OUTCOME, ACTION and SAFETY are graded as not-applicable-in-this-mode, and
      the row is stamped ``ExecutionMode.RECORDED``.

    What it does NOT change: the model. A recorded run's planner calls are real
    calls, so a recorded run spends real money at roughly live per-scenario
    rates. The LLM leg still degrades to the canned client under a placeholder
    key exactly as every other mode does, and a run that degraded says so in
    ``degraded`` — which is how the machinery is exercised at zero cost.

    ``model_role`` is recorded, not resolved, here: ``main`` resolves
    ``settings.agent_model`` from the role before the suite starts, and this
    is the label that travels with the result so a row can say which of the
    two roles produced it. It defaults to ``DEVELOPMENT`` because a run that
    did not say is not a benchmark run.
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

    # The scenario's fault, in one shape whichever way the YAML spells it:
    # a legacy ``chaos_setup`` normalizes to a one-hook plan (plan 01 § 4).
    # Read once, here, so the hooks that run and the hooks that are torn
    # down are provably the same tuple.
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
        # The replay clock is the run's OWN clock, so every age the agent can
        # compute is the age the recorder observed (ADR 0044). Built before the
        # LLM clients and before the transitions, like the live client, so a
        # recording that will not load costs nothing.
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
            # Let the fault become observable before anything looks for it.
            # One wait for the whole plan, ahead of the per-probe polling the
            # preconditions do: a cascade's second-order effect is not
            # visible the instant its last hook returns.
            if chaos_plan.settle_seconds:
                time.sleep(chaos_plan.settle_seconds)
            live_mcp_client = make_client(
                settings,
                tracer=tracer.mcp_hook() if tracer else None,
                token=mcp_token,
            )
            mcp_client = live_mcp_client
            # Establish the scenario's premise before spending anything on
            # it. Runs after seeding and before the first model call, so a
            # fault that was never manufactured costs one read instead of a
            # full graded run (see evals/preconditions.py, and
            # `bb1fa70abb4c` for the run that paid for this lesson).
            if scenario.expected_precondition:
                try:
                    _assert_preconditions(scenario, live_mcp_client, tracer)
                except PreconditionFailure:
                    live_mcp_client.close()
                    live_mcp_client = None
                    raise
        except BaseException:
            # The world was touched, so it has to be put back — even though
            # nothing will be graded. Same call the normal exit makes. Its
            # return value is dropped rather than carried: there is no row to
            # carry it on, and ``_tear_down`` has already written the latch,
            # which is the half that reaches the next invocation.
            _tear_down()
            raise
    else:
        mcp_client = CannedMCPClient(agent_visible.canned_tool_responses)

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

    # The two clients whose queues are read AFTER the run, to decide whether
    # there is a canned response left to spend. Bound before the metering
    # wrappers below, because ``has_remaining`` is the canned client's own
    # question and a wrapper is not a ``CannedLLMClient``.
    briefing_canned = briefing_llm if isinstance(briefing_llm, CannedLLMClient) else None
    judge_canned = judge_llm if isinstance(judge_llm, CannedLLMClient) else None

    # Cost, latency and context accounting for this run (WP-2.3). Every LLM
    # client the run can reach is wrapped, so the split is complete by
    # construction rather than by remembering to instrument each new call
    # site — and it covers the canned suite, where there is no tracer to
    # derive a split from (divergence D1).
    #
    # ``charged_to_ledger`` is declared HERE because this is the only place
    # that knows it, and the line it draws is WHOSE money a call is — not when
    # the call happened. Both post-run roles are billed after the state machine
    # has reached a terminal state, and they part company there:
    #
    # * ``briefing_writer`` is the AGENT's own cost. It writes the handoff the
    #   agent hands a human, on the agent's model, and until WO-R3-260 it
    #   reached no ledger at all — so every cost-per-run number undercounted
    #   the agent by exactly one call per run. It is charged (ADR 0015 § 4 as
    #   amended): metered because it is the agent's spend, and never a gate
    #   because there is no budget check left after a terminal state.
    # * ``briefing_judge`` is the EVALUATOR's own cost. It grades the run and
    #   is not part of it, so it stays out of the ledger and out of the
    #   reconciliation.
    #
    # Both are recorded either way — a billed call missing from the record is
    # the under-report ADR 0015 exists to prevent — and the report carries the
    # charged subset and the total side by side.
    accounting = RunAccounting()
    investigation_llm = accounting.meter(investigation_llm, "investigation_planner")
    remediation_planner_llm = accounting.meter(remediation_planner_llm, "remediation_planner")
    verification_judge_llm = accounting.meter(verification_judge_llm, "verification_judge")
    briefing_llm = accounting.meter(briefing_llm, "briefing_writer")
    judge_llm = accounting.meter(judge_llm, "briefing_judge", charged_to_ledger=False)

    # The inference strategy for this run (WP-0.2). Resolved here — the edge
    # that owns configuration — and passed to both the loop that runs it and the
    # provenance record that names it, so the two cannot disagree about which
    # strategy produced the row. An unknown INFERENCE_STRATEGY raises here,
    # before the first model call and before anything is spent.
    strategy = STRATEGIES.create(settings.inference_strategy, strategy_knobs(settings))
    transitions: dict[IncidentState, Transition] = dict(TRANSITIONS)
    transitions[IncidentState.INVESTIGATING] = make_llm_investigate(
        mcp_client,
        investigation_llm,
        model=settings.agent_model,
        strategy=strategy,
        # Where this run's per-step research records go: always to the run's
        # accounting (WP-2.3 needs the context size of every step, on every
        # run), and on to the trace store as well when one was built. A run
        # without ``EVAL_TRACE_DIR`` still writes no trace file — the sink
        # composes, it does not choose.
        record_step=accounting.step_sink(_step_sink(tracer) if tracer is not None else None),
        # Freshness re-probe (ADR 0009) is live-only: canned tool responses
        # are instant-consistent, and a re-probe would consume an extra
        # scripted planner response, breaking every canned scenario.
        reprobe_attempts=(settings.investigate_reprobe_attempts if live_mcp_available else 0),
        reprobe_delay_seconds=settings.investigate_reprobe_delay_seconds,
        # WP-2.4's third knob, wired here because this is its only consumer
        # (WO-R3-256): a strategy that needs more or fewer planner steps than
        # the loop's default says so through ``MAX_ITERATIONS_OVERRIDE``, and
        # until this line the setting configured nothing. Unset means the
        # loop's own default, named rather than re-declared — a second copy of
        # the number here would drift from the one the loop actually enforces.
        max_iterations=(
            _DEFAULT_MAX_ITERATIONS
            if settings.max_iterations_override is None
            else settings.max_iterations_override
        ),
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
    # RECORDED mode stops where the plan is made (04:117). Installed AFTER the
    # two transitions it replaces, so the replacement is visible as a
    # replacement rather than as an absence, and over both of PLANNING's
    # acting successors — a Tier-2 plan goes to AWAITING_APPROVAL, and an
    # approval against a recorded world is as meaningless as an action.
    handoff = RecordedHandoff()
    if recorded:
        transitions[IncidentState.REMEDIATING] = handoff
        transitions[IncidentState.AWAITING_APPROVAL] = handoff

    # Outside the try so a crash can still read what the run had spent when
    # it died — see ScenarioCrash.
    checkpointer = InMemoryCheckpointer()
    run: RunState | None = None
    # Same reason, one field further: the post-terminal briefing charge is not
    # in any checkpoint, so a crash AFTER enrichment (a grader raising, say)
    # would otherwise reconcile a split containing the briefing call against a
    # ledger that predates it, and report a false ``COST UNRECONCILED``.
    run_ledger: BudgetLedger | None = None
    try:
        # The scenario's declared cap IS the run's tool-call ceiling (ADR
        # 0019), not just the number it is graded against afterwards. Before
        # this, the two were different numbers and the planner was told the
        # fleet default of 25 in every scenario — including the ones whose
        # whole subject is behaviour under a tight budget.
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
        # The briefing is built BEFORE grading, because ``ScenarioExpectation.
        # expect_briefing_contains`` grades the handoff artifact as the human
        # receives it — enrichment included, since ``findings`` and
        # ``recommendation`` are empty in the deterministic template. Grading
        # still makes no LLM call of its own; it reads a finished object.
        briefing = render_briefing(final)
        briefing_error: str | None = None
        # The run's ledger AFTER the post-terminal briefing call is charged to
        # it (WO-R3-260). It is kept beside ``final`` and never put back on it:
        # ``grade`` below reads ``final`` and the BUDGET dimension is graded
        # from it, so a post-terminal charge that reached the graded state
        # would be a ceiling deciding an outcome the agent had already
        # finished. Metered, never gating — ADR 0015 § 4 as amended, made true
        # by which object holds the number rather than by everyone remembering
        # not to check it.
        run_ledger = final.budget
        if scenario.use_live_llm or (briefing_canned is not None and briefing_canned.has_remaining):
            try:
                briefing, run_ledger = enrich_briefing(
                    briefing, briefing_llm, model=settings.agent_model, budget=run_ledger
                )
            except (LLMError, ValidationError) as err:
                # Charge what the failed call already billed, exactly as the
                # investigation loop does with the same helper. A briefing
                # writer that raised after generating is billed work, and the
                # metering wrapper above has already recorded it — leaving the
                # ledger alone here would break the reconciliation on precisely
                # the run that cost money for nothing.
                run_ledger = accrue_llm_error(run_ledger, err, settings.agent_model)
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
        # Which world this run was actually in (INC-003, ADR 0040). The runner
        # is the only place that knows — ``grade()`` is a pure function of its
        # arguments and must stay one.
        if replay_client is not None:
            # A recorded world states which world it is IN the recording
            # (ADR 0043 § 4), and the recording is the only authority here:
            # ``live_mcp_available`` is False on this path by construction, so
            # the live/canned derivation below would answer "not the label's
            # world" for every recorded run and strike out the one dimension
            # recorded mode exists to measure. These two booleans are what the
            # recorder wrote down at the moment that world existed.
            replay_label = replay_client.world.world
            world_matches_ground_truth = label_describes_this_world(
                live_mcp=replay_label.live_mcp,
                chaos_seeded=replay_label.chaos_seeded,
            )
        else:
            # ``chaos_records`` holds the SETUP hooks at this point (teardown
            # runs after grading), and a canned run seeded nothing but is in the
            # label's world by definition.
            world_matches_ground_truth = label_describes_this_world(
                live_mcp=live_mcp_available,
                chaos_seeded=seeded_chaos(chaos_records),
            )
        report = grade(
            final,
            scenario.expectation,
            briefing=briefing,
            # The answer key, read straight off the scenario here and nowhere
            # else. The run above was built from ``agent_visible()`` (ADR
            # 0038), so this is the first and only point where the two sides
            # of the trust boundary meet — after the agent is finished.
            # ``None`` when the scenario declares no ground truth, which
            # grades ROOT_CAUSE vacuously rather than red.
            ground_truth=(
                None if scenario.ground_truth is None else scenario.ground_truth.root_causes
            ),
            # Which world the run was actually in (INC-003), decided above
            # because the answer differs by mode and a conditional inside an
            # argument list is a conditional nobody reads.
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
                    "kind": TraceKind.SCENARIO_END,
                    "scenario": scenario.name,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
        # The agent's run is over, cleanly or not, so the world goes back
        # before the exception leaves. A teardown failure rides ALONG with
        # the crash rather than replacing it: "the agent crashed" and "the
        # shared environment is now dirty" are two facts, and collapsing
        # them loses whichever one the reader needed.
        crash = ScenarioCrash(
            exc,
            () if run is None else tuple(checkpointer.history(run.incident_id)),
        )
        if live_mcp_client is not None:
            live_mcp_client.close()
            live_mcp_client = None
        crash.teardown_error = _tear_down()
        # What the run had billed before it died. The crash row is the
        # only place this can reach a report, and a crashed run's spend
        # is spend (invariant 9 one layer down).
        crash.accounting = accounting
        # And the ledger that spend was charged to, when the run got far
        # enough to have one. ``None`` falls back to the last checkpoint,
        # which is the right answer for a crash before the terminal state.
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
    # ``run_ledger`` is bound the moment the loop returns, and this path is
    # only reached when it did. The fallback makes that a fact the type
    # checker can see rather than one a reader has to trace.
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
        # A recorded run is degraded when the recording could not answer
        # something the agent asked for, or refused something it tried. The
        # field already meant "this row is not the measurement it looks like"
        # (a declared-live leg that fell back to canned); a miss is the same
        # statement about a replayed world, and ADR 0044 uses the same word for
        # it. Deliberately NOT true merely because a recorded run replayed a
        # platform — that is the mode, not a degradation of it, and
        # ``execution_mode`` below is where a reader learns it.
        degraded=(scenario.use_live_mcp and not live_mcp_available and not recorded)
        or (scenario.use_live_llm and not live_llm_available)
        or (replay_client is not None and replay_client.degraded),
        provenance=build_provenance(
            scenario.name,
            settings,
            model_role=model_role,
            invocation_id=invocation_id,
            # Derived from what the legs ACTUALLY did, the same two values
            # the live/canned flags above are derived from — never from the
            # --live flag, which says what was asked for. ``recorded`` is first
            # because it is the narrowest true statement: the platform leg was
            # a replay of one recorded moment, whatever the model leg did.
            execution_mode=(
                ExecutionMode.RECORDED
                if recorded
                else (
                    ExecutionMode.LIVE
                    if (live_mcp_available or live_llm_available)
                    else ExecutionMode.CANNED
                )
            ),
            # The run's own final ledger: seeded maxima and all four meters —
            # and, since WO-R3-260, the briefing writer's post-terminal call,
            # which is the agent's cost and belongs in the number a reader
            # takes for "what this run spent". The graded ``final.budget`` is
            # the same object up to that one charge; the difference is exactly
            # the metered-but-not-gating spend.
            budget=reported_ledger,
            recorded_at=tick(),
            # The strategy object the loop above actually ran with.
            strategy=strategy,
        ),
        # What it cost, by role and by planner step, reconciled against the
        # same ledger the provenance above carries (WP-2.3). It has to be the
        # same one: the charged split now includes ``briefing_writer``, so
        # reconciling against the pre-briefing ledger would report every run
        # with an enriched briefing as unreconciled.
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

    The `grader-brittleness` bucket was already correct on live run
    ``4974811d236f`` and it still cost a full trace read to learn what had
    gone wrong: the claim was written for an unfiltered listing and the agent
    verified with a filtered one. Both halves of that sentence are in the
    graded artifacts already — the claim is in the EVIDENCE detail, and the
    call shapes are in the ledger's ``arguments`` — so the diagnosis belongs
    in ``report.json`` where the coordinator reads it.

    The call shapes are listed per tool, deduplicated, in call order, and only
    for tools the failing claims actually name. That is the comparison a
    reader has to make: "the claim wants THIS shape, the agent used THAT
    one".
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

    Heuristic and deliberately conservative: anything ambiguous lands in
    "unclassified" rather than a wrong bucket. Priority order mirrors the
    debugging discipline in docs/lessons/live-eval-noise-sources.md —
    environment before consistency before variance before grader.
    """
    if report.passed:
        return "passed", ""
    dims = {d.dimension: d for d in report.dimensions}
    failing = {d.dimension for d in report.dimensions if not d.passed}
    evidence = final.evidence if final is not None else ()
    summaries = [e.result_summary for e in evidence]
    # First, and ahead of "transport", because it is the one bucket that says
    # the AGENT was right and the HARNESS could not read it. Live run
    # 779b19a287a7 graded RED on three dimensions with a correct decision in
    # hand, and reported `failure_class: unclassified` — two of the three
    # prefixes below already contain "LLM" and "invalid", so without this it
    # would have been filed as a network problem instead (ADR 0035).
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
    elif isinstance(cause, ChaosSetupFailed) or "chaos_setup" in error_detail:
        # ``run_all`` no longer routes a setup failure here at all — it
        # records an ungraded row instead, because there is no world to
        # grade in. This branch survives for the direct ``run_scenario``
        # caller (a test, a script) that still wants a row, and the
        # substring half survives for a caller raising a plain RuntimeError
        # in the old shape.
        crash_class = "shared-env"
    else:
        crash_class = "transport"
    # What the crash had billed, when the crash carried the measurement AND a
    # checkpoint to reconcile it against. ``None`` means no measurement exists
    # — a direct call from a test, or a failure before the first client was
    # built — and it is deliberately distinct from a zeroed record, which
    # would claim the run was free. The two conditions travel together in
    # practice: every LLM call the agent makes is inside a transition, and a
    # transition that ran left a checkpoint behind it.
    crash_accounting = exc.accounting if isinstance(exc, ScenarioCrash) else None
    # The crash's own ledger when it reached a terminal state, else the last
    # checkpoint's. The two differ by exactly the post-terminal briefing
    # charge, and the split being reconciled here contains it.
    crash_ledger = exc.ledger if isinstance(exc, ScenarioCrash) else None
    accounting = (
        None
        if crash_accounting is None or partial is None
        else build_accounting(crash_accounting, crash_ledger or partial.budget)
    )
    outcome = ScenarioOutcome(
        scenario=scenario.name,
        **_grouping_keys(scenario),
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
        # A crashed run whose teardown ALSO failed says two things, and the
        # second one outlives this row: the world the next live run would
        # inherit is dirty. Carried onto the row for the same reason it is
        # carried on a clean one — the report is the durable record, and the
        # on-disk latch is the enforcement.
        teardown_error=(exc.teardown_error if isinstance(exc, ScenarioCrash) else None),
        # Provenance survives the crash (ADR 0013). These defaulted to False,
        # so every crashed row in a live report claimed it had run canned —
        # and `degraded` False alongside said that was intended. A row that
        # misdescribes how it ran is worse than a missing row: the report is
        # the artifact, and a reader counting live coverage counted wrong.
        # ``recorded`` overrides the declared MCP leg, for the reason the
        # comment above gives: the row must not misdescribe how it ran. A
        # recorded run reached no platform whatever the scenario declares, and a
        # crashed recorded row claiming a live platform is exactly the reader
        # counting live coverage wrong.
        live_mcp=scenario.use_live_mcp and not recorded,
        live_llm=scenario.use_live_llm,
        # Same reasoning one field up: a crashed row that cannot say which
        # model and revision it crashed under is a row nobody can act on.
        # ``settings is None`` only for a direct call from a test that knows
        # no configuration — there is no model to name, so the record is
        # absent rather than invented.
        provenance=(
            None
            if settings is None
            else build_provenance(
                scenario.name,
                settings,
                model_role=model_role,
                invocation_id=invocation_id,
                # The DECLARED legs, like the two flags above: a crash can
                # happen before either leg is chosen, and what the scenario
                # asked for is the only honest answer available here. Except for
                # ``recorded``, which is not a declaration but an instruction
                # the caller gave — the platform leg WAS a replay, however early
                # the crash came.
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
            # NOT a graded row, and this is the whole point of the branch
            # (plan 01 § 4). A seeding failure means the benchmark world was
            # never built, so there is nothing the agent could have done
            # right or wrong in it. A crash row here would carry a
            # ``GradeReport`` saying the scenario failed, and every rate
            # derived from the report would then describe the agent using an
            # event that happened before it started.
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
        # _crashed_result outcomes keep degraded=False defaults: a crash is
        # already a failure, so this post-run count may undercount the
        # pre-run estimate when a live scenario crashes — acceptable, the
        # --live gate in main() is pre-run.
        degraded_count=sum(1 for o in outcomes if o.degraded),
        only_patterns=only_patterns,
        # Derived from the role every row was stamped with, so the console
        # line, the report and the archive are the same value by
        # construction — the ``degraded_count`` discipline (finding A-01).
        closing=model_role is ModelRole.BENCHMARK,
        outcomes=outcomes,
        ungraded=tuple(ungraded),
    )
    return report, trajectories, briefings


def write_report(report: RunReport, *, directory: Path = _REPORTS_DIR) -> Path:
    """Write ``report`` as ``report.<stamp>.<invocation_id>.json`` and return the path.

    The run's own identity names the file: ``generated_at`` supplies the
    stamp and ``invocation_id`` the discriminator, so the report cannot be
    labelled as a run other than the one that produced it. Exclusive-create
    — a second write at the same path is the same run reported twice, which
    is a bug, not a refresh.
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

    Each ``Trajectory`` already carries the ``invocation_id`` that produced
    it, so the filename is derived from the record rather than supplied
    beside it and cannot disagree with the contents. ``timestamp`` defaults
    to now; one run passes a single stamp so its whole suite groups
    together.
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

    ``EscalationBriefing`` is a product model and carries no run identity of
    its own, so unlike ``write_trajectories`` this one is told the
    ``invocation_id`` explicitly. Required, not defaulted: a briefing filed
    under an empty id is a briefing that cannot be joined back to the run
    that paid for it.
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

    WP-2.2's acceptance number. Derived from the rows rather than stored on
    ``RunReport``, which is what lets it be computed over the committed
    ``baseline.json`` and over every archived report — all of them predate
    the dimension, and evidence is never rewritten (invariant 9). The
    denominator is the corpus the loader actually produced, never a literal:
    the plan's acceptance line still says 37 and the directory holds 41.

    "Graded" is a scenario whose ROOT_CAUSE detail is substantive. Reusing
    ``is_vacuous_detail`` rather than re-deriving the condition keeps this
    number and the regression gate's vacated-assertion check reading the
    same signal — if one moves, both move.

    The ungraded rows are then split in two (INC-003): a row held back
    because the run's world is not the label's world is counted and named
    separately from a row whose scenario declares no label at all. Read off
    the detail the grader wrote rather than re-derived from the scenario
    files, so the number holds for an archived report as well — including one
    written before this rule existed, where it is simply zero.
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
    # Printed from the first row's own record, not from the settings this
    # process happens to hold: the line and the artifact then cannot
    # disagree, which is the same rule the degraded line below follows.
    first = next((o.provenance for o in report.outcomes if o.provenance is not None), None)
    if first is not None:
        print(
            f"provenance: {first.agent_model} ({first.model_role.value} role), "
            f"judge {first.judge_model}, commander {first.commander_revision[:12]}, "
            f"platform {first.platform_image_digest[:19]}, {first.execution_mode.value}"
        )
    if reason := report.non_closing_reason:
        print(f"NON-CLOSING: this report cannot close a phase — {reason}")
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
    print(root_cause_coverage(report).describe())
    # The run's bill, from the rows' own accounting records (WP-2.3) rather
    # than re-added from settings or a trace — the console number and the
    # artifact number are then the same value by construction, the rule the
    # degraded line above was written for (A-01). Silent on a canned suite,
    # where every fake bills nothing and a "$0.000000" line would be noise.
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
    """Extract scenario filters from ``--only <pattern>``.

    Accepts multiple patterns via repeated ``--only`` flags or as a
    comma-separated single value. Empty list means "no filter".

    How a pattern is matched depends on the mode, and ``main`` decides:
    under ``--live`` without ``--smoke`` each pattern must be a scenario's
    FULL NAME (a substring there widens the selection past the ADR 0020
    gate); under ``--smoke`` and offline a pattern matches any scenario
    whose name contains it. Parsing is the same either way.

    Examples:
        --only remediate_dlq_backlog_success          → one live selection
        --only consumer_lag_healthy,tool_             → two (comma-separated)
        --only remediate_ --only dlq_                 → two (repeated flag)
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

    ``--model-role development`` / ``--model-role benchmark`` (and the
    ``--model-role=<value>`` spelling), resolving ``AGENT_MODEL`` from
    ``DEVELOPMENT_MODEL`` or ``BENCHMARK_MODEL`` respectively (plan 02 § 9).

    Two properties are deliberate. The default is ``development``: a run that
    did not name a role is not a benchmark run, and the expensive mistake is
    a development run read as a reported number. And an unrecognised value is
    REFUSED rather than coerced to that default — the precedent is the
    ``--only`` refusals (a pattern that matched nothing used to run the
    remainder), because "--model-role benchmrk silently ran development" is
    the same class of bug as "--only typo silently ran a wider suite".

    Returns a pair rather than "a role or a message": ``ModelRole`` is a
    ``StrEnum``, so a caller cannot tell a role from a refusal string with
    ``isinstance`` — the refusal would be read as a selection, which is how
    the first draft of this made every other refusal in ``main`` unreachable.
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


#: The one value ``--mode`` takes. The other two modes are selected by what the
#: run can reach — ``--live`` for the platform, nothing for canned — and giving
#: them flag spellings here would be two ways to ask for one thing.
RECORDED_MODE: Final[str] = "recorded"


def _parse_mode(argv: Sequence[str]) -> tuple[str | None, str]:
    """The ``--mode`` selection: ``""`` for absent, the value, or ``None`` + a refusal.

    Three states rather than two, for the reason ``_parse_model_role`` records:
    a refusal that looks like a selection makes every later refusal unreachable.
    An unrecognised value is REFUSED rather than ignored — "--mode recordd ran
    the canned suite and reported it" is the same class of bug as a dead
    ``--only`` pattern, and this one would spend money on the way.
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

    Two selections, and the difference is what a reported result rests on.

    Without ``--world``, each scenario replays its NEWEST recording, resolved
    through ``artifacts.newest_or_none`` — never a glob and never ``ls -t``
    (CLAUDE.md invariant 9's corollary). A scenario with no recording is a
    REFUSAL, not a canned fallback: ``run_scenario`` would otherwise serve the
    canned fixtures and the row would land in a recorded report as though a
    world had been replayed, which is the same fabrication the ``--live``
    canned-only gate refuses one mode over.

    With ``--world <id>``, one exact recording is pinned — by its invocation id,
    which is what makes a reported number reproducible when a scenario has
    several recordings and the newest is not the one the result came from. A
    scenario NAME is accepted there too and means "the newest of that scenario":
    the two cannot collide (an invocation id is 12 hex characters and a scenario
    name is not), and refusing the friendlier spelling would only send operators
    to look the id up. Either way it must resolve to exactly one recording and
    the selection must be exactly that scenario — a pinned world and a wider
    selection is an instruction with two readings.
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

    # One resolver for both callers — ``make world-drift`` asks the same question
    # of the same id, and two copies of the rule would let the two resolve to
    # different recordings.
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

    The refusal has to name the reason per scenario, not just the names: the
    three causes have three different repairs. A chaos or write scenario
    belongs to the remediation stage under the full token; a
    ``smoke_exclusion`` is a deliberate hold-back whose recorded reason says
    what would have to be true to lift it.

    Chaos seeding and ``expected_action_tools`` can both be true, and both
    are reported. ``smoke_exclusion`` cannot coexist with either — the schema
    validator refuses a hold-back the predicate already covers — so it is
    reported alone.
    """
    causes: list[str] = []
    if scenario.seeds_chaos:
        # Named by the field the YAML actually uses, because the reader's
        # next move is to open that block. The wording for the legacy
        # spelling is unchanged.
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
    # Not reachable from a scenario the caller filtered on `not in_smoke_pass`,
    # and stated rather than assumed: a silent empty reason would turn the
    # refusal into a bare list of names.
    return "; ".join(causes) if causes else "not in the derived smoke pass"


def main() -> int:
    if "--clear-chaos-block" in sys.argv[1:]:
        # The operator half of the teardown latch, and deliberately its own
        # invocation rather than a flag on a run: clearing it asserts that
        # `make eval-reset` has already put the world back, and an assertion
        # bundled into the run it unblocks is one nobody makes consciously.
        cleared = clear_chaos_block()
        if cleared is None:
            print("no chaos teardown block is set; nothing to clear")
        else:
            print(f"cleared chaos teardown block: {cleared}")
        return 0
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
    if live and (blocked := chaos_block_reason()) is not None:
        # A previous run's chaos teardown did not complete, so the world the
        # next live run would read as its baseline still carries a fault
        # nobody intended. Refused HERE — before settings, before the
        # guards, before anything is spent — for the same reason exits 6 and
        # 7 are: a refusal that arrives after the money is gone is a report,
        # not a guard. Offline runs are untouched; canned scenarios share no
        # world.
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
        # The one combination that could touch the shared world while claiming
        # to be a replay. Refused here rather than reconciled: --live and
        # --smoke are about reaching a platform, and a recorded run's platform
        # is a file.
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
    # One identity per invocation, shared by the tracer, the trajectories,
    # the report, and the archive directory — so every artifact this run
    # produces can be joined, and none of them can collide with another
    # run's (CLAUDE.md invariant 9).
    invocation_id = uuid.uuid4().hex[:12]
    only_patterns = _parse_only(sys.argv[1:])
    if live and not smoke and not only_patterns:
        # A bare `--live` is the whole suite against one shared platform, with
        # real spend and no reset between scenarios. It was already refused —
        # but only INCIDENTALLY, by the exit-8 canned-only gate below, which
        # fires because the tree happens to contain scenarios with no live
        # leg. (How many is deliberately not written here: the count belongs
        # to the corpus, is stated once in docs/runbook.md, and is asserted
        # against evals/benchmark_inventory.json by
        # tests/unit/test_pre_spend_guards.py. The argument below does not
        # need the number, and every copy of it was one more thing to drift.)
        # That refusal is a fact about the scenario directory, not about
        # the invocation: give every scenario a live leg and an unfiltered
        # `--live` starts spending, having changed nothing in this file. The
        # exit-8 message also misnames the problem ("these scenarios cannot run
        # live") when the actual problem is "you named no scenario".
        #
        # So the missing filter is refused here, structurally, before the
        # settings load — no env, no scenario tree, nothing to be contingent
        # on. `--smoke` is exempt: it derives its own selection (WO-R2-123)
        # and runs read-scoped. The Makefile's `ifndef ONLY` guard on
        # `eval-live` says the same thing one layer out; this is the backstop
        # for every other entry point, since `python -m evals.runner --live`
        # never touches make.
        print(
            "LIVE FAIL: --live requires --only <scenario_name> (or --smoke). "
            "An unfiltered live selection is the whole suite against one shared "
            "platform — real spend, no reset between scenarios."
        )
        print("Name exactly one scenario, e.g. make eval-live ONLY=remediate_dlq_backlog_success")
        print("no scenarios ran, nothing was spent")
        return 2
    try:
        # A recorded run reads the real environment, and that is the whole
        # reason it costs money: the platform is a replay, the MODEL is not.
        # ``PLATFORM_MCP_URL`` is read too and deliberately ignored —
        # ``run_scenario`` forces the live MCP leg off whenever a recording is
        # passed, so a real URL in .env cannot make a recorded run touch it.
        settings = _settings_for_mode(live or recorded)
    except ValidationError as err:
        # Exit 3 is the preflight/env code (see the smoke-token and LLM-auth
        # exits below); a raw traceback would exit 1, the code reserved for
        # "a scenario failed" (A-15).
        fields = ", ".join(
            ".".join(str(part) for part in detail["loc"]) or "(settings)" for detail in err.errors()
        )
        print(f"PREFLIGHT FAIL (env): invalid or missing settings — {fields}")
        return 3
    # The role resolves the model the run bills, in one place, before any
    # scenario starts. ``model_copy`` and not a second Settings construction:
    # the resolved id is already guaranteed priced (the same validator that
    # guards AGENT_MODEL guards both role settings), and re-reading the
    # environment here would be a second chance for it to differ.
    settings = settings.model_copy(update={"agent_model": settings.model_for_role(model_role)})
    print(f"model role: {model_role.value} → AGENT_MODEL={settings.agent_model}")
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
    if smoke and not only_patterns:
        # The smoke pass selects itself (WO-R2-123). Membership is the
        # `in_smoke_pass` predicate on the scenario — eligible by the
        # runner's own two refusals, minus whatever declares a
        # `smoke_exclusion` reason in its own YAML. Until now this was a
        # hand-written pattern list in the Makefile (`SMOKE_ONLY`), which
        # #151 could only *check* after the fact: a renamed scenario or a
        # new read-only one still fell out of the pass, and the test that
        # caught it was a separate thing that had to be kept in step. A
        # derived selection cannot fall out of step with the tree it is
        # derived from. `SMOKE_ONLY=` remains as an operator override and
        # arrives through --only, below, exactly as before.
        held_back = sorted(
            (s.name, s.smoke_exclusion) for s in scenarios if s.smoke_exclusion is not None
        )
        scenarios = [s for s in scenarios if s.in_smoke_pass]
        print(f"smoke selection: {len(scenarios)} scenario(s) derived from {_SCENARIOS_DIR}")
        for name, reason in held_back:
            # Printed for the same reason the per-pattern counts below are:
            # the smoke log has to carry its own record of what it covered
            # and, here, of what it knowingly did not.
            print(f"  held back (smoke_exclusion): {name} — {reason}")
        if not scenarios:
            # Not reachable from a healthy tree, and precisely why it is
            # checked: a derivation that silently comes back empty is a
            # green smoke pass over nothing, which is the failure #151 was
            # written about wearing a different hat.
            print(
                "SELECTION FAIL: no scenario is in the smoke pass — every scenario "
                "either declares chaos_setup/expected_action_tools or carries a "
                "smoke_exclusion"
            )
            print("no scenarios ran, nothing was spent")
            return 2
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
        if live and not smoke:
            # On the spend path a pattern must be a scenario's FULL NAME, so
            # the selection is exactly what the operator typed and nothing
            # adjacent. Substring matching silently widened it: `ONLY=dlq_backlog`
            # takes `dlq_backlog` AND `remediate_dlq_backlog_success`, the
            # read-only one drains the seeded replay_safe pool before the
            # remediation is graded, and the report blames the agent. The ADR
            # 0020 gate does not catch it — only ONE of the two is mutating, so
            # `len(mutating) > 1` is False (2026-08-30: a read-only stage
            # smuggled in a mutating scenario).
            #
            # Exact match FIRST, so a name that is also a prefix of another
            # name stays runnable: `ONLY=dlq_backlog` selects the one scenario
            # called that. Refusing it because a longer name contains it would
            # make that scenario impossible to run live at all.
            #
            # `--smoke` keeps substring matching: it derives its own selection,
            # runs read-scoped, and SMOKE_ONLY is a documented substring
            # override (`SMOKE_ONLY=consumer_lag_`). Offline keeps it too —
            # no spend, no shared platform, and `make eval-reg` / `make baseline`
            # refuse ONLY outright, so nothing downstream reads a widened
            # offline selection.
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
        # A read-only stage runs the DERIVED smoke set and nothing else.
        # run_scenario fires chaos_setup under the evaluator's chaos principal
        # (PLATFORM_CHAOS_TOKEN) regardless of --smoke, so seeding mutates the
        # shared world during the stage whose whole claim is that it changes
        # nothing. A read-scoped AGENT token does not prevent it — the seeding
        # never used that credential, and since v0.6.5 it could not.
        # The #80 principal guard only inspects the AGENT
        # client's token and the exit-5 post-stage audit sees the write after
        # it lands, so the only prevention is refusing the selection outright,
        # here: after --only (the reachable channel, since SMOKE_ONLY is
        # .env-overridable) and before preflight, guard, and any spend. There
        # is no opt-out flag: a scenario outside the derived set is not a
        # smoke scenario (S-03).
        #
        # This checked `chaos_setup` alone, and that was half the door. The
        # derived set is `in_smoke_pass` — NOT chaos_setup, AND no
        # expected_action_tools, AND no smoke_exclusion — but `--only`
        # bypasses the derivation entirely (`if smoke and not only_patterns`
        # above), so an override could re-admit anything the derivation had
        # dropped for the other two reasons. A scenario with
        # expected_action_tools and no chaos_setup passed every guard here: a
        # graded Tier-1 write inside the stage whose purpose is proving the
        # smoke token cannot write. Same shape as 2026-08-30, different door.
        # Scenario.smoke_eligible's own docstring already claimed this
        # refusal existed; now it does.
        #
        # The override may still NARROW the derived set — that is what
        # SMOKE_ONLY is for, and substring patterns keep working for
        # scenarios that are in it. It may not widen it.
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
        # ``seeds_chaos``, not ``chaos_setup``: ADR 0020 is UNCHANGED by the
        # ChaosPlan work, and a plan with two hooks is still ONE scenario —
        # it seeds one world and is reset once. What would have changed
        # silently is the other direction: reading the legacy field leaves
        # ``chaos_setup`` None on a plan-declaring scenario, so a two-hook
        # remediation scenario would stop counting as mutating and could be
        # selected alongside another one. That is a regression, not a
        # simplification.
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
            # The mirror of the --live degradation refusal below, and the same
            # argument: a recorded run whose planner is the canned client is a
            # CANNED run wearing a recorded label, and its row would be counted
            # as a measurement of a strategy that never ran. Refused before the
            # archive exists.
            #
            # It is also why the offline proof of this mode drives ``run_all``
            # directly from tests rather than through this entry point: there the
            # canned planner is the point and the row is never reported.
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
    chaos_guard_required = live_platform and any(s.seeds_chaos for s in scenarios)
    if write_guard_required or chaos_guard_required:
        # TWO clients, because there are now two principals (platform v0.6.5,
        # owner decision O-4). The agent's client is `mcp_token` — None on a
        # live non-smoke run, so `settings.platform_token`, the credential
        # `run_scenario` hands the agent — and `assert_write_capable_principal`
        # asks both halves of the question about it: it must be able to act,
        # and it must NOT be able to seed (a token that can seed can read the
        # chaos audit rows, which is the answer key). The chaos client is
        # `PLATFORM_CHAOS_TOKEN`, the credential `chaos_setup` actually fires
        # under, and it is guarded where it is used rather than where it is
        # configured — the whole lesson of evals/guards.py.
        #
        # The chaos credential is resolved HERE, before the archive exists and
        # before a single model call, so an unset PLATFORM_CHAOS_TOKEN costs
        # one refusal instead of a scenario crash mid-invocation.
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
                    # A chaos-only selection declares no Tier-1 action, so
                    # there is no write scope to assert — but there is still an
                    # agent, and the leak does not care whether anything was
                    # remediated. Asserted here rather than left to the write
                    # guard it does not run.
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

    # The post-stage audit assertion below can only ever see the newest 200
    # rows: list_audit_events has no offset and no created_after (verified
    # against the pinned platform — `offset` is refused -32602
    # extra_forbidden), so rows that scroll past the cap are unreachable the
    # moment the stage ends. The one window the guard CAN cover is the one
    # it watches while it happens, so the scan takes a page after every
    # scenario and the assertion consumes the union. Without this a smoke
    # stage louder than 200 `agent.tool_invoked` rows exits 5 as
    # "inconclusive" — a false red on a paid run, masking the real result.
    audit_scan = AuditWindowScan(stage_started_at) if guard_required else None
    scan_client = make_client(settings, token=mcp_token) if guard_required else None

    def _after_scenario(result: ScenarioResult) -> None:
        archive_scenario(target, result, invocation_id=invocation_id, trace_dir=trace_dir)
        if audit_scan is None or scan_client is None:
            return
        try:
            audit_scan.checkpoint(scan_client)
        except Exception as err:  # noqa: BLE001 — a checkpoint is best-effort
            # Not fatal: the assertion is the post-stage read, and a missed
            # checkpoint only narrows coverage, which fails closed on its
            # own. Printed rather than swallowed so a systematically broken
            # checkpoint is visible instead of quietly degrading the guard
            # back to one page.
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
    # The per-scenario archive writes already happened, inside run_all.
    # report.json goes down next — the completion marker, and the last write
    # into the archive — and only then are the top-level copies written.
    # Order matters: if those writes ever fail, the durable record is
    # already complete on disk. Until 2026-08-08 only the flat files
    # existed, so a routine offline `make eval` erased Run 001's paid live
    # trajectories (study/findings.md F-003).
    #
    # One stamp for the whole run: report.generated_at. The suite's
    # trajectories, briefings and report therefore share a filename prefix
    # and sort together, and every one of them carries this run's
    # invocation_id.
    archived = finalize_archive(target, report)
    written_report = write_report(report)
    write_trajectories(trajectories, timestamp=report.generated_at)
    write_briefings(
        briefings, ran_names, invocation_id=invocation_id, timestamp=report.generated_at
    )
    _print_summary(report)
    print(f"run archived: {_repo_relative(archived)} (immutable)")
    print(f"report: {_repo_relative(written_report)}")

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

    # Order is the blast radius, widest first. A contaminated world outranks
    # everything because it is the only one of the three that reaches the
    # NEXT invocation; an ungraded scenario outranks a failed one because
    # "the agent failed" is the reading exit 1 invites and is exactly what
    # did not happen. Every one of them lands AFTER the archive and the
    # report are on disk — evidence first, verdict second.
    # The latch, not only the report rows: a scenario abandoned at SEEDING
    # has no row to carry ``teardown_error``, and its teardown can fail too.
    # The latch is written on every path, so reading it here is the one check
    # that covers all of them. Live only, like the pre-run refusal — an
    # offline suite shares no world with the dirty platform and must keep
    # exiting on its own result.
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
