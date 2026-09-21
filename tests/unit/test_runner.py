from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Final
from uuid import UUID

import pytest
from pydantic import BaseModel, SecretStr

from evals import artifacts
from evals import chaos_hooks as chaos_hooks_module
from evals import guards as guards_module
from evals import runner as runner_module
from evals.chaos_hooks import ChaosInvocationError
from evals.fakes import CannedMCPClient
from evals.graders.deterministic import (
    DimensionResult,
    GradeDimension,
    GradeReport,
    ScenarioExpectation,
)
from evals.graders.root_cause import is_not_graded_detail
from evals.runner import (
    _SCENARIOS_DIR,
    ALERT_FROM_PLATFORM_FLAG,
    EXTERNAL_CHAOS_SEEDER,
    PLATFORM_ALERT_SOURCE,
    SCENARIO_ALERT_SOURCE,
    WORLD_ALREADY_FAULTED_FLAG,
    ChaosSetupFailed,
    PreconditionNotMet,
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
from evals.scenarios.loader import load_scenarios
from evals.scenarios.schema import (
    ChaosHook,
    ChaosPlan,
    PreconditionField,
    PreconditionProbe,
    Scenario,
    ScenarioDifficulty,
    TtlFromWindows,
)
from incident_commander.agent import factory
from incident_commander.agent.briefing import EscalationBriefing
from incident_commander.agent.investigation import make_llm_investigate
from incident_commander.agent.remediation import make_llm_verify
from incident_commander.agent.state import BudgetLedger, EvidenceEntry, IncidentState, RunState
from incident_commander.api.schemas import AlertPayload
from incident_commander.config import Settings, settings_env_var_names
from incident_commander.llm.client import LLMResult
from incident_commander.llm.fakes import CannedLLMClient
from incident_commander.tools.mcp_client import LabProbeClient, MCPError, ToolResult

_NOW = datetime(2026, 8, 8, tzinfo=UTC)
# Stand-in for the path a real ``write_report`` returns, for the tests that
# stub the writers out so ``main()`` never touches evals/ (ADR 0011 freeze).
_STUB_REPORT_PATH = Path("evals/reports/report.20260808T000000Z.stubbed0000.json")


def _is_write_locked(path: Path) -> bool:
    return path.stat().st_mode & 0o222 == 0


def _unlock_tree(root: Path) -> None:
    """Deliberately unlock an archive tree so pytest can clean tmp_path.

    Mirrors the operator unlock in docs/runbook.md: flags clear before modes,
    because chmod on a ``uchg`` path is itself EPERM.
    """
    if not root.exists():
        return
    paths = [root, *root.rglob("*")]
    if hasattr(os, "chflags"):
        for path in paths:
            try:
                os.chflags(path, getattr(path.stat(), "st_flags", 0) & ~stat.UF_IMMUTABLE)
            except OSError:
                continue
    for path in paths:
        try:
            path.chmod(path.stat().st_mode | (0o700 if path.is_dir() else 0o600))
        except OSError:
            continue


@pytest.fixture(autouse=True)
def _tmp_archives_unlocked_for_cleanup(tmp_path: Path) -> Iterator[None]:
    """Any test that finalizes an archive under tmp_path leaves immutable files behind,
    and pytest's own retention sweep is what trips over them.
    """
    yield
    _unlock_tree(tmp_path)


def _test_settings(**overrides: Any) -> Settings:
    defaults: dict[str, Any] = {
        "anthropic_api_key": SecretStr("eval"),
        "judge_model": "claude-haiku-4-5",
        "platform_mcp_url": "https://eval.local",
        "platform_rest_url": "https://eval.local",
        "platform_token": SecretStr("eval"),
        # The evaluator's principal, present by default because seeding a chaos plan
        # refuses without it (platform v0.6.5) — otherwise every plan test would fail on
        # the credential rather than on its subject.
        "platform_chaos_token": SecretStr("eval-chaos"),
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
            # Value text, not key text: `lag` would name the field get_consumer_lag serializes
            # whatever the reading is, and the schema refuses that shape.
            expected_evidence_contains=("billing",),
            max_tool_calls=5,
        ),
        canned_tool_responses={
            "get_consumer_lag": ToolResult(
                content=[
                    {
                        "type": "text",
                        "text": (
                            '{"consumer_group":"billing","lag":42,"lag_known":true,"source":"static",'
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

    ADR 0007: a soft-quality decoration failing on an already-graded run is a missing
    column, not a transport crash.
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
        """Regression: `run_all` used to propagate the first scenario's exception, wiping
        every scenario that had not run yet.
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
        # Every shipped scenario has canned fallback data, so all of them run and pass
        # offline — including ROOT_CAUSE on the 32 that declare a ground truth, so this also
        # says no canned planner misdiagnoses the world its own fixtures serve.
        report, _, _ = run_all(scenarios, _test_settings())
        failed = sorted(o.scenario for o in report.outcomes if not o.report.passed)
        assert report.failed == 0, f"scenarios red in the offline suite: {failed}"
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

        # No live platform is reachable here, so the MCP client factory is patched to return
        # a fake with a no-op close() — the runner calls .close() in its finally block.
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
    """Filenames are versioned; every read goes through ``artifacts.newest``.

    These used to assert the fixed name ``latest.json``, and now assert the resolver
    returns what was just written — the same guarantee, in production's spelling.
    """

    def test_round_trip_json(self, tmp_path: Path) -> None:
        report, _, _ = run_all(
            [_passing_scenario()], _test_settings(), invocation_id="inv000000001"
        )
        written = write_report(report, directory=tmp_path)
        assert written == artifacts.newest("report", directory=tmp_path)
        loaded = RunReport.model_validate_json(written.read_text())
        assert loaded == report

    def test_creates_parent_directory(self, tmp_path: Path) -> None:
        report, _, _ = run_all(
            [_passing_scenario()], _test_settings(), invocation_id="inv000000001"
        )
        target = tmp_path / "nested" / "reports"
        written = write_report(report, directory=target)
        assert written.exists()
        assert json.loads(written.read_text())["total"] == 1

    def test_the_name_carries_the_runs_own_identity(self, tmp_path: Path) -> None:
        report, _, _ = run_all(
            [_passing_scenario()], _test_settings(), invocation_id="inv000000001"
        )
        written = write_report(report, directory=tmp_path)
        assert written.name == artifacts.version_name(
            "report", timestamp=report.generated_at, invocation_id="inv000000001"
        )

    def test_a_run_with_no_identity_cannot_name_its_report(self, tmp_path: Path) -> None:
        """An unidentified run is refused, not filed under a blank id.

        A report with no ``invocation_id`` cannot be joined back to the run that paid for
        it, and two such runs would collide within the same second.
        """
        report, _, _ = run_all([_passing_scenario()], _test_settings())
        assert report.invocation_id == ""
        with pytest.raises(ValueError, match="invocation_id"):
            write_report(report, directory=tmp_path)


class TestWriteTrajectories:
    def test_writes_one_file_per_trajectory(self, tmp_path: Path) -> None:
        _, trajectories, _ = run_all(
            [_passing_scenario(), _noise_scenario()], _test_settings(), invocation_id="inv000000001"
        )
        write_trajectories(trajectories, directory=tmp_path)
        resolved = {
            name: artifacts.newest("trajectory", name, directory=tmp_path)
            for name in ("consumer_lag_pass", "noise_alert")
        }
        assert len(set(resolved.values())) == 2
        assert sorted(p.name for p in tmp_path.iterdir()) == sorted(
            p.name for p in resolved.values()
        )

    def test_round_trip_trajectory(self, tmp_path: Path) -> None:
        _, trajectories, _ = run_all(
            [_passing_scenario()], _test_settings(), invocation_id="inv000000001"
        )
        write_trajectories(trajectories, directory=tmp_path)
        newest = artifacts.newest("trajectory", "consumer_lag_pass", directory=tmp_path)
        assert Trajectory.model_validate_json(newest.read_text()) == trajectories[0]

    def test_creates_directory(self, tmp_path: Path) -> None:
        _, trajectories, _ = run_all(
            [_passing_scenario()], _test_settings(), invocation_id="inv000000001"
        )
        target = tmp_path / "nested" / "trajectories"
        write_trajectories(trajectories, directory=target)
        assert artifacts.newest("trajectory", "consumer_lag_pass", directory=target).exists()


class TestWriteBriefings:
    def test_writes_one_file_per_briefing(self, tmp_path: Path) -> None:
        _, _, briefings = run_all([_passing_scenario(), _noise_scenario()], _test_settings())
        write_briefings(
            briefings,
            ["consumer_lag_pass", "noise_alert"],
            directory=tmp_path,
            invocation_id="inv000000001",
        )
        resolved = {
            name: artifacts.newest("briefing", name, directory=tmp_path)
            for name in ("consumer_lag_pass", "noise_alert")
        }
        assert len(set(resolved.values())) == 2
        assert sorted(p.name for p in tmp_path.iterdir()) == sorted(
            p.name for p in resolved.values()
        )

    def test_round_trip_briefing(self, tmp_path: Path) -> None:
        _, _, briefings = run_all([_passing_scenario()], _test_settings())
        write_briefings(
            briefings, ["consumer_lag_pass"], directory=tmp_path, invocation_id="inv000000001"
        )
        newest = artifacts.newest("briefing", "consumer_lag_pass", directory=tmp_path)
        assert EscalationBriefing.model_validate_json(newest.read_text()) == briefings[0]

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
        assert _classify_failure(self._report(set()), self._final()) == ("passed", "")

    def test_budget_only_is_llm_variance(self) -> None:
        got = _classify_failure(self._report({GradeDimension.BUDGET}), self._final())
        assert got == ("llm-variance", "")

    def test_evidence_only_is_grader_brittleness(self) -> None:
        got = _classify_failure(self._report({GradeDimension.EVIDENCE}), self._final())
        assert got[0] == "grader-brittleness"

    def test_the_grader_drift_bucket_carries_its_diagnosis(self) -> None:
        """The bucket names the suspect; the detail says where to look.

        `grader-brittleness` was already right on live run 4974811d236f (INC-001) and
        still cost a full trace read to learn WHICH claim and call shape disagreed.
        """
        report = GradeReport(
            scenario="s",
            passed=False,
            dimensions=tuple(
                DimensionResult(
                    dimension=d,
                    passed=d is not GradeDimension.EVIDENCE,
                    detail=(
                        "['list_dlq_messages'] field 'total' expected equals 4, observed (last) [0]"
                        if d is GradeDimension.EVIDENCE
                        else "ok"
                    ),
                )
                for d in GradeDimension
            ),
        )
        final = self._final().model_copy(
            update={
                "evidence": (
                    EvidenceEntry(
                        tool_name="list_dlq_messages",
                        arguments={"remediation_hint": None},
                        result_summary='{"total":5}',
                        timestamp=self._NOW,
                    ),
                    EvidenceEntry(
                        tool_name="list_dlq_messages",
                        arguments={"remediation_hint": "replay_safe"},
                        result_summary='{"total":0}',
                        timestamp=self._NOW,
                    ),
                    EvidenceEntry(
                        tool_name="_verify_judge",
                        arguments={},
                        result_summary="verified",
                        timestamp=self._NOW,
                    ),
                )
            }
        )
        bucket, detail = _classify_failure(report, final)
        assert bucket == "grader-brittleness"
        assert "grader-drift signature" in detail
        assert "read the trajectory before changing any prompt" in detail
        # The claim that failed...
        assert "expected equals 4" in detail
        # ...beside the shapes the agent actually used, so the mismatch reads
        # off the report instead of out of the trace.
        assert '"remediation_hint": null' in detail
        assert '"remediation_hint": "replay_safe"' in detail
        # Bookkeeping entries are not tool calls and stay out of it.
        assert "_verify_judge" not in detail

    def test_tool_is_error_is_shared_env(self) -> None:
        final = self._final(("get_redis_health", "tool reported is_error=True (get_redis_health)"))
        got = _classify_failure(self._report({GradeDimension.OUTCOME}), final)
        assert got == ("shared-env", "")

    def test_real_mcp_transport_summary_is_transport(self) -> None:
        # Summary built as investigation.py's tool-error escalation builds it — from str()
        # of a real MCPError, which never renders the class name (A-07).
        err = MCPError(-32000, "connection reset by peer")
        final = self._final(("get_consumer_lag", f"tool error (get_consumer_lag): {err}"))
        got = _classify_failure(self._report({GradeDimension.OUTCOME}), final)
        assert got == ("transport", "")

    def test_transport_beats_shared_env(self) -> None:
        # Priority order is deliberate: transport before shared-env, per the
        # debugging discipline in docs/lessons/live-eval-noise-sources.md.
        err = MCPError(-32000, "connection reset by peer")
        final = self._final(
            ("get_consumer_lag", f"tool error (get_consumer_lag): {err}"),
            ("get_redis_health", "tool reported is_error=True (get_redis_health)"),
        )
        got = _classify_failure(self._report({GradeDimension.OUTCOME}), final)
        assert got == ("transport", "")

    def test_not_verified_with_correct_action_is_eventual_consistency(self) -> None:
        final = self._final(
            ("pause_dag", '{"accepted": true}'),
            ("_verify_judge", "not_verified: nodes still waiting"),
        )
        got = _classify_failure(self._report({GradeDimension.OUTCOME}), final)
        assert got == ("eventual-consistency", "")

    def test_wrong_action_outcome_fail_is_unclassified(self) -> None:
        # ACTION failed + OUTCOME failed with no environment/consistency
        # signature: the conservative bucket is "look at the trace".
        got = _classify_failure(
            self._report({GradeDimension.OUTCOME, GradeDimension.ACTION}), self._final()
        )
        assert got == ("unclassified", "")

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

    ``write_trajectories`` keyed on scenario name and used ``write_text``, so any
    later run overwrote the previous one — which is how Run 001's paid live
    trajectories were destroyed (F-003).
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

    def test_a_later_top_level_write_destroys_nothing(self, tmp_path: Path) -> None:
        """The exact Run 001 loss (F-003), now impossible in BOTH places.

        This test used to ASSERT the loss, checking only that the archive still held the
        first run, because the flat file was a pointer by design. That exception is
        withdrawn: the second run must leave the first's top-level copy readable too.
        """
        runs, flat = tmp_path / "runs", tmp_path / "trajectories"
        first_traj = Trajectory(
            scenario="s", incident_id="first", checkpoints=(), invocation_id="inv_one"
        )
        second_traj = Trajectory(
            scenario="s", incident_id="second", checkpoints=(), invocation_id="inv_two"
        )
        archive_run("inv_one", self._report("s"), [first_traj], [], [], runs)
        [first_flat] = write_trajectories([first_traj], flat)
        [second_flat] = write_trajectories([second_traj], flat)

        assert first_flat != second_flat
        assert json.loads(first_flat.read_text())["incident_id"] == "first"
        assert json.loads(second_flat.read_text())["incident_id"] == "second"
        assert artifacts.newest("trajectory", "s", directory=flat) == second_flat

        archived = json.loads((runs / "inv_one" / "trajectories" / "s.json").read_text())
        assert archived["incident_id"] == "first", "archive must not follow the newest"


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

    Until 2026-08-09 every artifact but the traces lived in memory until the suite
    finished, so a Ctrl-C at scenario 30 of 37 threw away 30 scenarios of paid
    evidence (S-05/A-14); the archive also omitted traces (S-07) and wrote
    report.json FIRST, so a half-written archive looked complete (S-08).
    """

    def test_killed_suite_keeps_completed_scenarios(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The assertion that would have caught S-05/A-14: run_all catches Exception only, so
        # a KeyboardInterrupt escapes by design — and scenario 1's evidence must already be
        # on disk when it does.
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
        # Catches S-07: the archive omitted traces, so the join from an archived run to its
        # prompts ran through the flat multi-vintage trace file and did not survive it.
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
        # Catches S-08: report.json used to be the FIRST archive write, so a crash
        # mid-archive left a directory that looked complete.
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
        # End-to-end wiring, in process: main() must create the run directory BEFORE run_all
        # and hand it a streaming callback. run_all and the flat writers are stubbed.
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
        # write_report returns the path it wrote, which main() prints.
        monkeypatch.setattr(runner_module, "write_report", lambda *_a, **_kw: _STUB_REPORT_PATH)
        monkeypatch.setattr(runner_module, "write_trajectories", lambda *_a, **_kw: [])
        monkeypatch.setattr(runner_module, "write_briefings", lambda *_a, **_kw: [])
        monkeypatch.setattr(sys, "argv", ["evals.runner"])

        assert runner_module.main() == 0
        assert (targets[0] / "report.json").exists()


class TestCompletedArchiveIsLocked:
    """CLAUDE.md invariant 9, enforced by the filesystem (ADR 0021).

    Exclusive-create protects the archive from the runner; nothing protected it from
    anything else, and 0 of 371 files under ``evals/runs/`` carried on-disk
    protection when a routine cleanup came within one command of destroying 195 run
    files. Write-bit assertions run everywhere; the ``uchg`` layer only where the
    platform has it.
    """

    def _streamed_archive(self, tmp_path: Path) -> Path:
        target = tmp_path / "runs" / "inv"
        (target / "trajectories").mkdir(parents=True)
        (target / "briefings").mkdir(parents=True)
        runner_module.archive_scenario(
            target, _finished_result("consumer_lag_pass"), invocation_id="inv", trace_dir=None
        )
        return target

    def test_finalize_locks_every_path_in_the_archive(self, tmp_path: Path) -> None:
        target = self._streamed_archive(tmp_path)
        runner_module.finalize_archive(target, _stub_report("consumer_lag_pass"))

        for path in (
            target,
            target / "report.json",
            target / "trajectories",
            target / "trajectories" / "consumer_lag_pass.json",
            target / "briefings",
            target / "briefings" / "consumer_lag_pass.json",
        ):
            assert _is_write_locked(path), f"{path} must have no write bits after finalize"
        # The claims that matter, made against the filesystem itself:
        # no truncation, no deletion, no new entries.
        with pytest.raises(PermissionError):
            (target / "report.json").open("a")
        with pytest.raises(PermissionError):
            (target / "trajectories" / "consumer_lag_pass.json").unlink()
        with pytest.raises(PermissionError):
            (target / "intruder.json").open("x")

    def test_scenario_files_lock_as_they_land_but_the_run_stays_appendable(
        self, tmp_path: Path
    ) -> None:
        # The killed-run half: each scenario's evidence is protected the moment it is
        # durable (ADR 0017) while the DIRECTORIES stay writable so N+1 and the marker can
        # land. A fresh invocation_id per run means the unlocked window is this run's alone.
        target = self._streamed_archive(tmp_path)

        first = target / "trajectories" / "consumer_lag_pass.json"
        assert _is_write_locked(first)
        with pytest.raises(PermissionError):
            first.open("w")
        assert not _is_write_locked(target), "run dir must accept the next scenario"
        assert not _is_write_locked(target / "trajectories")

        runner_module.archive_scenario(
            target, _finished_result("noise_alert"), invocation_id="inv", trace_dir=None
        )
        assert _is_write_locked(target / "trajectories" / "noise_alert.json")

    @pytest.mark.skipif(not hasattr(os, "chflags"), reason="no file flags on this platform")
    def test_uchg_backs_the_lock_where_the_platform_has_it(self, tmp_path: Path) -> None:
        # On macOS the lock must survive what chmod alone cannot: unlink goes by the PARENT
        # directory's write bit, so before finalize a read-only file in a writable directory
        # would still delete. ``uchg`` is the layer that refuses that.
        target = self._streamed_archive(tmp_path)
        first = target / "trajectories" / "consumer_lag_pass.json"
        assert getattr(first.stat(), "st_flags", 0) & stat.UF_IMMUTABLE
        with pytest.raises(PermissionError):
            first.unlink()

        runner_module.finalize_archive(target, _stub_report("consumer_lag_pass"))
        assert getattr(target.stat(), "st_flags", 0) & stat.UF_IMMUTABLE
        assert getattr((target / "report.json").stat(), "st_flags", 0) & stat.UF_IMMUTABLE

    def test_lock_failure_is_logged_and_never_fatal(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A filesystem that refuses chmod must not turn a finished run into a crashed one:
        # by lock time the evidence bytes are durable and the lock is best-effort on top.
        def _refuse(*_args: Any, **_kwargs: Any) -> None:
            raise OSError("Operation not permitted: filesystem refuses chmod")

        monkeypatch.setattr(os, "chmod", _refuse)
        target = self._streamed_archive(tmp_path)
        runner_module.finalize_archive(target, _stub_report("consumer_lag_pass"))

        report = json.loads((target / "report.json").read_text())
        assert report["total"] == 1, "the archive itself must be intact"
        assert "archive lock skipped" in capsys.readouterr().out

    def test_writing_into_a_finalized_archive_fails_loudly(self, tmp_path: Path) -> None:
        # The re-run story at the file layer. Reaching a finalized directory requires
        # colliding with its invocation id, which already fails before any spend; the lock
        # is the second wall — a NEW scenario cannot land in a sealed archive.
        target = self._streamed_archive(tmp_path)
        runner_module.finalize_archive(target, _stub_report("consumer_lag_pass"))

        with pytest.raises(PermissionError):
            runner_module.archive_scenario(
                target, _finished_result("noise_alert"), invocation_id="inv", trace_dir=None
            )
        with pytest.raises(FileExistsError):
            runner_module.finalize_archive(target, _stub_report("consumer_lag_pass"))


class TestRunsDirIsTracked:
    """CLAUDE.md invariant 9: evals/runs/ must be commit-able (S-06).

    .gitignore carried an ``evals/runs/*`` entry, so no archive was ever tracked and
    routine git hygiene erased every one.

    It asks GIT, and does not pattern-match the file. Scanning .gitignore for a line
    starting ``evals/runs`` is not the question: the anchored spelling, ``runs/``,
    ``**/runs/``, a pattern in any parent file, ``.git/info/exclude`` and the global
    excludes all re-ignore the archive with the assertion green, and it reported a
    violation for a COMMENT line too. ``git check-ignore`` resolves the precedence git
    itself will apply — the resolver that erased the archives.

    The per-run output ignores stay ignored, versioned filenames included.
    ``evals/traces`` is deliberately NOT among them: for a scenario killed before its
    archive slice is written it is the only record that the work was billed.
    """

    # A path only a real archive contains. Asking about the bare directory would
    # under-test, because ``evals/runs/*`` ignores the CONTENTS. The path need not exist
    # — check-ignore answers from the patterns, which is what lets this run on a clean tree.
    _DURABLE_RECORD: Final[str] = "evals/runs/inv-20260101-000000/report.json"

    # A trajectory that must stay ignored, proving the probe below can still answer
    # "yes". Spelled in the VERSIONED form the runner writes now: an ignore rule written
    # as `evals/trajectories/*.json` would keep this green while every per-run file it
    # covers started showing up untracked.
    _IGNORED_TRAJECTORY: Final[str] = (
        "evals/trajectories/consumer_lag_pass.20260101T000000Z.abc123abc123.json"
    )
    # The pre-versioning flat name. Still on disk in existing checkouts as
    # evidence (never deleted, never renamed), so its ignore must hold too.
    _IGNORED_LEGACY_TRAJECTORY: Final[str] = "evals/trajectories/consumer_lag_pass.json"

    @staticmethod
    def _git_ignores(path: str) -> bool:
        """Would git skip ``path``? Exit 0 = ignored, 1 = not, anything else = broken.

        128 is git refusing to answer, which must fail loudly: read as "not ignored" it is
        a guard that passes because it broke. No network — a local subprocess against the
        working tree.
        """
        repo_root = Path(__file__).resolve().parents[2]
        try:
            result = subprocess.run(
                ["git", "check-ignore", "-q", "--", path],
                capture_output=True,
                text=True,
                cwd=repo_root,
                check=False,
            )
        except FileNotFoundError:  # pragma: no cover - git is present in CI
            pytest.skip(
                "git is not on PATH, so the ignore rules for the invariant-9 "
                "durable record cannot be resolved. This guard needs a real git."
            )
        if result.returncode not in (0, 1):
            raise AssertionError(
                f"`git check-ignore -q -- {path}` exited {result.returncode} in "
                f"{repo_root} instead of 0 (ignored) or 1 (not ignored), so this "
                f"guard could not be evaluated at all: {result.stderr.strip()!r}. "
                f"128 usually means the tests are running outside a git work "
                f"tree; run them from a checkout, or fix the ignore file git is "
                f"complaining about."
            )
        return result.returncode == 0

    def test_git_does_not_ignore_the_durable_record(self) -> None:
        assert not self._git_ignores(self._DURABLE_RECORD), (
            f"git ignores {self._DURABLE_RECORD}, the CLAUDE.md invariant-9 "
            f"durable record. A live campaign's archive cannot be committed and "
            f"`git clean -fdx` erases paid evidence — exactly finding S-06, when "
            f"`evals/runs/*` sat in .gitignore and no archive was ever tracked. "
            f"Run `git check-ignore -v {self._DURABLE_RECORD}` to see which file "
            f"and line matched, and remove that pattern (or negate it with a "
            f"`!evals/runs/**` line below it). Note this is not necessarily "
            f".gitignore: nested ignore files, .git/info/exclude and the global "
            f"excludes file all count."
        )

    def test_the_probe_can_still_say_yes(self) -> None:
        """Canary: prove the check above is capable of failing.

        ``_git_ignores`` returning False is only evidence if it can return True — a wrong
        cwd or a git that stopped resolving patterns would answer "not ignored" for
        everything while the archive was being deleted.
        """
        for path in (self._IGNORED_TRAJECTORY, self._IGNORED_LEGACY_TRAJECTORY):
            assert self._git_ignores(path), (
                f"git does NOT ignore {path}, a per-run trajectory .gitignore "
                f"holds under `evals/trajectories/*`. Either that ignore was "
                f"deliberately dropped — in which case point this canary at another "
                f"still-ignored path, it exists only to prove the probe discriminates "
                f"— or `git check-ignore` is not resolving patterns here at all, "
                f"which would make the durable-record guard above vacuous."
            )


# --- Run provenance + exit-code contract (ADR 0013; findings A-01/S-09/A-04/A-15) ---

# Every env var Settings can read, walked from the model. Exit-code tests must clear
# ALL of them and chdir away from any real .env, or a developer's environment leaks
# in — the A-04 mechanism. Hand-kept until WO-R2-87, when it had already drifted.
_SETTINGS_ENV_VARS = settings_env_var_names()

# A present-but-offline env for a --live run: every field set and parseable, with
# placeholder key and URL. A verbatim `.env.example` copy no longer parses this way —
# env_ignore_empty treats its blank required entries as unset.
_PLACEHOLDER_LIVE_ENV = {
    "ANTHROPIC_API_KEY": "eval",
    "JUDGE_MODEL": "claude-haiku-4-5",
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
    "JUDGE_MODEL": "claude-haiku-4-5",
    "PLATFORM_MCP_URL": "http://real.host:8001/mcp",
    "PLATFORM_REST_URL": "http://real.host:8000",
    "PLATFORM_TOKEN": "sa_agent_reads_and_acts",
    "PLATFORM_CHAOS_TOKEN": "sa_evaluator_chaos_invoke",
    "PLATFORM_SMOKE_TOKEN": "sa_smoke_read_only",
    "PLATFORM_WEBHOOK_SECRET": "whsec_test",
    "DATABASE_URL": "postgresql://eval:eval@localhost:5432/eval",
}


# A smoke pass selects chaos-free scenarios, as `make eval-smoke` does. An
# unfiltered `--live --smoke` selects the whole suite and is refused with exit 6: it
# would seed chaos under the full principal during the read-only stage (S-03). One
# representative pattern stands in for the full SMOKE_ONLY list.
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


def test_isolate_settings_env_clears_every_variable_settings_reads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The exit-code tests below are only meaningful if this holds.

    ``_SETTINGS_ENV_VARS`` was typed by hand under the claim that it was every var
    Settings reads, and it was not — so a developer with AGENT_ENABLED exported ran
    the exit-code contract against their own shell, the A-04 mechanism reintroduced
    inside the guard against it. Derived from the model now.
    """
    for name in settings_env_var_names():
        monkeypatch.setenv(name, "9999")
    _isolate_settings_env(monkeypatch, tmp_path)
    survivors = sorted(name for name in settings_env_var_names() if name in os.environ)
    assert survivors == [], (
        f"these Settings variables survive _isolate_settings_env: {survivors}. "
        "Whatever the developer exported for them is what the exit-code tests read."
    )


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
    """Stub run_all and the flat writers so main() can cross the run boundary without
    touching evals/, and redirect the run archive at tmp_path.
    """
    run_all_calls: list[dict[str, Any]] = []

    def _stub_run_all(*args: Any, **kwargs: Any) -> Any:
        run_all_calls.append({"args": args, "kwargs": kwargs})
        return _green_stub_report(), (), ()

    monkeypatch.setattr(runner_module, "_RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(runner_module, "run_all", _stub_run_all)
    monkeypatch.setattr(runner_module, "write_report", lambda *_a, **_kw: _STUB_REPORT_PATH)
    monkeypatch.setattr(runner_module, "write_trajectories", lambda *_a, **_kw: [])
    monkeypatch.setattr(runner_module, "write_briefings", lambda *_a, **_kw: [])
    return run_all_calls


class _StubGuardClient:
    def close(self) -> None:
        return None


def _split_agent_probe(monkeypatch: pytest.MonkeyPatch) -> Any:
    """One client that answers as the post-v0.6.5 AGENT principal.

    For tests that patch ``make_client`` directly: the Tier-1 probe is refused on its
    arguments (the token can act) and the chaos probe on scope (it cannot seed).
    """
    probe = _ClosableCanned({})

    def _call_tool(name: str, _arguments: Any, **_kw: Any) -> Any:
        if name == guards_module._CHAOS_PROBE_TOOL:
            raise MCPError(-32002, "missing required scope: chaos:invoke")
        raise MCPError(-32602, "invalid tool arguments")

    monkeypatch.setattr(probe, "call_tool", _call_tool)
    return probe


def _stub_principal_probe(
    monkeypatch: pytest.MonkeyPatch,
    behavior: Exception | None = None,
    *,
    agent_chaos_behavior: Exception | None = None,
) -> list[str]:
    """Give main()'s principal guards a local probe client.

    Without it, any test reaching a guard under a live-looking env builds a real
    ``MCPClient`` and fires a real ``tools/call`` at PLATFORM_MCP_URL — invisible in
    the exit code, because the guards fail closed on the resulting error.

    The default is an argument refusal, which is what both the write and the chaos
    guard require to let a run proceed. One exception, the token split: the client
    built for the AGENT answers the CHAOS probe with a SCOPE refusal, because a token
    that can fire the lab is served the lab's audit rows. Which client is which is
    read off the token, as the runner does. Returns the probe tool names called, so a
    test can assert WHICH scope was probed.
    """
    probed: list[str] = []
    error = behavior if behavior is not None else MCPError(-32602, "invalid tool arguments")
    blind = (
        agent_chaos_behavior
        if agent_chaos_behavior is not None
        else MCPError(-32002, "missing required scope: chaos:invoke")
    )

    def _make(settings: Any, *_a: Any, token: str | None = None, **_kw: Any) -> Any:
        chaos_secret = getattr(settings, "platform_chaos_token", None)
        is_chaos_client = chaos_secret is not None and token == chaos_secret.get_secret_value()
        probe = _ClosableCanned({})

        def _call_tool(name: str, _arguments: Any, **_k: Any) -> Any:
            probed.append(name)
            # The agent's client answers the chaos probe with a SCOPE refusal by default — the
            # post-v0.6.5 principal, and the only shape that passes the blindness guard.
            if not is_chaos_client and name == guards_module._CHAOS_PROBE_TOOL:
                raise blind
            raise error

        monkeypatch.setattr(probe, "call_tool", _call_tool)
        return probe

    monkeypatch.setattr(runner_module, "make_client", _make)
    return probed


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
    """The committed baseline must parse, and must not claim what it does not know.

    Until WO-R3-249 this pinned the PRE-SCHEMA baseline, whose provenance was carried
    by defaults; that bless replaced it with a stamped 41-scenario report, so the same
    invariant now asserts the other side — a field this run DOES know is answered.
    """

    def test_committed_baseline_parses_and_states_what_it_knows(self) -> None:
        baseline_path = Path(__file__).resolve().parents[2] / "evals" / "reports" / "baseline.json"
        report = RunReport.model_validate_json(baseline_path.read_text())
        # The corpus the gate compares against, read off the artifact rather
        # than hand-written twice.
        assert report.total == len(report.outcomes) == 41
        # A NUMBER, not None: the blessed run knows how many scenarios fell back to canned.
        # None is the pre-schema "unknown", and asserting it here would be a falsehood.
        assert report.degraded_count == 34
        assert report.only_patterns == ()
        # The roll-up agrees with the rows it is a roll-up of — the shape a
        # half-stamped report would break.
        assert sum(o.degraded for o in report.outcomes) == report.degraded_count


class TestMainExitCodes:
    """The new preflight refusals all exit 3 BEFORE run_all — pre-spend."""

    def test_live_with_placeholder_env_refuses_exit_3(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The executed S-09 repro: present-but-placeholder values under --live, which at
        # HEAD ran the whole suite canned and returned 0. The verbatim-.env.example variant
        # now exits 3 even earlier, at construction.
        _isolate_settings_env(monkeypatch, tmp_path, _PLACEHOLDER_LIVE_ENV)
        _forbid_run_all(monkeypatch)
        # --only is load-bearing since ADR 0020: a full-suite --live selection is refused
        # before the environment is examined, because a wrong SELECTION is knowable without
        # touching the env. Narrowed here so the test still reaches the env path.
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
        # --only is mandatory under --live since the missing-filter backstop, and that
        # refusal runs BEFORE the settings load — so the broken-env claim needs a selection.
        monkeypatch.setattr(sys, "argv", ["evals.runner", "--live", "--only", "consumer_lag_pass"])
        assert runner_module.main() == 3

    def test_live_smoke_placeholder_platform_with_canned_only_selection_exits_3(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # A-04 defence in depth: the selection contains no use_live scenario (so the
        # degraded fail-fast is silent), the smoke token is present, and the platform is a
        # placeholder — there is no principal to guard.
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
        # The `make eval-smoke` shape must NOT trip the degraded fail-fast:
        # degraded_to_canned == 0. A unit-level replacement for observing it under the eval
        # freeze (ADR 0011), with every live collaborator stubbed.
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
            lambda _client, **_kwargs: guard_calls.append("principal"),
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
        # Neither principal id is configured here, so the post-stage audit stays
        # deliberately over-broad: any service account's in-window Tier-1 success fails.
        assert _principal_ids(audit_kwargs) == [None]

    def test_configured_principal_ids_reach_the_post_stage_audit(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # A-13: on a shared platform the guard must attribute violations to the principals
        # this stage owns, and BOTH ids are required — the F-001 failure mode writes under
        # the AGENT principal, so filtering to the smoke id would blind the guard to its own
        # reason for existing.
        env = dict(_REAL_LOOKING_LIVE_ENV)
        env["PLATFORM_AGENT_PRINCIPAL_ID"] = "agent-sa-uuid"
        env["PLATFORM_SMOKE_PRINCIPAL_ID"] = "smoke-sa-uuid"
        _isolate_settings_env(monkeypatch, tmp_path, env)
        _stub_run_pipeline(monkeypatch, tmp_path)
        audit_kwargs: list[dict[str, Any]] = []
        monkeypatch.setattr(runner_module, "preflight_auth", lambda _key: None)
        monkeypatch.setattr(runner_module, "make_client", lambda *_a, **_kw: _StubGuardClient())
        monkeypatch.setattr(
            runner_module, "assert_read_only_principal", lambda _client, **_kwargs: None
        )
        monkeypatch.setattr(
            runner_module,
            "assert_no_tier1_successes",
            lambda _client, _since, **kwargs: audit_kwargs.append(kwargs),
        )
        monkeypatch.setattr(sys, "argv", ["evals.runner", "--live", "--smoke", *_SMOKE_ONLY_ARGS])
        assert runner_module.main() == 0
        assert _principal_ids(audit_kwargs) == [frozenset({"agent-sa-uuid", "smoke-sa-uuid"})]

    def test_half_configured_principal_ids_fall_back_to_unfiltered(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Naming ONLY the smoke principal would filter out the agent rows F-001 writes, so a
        # half-configured env falls back to the over-broad default and says so.
        env = dict(_REAL_LOOKING_LIVE_ENV)
        env["PLATFORM_SMOKE_PRINCIPAL_ID"] = "smoke-sa-uuid"
        _isolate_settings_env(monkeypatch, tmp_path, env)
        _stub_run_pipeline(monkeypatch, tmp_path)
        audit_kwargs: list[dict[str, Any]] = []
        monkeypatch.setattr(runner_module, "preflight_auth", lambda _key: None)
        monkeypatch.setattr(runner_module, "make_client", lambda *_a, **_kw: _StubGuardClient())
        monkeypatch.setattr(
            runner_module, "assert_read_only_principal", lambda _client, **_kwargs: None
        )
        monkeypatch.setattr(
            runner_module,
            "assert_no_tier1_successes",
            lambda _client, _since, **kwargs: audit_kwargs.append(kwargs),
        )
        monkeypatch.setattr(sys, "argv", ["evals.runner", "--live", "--smoke", *_SMOKE_ONLY_ARGS])
        assert runner_module.main() == 0
        assert _principal_ids(audit_kwargs) == [None]
        assert "only one of PLATFORM_AGENT_PRINCIPAL_ID" in capsys.readouterr().out


def _principal_ids(audit_kwargs: list[dict[str, Any]]) -> list[Any]:
    """The `principal_ids` each post-stage audit call was given.

    The call also carries `scan=`, so these read the one kwarg they are about rather
    than pinning the whole signature; `scan` is the subject of
    `TestPostStageAuditIsCheckpointed`.
    """
    return [kwargs["principal_ids"] for kwargs in audit_kwargs]


class TestSmokeRefusesChaosSeeding:
    """S-03: a read-only stage does not seed chaos.

    ``run_scenario`` fires ``chaos_setup`` under the full principal regardless of
    ``--smoke``, the #80 guard only asserts the agent client's token, and the exit-5
    audit sees the write after it lands — so the only prevention is refusing the run.
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
        # At HEAD this run proceeds: the hook fires inside run_scenario under the FULL
        # principal during the stage whose purpose is proving the smoke token is read-only.
        # Driven through --only since WO-R2-123, which IS the case worth pinning — a bare
        # --smoke derives from ``in_smoke_pass``, so the operator override is the only way
        # one can still reach the stage (the reachable channel ADR 0018 names).
        _isolate_settings_env(monkeypatch, tmp_path, _REAL_LOOKING_LIVE_ENV)
        _forbid_run_all(monkeypatch)
        hook_calls: list[str] = []
        monkeypatch.setattr(
            runner_module,
            "invoke_chaos_hook",
            lambda _url, _token, name, _args: hook_calls.append(name),
        )
        monkeypatch.setattr(runner_module, "load_scenarios", lambda _dir: [self._chaos_scenario()])
        monkeypatch.setattr(
            sys, "argv", ["evals.runner", "--live", "--smoke", "--only", "remediate_"]
        )
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
        # --only for the same reason as the test above: the derived selection
        # cannot contain a chaos scenario, so the override is the channel.
        monkeypatch.setattr(
            sys, "argv", ["evals.runner", "--live", "--smoke", "--only", "remediate_"]
        )
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
        monkeypatch.setattr(
            runner_module, "assert_read_only_principal", lambda _client, **_kwargs: None
        )
        monkeypatch.setattr(runner_module, "assert_no_tier1_successes", lambda *_a, **_kw: None)
        monkeypatch.setattr(
            runner_module,
            "load_scenarios",
            lambda _dir: [_passing_scenario().model_copy(update={"use_live_mcp": True})],
        )
        monkeypatch.setattr(sys, "argv", ["evals.runner", "--live", "--smoke"])
        assert runner_module.main() == 0
        assert len(run_all_calls) == 1

    def test_a_bare_smoke_run_derives_past_the_chaos_scenarios(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # WO-R2-123: the default path no longer needs the exit-6 refusal, because a
        # chaos-declaring scenario is not in the derived selection. The refusal stays for the
        # override channel, and this pins that the two do not fight.
        _isolate_settings_env(monkeypatch, tmp_path, _REAL_LOOKING_LIVE_ENV)
        run_all_calls = _stub_run_pipeline(monkeypatch, tmp_path)
        monkeypatch.setattr(runner_module, "preflight_auth", lambda _key: None)
        monkeypatch.setattr(runner_module, "make_client", lambda *_a, **_kw: _StubGuardClient())
        monkeypatch.setattr(
            runner_module, "assert_read_only_principal", lambda _client, **_kwargs: None
        )
        monkeypatch.setattr(runner_module, "assert_no_tier1_successes", lambda *_a, **_kw: None)
        monkeypatch.setattr(
            runner_module,
            "invoke_chaos_hook",
            lambda *_a, **_kw: pytest.fail("no chaos hook may fire in the smoke stage"),
        )
        monkeypatch.setattr(
            runner_module,
            "load_scenarios",
            lambda _dir: [
                _passing_scenario().model_copy(update={"use_live_mcp": True}),
                self._chaos_scenario(),
            ],
        )
        monkeypatch.setattr(sys, "argv", ["evals.runner", "--live", "--smoke"])
        assert runner_module.main() == 0
        assert len(run_all_calls) == 1
        assert "remediate_consumer_lag_success" not in run_all_calls[0]
        assert "smoke selection: 1 scenario(s)" in capsys.readouterr().out

    def test_non_smoke_live_run_keeps_chaos_seeding(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The three remediate_* scenarios legitimately seed chaos on a live
        # non-smoke run; the fix is stage-gating, not removing chaos.
        _isolate_settings_env(monkeypatch, tmp_path, _REAL_LOOKING_LIVE_ENV)
        run_all_calls = _stub_run_pipeline(monkeypatch, tmp_path)
        monkeypatch.setattr(runner_module, "preflight_auth", lambda _key: None)
        monkeypatch.setattr(runner_module, "load_scenarios", lambda _dir: [self._chaos_scenario()])
        # A chaos-seeding selection now clears the chaos:invoke guard before it
        # is allowed to run — see TestLiveChaosSeedingGuardsTheChaosScope.
        _stub_principal_probe(monkeypatch)
        monkeypatch.setattr(
            sys, "argv", ["evals.runner", "--live", "--only", "remediate_consumer_lag_success"]
        )
        assert runner_module.main() == 0
        assert len(run_all_calls) == 1


class TestSmokeRefusesAnythingOutsideTheDerivedSet:
    """The other half of the door above: ``--only`` could re-admit a WRITE.

    ``--only`` bypasses the derivation entirely and the gate it ran into checked
    ``chaos_setup`` alone — so a scenario declaring ``expected_action_tools`` and no
    chaos passed every guard: a graded Tier-1 write inside the stage whose purpose is
    proving the smoke token cannot write. Five shipped scenarios are that shape, all
    reachable by ``SMOKE_ONLY=dlq_``. ``Scenario.smoke_eligible``'s docstring already
    asserted the refusal existed, so the derivation and the gate disagreed about what
    the stage admits — and the gate is the one that runs. The override may still NARROW.
    """

    def _smoke_env(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _isolate_settings_env(monkeypatch, tmp_path, _REAL_LOOKING_LIVE_ENV)
        _forbid_run_all(monkeypatch)
        monkeypatch.setattr(runner_module, "preflight_auth", lambda _key: None)
        monkeypatch.setattr(
            runner_module, "assert_read_only_principal", lambda _client, **_kwargs: None
        )

    def test_a_write_scenario_cannot_be_smuggled_in_by_only(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # RED BEFORE: exit 0 — dlq_replay_safe_success declares expected_action_tools and no
        # chaos_setup, so the chaos-only gate waved it through and the stage graded a Tier-1
        # write under the read-scoped token.
        self._smoke_env(monkeypatch, tmp_path)
        monkeypatch.setattr(
            sys,
            "argv",
            ["evals.runner", "--live", "--smoke", "--only", "dlq_replay_safe_success"],
        )
        assert runner_module.main() == 6
        out = capsys.readouterr().out
        assert "SMOKE FAIL: 1 selected scenario(s) are not in the read-only smoke pass" in out
        assert "dlq_replay_safe_success — declares expected_action_tools" in out
        assert "no scenarios ran, nothing was spent" in out

    def test_the_refusal_names_every_offender_and_its_reason(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # `SMOKE_ONLY=dlq_` is the realistic operator form and covers both surviving reasons
        # at once: four write scenarios plus dlq_backlog, held back by a hand-written
        # smoke_exclusion. Three causes, three repairs, so the reason is per scenario — and
        # since the v0.6.2 re-pin `dlq_human_required_escalates` is held back for TWO at
        # once, asserted as two, because a reader needs all of them and not the first.
        self._smoke_env(monkeypatch, tmp_path)
        monkeypatch.setattr(sys, "argv", ["evals.runner", "--live", "--smoke", "--only", "dlq_"])
        assert runner_module.main() == 6
        out = capsys.readouterr().out
        assert "dlq_human_required_escalates — declares chaos_setup" in out
        assert "; declares expected_action_tools" in out
        assert "dlq_backlog — smoke_exclusion: " in out
        assert "--only narrows the derived smoke selection; it cannot widen it." in out

    def test_a_narrowing_override_still_runs(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # SMOKE_ONLY keeps substring semantics for scenarios that ARE in the derived set:
        # narrowing is the point of the override, and only widening is refused.
        _isolate_settings_env(monkeypatch, tmp_path, _REAL_LOOKING_LIVE_ENV)
        monkeypatch.setattr(runner_module, "preflight_auth", lambda _key: None)
        monkeypatch.setattr(
            runner_module, "assert_read_only_principal", lambda _client, **_kwargs: None
        )
        monkeypatch.setattr(
            runner_module, "assert_no_tier1_successes", lambda _client, _since, **_kw: None
        )
        _stub_principal_probe(monkeypatch)
        calls = _stub_run_pipeline(monkeypatch, tmp_path)
        monkeypatch.setattr(sys, "argv", ["evals.runner", "--live", "--smoke", "--only", "noise_"])
        assert runner_module.main() == 0
        selected = sorted(s.name for call in calls for s in call["args"][0])
        assert len(selected) == 5, f"a narrowing override must still run: {selected}"
        assert "SMOKE FAIL" not in capsys.readouterr().out

    def test_a_bare_smoke_run_is_unaffected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The derivation and the gate now agree by construction, so the gate can never fire
        # on a bare --smoke. If it ever does, the two have drifted apart.
        _isolate_settings_env(monkeypatch, tmp_path, _REAL_LOOKING_LIVE_ENV)
        monkeypatch.setattr(runner_module, "preflight_auth", lambda _key: None)
        monkeypatch.setattr(
            runner_module, "assert_read_only_principal", lambda _client, **_kwargs: None
        )
        monkeypatch.setattr(
            runner_module, "assert_no_tier1_successes", lambda _client, _since, **_kw: None
        )
        _stub_principal_probe(monkeypatch)
        calls = _stub_run_pipeline(monkeypatch, tmp_path)
        monkeypatch.setattr(sys, "argv", ["evals.runner", "--live", "--smoke"])
        assert runner_module.main() == 0
        selected = [s.name for call in calls for s in call["args"][0]]
        assert selected, "a bare --smoke must still derive a non-empty selection"

    def test_the_gate_agrees_with_the_derivation(self) -> None:
        # The gate's predicate must BE the derivation's, not a copy of it.
        # Two hand-kept lists drifting apart is the whole defect, one level up.
        scenarios = load_scenarios(_SCENARIOS_DIR)
        assert [s.name for s in scenarios if not s.in_smoke_pass], (
            "no scenario is held out of the smoke pass — this gate has no subject"
        )
        for scenario in scenarios:
            # `seeds_chaos`, not `chaos_setup`: a scenario spelling its world with the composable
            # `chaos_plan` leaves the legacy field None while still firing hooks, and this copy
            # of the predicate said "eligible" for WO-R3-214's four the moment they landed.
            expected = (
                not scenario.seeds_chaos
                and not scenario.expectation.expected_action_tools
                and scenario.smoke_exclusion is None
            )
            assert scenario.in_smoke_pass is expected, scenario.name


class TestCannedEquivalentKnobWarning:
    """S-10: a --live run whose probe knobs sit at the canned-equivalent defaults
    reproduces both documented live failure modes (ADR 0006, ADR 0009). It warns and
    never exits, because an explicit ``VERIFY_PROBE_ATTEMPTS=1`` is indistinguishable
    from unset and a hard fail would ban single-probe live experiments.
    """

    def test_warns_on_default_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # No knob overrides: the config defaults are deliberately canned-equivalent, so
        # default-constructed settings must warn. delenv guards against a developer shell.
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
        # Wiring: the warning fires immediately after the settings load, before the degraded
        # fail-fast, so even a refused --live run surfaces it. Pre-spend.
        _isolate_settings_env(monkeypatch, tmp_path, _PLACEHOLDER_LIVE_ENV)
        _forbid_run_all(monkeypatch)
        # --only is load-bearing since ADR 0020: a full-suite --live selection is refused
        # before the environment is examined. Narrowed so the test reaches the env path.
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

    Two defects, one wire. The runtime ceiling was ``settings.budget_max_tool_calls``
    whatever the scenario declared, and ``_format_planner_context`` renders "Budget
    remaining" from that same ledger — so the planner was told 25 in every scenario,
    including the ones whose whole subject is a tight budget.
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
        # The defect this closes is visible only in the prompt text: the planner reads
        # "Budget remaining: tool_calls=N" and decides how many probes it can afford.
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
        # The suite-wide statement: nine scenarios declare a cap of 0, which start_run
        # ignores, and the rest now run under the number they are graded against.
        from evals.scenarios.loader import load_scenarios

        scenarios = load_scenarios(Path(__file__).resolve().parents[2] / "evals" / "scenarios")
        report, _, _ = run_all(scenarios, _test_settings())
        failed = [o.scenario for o in report.outcomes if not o.report.passed]
        assert failed == [], f"scenarios red under their own ceiling: {failed}"
        # Anti-vacuity: a sweep over an empty report is green and says nothing.
        assert len(report.outcomes) >= 41
        # BUDGET is the dimension a ceiling can move, asserted directly so a future red
        # elsewhere cannot be mistaken for a budget failure.
        over_budget = [
            outcome.scenario
            for outcome in report.outcomes
            for dimension in outcome.report.dimensions
            if dimension.dimension is GradeDimension.BUDGET and not dimension.passed
        ]
        assert over_budget == [], f"scenarios over their own ceiling: {over_budget}"


class _ClosableCanned(CannedMCPClient):
    """CannedMCPClient with the close() the live path calls."""

    def close(self) -> None:  # pragma: no cover - no-op
        return None


class _ScriptedCanned(_ClosableCanned):
    """A canned client that stops answering on chosen (1-based) attempts.

    CannedMCPClient scripts changing ANSWERS; it cannot script a platform that stops
    answering mid-window, which is what the Not-Met/Unverifiable split turns on.
    """

    def __init__(self, responses: Any, *, dead_on: set[int]) -> None:
        super().__init__(responses)
        self._dead_on = dead_on
        self._attempts = 0

    def call_tool(
        self,
        name: str,
        arguments: Any,
        *,
        timeout_seconds: float | None = None,
        # The premise reads go through ``LabProbeClient`` since ADR 0075, so every fake
        # standing in for the transport takes the label and hands it on.
        lab_probe: str | None = None,
        lab_principal_token: str | None = None,
    ) -> ToolResult:
        self._attempts += 1
        if self._attempts in self._dead_on:
            self.calls.append((name, dict(arguments)))
            self.labels.append((lab_probe, lab_principal_token))
            raise MCPError(-32000, "connection reset by peer")
        return super().call_tool(
            name,
            arguments,
            timeout_seconds=timeout_seconds,
            lab_probe=lab_probe,
            lab_principal_token=lab_principal_token,
        )


class TestPreconditions:
    """An unmet premise abandons the run instead of grading the agent on it.

    The distinction `bb1fa70abb4c` could not draw: a run that never happened says
    nothing about the agent.
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
                        {
                            "consumer_group": "billing",
                            "lag": lag,
                            "lag_known": lag is not None,
                            "source": "static",
                            "cache_key": "kafka:x",
                        }
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
        # Offline the broken state is served by construction, so there is nothing to
        # establish — and an offline suite must never need a platform to run.
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

        "The fault was never manufactured" is a claim about the world and needs the world
        to have answered; reporting a dead platform as one sends the reader to seeding.
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

        One malformed text block ended the run as a crash bucketed "transport", losing
        both the failing probe and the fact that it was a precondition.
        """
        garbage = ToolResult(content=[{"type": "text", "text": "<html>502 Bad Gateway</html>"}])
        client = _ClosableCanned({"get_consumer_lag": garbage})
        with pytest.raises(runner_module.PreconditionUnverifiable, match="readable"):
            self._run_live(monkeypatch, self._live_scenario_with_precondition(), client)

    def test_a_dead_platform_on_the_decisive_attempt_is_unverifiable_not_unmet(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A platform that dies mid-polling is UNKNOWN, not FALSE.

        `answered` latched True on the first readable payload while `failures` was
        overwritten each attempt, so [readable-but-unmet, dead platform] reported the
        transport error under "the fault was never manufactured" — inside a committed
        append-only artifact, at the cost of a paid re-run.
        """
        monkeypatch.setattr(time, "sleep", lambda _s: None)
        client = _ScriptedCanned({"get_consumer_lag": self._lag_result(0)}, dead_on={2})
        scenario = self._live_scenario_with_precondition(attempts=2, delay_seconds=1.0)
        with pytest.raises(runner_module.PreconditionUnverifiable) as caught:
            self._run_live(monkeypatch, scenario, client)
        message = str(caught.value)
        assert "connection reset by peer" in message
        assert "UNKNOWN" in message
        assert "never manufactured" not in message

    def test_the_last_reading_still_decides_when_the_window_merely_expires(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The other half of "the decisive attempt decides": every attempt was readable, so
        # this IS a claim about the world, and it quotes the LAST reading.
        monkeypatch.setattr(time, "sleep", lambda _s: None)
        client = _ClosableCanned({"get_consumer_lag": [self._lag_result(0), self._lag_result(-1)]})
        scenario = self._live_scenario_with_precondition(attempts=2, delay_seconds=1.0)
        with pytest.raises(runner_module.PreconditionNotMet) as caught:
            self._run_live(monkeypatch, scenario, client)
        message = str(caught.value)
        assert "never manufactured" in message
        assert "observed [-1]" in message, "reported a stale reading, not the last one"
        assert "observed [0]" not in message

    def test_a_platform_that_comes_back_before_the_window_closes_is_unmet(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Symmetry check: transport failure FIRST, readable-but-unmet LAST. The decisive
        # attempt answered, so the premise really is false.
        monkeypatch.setattr(time, "sleep", lambda _s: None)
        client = _ScriptedCanned({"get_consumer_lag": self._lag_result(0)}, dead_on={1})
        scenario = self._live_scenario_with_precondition(attempts=2, delay_seconds=1.0)
        with pytest.raises(runner_module.PreconditionNotMet, match="never manufactured"):
            self._run_live(monkeypatch, scenario, client)

    def test_a_world_that_answers_falsely_is_still_unmet(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The other side of the pair: the platform answered, the premise is
        # false, and that IS a claim about the world.
        client = _ClosableCanned({"get_consumer_lag": self._lag_result(0)})
        with pytest.raises(runner_module.PreconditionNotMet, match="never manufactured"):
            self._run_live(monkeypatch, self._live_scenario_with_precondition(), client)


# The two independent faults the WP-1.2 fixture manufactures: a dead consumer (lag
# climbs) and a poisoned cache key (size is the chaos write's own number, not the
# seeded fixture's 120 — docs/eval-methodology.md).
_TWO_FAULT_GROUP: Final = "billing"
_TWO_FAULT_KEY: Final = "cache:jobs:worker-dispatcher:hot_set"
_TWO_FAULT_CHAOS_SIZE: Final = 90
_TWO_FAULT_SEEDED_SIZE: Final = 120


class _OrderedCanned(_ClosableCanned):
    """A live-path client that appends every call to a shared ordering list.

    The only way to state WP-1.2's acceptance as one assertion: the probes and the
    first model call are made by different objects, so "both faults proven before the
    first model call" is a claim about the sequence they share.
    """

    def __init__(self, responses: Any, order: list[str]) -> None:
        super().__init__(responses)
        self._order = order

    def call_tool(
        self,
        name: str,
        arguments: Any,
        *,
        timeout_seconds: float | None = None,
        lab_probe: str | None = None,
        lab_principal_token: str | None = None,
    ) -> ToolResult:
        self._order.append(f"tool:{name}")
        return super().call_tool(
            name,
            arguments,
            timeout_seconds=timeout_seconds,
            lab_probe=lab_probe,
            lab_principal_token=lab_principal_token,
        )


class TestTwoFaultPreconditions:
    """WP-1.2: a two-fault scenario proves BOTH faults before the first model call.

    Nothing here is new machinery, which is the point of the packet: what did not
    exist was the PROOF for a world with more than one fault, since every precondition
    test above drives a single probe and the shipped multi-probe scenarios only run
    live. So: a synthetic two-fault scenario, one read-only probe per fault, driven
    through the live-MCP path with a fake platform. A fixture rather than a canned
    YAML because a canned run ignores preconditions entirely.
    """

    @staticmethod
    def _two_fault_scenario(
        *,
        lag_probe: dict[str, Any] | None = None,
        cache_probe: dict[str, Any] | None = None,
    ) -> Scenario:
        """Two faults, two hooks, one read-only probe each, in declared order."""
        return _passing_scenario().model_copy(
            update={
                "name": "two_fault_probe",
                "use_live_mcp": True,
                "chaos_plan": ChaosPlan(
                    setup=(
                        ChaosHook(
                            name="kill_consumer",
                            arguments={"consumer_group": _TWO_FAULT_GROUP},
                        ),
                        ChaosHook(name="create_stale_cache", arguments={"key": _TWO_FAULT_KEY}),
                    ),
                ),
                "expected_precondition": (
                    PreconditionProbe(
                        tool="get_consumer_lag",
                        arguments={"consumer_group": _TWO_FAULT_GROUP},
                        expect=(PreconditionField(path="lag", at_least=1),),
                        **(lag_probe or {}),
                    ),
                    PreconditionProbe(
                        tool="get_cache_key_info",
                        arguments={"key": _TWO_FAULT_KEY},
                        expect=(PreconditionField(path="size", equals=_TWO_FAULT_CHAOS_SIZE),),
                        **(cache_probe or {}),
                    ),
                ),
            }
        )

    @staticmethod
    def _lag(lag: int) -> ToolResult:
        return ToolResult(
            content=[
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "consumer_group": _TWO_FAULT_GROUP,
                            "lag": lag,
                            "lag_known": True,
                            "source": "static",
                            "cache_key": "kafka:x",
                        }
                    ),
                }
            ]
        )

    @staticmethod
    def _cache(size: int) -> ToolResult:
        return ToolResult(
            content=[
                {
                    "type": "text",
                    "text": json.dumps({"key": _TWO_FAULT_KEY, "exists": True, "size": size}),
                }
            ]
        )

    @staticmethod
    def _record_llm(monkeypatch: pytest.MonkeyPatch, order: list[str]) -> list[CannedLLMClient]:
        """Every canned LLM client built, and an ``llm`` mark on every call."""
        built: list[CannedLLMClient] = []

        class _Recording(CannedLLMClient):
            def __init__(self, payloads: list[dict[str, Any]]) -> None:
                super().__init__(payloads)
                built.append(self)

            def call[T: BaseModel](
                self,
                system_prompt: str,
                user_message: str,
                output_model: type[T],
                model: str,
                max_tokens: int = 4096,
                *,
                repair_of: str | None = None,
                temperature: float | None = None,
            ) -> LLMResult[T]:
                order.append("llm")
                return super().call(
                    system_prompt,
                    user_message,
                    output_model,
                    model,
                    max_tokens,
                    repair_of=repair_of,
                )

        monkeypatch.setattr(runner_module, "CannedLLMClient", _Recording)
        return built

    def _run(
        self,
        monkeypatch: pytest.MonkeyPatch,
        scenario: Scenario,
        responses: dict[str, Any],
    ) -> tuple[list[str], list[CannedLLMClient], ScenarioResult | None]:
        """Seed, probe and run against a fake platform; return the ordering.

        ``None`` for the result when the premise was false — the caller is inside
        ``pytest.raises`` and wants the ordering, not a grade that does not exist.
        """
        order: list[str] = []
        built = self._record_llm(monkeypatch, order)

        def _fake_invoke(
            _url: str, _token: str, name: str, _arguments: dict[str, Any]
        ) -> dict[str, Any]:
            order.append(f"hook:{name}")
            return {"seeded": name}

        monkeypatch.setattr(runner_module, "invoke_chaos_hook", _fake_invoke)
        monkeypatch.setattr(
            runner_module, "make_client", lambda *_a, **_kw: _OrderedCanned(responses, order)
        )
        result = run_scenario(
            scenario, _test_settings(platform_mcp_url="http://real.host:8001/mcp")
        )
        return order, built, result

    def test_both_faults_are_proven_before_the_first_model_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Plan 04 § WP-1.2's acceptance, verbatim, in one ordering assertion."""
        order, built, result = self._run(
            monkeypatch,
            self._two_fault_scenario(),
            {
                "get_consumer_lag": self._lag(4200),
                "get_cache_key_info": self._cache(_TWO_FAULT_CHAOS_SIZE),
            },
        )
        assert result is not None and result.outcome.report.passed
        # Seed both faults in declared order, then prove both in declared
        # order, and only then spend a token.
        assert order[:4] == [
            "hook:kill_consumer",
            "hook:create_stale_cache",
            "tool:get_consumer_lag",
            "tool:get_cache_key_info",
        ]
        assert "llm" in order, "the agent never ran, so the ordering proves nothing"
        assert order.index("llm") == 4
        # Both probes are the harness's reads, not the agent's: neither is
        # charged to the run's tool-call budget.
        assert result.outcome.tool_calls_used == 1
        assert sum(len(client.calls) for client in built) == len(
            [mark for mark in order if mark == "llm"]
        )

    def test_an_unmet_first_fault_abandons_the_run_and_names_only_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The consumer is alive, so the world is not the one being graded."""
        with pytest.raises(runner_module.PreconditionNotMet) as caught:
            self._run(
                monkeypatch,
                self._two_fault_scenario(),
                {
                    "get_consumer_lag": self._lag(0),
                    "get_cache_key_info": self._cache(_TWO_FAULT_CHAOS_SIZE),
                },
            )
        message = str(caught.value)
        assert "never manufactured" in message
        assert "get_consumer_lag: lag expected at_least 1.0, observed [0]" in message
        # Which premise failed, and — just as important — which did not. The
        # second fault's tool must not appear in a message about the first.
        assert "get_cache_key_info" not in message

    def test_an_unmet_second_fault_abandons_the_run_and_names_only_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The cache key is there but carries the seeded size, not the chaos write.

        The half a single-probe suite cannot see: the first premise holds, so reaching
        this failure requires the loop to keep going and the message to name the SECOND
        probe.
        """
        with pytest.raises(runner_module.PreconditionNotMet) as caught:
            self._run(
                monkeypatch,
                self._two_fault_scenario(),
                {
                    "get_consumer_lag": self._lag(4200),
                    "get_cache_key_info": self._cache(_TWO_FAULT_SEEDED_SIZE),
                },
            )
        message = str(caught.value)
        assert "never manufactured" in message
        assert "get_cache_key_info: size expected equals 90, observed [120]" in message
        assert "get_consumer_lag" not in message

    def test_a_false_premise_in_either_fault_costs_no_model_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The cost argument, for a multi-fault world: still zero either way.

        Asserted on the LLM fakes' own call counts rather than the exception text, because
        the text is what a message change may edit.
        """
        worlds = (
            {
                "get_consumer_lag": self._lag(0),
                "get_cache_key_info": self._cache(_TWO_FAULT_CHAOS_SIZE),
            },
            {
                "get_consumer_lag": self._lag(4200),
                "get_cache_key_info": self._cache(_TWO_FAULT_SEEDED_SIZE),
            },
        )
        for responses in worlds:
            order: list[str] = []
            built = self._record_llm(monkeypatch, order)

            def _fake_invoke(
                _url: str, _token: str, name: str, _arguments: dict[str, Any]
            ) -> dict[str, Any]:
                return {"seeded": name}

            monkeypatch.setattr(runner_module, "invoke_chaos_hook", _fake_invoke)
            monkeypatch.setattr(
                runner_module,
                "make_client",
                lambda *_a, _responses=responses, _order=order, **_kw: _OrderedCanned(
                    _responses, _order
                ),
            )
            with pytest.raises(runner_module.PreconditionNotMet):
                run_scenario(
                    self._two_fault_scenario(),
                    _test_settings(platform_mcp_url="http://real.host:8001/mcp"),
                )
            assert "llm" not in order
            assert all(not client.calls for client in built), "a model ran on a false premise"

    def test_each_fault_polls_on_its_own_attempts_and_delay(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Attempt/delay semantics are per probe, and unchanged by there being two.

        The two faults land on different clocks — a killed consumer's lag trails the
        metrics interval, a cache write is visible at once — so a shared polling budget
        would be wrong for one of them by construction.
        """
        slept: list[float] = []
        monkeypatch.setattr(time, "sleep", slept.append)
        scenario = self._two_fault_scenario(
            lag_probe={"attempts": 3, "delay_seconds": 15.0},
            cache_probe={"attempts": 2, "delay_seconds": 5.0},
        )
        order, _built, result = self._run(
            monkeypatch,
            scenario,
            {
                "get_consumer_lag": [self._lag(0), self._lag(0), self._lag(9000)],
                "get_cache_key_info": [
                    self._cache(_TWO_FAULT_SEEDED_SIZE),
                    self._cache(_TWO_FAULT_CHAOS_SIZE),
                ],
            },
        )
        assert result is not None and result.outcome.report.passed
        # Two waits on the first probe's clock, then one on the second's.
        # A shared budget would read [15.0, 15.0] or [15.0, 15.0, 15.0].
        assert slept == [15.0, 15.0, 5.0]
        assert order[:5] == [
            "hook:kill_consumer",
            "hook:create_stale_cache",
            "tool:get_consumer_lag",
            "tool:get_consumer_lag",
            "tool:get_consumer_lag",
        ]
        assert order.index("llm") > order.index("tool:get_cache_key_info")

    def test_a_dead_platform_on_the_second_fault_is_unverifiable_not_unmet(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The not-met / unverifiable split is per probe, not per scenario.

        A met first premise must not latch "the world answered" for the second; getting
        this wrong is what once let a dead platform report as "the fault was never
        manufactured".
        """
        with pytest.raises(runner_module.PreconditionUnverifiable) as caught:
            self._run(
                monkeypatch,
                self._two_fault_scenario(),
                # No canned answer for the second probe's tool => MCPError.
                {"get_consumer_lag": self._lag(4200)},
            )
        message = str(caught.value)
        assert "UNKNOWN" in message
        assert "get_cache_key_info" in message
        assert "never manufactured" not in message


class TestTheShippedDualFaultPreconditions(TestTwoFaultPreconditions):
    """The same acceptance, on the SHIPPED worlds rather than a synthetic one.

    WO-R3-184 proved the machinery with a fixture because no scenario had two faults;
    WO-R3-228 shipped two, so the claim can now be made about the corpus. Inherits the
    seeding/ordering harness above and swaps the scenario: these are canned worlds, so
    the live flag is flipped for the drive and nothing else about them is touched.
    """

    #: The dual-fault world driven end to end here. Two probes, two tools, and its second
    #: premise is a reading rather than a hook's own write, which is the harder half.
    NAME: Final = "dual_fault_consumer_lag_and_bad_deploy"
    GROUP: Final = "worker-dispatcher"

    @staticmethod
    def _shipped_dual_fault() -> list[Scenario]:
        corpus = load_scenarios(Path(__file__).resolve().parents[2] / "evals" / "scenarios")
        return [s for s in corpus if s.difficulty is ScenarioDifficulty.MULTI_FAULT]

    @classmethod
    def _live(cls) -> Scenario:
        scenario = next(s for s in cls._shipped_dual_fault() if s.name == cls.NAME)
        return scenario.model_copy(update={"use_live_mcp": True})

    @pytest.fixture(autouse=True)
    def _no_real_sleeping(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The shipped lag probe polls 10 x 15s; a real wait would be 2.5 minutes a case.

        Autouse rather than per-test because every drive in this class goes through the
        same probe, and the polling budget itself is asserted from the scenario below.
        """
        monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    @staticmethod
    def _deploys(note: str | None) -> ToolResult:
        """The deploy history, with or without the annotation that IS the second fault."""
        return ToolResult(
            content=[
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "total": 1,
                            "source": "deploy_markers",
                            "entries": [
                                {
                                    "version": "v0.4.2",
                                    "revision": "c9f4d02",
                                    "image_tag": "v0.4.2",
                                    "deployed_at": "2026-07-28T06:00:00Z",
                                    "environment": "prod",
                                    "notes": note,
                                }
                            ],
                        }
                    ),
                }
            ]
        )

    @classmethod
    def _lag(cls, lag: int) -> ToolResult:
        return ToolResult(
            content=[
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "consumer_group": cls.GROUP,
                            "lag": lag,
                            "lag_known": True,
                            "source": "live",
                            "cache_key": f"kafka:consumer_lag:{cls.GROUP}",
                        }
                    ),
                }
            ]
        )

    def test_the_corpus_has_dual_fault_worlds_and_each_proves_every_fault(self) -> None:
        """One probe per fault, and no fault proven by another fault's probe.

        The shape claim, over the corpus rather than over one file: a world with two
        faults and one precondition is a world where half the premise is assumed.
        """
        shipped = self._shipped_dual_fault()
        assert len(shipped) >= 2, "no multi_fault scenario shipped — the sweep is vacuous"
        for scenario in shipped:
            assert scenario.ground_truth is not None
            faults = len(scenario.ground_truth.root_causes)
            probes = scenario.expected_precondition
            assert len(probes) == faults, (
                f"{scenario.name} names {faults} root cause(s) and declares "
                f"{len(probes)} precondition probe(s). One per fault: a fault nobody "
                "probes is a premise the run assumes."
            )
            tools = [probe.tool for probe in probes]
            assert len(set(tools)) == len(tools), (
                f"{scenario.name} proves two faults through {tools} — one read cannot "
                "establish two independent faults, so one of them is unproven."
            )
            assert scenario.ground_truth.incident_count == faults

    def test_both_faults_are_proven_before_the_first_model_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Seed, then prove both, and only then spend a token — on a shipped world."""
        order, _built, _result = self._run(
            monkeypatch,
            self._live(),
            {
                "get_consumer_lag": self._lag(33400),
                "get_deploy_history": self._deploys("correlated with billing failures"),
                "restart_consumer_group": ToolResult(
                    content=[{"type": "text", "text": json.dumps({"accepted": True})}]
                ),
            },
        )
        assert order[:3] == [
            "hook:kill_consumer",
            "tool:get_consumer_lag",
            "tool:get_deploy_history",
        ]
        assert "llm" in order, "the agent never ran, so the ordering proves nothing"
        assert order.index("llm") == 3

    def test_an_unmet_first_fault_abandons_the_run_and_names_only_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The group is draining, so the world is not the one this scenario grades."""
        with pytest.raises(runner_module.PreconditionNotMet) as caught:
            self._run(
                monkeypatch,
                self._live(),
                {
                    "get_consumer_lag": self._lag(0),
                    "get_deploy_history": self._deploys("correlated with billing failures"),
                },
            )
        message = str(caught.value)
        assert "never manufactured" in message
        assert "get_consumer_lag: lag expected at_least 20.0, observed [0]" in message
        assert "get_deploy_history" not in message

    def test_an_unmet_second_fault_abandons_the_run_and_names_only_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The release is there and NOT annotated: one fault, not two.

        The half that matters for a dual fault whose second premise is furniture the
        world already holds — an unannotated marker is a deploy, not a regression, and a
        run graded on two causes in that world would be graded on a fault nobody made.
        """
        with pytest.raises(runner_module.PreconditionNotMet) as caught:
            self._run(
                monkeypatch,
                self._live(),
                {
                    "get_consumer_lag": self._lag(33400),
                    "get_deploy_history": self._deploys(None),
                },
            )
        message = str(caught.value)
        assert "never manufactured" in message
        assert "get_deploy_history" in message
        assert "get_consumer_lag" not in message

    def test_a_false_premise_in_either_fault_costs_no_model_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Zero either way, on the shipped world, asserted on the fakes' own counts."""
        worlds = (
            {
                "get_consumer_lag": self._lag(0),
                "get_deploy_history": self._deploys("correlated with billing failures"),
            },
            {
                "get_consumer_lag": self._lag(33400),
                "get_deploy_history": self._deploys(None),
            },
        )
        for responses in worlds:
            order: list[str] = []
            built = self._record_llm(monkeypatch, order)
            monkeypatch.setattr(
                runner_module,
                "invoke_chaos_hook",
                lambda _url, _token, name, _arguments: {"seeded": name},
            )
            monkeypatch.setattr(
                runner_module,
                "make_client",
                lambda *_a, _responses=responses, _order=order, **_kw: _OrderedCanned(
                    _responses, _order
                ),
            )
            with pytest.raises(runner_module.PreconditionNotMet):
                run_scenario(
                    self._live(), _test_settings(platform_mcp_url="http://real.host:8001/mcp")
                )
            assert "llm" not in order
            assert all(not client.calls for client in built), "a model ran on a false premise"

    def test_each_fault_polls_on_its_own_attempts_and_delay(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The shipped world's own budgets: the lag polls, the deploy marker does not.

        A killed consumer's backlog trails the platform's metrics interval and a deploy
        marker is written the moment the release lands, so a shared polling budget would
        be wrong for one of them by construction.
        """
        scenario = self._live()
        lag_probe, deploy_probe = scenario.expected_precondition
        assert lag_probe.tool == "get_consumer_lag"
        assert (lag_probe.attempts, lag_probe.delay_seconds) == (10, 15.0)
        assert deploy_probe.tool == "get_deploy_history"
        assert (deploy_probe.attempts, deploy_probe.delay_seconds) == (1, 0.0)


class TestLiveRequiresAnExplicitSelection:
    """A bare ``--live`` is the whole suite, and must be refused as such.

    It always LOOKED refused, by the exit-8 canned-only gate — but that is a property
    of ``evals/scenarios/`` rather than of the invocation, and the message names the
    wrong problem. The Makefile's `ifndef ONLY` says the same thing one layer out;
    this is the backstop, since `python -m evals.runner --live` never comes through make.
    """

    def test_live_without_only_is_refused_before_the_scenario_tree_is_read(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Both stubs explode, so passing proves the refusal is structural: it precedes the
        # settings load AND the scenario load, the two things the old incidental refusal
        # depended on.
        def _boom(*_a: Any, **_k: Any) -> Any:
            raise AssertionError("the missing-filter refusal must precede this")

        monkeypatch.setattr(runner_module, "_settings_for_mode", _boom)
        monkeypatch.setattr(runner_module, "load_scenarios", _boom)
        _forbid_run_all(monkeypatch)
        monkeypatch.setattr(sys, "argv", ["evals.runner", "--live"])
        assert runner_module.main() == 2
        out = capsys.readouterr().out
        assert "LIVE FAIL: --live requires --only" in out
        assert "no scenarios ran, nothing was spent" in out
        # A refusal that does not hand over the runnable form gets worked around.
        assert "make eval-live ONLY=" in out

    def test_smoke_is_exempt(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # `--live --smoke` derives its own selection (WO-R2-123), so a bare one is not an
        # unfiltered run — it must fail on its own terms rather than for a missing --only.
        _isolate_settings_env(monkeypatch, tmp_path, _PLACEHOLDER_LIVE_ENV)
        _forbid_run_all(monkeypatch)
        monkeypatch.setattr(sys, "argv", ["evals.runner", "--live", "--smoke"])
        assert runner_module.main() == 3
        assert "LIVE FAIL: --live requires --only" not in capsys.readouterr().out

    def test_an_offline_run_still_needs_no_only(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The guard is about spend, and an offline run has none. Refusing here would take out
        # `make eval-reg` and `make baseline`, which forbid ONLY so they gate on the suite.
        _isolate_settings_env(monkeypatch, tmp_path)
        seen: list[Path] = []

        def _record(directory: Path) -> list[Scenario]:
            seen.append(directory)
            return [_passing_scenario()]

        monkeypatch.setattr(runner_module, "load_scenarios", _record)
        _stub_run_pipeline(monkeypatch, tmp_path)
        monkeypatch.setattr(sys, "argv", ["evals.runner"])
        assert runner_module.main() == 0
        assert seen, "an offline run with no --only must still reach the scenario load"


class TestLiveOnlyMatchesByFullScenarioName:
    """``--only`` was an unanchored substring, and silently widened selections.

    ``ONLY=dlq_backlog`` took ``remediate_dlq_backlog_success`` too; the read-only one
    runs first and drains the seeded replay_safe pool the remediation is graded on, so
    a correct agent reds. ADR 0020 cannot catch it — only one of the two mutates.
    Scoped to the spend path: ``--smoke`` and offline keep substring matching.
    """

    def _select(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *args: str
    ) -> tuple[int, list[str]]:
        """Run main() over the REAL scenario tree and report what it selected.

        The real tree is the subject: the widening is a property of the actual names, so a
        synthetic pair would test the matcher against itself.
        """
        _isolate_settings_env(monkeypatch, tmp_path, _REAL_LOOKING_LIVE_ENV)
        monkeypatch.setattr(runner_module, "preflight_auth", lambda _key: None)
        _stub_principal_probe(monkeypatch)
        calls = _stub_run_pipeline(monkeypatch, tmp_path)
        monkeypatch.setattr(sys, "argv", ["evals.runner", *args])
        code = runner_module.main()
        selected = [s.name for call in calls for s in call["args"][0]]
        return code, sorted(selected)

    def test_a_name_that_is_a_prefix_of_another_selects_only_itself(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # RED BEFORE: this selected both dlq_backlog and remediate_dlq_backlog_success.
        # Exact match comes FIRST for this case — refusing `dlq_backlog` as ambiguous would
        # make that scenario unrunnable live forever.
        code, selected = self._select(monkeypatch, tmp_path, "--live", "--only", "dlq_backlog")
        assert code == 0
        assert selected == ["dlq_backlog"]

    def test_a_substring_pattern_is_refused_and_names_what_it_would_have_taken(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # `dlq_` is not a scenario, so there is no exact match to prefer. Never silently
        # widen — and never merely refuse: name the candidates, or the operator retries blind.
        code, selected = self._select(monkeypatch, tmp_path, "--live", "--only", "dlq_")
        assert code == 2
        assert selected == [], "refusal must precede the run boundary"
        out = capsys.readouterr().out
        assert "SELECTION FAIL: 1 --only pattern(s) are not scenario names: dlq_" in out
        assert "Did you mean:" in out
        for name in ("dlq_backlog", "dlq_mixed_partial", "remediate_dlq_backlog_success"):
            assert f"--only dlq_ → {name}" in out
        assert "no scenarios ran, nothing was spent" in out

    def test_the_positive_control_selects_exactly_one(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The runbook's own form for every live remediation step.
        code, selected = self._select(
            monkeypatch, tmp_path, "--live", "--only", "remediate_dlq_backlog_success"
        )
        assert code == 0
        assert selected == ["remediate_dlq_backlog_success"]

    def test_a_comma_list_of_exact_names_still_selects_all_of_them(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Exact-match is per pattern, not a one-scenario cap: ADR 0020 is what limits a live
        # selection to one MUTATING scenario, and it has to stay the thing that says so.
        code, selected = self._select(
            monkeypatch, tmp_path, "--live", "--only", "dlq_backlog,noise_info_orders"
        )
        assert code == 0
        assert selected == ["dlq_backlog", "noise_info_orders"]

    def test_a_dead_pattern_keeps_its_own_refusal(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # #151's message must survive: a pattern matching nothing is a renamed or deleted
        # scenario, which is a different repair from one that matches too much, so it must
        # not be swallowed by the new message even though both exit 2.
        code, _ = self._select(
            monkeypatch, tmp_path, "--live", "--only", "dlq_backlog,scenario_renamed_away"
        )
        assert code == 2
        out = capsys.readouterr().out
        assert (
            "SELECTION FAIL: 1 --only pattern(s) matched no scenario: scenario_renamed_away" in out
        )
        assert "renamed" in out

    def test_smoke_keeps_substring_matching(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # SMOKE_ONLY is a documented substring override and reaches the runner as --only.
        # Applying the exact-name rule would break an operator control for no gain: the
        # read-only stage neither spends on Tier-1 actions nor shares mutable state.
        _isolate_settings_env(monkeypatch, tmp_path, _REAL_LOOKING_LIVE_ENV)
        monkeypatch.setattr(runner_module, "preflight_auth", lambda _key: None)
        monkeypatch.setattr(
            runner_module, "assert_read_only_principal", lambda _client, **_kwargs: None
        )
        monkeypatch.setattr(
            runner_module, "assert_no_tier1_successes", lambda _client, _since, **_kw: None
        )
        _stub_principal_probe(monkeypatch)
        calls = _stub_run_pipeline(monkeypatch, tmp_path)
        monkeypatch.setattr(sys, "argv", ["evals.runner", "--live", "--smoke", "--only", "noise_"])
        assert runner_module.main() == 0
        selected = [s.name for call in calls for s in call["args"][0]]
        assert len(selected) > 1, "--smoke must keep substring matching; SMOKE_ONLY needs it"
        assert "SELECTION FAIL" not in capsys.readouterr().out

    def test_an_offline_run_keeps_substring_matching(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _isolate_settings_env(monkeypatch, tmp_path)
        calls = _stub_run_pipeline(monkeypatch, tmp_path)
        monkeypatch.setattr(sys, "argv", ["evals.runner", "--only", "dlq_backlog"])
        assert runner_module.main() == 0
        selected = [s.name for call in calls for s in call["args"][0]]
        assert sorted(selected) == ["dlq_backlog", "remediate_dlq_backlog_success"], (
            "offline --only keeps substring matching — no spend, no shared platform, "
            "and eval-reg/baseline refuse ONLY outright so nothing downstream reads "
            "a widened offline selection"
        )


class TestLiveRefusesABatchOfMutatingScenarios:
    """ADR 0020: one state-mutating scenario per live invocation, enforced.

    Nine remediation scenarios share ONE platform with no reset between them, and the
    seeded pool carries one replay_safe row two of them both consume — so in a single
    invocation a CORRECT agent greens one and reds the other, and the report blames
    the agent.
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
            # An allowed selection runs past the ADR 0020 gate into the principal guards, which
            # build a client from PLATFORM_MCP_URL. Unstubbed, this fired a real Tier-1-capable
            # tools/call from the unit suite (WO-R2-35).
            _stub_principal_probe(monkeypatch)
            _stub_run_pipeline(monkeypatch, tmp_path)
        # Every scenario named exactly: --live selects by full name, and a comma list of
        # exact names is how a multi-scenario selection is even expressible.
        monkeypatch.setattr(
            sys,
            "argv",
            ["evals.runner", "--live", "--only", ",".join(s.name for s in scenarios)],
        )
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

    @staticmethod
    def _two_hook_plan_scenario(name: str) -> Scenario:
        base = _passing_scenario()
        return base.model_copy(
            update={
                "name": name,
                "use_live_mcp": True,
                "expectation": base.expectation.model_copy(update={"name": name}),
                "chaos_plan": ChaosPlan(
                    setup=(
                        ChaosHook(
                            name="kill_consumer",
                            arguments={"consumer_group": "worker-dispatcher"},
                        ),
                        ChaosHook(name="create_stale_cache", arguments={"key": "kafka:lag"}),
                    ),
                    teardown=(ChaosHook(name="saturate_redis"),),
                ),
            }
        )

    def test_a_two_hook_plan_is_one_scenario_and_still_runs(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """ADR 0020 is UNCHANGED by ChaosPlan: two hooks, still one scenario.

        Plan 01 § 4's closing line. A gate counting hooks rather than scenarios would make
        every multi-fault world unrunnable the day it landed.
        """
        code = self._run_main(
            monkeypatch,
            tmp_path,
            [self._two_hook_plan_scenario("cascade_a")],
            expect_refusal=False,
        )
        assert code != 7

    def test_a_two_hook_plan_still_counts_as_mutating(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The regression this gate would have taken silently.

        ``chaos_setup`` is None on a plan-declaring scenario, so a gate reading the legacy
        field counts a two-fault seeding scenario as read-only and lets it be selected
        beside a remediation scenario.
        """
        code = self._run_main(
            monkeypatch,
            tmp_path,
            [self._two_hook_plan_scenario("cascade_a"), self._mutating("remediate_a")],
            expect_refusal=True,
        )
        assert code == 7
        out = capsys.readouterr().out
        assert "cascade_a" in out
        assert "nothing was spent" in out

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


class TestLiveRefusesCannedOnlySelection:
    """A canned-only scenario in a --live selection is refused: exit 8.

    The flags being false is a statement about the WORLD, not the env — the platform
    cannot manufacture the fault (alert_storm needs many alerts in a short window and
    the three producers each emit one; remediate_verify_fails needs a consumer group
    that stays dead). Without the gate ``run_scenario`` serves the canned fixtures and
    the row lands in the live report's pass count as if the world had been graded.
    """

    _CANNED_ONLY_SHIPPED = (
        "alert_storm",
        "remediate_verify_fails",
    )

    @staticmethod
    def _forbid_clients_and_hooks(monkeypatch: pytest.MonkeyPatch) -> None:
        # The refusal must precede every guard, preflight, and chaos hook —
        # nothing may touch the platform on a refused selection.
        def _boom(*_a: Any, **_kw: Any) -> Any:
            raise AssertionError("a refused selection must not touch the platform")

        monkeypatch.setattr(runner_module, "make_client", _boom)
        monkeypatch.setattr(runner_module, "preflight_auth", _boom)
        monkeypatch.setattr(runner_module, "invoke_chaos_hook", _boom)

    @pytest.mark.parametrize("name", _CANNED_ONLY_SHIPPED)
    def test_shipped_canned_only_scenario_selected_live_is_refused(
        self,
        name: str,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Real shipped corpus, real-looking env: this pins BOTH halves, the YAML marker and
        # the runner gate that honors it. At HEAD those selections ran canned to exit 0
        # inside a "live" invocation.
        _isolate_settings_env(monkeypatch, tmp_path, _REAL_LOOKING_LIVE_ENV)
        _forbid_run_all(monkeypatch)
        self._forbid_clients_and_hooks(monkeypatch)
        monkeypatch.setattr(sys, "argv", ["evals.runner", "--live", "--only", name])
        assert runner_module.main() == 8
        out = capsys.readouterr().out
        assert "LIVE FAIL" in out
        assert name in out
        assert f"make eval ONLY={name}" in out
        assert "no scenarios ran, nothing was spent" in out

    def test_the_refusal_is_knowable_without_touching_the_env(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Placeholder env: a wrong SELECTION outranks a wrong env (the ADR 0020 precedent).
        # Without the gate this invocation sails past the degraded fail-fast — canned is a
        # canned-only scenario's intended mode — and runs canned to exit 0.
        _isolate_settings_env(monkeypatch, tmp_path, _PLACEHOLDER_LIVE_ENV)
        _forbid_run_all(monkeypatch)
        self._forbid_clients_and_hooks(monkeypatch)
        monkeypatch.setattr(sys, "argv", ["evals.runner", "--live", "--only", "alert_storm"])
        assert runner_module.main() == 8

    def test_smoke_may_still_select_canned_only_scenarios(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The exemption, pinned: SMOKE_ONLY deliberately includes canned-only
        # harness-sanity scenarios and the smoke report mixes canned and live rows by
        # design. Gating smoke would gut that stage.
        _isolate_settings_env(monkeypatch, tmp_path, _REAL_LOOKING_LIVE_ENV)
        _stub_run_pipeline(monkeypatch, tmp_path)
        monkeypatch.setattr(runner_module, "load_scenarios", lambda _d: [_passing_scenario()])
        monkeypatch.setattr(runner_module, "preflight_auth", lambda _key: None)
        monkeypatch.setattr(runner_module, "make_client", lambda *_a, **_kw: _StubGuardClient())
        monkeypatch.setattr(
            runner_module, "assert_read_only_principal", lambda _client, **_kwargs: None
        )
        monkeypatch.setattr(
            runner_module, "assert_no_tier1_successes", lambda _client, _since, **_kw: None
        )
        monkeypatch.setattr(sys, "argv", ["evals.runner", "--live", "--smoke"])
        assert runner_module.main() == 0

    def test_a_live_declaring_selection_is_not_refused(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The gate keys on the declared flags, not on a name prefix.
        live_scenario = _passing_scenario().model_copy(update={"use_live_mcp": True})
        _isolate_settings_env(monkeypatch, tmp_path, _REAL_LOOKING_LIVE_ENV)
        _stub_run_pipeline(monkeypatch, tmp_path)
        monkeypatch.setattr(runner_module, "load_scenarios", lambda _d: [live_scenario])
        monkeypatch.setattr(runner_module, "make_client", lambda *_a, **_kw: _StubGuardClient())
        monkeypatch.setattr(sys, "argv", ["evals.runner", "--live", "--only", "consumer_lag_pass"])
        assert runner_module.main() == 0

    def test_canned_only_is_the_absence_of_every_live_leg(self) -> None:
        # The marker is derived from the existing flags, not a parallel
        # field that could drift from them.
        base = _passing_scenario()
        assert base.canned_only is True
        assert base.model_copy(update={"use_live_mcp": True}).canned_only is False
        assert base.model_copy(update={"use_live_llm": True}).canned_only is False


class TestCrashedRowsKeepTheirProvenance:
    """A crashed row must not claim it ran canned.

    live_mcp/live_llm defaulted to False on the synthesized outcome, so every crashed
    row in a live report described itself as offline — and `degraded` False alongside
    said that was intended.
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

    Every principal check was gated on `smoke`, and a remediation stage under a
    read-scoped token does not fail fast: each scenario attempts its action, is
    refused, and grades red after full spend.
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
        monkeypatch.setattr(sys, "argv", ["evals.runner", "--live", "--only", "remediate_a"])
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

    def test_a_write_capable_token_that_cannot_seed_proceeds(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The post-v0.6.5 agent principal: the Tier-1 probe is refused on its arguments (it
        # can act) and the chaos probe on scope (it cannot seed, so the platform withholds
        # the chaos audit rows).
        assert self._main(monkeypatch, tmp_path, _split_agent_probe(monkeypatch)) != 4

    def test_a_token_that_can_also_seed_chaos_is_refused_before_any_spend(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The leak this whole packet closes, caught at the guard.

        A principal refused on ARGUMENTS by both probes is the pre-split four-scope token:
        it can act, and because it can fire the lab the platform serves it audit rows
        naming the hook seconds before the alert. Every diagnosis claim on such a run is
        unfalsifiable, so it is refused as hard as a token that cannot act at all.
        """
        probe = _ClosableCanned({})
        monkeypatch.setattr(
            probe,
            "call_tool",
            lambda *a, **k: (_ for _ in ()).throw(MCPError(-32602, "invalid tool arguments")),
        )
        assert self._main(monkeypatch, tmp_path, probe) == 4
        out = capsys.readouterr().out
        assert "nothing was spent" in out
        assert "chaos:invoke" in out

    def test_a_read_only_live_selection_is_not_guarded(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # No expected_action_tools means nothing will be executed, so there is no write
        # scope to require — and demanding one would break the read-only live stage.
        _isolate_settings_env(monkeypatch, tmp_path, _REAL_LOOKING_LIVE_ENV)
        base = _passing_scenario()
        read_only = base.model_copy(update={"name": "read_only", "use_live_mcp": True})
        monkeypatch.setattr(runner_module, "load_scenarios", lambda _d: [read_only])

        def _never(*_a: Any, **_k: Any) -> Any:
            raise AssertionError("the write guard must not probe a read-only selection")

        monkeypatch.setattr(runner_module, "assert_write_capable_principal", _never)
        _stub_run_pipeline(monkeypatch, tmp_path)
        monkeypatch.setattr(sys, "argv", ["evals.runner", "--live", "--only", "read_only"])
        assert runner_module.main() != 4


class TestLiveChaosSeedingGuardsTheChaosScope:
    """A scenario that mutates only through ``chaos_setup`` ran unguarded.

    The write guard is keyed on ``expected_action_tools`` and a chaos-only scenario
    declares none, so the one check that could have caught a wrong token skipped it
    and the run reached ``run_scenario``, which fires the hook under
    ``settings.platform_token`` — raising mid-run, after the archive is open. The scope
    it needs is ``chaos:invoke``: probing for write scope would pass a token that
    cannot seed and refuse one that can.
    """

    @staticmethod
    def _chaos_only(name: str = "seeds_chaos") -> Scenario:
        base = _passing_scenario()
        return base.model_copy(
            update={
                "name": name,
                "use_live_mcp": True,
                "chaos_setup": ChaosHook(
                    name="kill_consumer", arguments={"consumer_group": "worker-dispatcher"}
                ),
                "expectation": base.expectation.model_copy(update={"name": name}),
            }
        )

    def _main(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        scenarios: list[Scenario],
        probe_error: Exception | None = None,
    ) -> tuple[int, list[str]]:
        _isolate_settings_env(monkeypatch, tmp_path, _REAL_LOOKING_LIVE_ENV)
        monkeypatch.setattr(runner_module, "load_scenarios", lambda _d: scenarios)
        probed = _stub_principal_probe(monkeypatch, probe_error)
        _stub_run_pipeline(monkeypatch, tmp_path)
        monkeypatch.setattr(
            sys,
            "argv",
            ["evals.runner", "--live", "--only", ",".join(s.name for s in scenarios)],
        )
        return runner_module.main(), probed

    def test_a_chaos_only_selection_without_chaos_scope_is_refused_before_spend(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code, _ = self._main(
            monkeypatch,
            tmp_path,
            [self._chaos_only()],
            MCPError(-32002, "missing required scope: chaos:invoke"),
        )
        assert code == 4
        out = capsys.readouterr().out
        assert "nothing was spent" in out
        assert "chaos:invoke" in out

    def test_a_chaos_capable_token_proceeds(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        code, _ = self._main(monkeypatch, tmp_path, [self._chaos_only()])
        assert code != 4

    def test_the_chaos_scope_is_the_one_probed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The naive fix — reuse the Tier-1 probe — asks about actions:execute
        # for a scenario that executes no Tier-1 action.
        def _never(*_a: Any, **_k: Any) -> Any:
            raise AssertionError("a chaos-only selection must not be probed for write scope")

        monkeypatch.setattr(runner_module, "assert_write_capable_principal", _never)
        _, probed = self._main(monkeypatch, tmp_path, [self._chaos_only()])
        # The SAME hook fired twice at two principals with opposite expectations: the
        # agent's token must be refused on scope, the evaluator's must get past it. A
        # chaos-only scenario has an agent in it too, and the audit leak does not care that
        # nothing was remediated.
        assert probed == [guards_module._CHAOS_PROBE_TOOL, guards_module._CHAOS_PROBE_TOOL]

    def test_a_scenario_that_both_seeds_and_acts_is_probed_for_both(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        base = self._chaos_only("seeds_and_acts")
        both = base.model_copy(
            update={
                "expectation": base.expectation.model_copy(
                    update={"expected_action_tools": ("restart_consumer_group",)}
                )
            }
        )
        _, probed = self._main(monkeypatch, tmp_path, [both])
        # Three probes, two principals: the agent must be able to act and
        # unable to seed; the evaluator must be able to seed.
        assert probed == [
            guards_module._PROBE_TOOL,
            guards_module._CHAOS_PROBE_TOOL,
            guards_module._CHAOS_PROBE_TOOL,
        ]

    def test_a_selection_that_seeds_nothing_is_not_chaos_guarded(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Requiring chaos:invoke from the read-only live stage would refuse a
        # selection that is entitled to run.
        def _never(*_a: Any, **_k: Any) -> Any:
            raise AssertionError("the chaos guard must not probe a chaos-free selection")

        monkeypatch.setattr(runner_module, "assert_chaos_capable_principal", _never)
        base = _passing_scenario()
        read_only = base.model_copy(update={"name": "read_only", "use_live_mcp": True})
        code, _ = self._main(monkeypatch, tmp_path, [read_only])
        assert code != 4

    def test_an_offline_platform_is_not_chaos_guarded(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # A placeholder platform has no principal to verify, exactly as for
        # the other two guards.
        def _never(*_a: Any, **_k: Any) -> Any:
            raise AssertionError("no principal to guard against a placeholder platform")

        monkeypatch.setattr(runner_module, "assert_chaos_capable_principal", _never)
        _isolate_settings_env(monkeypatch, tmp_path, _PLACEHOLDER_LIVE_ENV)
        monkeypatch.setattr(runner_module, "load_scenarios", lambda _d: [self._chaos_only()])
        _stub_run_pipeline(monkeypatch, tmp_path)
        monkeypatch.setattr(sys, "argv", ["evals.runner"])
        assert runner_module.main() != 4


class TestVerificationJudgeIsPinned:
    """The verdict that decides RESOLVED must not ride the unpinned model.

    JUDGE_MODEL is pinned separately from AGENT_MODEL so results stay comparable
    across a model-pin change. The briefing judge used it; the verification judge —
    whose verdict decides RESOLVED — did not.
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
        settings = _test_settings(agent_model="claude-sonnet-4-6", judge_model="claude-haiku-4-5")
        run_scenario(_passing_scenario(), settings)
        assert seen["model"] == "claude-haiku-4-5"

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
        settings = _test_settings(agent_model="claude-sonnet-4-6", judge_model="claude-haiku-4-5")
        run_scenario(_passing_scenario(), settings)
        assert seen["model"] == "claude-sonnet-4-6"


class TestACrashedRowReportsWhatItSpent:
    """A crash after N tool calls must report N, not zero.

    ``_crashed_result`` hardcoded ``tool_calls_used=0`` because the exception was all
    ``run_all``'s handler could reach, so every crashed row claimed the scenario spent
    nothing — and the report is the artifact the cost columns are read from.
    """

    def test_a_crash_after_tool_calls_reports_them(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Crash AFTER the loop has run and checkpointed: the briefing step
        # is downstream of every tool call the scenario makes.
        def _boom(_final: Any) -> Any:
            raise RuntimeError("briefing exploded")

        monkeypatch.setattr(runner_module, "render_briefing", _boom)
        report, trajectories, _ = run_all([_passing_scenario()], _test_settings())
        outcome = report.outcomes[0]
        assert not outcome.report.passed
        assert outcome.tool_calls_used > 0
        assert outcome.final_state is not IncidentState.TRIAGE
        # The run's evidence survives the crash rather than being replaced
        # by a nil-id placeholder.
        assert trajectories[0].checkpoints
        assert trajectories[0].incident_id != "00000000-0000-0000-0000-000000000000"

    def test_the_crash_row_still_names_the_original_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The wrapper is a carrier, not a new failure mode."""

        def _boom(_final: Any) -> Any:
            raise RuntimeError("briefing exploded")

        monkeypatch.setattr(runner_module, "render_briefing", _boom)
        report, _, _ = run_all([_passing_scenario()], _test_settings())
        detail = report.outcomes[0].report.dimensions[0].detail
        assert "RuntimeError: briefing exploded" in detail
        assert "ScenarioCrash" not in detail

    def test_a_crash_before_the_run_starts_still_reports_zero(self) -> None:
        """Nothing ran, so nothing is claimed — the honest zero stays."""
        result = _crashed_result(_passing_scenario(), RuntimeError("boom"))
        assert result.outcome.tool_calls_used == 0
        assert result.outcome.final_state is IncidentState.TRIAGE
        assert result.outcome.failure_class == "transport"


class _StubAuditClient:
    """A client that answers the audit read with an empty, conclusive page."""

    def __init__(self) -> None:
        self.audit_reads = 0
        #: The lab label each checkpoint carried (WO-R3-335); None with no credential.
        self.labels: list[tuple[str | None, str | None]] = []

    def call_tool(
        self,
        name: str,
        arguments: Any,
        *,
        timeout_seconds: float | None = None,
        lab_probe: str | None = None,
        lab_principal_token: str | None = None,
    ) -> ToolResult:
        self.audit_reads += 1
        self.labels.append((lab_probe, lab_principal_token))
        return ToolResult(
            content=[{"type": "text", "text": json.dumps({"total": 0, "events": []})}]
        )

    def close(self) -> None:
        return None


class TestPostStageAuditIsCheckpointed:
    """B2: the post-stage audit reads the window while the stage runs.

    ``list_audit_events`` has no offset and no created_after, so once the stage ends
    the newest 200 rows are all there will ever be — and a louder smoke stage used to
    exit 5 "inconclusive", a false red on a paid run.
    """

    def test_a_checkpoint_is_taken_after_every_scenario(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _isolate_settings_env(monkeypatch, tmp_path, _REAL_LOOKING_LIVE_ENV)
        _stub_run_pipeline(monkeypatch, tmp_path)
        audit_client = _StubAuditClient()
        scans: list[Any] = []

        def _run_all_firing_two_scenarios(*_args: Any, **kwargs: Any) -> Any:
            on_result = kwargs["on_result"]
            on_result(None)
            on_result(None)
            return _green_stub_report(), (), ()

        monkeypatch.setattr(runner_module, "run_all", _run_all_firing_two_scenarios)
        monkeypatch.setattr(runner_module, "archive_scenario", lambda *_a, **_kw: None)
        monkeypatch.setattr(runner_module, "preflight_auth", lambda _key: None)
        monkeypatch.setattr(runner_module, "make_client", lambda *_a, **_kw: audit_client)
        monkeypatch.setattr(
            runner_module, "assert_read_only_principal", lambda _client, **_kwargs: None
        )
        monkeypatch.setattr(
            runner_module,
            "assert_no_tier1_successes",
            lambda _client, _since, **kwargs: scans.append(kwargs["scan"]),
        )
        monkeypatch.setattr(sys, "argv", ["evals.runner", "--live", "--smoke", *_SMOKE_ONLY_ARGS])
        assert runner_module.main() == 0
        # Two scenarios finished, so two pages were banked mid-stage. The
        # third read is the assertion's own, which is stubbed out here.
        assert audit_client.audit_reads == 2
        assert len(scans) == 1
        assert scans[0].checkpoints == 2
        assert scans[0].since is not None

    def test_a_failing_checkpoint_does_not_abort_the_stage(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A checkpoint is best-effort: losing one only narrows coverage, which fails closed
        # at the assertion, and aborting a paid stage over a transient read would be worse.
        # But it must be said out loud, or a broken checkpoint silently degrades the guard.
        _isolate_settings_env(monkeypatch, tmp_path, _REAL_LOOKING_LIVE_ENV)
        _stub_run_pipeline(monkeypatch, tmp_path)

        class _AngryClient(_StubAuditClient):
            def call_tool(
                self,
                name: str,
                arguments: Any,
                *,
                timeout_seconds: float | None = None,
                lab_probe: str | None = None,
                lab_principal_token: str | None = None,
            ) -> ToolResult:
                raise MCPError(-32603, "audit is having a moment")

        def _run_all_firing_one_scenario(*_args: Any, **kwargs: Any) -> Any:
            kwargs["on_result"](None)
            return _green_stub_report(), (), ()

        monkeypatch.setattr(runner_module, "run_all", _run_all_firing_one_scenario)
        monkeypatch.setattr(runner_module, "archive_scenario", lambda *_a, **_kw: None)
        monkeypatch.setattr(runner_module, "preflight_auth", lambda _key: None)
        monkeypatch.setattr(runner_module, "make_client", lambda *_a, **_kw: _AngryClient())
        monkeypatch.setattr(
            runner_module, "assert_read_only_principal", lambda _client, **_kwargs: None
        )
        monkeypatch.setattr(runner_module, "assert_no_tier1_successes", lambda *_a, **_kw: None)
        monkeypatch.setattr(sys, "argv", ["evals.runner", "--live", "--smoke", *_SMOKE_ONLY_ARGS])
        assert runner_module.main() == 0
        assert "checkpoint skipped" in capsys.readouterr().out


class TestChaosPlanRunnerSemantics:
    """WP-1.1: setup in order, settle, preconditions, run, teardown in finally.

    Plan 01 § 4's nine steps, and the two failure semantics that make them worth the
    machinery: a failed SETUP means the world is invalid, so the agent is not graded;
    a failed TEARDOWN leaves a valid grade beside a contaminated environment.
    """

    @staticmethod
    def _live_settings() -> Settings:
        return _test_settings(platform_mcp_url="http://real.host:8001/mcp")

    @staticmethod
    def _plan_scenario(plan: ChaosPlan, **extra: Any) -> Scenario:
        base = _passing_scenario()
        return base.model_copy(
            update={"name": "plan_probe", "use_live_mcp": True, "chaos_plan": plan, **extra}
        )

    @staticmethod
    def _two_hook_plan(**extra: Any) -> ChaosPlan:
        return ChaosPlan(
            setup=(
                ChaosHook(name="kill_consumer", arguments={"consumer_group": "worker-dispatcher"}),
                ChaosHook(name="create_stale_cache", arguments={"key": "kafka:lag"}),
            ),
            teardown=(ChaosHook(name="saturate_redis", arguments={"num_keys": 0}),),
            **extra,
        )

    @staticmethod
    def _record_hooks(
        monkeypatch: pytest.MonkeyPatch,
        failures: dict[str, str] | None = None,
    ) -> list[str]:
        """Capture hook names in fire order; raise for any name in ``failures``."""
        fired: list[str] = []

        def _fake_invoke(
            _url: str, _token: str, name: str, _arguments: dict[str, Any]
        ) -> dict[str, Any]:
            fired.append(name)
            if failures and name in failures:
                raise ChaosInvocationError(failures[name])
            return {"seeded": name}

        monkeypatch.setattr(runner_module, "invoke_chaos_hook", _fake_invoke)
        return fired

    @staticmethod
    def _stub_live_client(monkeypatch: pytest.MonkeyPatch, scenario: Scenario) -> None:
        monkeypatch.setattr(
            runner_module,
            "make_client",
            lambda *_a, **_kw: _ClosableCanned(scenario.canned_tool_responses),
        )

    @pytest.fixture(autouse=True)
    def _isolated_block(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        # The teardown latch is a real file at a module constant: without redirecting it,
        # one failing-teardown test would block live runs in the developer's own checkout.
        monkeypatch.setattr(runner_module, "_CHAOS_BLOCK_PATH", tmp_path / "block.json")

    # --- setup ---------------------------------------------------------

    def test_setup_hooks_fire_in_declared_order_before_the_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scenario = self._plan_scenario(self._two_hook_plan())
        fired = self._record_hooks(monkeypatch)
        self._stub_live_client(monkeypatch, scenario)
        result = run_scenario(scenario, self._live_settings())
        assert result.outcome.final_state is IncidentState.ESCALATED
        # Setup in declaration order, then teardown. Order is the scenario's
        # claim about its world, not an implementation detail.
        assert fired == ["kill_consumer", "create_stale_cache", "saturate_redis"]

    def test_setup_results_are_recorded_on_the_outcome(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scenario = self._plan_scenario(self._two_hook_plan())
        self._record_hooks(monkeypatch)
        self._stub_live_client(monkeypatch, scenario)
        result = run_scenario(scenario, self._live_settings())
        records = result.outcome.chaos_hooks
        assert [(r.phase, r.name, r.ok) for r in records] == [
            ("setup", "kill_consumer", True),
            ("setup", "create_stale_cache", True),
            ("teardown", "saturate_redis", True),
        ]
        # The hook's own response, so the archive can answer "what world was
        # this graded in?" without a trace file.
        assert records[0].result == {"seeded": "kill_consumer"}

    def test_a_canned_run_fires_no_hook_from_a_plan(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Offline placeholder URL: the canned responses already encode the
        # broken state, and teardown has nothing to compensate.
        scenario = self._plan_scenario(self._two_hook_plan())
        fired = self._record_hooks(monkeypatch)
        result = run_scenario(scenario, _test_settings())
        assert fired == []
        assert result.outcome.chaos_hooks == ()
        assert result.outcome.teardown_error is None

    def test_settle_seconds_is_waited_before_the_preconditions_look(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        order: list[str] = []

        def _fake_invoke(
            _url: str, _token: str, name: str, _arguments: dict[str, Any]
        ) -> dict[str, Any]:
            order.append(f"hook:{name}")
            return {}

        monkeypatch.setattr(runner_module, "invoke_chaos_hook", _fake_invoke)
        monkeypatch.setattr("evals.runner.time.sleep", lambda s: order.append(f"sleep:{s}"))
        scenario = self._plan_scenario(
            ChaosPlan(setup=(ChaosHook(name="saturate_redis"),), settle_seconds=7.5),
            expected_precondition=(
                PreconditionProbe(
                    tool="get_consumer_lag",
                    arguments={"consumer_group": "billing"},
                    expect=(PreconditionField(path="lag", equals=42),),
                ),
            ),
        )
        self._stub_live_client(monkeypatch, scenario)
        run_scenario(scenario, self._live_settings())
        # The whole ordering claim in one assertion: seed, settle, then look.
        assert order[:2] == ["hook:saturate_redis", "sleep:7.5"]

    def test_setup_failure_stops_at_the_failing_hook(self, monkeypatch: pytest.MonkeyPatch) -> None:
        scenario = self._plan_scenario(self._two_hook_plan())
        fired = self._record_hooks(
            monkeypatch, failures={"kill_consumer": "poison_fixture_name_in_use (-32011: no)"}
        )
        self._stub_live_client(monkeypatch, scenario)
        with pytest.raises(ChaosSetupFailed) as excinfo:
            run_scenario(scenario, self._live_settings())
        # The second setup hook would have seeded into a world that does not
        # exist yet, so it must not have fired. Teardown still did.
        assert fired == ["kill_consumer", "saturate_redis"]
        message = str(excinfo.value)
        assert "kill_consumer" in message
        assert "setup hook 1 of 2" in message
        # The platform's own refusal NAME, not a bare JSON-RPC code: the
        # difference between "reset the world" and "retry" (chaos_hooks.py).
        assert "poison_fixture_name_in_use" in message

    def test_a_legacy_chaos_setup_failure_is_a_chaos_setup_failed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The legacy spelling runs the same path, and the message keeps the
        # `chaos_setup <hook>` shape the runbook and the crash bucketing read.
        base = _passing_scenario()
        scenario = base.model_copy(
            update={
                "name": "chaos_probe",
                "use_live_mcp": True,
                "chaos_setup": ChaosHook(
                    name="inject_latency",
                    arguments={"consumer_group": "wd", "latency_ms": 2000},
                ),
            }
        )
        self._record_hooks(monkeypatch, failures={"inject_latency": "platform said no"})
        with pytest.raises(ChaosSetupFailed, match="chaos_probe.*inject_latency.*platform said no"):
            run_scenario(scenario, self._live_settings())

    def test_a_setup_failure_produces_no_grade_report(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The packet's sharpest requirement: not "it failed", but "no grade".

        A crash row would carry a GradeReport saying the scenario failed, and every rate
        derived from the report would describe the agent using a pre-run event.
        """
        scenario = self._plan_scenario(ChaosPlan(setup=(ChaosHook(name="saturate_redis"),)))
        self._record_hooks(monkeypatch, failures={"saturate_redis": "platform said no"})
        report, trajectories, briefings = run_all([scenario], self._live_settings())
        assert report.outcomes == ()
        assert trajectories == ()
        assert briefings == ()
        assert report.total == 0
        assert report.passed == 0
        assert report.failed == 0
        assert [(u.scenario, u.phase) for u in report.ungraded] == [("plan_probe", "chaos_setup")]
        assert "saturate_redis" in report.ungraded[0].reason

    def test_a_setup_failure_does_not_stop_the_rest_of_the_suite(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scenario = self._plan_scenario(ChaosPlan(setup=(ChaosHook(name="saturate_redis"),)))
        self._record_hooks(monkeypatch, failures={"saturate_redis": "platform said no"})
        report, _, _ = run_all([scenario, _passing_scenario()], self._live_settings())
        assert [o.scenario for o in report.outcomes] == ["consumer_lag_pass"]
        assert len(report.ungraded) == 1

    # --- teardown ------------------------------------------------------

    def test_teardown_runs_when_the_agent_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        scenario = self._plan_scenario(self._two_hook_plan())
        fired = self._record_hooks(monkeypatch)
        self._stub_live_client(monkeypatch, scenario)

        def _boom(*_a: Any, **_kw: Any) -> Any:
            raise RuntimeError("the agent loop died")

        monkeypatch.setattr(runner_module, "run_to_completion", _boom)
        with pytest.raises(runner_module.ScenarioCrash, match="the agent loop died"):
            run_scenario(scenario, self._live_settings())
        assert fired[-1] == "saturate_redis"

    def test_teardown_runs_when_a_precondition_is_unmet(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The world was seeded, so it has to be put back even though the run
        # was abandoned before the first model call.
        scenario = self._plan_scenario(
            ChaosPlan(
                setup=(ChaosHook(name="saturate_redis"),),
                teardown=(ChaosHook(name="bad_deploy", arguments={"label": "restore"}),),
            ),
            expected_precondition=(
                PreconditionProbe(
                    tool="get_consumer_lag",
                    arguments={"consumer_group": "billing"},
                    expect=(PreconditionField(path="lag", equals=999),),
                ),
            ),
        )
        fired = self._record_hooks(monkeypatch)
        self._stub_live_client(monkeypatch, scenario)
        with pytest.raises(runner_module.PreconditionNotMet):
            run_scenario(scenario, self._live_settings())
        assert fired == ["saturate_redis", "bad_deploy"]

    def test_teardown_failure_is_distinct_from_the_grade_and_sets_the_block(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        scenario = self._plan_scenario(self._two_hook_plan())
        self._record_hooks(monkeypatch, failures={"saturate_redis": "compensator refused"})
        self._stub_live_client(monkeypatch, scenario)
        result = run_scenario(scenario, self._live_settings(), invocation_id="abc123")
        # The RUN is untouched: it was graded on a valid world and it passed.
        assert result.outcome.report.passed is True
        assert result.outcome.failure_class == "passed"
        # The ENVIRONMENT is not.
        assert result.outcome.teardown_error is not None
        assert "saturate_redis" in result.outcome.teardown_error
        assert "compensator refused" in result.outcome.teardown_error
        assert runner_module.chaos_block_reason() is not None
        payload = json.loads((tmp_path / "block.json").read_text())
        assert payload["scenario"] == "plan_probe"
        assert payload["invocation_id"] == "abc123"

    def test_teardown_failure_beside_an_agent_crash_reports_both(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scenario = self._plan_scenario(self._two_hook_plan())
        self._record_hooks(monkeypatch, failures={"saturate_redis": "compensator refused"})
        self._stub_live_client(monkeypatch, scenario)

        def _boom(*_a: Any, **_kw: Any) -> Any:
            raise RuntimeError("the agent loop died")

        monkeypatch.setattr(runner_module, "run_to_completion", _boom)
        report, _, _ = run_all([scenario], self._live_settings())
        outcome = report.outcomes[0]
        # Two separate facts, neither swallowing the other.
        assert "the agent loop died" in outcome.report.dimensions[0].detail
        assert outcome.teardown_error is not None
        assert "compensator refused" in outcome.teardown_error
        assert report.contaminated_scenarios == ("plan_probe",)

    def test_a_clean_teardown_leaves_no_block(self, monkeypatch: pytest.MonkeyPatch) -> None:
        scenario = self._plan_scenario(self._two_hook_plan())
        self._record_hooks(monkeypatch)
        self._stub_live_client(monkeypatch, scenario)
        result = run_scenario(scenario, self._live_settings())
        assert result.outcome.teardown_error is None
        assert runner_module.chaos_block_reason() is None

    def test_every_teardown_hook_gets_its_turn_even_after_one_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Stopping at the first failure would skip compensators that might
        # still have worked, on the one path where that matters most.
        scenario = self._plan_scenario(
            ChaosPlan(
                setup=(ChaosHook(name="kill_consumer", arguments={"consumer_group": "wd"}),),
                teardown=(
                    ChaosHook(name="saturate_redis"),
                    ChaosHook(name="bad_deploy", arguments={"label": "restore"}),
                ),
            )
        )
        fired = self._record_hooks(monkeypatch, failures={"saturate_redis": "refused"})
        self._stub_live_client(monkeypatch, scenario)
        result = run_scenario(scenario, self._live_settings())
        assert fired == ["kill_consumer", "saturate_redis", "bad_deploy"]
        assert result.outcome.teardown_error is not None
        assert "1 of 2 hook(s)" in result.outcome.teardown_error


class TestChaosTeardownBlocksFurtherLiveRuns:
    """The latch: a failed teardown refuses the NEXT live invocation."""

    @pytest.fixture(autouse=True)
    def _isolated_block(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(runner_module, "_CHAOS_BLOCK_PATH", tmp_path / "block.json")

    def test_live_run_is_refused_while_the_block_is_set(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        runner_module.write_chaos_block("plan_probe", "saturate_redis: compensator refused")
        _isolate_settings_env(monkeypatch, tmp_path, _REAL_LOOKING_LIVE_ENV)
        _forbid_run_all(monkeypatch)

        def _boom(*_a: Any, **_kw: Any) -> Any:
            raise AssertionError("a blocked run must not touch the platform")

        monkeypatch.setattr(runner_module, "make_client", _boom)
        monkeypatch.setattr(runner_module, "preflight_auth", _boom)
        monkeypatch.setattr(runner_module, "invoke_chaos_hook", _boom)
        monkeypatch.setattr(sys, "argv", ["evals.runner", "--live", "--only", "dlq_backlog"])
        assert runner_module.main() == 10
        out = capsys.readouterr().out
        assert "CONTAMINATED WORLD" in out
        assert "compensator refused" in out
        # A refusal that does not hand over the next command gets worked
        # around — the ADR 0020 lesson, applied to this gate.
        assert "make eval-reset PURGE_IDEMPOTENCY=1" in out
        assert "--clear-chaos-block" in out
        # Since cmd #233 the reset's last recipe line clears the latch itself, so the advice
        # must not read as two steps the operator owes: a second command that is already run
        # for you is one that gets run out of order.
        assert "clears the block on success" in out
        assert "no stack left to reset" in out
        assert "nothing was spent" in out

    def test_offline_runs_are_not_blocked(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Canned scenarios share no world, so a dirty platform says nothing
        # about them — and the offline gate must keep running in CI.
        runner_module.write_chaos_block("plan_probe", "saturate_redis: compensator refused")
        _isolate_settings_env(monkeypatch, tmp_path, _PLACEHOLDER_LIVE_ENV)
        _stub_run_pipeline(monkeypatch, tmp_path)
        monkeypatch.setattr(sys, "argv", ["evals.runner"])
        assert runner_module.main() == 0

    def test_clear_chaos_block_drops_the_latch(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        runner_module.write_chaos_block("plan_probe", "saturate_redis: compensator refused")
        monkeypatch.setattr(sys, "argv", ["evals.runner", "--clear-chaos-block"])
        assert runner_module.main() == 0
        assert "compensator refused" in capsys.readouterr().out
        assert runner_module.chaos_block_reason() is None

    def test_clearing_an_unset_block_is_not_an_error(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(sys, "argv", ["evals.runner", "--clear-chaos-block"])
        assert runner_module.main() == 0
        assert "nothing to clear" in capsys.readouterr().out

    def test_an_unreadable_block_file_still_blocks(self, tmp_path: Path) -> None:
        # It can only exist because something wrote it, and "we cannot tell
        # what went wrong" is not a reason to carry on spending.
        (tmp_path / "block.json").write_text("{not json")
        assert runner_module.chaos_block_reason() is not None


def _never_stopping_scenario(name: str = "iteration_override_probe") -> Scenario:
    """A canned scenario whose planner probes forever and never stops.

    The loop's own iteration bound is the only thing that ends it, so the number of
    probes it got through IS the bound — which is the observable
    ``MAX_ITERATIONS_OVERRIDE`` has to move. Six planner responses and a ceiling of
    nine, so neither the queue nor the budget can end the run first.
    """
    probe = {
        "hypotheses": [
            {
                "category": "consumer_saturation",
                "name": "consumer_saturation",
                "confidence": 0.55,
                "reasoning": "Paging severity on the billing consumer.",
            }
        ],
        "next_action": {
            "kind": "probe",
            "tool_name": "get_consumer_lag",
            "arguments": {"consumer_group": "billing"},
        },
    }
    return Scenario(
        name=name,
        alert=AlertPayload(source="platform.kafka", severity="high", group="billing"),
        expectation=ScenarioExpectation(
            name=name,
            expected_terminal_state=IncidentState.ESCALATED,
            max_tool_calls=9,
        ),
        canned_tool_responses={
            "get_consumer_lag": ToolResult(
                content=[
                    {
                        "type": "text",
                        "text": (
                            '{"consumer_group":"billing","lag":42,"lag_known":true,'
                            '"source":"static",'
                            '"cache_key":"kafka:consumer_lag:worker-dispatcher"}'
                        ),
                    }
                ],
            )
        },
        canned_llm_responses={"investigation_planner": [dict(probe) for _ in range(6)]},
    )


class TestMaxIterationsOverrideReachesTheLoop:
    """WO-R3-256: WP-2.4's third knob had no consumer.

    ``MAX_ITERATIONS_OVERRIDE`` landed on ``Settings`` with cmd #251 while its one
    call site was owned by another packet, so the setting configured nothing —
    harmless while ``baseline`` is the only strategy, and wrong the moment a strategy
    needing another bound runs: the run would be measured under the fleet default
    while its provenance named a strategy that asked for something else.
    """

    def test_the_default_bound_is_five_planner_steps(self) -> None:
        """The control: unset means the loop's own default, unchanged."""
        result = run_scenario(_never_stopping_scenario(), _test_settings())
        assert result.outcome.tool_calls_used == 5

    def test_the_override_bounds_the_investigation_loop(self) -> None:
        result = run_scenario(
            _never_stopping_scenario(),
            _test_settings(max_iterations_override=2),
        )
        assert result.outcome.tool_calls_used == 2

    def test_the_override_can_widen_the_bound_too(self) -> None:
        """Not only downward: a strategy may need more steps than five."""
        result = run_scenario(
            _never_stopping_scenario(),
            _test_settings(max_iterations_override=6),
        )
        assert result.outcome.tool_calls_used == 6


class TestCrashPathLedgerIsSeededFromTheScaledCeilings:
    """WO-R3-256: a crash row must not report the UNSCALED ceilings.

    ``_crashed_result`` rebuilds the ledger a run "would have been seeded with" and
    read the configured ceilings, which are BEFORE WP-2.4's per-strategy multipliers —
    so under a non-1.0 multiplier the row named budgets no run would ever have had.
    """

    def _crash_budget(self, **overrides: Any) -> BudgetLedger:
        result = _crashed_result(
            _passing_scenario(),
            RuntimeError("platform unreachable"),
            settings=_test_settings(**overrides),
        )
        provenance = result.outcome.provenance
        assert provenance is not None
        return provenance.budget

    def test_a_doubled_token_ceiling_reaches_the_crash_row(self) -> None:
        budget = self._crash_budget(
            budget_max_tokens=500_000,
            token_budget_multiplier=Decimal("2"),
        )
        assert budget.max_tokens == 1_000_000

    def test_a_doubled_dollar_ceiling_reaches_the_crash_row(self) -> None:
        budget = self._crash_budget(
            budget_max_usd=Decimal("5.00"),
            usd_budget_multiplier=Decimal("2"),
        )
        assert budget.max_usd == Decimal("10.00")

    def test_the_unmultiplied_dimensions_are_untouched(self) -> None:
        """Tool calls and wall seconds are never scaled (WP-2.4)."""
        budget = self._crash_budget(
            budget_max_seconds=1_800,
            token_budget_multiplier=Decimal("2"),
            usd_budget_multiplier=Decimal("2"),
        )
        # The scenario's own declared cap, not the fleet default, and not
        # doubled (ADR 0019 + WP-2.4).
        assert budget.max_tool_calls == 5
        assert budget.max_wall_seconds == 1_800

    def test_the_baseline_multipliers_leave_the_row_where_it_was(self) -> None:
        budget = self._crash_budget(budget_max_tokens=500_000, budget_max_usd=Decimal("5.00"))
        assert budget.max_tokens == 500_000
        assert budget.max_usd == Decimal("5.00")


class TestTheWorldMayAlreadyBeFaulted:
    """ADR 0075 item 5: one fault, fired once, by whoever is driving the demo.

    The fourth take's audit stream holds ``chaos.tool_invoked kill_consumer`` twice — once from
    ``scripts/demo_live.py`` step 3 at 03:18:05, and again from this runner's own
    ``_seed_chaos_plan`` at 03:19:48 when step 5 started the agent. The world tolerates the
    repeat (the hook re-arms); the RECORD does not, because every reader that anchors on the
    take's newest chaos row then measures the demo from a re-arm 1 m 43 s late.
    """

    @staticmethod
    def _scenario() -> Scenario:
        """A live scenario with a hook to skip AND a premise to still check."""
        base = _passing_scenario()
        return base.model_copy(
            update={
                "name": "already_faulted_probe",
                "use_live_mcp": True,
                "chaos_setup": ChaosHook(
                    name="kill_consumer",
                    arguments={"consumer_group": "billing", "ttl_seconds": 300},
                ),
                "expected_precondition": (
                    PreconditionProbe(
                        tool="get_consumer_lag",
                        arguments={"consumer_group": "billing"},
                        expect=(PreconditionField(path="lag", at_least=20),),
                    ),
                ),
            }
        )

    @staticmethod
    def _no_chaos_may_fire(monkeypatch: pytest.MonkeyPatch) -> None:
        """Pin at the ChaosClient seam, not only at the runner's reference to it.

        ``invoke_chaos_hook`` builds a ``ChaosClient`` and calls it, so a path that reached the
        platform some other way would still pass a check on the runner's own name. This makes
        the transport itself the tripwire.
        """

        def _boom(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError(
                "a chaos hook was fired in --world-already-faulted mode: that is the second "
                "injection the fourth take's timeline was measured from"
            )

        monkeypatch.setattr(chaos_hooks_module.ChaosClient, "call", _boom)
        monkeypatch.setattr(chaos_hooks_module.ChaosClient, "__init__", _boom)

    def _live_client(self, monkeypatch: pytest.MonkeyPatch) -> _ClosableCanned:
        client = _ClosableCanned(self._scenario().canned_tool_responses)
        monkeypatch.setattr("evals.runner.make_client", lambda *_a, **_kw: client)
        return client

    def test_no_hook_fires_and_the_premise_is_still_checked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._no_chaos_may_fire(monkeypatch)
        client = self._live_client(monkeypatch)
        settings = _test_settings(platform_mcp_url="http://real.host:8001/mcp")

        result = run_scenario(self._scenario(), settings, world_already_faulted=True)

        assert result.outcome.final_state is IncidentState.ESCALATED
        # The premise WAS read — the flag says who seeded, not whether to check.
        assert ("get_consumer_lag", {"consumer_group": "billing"}) in client.calls

    def test_the_archive_names_who_seeded_instead(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._no_chaos_may_fire(monkeypatch)
        self._live_client(monkeypatch)
        settings = _test_settings(platform_mcp_url="http://real.host:8001/mcp")

        result = run_scenario(self._scenario(), settings, world_already_faulted=True)

        provenance = result.outcome.provenance
        assert provenance is not None
        assert provenance.chaos_seeded_by == EXTERNAL_CHAOS_SEEDER
        # So a grader counting the scenario's chaos rows knows the one row it finds sits
        # BEFORE this run rather than that a hook went missing.
        assert result.outcome.chaos_hooks == ()

    def test_the_default_still_seeds_and_names_nobody(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[str] = []

        def _fake_invoke(
            _url: str, _token: str, name: str, _arguments: dict[str, Any]
        ) -> dict[str, Any]:
            calls.append(name)
            return {"seeded": True}

        monkeypatch.setattr(runner_module, "invoke_chaos_hook", _fake_invoke)
        self._live_client(monkeypatch)
        settings = _test_settings(platform_mcp_url="http://real.host:8001/mcp")

        result = run_scenario(self._scenario(), settings)

        assert calls == ["kill_consumer"]
        provenance = result.outcome.provenance
        assert provenance is not None
        assert provenance.chaos_seeded_by is None

    def test_a_precondition_that_fails_is_still_a_precondition_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The premise is the one thing this mode does not get to answer."""
        self._no_chaos_may_fire(monkeypatch)
        healthy = _ClosableCanned(
            {
                "get_consumer_lag": ToolResult(
                    content=[
                        {
                            "type": "text",
                            "text": (
                                '{"consumer_group":"billing","lag":0,"lag_known":true,'
                                '"source":"live"}'
                            ),
                        }
                    ]
                )
            }
        )
        monkeypatch.setattr("evals.runner.make_client", lambda *_a, **_kw: healthy)
        settings = _test_settings(platform_mcp_url="http://real.host:8001/mcp")

        with pytest.raises(PreconditionNotMet):
            run_scenario(self._scenario(), settings, world_already_faulted=True)

    def test_the_root_cause_label_still_describes_this_world(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """INC-003's "live but nothing seeded" case is the SMOKE pass, not this one.

        Read off the grade rather than off the predicate: a run whose ROOT_CAUSE came back
        "not graded — a live run that seeded no fault" would be saying something false, and the
        flag would quietly change what the run measures instead of who fired the hook. The
        premise probes are what make it a verified fact (an unmet premise abandons the run).
        """
        self._no_chaos_may_fire(monkeypatch)
        self._live_client(monkeypatch)
        settings = _test_settings(platform_mcp_url="http://real.host:8001/mcp")

        result = run_scenario(self._scenario(), settings, world_already_faulted=True)

        root_cause = next(
            d for d in result.outcome.report.dimensions if d.dimension is GradeDimension.ROOT_CAUSE
        )
        assert not is_not_graded_detail(root_cause.detail), root_cause.detail

    def test_a_self_expiring_fault_may_not_be_seeded_elsewhere(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A temporal template's recovery clock is read off the seeding it would skip."""
        self._no_chaos_may_fire(monkeypatch)
        self._live_client(monkeypatch)
        temporal = self._scenario().model_copy(
            update={
                "chaos_setup": ChaosHook(
                    name="kill_consumer",
                    arguments={"consumer_group": "billing"},
                    ttl_from_windows=TtlFromWindows(
                        floor_seconds=60,
                        floor_reason="the lag metric refreshes on a 60-second interval",
                    ),
                )
            }
        )
        settings = _test_settings(platform_mcp_url="http://real.host:8001/mcp")

        with pytest.raises(ChaosSetupFailed, match="derived TTL"):
            run_scenario(temporal, settings, world_already_faulted=True)

    def test_the_flag_is_refused_with_a_recorded_run(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A replay seeds nothing, so there is no seeding for the flag to skip."""
        monkeypatch.setattr(
            sys,
            "argv",
            ["evals.runner", "--mode", "recorded", WORLD_ALREADY_FAULTED_FLAG, "--only", "x"],
        )
        assert runner_module.main() == 2
        out = capsys.readouterr().out
        assert WORLD_ALREADY_FAULTED_FLAG in out
        assert "no scenarios ran, nothing was spent" in out


def _platform_alert(
    *,
    fingerprint: str = "consumer_stalled",
    subject: dict[str, Any] | None = None,
    alert_id: str = "7a3e58d2-4b33-4349-9f03-b9c58a71c4f2",
) -> dict[str, Any]:
    """One row in the shape ``list_active_alerts`` returns for a rule the platform raised.

    Recorded from the v0.6.18 stack on 2026-09-21, then parameterised: the fingerprint and
    every subject field live inside ``extra_data``, the summary fields are repeated at the top
    level, and ``source`` reads ``kafka:consumer_lag`` — which is NOT the spelling the corpus
    uses, which is the point of several tests below.
    """
    payload: dict[str, Any] = {
        "fingerprint": fingerprint,
        "severity": "critical",
        "source": "kafka:consumer_lag",
        "lag": 41,
        "threshold": 20,
        "measured_at": "2026-09-21T07:26:12.306870+00:00",
        "summary": "worker-dispatcher is 41 messages behind (threshold 20).",
    }
    payload.update(subject or {"consumer_group": "billing", "group": "billing"})
    return {
        "id": alert_id,
        "severity": "critical",
        "source": "kafka:consumer_lag",
        "title": "Consumer lag on billing: 41 messages behind",
        "description": payload["summary"],
        "fired_at": "2026-09-21T07:26:12.308524Z",
        "extra_data": payload,
    }


class TestTheAlertMayComeFromThePlatform:
    """Owner decision O-36, platform ADR 0039: the agent is paged by the platform.

    The fourth take's agent triaged an alert its own scenario file had written, while the
    platform's stream read the same three seeded fixtures before, during and after the fault —
    ``list_active_alerts`` answered ``total: 3`` throughout. The middle clause of "jobs pile
    up, the platform pages, the agent responds" was a fixture.
    """

    @staticmethod
    def _scenario() -> Scenario:
        """A live scenario whose alert names a fingerprint AND a subject."""
        base = _passing_scenario()
        return base.model_copy(
            update={
                "name": "paged_by_the_platform",
                "use_live_mcp": True,
                "alert": AlertPayload(
                    source="platform.kafka",
                    severity="critical",
                    fingerprint="consumer_stalled",
                    group="billing",
                ),
            }
        )

    def _client(self, monkeypatch: pytest.MonkeyPatch, alerts: list[dict[str, Any]]) -> Any:
        """A canned client that also answers ``list_active_alerts`` with ``alerts``."""
        responses = dict(self._scenario().canned_tool_responses)
        responses["list_active_alerts"] = ToolResult(
            content=[
                {"type": "text", "text": json.dumps({"total": len(alerts), "alerts": alerts})}
            ],
            is_error=False,
        )
        client = _ClosableCanned(responses)
        monkeypatch.setattr("evals.runner.make_client", lambda *_a, **_kw: client)
        return client

    @staticmethod
    def _settings() -> Settings:
        return _test_settings(
            platform_mcp_url="http://real.host:8001/mcp",
            platform_smoke_token=SecretStr("eval-smoke"),
        )

    def test_the_run_starts_from_the_platforms_payload_verbatim(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        row = _platform_alert()
        self._client(monkeypatch, [row])

        result = run_scenario(self._scenario(), self._settings(), alert_from_platform=True)

        # The brief the agent read is the alert row's own `extra_data` — every key, no key
        # added, no key dropped. The scenario's `alert:` block is not merged in: a payload
        # this harness had a hand in assembling would be the old arrangement with a step.
        started = result.trajectory.checkpoints[0]
        assert started.alert == row["extra_data"]
        assert started.alert["source"] == "kafka:consumer_lag"

    def test_the_provenance_names_the_row_it_was_paged_with(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        row = _platform_alert()
        self._client(monkeypatch, [row])

        result = run_scenario(self._scenario(), self._settings(), alert_from_platform=True)

        provenance = result.outcome.provenance
        assert provenance is not None
        assert provenance.alert_source == PLATFORM_ALERT_SOURCE
        # So a report can be joined to the `alert.raised` audit row that started it, which is
        # the claim O-36 makes and the one thing a fingerprint cannot identify on its own.
        assert provenance.alert_id == row["id"]

    def test_the_default_is_the_scenarios_own_block_and_says_so(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._client(monkeypatch, [_platform_alert()])

        result = run_scenario(self._scenario(), self._settings())

        provenance = result.outcome.provenance
        assert provenance is not None
        assert (provenance.alert_source, provenance.alert_id) == (SCENARIO_ALERT_SOURCE, None)
        assert result.trajectory.checkpoints[0].alert["source"] == "platform.kafka"

    def test_the_match_ignores_source_and_reads_fingerprint_and_subject(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The platform's `source` is not the corpus's, and matching on it would find nothing.

        `kafka:consumer_lag` / `dlq:threshold` on the row against `platform.kafka` /
        `platform.dlq` in the YAML — platform ADR 0039's own divergence note. A run that keyed
        on source would wait out its whole bound with the right alert in front of it.
        """
        self._client(monkeypatch, [_platform_alert()])

        result = run_scenario(self._scenario(), self._settings(), alert_from_platform=True)

        assert result.outcome.provenance is not None
        assert result.outcome.provenance.alert_source == PLATFORM_ALERT_SOURCE

    def test_a_different_subject_is_not_this_scenarios_page(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same rule, another group. Matching on fingerprint alone would take it.

        This is the 2026-08-30 failure one field along: that run probed the default group
        while the alert named `unknown-consumer`, and only a VALUE comparison catches it.
        """
        self._client(
            monkeypatch,
            [
                _platform_alert(
                    subject={"consumer_group": "shipping-consumer", "group": "shipping-consumer"}
                )
            ],
        )
        monkeypatch.setattr(runner_module, "_PLATFORM_ALERT_TIMEOUT_SECONDS", 0.0)
        monkeypatch.setattr(runner_module, "_PLATFORM_ALERT_POLL_SECONDS", 0.0)

        with pytest.raises(ChaosSetupFailed, match="did not raise"):
            run_scenario(self._scenario(), self._settings(), alert_from_platform=True)

    def test_the_wait_is_bounded_and_fails_loudly_rather_than_forever(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The three seeded fixtures are always there, so "no alerts" is never the world: the
        # failure has to be "none of them is this one", with the count in the message.
        self._client(monkeypatch, [_platform_alert(fingerprint="job_dispatch_latency_fast_burn")])
        monkeypatch.setattr(runner_module, "_PLATFORM_ALERT_TIMEOUT_SECONDS", 0.0)
        monkeypatch.setattr(runner_module, "_PLATFORM_ALERT_POLL_SECONDS", 0.0)

        with pytest.raises(ChaosSetupFailed, match="1 active alert"):
            run_scenario(self._scenario(), self._settings(), alert_from_platform=True)

    def test_a_scenario_with_no_fingerprint_is_refused_rather_than_matched_loosely(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._client(monkeypatch, [_platform_alert()])
        nameless = self._scenario().model_copy(
            update={"alert": AlertPayload(source="platform.kafka", severity="critical")}
        )

        with pytest.raises(ChaosSetupFailed, match="no alert `fingerprint`"):
            run_scenario(nameless, self._settings(), alert_from_platform=True)

    def test_it_reads_under_the_smoke_principal_and_labels_the_probe(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ADR 0074 + platform ADR 0038: the evaluator's question, on a token that cannot act.

        An unlabelled read on the SMOKE account lands as `agent.tool_invoked` inside the take,
        and the demo page then counts it as a call the agent made and never reported (F4).
        """
        seen: list[dict[str, Any]] = []
        client = self._client(monkeypatch, [_platform_alert()])

        def _make(_settings: Settings, **kwargs: Any) -> Any:
            seen.append(kwargs)
            return client

        monkeypatch.setattr("evals.runner.make_client", _make)
        labels: list[tuple[str, str]] = []
        original = LabProbeClient.call_tool

        def _spy(self: Any, name: str, arguments: dict[str, Any], **kw: Any) -> ToolResult:
            labels.append((name, self.reason))
            return original(self, name, arguments, **kw)

        monkeypatch.setattr(LabProbeClient, "call_tool", _spy)

        run_scenario(self._scenario(), self._settings(), alert_from_platform=True)

        assert ("list_active_alerts", runner_module.PLATFORM_ALERT_PROBE_REASON) in labels
        assert any(kwargs.get("token") == "eval-smoke" for kwargs in seen)

    def test_an_unset_smoke_token_refuses_rather_than_using_the_agents(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._client(monkeypatch, [_platform_alert()])
        settings = _test_settings(platform_mcp_url="http://real.host:8001/mcp")

        with pytest.raises(ChaosSetupFailed, match="read-only principal"):
            run_scenario(self._scenario(), settings, alert_from_platform=True)

    def test_a_canned_run_refuses_rather_than_silently_doing_nothing(self) -> None:
        """The quiet failure this guard exists for: no platform, and a row that claims one."""
        canned = self._scenario().model_copy(update={"use_live_mcp": False})

        with pytest.raises(ValueError, match="needs a live platform"):
            run_scenario(canned, self._settings(), alert_from_platform=True)

    def test_a_recorded_run_refuses_at_the_function_and_at_the_cli(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(ValueError, match="no alert stream"):
            run_scenario(
                self._scenario(),
                self._settings(),
                recorded_world=Path("nowhere.json"),
                alert_from_platform=True,
            )
        monkeypatch.setattr(
            sys,
            "argv",
            ["evals.runner", "--mode", "recorded", ALERT_FROM_PLATFORM_FLAG, "--only", "x"],
        )
        assert runner_module.main() == 2
        out = capsys.readouterr().out
        assert ALERT_FROM_PLATFORM_FLAG in out
        assert "no scenarios ran, nothing was spent" in out

    def test_a_crash_before_the_page_does_not_claim_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The asymmetry with ``chaos_seeded_by``, which IS the caller's instruction.

        "A world somebody else broke" is true from the first line of the run; "the platform
        paged this run" is only true once a row came back. So a run that died in its seeding
        records ``scenario``, which is what happened.
        """

        def _boom(*_a: Any, **_kw: Any) -> Any:
            raise ChaosInvocationError("platform said no")

        monkeypatch.setattr("evals.runner.invoke_chaos_hook", _boom)
        seeded = self._scenario().model_copy(
            update={
                "chaos_setup": ChaosHook(
                    name="kill_consumer",
                    arguments={"consumer_group": "billing", "ttl_seconds": 300},
                )
            }
        )
        self._client(monkeypatch, [_platform_alert()])

        crashed = _crashed_result(
            seeded,
            _capture(lambda: run_scenario(seeded, self._settings(), alert_from_platform=True)),
            settings=self._settings(),
        )

        provenance = crashed.outcome.provenance
        assert provenance is not None
        assert (provenance.alert_source, provenance.alert_id) == (SCENARIO_ALERT_SOURCE, None)


def _capture(call: Any) -> BaseException:
    """Run ``call`` and hand back whatever it raised — the shape ``run_all`` passes on."""
    try:
        call()
    except BaseException as err:  # noqa: BLE001 - the exception IS the return value here
        return err
    raise AssertionError("expected the call to raise")
