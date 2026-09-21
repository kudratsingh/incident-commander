"""``candidate_selector`` — a generator arm plus a selection (plan 02 § 12, WP-6.2).

Selection is not authorization: the selector chooses among DIAGNOSES and every gate in
``investigation.py`` still runs. A ``select`` emits the selected candidate and ONLY it, since the
schema re-sorts by confidence. Its own metered role; a failure re-raises ``SelectorFailed``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from types import MappingProxyType
from typing import Any, Final

from incident_commander.agent.accounting import accrue_structured_call
from incident_commander.agent.candidates import DiagnosisCandidate
from incident_commander.agent.hypothesis import (
    Hypothesis,
    InvestigationStep,
    NextAction,
    StopAction,
)
from incident_commander.agent.selection import (
    SELECTOR_ROLE,
    SelectionDecision,
    SelectionResult,
    select_candidate,
)
from incident_commander.agent.state import RunState
from incident_commander.agent.strategies.generation import (
    CandidateGeneration,
    CandidateGenerator,
)
from incident_commander.agent.strategies.knobs import StrategyKnobs
from incident_commander.agent.strategies.names import StrategyName
from incident_commander.agent.strategies.protocol import StrategyContext
from incident_commander.agent.strategies.records import (
    LLMCallRecord,
    SelectorRecord,
    StepRecord,
)
from incident_commander.llm.client import LLMError, LLMUsage
from incident_commander.llm.repair import RepairedCall, sum_usage, usage_of

#: The message this arm refuses with when a step is planned without a separate selector client.
#: Named so a test can assert the reason rather than merely that something raised.
NO_SELECTOR_CLIENT: Final[str] = "no selector client is on the strategy context"

#: The reason the run stops when the selector asks for another read but the candidate it points
#: at proposes none. One constant, because the grader, the briefing and a test all read it.
PROBE_MORE_WITHOUT_A_PROBE: Final[str] = (
    "selector asked for another probe and the candidate it points at proposes none"
)


class SelectorFailed(LLMError):
    """The selector call failed, and the step's whole bill comes with it.

    An ``LLMError`` so the loop's existing ``except`` arm escalates as for ``baseline``;
    ``usage`` sums every billed leg, generation included (ADR 0045).
    """

    def __init__(self, generator: str, cause: BaseException, usage: LLMUsage | None) -> None:
        super().__init__(
            f"candidate_selector failed over a {generator} set: {cause}",
            usage=usage,
            record_id=getattr(cause, "record_id", None),
        )
        self.generator = generator
        self.cause = cause


class CandidateSelectorStrategy:
    """One generator arm, then one selection over the set it produced."""

    name: str = StrategyName.CANDIDATE_SELECTOR.value

    def __init__(self, knobs: StrategyKnobs | None = None) -> None:
        """Build the arm from its inference block.

        The generator comes from the registry and is refused here if it cannot ``generate``.
        The import is local because the registry imports this module: at module level it cycles.
        """
        from incident_commander.agent.strategies.registry import STRATEGIES

        resolved = knobs if knobs is not None else StrategyKnobs()
        built = STRATEGIES.create(resolved.selector_generator, resolved)
        if not isinstance(built, CandidateGenerator):
            raise ValueError(
                f"strategy {resolved.selector_generator!r} cannot supply a candidate "
                "set: a candidate_selector composes over a generator "
                "(best_of_n_enumerated or best_of_n_sampled), and `baseline` "
                "considers one diagnosis, so there is nothing to select between."
            )
        self._generator: Final[CandidateGenerator] = built
        self.config: Mapping[str, Any] = MappingProxyType(
            {
                # All three parts of this arm's identity, with the generator's own settings
                # merged in rather than copied, so a report row shows what actually ran.
                **dict(built.config),
                "generator": built.name,
                "selector": SELECTOR_ROLE,
            }
        )

    @property
    def generator(self) -> CandidateGenerator:
        """The arm that supplies the candidate set."""
        return self._generator

    def plan_next_step(
        self, run_state: RunState, at: datetime, ctx: StrategyContext
    ) -> tuple[RunState, InvestigationStep, StepRecord]:
        """Generate the set, select from it, emit the selection's step. Order matters: the
        generation is charged before the selector is asked."""
        # 1. Refuse unless a separate client is available for the selector: sharing the
        #    planner's client would report selection's tokens as generation's.
        if ctx.selector_llm_client is None:
            raise ValueError(
                f"{NO_SELECTOR_CLIENT}. The selector is its own metered role "
                "(agent/selection.SELECTOR_ROLE); falling back to the planner's "
                "client would report selection's tokens under generation's role."
            )
        # 2. Ask the generator for the candidate set, which charges its call to the budget
        #    before the selector is asked anything.
        generation = self._generator.generate(run_state, at, ctx)
        # 3. Ask the selector to choose one candidate. If that call fails, the error carries
        #    the generation's bill as well, so the earlier call is still charged.
        try:
            call = select_candidate(
                ctx.selector_llm_client,
                run_state=generation.run_state,
                candidates=generation.candidates,
                model=ctx.model,
            )
        except Exception as err:
            raise SelectorFailed(
                self._generator.name,
                err,
                sum_usage(generation.billed_usage, usage_of(err)),
            ) from err
        # 4. Turn the selector's decision into the step the loop will run, and charge the
        #    selector's own call to the budget.
        decision = call.result.output
        step = self._step_for(decision, generation)
        updated = generation.run_state.model_copy(
            update={
                # The same charging function every other call goes through, so a selector
                # call that had to be re-asked bills both attempts.
                "budget": accrue_structured_call(generation.run_state.budget, call, ctx.model),
                "hypotheses": step.hypotheses,
                "updated_at": at,
            }
        )
        record = self._record(generation, decision, call, step, updated, ctx)
        if ctx.record_step is not None:
            ctx.record_step(record)
        return updated, step, record

    def _step_for(
        self, decision: SelectionResult, generation: CandidateGeneration
    ) -> InvestigationStep:
        """The decision, as the step the loop runs."""
        return step_for_selection(
            decision,
            generation.candidates,
            committed_action=generation.proposed_step.next_action,
        )

    def _record(
        self,
        generation: CandidateGeneration,
        decision: SelectionResult,
        call: RepairedCall[SelectionResult],
        step: InvestigationStep,
        updated: RunState,
        ctx: StrategyContext,
    ) -> StepRecord:
        """The generator's record, rebuilt with the selector block.

        ``dataclasses.replace`` rather than a second assembly, which would be two definitions of
        ``planner_context_chars``. The candidate set stays as the generator recorded it, so
        ``pass@k`` and ``selected@k`` come from one record.
        """
        before = generation.run_state
        return replace(
            generation.record,
            strategy=self.name,
            emitted_step=step,
            hypothesis_state_after=step.hypotheses,
            selector=SelectorRecord(
                selected_candidate_id=decision.selected_candidate_id,
                scores=dict(decision.scores),
                uncertainty=decision.uncertainty,
                decision=decision.decision.value,
                call_id=call.result.record_id,
            ),
            llm_calls=(
                *generation.record.llm_calls,
                LLMCallRecord(
                    role=SELECTOR_ROLE,
                    model=ctx.model,
                    tokens_used=updated.budget.tokens_used - before.budget.tokens_used,
                    usd_used=updated.budget.usd_used - before.budget.usd_used,
                    input_tokens=call.result.input_tokens,
                    output_tokens=call.result.output_tokens,
                    cache_read_tokens=call.result.cache_read_tokens,
                    cache_creation_tokens=call.result.cache_creation_tokens,
                    call_id=call.result.record_id,
                    elapsed_ms=call.result.elapsed_ms,
                ),
            ),
        )


def chosen_candidate(
    decision: SelectionResult, candidates: Sequence[DiagnosisCandidate]
) -> DiagnosisCandidate | None:
    """The candidate the decision points at. ``None`` is ``escalate`` or empty ``scores``."""
    chosen_id = decision.chosen_candidate_id
    if chosen_id is None:
        return None
    return next(candidate for candidate in candidates if candidate.candidate_id == chosen_id)


def step_for_selection(
    decision: SelectionResult,
    candidates: Sequence[DiagnosisCandidate],
    *,
    committed_action: NextAction,
) -> InvestigationStep:
    """One selection, as the step the loop runs (``SelectionDecision`` is closed).

    Shared at module level so ``search``'s chosen path hands the loop the same shape (WP-12.1);
    ``committed_action`` is what a ``select`` acts on.
    """
    chosen = chosen_candidate(decision, candidates)
    if decision.decision is SelectionDecision.ESCALATE:
        return InvestigationStep(
            hypotheses=(_hypothesis_of(candidates[0], decision),),
            next_action=StopAction(reason=f"selector escalated: {decision.reasoning}"),
        )
    if chosen is None:
        # Only reachable if the selector asked for another read while scoring no candidate,
        # which its schema forbids. Handled rather than asserted away, in case that loosens.
        return InvestigationStep(
            hypotheses=(_hypothesis_of(candidates[0], decision),),
            next_action=StopAction(reason=PROBE_MORE_WITHOUT_A_PROBE),
        )
    if decision.decision is SelectionDecision.SELECT:
        # Emit only the chosen candidate: the step's schema re-sorts a ranking by confidence,
        # so handing over the whole set could act on a diagnosis the selector rejected.
        return InvestigationStep(
            hypotheses=(_hypothesis_of(chosen, decision),),
            next_action=committed_action,
        )
    if chosen.next_probe is None:
        return InvestigationStep(
            hypotheses=(_hypothesis_of(chosen, decision),),
            next_action=StopAction(reason=f"{PROBE_MORE_WITHOUT_A_PROBE} ({chosen.candidate_id})"),
        )
    return InvestigationStep(
        hypotheses=(_hypothesis_of(chosen, decision),),
        next_action=chosen.next_probe,
    )


def _hypothesis_of(candidate: DiagnosisCandidate, decision: SelectionResult) -> Hypothesis:
    """One candidate, as the ranking the rest of the loop reads.

    ``reasoning`` is the selector's own, prefixed with the decision and score: it is required
    and ``DiagnosisCandidate`` has none (ADR 0042). Derived, not invented (ADR 0047).
    """
    score = decision.scores.get(candidate.candidate_id)
    scored = "unscored" if score is None else f"score {score}"
    return Hypothesis(
        category=candidate.category,
        name=candidate.name,
        confidence=candidate.confidence,
        reasoning=(
            f"candidate {candidate.candidate_id} ({decision.decision.value}, {scored}, "
            f"selector uncertainty {decision.uncertainty}); {decision.reasoning}"
        ),
    )
