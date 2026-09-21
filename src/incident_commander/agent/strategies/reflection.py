"""``reflection`` — ``baseline``'s planner call, one critique, at most one revision (WP-9.1).

The first call is ``investigation._plan_next_step`` VERBATIM, so this arm is the control group
plus a pass. The cap is structural — one ``RevisionPass`` token, no loop over the critic — because
a bound in the critic's instructions fails open. Revision is not authorization: every gate runs.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from types import MappingProxyType
from typing import Any, Final

from incident_commander.agent.accounting import accrue_structured_call
from incident_commander.agent.hypothesis import InvestigationStep, ProbeAction
from incident_commander.agent.investigation import _plan_next_step
from incident_commander.agent.reflection import (
    CRITIC_ROLE,
    MAX_REVISION_PASSES,
    RevisionPass,
    RevisionVerdict,
    StepCritique,
    critique_step,
    format_revision_context,
    revise_step,
    revision_system_prompt,
)
from incident_commander.agent.state import RunState
from incident_commander.agent.strategies.knobs import StrategyKnobs
from incident_commander.agent.strategies.names import StrategyName
from incident_commander.agent.strategies.protocol import StrategyContext
from incident_commander.agent.strategies.records import (
    CandidateRecord,
    LLMCallRecord,
    PlannerCall,
    RevisionRecord,
    StepRecord,
)
from incident_commander.llm.client import LLMError, LLMUsage
from incident_commander.llm.repair import RepairedCall, sum_usage, usage_of

#: Same role, and so the same trace label, as ``baseline``'s planner call — for BOTH of this
#: arm's planner calls. A split by call index would stop the token total being comparable.
_PLANNER_ROLE: Final[str] = "investigation_planner"

#: Named so a test asserts the guard's own marker rather than that something raised (F-007).
NO_CRITIC_CLIENT: Final[str] = "no critic client is on the strategy context"

#: Which call of the step failed, for ``ReflectionFailed``'s message.
CRITIC_STAGE: Final[str] = "critic"
REVISION_STAGE: Final[str] = "revision"


class ReflectionFailed(LLMError):
    """A call after the first one failed, and the step's whole bill comes with it.

    An ``LLMError`` so the loop's existing ``except`` arm escalates as for ``baseline``;
    ``usage`` sums every billed leg.
    """

    def __init__(self, stage: str, cause: BaseException, usage: LLMUsage | None) -> None:
        super().__init__(
            f"reflection {stage} call failed: {cause}",
            usage=usage,
            record_id=getattr(cause, "record_id", None),
        )
        self.stage = stage
        self.cause = cause


class ReflectionStrategy:
    """One planner call, one critique, at most one revision — per step, capped in code."""

    name: str = StrategyName.REFLECTION.value

    def __init__(self, knobs: StrategyKnobs | None = None) -> None:
        """Takes the inference block every registry factory is handed, and reads nothing: the
        pass count is the safety property, and a cap an operator can raise is not a cap."""
        self.config: Mapping[str, Any] = MappingProxyType(
            {
                "passes": MAX_REVISION_PASSES,
                # How the bound is held, so no row has to trust the number above.
                "cap": "structural",
                "critic": CRITIC_ROLE,
                # Of the PLANNER's view (ADR 0044): both turns get ``baseline``'s context. The
                # critic does see ids, which is why that is a separate key.
                "evidence_ids_rendered": False,
                "critic_sees_evidence_ids": True,
            }
        )

    def plan_next_step(
        self, run_state: RunState, at: datetime, ctx: StrategyContext
    ) -> tuple[RunState, InvestigationStep, StepRecord]:
        """Plan, critique, and revise once if the critique found something.

        Straight-line: the single pass is taken through a ``RevisionPass``, so a later edit that
        added a loop would raise instead of billing again.
        """
        if ctx.critic_llm_client is None:
            raise ValueError(
                f"{NO_CRITIC_CLIENT}. The critic is its own metered role "
                "(agent/reflection.CRITIC_ROLE); falling back to the planner's client "
                "would report the critique's tokens under the planner's role, and "
                '"added tokens" is the number this arm exists to report.'
            )
        step_model = ctx.step_model(InvestigationStep)
        planned, initial_step, planner = _plan_next_step(
            run_state, at, ctx.llm_client, ctx.model, step_model
        )
        budget = RevisionPass()
        try:
            critic = critique_step(
                ctx.critic_llm_client, run_state=run_state, step=initial_step, model=ctx.model
            )
        except Exception as err:
            raise ReflectionFailed(
                CRITIC_STAGE, err, sum_usage(planner.billed_usage, usage_of(err))
            ) from err
        critique = critic.result.output
        after_critic = planned.model_copy(
            update={
                # The same function every other call is charged through, so a repaired
                # critique bills both legs (ADR 0015).
                "budget": accrue_structured_call(planned.budget, critic, ctx.model),
                "updated_at": at,
            }
        )
        if critique.verdict is RevisionVerdict.KEEP:
            record = self._record(
                before=run_state,
                planned=planned,
                after_critic=after_critic,
                after=after_critic,
                emitted=initial_step,
                initial_step=initial_step,
                planner=planner,
                critic=critic,
                revision=None,
                revision_context="",
                budget=budget,
                ctx=ctx,
            )
            if ctx.record_step is not None:
                ctx.record_step(record)
            return after_critic, initial_step, record
        budget.spend()
        # A local, not inline: the record measures the string that was SENT.
        revision_context = format_revision_context(run_state, initial_step, critique)
        try:
            revision = revise_step(
                ctx.llm_client,
                user_message=revision_context,
                model=ctx.model,
                # Same schema as the first call: a narrowing cannot lapse because a critic
                # spoke (ADR 0074).
                output_model=step_model,
            )
        except Exception as err:
            raise ReflectionFailed(
                REVISION_STAGE,
                err,
                sum_usage(planner.billed_usage, _billed(critic), usage_of(err)),
            ) from err
        revised_step = revision.result.output
        updated = after_critic.model_copy(
            update={
                "budget": accrue_structured_call(after_critic.budget, revision, ctx.model),
                "hypotheses": revised_step.hypotheses,
                "updated_at": at,
            }
        )
        record = self._record(
            before=run_state,
            planned=planned,
            after_critic=after_critic,
            after=updated,
            emitted=revised_step,
            initial_step=initial_step,
            planner=planner,
            critic=critic,
            revision=revision,
            revision_context=revision_context,
            budget=budget,
            ctx=ctx,
        )
        if ctx.record_step is not None:
            ctx.record_step(record)
        return updated, revised_step, record

    def _record(
        self,
        *,
        before: RunState,
        planned: RunState,
        after_critic: RunState,
        after: RunState,
        emitted: InvestigationStep,
        initial_step: InvestigationStep,
        planner: PlannerCall,
        critic: RepairedCall[StepCritique],
        revision: RepairedCall[InvestigationStep] | None,
        revision_context: str,
        budget: RevisionPass,
        ctx: StrategyContext,
    ) -> StepRecord:
        """One record per step, carrying both steps and every call the step billed.

        ``candidate_set`` is the EMITTED step's top hypothesis, so this arm's ``pass@1`` means
        what it means for ``baseline``; the replaced step is under ``revision``.
        """
        critique = critic.result.output
        top = emitted.hypotheses[0]
        action = emitted.next_action
        revised = revision is not None
        calls: tuple[LLMCallRecord, ...] = (
            LLMCallRecord(
                role=_PLANNER_ROLE,
                model=ctx.model,
                tokens_used=planned.budget.tokens_used - before.budget.tokens_used,
                usd_used=planned.budget.usd_used - before.budget.usd_used,
                input_tokens=planner.input_tokens,
                output_tokens=planner.output_tokens,
                cache_read_tokens=planner.cache_read_tokens,
                cache_creation_tokens=planner.cache_creation_tokens,
                call_id=planner.record_id,
                elapsed_ms=planner.elapsed_ms,
            ),
            LLMCallRecord(
                role=CRITIC_ROLE,
                model=ctx.model,
                tokens_used=after_critic.budget.tokens_used - planned.budget.tokens_used,
                usd_used=after_critic.budget.usd_used - planned.budget.usd_used,
                input_tokens=critic.result.input_tokens,
                output_tokens=critic.result.output_tokens,
                cache_read_tokens=critic.result.cache_read_tokens,
                cache_creation_tokens=critic.result.cache_creation_tokens,
                call_id=critic.result.record_id,
                elapsed_ms=critic.result.elapsed_ms,
            ),
        )
        if revision is not None:
            calls = (
                *calls,
                LLMCallRecord(
                    role=_PLANNER_ROLE,
                    model=ctx.model,
                    tokens_used=after.budget.tokens_used - after_critic.budget.tokens_used,
                    usd_used=after.budget.usd_used - after_critic.budget.usd_used,
                    input_tokens=revision.result.input_tokens,
                    output_tokens=revision.result.output_tokens,
                    cache_read_tokens=revision.result.cache_read_tokens,
                    cache_creation_tokens=revision.result.cache_creation_tokens,
                    call_id=revision.result.record_id,
                    elapsed_ms=revision.result.elapsed_ms,
                ),
            )
        return StepRecord(
            run_id=str(before.incident_id),
            iteration=ctx.iteration,
            strategy=self.name,
            model=ctx.model,
            candidate_set=(
                CandidateRecord(
                    category=top.category,
                    name=top.name,
                    confidence=top.confidence,
                    proposed_probe=(action.tool_name if isinstance(action, ProbeAction) else None),
                    generation_call_id=(
                        revision.result.record_id if revision is not None else planner.record_id
                    ),
                ),
            ),
            # No selector: this arm revises one diagnosis, it does not choose between several.
            selector=None,
            revision=RevisionRecord(
                initial_step=initial_step,
                verdict=critique.verdict.value,
                findings=critique.findings,
                contradicted_evidence_ids=tuple(
                    str(value) for value in critique.contradicted_evidence_ids
                ),
                revised=revised,
                passes_used=budget.spent,
                passes_allowed=budget.allowed,
                critic_call_id=critic.result.record_id,
                revision_call_id="" if revision is None else revision.result.record_id,
            ),
            emitted_step=emitted,
            hypothesis_state_before=before.hypotheses,
            hypothesis_state_after=after.hypotheses,
            llm_calls=calls,
            # BOTH planner turns, summed: a revised step fed the planner twice. The per-turn
            # split is in ``llm_calls``.
            planner_input_tokens=(
                planner.context_tokens
                + (
                    0
                    if revision is None
                    else (
                        revision.result.input_tokens
                        + revision.result.cache_read_tokens
                        + revision.result.cache_creation_tokens
                    )
                )
            ),
            planner_context_chars=(
                planner.context_chars
                + (0 if revision is None else len(revision_system_prompt()) + len(revision_context))
            ),
        )


def _billed(call: RepairedCall[Any]) -> LLMUsage | None:
    """Everything one call billed: the leg that parsed, and each repair leg before it."""
    return sum_usage(*(usage_of(err) for err in call.failures), call.result)
