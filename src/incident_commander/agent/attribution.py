"""Who caused the recovery, from the run's OWN readings only — never the evaluator's clock.

O-29 / ADR 0071 (amends ADR 0062): credit an action only when the LAST reading before it read
the fault present and the reading after reads it gone. One derivation, every reader (INC-002).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Any, Final, NamedTuple

from pydantic import BaseModel, ConfigDict

from incident_commander.agent.investigation import alert_subject
from incident_commander.agent.state import EvidenceEntry, RunState
from incident_commander.tools.policies import RESOURCE_ARG_FIELDS, Tier, tier_of
from incident_commander.tools.registry import TOOL_REGISTRY


class RecoveredReading(NamedTuple):
    """How one read tool says "the fault this resource had is gone": a field of the reading,
    and the value that means recovered — the form a guard and a grader can both read."""

    tool_name: str
    """Read tool that observes the resource."""
    argument_field: str
    """The argument carrying the resource name. Never ``None``: a reading that cannot
    name its resource is not a statement about one (see ``RECOVERED_READING``)."""
    field: str
    """The field in the reading that answers it."""
    recovered_value: object
    """The value of that field which means the fault is gone."""
    why: str
    """What makes this reading the recovered one, quoted into a refusal and a grade so a
    human is never asked to take the predicate on trust."""


#: For each read tool, how its reading says the fault is already gone. Every read tool the agent
#: can use has an entry; ``None`` states on purpose that this tool's reading can never say so.
RECOVERED_READING: Final[dict[str, RecoveredReading | None]] = {
    # A key that expired by itself and a key the agent invalidated read exactly the same, which
    # is why a claim needs the readings from BEFORE and after the action, not just this one.
    "get_cache_key_info": RecoveredReading(
        "get_cache_key_info",
        "key",
        "exists",
        False,
        "the platform reports every field null for a key it does not hold, so `exists: false` "
        "is the state an invalidation leaves and the state an expiry leaves",
    ),
    # No entry: this reading may be cached, so a zero backlog can mean the queue drained or
    # merely that the measurement predates the fault. An entry would need the measurement's age.
    "get_consumer_lag": None,
    # No entry: no argument names a single row, a row missing from one page proves nothing about
    # the rest (incidents INC-001 and INC-002), and a fenced row is still listed anyway.
    "list_dlq_messages": None,
    # No entry: nothing in a chain's reading shows a fault ending on its own. `paused` is the
    # state the agent's own action writes, and a fence was measured to change nothing else.
    "get_dag_state": None,
    # No entry: a trace records work that already finished, so it never recovers.
    "get_trace": None,
}


class AttributionVerdict(StrEnum):
    """What a run may claim about a recovery it read. Three answers, and no others (O-29)."""

    ATTRIBUTED = "attributed"
    """The last pre-action reading showed the fault present and the reading after the action
    shows it gone: the recovery is this action's to claim."""
    CLEARED_ON_ITS_OWN = "cleared_on_its_own"
    """The resource was read broken and then read healthy with no action taken at all."""
    CANNOT_ATTRIBUTE = "cannot_attribute"
    """A recovery was read, and no fault-present reading immediately precedes the action —
    so the action may have caused it and the evidence cannot say."""


#: The two sentences a run must use to report a recovery it cannot claim credit for, written once
#: here: the shared prompt rule quotes them and a test holds the briefing to the same words.
CLEARED_ON_ITS_OWN_SENTENCE: Final[str] = "the issue cleared on its own before I could act"
CANNOT_ATTRIBUTE_SENTENCE: Final[str] = "recovered, but I cannot confirm my action caused it"

#: Which of those sentences goes with each verdict. A verdict the evidence supports has none,
#: because a claim that holds up needs no disclaimer.
VERDICT_SENTENCE: Final[dict[AttributionVerdict, str]] = {
    AttributionVerdict.CLEARED_ON_ITS_OWN: CLEARED_ON_ITS_OWN_SENTENCE,
    AttributionVerdict.CANNOT_ATTRIBUTE: CANNOT_ATTRIBUTE_SENTENCE,
}


