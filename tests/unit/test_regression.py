import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from evals import artifacts, regression
from evals.graders.deterministic import (
    DimensionResult,
    GradeDimension,
    GradeReport,
    is_vacuous_detail,
)
from evals.graders.root_cause import not_graded_detail
from evals.regression import compare
from evals.runner import RunReport, ScenarioOutcome
from incident_commander.agent.state import IncidentState


def _outcome(name: str, passed: bool) -> ScenarioOutcome:
    return ScenarioOutcome(
        scenario=name,
        final_state=IncidentState.ESCALATED,
        tool_calls_used=0,
        report=GradeReport(
            scenario=name,
            passed=passed,
            dimensions=(
                DimensionResult(
                    dimension=GradeDimension.OUTCOME,
                    passed=passed,
                    detail="",
                ),
            ),
        ),
    )


def _report(
    outcomes: tuple[ScenarioOutcome, ...],
    *,
    degraded_count: int | None = None,
    only_patterns: tuple[str, ...] = (),
) -> RunReport:
    passed = sum(1 for o in outcomes if o.report.passed)
    return RunReport(
        generated_at=datetime(2026, 7, 16, tzinfo=UTC),
        total=len(outcomes),
        passed=passed,
        failed=len(outcomes) - passed,
        degraded_count=degraded_count,
        only_patterns=only_patterns,
        outcomes=outcomes,
    )


def _point_gate_at(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    baseline: RunReport,
    latest: RunReport | None,
) -> None:
    """Write synthetic reports under tmp_path and aim main() at them.

    evals/reports/ is append-only evidence, so tests never point at the real directory.
    A DECOY older report goes down alongside, so a gate resolving oldest-first fails.
    """
    reports = tmp_path / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    baseline_path = reports / "baseline.json"
    baseline_path.write_text(baseline.model_dump_json())
    if latest is not None:
        artifacts.write_versioned(
            "report",
            content=baseline.model_dump_json(),
            timestamp=datetime(2026, 1, 1, tzinfo=UTC),
            invocation_id="decoy0000001",
            directory=reports,
        )
        artifacts.write_versioned(
            "report",
            content=latest.model_dump_json(),
            timestamp=datetime(2026, 9, 6, tzinfo=UTC),
            invocation_id="graded000002",
            directory=reports,
        )
    monkeypatch.setattr(regression, "_BASELINE", baseline_path)
    monkeypatch.setattr(regression, "_REPORTS_DIR", reports)


class TestCompare:
    def test_no_changes_no_regressions(self) -> None:
        baseline = _report((_outcome("a", True), _outcome("b", True)))
        latest = _report((_outcome("a", True), _outcome("b", True)))
        result = compare(baseline, latest)
        assert result.regressions == ()
        assert result.improvements == ()
        assert result.new_scenarios == ()
        assert result.dropped_scenarios == ()
        assert result.has_regressions is False

    def test_pass_to_fail_is_regression(self) -> None:
        baseline = _report((_outcome("a", True), _outcome("b", True)))
        latest = _report((_outcome("a", True), _outcome("b", False)))
        result = compare(baseline, latest)
        assert result.regressions == ("b",)
        assert result.has_regressions is True

    def test_fail_to_pass_is_improvement_not_regression(self) -> None:
        baseline = _report((_outcome("a", False),))
        latest = _report((_outcome("a", True),))
        result = compare(baseline, latest)
        assert result.regressions == ()
        assert result.improvements == ("a",)
        assert result.has_regressions is False

    def test_new_scenario_not_regression(self) -> None:
        baseline = _report((_outcome("a", True),))
        latest = _report((_outcome("a", True), _outcome("b", False)))
        result = compare(baseline, latest)
        assert result.regressions == ()
        assert result.new_scenarios == ("b",)
        assert result.has_regressions is False

    def test_dropped_is_not_a_regression(self) -> None:
        # Classification is unchanged by the A-03 fix: a dropped scenario is reported distinctly.
        # The GATE decision moved to main(), where TestMainGate pins that dropped now exits 1.
        baseline = _report((_outcome("a", True), _outcome("b", True)))
        latest = _report((_outcome("a", True),))
        result = compare(baseline, latest)
        assert result.dropped_scenarios == ("b",)
        assert result.regressions == ()
        assert result.has_regressions is False

    def test_regressions_sorted(self) -> None:
        baseline = _report((_outcome("z", True), _outcome("a", True), _outcome("m", True)))
        latest = _report((_outcome("z", False), _outcome("a", False), _outcome("m", True)))
        result = compare(baseline, latest)
        assert result.regressions == ("a", "z")

    def test_baseline_failing_scenario_still_failing_not_regression(self) -> None:
        baseline = _report((_outcome("a", False),))
        latest = _report((_outcome("a", False),))
        result = compare(baseline, latest)
        assert result.regressions == ()
        assert result.improvements == ()


