from datetime import UTC, datetime

from evals.graders.deterministic import (
    DimensionResult,
    GradeDimension,
    GradeReport,
)
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


def _report(outcomes: tuple[ScenarioOutcome, ...]) -> RunReport:
    passed = sum(1 for o in outcomes if o.report.passed)
    return RunReport(
        generated_at=datetime(2026, 7, 16, tzinfo=UTC),
        total=len(outcomes),
        passed=passed,
        failed=len(outcomes) - passed,
        outcomes=outcomes,
    )


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

    def test_dropped_scenario_reported(self) -> None:
        baseline = _report((_outcome("a", True), _outcome("b", True)))
        latest = _report((_outcome("a", True),))
        result = compare(baseline, latest)
        assert result.dropped_scenarios == ("b",)
        assert result.regressions == ()

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
