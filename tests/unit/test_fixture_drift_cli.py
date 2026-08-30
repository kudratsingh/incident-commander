"""``scripts/fixture_drift.py``'s two decisions: when to wait, and when to bless.

Both are exercised with ``probe_live`` replaced, because both are about what
the script does with a result rather than about how the result was obtained
— that half lives in ``test_fixture_drift.py``. Nothing here touches the
network and nothing writes under ``evals/`` (ADR 0011 freeze): the bless
tests assert the committed ledger is byte-identical afterwards.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from evals.fixture_drift import Drift
from evals.fixture_probe import ProbeError, ProbeResult
from scripts import fixture_drift as cli


@pytest.fixture
def platform_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PLATFORM_MCP_URL", "http://x/mcp")
    monkeypatch.setenv("PLATFORM_SMOKE_TOKEN", "read-scoped")


def _result(
    *,
    drifts: tuple[Drift, ...] = (),
    errors: tuple[ProbeError, ...] = (),
    compared: tuple[tuple[str, str], ...] = (),
) -> ProbeResult:
    return ProbeResult(
        drifts=drifts,
        errors=errors,
        checked=len(compared),
        skipped_write_tier=0,
        live_calls=len(compared),
        compared=compared,
    )


def _scripted(monkeypatch: pytest.MonkeyPatch, outcomes: list[Any]) -> list[Any]:
    """Replace ``probe_live`` with a queue of results-or-raises."""
    remaining = list(outcomes)

    def fake_probe(calls: Any, **kwargs: Any) -> ProbeResult:  # noqa: ARG001
        outcome = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        if isinstance(outcome, BaseException):
            raise outcome
        assert isinstance(outcome, ProbeResult)
        return outcome

    monkeypatch.setattr(cli, "probe_live", fake_probe)
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    return remaining


class TestReadinessGate:
    """``--await-fixtures`` exists to survive a platform that is still booting.

    It only ever caught ``UnseededPlatformError`` — the "up but empty" case —
    so the connection errors a platform produces while it is *not yet up*
    killed the poll loop on attempt one, which is precisely the window the
    gate was added for.
    """

    def test_survives_a_platform_that_has_not_opened_its_port(
        self, monkeypatch: pytest.MonkeyPatch, platform_env: None
    ) -> None:
        remaining = _scripted(
            monkeypatch,
            [
                httpx.ConnectError("[Errno 61] Connection refused"),
                httpx.ReadTimeout("timed out"),
                _result(compared=(("s", "get_consumer_lag"),)),
            ],
        )
        assert cli.main(["--await-fixtures", "60"]) == 0
        assert len(remaining) == 1, "the gate gave up before the platform came up"

    def test_a_failure_that_outlives_the_budget_is_still_reported(
        self,
        monkeypatch: pytest.MonkeyPatch,
        platform_env: None,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # A readiness gate, not a retry: an unresolved call at the deadline is
        # reported with the reason, never swallowed into a pass.
        _scripted(monkeypatch, [httpx.ConnectError("[Errno 61] Connection refused")])
        assert cli.main(["--await-fixtures", "0"]) == 2
        assert "Connection refused" in capsys.readouterr().err
