"""WP-13.1 — the uncertainty policy (plan 02 § 15–16, plan 03 § 4, ADR 0061).

The packet's claim is that no escalation threshold can exist without a name, a default and the
split that default came from, so the tests are grouped that way:

* ``TestEverySignalIsANamedThreshold`` — plan 02 § 15's seven signals, each with a threshold, a
  ``Settings`` field and a default reachable without configuration.
* ``TestThePolicyModuleHoldsNoMagicNumber`` — every numeric literal in the module sits inside a
  ``ThresholdDefault`` row, proved by an AST scan that is itself shown to catch a planted one.
* ``TestTheHoldoutCannotSetAThreshold`` — a holdout-derived default is refused at construction,
  and the split vocabulary is pinned to the corpus's own.
* ``TestTheAttemptFailedSignalReadsTheRun`` — it fires on the ADR-0056 attempt record and not on
  a run that made no attempt.
* ``TestTheRemediateThresholdIsUntouched`` — 0.7 stays in the loop, gets no knob, is equal to no
  declared default, and is reported as an operating point.
* ``TestTheReportRecordsTheSplit`` — the projection and the documented table agree, and an
  operator override reports no split at all.
* ``TestTheSignalsFireOnTheirThresholds`` — each signal on and off its own number.
* ``TestNothingShippedReadsThePolicyYet`` — no arm imports it, so no baseline number can move.
"""

from __future__ import annotations

import ast
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Final

import pytest

from evals.scenarios.schema import BenchmarkSplit
from incident_commander.agent.candidates import DiagnosisCandidate, EvidenceRef, grounded_in
from incident_commander.agent.hypothesis import Hypothesis, HypothesisCategory
from incident_commander.agent.investigation import _REMEDIATE_CONFIDENCE_THRESHOLD
from incident_commander.agent.planner_context import ATTEMPT_FAILED_MARKER
from incident_commander.agent.state import EvidenceEntry, RunState
from incident_commander.agent.strategies.policy import (
    UNCERTAINTY_DEFAULTS,
    EscalationSignal,
    HoldoutTunedThresholdError,
    ThresholdDefault,
    ThresholdName,
    ThresholdSplit,
    UncertaintyReading,
    UncertaintyThresholds,
    candidate_disagreement,
    contradicting_evidence,
    declared_default,
    env_var_for,
    evaluate,
    failed_attempts,
    provenance_rows,
    reading_of,
    settings_field_for,
    signals_served,
)
from incident_commander.config import Settings

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
_SRC: Final[Path] = _REPO_ROOT / "src" / "incident_commander"
_POLICY: Final[Path] = _SRC / "agent" / "strategies" / "policy.py"
_METHODOLOGY: Final[Path] = _REPO_ROOT / "docs" / "eval-methodology.md"

_DOC_HEADING: Final[str] = "## Uncertainty thresholds and the split each default came from"
# One row of that section's table: `name` | `ENV_VAR` | default | `split` | `signal`.
_DOC_ROW: Final[re.Pattern[str]] = re.compile(
    r"^\|\s*`([a-z0-9_]+)`\s*\|\s*`([A-Z0-9_]+)`\s*\|\s*([0-9.]+)\s*\|\s*`([a-z]+)`\s*\|"
    r"\s*`([a-z0-9_]+)`\s*\|"
)

# Plan 02 § 15's list, transcribed once. A rename here is a plan divergence to report, not a
# test to update, which is why the names are written out rather than derived from the enum.
_PLAN_SIGNALS: Final[frozenset[str]] = frozenset(
    {
        "top1_confidence_low",
        "top1_top2_margin_narrow",
        "selector_uncertainty_high",
        "candidate_disagreement_high",
        "contradictory_evidence",
        "remediation_attempt_failed",
        "confidence_low_after_k_probes",
    }
)


def _hypothesis(
    confidence: float,
    category: HypothesisCategory = HypothesisCategory.CONSUMER_SATURATION,
) -> Hypothesis:
    return Hypothesis(
        category=category,
        name="worker-dispatcher backlog",
        confidence=confidence,
        reasoning="lag climbing while the group holds one member",
    )


def _probe_entry(at: datetime) -> EvidenceEntry:
    return EvidenceEntry(
        tool_name="get_consumer_lag",
        arguments={"group": "worker-dispatcher"},
        result_summary='{"lag": 29}',
        timestamp=at,
    )


