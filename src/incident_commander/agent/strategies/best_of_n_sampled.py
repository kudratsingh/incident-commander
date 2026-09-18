"""``best_of_n_sampled`` — N independent planner calls at a temperature (WP-5.3).

Plan 02 § 11.2, and the literature's pass@k: N independent draws from one context, the candidate
set being the union of their top hypotheses deduplicated by ``(category, name)``. N× planner
cost, which is why 03 § 8 gates it to the final matrix. It measures the *spread of the model's
own distribution*, where ``best_of_n_enumerated`` measures enumeration (decision D4). The
emitted step is an ordinary one, so the ``FIX_MAP`` gate, the 0.7 threshold, ADR 0041's refusal
and the ADR-0009 re-probe all run on it unchanged in ``investigation.py`` (plan 02 § 18).

Three decisions. **The emitted step is one sample's, verbatim — never a blend:** the most
confident sample's ranking *and* its ``next_action``, ties to the earliest draw, because a blend
would emit a step no model proposed (ADR 0045). **Every call accrues, including before a
failure:** the loop charges ``accrue_llm_error`` against the state held *before*
``plan_next_step``, so a failure is re-raised as ``SampledPlannerFailed`` carrying every billed
leg's summed usage (ADR 0015). **The union is by ``(category, name)``** — no case-folding, no
whitespace collapsing, the rule ``agent/candidates.py`` applies inside one set, because ``name``
is operator-facing free text and the duplicate rate must measure what the model produced.

Cost is **declared, not discovered** (decision C12): the ledger is seeded by
``TOKEN_BUDGET_MULTIPLIER`` / ``USD_BUDGET_MULTIPLIER``, and an arm metered against a one-call
ceiling reads as the strategy failing (decision C4). 03 § 11 prices ``sampled-8`` at 6–8×
planner cost against a blended ~2.5, and divergence J3 notes the disagreement; measuring it
needs a run, so compare the ``investigation_planner`` token total (WP-2.3) with a ``baseline``
run, to ±20%. ``settings.sample_temperature`` (default 1.0) reaches the client per call and
``llm/client.py`` sends the field **only** when given, so no other role's bytes move; newer
models reject it (``llm/client.SAMPLING_REJECTED_MODELS``).
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

#: Same role as every other planner call, so a per-role cost breakdown can
#: compare this arm with ``baseline`` instead of splitting them apart.
_PLANNER_ROLE: Final[str] = "investigation_planner"

#: The temperature an arm built without knobs samples at (plan 02 § 11.2's default). The
#: configured value is ``Settings.sample_temperature``.
DEFAULT_SAMPLE_TEMPERATURE: Final[float] = 1.0


class SampledPlannerFailed(LLMError):
    """One of the N samples failed, and the step's whole bill comes with it.

    An ``LLMError`` subclass so the loop's existing ``except`` arm escalates as for
    ``baseline``. ``usage`` sums every billed leg, ADR-0035 repairs included, charged once by
    ``accrue_llm_error``; without it the k−1 completed samples would be charged to nobody
    (ADR 0015, ADR 0045).
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
        """Build the arm from its inference block.

        ``knobs=None`` gives N=1 at the default temperature; 8 would spend 8× as much.
        """
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
                # A float in a provenance record is a float a reader has to trust
                # the JSON round-trip of; the string is the value as configured.
                "sample_temperature": str(Decimal(str(self._temperature))),
                # False, stated rather than omitted: this arm's schema has no ``EvidenceRef``,
                # so it is NOT comparable with the enumerated arm here (ADR 0044).
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
        """This arm running alone: draw, record, emit the best sample's step.

        Expressed via ``generate`` (WP-6.2): one draw path.
        """
        generation = self.generate(run_state, at, ctx)
        if ctx.record_step is not None:
            ctx.record_step(generation.record)
        return generation.run_state, generation.proposed_step, generation.record

    def generate(
        self, run_state: RunState, at: datetime, ctx: StrategyContext
    ) -> CandidateGeneration:
        """Draw N samples, propose the best one's step, record the union.

        The context is rendered **once** and reused, which makes these samples of one
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

        Sequential, deliberately: the loop is a synchronous state machine (ADR 0002), and a
        thread pool here would need the accrual, the tracer and the ledger to be thread-safe.
        The ~8× latency at ``sampled-8`` is reported on the ``StepRecord``.
        """
        drawn: list[RepairedCall[InvestigationStep]] = []
        for sample in range(1, self._n + 1):
            try:
                drawn.append(
                    call_with_output_repair(
                        ctx.llm_client,
                        system_prompt=self._system_prompt,
                        user_message=user_message,
                        output_model=InvestigationStep,
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

        One ``LLMCallRecord`` per sample, so ``llm_calls`` has length N. The ledger delta is on
        the FIRST record and zero on the rest: repeating a per-step quantity would multiply the
        step's charge by N.
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
            # No selector: this arm draws, and selects no further than the most confident draw
            # (plan 02 § 12).
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
            # Rendered once and sent N times, so the step's context cost is N × the string:
            # ``planner_context_chars`` is what each arm paid for context, not one render.
            planner_context_chars=self._n * (len(self._system_prompt) + len(user_message)),
            # Rejections are per sample, in sample order.
            generation_rejections=tuple(
                f"sample_{index + 1}_repaired"
                for index, call in enumerate(calls)
                if call.was_repaired
            ),
        )


def _billed_so_far(
    drawn: Sequence[RepairedCall[InvestigationStep]], failure: BaseException
) -> LLMUsage | None:
    """Everything this step has billed: the completed samples and the failure.

    ``None`` only when nothing reported a usage, where a guess would be invented.
    """
    billed: list[LLMUsage | None] = []
    for call in drawn:
        billed.extend(usage_of(err) for err in call.failures)
        billed.append(call.result)
    billed.append(usage_of(failure))
    return sum_usage(*billed)


def _best_sample(
    calls: Sequence[RepairedCall[InvestigationStep]],
) -> RepairedCall[InvestigationStep]:
    """The sample whose top hypothesis is the most confident.

    Ties go to the earlier draw. Index 0 is each sample's top hypothesis because
    ``InvestigationStep`` re-sorts by confidence (B-07).
    """
    return max(
        calls,
        key=lambda call: (call.result.output.hypotheses[0].confidence, -calls.index(call)),
    )


def _union_of_tops(
    calls: Sequence[RepairedCall[InvestigationStep]],
) -> tuple[tuple[DiagnosisCandidate, str], ...]:
    """The samples' top hypotheses, deduplicated by ``(category, name)``.

    First occurrence wins, so a later agreeing sample is evidence *of* the candidate, not a
    second one. Returned confidence-descending, so index 0 is the emitted diagnosis, each
    paired with the trace-record id of the call that drew it. **``DiagnosisCandidate``
    rather than ``CandidateRecord`` since WP-6.2**, because a selector needs the whole
    ``next_probe`` and a record keeps only the tool name; ``generation.candidate_record_of``
    still builds the record from these. Constructed rather than ``model_validate``d: a sampled
    candidate cites nothing, so no ledger binding is needed here and none is faked.
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
