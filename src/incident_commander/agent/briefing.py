"""Escalation briefing: what a human sees when the agent hands off.

Deterministic template; ``briefing_enrichment.py`` fills ``findings`` and ``recommendation``.
Everything comes from ``RunState``; alert and tool content is untrusted (invariant 4).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

from incident_commander.agent.attribution import AttributionRead, attribution_of
from incident_commander.agent.incidents import IncidentSlot, IncidentSlots, incident_slots
from incident_commander.agent.investigation import REMEDIATE_CONFIDENCE_THRESHOLD
from incident_commander.agent.planner_context import render_already_attempted
from incident_commander.agent.state import EvidenceEntry, IncidentState, RunState


class ProbeSummary(BaseModel):
    """One entry in the investigation trail: the call, and what it returned.

    ``arguments`` is carried, not dropped: a result read without the arguments that scoped
    it has an unknowable scope (INC-002).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool: str
    summary: str
    arguments: dict[str, Any] = Field(default_factory=dict)


TRAIL_HEADING: Final = "Investigation trail:"
NO_TRAIL_LINE: Final = "No probes were run before escalation."


def trail_of(evidence: Sequence[EvidenceEntry]) -> tuple[ProbeSummary, ...]:
    """The probes out of a run's evidence ledger, for any reader of the trail.

    One projection for writer, judge and selector (INC-002). The underscore prefix filters
    bookkeeping markers, the same way ``evals/graders/deterministic.py`` does.
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
    """The investigation-trail block, as both LLM readers are shown it — one function for
    writer and judge so they cannot drift (``tests/unit/test_llm_judge.py`` pins it)."""
    if not trail:
        return [NO_TRAIL_LINE]
    return [TRAIL_HEADING, *(render_probe(probe) for probe in trail)]


def render_probe(probe: ProbeSummary) -> str:
    """One trail line: the call with its arguments first, then what it returned (INC-002)."""
    return f"  - {probe.tool}({_render_arguments(probe.arguments)}) -> {probe.summary}"


def _render_arguments(arguments: Mapping[str, Any]) -> str:
    """``key=value`` pairs, ``repr``'d, in the order the agent sent them.

    ``None`` ones included: ``remediation_hint=None`` is what tells an unfiltered read from
    a filtered one.
    """
    return ", ".join(f"{key}={value!r}" for key, value in arguments.items())


#: The two headings the causes block uses. Named once here, because the renderer, the grader and
#: the shared prompt rule must all use exactly the same words.
INCIDENTS_HEADING: Final = "Incidents this run named:"
REMAINDER_HEADING: Final = "Remaining (not addressed by this run):"


def incidents_of(run_state: RunState) -> IncidentSlots:
    """A run's incident slots at the bar the loop acts on (WP-11.3, ADR 0065).

    The one place the briefing side applies ``REMEDIATE_CONFIDENCE_THRESHOLD``, so the handoff
    and the grade come from the same projection (INC-002).
    """
    return incident_slots(
        hypotheses=run_state.hypotheses,
        evidence=run_state.evidence,
        bar=REMEDIATE_CONFIDENCE_THRESHOLD,
    )


def render_incidents(slots: IncidentSlots) -> list[str]:
    """The incident-slot block, as both LLM readers are shown it.

    One function for writer and judge so they cannot drift. Empty for a run that produced no
    ranking, which keeps pre-WP-11.3 contexts byte-identical.
    """
    if slots.primary is None:
        return []
    lines = [INCIDENTS_HEADING, _slot_line("PRIMARY", slots.primary)]
    lines.extend(_slot_line("SECONDARY", slot) for slot in slots.secondary)
    if slots.unresolved_extra:
        lines.append(REMAINDER_HEADING)
        lines.extend(f"  - {_named_slot(slot)}" for slot in slots.unresolved_extra)
    return lines


def _slot_line(role: str, slot: IncidentSlot) -> str:
    """One slot line: which slot, which cause, and whether this run acted on it."""
    acted = (
        "an action in this run targeted this cause"
        if slot.addressed
        else "no action in this run targeted this cause"
    )
    return f"  - {role}: {_named_slot(slot)} — {acted}"


def _named_slot(slot: IncidentSlot) -> str:
    """A cause as every reader names it: the agent's own label, name and confidence."""
    return f"{slot.category.value} / {slot.name} (confidence {slot.confidence:.2f})"


#: The heading the recovery-attribution block uses, named here for the same reason as the two
#: above: three separate readers have to agree on the text.
ATTRIBUTION_HEADING: Final = "Recovery attribution (from this run's own readings):"


def render_attribution(read: AttributionRead | None) -> list[str]:
    """The attribution block, as both LLM readers are shown it.

    One rendering for writer and judge so they cannot drift (INC-002). Empty for a run with no
    verdict, which keeps those contexts byte-identical to what they were before it existed.
    """
    if read is None:
        return []
    lines = [
        ATTRIBUTION_HEADING,
        f"  - VERDICT: {read.verdict.value} — {read.resource} read through {read.probe_tool}",
        f"  - WHY: {read.detail}.",
    ]
    if read.sentence:
        lines.append(f"  - REPORT IT AS: {read.sentence}.")
    return lines


class AttemptedAction(BaseModel):
    """A Tier-1 action invoked before the agent escalated — recorded because an action a
    human believes never fired is one they may fire again."""

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
    # Computed from the run's own ranking and attempts rather than written by the model, so
    # what is left unaddressed does not depend on what the writer chooses to say.
    incidents: IncidentSlots = Field(default_factory=IncidentSlots)
    # Computed for the same reason: whether the action caused the recovery is a statement about
    # two readings taken either side of it, so it is not left to the writer to judge.
    attribution: AttributionRead | None = None
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
        incidents=incidents_of(run_state),
        attribution=attribution_of(run_state),
        # ``trail_of`` leaves the escalation row out of the trail: its reason is already in
        # ``escalation_reason`` above, and it was never a real probe.
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

    Every escalation path appends its marker last. RESOLVED and non-terminal states are
    excluded: their last marker is a verdict, not a reason the agent escalated.
    """
    if not run_state.state.is_terminal or run_state.state is IncidentState.RESOLVED:
        return None
    if not run_state.evidence:
        return None
    last = run_state.evidence[-1]
    return last if last.tool_name.startswith("_") else None


def _escalation_reason(marker: EvidenceEntry | None, evidence: Sequence[EvidenceEntry]) -> str:
    """Why the agent stopped, in the words the writer recorded, plus any earlier attempt.

    From ``result_summary``, not ``arguments["reason"]``: every writer sets it. The attempt
    records are appended because the trail filters underscore markers, and a human handed a
    reinvestigated escalation must still hear about the Tier-1 write already made (ADR 0056).
    """
    reason = marker.result_summary if marker is not None else ""
    attempted = render_already_attempted(evidence)
    if not attempted:
        return reason
    return f"{reason}\n\n{attempted}" if reason else attempted


def _attempted_action(marker: EvidenceEntry | None) -> AttemptedAction | None:
    """The Tier-1 call recorded on the marker, under the same two argument keys
    ``evals/graders/deterministic.py`` reads."""
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
    # Older alerts spell it `group`; the platform's own argument is `consumer_group`, and the
    # investigation loop accepts both spellings the same way.
    group = alert.get("consumer_group") or alert.get("group")
    parts = [f"source={source}", f"severity={severity}"]
    if fingerprint is not None:
        parts.append(f"fingerprint={fingerprint}")
    if group is not None:
        parts.append(f"group={group}")
    return " ".join(parts)
