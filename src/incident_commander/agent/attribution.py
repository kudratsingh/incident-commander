"""Who caused the recovery: the run's own readings, before its action and after it.

Owner decision O-29 (2026-09-19), ADR 0071 (``docs/ADR/``), amending ADR 0062: a recovery is
credited to an action only when the run's LAST reading of the acted resource before that action
showed the fault present and its reading after shows the fault gone. One derivation for every
reader — the planner guard that refuses an action on a resource already reading healthy, the
briefing slot a human is handed, and the ``ATTRIBUTION`` grade (INC-002: a rule about reading
evidence goes to every reader of it, in one change).

Nothing here reads the evaluator's expiry clock. A reading cannot say who removed a key
(ADR 0062), and the fault's TTL is evaluator-only (ADR 0038) — so what the agent may claim is
decided by what the agent itself read, which is also the only thing a live operator has.
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
    """How one read tool says "the fault this resource had is gone".

    The mirror of a verify expectation, in the one form a guard and a grader can both
    read: a field of the reading, and the value that means recovered.
    """

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


#: Single source of truth for "this reading shows the fault already gone". Keyed on the READ
#: tool, because the question is about a reading and not about an action: the same
#: ``get_cache_key_info(key=…)`` answers it whether the run is about to act, has just acted, or
#: never acts at all.
#:
#: TOTAL over every read tool named by ``investigation.ALERT_SUBJECT_PROBES`` or
#: ``remediation.VERIFY_PROBE_FOR_ACTION``, and ``None`` is a DECLARED inert entry with its
#: reason, never an omission — ``tests/unit/test_attribution.py::TestTheRecoveredReadingMap``
#: pins the totality, so a new probe tool arrives as a decision rather than as silence.
RECOVERED_READING: Final[dict[str, RecoveredReading | None]] = {
    # The entry is gone. This is the reading ADR 0062 was written from: an expired key and an
    # invalidated one are byte-identical, so the reading cannot say who removed it — which is
    # exactly why the PAIR of readings, before and after, is what a claim may rest on.
    "get_cache_key_info": RecoveredReading(
        "get_cache_key_info",
        "key",
        "exists",
        False,
        "the platform reports every field null for a key it does not hold, so `exists: false` "
        "is the state an invalidation leaves and the state an expiry leaves",
    ),
    # INERT, and the reason is ADR 0009's. `get_consumer_lag` is a DECLARED CACHED read
    # (`policies.CACHED_READ_FRESHNESS_SECONDS`, 60s): a zero can be a drained backlog or a
    # measurement taken before the fault existed, and on 2026-08-03 a stale zero killed a
    # correct diagnosis and bought a wrong remediation. Reading one as "the fault ended by
    # itself" would rebuild that failure inside the guard this map serves. `measured_at` /
    # `age_seconds` (v0.6.7) are what a future entry would have to read.
    "get_consumer_lag": None,
    # INERT: no argument names one row. `list_dlq_messages` reads the queue, and an absence
    # from a filtered or partial page proves only that page — INC-001 and INC-002 are both
    # that mistake, once in a claim and once in a judge. A fence also leaves the row listed
    # (ADR 0033), so "gone from the listing" is not even the recovered state for every action
    # this listing verifies.
    "list_dlq_messages": None,
    # INERT: nothing in a chain reading says the fault ended on its own. `dead_letter` is
    # terminal and only a replay leaves it, `waiting` descendants promote only once their
    # parent completes, and `paused` is the state the agent's OWN stabilizer writes — ADR 0033
    # measured a fence leaving the whole chain byte-identical.
    "get_dag_state": None,
    # INERT: a trace is a record of work that already happened. It does not recover.
    "get_trace": None,
}


class AttributionVerdict(StrEnum):
    """What this run may say about the recovery it read (O-29's three answers)."""

    ATTRIBUTED = "attributed"
    """The last pre-action reading showed the fault present and the reading after the action
    shows it gone: the recovery is this action's to claim."""
    CLEARED_ON_ITS_OWN = "cleared_on_its_own"
    """The resource was read broken and then read healthy with no action taken at all."""
    CANNOT_ATTRIBUTE = "cannot_attribute"
    """A recovery was read, and no fault-present reading immediately precedes the action —
    so the action may have caused it and the evidence cannot say."""


#: The sentences O-29 requires a run to report, verbatim in one place. The shared prompt rule
#: quotes them, the planner guard's escalation carries the first, and
#: ``tests/unit/test_attribution.py`` pins that the words a reader is given and the words a
#: briefing carries are the same words.
CLEARED_ON_ITS_OWN_SENTENCE: Final[str] = "the issue cleared on its own before I could act"
CANNOT_ATTRIBUTE_SENTENCE: Final[str] = "recovered, but I cannot confirm my action caused it"

#: Which sentence each verdict is reported with. ``ATTRIBUTED`` has none: a claim that holds
#: needs no disclaimer.
VERDICT_SENTENCE: Final[dict[AttributionVerdict, str]] = {
    AttributionVerdict.CLEARED_ON_ITS_OWN: CLEARED_ON_ITS_OWN_SENTENCE,
    AttributionVerdict.CANNOT_ATTRIBUTE: CANNOT_ATTRIBUTE_SENTENCE,
}


class AttributionRead(BaseModel):
    """What one run's own readings say about the recovery in it.

    Structural, computed from the ledger, and carried on the briefing so the handoff states
    it rather than leaving it to prose (ADR 0065's shape, ADR 0071's subject).
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
        """The report O-29 requires for this verdict, or "" where none is required."""
        return VERDICT_SENTENCE.get(self.verdict, "")


def resource_values(tool: str, arguments: Mapping[str, Any]) -> set[str]:
    """Every resource one call names, per ``RESOURCE_ARG_FIELDS``.

    The map is the single source of truth and this is its reading-side reader, beside
    ``remediation._resource_values`` on the plan side and ``remediation._subject_kind`` on the
    alert side. Values are stripped and empties dropped, which the plan side does not do
    because its own argument guards reject those shapes before any of them is compared.
    """
    values: set[str] = set()
    for field in RESOURCE_ARG_FIELDS.get(tool, frozenset()):
        raw = arguments.get(field)
        for value in raw if isinstance(raw, (list, tuple)) else [raw]:
            if isinstance(value, str) and value.strip():
                values.add(value.strip())
    return values


def _matches(value: object, expected: object) -> bool:
    """Value equality that does not let ``0`` satisfy ``False`` (S-20's lesson)."""
    if isinstance(expected, bool):
        return value is expected
    return value == expected


def reads_recovered(entry: EvidenceEntry, reading: RecoveredReading) -> bool | None:
    """Whether one entry's reading shows the fault gone. ``None`` when it cannot say.

    ``None`` is the third answer and it is load-bearing: an unparseable summary or a missing
    field is not a healthy reading, and treating it as one would refuse a correct action.
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

    Position, not timestamp: a canned run stamps whole transitions with one clock, and the
    order a run read things in is what "before the action" means (``_evidence_before`` slices
    the same way).
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
    """The call one entry represents — a refused Tier-1 attempt included.

    A platform-refused write is recorded on the escalation marker with ``attempted_tool``;
    it caused no recovery either, and SAFETY and ATTRIBUTION read the same shape.
    """
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

    Newest, because the state a run hands off rests on its last action. Searched backwards
    rather than taken from the last call outright: an action whose probe is a declared inert
    entry answers nothing, and stopping there would call a run unreadable because of a tool
    this map has no predicate for.
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

    The planner guard's question, asked of a plan that has not executed: with no action on
    the ledger yet, "the last reading before the action" is the last reading there is.
    ``None`` means no declared reading says so — including every case where the run read that
    resource and found the fault, and every case where it read nothing at all. Not reading
    the resource is a different defect with a different steer (ADR 0027), and answering it
    here would refuse correct plans the corpus already grades.
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

    ``None`` is the common case and it is not a gap: a run whose resource no declared reading
    can observe, or one where no recovery was read at all, has nothing to attribute — and a
    verdict invented there would be the assertion-nothing-could-satisfy shape of INC-001.
    """
    evidence = run_state.evidence
    acted = _acted_subject(evidence)
    if acted is not None:
        return _acted_verdict(evidence, *acted)
    if tier_one_calls(evidence):
        # An action fired on something this map cannot read. "It cleared before I could act"
        # is false of such a run, so the alerted-subject branch is not available to it.
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
        # No recovery was read, so nothing is being credited to the action.
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

    Both halves are required. The newest reading shows it gone AND an earlier one showed it
    present — without the second, a resource that was never broken in this run would report
    as having cleared itself, which is a healthy world (``no_fault``) wearing this verdict's
    words.
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
