"""`--mode rehearsal` (WO-R3-316, ADR 0069): the real platform, a scripted planner.

Recorded mode's mirror. A recording replaces the PLATFORM and keeps the model; a rehearsal
keeps the platform — real seeding, real preconditions, a real Tier-1 action — and replaces
the MODEL. It exists for the demo: `make demo-live` has to be walked end to end before it is
walked on camera, and doing that with a real model would spend money every take.

Two properties are load-bearing and everything here is about one or the other:

1. **No model call is possible.** Not "is not made" — cannot be made, from two directions
   at once: the settings carry the placeholder key, and the flag alone forces the canned
   client even if a real key reaches ``run_scenario``.
2. **No reader can count the row as live.** ``execution_mode`` is its own member,
   ``degraded`` is True, ``provenance.rehearsal`` is True, the report cannot mark itself
   closing, and the research and phase-close assemblers REFUSE such a row rather than
   averaging it in.

Everything here is hermetic and free: no platform is reached, no hook fires, no key is read.
"""

from __future__ import annotations

import os
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import pytest
from pydantic import SecretStr, ValidationError

from evals import phase_close_report as close
from evals import research_report as research
from evals import runner as runner_module
from evals.candidate_metrics import world_key
from evals.fakes import CannedMCPClient
from evals.graders.deterministic import GradeReport, ScenarioExpectation
from evals.runner import (
    REHEARSAL_MODE,
    ExecutionMode,
    RunReport,
    ScenarioOutcome,
    run_all,
    run_scenario,
)
from evals.scenarios.schema import Scenario
from incident_commander.agent.factory import start_run
from incident_commander.agent.state import IncidentState
from incident_commander.api.schemas import AlertPayload
from incident_commander.config import ModelRole, Settings, settings_env_var_names
from incident_commander.llm.fakes import CannedLLMClient
from incident_commander.tools.mcp_client import MCPError, ToolResult

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]

#: A real-looking env for a rehearsal: the platform half is real, and the key is a
#: real-SHAPED string on purpose — the claim under test is that it is never used.
_REAL_LOOKING_ENV: Final[dict[str, str]] = {
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

_OFFLINE_ENV: Final[dict[str, str]] = {
    **_REAL_LOOKING_ENV,
    "PLATFORM_MCP_URL": "https://eval.local",
    "PLATFORM_REST_URL": "https://eval.local",
}


class _ClosableCanned(CannedMCPClient):
    """CannedMCPClient with the ``close()`` the live path calls."""

    def close(self) -> None:  # pragma: no cover - no-op
        return None


def _settings(**overrides: Any) -> Settings:
    """Settings with a REAL-looking model key, which is the interesting case here."""
    defaults: dict[str, Any] = {
        "anthropic_api_key": SecretStr("sk-ant-test-not-a-real-key"),
        "judge_model": "claude-haiku-4-5",
        "platform_mcp_url": "https://eval.local",
        "platform_rest_url": "https://eval.local",
        "platform_token": SecretStr("eval"),
        "platform_chaos_token": SecretStr("eval-chaos"),
        "platform_webhook_secret": SecretStr("eval"),
        "database_url": "postgresql://eval:eval@localhost:5432/eval",
    }
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)  # type: ignore[call-arg]


