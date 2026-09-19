"""The uncertainty policy: the thresholds that decide when to buy more inference (WP-13.1).

Plan 02 § 15's escalation signals, each a named threshold with a declared default and the
benchmark split that default was set on; a default declared on the holdout is refused here
rather than reviewed (plan 03 § 4, ADR 0061). These are a NEW set — the loop's remediate bar
is untouched and reported rather than tuned (plan 02 § 16) — and every number in this module
lives in a ``ThresholdDefault`` row, so a threshold cannot be written without its provenance.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from incident_commander.agent.candidates import DiagnosisCandidate
from incident_commander.agent.planner_context import ATTEMPT_FAILED_MARKER
from incident_commander.agent.state import RunState


class EscalationSignal(StrEnum):
    """Plan 02 § 15's escalation signals, closed. A member lands only with its threshold."""

    TOP1_CONFIDENCE_LOW = "top1_confidence_low"
    TOP1_TOP2_MARGIN_NARROW = "top1_top2_margin_narrow"
    SELECTOR_UNCERTAINTY_HIGH = "selector_uncertainty_high"
    CANDIDATE_DISAGREEMENT_HIGH = "candidate_disagreement_high"
    CONTRADICTORY_EVIDENCE = "contradictory_evidence"
    REMEDIATION_ATTEMPT_FAILED = "remediation_attempt_failed"
    CONFIDENCE_LOW_AFTER_K_PROBES = "confidence_low_after_k_probes"


class ThresholdName(StrEnum):
    """Every tunable number here; the value is also its ``UncertaintyThresholds`` field name.

    A ``_floor`` fires BELOW itself, a ``_ceiling`` fires ABOVE itself and a ``_count`` fires AT
    itself — three suffixes for three comparisons, so a reader need not open ``evaluate``.
    """

    TOP1_CONFIDENCE_FLOOR = "top1_confidence_floor"
    TOP1_TOP2_MARGIN_FLOOR = "top1_top2_margin_floor"
    SELECTOR_UNCERTAINTY_CEILING = "selector_uncertainty_ceiling"
    CANDIDATE_DISAGREEMENT_CEILING = "candidate_disagreement_ceiling"
    CONTRADICTORY_EVIDENCE_COUNT = "contradictory_evidence_count"
    FAILED_ATTEMPT_COUNT = "failed_attempt_count"
    PROBE_COUNT_BEFORE_CONFIDENCE_CHECK = "probe_count_before_confidence_check"
    CONFIDENCE_FLOOR_AFTER_PROBES = "confidence_floor_after_probes"


class ThresholdSplit(StrEnum):
    """Which split a default was set on.

    ``dev``/``validation``/``holdout`` are ``evals.scenarios.schema.BenchmarkSplit``'s members,
    pinned to it by a test rather than imported — nothing under ``agent/`` imports the harness.
    ``untuned`` is the fourth and, today, the honest one: a declared starting point no run has
    moved. It is a real provenance value, not a missing one, and the report prints it.
    """

    DEV = "dev"
    VALIDATION = "validation"
    HOLDOUT = "holdout"
    UNTUNED = "untuned"


#: The splits a default may be set on. ``holdout`` is absent by design (plan 03 § 4).
TUNABLE_SPLITS: Final[frozenset[ThresholdSplit]] = frozenset(
    {ThresholdSplit.DEV, ThresholdSplit.VALIDATION, ThresholdSplit.UNTUNED}
)

#: Prefix every threshold's environment variable carries, so the ``Settings`` field names and
#: these names are ONE derivation instead of two lists that drift.
ENV_PREFIX: Final[str] = "UNCERTAINTY_"


class HoldoutTunedThresholdError(ValueError):
    """A threshold's default was declared from a holdout-split run (plan 03 § 4)."""

    def __init__(self, what: str) -> None:
        super().__init__(
            f"{what} declares a default tuned on the holdout split. The holdout is never tuned "
            "against, and a threshold set from it invalidates every later claim made on that "
            "split (plan 03 § 4, ADR 0061). Re-derive the number on dev or validation and "
            "declare that split, or leave the default untuned."
        )
        self.what = what


