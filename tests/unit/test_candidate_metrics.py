"""WP-5.2's reporting half: pass@k, appeared-at-any-step, and two duplicate rates.

The two properties that are decisions rather than arithmetic:

* pass@k is scored at the **deciding** step and "appeared at any step" is a
  different number — ``TestTheTwoQuestionsDiffer`` builds the fixture the order
  asks for, where the correct candidate appears early and is dropped, and shows
  the two disagreeing on it.
* "duplicate rate" is two numbers and neither is the other — a set that repeats
  itself never reaches a record (the schema refuses it), so the within-step rate
  is computed from billed refusals while the cross-step rate is computed from
  what was accepted.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from evals import research_report as research
from evals.candidate_metrics import (
    REPORTED_KS,
    deciding_step,
    measure,
    steps_of,
)
from evals.tracing import TraceKind
from incident_commander.agent.hypothesis import HypothesisCategory

_LAG = HypothesisCategory.CONSUMER_SATURATION
_CACHE = HypothesisCategory.STALE_CACHE
_DB = HypothesisCategory.DB_QUERY_LATENCY


def _record(
    iteration: int,
    labels: list[tuple[HypothesisCategory, str]],
    *,
    action: str = "probe",
    rejections: list[str] | None = None,
    strategy: str = "best_of_n_enumerated",
) -> dict[str, Any]:
    """One ``step`` trace record, in the JSON shape a trace file holds."""
    return {
        "kind": TraceKind.STEP.value,
        "iteration": iteration,
        "strategy": strategy,
        "candidate_set": [
            {"candidate_id": f"c{i}", "category": category.value, "name": name, "confidence": 0.5}
            for i, (category, name) in enumerate(labels)
        ],
        "emitted_step": {"next_action": {"kind": action}},
        "generation_rejections": rejections or [],
    }


class TestPassAtK:
    def test_a_correct_cause_inside_the_top_k_is_a_hit(self) -> None:
        records = [_record(0, [(_DB, "db"), (_LAG, "lag")], action="stop")]
        metrics = measure(records, [_LAG])
        assert metrics.pass_at(2).hit is True
        # ...and outside it is not. Rank 1 is not inside the top 1.
        assert metrics.pass_at(1).hit is False

    def test_k_is_capped_at_the_set_size_and_the_cap_is_reported(self) -> None:
        """pass@8 over a 2-candidate set is pass@2 wearing a bigger number."""
        metrics = measure([_record(0, [(_LAG, "lag"), (_DB, "db")], action="stop")], [_LAG])
        assert metrics.pass_at(8).effective_k == 2
        assert metrics.pass_at(2).effective_k == 2
        assert "capped at 2" in metrics.pass_at(8).describe()

    def test_every_reported_k_is_computed(self) -> None:
        metrics = measure([_record(0, [(_LAG, "lag")], action="stop")], [_LAG])
        assert [entry.k for entry in metrics.pass_at_k] == list(REPORTED_KS)

    def test_a_run_with_no_steps_scores_nothing_rather_than_a_miss(self) -> None:
        metrics = measure([], [_LAG])
        assert metrics.pass_at(1).hit is None
        assert metrics.appeared_at_any_step is None
        assert metrics.cross_step_duplicate_rate is None
        assert "not scored" in metrics.pass_at(1).describe()

    def test_a_multi_fault_label_needs_only_one_correct_candidate(self) -> None:
        """pass@k asks "was a correct cause available", not "was the set right".

        That is the ROOT_CAUSE dimension's exact-set question, and plan 03 names
        the two separately.
        """
        metrics = measure([_record(0, [(_CACHE, "cache")], action="stop")], [_LAG, _CACHE])
        assert metrics.pass_at(1).hit is True


class TestTheDecidingStep:
    def test_the_step_that_emitted_remediate_or_stop_is_the_one_scored(self) -> None:
        steps = steps_of(
            [
                _record(0, [(_LAG, "lag")]),
                _record(1, [(_DB, "db")], action="stop"),
                _record(2, [(_LAG, "lag")]),  # a later probe, e.g. a concatenated re-run
            ]
        )
        step = deciding_step(steps)
        assert step is not None and step.iteration == 1

    def test_a_run_that_never_decided_is_scored_on_its_last_step(self) -> None:
        """Budget exhaustion, ``max_iterations`` and a probe failure all land here."""
        steps = steps_of([_record(0, [(_LAG, "lag")]), _record(1, [(_DB, "db")])])
        step = deciding_step(steps)
        assert step is not None and step.iteration == 1

    def test_no_steps_means_no_deciding_step(self) -> None:
        assert deciding_step(()) is None


class TestTheTwoQuestionsDiffer:
    """The order's fixture: the correct candidate appears early and is dropped."""

    def test_appeared_at_any_step_is_true_where_pass_at_k_is_a_miss(self) -> None:
        records = [
            _record(0, [(_LAG, "lag"), (_DB, "db")]),
            _record(1, [(_DB, "db"), (_CACHE, "cache")]),
            _record(2, [(_DB, "db"), (_CACHE, "cache")], action="stop"),
        ]
        metrics = measure(records, [_LAG])
        assert metrics.appeared_at_any_step is True
        assert metrics.first_appeared_at_iteration == 0
        # The deciding step dropped it, so every pass@k at that step is a miss.
        assert all(entry.hit is False for entry in metrics.pass_at_k)

    def test_appeared_at_any_step_reads_the_whole_set_not_the_top_k(self) -> None:
        records = [_record(0, [(_DB, "db"), (_CACHE, "cache"), (_LAG, "lag")], action="stop")]
        metrics = measure(records, [_LAG])
        assert metrics.appeared_at_any_step is True
        assert metrics.pass_at(1).hit is False

    def test_never_generated_is_false_on_both(self) -> None:
        metrics = measure([_record(0, [(_DB, "db")], action="stop")], [_LAG])
        assert metrics.appeared_at_any_step is False
        assert metrics.first_appeared_at_iteration is None


