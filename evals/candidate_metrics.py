"""pass@k, selected@k, the oracle gap and the duplicate rates, over step records.

Plan 02 § 11.3 / 03 § 7.2 (WP-5.2) plus plan 02 § 12 / 03 § 7.3 (WP-6.2's
``selected@k``, ``oracle_gap@k`` and the world key a paired comparison pairs on),
split out of ``evals/research_report.py`` so a phase-close or a selector packet can
use them without its archive plumbing. Input: ``StepRecord`` objects or the JSON a
trace holds them as — the same data at two ages, so a metric stays recomputable
from the evidence. Three definitions are decisions: pass@k is scored at the
DECIDING step (``appeared_at_any_step`` is the weaker question, and the gap is a
finding about the loop); k is capped at the set size and the cap is reported; and
"duplicate rate" is two numbers, within-step (a schema refusal, ADR 0042) and
cross-step (how much new N bought). Ground truth is the evaluator's (ADR 0038).
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

#: The one ``SelectionDecision`` that commits to a diagnosis. A literal, not an
#: import: this reads records already on disk, and a value the enum has since
#: renamed is a fact about an archive rather than a bug to raise on.
_SELECT: Final[str] = "select"


@dataclass(frozen=True, slots=True, kw_only=True)
class Candidate:
    """One candidate as the metrics read it: its label and its rank.

    A projection of ``CandidateRecord``: carrying its token counters and citations
    through the arithmetic would invite a metric to depend on them.
    """

    category: HypothesisCategory
    name: str
    #: 0-based position in the validated set, so ``rank < k`` is "inside the top k".
    #: Ranked by confidence at the schema boundary, never as the model typed it.
    rank: int
    #: The id the selector's ``scores`` and ``selected_candidate_id`` are keyed by
    #: (WP-6.2), so ``selected@k`` can resolve a selection to a rank.
    candidate_id: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class Selection:
    """The ``candidate_selector``'s decision over one step's set (WP-6.2).

    A projection of ``SelectorRecord``, for the reason ``Candidate`` is one of
    ``CandidateRecord``: the decision, the id it named and the uncertainty, no more.
    """

    #: ``select`` | ``probe_more`` | ``escalate``.
    decision: str
    #: ``None`` for every decision but ``select`` — the schema states a selection if
    #: and only if it commits to one (ADR 0048).
    selected_candidate_id: str | None
    #: Stated unsureness, 0.0–1.0. ``None`` when the record predates the field.
    uncertainty: float | None = None
    scores: Mapping[str, float] = field(default_factory=dict)

    @property
    def committed(self) -> bool:
        """Did the selector commit to a diagnosis at all?

        Choosing none is a MISS, not a vacuum: the oracle gap measures exactly a
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
    #: The selector's decision over this step's set, or ``None`` when none ran —
    #: every ``baseline`` step and every generator-only step.
    selection: Selection | None = None

    @property
    def is_deciding(self) -> bool:
        return self.action_kind in _DECIDING_ACTIONS


@dataclass(frozen=True, slots=True, kw_only=True)
class PassAtK:
    """pass@k for one k, with the honesty fields that keep it readable."""

    k: int
    #: ``min(k, size of the set scored)``; smaller than ``k`` says the run could not
    #: answer the k that was asked.
    effective_k: int
    #: Correct root cause in the deciding step's top ``effective_k``? ``None`` when
    #: there was no set to score.
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
    #: Total candidates generated — N × steps for an arm that had no set rejected.
    candidates_generated: int
    pass_at_k: tuple[PassAtK, ...]
    #: Did the correct cause appear in ANY step's set (plan 02 § 11.3)? ``None`` when
    #: the run produced no candidates at all.
    appeared_at_any_step: bool | None
    #: First 0-based iteration it appeared at, or ``None``. Plan 03 § 7.4's "calls
    #: before the correct diagnosis first appears", in this record's unit.
    first_appeared_at_iteration: int | None
    #: 1 − distinct/total over every candidate generated, by ``(category, name)``.
    cross_step_duplicate_rate: float | None
    #: Billed candidate sets the schema refused, over billed sets total.
    within_step_rejection_rate: float | None
    #: How many refusals of each class, e.g. ``{"duplicate_candidate": 2}``.
    rejections_by_class: Mapping[str, int]

    def pass_at(self, k: int) -> PassAtK:
        """The ``PassAtK`` for one k.

        Raises for a k that was not computed: the caller has a bug, and a ``None``
        would be read as "no hit" by almost any next line.
        """
        for entry in self.pass_at_k:
            if entry.k == k:
                return entry
        raise KeyError(
            f"pass@{k} was not computed; this run measured {[e.k for e in self.pass_at_k]}"
        )


def step_of(record: StepRecord | Mapping[str, Any]) -> Step:
    """One ``StepRecord``, or its JSON form, reduced to a ``Step``.

    An unknown ``category`` raises rather than being dropped: that record came from
    another version of the agent, and a miss there is really a mismatch.
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

    Absent (pre-Phase 6) and explicitly ``null`` (``baseline``, the generator-only
    arms) both mean no selector ran; the report tells them apart by strategy name.
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

    The LAST step emitting ``remediate``/``stop``, else the last step of any kind
    (budget exhaustion, ``max_iterations``, a probe failure). Last, not first: a
    trace with two is a re-run concatenated in (invariant 9), and newest wins.
    """
    if not steps:
        return None
    deciding = [step for step in steps if step.is_deciding]
    return deciding[-1] if deciding else steps[-1]


