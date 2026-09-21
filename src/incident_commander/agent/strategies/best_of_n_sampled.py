"""``best_of_n_sampled`` — N independent planner calls at a temperature (plan 02 § 11.2, WP-5.3).

Pass@k: N draws from one context, the set being the union of their top hypotheses by
``(category, name)`` with no folding. The emitted step is ONE sample's, verbatim — the most
confident, ties to the earliest — because a blend would emit a step no model proposed (ADR 0045).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Final

from incident_commander.agent.accounting import accrue_structured_call
from incident_commander.agent.candidates import DiagnosisCandidate
from incident_commander.agent.hypothesis import (
    Hypothesis,
    HypothesisCategory,
    InvestigationStep,
    ProbeAction,
)
from incident_commander.agent.planner_context import format_planner_context
from incident_commander.agent.state import RunState
from incident_commander.agent.strategies.generation import (
    CandidateGeneration,
    billed_usage_of,
    candidate_record_of,
)
from incident_commander.agent.strategies.knobs import StrategyKnobs
from incident_commander.agent.strategies.names import StrategyName
from incident_commander.agent.strategies.protocol import StrategyContext
from incident_commander.agent.strategies.records import (
    LLMCallRecord,
    StepRecord,
)
from incident_commander.llm.client import LLMError, LLMUsage
from incident_commander.llm.prompts.loader import load_prompt
from incident_commander.llm.repair import (
    RepairedCall,
    call_with_output_repair,
    sum_usage,
    usage_of,
)

#: This arm's calls are labelled with the same role as every other planner call, so a per-role
#: cost breakdown can compare it with ``baseline`` rather than splitting the two apart.
_PLANNER_ROLE: Final[str] = "investigation_planner"

#: The sampling temperature used when this arm is built with no settings of its own; a run
#: normally takes the configured ``Settings.sample_temperature`` instead.
DEFAULT_SAMPLE_TEMPERATURE: Final[float] = 1.0


class SampledPlannerFailed(LLMError):
    """One of the N samples failed, and the step's whole bill comes with it.

    An ``LLMError`` so the loop's existing ``except`` arm escalates as for ``baseline``.
    ``usage`` sums every billed leg; without it the k−1 completed samples go uncharged.
    """

    def __init__(self, sample: int, of: int, cause: BaseException, usage: LLMUsage | None) -> None:
        super().__init__(
            f"sampled planner call {sample} of {of} failed: {cause}",
            usage=usage,
            record_id=getattr(cause, "record_id", None),
        )
        self.sample = sample
        self.of = of
        self.cause = cause


class BestOfNSampledStrategy:
    """N independent planner calls per step; the best sample's step is emitted."""

    name: str = StrategyName.BEST_OF_N_SAMPLED.value

    def __init__(self, knobs: StrategyKnobs | None = None) -> None:
        """Build the arm from its inference block. ``knobs=None`` gives N=1 at the default
        temperature; 8 would spend 8× as much."""
        resolved = knobs if knobs is not None else StrategyKnobs()
        self._n: Final[int] = resolved.n
        self._temperature: Final[float] = (
            DEFAULT_SAMPLE_TEMPERATURE
            if resolved.sample_temperature is None
            else resolved.sample_temperature
        )
        self._system_prompt: Final[str] = load_prompt("investigation_planner")
        self.config: Mapping[str, Any] = MappingProxyType(
            {
                "n": resolved.n,
                # Stored as a string so a reader of the record need not trust a float
                # surviving a JSON round trip unchanged.
                "sample_temperature": str(Decimal(str(self._temperature))),
                # Written out rather than left absent: this arm's schema has no place to cite
                # evidence, so its candidates are not comparable with the enumerated arm's.
                "evidence_ids_rendered": False,
            }
        )

    @property
    def n(self) -> int:
        """How many independent samples this arm draws per step."""
        return self._n

    @property
    def temperature(self) -> float:
        """The temperature every one of those samples is drawn at."""
        return self._temperature

    def plan_next_step(
        self, run_state: RunState, at: datetime, ctx: StrategyContext
    ) -> tuple[RunState, InvestigationStep, StepRecord]:
        """This arm running alone: draw, record, emit the best sample's step. Expressed via
        ``generate`` (WP-6.2), so there is one draw path."""
        generation = self.generate(run_state, at, ctx)
        if ctx.record_step is not None:
            ctx.record_step(generation.record)
        return generation.run_state, generation.proposed_step, generation.record

    def generate(
        self, run_state: RunState, at: datetime, ctx: StrategyContext
    ) -> CandidateGeneration:
        """Draw N samples, propose the best one's step, record the union.

        The context is rendered ONCE and reused — that is what makes these samples of one
        distribution; later draws pay the cache-read rate for it.
        """
        user_message = format_planner_context(run_state)
        calls = self._draw(user_message, ctx)
        winner = _best_sample(calls)
        step = winner.result.output
        budget = run_state.budget
        for call in calls:
            budget = accrue_structured_call(budget, call, ctx.model)
        updated = run_state.model_copy(
            update={
                "budget": budget,
                "hypotheses": step.hypotheses,
                "updated_at": at,
            }
        )
        union = _union_of_tops(calls)
        return CandidateGeneration(
            run_state=updated,
            candidates=tuple(candidate for candidate, _ in union),
            proposed_step=step,
            record=self._record(run_state, updated, step, calls, union, ctx, user_message),
            billed_usage=billed_usage_of(calls),
        )

    def _draw(
        self, user_message: str, ctx: StrategyContext
    ) -> tuple[RepairedCall[InvestigationStep], ...]:
        """N independent calls, or a failure carrying the step's whole bill.

        Sequential deliberately: the loop is a synchronous state machine (ADR 0002), and a
        thread pool would need the accrual, the tracer and the ledger to be thread-safe.
        """
        drawn: list[RepairedCall[InvestigationStep]] = []
        for sample in range(1, self._n + 1):
            try:
                drawn.append(
                    call_with_output_repair(
                        ctx.llm_client,
                        system_prompt=self._system_prompt,
                        user_message=user_message,
                        output_model=ctx.step_model(InvestigationStep),
                        model=ctx.model,
                        temperature=self._temperature,
                    )
                )
            except Exception as err:
                raise SampledPlannerFailed(
                    sample, self._n, err, _billed_so_far(drawn, err)
                ) from err
        return tuple(drawn)

    def _record(
        self,
        before: RunState,
        after: RunState,
        step: InvestigationStep,
        calls: Sequence[RepairedCall[InvestigationStep]],
        union: Sequence[tuple[DiagnosisCandidate, str]],
        ctx: StrategyContext,
        user_message: str,
    ) -> StepRecord:
        """The deduplicated union of the samples' top hypotheses.

        One ``LLMCallRecord`` per sample. The ledger delta sits on the FIRST record and is zero
        on the rest: repeating a per-step quantity would multiply the step's charge by N.
        """
        candidates = tuple(
            candidate_record_of(candidate, generation_call_id=call_id)
            for candidate, call_id in union
        )
        delta_tokens = after.budget.tokens_used - before.budget.tokens_used
        delta_usd = after.budget.usd_used - before.budget.usd_used
        return StepRecord(
            run_id=str(before.incident_id),
            iteration=ctx.iteration,
            strategy=self.name,
            model=ctx.model,
            candidate_set=candidates,
            # No selection was made: this arm takes several independent samples and goes no
            # further than picking the most confident of them.
            selector=None,
            emitted_step=step,
            hypothesis_state_before=before.hypotheses,
            hypothesis_state_after=after.hypotheses,
            llm_calls=tuple(
                LLMCallRecord(
                    role=_PLANNER_ROLE,
                    model=ctx.model,
                    tokens_used=delta_tokens if index == 0 else 0,
                    usd_used=delta_usd if index == 0 else Decimal("0"),
                    input_tokens=call.result.input_tokens,
                    output_tokens=call.result.output_tokens,
                    cache_read_tokens=call.result.cache_read_tokens,
                    cache_creation_tokens=call.result.cache_creation_tokens,
                    call_id=call.result.record_id,
                    elapsed_ms=call.result.elapsed_ms,
                )
                for index, call in enumerate(calls)
            ),
            planner_input_tokens=sum(
                call.result.input_tokens
                + call.result.cache_read_tokens
                + call.result.cache_creation_tokens
                for call in calls
            ),
            # The context is built once but sent with all N calls, so what the arm paid for
            # context is that length multiplied by N.
            planner_context_chars=self._n * (len(self._system_prompt) + len(user_message)),
            # One entry per sample that had to be re-asked, in the order the samples were taken.
            generation_rejections=tuple(
                f"sample_{index + 1}_repaired"
                for index, call in enumerate(calls)
                if call.was_repaired
            ),
        )


