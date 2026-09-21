"""Charge LLM calls to the incident budget ledger (ADR 0015), and split the same billed
work by role, token class, call and planner step (WP-2.3).

Here rather than in ``state.py``, which stays free of ``llm`` imports. Both views come from
the same ``LLMUsage`` objects; ``RunAccounting.reconciles_with`` asserts they still agree.
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
    """ADR 0015's token *volume* for one call: every class, discards included. One
    definition for both consumers, so the split cannot drift from the total."""
    return (
        usage.input_tokens
        + usage.output_tokens
        + usage.cache_creation_tokens
        + usage.cache_read_tokens
        + usage.discarded_output_tokens
    )


def accrue_llm_usage(budget: BudgetLedger, usage: LLMUsage, model: str) -> BudgetLedger:
    """Return ``budget`` with this call's tokens and dollars added.

    ``tokens_used`` counts every token class, cache and discards included: summing
    only input+output under-enforced ``BUDGET_MAX_TOKENS`` when caching worked (C-06).
    """
    return budget.model_copy(
        update={
            "tokens_used": budget.tokens_used + token_volume(usage),
            "usd_used": budget.usd_used + cost_of(model, usage),
        }
    )


def accrue_llm_error(budget: BudgetLedger, err: Exception, model: str) -> BudgetLedger:
    """Charge whatever a *failed* LLM call already billed. A truncated response or an
    exhausted retry is billed work; anything without ``LLMError.usage`` is a no-op."""
    if not isinstance(err, LLMError) or err.usage is None:
        return budget
    return accrue_llm_usage(budget, err.usage, model)


def accrue_structured_call(
    budget: BudgetLedger, call: RepairedCall[Any], model: str
) -> BudgetLedger:
    """Charge a possibly-repaired structured-output call: every leg of it.

    A repair is one more billed call (ADR 0035); ADR 0015 charges it whatever it produced.
    """
    for failure in call.failures:
        budget = accrue_llm_error(budget, failure, model)
    return accrue_llm_usage(budget, call.result, model)


def _elapsed_ms(seconds: float) -> int:
    """Whole milliseconds, never negative — ``0`` is a measurement, not a gap.

    ``llm/client.py``'s definition, reused so the two latency columns cannot disagree.
    """
    return elapsed_ms_of(seconds)


@dataclass(frozen=True, slots=True, kw_only=True)
class LLMCallAccounting:
    """What one LLM call, made under one role, billed — and how long it took.

    The provider counters stay apart because cache creation and cache read are priced
    differently (``llm/pricing.py``).
    """

    role: str
    model: str
    #: Whether this role's calls are charged to the run's budget. False only for the evaluator's
    #: own spending, which is not the agent's; the briefing writer IS charged.
    charged_to_ledger: bool = True
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    discarded_output_tokens: int = 0
    tokens_used: int = 0
    usd_used: Decimal = Decimal("0")
    elapsed_ms: int = 0
    #: The provider billed this call and it then raised. Recorded rather than dropped: treating
    #: a failure as free is exactly the under-reporting the budget rules exist to prevent.
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
    """Every call one role made, added up: the per-role line a cost report shows."""

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

    Derived from ``StepRecord``, never measured again: only the strategy knows what the
    planner was fed.
    """

    iteration: int
    strategy: str
    #: How many tokens of context the planner was fed this step, as the provider counted them.
    #: Honestly 0 on an offline run, where the fake client bills nothing.
    planner_input_tokens: int
    #: The same context measured in characters, so an offline run still has a real number.
    #: Characters are not tokens and the two are not interchangeable.
    planner_context_chars: int
    #: How many competing diagnoses the strategy considered this step. The control group
    #: considers one.
    candidates: int
    #: 1 where an extra call chose between those diagnoses this step, otherwise 0.
    selector_calls: int
    #: 1 where a critic call reviewed this step, otherwise 0.
    critic_calls: int = 0
    #: 1 where the critique led to a second planner call, otherwise 0. Kept apart from the
    #: count above, because a critique that cost tokens and changed nothing is its own case.
    revised: int = 0
    #: How many branches an exploration took this step, and how many reads the whole walk made,
    #: the chosen path's included: all of them came out of one shared ceiling.
    search_branches: int = 0
    search_branch_tool_calls: int = 0


