from datetime import datetime

from evals.graders.deterministic import (
    DimensionResult,
    GradeDimension,
    GradeReport,
    ScenarioExpectation,
    grade,
)
from incident_commander.agent.state import EvidenceEntry, IncidentState, RunState


def _with_terminal(
    run_state: RunState, state: IncidentState, evidence: tuple[EvidenceEntry, ...] = ()
) -> RunState:
    return run_state.model_copy(update={"state": state, "evidence": evidence})


def _evidence(now: datetime, tool: str, summary: str) -> EvidenceEntry:
    return EvidenceEntry(
        tool_name=tool,
        arguments={},
        result_summary=summary,
        timestamp=now,
    )


class TestOutcomeDimension:
    def test_matching_terminal_state_passes(self, run_state: RunState) -> None:
        run = _with_terminal(run_state, IncidentState.ESCALATED)
        exp = ScenarioExpectation(name="s", expected_terminal_state=IncidentState.ESCALATED)
        report = grade(run, exp)
        outcome = _dim(report, GradeDimension.OUTCOME)
        assert outcome.passed is True

    def test_wrong_terminal_state_fails(self, run_state: RunState) -> None:
        run = _with_terminal(run_state, IncidentState.ESCALATED)
        exp = ScenarioExpectation(name="s", expected_terminal_state=IncidentState.RESOLVED)
        report = grade(run, exp)
        outcome = _dim(report, GradeDimension.OUTCOME)
        assert outcome.passed is False
        assert "resolved" in outcome.detail
        assert "escalated" in outcome.detail


class TestEvidenceDimension:
    def test_no_expectations_passes(self, run_state: RunState) -> None:
        run = _with_terminal(run_state, IncidentState.ESCALATED)
        exp = ScenarioExpectation(name="s", expected_terminal_state=IncidentState.ESCALATED)
        report = grade(run, exp)
        assert _dim(report, GradeDimension.EVIDENCE).passed is True

    def test_all_expected_signals_present_passes(self, run_state: RunState, now: datetime) -> None:
        evidence = (_evidence(now, "get_consumer_lag", '{"group":"billing","lag":42}'),)
        run = _with_terminal(run_state, IncidentState.ESCALATED, evidence)
        exp = ScenarioExpectation(
            name="s",
            expected_terminal_state=IncidentState.ESCALATED,
            expected_evidence_contains=("billing", "lag"),
        )
        report = grade(run, exp)
        result = _dim(report, GradeDimension.EVIDENCE)
        assert result.passed is True

    def test_missing_signal_fails_with_detail(self, run_state: RunState, now: datetime) -> None:
        evidence = (_evidence(now, "get_consumer_lag", "lag=42"),)
        run = _with_terminal(run_state, IncidentState.ESCALATED, evidence)
        exp = ScenarioExpectation(
            name="s",
            expected_terminal_state=IncidentState.ESCALATED,
            expected_evidence_contains=("billing", "payments"),
        )
        report = grade(run, exp)
        result = _dim(report, GradeDimension.EVIDENCE)
        assert result.passed is False
        assert "billing" in result.detail
        assert "payments" in result.detail


class TestBudgetDimension:
    def test_no_cap_passes(self, run_state: RunState) -> None:
        run = _with_terminal(run_state, IncidentState.ESCALATED)
        exp = ScenarioExpectation(name="s", expected_terminal_state=IncidentState.ESCALATED)
        report = grade(run, exp)
        assert _dim(report, GradeDimension.BUDGET).passed is True

    def test_under_cap_passes(self, run_state: RunState) -> None:
        used = run_state.budget.model_copy(update={"tool_calls_used": 3})
        run = run_state.model_copy(update={"state": IncidentState.ESCALATED, "budget": used})
        exp = ScenarioExpectation(
            name="s",
            expected_terminal_state=IncidentState.ESCALATED,
            max_tool_calls=5,
        )
        report = grade(run, exp)
        assert _dim(report, GradeDimension.BUDGET).passed is True

    def test_over_cap_fails(self, run_state: RunState) -> None:
        used = run_state.budget.model_copy(update={"tool_calls_used": 8})
        run = run_state.model_copy(update={"state": IncidentState.ESCALATED, "budget": used})
        exp = ScenarioExpectation(
            name="s",
            expected_terminal_state=IncidentState.ESCALATED,
            max_tool_calls=5,
        )
        report = grade(run, exp)
        result = _dim(report, GradeDimension.BUDGET)
        assert result.passed is False
        assert "8" in result.detail and "5" in result.detail

    def test_at_cap_passes(self, run_state: RunState) -> None:
        used = run_state.budget.model_copy(update={"tool_calls_used": 5})
        run = run_state.model_copy(update={"state": IncidentState.ESCALATED, "budget": used})
        exp = ScenarioExpectation(
            name="s",
            expected_terminal_state=IncidentState.ESCALATED,
            max_tool_calls=5,
        )
        report = grade(run, exp)
        assert _dim(report, GradeDimension.BUDGET).passed is True


class TestAggregate:
    def test_all_dimensions_pass_report_passes(self, run_state: RunState, now: datetime) -> None:
        evidence = (_evidence(now, "get_consumer_lag", '{"lag":0}'),)
        run = _with_terminal(run_state, IncidentState.ESCALATED, evidence)
        exp = ScenarioExpectation(
            name="happy",
            expected_terminal_state=IncidentState.ESCALATED,
            expected_evidence_contains=("lag",),
            max_tool_calls=25,
        )
        report = grade(run, exp)
        assert report.passed is True
        assert report.scenario == "happy"
        assert len(report.dimensions) == 3

    def test_any_dimension_fails_report_fails(self, run_state: RunState, now: datetime) -> None:
        run = _with_terminal(run_state, IncidentState.RESOLVED)
        exp = ScenarioExpectation(name="sad", expected_terminal_state=IncidentState.ESCALATED)
        report = grade(run, exp)
        assert report.passed is False

    def test_report_serializes_isomorphically(self, run_state: RunState, now: datetime) -> None:
        run = _with_terminal(run_state, IncidentState.ESCALATED)
        exp = ScenarioExpectation(name="s", expected_terminal_state=IncidentState.ESCALATED)
        report = grade(run, exp)
        loaded = GradeReport.model_validate_json(report.model_dump_json())
        assert loaded == report


def _dim(report: GradeReport, name: GradeDimension) -> DimensionResult:
    for d in report.dimensions:
        if d.dimension == name:
            return d
    raise AssertionError(f"dimension {name.value} not in report")
