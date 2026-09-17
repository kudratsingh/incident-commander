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
"""

from __future__ import annotations

from collections.abc import Iterable

from pydantic import BaseModel, ConfigDict

from incident_commander.agent.hypothesis import Hypothesis, HypothesisCategory
from incident_commander.agent.state import RunState


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

    @property
    def accuracy(self) -> float | None:
        """Correct / graded, or ``None`` when nothing was graded."""
        return None if self.graded == 0 else self.correct / self.graded

    def describe(self) -> str:
        """One line for the run summary and for a phase report."""
        if self.graded == 0:
            return (
                f"root cause: not measured — 0 of {self.total} scenario(s) declare a "
                "ground truth, so no run was graded on diagnosis"
            )
        accuracy = self.correct / self.graded
        return (
            f"root cause: {self.correct}/{self.graded} correct "
            f"({accuracy:.0%}) over {self.graded} of {self.total} scenario(s) "
            "carrying a ground truth"
        )


def coverage_over(verdicts: Iterable[tuple[bool, bool]], total: int) -> RootCauseCoverage:
    """Roll up ``(is_graded, passed)`` pairs, one per scenario, into coverage.

    Takes plain pairs rather than ``DimensionResult``s so this module keeps
    its one-way dependency on ``deterministic.py``; the caller decides what
    counts as graded (in the runner: a ROOT_CAUSE detail ``is_vacuous_detail``
    does not match).
    """
    graded = [passed for is_graded, passed in verdicts if is_graded]
    return RootCauseCoverage(total=total, graded=len(graded), correct=sum(graded))
