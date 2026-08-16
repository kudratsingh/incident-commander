from __future__ import annotations

import json
import sys
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from pydantic import SecretStr

from evals import runner as runner_module
from evals.chaos_hooks import ChaosInvocationError
from evals.fakes import CannedMCPClient
from evals.graders.deterministic import (
    DimensionResult,
    GradeDimension,
    GradeReport,
    ScenarioExpectation,
)
from evals.runner import (
    RunReport,
    ScenarioOutcome,
    ScenarioResult,
    Trajectory,
    _canned_equivalent_knob_warning,
    _classify_failure,
    _crashed_result,
    _eval_defaults,
    _is_offline_placeholder,
    _print_summary,
    archive_run,
    run_all,
    run_scenario,
    write_briefings,
    write_report,
    write_trajectories,
)
from evals.scenarios.schema import ChaosHook, PreconditionField, PreconditionProbe, Scenario
from incident_commander.agent import factory
from incident_commander.agent.briefing import EscalationBriefing
from incident_commander.agent.investigation import make_llm_investigate
from incident_commander.agent.remediation import make_llm_verify
from incident_commander.agent.state import BudgetLedger, EvidenceEntry, IncidentState, RunState
from incident_commander.api.schemas import AlertPayload
from incident_commander.config import Settings
from incident_commander.llm.fakes import CannedLLMClient
from incident_commander.tools.mcp_client import MCPError, ToolResult

_NOW = datetime(2026, 8, 8, tzinfo=UTC)


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
                            "category": "consumer_saturation",
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
                            "category": "consumer_saturation",
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
                                "category": "unknown",
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


class TestPostGradeDecorationsAreContained:
    """The briefing writer and the judge run AFTER grade(); neither may void the run.

    ADR 0007: a crashed scenario is an eval-infrastructure bug by definition,
    so a soft-quality decoration failing on an already-graded run must be
    recorded as a missing column, not a transport crash.
    """

    def _with_llm_queue(self, role: str, queue: list[dict[str, Any]]) -> Scenario:
        scenario = _passing_scenario()
        responses = dict(scenario.canned_llm_responses)
        responses[role] = queue
        return scenario.model_copy(update={"canned_llm_responses": responses})

    def test_briefing_enrichment_failure_preserves_the_graded_pass(self) -> None:
        # BriefingContent.findings has min_length=1, so an empty string is a
        # proven ValidationError raiser (test_briefing_enrichment.py).
        scenario = self._with_llm_queue(
            "briefing_writer", [{"findings": "", "recommendation": "x"}]
        )
        result = run_scenario(scenario, _test_settings())
        assert result.outcome.report.passed is True
        assert result.outcome.briefing_error is not None
        assert "briefing enrichment failed" in result.outcome.briefing_error
        # The deterministic briefing stands, unenriched.
        assert result.briefing.findings == ""
        assert result.briefing.recommendation == ""

    def test_briefing_enrichment_transport_failure_is_contained(self) -> None:
        # Empty queue → CannedLLMClient raises LLMError on exhaustion, the
        # canned stand-in for a live transport failure after retries.
        scenario = self._with_llm_queue("briefing_writer", [])
        scenario = scenario.model_copy(update={"use_live_llm": True})
        result = run_scenario(scenario, _test_settings())
        assert result.outcome.report.passed is True
        assert result.outcome.briefing_error is not None

    def test_judge_schema_violation_is_contained(self) -> None:
        # groundedness has le=1.0; constrained decoding does not enforce
        # numeric bounds, so a live judge can emit 1.5.
        scenario = self._with_llm_queue(
            "briefing_judge",
            [{"groundedness": 1.5, "actionability": 0.5, "reasoning": "r"}],
        )
        result = run_scenario(scenario, _test_settings())
        assert result.outcome.report.passed is True
        assert result.outcome.judge_score is None
        assert result.outcome.judge_error is not None
        assert "judge call failed" in result.outcome.judge_error


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

    def test_one_crashing_scenario_does_not_abort_batch(self, monkeypatch: Any) -> None:
        """Regression: earlier `run_all` propagated the first scenario's exception,
        wiping every scenario that hadn't run yet. Live-eval batches now survive
        a per-scenario crash by capturing it as a failed outcome and continuing.
        """
        from evals import runner as runner_module

        real_run_scenario = runner_module.run_scenario
        crashed_names: list[str] = []

        def flaky_run_scenario(scenario: Scenario, *args: Any, **kwargs: Any) -> Any:
            if scenario.name == "consumer_lag_pass":
                # Second scenario in the batch — simulate the platform 500ing.
                crashed_names.append(scenario.name)
                raise RuntimeError("simulated platform outage")
            return real_run_scenario(scenario, *args, **kwargs)

        monkeypatch.setattr(runner_module, "run_scenario", flaky_run_scenario)
        report, trajectories, briefings = runner_module.run_all(
            [_noise_scenario(), _passing_scenario(), _bad_expectation_scenario()],
            _test_settings(),
        )
        assert report.total == 3
        assert crashed_names == ["consumer_lag_pass"]
        # The crashed scenario is captured as a failed outcome, not dropped.
        outcomes_by_name = {o.scenario: o for o in report.outcomes}
        assert outcomes_by_name["consumer_lag_pass"].report.passed is False
        assert "simulated platform outage" in (
            outcomes_by_name["consumer_lag_pass"].report.dimensions[0].detail
        )
        # The other two scenarios ran normally.
        assert outcomes_by_name["noise_alert"].final_state == IncidentState.ESCALATED
        assert outcomes_by_name["misexpected"].report.passed is False
        assert len(trajectories) == 3
        assert len(briefings) == 3

    def test_empty_scenario_list(self) -> None:
        report, trajectories, _ = run_all([], _test_settings())
        assert report.total == 0
        assert trajectories == ()

    def test_shipped_scenarios_pass(self) -> None:
        from evals.scenarios.loader import load_scenarios

        scenarios = load_scenarios(Path(__file__).resolve().parents[2] / "evals" / "scenarios")
        # Every shipped scenario has canned fallback data, so all of them
        # run — and pass — in offline mode.
        report, _, _ = run_all(scenarios, _test_settings())
        assert report.failed == 0
        assert report.total >= 10  # taxonomy expansion floor


class TestLiveMcpDispatch:
    def _live_scenario_with_canned_fallback(self) -> Scenario:
        # A live-mcp scenario that also ships canned data — this is the
        # required shape so offline `make eval` stays deterministic.
        base = _passing_scenario()
        return base.model_copy(update={"name": "live_probe", "use_live_mcp": True})

    def test_run_scenario_falls_back_to_canned_when_offline_placeholder(self) -> None:
        # Placeholder MCP URL => canned client is used, scenario still runs.
        result = run_scenario(self._live_scenario_with_canned_fallback(), _test_settings())
        assert result.outcome.final_state is IncidentState.ESCALATED
        assert result.outcome.report.passed is True

    def test_run_all_runs_every_scenario_even_when_offline(self) -> None:
        live = self._live_scenario_with_canned_fallback()
        canned = _passing_scenario()
        report, _, _ = run_all([live, canned], _test_settings())
        assert report.total == 2
        assert {o.scenario for o in report.outcomes} == {"live_probe", "consumer_lag_pass"}

    def test_canned_scenario_ignores_real_platform_url(self) -> None:
        # A canned scenario must still run cleanly even when the URL isn't a
        # placeholder — the URL only matters for use_live_mcp scenarios.
        settings = _test_settings(platform_mcp_url="http://real.host:8001/mcp")
        report, _, _ = run_all([_passing_scenario()], settings)
        assert report.total == 1
        assert report.passed == 1


class TestChaosSetupHook:
    """Scenarios declare their own chaos setup; runner fires it only in live mode."""

    def _scenario_with_chaos(self) -> Scenario:
        base = _passing_scenario()
        return base.model_copy(
            update={
                "name": "chaos_probe",
                "use_live_mcp": True,
                "chaos_setup": ChaosHook(
                    name="inject_latency",
                    arguments={"consumer_group": "wd", "latency_ms": 2000},
                ),
            }
        )

    def test_chaos_setup_not_invoked_in_canned_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Offline placeholder URL → falls back to canned; hook must not fire.
        calls: list[tuple[str, dict[str, Any]]] = []

        def _fake_invoke(
            url: str, token: str, name: str, arguments: dict[str, Any]
        ) -> dict[str, Any]:
            calls.append((name, arguments))
            return {}

        monkeypatch.setattr("evals.runner.invoke_chaos_hook", _fake_invoke)
        result = run_scenario(self._scenario_with_chaos(), _test_settings())
        assert result.outcome.final_state is IncidentState.ESCALATED
        assert calls == []

    def test_chaos_setup_invoked_before_run_in_live_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[str, dict[str, Any]]] = []

        def _fake_invoke(
            url: str, token: str, name: str, arguments: dict[str, Any]
        ) -> dict[str, Any]:
            calls.append((name, arguments))
            return {"seeded": True}

        # We can't actually reach a live platform here, so we also monkeypatch
        # the MCP client factory to return a fake with a no-op close() (the
        # runner treats live_mcp_client as an MCPClient and calls .close() in
        # its finally block).
        class _ClosableCannedMCP(CannedMCPClient):
            def close(self) -> None:  # pragma: no cover - no-op
                return None

        monkeypatch.setattr("evals.runner.invoke_chaos_hook", _fake_invoke)
        monkeypatch.setattr(
            "evals.runner.make_client",
            lambda *_a, **_kw: _ClosableCanned(self._scenario_with_chaos().canned_tool_responses),
        )
        settings = _test_settings(platform_mcp_url="http://real.host:8001/mcp")
        result = run_scenario(self._scenario_with_chaos(), settings)
        assert result.outcome.final_state is IncidentState.ESCALATED
        assert calls == [("inject_latency", {"consumer_group": "wd", "latency_ms": 2000})]

    def test_chaos_setup_failure_surfaces_as_run_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _failing_invoke(*_a: Any, **_kw: Any) -> dict[str, Any]:
            raise ChaosInvocationError("platform said no")

        monkeypatch.setattr("evals.runner.invoke_chaos_hook", _failing_invoke)
        settings = _test_settings(platform_mcp_url="http://real.host:8001/mcp")
        with pytest.raises(RuntimeError, match="chaos_probe.*inject_latency.*platform said no"):
            run_scenario(self._scenario_with_chaos(), settings)


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


