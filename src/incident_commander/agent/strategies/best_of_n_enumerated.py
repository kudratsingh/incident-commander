"""``best_of_n_enumerated`` — one planner call, N candidate diagnoses (WP-5.2).

Plan 02 § 11.1. The cheap half of the generation question: the input is
``baseline``'s, the output is asked for N times over, and what it measures is
the model's ability to **enumerate** — to name N distinct diagnoses of one
world in one breath. That is not the literature's pass@k
(``best_of_n_sampled`` is), and decision D4 keeps both because the difference
between their oracle gaps is itself a finding.

What is the same as ``baseline``
--------------------------------

Everything outside the call. The emitted ``InvestigationStep`` has
``baseline``'s exact shape, so the ``FIX_MAP`` gate, the 0.7 threshold, the
subject-probe refusal, the whole-queue refusal (ADR 0041), the ADR-0009
freshness re-probe, ``max_iterations`` and the tier re-check all run on it
unchanged, in ``investigation.py``, where they are shared. **Candidate
generation is not authorization** (plan 02 § 18): N candidates widen what the
agent has considered and change nothing about what it may do. More thinking,
not more privilege.

``tests/unit/test_best_of_n.py::TestNIsOneReproducesBaseline`` is the cheapest
proof that the seam is honest: at N=1 a canned scenario walks the same
trajectory — the same probes with the same arguments, the same terminal state,
the same top diagnosis at every step — as ``baseline`` walks on the same world.

What is different, and why each difference exists
-------------------------------------------------

1. **The output schema.** ``candidate_step_model(N)``: exactly N
   ``DiagnosisCandidate``s, no duplicate ``(category, name)``, every
   ``EvidenceRef`` resolving to a real ledger entry (ADR 0042), plus the
   step-level ``next_action``. ``min_length == max_length == N``: a short set is
   **rejected, never padded** — a padded set would make "the model produced N
   candidates" false in exactly the runs where it matters, and every pass@k
   over it a lie by construction.

2. **The prompt.** ``investigation_planner.md`` verbatim, plus one addendum
   file (``investigation_planner_best_of_n.md``) describing the candidate set
   and the citation rule. An addendum rather than a second copy of the planner
   prompt: the rules in that file are the agent's behaviour, and two copies of
   them would drift, which would make an arm comparison a comparison of
   prompts.

3. **Evidence ids in the context.** The citation rule is unaskable until the
   ids are on the page — see ``agent/planner_context.py`` and ADR 0044 for why
   they are rendered for this arm and not for ``baseline``, and for what that
   costs.

4. **The emitted hypotheses' ``reasoning``.** ``DiagnosisCandidate`` has no
   ``reasoning`` field (ADR 0042: a candidate justifies itself with the
   evidence it cites) and ``Hypothesis.reasoning`` is required. So this
   strategy **derives** it — deterministically, from the candidate's own
   citations — rather than inventing prose or asking the model for a field its
   schema deliberately does not have. The consequence is named rather than
   hidden: on this arm the ``reasoning`` a briefing and the LLM judge read is a
   citation list, not model prose, so the judge's soft dimensions are not
   comparable between this arm and ``baseline``. ADR 0044 records it and the PR
   reports it as a limit.

5. **What the record holds.** N ``CandidateRecord``s, not one — which is what
   fills ``branch_count`` (WP-2.3: candidates considered beyond the emitted
   one) without a line of new accounting, and what every pass@k in
   ``evals/candidate_metrics.py`` is computed from. Plus
   ``generation_rejections``: the validation classes of the billed calls that
   were rejected before the accepted one, which is what "the model could not
   enumerate N distinct grounded candidates" looks like in the data.

Budget
------

Nothing here reads or applies one. The arm's ledger is seeded N× by
``TOKEN_BUDGET_MULTIPLIER`` / ``USD_BUDGET_MULTIPLIER`` where every ledger is
seeded (``agent/factory.py``, WP-2.4), and that is the only place either
number is read — ``tests/unit/test_budgets.py::TestNoOtherCallSiteScalesABudget``
refuses a second reader under ``src/``, ``evals/`` or ``scripts/``, and it
refused an earlier draft of this module that carried the ratio through so the
arm could stamp it.

What a BUDGET result for this arm is read beside, then, is ``strategy_config.n``
(stamped below) and the seeded ledger the provenance record already carries. A
best-of-8 arm metered against a one-candidate ceiling exhausts it and reads as
the strategy failing rather than as the budget refusing to fund it (decision
C4); those two fields together are what make that visible, and
``tests/unit/test_best_of_n.py::TestTheBudgetMultiplierReachesTheArm`` is what
proves the multiplier reaches this arm's ledger at all.
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

#: Same role string, and so the same trace label, as ``baseline``'s planner
#: call: the two arms make the same *kind* of call, and a cost breakdown that
#: split them by role would stop being able to compare them.
_PLANNER_ROLE: Final[str] = "investigation_planner"

#: The addendum appended to ``investigation_planner.md`` for this arm.
ADDENDUM_PROMPT: Final[str] = "investigation_planner_best_of_n"

#: Validation-failure classes worth telling apart in the record. Matched on the
#: markers ``agent/candidates.py`` exports, and on pydantic's own length error
#: types, rather than on prose re-spelled here — so a reworded message cannot
#: silently reclassify every rejection as ``other``.
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
    # No class docstring on this or on the generated subclass below. Pydantic
    # puts a class docstring into the model's JSON schema as its
    # ``description``, and this schema is shown to the model on the
    # ``record_output`` tool — prose here would be prompt text nobody reviewed
    # (the rule ``agent/candidates.py`` records). Field descriptions are the
    # opposite case and are deliberate.
    model_config = ConfigDict(extra="forbid")

    candidates: CandidateTuple
    next_action: NextAction


@lru_cache(maxsize=8)
def candidate_step_model(n: int) -> type[CandidateStep]:
    """``CandidateStep`` bounded to exactly ``n`` candidates.

    Built rather than written out, because N is configuration: one hand-written
    model per value of N would make the set of runnable Ns a property of this
    file instead of of the config, and would guarantee that the schema for N=2
    differs from the schema for N=4 in some way nobody intended.

    The bound comes from ``candidates.exact_candidate_tuple(n)``, which keeps
    every set-level rule (grounding, no duplicate id, no duplicate
    ``(category, name)``, ranking normalised) and adds ``minItems``/``maxItems``.
    It is not the expression ``agent/candidates.py`` originally recommended —
    that one enforced the bound and advertised it as ``minLength``/``maxLength``
    on an array, which a JSON-Schema reader ignores, so the model was never told
    N. The factory's docstring has the detail; WP-5.2's PR reports it.

    ``CandidateStep`` itself is the typed shape and is never the schema a run
    is held to: it carries ``CandidateTuple``'s own ``min_length=1`` and no
    upper bound, so a model answering against it could return one candidate for
    an N=8 arm. Every caller goes through this function, and
    ``tests/unit/test_best_of_n.py`` asserts the bound rather than trusting it.

    Cached per N so one arm builds one model: ``model_json_schema()`` is
    rendered into every planner call, and a model rebuilt per step would make
    the schema a new object five times a run for no reason.
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

        ``knobs=None`` falls back to ``StrategyKnobs()``, i.e. N=1 — the
        control group's shape. A default of 8 would make a caller who forgot the
        configuration spend eight times as much and believe it had measured
        something.
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
                # So no artifact can put this arm beside ``baseline`` without
                # saying that one of the two saw evidence ids (ADR 0044).
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

        Expressed in terms of ``generate`` rather than beside it (WP-6.2), so
        there is exactly one generation path whether or not a selector is
        composed over this arm. The sink is called here rather than inside
        ``generate`` for the same reason: under a selector the record that
        reaches the trace is the one carrying the selector block, and a step
        must produce one record, not two.
        """
        generation = self.generate(run_state, at, ctx)
        if ctx.record_step is not None:
            ctx.record_step(generation.record)
        return generation.run_state, generation.proposed_step, generation.record

    def generate(
        self, run_state: RunState, at: datetime, ctx: StrategyContext
    ) -> CandidateGeneration:
        """One call, N candidates, the top one proposed as an ordinary step.

        Exceptions propagate exactly as they do for ``baseline``: the loop's
        ``except (ValueError, ValidationError, LLMError)`` arm charges what the
        failed call billed and escalates (ADR 0015, ADR 0035). That includes an
        exhausted repair on a model that could not produce N distinct grounded
        candidates — a real finding about the arm, which must cost the run what
        it cost rather than be quietly retried into a smaller N.

        ``grounded_in`` wraps the call rather than the parse afterwards, and
        that is what makes an ungrounded citation repairable: the validator
        raises **inside** ``llm_client.call``, so it is an ordinary output
        failure and ADR 0035's one bounded re-ask applies
        (``agent/candidates.py`` explains the choice at length).
        """
        # Bound to a local for the reason ``_plan_next_step`` binds its own: the
        # record measures the string that was sent, and re-rendering it to
        # measure it would measure a second render.
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
                # Charged through the same function ``baseline`` is charged
                # through: it bills a repaired call's rejected leg as well as
                # its accepted one, and a strategy that re-derived the accrual
                # would make every budget number a lower bound (ADR 0015).
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

        ``baseline``'s record holds one candidate because one is all it
        considered. This one holds N in the order the validator normalised them
        — index 0 is the emitted diagnosis — and the difference between "what
        was emitted" and "what was available" is the measurement Phases 5 and 6
        exist to make.
        """
        result = call.result
        return StepRecord(
            run_id=str(before.incident_id),
            iteration=ctx.iteration,
            strategy=self.name,
            model=ctx.model,
            # One call generated every candidate, so every candidate names the
            # same call. A strategy that generates them in separate calls
            # (WP-5.3) is where this starts to differ per candidate.
            candidate_set=tuple(
                candidate_record_of(candidate, generation_call_id=result.record_id)
                for candidate in candidates
            ),
            # No selector: this arm generates, it does not select. Plan 02 § 12's
            # role arrives in Phase 6 and fills this.
            selector=None,
            emitted_step=step,
            hypothesis_state_before=before.hypotheses,
            hypothesis_state_after=after.hypotheses,
            llm_calls=(
                LLMCallRecord(
                    role=_PLANNER_ROLE,
                    model=ctx.model,
                    # The ledger's own delta, which is the number ADR 0015 holds
                    # the run to: it already includes every rejected-and-billed
                    # leg, which the four counters below cannot see.
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

    Read off the markers ``agent/candidates.py`` exports and pydantic's own
    ``too_short`` / ``too_long`` error types, so the classification survives a
    reworded message. Anything unmatched is ``other`` rather than dropped: a
    rejection nobody could classify is still a rejection that was paid for.
    """
    message = str(error)
    for marker, label in _REJECTION_MARKERS:
        if marker in message:
            return label
    return REJECTION_OTHER


def _hypothesis_of(candidate: DiagnosisCandidate) -> Hypothesis:
    """One candidate, as the ranking the rest of the loop reads.

    Field for field except ``reasoning``, which ``DiagnosisCandidate`` does not
    have and ``Hypothesis`` requires — see the module docstring, point 4.
    """
    return Hypothesis(
        category=candidate.category,
        name=candidate.name,
        confidence=candidate.confidence,
        reasoning=citation_reasoning(candidate),
    )


def citation_reasoning(candidate: DiagnosisCandidate) -> str:
    """The candidate's justification: the evidence it cited, and nothing else.

    Deterministic and derived, so it cannot be mistaken for something the model
    wrote. ``min_length=1`` on ``Hypothesis.reasoning`` means silence is not a
    legal value, and the honest thing to say about a candidate that cited
    nothing is that it cited nothing.
    """
    supports = ", ".join(str(ref.evidence_id) for ref in candidate.evidence_for) or "none"
    against = ", ".join(str(ref.evidence_id) for ref in candidate.evidence_against) or "none"
    return (
        f"candidate {candidate.candidate_id} (enumerated); "
        f"evidence_for: {supports}; evidence_against: {against}"
    )
