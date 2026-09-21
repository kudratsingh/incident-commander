"""Submit jobs at a steady rate so a killed consumer builds real backlog.

With nothing arriving, `kill_consumer` leaves `get_consumer_lag` reading 0 and
`remediate_consumer_lag_success` asserts a fault that cannot exist. Job creation needs a USER
token; backpressure rejects new jobs past lag 1000, so a 503 means the fault is manufactured.

The loop paces itself against the platform's own allowance rather than discovering it with
429s — see ``WindowPacer`` — because a producer that front-loads a window and then waits it out
draws a backlog that climbs and then sits flat, which is the fifth take's finding F4.
"""

from __future__ import annotations

import argparse
import contextlib
import itertools
import json
import os
import signal
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from types import FrameType
from typing import Any, Final

import httpx

from incident_commander.tools.mcp_client import (
    LabProbeClient,
    MCPClient,
    MCPClientProtocol,
    MCPError,
)

DEFAULT_BASE_URL: Final = "http://localhost:8000/api/v1"
DEFAULT_EMAIL: Final = "agent-demo@example.com"
DEFAULT_PASSWORD: Final = "demo-agent-pass-123"  # noqa: S105 - dev-only placeholder
# The requested spacing between jobs, and only a FLOOR: `WindowPacer` slows the loop further
# whenever the platform's allowance would not cover the rate asked for.
DEFAULT_INTERVAL_SECONDS: Final = 3.0
# A platform JobType value (backend/app/models/enums.py), and the one the eval fixture pack
# uses, so the traffic blends with the seeded rows.
DEFAULT_JOB_TYPE: Final = "bulk_api_sync"

#: The platform's `POST /jobs` allowance this loop paces itself against — a FIXED window keyed
#: on the caller's ADDRESS, shared with `POST /sagas`. 30 per 60 s is the platform's DEFAULT;
#: since v0.6.20 it is the `JOB_CREATE_RATE_LIMIT` setting, and the demo stack runs 240, so pass
#: `--max-per-window` whatever the stack being driven is really set to.
DEFAULT_MAX_PER_WINDOW: Final = 30
DEFAULT_WINDOW_SECONDS: Final = 60.0

#: Why this loop's lag reads are the lab's own rather than the agent's (platform ADR 0038).
#: Unlabelled they land as `agent.tool_invoked`, and the demo page with no run selected counts
#: every such row as a step the agent made — the fifth take's finding F5.
LAB_PROBE_REASON: Final = "traffic: lag read"

#: The group whose backlog is the fault every consumer-lag scenario is about.
_LAG_GROUP: Final = "worker-dispatcher"

_BACKPRESSURE_STATUS: Final = 503
_RATE_LIMITED_STATUS: Final = 429


@dataclass
class Tally:
    """What the loop did, for the summary line."""

    created: int = 0
    rate_limited: int = 0
    backpressured: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def submitted(self) -> int:
        """Every request the loop actually sent, whatever came back.

        It feeds ``--count``, so it counts ATTEMPTS: summing only the reportable outcomes
        left a dead-platform run looping forever.
        """
        return self.created + self.rate_limited + self.backpressured + len(self.errors)

    def describe(self) -> str:
        """The one-line summary, with each outcome in its own bucket."""
        parts = [f"{self.created} created"]
        if self.rate_limited:
            parts.append(f"{self.rate_limited} rate-limited")
        if self.backpressured:
            parts.append(f"{self.backpressured} backpressured (lag is deep — good)")
        if self.errors:
            parts.append(f"{len(self.errors)} error(s)")
        return ", ".join(parts)


def login(client: httpx.Client, email: str, password: str) -> str:
    """Log in the demo USER — job creation needs one, not a service account."""
    r = client.post("/auth/login", json={"email": email, "password": password})
    r.raise_for_status()
    token: str = r.json()["access_token"]
    return token


def submit_one(client: httpx.Client, jwt: str, job_type: str, tally: Tally) -> None:
    """One job. Classifies the response rather than raising on it."""
    response = client.post(
        "/jobs",
        json={
            "type": job_type,
            "payload": {"source": "eval-traffic-loop"},
            # Unique per submission: a repeated idempotency key has the platform dedupe the
            # traffic away, leaving one job however long the loop runs.
            "idempotency_key": f"traffic-{uuid.uuid4()}",
        },
        headers={"Authorization": f"Bearer {jwt}"},
    )
    if response.status_code == _BACKPRESSURE_STATUS:
        tally.backpressured += 1
        return
    if response.status_code == _RATE_LIMITED_STATUS:
        tally.rate_limited += 1
        return
    if response.status_code >= 400:
        tally.errors.append(f"HTTP {response.status_code}: {response.text[:120]}")
        return
    tally.created += 1


