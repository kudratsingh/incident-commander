from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from incident_commander.agent.investigation import make_llm_investigate
from incident_commander.agent.state import IncidentState, RunState
from incident_commander.llm.client import LLMResult
from incident_commander.llm.fakes import CannedLLMClient
from incident_commander.tools.mcp_client import MCPError, ToolResult


class _FakeMCPClient:
    def __init__(
        self,
        handler: Callable[[str, Mapping[str, Any]], ToolResult],
    ) -> None:
        self._handler = handler
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call_tool(self, name: str, arguments: Mapping[str, Any]) -> ToolResult:
        self.calls.append((name, dict(arguments)))
        return self._handler(name, arguments)


def _consumer_lag_response(group: str, lag: int) -> ToolResult:
    return ToolResult(
        content=[
            {
                "type": "text",
                "text": json.dumps(
                    {"group": group, "lag": lag, "timestamp": "2026-07-16T12:00:00Z"}
                ),
            }
        ]
    )


def _probe_then_stop_llm(group: str = "billing") -> CannedLLMClient:
    return CannedLLMClient(
        [
            {
                "hypotheses": [
                    {
                        "name": "consumer_saturation",
                        "confidence": 0.55,
                        "reasoning": "Alert severity suggests saturation.",
                    }
                ],
                "next_action": {
                    "kind": "probe",
                    "tool_name": "get_consumer_lag",
                    "arguments": {"group": group},
                },
            },
            {
                "hypotheses": [
                    {
                        "name": "consumer_saturation",
                        "confidence": 0.9,
                        "reasoning": "Lag reading confirms saturation.",
                    }
                ],
                "next_action": {
                    "kind": "stop",
                    "reason": "confidence sufficient for handoff",
                },
            },
        ]
    )


def _investigating(run_state: RunState, group: str = "billing") -> RunState:
    return run_state.model_copy(
        update={
            "state": IncidentState.INVESTIGATING,
            "alert": {"source": "kafka", "severity": "high", "group": group},
        }
    )


class TestHappyPath:
    def test_probe_then_stop_escalates_with_hypotheses(
        self, run_state: RunState, now: datetime
    ) -> None:
        mcp = _FakeMCPClient(lambda _n, _a: _consumer_lag_response("billing", 42))
        llm = _probe_then_stop_llm()
        transition = make_llm_investigate(mcp, llm, model="claude-sonnet-4-6")
        result = transition(_investigating(run_state), now)

        assert result.state is IncidentState.ESCALATED
        assert result.hypotheses[0].name == "consumer_saturation"
        assert result.hypotheses[0].confidence == 0.9
        assert result.budget.tool_calls_used == 1

    def test_evidence_records_probe_output(self, run_state: RunState, now: datetime) -> None:
        mcp = _FakeMCPClient(lambda _n, _a: _consumer_lag_response("billing", 42))
        llm = _probe_then_stop_llm()
        transition = make_llm_investigate(mcp, llm, model="m")
        result = transition(_investigating(run_state), now)
        tool_evidence = [e for e in result.evidence if e.tool_name == "get_consumer_lag"]
        assert len(tool_evidence) == 1
        assert '"lag":42' in tool_evidence[0].result_summary

    def test_planner_stop_recorded(self, run_state: RunState, now: datetime) -> None:
        mcp = _FakeMCPClient(lambda _n, _a: _consumer_lag_response("billing", 42))
        transition = make_llm_investigate(mcp, _probe_then_stop_llm(), model="m")
        result = transition(_investigating(run_state), now)
        stops = [e for e in result.evidence if e.tool_name == "_planner_stop"]
        assert len(stops) == 1
        assert "confidence sufficient" in stops[0].result_summary


class TestImmediateStop:
    def test_planner_stops_without_probing(self, run_state: RunState, now: datetime) -> None:
        mcp = _FakeMCPClient(lambda _n, _a: _consumer_lag_response("billing", 0))
        llm = CannedLLMClient(
            [
                {
                    "hypotheses": [
                        {
                            "name": "false_positive",
                            "confidence": 0.8,
                            "reasoning": "Alert without actionable signal.",
                        }
                    ],
                    "next_action": {
                        "kind": "stop",
                        "reason": "no discriminating probe available",
                    },
                }
            ]
        )
        transition = make_llm_investigate(mcp, llm, model="m")
        result = transition(_investigating(run_state), now)
        assert result.state is IncidentState.ESCALATED
        assert result.budget.tool_calls_used == 0
        assert mcp.calls == []


