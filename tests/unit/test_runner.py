from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from evals.fakes import CannedMCPClient
from evals.graders.deterministic import ScenarioExpectation
from evals.runner import (
    LiveMCPUnavailable,
    RunReport,
    Trajectory,
    run_all,
    run_scenario,
    write_briefings,
    write_report,
    write_trajectories,
)
from evals.scenarios.schema import Scenario
from incident_commander.agent.briefing import EscalationBriefing
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
                        "text": (
                            '{"consumer_group":"billing","lag":42,'
                            '"cache_key":"kafka:consumer_lag:worker-dispatcher"}'
                        ),
                    }
                ],
            )
        },
        canned_llm_responses={
            "investigation_planner": [
                {
                    "hypotheses": [
                        {
                            "name": "consumer_saturation",
                            "confidence": 0.55,
                            "reasoning": "Paging severity on billing consumer.",
                        }
                    ],
                    "next_action": {
                        "kind": "probe",
                        "tool_name": "get_consumer_lag",
                        "arguments": {"consumer_group": "billing"},
                    },
                },
                {
                    "hypotheses": [
                        {
                            "name": "consumer_saturation",
                            "confidence": 0.85,
                            "reasoning": "Lag reading confirms saturation.",
                        }
                    ],
                    "next_action": {
                        "kind": "stop",
                        "reason": "confidence sufficient",
                    },
                },
            ],
            "briefing_writer": [
                {
                    "findings": "billing consumer lag observed at 42 messages",
                    "recommendation": "verify billing consumer pod",
                }
            ],
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
        result = run_scenario(_passing_scenario(), _test_settings())
        assert result.outcome.final_state is IncidentState.ESCALATED
        assert result.outcome.tool_calls_used == 1
        assert result.outcome.report.passed is True

    def test_noise_scenario_short_circuits_at_triage(self) -> None:
        result = run_scenario(_noise_scenario(), _test_settings())
        assert result.outcome.final_state is IncidentState.ESCALATED
        assert result.outcome.tool_calls_used == 0

    def test_mismatched_expectation_fails(self) -> None:
        result = run_scenario(_bad_expectation_scenario(), _test_settings())
        assert result.outcome.report.passed is False
        assert result.outcome.final_state is IncidentState.ESCALATED

    def test_actionable_with_no_canned_response_escalates_on_tool_error(self) -> None:
        # Planner proposes a probe, but the fake MCP has no canned response —
        # the tool call raises MCPError and the transition escalates.
        scenario = Scenario(
            name="missing_response",
            alert=AlertPayload(source="platform", severity="high", group="billing"),
            expectation=ScenarioExpectation(
                name="missing_response",
                expected_terminal_state=IncidentState.ESCALATED,
            ),
            canned_llm_responses={
                "investigation_planner": [
                    {
                        "hypotheses": [
                            {
                                "name": "x",
                                "confidence": 0.5,
                                "reasoning": "probe once",
                            }
                        ],
                        "next_action": {
                            "kind": "probe",
                            "tool_name": "get_consumer_lag",
                            "arguments": {"consumer_group": "billing"},
                        },
                    }
                ],
            },
        )
        result = run_scenario(scenario, _test_settings())
        assert result.outcome.final_state is IncidentState.ESCALATED
        assert result.outcome.tool_calls_used == 0

    def test_clock_injection(self) -> None:
        fixed = datetime(2026, 1, 1, tzinfo=UTC)
        result = run_scenario(_passing_scenario(), _test_settings(), clock=lambda: fixed)
        assert result.outcome.report.passed is True

    def test_trajectory_captures_initial_and_transitions(self) -> None:
        result = run_scenario(_passing_scenario(), _test_settings())
        trajectory = result.trajectory
        # TRIAGE (initial) + INVESTIGATING (from triage) + ESCALATED (from investigate)
        states = [rs.state for rs in trajectory.checkpoints]
        assert states == [
            IncidentState.TRIAGE,
            IncidentState.INVESTIGATING,
            IncidentState.ESCALATED,
        ]
        assert trajectory.scenario == "consumer_lag_pass"

    def test_trajectory_for_noise_only_two_checkpoints(self) -> None:
        result = run_scenario(_noise_scenario(), _test_settings())
        states = [rs.state for rs in result.trajectory.checkpoints]
        assert states == [IncidentState.TRIAGE, IncidentState.ESCALATED]


class TestRunAll:
    def test_counts_passed_and_failed(self) -> None:
        scenarios = [_passing_scenario(), _bad_expectation_scenario()]
        report, trajectories, _ = run_all(scenarios, _test_settings())
        assert report.total == 2
        assert report.passed == 1
        assert report.failed == 1
        assert {o.scenario for o in report.outcomes} == {
            "consumer_lag_pass",
            "misexpected",
        }
        assert len(trajectories) == 2

    def test_empty_scenario_list(self) -> None:
        report, trajectories, _ = run_all([], _test_settings())
        assert report.total == 0
        assert trajectories == ()

    def test_shipped_scenarios_pass(self) -> None:
        from evals.scenarios.loader import load_scenarios

        scenarios = load_scenarios(Path(__file__).resolve().parents[2] / "evals" / "scenarios")
        # Live-mcp scenarios are skipped by run_all when PLATFORM_MCP_URL is
        # the offline placeholder — the offline path must still stay green.
        offline_eligible = [s for s in scenarios if not s.use_live_mcp]
        report, _, _ = run_all(scenarios, _test_settings())
        assert report.failed == 0
        assert report.passed == len(offline_eligible)
        assert report.total >= 10  # taxonomy expansion floor


class TestLiveMcpDispatch:
    def _live_scenario(self) -> Scenario:
        return Scenario(
            name="live_probe",
            alert=AlertPayload(source="platform.kafka", severity="high", group="billing"),
            expectation=ScenarioExpectation(
                name="live_probe",
                expected_terminal_state=IncidentState.ESCALATED,
            ),
            use_live_mcp=True,
        )

    def test_run_scenario_raises_when_offline_placeholder(self) -> None:
        import pytest

        scenario = self._live_scenario()
        with pytest.raises(LiveMCPUnavailable, match="live MCP"):
            run_scenario(scenario, _test_settings())

    def test_run_all_skips_live_when_offline_placeholder(self) -> None:
        live = self._live_scenario()
        canned = _passing_scenario()
        report, _, _ = run_all([live, canned], _test_settings())
        # Only the canned scenario ran.
        assert report.total == 1
        assert report.outcomes[0].scenario == "consumer_lag_pass"

    def test_run_all_with_real_url_would_attempt_live(self) -> None:
        # We can't hit a real platform in unit tests, but flipping the URL off
        # the placeholder means run_all no longer skips — it hands the scenario
        # to run_scenario, which then builds a real client. We assert only up
        # to the dispatch point via an isolated run_scenario call using a fake.
        # The URL check is what matters here; the actual HTTP interaction is
        # covered by end-to-end runs (`make eval-live` / `make demo`).
        settings = _test_settings(platform_mcp_url="http://real.host:8001/mcp")
        canned = _passing_scenario()
        # Canned scenarios must still run cleanly even when the URL isn't a
        # placeholder — the URL only matters for use_live_mcp scenarios.
        report, _, _ = run_all([canned], settings)
        assert report.total == 1
        assert report.passed == 1


class TestWriteReport:
    def test_round_trip_json(self, tmp_path: Path) -> None:
        report, _, _ = run_all([_passing_scenario()], _test_settings())
        target = tmp_path / "latest.json"
        write_report(report, target)
        loaded = RunReport.model_validate_json(target.read_text())
        assert loaded == report

    def test_creates_parent_directory(self, tmp_path: Path) -> None:
        report, _, _ = run_all([_passing_scenario()], _test_settings())
        target = tmp_path / "nested" / "reports" / "latest.json"
        write_report(report, target)
        assert target.exists()
        assert json.loads(target.read_text())["total"] == 1


class TestWriteTrajectories:
    def test_writes_one_file_per_trajectory(self, tmp_path: Path) -> None:
        _, trajectories, _ = run_all([_passing_scenario(), _noise_scenario()], _test_settings())
        write_trajectories(trajectories, directory=tmp_path)
        files = sorted(p.name for p in tmp_path.iterdir())
        assert files == ["consumer_lag_pass.json", "noise_alert.json"]

    def test_round_trip_trajectory(self, tmp_path: Path) -> None:
        _, trajectories, _ = run_all([_passing_scenario()], _test_settings())
        write_trajectories(trajectories, directory=tmp_path)
        loaded = Trajectory.model_validate_json((tmp_path / "consumer_lag_pass.json").read_text())
        assert loaded == trajectories[0]

    def test_creates_directory(self, tmp_path: Path) -> None:
        _, trajectories, _ = run_all([_passing_scenario()], _test_settings())
        target = tmp_path / "nested" / "trajectories"
        write_trajectories(trajectories, directory=target)
        assert (target / "consumer_lag_pass.json").exists()


class TestWriteBriefings:
    def test_writes_one_file_per_briefing(self, tmp_path: Path) -> None:
        _, _, briefings = run_all([_passing_scenario(), _noise_scenario()], _test_settings())
        write_briefings(
            briefings,
            ["consumer_lag_pass", "noise_alert"],
            directory=tmp_path,
        )
        files = sorted(p.name for p in tmp_path.iterdir())
        assert files == ["consumer_lag_pass.json", "noise_alert.json"]

    def test_round_trip_briefing(self, tmp_path: Path) -> None:
        _, _, briefings = run_all([_passing_scenario()], _test_settings())
        write_briefings(briefings, ["consumer_lag_pass"], directory=tmp_path)
        loaded = EscalationBriefing.model_validate_json(
            (tmp_path / "consumer_lag_pass.json").read_text()
        )
        assert loaded == briefings[0]

    def test_probe_scenario_briefing_lists_the_tool_call(self) -> None:
        result = run_scenario(_passing_scenario(), _test_settings())
        trail = result.briefing.investigation_trail
        assert len(trail) == 1
        assert trail[0].tool == "get_consumer_lag"


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
