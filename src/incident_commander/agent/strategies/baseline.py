"""``baseline`` — the current behaviour, behind the seam and otherwise untouched.

``plan_next_step`` calls ``investigation._plan_next_step`` **verbatim** rather than copying it:
the control group's whole value is that it is not a re-implementation, and the canned suite
comes out byte-identical.
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

#: The prompt this arm asks with, and the label the tracer files its calls under. One string for
#: both, so the record's role and the trace's role cannot come to disagree.
_PLANNER_ROLE: Final[str] = "investigation_planner"

#: This arm has nothing to configure. Read-only, so a caller cannot put something in the empty
#: block and have it stored with the run as though it were a real setting.
_NO_CONFIG: Final[Mapping[str, Any]] = MappingProxyType({})


class BaselineStrategy:
    """The control group every later strategy is measured against."""

    name: str = StrategyName.BASELINE.value
    config: Mapping[str, Any] = _NO_CONFIG

    def __init__(self, knobs: StrategyKnobs | None = None) -> None:
        """Takes the inference block every registry factory is handed, and reads nothing: one
        factory shape for ``StrategyRegistry``. A knob here would make it a different arm."""

    def plan_next_step(
        self, run_state: RunState, at: datetime, ctx: StrategyContext
    ) -> tuple[RunState, InvestigationStep, StepRecord]:
        """One planner call, plus the record of it. Exceptions propagate: the loop's ``except``
        arm charges what the failed call billed and escalates (ADR 0015)."""
        updated, step, call = _plan_next_step(
            run_state,
            at,
            ctx.llm_client,
            ctx.model,
            # The full step schema, unless the loop withdrew the probe option for this step;
            # this arm never makes that decision itself.
            ctx.step_model(InvestigationStep),
        )
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
        """Build the step's ``StepRecord``: one candidate, the top hypothesis — the whole of
        what ``baseline`` considered. The rest of the ranking is ``hypothesis_state_after``."""
        action = step.next_action
        top = step.hypotheses[0]
        candidate = CandidateRecord(
            category=top.category,
            name=top.name,
            confidence=top.confidence,
            proposed_probe=(action.tool_name if isinstance(action, ProbeAction) else None),
            # A single call produced the whole ranking, so this one candidate records that
            # call's id as the call that generated it.
            generation_call_id=call.record_id,
        )
        return StepRecord(
            run_id=str(before.incident_id),
            iteration=ctx.iteration,
            strategy=self.name,
            model=ctx.model,
            candidate_set=(candidate,),
            # No selection was made: with one candidate there is nothing to choose between.
            selector=None,
            emitted_step=step,
            hypothesis_state_before=before.hypotheses,
            hypothesis_state_after=after.hypotheses,
            llm_calls=(
                LLMCallRecord(
                    role=_PLANNER_ROLE,
                    model=ctx.model,
                    # What the run's own budget moved by, which is the number the budget rules
                    # hold a run to: it includes attempts that were billed and then rejected.
                    tokens_used=after.budget.tokens_used - before.budget.tokens_used,
                    usd_used=after.budget.usd_used - before.budget.usd_used,
                    input_tokens=call.input_tokens,
                    output_tokens=call.output_tokens,
                    cache_read_tokens=call.cache_read_tokens,
                    cache_creation_tokens=call.cache_creation_tokens,
                    call_id=call.record_id,
                    # How long the call took, as the LLM client measured it. ``None`` is a real
                    # answer — an offline client does not time itself — so never fill one in.
                    elapsed_ms=call.elapsed_ms,
                ),
            ),
            planner_input_tokens=call.context_tokens,
            planner_context_chars=call.context_chars,
        )
