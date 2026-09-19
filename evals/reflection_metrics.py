"""Cases fixed, cases harmed, net, and what the pass added (plan 02 § 13, 03 § 7 — WP-9.1).

Two measurements, and the report prints both. WITHIN one reflection run, the pass's own effect:
both steps are on the ``StepRecord``, so the deciding step says whether the revision fixed a
wrong diagnosis or broke a right one — a pass that recorded only its output could not be measured
for harm, which is the number that decides whether reflection ships. ACROSS arms, the paired
comparison plan 03 § 12 asks for: the same world under ``baseline`` and under ``reflection``,
paired by ``candidate_metrics.WorldKey`` and refused when the worlds differ. **Harmed is reported
beside fixed**, never folded into a net average: a positive net that hides a regression on the
obvious faults is the wrong summary.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final

from evals.candidate_metrics import WorldKey
from incident_commander.agent.hypothesis import HypothesisCategory
from incident_commander.agent.strategies.records import StepRecord

#: ``next_action.kind`` values that end the investigation loop; the step carrying one is the
#: deciding step. ``candidate_metrics``'s definition, restated rather than imported private.
_DECIDING_ACTIONS: Final[frozenset[str]] = frozenset({"remediate", "stop"})

#: The group label used when a run carries no family or difficulty. The word, not ``""``, so a
#: table row cannot be read as a group whose name is blank.
UNLABELLED: Final[str] = "unlabelled"


class RevisionOutcome(StrEnum):
    """What one revision pass, or one paired case, did to the answer."""

    FIXED = "fixed"
    HARMED = "harmed"
    UNCHANGED_CORRECT = "unchanged_correct"
    UNCHANGED_WRONG = "unchanged_wrong"
    #: The critique kept the step, so there is nothing to attribute. Within-run only.
    NOT_REVISED = "not_revised"
    #: No ground truth, or no diagnosis to grade. NOT a miss (ADR 0040).
    NOT_GRADED = "not_graded"


@dataclass(frozen=True, slots=True, kw_only=True)
class RevisedStep:
    """One planner step's reflection pass, reduced to what these metrics are computed from.

    A projection of ``StepRecord.revision`` plus the step that was emitted: the two categories
    are the whole of the fixed-or-harmed question, and carrying more would invite a metric to
    depend on it.
    """

    iteration: int
    strategy: str
    #: ``probe`` / ``remediate`` / ``stop`` of the EMITTED step.
    action_kind: str
    #: Top-1 category of what the planner proposed first. ``None`` when the record has no
    #: revision block at all — every arm but ``reflection``.
    initial_category: HypothesisCategory | None
    #: Top-1 category of the step the run acted on.
    emitted_category: HypothesisCategory | None
    verdict: str = ""
    revised: bool = False
    findings: tuple[str, ...] = ()
    passes_used: int = 0
    passes_allowed: int = 0

    @property
    def is_deciding(self) -> bool:
        return self.action_kind in _DECIDING_ACTIONS

    @property
    def had_critique(self) -> bool:
        """Did a critic read this step at all?"""
        return bool(self.verdict)


def _top_category(block: Any) -> HypothesisCategory | None:
    """The top-1 category of a step, from the model or from its JSON. ``None`` if absent.

    The ranking is normalised at the schema boundary (B-07), so index 0 is the top pick in
    both forms and no re-sorting happens here.
    """
    if block is None:
        return None
    hypotheses = (
        block.hypotheses if hasattr(block, "hypotheses") else (block.get("hypotheses") or ())
    )
    if not hypotheses:
        return None
    first = hypotheses[0]
    category = first.category if hasattr(first, "category") else first.get("category")
    return None if category is None else HypothesisCategory(category)


def revised_step_of(record: StepRecord | Mapping[str, Any]) -> RevisedStep:
    """One ``StepRecord``, or its JSON form, reduced to a ``RevisedStep``.

    An unknown category raises rather than being dropped: that record came from another
    version of the agent, and a miss there is really a mismatch.
    """
    if isinstance(record, StepRecord):
        revision = record.revision
        return RevisedStep(
            iteration=record.iteration,
            strategy=record.strategy,
            action_kind=record.emitted_step.next_action.kind,
            initial_category=(None if revision is None else _top_category(revision.initial_step)),
            emitted_category=_top_category(record.emitted_step),
            verdict="" if revision is None else revision.verdict,
            revised=bool(revision is not None and revision.revised),
            findings=() if revision is None else revision.findings,
            passes_used=0 if revision is None else revision.passes_used,
            passes_allowed=0 if revision is None else revision.passes_allowed,
        )
    block = record.get("revision")
    written: Mapping[str, Any] | None = block if isinstance(block, Mapping) else None
    emitted = record.get("emitted_step") or {}
    action = emitted.get("next_action") or {}
    return RevisedStep(
        iteration=int(record.get("iteration", 0)),
        strategy=str(record.get("strategy", "")),
        action_kind=str(action.get("kind", "")),
        initial_category=(None if written is None else _top_category(written.get("initial_step"))),
        emitted_category=_top_category(emitted),
        verdict="" if written is None else str(written.get("verdict", "")),
        revised=bool(written is not None and written.get("revised")),
        findings=(
            () if written is None else tuple(str(line) for line in written.get("findings") or ())
        ),
        passes_used=0 if written is None else int(written.get("passes_used", 0)),
        passes_allowed=0 if written is None else int(written.get("passes_allowed", 0)),
    )


def revised_steps_of(records: Iterable[StepRecord | Mapping[str, Any]]) -> tuple[RevisedStep, ...]:
    """Every record, in the order given — which is the order they were made."""
    return tuple(revised_step_of(record) for record in records)


def deciding_step(steps: Sequence[RevisedStep]) -> RevisedStep | None:
    """The step whose answer the run acted on.

    ``candidate_metrics.deciding_step``'s rule, on this projection: the LAST step emitting
    ``remediate``/``stop``, else the last step of any kind.
    """
    if not steps:
        return None
    deciding = [step for step in steps if step.is_deciding]
    return deciding[-1] if deciding else steps[-1]


def outcome_of(step: RevisedStep, expected: Iterable[HypothesisCategory]) -> RevisionOutcome:
    """What this step's pass did to the top-1 diagnosis.

    ``NOT_REVISED`` when the critique kept the step: the pass is then a cost with no effect on
    the answer, which is a different finding from one that changed nothing while revising.
    """
    expected_set = frozenset(expected)
    if not step.revised:
        return RevisionOutcome.NOT_REVISED
    if not expected_set or step.initial_category is None or step.emitted_category is None:
        return RevisionOutcome.NOT_GRADED
    was_right = step.initial_category in expected_set
    is_right = step.emitted_category in expected_set
    if was_right and not is_right:
        return RevisionOutcome.HARMED
    if is_right and not was_right:
        return RevisionOutcome.FIXED
    return RevisionOutcome.UNCHANGED_CORRECT if is_right else RevisionOutcome.UNCHANGED_WRONG


@dataclass(frozen=True, slots=True, kw_only=True)
class ReflectionMetrics:
    """Every WP-9.1 within-run number for one run, over one ground truth."""

    strategy: str
    steps: int
    #: Steps a critic read. Equal to ``steps`` on a ``reflection`` run, 0 on every other arm.
    critiques: int
    #: Steps whose critique was acted on. Below ``critiques`` by the kept ones.
    revisions: int
    #: How many findings of each class, e.g. ``{"contradiction": 2}``.
    findings_by_class: Mapping[str, int]
    #: The pass's effect at the deciding step — the one the run's answer rests on.
    deciding_outcome: RevisionOutcome
    #: Every step's outcome, in order, so a fix at step 1 undone at step 3 is visible.
    outcomes: tuple[RevisionOutcome, ...]
    #: Highest ``passes_used`` seen, and the cap it was taken against. A run whose records
    #: report more than the cap is a breach, and the report says so rather than averaging it.
    max_passes_used: int
    passes_allowed: int

    @property
    def cap_held(self) -> bool:
        """Did every step stay inside its stated cap?"""
        return self.passes_allowed == 0 or self.max_passes_used <= self.passes_allowed

    def describe(self) -> tuple[str, ...]:
        """Harmed on its own line, beside fixed, never only as a net."""
        return (
            f"critiques: {self.critiques} over {self.steps} steps; revisions: {self.revisions}",
            f"deciding step: {self.deciding_outcome.value}",
            f"cap: {self.max_passes_used} of {self.passes_allowed} passes used"
            + ("" if self.cap_held else " — CAP BREACHED"),
        )


def findings_by_class(steps: Sequence[RevisedStep]) -> dict[str, int]:
    """How many findings of each class the critiques named, over the whole run.

    Keyed by the prefix ``StepCritique.findings`` writes, so the report's classes are the
    prompt's four checks and not a second taxonomy.
    """
    counted: dict[str, int] = {}
    for step in steps:
        for line in step.findings:
            label = line.split(":", 1)[0]
            counted[label] = counted.get(label, 0) + 1
    return dict(sorted(counted.items()))


def measure_revision(
    records: Iterable[StepRecord | Mapping[str, Any]],
    expected: Iterable[HypothesisCategory],
) -> ReflectionMetrics:
    """Every WP-9.1 within-run number for one run.

    An empty ``expected`` is legal (the scenario declares no ground truth) and every
    correctness field comes back ``NOT_GRADED``. Not misses: unlabelled is NOT GRADED (ADR 0040).
    """
    steps = revised_steps_of(records)
    expected_set = frozenset(expected)
    deciding = deciding_step(steps)
    return ReflectionMetrics(
        strategy=steps[0].strategy if steps else "",
        steps=len(steps),
        critiques=sum(1 for step in steps if step.had_critique),
        revisions=sum(1 for step in steps if step.revised),
        findings_by_class=findings_by_class(steps),
        deciding_outcome=(
            RevisionOutcome.NOT_GRADED if deciding is None else outcome_of(deciding, expected_set)
        ),
        outcomes=tuple(outcome_of(step, expected_set) for step in steps),
        max_passes_used=max((step.passes_used for step in steps), default=0),
        passes_allowed=max((step.passes_allowed for step in steps), default=0),
    )


# --------------------------------------------------------------------------
# The paired comparison against the baseline arm (plan 03 § 12)
# --------------------------------------------------------------------------


class ArmsMetDifferentWorlds(ValueError):
    """Two arms were paired on runs that did not happen in the same world.

    A refusal, not a footnote: fixed-versus-harmed across worlds is a difference between
    worlds wearing the name of one between strategies, and INC-003 withdrew a "61%" for
    exactly that.
    """

    def __init__(self, left: WorldKey, right: WorldKey) -> None:
        super().__init__(
            f"cannot pair {left} with {right}: cases fixed and harmed are paired per "
            "instance (plan 03 § 12), so two runs must have met the same world "
            "(ADR 0040, ADR 0043, INC-003)."
        )
        self.left = left
        self.right = right


class TwoRunsOfOneWorld(ValueError):
    """One arm has two runs of the same world, so the pairing is ambiguous.

    Repeated runs are the plan's own protocol (03 § 10) and they are aggregated *before*
    pairing; picking one here would be picking a number.
    """

    def __init__(self, arm: str, world: WorldKey) -> None:
        super().__init__(
            f"arm {arm!r} has more than one run of {world}. Aggregate repeats into one "
            "figure per world before pairing (plan 03 § 10); choosing one of them here "
            "would be choosing the answer."
        )
        self.arm = arm
        self.world = world


@dataclass(frozen=True, slots=True, kw_only=True)
class RunCost:
    """One run's answer and its compute, as a paired comparison reads it.

    Assembled by the caller from the run's provenance and accounting: family and difficulty are
    evaluator-side scenario metadata (ADR 0038), never read off a step record.
    """

    world: WorldKey
    strategy: str
    family: str = UNLABELLED
    difficulty: str = UNLABELLED
    #: Was the top-1 diagnosis at the deciding step a correct root cause? ``None`` when the
    #: scenario carries no ground truth — not graded, rather than wrong.
    correct: bool | None = None
    #: The ``investigation_planner`` role's volume for the run.
    planner_tokens: int = 0
    #: The ``reflection_critic`` role's volume. 0 on the baseline arm, written rather than
    #: omitted so a reader never has to decide what a missing key meant.
    critic_tokens: int = 0
    tool_calls: int = 0
    llm_calls: int = 0

    @property
    def tokens(self) -> int:
        """Both roles: what the arm spent on deciding what to do next."""
        return self.planner_tokens + self.critic_tokens


@dataclass(frozen=True, slots=True, kw_only=True)
class PairedCase:
    """One world under both arms: what changed, and what the change cost."""

    world: WorldKey
    family: str
    difficulty: str
    baseline_correct: bool | None
    reflection_correct: bool | None
    outcome: RevisionOutcome
    added_tokens: int
    added_tool_calls: int
    added_llm_calls: int

    @property
    def graded(self) -> bool:
        return self.outcome is not RevisionOutcome.NOT_GRADED


@dataclass(frozen=True, slots=True, kw_only=True)
class GroupTotals:
    """One family's or one difficulty's row of the fixed/harmed/cost table."""

    group: str
    cases: int
    graded: int
    fixed: int
    harmed: int
    unchanged_correct: int
    unchanged_wrong: int
    added_tokens: int
    added_tool_calls: int

    @property
    def net(self) -> int:
        """Fixed minus harmed. Printed *after* both, never instead of them."""
        return self.fixed - self.harmed

    def describe(self) -> str:
        return (
            f"{self.group}: {self.graded} graded of {self.cases} paired — "
            f"fixed {self.fixed}, harmed {self.harmed}, net {self.net:+d}; "
            f"added {self.added_tokens} tokens, {self.added_tool_calls} tool calls"
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ReflectionComparison:
    """``reflection`` against ``baseline`` on the worlds both arms ran."""

    cases: tuple[PairedCase, ...] = ()
    #: Worlds only one arm ran. Reported, not dropped silently: an unpaired world is a hole
    #: in the comparison, and the count is what tells a reader how big a hole.
    unpaired: tuple[WorldKey, ...] = field(default_factory=tuple)

    @property
    def graded(self) -> int:
        return sum(1 for case in self.cases if case.graded)

    @property
    def fixed(self) -> int:
        return sum(1 for case in self.cases if case.outcome is RevisionOutcome.FIXED)

    @property
    def harmed(self) -> int:
        return sum(1 for case in self.cases if case.outcome is RevisionOutcome.HARMED)

    @property
    def net(self) -> int:
        return self.fixed - self.harmed

    @property
    def added_tokens(self) -> int:
        return sum(case.added_tokens for case in self.cases)

    @property
    def added_tool_calls(self) -> int:
        return sum(case.added_tool_calls for case in self.cases)

    def by_family(self) -> tuple[GroupTotals, ...]:
        """One row per family, sorted — plan 04 WP-9.2's first cut."""
        return _grouped(self.cases, lambda case: case.family)

    def by_difficulty(self) -> tuple[GroupTotals, ...]:
        return _grouped(self.cases, lambda case: case.difficulty)

    def describe(self) -> tuple[str, ...]:
        """The headline with harmed on the same footing as fixed, then the two tables."""
        return (
            f"paired worlds: {len(self.cases)} ({self.graded} graded); "
            f"unpaired: {len(self.unpaired)}",
            f"cases fixed: {self.fixed}",
            f"cases harmed: {self.harmed}",
            f"net: {self.net:+d}",
            f"added: {self.added_tokens} tokens, {self.added_tool_calls} tool calls",
            *(f"family {row.describe()}" for row in self.by_family()),
            *(f"difficulty {row.describe()}" for row in self.by_difficulty()),
        )


