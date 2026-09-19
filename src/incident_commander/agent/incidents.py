"""Which causes a run named, and which it left standing: primary / secondary / unresolved-extra.

One projection of a run for every reader — the escalation briefing, the deterministic grader,
the briefing judge and the ``StepRecord`` stream (WP-11.3, ADR 0065, INC-002). Derivation
only: the bar is passed in and every gate stays in ``agent/investigation.py``.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence

from pydantic import BaseModel, ConfigDict

from incident_commander.agent.hypothesis import Hypothesis, HypothesisCategory
from incident_commander.agent.planner_context import ATTEMPT_FAILED_MARKER, PLAN_MARKER
from incident_commander.agent.state import EvidenceEntry

#: The argument key both markers carry the targeted cause under. One spelling, because a
#: second one is how a remediated cause starts reading as an unaddressed one (ADR 0059).
TARGET_KEY = "target_hypothesis"


class IncidentSlot(BaseModel):
    """One cause a run named, and whether any attempt in that run aimed at it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    category: HypothesisCategory
    name: str
    confidence: float
    addressed: bool


class IncidentSlots(BaseModel):
    """A run's causes by slot: the one it is about, the others it asserts, the remainder.

    ``unresolved_extra`` is WO-R2-164's rule made structural — every cause the run still
    asserts and took no action on, so a briefing cannot omit a remainder by accident.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    primary: IncidentSlot | None = None
    secondary: tuple[IncidentSlot, ...] = ()
    unresolved_extra: tuple[IncidentSlot, ...] = ()

    @property
    def asserted(self) -> tuple[IncidentSlot, ...]:
        """Primary first, then every other cause the ranking holds at the bar."""
        return ((self.primary,) if self.primary is not None else ()) + self.secondary

    @property
    def addressed_any(self) -> bool:
        """Did an attempt in this run aim at a cause the run named?"""
        return any(slot.addressed for slot in self.asserted)

    @property
    def categories(self) -> tuple[HypothesisCategory, ...]:
        """The diagnosed SET ``ROOT_CAUSE`` scores (ADR 0059), sorted so two equal sets render
        identically."""
        return tuple(sorted({slot.category for slot in self.asserted}, key=lambda c: c.value))


def addressed_targets(evidence: Sequence[EvidenceEntry]) -> frozenset[str]:
    """Every cause an attempt in this run aimed at, in the spelling its plan used.

    Read off the two markers that carry it — the plan that survived its guards and each
    failed attempt (ADR 0056) — so the fact travels with the ledger rather than with a flag.
    """
    return frozenset(
        str(entry.arguments[TARGET_KEY])
        for entry in evidence
        if entry.tool_name in {PLAN_MARKER, ATTEMPT_FAILED_MARKER} and TARGET_KEY in entry.arguments
    )


def _is_addressed(hypothesis: Hypothesis, targets: Collection[str]) -> bool:
    """Name OR category: ``RemediationPlan.target_hypothesis`` is a free string and the corpus
    spells it both ways (ADR 0059) — reading one spelling would call a fixed cause unaddressed."""
    return hypothesis.name in targets or hypothesis.category.value in targets


def incident_slots(
    *,
    hypotheses: Sequence[Hypothesis],
    evidence: Sequence[EvidenceEntry],
    bar: float,
) -> IncidentSlots:
    """Split a ranking into slots at the bar the loop acts on.

    ``bar`` is a parameter, not an import: this module is read by the loop, so it cannot
    import ``investigation.REMEDIATE_CONFIDENCE_THRESHOLD`` back. Its callers pass that one
    constant. The top hypothesis is the primary whatever its confidence — the same reading
    ``final_diagnosis`` has always had — and only the REST of the ranking must clear the bar,
    so hedging below it stays free.
    """
    if not hypotheses:
        return IncidentSlots()
    targets = addressed_targets(evidence)

    def slot(hypothesis: Hypothesis) -> IncidentSlot:
        return IncidentSlot(
            category=hypothesis.category,
            name=hypothesis.name,
            confidence=hypothesis.confidence,
            addressed=_is_addressed(hypothesis, targets),
        )

    primary = slot(hypotheses[0])
    secondary = tuple(slot(h) for h in hypotheses[1:] if h.confidence >= bar)
    return IncidentSlots(
        primary=primary,
        secondary=secondary,
        unresolved_extra=tuple(s for s in (primary, *secondary) if not s.addressed),
    )