def assert_tuning_split_allowed(split: ThresholdSplit, what: str) -> None:
    """Refuse a default derived from the holdout. The mechanism, not a convention."""
    if split not in TUNABLE_SPLITS:
        raise HoldoutTunedThresholdError(what)


def env_var_for(name: ThresholdName) -> str:
    """The environment variable that overrides ``name``'s declared default."""
    return f"{ENV_PREFIX}{name.value.upper()}"


def settings_field_for(name: ThresholdName) -> str:
    """The ``config.Settings`` field carrying that override."""
    return env_var_for(name).lower()


@dataclass(frozen=True, slots=True, kw_only=True)
class ThresholdDefault:
    """One declared default: the number, the signal it serves, its split and its source."""

    name: ThresholdName
    signal: EscalationSignal
    value: float
    split: ThresholdSplit
    source: str
    rationale: str

    def __post_init__(self) -> None:
        assert_tuning_split_allowed(self.split, env_var_for(self.name))

    @property
    def env_var(self) -> str:
        """The environment variable an operator overrides this default with."""
        return env_var_for(self.name)


# The ONE place a number in this packet is written. Every row names the split its value came
# from, and `__post_init__` refuses `holdout`, so "never tuned on the holdout" is a property of
# the declaration rather than of anyone's memory. Nothing here has been tuned yet: the sweep
# that would move these is WP-13.2's, and until it runs `untuned` is the true provenance.
_DECLARED: Final[tuple[ThresholdDefault, ...]] = (
    ThresholdDefault(
        name=ThresholdName.TOP1_CONFIDENCE_FLOOR,
        signal=EscalationSignal.TOP1_CONFIDENCE_LOW,
        value=0.75,
        split=ThresholdSplit.UNTUNED,
        source="WO-R3-234: declared, no run behind it",
        rationale=(
            "A band ABOVE the loop's remediate bar, so the ladder buys inference while the run "
            "is still inside the region where that gate would refuse to act, never after it "
            "has refused. Deliberately not equal to the bar: an equal default would read as "
            "the gate itself having moved into configuration."
        ),
    ),
    ThresholdDefault(
        name=ThresholdName.TOP1_TOP2_MARGIN_FLOOR,
        signal=EscalationSignal.TOP1_TOP2_MARGIN_NARROW,
        value=0.15,
        split=ThresholdSplit.UNTUNED,
        source="WO-R3-234: declared, no run behind it",
        rationale=(
            "Two hypotheses this close are not separated by the evidence read so far, whatever "
            "the top one's absolute confidence is; the family scenarios are built to produce "
            "exactly that shape (ADR 0051, ADR 0053)."
        ),
    ),
    ThresholdDefault(
        name=ThresholdName.SELECTOR_UNCERTAINTY_CEILING,
        signal=EscalationSignal.SELECTOR_UNCERTAINTY_HIGH,
        value=0.4,
        split=ThresholdSplit.UNTUNED,
        source="WO-R3-234: declared, no run behind it",
        rationale=(
            "Read from `SelectionResult.uncertainty` (ADR 0048). Self-reported, and the "
            "selector's own numbers are withheld until its arm is calibrated (ADR 0049), so "
            "this default is a starting point and the sweep that sets it is WP-13.2's."
        ),
    ),
    ThresholdDefault(
        name=ThresholdName.CANDIDATE_DISAGREEMENT_CEILING,
        signal=EscalationSignal.CANDIDATE_DISAGREEMENT_HIGH,
        value=0.5,
        split=ThresholdSplit.UNTUNED,
        source="WO-R3-234: declared, no run behind it",
        rationale=(
            "The share of a candidate set whose category is not the leader's. Above this, more "
            "than half the set disagrees with what the arm is about to commit to."
        ),
    ),
    ThresholdDefault(
        name=ThresholdName.CONTRADICTORY_EVIDENCE_COUNT,
        signal=EscalationSignal.CONTRADICTORY_EVIDENCE,
        value=1,
        split=ThresholdSplit.UNTUNED,
        source="WO-R3-234: declared, no run behind it",
        rationale=(
            "One `evidence_against` reference on the leading candidate (ADR 0042) is already a "
            "contradiction the arm grounded in a probe it read; counting them is the whole "
            "measurement, so the count that fires is one."
        ),
    ),
    ThresholdDefault(
        name=ThresholdName.FAILED_ATTEMPT_COUNT,
        signal=EscalationSignal.REMEDIATION_ATTEMPT_FAILED,
        value=1,
        split=ThresholdSplit.UNTUNED,
        source="WO-R3-234: declared, no run behind it",
        rationale=(
            "One attempt record on the run (ADR 0056) means the world answered a Tier-1 action "
            "and the incident is still open — the strongest evidence available that the "
            "diagnosis was wrong, and the reinvestigation that follows is worth more inference."
        ),
    ),
    ThresholdDefault(
        name=ThresholdName.PROBE_COUNT_BEFORE_CONFIDENCE_CHECK,
        signal=EscalationSignal.CONFIDENCE_LOW_AFTER_K_PROBES,
        value=3,
        split=ThresholdSplit.UNTUNED,
        source="WO-R3-234: declared, no run behind it",
        rationale=(
            "K, counted as probe tool calls on the run's ledger. Three of the loop's five "
            "default iterations: enough evidence that a low confidence is the evidence's "
            "verdict rather than the first step's."
        ),
    ),
    ThresholdDefault(
        name=ThresholdName.CONFIDENCE_FLOOR_AFTER_PROBES,
        signal=EscalationSignal.CONFIDENCE_LOW_AFTER_K_PROBES,
        value=0.85,
        split=ThresholdSplit.UNTUNED,
        source="WO-R3-234: declared, no run behind it",
        rationale=(
            "Higher than the instantaneous floor on purpose: after K probes the bar for 'this "
            "has converged' is higher than at the first step, and a floor equal to the other "
            "one would make this signal a subset of it that never fires alone."
        ),
    ),
)

