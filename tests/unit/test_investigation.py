from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from incident_commander.agent.investigation import make_investigate
from incident_commander.agent.state import IncidentState, RunState
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


def _canned_result(payload: dict[str, Any]) -> ToolResult:
    return ToolResult(content=[{"type": "text", "text": json.dumps(payload)}])


def _with_alert(run_state: RunState, alert: dict[str, object]) -> RunState:
    return run_state.model_copy(update={"alert": alert})


class TestMakeInvestigate:
    def test_happy_path_escalates_with_evidence(self, run_state: RunState, now: datetime) -> None:
        client = _FakeMCPClient(
            lambda _n, _a: _canned_result(
                {
                    "consumer_group": "billing",
                    "lag": 42,
                    "cache_key": "kafka:consumer_lag:worker-dispatcher",
                }
            )
        )
        transition = make_investigate(client)
        run = _with_alert(
            run_state.model_copy(update={"state": IncidentState.INVESTIGATING}),
            {"consumer_group": "billing"},
        )
        result = transition(run, now)

        assert result.state is IncidentState.ESCALATED
        assert len(result.evidence) == 1
        entry = result.evidence[0]
        assert entry.tool_name == "get_consumer_lag"
        assert entry.arguments == {"consumer_group": "billing"}
        assert "lag" in entry.result_summary

    def test_budget_incremented_by_one(self, run_state: RunState, now: datetime) -> None:
        client = _FakeMCPClient(
            lambda _n, _a: _canned_result(
                {
                    "consumer_group": "billing",
                    "lag": 0,
                    "cache_key": "kafka:consumer_lag:worker-dispatcher",
                }
            )
        )
        transition = make_investigate(client)
        run = _with_alert(
            run_state.model_copy(update={"state": IncidentState.INVESTIGATING}),
            {"consumer_group": "billing"},
        )
        result = transition(run, now)
        assert result.budget.tool_calls_used == run.budget.tool_calls_used + 1

    def test_calls_tool_with_group_from_alert(self, run_state: RunState, now: datetime) -> None:
        client = _FakeMCPClient(
            lambda _n, _a: _canned_result(
                {
                    "consumer_group": "payments",
                    "lag": 1,
                    "cache_key": "kafka:consumer_lag:worker-dispatcher",
                }
            )
        )
        transition = make_investigate(client)
        run = _with_alert(
            run_state.model_copy(update={"state": IncidentState.INVESTIGATING}),
            {"consumer_group": "payments"},
        )
        transition(run, now)
        assert client.calls == [("get_consumer_lag", {"consumer_group": "payments"})]

    def test_missing_group_uses_unknown(self, run_state: RunState, now: datetime) -> None:
        client = _FakeMCPClient(
            lambda _n, _a: _canned_result(
                {
                    "consumer_group": "unknown",
                    "lag": 0,
                    "cache_key": "kafka:consumer_lag:worker-dispatcher",
                }
            )
        )
        transition = make_investigate(client)
        run = _with_alert(
            run_state.model_copy(update={"state": IncidentState.INVESTIGATING}),
            {"source": "billing"},
        )
        transition(run, now)
        # No consumer_group in alert → default to platform's worker-dispatcher.
        assert client.calls[0][1] == {"consumer_group": "worker-dispatcher"}

    def test_mcp_error_escalates_with_reason(self, run_state: RunState, now: datetime) -> None:
        def raise_error(_n: str, _a: Mapping[str, Any]) -> ToolResult:
            raise MCPError(-32602, "invalid group")

        transition = make_investigate(_FakeMCPClient(raise_error))
        run = _with_alert(
            run_state.model_copy(update={"state": IncidentState.INVESTIGATING}),
            {"consumer_group": "billing"},
        )
        result = transition(run, now)
        assert result.state is IncidentState.ESCALATED
        assert "tool error" in result.evidence[0].result_summary

    def test_mcp_error_does_not_increment_budget(self, run_state: RunState, now: datetime) -> None:
        def raise_error(_n: str, _a: Mapping[str, Any]) -> ToolResult:
            raise MCPError(-32602, "boom")

        transition = make_investigate(_FakeMCPClient(raise_error))
        run = _with_alert(
            run_state.model_copy(update={"state": IncidentState.INVESTIGATING}),
            {"consumer_group": "billing"},
        )
        result = transition(run, now)
        assert result.budget.tool_calls_used == run.budget.tool_calls_used

    def test_is_error_result_escalates(self, run_state: RunState, now: datetime) -> None:
        transition = make_investigate(
            _FakeMCPClient(
                lambda _n, _a: ToolResult(content=[{"type": "text", "text": "x"}], is_error=True)
            )
        )
        run = _with_alert(
            run_state.model_copy(update={"state": IncidentState.INVESTIGATING}),
            {"consumer_group": "billing"},
        )
        result = transition(run, now)
        assert result.state is IncidentState.ESCALATED
        assert "is_error=True" in result.evidence[0].result_summary

    def test_missing_text_block_escalates(self, run_state: RunState, now: datetime) -> None:
        transition = make_investigate(
            _FakeMCPClient(lambda _n, _a: ToolResult(content=[{"type": "image", "data": "..."}]))
        )
        run = _with_alert(
            run_state.model_copy(update={"state": IncidentState.INVESTIGATING}),
            {"consumer_group": "billing"},
        )
        result = transition(run, now)
        assert result.state is IncidentState.ESCALATED
        assert "output parse failed" in result.evidence[0].result_summary

    def test_invalid_output_shape_escalates(self, run_state: RunState, now: datetime) -> None:
        transition = make_investigate(
            _FakeMCPClient(lambda _n, _a: _canned_result({"consumer_group": "billing", "lag": -1}))
        )
        run = _with_alert(
            run_state.model_copy(update={"state": IncidentState.INVESTIGATING}),
            {"consumer_group": "billing"},
        )
        result = transition(run, now)
        assert result.state is IncidentState.ESCALATED
        assert "output parse failed" in result.evidence[0].result_summary

    def test_transition_leaves_input_unchanged(self, run_state: RunState, now: datetime) -> None:
        client = _FakeMCPClient(
            lambda _n, _a: _canned_result(
                {
                    "consumer_group": "billing",
                    "lag": 0,
                    "cache_key": "kafka:consumer_lag:worker-dispatcher",
                }
            )
        )
        transition = make_investigate(client)
        run = _with_alert(
            run_state.model_copy(update={"state": IncidentState.INVESTIGATING}),
            {"consumer_group": "billing"},
        )
        original_used = run.budget.tool_calls_used
        _ = transition(run, now)
        assert run.state is IncidentState.INVESTIGATING
        assert run.budget.tool_calls_used == original_used
        assert run.evidence == ()