class TestTheTwoDuplicateRates:
    def test_a_duplicate_heavy_fake_shows_a_non_zero_cross_step_rate(self) -> None:
        """The same four candidates at every step: legal, and worth knowing."""
        same = [(_LAG, "lag"), (_DB, "db"), (_CACHE, "cache"), (_LAG, "lag-slow")]
        records = [_record(i, same, action="stop" if i == 2 else "probe") for i in range(3)]
        metrics = measure(records, [_LAG])
        assert metrics.candidates_generated == 12
        # 4 distinct labels out of 12 generated.
        assert metrics.cross_step_duplicate_rate == pytest.approx(1 - 4 / 12)

    def test_a_run_that_never_repeats_scores_zero(self) -> None:
        records = [
            _record(0, [(_LAG, "lag"), (_DB, "db")]),
            _record(1, [(_CACHE, "cache"), (_LAG, "lag-slow")], action="stop"),
        ]
        assert measure(records, [_LAG]).cross_step_duplicate_rate == 0.0

    def test_within_step_refusals_are_the_other_number(self) -> None:
        """A refused set is billed and is not in the accepted data at all.

        Two refusals across two steps means four billed sets, two of which the
        schema threw away.
        """
        records = [
            _record(0, [(_LAG, "lag")], rejections=["duplicate_candidate"]),
            _record(1, [(_DB, "db")], action="stop", rejections=["short_set"]),
        ]
        metrics = measure(records, [_LAG])
        assert metrics.within_step_rejection_rate == pytest.approx(2 / 4)
        assert metrics.rejections_by_class == {"duplicate_candidate": 1, "short_set": 1}

    def test_neither_rate_stands_in_for_the_other(self) -> None:
        # A run that repeats itself across steps and was never refused, and a
        # run that was refused and never repeated: each scores on one rate only.
        repeats = [_record(i, [(_LAG, "lag")], action="stop" if i else "probe") for i in range(2)]
        refused = [_record(0, [(_LAG, "lag")], action="stop", rejections=["duplicate_candidate"])]
        assert measure(repeats, [_LAG]).within_step_rejection_rate == 0.0
        assert measure(repeats, [_LAG]).cross_step_duplicate_rate == 0.5
        assert measure(refused, [_LAG]).cross_step_duplicate_rate == 0.0
        assert measure(refused, [_LAG]).within_step_rejection_rate == 0.5

    def test_no_steps_means_no_rate_rather_than_zero(self) -> None:
        metrics = measure([], [_LAG])
        assert metrics.within_step_rejection_rate is None
        assert metrics.rejections_by_class == {}


