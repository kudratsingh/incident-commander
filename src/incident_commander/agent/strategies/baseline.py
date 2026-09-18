"""``baseline`` — the current behaviour, behind the seam and otherwise untouched.

``plan_next_step`` calls the private ``investigation._plan_next_step`` **verbatim** — one
planner call, wrapped by ``call_with_output_repair`` (ADR 0035) and accrued by
``accrue_structured_call`` (ADR 0015) — rather than copying it: the control group's whole value
is that it is not a re-implementation, and the canned suite comes out byte-identical (plan 04
working rule 5).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from types import MappingProxyType
from typing import Any, Final

from incident_commander.agent.hypothesis import InvestigationStep, ProbeAction
from incident_commander.agent.investigation import _plan_next_step
from incident_commander.agent.state import RunState
from incident_commander.agent.strategies.knobs import StrategyKnobs
from incident_commander.agent.strategies.names import StrategyName
from incident_commander.agent.strategies.protocol import StrategyContext
from incident_commander.agent.strategies.records import (
    CandidateRecord,
    LLMCallRecord,
    PlannerCall,
    StepRecord,
)

#: The planner call's prompt role, and the label the tracer writes its LLM records with. One
#: string, so a record's ``role`` and the trace's ``role`` cannot drift apart.
_PLANNER_ROLE: Final[str] = "investigation_planner"

#: Nothing to configure. Read-only so a caller cannot fill the empty block and have it stamped
#: into a provenance record as if it were a setting.
_NO_CONFIG: Final[Mapping[str, Any]] = MappingProxyType({})


class BaselineStrategy:
    """The control group every later strategy is measured against."""

    name: str = StrategyName.BASELINE.value
    config: Mapping[str, Any] = _NO_CONFIG

    def __init__(self, knobs: StrategyKnobs | None = None) -> None:
        """Takes the inference block every registry factory is handed, and reads nothing.

        The parameter exists so ``StrategyRegistry`` has one factory shape; ``baseline`` has no
        knob, since one that changed it would make it a different arm.
        """

    def plan_next_step(
        self, run_state: RunState, at: datetime, ctx: StrategyContext
    ) -> tuple[RunState, InvestigationStep, StepRecord]:
        """One planner call, plus the record of it.

        Exceptions propagate as before the seam: the loop's ``except`` arm charges what the
        failed call billed and escalates (ADR 0015, ADR 0035).
        """
        updated, step, call = _plan_next_step(run_state, at, ctx.llm_client, ctx.model)
        record = self._record(run_state, updated, step, call, ctx)
        if ctx.record_step is not None:
            ctx.record_step(record)
        return updated, step, record

    def _record(
        self,
        before: RunState,
        after: RunState,
        step: InvestigationStep,
        call: PlannerCall,
        ctx: StrategyContext,
    ) -> StepRecord:
        """Build the step's ``StepRecord``.

        One candidate — the planner's top hypothesis, the whole of what ``baseline``
        considered; the rest of the ranking is ``hypothesis_state_after``.
        """
        action = step.next_action
        top = step.hypotheses[0]
        candidate = CandidateRecord(
            category=top.category,
            name=top.name,
            confidence=top.confidence,
            proposed_probe=(action.tool_name if isinstance(action, ProbeAction) else None),
            # One call generated the whole ranking, so the one candidate names it.
            generation_call_id=call.record_id,
        )
        return StepRecord(
            run_id=str(before.incident_id),
            iteration=ctx.iteration,
            strategy=self.name,
            model=ctx.model,
            candidate_set=(candidate,),
            # No selector: with one candidate there is nothing to select
            # between (plan 02 § 7 — "null for baseline").
            selector=None,
            emitted_step=step,
            hypothesis_state_before=before.hypotheses,
            hypothesis_state_after=after.hypotheses,
            llm_calls=(
                LLMCallRecord(
                    role=_PLANNER_ROLE,
                    model=ctx.model,
                    # The ledger's own delta, the number ADR 0015 holds the run to: it includes
                    # billed-then-discarded attempts, which the counters below cannot see.
                    tokens_used=after.budget.tokens_used - before.budget.tokens_used,
                    usd_used=after.budget.usd_used - before.budget.usd_used,
                    input_tokens=call.input_tokens,
                    output_tokens=call.output_tokens,
                    cache_read_tokens=call.cache_read_tokens,
                    cache_creation_tokens=call.cache_creation_tokens,
                    call_id=call.record_id,
                    # Filled since WO-R3-260, from the client's own stopwatch. ``None`` is a
                    # real answer (a canned client does not time itself); never invent one.
                    elapsed_ms=call.elapsed_ms,
                ),
            ),
            planner_input_tokens=call.context_tokens,
            planner_context_chars=call.context_chars,
        )
