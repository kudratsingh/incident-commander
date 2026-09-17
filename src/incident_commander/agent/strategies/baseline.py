"""``baseline`` — the current behaviour, behind the seam and otherwise untouched.

``plan_next_step`` calls ``investigation._plan_next_step`` **verbatim**: one
planner call, wrapped by ``call_with_output_repair`` (ADR 0035) and accrued by
``accrue_structured_call`` (ADR 0015), returning the same ``RunState`` and the
same ``InvestigationStep`` the loop has always received. No prompt, no context
rendering, no schema and no retry policy is re-implemented here — a second copy
of that body would be a second thing to keep in step with the first, and the
whole value of ``baseline`` is that it is not a re-implementation. It is the
control group: the canned suite comes out byte-identical, which is the proof
that nothing moved (plan 04 working rule 5).

Importing the private ``_plan_next_step`` is deliberate. The alternative — move
the body here — would take the planner call out of the module that owns the
loop, the gates and the re-probe, for no behavioural gain, and it would put a
packet whose acceptance is "byte-identical" in the business of moving code the
live campaign's eight green runs were made with.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from types import MappingProxyType
from typing import Any, Final

from incident_commander.agent.hypothesis import InvestigationStep, ProbeAction
from incident_commander.agent.investigation import _plan_next_step
from incident_commander.agent.state import RunState
from incident_commander.agent.strategies.names import StrategyName
from incident_commander.agent.strategies.protocol import StrategyContext
from incident_commander.agent.strategies.records import (
    CandidateRecord,
    LLMCallRecord,
    PlannerCall,
    StepRecord,
)

#: The prompt role the planner call is made under, and the label the tracer
#: writes its LLM records with (``evals/runner.py``'s ``llm_hook``). One string,
#: so a record's ``role`` and the trace's ``role`` cannot drift apart.
_PLANNER_ROLE: Final[str] = "investigation_planner"

#: Nothing to configure. Read-only so the empty block cannot be filled in by a
#: caller and then stamped into a provenance record as if it were a setting.
_NO_CONFIG: Final[Mapping[str, Any]] = MappingProxyType({})


class BaselineStrategy:
    """The one strategy that exists today, and the control every later one is
    measured against."""

    name: str = StrategyName.BASELINE.value
    config: Mapping[str, Any] = _NO_CONFIG

    def plan_next_step(
        self, run_state: RunState, at: datetime, ctx: StrategyContext
    ) -> tuple[RunState, InvestigationStep, StepRecord]:
        """One planner call, plus the record of it.

        Exceptions propagate exactly as they did before the seam existed: the
        loop's own ``except (ValueError, ValidationError, LLMError)`` arm
        charges what the failed call billed and escalates (ADR 0015, ADR 0035).
        Catching anything here would move that accounting behind a strategy,
        where each new strategy would have to remember to repeat it.
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

        The candidate set holds exactly one entry: the hypothesis the planner
        put on top, which for ``baseline`` is the whole of what it considered —
        one call, one ranking, no alternatives generated and none discarded.
        The rest of the ranking is not dropped; it is what
        ``hypothesis_state_after`` is. A best-of-N strategy is the one that
        makes this set longer, and the difference between the two lengths is the
        measurement Phases 5 and 6 are built on.

        The numbers come from ``call`` — the planner call's own report — not
        from anything rebuilt here. ``planner_input_tokens`` is the provider's
        count of the context it was fed (zero on a canned run, which bills
        nothing), and ``planner_context_chars`` is that same context measured
        locally, so an offline record is not silently all-zero.
        """
        action = step.next_action
        top = step.hypotheses[0]
        candidate = CandidateRecord(
            category=top.category,
            name=top.name,
            confidence=top.confidence,
            proposed_probe=(action.tool_name if isinstance(action, ProbeAction) else None),
            # One call generated the whole ranking, so the one candidate names
            # it. For a strategy that generates candidates in separate calls
            # this is what says which call produced which candidate.
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
                    # The ledger's own delta across the call — which is the
                    # number ADR 0015 holds the run to. It already includes a
                    # repair's second call and any billed-then-discarded
                    # attempt, which the counters below cannot see.
                    tokens_used=after.budget.tokens_used - before.budget.tokens_used,
                    usd_used=after.budget.usd_used - before.budget.usd_used,
                    input_tokens=call.input_tokens,
                    output_tokens=call.output_tokens,
                    cache_read_tokens=call.cache_read_tokens,
                    cache_creation_tokens=call.cache_creation_tokens,
                    call_id=call.record_id,
                ),
            ),
            planner_input_tokens=call.context_tokens,
            planner_context_chars=call.context_chars,
        )
