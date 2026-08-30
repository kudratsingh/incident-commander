"""The polling window: one arithmetic, and the docs held to it (WO-R2-88).

Two loops in this repo poll a live platform for a state change: ADR 0006's
verify window (``agent/remediation.py::make_llm_verify``) and the eval
precondition probe (``evals/runner.py::_assert_preconditions``). Both sleep
BETWEEN attempts, so N attempts span ``(N - 1) * delay`` — and both this
ADR's stated bound and the precondition guard used to say ``N * delay``,
overstating every window by one whole delay.

That overstatement is not a rounding error. A polling window is sized to
outlast a staleness (here the platform's 60s metrics interval); a guard that
overstates it green-lights a window that cannot see the change it is waiting
for, and reports the pass as coverage. The guard passed a 5-attempt, 14.9s
probe — 74.5s claimed, 59.6s real.

So the arithmetic lives in ``config.polling_window_seconds`` and this module
holds three things to it: the formula, the two loops that must really sleep
that long, and ADR 0006's own numbers.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Final
from uuid import uuid4

import pytest

from evals import runner
from evals.scenarios.loader import load_scenarios
from incident_commander.agent.remediation import RemediationPlan, make_llm_verify
from incident_commander.agent.state import BudgetLedger, IncidentState, RunState
from incident_commander.config import (
    PLATFORM_METRICS_INTERVAL_SECONDS,
    Settings,
    polling_window_seconds,
)
from incident_commander.llm.fakes import CannedLLMClient
from incident_commander.tools.mcp_client import ToolResult

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
_ADR: Final[Path] = _REPO_ROOT / "docs" / "ADR" / "0006-verification-is-a-polling-window.md"
_ENV_EXAMPLE: Final[Path] = _REPO_ROOT / ".env.example"
_RUNBOOK: Final[Path] = _REPO_ROOT / "docs" / "runbook.md"


@pytest.fixture(autouse=True)
def _isolate_probe_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer's exported knobs must not decide what the defaults are."""
    for name in ("VERIFY_PROBE_ATTEMPTS", "VERIFY_PROBE_DELAY_SECONDS"):
        monkeypatch.delenv(name, raising=False)


class TestTheFormula:
    """``(attempts - 1) * delay`` — the sleeps, not the probes."""

    @pytest.mark.parametrize(
        ("attempts", "delay", "window"),
        [
            (1, 15.0, 0.0),  # one probe is not a window
            (2, 15.0, 15.0),
            (3, 15.0, 30.0),
            (6, 15.0, 75.0),  # the shipped lag precondition
            (6, 20.0, 100.0),  # the live-recommended verify knobs
        ],
    )
    def test_window_counts_the_gaps_between_attempts(
        self, attempts: int, delay: float, window: float
    ) -> None:
        assert polling_window_seconds(attempts, delay) == pytest.approx(window)

    def test_a_window_the_old_arithmetic_green_lit_is_now_short(self) -> None:
        """The regression case, stated as the two numbers side by side.

        ``attempts * delay`` says 74.5s and clears the 60s metrics interval.
        The real wait is 59.6s and does not: every probe in this window can
        read the pre-change value and the run aborts as "never manufactured".
        """
        attempts, delay = 5, 14.9
        assert attempts * delay >= PLATFORM_METRICS_INTERVAL_SECONDS
        assert polling_window_seconds(attempts, delay) < PLATFORM_METRICS_INTERVAL_SECONDS

    def test_settings_report_their_own_window(self) -> None:
        settings = _settings(verify_probe_attempts=6, verify_probe_delay_seconds=20.0)
        assert settings.verify_polling_window_seconds == pytest.approx(100.0)

    def test_the_default_window_is_a_single_probe(self) -> None:
        # Canned runs must stay instant-consistent (ADR 0006 decision drivers).
        assert _settings().verify_polling_window_seconds == 0.0


