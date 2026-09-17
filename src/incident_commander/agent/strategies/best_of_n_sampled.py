"""``best_of_n_sampled`` — N independent planner calls at a temperature (WP-5.3).

Plan 02 § 11.2. This is the literature's pass@k: N independent draws from the
same context, each returning a normal ``InvestigationStep``, with the candidate
set as the union of their top hypotheses deduplicated by ``(category, name)``.
N× planner cost, which is why 03 § 8 gates it to the final matrix — and why the
enumerated variant exists beside it: the difference between the two arms' oracle
gaps is the finding (decision D4).

The two arms differ in what they measure, not only in what they cost.
``best_of_n_enumerated`` asks one call for N distinct diagnoses, so it measures
whether the model can *enumerate* alternatives when told to. This one asks the
same question N times and takes what comes back, so it measures the *spread of
the model's own distribution* — whether a diagnosis it gave 0.9 confidence is
the diagnosis it would give again.

What it shares with every other arm
-----------------------------------

The emitted ``InvestigationStep`` is an ordinary one, so the ``FIX_MAP`` gate,
the 0.7 threshold, the subject-probe refusal, ADR 0041's whole-queue refusal,
the ADR-0009 freshness re-probe, ``max_iterations`` and the tier re-check all
run on it unchanged in ``investigation.py``. N samples widen what the agent has
considered and change nothing about what it may do (plan 02 § 18).

Three decisions, each of which could have gone another way
----------------------------------------------------------

**1. The emitted step is one sample's, verbatim — never a blend.** The winner
is the sample whose top hypothesis has the highest confidence (ties to the
earliest sample, so the choice is deterministic given the draws), and the whole
of that sample's step is emitted: its ranking *and* its ``next_action``. Taking
the ranking from one sample and the action from another would emit a step no
model proposed — a probe chosen to discriminate a hypothesis that is not the one
now on top — and every downstream gate would then be applied to a decision
nothing is accountable for. ADR 0045 records it.

**2. Every call accrues, including the ones before a failure.** N independent
calls means N accruals (ADR 0015: the meter may over-report, never under). The
trap is the failure path, not the happy one: if sample k raises, the loop's
``except`` arm charges ``accrue_llm_error`` against the ``RunState`` it held
*before* ``plan_next_step`` was entered, so anything this strategy had accrued
internally is discarded with the state it was written into. So the failure is
re-raised as a ``SampledPlannerFailed`` carrying the **summed** usage of every
billed leg of the step — the k−1 samples that returned, their repairs, and the
failing call — and the loop's single accrual charges all of it exactly once.

**3. The union is by ``(category, name)``, exactly as stated.** Two samples that
agree produce one candidate, not two, which is the whole point of a union — and
what makes "the model produced 4 distinct diagnoses out of 8 draws" a
measurement rather than an artefact of counting. No case-folding and no
whitespace collapsing, the same rule ``agent/candidates.py`` applies inside one
set and for the same reason: ``name`` is operator-facing free text, and the
duplicate rate is a measurement of what the model produced, not of what a
normaliser could hide.

Cost
----

The multiplier is **declared, not discovered** (decision C12): the arm's ledger
is seeded by ``TOKEN_BUDGET_MULTIPLIER`` / ``USD_BUDGET_MULTIPLIER`` where every
ledger is seeded, and an N-sample arm metered against a one-call ceiling
exhausts it and reads as the strategy failing (decision C4). 03 § 11 prices
``sampled-8`` at 6–8× planner cost with a blended multiplier of ~2.5, and
divergence J3 notes the plan's own arm count and estimate disagree. **Measuring
the real ratio needs a run**, so it is deferred: the number to compare is the
``investigation_planner`` role's token total in the run accounting (WP-2.3)
against a ``baseline`` run over the same scenarios, and the tolerance to report
it against is ±20% of the declared multiplier — wide enough to absorb the
context being identical while only the output multiplies, narrow enough that a
2.5 declared against a measured 6 fails it.

Temperature
-----------

``settings.sample_temperature`` (default 1.0) reaches the client per call, and
``llm/client.py`` sends the field **only** when one is given, so no other role's
request bytes move. Newer Anthropic models reject the sampling parameters
outright (``llm/client.SAMPLING_REJECTED_MODELS``); this repo's two priced ids
accept them, and a test fails the day that stops being true.
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

#: The temperature an arm built without knobs samples at. Plan 02 § 11.2's
#: default, restated here only as the fallback for a directly-constructed
#: strategy — the configured value is ``Settings.sample_temperature``.
DEFAULT_SAMPLE_TEMPERATURE: Final[float] = 1.0


class SampledPlannerFailed(LLMError):
    """One of the N samples failed, and the step's whole bill comes with it.

    An ``LLMError`` subclass so the investigation loop's existing
    ``except (ValueError, ValidationError, LLMError)`` arm catches it and
    escalates exactly as it does for ``baseline`` — no new arm, no new
    behaviour, and no strategy deciding on its own that a failure is
    survivable.

    ``usage`` is the sum of every billed leg of the step: the samples that
    returned, any ADR-0035 repair inside them, and the call that failed. The
    loop charges it once with the ``accrue_llm_error`` it already has. Without
    this the k−1 completed samples would be charged to nobody, because the
    state they were accrued into is the state the raise discards (ADR 0015,
    ADR 0045).
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

        ``knobs=None`` gives N=1 at the default temperature: one call, which is
        ``baseline``'s shape with a temperature on it. A default of 8 would make
        a caller who forgot the configuration spend eight times as much.
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
                # False, and stated rather than omitted: this arm's output schema
                # is a plain ``InvestigationStep`` with no ``EvidenceRef`` in it,
                # so it has no reason to be shown ids and is NOT comparable on
                # that axis with ``best_of_n_enumerated``, which is (ADR 0044).
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

        Expressed in terms of ``generate`` (WP-6.2) so there is one draw path
        whether or not a selector is composed over this arm; see
        ``strategies/generation.py`` for why the sink is called here.
        """
        generation = self.generate(run_state, at, ctx)
        if ctx.record_step is not None:
            ctx.record_step(generation.record)
        return generation.run_state, generation.proposed_step, generation.record

    def generate(
        self, run_state: RunState, at: datetime, ctx: StrategyContext
    ) -> CandidateGeneration:
        """Draw N samples, propose the best one's step, record the union.

        The context is rendered **once** and reused for every sample. That is
        what makes them samples of one distribution rather than N answers to N
        questions, and it is also why the arm is cheap on the input side: with
        the system prompt cached (``llm/client.py``), the second and later draws
        pay the cache-read rate for the context and full price only for output.
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

        Sequential rather than concurrent, deliberately. The loop this sits in
        is a synchronous state machine (ADR 0002) and the only client is a
        blocking one; adding a thread pool here would put concurrency inside a
        strategy, where the accrual, the tracer's append-only writes and the
        run's single ledger would all need to become thread-safe for a latency
        win on an offline benchmark. It is a real cost — ``sampled-8`` is
        roughly 8× the planner latency of ``baseline`` — and it is reported
        rather than engineered away: latency per step is on the ``StepRecord``.
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

        One ``LLMCallRecord`` per sample, so ``llm_calls`` has length N and a
        cost reader can see that the step cost N calls rather than inferring it
        from a total. The ledger delta is carried on the FIRST record and zero on
        the rest: it is a per-step quantity (``after.budget - before.budget``)
        and attributing it to each sample would multiply the step's charge by N
        for anyone who summed the column. The four provider counters are
        per-sample and are where the split actually lives.
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
            # No selector: this arm draws, it does not select between the draws
            # beyond taking the most confident one. Plan 02 § 12's role arrives
            # in Phase 6 and is what fills this.
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
            # The context was rendered once and sent N times, so the step's
            # context cost is N × the string. Not the length of one render: the
            # arm really did feed the planner that much context, and a reader
            # comparing ``planner_context_chars`` across arms is comparing what
            # each one paid for context.
            planner_context_chars=self._n * (len(self._system_prompt) + len(user_message)),
            # Rejections are per sample, in sample order, so a step where the
            # third draw needed a repair says so.
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

    ``None`` only when nothing anywhere reported a usage — a fake client that
    raises a bare ``ValueError`` on the first draw — in which case charging a
    guess would be an over-report invented rather than measured (the rule
    ``MeteredLLMClient`` follows for the same case).
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

    ``max`` over a stable key with the sample index as the tiebreak, so two
    samples that agree on confidence resolve to the earlier draw and the choice
    is deterministic given the draws. Index 0 of each sample's ranking is its
    top hypothesis by construction — ``InvestigationStep`` re-sorts by
    confidence at the schema boundary (B-07).
    """
    return max(
        calls,
        key=lambda call: (call.result.output.hypotheses[0].confidence, -calls.index(call)),
    )


def _union_of_tops(
    calls: Sequence[RepairedCall[InvestigationStep]],
) -> tuple[tuple[DiagnosisCandidate, str], ...]:
    """The samples' top hypotheses, deduplicated by ``(category, name)``.

    First occurrence wins, so the recorded confidence and the recorded
    generating call are the first sample that produced that diagnosis — a
    later agreeing sample is evidence *of* the candidate, not a second one.
    Returned in confidence-descending order (stable, so agreeing confidences
    keep draw order) to match every other arm's record, where index 0 is the
    emitted diagnosis.

    Each entry is the candidate paired with the trace-record id of the call that
    drew it. **``DiagnosisCandidate`` rather than ``CandidateRecord`` since
    WP-6.2**, because a selector composed over this arm needs the whole
    ``next_probe`` — a record keeps only the tool name, and a probe re-derived
    from one would lose its arguments. The record is still built from these, by
    ``generation.candidate_record_of``, so the dedup rule has one implementation.

    Constructed rather than ``model_validate``d: ``DiagnosisCandidate``'s
    grounding validator lives on ``EvidenceRef``, a sampled candidate cites
    nothing, and the set-level rules belong to ``CandidateTuple`` — so no ledger
    binding is needed here and none is faked. A sampled candidate citing no
    evidence is the honest reading of "a plain ``InvestigationStep`` has no
    per-hypothesis citation field", the same one ``baseline``'s record makes.
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
