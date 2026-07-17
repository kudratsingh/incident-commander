"""LLM-generated findings and recommendation for an EscalationBriefing.

The deterministic template in ``briefing.py`` produces everything up to
``findings`` and ``recommendation``. This module fills those with an LLM call
using the ``briefing_writer`` prompt. The template shape stays authoritative —
the LLM only writes into the two free-form strings.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from incident_commander.agent.briefing import EscalationBriefing
from incident_commander.llm.client import LLMClientProtocol
from incident_commander.llm.prompts.loader import load_prompt


class BriefingContent(BaseModel):
    """LLM-produced portion of the briefing. Validated by the tool-use schema."""

    model_config = ConfigDict(extra="forbid")

    findings: str = Field(min_length=1)
    recommendation: str = Field(min_length=1)


def enrich_briefing(
    briefing: EscalationBriefing,
    llm_client: LLMClientProtocol,
    model: str,
) -> EscalationBriefing:
    """Return a new briefing with ``findings`` and ``recommendation`` filled by an LLM."""
    result = llm_client.call(
        system_prompt=load_prompt("briefing_writer"),
        user_message=_format_context(briefing),
        output_model=BriefingContent,
        model=model,
    )
    return briefing.model_copy(
        update={
            "findings": result.output.findings,
            "recommendation": result.output.recommendation,
        }
    )


def _format_context(briefing: EscalationBriefing) -> str:
    lines = [
        f"Incident {briefing.incident_id}",
        f"Final state: {briefing.final_state.value}",
        f"Alert: {briefing.alert_summary}",
    ]
    if briefing.investigation_trail:
        lines.append("Investigation trail:")
        for probe in briefing.investigation_trail:
            lines.append(f"  - {probe.tool}: {probe.summary}")
    else:
        lines.append("No probes were run before escalation.")
    lines.append(f"Budget used: {briefing.budget_used}")
    return "\n".join(lines)
