"""Charge one LLM call to the incident budget ledger (ADR 0015) — and record
what it cost, how long it took, and how much context it was given (WP-2.3).

Lives here rather than in ``agent/state.py`` because ``state.py``
deliberately stays free of ``llm`` imports: the checkpoint schema must
not depend on the model client.

Two things are built here, and the relationship between them is the point:

* the **ledger accrual** (``accrue_*``) — four running totals ADR 0015 holds
  an unattended run to, and nothing else;
* the **run accounting** (``RunAccounting``) — the same billed work split by
  role, by token class, by call and by planner step, which is what a
  cost/accuracy comparison between two strategies is computed from.

The split is derived from the same calls the ledger sees, never re-derived
from prices or re-counted from a trace, and ``RunAccounting.reconciles_with``
is what keeps saying so: a breakdown that does not add up to the total it
breaks down reports one strategy as cheaper than it was, and nothing in a
green test suite would notice.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from pydantic import BaseModel

from incident_commander.agent.state import BudgetLedger
from incident_commander.agent.strategies.records import StepRecord, StepSink
from incident_commander.llm.client import (
    LLMClientProtocol,
    LLMError,
    LLMResult,
    LLMUsage,
    elapsed_ms_of,
)
from incident_commander.llm.pricing import cost_of
from incident_commander.llm.repair import RepairedCall


def token_volume(usage: LLMUsage) -> int:
    """ADR 0015's token *volume* for one call: every class, discards included.

    One definition, because there are two consumers — the ledger meter below
    and every accounting record beside it — and two copies of this sum is
    exactly how a breakdown stops adding up to the total it breaks down. The
    reconciliation test would catch the drift; having one expression means
    there is nothing to catch.
    """
    return (
        usage.input_tokens
        + usage.output_tokens
        + usage.cache_creation_tokens
        + usage.cache_read_tokens
        + usage.discarded_output_tokens
    )


def accrue_llm_usage(budget: BudgetLedger, usage: LLMUsage, model: str) -> BudgetLedger:
    """Return ``budget`` with this call's tokens and dollars added.

    ``tokens_used`` is total token *volume* — input + output +
    cache_creation + cache_read. ``client.py`` caches the system prompt,
    so on a live run most input volume arrives on the cache counters;
    summing only input+output metered the un-cached remainder and
    under-enforced ``BUDGET_MAX_TOKENS`` exactly when caching worked
    well (C-06). ``usd_used`` carries the cost weighting, so the volume
    meter stays a volume meter (ADR 0015).

    ``discarded_output_tokens`` joins the volume for the same reason it
    joins the dollars: a retried attempt spent real capacity, and a
    ceiling that cannot see it is not a ceiling.
    """
    return budget.model_copy(
        update={
            "tokens_used": budget.tokens_used + token_volume(usage),
            "usd_used": budget.usd_used + cost_of(model, usage),
        }
    )


def accrue_llm_error(budget: BudgetLedger, err: Exception, model: str) -> BudgetLedger:
    """Charge whatever a *failed* LLM call already billed. No-op if unknown.

    The transitions turn an ``LLMError`` into a graded escalation rather
    than a crash, which is right — but it made the failure free. A
    truncated response and three retried 5xx are both billed work that
    ended in an ``except`` arm, and neither reached ``tokens_used`` or
    ``usd_used``. Callers pass the exception they caught; anything that is
    not an ``LLMError`` carrying usage (a ``ValueError`` from the canned
    client, a pydantic ``ValidationError``) leaves the ledger untouched.
    """
    if not isinstance(err, LLMError) or err.usage is None:
        return budget
    return accrue_llm_usage(budget, err.usage, model)


def accrue_structured_call(
    budget: BudgetLedger, call: RepairedCall[Any], model: str
) -> BudgetLedger:
    """Charge a possibly-repaired structured-output call: every leg of it.

    A repair is one more billed LLM call (ADR 0035), and ADR 0015's rule is
    that a billed call reaches the ledger whatever it produced. Charging only
    the leg that parsed would make the repaired path look exactly as cheap as
    the clean one, which is the shape of under-report that lets an unattended
    run outspend its ceiling.
    """
    for failure in call.failures:
        budget = accrue_llm_error(budget, failure, model)
    return accrue_llm_usage(budget, call.result, model)


def _elapsed_ms(seconds: float) -> int:
    """Whole milliseconds, never negative. ``0`` is a measurement, not a gap.

    ``llm/client.py``'s own definition, used rather than repeated: since
    WO-R3-260 the client times its logical calls too (``LLMResult.elapsed_ms``)
    and a second rounding here would let the two latency columns disagree by a
    millisecond for no reason a reader could explain.

    This number is taken around the call from OUTSIDE the client, so it covers
    every client — including the canned one, which does not time itself. A
    canned call that really did take under half a millisecond reports ``0``
    and means it.
    """
    return elapsed_ms_of(seconds)


@dataclass(frozen=True, slots=True, kw_only=True)
class LLMCallAccounting:
    """What one LLM call, made under one role, billed — and how long it took.

    The four provider counters are kept apart rather than summed into
    "input": cache creation is billed at 1.25x input and cache read at 0.1x
    (``llm/pricing.py``), so a breakdown that folds them together misprices
    every run that caches — which, since ``llm/client.py`` caches the system
    prompt, is every live run.

    ``tokens_used`` is the same volume the ledger charges (``token_volume``)
    and ``usd_used`` is ``cost_of``'s answer, never arithmetic repeated here.
    Both are per call, so the run total is a sum and the reconciliation below
    is an equality rather than an estimate.
    """

    role: str
    model: str
    #: Whether this role's calls reach ``BudgetLedger``. False for the
    #: EVALUATOR's own spend and nothing else: the briefing judge grades the
    #: run and is not part of it. Everything the agent itself buys is charged,
    #: the briefing writer included since WO-R3-260 — it runs after the
    #: terminal state, so it is metered and never gates (ADR 0015 § 4, as
    #: amended). That is why the reconciliation reads only the charged subset
    #: and the report still reports both.
    charged_to_ledger: bool = True
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    discarded_output_tokens: int = 0
    tokens_used: int = 0
    usd_used: Decimal = Decimal("0")
    elapsed_ms: int = 0
    #: The call was billed and then raised (a truncated response, an
    #: exhausted retry loop, an output the schema rejected). Recorded rather
    #: than dropped: a failure that costs nothing in the record is the
    #: under-report ADR 0015 exists to prevent.
    failed: bool = False

    @classmethod
    def of(
        cls,
        *,
        role: str,
        model: str,
        usage: LLMUsage,
        elapsed_ms: int,
        charged_to_ledger: bool = True,
        failed: bool = False,
    ) -> LLMCallAccounting:
        """Price and split one call's usage. The only constructor callers use."""
        return cls(
            role=role,
            model=model,
            charged_to_ledger=charged_to_ledger,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_creation_tokens=usage.cache_creation_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            discarded_output_tokens=usage.discarded_output_tokens,
            tokens_used=token_volume(usage),
            usd_used=cost_of(model, usage),
            elapsed_ms=elapsed_ms,
            failed=failed,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class RoleTotals:
    """One role's calls, added up. The unit plan 03 § 7.8 asks for."""

    role: str
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


@dataclass(frozen=True, slots=True, kw_only=True)
class StepAccounting:
    """The context and branching of one planner step, as the record reported it.

    Derived from ``StepRecord`` rather than measured again: the strategy is
    the only thing that can honestly say how many candidates it considered or
    what the planner was fed, and a second measurement taken here would be a
    different render of the context from the one the model actually saw.
    """

    iteration: int
    strategy: str
    #: The provider's count of the context fed to the planner this step. ``0``
    #: on a canned run, honestly: the fake client bills nothing.
    planner_input_tokens: int
    #: That same context measured locally, in characters — the measurement the
    #: offline suite *can* make (divergence D1). Characters are not tokens and
    #: are never reported as if they were.
    planner_context_chars: int
    #: How many diagnoses the strategy considered this step. ``baseline``
    #: considers one.
    candidates: int
    #: 1 when a ``candidate_selector`` decided between them, else 0.
    selector_calls: int


@dataclass(frozen=True, slots=True)
class MeteredLLMClient:
    """An ``LLMClientProtocol`` that records what each call billed, by role.

    A wrapper rather than a change to ``llm/client.py`` for two reasons. The
    role is not a fact the client knows — the eval runner is what decides that
    *this* client is the investigation planner and *that* one is the briefing
    judge — and the canned client has no tracer at all, so a split derived
    from trace records would exist for live runs and be empty for the entire
    offline suite. Wrapping covers both with one seam, and the seam sees every
    billed leg: a repaired call (ADR 0035) arrives here twice, once raising
    and once parsing, which is exactly how the ledger charges it.

    It changes nothing about the call. ``repair_of``, ``max_tokens`` and
    ``temperature`` are forwarded untouched, and an exception is re-raised after
    it is recorded — a metering wrapper that swallowed a failure would turn an
    escalation into a silent success.
    """

    inner: LLMClientProtocol
    role: str
    record: Callable[[LLMCallAccounting], None]
    charged_to_ledger: bool = True
    clock: Callable[[], float] = time.monotonic

    def call[T: BaseModel](
        self,
        system_prompt: str,
        user_message: str,
        output_model: type[T],
        model: str,
        max_tokens: int = 4096,
        *,
        repair_of: str | None = None,
        temperature: float | None = None,
    ) -> LLMResult[T]:
        started = self.clock()
        try:
            result = self.inner.call(
                system_prompt=system_prompt,
                user_message=user_message,
                output_model=output_model,
                model=model,
                max_tokens=max_tokens,
                repair_of=repair_of,
                temperature=temperature,
            )
        except Exception as err:
            # Only what the failure itself reports. ``LLMError`` carries the
            # usage of a call that was billed and then raised; a
            # ``ValidationError`` from a fake carries none, and charging a
            # guess there would be an over-report invented rather than
            # measured.
            usage = getattr(err, "usage", None)
            if isinstance(usage, LLMUsage):
                self._record(usage, model, started, failed=True)
            raise
        # ``LLMResult`` IS an ``LLMUsage``, so the happy path is priced by the
        # same arithmetic as the failing one.
        self._record(result, model, started)
        return result

    def _record(self, usage: LLMUsage, model: str, started: float, *, failed: bool = False) -> None:
        self.record(
            LLMCallAccounting.of(
                role=self.role,
                model=model,
                usage=usage,
                elapsed_ms=_elapsed_ms(self.clock() - started),
                charged_to_ledger=self.charged_to_ledger,
                failed=failed,
            )
        )


@dataclass
class RunAccounting:
    """Every billed LLM call of one run, plus every planner step's context.

    Mutable and per-run by design: it is the one place a run's cost is
    assembled, and the run's own edge (``evals/runner.py``) owns it. Nothing
    the agent can read — like ``StepRecord``, this is written *about* a run
    and never back into one, so no accounting change can move a trajectory.
    """

    calls: list[LLMCallAccounting] = field(default_factory=list)
    steps: list[StepAccounting] = field(default_factory=list)

    # --- collection -------------------------------------------------------

    def record_call(self, call: LLMCallAccounting) -> None:
        self.calls.append(call)

    def record_step(self, record: StepRecord) -> None:
        """Take one planner step's context and branching off its ``StepRecord``.

        ``None`` on either measurement means the strategy did not measure it;
        it is recorded as 0 rather than dropped, because a step missing from
        the per-step list would shorten the run and make the totals describe
        fewer steps than the run took.
        """
        self.steps.append(
            StepAccounting(
                iteration=record.iteration,
                strategy=record.strategy,
                planner_input_tokens=record.planner_input_tokens or 0,
                planner_context_chars=record.planner_context_chars or 0,
                candidates=len(record.candidate_set),
                selector_calls=0 if record.selector is None else 1,
            )
        )

    def meter(
        self,
        client: LLMClientProtocol,
        role: str,
        *,
        charged_to_ledger: bool = True,
        clock: Callable[[], float] = time.monotonic,
    ) -> MeteredLLMClient:
        """Wrap ``client`` so its calls land here under ``role``."""
        return MeteredLLMClient(
            client, role, self.record_call, charged_to_ledger=charged_to_ledger, clock=clock
        )

    def step_sink(self, forward: StepSink | None = None) -> StepSink:
        """A ``StepSink`` that accounts for each step and passes it on.

        Composed rather than exclusive: the trace store wants the whole
        ``StepRecord`` and this wants four numbers off it, and a run that had
        to choose would either lose the research record or lose the context
        column on every run without ``EVAL_TRACE_DIR`` — which is the whole
        offline suite.
        """

        def sink(record: StepRecord) -> None:
            self.record_step(record)
            if forward is not None:
                forward(record)

        return sink

    # --- totals -----------------------------------------------------------

    @property
    def roles(self) -> tuple[RoleTotals, ...]:
        """Per-role totals, in the order each role first billed something."""
        order: list[str] = []
        grouped: dict[str, list[LLMCallAccounting]] = {}
        for call in self.calls:
            if call.role not in grouped:
                order.append(call.role)
                grouped[call.role] = []
            grouped[call.role].append(call)
        return tuple(self._totals(role, grouped[role]) for role in order)

    @staticmethod
    def _totals(role: str, calls: list[LLMCallAccounting]) -> RoleTotals:
        return RoleTotals(
            role=role,
            charged_to_ledger=calls[0].charged_to_ledger,
            calls=len(calls),
            input_tokens=sum(call.input_tokens for call in calls),
            output_tokens=sum(call.output_tokens for call in calls),
            cache_creation_tokens=sum(call.cache_creation_tokens for call in calls),
            cache_read_tokens=sum(call.cache_read_tokens for call in calls),
            discarded_output_tokens=sum(call.discarded_output_tokens for call in calls),
            tokens_used=sum(call.tokens_used for call in calls),
            usd_used=sum((call.usd_used for call in calls), Decimal("0")),
            elapsed_ms=sum(call.elapsed_ms for call in calls),
        )

    @property
    def llm_calls(self) -> int:
        return len(self.calls)

    @property
    def tokens_used(self) -> int:
        """Every role's volume, the evaluator's own included."""
        return sum(call.tokens_used for call in self.calls)

    @property
    def usd_used(self) -> Decimal:
        return sum((call.usd_used for call in self.calls), Decimal("0"))

    @property
    def charged_tokens_used(self) -> int:
        """Only what the run's own ledger was charged for."""
        return sum(call.tokens_used for call in self.calls if call.charged_to_ledger)

    @property
    def charged_usd_used(self) -> Decimal:
        return sum((call.usd_used for call in self.calls if call.charged_to_ledger), Decimal("0"))

    @property
    def elapsed_ms(self) -> int:
        """Time spent inside LLM calls. Not the run's wall clock — the loop
        also probes, sleeps out a freshness window and grades."""
        return sum(call.elapsed_ms for call in self.calls)

    @property
    def selector_calls(self) -> int:
        """0 for ``baseline``: with one candidate there is nothing to select."""
        return sum(step.selector_calls for step in self.steps)

    @property
    def branch_count(self) -> int:
        """Candidates considered *beyond* the one that was emitted.

        0 for ``baseline`` by construction — one call, one ranking, nothing
        enumerated behind it — which is the number a best-of-N arm is
        compared against. Counting the emitted candidate too would make the
        control group's branch count equal its step count and the comparison
        would read as "baseline branches as much as best-of-2".
        """
        return sum(max(step.candidates - 1, 0) for step in self.steps)

    @property
    def planner_input_tokens(self) -> tuple[int, ...]:
        """Per step, in order (plan 02 § 17)."""
        return tuple(step.planner_input_tokens for step in self.steps)

    @property
    def planner_context_chars(self) -> tuple[int, ...]:
        return tuple(step.planner_context_chars for step in self.steps)

    def reconciles_with(self, budget: BudgetLedger) -> bool:
        """Does the charged split add up to what the ledger charged?

        The property that makes the breakdown usable as evidence. It is an
        equality, not a tolerance, because both sides are built from the same
        ``LLMUsage`` objects by the same two functions.

        One path can make it false without anything being wrong, and it is
        worth naming rather than hiding behind a tolerance: when a structured
        call exhausts its repair (ADR 0035), the caller charges the ledger the
        *summed* usage of every failed leg in one go, and ``repair.sum_usage``
        carries ``discarded_max_tokens`` as a maximum rather than a sum. If two
        legs of one call requested different output caps, the ledger's single
        conservative charge is larger than the per-leg sum here. That is
        ADR 0015's direction — the ledger over-reports, never under — and the
        report records both numbers so a reader sees which way it went instead
        of being told everything agreed.
        """
        return (
            self.charged_tokens_used == budget.tokens_used
            and self.charged_usd_used == budget.usd_used
        )
