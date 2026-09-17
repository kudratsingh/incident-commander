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

from pydantic import ConfigDict, Field

from incident_commander.agent.accounting import accrue_structured_call
from incident_commander.agent.briefing import EscalationBriefing, render_trail
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
    """The briefing with ``findings`` and ``recommendation`` filled by an LLM,
    and the run ledger with what that cost added to it.

    Gets the same one bounded repair as the two planners (ADR 0035), and both
    legs are accrued the same way they are (``accrue_structured_call``).

    **This is the agent's own cost and it is metered** (WO-R3-260, ADR 0015 § 4
    as amended). It is written prose the handoff carries, bought with the
    agent's model on the agent's behalf, and leaving it out made every
    cost-per-run comparison undercount the agent by exactly one call. It is
    **never a gate**: enrichment runs after the state machine has reached a
    terminal state, so there is no ``is_exhausted`` check left for a ceiling to
    trip, and the caller keeps this ledger beside the graded run rather than
    putting it back on ``RunState`` — a post-terminal charge that reached the
    graded state would be a budget dimension deciding an outcome the agent had
    already finished.

    Returning the ledger rather than taking a mutable one is the same shape
    ``investigation._plan_next_step`` uses: the charge is visible in the
    caller's own code, so a call site that forgets it is a type error rather
    than a silent under-report.

    On a raised call — a transport failure, or a repair that exhausted — the
    caller charges what the failure itself billed with ``accrue_llm_error``,
    exactly as the investigation loop does. Nothing is swallowed here.
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

    How the run ended, why it stopped, any Tier-1 action already attempted,
    the investigation trail, and the budget it spent.
    """
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
    lines.extend(render_trail(briefing.investigation_trail))
    lines.append(f"Budget used: {briefing.budget_used}")
    return "\n".join(lines)
