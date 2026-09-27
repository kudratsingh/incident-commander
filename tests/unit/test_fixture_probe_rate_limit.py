"""A 429 from the platform is "not observed", never "no longer drifts" (WO-R3-307).

A rate-limited call contributes no drift, so every ledger row it would have matched read
as fixed: 42 stale entries on one run, 1 on the next, same ledger, same stack.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from evals.fixture_drift import CannedCall
from evals.fixture_drift_ledger import classify
from evals.fixture_probe import ProbeResult, probe_live
from scripts import fixture_drift as cli

_REQUEST = httpx.Request("POST", "http://x/mcp")
_SEEDED = {"list_dlq_messages": {"total": 1, "items": [{"id": "a"}]}}


class _Platform:
    """Answers each tool from a script; ``limited`` tools get a 429 for their first N calls."""

    def __init__(self, payloads: dict[str, Any], limited: dict[str, int]) -> None:
        self.payloads = {**_SEEDED, **payloads}
        self.limited = dict(limited)
        self.calls: list[str] = []

    def post(self, url: str, **kwargs: Any) -> Any:  # noqa: ARG002
        name = kwargs["json"]["params"]["name"]
        self.calls.append(name)
        if self.limited.get(name, 0) > 0:
            self.limited[name] -= 1
            return httpx.Response(429, text="Too Many Requests", request=_REQUEST)
        body = json.dumps(self.payloads[name])
        return httpx.Response(
            200,
            json={"result": {"content": [{"type": "text", "text": body}]}},
            request=_REQUEST,
        )


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    slept: list[float] = []
    monkeypatch.setattr("time.sleep", slept.append)
    return slept


def _call(scenario: str, tool: str, payload: dict[str, Any], *, chaos: bool = False) -> CannedCall:
    return CannedCall(
        scenario=scenario, tool=tool, arguments={}, payload=payload, chaos_seeded=chaos
    )


def _probe(platform: _Platform, calls: list[CannedCall]) -> ProbeResult:
    return probe_live(calls, mcp_url="http://x/mcp", token="read-scoped", client=platform)  # type: ignore[arg-type]


_TWO = [
    _call("limited", "get_consumer_lag", {"lag": 1200}),
    _call("fine", "get_redis_health", {"ok": True}),
]
_LIVE = {"get_consumer_lag": {"lag": 0}, "get_redis_health": {"ok": False}}


def test_a_call_that_stays_rate_limited_is_neither_new_nor_stale(no_sleep: list[float]) -> None:
    platform = _Platform(_LIVE, limited={"get_consumer_lag": 99})
    result = _probe(platform, _TWO)

    assert result.errors == (), "a 429 is not a probe failure; it is no reading at all"
    assert result.rate_limited == (("limited", "get_consumer_lag"),)
    assert result.rate_limited_calls == 1
    assert ("limited", "get_consumer_lag") not in result.compared
    # The other fixture was still read and its drift still reported.
    assert [(d.scenario, d.path) for d in result.drifts] == [("fine", "ok")]

    ledger = frozenset(
        {
            ("limited", "get_consumer_lag", "lag", "value"),
            ("fine", "get_redis_health", "ok", "value"),
        }
    )
    new, stale = classify(result.drifts, ledger, unobserved=result.rate_limited)
    assert new == ()
    assert stale == (), "the rate-limited row was read as fixed"


def test_the_back_off_is_bounded(no_sleep: list[float]) -> None:
    platform = _Platform(_LIVE, limited={"get_consumer_lag": 99})
    _probe(platform, _TWO)
    attempts = platform.calls.count("get_consumer_lag")
    assert 1 < attempts <= 5
    assert len(no_sleep) == attempts - 1
    assert sum(no_sleep) <= 60


def test_a_429_that_clears_on_retry_is_an_ordinary_reading(no_sleep: list[float]) -> None:
    platform = _Platform(_LIVE, limited={"get_consumer_lag": 1})
    result = _probe(platform, _TWO)
    assert result.rate_limited == ()
    assert result.rate_limited_calls == 0
    assert ("limited", "get_consumer_lag") in result.compared
    assert {d.scenario for d in result.drifts} == {"limited", "fine"}
    assert no_sleep, "the retry did not back off"


def test_a_rate_limited_chaos_fixture_is_not_compared_against_nothing(
    no_sleep: list[float],
) -> None:
    # A chaos-seeded fixture's failed call is compared against {} on purpose; a 429 is
    # not an observation of the un-faulted world and must not take that path.
    platform = _Platform(_LIVE, limited={"get_consumer_lag": 99})
    result = _probe(platform, [_call("seeded", "get_consumer_lag", {"lag": 1200}, chaos=True)])
    assert result.drifts == ()
    assert result.compared == ()
    assert result.rate_limited == (("seeded", "get_consumer_lag"),)


def test_the_report_names_how_many_calls_were_rate_limited(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("PLATFORM_MCP_URL", "http://x/mcp")
    monkeypatch.setenv("PLATFORM_SMOKE_TOKEN", "read-scoped")
    limited = ProbeResult(
        drifts=(),
        errors=(),
        checked=2,
        skipped_write_tier=0,
        live_calls=2,
        compared=(("fine", "get_redis_health"),),
        rate_limited=(("limited", "get_consumer_lag"),),
        rate_limited_calls=1,
    )
    monkeypatch.setattr(cli, "probe_live", lambda calls, **kwargs: limited)  # noqa: ARG005

    assert cli.main([]) == 1, "an unread fixture must not pass as agreement"
    out = capsys.readouterr().out
    assert "rate-limited calls: 1" in out
    assert "limited:get_consumer_lag" in out