@dataclass(frozen=True, slots=True)
class MeteredLLMClient:
    """An ``LLMClientProtocol`` that records what each call billed, by role.

    A wrapper because the role is the runner's fact, not the client's. Every billed leg
    arrives here — a repaired call (ADR 0035) twice — and an exception is re-raised.
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
            # Charge only what the failure itself reports it was billed. Estimating here would
            # be an invented number rather than a measured one.
            usage = getattr(err, "usage", None)
            if isinstance(usage, LLMUsage):
                self._record(usage, model, started, failed=True)
            raise
        # A successful result carries the same usage fields as a failure, so both paths are
        # priced by exactly the same arithmetic.
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

    Mutable and per-run; ``evals/runner.py`` owns it. Written *about* a run, never
    back into one.
    """

    calls: list[LLMCallAccounting] = field(default_factory=list)
    steps: list[StepAccounting] = field(default_factory=list)

    # --- recording one call or one step -----------------------------------

    def record_call(self, call: LLMCallAccounting) -> None:
        self.calls.append(call)

    def record_step(self, record: StepRecord) -> None:
        """Take one planner step's context and branching off its ``StepRecord``.

        An unmeasured value records 0, never drops the step.
        """
        self.steps.append(
            StepAccounting(
                iteration=record.iteration,
                strategy=record.strategy,
                planner_input_tokens=record.planner_input_tokens or 0,
                planner_context_chars=record.planner_context_chars or 0,
                candidates=len(record.candidate_set),
                selector_calls=0 if record.selector is None else 1,
                critic_calls=0 if record.revision is None else 1,
                revised=1 if record.revision is not None and record.revision.revised else 0,
                search_branches=0 if record.search is None else record.search.branches_taken,
                search_branch_tool_calls=(
                    0
                    if record.search is None
                    else sum(node.tool_calls_used for node in record.search.nodes)
                ),
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
        """A ``StepSink`` that accounts for each step and passes it on — composed, because
        the trace store wants the whole ``StepRecord`` and this wants four numbers off it."""

        def sink(record: StepRecord) -> None:
            self.record_step(record)
            if forward is not None:
                forward(record)

        return sink

    # --- adding it all up, per role and per run ---------------------------

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
    def critic_calls(self) -> int:
        """0 for every arm but ``reflection``: nothing else critiques its own step."""
        return sum(step.critic_calls for step in self.steps)

    @property
    def revised_steps(self) -> int:
        """Steps whose critique was acted on. Below ``critic_calls`` by the kept ones."""
        return sum(step.revised for step in self.steps)

    @property
    def search_branches(self) -> int:
        """How many exploration branches the run took; 0 for every arm but ``search``."""
        return sum(step.search_branches for step in self.steps)

    @property
    def search_branch_tool_calls(self) -> int:
        """Reads the walks made, against the one ceiling the whole run spends from."""
        return sum(step.search_branch_tool_calls for step in self.steps)

    @property
    def branch_count(self) -> int:
        """Candidates considered *beyond* the one that was emitted.

        0 for ``baseline`` — the control group a best-of-N arm is compared against.
        """
        return sum(max(step.candidates - 1, 0) for step in self.steps)

    @property
    def planner_input_tokens(self) -> tuple[int, ...]:
        """How much context each planner step was fed, in the order the steps ran."""
        return tuple(step.planner_input_tokens for step in self.steps)

    @property
    def planner_context_chars(self) -> tuple[int, ...]:
        return tuple(step.planner_context_chars for step in self.steps)

    def reconciles_with(self, budget: BudgetLedger) -> bool:
        """Does the charged split add up to what the ledger charged?

        An equality, not a tolerance: both sides come from the same ``LLMUsage`` objects.
        An exhausted repair (ADR 0035) can make it false honestly, by over-reporting.
        """
        return (
            self.charged_tokens_used == budget.tokens_used
            and self.charged_usd_used == budget.usd_used
        )
