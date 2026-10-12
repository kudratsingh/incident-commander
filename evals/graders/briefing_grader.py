"""The two briefing faults that are lookups, not judgments (owner O-61, ADR 0083).

The arithmetic behind ``GradeDimension.BRIEFING``, split out so ``deterministic.py`` imports this
and not the reverse. Both checks read the briefing's own trail, the alert and the run's actions —
the archive, never a model:

* **cached reading called confirmed** — a reading whose platform fields say it is not fresh
  (``READING_FRESHNESS``) is reported in ``findings`` or ``recommendation`` with a freshness word.
* **copied from a non-subject DLQ row** — a ``recommendation`` step that carries a standing
  dead-letter row's own ``suggested_fix`` words, for a row this incident is not about.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from incident_commander.agent.briefing import EscalationBriefing, ProbeSummary
from incident_commander.agent.state import EvidenceEntry
from incident_commander.tools.policies import Tier, tier_of
from incident_commander.tools.registry import TOOL_REGISTRY

# --- Check 1: a cached reading called confirmed ---------------------------------------------


@dataclass(frozen=True)
class FreshnessFields:
    """Where one tool's result says what it measured and whether that measurement is fresh."""

    value: str
    subject: str
    source: str
    timestamp: str
    live_source: str = "live"


#: Every tool whose result CAN be a stale reading, by the platform's own output schema. Pinned
#: against ``contracts/platform-tools.snapshot.json`` by ``tests/unit/test_briefing_grader.py``:
#: the other readings with a ``measured_at`` declare it required, so they are never untimestamped.
READING_FRESHNESS: Final[Mapping[str, FreshnessFields]] = {
    "get_consumer_lag": FreshnessFields(
        value="lag", subject="consumer_group", source="source", timestamp="measured_at"
    ),
}

#: The words that report a reading as fresh. Whole words, so ``unconfirmed`` is not one.
FRESHNESS_WORDS: Final[tuple[str, ...]] = (
    "confirmed",
    "verified",
    "current",
    "currently",
    "live",
    "fresh",
    "freshly",
    "re-measured",
    "remeasured",
)

#: Phrases that tell the reader the number is not fresh. One in the same clause cancels the
#: claim: the owner's useful packet-1 #13 says "confirmed lag of 30,000 (cached reading, …)".
STALENESS_DISCLOSURES: Final[tuple[str, ...]] = (
    "cached",
    "static",
    "stale",
    "untimestamped",
    "no timestamp",
    "timestamp unknown",
    "without a timestamp",
    "recorded constant",
    "not live",
    "not a live",
    "not re-measured",
)

_FRESHNESS_RE: Final = re.compile(
    r"(?<![\w-])(" + "|".join(re.escape(word) for word in FRESHNESS_WORDS) + r")(?![\w-])",
    re.IGNORECASE,
)
_NEGATED_RE: Final = re.compile(r"\b(?:not|never|no longer)\s+$", re.IGNORECASE)
_DISCLOSURE_RE: Final = re.compile(
    r"\b(?:" + "|".join(re.escape(phrase) for phrase in STALENESS_DISCLOSURES) + r")\b",
    re.IGNORECASE,
)
# A clause ends at a semicolon or at a sentence stop followed by space ("v0.4.2" is not a stop).
_CLAUSE_SPLIT_RE: Final = re.compile(r";|(?<=[.!?])\s+")


@dataclass(frozen=True)
class CachedReading:
    """One reading in the trail that the platform says is not a fresh measurement."""

    tool: str
    subject: str
    value: int | float
    source: object
    timestamp: object

    def describe(self) -> str:
        return (
            f"{self.tool}({self.subject}) -> {self.value} with source={self.source!r}, "
            f"measured_at={self.timestamp!r}"
        )


@dataclass(frozen=True)
class CachedReadingClaim:
    """A clause that reports a cached reading's value as fresh."""

    reading: CachedReading
    word: str
    clause: str

    def describe(self) -> str:
        return (
            f"cached reading called {self.word}: {self.reading.describe()}, reported as "
            f'"{self.clause}"'
        )


def cached_readings(trail: Iterable[ProbeSummary]) -> list[CachedReading]:
    """Every reading whose ``source`` is not live or whose timestamp is missing, deduplicated."""
    found: dict[tuple[str, str, int | float], CachedReading] = {}
    for probe in trail:
        fields = READING_FRESHNESS.get(probe.tool)
        parsed = _parse(probe.summary)
        if fields is None or not isinstance(parsed, dict):
            continue
        value = parsed.get(fields.value)
        if isinstance(value, bool) or not isinstance(value, int | float):
            continue
        source, timestamp = parsed.get(fields.source), parsed.get(fields.timestamp)
        if source == fields.live_source and timestamp:
            continue
        subject = str(parsed.get(fields.subject) or probe.arguments.get(fields.subject) or "")
        found.setdefault(
            (probe.tool, subject, value),
            CachedReading(probe.tool, subject, value, source, timestamp),
        )
    return list(found.values())


