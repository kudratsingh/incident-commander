from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from evals.fakes import CannedMCPClient
from evals.graders.deterministic import ScenarioExpectation
from evals.runner import RunReport, run_all, run_scenario, write_report
from evals.scenarios.schema import Scenario
from incident_commander.agent.state import IncidentState
from incident_commander.api.schemas import AlertPayload
from incident_commander.config import Settings
from incident_commander.tools.mcp_client import MCPError, ToolResult


def _test_settings(**overrides: Any) -> Settings:
    defaults: dict[str, Any] = {
        "anthropic_api_key": SecretStr("eval"),
        "judge_model": "eval-judge",
        "platform_mcp_url": "https://eval.local",
        "platform_rest_url": "https://eval.local",
        "platform_token": SecretStr("eval"),
        "platform_webhook_secret": SecretStr("eval"),
        "database_url": "postgresql://eval:eval@localhost:5432/eval",
    }
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)  # type: ignore[call-arg]


def _passing_scenario() -> Scenario:
    return Scenario(
        name="consumer_lag_pass",
        alert=AlertPayload(source="platform.kafka", severity="high", group="billing"),
        expectation=ScenarioExpectation(
            name="consumer_lag_pass",
            expected_terminal_state=IncidentState.ESCALATED,
            expected_evidence_contains=("billing", "lag"),
            max_tool_calls=5,
        ),
        canned_tool_responses={
            "get_consumer_lag": ToolResult(
                content=[
                    {
                        "type": "text",
                        "text": '{"group":"billing","lag":42,"timestamp":"2026-07-16T12:00:00Z"}',
                    }
                ],
            )
        },
    )


def _noise_scenario() -> Scenario:
    return Scenario(
        name="noise_alert",
        alert=AlertPayload(source="test", severity="info"),
        expectation=ScenarioExpectation(
            name="noise_alert",
            expected_terminal_state=IncidentState.ESCALATED,
        ),
    )


def _bad_expectation_scenario() -> Scenario:
    return Scenario(
        name="misexpected",
        alert=AlertPayload(source="test", severity="info"),
        expectation=ScenarioExpectation(
            name="misexpected",
            expected_terminal_state=IncidentState.RESOLVED,
        ),
    )


class TestRunScenario:
    def test_probe_scenario_escalates_and_passes(self) -> None:
        outcome = run_scenario(_passing_scenario(), _test_settings())
        assert outcome.final_state is IncidentState.ESCALATED
        assert outcome.tool_calls_used == 1
        assert outcome.report.passed is True

    def test_noise_scenario_short_circuits_at_triage(self) -> None:
        outcome = run_scenario(_noise_scenario(), _test_settings())
        assert outcome.final_state is IncidentState.ESCALATED
        assert outcome.tool_calls_used == 0

    def test_mismatched_expectation_fails(self) -> None:
        outcome = run_scenario(_bad_expectation_scenario(), _test_settings())
        assert outcome.report.passed is False
        assert outcome.final_state is IncidentState.ESCALATED

    def test_actionable_with_no_canned_response_escalates_on_tool_error(self) -> None:
        scenario = Scenario(
            name="missing_response",
            alert=AlertPayload(source="platform", severity="high", group="billing"),
            expectation=ScenarioExpectation(
                name="missing_response",
                expected_terminal_state=IncidentState.ESCALATED,
            ),
        )
        outcome = run_scenario(scenario, _test_settings())
        assert outcome.final_state is IncidentState.ESCALATED
        # Tool call raised MCPError before the budget increment.
        assert outcome.tool_calls_used == 0

    def test_clock_injection(self) -> None:
        fixed = datetime(2026, 1, 1, tzinfo=UTC)
        outcome = run_scenario(_passing_scenario(), _test_settings(), clock=lambda: fixed)
        assert outcome.report.passed is True


class TestRunAll:
    def test_counts_passed_and_failed(self) -> None:
        scenarios = [_passing_scenario(), _bad_expectation_scenario()]
        report = run_all(scenarios, _test_settings())
        assert report.total == 2
        assert report.passed == 1
        assert report.failed == 1
        assert {o.scenario for o in report.outcomes} == {
            "consumer_lag_pass",
            "misexpected",
        }

    def test_empty_scenario_list(self) -> None:
        report = run_all([], _test_settings())
        assert report.total == 0
        assert report.passed == 0
        assert report.failed == 0

    def test_shipped_scenario_passes(self) -> None:
        from evals.scenarios.loader import load_scenarios

        scenarios = load_scenarios(Path(__file__).resolve().parents[2] / "evals" / "scenarios")
        report = run_all(scenarios, _test_settings())
        assert report.failed == 0
        assert report.passed == len(scenarios)


class TestWriteReport:
    def test_round_trip_json(self, tmp_path: Path) -> None:
        report = run_all([_passing_scenario()], _test_settings())
        target = tmp_path / "latest.json"
        write_report(report, target)
        loaded = RunReport.model_validate_json(target.read_text())
        assert loaded == report

    def test_creates_parent_directory(self, tmp_path: Path) -> None:
        report = run_all([_passing_scenario()], _test_settings())
        target = tmp_path / "nested" / "reports" / "latest.json"
        write_report(report, target)
        assert target.exists()
        assert json.loads(target.read_text())["total"] == 1


class TestCannedMCPClient:
    def test_returns_scripted_response(self) -> None:
        response = ToolResult(content=[{"type": "text", "text": "hello"}])
        client = CannedMCPClient({"get_consumer_lag": response})
        result = client.call_tool("get_consumer_lag", {"group": "billing"})
        assert result == response
        assert client.calls == [("get_consumer_lag", {"group": "billing"})]

    def test_missing_response_raises_mcp_error(self) -> None:
        client = CannedMCPClient({})
        import pytest

        with pytest.raises(MCPError, match="no canned response"):
            client.call_tool("unknown_tool", {})
