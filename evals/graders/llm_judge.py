"""LLM-as-judge grader for briefing quality.

Uses the pinned ``JUDGE_MODEL`` from Settings so eval scores stay stable across
agent-model swaps — a rewritten prompt shouldn't move the judge's rubric. Two
dimensions, both 0-1: groundedness (no invented facts) and actionability
(concrete verification step for the human).

Scored per scenario; aggregate stats land in RunReport. Regression gating on
judge scores is intentionally deferred — Phase 2 exit is "briefings graded,"
not "briefings all >= 0.8." The bar is set from baseline in Phase 3 once we
have a real distribution.
"""

from __future__ import annotations

from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from incident_commander.agent.briefing import EscalationBriefing
from incident_commander.llm.client import LLMClientProtocol
from incident_commander.llm.prompts.loader import load_prompt

USEFUL_THRESHOLD: Final[float] = 0.7


class JudgeScore(BaseModel):
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
    """Grade a briefing. Uses the pinned ``JUDGE_MODEL`` at the call site."""
    result = judge_client.call(
        system_prompt=load_prompt("briefing_judge"),
        user_message=_format_briefing(briefing),
        output_model=JudgeScore,
        model=model,
    )
    return result.output


def _format_briefing(briefing: EscalationBriefing) -> str:
    lines = [
        f"Incident: {briefing.incident_id}",
        f"Final state: {briefing.final_state.value}",
        f"Alert: {briefing.alert_summary}",
    ]
    if briefing.investigation_trail:
        lines.append("Investigation trail:")
        for probe in briefing.investigation_trail:
            lines.append(f"  - {probe.tool}: {probe.summary}")
    else:
        lines.append("No probes were run before escalation.")
    lines.append(f"Findings: {briefing.findings}")
    lines.append(f"Recommendation: {briefing.recommendation}")
    return "\n".join(lines)