def cached_reading_claims(briefing: EscalationBriefing) -> list[CachedReadingClaim]:
    """Clauses of ``findings``/``recommendation`` that call a cached reading fresh.

    A clause fires when it carries the reading's value, names what was read (the value field or
    the subject), holds a freshness word not negated by "not"/"never", and discloses nothing.
    """
    readings = cached_readings(briefing.investigation_trail)
    claims: list[CachedReadingClaim] = []
    for clause in _clauses(briefing.findings, briefing.recommendation):
        if _DISCLOSURE_RE.search(clause):
            continue
        word = _freshness_word(clause)
        if word is None:
            continue
        for reading in readings:
            fields = READING_FRESHNESS[reading.tool]
            if _mentions_value(clause, reading.value) and _names_any(
                clause, (fields.value, reading.subject)
            ):
                claims.append(CachedReadingClaim(reading, word, clause))
    return claims


def _clauses(*texts: str) -> Iterator[str]:
    for text in texts:
        for clause in _CLAUSE_SPLIT_RE.split(text):
            if clause.strip():
                yield clause.strip()


def _freshness_word(clause: str) -> str | None:
    for match in _FRESHNESS_RE.finditer(clause):
        if not _NEGATED_RE.search(clause[: match.start()]):
            return match.group(1).lower()
    return None


def _mentions_value(clause: str, value: int | float) -> bool:
    """The number as a writer spells it: ``15000``, ``15,000`` or ``15k``."""
    spellings = {str(value)}
    if isinstance(value, int) or value.is_integer():
        whole = int(value)
        spellings |= {str(whole), f"{whole:,}"}
        if whole >= 1000 and whole % 1000 == 0:
            spellings |= {f"{whole // 1000}k", f"{whole // 1000}K"}
    pattern = r"(?<![\w.,])(?:" + "|".join(map(re.escape, spellings)) + r")(?![\w]|[.,]\d)"
    return re.search(pattern, clause) is not None


def _names_any(clause: str, names: Iterable[str]) -> bool:
    return any(
        name and re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", clause, re.IGNORECASE)
        for name in names
    )


# --- Check 2: a recommendation step copied from a non-subject dead-letter row -------------------

#: How many of a row's own ``suggested_fix`` words a step must carry to be its copy. Three: two
#: coincide on a compound ("task health", owner-useful packet-1 #13), and the owner's smallest
#: copy carries three (packet-1 #6: vpc, egress, smtp.mailer.internal). See ADR 0083.
COPY_WORD_THRESHOLD: Final[int] = 3

#: Words a step names a row as remaining with (ADR 0065's remainder): the row's own hint
#: beside one of these is the remainder rule, not a copy.
REMAINDER_MARKERS: Final[tuple[str, ...]] = (
    "remaining",
    "remain",
    "remains",
    "unresolved",
    "unrelated",
    "not addressed",
    "independent of this incident",
)

#: Function words and the imperative verbs every recommendation uses; never evidence of a copy.
_STOPWORD_TEXT: Final[str] = """
    a an the and or but if then than that this these those to of in on at by for from with
    without into onto over under is are was were be been being it its as not no nor so such can
    could may might must should would will shall do does did done have has had once before after
    again any all each every both either neither there here which who whom whose what when where
    why how its their them they you your our we via per
    check verify confirm ensure review inspect investigate look assess
"""
STOPWORDS: Final[frozenset[str]] = frozenset(_STOPWORD_TEXT.split())

_STEP_SPLIT_RE: Final = re.compile(r";|(?<=[.!?])\s+|,?\s+then\s+", re.IGNORECASE)
_TOKEN_RE: Final = re.compile(r"[a-z0-9](?:[a-z0-9_.:-]*[a-z0-9])?")
_PORT_RE: Final = re.compile(r":\d+$")
_REMAINDER_RE: Final = re.compile(
    r"\b(?:" + "|".join(re.escape(marker) for marker in REMAINDER_MARKERS) + r")\b",
    re.IGNORECASE,
)
_SHORT_ID: Final[int] = 8


@dataclass(frozen=True)
class StandingRow:
    """One dead-letter row the trail shows, with the ``suggested_fix`` the platform wrote for it."""

    row_id: str
    remediation_hint: object
    suggested_fix: str

    @property
    def short_id(self) -> str:
        return self.row_id[:_SHORT_ID]


@dataclass(frozen=True)
class CopiedStep:
    """A recommendation step that carries a non-subject row's own ``suggested_fix`` words."""

    row: StandingRow
    step: str
    shared: tuple[str, ...]

    def describe(self) -> str:
        return (
            f"recommendation step copied from dead-letter row {self.row.row_id} "
            f"(not this incident's subject; shares {', '.join(self.shared)} with its "
            f'suggested_fix): "{self.step}"'
        )


