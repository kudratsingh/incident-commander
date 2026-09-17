"""Root-cause scoring: what the agent concluded vs what was actually wrong.

The arithmetic behind ``GradeDimension.ROOT_CAUSE`` (plan 03 § 7.1, WP-2.2),
kept in its own module for two reasons.

**Direction of dependency.** ``GroundTruth`` lives on ``Scenario``
(``evals/scenarios/schema.py``), and that module already imports
``ScenarioExpectation`` from ``evals/graders/deterministic.py`` — so the
grader cannot import the scenario schema back without a cycle. Nothing here
imports either one: the inputs are ``HypothesisCategory`` values and a
``RunState``, both from ``incident_commander``. ``deterministic.py`` imports
this module, builds the ``DimensionResult``, and the arrow points one way.

**It is wanted outside the grader.** Plan 03 § 12's reward v0 is "root-cause
match (set-F1 for multi-fault)" plus action, safety and budget terms, and
WP-2.5's aggregate report slices root-cause accuracy by family and
difficulty. Both want these numbers without the 1800 lines of evidence
grammar beside them.

Three definitions are load-bearing and each is a decision, not an
implementation detail:

1. **The final diagnosis is the top candidate at the step that emitted
   ``remediate`` or ``stop``** (plan 02 § 11.3), and this module reads it
   from ``RunState.hypotheses[0]``. That is sound today and the reason is
   checked rather than assumed — see ``final_diagnosis``.

2. **The diagnosis is ONE label, even against a multi-fault ground truth.**
   The rest of a ranking is the alternatives the agent considered and ranked
   *lower*; counting them as claimed causes would pay an agent for hedging,
   and "correct root cause anywhere in the final candidate set" is a
   different metric the plan names separately (pass@k, § 7.2). So a
   single-diagnosis strategy scores at most ``recall = 1/n`` on an n-cause
   world and cannot match the set exactly. That is a true statement about
   the baseline strategy rather than a defect here, and it is why precision,
   recall and F1 are reported beside the exact-set verdict instead of one
   bare bit. The functions below are set-shaped throughout, so a strategy
   that emits several diagnoses needs no change here.

3. **Exact set is the pass condition.** Partial credit is measured and
   reported; it does not turn a wrong answer green.

4. **A label is true of ONE world** (INC-003, WO-R3-265). The labels were
   read off each scenario's canned fixtures, and the read-only smoke pass
   runs those same scenarios against an unseeded live stack where the fault
   does not exist. Applying the label there grades the agent against a world
   it was never in — it cost seven false reds and one meaningless "61%" on
   the paid archive ``0db6fe722f7c``. ``label_describes_this_world`` is the
   one place that rule lives, and ``not_graded_detail`` is what the dimension
   says when the answer is no.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Final

from pydantic import BaseModel, ConfigDict

from incident_commander.agent.hypothesis import Hypothesis, HypothesisCategory
from incident_commander.agent.state import RunState

#: How a ROOT_CAUSE detail opens when the run's world is not the label's.
#: ``deterministic.is_vacuous_detail`` matches on it, so a not-graded row
#: leaves the accuracy denominator and the regression gate's vacated-assertion
#: check the same way an unasserted dimension does. Defined here rather than
#: there because the dimension's own vocabulary lives in this module and two
#: copies of a magic prefix is one copy too many.
NOT_GRADED_PREFIX: Final[str] = "not graded:"


def label_describes_this_world(*, live_mcp: bool, chaos_seeded: bool) -> bool:
    """Is the world this run was in the world the ground truth describes?

    Two worlds carry the label and one does not:

    * **canned** (``live_mcp`` false) — the fixtures the label was read from,
      by definition the world it describes. This is every offline run,
      including a declared-live scenario that fell back to canned, and it is
      why ``make eval-reg`` sees no change from this rule at all.
    * **live, and the scenario seeded its own fault** — the chaos plan built
      the world the label was written about, so the label is about this run.
      The remediation scenarios are this case.
    * **live, and nothing was seeded** — the read-only smoke pass. The world
      is whatever the shared stack happened to hold, the canned fault is not
      in it, and the label says nothing about it. INC-003.

    Note what is NOT a factor: whether the row would pass. Keeping the greens
    of a mismatched world and dropping only the reds would buy back the same
    invalid number with a friendlier sign.

    Keyword-only, boolean-in/boolean-out, and deliberately trivial: the point
    is that the runner and ``scripts/regrade_archive.py`` answer this question
    with the same function, or a re-grade of an archive is not the grade the
    runner would have given.
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

    Matched by prefix rather than by equality because the label it names
    varies per scenario, and by shape rather than by an enumerated list for
    the reason ``is_vacuous_detail`` gives: a wording that changes must not
    silently stop being recognised as an absence of a claim.
    """
    return detail.startswith(NOT_GRADED_PREFIX)


def final_diagnosis(run: RunState) -> Hypothesis | None:
    """The agent's answer to "what was wrong?", or ``None`` if it never said.

    Plan 02 § 11.3 defines it as the top candidate at the step that emitted
    ``remediate`` or ``stop``. ``RunState.hypotheses`` holds only the LATEST
    ranking (``agent/state.py``), so reading it here is correct only if
    nothing overwrites that ranking after the deciding step. It is:
    ``agent/investigation.py::_plan_next_step`` is the single writer of the
    field in the whole package, it writes once per planner call, and the
    loop's ``remediate``/``stop`` branches return immediately from the
    iteration that wrote it. ``agent/remediation.py`` reads ``hypotheses[0]``
    and never assigns it. Index 0 is the top candidate by construction —
    ``InvestigationStep`` re-sorts by confidence at the schema boundary
    (B-07), so it does not depend on the model's emitted order.

    The StepRecord stream (WP-2.1) carries the same ranking per step and was
    the alternative source. It is NOT used, and the reason is that it would
    have made this dimension unmeasurable exactly where the suite runs:
    records reach the trace store only when ``EVAL_TRACE_DIR`` is set, and
    ``make eval`` — the target behind the regression gate — does not set it
    (``agent/strategies/records.py``, divergence D1). A dimension that grades
    only under an opt-in environment variable is a dimension the offline
    suite cannot fail.

    Three run shapes end without a ``remediate``/``stop`` step at all —
    budget exhaustion mid-investigation, ``max_iterations``, and a probe or
    planner failure that escalates. For those this returns the last ranking
    the agent produced, which is the most recent answer it gave; a run that
    never reached a planner call at all has no ranking and returns ``None``,
    which grades red rather than vacuous when a ground truth was declared.
    Silence is not a correct diagnosis.
    """
    return run.hypotheses[0] if run.hypotheses else None


class RootCauseScore(BaseModel):
    """One run's diagnosis measured against one scenario's ground truth.

    Set-shaped on both sides so the same arithmetic covers the single-fault
    case (where it degenerates to an exact match) and the multi-fault case
    plan 03 § 7.1 asks for. ``exact_set`` is the verdict; the three rates are
    the partial credit that says *how* wrong a wrong answer was, which is the
    difference between "named an unrelated fault" and "named one of the two
    real ones".
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

        Deliberately does not end in " set": ``is_vacuous_detail`` matches a
        detail by that shape, and a substantive result that read as vacuous
        would disable the regression gate's vacated-assertion check for this
        dimension (``deterministic.py``:67-91).
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

    ``NO_FAULT`` needs no branch of its own: the level-0 control declares
    ``root_causes: [no_fault]`` and the schema refuses to pair that label
    with any other (``GroundTruth._no_fault_is_the_whole_answer``), so "the
    agent correctly reported nothing was wrong" is the ordinary exact-set
    case with ``no_fault`` on both sides, and an agent that names a fault in
    a healthy world fails it the ordinary way.

    An empty prediction scores 0.0 precision rather than raising or being
    undefined: a run that named no cause made no correct claim, and the
    alternative convention (1.0, "it was never wrong") would score silence
    above a wrong answer. ``expected`` is never empty — ``root_causes`` has
    ``min_length=1`` — so recall needs no such convention, but it is written
    to survive one rather than dividing by zero if the schema ever relaxes.
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

    Two numbers rather than one, and the split is the honest part: a corpus
    where no scenario declares a ground truth has an accuracy that is not 0%
    and not 100% — it does not exist. Reporting a bare percentage over the
    whole suite would read as "the agent diagnosed 0 of 41 correctly" when
    the truth is "41 of 41 were never asked".
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Scenarios in the report, graded or not.
    total: int
    #: Scenarios that declared a ground truth and so carry a real verdict.
    graded: int
    #: Of those, how many named the declared cause exactly.
    correct: int
    #: Of the ungraded, how many were held back because the run was in a world
    #: the label does not describe (INC-003) rather than because no label
    #: exists. Both are outside the denominator and they are different facts:
    #: "nine scenarios declare no label" is about the corpus, "seven labels
    #: describe a world this run did not have" is about the run. Defaults to 0
    #: so every existing caller — and every archived report read back through
    #: this model — keeps its meaning unchanged.
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
            # The wording when nothing was held back is unchanged to the byte:
            # it is quoted in docs and in committed reports, and a report that
            # says the same thing should say it the same way.
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

    Takes plain pairs rather than ``DimensionResult``s so this module keeps
    its one-way dependency on ``deterministic.py``; the caller decides what
    counts as graded (in the runner: a ROOT_CAUSE detail ``is_vacuous_detail``
    does not match).

    ``world_mismatch`` is how many of the ungraded rows were held back by
    INC-003's rule. Keyword-only with a default, for the same reason
    ``grade``'s new argument is: a caller that cannot tell the two absences
    apart keeps reporting exactly what it reported before rather than
    guessing.
    """
    graded = [passed for is_graded, passed in verdicts if is_graded]
    return RootCauseCoverage(
        total=total,
        graded=len(graded),
        correct=sum(graded),
        not_graded_world=world_mismatch,
    )
