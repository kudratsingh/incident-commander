"""``adaptive`` — the ladder: more inference only where a step's own numbers ask for it (WP-13.2).

``baseline → best_of_n_enumerated(4) → candidate_selector → search or escalate``, each rung entered
only because WP-13.1's thresholds fired below it (ADR 0061, ADR 0064). Every rung emits an ordinary
step, so ``investigation.py``'s gates run unchanged: a rung buys thinking, never privilege.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Final

from incident_commander.agent.accounting import accrue_structured_call
from incident_commander.agent.candidates import DiagnosisCandidate
from incident_commander.agent.hypothesis import InvestigationStep, ProbeAction, StopAction
from incident_commander.agent.investigation import _plan_next_step
from incident_commander.agent.selection import (
    SELECTOR_ROLE,
    SelectionResult,
    select_candidate,
)
from incident_commander.agent.state import BudgetLedger, RunState
from incident_commander.agent.strategies.candidate_selector import step_for_selection
from incident_commander.agent.strategies.generation import (
    CandidateGeneration,
    CandidateGenerator,
    candidate_record_of,
)
from incident_commander.agent.strategies.knobs import StrategyKnobs
from incident_commander.agent.strategies.names import StrategyName
from incident_commander.agent.strategies.policy import (
    EscalationSignal,
    FiredSignals,
    ThresholdName,
    UncertaintyThresholds,
    evaluate,
    reading_of,
)
from incident_commander.agent.strategies.protocol import InvestigationStrategy, StrategyContext
from incident_commander.agent.strategies.records import (
    CandidateRecord,
    LadderRecord,
    LLMCallRecord,
    PlannerCall,
    RungRecord,
    SearchRecord,
    SelectorRecord,
    StepRecord,
)
from incident_commander.llm.client import LLMError, LLMUsage
from incident_commander.llm.repair import RepairedCall, sum_usage, usage_of

#: Same role string, and so the same trace label, as ``baseline``'s planner call — for every
#: rung's planner call. A split by rung would stop the token total being comparable.
_PLANNER_ROLE: Final[str] = "investigation_planner"

#: N the enumerated rung generates. In code, not in ``StrategyKnobs``: an environment that could
#: set this to 1 would report an adaptive number for a run whose second rung was a baseline call.
LADDER_N: Final[int] = 4

#: Named so a test asserts the guard's own marker rather than that something raised (F-007).
NO_SELECTOR_CLIENT: Final[str] = "no selector client is on the strategy context"

#: Why the ``escalate`` rung exists, and what it means when a step lands on it.
LADDER_EXHAUSTED: Final[str] = (
    "adaptive escalated: every rung of the ladder ran and the step is still uncertain"
)

#: Why the tail is ``escalate`` rather than ``search`` on this run (ADR 0060).
SEARCH_RUNG_UNAVAILABLE: Final[str] = (
    "the search rung reads the world and runs in recorded mode only (ADR 0060); this run has no "
    "branch prober, so the ladder ends one rung short"
)

#: Which rung's call failed, for ``AdaptiveFailed``'s message.
ENUMERATED_STAGE: Final[str] = "best_of_n_enumerated rung"
SELECTOR_STAGE: Final[str] = "candidate_selector rung"
SEARCH_STAGE: Final[str] = "search rung"


class Rung(StrEnum):
    """One step of plan 02 § 15's ladder. Closed: a rung lands with its transition."""

    BASELINE = "baseline"
    ENUMERATED = "best_of_n_enumerated"
    SELECTOR = "candidate_selector"
    SEARCH = "search"
    #: Not a strategy: the tail taken when ``search`` cannot run. It buys no inference and
    #: emits a ``StopAction``, which the loop turns into an escalation with a briefing.
    ESCALATE = "escalate"


#: The rungs every climb shares. The tail (``search`` or ``escalate``) is resolved per step,
#: because whether a branch may read is a property of the MODE.
CLIMB: Final[tuple[Rung, ...]] = (Rung.BASELINE, Rung.ENUMERATED, Rung.SELECTOR)

