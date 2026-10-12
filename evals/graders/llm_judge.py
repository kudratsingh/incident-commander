"""LLM-as-judge grader for briefing quality.

Uses the pinned ``JUDGE_MODEL``, so scores stay stable across agent-model swaps. Two
dimensions, both 0-1: groundedness (no invented facts) and actionability (a concrete
verification step). Regression gating on judge scores awaits a real distribution.
"""

from __future__ import annotations

from typing import Final

from pydantic import ConfigDict, Field

from incident_commander.agent.briefing import (
    EscalationBriefing,
    render_attribution,
    render_incidents,
    render_trail,
)
from incident_commander.llm.client import LLMClientProtocol
from incident_commander.llm.prompts.loader import load_prompt
from incident_commander.llm.repair import call_with_output_repair
from incident_commander.llm.structured import StructuredOutput

USEFUL_THRESHOLD: Final[float] = 0.7

#: The prompt file this judge's rubric lives in. Named once: the call below loads
#: it and the calibration harness hashes it (plan 03 § 110's attribution rule).
JUDGE_PROMPT: Final[str] = "briefing_judge"

#: Rubric v2's checks, by the names the prompt gives them in bold and in the order the judge
#: applies them (WO-R3-378, owner O-58, ADR 0082): the v1 floor kept as a check, then INC-010's
#: three fault patterns and the actionability fix. ``reasoning`` must name the check that decided
#: each score, so the calibration records which of these a reasoning cites (``cited_checks``).
BRIEFING_JUDGE_CHECKS: Final[tuple[str, ...]] = (
    "invented fact",
    "derived, not copied",
    "blind probe",
    "a guess is not a finding",
    "look at X, do nothing",
)

#: What ``reasoning`` says for a score no check lowered, also a citation.
NO_CHECK_FIRED: Final[str] = "no check fired"


def cited_checks(reasoning: str) -> tuple[str, ...]:
    """The checks a judge's ``reasoning`` names, in rubric order; ``no check fired`` last.

    Case-insensitive substring match on the bold names: a judge told to name the deciding check
    by its name either does or does not, and this is how a report counts which.
    """
    lowered = reasoning.lower()
    named = tuple(name for name in BRIEFING_JUDGE_CHECKS if name.lower() in lowered)
    return named + ((NO_CHECK_FIRED,) if NO_CHECK_FIRED in lowered else ())


class JudgeScore(StructuredOutput):
    """Per-briefing judge score. LLM emits the two numeric dimensions + reasoning."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    groundedness: float = Field(ge=0.0, le=1.0)
    actionability: float = Field(ge=0.0, le=1.0)
    reasoning: str = Field(min_length=1)

    @property
    def overall(self) -> float:
        return (self.groundedness + self.actionability) / 2

    @property
    def is_useful(self) -> bool:
        return self.overall >= USEFUL_THRESHOLD


def judge_briefing(
    briefing: EscalationBriefing,
    judge_client: LLMClientProtocol,
    model: str,
) -> JudgeScore:
    """Grade a briefing. Uses the pinned ``JUDGE_MODEL`` at the call site.

    One bounded re-ask on an output-shape failure (ADR 0035, WO-R2-174); a second raises
    ``OutputRepairExhausted``, and ``evals/runner.py`` records ``judge_error`` instead.
    """
    call = call_with_output_repair(
        judge_client,
        system_prompt=load_prompt(JUDGE_PROMPT),
        user_message=format_briefing_context(briefing),
        output_model=JudgeScore,
        model=model,
    )
    return call.result.output


def format_briefing_context(briefing: EscalationBriefing) -> str:
    """The context the judge grades against.

    Public since WP-6.3, so the calibration harness asks through the same function. Must
    show everything the WRITER was shown, in the writer's wording (pinned by
    ``test_llm_judge.py``), including each probe's ARGUMENTS: a result without its scope
    read as "the whole queue is empty" and scored an honest briefing 0.0 (INC-002).
    """
    lines = [
        f"Incident: {briefing.incident_id}",
        f"Final state: {briefing.final_state.value}",
        f"Alert: {briefing.alert_summary}",
    ]
    if briefing.escalation_reason:
        lines.append(f"Why the run ended: {briefing.escalation_reason}")
    if briefing.attempted_action is not None:
        lines.append(
            f"Tier-1 action ALREADY ATTEMPTED (do not recommend repeating it "
            f"without checking its effect first): {briefing.attempted_action.tool} "
            f"{briefing.attempted_action.arguments}"
        )
    # Same block, same words, same place as the writer's (WP-11.3): the remainder is run
    # state, so a judge blind to it would score the writer down for naming it (INC-002).
    lines.extend(render_incidents(briefing.incidents))
    # Same block, same words, same place as the writer's (ADR 0071): a judge blind to it
    # would score down a briefing for refusing credit the run's own readings refuse.
    lines.extend(render_attribution(briefing.attribution))
    lines.extend(render_trail(briefing.investigation_trail))
    lines.append(f"Findings: {briefing.findings}")
    lines.append(f"Recommendation: {briefing.recommendation}")
    return "\n".join(lines)