def standing_rows(trail: Iterable[ProbeSummary]) -> list[StandingRow]:
    """Every row a ``list_dlq_messages`` result carries with a ``suggested_fix``, first sighting."""
    rows: dict[str, StandingRow] = {}
    for probe in trail:
        if probe.tool != "list_dlq_messages":
            continue
        for item in _items(_parse(probe.summary)):
            triage = item.get("triage")
            fix = triage.get("suggested_fix") if isinstance(triage, dict) else None
            row_id = str(item.get("id") or "")
            if fix and row_id:
                rows.setdefault(row_id, StandingRow(row_id, item.get("remediation_hint"), str(fix)))
    return list(rows.values())


def subject_row_ids(
    rows: Sequence[StandingRow],
    *,
    alert: Mapping[str, object],
    actions: Sequence[tuple[str, Mapping[str, object]]],
) -> set[str]:
    """The rows this incident IS about: named by the alert, in a DLQ page's slice, or acted on.

    A category replay acts on every row of that category; an action naming a row's id acts on it.
    """
    alert_text = json.dumps(alert, default=str)
    dlq_page = alert.get("source") == "platform.dlq"

    def is_subject(row: StandingRow) -> bool:
        in_page_slice = dlq_page and alert.get("remediation_hint") in (None, row.remediation_hint)
        return (
            row.short_id in alert_text
            or in_page_slice
            or any(_acts_on(row, tool, arguments) for tool, arguments in actions)
        )

    return {row.row_id for row in rows if is_subject(row)}


def run_actions(
    evidence: Iterable[EvidenceEntry], briefing: EscalationBriefing
) -> list[tuple[str, Mapping[str, object]]]:
    """Every write the run made or attempted: action calls, and the tool a marker records."""
    actions: list[tuple[str, Mapping[str, object]]] = []
    for entry in evidence:
        if entry.tool_name.startswith("_"):
            attempted = entry.arguments.get("attempted_tool")
            raw = entry.arguments.get("attempted_arguments")
            if isinstance(attempted, str):
                actions.append((attempted, raw if isinstance(raw, dict) else {}))
        elif _is_action(entry.tool_name):
            actions.append((entry.tool_name, entry.arguments))
    if briefing.attempted_action is not None:
        actions.append((briefing.attempted_action.tool, briefing.attempted_action.arguments))
    return actions


def copied_steps(
    briefing: EscalationBriefing,
    *,
    alert: Mapping[str, object],
    actions: Sequence[tuple[str, Mapping[str, object]]],
) -> list[CopiedStep]:
    """Recommendation steps that carry ``COPY_WORD_THRESHOLD`` of a non-subject row's own words.

    A row's own words are its ``suggested_fix`` words found nowhere else in the trail and not
    in the alert: a word this incident's readings carry is derivable, so it is never a copy.
    """
    trail = briefing.investigation_trail
    rows = standing_rows(trail)
    subjects = subject_row_ids(rows, alert=alert, actions=actions)
    alert_words = _words(json.dumps(alert, default=str))
    copies: list[CopiedStep] = []
    for row in rows:
        if row.row_id in subjects:
            continue
        own = _words(row.suggested_fix) - _words(_trail_without(trail, row.row_id)) - alert_words
        for step in _steps(briefing.recommendation):
            shared = own & _words(step)
            if len(shared) < COPY_WORD_THRESHOLD or _names_as_remainder(step, row):
                continue
            copies.append(CopiedStep(row, step, tuple(sorted(shared))))
    return copies


def _steps(recommendation: str) -> list[str]:
    return [step.strip() for step in _STEP_SPLIT_RE.split(recommendation) if step.strip()]


def _names_as_remainder(step: str, row: StandingRow) -> bool:
    return row.short_id in step or _REMAINDER_RE.search(step) is not None


def _words(text: str) -> set[str]:
    words: set[str] = set()
    for token in _TOKEN_RE.findall(text.lower()):
        token = _PORT_RE.sub("", token)
        if len(token) >= 3 and token not in STOPWORDS and not token.replace(".", "").isdigit():
            words.add(token)
    return words


def _trail_without(trail: Iterable[ProbeSummary], row_id: str) -> str:
    """The whole trail as text, with every sighting of one row taken out."""
    parts: list[str] = []
    for probe in trail:
        parsed = _parse(probe.summary)
        if probe.tool == "list_dlq_messages" and isinstance(parsed, dict):
            kept = [item for item in _items(parsed) if str(item.get("id")) != row_id]
            parts.append(json.dumps({**parsed, "items": kept}))
        else:
            parts.append(probe.summary)
    return " ".join(parts)


def _acts_on(row: StandingRow, tool: str, arguments: Mapping[str, object]) -> bool:
    if tool == "replay_dlq_by_category" and arguments.get("category") == row.remediation_hint:
        return True
    return row.row_id in json.dumps(arguments, default=str)


def _is_action(tool: str) -> bool:
    return tool in TOOL_REGISTRY and tier_of(tool) is not Tier.READ


def _items(parsed: object) -> list[dict[str, Any]]:
    if not isinstance(parsed, dict) or not isinstance(parsed.get("items"), list):
        return []
    return [item for item in parsed["items"] if isinstance(item, dict)]


def _parse(summary: str) -> object:
    try:
        return json.loads(summary)
    except ValueError:
        return None