#: Signals about the RUN rather than this step, so no rung clears them (ADR 0056). They buy the
#: climb but do NOT decide the tail: a tail decided by an unclearable signal is taken
#: unconditionally, which would cut ADR 0056's reinvestigation short.
UNCLEARABLE: Final[frozenset[EscalationSignal]] = frozenset(
    {EscalationSignal.REMEDIATION_ATTEMPT_FAILED}
)


class AdaptiveFailed(LLMError):
    """A call above the baseline rung failed, and the step's whole bill comes with it.

    An ``LLMError`` so the loop's existing ``except`` arm escalates as for ``baseline``;
    ``usage`` sums every billed leg of every rung so far (ADR 0045).
    """

    def __init__(self, stage: str, cause: BaseException, usage: LLMUsage | None) -> None:
        super().__init__(
            f"adaptive {stage} failed: {cause}",
            usage=usage,
            record_id=getattr(cause, "record_id", None),
        )
        self.stage = stage
        self.cause = cause


def ladder_for(ctx: StrategyContext) -> tuple[Rung, ...]:
    """The rungs this step may climb: ``search`` at the top where a branch may read."""
    tail = Rung.SEARCH if ctx.branch_prober is not None else Rung.ESCALATE
    return (*CLIMB, tail)


class AdaptiveStrategy:
    """The ladder: ``baseline``, then one rung per escalation signal that is still firing."""

    name: str = StrategyName.ADAPTIVE.value

    def __init__(self, knobs: StrategyKnobs | None = None) -> None:
        """Build the arm: the thresholds it compares against, and the rungs above the first.

        The higher arms come from the registry, so the ladder runs the same objects the fixed
        arms do. The import is local because the registry imports this module.
        """
        from incident_commander.agent.strategies.registry import STRATEGIES

        resolved = knobs if knobs is not None else StrategyKnobs()
        self._thresholds: Final[UncertaintyThresholds] = _thresholds_from(resolved)
        generator = STRATEGIES.create(
            StrategyName.BEST_OF_N_ENUMERATED.value, replace(resolved, n=LADDER_N)
        )
        if not isinstance(generator, CandidateGenerator):  # pragma: no cover - pinned by a test
            raise ValueError(
                f"the ladder's second rung ({StrategyName.BEST_OF_N_ENUMERATED.value}) cannot "
                "supply a candidate set, so the selector rung above it would have nothing to "
                "select between."
            )
        self._generator: Final[CandidateGenerator] = generator
        self._search: Final[InvestigationStrategy] = STRATEGIES.create(
            StrategyName.SEARCH.value, resolved
        )
        self.config: Mapping[str, Any] = MappingProxyType(
            {
                # The ladder as configured, tail included as the pair it can be; which one a
                # step ran is in its ``LadderRecord``.
                "ladder": [rung.value for rung in CLIMB]
                + [f"{Rung.SEARCH.value}_or_{Rung.ESCALATE.value}"],
                "n": LADDER_N,
                "generator": generator.name,
                "selector": SELECTOR_ROLE,
                # How the ladder is held: a sequence in code, not a number an operator sets.
                "cap": "structural",
                # Per RUNG here (ADR 0044), so a table must read ``ladder.terminated_on`` before
                # it puts this arm beside another.
                "evidence_ids_rendered": "baseline rung no, every rung above it yes",
                "search_rung_requires": "recorded mode (ADR 0060)",
                "depth": resolved.search_depth,
                "branch": resolved.search_branch,
                # The operating point, with the split behind each default (ADR 0061). Plain
                # dicts: this block is stamped into the run's provenance record as JSON.
                "thresholds": {
                    name: dict(row) for name, row in self._thresholds.as_strategy_config().items()
                },
            }
        )

    @property
    def thresholds(self) -> UncertaintyThresholds:
        """The operating point every rung transition on this arm is decided by."""
        return self._thresholds

    @property
    def generator(self) -> CandidateGenerator:
        """The arm the enumerated rung runs."""
        return self._generator

    @property
    def search(self) -> InvestigationStrategy:
        """The arm the top rung runs, where the mode allows one."""
        return self._search

    def plan_next_step(
        self, run_state: RunState, at: datetime, ctx: StrategyContext
    ) -> tuple[RunState, InvestigationStep, StepRecord]:
        """Climb until nothing fires, then hand the loop the rung's step.

        Refuses before any call when no selector client is on the context: a ladder that
        silently stopped at the second rung would report an adaptive number for a best-of-N run.
        """
        if ctx.selector_llm_client is None:
            raise ValueError(
                f"{NO_SELECTOR_CLIENT}. The ladder's third rung is the "
                f"{SELECTOR_ROLE} role on its own metered client "
                "(agent/selection.SELECTOR_ROLE); borrowing the planner's would report "
                "selection's tokens under generation's role."
            )
        return _Climb(strategy=self, ctx=ctx, at=at, ladder=ladder_for(ctx)).run(run_state)


