"""Root-cause scoring: what the agent concluded vs what was actually wrong.

The arithmetic behind ``GradeDimension.ROOT_CAUSE`` (plan 03 § 7.1, WP-2.2), split out so
``deterministic.py`` imports this and not the reverse. Four decisions: the final diagnosis
is ``RunState.hypotheses[0]``; the diagnosed SET adds every cause the ranking asserts at the
acting bar (ADR 0059); exact set passes; a label is true of ONE world (INC-003, WO-R3-265).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Final

from pydantic import BaseModel, ConfigDict

from incident_commander.agent.briefing import incidents_of
from incident_commander.agent.hypothesis import Hypothesis, HypothesisCategory
from incident_commander.agent.state import RunState

#: How a ROOT_CAUSE detail opens when the run's world is not the label's.
#: ``deterministic.is_vacuous_detail`` matches on it, so the row leaves the accuracy
#: denominator as an unasserted dimension would.
NOT_GRADED_PREFIX: Final[str] = "not graded:"


def label_describes_this_world(*, live_mcp: bool, chaos_seeded: bool) -> bool:
    """Is the world this run was in the world the ground truth describes?

    Canned carries the label, and so does live that seeded its own fault; live with nothing
    seeded is the smoke pass and does not (INC-003). Trivial on purpose: the runner and
    ``scripts/regrade_archive.py`` must answer with the same function.
    """
    return not live_mcp or chaos_seeded


def not_graded_detail(expected: str) -> str:
    """The ROOT_CAUSE detail for a run the label does not describe.

    Names the label it held back, so a reader of the archive can see which
    statement was skipped rather than only that one was.
    """
    return (
        f"{NOT_GRADED_PREFIX} the label describes a world this run did not have "
        f"— a live run that seeded no fault; ground truth {expected}"
    )


def is_not_graded_detail(detail: str) -> bool:
    """True for a detail ``not_graded_detail`` produced.

    By prefix, not equality: the label it names varies per scenario.
    """
    return detail.startswith(NOT_GRADED_PREFIX)


def diagnosis_set(run: RunState) -> tuple[HypothesisCategory, ...]:
    """Every cause the run's final ranking ASSERTS, not merely considers (ADR 0059).

    The top hypothesis plus any other held at or above the acting bar, so hedging below it
    stays free and a wrong second cause costs precision. Read off the SLOTS the briefing
    shows a human (WP-11.3, ADR 0065), or the grade and the handoff drift apart.
    """
    return incidents_of(run).categories


def final_diagnosis(run: RunState) -> Hypothesis | None:
    """The agent's answer to "what was wrong?", or ``None`` if it never said.

    Plan 02 § 11.3's top candidate at the deciding step; index 0 is top by construction
    (``InvestigationStep`` re-sorts, B-07). NOT the StepRecord stream, which needs
    ``EVAL_TRACE_DIR`` and so would not grade under ``make eval`` (divergence D1).
    """
    return run.hypotheses[0] if run.hypotheses else None


class RootCauseScore(BaseModel):
    """One run's diagnosis measured against one scenario's ground truth.

    Set-shaped on both sides, so one arithmetic covers single- and multi-fault
    (plan 03 § 7.1). ``exact_set`` is the verdict; the rates are partial credit.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    predicted: tuple[HypothesisCategory, ...]
    expected: tuple[HypothesisCategory, ...]
    matched: tuple[HypothesisCategory, ...]
    exact_set: bool
    precision: float
    recall: float
    f1: float

    def describe(self) -> str:
        """The sentence the ROOT_CAUSE dimension's detail is built from.

        Must not end in " set": ``is_vacuous_detail`` matches that shape, and a
        substantive result reading as vacuous would disable the gate's check.
        """
        predicted = ", ".join(c.value for c in self.predicted) or "nothing"
        expected = ", ".join(c.value for c in self.expected)
        verdict = "exact match" if self.exact_set else "not the declared cause"
        return (
            f"diagnosed {predicted}; ground truth {expected} — {verdict} "
            f"(precision {self.precision:.2f}, recall {self.recall:.2f}, "
            f"F1 {self.f1:.2f})"
        )