def _grouped(cases: Sequence[PairedCase], key: Any, /) -> tuple[GroupTotals, ...]:
    """The cases bucketed by ``key`` into one ``GroupTotals`` each, sorted by group name."""
    buckets: dict[str, list[PairedCase]] = {}
    for case in cases:
        buckets.setdefault(key(case), []).append(case)
    return tuple(
        GroupTotals(
            group=group,
            cases=len(members),
            graded=sum(1 for case in members if case.graded),
            fixed=sum(1 for case in members if case.outcome is RevisionOutcome.FIXED),
            harmed=sum(1 for case in members if case.outcome is RevisionOutcome.HARMED),
            unchanged_correct=sum(
                1 for case in members if case.outcome is RevisionOutcome.UNCHANGED_CORRECT
            ),
            unchanged_wrong=sum(
                1 for case in members if case.outcome is RevisionOutcome.UNCHANGED_WRONG
            ),
            added_tokens=sum(case.added_tokens for case in members),
            added_tool_calls=sum(case.added_tool_calls for case in members),
        )
        for group, members in sorted(buckets.items())
    )


def _by_world(arm: str, runs: Iterable[RunCost]) -> dict[WorldKey, RunCost]:
    """One arm's runs keyed by world, refusing two runs of the same one."""
    indexed: dict[WorldKey, RunCost] = {}
    for run in runs:
        if run.world in indexed:
            raise TwoRunsOfOneWorld(arm, run.world)
        indexed[run.world] = run
    return indexed