def _attempt_entry(at: datetime) -> EvidenceEntry:
    """The record ``agent/remediation.py`` appends when an attempt did not resolve (ADR 0056)."""
    return EvidenceEntry(
        tool_name=ATTEMPT_FAILED_MARKER,
        arguments={"tool": "restart_consumer_group"},
        result_summary="restart_consumer_group ran and the backlog did not move",
        timestamp=at,
    )


def _run(run_state: RunState, **update: Any) -> RunState:
    return run_state.model_copy(update=update)


def _with_probes(run_state: RunState, probes: int) -> RunState:
    return _run(
        run_state,
        budget=run_state.budget.model_copy(update={"tool_calls_used": probes}),
    )


def _candidate(
    candidate_id: str,
    category: HypothesisCategory,
    confidence: float,
    *,
    against: tuple[EvidenceRef, ...] = (),
) -> DiagnosisCandidate:
    return DiagnosisCandidate(
        candidate_id=candidate_id,
        category=category,
        name=f"{candidate_id} reading",
        confidence=confidence,
        evidence_against=against,
        next_probe=None,
    )


def _policy_tree() -> ast.Module:
    return ast.parse(_POLICY.read_text(encoding="utf-8"))


def _callee(func: ast.expr) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _declared_value_nodes(tree: ast.AST) -> set[int]:
    """``id()`` of every ``Constant`` that is a ``ThresholdDefault(value=…)`` argument."""
    allowed: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _callee(node.func) == ThresholdDefault.__name__:
            for keyword in node.keywords:
                if keyword.arg == "value" and isinstance(keyword.value, ast.Constant):
                    allowed.add(id(keyword.value))
    return allowed


def _numbers_outside_declarations(tree: ast.AST) -> list[str]:
    """Numeric literals in ``tree`` that are not a declared default's value.

    Booleans are excluded: ``frozen=True`` is a dataclass option, not a threshold.
    """
    allowed = _declared_value_nodes(tree)
    return [
        f"line {node.lineno}: {node.value!r}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, int | float)
        and not isinstance(node.value, bool)
        and id(node) not in allowed
    ]


def _documented_rows() -> dict[str, tuple[str, float, str, str]]:
    """The methodology's threshold table: name -> (env var, default, split, signal)."""
    text = _METHODOLOGY.read_text(encoding="utf-8")
    parts = text.split(_DOC_HEADING, 1)
    assert len(parts) == 2, f"{_METHODOLOGY.name} has no '{_DOC_HEADING}' heading"
    section = parts[1].split("\n## ", 1)[0]
    rows: dict[str, tuple[str, float, str, str]] = {}
    for line in section.splitlines():
        match = _DOC_ROW.match(line)
        if match is not None:
            rows[match.group(1)] = (
                match.group(2),
                float(match.group(3)),
                match.group(4),
                match.group(5),
            )
    return rows


class TestEverySignalIsANamedThreshold:
    """Plan 02 § 15's signals, as configuration rather than as code."""

    def test_the_signal_set_is_the_plans(self) -> None:
        assert {signal.value for signal in EscalationSignal} == _PLAN_SIGNALS

    def test_every_signal_is_decided_by_at_least_one_threshold(self) -> None:
        served = signals_served()
        bare = sorted(signal.value for signal in EscalationSignal if not served.get(signal))
        assert bare == [], (
            f"these signals have no threshold behind them: {bare}. A signal without a named "
            "number is a decision in the code, which is what this packet removes."
        )

    def test_every_threshold_has_an_environment_variable_settings_reads(self) -> None:
        missing = sorted(
            env_var_for(name)
            for name in ThresholdName
            if settings_field_for(name) not in Settings.model_fields
        )
        assert missing == [], (
            f"these thresholds have no Settings field: {missing}. Add the override to "
            "src/incident_commander/config.py, .env.example and test_config.py's "
            "_DOCUMENTED_ENV_VARS."
        )

    def test_an_unset_override_means_the_declared_default(self) -> None:
        # The default of every field is None, so "unset" resolves to the table — the only
        # value that has a split behind it.
        settings_defaults = {
            settings_field_for(name): Settings.model_fields[settings_field_for(name)].default
            for name in ThresholdName
        }
        assert set(settings_defaults.values()) == {None}, (
            f"these overrides carry a default of their own: {settings_defaults}. A number in "
            "config.py is a second declaration with no split attached to it."
        )

    def test_the_dataclass_defaults_are_the_declared_ones(self) -> None:
        policy = UncertaintyThresholds()
        assert {name.value: policy.value_of(name) for name in ThresholdName} == {
            name.value: float(declared_default(name)) for name in ThresholdName
        }

    def test_an_override_reaches_the_policy(self) -> None:
        policy = UncertaintyThresholds.resolve(top1_confidence_floor=0.9, failed_attempt_count=2)
        assert policy.top1_confidence_floor == 0.9
        assert policy.failed_attempt_count == 2
        # Everything not overridden stays declared.
        assert policy.top1_top2_margin_floor == declared_default(
            ThresholdName.TOP1_TOP2_MARGIN_FLOOR
        )