def score_root_cause(
    predicted: Iterable[HypothesisCategory],
    expected: Iterable[HypothesisCategory],
) -> RootCauseScore:
    """Score a diagnosis set against a ground-truth set.

    ``NO_FAULT`` needs no branch: the schema refuses to pair it with another label, so it is
    the ordinary exact-set case. An empty prediction scores 0.0 precision, because the 1.0
    convention would score silence above a wrong answer.
    """
    predicted_set = frozenset(predicted)
    expected_set = frozenset(expected)
    matched = predicted_set & expected_set
    precision = len(matched) / len(predicted_set) if predicted_set else 0.0
    recall = len(matched) / len(expected_set) if expected_set else 0.0
    f1 = 0.0 if precision + recall == 0 else 2 * precision * recall / (precision + recall)
    return RootCauseScore(
        predicted=_sorted(predicted_set),
        expected=_sorted(expected_set),
        matched=_sorted(matched),
        exact_set=predicted_set == expected_set,
        precision=precision,
        recall=recall,
        f1=f1,
    )


def _sorted(categories: frozenset[HypothesisCategory]) -> tuple[HypothesisCategory, ...]:
    """Stable order for a set, so two equal scores render identically."""
    return tuple(sorted(categories, key=lambda category: category.value))


class RootCauseCoverage(BaseModel):
    """How much of a suite was root-cause-graded, and how much of that was right.

    Two numbers, because a corpus that declares no ground truth has no accuracy —
    a bare percentage would read as 0% correct where nothing was asked.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Scenarios in the report, graded or not.
    total: int
    #: Scenarios that declared a ground truth and so carry a real verdict.
    graded: int
    #: Of those, how many named the declared cause exactly.
    correct: int
    #: Of the ungraded, how many were held back because the run's world is not the label's
    #: (INC-003) rather than because no label exists — both outside the denominator, but
    #: different facts. Defaults to 0, so archives keep their meaning.
    not_graded_world: int = 0

    @property
    def accuracy(self) -> float | None:
        """Correct / graded, or ``None`` when nothing was graded."""
        return None if self.graded == 0 else self.correct / self.graded

    def describe(self) -> str:
        """One line for the run summary and for a phase report."""
        held_back = (
            f"; {self.not_graded_world} not graded — the label describes a world "
            "the run did not have"
            if self.not_graded_world
            else ""
        )
        if self.graded == 0:
            # Unchanged to the byte: this wording is quoted in docs and in
            # committed reports.
            if not self.not_graded_world:
                return (
                    f"root cause: not measured — 0 of {self.total} scenario(s) declare a "
                    "ground truth, so no run was graded on diagnosis"
                )
            return (
                f"root cause: not measured — 0 of {self.total} scenario(s) were graded "
                f"on diagnosis{held_back}"
            )
        accuracy = self.correct / self.graded
        return (
            f"root cause: {self.correct}/{self.graded} correct "
            f"({accuracy:.0%}) over {self.graded} of {self.total} scenario(s) "
            f"carrying a ground truth{held_back}"
        )


def coverage_over(
    verdicts: Iterable[tuple[bool, bool]], total: int, *, world_mismatch: int = 0
) -> RootCauseCoverage:
    """Roll up ``(is_graded, passed)`` pairs, one per scenario, into coverage.

    Plain pairs, not ``DimensionResult``s, to keep the one-way dependency on
    ``deterministic.py``; the caller decides what counts as graded.
    ``world_mismatch`` is the rows INC-003's rule held back.
    """
    graded = [passed for is_graded, passed in verdicts if is_graded]
    return RootCauseCoverage(
        total=total,
        graded=len(graded),
        correct=sum(graded),
        not_graded_world=world_mismatch,
    )
