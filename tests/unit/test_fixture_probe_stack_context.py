"""The drift report says which stack context it read, and from what (WO-R3-308)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from evals.fixture_drift import CannedCall
from evals.fixture_probe import ProbeResult, probe_live
from scripts import fixture_drift as cli

_REQUEST = httpx.Request("POST", "http://x/mcp")


class _Platform:
    def __init__(self, lag: dict[str, Any]) -> None:
        self.payloads = {"list_dlq_messages": {"items": [{"id": "a"}]}, "get_consumer_lag": lag}

    def post(self, url: str, **kwargs: Any) -> Any:  # noqa: ARG002
        body = json.dumps(self.payloads[kwargs["json"]["params"]["name"]])
        return httpx.Response(
            200, json={"result": {"content": [{"type": "text", "text": body}]}}, request=_REQUEST
        )


def _probe(lag: dict[str, Any]) -> ProbeResult:
    call = CannedCall(
        scenario="s",
        tool="get_consumer_lag",
        arguments={"consumer_group": "worker-dispatcher"},
        payload={"lag": 0},
    )
    return probe_live([call], mcp_url="http://x/mcp", token="t", client=_Platform(lag))  # type: ignore[arg-type]


def test_a_recent_measurement_reads_warm_and_says_so() -> None:
    result = _probe({"lag": 0, "lag_known": True, "measured_at": "2026-09-27T10:00:00Z"})
    assert result.stack_context == "warm"
    assert "lag_known=true" in result.stack_context_reason
    assert "has measured the lag within its last window" in result.stack_context_reason


def test_no_measurement_in_the_window_reads_cold_and_says_so() -> None:
    result = _probe({"lag": None, "lag_known": False, "measured_at": None})
    assert result.stack_context == "cold"
    assert "measured_at=null" in result.stack_context_reason
    assert "has not measured" in result.stack_context_reason


def test_the_report_prints_the_context_and_its_reason(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("PLATFORM_MCP_URL", "http://x/mcp")
    monkeypatch.setenv("PLATFORM_SMOKE_TOKEN", "read-scoped")
    result = ProbeResult(
        drifts=(),
        errors=(),
        checked=1,
        skipped_write_tier=0,
        live_calls=1,
        stack_context="cold",
        stack_context_reason="the reading",
    )
    monkeypatch.setattr(cli, "probe_live", lambda calls, **kwargs: result)  # noqa: ARG005
    cli.main([])
    assert "stack context: cold because the reading" in capsys.readouterr().out