#: The declared defaults, by name. The report reads its splits from here.
UNCERTAINTY_DEFAULTS: Final[Mapping[ThresholdName, ThresholdDefault]] = MappingProxyType(
    {declared.name: declared for declared in _DECLARED}
)


def declared_default(name: ThresholdName) -> float:
    """``name``'s declared default, whatever an operator has overridden it with."""
    return UNCERTAINTY_DEFAULTS[name].value


def _declared_count(name: ThresholdName) -> int:
    """The same, for the three thresholds that count things rather than score them."""
    return int(declared_default(name))


@dataclass(frozen=True, slots=True, kw_only=True)
class UncertaintyThresholds:
    """The operating point the policy compares against, defaulting to the declared table.

    Built at the edge from ``Settings`` like ``StrategyKnobs`` is, so nothing under ``agent/``
    reads configuration. An unset override means the declared default, which is the only value
    with a split behind it.
    """

    top1_confidence_floor: float = declared_default(ThresholdName.TOP1_CONFIDENCE_FLOOR)
    top1_top2_margin_floor: float = declared_default(ThresholdName.TOP1_TOP2_MARGIN_FLOOR)
    selector_uncertainty_ceiling: float = declared_default(
        ThresholdName.SELECTOR_UNCERTAINTY_CEILING
    )
    candidate_disagreement_ceiling: float = declared_default(
        ThresholdName.CANDIDATE_DISAGREEMENT_CEILING
    )
    contradictory_evidence_count: int = _declared_count(ThresholdName.CONTRADICTORY_EVIDENCE_COUNT)
    failed_attempt_count: int = _declared_count(ThresholdName.FAILED_ATTEMPT_COUNT)
    probe_count_before_confidence_check: int = _declared_count(
        ThresholdName.PROBE_COUNT_BEFORE_CONFIDENCE_CHECK
    )
    confidence_floor_after_probes: float = declared_default(
        ThresholdName.CONFIDENCE_FLOOR_AFTER_PROBES
    )

    @classmethod
    def resolve(
        cls,
        *,
        top1_confidence_floor: float | None = None,
        top1_top2_margin_floor: float | None = None,
        selector_uncertainty_ceiling: float | None = None,
        candidate_disagreement_ceiling: float | None = None,
        contradictory_evidence_count: int | None = None,
        failed_attempt_count: int | None = None,
        probe_count_before_confidence_check: int | None = None,
        confidence_floor_after_probes: float | None = None,
    ) -> UncertaintyThresholds:
        """Build a policy from optional overrides; ``None`` means the declared default.

        Spelled out rather than taking a mapping: the edge hands over eight ``Settings``
        fields, and a mapping would let a typo become a silently ignored knob.
        """
        declared = cls()
        return cls(
            top1_confidence_floor=_or_declared(
                top1_confidence_floor, declared.top1_confidence_floor
            ),
            top1_top2_margin_floor=_or_declared(
                top1_top2_margin_floor, declared.top1_top2_margin_floor
            ),
            selector_uncertainty_ceiling=_or_declared(
                selector_uncertainty_ceiling, declared.selector_uncertainty_ceiling
            ),
            candidate_disagreement_ceiling=_or_declared(
                candidate_disagreement_ceiling, declared.candidate_disagreement_ceiling
            ),
            contradictory_evidence_count=_or_declared(
                contradictory_evidence_count, declared.contradictory_evidence_count
            ),
            failed_attempt_count=_or_declared(failed_attempt_count, declared.failed_attempt_count),
            probe_count_before_confidence_check=_or_declared(
                probe_count_before_confidence_check, declared.probe_count_before_confidence_check
            ),
            confidence_floor_after_probes=_or_declared(
                confidence_floor_after_probes, declared.confidence_floor_after_probes
            ),
        )

    def value_of(self, name: ThresholdName) -> float:
        """This policy's live value for ``name``. The enum value is the field name."""
        return float(getattr(self, name.value))

    def as_strategy_config(self) -> Mapping[str, Mapping[str, object]]:
        """The provenance block a run's report stamps: value, default and the default's split.

        One entry per threshold, so a reported number cannot be read apart from where it came
        from. ``adaptive`` (WP-13.2) is what puts this into ``strategy_config``.
        """
        return MappingProxyType({row.name.value: row.as_config() for row in provenance_rows(self)})


