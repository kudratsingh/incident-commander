"""Escalation briefing: what a human sees when the agent hands off.

Phase 2 v0 is deterministic. It captures the load-bearing shape (alert summary,
investigation trail, evidence highlights) so ``findings`` and ``recommendation``
can later be filled by an LLM without moving the surface everyone else consumes.

Everything a briefing carries comes from ``RunState`` — never from external
input at render time. That's important: alert content, tool output, and error
strings are all untrusted (CLAUDE.md invariant 4) and stay quoted-not-executed
inside the ``ProbeSummary`` records.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from incident_commander.agent.state import IncidentState, RunState

_TRIAGE_TOOLS = frozenset({"_triage", "_escalate"})


class ProbeSummary(BaseModel):
    """One entry in the investigation trail."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool: str
    summary: str


class EscalationBriefing(BaseModel):
    """Handoff artifact rendered when the agent escalates."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    incident_id: str
    final_state: IncidentState
    alert_summary: str
    investigation_trail: tuple[ProbeSummary, ...] = ()
    findings: str = ""
    recommendation: str = ""
    budget_used: dict[str, int | float | str] = Field(default_factory=dict)


def render_briefing(run_state: RunState) -> EscalationBriefing:
    """Build a briefing from a completed run. Deterministic template — no LLM."""
    return EscalationBriefing(
        incident_id=str(run_state.incident_id),
        final_state=run_state.state,
        alert_summary=_render_alert_summary(run_state),
        investigation_trail=tuple(
            ProbeSummary(tool=entry.tool_name, summary=entry.result_summary)
            for entry in run_state.evidence
            if entry.tool_name not in _TRIAGE_TOOLS
        ),
        findings="",
        recommendation="",
        budget_used={
            "tool_calls": run_state.budget.tool_calls_used,
            "tokens": run_state.budget.tokens_used,
            "wall_seconds": run_state.budget.wall_seconds_used,
            "usd": str(run_state.budget.usd_used),
        },
    )


def _render_alert_summary(run_state: RunState) -> str:
    alert = run_state.alert
    source = str(alert.get("source", "unknown"))
    severity = str(alert.get("severity", "unknown"))
    fingerprint = alert.get("fingerprint")
    group = alert.get("group")
    parts = [f"source={source}", f"severity={severity}"]
    if fingerprint is not None:
        parts.append(f"fingerprint={fingerprint}")
    if group is not None:
        parts.append(f"group={group}")
    return " ".join(parts)
