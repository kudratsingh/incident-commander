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

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from incident_commander.agent.state import EvidenceEntry, IncidentState, RunState


class ProbeSummary(BaseModel):
    """One entry in the investigation trail."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool: str
    summary: str


class AttemptedAction(BaseModel):
    """A Tier-1 action that was invoked before the agent escalated.

    Present only when the call was made and did not land a normal evidence
    entry of its own — the platform errored, refused it, or returned a
    response we could not parse. The human must be told: an action they
    believe never fired is an action they may fire again.
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
        escalation_reason=_escalation_reason(terminal_marker),
        attempted_action=_attempted_action(terminal_marker),
        investigation_trail=tuple(
            ProbeSummary(tool=entry.tool_name, summary=entry.result_summary)
            # Bookkeeping markers are underscore-prefixed by convention; no
            # registry tool name is. Filtering structurally (the grader does
            # the same, evals/graders/deterministic.py) means a new evidence
            # writer cannot drift out of a hand-maintained exclusion list.
            #
            # The filter is right about the trail and used to be wrong about
            # the *reason*: the escalation marker is the only carrier of why
            # the agent gave up, so filtering it here deleted that line from
            # the handoff entirely. It is read back out above into its own
            # field instead of being smuggled into the trail as a fake probe.
            for entry in run_state.evidence
            if not entry.tool_name.startswith("_")
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


def _terminal_marker(run_state: RunState) -> EvidenceEntry | None:
    """The bookkeeping entry that ended the run, if the run ended badly.

    Structural, like the trail filter above, so no writer has to be listed
    anywhere: every escalation path (``_remediation_escalate``, ``_escalate``,
    ``_planner_escalate``, ``_planner_stop``) and the crash rail's
    ``_run_failure`` finish by appending their marker and transitioning to a
    terminal state, so the marker is the *last* evidence entry. A new writer
    that follows the same convention is picked up for free.

    Two states are excluded rather than named as exceptions. RESOLVED: its
    last entry is ``_verify_judge``, a verdict — a real reason string, but not
    a reason the agent escalated, and labelling it one would put "verified:
    lag is zero" under a heading that says the handoff needs a human.
    Non-terminal: mid-run, the last marker is a handoff note like
    ``_planner_remediate``, which reads as a reason and is not one. The run
    has not ended, so nothing ended it.
    """
    if not run_state.state.is_terminal or run_state.state is IncidentState.RESOLVED:
        return None
    if not run_state.evidence:
        return None
    last = run_state.evidence[-1]
    return last if last.tool_name.startswith("_") else None


def _escalation_reason(marker: EvidenceEntry | None) -> str:
    """Why the agent stopped, in the words the writer recorded.

    Read from ``result_summary`` rather than ``arguments["reason"]``: every
    writer sets the summary (``_triage`` classifies a noise alert straight to
    ESCALATED with no ``reason`` argument at all), and the summary is the
    rendered line — ``"planner stop: ..."``, ``"escalated: ..."`` — which is
    what a human wants to read.
    """
    return marker.result_summary if marker is not None else ""


def _attempted_action(marker: EvidenceEntry | None) -> AttemptedAction | None:
    """The Tier-1 call recorded on the marker, if one was made.

    Mirrors ``_effective_call`` in ``evals/graders/deterministic.py``: the
    grader reads these same two argument keys to charge a refused attempt to
    the SAFETY dimension. The human handoff should not know less than the
    grader does.
    """
    if marker is None:
        return None
    tool = marker.arguments.get("attempted_tool")
    if not isinstance(tool, str):
        return None
    raw = marker.arguments.get("attempted_arguments")
    return AttemptedAction(tool=tool, arguments=dict(raw) if isinstance(raw, dict) else {})


def _render_alert_summary(run_state: RunState) -> str:
    alert = run_state.alert
    source = str(alert.get("source", "unknown"))
    severity = str(alert.get("severity", "unknown"))
    fingerprint = alert.get("fingerprint")
    # Accept legacy `group` field for backward-compat with older alert
    # producers; platform's tool arg is `consumer_group`. Mirrors
    # investigation.py's fallback.
    group = alert.get("consumer_group") or alert.get("group")
    parts = [f"source={source}", f"severity={severity}"]
    if fingerprint is not None:
        parts.append(f"fingerprint={fingerprint}")
    if group is not None:
        parts.append(f"group={group}")
    return " ".join(parts)
