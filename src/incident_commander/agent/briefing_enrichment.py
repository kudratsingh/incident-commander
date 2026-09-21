"""LLM-generated ``findings`` and ``recommendation`` for an EscalationBriefing.

Fills the two free-form strings of ``briefing.py``'s template from the ``briefing_writer``
prompt. Eval-only on purpose: a production briefing is complete without it, since the
load-bearing fields are deterministic.
"""

from __future__ import annotations

from pydantic import ConfigDict, Field

from incident_commander.agent.accounting import accrue_structured_call
from incident_commander.agent.briefing import (
    EscalationBriefing,
    render_attribution,
    render_incidents,
    render_trail,
)
from incident_commander.agent.state import BudgetLedger
from incident_commander.llm.client import LLMClientProtocol
from incident_commander.llm.prompts.loader import load_prompt
from incident_commander.llm.repair import call_with_output_repair
from incident_commander.llm.structured import StructuredOutput


class BriefingContent(StructuredOutput):
    """LLM-produced portion of the briefing. Validated by the tool-use schema."""

    model_config = ConfigDict(extra="forbid")

    findings: str = Field(min_length=1)
    recommendation: str = Field(min_length=1)


def enrich_briefing(
    briefing: EscalationBriefing,
    llm_client: LLMClientProtocol,
    model: str,
    *,
    budget: BudgetLedger,
) -> tuple[EscalationBriefing, BudgetLedger]:
    """Fill ``findings`` and ``recommendation`` by LLM; return the briefing and ledger.

    One bounded repair (ADR 0035), both legs accrued. Metered but never a gate — it runs
    after the terminal state (ADR 0015 § 4).
    """
    call = call_with_output_repair(
        llm_client,
        system_prompt=load_prompt("briefing_writer"),
        user_message=_format_context(briefing),
        output_model=BriefingContent,
        model=model,
    )
    enriched = briefing.model_copy(
        update={
            "findings": call.result.output.findings,
            "recommendation": call.result.output.recommendation,
        }
    )
    return enriched, accrue_structured_call(budget, call, model)


def _format_context(briefing: EscalationBriefing) -> str:
    """What the briefing writer is shown: how the run ended, any Tier-1 action already
    attempted, the causes named and the remainder left (WP-11.3), the trail, the spend."""
    lines = [
        f"Incident {briefing.incident_id}",
        f"Final state: {briefing.final_state.value}",
        f"Alert: {briefing.alert_summary}",
    ]
    # Deterministic fields the writer summarizes, never invents: a writer blind to the
    # attempted action would re-recommend it.
    if briefing.escalation_reason:
        lines.append(f"Why the run ended: {briefing.escalation_reason}")
    if briefing.attempted_action is not None:
        lines.append(
            f"Tier-1 action ALREADY ATTEMPTED (do not recommend repeating it "
            f"without checking its effect first): {briefing.attempted_action.tool} "
            f"{briefing.attempted_action.arguments}"
        )
    # Slots before the trail: the remainder block is the one part of the handoff the writer
    # may not contradict.
    lines.extend(render_incidents(briefing.incidents))
    # Whose recovery this was is run state, so the writer is shown it rather than asked to
    # infer it from the trail (ADR 0071).
    lines.extend(render_attribution(briefing.attribution))
    lines.extend(render_trail(briefing.investigation_trail))
    lines.append(f"Budget used: {briefing.budget_used}")
    return "\n".join(lines)