class TestErrorPaths:
    def test_unknown_tool_from_planner_escalates(self, run_state: RunState, now: datetime) -> None:
        mcp = _FakeMCPClient(lambda _n, _a: _consumer_lag_response("billing", 42))
        llm = CannedLLMClient(
            [
                {
                    "hypotheses": [{"name": "x", "confidence": 0.5, "reasoning": "r"}],
                    "next_action": {
                        "kind": "probe",
                        "tool_name": "made_up_tool",
                        "arguments": {},
                    },
                }
            ]
        )
        transition = make_llm_investigate(mcp, llm, model="m")
        result = transition(_investigating(run_state), now)
        assert result.state is IncidentState.ESCALATED
        escalations = [e for e in result.evidence if e.tool_name == "_planner_escalate"]
        assert any("unknown tool" in e.result_summary for e in escalations)

    def test_tool_error_escalates(self, run_state: RunState, now: datetime) -> None:
        def erroring(_n: str, _a: Mapping[str, Any]) -> ToolResult:
            raise MCPError(-32602, "boom")

        mcp = _FakeMCPClient(erroring)
        llm = _probe_then_stop_llm()
        transition = make_llm_investigate(mcp, llm, model="m")
        result = transition(_investigating(run_state), now)
        assert result.state is IncidentState.ESCALATED
        escalations = [e for e in result.evidence if e.tool_name == "_planner_escalate"]
        assert any("tool error" in e.result_summary for e in escalations)
        assert result.budget.tool_calls_used == 0

    def test_output_schema_mismatch_escalates(self, run_state: RunState, now: datetime) -> None:
        mcp = _FakeMCPClient(
            lambda _n, _a: ToolResult(
                content=[
                    {
                        "type": "text",
                        "text": '{"group":"billing","lag":-99,"timestamp":"2026-07-16T12:00:00Z"}',
                    }
                ]
            )
        )
        transition = make_llm_investigate(mcp, _probe_then_stop_llm(), model="m")
        result = transition(_investigating(run_state), now)
        assert result.state is IncidentState.ESCALATED
        assert any("output parse failed" in e.result_summary for e in result.evidence)


class TestBudgetGuards:
    def test_exhausted_tokens_escalates_before_llm_call(
        self, run_state: RunState, now: datetime
    ) -> None:
        run = run_state.model_copy(
            update={
                "state": IncidentState.INVESTIGATING,
                "alert": {"source": "kafka", "severity": "high", "group": "billing"},
                "budget": run_state.budget.model_copy(
                    update={"tokens_used": run_state.budget.max_tokens}
                ),
            }
        )
        mcp = _FakeMCPClient(lambda _n, _a: _consumer_lag_response("billing", 42))
        llm = _probe_then_stop_llm()
        transition = make_llm_investigate(mcp, llm, model="m")
        result = transition(run, now)
        assert result.state is IncidentState.ESCALATED
        # No LLM calls happened.
        assert llm.calls == []

    def test_max_iterations_stops_probe_loop(self, run_state: RunState, now: datetime) -> None:
        # Planner always probes, never stops.
        mcp = _FakeMCPClient(lambda _n, _a: _consumer_lag_response("billing", 42))
        endless_probe = {
            "hypotheses": [{"name": "loop", "confidence": 0.3, "reasoning": "keep probing"}],
            "next_action": {
                "kind": "probe",
                "tool_name": "get_consumer_lag",
                "arguments": {"group": "billing"},
            },
        }
        llm = CannedLLMClient([endless_probe] * 10)
        transition = make_llm_investigate(mcp, llm, model="m", max_iterations=3)
        result = transition(_investigating(run_state), now)
        assert result.state is IncidentState.ESCALATED
        # Three iterations = three probes.
        assert result.budget.tool_calls_used == 3
        assert any("max iterations" in e.result_summary for e in result.evidence)

    def test_llm_tokens_billed_to_budget(self, run_state: RunState, now: datetime) -> None:
        from pydantic import BaseModel

        class _CountingLLM:
            def __init__(self) -> None:
                self.calls = 0

            def call[T: BaseModel](
                self,
                system_prompt: str,
                user_message: str,
                output_model: type[T],
                model: str,
                max_tokens: int = 2048,
            ) -> LLMResult[T]:
                self.calls += 1
                payload = {
                    "hypotheses": [{"name": "x", "confidence": 0.9, "reasoning": "r"}],
                    "next_action": {"kind": "stop", "reason": "done"},
                }
                return LLMResult(
                    output=output_model.model_validate(payload),
                    input_tokens=1000,
                    output_tokens=500,
                    cache_creation_tokens=0,
                    cache_read_tokens=0,
                    stop_reason="tool_use",
                )

        mcp = _FakeMCPClient(lambda _n, _a: _consumer_lag_response("billing", 42))
        llm = _CountingLLM()
        transition = make_llm_investigate(mcp, llm, model="m")
        result = transition(_investigating(run_state), now)
        assert result.budget.tokens_used == 1500
        assert llm.calls == 1
