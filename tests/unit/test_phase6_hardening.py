"""Regression tests for the live-eval hardening fixes.

Covers three behavior changes:

1. ``run_to_completion`` exempts VERIFYING from the budget short-circuit —
   an executed Tier-1 action must always be verified, even over budget.
2. ``make_llm_plan`` refuses to enter REMEDIATING without headroom for
   the action + verify pair (>= 2 tool calls remaining).
3. ``make_llm_verify`` can poll the verify probe: ``not_verified`` on an
   eventually-consistent read retries after a delay instead of
   escalating on the first instant read.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from incident_commander.agent.hypothesis import Hypothesis, HypothesisCategory
from incident_commander.agent.loop import run_to_completion
from incident_commander.agent.remediation import (
    RemediationPlan,
    make_llm_plan,
    make_llm_verify,
)
from incident_commander.agent.state import BudgetLedger, IncidentState, RunState
from incident_commander.llm.fakes import CannedLLMClient
from incident_commander.tools.mcp_client import ToolResult


def _clock() -> datetime:
    return datetime.now(UTC)


def _budget(max_tool_calls: int = 10, used: int = 0) -> BudgetLedger:
    return BudgetLedger(
        max_tool_calls=max_tool_calls,
        tool_calls_used=used,
        max_tokens=100_000,
        max_wall_seconds=600,
        max_usd=Decimal("5.00"),
    )


def _run_state(state: IncidentState, budget: BudgetLedger, **extra: Any) -> RunState:
    now = _clock()
    return RunState(
        incident_id=uuid4(),
        state=state,
        alert={"source": "test", "severity": "high"},
        budget=budget,
        created_at=now,
        updated_at=now,
        **extra,
    )


_PLAN = RemediationPlan(
    target_hypothesis="consumer_saturation",
    action_tool="restart_consumer_group",
    action_arguments={"consumer_group": "worker-dispatcher"},
    verify_tool="get_consumer_lag",
    verify_arguments={"consumer_group": "worker-dispatcher"},
    verify_expectation="lag should drop toward zero",
)


class _SequencedMCP:
    """Returns one canned ToolResult per call, in order."""

    def __init__(self, results: list[ToolResult]) -> None:
        self._results = list(results)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call_tool(self, name: str, arguments: Mapping[str, Any]) -> ToolResult:
        self.calls.append((name, dict(arguments)))
        return self._results.pop(0)


def _lag_result(lag: int) -> ToolResult:
    payload = {
        "consumer_group": "worker-dispatcher",
        "lag": lag,
        "cache_key": "kafka:consumer_lag:worker-dispatcher",
    }
    return ToolResult(content=[{"type": "text", "text": json.dumps(payload)}], is_error=False)


class TestBudgetExemptsVerifying:
    def test_verifying_dispatches_even_when_budget_exhausted(self) -> None:
        run = _run_state(
            IncidentState.VERIFYING,
            _budget(max_tool_calls=3, used=3),  # exhausted
            remediation_plan=_PLAN.model_dump(mode="json"),
        )

        def verify_stub(rs: RunState, at: datetime) -> RunState:
            return rs.with_state(IncidentState.RESOLVED, at)

        final = run_to_completion(
            run, clock=_clock, transitions={IncidentState.VERIFYING: verify_stub}
        )
        assert final.state is IncidentState.RESOLVED
        assert not any(e.tool_name == "_escalate" for e in final.evidence)

    def test_non_verifying_states_still_short_circuit(self) -> None:
        run = _run_state(IncidentState.INVESTIGATING, _budget(max_tool_calls=3, used=3))
        final = run_to_completion(run, clock=_clock, transitions={})
        assert final.state is IncidentState.ESCALATED
        assert any("budget exhausted" in e.result_summary for e in final.evidence)


class TestPlanningHeadroom:
    def test_plan_escalates_without_room_for_action_plus_verify(self) -> None:
        llm = CannedLLMClient([])  # must never be consulted
        transition = make_llm_plan(llm, model="test-model")
        run = _run_state(
            IncidentState.PLANNING,
            _budget(max_tool_calls=5, used=4),  # only 1 call left
            hypotheses=(
                Hypothesis(
                    category=HypothesisCategory.CONSUMER_SATURATION,
                    name="consumer_saturation",
                    confidence=0.9,
                    reasoning="lag sustained",
                ),
            ),
        )
        result = transition(run, _clock())
        assert result.state is IncidentState.ESCALATED
        assert any("insufficient tool-call budget" in e.result_summary for e in result.evidence)
        assert llm.calls == []  # escalated before spending planner tokens


class TestVerifyPolling:
    def test_not_verified_then_verified_resolves_on_second_probe(self) -> None:
        mcp = _SequencedMCP([_lag_result(15_000), _lag_result(0)])
        judge = CannedLLMClient(
            [
                {"verdict": "not_verified", "reasoning": "lag still 15000"},
                {"verdict": "verified", "reasoning": "lag recovered to 0"},
            ]
        )
        slept: list[float] = []
        transition = make_llm_verify(
            mcp,
            judge,
            model="test-model",
            probe_attempts=3,
            probe_delay_seconds=15.0,
            sleep=slept.append,
        )
        run = _run_state(
            IncidentState.VERIFYING,
            _budget(max_tool_calls=10, used=2),
            remediation_plan=_PLAN.model_dump(mode="json"),
        )
        result = transition(run, _clock())
        assert result.state is IncidentState.RESOLVED
        assert slept == [15.0]  # one wait between the two probes
        assert result.budget.tool_calls_used == 4  # +2 probes
        judge_entries = [e for e in result.evidence if e.tool_name == "_verify_judge"]
        assert len(judge_entries) == 2
        assert judge_entries[-1].result_summary.startswith("verified")

    def test_exhausting_attempts_escalates_with_full_probe_history(self) -> None:
        mcp = _SequencedMCP([_lag_result(15_000), _lag_result(14_000)])
        judge = CannedLLMClient(
            [
                {"verdict": "not_verified", "reasoning": "still high"},
                {"verdict": "not_verified", "reasoning": "barely moved"},
            ]
        )
        transition = make_llm_verify(
            mcp,
            judge,
            model="test-model",
            probe_attempts=2,
            probe_delay_seconds=0.0,
            sleep=lambda _s: None,
        )
        run = _run_state(
            IncidentState.VERIFYING,
            _budget(max_tool_calls=10, used=2),
            remediation_plan=_PLAN.model_dump(mode="json"),
        )
        result = transition(run, _clock())
        assert result.state is IncidentState.ESCALATED
        assert len(mcp.calls) == 2
        assert len([e for e in result.evidence if e.tool_name == "_verify_judge"]) == 2

    def test_default_single_probe_preserves_legacy_behavior(self) -> None:
        mcp = _SequencedMCP([_lag_result(15_000)])
        judge = CannedLLMClient([{"verdict": "not_verified", "reasoning": "still high"}])
        transition = make_llm_verify(mcp, judge, model="test-model")
        run = _run_state(
            IncidentState.VERIFYING,
            _budget(max_tool_calls=10, used=2),
            remediation_plan=_PLAN.model_dump(mode="json"),
        )
        result = transition(run, _clock())
        assert result.state is IncidentState.ESCALATED
        assert len(mcp.calls) == 1