class TestFailureClassification:
    """failure_class: the five-bucket taxonomy as a report column (issue #59)."""

    _NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)

    def _report(self, failing: set[GradeDimension]) -> GradeReport:
        dims = tuple(
            DimensionResult(dimension=d, passed=d not in failing, detail="x")
            for d in GradeDimension
        )
        return GradeReport(scenario="s", passed=not failing, dimensions=dims)

    def _final(self, *entries: tuple[str, str]) -> RunState:
        evidence = tuple(
            EvidenceEntry(tool_name=t, arguments={}, result_summary=s, timestamp=self._NOW)
            for t, s in entries
        )
        return RunState(
            incident_id=UUID("11111111-1111-1111-1111-111111111111"),
            state=IncidentState.ESCALATED,
            alert={"source": "platform.kafka", "severity": "high"},
            budget=BudgetLedger(
                max_tool_calls=25,
                max_tokens=200_000,
                max_wall_seconds=600,
                max_usd=Decimal("1.00"),
            ),
            evidence=evidence,
            created_at=self._NOW,
            updated_at=self._NOW,
        )

    def test_passed(self) -> None:
        assert _classify_failure(self._report(set()), self._final()) == "passed"

    def test_budget_only_is_llm_variance(self) -> None:
        got = _classify_failure(self._report({GradeDimension.BUDGET}), self._final())
        assert got == "llm-variance"

    def test_evidence_only_is_grader_brittleness(self) -> None:
        got = _classify_failure(self._report({GradeDimension.EVIDENCE}), self._final())
        assert got == "grader-brittleness"

    def test_tool_is_error_is_shared_env(self) -> None:
        final = self._final(("get_redis_health", "tool reported is_error=True (get_redis_health)"))
        got = _classify_failure(self._report({GradeDimension.OUTCOME}), final)
        assert got == "shared-env"

    def test_real_mcp_transport_summary_is_transport(self) -> None:
        # Summary built exactly as investigation.py's tool-error escalation
        # builds it — from str() of a real MCPError, which renders
        # "MCP error <code>: <message>" and never the class name (A-07).
        err = MCPError(-32000, "connection reset by peer")
        final = self._final(("get_consumer_lag", f"tool error (get_consumer_lag): {err}"))
        got = _classify_failure(self._report({GradeDimension.OUTCOME}), final)
        assert got == "transport"

    def test_transport_beats_shared_env(self) -> None:
        # Priority order is deliberate: transport before shared-env, per the
        # debugging discipline in docs/lessons/live-eval-noise-sources.md.
        err = MCPError(-32000, "connection reset by peer")
        final = self._final(
            ("get_consumer_lag", f"tool error (get_consumer_lag): {err}"),
            ("get_redis_health", "tool reported is_error=True (get_redis_health)"),
        )
        got = _classify_failure(self._report({GradeDimension.OUTCOME}), final)
        assert got == "transport"

    def test_not_verified_with_correct_action_is_eventual_consistency(self) -> None:
        final = self._final(
            ("pause_dag", '{"accepted": true}'),
            ("_verify_judge", "not_verified: nodes still waiting"),
        )
        got = _classify_failure(self._report({GradeDimension.OUTCOME}), final)
        assert got == "eventual-consistency"

    def test_wrong_action_outcome_fail_is_unclassified(self) -> None:
        # ACTION failed + OUTCOME failed with no environment/consistency
        # signature: the conservative bucket is "look at the trace".
        got = _classify_failure(
            self._report({GradeDimension.OUTCOME, GradeDimension.ACTION}), self._final()
        )
        assert got == "unclassified"

    def test_crashed_result_is_transport_or_shared_env(self) -> None:
        scenario = _passing_scenario()
        transport = _crashed_result(scenario, RuntimeError("boom"))
        assert transport.outcome.failure_class == "transport"
        env = _crashed_result(
            scenario, RuntimeError("scenario 'x' chaos_setup 'create_stale_cache' failed")
        )
        assert env.outcome.failure_class == "shared-env"


class TestCannedSequencing:
    def test_sequence_consumed_in_order_and_last_repeats(self) -> None:
        a = ToolResult(content=[{"type": "text", "text": '{"paused": false}'}])
        b = ToolResult(content=[{"type": "text", "text": '{"paused": true}'}])
        client = CannedMCPClient({"get_dag_state": (a, b)})
        first = client.call_tool("get_dag_state", {})
        second = client.call_tool("get_dag_state", {})
        third = client.call_tool("get_dag_state", {})
        assert first is a
        assert second is b
        assert third is b  # last repeats: verify polling must not crash

    def test_single_response_served_forever(self) -> None:
        a = ToolResult(content=[{"type": "text", "text": "{}"}])
        client = CannedMCPClient({"get_consumer_lag": a})
        assert client.call_tool("get_consumer_lag", {}) is a
        assert client.call_tool("get_consumer_lag", {}) is a


class TestRunArchiveIsAppendOnly:
    """CLAUDE.md invariant 9 for the runner's own artifacts.

    ``write_trajectories`` keyed files on scenario name alone and used
    ``write_text``, so any later run — including a free offline
    ``make eval`` — overwrote the previous one. Run 001's paid live
    trajectories were destroyed exactly this way while the trace-truncation
    fix was being developed (study/findings.md F-003).
    """

    def _report(self, scenario: str) -> RunReport:
        return RunReport(
            generated_at=_NOW,
            total=1,
            passed=1,
            failed=0,
            outcomes=(
                ScenarioOutcome(
                    scenario=scenario,
                    final_state=IncidentState.ESCALATED,
                    tool_calls_used=1,
                    report=GradeReport(scenario=scenario, passed=True, dimensions=()),
                ),
            ),
        )

    def _bits(self, scenario: str) -> tuple[Trajectory, EscalationBriefing]:
        return (
            Trajectory(scenario=scenario, incident_id="i", checkpoints=()),
            EscalationBriefing(
                incident_id="i", final_state=IncidentState.ESCALATED, alert_summary="a"
            ),
        )

    def test_two_invocations_both_survive(self, tmp_path: Path) -> None:
        traj, brief = self._bits("s")
        first = archive_run("inv_one", self._report("s"), [traj], [brief], ["s"], tmp_path)
        second = archive_run("inv_two", self._report("s"), [traj], [brief], ["s"], tmp_path)

        assert first != second
        for target in (first, second):
            assert (target / "report.json").exists()
            assert (target / "trajectories" / "s.json").exists()
            assert (target / "briefings" / "s.json").exists()

    def test_reusing_an_invocation_id_fails_loudly(self, tmp_path: Path) -> None:
        # Exclusive-create is the load-bearing half: a future refactor that
        # routes two runs at one directory must crash, not silently delete.
        traj, brief = self._bits("s")
        archive_run("dupe", self._report("s"), [traj], [brief], ["s"], tmp_path)
        with pytest.raises(FileExistsError):
            archive_run("dupe", self._report("s"), [traj], [brief], ["s"], tmp_path)

    def test_archive_survives_a_later_flat_write(self, tmp_path: Path) -> None:
        # The exact Run 001 loss: a second run refreshes the flat pointer
        # while the first run's evidence stays intact in the archive.
        runs, flat = tmp_path / "runs", tmp_path / "trajectories"
        first_traj = Trajectory(scenario="s", incident_id="first", checkpoints=())
        archive_run("inv_one", self._report("s"), [first_traj], [], [], runs)
        write_trajectories([first_traj], flat)
        write_trajectories([Trajectory(scenario="s", incident_id="second", checkpoints=())], flat)

        assert json.loads((flat / "s.json").read_text())["incident_id"] == "second"
        archived = json.loads((runs / "inv_one" / "trajectories" / "s.json").read_text())
        assert archived["incident_id"] == "first", "archive must not follow the pointer"