class TestThePolicyModuleHoldsNoMagicNumber:
    """A number in the policy may only exist attached to its split and its reason."""

    def test_no_number_lives_outside_a_declared_default(self) -> None:
        stray = _numbers_outside_declarations(_policy_tree())
        assert stray == [], (
            f"{_POLICY.name} holds numeric literals outside a ThresholdDefault row: {stray}. "
            "Every threshold is declared once, with the split it was set on; a bare number is "
            "a threshold nobody can report the provenance of."
        )

    def test_the_scan_sees_one_declared_value_per_threshold(self) -> None:
        # Anti-vacuity: a scan that found no declarations would pass the test above trivially.
        assert len(_declared_value_nodes(_policy_tree())) == len(ThresholdName)

    def test_the_scan_catches_a_planted_number(self) -> None:
        # The same scan over the module plus one bare comparison, so the guard is shown to
        # fail rather than assumed to.
        planted = ast.parse(
            _POLICY.read_text(encoding="utf-8") + "\n\ndef _planted(x: float) -> bool:\n"
            "    return x < 0.61\n"
        )
        assert [row for row in _numbers_outside_declarations(planted) if "0.61" in row] != []


class TestTheHoldoutCannotSetAThreshold:
    """Plan 03 § 4: the holdout is never tuned against, and the enforcement is mechanical."""

    def test_a_holdout_derived_default_is_refused(self) -> None:
        with pytest.raises(HoldoutTunedThresholdError) as raised:
            ThresholdDefault(
                name=ThresholdName.TOP1_CONFIDENCE_FLOOR,
                signal=EscalationSignal.TOP1_CONFIDENCE_LOW,
                value=0.8,
                split=ThresholdSplit.HOLDOUT,
                source="a sweep over the held-out templates",
                rationale="tuned where nothing may be tuned",
            )
        message = str(raised.value)
        assert env_var_for(ThresholdName.TOP1_CONFIDENCE_FLOOR) in message
        assert "holdout" in message

    @pytest.mark.parametrize(
        "split", [ThresholdSplit.DEV, ThresholdSplit.VALIDATION, ThresholdSplit.UNTUNED]
    )
    def test_the_other_splits_are_allowed(self, split: ThresholdSplit) -> None:
        declared = ThresholdDefault(
            name=ThresholdName.TOP1_CONFIDENCE_FLOOR,
            signal=EscalationSignal.TOP1_CONFIDENCE_LOW,
            value=0.8,
            split=split,
            source="a dev sweep",
            rationale="allowed",
        )
        assert declared.split is split

    def test_no_shipped_default_is_tuned_on_the_holdout(self) -> None:
        offending = sorted(
            name.value
            for name, declared in UNCERTAINTY_DEFAULTS.items()
            if declared.split is ThresholdSplit.HOLDOUT
        )
        assert offending == []

    def test_every_shipped_default_names_a_split_and_a_source(self) -> None:
        bare = sorted(
            name.value
            for name, declared in UNCERTAINTY_DEFAULTS.items()
            if not declared.source.strip() or not declared.rationale.strip()
        )
        assert bare == [], f"these defaults do not say where the number came from: {bare}"

    def test_the_split_vocabulary_is_the_corpus_one(self) -> None:
        # `untuned` is this module's own fourth value; the other three must be the corpus's,
        # or a threshold could claim a split no scenario can be in.
        assert {split.value for split in ThresholdSplit} - {ThresholdSplit.UNTUNED.value} == {
            split.value for split in BenchmarkSplit
        }


