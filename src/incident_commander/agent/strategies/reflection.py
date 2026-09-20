"""``reflection`` — ``baseline``'s planner call, one critique of it, at most one revision (WP-9.1).

Plan 02 § 13. The first call is ``investigation._plan_next_step`` **verbatim**, so this arm is the
control group plus a pass rather than a re-implementation; then one ``reflection_critic`` call over
the step it produced; then, only if the critique named a finding, ONE more planner call carrying
that critique. **The cap is structural** — a ``RevisionPass`` token spent once, and no loop over
the critic — because a bound stated in the critic's instructions is a bound that fails open.

Both steps reach the ``StepRecord`` (``RevisionRecord.initial_step`` beside ``emitted_step``),
which is what lets the report attribute a fix or a HARM to the pass. The revised step is an
ordinary ``InvestigationStep``, so the ``FIX_MAP`` gate, the 0.7 threshold, ADR 0041's whole-queue
refusal, the ADR-0009 re-probe and ``_execute_probe``'s tier re-check all run on it unchanged in
``investigation.py``: **revision is not authorization** (plan 02 § 18). The critic may name only a
``ReadToolName`` and never an action, and both planner turns get ``baseline``'s context bytes, so
the planner's view differs from the control group's by the appended critique alone.

Cost is declared, not discovered: up to two planner calls and one critic call per step, metered
apart by role (WP-2.3), with the ledger seeded by ``TOKEN_BUDGET_MULTIPLIER`` in
``agent/factory.py`` and nowhere else.
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

#: Same role string, and so the same trace label, as ``baseline``'s planner call — for BOTH of
#: this arm's planner calls. A split by call index would stop the token total being comparable.
_PLANNER_ROLE: Final[str] = "investigation_planner"

#: Named so a test asserts the guard's own marker rather than that something raised (F-007).
NO_CRITIC_CLIENT: Final[str] = "no critic client is on the strategy context"

#: Which call of the step failed, for ``ReflectionFailed``'s message.
CRITIC_STAGE: Final[str] = "critic"
REVISION_STAGE: Final[str] = "revision"


class ReflectionFailed(LLMError):
    """A call after the first one failed, and the step's whole bill comes with it.

    An ``LLMError`` subclass so the loop's existing ``except`` arm escalates as for
    ``baseline``. ``usage`` sums every billed leg — same shape and reason as
    ``best_of_n_sampled.SampledPlannerFailed`` and ``candidate_selector.SelectorFailed``.
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
        """Takes the inference block every registry factory is handed, and reads nothing.

        There is no knob: the pass count is the safety property, and a cap an operator can
        raise is not a cap. What ran is stamped instead, so a report row carries the bound.
        """
        self.config: Mapping[str, Any] = MappingProxyType(
            {
                "passes": MAX_REVISION_PASSES,
                # How the bound is held, so no row has to trust the number above.
                "cap": "structural",
                "critic": CRITIC_ROLE,
                # ADR 0044, of the PLANNER's view: both planner turns get ``baseline``'s
                # context. The critic does see ids, which is why that is stated separately.
                "evidence_ids_rendered": False,
                "critic_sees_evidence_ids": True,
            }
        )

    def plan_next_step(
        self, run_state: RunState, at: datetime, ctx: StrategyContext
    ) -> tuple[RunState, InvestigationStep, StepRecord]:
        """Plan, critique, and revise once if the critique found something.

        Straight-line: no loop over the critic, and the single pass is taken through a
        ``RevisionPass`` so a later edit that added one would raise instead of billing again.
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
                # Charged through the same function every other call is charged through, so a
                # repaired critique bills both legs (ADR 0015).
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
        # A local, not inline: the record measures the string that was sent, and a second
        # render could differ from the string the model saw.
        revision_context = format_revision_context(run_state, initial_step, critique)
        try:
            revision = revise_step(
                ctx.llm_client,
                user_message=revision_context,
                model=ctx.model,
                # The revision is the same step under the same schema: a narrowing the
                # first call was held to cannot lapse because a critic spoke (ADR 0074).
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
        what it means for ``baseline``; the step that was replaced is under ``revision``. Each
        call's ledger figures are that call's own delta, so three calls in one step stay apart.
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
            # BOTH planner turns, summed: the field is the context this step fed the planner,
            # and a revised step fed it twice. The per-turn split is in ``llm_calls``.
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