def case_outcome(baseline: bool | None, reflection: bool | None) -> RevisionOutcome:
    """The paired verdict for one world: fixed, harmed, unchanged, or not graded."""
    if baseline is None or reflection is None:
        return RevisionOutcome.NOT_GRADED
    if reflection and not baseline:
        return RevisionOutcome.FIXED
    if baseline and not reflection:
        return RevisionOutcome.HARMED
    return RevisionOutcome.UNCHANGED_CORRECT if reflection else RevisionOutcome.UNCHANGED_WRONG


def compare(
    baseline_runs: Iterable[RunCost],
    reflection_runs: Iterable[RunCost],
) -> ReflectionComparison:
    """Pair the two arms world by world, and refuse to pair across worlds.

    The pairing key is ``WorldKey``, so a canned pair is the fixtures, a recorded pair is one
    recording's fingerprint, and two LIVE runs never pair at all (``candidate_metrics``).
    """
    left = _by_world("baseline", baseline_runs)
    right = _by_world("reflection", reflection_runs)
    cases: list[PairedCase] = []
    for world in sorted(set(left) & set(right), key=str):
        control, arm = left[world], right[world]
        if control.world != arm.world:
            # Unreachable through the key above; stated because the refusal is the rule and
            # a caller that builds a case by hand must hit it too.
            raise ArmsMetDifferentWorlds(control.world, arm.world)
        cases.append(
            PairedCase(
                world=world,
                family=arm.family or UNLABELLED,
                difficulty=arm.difficulty or UNLABELLED,
                baseline_correct=control.correct,
                reflection_correct=arm.correct,
                outcome=case_outcome(control.correct, arm.correct),
                added_tokens=arm.tokens - control.tokens,
                added_tool_calls=arm.tool_calls - control.tool_calls,
                added_llm_calls=arm.llm_calls - control.llm_calls,
            )
        )
    unpaired = tuple(sorted(set(left) ^ set(right), key=str))
    return ReflectionComparison(cases=tuple(cases), unpaired=unpaired)
