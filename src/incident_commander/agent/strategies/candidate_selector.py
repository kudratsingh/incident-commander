"""``candidate_selector`` — a generator arm plus a selection (WP-6.2).

Plan 02 § 12 and plan 03 § 7.3. This is the arm the buildout's headline question
turns on: is the bottleneck generation or selection? A best-of-N arm produces the
set and this strategy asks the ``candidate_selector`` role which member of it the
run should act on. The evaluator then reads ``oracle_gap@k = pass@k − selected@k``
off the recorded steps: a small gap says generation limits the agent, a large one
says selection does.

The arm is ``(generator, N, selector)``, so all three are stamped into
``strategy_config``. Two different generators under one selector are two arms, and
a report keyed on the strategy name alone would collapse them into one row.

Selection is not authorization
------------------------------

The whole of plan 02 § 18, and it is the property this module is written to
preserve. The selector chooses among **diagnoses**; it never chooses an action.
Whatever it selects, the emitted ``InvestigationStep`` goes back to
``investigation.py`` and meets the ``FIX_MAP`` gate, the 0.7 threshold, the
subject-probe refusal, ADR 0041's whole-queue refusal, the ADR-0009 freshness
re-probe, ``max_iterations`` and ``_execute_probe``'s runtime tier re-check,
exactly as ``baseline``'s step does. A selected candidate whose category is
outside ``FIX_MAP`` gets a ``StopAction`` from the existing gate; that is correct
and is not special-cased here.

The three decisions, and what each emits
----------------------------------------

**``select``** — the emitted step is built from the selected candidate, **and
only from it**. That is a decision rather than an obvious reading, because
``InvestigationStep`` re-sorts its ``hypotheses`` by confidence at the schema
boundary (B-07) and three gates read index 0 as the top pick. Emitting the whole
set with the selection first would therefore let a more-confident *unselected*
candidate sort into index 0 — the loop would gate on a diagnosis the selector
rejected, which is the one failure this arm cannot be allowed to have. So the
step carries one hypothesis, the selected one, and the alternatives stay where
research data belongs: the ``StepRecord``'s ``candidate_set``, which holds all N.

The consequence is named rather than hidden. On this arm the run's hypothesis
ranking after a step is one hypothesis rather than a list, so a briefing written
on this arm sees the commitment and not the alternatives — the judge's soft
dimensions are not comparable between this arm and ``baseline``, the same limit
ADR 0044 records for the enumerated arm's derived ``reasoning``. ADR 0049
records this one.

**``probe_more``** — the emitted step is the ``next_probe`` of the candidate the
decision points at (``SelectionResult.chosen_candidate_id``: the highest-scored
one, since ``probe_more`` names none — ADR 0048). The probe goes through
``_execute_probe`` and its tier re-check like any other, so the selector cannot
widen what may be probed; and it cannot smuggle a non-read tool in either, because
``ProbeAction.tool_name`` is a ``ReadToolName`` literal and a candidate carrying
anything else fails validation at the generator's own schema boundary.

A ``probe_more`` whose candidate proposes no probe is an incoherent decision — it
asks for evidence and names no way to get it. It is **not** silently replaced by
the generator's own step (that would let a strategy overrule a decision the model
made) and it does not raise (a harness crash for a model's incoherence). It
becomes a ``StopAction`` naming exactly that, which routes through the loop's
existing ``_finalize`` and ends the run with a briefing. Fail-safe, visible in the
trajectory, and nothing is fabricated.

**``escalate``** — a ``StopAction`` carrying the selector's own reasoning, through
the same ``_finalize`` path ``baseline`` reaches when its planner says stop.

Cost, and the trap ADR 0045 named
---------------------------------

The selector is its own ROLE (``selection.SELECTOR_ROLE``), metered on its own
client (``StrategyContext.selector_llm_client``) and charged to the run ledger
through the same ``accrue_structured_call`` every other call uses. Folding it into
``investigation_planner`` would make "what did selection cost" unanswerable, which
is the number this arm is compared on.

The failure path is where the accounting is easy to break, and it is ADR 0045's
trap one layer up: the generation is paid for **before** the selector call, and the
loop's ``except`` arm charges ``accrue_llm_error`` against the ``RunState`` it held
*before* ``plan_next_step`` — so a selector that simply let its exception through
would charge the generation to nobody. It is re-raised as ``SelectorFailed``
carrying the summed usage of every billed leg of the step, generation included, and
the loop's single accrual charges all of it exactly once.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime
from types import MappingProxyType
from typing import Any, Final

from incident_commander.agent.accounting import accrue_structured_call
from incident_commander.agent.candidates import DiagnosisCandidate
from incident_commander.agent.hypothesis import (
    Hypothesis,
    InvestigationStep,
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

#: Raised when a step is planned with no selector client on the context. Named
#: so a test asserts the guard's own marker rather than the fact that something
#: raised (F-007).
NO_SELECTOR_CLIENT: Final[str] = "no selector client is on the strategy context"

#: The stop reason for a ``probe_more`` whose candidate proposes no probe. A
#: constant because the grader, the briefing and the test all read it, and three
#: spellings of one reason is how a classifier stops matching what it classifies.
PROBE_MORE_WITHOUT_A_PROBE: Final[str] = (
    "selector asked for another probe and the candidate it points at proposes none"
)


class SelectorFailed(LLMError):
    """The selector call failed, and the step's whole bill comes with it.

    An ``LLMError`` subclass so the investigation loop's existing
    ``except (ValueError, ValidationError, LLMError)`` arm catches it and
    escalates exactly as it does for ``baseline`` — no new arm and no strategy
    deciding for itself that a failure is survivable.

    ``usage`` is the sum of every billed leg of the step: the generation's calls
    and their repairs, plus the selector call and its repair. Same shape and same
    reason as ``best_of_n_sampled.SampledPlannerFailed`` (ADR 0045); the
    difference is only which part of the step was already paid for.
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

        The generator is resolved through the registry, which refuses an unknown
        name with the known ones listed — and then refuses again here if what it
        built cannot ``generate``. ``baseline`` is a name the registry has and
        this arm cannot use: it considers one diagnosis, so a selector over its
        set is a billed call whose only possible answer is the candidate it was
        handed.

        Imported inside the constructor rather than at module scope because the
        registry imports every strategy, this one included: a module-level import
        would be a cycle. The indirection is the point of the registry — one place
        resolves a name — so the alternative (importing the two arms directly) would
        mean a config value this arm accepts that the registry has never heard of.
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
                # All three parts of the arm identity (plan 02 § 12). The
                # generator's own knobs are folded in rather than restated, so a
                # report row carries the N and the temperature that actually ran.
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

        Order matters and is not an implementation detail: the generation is
        charged into ``generation.run_state`` before the selector is asked, so a
        selector failure carries a bill that already exists (see
        ``SelectorFailed``).
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
        """The decision, as the step the loop runs.

        Three branches and no fourth: ``SelectionDecision`` is a closed set, so
        a value the schema accepted is one of these.
        """
        chosen = self._chosen(decision, generation)
        if decision.decision is SelectionDecision.ESCALATE:
            return InvestigationStep(
                hypotheses=(_hypothesis_of(generation.candidates[0], decision),),
                next_action=StopAction(reason=f"selector escalated: {decision.reasoning}"),
            )
        if chosen is None:
            # Only reachable if ``scores`` were empty on a ``probe_more``, which
            # the schema forbids (every candidate is scored). Stated rather than
            # asserted away: the branch is the honest answer if that rule ever
            # loosens.
            return InvestigationStep(
                hypotheses=(_hypothesis_of(generation.candidates[0], decision),),
                next_action=StopAction(reason=PROBE_MORE_WITHOUT_A_PROBE),
            )
        if decision.decision is SelectionDecision.SELECT:
            # One hypothesis, the selected one. See the module docstring: the
            # schema re-sorts by confidence, so a set emitted here could put an
            # unselected candidate at index 0 and gate the run on a diagnosis
            # the selector rejected.
            return InvestigationStep(
                hypotheses=(_hypothesis_of(chosen, decision),),
                next_action=generation.proposed_step.next_action,
            )
        if chosen.next_probe is None:
            return InvestigationStep(
                hypotheses=(_hypothesis_of(chosen, decision),),
                next_action=StopAction(
                    reason=f"{PROBE_MORE_WITHOUT_A_PROBE} ({chosen.candidate_id})"
                ),
            )
        return InvestigationStep(
            hypotheses=(_hypothesis_of(chosen, decision),),
            next_action=chosen.next_probe,
        )

    @staticmethod
    def _chosen(
        decision: SelectionResult, generation: CandidateGeneration
    ) -> DiagnosisCandidate | None:
        """The candidate the decision points at, resolved against the set.

        ``chosen_candidate_id`` is validated against the bound set, so a
        non-``None`` id always resolves; the ``None`` return is the ``escalate``
        case and the empty-``scores`` case the schema forbids.
        """
        chosen_id = decision.chosen_candidate_id
        if chosen_id is None:
            return None
        return next(
            candidate for candidate in generation.candidates if candidate.candidate_id == chosen_id
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

        ``dataclasses.replace`` rather than a second assembly: the generator
        already measured its own context and its own calls, and a record built
        twice is two definitions of ``planner_context_chars`` — the number an arm
        comparison is read off. The whole candidate set stays exactly as the
        generator recorded it, which is what makes ``pass@k`` and ``selected@k``
        computable from one record on the same set.

        The ledger delta on the selector's ``LLMCallRecord`` is the selector
        call's own: ``updated`` minus the state the generation left, so the
        generation's charge stays on the generator's records and is not counted
        twice by anyone summing the column.
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


def _hypothesis_of(candidate: DiagnosisCandidate, decision: SelectionResult) -> Hypothesis:
    """One candidate, as the ranking the rest of the loop reads.

    ``reasoning`` is the selector's own, prefixed with the decision and the
    candidate's score, because ``Hypothesis.reasoning`` is required and a
    ``DiagnosisCandidate`` has none (ADR 0042). Derived rather than invented: every
    part of the string is something the selector said, so nothing here can be
    mistaken for model prose about the incident itself. Same limit as the
    enumerated arm's citation reasoning (ADR 0044), for the same reason, and ADR
    0047 records it.
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