def _or_declared[T: (float, int)](override: T | None, declared: T) -> T:
    """The override when an operator set one, else the declared default."""
    return declared if override is None else override


@dataclass(frozen=True, slots=True, kw_only=True)
class UncertaintyReading:
    """What an arm measured this step. ``None`` means "not measured", never "zero"."""

    #: ``SelectionResult.uncertainty`` from a ``candidate_selector`` step (ADR 0048).
    selector_uncertainty: float | None = None
    #: Share of the candidate set disagreeing with the leader — see ``candidate_disagreement``.
    candidate_disagreement: float | None = None
    #: ``evidence_against`` references on the leading candidate (ADR 0042).
    contradictory_evidence: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class FiredSignals:
    """Which signals fired, in words, and which nothing this step could measure."""

    fired: frozenset[EscalationSignal]
    reasons: tuple[str, ...]
    unmeasured: frozenset[EscalationSignal]

    @property
    def escalate(self) -> bool:
        """Whether the ladder should buy more inference for this step."""
        return bool(self.fired)


def failed_attempts(run_state: RunState) -> int:
    """Tier-1 attempts this run has made that did not end the incident (ADR 0056).

    Counted from the attempt records ``agent/remediation.py`` appends under
    ``planner_context.ATTEMPT_FAILED_MARKER`` — imported, never re-spelled, because a second
    spelling of that name is how one reader of it stops matching.
    """
    return sum(entry.tool_name == ATTEMPT_FAILED_MARKER for entry in run_state.evidence)


