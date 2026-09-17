"""pass@k, selected@k, the oracle gap and the duplicate rates, over step records.

Plan 02 § 11.3 and plan 03 § 7.2 (WP-5.2's reporting half), plus plan 02 § 12 and
plan 03 § 7.3 (WP-6.2's: ``selected@k``, ``oracle_gap@k`` and the world key a
paired comparison pairs on). The arithmetic lives
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
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Final

from incident_commander.agent.hypothesis import HypothesisCategory
from incident_commander.agent.strategies.records import StepRecord

#: The ks plan 02 § 11.3 reports. A run whose sets are smaller answers the
#: larger ones at its own size, and says so through ``effective_k``.
REPORTED_KS: Final[tuple[int, ...]] = (1, 2, 4, 8)

#: ``next_action.kind`` values that end the investigation loop. The step
#: carrying one is the deciding step (plan 02 § 11.3).
_DECIDING_ACTIONS: Final[frozenset[str]] = frozenset({"remediate", "stop"})

#: The one ``SelectionDecision`` that commits to a diagnosis. A literal rather
#: than an import of the enum: nothing here may import
#: ``src/incident_commander`` beyond the two label types it already needs, and
#: this module reads records that are already on disk — a value the enum has
#: since renamed is a fact about an archive, not a bug to raise on.
_SELECT: Final[str] = "select"


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
    #: The id the selector's ``scores`` and ``selected_candidate_id`` are keyed
    #: by (WP-6.2). Carried so ``selected@k`` can resolve a selection to a rank;
    #: pass@k never reads it.
    candidate_id: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class Selection:
    """The ``candidate_selector``'s decision over one step's set (WP-6.2).

    A projection of ``SelectorRecord``, for the same reason ``Candidate`` is one
    of ``CandidateRecord``: ``selected@k`` needs the decision, the id it named
    and the uncertainty it stated, and carrying the call id through the
    arithmetic would invite a metric to depend on it.
    """

    #: ``select`` | ``probe_more`` | ``escalate``.
    decision: str
    #: ``None`` for every decision but ``select`` — the schema states a selection
    #: if and only if it commits to one (ADR 0048).
    selected_candidate_id: str | None
    #: The selector's stated unsureness, 0.0–1.0. ``None`` when the record
    #: predates the field.
    uncertainty: float | None = None
    scores: Mapping[str, float] = field(default_factory=dict)

    @property
    def committed(self) -> bool:
        """Did the selector commit to a diagnosis at all?

        ``selected@k`` is "the selector chose a CORRECT candidate", so a decision
        that chose none is a miss rather than a vacuum: the run went on without a
        committed diagnosis, and the oracle gap is exactly the measure of a
        correct candidate that was available and not taken.
        """
        return self.decision == _SELECT and self.selected_candidate_id is not None


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
    #: The selector's decision over this step's set, or ``None`` when no selector
    #: ran — which is every ``baseline`` step and every generator-only step.
    selection: Selection | None = None

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
                Candidate(
                    category=candidate.category,
                    name=candidate.name,
                    rank=rank,
                    candidate_id=candidate.candidate_id,
                )
                for rank, candidate in enumerate(record.candidate_set)
            ),
            action_kind=record.emitted_step.next_action.kind,
            rejections=record.generation_rejections,
            selection=(
                None
                if record.selector is None
                else Selection(
                    decision=record.selector.decision,
                    selected_candidate_id=record.selector.selected_candidate_id,
                    uncertainty=record.selector.uncertainty,
                    scores=dict(record.selector.scores),
                )
            ),
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
                candidate_id=str(candidate.get("candidate_id", "")),
            )
            for rank, candidate in enumerate(record.get("candidate_set") or ())
        ),
        action_kind=str(action.get("kind", "")),
        rejections=tuple(str(reason) for reason in record.get("generation_rejections") or ()),
        selection=_selection_of(record.get("selector")),
    )


def _selection_of(block: Any) -> Selection | None:
    """One trace record's ``selector`` block, or ``None``.

    ``None`` when the key is absent (a record written before Phase 6) and when it
    is explicitly ``null`` (``baseline`` and the generator-only arms write it that
    way). The two mean the same thing here — no selector ran — and the report
    tells them apart by the strategy name, which it already carries.
    """
    if not isinstance(block, Mapping):
        return None
    uncertainty = block.get("uncertainty")
    selected = block.get("selected_candidate_id")
    return Selection(
        decision=str(block.get("decision", "")),
        selected_candidate_id=None if selected is None else str(selected),
        uncertainty=None if uncertainty is None else float(uncertainty),
        scores={str(key): float(value) for key, value in (block.get("scores") or {}).items()},
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


# --------------------------------------------------------------------------
# selected@k and the oracle gap (WP-6.2, plan 03 § 7.3)
# --------------------------------------------------------------------------


class OracleGapAcrossWorlds(ValueError):
    """An oracle gap was asked for across two different worlds.

    ``oracle_gap@k = pass@k − selected@k`` is a PAIRED difference: both terms have
    to be about the same instance, or the number is the difference between two
    worlds wearing the name of a difference between two capabilities (plan 03 § 12,
    03:145, "compare arms on the same instances"). This is the refusal, and it is a
    refusal rather than a footnote because INC-003 is what a footnote costs: a
    label written about one world, applied to another, produced a live
    root-cause figure of "61%" that meant nothing and had to be withdrawn.
    """

    def __init__(self, left: WorldKey, right: WorldKey) -> None:
        super().__init__(
            f"cannot compute an oracle gap across two worlds: {left} vs {right}. "
            "pass@k and selected@k are paired per instance (plan 03 § 12); a "
            "difference computed across worlds is not a paired comparison "
            "(ADR 0040, INC-003)."
        )
        self.left = left
        self.right = right


#: How a run's world is identified per execution mode.
#:
#: ``canned`` — the fixtures ARE the world and they are committed, so two canned
#: runs of one scenario ran in the same world and are paired.
#:
#: ``live`` — a live world is only ever itself. Nothing guarantees that two live
#: runs of one scenario met the same database, the same queue depth or the same
#: seeded fault (INC-003 is the case where "the same scenario" spanned two
#: worlds), so the world is keyed by the ARCHIVE and two live runs never pair.
#: Conservative in the one direction that matters: it refuses a comparison that
#: might be sound rather than reporting one that might not be.
#:
#: ``recorded`` — a recording is a world (ADR 0043), so the recording's own
#: identity is the key and two arms replayed against one recording DO pair. That
#: identity is ``recorder.world_fingerprint``, which is the repo's existing answer
#: to "are these two recordings of the same world": the same scenario, the same
#: label, the same calls with the same wired arguments and the same answers, minus
#: the volatile fields and the provenance block. Not the file path and not the
#: archive — a path is a name and an archive is a run, and two archives that
#: replayed one recording are the paired comparison plan 03 § 5 puts strategy
#: comparisons in. The runner already carries it on every recorded outcome as
#: ``ScenarioOutcome.replay["world_fingerprint"]`` (WP-3.3), so nothing new is
#: minted here.
_WORLD_INSTANCE: Final[Mapping[str, str]] = MappingProxyType(
    {"canned": "fixtures", "live": "archive", "recorded": "world_fingerprint"}
)


@dataclass(frozen=True, slots=True, kw_only=True)
class WorldKey:
    """Which world one run happened in — the unit a paired comparison pairs on.

    Not the scenario NAME, which is what the research report pairs on today and
    says so in its own limits. A name identifies the template; this identifies the
    instance, and the whole value of the oracle gap is that its two terms are
    measured on one instance.
    """

    scenario: str
    #: ``canned`` / ``live`` / ``recorded`` — ``RunProvenance.execution_mode``.
    mode: str
    #: What distinguishes this world from another of the same mode. See
    #: ``_WORLD_INSTANCE`` for the rule per mode.
    instance: str

    def __str__(self) -> str:
        return f"{self.scenario}@{self.mode}:{self.instance}"


def world_key(
    *,
    scenario: str,
    execution_mode: str,
    archive: str,
    world_fingerprint: str | None = None,
) -> WorldKey:
    """The world one run happened in.

    ``world_fingerprint`` is the recording's own identity — the value the runner
    puts on every recorded outcome as ``replay["world_fingerprint"]``, computed by
    ``evals/recorder.world_fingerprint``. Required for a recorded run and ignored
    for every other mode: a canned world is its committed fixtures and a live
    world is only itself, so neither has a fingerprint to pair on.
    """
    names = _WORLD_INSTANCE.get(execution_mode)
    if names is None:
        raise ValueError(
            f"unknown execution mode {execution_mode!r} for {scenario!r}; a world "
            f"key has a rule per mode and this one has none (known: "
            f"{', '.join(sorted(_WORLD_INSTANCE))}). A mode with no rule must not "
            "fall back to a pairing rule chosen for another mode."
        )
    if names == "world_fingerprint":
        if not world_fingerprint:
            raise ValueError(
                f"a recorded run of {scenario!r} carries no world fingerprint; a "
                "recording IS the world (ADR 0043), so without the recording's own "
                "identity there is nothing to pair on. The runner puts it on every "
                'recorded outcome as replay["world_fingerprint"].'
            )
        return WorldKey(scenario=scenario, mode=execution_mode, instance=world_fingerprint)
    instance = archive if names == "archive" else names
    return WorldKey(scenario=scenario, mode=execution_mode, instance=instance)


@dataclass(frozen=True, slots=True, kw_only=True)
class SelectedAtK:
    """selected@k for one k: did the selector COMMIT to a correct candidate?"""

    k: int
    effective_k: int
    #: ``True`` when the selector selected a candidate whose category is a correct
    #: root cause and whose rank is inside the top ``effective_k``. ``False`` when
    #: it selected something else, or committed to nothing (``probe_more`` /
    #: ``escalate`` are not selections). ``None`` when no selector ran at all, or
    #: there was no set to select from — not graded, rather than a miss.
    hit: bool | None
    #: Why ``hit`` is ``None``, in the report's own words. Empty when it is not.
    not_scored_because: str = ""

    def describe(self) -> str:
        if self.hit is None:
            return f"selected@{self.k}: not scored — {self.not_scored_because}"
        verdict = "hit" if self.hit else "miss"
        cap = "" if self.effective_k == self.k else f" (capped at {self.effective_k})"
        return f"selected@{self.k}: {verdict}{cap}"


@dataclass(frozen=True, slots=True, kw_only=True)
class OracleGap:
    """``pass@k − selected@k`` for one run at one k, and the pair behind it.

    The gap is 1 when a correct candidate was in the top k and the selector did
    not commit to it, and 0 otherwise. It cannot be −1: a selection that hits is a
    correct candidate inside the top k, which is exactly pass@k's own condition,
    so ``selected@k ⟹ pass@k``. ``measure_selection`` asserts that rather than
    assuming it, because a negative gap in a report would read as the selector
    finding a candidate the generator did not produce.
    """

    k: int
    world: WorldKey
    pass_hit: bool | None
    selected_hit: bool | None
    #: ``None`` when either term is not scored. A gap with one term is not a gap,
    #: and reporting it as 0 would say "selection cost nothing" about a run where
    #: selection was never measured.
    gap: int | None
    not_scored_because: str = ""


def selected_at_k(
    steps: Sequence[Step], expected: Iterable[HypothesisCategory], k: int
) -> SelectedAtK:
    """Did the selector commit to a correct candidate at the deciding step?

    Scored at the same step as ``pass_at_k`` and against the same truncation, so
    the two are comparable by construction: a selection of a candidate ranked
    outside the top ``k`` is a miss at ``k``, exactly as a correct candidate
    ranked outside it is a miss for pass@k.

    Committing to nothing is a MISS and not an abstention. ``probe_more`` and
    ``escalate`` are real answers about the run — it went on, or it stopped,
    without a diagnosis — and the oracle gap exists precisely to measure a
    correct candidate that was available and not taken. Scoring them as "not
    graded" would make the selector look best exactly when it decided least.
    """
    expected_set = frozenset(expected)
    step = deciding_step(steps)
    if step is None or not step.candidates:
        return SelectedAtK(
            k=k, effective_k=0, hit=None, not_scored_because="the run produced no candidate set"
        )
    effective = min(k, len(step.candidates))
    if step.selection is None:
        return SelectedAtK(
            k=k,
            effective_k=effective,
            hit=None,
            not_scored_because="no candidate_selector ran on this step",
        )
    if not step.selection.committed:
        return SelectedAtK(k=k, effective_k=effective, hit=False)
    chosen = next(
        (
            candidate
            for candidate in step.candidates
            if candidate.candidate_id == step.selection.selected_candidate_id
        ),
        None,
    )
    if chosen is None:
        # An id that names no candidate in the recorded set. The schema refuses
        # one (ADR 0048), so this is an archive written by a different version of
        # the agent — reported as a miss rather than raising, because the record
        # is evidence and the run really did commit to something unresolvable.
        return SelectedAtK(k=k, effective_k=effective, hit=False)
    return SelectedAtK(
        k=k,
        effective_k=effective,
        hit=chosen.rank < effective and chosen.category in expected_set,
    )


def oracle_gap_at_k(
    steps: Sequence[Step],
    expected: Iterable[HypothesisCategory],
    k: int,
    *,
    world: WorldKey,
    against: WorldKey | None = None,
) -> OracleGap:
    """``pass@k − selected@k`` for one run, refusing an unpaired comparison.

    ``against`` is the world the other term was measured in, when the caller is
    pairing two runs. Passing a different world raises
    ``OracleGapAcrossWorlds``. The ordinary case — both terms off the same run's
    own steps — passes nothing and is paired by construction.
    """
    if against is not None and against != world:
        raise OracleGapAcrossWorlds(world, against)
    expected_set = frozenset(expected)
    available = pass_at_k(steps, expected_set, k)
    committed = selected_at_k(steps, expected_set, k)
    if available.hit is None or committed.hit is None:
        return OracleGap(
            k=k,
            world=world,
            pass_hit=available.hit,
            selected_hit=committed.hit,
            gap=None,
            not_scored_because=committed.not_scored_because or "the run produced no candidate set",
        )
    gap = int(available.hit) - int(committed.hit)
    if gap < 0:
        raise ValueError(
            f"oracle gap {gap} at k={k} for {world}: selected@k hit while pass@k "
            "missed, which is impossible — a selection that hits is a correct "
            "candidate inside the top k. The record's candidate ranks and its "
            "selector block disagree."
        )
    return OracleGap(k=k, world=world, pass_hit=available.hit, selected_hit=committed.hit, gap=gap)


@dataclass(frozen=True, slots=True, kw_only=True)
class SelectionMetrics:
    """Every WP-6.2 number for one run, over one ground truth and one world."""

    world: WorldKey
    strategy: str
    #: How many steps a selector actually ran on. 0 for every arm but one.
    selector_calls: int
    selected_at_k: tuple[SelectedAtK, ...]
    oracle_gap: tuple[OracleGap, ...]
    #: The selector's stated unsureness at the deciding step, paired with whether
    #: it committed to a correct candidate — plan 04 WP-6.2's "uncertainty vs
    #: correctness". ``None`` on either side when the run did not measure it.
    #: A pair, never two columns: an uncertainty without its verdict is a number
    #: nobody can calibrate, which is the whole of plan 03 § 9.
    uncertainty: float | None
    uncertainty_was_right: bool | None
    #: The decision the deciding step took, or ``""``. Reported beside the gap
    #: because a miss for ``escalate`` and a miss for a wrong ``select`` are
    #: different findings about the selector.
    decision: str

    def selected_at(self, k: int) -> SelectedAtK:
        for entry in self.selected_at_k:
            if entry.k == k:
                return entry
        raise KeyError(
            f"selected@{k} was not computed; this run measured {[e.k for e in self.selected_at_k]}"
        )


def measure_selection(
    records: Iterable[StepRecord | Mapping[str, Any]],
    expected: Iterable[HypothesisCategory],
    *,
    world: WorldKey,
    ks: Sequence[int] = REPORTED_KS,
) -> SelectionMetrics:
    """Every WP-6.2 number for one run.

    Takes the world it was measured in rather than deriving one, because the
    world is a property of the RUN (its provenance) and not of its step records —
    the same reason ``measure`` takes the ground truth rather than reading it off
    the archive.
    """
    steps = steps_of(records)
    expected_set = frozenset(expected)
    step = deciding_step(steps)
    selection = step.selection if step is not None else None
    committed = selected_at_k(steps, expected_set, 1)
    return SelectionMetrics(
        world=world,
        strategy=steps[0].strategy if steps else "",
        selector_calls=sum(1 for entry in steps if entry.selection is not None),
        selected_at_k=tuple(selected_at_k(steps, expected_set, k) for k in ks),
        oracle_gap=tuple(oracle_gap_at_k(steps, expected_set, k, world=world) for k in ks),
        uncertainty=None if selection is None else selection.uncertainty,
        uncertainty_was_right=committed.hit,
        decision="" if selection is None else selection.decision,
    )