class WindowPacer:
    """How long to wait before the next submission so the platform never has to refuse one.

    The platform's window is ``int(time.time()) // window``, so this reads the same clock and
    spreads whatever allowance is left over the time that is left in the window. The effect is
    a backlog that grows at every sample instead of jumping and then sitting flat (finding F4).
    """

    def __init__(
        self,
        *,
        limit: int = DEFAULT_MAX_PER_WINDOW,
        window: float = DEFAULT_WINDOW_SECONDS,
        floor: float = 0.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.limit = limit
        self.window = window
        #: The spacing the operator asked for. The pacer only ever slows the loop down.
        self.floor = floor
        self._clock = clock
        self._window_start = -1
        self._used = 0

    def spend(self) -> None:
        """Book one submission against the window it landed in."""
        self._roll()
        self._used += 1

    def refused(self) -> None:
        """Take a 429 as "this window is gone": another producer spent part of the allowance.

        The baseline loop, a second terminal or the operator's own browser all share one
        bucket, and none of them is visible from here — a refusal is how the loop finds out.
        """
        self._roll()
        self._used = self.limit

    def wait_seconds(self) -> float:
        """Seconds to wait before the next submission."""
        if self.limit <= 0:
            return self.floor
        now = self._roll()
        left = (self._window_start + 1) * self.window - now
        remaining = self.limit - self._used
        # Nothing left to spend here, so the only honest wait is for the next window.
        if remaining <= 0:
            return max(self.floor, left)
        return max(self.floor, left / remaining)

    def _roll(self) -> float:
        """Reset the count when the clock has moved into a new window. Returns the reading."""
        now = self._clock()
        window_start = int(now // self.window)
        if window_start != self._window_start:
            self._window_start, self._used = window_start, 0
        return now


class LagReader:
    """Reads worker-dispatcher lag through `get_consumer_lag`, labelled as the lab's own.

    Read-scoped by construction (it takes PLATFORM_SMOKE_TOKEN), and the read says who made it:
    a `_lab_probe` reason plus the lab's credential make the platform file the row as
    `lab.probe` instead of as a read the agent took (platform ADR 0038).
    """

    def __init__(
        self,
        mcp_url: str | None,
        token: str | None,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.enabled = bool(mcp_url and token and token.strip())
        self._transport: MCPClient | None = None
        self._client: MCPClientProtocol | None = None
        if self.enabled:
            self._transport = MCPClient(
                base_url=str(mcp_url), token=str(token), transport=transport
            )
            self._client = LabProbeClient(
                self._transport, reason=LAB_PROBE_REASON, principal_token=str(token)
            )

    def read(self) -> int | None:
        """The current lag, or ``None`` when it is switched off or cannot be read."""
        if self._client is None:
            return None
        try:
            result = self._client.call_tool("get_consumer_lag", {"consumer_group": _LAG_GROUP})
        except MCPError:
            return None
        for block in result.content:
            if block.get("type") != "text":
                continue
            try:
                value = json.loads(str(block.get("text"))).get("lag")
            except (AttributeError, TypeError, ValueError):
                return None
            return value if isinstance(value, int) and not isinstance(value, bool) else None
        return None

    def close(self) -> None:
        if self._transport is not None:
            self._transport.close()


def run(
    client: httpx.Client,
    jwt: str,
    *,
    job_type: str,
    interval: float,
    max_submissions: int | None,
    until_lag: int | None,
    lag_reader: LagReader | None = None,
    pacer: WindowPacer | None = None,
    sleep: Any = time.sleep,
    on_tick: Any = print,
) -> Tally:
    """Submit until stopped, until the count is reached, or until lag is deep.

    ``pacer`` decides the wait between submissions when one is given; ``interval`` is the wait
    otherwise, and the pacer's floor either way.
    """
    tally = Tally()
    stopping = False

    def _stop(_signum: int, _frame: FrameType | None) -> None:
        nonlocal stopping
        stopping = True
        on_tick("\nstopping after the current submission…")

    # Not on the main thread (tests) raises; the count/lag limits still apply.
    with contextlib.suppress(ValueError):
        signal.signal(signal.SIGINT, _stop)

    for n in itertools.count(1):
        if stopping:
            break
        if max_submissions is not None and tally.submitted >= max_submissions:
            break
        # 1. Submit one job, and tell the pacer what it cost: one creation against this
        #    window's allowance, or a refusal, which means the allowance is already gone.
        refused_before = tally.rate_limited
        submit_one(client, jwt, job_type, tally)
        if pacer is not None:
            pacer.spend()
            if tally.rate_limited > refused_before:
                pacer.refused()
        # 2. Read the lag only when something will use it — the stopping condition, or the
        #    tick line. Each read is an audit row, and 119 unread ones filled take five's ledger.
        reporting = n % 10 == 0 or tally.backpressured == 1
        needed = lag_reader is not None and (until_lag is not None or reporting)
        lag = lag_reader.read() if needed and lag_reader is not None else None
        if reporting:
            suffix = f" · lag {lag}" if lag is not None else ""
            on_tick(f"  {tally.describe()}{suffix}")
        if until_lag is not None and lag is not None and lag >= until_lag:
            on_tick(f"  lag reached {lag} (>= {until_lag}) — stopping")
            break
        if stopping:
            break
        sleep(pacer.wait_seconds() if pacer is not None else interval)
    return tally


def main(argv: list[str] | None = None) -> int:
    """Check the flags, log in, run the loop, and report what arrived."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--email", default=DEFAULT_EMAIL)
    parser.add_argument("--password", default=DEFAULT_PASSWORD)
    parser.add_argument("--job-type", default=DEFAULT_JOB_TYPE)
    parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_INTERVAL_SECONDS,
        help=f"seconds between submissions, as a FLOOR (default {DEFAULT_INTERVAL_SECONDS}); "
        "the loop waits longer whenever the platform's own allowance would not cover it",
    )
    parser.add_argument(
        "--max-per-window",
        type=int,
        default=DEFAULT_MAX_PER_WINDOW,
        help=f"the platform's `POST /jobs` allowance this loop paces itself against "
        f"(default {DEFAULT_MAX_PER_WINDOW} per window; 0 switches the pacing off)",
    )
    parser.add_argument(
        "--window-seconds",
        type=float,
        default=DEFAULT_WINDOW_SECONDS,
        help=f"the length of that fixed window (default {DEFAULT_WINDOW_SECONDS:.0f})",
    )
    parser.add_argument("--count", type=int, default=None, help="stop after N submissions")
    parser.add_argument(
        "--until-lag",
        type=int,
        default=None,
        help="stop once worker-dispatcher lag reaches this value "
        "(needs PLATFORM_MCP_URL + PLATFORM_SMOKE_TOKEN to read it)",
    )
    parser.add_argument("--mcp-url", default=os.environ.get("PLATFORM_MCP_URL"))
    args = parser.parse_args(argv)

    if args.interval <= 0:
        print("ERROR: --interval must be positive", file=sys.stderr)
        return 2

    smoke_token = os.environ.get("PLATFORM_SMOKE_TOKEN")
    can_read_lag = bool(args.mcp_url and smoke_token and smoke_token.strip())
    if args.until_lag is not None and not can_read_lag:
        # Refuse rather than run forever: --until-lag with no way to read lag is a loop
        # with no stopping condition.
        print(
            "ERROR: --until-lag needs PLATFORM_MCP_URL and PLATFORM_SMOKE_TOKEN "
            "so the loop can read the lag it is waiting for. Run "
            "`make bootstrap-token`, or drop --until-lag and stop it with Ctrl-C.",
            file=sys.stderr,
        )
        return 2

    with httpx.Client(base_url=args.base_url, timeout=10.0) as client:
        try:
            jwt = login(client, args.email, args.password)
        except httpx.HTTPError as err:
            print(
                f"ERROR: could not log in as {args.email}: {err}. "
                "Run `make demo` and `make bootstrap-token` first — this needs the "
                "USER account, not the service-account token the eval uses.",
                file=sys.stderr,
            )
            return 2
        allowance = (
            f"{args.max_per_window} per {args.window_seconds:.0f}s"
            if args.max_per_window > 0
            else "unpaced"
        )
        print(
            f"submitting {args.job_type} every {args.interval}s at most, within the "
            f"platform's allowance ({allowance}), as {args.email} (Ctrl-C to stop)"
        )
        reader = LagReader(args.mcp_url, smoke_token) if can_read_lag else None
        try:
            tally = run(
                client,
                jwt,
                job_type=args.job_type,
                interval=args.interval,
                max_submissions=args.count,
                until_lag=args.until_lag,
                lag_reader=reader,
                pacer=WindowPacer(
                    limit=args.max_per_window,
                    window=args.window_seconds,
                    floor=args.interval,
                ),
            )
        finally:
            if reader is not None:
                reader.close()

    print(f"traffic loop finished: {tally.describe()}")
    for failure in tally.errors[:5]:
        print(f"  error: {failure}")
    # Errors that are not backpressure or rate limiting mean the traffic never arrived, so
    # the scenario would assert a fault nobody built.
    return 1 if tally.errors and tally.created == 0 else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