class TestMainGate:
    """Exit-code policy of regression.main() (A-03, S-14; ADR 0013).

    0 = comparable full-suite input with no regressions or coverage loss; 1 = gate failed;
    2 = not a comparable input. A provenance mismatch warns and never gates (S-14).
    """

    def test_dropped_scenarios_fail_the_gate(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The exact repro_a03.py shape: a latest that silently lost b and c exited 0.
        baseline = _report((_outcome("a", True), _outcome("b", True), _outcome("c", True)))
        latest = _report((_outcome("a", True),))
        _point_gate_at(monkeypatch, tmp_path, baseline, latest)
        assert regression.main() == 1
        err = capsys.readouterr().err
        assert "GATE FAIL" in err
        assert "2 baseline scenario(s) missing" in err

    def test_dropped_is_not_a_regression_but_fails_the_gate(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The two halves of the old test_dropped_scenario_reported, made
        # explicit: classification unchanged (intent), exit 0 was the bug.
        baseline = _report((_outcome("a", True), _outcome("b", True)))
        latest = _report((_outcome("a", True),))
        result = compare(baseline, latest)
        assert result.regressions == ()
        assert result.dropped_scenarios == ("b",)
        _point_gate_at(monkeypatch, tmp_path, baseline, latest)
        assert regression.main() == 1

    def test_regressions_still_fail_the_gate(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        baseline = _report((_outcome("a", True), _outcome("b", True)))
        latest = _report((_outcome("a", True), _outcome("b", False)))
        _point_gate_at(monkeypatch, tmp_path, baseline, latest)
        assert regression.main() == 1

    def test_filtered_latest_is_refused_even_when_green(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A filtered run proves nothing about the full suite, so it is refused (exit 2).
        baseline = _report((_outcome("a", True), _outcome("b", True)))
        latest = _report(
            (_outcome("a", True), _outcome("b", True)),
            only_patterns=("remediate_",),
        )
        _point_gate_at(monkeypatch, tmp_path, baseline, latest)
        assert regression.main() == 2
        err = capsys.readouterr().err
        assert "filtered run" in err
        assert "remediate_" in err

    def test_missing_latest_is_not_comparable(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        baseline = _report((_outcome("a", True),))
        _point_gate_at(monkeypatch, tmp_path, baseline, latest=None)
        assert regression.main() == 2
        assert "no report found under" in capsys.readouterr().err

    def test_provenance_unknown_warns_without_gating(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The committed baseline predates provenance stamping while a fresh latest records 32:
        # the gate warns and still exits 0 (the S-14 contract).
        baseline = _report(
            (_outcome("a", True), _outcome("b", True)),
            degraded_count=None,
        )
        latest = _report(
            (_outcome("a", True), _outcome("b", True)),
            degraded_count=32,
        )
        _point_gate_at(monkeypatch, tmp_path, baseline, latest)
        assert regression.main() == 0
        assert "PROVENANCE" in capsys.readouterr().out

    def test_provenance_mismatch_warns_without_gating(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        baseline = _report(
            (_outcome("a", True), _outcome("b", True)),
            degraded_count=0,
        )
        latest = _report(
            (_outcome("a", True), _outcome("b", True)),
            degraded_count=32,
        )
        _point_gate_at(monkeypatch, tmp_path, baseline, latest)
        assert regression.main() == 0
        out = capsys.readouterr().out
        assert "PROVENANCE WARNING" in out
        assert "baseline ran 0 degraded, latest 32" in out

    def test_full_suite_matching_provenance_is_quiet_and_green(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # New scenarios still never fail the gate, and equal known
        # degraded_counts emit no provenance line at all.
        baseline = _report(
            (_outcome("a", True), _outcome("b", True)),
            degraded_count=0,
        )
        latest = _report(
            (_outcome("a", True), _outcome("b", True), _outcome("c", False)),
            degraded_count=0,
        )
        _point_gate_at(monkeypatch, tmp_path, baseline, latest)
        assert regression.main() == 0
        assert "PROVENANCE" not in capsys.readouterr().out


def _graded(name: str, dimensions: dict[GradeDimension, str]) -> ScenarioOutcome:
    """A passing outcome whose dimensions carry the given detail strings.

    A suite green while checking less.
    """
    return ScenarioOutcome(
        scenario=name,
        final_state=IncidentState.ESCALATED,
        tool_calls_used=0,
        report=GradeReport(
            scenario=name,
            passed=True,
            dimensions=tuple(
                DimensionResult(dimension=dim, passed=True, detail=detail)
                for dim, detail in dimensions.items()
            ),
        ),
    )


_FULL: dict[GradeDimension, str] = {
    GradeDimension.OUTCOME: "terminal state escalated matched expectation",
    GradeDimension.ACTION: "equivalent action fired: ['restart_consumer_group'] (1 call(s))",
    GradeDimension.SAFETY: "no replay attempts on 2 forbidden job_ids",
}


class TestCoverageLossWithoutRedScenarios:
    """The suite can shrink while every scenario still passes.

    ``GradeReport.passed`` is an ``all()`` over the dimensions, so removing
    a check can only push the roll-up greener. Both shapes below leave
    every scenario green and the old scenario-level diff completely empty.
    """

    def test_dropping_a_grading_dimension_fails_the_gate(self) -> None:
        baseline = _report((_graded("a", _FULL),))
        thinner = dict(_FULL)
        del thinner[GradeDimension.SAFETY]
        latest = _report((_graded("a", thinner),))

        result = compare(baseline, latest)
        assert result.regressions == ()
        assert result.dropped_scenarios == ()
        assert result.dropped_dimensions == ("a:safety",)
        assert result.gate_failed

    def test_vacating_an_assertion_fails_the_gate(self) -> None:
        baseline = _report((_graded("a", _FULL),))
        emptied = dict(_FULL)
        emptied[GradeDimension.ACTION] = "no action expectation set"
        latest = _report((_graded("a", emptied),))

        result = compare(baseline, latest)
        assert result.regressions == ()
        assert result.vacated_assertions == ("a:action",)
        assert result.gate_failed

    def test_the_legacy_baseline_safety_phrasing_is_not_a_false_positive(self) -> None:
        """The committed baseline says "no forbidden replay ids set".

        The grader says "no safety expectations set" now; both are the same vacuous state.
        """
        baseline = _report((_graded("a", {GradeDimension.SAFETY: "no forbidden replay ids set"}),))
        latest = _report((_graded("a", {GradeDimension.SAFETY: "no safety expectations set"}),))

        result = compare(baseline, latest)
        assert result.vacated_assertions == ()
        assert not result.gate_failed

    def test_adding_an_assertion_is_not_a_regression(self) -> None:
        baseline = _report((_graded("a", {GradeDimension.ACTION: "no action expectation set"}),))
        latest = _report((_graded("a", {GradeDimension.ACTION: "equivalent action fired: ['x']"}),))

        result = compare(baseline, latest)
        assert result.vacated_assertions == ()
        assert not result.gate_failed

    def test_a_new_dimension_is_not_a_drop(self) -> None:
        baseline = _report((_graded("a", {GradeDimension.OUTCOME: "ok"}),))
        latest = _report(
            (_graded("a", {GradeDimension.OUTCOME: "ok", GradeDimension.SAFETY: "clean"}),)
        )

        result = compare(baseline, latest)
        assert result.dropped_dimensions == ()
        assert not result.gate_failed

    def test_identical_reports_report_no_changes(self) -> None:
        report = _report((_graded("a", _FULL), _graded("b", _FULL)))
        result = compare(report, report)
        assert not result.gate_failed
        assert result.dropped_dimensions == ()
        assert result.vacated_assertions == ()

    def test_gate_exits_nonzero_and_explains_a_vacated_assertion(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        baseline = _report((_graded("a", _FULL),))
        emptied = dict(_FULL)
        emptied[GradeDimension.ACTION] = "no action expectation set"
        _point_gate_at(monkeypatch, tmp_path, baseline, _report((_graded("a", emptied),)))

        assert regression.main() == 1
        captured = capsys.readouterr()
        assert "VACATED ASSERTIONS" in captured.out
        assert "a:action" in captured.out
        assert "empty assertion" in captured.err

    def test_committed_baseline_compares_clean_against_itself(self) -> None:
        """The real artifact, not a synthetic one — no false positives."""
        baseline = regression._load_report(
            Path(__file__).resolve().parents[2] / "evals" / "reports" / "baseline.json"
        )
        result = compare(baseline, baseline)
        assert not result.gate_failed

    def test_root_cause_joining_the_grader_does_not_gate_the_committed_baseline(
        self,
    ) -> None:
        """WO-R3-191's gate question, asked against the real baseline.

        The baseline's rows carry five dimensions and fresh rows six — coverage GROWING, which
        ``dropped_dimensions`` (baseline − latest) cannot see. Asserted on the committed artifact.
        """
        baseline = regression._load_report(
            Path(__file__).resolve().parents[2] / "evals" / "reports" / "baseline.json"
        )
        assert baseline.outcomes, "the committed baseline is empty"
        assert not any(
            dimension.dimension is GradeDimension.ROOT_CAUSE
            for outcome in baseline.outcomes
            for dimension in outcome.report.dimensions
        ), "the baseline already carries ROOT_CAUSE — this test has served its purpose"

        latest = baseline.model_copy(
            update={
                "outcomes": tuple(
                    outcome.model_copy(
                        update={
                            "report": outcome.report.model_copy(
                                update={
                                    "dimensions": (
                                        *outcome.report.dimensions,
                                        DimensionResult(
                                            dimension=GradeDimension.ROOT_CAUSE,
                                            passed=True,
                                            detail="no ground truth set",
                                        ),
                                    )
                                }
                            )
                        }
                    )
                    for outcome in baseline.outcomes
                )
            }
        )
        result = compare(baseline, latest)
        assert result.dropped_dimensions == (), (
            "the gate read a NEW dimension as a dropped one; re-blessing the baseline "
            "would hide real coverage loss behind that re-bless"
        )
        assert result.regressions == ()
        assert result.vacated_assertions == ()
        assert not result.gate_failed


class TestVacuityClassifier:
    """``is_vacuous_detail`` reads the grader's own wording — pin both sides."""

    @pytest.mark.parametrize(
        "detail",
        [
            "no evidence expectations set",
            "no budget expectation set",
            "no action expectation set",
            "no safety expectations set",
            "no forbidden replay ids set",
        ],
    )
    def test_nothing_asserted_details_are_vacuous(self, detail: str) -> None:
        assert is_vacuous_detail(detail)

    @pytest.mark.parametrize(
        "detail",
        [
            "no replay attempts on 2 forbidden job_ids",
            "no tool from equivalence set ['x'] was called; tools called: []",
            "terminal state escalated matched expectation",
            "used 2 tool calls, cap 6",
            "",
        ],
    )
    def test_substantive_details_are_not_vacuous(self, detail: str) -> None:
        assert not is_vacuous_detail(detail)

    def test_the_world_scoped_not_graded_detail_is_vacuous(self) -> None:
        """INC-003's second shape of "nothing was asserted here".

        ROOT_CAUSE passes without a verdict when no label is declared and when the run was in a
        world the label does not describe; both must read as vacuous.
        """
        detail = not_graded_detail("db_query_latency")
        assert is_vacuous_detail(detail)

    def test_a_graded_root_cause_verdict_is_still_substantive(self) -> None:
        """The near-miss the shape must not swallow."""
        assert not is_vacuous_detail(
            "diagnosed no_fault; ground truth db_query_latency — not the declared cause "
            "(precision 0.00, recall 0.00, F1 0.00)"
        )

    def test_every_nothing_asserted_branch_in_the_grader_is_classified(self) -> None:
        """Walks the grader for the literal it emits when nothing is set.

        A new dimension with other wording would be invisible.
        """
        source = (
            Path(__file__).resolve().parents[2] / "evals" / "graders" / "deterministic.py"
        ).read_text(encoding="utf-8")
        emitted = set(re.findall(r'detail="(no [^"]*set)"', source))
        assert emitted, (
            "no 'nothing asserted' detail literals found in deterministic.py — "
            "either they were rephrased (update is_vacuous_detail and this "
            "walk together) or this test lost its subject."
        )
        unclassified = sorted(d for d in emitted if not is_vacuous_detail(d))
        assert unclassified == [], (
            f"the grader emits {unclassified} when no expectation is set, but "
            f"is_vacuous_detail does not recognise them, so evals/regression.py "
            f"cannot tell a deleted expectation from a real pass."
        )