class TestItReadsBothAgesOfTheSameData:
    def test_a_step_record_object_and_its_json_reduce_alike(self) -> None:

        from incident_commander.agent.hypothesis import (
            Hypothesis,
            InvestigationStep,
            StopAction,
        )
        from incident_commander.agent.strategies.records import CandidateRecord, StepRecord

        record = StepRecord(
            run_id="r",
            iteration=3,
            strategy="best_of_n_enumerated",
            model="m",
            candidate_set=(
                CandidateRecord(category=_LAG, name="lag", confidence=0.9),
                CandidateRecord(category=_DB, name="db", confidence=0.1),
            ),
            emitted_step=InvestigationStep(
                hypotheses=(Hypothesis(category=_LAG, name="lag", confidence=0.9, reasoning="x"),),
                next_action=StopAction(reason="done"),
            ),
            generation_rejections=("duplicate_candidate",),
        )
        from_object = steps_of([record])[0]
        from_json = steps_of([json.loads(json.dumps(record.as_trace_record(), default=str))])[0]
        assert from_object == from_json
        assert from_object.is_deciding
        assert from_object.rejections == ("duplicate_candidate",)

    def test_an_unknown_category_raises_rather_than_scoring_a_miss(self) -> None:
        """A record from a different version of the agent is a mismatch, not a miss."""
        bad = _record(0, [(_LAG, "lag")], action="stop")
        bad["candidate_set"][0]["category"] = "a_category_that_never_existed"
        with pytest.raises(ValueError):
            measure([bad], [_LAG])


class TestTheReportSectionScopesItselfToEnumeratingArms:
    """pass@1 over a one-candidate set is the ROOT_CAUSE dimension renamed."""

    def test_the_committed_scope_stays_not_measurable(self) -> None:
        document = research.assemble(research.REPO_ROOT)
        section = document["sections"]["pass_at_k_vs_selected_at_k"]
        assert section["measurable"] is False
        assert section["requires"]

    def test_baseline_step_records_are_present_and_are_not_reported(self) -> None:
        """The reason the section is unmeasurable is the set SIZE, not their absence.

        Three live archives in scope carry ``step`` records. If this ever finds
        none, the scoping constant below is being tested against nothing.
        """
        found = {
            archive: records
            for archive in research.SCOPE
            if (records := research.step_records_in(research.REPO_ROOT, archive))
        }
        assert found, "no step records in scope — this test would prove nothing"
        sizes = {
            len(record["candidate_set"])
            for records in found.values()
            for scenario_records in records.values()
            for record in scenario_records
        }
        assert sizes == {1}
        assert research.ENUMERATING_SET_SIZE == 2
        assert (
            research._candidate_rows(research.REPO_ROOT, research.read_scope(research.REPO_ROOT))
            == []
        )

    def test_an_enumerating_archive_makes_the_section_measurable(self, tmp_path: Path) -> None:
        """The other direction: the code path is not dead, it is unfed.

        A synthetic archive carrying four-candidate sets for a scenario that
        declares a ground truth, read through the real assembler helpers.
        """
        scenario = next(
            name for name, causes in research._ground_truths(research.REPO_ROOT).items() if causes
        )
        expected = research._ground_truths(research.REPO_ROOT)[scenario][0]
        traces = tmp_path / "evals" / "runs" / "aaaaaaaaaaaa" / "traces"
        traces.mkdir(parents=True)
        (traces / f"{scenario}.jsonl").write_text(
            "\n".join(
                json.dumps(
                    _record(
                        i,
                        [(expected, "right"), (_DB, "wrong-a"), (_CACHE, "wrong-b"), (_LAG, "x")],
                        action="stop" if i == 1 else "probe",
                    )
                )
                for i in range(2)
            )
            + "\n"
        )
        records = research.step_records_in(tmp_path, "aaaaaaaaaaaa")
        assert set(records) == {scenario}
        metrics = measure(records[scenario], [expected])
        assert metrics.pass_at(4).hit is True
        assert metrics.candidates_generated == 8
