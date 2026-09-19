"""``candidate_selector`` — a generator arm plus a selection (WP-6.2).

Plan 02 § 12 and plan 03 § 7.3, and the buildout's headline question: is the bottleneck
generation or selection? A best-of-N arm produces the set, this strategy asks the
``candidate_selector`` role which member to act on, and the evaluator reads
``oracle_gap@k = pass@k − selected@k`` off the recorded steps. The arm is
``(generator, N, selector)``, all three stamped into ``strategy_config``.

**Selection is not authorization** (plan 02 § 18). The selector chooses among *diagnoses*,
never an action: the emitted step still meets the FIX_MAP gate, the 0.7 confidence gate,
ADR 0041's whole-queue refusal, the ADR-0009 re-probe and ``_execute_probe``'s tier re-check in
``investigation.py``, and a selected category outside ``FIX_MAP`` gets the gate's ``StopAction``.

**``select``** emits the selected candidate **and only it**, because ``InvestigationStep``
re-sorts by confidence (B-07) and a more-confident *unselected* candidate would otherwise gate
the run on a diagnosis the selector rejected; the alternatives stay in ``candidate_set``. The
ranking is then one hypothesis, so a briefing is not comparable with ``baseline``'s (ADR 0044,
ADR 0049). **``probe_more``** emits the ``next_probe`` of
``SelectionResult.chosen_candidate_id`` (the highest-scored one, since ``probe_more`` names
none — ADR 0048); ``ProbeAction.tool_name`` is a ``ReadToolName`` literal, so no non-read tool
can be smuggled in, and a candidate proposing no probe becomes a ``StopAction`` through
``_finalize`` rather than the generator's step or a crash. **``escalate``** is a ``StopAction``
carrying the selector's reasoning, down that same path.

The selector is its own role (``selection.SELECTOR_ROLE``) on its own client
(``StrategyContext.selector_llm_client``), charged through ``accrue_structured_call``, so its
cost is not folded into ``investigation_planner``'s. It is paid after the generation, and the
loop charges ``accrue_llm_error`` against the state held *before* ``plan_next_step``, so a
failure is re-raised as ``SelectorFailed`` carrying every billed leg's summed usage (ADR 0045's
trap one layer up).
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

#: Raised when a step is planned with no selector client on the context. Named so a test can
#: assert the guard's own marker rather than that something raised (F-007).
NO_SELECTOR_CLIENT: Final[str] = "no selector client is on the strategy context"

#: The stop reason for a ``probe_more`` whose candidate proposes no probe. A constant because
#: the grader, the briefing and the test all read it, and three spellings would not match.
PROBE_MORE_WITHOUT_A_PROBE: Final[str] = (
    "selector asked for another probe and the candidate it points at proposes none"
)


class SelectorFailed(LLMError):
    """The selector call failed, and the step's whole bill comes with it.

    An ``LLMError`` subclass so the loop's existing ``except`` arm escalates as for
    ``baseline``. ``usage`` sums every billed leg, generation included — same shape and reason
    as ``best_of_n_sampled.SampledPlannerFailed`` (ADR 0045).
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

        The generator is resolved through the registry, then refused again here if what it
        built cannot ``generate`` — ``baseline`` is a name the registry has and this arm cannot
        use. The import is inside the constructor because the registry imports every strategy,
        this one included, so a module-level import would be a cycle.
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
                # All three parts of the arm identity (plan 02 § 12), the generator's own knobs
                # folded in rather than restated, so a report row carries what actually ran.
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
        """Generate the set, select from it, emit the selection's step.

        Order matters: the generation is charged before the selector is asked.
        """
        if ctx.selector_llm_client is None:
            raise ValueError(
                f"{NO_SELECTOR_CLIENT}. The selector is its own metered role "
                "(agent/selection.SELECTOR_ROLE); falling back to the planner's "
                "client would report selection's tokens under generation's role."
            )
        generation = self._generator.generate(run_state, at, ctx)
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
        decision = call.result.output
        step = self._step_for(decision, generation)
        updated = generation.run_state.model_copy(
            update={
                # Charged through the same function every other call is charged
                # through, so a repaired selector call bills both legs (ADR 0015).
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

        ``dataclasses.replace`` rather than a second assembly, which would be two definitions
        of ``planner_context_chars``. The candidate set stays as the generator recorded it, so
        ``pass@k`` and ``selected@k`` come from one record; the selector's ledger delta is its
        own call's.
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

    Module-level and shared, so ``search``'s chosen path hands the loop the same shape this
    arm does (WP-12.1); ``committed_action`` is what a ``select`` acts on.
    """
    chosen = chosen_candidate(decision, candidates)
    if decision.decision is SelectionDecision.ESCALATE:
        return InvestigationStep(
            hypotheses=(_hypothesis_of(candidates[0], decision),),
            next_action=StopAction(reason=f"selector escalated: {decision.reasoning}"),
        )
    if chosen is None:
        # Only reachable if ``scores`` were empty on a ``probe_more``, which the schema
        # forbids. Stated rather than asserted away, in case that rule ever loosens.
        return InvestigationStep(
            hypotheses=(_hypothesis_of(candidates[0], decision),),
            next_action=StopAction(reason=PROBE_MORE_WITHOUT_A_PROBE),
        )
    if decision.decision is SelectionDecision.SELECT:
        # One hypothesis, the selected one: the schema re-sorts by confidence, so a set
        # emitted here could gate the run on a diagnosis the selector rejected.
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

    ``reasoning`` is the selector's own, prefixed with the decision and score, as
    ``Hypothesis.reasoning`` is required and ``DiagnosisCandidate`` has none (ADR 0042).
    Derived, not invented (ADR 0047).
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