def _thresholds_from(knobs: StrategyKnobs) -> UncertaintyThresholds:
    """The policy this arm runs, from the edge's overrides. ``None`` means the declared default."""
    return UncertaintyThresholds.resolve(
        top1_confidence_floor=knobs.uncertainty_top1_confidence_floor,
        top1_top2_margin_floor=knobs.uncertainty_top1_top2_margin_floor,
        selector_uncertainty_ceiling=knobs.uncertainty_selector_uncertainty_ceiling,
        candidate_disagreement_ceiling=knobs.uncertainty_candidate_disagreement_ceiling,
        contradictory_evidence_count=knobs.uncertainty_contradictory_evidence_count,
        failed_attempt_count=knobs.uncertainty_failed_attempt_count,
        probe_count_before_confidence_check=knobs.uncertainty_probe_count_before_confidence_check,
        confidence_floor_after_probes=knobs.uncertainty_confidence_floor_after_probes,
    )


@dataclass(slots=True)
class _Climb:
    """One planner step's climb: the rungs taken, what each cost, and the one bill.

    Mutable and per-step, like ``search``'s walk: it exists so the calls, the billed usage and
    the rung records have ONE carrier, whichever rung the step ends on.
    """

    strategy: AdaptiveStrategy
    ctx: StrategyContext
    at: datetime
    ladder: tuple[Rung, ...]
    calls: tuple[LLMCallRecord, ...] = ()
    billed: tuple[LLMUsage | None, ...] = ()
    rungs: tuple[RungRecord, ...] = ()
    rejections: tuple[str, ...] = ()
    planner_input_tokens: int = 0
    planner_context_chars: int = 0

    def run(self, run_state: RunState) -> tuple[RunState, InvestigationStep, StepRecord]:
        """The ladder, one rung at a time, each entered only by the one below's signals."""
        # 1. Rung 0, `baseline`: one planner call, then read the signals off what it produced.
        before = run_state
        planned, step, planner = self._baseline_rung(run_state)
        fired = self._note(
            Rung.BASELINE,
            entered_because=frozenset(),
            signals=evaluate(planned, thresholds=self.strategy.thresholds),
            spent=(before.budget, planned.budget),
            calls=1,
        )
        candidate_set: tuple[CandidateRecord, ...] = (_baseline_candidate(step, planner.record_id),)
        # 2. Nothing fired: an easy step costs one planner call, which is the cheapness claim.
        if not fired.escalate:
            return self._handoff(before, planned, step, Rung.BASELINE, candidate_set=candidate_set)

        # 3. Rung 1, `best_of_n_enumerated(4)`: N candidates over the same ledger.
        generation = self._generate(planned)
        enumerated, candidates = generation.run_state, generation.candidates
        generation_call_id = _generation_call_id(generation)
        candidate_set = tuple(
            candidate_record_of(candidate, generation_call_id=generation_call_id)
            for candidate in candidates
        )
        fired = self._note(
            Rung.ENUMERATED,
            entered_because=fired.fired,
            signals=evaluate(
                enumerated,
                thresholds=self.strategy.thresholds,
                reading=reading_of(candidates),
            ),
            spent=(planned.budget, enumerated.budget),
            calls=1,
        )
        if not fired.escalate:
            return self._handoff(
                before,
                enumerated,
                generation.proposed_step,
                Rung.ENUMERATED,
                candidate_set=candidate_set,
            )

        # 4. Rung 2, `candidate_selector`: one selection over the set rung 1 paid for.
        selection, selected, selector_call_id = self._select(enumerated, candidates)
        step = step_for_selection(
            selection, candidates, committed_action=generation.proposed_step.next_action
        )
        selected = selected.model_copy(
            update={"hypotheses": step.hypotheses, "updated_at": self.at}
        )
        selector = SelectorRecord(
            selected_candidate_id=selection.selected_candidate_id,
            scores=dict(selection.scores),
            uncertainty=selection.uncertainty,
            decision=selection.decision.value,
            call_id=selector_call_id,
        )
        fired = self._note(
            Rung.SELECTOR,
            entered_because=fired.fired,
            signals=evaluate(
                selected,
                thresholds=self.strategy.thresholds,
                reading=reading_of(candidates, selector_uncertainty=selection.uncertainty),
            ),
            spent=(enumerated.budget, selected.budget),
            calls=1,
        )
        # 5. The tail is decided by the signals a rung COULD have cleared (``UNCLEARABLE``).
        if not fired.fired - UNCLEARABLE:
            return self._handoff(
                before,
                selected,
                step,
                Rung.SELECTOR,
                candidate_set=candidate_set,
                selector=selector,
            )

        # 6. Rung 3: a bounded walk where the mode allows one, else stop and say what fired.
        if self.ladder[-1] is Rung.SEARCH:
            return self._search_rung(before, selected, entered_because=fired.fired)
        return self._escalate_rung(
            before,
            selected,
            step,
            fired,
            candidate_set=candidate_set,
            selector=selector,
        )

    # --- the rungs --------------------------------------------------------

    def _baseline_rung(
        self, run_state: RunState
    ) -> tuple[RunState, InvestigationStep, PlannerCall]:
        """Rung 0: ``investigation._plan_next_step`` verbatim, as ``baseline`` runs it.

        Its exceptions propagate untouched, exactly as on the control group: the loop's
        ``except`` arm charges what the failed call billed and escalates (ADR 0015, ADR 0035).
        """
        planned, step, planner = _plan_next_step(
            run_state,
            self.at,
            self.ctx.llm_client,
            self.ctx.model,
            # As the control group runs it, narrowing included (ADR 0074).
            self.ctx.step_model(InvestigationStep),
        )
        self.billed = (planner.billed_usage,)
        self.planner_input_tokens += planner.context_tokens
        self.planner_context_chars += planner.context_chars
        self.calls = (
            LLMCallRecord(
                role=_PLANNER_ROLE,
                model=self.ctx.model,
                # The ledger's own delta, the number ADR 0015 holds the run to.
                tokens_used=planned.budget.tokens_used - run_state.budget.tokens_used,
                usd_used=planned.budget.usd_used - run_state.budget.usd_used,
                input_tokens=planner.input_tokens,
                output_tokens=planner.output_tokens,
                cache_read_tokens=planner.cache_read_tokens,
                cache_creation_tokens=planner.cache_creation_tokens,
                call_id=planner.record_id,
                elapsed_ms=planner.elapsed_ms,
            ),
        )
        return planned, step, planner

    def _generate(self, run_state: RunState) -> CandidateGeneration:
        """Rung 1: one ``best_of_n_enumerated(4)`` call over the same ledger."""
        try:
            generation = self.strategy.generator.generate(run_state, self.at, self.ctx)
        except Exception as err:
            raise AdaptiveFailed(
                ENUMERATED_STAGE, err, sum_usage(*self.billed, usage_of(err))
            ) from err
        self.billed = (*self.billed, generation.billed_usage)
        self.calls = (*self.calls, *generation.record.llm_calls)
        self.rejections = (*self.rejections, *generation.record.generation_rejections)
        self.planner_input_tokens += generation.record.planner_input_tokens or 0
        self.planner_context_chars += generation.record.planner_context_chars or 0
        return generation

    def _select(
        self, run_state: RunState, candidates: Sequence[DiagnosisCandidate]
    ) -> tuple[SelectionResult, RunState, str]:
        """Rung 2: one selector call over the set rung 1 already paid for."""
        client = self.ctx.selector_llm_client
        if client is None:  # pragma: no cover - refused before the climb starts
            raise ValueError(NO_SELECTOR_CLIENT)
        try:
            call = select_candidate(
                client, run_state=run_state, candidates=candidates, model=self.ctx.model
            )
        except Exception as err:
            raise AdaptiveFailed(
                SELECTOR_STAGE, err, sum_usage(*self.billed, usage_of(err))
            ) from err
        self.billed = (*self.billed, _billed(call))
        after = run_state.model_copy(
            update={
                "budget": accrue_structured_call(run_state.budget, call, self.ctx.model),
                "updated_at": self.at,
            }
        )
        self.calls = (
            *self.calls,
            LLMCallRecord(
                role=SELECTOR_ROLE,
                model=self.ctx.model,
                tokens_used=after.budget.tokens_used - run_state.budget.tokens_used,
                usd_used=after.budget.usd_used - run_state.budget.usd_used,
                input_tokens=call.result.input_tokens,
                output_tokens=call.result.output_tokens,
                cache_read_tokens=call.result.cache_read_tokens,
                cache_creation_tokens=call.result.cache_creation_tokens,
                call_id=call.result.record_id,
                elapsed_ms=call.result.elapsed_ms,
            ),
        )
        return call.result.output, after, call.result.record_id

    def _search_rung(
        self,
        before: RunState,
        run_state: RunState,
        *,
        entered_because: frozenset[EscalationSignal],
    ) -> tuple[RunState, InvestigationStep, StepRecord]:
        """Rung 3, recorded mode: one bounded walk, its record folded into this step's.

        The walk runs with the sink taken off its context: one planner step produces exactly one
        ``StepRecord``, and this one is the ladder's.
        """
        spent = run_state.budget
        try:
            walked, step, record = self.strategy.search.plan_next_step(
                run_state, self.at, replace(self.ctx, record_step=None)
            )
        except Exception as err:
            raise AdaptiveFailed(SEARCH_STAGE, err, sum_usage(*self.billed, usage_of(err))) from err
        self.calls = (*self.calls, *record.llm_calls)
        self.rejections = (*self.rejections, *record.generation_rejections)
        self.planner_input_tokens += record.planner_input_tokens or 0
        self.planner_context_chars += record.planner_context_chars or 0
        self._note(
            Rung.SEARCH,
            entered_because=entered_because,
            signals=evaluate(
                walked,
                thresholds=self.strategy.thresholds,
                reading=reading_of(
                    (),
                    selector_uncertainty=None
                    if record.selector is None
                    else record.selector.uncertainty,
                ),
            ),
            spent=(spent, walked.budget),
            calls=len(record.llm_calls),
        )
        return self._handoff(
            before,
            walked,
            step,
            Rung.SEARCH,
            candidate_set=record.candidate_set,
            selector=record.selector,
            search=record.search,
        )

    def _escalate_rung(
        self,
        before: RunState,
        run_state: RunState,
        step: InvestigationStep,
        fired: FiredSignals,
        *,
        candidate_set: tuple[CandidateRecord, ...],
        selector: SelectorRecord | None,
    ) -> tuple[RunState, InvestigationStep, StepRecord]:
        """The tail where ``search`` cannot run: stop, and say which signals were still firing.

        A ``StopAction``, so the loop escalates with a briefing and no rung has acted. The
        ranking is the rung below's: the diagnosis stands, the decision does not.
        """
        self._note(
            Rung.ESCALATE,
            entered_because=fired.fired,
            signals=fired,
            spent=(run_state.budget, run_state.budget),
            calls=0,
        )
        reason = "; ".join((LADDER_EXHAUSTED, *fired.reasons, SEARCH_RUNG_UNAVAILABLE))
        stopped = InvestigationStep(
            hypotheses=step.hypotheses, next_action=StopAction(reason=reason)
        )
        return self._handoff(
            before,
            run_state,
            stopped,
            Rung.ESCALATE,
            candidate_set=candidate_set,
            selector=selector,
        )

    # --- the record -------------------------------------------------------

    def _note(
        self,
        rung: Rung,
        *,
        entered_because: frozenset[EscalationSignal],
        signals: FiredSignals,
        spent: tuple[BudgetLedger, BudgetLedger],
        calls: int,
    ) -> FiredSignals:
        """Record one rung: why it was entered, what it cost, what is still firing after it."""
        was, now = spent
        self.rungs = (
            *self.rungs,
            RungRecord(
                rung=rung.value,
                index=len(self.rungs),
                entered_because=_signal_names(entered_because),
                fired=_signal_names(signals.fired),
                unmeasured=_signal_names(signals.unmeasured),
                reasons=signals.reasons,
                llm_calls=calls,
                tokens_used=now.tokens_used - was.tokens_used,
                usd_used=now.usd_used - was.usd_used,
                tool_calls_used=now.tool_calls_used - was.tool_calls_used,
            ),
        )
        return signals

    def _handoff(
        self,
        before: RunState,
        after: RunState,
        step: InvestigationStep,
        terminated_on: Rung,
        *,
        candidate_set: tuple[CandidateRecord, ...],
        selector: SelectorRecord | None = None,
        search: SearchRecord | None = None,
    ) -> tuple[RunState, InvestigationStep, StepRecord]:
        """The rung that ended the climb, as the loop takes any other step."""
        last = len(self.rungs) - 1
        rungs = tuple(
            replace(rung, climbed=index < last, emitted=index == last)
            for index, rung in enumerate(self.rungs)
        )
        still_firing = set(rungs[last].fired) & set(_signal_names(UNCLEARABLE))
        record = StepRecord(
            run_id=str(before.incident_id),
            iteration=self.ctx.iteration,
            strategy=self.strategy.name,
            model=self.ctx.model,
            candidate_set=candidate_set,
            selector=selector,
            search=search,
            ladder=LadderRecord(
                ladder=tuple(rung.value for rung in self.ladder),
                terminated_on=terminated_on.value,
                rungs_used=len(rungs),
                # Every call the step made beyond the baseline rung's one. Zero is the
                # cheapness claim, and it is a subtraction rather than a sentence.
                extra_llm_calls=len(self.calls) - 1,
                search_available=self.ladder[-1] is Rung.SEARCH,
                unclearable=tuple(sorted(still_firing)),
                thresholds={
                    name.value: self.strategy.thresholds.value_of(name) for name in ThresholdName
                },
                rungs=rungs,
            ),
            emitted_step=step,
            hypothesis_state_before=before.hypotheses,
            hypothesis_state_after=after.hypotheses,
            llm_calls=self.calls,
            planner_input_tokens=self.planner_input_tokens,
            planner_context_chars=self.planner_context_chars,
            generation_rejections=self.rejections,
        )
        if self.ctx.record_step is not None:
            self.ctx.record_step(record)
        return after, step, record


def _signal_names(signals: frozenset[EscalationSignal]) -> tuple[str, ...]:
    """Signal values, sorted — two records of the same climb render the same order."""
    return tuple(sorted(signal.value for signal in signals))


def _baseline_candidate(step: InvestigationStep, generation_call_id: str) -> CandidateRecord:
    """The baseline rung's one candidate, as ``baseline`` records it."""
    action = step.next_action
    top = step.hypotheses[0]
    return CandidateRecord(
        category=top.category,
        name=top.name,
        confidence=top.confidence,
        proposed_probe=(action.tool_name if isinstance(action, ProbeAction) else None),
        generation_call_id=generation_call_id,
    )


def _generation_call_id(generation: CandidateGeneration) -> str:
    """The generation's own call id, off the record it already built."""
    candidates = generation.record.candidate_set
    return candidates[0].generation_call_id if candidates else ""


def _billed(call: RepairedCall[Any]) -> LLMUsage | None:
    """Everything one call billed: the leg that parsed, and each repair leg before it."""
    return sum_usage(*(usage_of(err) for err in call.failures), call.result)
