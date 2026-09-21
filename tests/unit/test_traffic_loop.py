"""The traffic producer: the missing half of consumer lag.

Lag is arrival minus service. `kill_consumer` supplies the service half, but with nothing
arriving the backlog stays at zero and the scenario asserts a fault that cannot exist.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from incident_commander.tools.mcp_client import LAB_PRINCIPAL_HEADER, LAB_PROBE_PARAM
from scripts.traffic_loop import (
    DEFAULT_INTERVAL_SECONDS,
    DEFAULT_MAX_PER_WINDOW,
    DEFAULT_WINDOW_SECONDS,
    LAB_PROBE_REASON,
    LagReader,
    Tally,
    WindowPacer,
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
        reader = LagReader("http://x/mcp", None)
        assert reader.enabled is False
        assert reader.read() is None

    def test_disabled_on_an_empty_token(self) -> None:
        assert LagReader("http://x/mcp", "   ").enabled is False

    def test_reads_the_lag_out_of_an_mcp_result(self) -> None:
        payload = {
            "result": {"content": [{"type": "text", "text": '{"consumer_group":"wd","lag":4200}'}]}
        }
        reader = LagReader(
            "http://x/mcp",
            "tok",
            transport=httpx.MockTransport(lambda _r: httpx.Response(200, json=payload)),
        )
        assert reader.read() == 4200
        reader.close()

    def test_an_unreadable_response_is_none_not_a_crash(self) -> None:
        reader = LagReader(
            "http://x/mcp",
            "tok",
            transport=httpx.MockTransport(lambda _r: httpx.Response(500, text="nope")),
        )
        assert reader.read() is None
        reader.close()


class TestTheLagReadIsTheLabsOwnRead:
    """The fifth take's F5: `agent.tool_invoked get_consumer_lag` every ~0.8 s, from here.

    With no run selected the demo page counted every tool row as the agent's, so the ledger
    showed the agent reading lag ten seconds before the lab had injected anything. The read is
    the lab's, so it says so on the wire and lands as `lab.probe` (platform ADR 0038).
    """

    @staticmethod
    def _captured() -> tuple[list[httpx.Request], httpx.MockTransport]:
        seen: list[httpx.Request] = []

        def _handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            payload = {"result": {"content": [{"type": "text", "text": '{"lag": 7}'}]}}
            return httpx.Response(200, json=payload)

        return seen, httpx.MockTransport(_handler)

    def test_the_read_carries_the_label_and_the_lab_credential(self) -> None:
        seen, transport = self._captured()
        reader = LagReader("http://x/mcp", "smoke-token", transport=transport)

        assert reader.read() == 7
        reader.close()

        params = json.loads(seen[0].content)["params"]
        assert params[LAB_PROBE_PARAM] == LAB_PROBE_REASON == "traffic: lag read"
        # Beside `arguments`, never inside it: inside, the field would reach a tool's input
        # model and therefore `tools/list`, which is the contract the commander pins.
        assert LAB_PROBE_PARAM not in params["arguments"]
        assert seen[0].headers[LAB_PRINCIPAL_HEADER] == "Bearer smoke-token"

    def test_the_loop_reads_the_lag_only_when_it_has_a_use_for_it(self) -> None:
        """119 of the take's 162 audit rows were this read on every submission, and nothing
        looked at most of them. It is taken for `--until-lag` and for a printed tick."""
        reads = {"n": 0}

        class _Reader(LagReader):
            def __init__(self) -> None:
                self.enabled = True

            def read(self) -> int | None:
                reads["n"] += 1
                return 3

        run(
            _client(_created),
            "jwt",
            job_type="bulk_api_sync",
            interval=0.0,
            max_submissions=9,
            until_lag=None,
            lag_reader=_Reader(),
            sleep=lambda _s: None,
            on_tick=lambda _m: None,
        )

        assert reads["n"] == 0, "nine submissions print no tick, so no lag read is needed"


class TestThePacerStaysUnderThePlatformsAllowance:
    """The fifth take's F4: the lag climbed to 28 and then sat flat at 28 for 25 seconds.

    `POST /jobs` allows a FIXED window of creations per caller address, and it was 30 per 60 s
    when the take ran: a producer at 0.75 s spent the whole window in 22 s and then collected
    429s until it rolled, which on the console's chart is a climb that stops being a climb. The
    pacer reads the same clock the platform cuts its window from and spreads what is left over
    the time that is left. The 30 is only the platform's DEFAULT since v0.6.20
    (`JOB_CREATE_RATE_LIMIT`), which is why the limit is an argument here.
    """

    @staticmethod
    def _pacer(
        *, at: float, floor: float = 0.0, limit: int = 30
    ) -> tuple[WindowPacer, dict[str, float]]:
        clock = {"t": at}
        pacer = WindowPacer(limit=limit, window=60.0, floor=floor, clock=lambda: clock["t"])
        return pacer, clock

    def test_the_default_allowance_is_the_platforms_own_default(self) -> None:
        """Not the demo stack's 240: a loop told a ceiling the stack does not honour gets 429s,
        so the safe default is what an unconfigured platform allows."""
        assert (DEFAULT_MAX_PER_WINDOW, DEFAULT_WINDOW_SECONDS) == (30, 60.0)

    def test_a_whole_window_is_spread_evenly_across_it(self) -> None:
        pacer, _ = self._pacer(at=1_200_000.0)
        assert pacer.wait_seconds() == pytest.approx(2.0)

    def test_the_tail_of_a_window_is_used_at_the_rate_it_allows(self) -> None:
        # 20 s left and 30 creations unspent: the honest interval is shorter, not refused.
        pacer, _ = self._pacer(at=1_200_040.0)
        assert pacer.wait_seconds() == pytest.approx(20.0 / 30.0)

    def test_a_spent_window_waits_for_the_next_one_and_nothing_longer(self) -> None:
        pacer, clock = self._pacer(at=1_200_000.0)
        for _ in range(30):
            pacer.spend()
        clock["t"] = 1_200_022.0

        assert pacer.wait_seconds() == pytest.approx(38.0)

    def test_the_next_window_restores_the_whole_allowance(self) -> None:
        pacer, clock = self._pacer(at=1_200_000.0)
        for _ in range(30):
            pacer.spend()
        clock["t"] = 1_200_060.0

        assert pacer.wait_seconds() == pytest.approx(2.0)

    def test_a_refusal_marks_the_window_spent_rather_than_asking_again(self) -> None:
        """Another producer may have spent this window — the baseline loop, or the operator's
        own browser — and the pacer cannot see those. A 429 is how it finds out."""
        pacer, clock = self._pacer(at=1_200_010.0)
        pacer.refused()

        assert pacer.wait_seconds() == pytest.approx(50.0)
        clock["t"] = 1_200_060.0
        assert pacer.wait_seconds() == pytest.approx(2.0)

    def test_the_requested_interval_is_a_floor_the_pacer_never_undercuts(self) -> None:
        pacer, _ = self._pacer(at=1_200_040.0, floor=3.0)
        assert pacer.wait_seconds() == pytest.approx(3.0)

    def test_a_zero_limit_switches_the_pacing_off(self) -> None:
        pacer, _ = self._pacer(at=1_200_040.0, floor=0.5, limit=0)
        assert pacer.wait_seconds() == pytest.approx(0.5)

    def test_a_paced_loop_never_asks_for_more_than_the_window_allows(self) -> None:
        """The property, over a simulated two minutes: no 429 is ever earned."""
        clock = {"t": 1_200_000.0}
        pacer = WindowPacer(limit=30, window=60.0, floor=0.0, clock=lambda: clock["t"])
        created: list[float] = []

        def _handler(_request: httpx.Request) -> httpx.Response:
            bucket = int(clock["t"] // 60)
            if sum(1 for at in created if int(at // 60) == bucket) >= 30:
                return httpx.Response(429)
            created.append(clock["t"])
            return httpx.Response(201, json={"id": "job"})

        tally = run(
            _client(_handler),
            "jwt",
            job_type="bulk_api_sync",
            interval=0.0,
            max_submissions=60,
            until_lag=None,
            pacer=pacer,
            sleep=lambda seconds: clock.__setitem__("t", clock["t"] + seconds),
            on_tick=lambda _m: None,
        )

        assert tally.rate_limited == 0, "the pacer must never earn a refusal"
        assert tally.created == 60


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


class TestCountAlwaysTerminates:
    """``--count`` is a stop condition, so it has to count attempts (WO-R2-99).

    ``Tally.submitted`` summed created + rate-limited + backpressured and left ``errors`` out,
    so a run pointed at a dead platform never advanced it and never terminated.
    """

    @staticmethod
    def _bounded_sleep(limit: int) -> Any:
        """A sleep that fails the test rather than letting it hang forever."""
        seen = {"n": 0}

        def _sleep(_seconds: float) -> None:
            seen["n"] += 1
            if seen["n"] > limit:
                raise AssertionError(
                    f"--count never reached after {limit} iterations — the loop does not terminate"
                )

        return _sleep

    def test_a_run_whose_every_request_errors_stops_at_count(self) -> None:
        tally = run(
            _client(lambda _r: httpx.Response(500, text="platform is down")),
            "jwt",
            job_type="bulk_api_sync",
            interval=0.0,
            max_submissions=3,
            until_lag=None,
            sleep=self._bounded_sleep(20),
            on_tick=lambda *_a: None,
        )
        assert tally.created == 0
        assert len(tally.errors) == 3, "errors must count toward --count"

    def test_errors_and_successes_share_the_one_budget(self) -> None:
        """Mixed outcomes must not let the run overshoot the count either."""
        seen = {"n": 0}

        def _handler(_request: httpx.Request) -> httpx.Response:
            seen["n"] += 1
            return httpx.Response(201, json={"id": "job"}) if seen["n"] % 2 else httpx.Response(500)

        tally = run(
            _client(_handler),
            "jwt",
            job_type="bulk_api_sync",
            interval=0.0,
            max_submissions=4,
            until_lag=None,
            sleep=self._bounded_sleep(20),
            on_tick=lambda *_a: None,
        )
        assert tally.created + len(tally.errors) == 4
        assert seen["n"] == 4, "exactly --count requests, whatever they returned"