class TestBothLoopsSleepExactlyThatLong:
    """The formula is only worth anything if the runtime agrees with it."""

    def test_the_verify_window_sleeps_between_attempts(self) -> None:
        attempts, delay = 4, 15.0
        slept: list[float] = []
        transition = make_llm_verify(
            _SequencedMCP([_lag_result(15_000)] * attempts),
            CannedLLMClient(
                [{"verdict": "not_verified", "reasoning": "cached read"}] * attempts
            ),
            model="test-model",
            probe_attempts=attempts,
            probe_delay_seconds=delay,
            sleep=slept.append,
        )

        result = transition(_verifying_run(), datetime.now(UTC))

        assert result.state is IncidentState.ESCALATED
        assert len(slept) == attempts - 1
        assert sum(slept) == pytest.approx(polling_window_seconds(attempts, delay))

    def test_the_precondition_probe_sleeps_between_attempts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The shipped lag precondition, driven against a world that stays 0.

        Every attempt reads a live-shaped payload that does not satisfy the
        expectation, so the loop runs to exhaustion — the longest wait the
        probe can impose, which is the one the guard is sizing.
        """
        scenario = next(
            s
            for s in load_scenarios(_REPO_ROOT / "evals" / "scenarios")
            if s.name == "remediate_consumer_lag_success"
        )
        probe = next(p for p in scenario.expected_precondition if p.tool == "get_consumer_lag")
        slept: list[float] = []
        monkeypatch.setattr(runner, "time", SimpleNamespace(sleep=slept.append))
        client = _SequencedMCP([_lag_result(0)] * probe.attempts)

        with pytest.raises(runner.PreconditionNotMet):
            runner._assert_preconditions(scenario, client, None)

        assert len(client.calls) == probe.attempts
        assert len(slept) == probe.attempts - 1
        assert sum(slept) == pytest.approx(
            polling_window_seconds(probe.attempts, probe.delay_seconds)
        )


class TestAdr0006StatesTheOperativeNumbers:
    """Doc-drift tripwire, same shape as ``test_docs_env_vars`` (B-03).

    ADR 0006 said the window was "default 3 attempts × 15s = 45s" while no
    configuration used 3 and the arithmetic counted a delay that is never
    slept. A number in an ADR that no test reads is a number that drifts, so
    the amendment's figures are built here from the constants themselves.
    """

    @staticmethod
    def _adr_text() -> str:
        return _ADR.read_text(encoding="utf-8")

    @staticmethod
    def _env_example_value(var: str) -> str:
        for line in _ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
            if line.startswith(f"{var}="):
                return line.split("=", 1)[1].strip()
        raise AssertionError(f"{var} is not set in .env.example")

    def test_the_amendment_quotes_the_default_window(self) -> None:
        attempts = Settings.model_fields["verify_probe_attempts"].default
        delay = Settings.model_fields["verify_probe_delay_seconds"].default
        window = polling_window_seconds(attempts, delay)
        expected = (
            f"* Defaults (`VERIFY_PROBE_ATTEMPTS={attempts}`, "
            f"`VERIFY_PROBE_DELAY_SECONDS={delay:g}`): window **{window:g}s**."
        )
        assert expected in self._adr_text(), (
            f"ADR 0006's amendment must state the default window as {expected!r}. "
            "Settings' defaults moved and the ADR did not."
        )

    def test_the_amendment_quotes_the_live_window(self) -> None:
        attempts = int(self._env_example_value("VERIFY_PROBE_ATTEMPTS"))
        delay = float(self._env_example_value("VERIFY_PROBE_DELAY_SECONDS"))
        window = polling_window_seconds(attempts, delay)
        expected = (
            f"* Live-recommended (`VERIFY_PROBE_ATTEMPTS={attempts}`, "
            f"`VERIFY_PROBE_DELAY_SECONDS={delay:g}`): window **{window:g}s**."
        )
        assert expected in self._adr_text(), (
            f"ADR 0006's amendment must state the live window as {expected!r}. "
            ".env.example ships the live-recommended knobs and the ADR did not follow."
        )

    def test_the_runbook_table_and_env_example_agree(self) -> None:
        """The knob table calls itself the source of record — so it must be one."""
        row = re.search(
            r"^\|\s*`VERIFY_PROBE_ATTEMPTS`\s*\|\s*(\S+)\s*\|\s*(\S+)\s*\|",
            _RUNBOOK.read_text(encoding="utf-8"),
            re.MULTILINE,
        )
        assert row is not None, "docs/runbook.md lost its VERIFY_PROBE_ATTEMPTS row"
        documented_default, documented_live = row.group(1), row.group(2)
        assert int(documented_default) == Settings.model_fields["verify_probe_attempts"].default
        assert documented_live == self._env_example_value("VERIFY_PROBE_ATTEMPTS")


# ---------------------------------------------------------------------------
# Fakes. Deliberately small: this module is about seconds, not trajectories.


_PLAN: Final[RemediationPlan] = RemediationPlan(
    target_hypothesis="consumer_saturation",
    action_tool="restart_consumer_group",
    action_arguments={"consumer_group": "worker-dispatcher"},
    verify_tool="get_consumer_lag",
    verify_arguments={"consumer_group": "worker-dispatcher"},
    verify_expectation="lag should drop toward zero",
)


class _SequencedMCP:
    """Returns one canned ``ToolResult`` per call, in order."""

    def __init__(self, results: list[ToolResult]) -> None:
        self._results = list(results)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call_tool(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        timeout_seconds: float | None = None,
    ) -> ToolResult:
        self.calls.append((name, dict(arguments)))
        return self._results.pop(0)


def _lag_result(lag: int) -> ToolResult:
    payload = {
        "consumer_group": "worker-dispatcher",
        "lag": lag,
        "cache_key": "kafka:consumer_lag:worker-dispatcher",
    }
    return ToolResult(content=[{"type": "text", "text": json.dumps(payload)}], is_error=False)


def _settings(**overrides: Any) -> Settings:
    """Bypass any local ``.env`` — same constructor shape as test_config."""
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        anthropic_api_key="sk-ant-test",
        judge_model="claude-haiku-4-5",
        platform_mcp_url="https://mcp.platform.local",
        platform_rest_url="https://api.platform.local",
        platform_token="svc-token",
        platform_webhook_secret="hmac-secret",
        database_url="postgresql://commander:commander@localhost:5432/commander",
        **overrides,
    )


def _verifying_run() -> RunState:
    now = datetime.now(UTC)
    return RunState(
        incident_id=uuid4(),
        state=IncidentState.VERIFYING,
        alert={"source": "test", "severity": "high"},
        budget=BudgetLedger(
            max_tool_calls=25,
            max_tokens=100_000,
            max_wall_seconds=600,
            max_usd=Decimal("5.00"),
        ),
        created_at=now,
        updated_at=now,
        remediation_plan=_PLAN.model_dump(mode="json"),
    )
