from __future__ import annotations

import json
import sys
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
    Trajectory,
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
from evals.scenarios.schema import ChaosHook, Scenario
from incident_commander.agent.briefing import EscalationBriefing
from incident_commander.agent.state import BudgetLedger, EvidenceEntry, IncidentState, RunState
from incident_commander.api.schemas import AlertPayload
from incident_commander.config import Settings
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
            lambda *_a, **_kw: _ClosableCannedMCP(
                self._scenario_with_chaos().canned_tool_responses
            ),
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
)

# What a verbatim `.env.example` copy gives a --live run: every field present
# and valid, ANTHROPIC_API_KEY empty, platform URL the offline placeholder.
_PLACEHOLDER_LIVE_ENV = {
    "ANTHROPIC_API_KEY": "",
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


def _stub_run_pipeline(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Stub run_all and every artifact writer so main() can cross the run
    boundary without touching evals/{runs,reports,trajectories,briefings}."""
    run_all_calls: list[dict[str, Any]] = []

    def _stub_run_all(*args: Any, **kwargs: Any) -> Any:
        run_all_calls.append({"args": args, "kwargs": kwargs})
        return _green_stub_report(), (), ()

    monkeypatch.setattr(runner_module, "run_all", _stub_run_all)
    monkeypatch.setattr(
        runner_module, "archive_run", lambda *_a, **_kw: runner_module._RUNS_DIR / "stub"
    )
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
        # The executed S-09 repro: a verbatim .env.example copy under --live.
        # At HEAD this runs the whole suite canned and returns 0.
        _isolate_settings_env(monkeypatch, tmp_path, _PLACEHOLDER_LIVE_ENV)
        _forbid_run_all(monkeypatch)
        monkeypatch.setattr(sys, "argv", ["evals.runner", "--live"])
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
        run_all_calls = _stub_run_pipeline(monkeypatch)
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
        run_all_calls = _stub_run_pipeline(monkeypatch)
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
        monkeypatch.setattr(
            runner_module,
            "assert_no_tier1_successes",
            lambda _client, _since: guard_calls.append("audit"),
        )
        monkeypatch.setattr(sys, "argv", ["evals.runner", "--live", "--smoke"])
        assert runner_module.main() == 0
        assert preflight_calls == ["sk-ant-test-not-a-real-key"]
        assert guard_calls == ["principal", "audit"]
        assert len(run_all_calls) == 1
        # The read-scoped smoke token — not the write token — reached run_all.
        assert run_all_calls[0]["kwargs"]["mcp_token"] == "sa_smoke_read_only"
