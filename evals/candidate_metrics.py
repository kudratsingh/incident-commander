"""pass@k, appeared-at-any-step and duplicate rate over a run's step records.

Plan 02 § 11.3 and plan 03 § 7.2, WP-5.2's reporting half. The arithmetic lives
here rather than in ``evals/research_report.py`` for the reason
``evals/graders/root_cause.py`` gives for its own split: the numbers are wanted
outside the report — by a phase-close, by a future recorded-mode sweep, by a
selector packet computing ``oracle_gap@k = pass@k − selected@k`` (plan 02 § 12)
— and none of those want the report's 1600 lines of archive plumbing beside
them.

The input is a sequence of ``StepRecord``s as
``agent/strategies/records.py`` builds them, or the JSON dicts a trace file
holds them as. Both, because they are the same data at two ages: a live run has
the objects in memory and a finished run has the JSONL, and a metric that could
only be computed from one of them could not be recomputed from the evidence.

Three definitions are decisions rather than implementation details.

**1. pass@k is scored at the deciding step, and "appeared at any step" is a
different number.** The deciding step is the one that emitted ``remediate`` or
``stop`` — the same definition ``graders/root_cause.final_diagnosis`` uses for
the single diagnosis, and for the same reason: that is the step whose answer
the run acted on. A run that never reached one (budget exhaustion,
``max_iterations``, a probe failure) is scored on its last step, which is the
most recent answer it gave. ``appeared_at_any_step`` asks the weaker question —
did the correct cause EVER show up in a candidate set — and the gap between the
two is a finding about the loop, not about generation: it is the correct
diagnosis that was produced and then dropped.

**2. k is capped at the set size, and the cap is reported.** pass@8 over a
4-candidate set is pass@4 wearing a bigger number. ``PassAtK.k`` is what was
asked for and ``PassAtK.effective_k`` is what the set could answer, so a table
cannot print a k the run never had.

**3. "Duplicate rate" is two numbers, and neither is the other.** The schema
forbids a duplicate ``(category, name)`` *within* one set (ADR 0042), so a set
that repeats itself never reaches a record at all — it is a billed rejection,
counted by ``within_step_rejection_rate`` off ``generation_rejections``. What
CAN repeat is a candidate across the steps of one run, and
``cross_step_duplicate_rate`` measures that: 1 − distinct/total over every
candidate the run generated. It is the number that answers "how much genuinely
new did N buy", and a strategy that emits the same four candidates at every
step scores high on it while breaking no rule. Reporting one of the two as "the
duplicate rate" would let a reader take a schema refusal for a modelling
finding, or the reverse.

Ground truth arrives as a set of ``HypothesisCategory`` — the scenario's
``ground_truth.root_causes`` — and is the evaluator's, never the agent's
(ADR 0038). Nothing here is importable from ``src/incident_commander``: these
metrics are computed *about* a run and can never be read back into one.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from incident_commander.agent.hypothesis import HypothesisCategory
from incident_commander.agent.strategies.records import StepRecord

#: The ks plan 02 § 11.3 reports. A run whose sets are smaller answers the
#: larger ones at its own size, and says so through ``effective_k``.
REPORTED_KS: Final[tuple[int, ...]] = (1, 2, 4, 8)

#: ``next_action.kind`` values that end the investigation loop. The step
#: carrying one is the deciding step (plan 02 § 11.3).
_DECIDING_ACTIONS: Final[frozenset[str]] = frozenset({"remediate", "stop"})


@dataclass(frozen=True, slots=True, kw_only=True)
class Candidate:
    """One candidate as the metrics read it: its label and its rank.

    A projection of ``CandidateRecord``, not a second copy of it. pass@k needs
    a category, a name and a position; carrying the token counters and the
    citations through the arithmetic would invite a metric to depend on them.
    """

    category: HypothesisCategory
    name: str
    #: 0-based position in the validated set, so ``rank < k`` is "inside the
    #: top k". The set is ranked by confidence at the schema boundary
    #: (``candidates._one_candidate_each``), so the order is the model's
    #: ranking normalised, never the order it happened to type.
    rank: int


@dataclass(frozen=True, slots=True, kw_only=True)
class Step:
    """One planner step, reduced to what these metrics are computed from."""

    iteration: int
    strategy: str
    candidates: tuple[Candidate, ...]
    #: ``probe`` / ``remediate`` / ``stop``.
    action_kind: str
    #: Validation classes of the billed sets rejected before the accepted one.
    rejections: tuple[str, ...] = ()

    @property
    def is_deciding(self) -> bool:
        return self.action_kind in _DECIDING_ACTIONS


@dataclass(frozen=True, slots=True, kw_only=True)
class PassAtK:
    """pass@k for one k, with the honesty fields that keep it readable."""

    k: int
    #: ``min(k, size of the set scored)``. Equal to ``k`` when the set was big
    #: enough; smaller says the run could not answer the k that was asked.
    effective_k: int
    #: Did the correct root cause appear in the top ``effective_k`` of the
    #: deciding step's set? ``None`` when there was no set to score.
    hit: bool | None

    def describe(self) -> str:
        if self.hit is None:
            return f"pass@{self.k}: not scored — the run produced no candidate set"
        verdict = "hit" if self.hit else "miss"
        cap = "" if self.effective_k == self.k else f" (capped at {self.effective_k})"
        return f"pass@{self.k}: {verdict}{cap}"


@dataclass(frozen=True, slots=True, kw_only=True)
class CandidateMetrics:
    """Every WP-5.2 number for one run, over one ground truth."""

    strategy: str
    steps: int
    #: Total candidates generated across the run — N × steps for an arm that
    #: never had a set rejected.
    candidates_generated: int
    pass_at_k: tuple[PassAtK, ...]
    #: Did the correct cause appear in ANY step's set (plan 02 § 11.3)?
    #: ``None`` when the run produced no candidates at all.
    appeared_at_any_step: bool | None
    #: First 0-based iteration at which it appeared, or ``None`` if never. Plan
    #: 03 § 7.4's "calls before the correct diagnosis first appears", in the
    #: unit this record can measure honestly.
    first_appeared_at_iteration: int | None
    #: 1 − distinct/total over every candidate generated, by ``(category,
    #: name)``. ``None`` when nothing was generated.
    cross_step_duplicate_rate: float | None
    #: Billed candidate sets the schema refused, over billed sets total
    #: (refused + accepted). ``None`` when no step ran.
    within_step_rejection_rate: float | None
    #: How many refusals of each class, e.g. ``{"duplicate_candidate": 2}``.
    rejections_by_class: Mapping[str, int]

    def pass_at(self, k: int) -> PassAtK:
        """The ``PassAtK`` for one k.

        Raises rather than returning ``None`` for a k that was not computed: a
        caller asking for pass@3 over a run measured at ``REPORTED_KS`` has a
        bug, and a ``None`` there would be read as "no hit" by the next line of
        almost any caller.
        """
        for entry in self.pass_at_k:
            if entry.k == k:
                return entry
        raise KeyError(
            f"pass@{k} was not computed; this run measured {[e.k for e in self.pass_at_k]}"
        )


def step_of(record: StepRecord | Mapping[str, Any]) -> Step:
    """One ``StepRecord``, or its JSON form, reduced to a ``Step``.

    Both shapes, because they are the same data at two ages — see the module
    docstring. An unknown ``category`` string in a JSON record raises rather
    than being dropped: a category the enum has never heard of means the record
    was written by a different version of the agent, and silently scoring it as
    "not the ground truth" would report a miss that is really a mismatch.
    """
    if isinstance(record, StepRecord):
        return Step(
            iteration=record.iteration,
            strategy=record.strategy,
            candidates=tuple(
                Candidate(category=candidate.category, name=candidate.name, rank=rank)
                for rank, candidate in enumerate(record.candidate_set)
            ),
            action_kind=record.emitted_step.next_action.kind,
            rejections=record.generation_rejections,
        )
    emitted = record.get("emitted_step") or {}
    action = emitted.get("next_action") or {}
    return Step(
        iteration=int(record.get("iteration", 0)),
        strategy=str(record.get("strategy", "")),
        candidates=tuple(
            Candidate(
                category=HypothesisCategory(candidate["category"]),
                name=str(candidate.get("name", "")),
                rank=rank,
            )
            for rank, candidate in enumerate(record.get("candidate_set") or ())
        ),
        action_kind=str(action.get("kind", "")),
        rejections=tuple(str(reason) for reason in record.get("generation_rejections") or ()),
    )


def steps_of(records: Iterable[StepRecord | Mapping[str, Any]]) -> tuple[Step, ...]:
    """Every record, in the order given — which is the order they were made."""
    return tuple(step_of(record) for record in records)


def deciding_step(steps: Sequence[Step]) -> Step | None:
    """The step whose answer the run acted on.

    The last step that emitted ``remediate`` or ``stop``; failing that, the last
    step of any kind, for the three run shapes that end without one (budget
    exhaustion, ``max_iterations``, an escalating probe failure). ``None`` only
    when there were no steps at all — a run that never reached a planner call,
    which has no diagnosis to score and is a miss rather than a vacuum on every
    metric that takes one.

    The LAST deciding step rather than the first: the loop returns from the
    iteration that emits one, so there is normally exactly one, and a trace
    holding two is a re-run concatenated into the same file (invariant 9 keeps
    both). Taking the last reads the most recent attempt, which is the same rule
    ``evals/artifacts.py`` applies to versioned outputs.
    """
    if not steps:
        return None
    deciding = [step for step in steps if step.is_deciding]
    return deciding[-1] if deciding else steps[-1]


def pass_at_k(steps: Sequence[Step], expected: Iterable[HypothesisCategory], k: int) -> PassAtK:
    """Did the correct root cause appear in the deciding step's top ``k``?

    ``expected`` is the scenario's ``ground_truth.root_causes``. A hit is one
    candidate whose category is IN that set — not the exact-set match
    ``graders/root_cause.score_root_cause`` requires of a single diagnosis. The
    two answer different questions and plan 03 names them separately: pass@k is
    "was a correct cause available", the ROOT_CAUSE dimension is "was the
    answer right". On a single-fault ground truth, which is every labelled
    scenario in the corpus today, they coincide at k=1.
    """
    expected_set = frozenset(expected)
    step = deciding_step(steps)
    if step is None or not step.candidates:
        return PassAtK(k=k, effective_k=0, hit=None)
    effective = min(k, len(step.candidates))
    hit = any(
        candidate.category in expected_set
        for candidate in step.candidates
        if candidate.rank < effective
    )
    return PassAtK(k=k, effective_k=effective, hit=hit)


def first_appearance(steps: Sequence[Step], expected: Iterable[HypothesisCategory]) -> int | None:
    """The first iteration whose candidate set held a correct cause, or ``None``.

    Scored over the WHOLE set at each step, not the top k: "appeared at any
    step" is a question about generation, and a correct candidate ranked last is
    still a correct candidate the strategy produced.
    """
    expected_set = frozenset(expected)
    for step in steps:
        if any(candidate.category in expected_set for candidate in step.candidates):
            return step.iteration
    return None


def cross_step_duplicate_rate(steps: Sequence[Step]) -> float | None:
    """1 − distinct/total over every candidate the run generated.

    By ``(category, name)`` exactly as stated, and deliberately without
    case-folding or whitespace collapsing — the same rule
    ``candidates._one_candidate_each`` applies within a set, and for the same
    reason: this is a measurement of what the model produced, not of what a
    normaliser could hide.

    0.0 for ``baseline`` only by accident of its shape — one candidate per step,
    and a run that ranked the same top hypothesis at all five steps scores 0.8
    here. That is not a defect in the number: it is the honest reading of "this
    run considered one diagnosis five times". Compare rates within an arm, or
    between arms at the same step count; the metric is not normalised for N.
    """
    labels = [
        (candidate.category, candidate.name) for step in steps for candidate in step.candidates
    ]
    if not labels:
        return None
    return 1.0 - len(set(labels)) / len(labels)


def within_step_rejection_rate(steps: Sequence[Step]) -> float | None:
    """Billed candidate sets the schema refused, over billed sets total.

    One accepted set per step, plus each rejection recorded on it, so the
    denominator is every set the run paid for (ADR 0015 charges a rejected leg
    exactly like an accepted one). ``None`` when no step ran.
    """
    if not steps:
        return None
    rejected = sum(len(step.rejections) for step in steps)
    return rejected / (rejected + len(steps))


def rejections_by_class(steps: Sequence[Step]) -> dict[str, int]:
    """How many refusals of each validation class, for the whole run."""
    counted: dict[str, int] = {}
    for step in steps:
        for reason in step.rejections:
            counted[reason] = counted.get(reason, 0) + 1
    return dict(sorted(counted.items()))


def measure(
    records: Iterable[StepRecord | Mapping[str, Any]],
    expected: Iterable[HypothesisCategory],
    ks: Sequence[int] = REPORTED_KS,
) -> CandidateMetrics:
    """Every WP-5.2 number for one run.

    ``expected`` empty is legal and means the scenario declares no ground truth
    (9 of the 41 abstain deliberately). Every correctness field then comes back
    ``False`` / ``None`` rather than raising — but a caller must not report
    those as misses: an unlabelled scenario is not graded, it is *not graded*
    (ADR 0040), and the caller that knows which case it is in is the one that
    holds the scenario.
    """
    steps = steps_of(records)
    expected_set = frozenset(expected)
    generated = sum(len(step.candidates) for step in steps)
    appeared = first_appearance(steps, expected_set)
    return CandidateMetrics(
        strategy=steps[0].strategy if steps else "",
        steps=len(steps),
        candidates_generated=generated,
        pass_at_k=tuple(pass_at_k(steps, expected_set, k) for k in ks),
        appeared_at_any_step=None if generated == 0 else appeared is not None,
        first_appeared_at_iteration=appeared,
        cross_step_duplicate_rate=cross_step_duplicate_rate(steps),
        within_step_rejection_rate=within_step_rejection_rate(steps),
        rejections_by_class=rejections_by_class(steps),
    )