class TestTheAttemptFailedSignalReadsTheRun:
    """The one signal WP-10.1 made available: read off the run, not re-derived."""

    def test_a_run_that_made_no_attempt_does_not_fire_it(
        self, run_state: RunState, now: datetime
    ) -> None:
        probed = _run(
            run_state,
            evidence=(_probe_entry(now),),
            hypotheses=(_hypothesis(0.9),),
        )
        assert failed_attempts(probed) == 0
        fired = evaluate(probed).fired
        assert EscalationSignal.REMEDIATION_ATTEMPT_FAILED not in fired

    def test_the_attempt_record_fires_it(self, run_state: RunState, now: datetime) -> None:
        attempted = _run(
            run_state,
            evidence=(_probe_entry(now), _attempt_entry(now)),
            hypotheses=(_hypothesis(0.9),),
        )
        assert failed_attempts(attempted) == 1
        result = evaluate(attempted)
        assert EscalationSignal.REMEDIATION_ATTEMPT_FAILED in result.fired
        assert result.escalate
        assert any("ADR 0056" in reason for reason in result.reasons)

    def test_every_attempt_record_is_counted(self, run_state: RunState, now: datetime) -> None:
        twice = _run(run_state, evidence=(_attempt_entry(now), _attempt_entry(now)))
        assert failed_attempts(twice) == 2

    def test_the_marker_is_imported_and_never_re_spelled(self) -> None:
        source = _POLICY.read_text(encoding="utf-8")
        assert ATTEMPT_FAILED_MARKER not in source, (
            "the policy module spells the attempt-failed marker out. Import it from "
            "agent/planner_context.py — a second spelling is how one reader stops matching."
        )
        assert "ATTEMPT_FAILED_MARKER" in source


class TestTheRemediateThresholdIsUntouched:
    """Plan 02 § 16: the 0.7 bar is a reported operating point, not one of these knobs."""

    def test_the_bar_is_still_where_it_was(self) -> None:
        assert _REMEDIATE_CONFIDENCE_THRESHOLD == 0.7

    def test_no_settings_field_would_move_it(self) -> None:
        knobs = sorted(field for field in Settings.model_fields if "remediate" in field)
        assert knobs == [], (
            f"{knobs} would let the remediate bar move from an environment. It is re-examined "
            "per model in the phase-close protocol (plan 02 § 16); a knob makes that a habit "
            "nobody can audit, and moves every arm's numbers at once."
        )

    def test_no_declared_default_is_equal_to_it(self) -> None:
        collisions = sorted(
            name.value
            for name in ThresholdName
            if declared_default(name) == _REMEDIATE_CONFIDENCE_THRESHOLD
        )
        assert collisions == [], (
            f"{collisions} default to the remediate bar's own value. A threshold equal to it "
            "reads as the gate having moved into this table, which is the confusion plan "
            "02 § 16 exists to prevent."
        )

    def test_the_methodology_reports_it_as_the_operating_point(self) -> None:
        text = _METHODOLOGY.read_text(encoding="utf-8")
        assert _DOC_HEADING in text
        section = text.split(_DOC_HEADING, 1)[1].split("\n## ", 1)[0]
        assert str(_REMEDIATE_CONFIDENCE_THRESHOLD) in section, (
            "the methodology section must state the remediate bar's value, because a reported "
            "operating point that nobody writes down is not reported."
        )
        assert "operating point" in section


class TestTheReportRecordsTheSplit:
    """Which split each default came from is a printed answer, not a promise."""

    def test_one_row_per_threshold_each_carrying_its_split(self) -> None:
        rows = provenance_rows()
        assert [row.name for row in rows] == list(ThresholdName)
        assert all(row.is_declared_default for row in rows)
        assert {row.live_value_split for row in rows} == {ThresholdSplit.UNTUNED}

    def test_an_overridden_threshold_reports_no_split_for_its_value(self) -> None:
        rows = {
            row.name: row
            for row in provenance_rows(UncertaintyThresholds.resolve(top1_confidence_floor=0.6))
        }
        overridden = rows[ThresholdName.TOP1_CONFIDENCE_FLOOR]
        assert overridden.value == 0.6
        assert not overridden.is_declared_default
        # The declared split still describes the DEFAULT; it describes no operator's value.
        assert overridden.declared_split is ThresholdSplit.UNTUNED
        assert overridden.live_value_split is None

    def test_the_strategy_config_block_carries_value_and_provenance(self) -> None:
        block = UncertaintyThresholds().as_strategy_config()
        assert set(block) == {name.value for name in ThresholdName}
        entry = block[ThresholdName.TOP1_CONFIDENCE_FLOOR.value]
        assert entry["value"] == declared_default(ThresholdName.TOP1_CONFIDENCE_FLOOR)
        assert entry["declared_split"] == ThresholdSplit.UNTUNED.value
        assert entry["is_declared_default"] is True
        assert entry["signal"] == EscalationSignal.TOP1_CONFIDENCE_LOW.value

    def test_the_env_example_shows_the_declared_defaults(self) -> None:
        # `.env.example` ships each threshold commented out at its declared default, so an
        # operator reads the real number. That is a second copy — this is its tripwire.
        text = (_REPO_ROOT / ".env.example").read_text(encoding="utf-8")
        shown = {
            match.group(1): float(match.group(2))
            for match in re.finditer(r"^#\s*(UNCERTAINTY_[A-Z0-9_]+)=([0-9.]+)$", text, re.M)
        }
        assert shown == {
            env_var_for(name): float(declared_default(name)) for name in ThresholdName
        }, (
            ".env.example's commented threshold values have drifted from the declared table "
            f"in {_POLICY.name}. Shown: {shown}."
        )

    def test_the_documented_table_matches_the_declared_defaults(self) -> None:
        documented = _documented_rows()
        expected = {
            name.value: (
                env_var_for(name),
                float(declared_default(name)),
                UNCERTAINTY_DEFAULTS[name].split.value,
                UNCERTAINTY_DEFAULTS[name].signal.value,
            )
            for name in ThresholdName
        }
        assert documented == expected, (
            f"{_METHODOLOGY.name}'s threshold table has drifted from the code. Documented: "
            f"{documented}; declared: {expected}."
        )


