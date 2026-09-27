"""RESOLVED needs a reading of the alerted metric taken AFTER the action, inside the threshold.

ADR 0077 (INC-005). One predicate, held once: the verify loop gates RESOLVED on it, the judge is
shown the reading through it, and the evidence grader grades a finished run on it.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any, Final, Literal, NamedTuple

from incident_commander.agent.investigation import AlertSubject, alert_subject, subject_reads
from incident_commander.agent.state import EvidenceEntry, RunState
from incident_commander.tools.policies import Tier, tier_of
from incident_commander.tools.registry import TOOL_REGISTRY


class MetricReading(NamedTuple):
    """Where one read tool keeps the alerted number, its measurement time and its history."""

    tool_name: str
    """Read tool that observes the resource."""
    value_field: str
    """The field holding the number the alert's threshold is about."""
    known_field: str | None
    """A boolean that must be ``True`` before the number means anything, or ``None``."""
    measured_at_field: str
    """When the platform measured the number, on the platform's clock."""
    age_field: str
    """How old the measurement was when the platform served it, in whole seconds."""
    samples_field: str
    """The list of earlier measurements, each with its own value and ``measured_at``."""


#: For each read tool an alert subject can name, how its reading states a number with a
#: threshold. ``None`` says on purpose that the tool's reading is not that shape (ADR 0077).
METRIC_READING: Final[dict[str, MetricReading | None]] = {
    # The platform's consumer_stalled page carries `threshold`, and this reading is a cached
    # measurement with its own time, so a read made just after the action can predate it.
    "get_consumer_lag": MetricReading(
        "get_consumer_lag", "lag", "lag_known", "measured_at", "age_seconds", "recent_samples"
    ),
    # No entry: an invalidation reports its effect (`deleted: true`) rather than showing it in
    # a number, so the judge decides from the action's own result as it does today.
    "get_cache_key_info": None,
    # No entry: a chain's state is a set of node statuses, not a number with a threshold.
    "get_dag_state": None,
    # No entry: a trace records work that already finished; it has no threshold to be inside.
    "get_trace": None,
    # No entry: a listing is read at call time, so it cannot be older than the action, and a
    # scheduled replay leaves its rows listed until the timer fires (the judge-decides path).
    "list_dlq_messages": None,
}

GateVerdict = Literal[
    "verified_on_stale_reading", "verified_above_threshold", "verified_without_a_reading"
]


class MetricExpectation(NamedTuple):
    """An alert whose subject is a number with a threshold, and where that number is read."""

    subject: AlertSubject
    reading: MetricReading
    threshold: float


class ReadingCheck(NamedTuple):
    """One reading of the alerted subject, judged against the action and the threshold."""

    value: object
    """The number as the platform reported it (``None`` when it reported none)."""
    measured_at: datetime | None
    """When it was measured, on the platform's clock."""
    action_at: datetime
    """When the action ran, moved onto the platform's clock (see ``platform_offset``)."""
    after_action: bool
    inside: bool


def _number(value: object) -> float | None:
    """A JSON number as a float, refusing ``bool`` (which Python counts as an int)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _timestamp(value: object) -> datetime | None:
    """An ISO-8601 string as an aware datetime, or ``None`` when it is not one."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _parsed(summary: str) -> Mapping[str, Any] | None:
    try:
        parsed = json.loads(summary)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, Mapping) else None


def metric_expectation(alert: Mapping[str, Any]) -> MetricExpectation | None:
    """The alert's metric-shaped subject, or ``None`` when the judge alone decides (ADR 0077).

    Metric-shaped means the subject's read tool has a ``METRIC_READING`` entry AND the alert
    states the number it was paged on (``threshold``), as the platform's own pages do.
    """
    subject = alert_subject(alert)
    if subject is None:
        return None
    reading = METRIC_READING.get(subject.tool_name)
    if reading is None:
        return None
    extra = alert.get("extra_data")
    raw = alert.get("threshold")
    if raw is None and isinstance(extra, Mapping):
        raw = extra.get("threshold")
    threshold = _number(raw)
    if threshold is None:
        return None
    return MetricExpectation(subject, reading, threshold)


