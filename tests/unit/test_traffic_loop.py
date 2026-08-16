"""The traffic producer: the missing half of consumer lag.

Lag is arrival minus service. `kill_consumer` supplies the service half —
the consumer stops — but with nothing arriving the backlog stays at zero,
`get_consumer_lag` keeps reading 0, and `remediate_consumer_lag_success`
asserts a fault that cannot exist. The runbook has described "the standard
1-job/2s loop" since 2026-08-04 as though it were runnable. It was not.

Every test here is offline: httpx.MockTransport, no platform, no sleeping.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from scripts.traffic_loop import (
    DEFAULT_INTERVAL_SECONDS,
    LagReader,
    Tally,
    main,
    run,
    submit_one,
)


def _client(handler: Any) -> httpx.Client:
    return httpx.Client(
        base_url="http://platform.test/api/v1", transport=httpx.MockTransport(handler)
    )


def _created(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(201, json={"id": "job-1"})


class TestSubmitClassification:
    """Two of the three failure responses are not failures."""

    def test_a_created_job_counts(self) -> None:
        tally = Tally()
        submit_one(_client(_created), "jwt", "bulk_api_sync", tally)
        assert tally.created == 1
        assert tally.errors == []

    def test_backpressure_is_the_success_signal_not_an_error(self) -> None:
        """503 means lag passed the platform's threshold, reading the very key
        the scenario measures. That is the fault being fully manufactured."""
        tally = Tally()
        submit_one(
            _client(lambda _r: httpx.Response(503, json={"detail": "worker is behind"})),
            "jwt",
            "bulk_api_sync",
            tally,
        )
        assert tally.backpressured == 1
        assert tally.errors == []
        assert "lag is deep" in tally.describe()

    def test_rate_limiting_is_not_an_error_either(self) -> None:
        tally = Tally()
        submit_one(_client(lambda _r: httpx.Response(429)), "jwt", "bulk_api_sync", tally)
        assert tally.rate_limited == 1
        assert tally.errors == []

    def test_a_real_failure_is_recorded(self) -> None:
        tally = Tally()
        submit_one(
            _client(lambda _r: httpx.Response(422, text="bad job type")),
            "jwt",
            "not_a_job_type",
            tally,
        )
        assert tally.errors and "422" in tally.errors[0]

    def test_each_submission_carries_a_fresh_idempotency_key(self) -> None:
        # A repeated key would have the platform dedupe the traffic away, and
        # a loop that produces one job however long it runs is not a loop.
        seen: list[str] = []

        def _capture(request: httpx.Request) -> httpx.Response:
            import json as _json

            seen.append(_json.loads(request.content)["idempotency_key"])
            return httpx.Response(201, json={"id": "x"})

        tally = Tally()
        client = _client(_capture)
        for _ in range(3):
            submit_one(client, "jwt", "bulk_api_sync", tally)
        assert len(set(seen)) == 3


class TestRateDefault:
    def test_the_default_leaves_headroom_under_the_platform_limit(self) -> None:
        """The runbook's "1 job/2s" is exactly 30/min against a 30/min limit.

        Sitting precisely on the limit means half the requests race the
        window and 429. The default backs off to 20/min.
        """
        per_minute = 60.0 / DEFAULT_INTERVAL_SECONDS
        assert per_minute < 30, "the default rate must stay under jobs:create's 30/60s"


class TestRunLoop:
    def test_count_bounds_the_loop(self) -> None:
        tally = run(
            _client(_created),
            "jwt",
            job_type="bulk_api_sync",
            interval=0.0,
            max_submissions=5,
            until_lag=None,
            sleep=lambda _s: None,
            on_tick=lambda _m: None,
        )
        assert tally.created == 5

    def test_until_lag_stops_once_the_backlog_is_deep(self) -> None:
        lags = iter([10, 50, 900, 1200])

        class _Reader(LagReader):
            def __init__(self) -> None:
                self.enabled = True

            def read(self) -> int | None:
                return next(lags, 1200)

        tally = run(
            _client(_created),
            "jwt",
            job_type="bulk_api_sync",
            interval=0.0,
            max_submissions=None,
            until_lag=1000,
            lag_reader=_Reader(),
            sleep=lambda _s: None,
            on_tick=lambda _m: None,
        )
        # Stopped on the reading that crossed the threshold, not before.
        assert tally.created == 4

    def test_backpressure_does_not_stop_the_loop(self) -> None:
        # Lag does not drain while the consumer is dead, so there is nothing
        # to be gained by giving up — and stopping would look like a failure.
        tally = run(
            _client(lambda _r: httpx.Response(503)),
            "jwt",
            job_type="bulk_api_sync",
            interval=0.0,
            max_submissions=3,
            until_lag=None,
            sleep=lambda _s: None,
            on_tick=lambda _m: None,
        )
        assert tally.backpressured == 3


class TestLagReader:
    def test_disabled_without_a_token(self) -> None:
        reader = LagReader("http://x/mcp", None, _client(_created))
        assert reader.enabled is False
        assert reader.read() is None

    def test_disabled_on_an_empty_token(self) -> None:
        assert LagReader("http://x/mcp", "   ", _client(_created)).enabled is False

    def test_reads_the_lag_out_of_an_mcp_result(self) -> None:
        payload = {
            "result": {"content": [{"type": "text", "text": '{"consumer_group":"wd","lag":4200}'}]}
        }
        reader = LagReader(
            "http://x/mcp", "tok", _client(lambda _r: httpx.Response(200, json=payload))
        )
        assert reader.read() == 4200

    def test_an_unreadable_response_is_none_not_a_crash(self) -> None:
        reader = LagReader(
            "http://x/mcp", "tok", _client(lambda _r: httpx.Response(500, text="nope"))
        )
        assert reader.read() is None


class TestCli:
    def test_until_lag_without_a_way_to_read_lag_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A stopping condition it cannot observe is worse than no flag."""
        monkeypatch.delenv("PLATFORM_SMOKE_TOKEN", raising=False)
        monkeypatch.delenv("PLATFORM_MCP_URL", raising=False)
        assert main(["--until-lag", "1000"]) == 2
        assert "needs PLATFORM_MCP_URL" in capsys.readouterr().err

    def test_a_non_positive_interval_is_refused(self) -> None:
        assert main(["--interval", "0"]) == 2
