"""``best_of_n_enumerated`` — one planner call, N candidate diagnoses (WP-5.2).

Plan 02 § 11.1. ``baseline``'s input with the output asked for N times over, so it measures
**enumeration** rather than the literature's pass@k (``best_of_n_sampled``; decision D4 keeps
both, because the difference between their oracle gaps is the finding). The emitted step has
``baseline``'s exact shape, so the ``FIX_MAP`` gate, the 0.7 threshold, ADR 0041's whole-queue
refusal, the ADR-0009 re-probe and the tier re-check all run on it unchanged in
``investigation.py``: **candidate generation is not authorization** (plan 02 § 18).

What differs: the schema is ``candidate_step_model(N)`` — exactly N ``DiagnosisCandidate``s,
every ``EvidenceRef`` resolving to a real ledger entry (ADR 0042), a short set **rejected,
never padded**; the prompt gains one addendum (``investigation_planner_best_of_n``) rather than
a second copy that could drift; evidence ids are rendered into the context by
``agent/planner_context.py``, and ``Hypothesis.reasoning`` is **derived** from the candidate's
own citations, so on this arm the judge's soft dimensions are not comparable with ``baseline``
(both ADR 0044); and the record holds N ``CandidateRecord``s, which fill ``branch_count``
(WP-2.3) and every pass@k in
``evals/candidate_metrics.py``, plus ``generation_rejections``.

Nothing here reads a budget: the ledger is seeded N× by ``TOKEN_BUDGET_MULTIPLIER`` /
``USD_BUDGET_MULTIPLIER`` in ``agent/factory.py`` (WP-2.4) and nowhere else
(``tests/unit/test_budgets.py::TestNoOtherCallSiteScalesABudget``), so a BUDGET result is read
beside ``strategy_config.n`` and that seeded ledger (decision C4).
``tests/unit/test_best_of_n.py`` pins both halves: ``TestNIsOneReproducesBaseline`` (N=1 walks
``baseline``'s trajectory) and ``TestTheBudgetMultiplierReachesTheArm``.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from functools import lru_cache
from types import MappingProxyType
from typing import Any, Final

from pydantic import ConfigDict, create_model

from incident_commander.agent.accounting import accrue_structured_call
from incident_commander.agent.candidates import (
    DUPLICATE_CANDIDATE,
    DUPLICATE_CANDIDATE_ID,
    UNKNOWN_EVIDENCE_ID,
    CandidateTuple,
    DiagnosisCandidate,
    exact_candidate_tuple,
    grounded_in,
)
from incident_commander.agent.hypothesis import Hypothesis, InvestigationStep, NextAction
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
from incident_commander.llm.prompts.loader import load_prompt
from incident_commander.llm.repair import RepairedCall, call_with_output_repair
from incident_commander.llm.structured import StructuredOutput

#: Same role string, and so the same trace label, as ``baseline``'s planner call: a cost
#: breakdown that split the two arms by role would stop being able to compare them.
_PLANNER_ROLE: Final[str] = "investigation_planner"

#: The addendum appended to ``investigation_planner.md`` for this arm.
ADDENDUM_PROMPT: Final[str] = "investigation_planner_best_of_n"

#: Validation-failure classes worth telling apart in the record. Matched on the markers
#: ``agent/candidates.py`` exports and pydantic's length error types, never on prose here.
_REJECTION_MARKERS: Final[tuple[tuple[str, str], ...]] = (
    (DUPLICATE_CANDIDATE_ID, "duplicate_candidate_id"),
    (DUPLICATE_CANDIDATE, "duplicate_candidate"),
    (UNKNOWN_EVIDENCE_ID, "ungrounded_evidence"),
    ("too_short", "short_set"),
    ("too_long", "long_set"),
)

#: What ``generation_rejections`` records when the message matches no marker.
REJECTION_OTHER: Final[str] = "other"


class CandidateStep(StructuredOutput):
    # No class docstring on this or on the generated subclass below: pydantic puts one into the
    # JSON schema as ``description``, which the model is shown on ``record_output``.
    model_config = ConfigDict(extra="forbid")

    candidates: CandidateTuple
    next_action: NextAction


@lru_cache(maxsize=8)
def candidate_step_model(n: int) -> type[CandidateStep]:
    """``CandidateStep`` bounded to exactly ``n`` candidates, cached so one arm builds one model.

    Built rather than written out, because N is configuration. The bound is
    ``candidates.exact_candidate_tuple(n)``, which advertises ``minItems``/``maxItems``; the
    ``minLength``/``maxLength`` form a JSON-Schema reader ignores, so the model was never told N.
    ``CandidateStep`` itself has no upper bound and is never the schema a run is held to — every
    caller comes through here, and ``tests/unit/test_best_of_n.py`` asserts the bound.
    """
    return create_model(
        f"CandidateStep{n}",
        __base__=CandidateStep,
        __module__=__name__,
        candidates=(exact_candidate_tuple(n), ...),
    )


class BestOfNEnumeratedStrategy:
    """One planner call per step, N candidates out of it, top candidate emitted."""

    name: str = StrategyName.BEST_OF_N_ENUMERATED.value

    def __init__(self, knobs: StrategyKnobs | None = None) -> None:
        """Build the arm from its inference block.

        ``knobs=None`` falls back to N=1: a default of 8 would spend eight times as much.
        """
        resolved = knobs if knobs is not None else StrategyKnobs()
        self._n: Final[int] = resolved.n
        self._output_model: Final[type[CandidateStep]] = candidate_step_model(resolved.n)
        self._system_prompt: Final[str] = (
            f"{load_prompt('investigation_planner').rstrip()}\n\n{load_prompt(ADDENDUM_PROMPT)}"
        )
        self.config: Mapping[str, Any] = MappingProxyType(
            {
                "n": resolved.n,
                # So no artifact can put this arm beside ``baseline`` without saying that one
                # of the two saw evidence ids (ADR 0044).
                "evidence_ids_rendered": True,
            }
        )

    @property
    def n(self) -> int:
        """How many candidates this arm asks for per step."""
        return self._n

    def plan_next_step(
        self, run_state: RunState, at: datetime, ctx: StrategyContext
    ) -> tuple[RunState, InvestigationStep, StepRecord]:
        """This arm running alone: generate, record, emit the top candidate's step.

        Expressed via ``generate`` (WP-6.2) so there is one generation path, and the sink is
        called here because a step must produce one record.
        """
        generation = self.generate(run_state, at, ctx)
        if ctx.record_step is not None:
            ctx.record_step(generation.record)
        return generation.run_state, generation.proposed_step, generation.record

    def generate(
        self, run_state: RunState, at: datetime, ctx: StrategyContext
    ) -> CandidateGeneration:
        """One call, N candidates, the top one proposed as an ordinary step.

        Exceptions propagate as for ``baseline`` (ADR 0015, ADR 0035), including an exhausted
        repair — a real finding, never quietly retried into a smaller N. ``grounded_in`` wraps
        the call, not the parse, so an ungrounded citation raises inside ``llm_client.call``
        and ADR 0035's one bounded re-ask applies.
        """
        # Bound to a local because the record measures the string that was sent; re-rendering
        # it to measure it would measure a second render.
        user_message = format_planner_context(run_state, show_evidence_ids=True)
        with grounded_in(run_state.evidence):
            call = call_with_output_repair(
                ctx.llm_client,
                system_prompt=self._system_prompt,
                user_message=user_message,
                output_model=self._output_model,
                model=ctx.model,
            )
        candidates = call.result.output.candidates
        step = InvestigationStep(
            hypotheses=tuple(_hypothesis_of(candidate) for candidate in candidates),
            next_action=call.result.output.next_action,
        )
        updated = run_state.model_copy(
            update={
                # Charged through the same function ``baseline`` uses: it bills a repaired
                # call's rejected leg too, so no budget number becomes a lower bound (ADR 0015).
                "budget": accrue_structured_call(run_state.budget, call, ctx.model),
                "hypotheses": step.hypotheses,
                "updated_at": at,
            }
        )
        return CandidateGeneration(
            run_state=updated,
            candidates=candidates,
            proposed_step=step,
            record=self._record(run_state, updated, step, candidates, call, ctx, user_message),
            billed_usage=billed_usage_of((call,)),
        )

    def _record(
        self,
        before: RunState,
        after: RunState,
        step: InvestigationStep,
        candidates: tuple[DiagnosisCandidate, ...],
        call: RepairedCall[CandidateStep],
        ctx: StrategyContext,
        user_message: str,
    ) -> StepRecord:
        """The whole candidate set, not just the winner.

        N in the validator's normalised order — index 0 is the emitted diagnosis.
        """
        result = call.result
        return StepRecord(
            run_id=str(before.incident_id),
            iteration=ctx.iteration,
            strategy=self.name,
            model=ctx.model,
            # One call generated every candidate, so every candidate names the same call; a
            # strategy generating them in separate calls (WP-5.3) differs per candidate.
            candidate_set=tuple(
                candidate_record_of(candidate, generation_call_id=result.record_id)
                for candidate in candidates
            ),
            # No selector: this arm generates, it does not select (plan 02 § 12).
            selector=None,
            emitted_step=step,
            hypothesis_state_before=before.hypotheses,
            hypothesis_state_after=after.hypotheses,
            llm_calls=(
                LLMCallRecord(
                    role=_PLANNER_ROLE,
                    model=ctx.model,
                    # The ledger's own delta, the number ADR 0015 holds the run to: it includes
                    # every rejected-and-billed leg, which the four counters below cannot see.
                    tokens_used=after.budget.tokens_used - before.budget.tokens_used,
                    usd_used=after.budget.usd_used - before.budget.usd_used,
                    input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                    cache_read_tokens=result.cache_read_tokens,
                    cache_creation_tokens=result.cache_creation_tokens,
                    call_id=result.record_id,
                    elapsed_ms=result.elapsed_ms,
                ),
            ),
            planner_input_tokens=(
                result.input_tokens + result.cache_read_tokens + result.cache_creation_tokens
            ),
            planner_context_chars=len(self._system_prompt) + len(user_message),
            generation_rejections=tuple(rejection_class(err) for err in call.failures),
        )


def rejection_class(error: BaseException) -> str:
    """Which validation rule a billed-and-rejected candidate set broke.

    Matched on markers and pydantic's ``too_short``/``too_long``, not prose. Unmatched is
    ``other``, not dropped.
    """
    message = str(error)
    for marker, label in _REJECTION_MARKERS:
        if marker in message:
            return label
    return REJECTION_OTHER


def _hypothesis_of(candidate: DiagnosisCandidate) -> Hypothesis:
    """One candidate, as the ranking the loop reads (all but ``reasoning``)."""
    return Hypothesis(
        category=candidate.category,
        name=candidate.name,
        confidence=candidate.confidence,
        reasoning=citation_reasoning(candidate),
    )


def citation_reasoning(candidate: DiagnosisCandidate) -> str:
    """The candidate's justification: the evidence it cited, and nothing else.

    Derived, so it cannot be mistaken for model prose; ``min_length=1`` forbids silence.
    """
    supports = ", ".join(str(ref.evidence_id) for ref in candidate.evidence_for) or "none"
    against = ", ".join(str(ref.evidence_id) for ref in candidate.evidence_against) or "none"
    return (
        f"candidate {candidate.candidate_id} (enumerated); "
        f"evidence_for: {supports}; evidence_against: {against}"
    )