def _scenario(name: str = "rehearsal_probe", **overrides: Any) -> Scenario:
    """A one-probe scenario that declares BOTH live legs, so both can be withheld."""
    base = Scenario(
        name=name,
        alert=AlertPayload(source="platform.kafka", severity="high", group="billing"),
        use_live_mcp=True,
        use_live_llm=True,
        expectation=ScenarioExpectation(
            name=name,
            expected_terminal_state=IncidentState.ESCALATED,
            expected_evidence_contains=("billing",),
            max_tool_calls=5,
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
                    "next_action": {"kind": "stop", "reason": "confidence sufficient"},
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
    return base.model_copy(update=overrides) if overrides else base


def _forbid_live_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make building a live LLM client the loudest possible failure."""

    def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("a live LLM client must not be built")

    monkeypatch.setattr(runner_module, "LLMClient", _boom)


def _isolate_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, env: dict[str, str] | None = None
) -> None:
    """chdir to a directory with no .env and reset every Settings variable."""
    monkeypatch.chdir(tmp_path)
    for var in settings_env_var_names():
        monkeypatch.delenv(var, raising=False)
    for var, value in (env or {}).items():
        monkeypatch.setenv(var, value)


def _forbid_run_all(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("run_all must not be reached")

    monkeypatch.setattr(runner_module, "run_all", _boom)


def _unlock_tree(root: Path) -> None:
    """Unlock an archive tree so pytest can clean ``tmp_path`` (test_runner's helper).

    A completed archive is locked read-only plus ``uchg`` (ADR 0021), which is the point —
    and it leaves pytest unable to remove its own temp directory. Flags clear before modes,
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


def _main(monkeypatch: pytest.MonkeyPatch, *argv: str) -> int:
    monkeypatch.setattr(sys, "argv", ["evals.runner", *argv])
    return runner_module.main()


def _rehearsal_outcome(scenario: str = "remediate_dlq_backlog_success") -> ScenarioOutcome:
    """A row shaped exactly as a rehearsal writes one, for the report readers."""
    provenance = runner_module.build_provenance(
        scenario,
        _settings(),
        model_role=ModelRole.BENCHMARK,
        invocation_id="rehearsal0001",
        execution_mode=ExecutionMode.REHEARSAL,
        budget=start_run(_scenario().agent_visible().alert, _settings(), datetime.now(UTC)).budget,
    )
    return ScenarioOutcome(
        scenario=scenario,
        final_state=IncidentState.RESOLVED,
        tool_calls_used=4,
        report=GradeReport(
            scenario=scenario,
            passed=True,
            dimensions=(),
        ),
        degraded=True,
        live_mcp=True,
        live_llm=False,
        provenance=provenance,
    )


# --------------------------------------------------------------------------
# 1. No model call is possible
# --------------------------------------------------------------------------


class TestTheModelLegIsScriptedWhateverTheKeyIs:
    """The flag alone is sufficient — the settings are belt, this is braces."""

    def test_no_live_llm_client_is_built_even_with_a_real_key_present(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _forbid_live_llm(monkeypatch)
        result = run_scenario(_scenario(), _settings(), rehearsal=True)

        assert result.outcome.live_llm is False

    def test_without_the_flag_the_same_settings_do_build_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The test above with its subject removed: it must fail, or it proves nothing.

        The same scenario, the same real-looking key, the same exploding stub — and the
        explosion happens. That is what makes the assertion above a claim about the mode
        rather than a claim about this scenario's fixtures.
        """
        _forbid_live_llm(monkeypatch)
        with pytest.raises(AssertionError, match="live LLM client"):
            run_scenario(_scenario(), _settings(), rehearsal=False)

    def test_every_role_gets_the_scenarios_scripted_client(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Not only the planner: the briefing writer and the judge are model calls too."""
        built: list[str] = []
        original = CannedLLMClient.__init__

        def _record(self: CannedLLMClient, *args: Any, **kwargs: Any) -> None:
            built.append("canned")
            original(self, *args, **kwargs)

        monkeypatch.setattr(CannedLLMClient, "__init__", _record)
        _forbid_live_llm(monkeypatch)
        run_scenario(_scenario(), _settings(), rehearsal=True)

        # Seven roles are wired in ``run_scenario``; the count is the claim that none of
        # them was left on the live branch.
        assert len(built) == 7

    def test_the_settings_for_the_mode_keep_the_platform_and_drop_the_key(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The other direction: the key never reaches the process's Settings at all."""
        _isolate_env(monkeypatch, tmp_path, _REAL_LOOKING_ENV)
        settings = runner_module._settings_for_mode(False, rehearsal=True)

        assert str(settings.platform_mcp_url) == _REAL_LOOKING_ENV["PLATFORM_MCP_URL"]
        assert settings.platform_token.get_secret_value() == _REAL_LOOKING_ENV["PLATFORM_TOKEN"]
        assert runner_module._is_offline_api_key(settings.anthropic_api_key.get_secret_value())
        # And the live branch is unchanged, which is the half a careless edit would break.
        assert not runner_module._is_offline_api_key(
            runner_module._settings_for_mode(True).anthropic_api_key.get_secret_value()
        )

    def test_a_recording_and_a_rehearsal_cannot_be_asked_for_together(self) -> None:
        with pytest.raises(ValueError, match="cannot be both"):
            run_scenario(
                _scenario(),
                _settings(),
                recorded_world=_REPO_ROOT / "evals" / "recorded_worlds",
                rehearsal=True,
            )


# --------------------------------------------------------------------------
# 2. No reader can count the row as live
# --------------------------------------------------------------------------


class TestTheRowSaysWhatItIs:
    def test_the_row_is_degraded_and_stamped_rehearsal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _forbid_live_llm(monkeypatch)
        outcome = run_scenario(_scenario(), _settings(), rehearsal=True).outcome

        assert outcome.degraded is True
        assert outcome.provenance is not None
        assert outcome.provenance.rehearsal is True
        assert outcome.provenance.execution_mode is ExecutionMode.REHEARSAL

    def test_a_scenario_declaring_no_live_model_leg_is_degraded_anyway(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``degraded`` is not a consequence of the canned planner here, it is the mode.

        A scenario with ``use_live_llm: false`` degrades nothing by running canned — that
        is its intended mode — so the old expression would have left this row reading
        ``degraded=False`` in a rehearsal archive: the one row a reader could mistake for a
        measurement.
        """
        _forbid_live_llm(monkeypatch)
        outcome = run_scenario(_scenario(use_live_llm=False), _settings(), rehearsal=True).outcome

        assert outcome.degraded is True

    def test_the_mode_is_rehearsal_even_when_the_platform_leg_is_really_live(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The case that would otherwise stamp ``LIVE``: a reachable platform.

        ``execution_mode`` is derived from what the legs DID, and a rehearsal's platform leg
        did run live — so without its own branch this row would have said ``live`` and every
        reader counting live rows would have counted it.
        """
        _forbid_live_llm(monkeypatch)
        monkeypatch.setattr(
            runner_module,
            "make_client",
            lambda *_a, **_kw: _ClosableCanned(_scenario().canned_tool_responses),
        )
        outcome = run_scenario(
            _scenario(),
            _settings(platform_mcp_url="http://real.host:8001/mcp"),
            rehearsal=True,
        ).outcome

        assert outcome.live_mcp is True
        assert outcome.provenance is not None
        assert outcome.provenance.execution_mode is ExecutionMode.REHEARSAL

    def test_a_crash_row_carries_the_mode_too(self) -> None:
        """A rehearsal that died before the first call is still not a measurement."""
        result = runner_module._crashed_result(
            _scenario(),
            RuntimeError("seeding refused"),
            "rehearsal0001",
            settings=_settings(),
            rehearsal=True,
        )

        assert result.outcome.degraded is True
        assert result.outcome.live_llm is False
        assert result.outcome.provenance is not None
        assert result.outcome.provenance.execution_mode is ExecutionMode.REHEARSAL
        assert result.outcome.provenance.rehearsal is True


class TestTheReportCannotCloseAnything:
    def test_a_rehearsal_suite_is_non_closing_under_the_benchmark_role(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The role says which model WOULD have been billed; a rehearsal billed none."""
        _forbid_live_llm(monkeypatch)
        report, _, _ = run_all(
            [_scenario()],
            _settings(),
            invocation_id="rehearsal0001",
            model_role=ModelRole.BENCHMARK,
            rehearsal=True,
        )

        assert report.closing is False
        assert report.rehearsal_scenarios == ("rehearsal_probe",)
        assert "rehearsals" in report.non_closing_reason
        assert report.degraded_count == 1

    def test_a_report_may_not_mark_itself_closing_over_a_rehearsal_row(self) -> None:
        with pytest.raises(ValidationError, match="demo rehearsals"):
            RunReport(
                generated_at=datetime.now(UTC),
                total=1,
                passed=1,
                failed=0,
                closing=True,
                outcomes=(_rehearsal_outcome(),),
            )

    def test_the_non_closing_reason_names_the_rehearsal_before_the_role(self) -> None:
        """Two different remedies: re-role a development run, REPLACE a rehearsal."""
        report = RunReport(
            generated_at=datetime.now(UTC),
            total=1,
            passed=1,
            failed=0,
            closing=False,
            invocation_id="rehearsal0001",
            outcomes=(_rehearsal_outcome(),),
        )

        assert "scripted planner" in report.non_closing_reason
        assert ModelRole.DEVELOPMENT.value not in report.non_closing_reason


class TestTheAssemblersRefuseIt:
    def test_the_research_report_refuses_a_rehearsal_row_in_scope(self, tmp_path: Path) -> None:
        report = RunReport(
            generated_at=datetime.now(UTC),
            total=1,
            passed=1,
            failed=0,
            closing=False,
            degraded_count=1,
            outcomes=(_rehearsal_outcome(),),
        )
        archive = tmp_path / "evals" / "runs" / "rehearsal0001"
        archive.mkdir(parents=True)
        (archive / "report.json").write_text(report.model_dump_json(), encoding="utf-8")

        with pytest.raises(research.RehearsalRefused) as refused:
            research.assemble(tmp_path, ["rehearsal0001"])

        assert "rehearsal0001/remediate_dlq_backlog_success" in str(refused.value)
        assert "SCOPE" in str(refused.value)

    def test_a_scope_with_no_rehearsal_row_is_not_refused(self) -> None:
        assert research.rehearsal_refusal([]) is None

    def test_the_phase_close_verdict_refuses_it_and_says_it_cannot_be_re_rolled(self) -> None:
        report = RunReport(
            generated_at=datetime.now(UTC),
            total=1,
            passed=1,
            failed=0,
            closing=False,
            invocation_id="rehearsal0001",
            outcomes=(_rehearsal_outcome(),),
        )
        verdict = close.closing_verdict([report])

        assert verdict["closing"] is False
        assert "REHEARSALS" in verdict["reason"]
        assert "rehearsal0001/remediate_dlq_backlog_success" in verdict["reason"]
        # The remedy has to be the right one: re-running the same invocation under another
        # role would produce the same non-measurement.
        assert "cannot help" in verdict["reason"]

    def test_a_rehearsal_archive_cannot_be_a_live_leg(self) -> None:
        report = RunReport(
            generated_at=datetime.now(UTC),
            total=1,
            passed=1,
            failed=0,
            closing=False,
            invocation_id="rehearsal0001",
            outcomes=(_rehearsal_outcome(),),
        )
        leg = close.LiveLeg(
            order=1,
            archive_id="rehearsal0001",
            scenario="remediate_dlq_backlog_success",
            story="the demo's own walk",
        )

        refusal = close.rehearsal_leg_refusal(leg, report)
        assert refusal is not None
        assert "cannot be a live leg" in refusal

    def test_a_real_live_leg_is_not_refused(self) -> None:
        live = _rehearsal_outcome().provenance
        assert live is not None
        report = RunReport(
            generated_at=datetime.now(UTC),
            total=1,
            passed=1,
            failed=0,
            closing=False,
            outcomes=(
                _rehearsal_outcome().model_copy(
                    update={
                        "degraded": False,
                        "provenance": live.model_copy(
                            update={"execution_mode": ExecutionMode.LIVE, "rehearsal": False}
                        ),
                    }
                ),
            ),
        )
        leg = close.LiveLeg(
            order=1,
            archive_id="42000dfda188",
            scenario="remediate_dlq_backlog_success",
            story="a paid run",
        )

        assert close.rehearsal_leg_refusal(leg, report) is None
        assert close.closing_verdict([report])["closing"] is True

    def test_a_rehearsal_row_has_no_pairing_rule_and_is_refused_rather_than_borrowing_one(
        self,
    ) -> None:
        """``candidate_metrics`` pairs arms on a world; a rehearsal is not an arm.

        Left OUT of ``_WORLD_INSTANCE`` deliberately, so the refusal it already writes for
        an unknown mode is the behaviour — a rule copied from ``live`` would quietly admit
        rehearsal rows into a paired comparison.
        """
        with pytest.raises(ValueError, match="unknown execution mode"):
            world_key(
                scenario="remediate_dlq_backlog_success",
                execution_mode=ExecutionMode.REHEARSAL.value,
                archive="rehearsal0001",
            )


# --------------------------------------------------------------------------
# The CLI: what it refuses, and what it does not weaken
# --------------------------------------------------------------------------


class TestTheCliRefusals:
    """Every gate a live run has, a rehearsal has too: it seeds into the same world."""

    def test_the_mode_flag_takes_both_values_and_nothing_else(self) -> None:
        assert runner_module._parse_mode(["--mode", "rehearsal"]) == ("rehearsal", "")
        assert runner_module._parse_mode(["--mode=rehearsal"]) == ("rehearsal", "")
        assert runner_module._parse_mode(["--mode", "recorded"]) == ("recorded", "")
        assert runner_module._parse_mode([])[0] == ""
        mode, refusal = runner_module._parse_mode(["--mode", "rehersal"])
        assert mode is None
        assert "rehearsal" in refusal

    def test_rehearsal_with_live_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _forbid_run_all(monkeypatch)
        assert _main(monkeypatch, "--mode", REHEARSAL_MODE, "--live", "--only", "x") == 2
        out = capsys.readouterr().out
        assert "MODE FAIL" in out
        assert "--live" in out
        assert "nothing was spent" in out

    def test_rehearsal_with_smoke_is_refused_before_the_mode_is_even_read(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``--smoke`` without ``--live`` already exits 3, and that order is deliberate.

        Pinned so the rehearsal gate cannot be read as the thing protecting this case: the
        smoke refusal precedes every mode question because it needs no environment.
        """
        _forbid_run_all(monkeypatch)
        assert _main(monkeypatch, "--mode", REHEARSAL_MODE, "--smoke") == 3
        assert "SMOKE FAIL" in capsys.readouterr().out

    def test_rehearsal_with_live_and_smoke_is_refused_as_a_mode_clash(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _forbid_run_all(monkeypatch)
        assert _main(monkeypatch, "--mode", REHEARSAL_MODE, "--live", "--smoke") == 2
        assert "MODE FAIL" in capsys.readouterr().out

    def test_rehearsal_without_only_is_refused_before_the_scenario_tree_is_read(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Free and still destructive: an unfiltered rehearsal seeds every fault."""

        def _boom(*_a: Any, **_k: Any) -> Any:
            raise AssertionError("the missing-filter refusal must precede this")

        monkeypatch.setattr(runner_module, "_settings_for_mode", _boom)
        monkeypatch.setattr(runner_module, "load_scenarios", _boom)
        _forbid_run_all(monkeypatch)
        assert _main(monkeypatch, "--mode", REHEARSAL_MODE) == 2
        out = capsys.readouterr().out
        assert "REHEARSAL FAIL" in out
        assert "make demo-live" in out
        assert "nothing was spent" in out

    def test_a_world_is_refused_outside_recorded_mode(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _forbid_run_all(monkeypatch)
        assert _main(monkeypatch, "--mode", REHEARSAL_MODE, "--world", "abc123") == 2
        assert "WORLD FAIL" in capsys.readouterr().out

    def test_a_placeholder_platform_is_refused_the_way_a_placeholder_key_is(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The mirror of ``--mode recorded``'s key refusal: with no platform, no rehearsal."""
        _isolate_env(monkeypatch, tmp_path, _OFFLINE_ENV)
        _forbid_run_all(monkeypatch)
        assert (
            _main(monkeypatch, "--mode", REHEARSAL_MODE, "--only", "remediate_dlq_backlog_success")
            == 3
        )
        out = capsys.readouterr().out
        assert "PREFLIGHT FAIL (env)" in out
        assert "PLATFORM_MCP_URL" in out
        assert "nothing was spent" in out

    def test_a_scenario_with_no_platform_leg_cannot_be_rehearsed(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _isolate_env(monkeypatch, tmp_path, _REAL_LOOKING_ENV)
        monkeypatch.setattr(
            runner_module,
            "load_scenarios",
            lambda _d: [_scenario("canned_world", use_live_mcp=False)],
        )
        _forbid_run_all(monkeypatch)
        assert _main(monkeypatch, "--mode", REHEARSAL_MODE, "--only", "canned_world") == 8
        out = capsys.readouterr().out
        assert "REHEARSAL FAIL" in out
        assert "use_live_mcp false" in out
        assert "make eval ONLY=canned_world" in out

    def test_two_mutating_scenarios_are_refused_and_the_advice_names_this_mode(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """ADR 0020 is about the shared world, not about spend — so it holds here."""
        mutating = [
            _scenario(
                name,
                expectation=_scenario(name).expectation.model_copy(
                    update={"expected_action_tools": ("restart_consumer_group",)}
                ),
            )
            for name in ("remediate_a", "remediate_b")
        ]
        _isolate_env(monkeypatch, tmp_path, _REAL_LOOKING_ENV)
        monkeypatch.setattr(runner_module, "load_scenarios", lambda _d: mutating)
        _forbid_run_all(monkeypatch)
        assert (
            _main(monkeypatch, "--mode", REHEARSAL_MODE, "--only", "remediate_a,remediate_b") == 7
        )
        out = capsys.readouterr().out
        assert "REHEARSAL FAIL" in out
        assert f"--mode {REHEARSAL_MODE} --only remediate_a" in out
        assert "make eval-reset" in out
        # The paid command must NOT be the one a free run is told to use.
        assert "make eval-live" not in out

    def test_a_substring_selection_is_refused_as_it_is_under_live(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _isolate_env(monkeypatch, tmp_path, _REAL_LOOKING_ENV)
        monkeypatch.setattr(runner_module, "load_scenarios", lambda _d: [_scenario("remediate_a")])
        _forbid_run_all(monkeypatch)
        assert _main(monkeypatch, "--mode", REHEARSAL_MODE, "--only", "remediate") == 2
        out = capsys.readouterr().out
        assert "SELECTION FAIL" in out
        assert "full scenario name" in out

    def test_a_contaminated_world_blocks_a_rehearsal_too(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The latch exists because the NEXT run inherits the world. A rehearsal is next."""
        monkeypatch.setattr(runner_module, "_CHAOS_BLOCK_PATH", tmp_path / "block.json")
        runner_module.write_chaos_block("plan_probe", "saturate_redis: compensator refused")
        _isolate_env(monkeypatch, tmp_path, _REAL_LOOKING_ENV)
        _forbid_run_all(monkeypatch)

        def _boom(*_a: Any, **_kw: Any) -> Any:
            raise AssertionError("a blocked run must not touch the platform")

        monkeypatch.setattr(runner_module, "make_client", _boom)
        assert (
            _main(monkeypatch, "--mode", REHEARSAL_MODE, "--only", "remediate_dlq_backlog_success")
            == 10
        )
        out = capsys.readouterr().out
        assert "CONTAMINATED WORLD" in out
        assert "rehearsal" in out
        assert "compensator refused" in out


class TestTheRunReachesTheRealPlatformUnderBothGuards:
    def test_it_runs_and_hands_run_all_the_flag_the_real_platform_and_no_key(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        scenario = _scenario(
            "remediate_probe",
            expectation=_scenario("remediate_probe").expectation.model_copy(
                update={"expected_action_tools": ("restart_consumer_group",)}
            ),
        )
        _isolate_env(monkeypatch, tmp_path, _REAL_LOOKING_ENV)
        monkeypatch.setattr(runner_module, "load_scenarios", lambda _d: [scenario])
        monkeypatch.setattr(runner_module, "_RUNS_DIR", tmp_path / "runs")
        probed: list[str] = []

        def _make(settings: Any, *_a: Any, token: str | None = None, **_kw: Any) -> Any:
            probe = _ClosableCanned({})

            def _call_tool(name: str, _arguments: Any, **_k: Any) -> Any:
                probed.append(name)
                chaos = settings.platform_chaos_token
                is_chaos_client = chaos is not None and token == chaos.get_secret_value()
                if not is_chaos_client and name == "inject_latency":
                    raise MCPError(-32002, "missing required scope: chaos:invoke")
                raise MCPError(-32602, "invalid tool arguments")

            monkeypatch.setattr(probe, "call_tool", _call_tool)
            return probe

        monkeypatch.setattr(runner_module, "make_client", _make)
        calls: list[dict[str, Any]] = []

        def _stub_run_all(*args: Any, **kwargs: Any) -> Any:
            calls.append({"args": args, "kwargs": kwargs})
            return (
                RunReport(
                    generated_at=datetime.now(UTC),
                    total=1,
                    passed=1,
                    failed=0,
                    degraded_count=1,
                    closing=False,
                    outcomes=(_rehearsal_outcome("remediate_probe"),),
                ),
                (),
                (),
            )

        monkeypatch.setattr(runner_module, "run_all", _stub_run_all)
        monkeypatch.setattr(runner_module, "write_report", lambda *_a, **_kw: tmp_path / "r.json")
        monkeypatch.setattr(runner_module, "write_trajectories", lambda *_a, **_kw: [])
        monkeypatch.setattr(runner_module, "write_briefings", lambda *_a, **_kw: [])

        try:
            assert _main(monkeypatch, "--mode", REHEARSAL_MODE, "--only", "remediate_probe") == 0
        finally:
            _unlock_tree(tmp_path / "runs")

        assert len(calls) == 1
        settings = calls[0]["args"][1]
        assert calls[0]["kwargs"]["rehearsal"] is True
        assert str(settings.platform_mcp_url) == _REAL_LOOKING_ENV["PLATFORM_MCP_URL"]
        assert runner_module._is_offline_api_key(settings.anthropic_api_key.get_secret_value())
        # The principal guards RAN: a rehearsal acts for real, so "the agent can act and
        # cannot seed" is exactly as load-bearing here as on the paid take.
        assert probed, "the two-principal guard must run for a rehearsal"
        out = capsys.readouterr().out
        assert f"mode: {REHEARSAL_MODE}" in out
        assert "measures the DEMO, never the agent" in out
        # The degraded line must not read as a misconfiguration to fix.
        assert "BY DESIGN" in out


class TestTheTwoPaidGatesAreNotWeakened:
    """The whole point of a third mode: neither existing refusal moves."""

    def test_live_with_a_placeholder_key_still_refuses_at_exit_3(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _isolate_env(
            monkeypatch, tmp_path, {**_REAL_LOOKING_ENV, "ANTHROPIC_API_KEY": "placeholder"}
        )
        _forbid_run_all(monkeypatch)
        monkeypatch.setattr(
            runner_module, "load_scenarios", lambda _d: [_scenario("remediate_probe")]
        )
        assert _main(monkeypatch, "--live", "--only", "remediate_probe") == 3
        out = capsys.readouterr().out
        assert "would degrade to canned" in out
        assert "nothing was spent" in out

    def test_recorded_with_a_placeholder_key_still_refuses_at_exit_3(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _isolate_env(monkeypatch, tmp_path, {**_REAL_LOOKING_ENV, "ANTHROPIC_API_KEY": "eval"})
        _forbid_run_all(monkeypatch)
        monkeypatch.setattr(runner_module, "_RUNS_DIR", tmp_path / "runs")
        assert (
            _main(monkeypatch, "--mode", "recorded", "--only", "remediate_consumer_lag_success")
            == 3
        )
        out = capsys.readouterr().out
        assert "ANTHROPIC_API_KEY" in out
        assert "labelled recorded" in out

    def test_live_still_requires_an_explicit_selection(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _forbid_run_all(monkeypatch)
        assert _main(monkeypatch, "--live") == 2
        assert "LIVE FAIL: --live requires --only" in capsys.readouterr().out


class TestTheDemoMachineUsesTheMode:
    def test_the_free_path_asks_for_the_mode_rather_than_blanking_a_key(self) -> None:
        """Belt-and-braces against a regression to the old shape (#315's divergence 1).

        Blanking ``ANTHROPIC_API_KEY`` for the subprocess made the whole run canned — the
        platform leg included, because ``_settings_for_mode(live=False)`` hardcodes
        ``eval.local``. The mode is the fix, and the demo machine must be the thing asking
        for it.
        """
        source = (_REPO_ROOT / "scripts" / "demo_live.py").read_text(encoding="utf-8")
        assert "REHEARSAL_MODE" in source
        assert '"ANTHROPIC_API_KEY": ""' not in source

    def test_the_runbook_no_longer_describes_the_gap_as_open(self) -> None:
        runbook = (_REPO_ROOT / "docs" / "demo-runbook.md").read_text(encoding="utf-8")
        assert "Known gap" not in runbook
        assert f"--mode {REHEARSAL_MODE}" in runbook


def test_the_adr_is_indexed() -> None:
    """A decision that constrains future readers is on the shelf, and findable."""
    index = (_REPO_ROOT / "docs" / "ADR" / "README.md").read_text(encoding="utf-8")
    assert "0069" in index
    adr = _REPO_ROOT / "docs" / "ADR" / "0069-a-rehearsal-is-a-third-provenance-not-a-live-run.md"
    assert adr.is_file()