def pass_at_k(steps: Sequence[Step], expected: Iterable[HypothesisCategory], k: int) -> PassAtK:
    """Did the correct root cause appear in the deciding step's top ``k``?

    A hit is any candidate whose category is IN ``ground_truth.root_causes``, not
    ``root_cause.score_root_cause``'s exact-set match: pass@k asks "was a correct
    cause available", ROOT_CAUSE asks "was the answer right".
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

    Over the WHOLE set at each step, not the top k: this is a question about
    generation, and a candidate ranked last is still one the strategy produced.
    """
    expected_set = frozenset(expected)
    for step in steps:
        if any(candidate.category in expected_set for candidate in step.candidates):
            return step.iteration
    return None


def cross_step_duplicate_rate(steps: Sequence[Step]) -> float | None:
    """1 − distinct/total over every candidate the run generated.

    By ``(category, name)``, with no case-folding: ``candidates._one_candidate_each``'s
    rule. NOT normalised for N — a ``baseline`` run that ranked one hypothesis five
    times scores 0.8 — so compare within an arm, or across arms at one step count.
    """
    labels = [
        (candidate.category, candidate.name) for step in steps for candidate in step.candidates
    ]
    if not labels:
        return None
    return 1.0 - len(set(labels)) / len(labels)


def within_step_rejection_rate(steps: Sequence[Step]) -> float | None:
    """Billed candidate sets the schema refused, over billed sets total.

    The denominator is every set the run paid for — one accepted per step plus each
    rejection on it — because ADR 0015 charges a rejected leg like an accepted one.
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

    An empty ``expected`` is legal (the scenario declares no ground truth) and every
    correctness field then comes back ``False``/``None``. Not misses: unlabelled is
    NOT GRADED (ADR 0040).
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

    ``oracle_gap@k = pass@k − selected@k`` is PAIRED (plan 03 § 12, 03:145): across
    worlds it is a difference between worlds wearing the name of one between
    capabilities. A refusal, not a footnote, because INC-003 withdrew a "61%".
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


#: How a run's world is identified per execution mode. ``canned``: the committed
#: fixtures ARE the world, so two canned runs pair. ``live``: keyed by the ARCHIVE,
#: so two live runs never pair — nothing guarantees they met the same database or
#: the same seeded fault (INC-003), and refusing a comparison that might be sound
#: beats reporting one that might not be. ``recorded``: a recording IS a world
#: (ADR 0043), keyed by ``recorder.world_fingerprint`` — not the path and not the
#: archive — which the runner already carries as ``replay["world_fingerprint"]``.
_WORLD_INSTANCE: Final[Mapping[str, str]] = MappingProxyType(
    {"canned": "fixtures", "live": "archive", "recorded": "world_fingerprint"}
)


@dataclass(frozen=True, slots=True, kw_only=True)
class WorldKey:
    """Which world one run happened in — the unit a paired comparison pairs on.

    Not the scenario NAME (what the research report pairs on today, and says so in
    its limits): a name identifies the template, this the instance.
    """

    scenario: str
    #: ``canned`` / ``live`` / ``recorded`` — ``RunProvenance.execution_mode``.
    mode: str
    #: What distinguishes this world from another of the same mode; rule per mode in
    #: ``_WORLD_INSTANCE``.
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

    ``world_fingerprint`` (``evals/recorder.world_fingerprint``, carried by the runner
    as ``replay["world_fingerprint"]``) is required for a recorded run and ignored
    otherwise: neither a canned nor a live world has a fingerprint to pair on.
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
    #: ``True`` for a selected candidate that is a correct root cause inside the top
    #: ``effective_k``; ``False`` for anything else, including committing to nothing
    #: (``probe_more``/``escalate`` are not selections); ``None`` when no selector
    #: ran or there was no set — not graded, rather than a miss.
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

    1 when a correct candidate was in the top k and the selector did not take it.
    It cannot be −1 (``selected@k ⟹ pass@k``), and that is asserted rather than
    assumed: a negative gap would read as a candidate the generator never produced.
    """

    k: int
    world: WorldKey
    pass_hit: bool | None
    selected_hit: bool | None
    #: ``None`` when either term is not scored: reporting a one-term gap as 0 would
    #: say "selection cost nothing" about a run that never measured it.
    gap: int | None
    not_scored_because: str = ""


def selected_at_k(
    steps: Sequence[Step], expected: Iterable[HypothesisCategory], k: int
) -> SelectedAtK:
    """Did the selector commit to a correct candidate at the deciding step?

    Same step and truncation as ``pass_at_k``, so the two are comparable. Committing
    to nothing is a MISS: "not graded" would make the selector look best exactly
    when it decided least.
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
        # An id naming no candidate in the set. The schema refuses one (ADR 0048), so
        # this archive came from another version of the agent — a miss rather than a
        # raise, because the run really did commit to something unresolvable.
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

    ``against`` is the world the other term was measured in when a caller pairs two
    runs; a different one raises ``OracleGapAcrossWorlds``. The ordinary case passes
    nothing and is paired by construction.
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
    #: Plan 04 WP-6.2's "uncertainty vs correctness": the stated unsureness at the
    #: deciding step, paired with whether it committed to a correct candidate. A
    #: pair, never two columns — an uncertainty without its verdict cannot be
    #: calibrated, which is the whole of plan 03 § 9.
    uncertainty: float | None
    uncertainty_was_right: bool | None
    #: The deciding step's decision, or ``""``. Beside the gap because a miss for
    #: ``escalate`` and a miss for a wrong ``select`` are different findings.
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

    Takes the world rather than deriving it: the world is a property of the RUN's
    provenance, not of its step records — the reason ``measure`` takes the ground
    truth rather than reading it off the archive.
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