class TestTheSignalsFireOnTheirThresholds:
    """Each signal on and off its own number, and unmeasured when nothing measured it."""

    def test_a_confident_single_hypothesis_fires_nothing_measurable(
        self, run_state: RunState
    ) -> None:
        confident = _run(run_state, hypotheses=(_hypothesis(0.95),))
        result = evaluate(confident)
        assert result.fired == frozenset()
        assert not result.escalate
        assert result.unmeasured == frozenset(
            {
                EscalationSignal.TOP1_TOP2_MARGIN_NARROW,
                EscalationSignal.SELECTOR_UNCERTAINTY_HIGH,
                EscalationSignal.CANDIDATE_DISAGREEMENT_HIGH,
                EscalationSignal.CONTRADICTORY_EVIDENCE,
            }
        )

    def test_a_run_with_no_hypothesis_measures_no_confidence(self, run_state: RunState) -> None:
        result = evaluate(run_state)
        assert EscalationSignal.TOP1_CONFIDENCE_LOW in result.unmeasured
        assert EscalationSignal.CONFIDENCE_LOW_AFTER_K_PROBES in result.unmeasured

    def test_low_top1_confidence_fires_below_the_floor(self, run_state: RunState) -> None:
        floor = declared_default(ThresholdName.TOP1_CONFIDENCE_FLOOR)
        below = _run(run_state, hypotheses=(_hypothesis(floor - 0.01),))
        at = _run(run_state, hypotheses=(_hypothesis(floor),))
        assert EscalationSignal.TOP1_CONFIDENCE_LOW in evaluate(below).fired
        assert EscalationSignal.TOP1_CONFIDENCE_LOW not in evaluate(at).fired

    def test_a_narrow_margin_fires_and_a_wide_one_does_not(self, run_state: RunState) -> None:
        narrow = _run(
            run_state,
            hypotheses=(
                _hypothesis(0.9),
                _hypothesis(0.85, HypothesisCategory.POISON_MESSAGE),
            ),
        )
        wide = _run(
            run_state,
            hypotheses=(
                _hypothesis(0.9),
                _hypothesis(0.2, HypothesisCategory.POISON_MESSAGE),
            ),
        )
        assert EscalationSignal.TOP1_TOP2_MARGIN_NARROW in evaluate(narrow).fired
        assert EscalationSignal.TOP1_TOP2_MARGIN_NARROW not in evaluate(wide).fired

    def test_selector_uncertainty_fires_above_its_ceiling(self, run_state: RunState) -> None:
        ceiling = declared_default(ThresholdName.SELECTOR_UNCERTAINTY_CEILING)
        unsure = evaluate(run_state, reading=UncertaintyReading(selector_uncertainty=ceiling + 0.1))
        sure = evaluate(run_state, reading=UncertaintyReading(selector_uncertainty=ceiling))
        assert EscalationSignal.SELECTOR_UNCERTAINTY_HIGH in unsure.fired
        assert EscalationSignal.SELECTOR_UNCERTAINTY_HIGH not in sure.fired
        assert EscalationSignal.SELECTOR_UNCERTAINTY_HIGH not in sure.unmeasured

    def test_candidate_disagreement_is_measured_from_the_set(
        self, run_state: RunState, now: datetime
    ) -> None:
        entry = _probe_entry(now)
        with grounded_in((entry,)):
            ref = EvidenceRef(evidence_id=entry.evidence_id)
            split_set = (
                _candidate("a", HypothesisCategory.CONSUMER_SATURATION, 0.6, against=(ref,)),
                _candidate("b", HypothesisCategory.POISON_MESSAGE, 0.5),
                _candidate("c", HypothesisCategory.STALE_CACHE, 0.4),
            )
        assert candidate_disagreement(split_set) == pytest.approx(2 / 3)
        assert contradicting_evidence(split_set) == 1
        reading = reading_of(split_set, selector_uncertainty=None)
        result = evaluate(run_state, reading=reading)
        assert EscalationSignal.CANDIDATE_DISAGREEMENT_HIGH in result.fired
        assert EscalationSignal.CONTRADICTORY_EVIDENCE in result.fired

    def test_an_agreeing_set_fires_neither(self, run_state: RunState) -> None:
        agreed = (
            _candidate("a", HypothesisCategory.CONSUMER_SATURATION, 0.6),
            _candidate("b", HypothesisCategory.CONSUMER_SATURATION, 0.5),
        )
        result = evaluate(run_state, reading=reading_of(agreed))
        assert EscalationSignal.CANDIDATE_DISAGREEMENT_HIGH not in result.fired
        assert EscalationSignal.CONTRADICTORY_EVIDENCE not in result.fired

    def test_an_empty_candidate_set_is_refused_rather_than_read_as_agreement(self) -> None:
        with pytest.raises(ValueError, match="at least one candidate"):
            candidate_disagreement(())

    def test_confidence_after_k_probes_needs_both_numbers(self, run_state: RunState) -> None:
        after = declared_default(ThresholdName.CONFIDENCE_FLOOR_AFTER_PROBES)
        k = int(declared_default(ThresholdName.PROBE_COUNT_BEFORE_CONFIDENCE_CHECK))
        early = _with_probes(_run(run_state, hypotheses=(_hypothesis(after - 0.05),)), k - 1)
        late = _with_probes(_run(run_state, hypotheses=(_hypothesis(after - 0.05),)), k)
        converged = _with_probes(_run(run_state, hypotheses=(_hypothesis(after),)), k)
        assert EscalationSignal.CONFIDENCE_LOW_AFTER_K_PROBES not in evaluate(early).fired
        assert EscalationSignal.CONFIDENCE_LOW_AFTER_K_PROBES in evaluate(late).fired
        assert EscalationSignal.CONFIDENCE_LOW_AFTER_K_PROBES not in evaluate(converged).fired

    def test_a_raised_threshold_changes_what_fires(self, run_state: RunState) -> None:
        # The point of the packet: the operating point is configuration, so the same run
        # escalates or does not depending on a declared number.
        state = _run(run_state, hypotheses=(_hypothesis(0.8),))
        assert EscalationSignal.TOP1_CONFIDENCE_LOW not in evaluate(state).fired
        raised = UncertaintyThresholds.resolve(top1_confidence_floor=0.9)
        assert EscalationSignal.TOP1_CONFIDENCE_LOW in evaluate(state, thresholds=raised).fired


class TestTheDecisionIsRecorded:
    """ADR 0061 exists, is accepted, and is in the index — the repo's own convention."""

    def test_the_adr_exists_and_is_indexed(self) -> None:
        adr = _REPO_ROOT / "docs" / "ADR"
        matches = sorted(adr.glob("0061-*.md"))
        assert len(matches) == 1
        assert "accepted" in matches[0].read_text(encoding="utf-8").lower()
        index = (adr / "README.md").read_text(encoding="utf-8")
        assert matches[0].name.removesuffix(".md").split("-", 1)[1] in index


class TestNothingShippedReadsThePolicyYet:
    """WP-13.1 ships the thresholds; WP-13.2's ``adaptive`` arm is what reads them."""

    def test_no_other_module_under_src_imports_the_policy(self) -> None:
        importers = sorted(
            path.relative_to(_REPO_ROOT).as_posix()
            for path in _SRC.rglob("*.py")
            if path != _POLICY and "strategies.policy" in path.read_text(encoding="utf-8")
        )
        assert importers == [], (
            f"{importers} read the uncertainty policy. Until WP-13.2 lands the adaptive arm, "
            "nothing does — which is what makes the canned baseline provably unmoved."
        )
