"""LLM-generated ``findings`` and ``recommendation`` for an EscalationBriefing.

Fills the two free-form strings of ``briefing.py``'s deterministic template using the
``briefing_writer`` prompt. **Eval-only on purpose**: ``evals/runner.py`` is the only
caller, and a production briefing is complete without it — the load-bearing fields are
deterministic (``tests/unit/test_briefing_enrichment.py`` pins the difference).
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

    One bounded repair (ADR 0035), both legs accrued. Metered as the agent's own
    cost but never a gate (ADR 0015 § 4) — it runs after the terminal state.
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
    """What the briefing writer is shown.

    How the run ended, why it stopped, any Tier-1 action already attempted, the causes it
    named and the remainder it left (WP-11.3), the investigation trail, and what it spent.
    """
    lines = [
        f"Incident {briefing.incident_id}",
        f"Final state: {briefing.final_state.value}",
        f"Alert: {briefing.alert_summary}",
    ]
    # Deterministic fields the writer summarizes, never invents; a writer blind
    # to the attempted action would re-recommend it.
    if briefing.escalation_reason:
        lines.append(f"Why the run ended: {briefing.escalation_reason}")
    if briefing.attempted_action is not None:
        lines.append(
            f"Tier-1 action ALREADY ATTEMPTED (do not recommend repeating it "
            f"without checking its effect first): {briefing.attempted_action.tool} "
            f"{briefing.attempted_action.arguments}"
        )
    # The slots come before the trail: they are what the run concluded about the trail, and
    # the remainder block is the one part of the handoff the writer may not contradict.
    lines.extend(render_incidents(briefing.incidents))
    # Beside the slots and for the same reason (ADR 0071): whose recovery this was is run
    # state, so the writer is shown it rather than asked to infer it from the trail.
    lines.extend(render_attribution(briefing.attribution))
    lines.extend(render_trail(briefing.investigation_trail))
    lines.append(f"Budget used: {briefing.budget_used}")
    return "\n".join(lines)
