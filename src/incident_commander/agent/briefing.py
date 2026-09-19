"""Escalation briefing: what a human sees when the agent hands off.

Deterministic template; ``briefing_enrichment.py`` fills ``findings`` and
``recommendation``. Everything comes from ``RunState``; alert and tool content is
untrusted (invariant 4).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

from incident_commander.agent.planner_context import render_already_attempted
from incident_commander.agent.state import EvidenceEntry, IncidentState, RunState


class ProbeSummary(BaseModel):
    """One entry in the investigation trail: the call, and what it returned.

    ``arguments`` is carried, not dropped: a result read without the arguments
    that scoped it has an unknowable scope (INC-002).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool: str
    summary: str
    arguments: dict[str, Any] = Field(default_factory=dict)


TRAIL_HEADING: Final = "Investigation trail:"
NO_TRAIL_LINE: Final = "No probes were run before escalation."


def trail_of(evidence: Sequence[EvidenceEntry]) -> tuple[ProbeSummary, ...]:
    """The probes out of a run's evidence ledger, for any reader of the trail.

    One projection for the briefing writer, the briefing judge and the selector
    (INC-002). The underscore prefix filters bookkeeping markers structurally;
    ``evals/graders/deterministic.py`` filters the same way.
    """
    return tuple(
        ProbeSummary(
            tool=entry.tool_name,
            summary=entry.result_summary,
            arguments=dict(entry.arguments),
        )
        for entry in evidence
        if not entry.tool_name.startswith("_")
    )


def render_trail(trail: Sequence[ProbeSummary]) -> list[str]:
    """The investigation-trail block, as both LLM readers are shown it.

    One function for the briefing writer and the briefing judge, so the two cannot
    drift; ``tests/unit/test_llm_judge.py`` pins that.
    """
    if not trail:
        return [NO_TRAIL_LINE]
    return [TRAIL_HEADING, *(render_probe(probe) for probe in trail)]


def render_probe(probe: ProbeSummary) -> str:
    """One trail line: the call with its arguments, then what it returned.

    Arguments first: the call's scope, then its result (INC-002).
    """
    return f"  - {probe.tool}({_render_arguments(probe.arguments)}) -> {probe.summary}"


def _render_arguments(arguments: Mapping[str, Any]) -> str:
    """``key=value`` pairs, ``repr``'d, in the order the agent sent them.

    Every argument, ``None`` ones included: ``remediation_hint=None`` is what
    distinguishes an unfiltered read from a filtered one.
    """
    return ", ".join(f"{key}={value!r}" for key, value in arguments.items())


class AttemptedAction(BaseModel):
    """A Tier-1 action that was invoked before the agent escalated.

    Recorded because an action a human believes never fired is one they may fire again.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class EscalationBriefing(BaseModel):
    """Handoff artifact rendered when the agent escalates."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    incident_id: str
    final_state: IncidentState
    alert_summary: str
    escalation_reason: str = ""
    attempted_action: AttemptedAction | None = None
    investigation_trail: tuple[ProbeSummary, ...] = ()
    findings: str = ""
    recommendation: str = ""
    budget_used: dict[str, int | float | str] = Field(default_factory=dict)


def render_briefing(run_state: RunState) -> EscalationBriefing:
    """Build a briefing from a completed run. Deterministic template — no LLM."""
    terminal_marker = _terminal_marker(run_state)
    return EscalationBriefing(
        incident_id=str(run_state.incident_id),
        final_state=run_state.state,
        alert_summary=_render_alert_summary(run_state),
        escalation_reason=_escalation_reason(terminal_marker, run_state.evidence),
        attempted_action=_attempted_action(terminal_marker),
        # ``trail_of`` filters out the escalation marker; the reason it carries
        # is read back out above into its own field, never faked as a probe.
        investigation_trail=trail_of(run_state.evidence),
        findings="",
        recommendation="",
        budget_used={
            "tool_calls": run_state.budget.tool_calls_used,
            "tokens": run_state.budget.tokens_used,
            "wall_seconds": run_state.budget.wall_seconds_used,
            "usd": str(run_state.budget.usd_used),
        },
    )


def _terminal_marker(run_state: RunState) -> EvidenceEntry | None:
    """The bookkeeping entry that ended the run, if the run ended badly.

    Structural: every escalation path appends its marker last, so the marker is the
    last evidence entry. RESOLVED and non-terminal states are excluded — their last
    marker is a verdict or a handoff note, not a reason the agent escalated.
    """
    if not run_state.state.is_terminal or run_state.state is IncidentState.RESOLVED:
        return None
    if not run_state.evidence:
        return None
    last = run_state.evidence[-1]
    return last if last.tool_name.startswith("_") else None


def _escalation_reason(marker: EvidenceEntry | None, evidence: Sequence[EvidenceEntry]) -> str:
    """Why the agent stopped, in the words the writer recorded, plus any earlier attempt.

    From ``result_summary``, not ``arguments["reason"]``: every writer sets it. A run that
    retried (ADR 0056) appends its attempt records: the trail filters underscore markers, so
    without this a human handed a reinvestigated escalation would not be told about the
    Tier-1 write already made on their system — the honesty ``remediate_verify_fails``
    grades. Empty for every run that made no failed attempt.
    """
    reason = marker.result_summary if marker is not None else ""
    attempted = render_already_attempted(evidence)
    if not attempted:
        return reason
    return f"{reason}\n\n{attempted}" if reason else attempted


def _attempted_action(marker: EvidenceEntry | None) -> AttemptedAction | None:
    """The Tier-1 call recorded on the marker, if one was made.

    The same two argument keys ``evals/graders/deterministic.py`` reads.
    """
    if marker is None:
        return None
    tool = marker.arguments.get("attempted_tool")
    if not isinstance(tool, str):
        return None
    raw = marker.arguments.get("attempted_arguments")
    return AttemptedAction(tool=tool, arguments=dict(raw) if isinstance(raw, dict) else {})


def _render_alert_summary(run_state: RunState) -> str:
    """One line naming the alert: where it came from, how bad it is, and what it points at."""
    alert = run_state.alert
    source = str(alert.get("source", "unknown"))
    severity = str(alert.get("severity", "unknown"))
    fingerprint = alert.get("fingerprint")
    # Legacy `group`; the tool arg is `consumer_group` (investigation.py too).
    group = alert.get("consumer_group") or alert.get("group")
    parts = [f"source={source}", f"severity={severity}"]
    if fingerprint is not None:
        parts.append(f"fingerprint={fingerprint}")
    if group is not None:
        parts.append(f"group={group}")
    return " ".join(parts)
