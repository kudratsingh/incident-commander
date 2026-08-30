"""LLM-generated findings and recommendation for an EscalationBriefing.

The deterministic template in ``briefing.py`` produces everything up to
``findings`` and ``recommendation``. This module fills those with an LLM call
using the ``briefing_writer`` prompt. The template shape stays authoritative —
the LLM only writes into the two free-form strings.

**Enrichment is eval-only, on purpose.** ``evals/runner.py`` is the only
caller; the service path (``api/app.py``) renders the deterministic briefing
and stops. That used to mean production shipped an emptier artifact than the
one the eval graded, which is why it is now written down rather than assumed:
see "Handoff artifact" in ``docs/safety-model.md``. The load-bearing facts —
why the agent stopped, and which Tier-1 action already fired — are
deterministic fields on ``EscalationBriefing``, so a production briefing is
complete without an LLM. ``findings`` and ``recommendation`` are prose *about*
those facts, and buying them costs an LLM call, a key, and a failure rail on
the incident path. ``tests/unit/test_briefing_enrichment.py`` pins the
consequence: the two paths differ in exactly those two strings and nothing
else.
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
    # The reason and the attempted action are the two facts the handoff
    # exists to deliver. They are deterministic fields, so the writer is
    # summarizing them, never inventing them — and a writer that never saw
    # the attempted action can recommend re-running it.
    if briefing.escalation_reason:
        lines.append(f"Why the run ended: {briefing.escalation_reason}")
    if briefing.attempted_action is not None:
        lines.append(
            f"Tier-1 action ALREADY ATTEMPTED (do not recommend repeating it "
            f"without checking its effect first): {briefing.attempted_action.tool} "
            f"{briefing.attempted_action.arguments}"
        )
    if briefing.investigation_trail:
        lines.append("Investigation trail:")
        for probe in briefing.investigation_trail:
            lines.append(f"  - {probe.tool}: {probe.summary}")
    else:
        lines.append("No probes were run before escalation.")
    lines.append(f"Budget used: {briefing.budget_used}")
    return "\n".join(lines)