def _leader(candidates: Sequence[DiagnosisCandidate]) -> DiagnosisCandidate:
    """The highest-confidence candidate. Raises on an empty set rather than inventing one."""
    if not candidates:
        raise ValueError(
            "an uncertainty reading needs at least one candidate; an empty set is a generator "
            "failure to report, not a disagreement of zero."
        )
    return max(candidates, key=lambda candidate: candidate.confidence)


def candidate_disagreement(candidates: Sequence[DiagnosisCandidate]) -> float:
    """Share of the set whose category is not the leading candidate's."""
    leader = _leader(candidates)
    dissenting = [c for c in candidates if c.category is not leader.category]
    return len(dissenting) / len(candidates)


def contradicting_evidence(candidates: Sequence[DiagnosisCandidate]) -> int:
    """``evidence_against`` references on the leading candidate."""
    return len(_leader(candidates).evidence_against)


def reading_of(
    candidates: Sequence[DiagnosisCandidate] | None = None,
    *,
    selector_uncertainty: float | None = None,
) -> UncertaintyReading:
    """The reading an arm can build from what it has: a candidate set, a selector's number."""
    if not candidates:
        return UncertaintyReading(selector_uncertainty=selector_uncertainty)
    return UncertaintyReading(
        selector_uncertainty=selector_uncertainty,
        candidate_disagreement=candidate_disagreement(candidates),
        contradictory_evidence=contradicting_evidence(candidates),
    )