def platform_offset(
    parsed: Mapping[str, Any], reading: MetricReading, read_at: datetime
) -> timedelta:
    """The platform's clock minus the agent's, from one reading.

    ``measured_at + age_seconds`` is the platform's "now" when it served the reading, and
    ``read_at`` is the agent's. With no age reported the two clocks are taken to agree.
    """
    measured_at = _timestamp(parsed.get(reading.measured_at_field))
    age = _number(parsed.get(reading.age_field))
    if measured_at is None or age is None:
        return timedelta(0)
    return measured_at + timedelta(seconds=age) - read_at


def check_reading(
    entry: EvidenceEntry, expectation: MetricExpectation, *, action_at: datetime
) -> ReadingCheck:
    """Whether one reading was measured after the action and is inside the threshold.

    ``entry.timestamp`` is when the agent made the read. "Inside" is the platform's own rule:
    the page fires at or above the threshold and resolves below it.
    """
    reading = expectation.reading
    parsed = _parsed(entry.result_summary) or {}
    action_on_platform = action_at + platform_offset(parsed, reading, entry.timestamp)
    measured_at = _timestamp(parsed.get(reading.measured_at_field))
    value = parsed.get(reading.value_field)
    known = reading.known_field is None or parsed.get(reading.known_field) is True
    number = _number(value) if known else None
    return ReadingCheck(
        value=value,
        measured_at=measured_at,
        action_at=action_on_platform,
        after_action=measured_at is not None and measured_at > action_on_platform,
        inside=number is not None and number < expectation.threshold,
    )


def action_entry(evidence: Sequence[EvidenceEntry]) -> EvidenceEntry | None:
    """The newest Tier-1 call on the ledger — the action a RESOLVED is about."""
    for entry in reversed(evidence):
        if entry.tool_name in TOOL_REGISTRY and tier_of(entry.tool_name) is Tier.TIER_1:
            return entry
    return None


def last_subject_reading(run_state: RunState, subject: AlertSubject) -> EvidenceEntry | None:
    """The newest reading of the alerted subject anywhere on the ledger."""
    reads = subject_reads(run_state, subject)
    return reads[-1] if reads else None


def _clock(moment: datetime | None) -> str:
    return "an unreported time" if moment is None else moment.isoformat()


def gate_miss(
    run_state: RunState, expectation: MetricExpectation
) -> tuple[GateVerdict, str] | None:
    """Why this run may NOT resolve yet, or ``None`` when a post-action reading allows it.

    Reads the newest reading of the subject after the newest action. The reason names the
    reading and both times, because it is what a human is told when the polls run out.
    """
    action = action_entry(run_state.evidence)
    if action is None:
        return None
    subject = expectation.subject
    reading = last_subject_reading(run_state, subject)
    named = f"{subject.tool_name}({subject.argument_field}={subject.value})"
    position = {entry.evidence_id: index for index, entry in enumerate(run_state.evidence)}
    if reading is None or position[reading.evidence_id] < position[action.evidence_id]:
        return (
            "verified_without_a_reading",
            f"no reading of {named} was taken after {action.tool_name} ran at "
            f"{_clock(action.timestamp)}",
        )
    check = check_reading(reading, expectation, action_at=action.timestamp)
    field = expectation.reading.value_field
    shown = f"{field} {check.value} measured at {_clock(check.measured_at)}"
    ran = (
        f"{action.tool_name} ran at {_clock(check.action_at)} on the platform's clock "
        f"({_clock(action.timestamp)} on the agent's)"
    )
    above = f"{check.value} is not below the alert's threshold {expectation.threshold:g}"
    if not check.after_action:
        return (
            "verified_on_stale_reading",
            f"the last reading of {named} ({shown}) was measured before {ran}, so it cannot "
            "show what the action did" + ("" if check.inside else f"; and {above}"),
        )
    if not check.inside:
        return (
            "verified_above_threshold",
            f"the last reading of {named} ({shown}, after {ran}): {above}",
        )
    return None