class AttributionRead(BaseModel):
    """What one run's own readings say about the recovery in it.

    Computed from the ledger and carried on the briefing, so the handoff states it rather
    than leaving it to prose (ADR 0065, ADR 0071).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: AttributionVerdict
    resource: str
    """The resource the verdict is about — the acted one, or the alert's own subject."""
    probe_tool: str
    """The read tool whose readings answered it."""
    acted: bool
    """Whether a Tier-1 action in this run touched that resource."""
    detail: str
    """The readings the verdict was derived from, in one sentence."""

    @property
    def sentence(self) -> str:
        """The sentence a run must report with this verdict, or "" where none is required."""
        return VERDICT_SENTENCE.get(self.verdict, "")


def resource_values(tool: str, arguments: Mapping[str, Any]) -> set[str]:
    """Every resource one call names, per ``RESOURCE_ARG_FIELDS`` — the reading-side reader of
    that map. Values are stripped and empties dropped; the plan side's guards do that earlier."""
    values: set[str] = set()
    for field in RESOURCE_ARG_FIELDS.get(tool, frozenset()):
        raw = arguments.get(field)
        for value in raw if isinstance(raw, (list, tuple)) else [raw]:
            if isinstance(value, str) and value.strip():
                values.add(value.strip())
    return values


def _matches(value: object, expected: object) -> bool:
    """Equality that will not let ``0`` count as ``False``, which in Python it otherwise does."""
    if isinstance(expected, bool):
        return value is expected
    return value == expected


def reads_recovered(entry: EvidenceEntry, reading: RecoveredReading) -> bool | None:
    """Whether one entry's reading shows the fault gone. ``None`` when it cannot say.

    ``None`` is load-bearing: an unparseable summary is not a healthy reading, and treating
    it as one would refuse a correct action.
    """
    try:
        parsed = json.loads(entry.result_summary)
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, Mapping) or reading.field not in parsed:
        return None
    return _matches(parsed[reading.field], reading.recovered_value)


def readings_of(
    evidence: Sequence[EvidenceEntry], reading: RecoveredReading, resource: str
) -> tuple[tuple[int, EvidenceEntry], ...]:
    """Every reading of ONE resource, with its position in the ledger.

    Position, not timestamp: a canned run stamps whole transitions with one clock, so ledger
    order is what "before the action" means.
    """
    return tuple(
        (index, entry)
        for index, entry in enumerate(evidence)
        if entry.tool_name == reading.tool_name
        and _named_resource(entry.arguments, reading.argument_field) == resource
    )


def _named_resource(arguments: Mapping[str, Any], field: str) -> str | None:
    value = arguments.get(field)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _effective_call(entry: EvidenceEntry) -> tuple[str, Mapping[str, Any]]:
    """The call one entry represents, a refused Tier-1 attempt included: a platform-refused
    write is recorded on the escalation marker under ``attempted_tool``."""
    attempted = entry.arguments.get("attempted_tool")
    if isinstance(attempted, str):
        raw = entry.arguments.get("attempted_arguments")
        return attempted, raw if isinstance(raw, Mapping) else {}
    return entry.tool_name, entry.arguments


def _is_tier_one(tool: str) -> bool:
    return tool in TOOL_REGISTRY and tier_of(tool) is Tier.TIER_1


def tier_one_calls(
    evidence: Sequence[EvidenceEntry],
) -> tuple[tuple[int, str, Mapping[str, Any]], ...]:
    """Every executed or refused Tier-1 call, in ledger order, with its position."""
    calls: list[tuple[int, str, Mapping[str, Any]]] = []
    for index, entry in enumerate(evidence):
        tool, arguments = _effective_call(entry)
        if _is_tier_one(tool):
            calls.append((index, tool, arguments))
    return tuple(calls)


class _Subject(NamedTuple):
    """A resource whose recovery is readable, and the predicate that reads it."""

    resource: str
    reading: RecoveredReading


def _acted_subject(evidence: Sequence[EvidenceEntry]) -> tuple[int, _Subject] | None:
    """The newest Tier-1 action whose resource a declared reading can observe.

    Searched backwards rather than taken from the last call: an action whose probe is an inert
    entry answers nothing, and stopping there would call the whole run unreadable.
    """
    for index, tool, arguments in reversed(tier_one_calls(evidence)):
        for resource in sorted(resource_values(tool, arguments)):
            for reading in RECOVERED_READING.values():
                if reading is None:
                    continue
                if readings_of(evidence, reading, resource):
                    return index, _Subject(resource, reading)
    return None


def alerted_subject(alert: Mapping[str, Any]) -> _Subject | None:
    """The alert's own subject, when a declared reading can say whether it recovered."""
    subject = alert_subject(alert)
    if subject is None:
        return None
    reading = RECOVERED_READING.get(subject.tool_name)
    if reading is None or reading.argument_field != subject.argument_field:
        return None
    return _Subject(subject.value, reading)