def evaluate(
    run_state: RunState,
    *,
    thresholds: UncertaintyThresholds | None = None,
    reading: UncertaintyReading | None = None,
) -> FiredSignals:
    """Which escalation signals this step fires, and which nothing measured.

    Four of the seven are read off the run alone, so ``baseline``'s state answers them; the
    three that need a candidate set or a selector's number come in through ``reading``, and a
    signal with no measurement is reported UNMEASURED rather than quietly not fired.
    """
    bars = thresholds if thresholds is not None else UncertaintyThresholds()
    seen = reading if reading is not None else UncertaintyReading()
    fired: list[EscalationSignal] = []
    reasons: list[str] = []
    unmeasured: list[EscalationSignal] = []

    def fire(signal: EscalationSignal, reason: str) -> None:
        fired.append(signal)
        reasons.append(f"{signal.value}: {reason}")

    ranked = iter(run_state.hypotheses)
    top = next(ranked, None)
    second = next(ranked, None)
    probes = run_state.budget.tool_calls_used

    if top is None:
        unmeasured.extend(
            (
                EscalationSignal.TOP1_CONFIDENCE_LOW,
                EscalationSignal.TOP1_TOP2_MARGIN_NARROW,
                EscalationSignal.CONFIDENCE_LOW_AFTER_K_PROBES,
            )
        )
    else:
        if top.confidence < bars.top1_confidence_floor:
            fire(
                EscalationSignal.TOP1_CONFIDENCE_LOW,
                f"top-1 confidence {top.confidence:.2f} is below the "
                f"{bars.top1_confidence_floor} floor",
            )
        if second is None:
            unmeasured.append(EscalationSignal.TOP1_TOP2_MARGIN_NARROW)
        elif (margin := top.confidence - second.confidence) < bars.top1_top2_margin_floor:
            fire(
                EscalationSignal.TOP1_TOP2_MARGIN_NARROW,
                f"top-1 leads top-2 by {margin:.2f}, under the {bars.top1_top2_margin_floor} floor",
            )
        if (
            probes >= bars.probe_count_before_confidence_check
            and top.confidence < bars.confidence_floor_after_probes
        ):
            fire(
                EscalationSignal.CONFIDENCE_LOW_AFTER_K_PROBES,
                f"{probes} probes spent and top-1 confidence {top.confidence:.2f} is below the "
                f"{bars.confidence_floor_after_probes} floor",
            )

    if seen.selector_uncertainty is None:
        unmeasured.append(EscalationSignal.SELECTOR_UNCERTAINTY_HIGH)
    elif seen.selector_uncertainty > bars.selector_uncertainty_ceiling:
        fire(
            EscalationSignal.SELECTOR_UNCERTAINTY_HIGH,
            f"the selector reported uncertainty {seen.selector_uncertainty:.2f}, over the "
            f"{bars.selector_uncertainty_ceiling} ceiling",
        )

    if seen.candidate_disagreement is None:
        unmeasured.append(EscalationSignal.CANDIDATE_DISAGREEMENT_HIGH)
    elif seen.candidate_disagreement > bars.candidate_disagreement_ceiling:
        fire(
            EscalationSignal.CANDIDATE_DISAGREEMENT_HIGH,
            f"{seen.candidate_disagreement:.2f} of the candidate set disagrees with the "
            f"leader, over the {bars.candidate_disagreement_ceiling} ceiling",
        )

    if seen.contradictory_evidence is None:
        unmeasured.append(EscalationSignal.CONTRADICTORY_EVIDENCE)
    elif seen.contradictory_evidence >= bars.contradictory_evidence_count:
        fire(
            EscalationSignal.CONTRADICTORY_EVIDENCE,
            f"the leading candidate cites {seen.contradictory_evidence} piece(s) of evidence "
            f"against itself, at or over the {bars.contradictory_evidence_count} that fires",
        )

    if (failed := failed_attempts(run_state)) >= bars.failed_attempt_count:
        fire(
            EscalationSignal.REMEDIATION_ATTEMPT_FAILED,
            f"{failed} remediation attempt(s) on this run did not end the incident (ADR 0056), "
            f"at or over the {bars.failed_attempt_count} that fires",
        )

    return FiredSignals(
        fired=frozenset(fired), reasons=tuple(reasons), unmeasured=frozenset(unmeasured)
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class ThresholdRow:
    """One row of the threshold table a report prints: the live value and its provenance."""

    name: ThresholdName
    signal: EscalationSignal
    env_var: str
    value: float
    declared: float
    declared_split: ThresholdSplit
    source: str

    @property
    def is_declared_default(self) -> bool:
        """Whether the live value is the declared default rather than an operator's override."""
        return self.value == self.declared

    @property
    def live_value_split(self) -> ThresholdSplit | None:
        """The split behind the LIVE value: ``None`` once an operator has overridden it.

        A split describes where a number came from, and nobody can say that of a value typed
        into an environment. Reporting ``None`` is the honest answer, and it is not the same
        claim as ``untuned``.
        """
        return self.declared_split if self.is_declared_default else None

    def as_config(self) -> Mapping[str, object]:
        """This row as a provenance entry for the run's ``strategy_config`` stamp."""
        return MappingProxyType(
            {
                "value": self.value,
                "declared_default": self.declared,
                "is_declared_default": self.is_declared_default,
                "declared_split": self.declared_split.value,
                "source": self.source,
                "signal": self.signal.value,
                "env_var": self.env_var,
            }
        )


def provenance_rows(thresholds: UncertaintyThresholds | None = None) -> tuple[ThresholdRow, ...]:
    """Every threshold with its live value and the split its default was set on.

    In ``ThresholdName`` order, so two reports of the same policy render the same table. This is
    what makes "which split did this number come from" a printed answer rather than a promise.
    """
    live = thresholds if thresholds is not None else UncertaintyThresholds()
    return tuple(
        ThresholdRow(
            name=name,
            signal=UNCERTAINTY_DEFAULTS[name].signal,
            env_var=env_var_for(name),
            value=live.value_of(name),
            declared=float(declared_default(name)),
            declared_split=UNCERTAINTY_DEFAULTS[name].split,
            source=UNCERTAINTY_DEFAULTS[name].source,
        )
        for name in ThresholdName
    )


def signals_served() -> Mapping[EscalationSignal, tuple[ThresholdName, ...]]:
    """Signal to the thresholds that decide it. Derived, so a new signal cannot arrive bare."""
    served: dict[EscalationSignal, tuple[ThresholdName, ...]] = {}
    for declared in _DECLARED:
        served[declared.signal] = (*served.get(declared.signal, ()), declared.name)
    return MappingProxyType(served)