def gate_pass_note(run_state: RunState, expectation: MetricExpectation) -> str | None:
    """The sentence a grade shows when the post-action reading held, or ``None`` if none did."""
    action = action_entry(run_state.evidence)
    reading = last_subject_reading(run_state, expectation.subject)
    if action is None or reading is None or gate_miss(run_state, expectation) is not None:
        return None
    check = check_reading(reading, expectation, action_at=action.timestamp)
    return (
        f"post-action reading held: {expectation.reading.value_field} {check.value} measured at "
        f"{_clock(check.measured_at)}, after {action.tool_name} at {_clock(check.action_at)} "
        f"(platform's clock), below the threshold {expectation.threshold:g}"
    )


def judge_view(
    tool_name: str, summary: str, *, action_at: datetime | None, read_at: datetime | None
) -> str:
    """The verify reading as the judge sees it: history OLDEST FIRST with a computed trend.

    Readings without a ``recent_samples`` list come back byte-identical. INC-005: a raw
    newest-first list was read backwards, so the list's order is never left to convention.
    """
    parsed = _parsed(summary)
    reading = METRIC_READING.get(tool_name)
    if parsed is None or reading is None or not isinstance(parsed.get(reading.samples_field), list):
        return summary
    # 1. The reading itself, without its history, so the raw list is never in front of the judge.
    raw_samples = parsed[reading.samples_field]
    current = {key: value for key, value in parsed.items() if key != reading.samples_field}
    # 2. Each sample with a readable time, sorted by that time rather than by list position.
    dated = sorted(
        (
            (moment, sample.get(reading.value_field))
            for sample in raw_samples
            if isinstance(sample, Mapping)
            and (moment := _timestamp(sample.get(reading.measured_at_field))) is not None
        ),
        key=lambda pair: pair[0],
    )
    # 3. The action's time on the platform's clock, so it compares with the samples' times.
    action_on_platform = (
        action_at + platform_offset(parsed, reading, read_at)
        if action_at is not None and read_at is not None
        else None
    )
    lines = [
        f"  {index}. {reading.value_field}={value} measured_at={moment.isoformat()}"
        for index, (moment, value) in enumerate(dated, start=1)
    ]
    trend = {
        "first": dated[0][1] if dated else None,
        "last": dated[-1][1] if dated else None,
        "direction": _direction(dated[-2][1], dated[-1][1]) if len(dated) > 1 else None,
        "samples_after_action": (
            sum(1 for moment, _ in dated if moment > action_on_platform)
            if action_on_platform is not None
            else None
        ),
    }
    skipped = len(raw_samples) - len(dated)
    # 4. Assembled in a fixed order: reading, history, trend, the action's time.
    parts = [
        json.dumps(current, separators=(",", ":")),
        "",
        f"Sample history, OLDEST FIRST ({len(dated)} samples, times on the platform's clock):",
        *(lines or ["  (none kept)"]),
    ]
    if skipped:
        parts.append(f"  ({skipped} sample(s) without a readable time left out)")
    parts.append(f"Trend: {json.dumps(trend, separators=(',', ':'))}")
    parts.append(
        "Action executed at (platform's clock): "
        + (action_on_platform.isoformat() if action_on_platform is not None else "not recorded")
    )
    return "\n".join(parts)


def _direction(before: object, newest: object) -> str | None:
    """How the newest sample moved from the one before it, or ``None`` if not numbers.

    The newest step, not oldest-to-newest: a window that climbed and then recovered to where it
    started would otherwise read ``flat``.
    """
    low, high = _number(before), _number(newest)
    if low is None or high is None:
        return None
    if high > low:
        return "rising"
    if high < low:
        return "falling"
    return "flat"