def already_recovered(
    evidence: Sequence[EvidenceEntry], tool: str, arguments: Mapping[str, Any]
) -> tuple[str, RecoveredReading] | None:
    """The resource this call would act on whose NEWEST reading already shows it recovered.

    The planner guard's question, asked before execution. ``None`` covers both "read and still
    broken" and "never read" — not reading it is a different defect with its own steer
    (ADR 0027).
    """
    for resource in sorted(resource_values(tool, arguments)):
        for reading in RECOVERED_READING.values():
            if reading is None:
                continue
            seen = readings_of(evidence, reading, resource)
            if seen and reads_recovered(seen[-1][1], reading) is True:
                return resource, reading
    return None


def render_reading(reading: RecoveredReading, resource: str, entry: EvidenceEntry) -> str:
    """One reading rendered as the call it was, for a refusal and for a grade."""
    return f"{reading.tool_name}({reading.argument_field}={resource!r}) -> {entry.result_summary}"


def attribution_of(run_state: RunState) -> AttributionRead | None:
    """What this run may claim about the recovery in it, or ``None`` when it claims none.

    ``None`` is the common case, not a gap: with no readable resource or no recovery read,
    there is nothing to attribute, and an invented verdict is INC-001's shape.
    """
    evidence = run_state.evidence
    acted = _acted_subject(evidence)
    if acted is not None:
        return _acted_verdict(evidence, *acted)
    if tier_one_calls(evidence):
        # The run acted on something no reading above can observe. "It cleared before I could
        # act" would be false of such a run, so it gets no verdict at all.
        return None
    subject = alerted_subject(run_state.alert)
    if subject is None:
        return None
    return _unacted_verdict(evidence, subject)


def _acted_verdict(
    evidence: Sequence[EvidenceEntry], action_index: int, subject: _Subject
) -> AttributionRead | None:
    """The verdict for a run that acted: the pair of readings around the action."""
    resource, reading = subject
    seen = readings_of(evidence, reading, resource)
    before = [entry for index, entry in seen if index < action_index]
    after = [entry for index, entry in seen if index > action_index]
    if not after or reads_recovered(after[-1], reading) is not True:
        # The newest reading after the action does not show the fault gone, so there is no
        # recovery to credit to anything.
        return None
    post = render_reading(reading, resource, after[-1])
    if before and reads_recovered(before[-1], reading) is False:
        return AttributionRead(
            verdict=AttributionVerdict.ATTRIBUTED,
            resource=resource,
            probe_tool=reading.tool_name,
            acted=True,
            detail=(
                f"the last reading of {resource} before the action — "
                f"{render_reading(reading, resource, before[-1])} — showed the fault present, "
                f"and the reading after it — {post} — shows it gone"
            ),
        )
    pre = (
        f"the last reading of {resource} before the action — "
        f"{render_reading(reading, resource, before[-1])} — already showed the fault gone"
        if before
        else f"no reading of {resource} precedes the action"
    )
    return AttributionRead(
        verdict=AttributionVerdict.CANNOT_ATTRIBUTE,
        resource=resource,
        probe_tool=reading.tool_name,
        acted=True,
        detail=f"{pre}, and the reading after it — {post} — shows the fault gone",
    )


def _unacted_verdict(
    evidence: Sequence[EvidenceEntry], subject: _Subject
) -> AttributionRead | None:
    """The verdict for a run that took no action: did the fault leave while it watched?

    Both halves are required — newest reading gone AND an earlier one present. Without the
    second, a healthy world (``no_fault``) would report as having cleared itself.
    """
    resource, reading = subject
    seen = [entry for _, entry in readings_of(evidence, reading, resource)]
    if not seen or reads_recovered(seen[-1], reading) is not True:
        return None
    present = next((entry for entry in seen if reads_recovered(entry, reading) is False), None)
    if present is None:
        return None
    return AttributionRead(
        verdict=AttributionVerdict.CLEARED_ON_ITS_OWN,
        resource=resource,
        probe_tool=reading.tool_name,
        acted=False,
        detail=(
            f"{resource} was read with the fault present — "
            f"{render_reading(reading, resource, present)} — and then read again with it gone "
            f"— {render_reading(reading, resource, seen[-1])} — and no Tier-1 action ran "
            f"between the two"
        ),
    )