def _finished_result(scenario: str) -> ScenarioResult:
    """A plausible ScenarioResult, as run_scenario would return it."""
    return ScenarioResult(
        outcome=ScenarioOutcome(
            scenario=scenario,
            final_state=IncidentState.ESCALATED,
            tool_calls_used=1,
            report=GradeReport(scenario=scenario, passed=True, dimensions=()),
        ),
        trajectory=Trajectory(scenario=scenario, incident_id="i", checkpoints=()),
        briefing=EscalationBriefing(
            incident_id="i", final_state=IncidentState.ESCALATED, alert_summary="a"
        ),
    )


def _stub_report(scenario: str) -> RunReport:
    return RunReport(
        generated_at=_NOW,
        total=1,
        passed=1,
        failed=0,
        outcomes=(_finished_result(scenario).outcome,),
    )


class TestIncrementalArchive:
    """The archive is written per scenario, and report.json marks completion.

    Until 2026-08-09 every artifact except the JSONL traces lived only in
    memory until the whole suite finished: a Ctrl-C at scenario 30 of 37 threw
    away 30 scenarios of paid live evidence, because the single bulk
    ``archive_run`` call came after the loop (S-05/A-14). The archive also
    omitted the invocation's traces (S-07) and wrote report.json FIRST, so a
    half-written archive was indistinguishable from a complete one (S-08).
    """

    def test_killed_suite_keeps_completed_scenarios(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The assertion that would have caught S-05/A-14: scenario 1 finishes,
        # scenario 2 takes a Ctrl-C. run_all catches Exception only, so the
        # KeyboardInterrupt escapes the suite by design — and scenario 1's
        # evidence must already be on disk when it does.
        target = tmp_path / "runs" / "inv"
        (target / "trajectories").mkdir(parents=True)
        (target / "briefings").mkdir(parents=True)

        def _stub_run_scenario(scenario: Scenario, *_args: Any, **_kwargs: Any) -> ScenarioResult:
            if scenario.name == "noise_alert":
                raise KeyboardInterrupt
            return _finished_result(scenario.name)

        monkeypatch.setattr(runner_module, "run_scenario", _stub_run_scenario)
        with pytest.raises(KeyboardInterrupt):
            runner_module.run_all(
                [_passing_scenario(), _noise_scenario()],
                _test_settings(),
                invocation_id="inv",
                on_result=lambda result: runner_module.archive_scenario(
                    target, result, invocation_id="inv", trace_dir=None
                ),
            )

        assert (target / "trajectories" / "consumer_lag_pass.json").exists()
        assert (target / "briefings" / "consumer_lag_pass.json").exists()
        # No completion marker: the directory self-describes as a killed run.
        assert not (target / "report.json").exists()

    def test_crashed_scenarios_are_archived_too(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The _crashed_result path streams as well — a scenario the suite
        # survived is still evidence of what the run paid for.
        target = tmp_path / "runs" / "inv"
        (target / "trajectories").mkdir(parents=True)
        (target / "briefings").mkdir(parents=True)

        def _boom(_scenario: Scenario, *_args: Any, **_kwargs: Any) -> ScenarioResult:
            raise RuntimeError("simulated platform outage")

        monkeypatch.setattr(runner_module, "run_scenario", _boom)
        runner_module.run_all(
            [_passing_scenario()],
            _test_settings(),
            invocation_id="inv",
            on_result=lambda result: runner_module.archive_scenario(
                target, result, invocation_id="inv", trace_dir=None
            ),
        )

        archived = json.loads((target / "briefings" / "consumer_lag_pass.json").read_text())
        assert "simulated platform outage" in archived["alert_summary"]

    def test_trace_slice_contains_only_this_invocation(self, tmp_path: Path) -> None:
        # Catches S-07: the archive omitted traces entirely, so the join from
        # an archived run to its prompts/responses ran through the flat
        # (gitignored, multi-vintage) trace file and did not survive it.
        trace_dir = tmp_path / "traces"
        trace_dir.mkdir()
        flat = trace_dir / "consumer_lag_pass.jsonl"
        original = (
            json.dumps({"kind": "llm", "invocation_id": "old", "seq": 0})
            + "\n"
            + json.dumps({"kind": "llm", "invocation_id": "new", "seq": 1})
            + "\n"
            + "{ not json at all\n"
            # No invocation_id: pre-invocation-id vintage, excluded by the same
            # convention scripts/estimate_cost.py uses.
            + json.dumps({"kind": "mcp", "seq": 2})
            + "\n"
            + json.dumps({"kind": "mcp", "invocation_id": "new", "seq": 3})
            + "\n"
        )
        flat.write_text(original)
        target = tmp_path / "runs" / "inv"
        (target / "trajectories").mkdir(parents=True)
        (target / "briefings").mkdir(parents=True)

        runner_module.archive_scenario(
            target,
            _finished_result("consumer_lag_pass"),
            invocation_id="new",
            trace_dir=trace_dir,
        )

        sliced = (target / "traces" / "consumer_lag_pass.jsonl").read_text().splitlines()
        records = [json.loads(line) for line in sliced]
        assert [r["seq"] for r in records] == [1, 3]
        assert all(r["invocation_id"] == "new" for r in records)
        # The flat file is read-only input — it is the only record of a
        # scenario killed mid-flight, before its archive_scenario call fires.
        assert flat.read_text() == original

    def test_trace_slice_is_skipped_when_the_scenario_was_not_traced(self, tmp_path: Path) -> None:
        trace_dir = tmp_path / "traces"
        trace_dir.mkdir()
        target = tmp_path / "runs" / "inv"
        (target / "trajectories").mkdir(parents=True)
        (target / "briefings").mkdir(parents=True)

        runner_module.archive_scenario(
            target, _finished_result("consumer_lag_pass"), invocation_id="new", trace_dir=trace_dir
        )

        assert not (target / "traces" / "consumer_lag_pass.jsonl").exists()

    def test_archiving_one_scenario_twice_fails_loudly(self, tmp_path: Path) -> None:
        # Exclusive-create per scenario, same discipline as archive_run: a
        # second write at a path that already holds evidence must crash.
        target = tmp_path / "runs" / "inv"
        (target / "trajectories").mkdir(parents=True)
        (target / "briefings").mkdir(parents=True)
        result = _finished_result("consumer_lag_pass")
        runner_module.archive_scenario(target, result, invocation_id="inv", trace_dir=None)
        with pytest.raises(FileExistsError):
            runner_module.archive_scenario(target, result, invocation_id="inv", trace_dir=None)

    def test_report_json_is_written_last_and_marks_completion(self, tmp_path: Path) -> None:
        # Catches S-08: report.json used to be the FIRST archive write, so a
        # crash mid-archive left a directory that looked complete. It is now
        # the last, and its absence is the "this run was killed" signal.
        target = tmp_path / "runs" / "inv"
        (target / "trajectories").mkdir(parents=True)
        (target / "briefings").mkdir(parents=True)
        runner_module.archive_scenario(
            target, _finished_result("consumer_lag_pass"), invocation_id="inv", trace_dir=None
        )
        assert (target / "trajectories" / "consumer_lag_pass.json").exists()
        assert not (target / "report.json").exists(), "no marker until the suite finishes"

        runner_module.finalize_archive(target, _stub_report("consumer_lag_pass"))
        assert (target / "report.json").exists()
        # The marker is exclusive-create too: it can be written once, ever.
        with pytest.raises(FileExistsError):
            runner_module.finalize_archive(target, _stub_report("consumer_lag_pass"))

    def test_main_streams_each_scenario_then_writes_the_marker(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # End-to-end wiring, in process: main() must create the run directory
        # BEFORE run_all and hand it a streaming callback, so evidence lands
        # while the suite is still running. run_all and the flat-pointer
        # writers are stubbed — nothing touches the real evals/ tree.
        _isolate_settings_env(monkeypatch, tmp_path)
        runs = tmp_path / "runs"
        monkeypatch.setattr(runner_module, "_RUNS_DIR", runs)
        targets: list[Path] = []

        def _stub_run_all(
            _scenarios: Any, _settings: Any, **kwargs: Any
        ) -> tuple[RunReport, tuple[Trajectory, ...], tuple[EscalationBriefing, ...]]:
            target = runs / str(kwargs["invocation_id"])
            targets.append(target)
            kwargs["on_result"](_finished_result("consumer_lag_pass"))
            # Mid-suite state: the finished scenario is durable, the run is
            # still marked incomplete.
            assert (target / "trajectories" / "consumer_lag_pass.json").exists()
            assert not (target / "report.json").exists()
            return _stub_report("consumer_lag_pass"), (), ()

        monkeypatch.setattr(runner_module, "run_all", _stub_run_all)
        monkeypatch.setattr(runner_module, "write_report", lambda *_a, **_kw: None)
        monkeypatch.setattr(runner_module, "write_trajectories", lambda *_a, **_kw: None)
        monkeypatch.setattr(runner_module, "write_briefings", lambda *_a, **_kw: None)
        monkeypatch.setattr(sys, "argv", ["evals.runner"])

        assert runner_module.main() == 0
        assert (targets[0] / "report.json").exists()


class TestRunsDirIsTracked:
    """CLAUDE.md invariant 9: evals/runs/ must be commit-able (S-06).

    The append-only archive is the durable record, yet .gitignore carried an
    ``evals/runs/*`` entry, so no archive was ever tracked and routine git
    hygiene (a clean or a fresh clone) erased every one. This is the assertion
    that would have caught it: the durable record's directory pattern must
    never sit in .gitignore. The flat-pointer ignores (trajectories,
    briefings, traces, latest.json, reports/human) are refreshable by design
    and stay ignored.
    """

    def test_gitignore_does_not_ignore_the_durable_record(self) -> None:
        gitignore = Path(__file__).resolve().parents[2] / ".gitignore"
        text = gitignore.read_text(encoding="utf-8")
        assert not any(line.strip().startswith("evals/runs") for line in text.splitlines()), (
            "evals/runs is the invariant-9 durable record; it must not be gitignored"
        )


# --- Run provenance + exit-code contract (ADR 0013; findings A-01/S-09/A-04/A-15) ---

# Every env var Settings can read (config.py), upper-cased per pydantic-settings.
# Exit-code tests must clear ALL of them and chdir away from any real .env, or a
# developer's environment leaks into the test — the exact A-04 mechanism.
_SETTINGS_ENV_VARS = (
    "ANTHROPIC_API_KEY",
    "AGENT_MODEL",
    "JUDGE_MODEL",
    "PLATFORM_MCP_URL",
    "PLATFORM_REST_URL",
    "PLATFORM_TOKEN",
    "PLATFORM_SMOKE_TOKEN",
    "PLATFORM_WEBHOOK_SECRET",
    "DATABASE_URL",
    "BUDGET_MAX_TOOL_CALLS",
    "BUDGET_MAX_TOKENS",
    "BUDGET_MAX_SECONDS",
    "BUDGET_MAX_USD",
    "VERIFY_PROBE_ATTEMPTS",
    "VERIFY_PROBE_DELAY_SECONDS",
    "INVESTIGATE_REPROBE_ATTEMPTS",
    "INVESTIGATE_REPROBE_DELAY_SECONDS",
    "ACTION_TOOL_TIMEOUT_SECONDS",
    "PLATFORM_AGENT_PRINCIPAL_ID",
    "PLATFORM_SMOKE_PRINCIPAL_ID",
)

# A present-but-offline env for a --live run: every field set and parseable,
# ANTHROPIC_API_KEY an offline placeholder, platform URL the offline
# placeholder. (A verbatim `.env.example` copy no longer parses this way:
# env_ignore_empty treats its blank required entries as unset, so that copy
# now dies as 'Field required' at construction — the broken-env exit-3 path.)
_PLACEHOLDER_LIVE_ENV = {
    "ANTHROPIC_API_KEY": "eval",
    "JUDGE_MODEL": "eval-judge",
    "PLATFORM_MCP_URL": "https://eval.local",
    "PLATFORM_REST_URL": "https://eval.local",
    "PLATFORM_TOKEN": "eval",
    "PLATFORM_WEBHOOK_SECRET": "eval",
    "DATABASE_URL": "postgresql://eval:eval@localhost:5432/eval",
}

# The `make eval-smoke` shape: a fully real-looking env (nothing offline).
# No network is ever touched — every live-side collaborator is monkeypatched.
_REAL_LOOKING_LIVE_ENV = {
    "ANTHROPIC_API_KEY": "sk-ant-test-not-a-real-key",
    "JUDGE_MODEL": "eval-judge",
    "PLATFORM_MCP_URL": "http://real.host:8001/mcp",
    "PLATFORM_REST_URL": "http://real.host:8000",
    "PLATFORM_TOKEN": "sa_full_scope",
    "PLATFORM_SMOKE_TOKEN": "sa_smoke_read_only",
    "PLATFORM_WEBHOOK_SECRET": "whsec_test",
    "DATABASE_URL": "postgresql://eval:eval@localhost:5432/eval",
}


# A smoke pass selects chaos-free scenarios, exactly as `make eval-smoke`
# does (`--only "$(SMOKE_ONLY)"`, Makefile:105-117 — the three remediate_*
# scenarios are deliberately absent from that list). An unfiltered
# `--live --smoke` selects all 37 and is refused with exit 6: it would seed
# chaos under the full write+chaos principal during the read-only stage
# (S-03). One representative pattern stands in for the full SMOKE_ONLY list.
_SMOKE_ONLY_ARGS = ["--only", "consumer_lag_healthy,tool_"]


def _isolate_settings_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    env: dict[str, str] | None = None,
) -> None:
    """chdir to tmp_path (no .env) and reset every Settings env var."""
    monkeypatch.chdir(tmp_path)
    for var in _SETTINGS_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    for var, value in (env or {}).items():
        monkeypatch.setenv(var, value)


def _forbid_run_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exit-code tests exercise main()'s pre-run paths only: reaching run_all
    would write into the append-only evidence dirs at module-constant paths."""

    def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("run_all must not be reached")

    monkeypatch.setattr(runner_module, "run_all", _boom)


def _green_stub_report() -> RunReport:
    return RunReport(
        generated_at=_NOW,
        total=1,
        passed=1,
        failed=0,
        outcomes=(
            ScenarioOutcome(
                scenario="stub",
                final_state=IncidentState.ESCALATED,
                tool_calls_used=0,
                report=GradeReport(scenario="stub", passed=True, dimensions=()),
            ),
        ),
    )


def _stub_run_pipeline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[dict[str, Any]]:
    """Stub run_all and the flat writers so main() can cross the run boundary
    without touching evals/{reports,trajectories,briefings}, and redirect the
    run archive at tmp_path — main() now creates runs/<invocation_id>/ itself
    and writes its completion marker there."""
    run_all_calls: list[dict[str, Any]] = []

    def _stub_run_all(*args: Any, **kwargs: Any) -> Any:
        run_all_calls.append({"args": args, "kwargs": kwargs})
        return _green_stub_report(), (), ()

    monkeypatch.setattr(runner_module, "_RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(runner_module, "run_all", _stub_run_all)
    monkeypatch.setattr(runner_module, "write_report", lambda *_a, **_kw: None)
    monkeypatch.setattr(runner_module, "write_trajectories", lambda *_a, **_kw: None)
    monkeypatch.setattr(runner_module, "write_briefings", lambda *_a, **_kw: None)
    return run_all_calls


class _StubGuardClient:
    def close(self) -> None:
        return None


class TestRunProvenance:
    """A-01/S-09: degradation must live in the artifact, not just stdout."""

    def _live_llm_scenario(self) -> Scenario:
        return _passing_scenario().model_copy(
            update={"name": "live_llm_probe", "use_live_llm": True}
        )

    def test_declared_live_llm_scenario_records_degraded_when_placeholder(self) -> None:
        # Placeholder API key => the declared-live LLM leg runs canned, and
        # the outcome must say so (at HEAD ScenarioOutcome has no such field).
        result = run_scenario(self._live_llm_scenario(), _test_settings())
        assert result.outcome.live_llm is False
        assert result.outcome.live_mcp is False
        assert result.outcome.degraded is True

    def test_fully_canned_scenario_is_not_degraded(self) -> None:
        # A scenario that declares no live leg cannot "degrade" — canned is
        # its intended mode.
        result = run_scenario(_passing_scenario(), _test_settings())
        assert result.outcome.live_llm is False
        assert result.outcome.live_mcp is False
        assert result.outcome.degraded is False

    def test_run_all_persists_degraded_count_and_only_patterns(self) -> None:
        report, _, _ = run_all(
            [self._live_llm_scenario(), _passing_scenario()],
            _test_settings(),
            only_patterns=("lag",),
        )
        assert report.degraded_count == 1
        assert report.only_patterns == ("lag",)

    def test_run_all_only_patterns_defaults_to_empty(self) -> None:
        report, _, _ = run_all([_passing_scenario()], _test_settings())
        assert report.degraded_count == 0
        assert report.only_patterns == ()

    def test_print_summary_reads_the_persisted_degraded_count(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The printed number and the persisted number must be the same value
        # by construction — their divergence was the A-01 finding.
        report, _, _ = run_all([self._live_llm_scenario()], _test_settings())
        _print_summary(report)
        out = capsys.readouterr().out
        assert "degraded: 1 scenarios fell back to canned" in out


class TestOfflinePlaceholderExactHost:
    """S-09 sub-finding: substring matching silently degraded real URLs."""

    def test_placeholder_host_matches(self) -> None:
        assert _is_offline_placeholder("https://eval.local") is True

    def test_placeholder_host_with_port_and_path_matches(self) -> None:
        assert _is_offline_placeholder("http://eval.local:8001/mcp") is True

    def test_placeholder_prefix_of_real_domain_counts_as_live(self) -> None:
        assert _is_offline_placeholder("https://eval.local.evil.example/mcp") is False


class TestEvalDefaultsPinned:
    """A-04: offline placeholder Settings must not absorb .env values."""

    def test_platform_smoke_token_ignores_dotenv(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _isolate_settings_env(monkeypatch, tmp_path)
        (tmp_path / ".env").write_text("PLATFORM_SMOKE_TOKEN=sa_smoke_from_dotenv\n")
        assert _eval_defaults().platform_smoke_token is None


class TestBaselineBackwardCompat:
    """The committed pre-schema baseline must keep parsing — append-only evidence
    is never rewritten; defaults carry the compatibility."""

    def test_committed_baseline_parses_with_unknown_provenance(self) -> None:
        baseline_path = Path(__file__).resolve().parents[2] / "evals" / "reports" / "baseline.json"
        report = RunReport.model_validate_json(baseline_path.read_text())
        assert report.total == 37
        # None = unknown (pre-schema), NOT 0: the committed baseline was a
        # 32/37-degraded canned run — claiming 0 would assert a falsehood.
        assert report.degraded_count is None
        assert report.only_patterns == ()
        assert all(o.degraded is False for o in report.outcomes)


class TestMainExitCodes:
    """The new preflight refusals all exit 3 BEFORE run_all — pre-spend."""

    def test_live_with_placeholder_env_refuses_exit_3(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The executed S-09 repro: present-but-placeholder values under
        # --live. At HEAD this ran the whole suite canned and returned 0.
        # (The verbatim-.env.example-copy variant of the repro now exits 3
        # even earlier — 'Field required' at construction, same path as
        # test_live_with_broken_env_exits_3_not_traceback.)
        _isolate_settings_env(monkeypatch, tmp_path, _PLACEHOLDER_LIVE_ENV)
        _forbid_run_all(monkeypatch)
        # --only is load-bearing since ADR 0020: a full-suite --live selection is
        # refused (exit 7) before the environment is examined, because a wrong
        # SELECTION is knowable without touching the env. Narrow to one read-only
        # scenario so this test still reaches the env path it is about.
        monkeypatch.setattr(
            sys, "argv", ["evals.runner", "--live", "--only", "consumer_lag_healthy_zero"]
        )
        assert runner_module.main() == 3

    def test_smoke_without_live_refuses_exit_3_before_settings(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # A-04: at HEAD this path runs the full suite canned (with .env-leaked
        # smoke token) and returns 0. Must refuse before Settings is built.
        _isolate_settings_env(monkeypatch, tmp_path)

        def _no_settings(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("_settings_for_mode must not be reached")

        monkeypatch.setattr(runner_module, "_settings_for_mode", _no_settings)
        _forbid_run_all(monkeypatch)
        monkeypatch.setattr(sys, "argv", ["evals.runner", "--smoke"])
        assert runner_module.main() == 3

    def test_live_with_broken_env_exits_3_not_traceback(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # A-15: at HEAD the pydantic ValidationError escapes main() and the
        # interpreter exits 1 — the code reserved for "scenario failed".
        _isolate_settings_env(monkeypatch, tmp_path)
        _forbid_run_all(monkeypatch)
        monkeypatch.setattr(sys, "argv", ["evals.runner", "--live"])
        assert runner_module.main() == 3

    def test_live_smoke_placeholder_platform_with_canned_only_selection_exits_3(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # A-04 defense-in-depth: the selection contains no use_live scenario
        # (so the degraded fail-fast is silent), the smoke token is present,
        # but the platform is a placeholder — there is no principal to guard.
        env = dict(_PLACEHOLDER_LIVE_ENV)
        env["PLATFORM_SMOKE_TOKEN"] = "sa_smoke_read_only"
        _isolate_settings_env(monkeypatch, tmp_path, env)
        _forbid_run_all(monkeypatch)
        monkeypatch.setattr(sys, "argv", ["evals.runner", "--live", "--smoke", "--only", "tool_"])
        assert runner_module.main() == 3

    def test_offline_run_still_exits_0(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Plain offline `make eval` (no --live) must keep exiting 0 — CI's
        # canned evals.yml job depends on it; degradation gates live runs only.
        _isolate_settings_env(monkeypatch, tmp_path)
        run_all_calls = _stub_run_pipeline(monkeypatch, tmp_path)
        monkeypatch.setattr(sys, "argv", ["evals.runner"])
        assert runner_module.main() == 0
        assert len(run_all_calls) == 1

    def test_live_smoke_with_real_env_passes_preflight(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The `make eval-smoke` shape (--live --smoke, nothing offline) must
        # NOT trip the degraded fail-fast: degraded_to_canned == 0. Unit-level
        # replacement for observing make eval-smoke under the eval freeze
        # (ADR 0011). Every live collaborator is stubbed — no network.
        _isolate_settings_env(monkeypatch, tmp_path, _REAL_LOOKING_LIVE_ENV)
        run_all_calls = _stub_run_pipeline(monkeypatch, tmp_path)
        preflight_calls: list[str] = []
        guard_calls: list[str] = []
        monkeypatch.setattr(
            runner_module, "preflight_auth", lambda key: preflight_calls.append(key)
        )
        monkeypatch.setattr(runner_module, "make_client", lambda *_a, **_kw: _StubGuardClient())
        monkeypatch.setattr(
            runner_module,
            "assert_read_only_principal",
            lambda _client: guard_calls.append("principal"),
        )
        audit_kwargs: list[dict[str, Any]] = []

        def _audit_guard(_client: Any, _since: Any, **kwargs: Any) -> None:
            guard_calls.append("audit")
            audit_kwargs.append(kwargs)

        monkeypatch.setattr(runner_module, "assert_no_tier1_successes", _audit_guard)
        monkeypatch.setattr(sys, "argv", ["evals.runner", "--live", "--smoke", *_SMOKE_ONLY_ARGS])
        assert runner_module.main() == 0
        assert preflight_calls == ["sk-ant-test-not-a-real-key"]
        assert guard_calls == ["principal", "audit"]
        assert len(run_all_calls) == 1
        # The read-scoped smoke token — not the write token — reached run_all.
        assert run_all_calls[0]["kwargs"]["mcp_token"] == "sa_smoke_read_only"
        # Neither principal id is configured in this env: the post-stage
        # audit stays deliberately over-broad (any service account's
        # in-window Tier-1 success fails the stage).
        assert audit_kwargs == [{"principal_ids": None}]

    def test_configured_principal_ids_reach_the_post_stage_audit(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # A-13: on a shared platform the guard must attribute violations to
        # the principals this stage owns. BOTH ids are required — the F-001
        # failure mode (the stage silently holding the full token) writes
        # under the AGENT principal, not the smoke one — so filtering to the
        # smoke id alone would blind the guard to its own reason for
        # existing. The ids come from Settings, printed by bootstrap-token.
        env = dict(_REAL_LOOKING_LIVE_ENV)
        env["PLATFORM_AGENT_PRINCIPAL_ID"] = "agent-sa-uuid"
        env["PLATFORM_SMOKE_PRINCIPAL_ID"] = "smoke-sa-uuid"
        _isolate_settings_env(monkeypatch, tmp_path, env)
        _stub_run_pipeline(monkeypatch, tmp_path)
        audit_kwargs: list[dict[str, Any]] = []
        monkeypatch.setattr(runner_module, "preflight_auth", lambda _key: None)
        monkeypatch.setattr(runner_module, "make_client", lambda *_a, **_kw: _StubGuardClient())
        monkeypatch.setattr(runner_module, "assert_read_only_principal", lambda _client: None)
        monkeypatch.setattr(
            runner_module,
            "assert_no_tier1_successes",
            lambda _client, _since, **kwargs: audit_kwargs.append(kwargs),
        )
        monkeypatch.setattr(sys, "argv", ["evals.runner", "--live", "--smoke", *_SMOKE_ONLY_ARGS])
        assert runner_module.main() == 0
        assert audit_kwargs == [{"principal_ids": frozenset({"agent-sa-uuid", "smoke-sa-uuid"})}]

    def test_half_configured_principal_ids_fall_back_to_unfiltered(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Naming ONLY the smoke principal would filter out the agent rows
        # the F-001 failure mode writes — the guard would stop catching the
        # thing it was built for. A half-configured env therefore falls back
        # to the over-broad default (safe side) and says so.
        env = dict(_REAL_LOOKING_LIVE_ENV)
        env["PLATFORM_SMOKE_PRINCIPAL_ID"] = "smoke-sa-uuid"
        _isolate_settings_env(monkeypatch, tmp_path, env)
        _stub_run_pipeline(monkeypatch, tmp_path)
        audit_kwargs: list[dict[str, Any]] = []
        monkeypatch.setattr(runner_module, "preflight_auth", lambda _key: None)
        monkeypatch.setattr(runner_module, "make_client", lambda *_a, **_kw: _StubGuardClient())
        monkeypatch.setattr(runner_module, "assert_read_only_principal", lambda _client: None)
        monkeypatch.setattr(
            runner_module,
            "assert_no_tier1_successes",
            lambda _client, _since, **kwargs: audit_kwargs.append(kwargs),
        )
        monkeypatch.setattr(sys, "argv", ["evals.runner", "--live", "--smoke", *_SMOKE_ONLY_ARGS])
        assert runner_module.main() == 0
        assert audit_kwargs == [{"principal_ids": None}]
        assert "only one of PLATFORM_AGENT_PRINCIPAL_ID" in capsys.readouterr().out


class TestSmokeRefusesChaosSeeding:
    """S-03: a read-only stage does not seed chaos.

    ``run_scenario`` fires ``chaos_setup`` under ``settings.platform_token``
    — the full write+chaos principal — regardless of ``--smoke``. The #80
    principal guard only asserts the AGENT client's token, and the exit-5
    post-stage audit sees the write after it lands. The only prevention is
    refusing the run before anything is spent.
    """

    def _chaos_scenario(self) -> Scenario:
        return _passing_scenario().model_copy(
            update={
                "name": "remediate_consumer_lag_success",
                "use_live_mcp": True,
                "chaos_setup": ChaosHook(
                    name="kill_consumer",
                    arguments={"consumer_group": "worker-dispatcher"},
                ),
            }
        )

    def test_smoke_with_a_chaos_scenario_exits_6_and_fires_no_hook(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # At HEAD this run proceeds: the hook fires inside run_scenario under
        # the FULL principal during the stage whose purpose is proving the
        # smoke token is read-only.
        _isolate_settings_env(monkeypatch, tmp_path, _REAL_LOOKING_LIVE_ENV)
        _forbid_run_all(monkeypatch)
        hook_calls: list[str] = []
        monkeypatch.setattr(
            runner_module,
            "invoke_chaos_hook",
            lambda _url, _token, name, _args: hook_calls.append(name),
        )
        monkeypatch.setattr(runner_module, "load_scenarios", lambda _dir: [self._chaos_scenario()])
        monkeypatch.setattr(sys, "argv", ["evals.runner", "--live", "--smoke"])
        assert runner_module.main() == 6
        assert hook_calls == []
        out = capsys.readouterr().out
        assert "SMOKE FAIL" in out
        assert "remediate_consumer_lag_success" in out

    def test_the_refusal_precedes_the_principal_guard(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Refuse before preflight/guard/spend: a stage that must not run is
        # cheaper to refuse than to guard, and the guard cannot see chaos.
        _isolate_settings_env(monkeypatch, tmp_path, _REAL_LOOKING_LIVE_ENV)
        _forbid_run_all(monkeypatch)

        def _boom(*_a: Any, **_kw: Any) -> Any:
            raise AssertionError("the principal guard must not be reached")

        monkeypatch.setattr(runner_module, "assert_read_only_principal", _boom)
        monkeypatch.setattr(runner_module, "preflight_auth", _boom)
        monkeypatch.setattr(runner_module, "invoke_chaos_hook", _boom)
        monkeypatch.setattr(runner_module, "load_scenarios", lambda _dir: [self._chaos_scenario()])
        monkeypatch.setattr(sys, "argv", ["evals.runner", "--live", "--smoke"])
        assert runner_module.main() == 6

    def test_only_filter_cannot_smuggle_a_chaos_scenario_past_the_gate(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The SMOKE_ONLY / --only path is the reachable channel S-03 names:
        # the gate runs on the SELECTED scenarios, after filtering.
        _isolate_settings_env(monkeypatch, tmp_path, _REAL_LOOKING_LIVE_ENV)
        _forbid_run_all(monkeypatch)
        monkeypatch.setattr(
            runner_module,
            "load_scenarios",
            lambda _dir: [_passing_scenario(), self._chaos_scenario()],
        )
        monkeypatch.setattr(
            sys, "argv", ["evals.runner", "--live", "--smoke", "--only", "remediate_"]
        )
        assert runner_module.main() == 6

    def test_chaos_free_smoke_selection_still_runs(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The gate is scoped to chaos: a normal smoke pass is unaffected.
        _isolate_settings_env(monkeypatch, tmp_path, _REAL_LOOKING_LIVE_ENV)
        run_all_calls = _stub_run_pipeline(monkeypatch, tmp_path)
        monkeypatch.setattr(runner_module, "preflight_auth", lambda _key: None)
        monkeypatch.setattr(runner_module, "make_client", lambda *_a, **_kw: _StubGuardClient())
        monkeypatch.setattr(runner_module, "assert_read_only_principal", lambda _client: None)
        monkeypatch.setattr(runner_module, "assert_no_tier1_successes", lambda *_a, **_kw: None)
        monkeypatch.setattr(
            runner_module,
            "load_scenarios",
            lambda _dir: [_passing_scenario().model_copy(update={"use_live_mcp": True})],
        )
        monkeypatch.setattr(sys, "argv", ["evals.runner", "--live", "--smoke"])
        assert runner_module.main() == 0
        assert len(run_all_calls) == 1

    def test_non_smoke_live_run_keeps_chaos_seeding(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The three remediate_* scenarios legitimately seed chaos on a live
        # non-smoke run; the fix is stage-gating, not removing chaos.
        _isolate_settings_env(monkeypatch, tmp_path, _REAL_LOOKING_LIVE_ENV)
        run_all_calls = _stub_run_pipeline(monkeypatch, tmp_path)
        monkeypatch.setattr(runner_module, "preflight_auth", lambda _key: None)
        monkeypatch.setattr(runner_module, "load_scenarios", lambda _dir: [self._chaos_scenario()])
        monkeypatch.setattr(sys, "argv", ["evals.runner", "--live"])
        assert runner_module.main() == 0
        assert len(run_all_calls) == 1


class TestCannedEquivalentKnobWarning:
    """S-10: a --live run whose probe knobs sit at the canned-equivalent
    defaults reproduces both documented live failure modes (ADR 0006 verify
    polling, ADR 0009 freshness re-probe). The runner warns — never exits —
    because through Settings an explicit VERIFY_PROBE_ATTEMPTS=1 is
    indistinguishable from unset, and a hard fail would ban deliberate
    single-probe live experiments with no escape hatch."""

    def test_warns_on_default_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # No knob overrides: the config.py defaults are deliberately
        # canned-equivalent (ADR 0006/0009 chose canned-default-off), so
        # default-constructed settings must warn. delenv guards against a
        # developer shell exporting the knobs into unset fields.
        monkeypatch.delenv("VERIFY_PROBE_ATTEMPTS", raising=False)
        monkeypatch.delenv("INVESTIGATE_REPROBE_ATTEMPTS", raising=False)
        msg = _canned_equivalent_knob_warning(_test_settings())
        assert msg is not None
        assert "VERIFY_PROBE_ATTEMPTS" in msg
        assert "INVESTIGATE_REPROBE_ATTEMPTS" in msg
        assert "docs/runbook.md" in msg

    def test_silent_at_live_recommended_values(self) -> None:
        settings = _test_settings(verify_probe_attempts=6, investigate_reprobe_attempts=1)
        assert _canned_equivalent_knob_warning(settings) is None

    def test_warns_when_only_reprobe_is_canned_equivalent(self) -> None:
        # `or`, not `and`: either knob at its canned-equivalent value leaves
        # one of the two documented live failure modes open.
        settings = _test_settings(verify_probe_attempts=6, investigate_reprobe_attempts=0)
        assert _canned_equivalent_knob_warning(settings) is not None

    def test_live_main_prints_warning_even_when_preflight_refuses(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Wiring: the warning fires immediately after the settings load —
        # before the degraded fail-fast — so even a refused --live run
        # surfaces it. Pre-spend: run_all stays forbidden.
        _isolate_settings_env(monkeypatch, tmp_path, _PLACEHOLDER_LIVE_ENV)
        _forbid_run_all(monkeypatch)
        # --only is load-bearing since ADR 0020: a full-suite --live selection is
        # refused (exit 7) before the environment is examined, because a wrong
        # SELECTION is knowable without touching the env. Narrow to one read-only
        # scenario so this test still reaches the env path it is about.
        monkeypatch.setattr(
            sys, "argv", ["evals.runner", "--live", "--only", "consumer_lag_healthy_zero"]
        )
        assert runner_module.main() == 3
        assert "canned-equivalent probe knobs" in capsys.readouterr().out

    def test_offline_main_never_warns(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # The knobs are live-only; a canned run forces single-probe and
        # no-reprobe by construction, so warning offline would be noise.
        _isolate_settings_env(monkeypatch, tmp_path)
        run_all_calls = _stub_run_pipeline(monkeypatch, tmp_path)
        monkeypatch.setattr(sys, "argv", ["evals.runner"])
        assert runner_module.main() == 0
        assert len(run_all_calls) == 1
        assert "canned-equivalent probe knobs" not in capsys.readouterr().out


class TestScenarioBudgetReachesTheRun:
    """ADR 0019: the declared cap is the run's ceiling, and the agent is told it.

    Two separate defects, one wire. The runtime ceiling was
    ``settings.budget_max_tool_calls`` for every scenario regardless of what
    the scenario declared, so a cap of 5 and a ceiling of 25 disagreed by
    20 calls. And ``_format_planner_context`` renders "Budget remaining:
    tool_calls=..." from that same ledger, so the investigation planner was
    told 25 in every scenario — including the ones whose entire subject is
    what the agent does when the budget is tight.
    """

    def test_ledger_is_seeded_from_the_scenario_cap(self) -> None:
        scenario = _passing_scenario()
        assert scenario.expectation.max_tool_calls == 5
        captured: list[RunState] = []
        real_start_run = factory.start_run

        def _spy(*args: Any, **kwargs: Any) -> RunState:
            run = real_start_run(*args, **kwargs)
            captured.append(run)
            return run

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(runner_module, "start_run", _spy)
            run_scenario(scenario, _test_settings(budget_max_tool_calls=25))
        assert captured, "start_run was not called"
        assert captured[0].budget.max_tool_calls == 5

    def test_planner_is_told_the_scenario_budget_not_the_fleet_default(self) -> None:
        # The defect this closes is visible only in the prompt text: the
        # planner reads "Budget remaining: tool_calls=N" and decides how many
        # probes it can afford from it.
        built: list[CannedLLMClient] = []

        class _Recording(CannedLLMClient):
            def __init__(self, payloads: Any) -> None:
                super().__init__(payloads)
                built.append(self)

        scenario = _passing_scenario()
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(runner_module, "CannedLLMClient", _Recording)
            run_scenario(scenario, _test_settings(budget_max_tool_calls=25))
        contexts = [msg for client in built for _system, msg in client.calls]
        planner_contexts = [c for c in contexts if "Budget remaining:" in c]
        assert planner_contexts, "the planner context was never rendered"
        assert "tool_calls=5" in planner_contexts[0]
        assert "tool_calls=25" not in planner_contexts[0]

    def test_a_scenario_without_a_cap_still_gets_the_setting(self) -> None:
        scenario = _noise_scenario()
        assert scenario.expectation.max_tool_calls is None
        captured: list[RunState] = []
        real_start_run = factory.start_run

        def _spy(*args: Any, **kwargs: Any) -> RunState:
            run = real_start_run(*args, **kwargs)
            captured.append(run)
            return run

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(runner_module, "start_run", _spy)
            run_scenario(scenario, _test_settings(budget_max_tool_calls=17))
        assert captured[0].budget.max_tool_calls == 17

    def test_every_shipped_scenario_still_grades_green_under_its_own_ceiling(self) -> None:
        # The suite-wide statement of the change: nine scenarios declare a cap
        # of 0, which start_run ignores (a zero ledger is born exhausted), and
        # the rest now run under the number they are graded against.
        from evals.scenarios.loader import load_scenarios

        scenarios = load_scenarios(Path(__file__).resolve().parents[2] / "evals" / "scenarios")
        report, _, _ = run_all(scenarios, _test_settings())
        failed = [o.scenario for o in report.outcomes if not o.report.passed]
        assert failed == [], f"scenarios red under their own ceiling: {failed}"


class _ClosableCanned(CannedMCPClient):
    """CannedMCPClient with the close() the live path calls."""

    def close(self) -> None:  # pragma: no cover - no-op
        return None


class TestPreconditions:
    """An unmet premise abandons the run instead of grading the agent on it.

    The distinction this draws is the one `bb1fa70abb4c` could not: a run
    that never happened says nothing about the agent, and must not be
    recorded as though it did.
    """

    @staticmethod
    def _live_scenario_with_precondition(**overrides: Any) -> Scenario:
        base = _passing_scenario()
        return base.model_copy(
            update={
                "name": "precondition_probe",
                "use_live_mcp": True,
                "expected_precondition": (
                    PreconditionProbe(
                        tool="get_consumer_lag",
                        arguments={"consumer_group": "billing"},
                        expect=(PreconditionField(path="lag", at_least=1),),
                        **overrides,
                    ),
                ),
            }
        )

    @staticmethod
    def _lag_result(lag: int | None) -> ToolResult:
        return ToolResult(
            content=[
                {
                    "type": "text",
                    "text": json.dumps(
                        {"consumer_group": "billing", "lag": lag, "cache_key": "kafka:x"}
                    ),
                }
            ]
        )

    def _run_live(
        self, monkeypatch: pytest.MonkeyPatch, scenario: Scenario, client: Any
    ) -> ScenarioResult:
        monkeypatch.setattr(runner_module, "make_client", lambda *a, **k: client)
        return run_scenario(scenario, _test_settings(platform_mcp_url="https://real.example"))

    def test_met_precondition_lets_the_run_proceed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _ClosableCanned({"get_consumer_lag": self._lag_result(4200)})
        result = self._run_live(monkeypatch, self._live_scenario_with_precondition(), client)
        assert result.outcome.report.passed
        # The probe is the harness's, not the agent's: it must not be charged
        # to the run's tool-call budget.
        assert result.outcome.tool_calls_used == 1

    def test_unmet_precondition_raises_before_the_agent_starts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _ClosableCanned({"get_consumer_lag": self._lag_result(0)})
        with pytest.raises(runner_module.PreconditionNotMet, match="never manufactured"):
            self._run_live(monkeypatch, self._live_scenario_with_precondition(), client)

    def test_no_model_call_is_made_on_an_unmet_precondition(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The cost argument, pinned. Abandoning early is the point."""
        built: list[CannedLLMClient] = []

        class _Recording(CannedLLMClient):
            def __init__(self, payloads: Any) -> None:
                super().__init__(payloads)
                built.append(self)

        monkeypatch.setattr(runner_module, "CannedLLMClient", _Recording)
        client = _ClosableCanned({"get_consumer_lag": self._lag_result(0)})
        with pytest.raises(runner_module.PreconditionNotMet):
            self._run_live(monkeypatch, self._live_scenario_with_precondition(), client)
        assert all(not c.calls for c in built), "a model was called despite a false premise"

    def test_an_unmet_precondition_is_its_own_failure_bucket(self) -> None:
        # Not "shared-env": nothing was contended. Not an agent grade either.
        result = runner_module._crashed_result(
            _passing_scenario(), runner_module.PreconditionNotMet("nope")
        )
        assert result.outcome.failure_class == "precondition"

    def test_polling_waits_for_a_fault_that_lands_late(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # kill_consumer stops the consumer at once; the lag metric trails by
        # up to the platform's 60s interval. The first look must not decide.
        slept: list[float] = []
        monkeypatch.setattr(time, "sleep", slept.append)
        client = _ClosableCanned(
            {"get_consumer_lag": [self._lag_result(0), self._lag_result(0), self._lag_result(9000)]}
        )
        scenario = self._live_scenario_with_precondition(attempts=4, delay_seconds=15.0)
        result = self._run_live(monkeypatch, scenario, client)
        assert result.outcome.report.passed
        assert slept == [15.0, 15.0]

    def test_polling_gives_up_and_reports_the_last_reading(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(time, "sleep", lambda _s: None)
        client = _ClosableCanned({"get_consumer_lag": self._lag_result(0)})
        scenario = self._live_scenario_with_precondition(attempts=3, delay_seconds=1.0)
        with pytest.raises(runner_module.PreconditionNotMet, match="3 attempt"):
            self._run_live(monkeypatch, scenario, client)

    def test_canned_runs_ignore_preconditions_entirely(self) -> None:
        # Offline the broken state is served by construction, so there is
        # nothing to establish — and an offline suite must never need a
        # platform to run.
        scenario = _passing_scenario().model_copy(
            update={
                "expected_precondition": (
                    PreconditionProbe(
                        tool="get_consumer_lag",
                        expect=(PreconditionField(path="lag", at_least=999_999),),
                    ),
                ),
            }
        )
        assert run_scenario(scenario, _test_settings()).outcome.report.passed

    def test_a_probe_that_never_answered_is_unverifiable_not_unmet(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """UNKNOWN is not FALSE, and the report must not conflate them.

        "The fault was never manufactured" is a claim about the world; making
        it requires the world to have answered. A refused scope or a dead
        platform says nothing about the fault, and reporting one as the other
        sends the reader to seeding when the platform is the problem.
        """
        client = _ClosableCanned({})  # no canned response => MCPError
        with pytest.raises(runner_module.PreconditionUnverifiable, match="UNKNOWN"):
            self._run_live(monkeypatch, self._live_scenario_with_precondition(), client)

    def test_an_unverifiable_precondition_buckets_as_transport_not_precondition(self) -> None:
        result = runner_module._crashed_result(
            _passing_scenario(), runner_module.PreconditionUnverifiable("no answer")
        )
        assert result.outcome.failure_class == "transport"

    def test_an_unreadable_probe_response_does_not_crash_the_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A bare json.loads here escaped the polling loop entirely.

        One malformed text block ended the run as an uncaught crash bucketed
        "transport", losing both the probe that failed and the fact that it
        was a precondition at all.
        """
        garbage = ToolResult(content=[{"type": "text", "text": "<html>502 Bad Gateway</html>"}])
        client = _ClosableCanned({"get_consumer_lag": garbage})
        with pytest.raises(runner_module.PreconditionUnverifiable, match="readable"):
            self._run_live(monkeypatch, self._live_scenario_with_precondition(), client)

    def test_a_world_that_answers_falsely_is_still_unmet(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The other side of the pair: the platform answered, the premise is
        # false, and that IS a claim about the world.
        client = _ClosableCanned({"get_consumer_lag": self._lag_result(0)})
        with pytest.raises(runner_module.PreconditionNotMet, match="never manufactured"):
            self._run_live(monkeypatch, self._live_scenario_with_precondition(), client)


class TestLiveRefusesABatchOfMutatingScenarios:
    """ADR 0020: one state-mutating scenario per live invocation, enforced.

    Nine remediation scenarios share ONE platform and the runner has no reset
    between them — the reset lives outside it. The seeded pool carries exactly
    one replay_safe row that two scenarios both consume, so in a single
    invocation a CORRECT agent greens one and reds the other, and the report
    blames the agent. Refusing the selection is the only place that can be
    stopped before spend.
    """

    @staticmethod
    def _mutating(name: str) -> Scenario:
        base = _passing_scenario()
        return base.model_copy(
            update={
                "name": name,
                "use_live_mcp": True,
                "expectation": base.expectation.model_copy(
                    update={"name": name, "expected_action_tools": ("restart_consumer_group",)}
                ),
            }
        )

    @staticmethod
    def _read_only(name: str) -> Scenario:
        base = _passing_scenario()
        return base.model_copy(
            update={
                "name": name,
                "use_live_mcp": True,
                "expectation": base.expectation.model_copy(update={"name": name}),
            }
        )

    def _run_main(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        scenarios: list[Scenario],
        *,
        expect_refusal: bool,
    ) -> int:
        # A fully real-looking live env: the guard must fire on the SELECTION,
        # not as a side effect of a misconfigured environment.
        _isolate_settings_env(monkeypatch, tmp_path, _REAL_LOOKING_LIVE_ENV)
        monkeypatch.setattr(runner_module, "load_scenarios", lambda _d: scenarios)
        if expect_refusal:
            # Refusal must happen BEFORE the run boundary — that is the claim.
            _forbid_run_all(monkeypatch)
        else:
            _stub_run_pipeline(monkeypatch, tmp_path)
        monkeypatch.setattr(sys, "argv", ["evals.runner", "--live"])
        return runner_module.main()

    def test_two_mutating_scenarios_are_refused_before_any_spend(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = self._run_main(
            monkeypatch,
            tmp_path,
            [self._mutating("remediate_a"), self._mutating("remediate_b")],
            expect_refusal=True,
        )
        assert code == 7
        out = capsys.readouterr().out
        assert "nothing was spent" in out
        # The message must hand over the runnable form, not just refuse.
        assert "make eval-live ONLY=remediate_a" in out
        assert "make eval-reset" in out

    def test_one_mutating_scenario_is_allowed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        code = self._run_main(
            monkeypatch, tmp_path, [self._mutating("remediate_a")], expect_refusal=False
        )
        assert code != 7

    def test_many_read_only_scenarios_are_allowed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The read-only stage runs 26 scenarios in one go and must keep doing so.
        scenarios = [self._read_only(f"read_{i}") for i in range(5)]
        assert self._run_main(monkeypatch, tmp_path, scenarios, expect_refusal=False) != 7

    def test_a_chaos_seeding_scenario_counts_as_mutating(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # It changes the world even with no expected_action_tools.
        seeding = self._read_only("seeds_chaos").model_copy(
            update={
                "chaos_setup": ChaosHook(
                    name="kill_consumer", arguments={"consumer_group": "worker-dispatcher"}
                )
            }
        )
        code = self._run_main(
            monkeypatch, tmp_path, [seeding, self._mutating("remediate_a")], expect_refusal=True
        )
        assert code == 7

    def test_offline_runs_are_untouched(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Canned scenarios share no world, so the whole suite still runs in one
        # invocation — the offline gate depends on it.
        _isolate_settings_env(monkeypatch, tmp_path, _PLACEHOLDER_LIVE_ENV)
        monkeypatch.setattr(
            runner_module,
            "load_scenarios",
            lambda _d: [self._mutating("remediate_a"), self._mutating("remediate_b")],
        )
        _stub_run_pipeline(monkeypatch, tmp_path)
        monkeypatch.setattr(sys, "argv", ["evals.runner"])
        assert runner_module.main() != 7


class TestCrashedRowsKeepTheirProvenance:
    """A crashed row must not claim it ran canned.

    live_mcp/live_llm defaulted to False on the synthesized outcome, so every
    crashed row in a live report described itself as an offline run — and
    `degraded` False alongside said that was intended. A reader counting live
    coverage counted wrong, and the report is the artifact.
    """

    def test_a_live_scenario_that_crashes_is_recorded_as_live(self) -> None:
        scenario = _passing_scenario().model_copy(
            update={"use_live_mcp": True, "use_live_llm": True}
        )
        outcome = runner_module._crashed_result(scenario, RuntimeError("boom")).outcome
        assert outcome.live_mcp is True
        assert outcome.live_llm is True

    def test_a_canned_scenario_that_crashes_is_still_recorded_as_canned(self) -> None:
        outcome = runner_module._crashed_result(_passing_scenario(), RuntimeError("boom")).outcome
        assert outcome.live_mcp is False
        assert outcome.live_llm is False


class TestLiveRemediationGuardsTheWriteScope:
    """The stage that spends money AND mutates was the one running unguarded.

    Every principal check was gated on `smoke`. A remediation stage under a
    read-scoped token does not fail fast — each scenario investigates, plans,
    attempts its action, is refused, and grades red after full spend.
    """

    @staticmethod
    def _remediation(name: str = "remediate_a") -> Scenario:
        base = _passing_scenario()
        return base.model_copy(
            update={
                "name": name,
                "use_live_mcp": True,
                "expectation": base.expectation.model_copy(
                    update={"name": name, "expected_action_tools": ("restart_consumer_group",)}
                ),
            }
        )

    def _main(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, probe: Any) -> int:
        _isolate_settings_env(monkeypatch, tmp_path, _REAL_LOOKING_LIVE_ENV)
        monkeypatch.setattr(runner_module, "load_scenarios", lambda _d: [self._remediation()])
        monkeypatch.setattr(runner_module, "make_client", lambda *a, **k: probe)
        _stub_run_pipeline(monkeypatch, tmp_path)
        monkeypatch.setattr(sys, "argv", ["evals.runner", "--live"])
        return runner_module.main()

    def test_a_read_scoped_token_is_refused_before_any_spend(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        probe = _ClosableCanned({})
        monkeypatch.setattr(
            probe,
            "call_tool",
            lambda *a, **k: (_ for _ in ()).throw(
                MCPError(-32002, "missing required scope: actions:execute")
            ),
        )
        assert self._main(monkeypatch, tmp_path, probe) == 4
        assert "nothing was spent" in capsys.readouterr().out

    def test_a_write_capable_token_proceeds(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        probe = _ClosableCanned({})
        monkeypatch.setattr(
            probe,
            "call_tool",
            lambda *a, **k: (_ for _ in ()).throw(MCPError(-32602, "invalid tool arguments")),
        )
        assert self._main(monkeypatch, tmp_path, probe) != 4

    def test_a_read_only_live_selection_is_not_guarded(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # No expected_action_tools means nothing will be executed, so there
        # is no write scope to require — and demanding one would break the
        # read-only live stage.
        _isolate_settings_env(monkeypatch, tmp_path, _REAL_LOOKING_LIVE_ENV)
        base = _passing_scenario()
        read_only = base.model_copy(update={"name": "read_only", "use_live_mcp": True})
        monkeypatch.setattr(runner_module, "load_scenarios", lambda _d: [read_only])

        def _never(*_a: Any, **_k: Any) -> Any:
            raise AssertionError("the write guard must not probe a read-only selection")

        monkeypatch.setattr(runner_module, "assert_write_capable_principal", _never)
        _stub_run_pipeline(monkeypatch, tmp_path)
        monkeypatch.setattr(sys, "argv", ["evals.runner", "--live"])
        assert runner_module.main() != 4


class TestVerificationJudgeIsPinned:
    """The verdict that decides RESOLVED must not ride the unpinned model.

    JUDGE_MODEL exists to be pinned separately from AGENT_MODEL so eval
    results stay comparable across a model-pin change. The briefing judge
    already used it; the verification judge — the one whose verdict decides
    whether an incident is RESOLVED — did not.
    """

    def test_the_verify_transition_gets_the_judge_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, Any] = {}
        real = make_llm_verify

        def _spy(*args: Any, **kwargs: Any) -> Any:
            seen.update(kwargs)
            return real(*args, **kwargs)

        monkeypatch.setattr(runner_module, "make_llm_verify", _spy)
        settings = _test_settings(agent_model="agent-model-x", judge_model="judge-model-y")
        run_scenario(_passing_scenario(), settings)
        assert seen["model"] == "judge-model-y"

    def test_the_investigation_planner_still_gets_the_agent_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Only the judgement moves. The agent's own reasoning stays on the
        # agent model, which is the thing under test.
        seen: dict[str, Any] = {}
        real = make_llm_investigate

        def _spy(*args: Any, **kwargs: Any) -> Any:
            seen.update(kwargs)
            return real(*args, **kwargs)

        monkeypatch.setattr(runner_module, "make_llm_investigate", _spy)
        settings = _test_settings(agent_model="agent-model-x", judge_model="judge-model-y")
        run_scenario(_passing_scenario(), settings)
        assert seen["model"] == "agent-model-x"
