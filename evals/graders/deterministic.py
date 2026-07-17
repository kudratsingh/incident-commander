"""Deterministic grader for a completed agent run.

Scores three dimensions with pure logic — no LLM in the loop:

* ``outcome``  — did the run reach the expected terminal state?
* ``evidence`` — do required signals appear in the evidence ledger?
* ``budget``   — did the run stay within the tool-call cap?

Aggregate ``passed`` is the conjunction. The scenario runner (Phase 1) will
call ``grade()`` per run and aggregate reports; regression gating (Phase 1)
compares aggregate counts against a committed baseline.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from incident_commander.agent.state import IncidentState, RunState


class GradeDimension(StrEnum):
    OUTCOME = "outcome"
    EVIDENCE = "evidence"
    BUDGET = "budget"


class ScenarioExpectation(BaseModel):
    """What we assert must be true of a completed run for the scenario to pass."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    expected_terminal_state: IncidentState
    expected_evidence_contains: tuple[str, ...] = ()
    max_tool_calls: int | None = None


class DimensionResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    dimension: GradeDimension
    passed: bool
    detail: str


class GradeReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    scenario: str
    passed: bool
    dimensions: tuple[DimensionResult, ...]


def grade(run: RunState, expectation: ScenarioExpectation) -> GradeReport:
    """Score a completed run. Returns a report; never raises on graded content."""
    dims = (
        _grade_outcome(run, expectation),
        _grade_evidence(run, expectation),
        _grade_budget(run, expectation),
    )
    return GradeReport(
        scenario=expectation.name,
        passed=all(d.passed for d in dims),
        dimensions=dims,
    )


def _grade_outcome(run: RunState, exp: ScenarioExpectation) -> DimensionResult:
    passed = run.state == exp.expected_terminal_state
    detail = (
        f"terminal state {run.state.value} matched expectation"
        if passed
        else f"expected {exp.expected_terminal_state.value}, got {run.state.value}"
    )
    return DimensionResult(dimension=GradeDimension.OUTCOME, passed=passed, detail=detail)


def _grade_evidence(run: RunState, exp: ScenarioExpectation) -> DimensionResult:
    if not exp.expected_evidence_contains:
        return DimensionResult(
            dimension=GradeDimension.EVIDENCE,
            passed=True,
            detail="no evidence expectations set",
        )
    corpus = " ".join(e.result_summary for e in run.evidence)
    missing = [s for s in exp.expected_evidence_contains if s not in corpus]
    if missing:
        return DimensionResult(
            dimension=GradeDimension.EVIDENCE,
            passed=False,
            detail=f"missing signals: {', '.join(missing)}",
        )
    return DimensionResult(
        dimension=GradeDimension.EVIDENCE,
        passed=True,
        detail=f"all {len(exp.expected_evidence_contains)} expected signals found",
    )


def _grade_budget(run: RunState, exp: ScenarioExpectation) -> DimensionResult:
    if exp.max_tool_calls is None:
        return DimensionResult(
            dimension=GradeDimension.BUDGET,
            passed=True,
            detail="no budget expectation set",
        )
    passed = run.budget.tool_calls_used <= exp.max_tool_calls
    detail = f"used {run.budget.tool_calls_used} tool calls, cap {exp.max_tool_calls}"
    return DimensionResult(dimension=GradeDimension.BUDGET, passed=passed, detail=detail)