def _billed_so_far(
    drawn: Sequence[RepairedCall[InvestigationStep]], failure: BaseException
) -> LLMUsage | None:
    """Everything this step has billed: the completed samples and the failure. ``None`` only
    when nothing reported a usage, where a guess would be invented."""
    billed: list[LLMUsage | None] = []
    for call in drawn:
        billed.extend(usage_of(err) for err in call.failures)
        billed.append(call.result)
    billed.append(usage_of(failure))
    return sum_usage(*billed)


def _best_sample(
    calls: Sequence[RepairedCall[InvestigationStep]],
) -> RepairedCall[InvestigationStep]:
    """The sample whose top hypothesis is the most confident; ties go to the earlier draw.
    Index 0 is each sample's top because ``InvestigationStep`` re-sorts by confidence (B-07)."""
    return max(
        calls,
        key=lambda call: (call.result.output.hypotheses[0].confidence, -calls.index(call)),
    )


def _union_of_tops(
    calls: Sequence[RepairedCall[InvestigationStep]],
) -> tuple[tuple[DiagnosisCandidate, str], ...]:
    """The samples' top hypotheses, deduplicated by ``(category, name)``.

    First occurrence wins, so a later agreeing sample is evidence *of* the candidate. Returned
    confidence-descending, each paired with the trace-record id of the call that drew it.
    Constructed, not ``model_validate``d: a sampled candidate cites nothing, so nothing is faked.
    """
    seen: dict[tuple[HypothesisCategory, str], tuple[DiagnosisCandidate, str]] = {}
    for index, call in enumerate(calls):
        top: Hypothesis = call.result.output.hypotheses[0]
        key = (top.category, top.name)
        if key in seen:
            continue
        action = call.result.output.next_action
        seen[key] = (
            DiagnosisCandidate(
                candidate_id=f"sample-{index + 1}",
                category=top.category,
                name=top.name,
                confidence=top.confidence,
                next_probe=action if isinstance(action, ProbeAction) else None,
            ),
            call.result.record_id,
        )
    return tuple(sorted(seen.values(), key=lambda entry: entry[0].confidence, reverse=True))
