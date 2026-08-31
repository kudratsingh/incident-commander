"""Submit jobs at a steady rate so a killed consumer builds real backlog.

Consumer lag is arrival minus service. The eval only ever had the service
half: `kill_consumer` stops the consumer, but with nothing arriving the
backlog stays at zero and `get_consumer_lag` keeps reading 0. So
`remediate_consumer_lag_success` asserts a fault that cannot exist, and
since preconditions landed it aborts honestly instead of grading the agent
on a healthy system.

`docs/runbook.md` has described "the standard 1-job/2s loop" since the
2026-08-04 kill-window experiment as though it were a thing you could run.
It was not — no such script existed anywhere in either repo. This is it.

Three platform behaviours shape the design, and all three are the platform
being correct rather than obstacles:

* **Job creation needs a USER token**, not the service account the eval
  uses. `POST /jobs` depends on `get_current_user`, so this logs in the same
  demo user `bootstrap_agent_token.py` registers.

* **`jobs:create` is rate limited to 30 per 60s.** The runbook's "1 job/2s"
  is exactly 30/min — precisely at the limit, so half the requests would
  race the window and 429. The default here is one every 3 seconds (20/min),
  which leaves headroom and still builds backlog faster than a dead consumer
  drains it (it drains at zero).

* **Backpressure rejects new jobs once lag passes 1000**, reading
  `kafka:consumer_lag:worker-dispatcher` — the very key the scenario
  measures. So a 503 here is not a failure: it is the platform telling you
  the fault is fully manufactured. The loop reports it that way and keeps
  going, because lag does not drain while the consumer is dead.

Run it in a second terminal alongside the scenario, or with `--until-lag`
to stop once the backlog is deep enough and let the scenario take over.
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
from dataclasses import dataclass, field
from types import FrameType
from typing import Any, Final

import httpx

DEFAULT_BASE_URL: Final = "http://localhost:8000/api/v1"
DEFAULT_EMAIL: Final = "agent-demo@example.com"
DEFAULT_PASSWORD: Final = "demo-agent-pass-123"  # noqa: S105 - dev-only placeholder
# 20/min against a 30/min limit. See the module docstring for why not 1/2s.
DEFAULT_INTERVAL_SECONDS: Final = 3.0
# One of the platform's four JobType values (backend/app/models/enums.py).
# bulk_api_sync is what the eval fixture pack already uses, so the traffic
# blends with the seeded rows rather than standing out as a new shape.
DEFAULT_JOB_TYPE: Final = "bulk_api_sync"

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

        This feeds the ``--count`` stop condition, so it counts ATTEMPTS —
        errors included. Summing only the three reportable outcomes meant a
        run against a dead platform, a stale token, or a rejected job type
        errored on every request, never advanced the count, and never
        terminated. ``describe()`` keeps errors in their own bucket, which
        is the distinction that actually matters to the operator.
        """
        return self.created + self.rate_limited + self.backpressured + len(self.errors)

    def describe(self) -> str:
        parts = [f"{self.created} created"]
        if self.rate_limited:
            parts.append(f"{self.rate_limited} rate-limited")
        if self.backpressured:
            parts.append(f"{self.backpressured} backpressured (lag is deep — good)")
        if self.errors:
            parts.append(f"{len(self.errors)} error(s)")
        return ", ".join(parts)


def login(client: httpx.Client, email: str, password: str) -> str:
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
            # Unique per submission: an idempotency key that repeated would
            # have the platform dedupe the traffic away, and a loop that
            # produces one job however long it runs is not a loop.
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


class LagReader:
    """Reads worker-dispatcher lag through the same tool the agent uses.

    There is no REST endpoint for it — `get_consumer_lag` is MCP-only — and
    that is the right surface anyway: the number worth watching is the one
    the scenario will actually see, not a Redis key this script has no
    business knowing.

    Read-scoped by construction: it takes PLATFORM_SMOKE_TOKEN, so a bug
    here cannot mutate the world the traffic is building. Optional — with no
    MCP url or token the loop still runs, it just cannot report lag.
    """

    def __init__(self, mcp_url: str | None, token: str | None, client: httpx.Client) -> None:
        self.enabled = bool(mcp_url and token and token.strip())
        self._url = mcp_url or ""
        self._token = token or ""
        self._client = client

    def read(self) -> int | None:
        if not self.enabled:
            return None
        try:
            response = self._client.post(
                self._url,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "get_consumer_lag",
                        "arguments": {"consumer_group": "worker-dispatcher"},
                    },
                },
                headers={"Authorization": f"Bearer {self._token}"},
            )
            payload = response.json()
            for block in payload.get("result", {}).get("content", []):
                if block.get("type") == "text":
                    value = json.loads(block["text"]).get("lag")
                    return value if isinstance(value, int) else None
        except (httpx.HTTPError, ValueError, AttributeError):
            return None
        return None


def run(
    client: httpx.Client,
    jwt: str,
    *,
    job_type: str,
    interval: float,
    max_submissions: int | None,
    until_lag: int | None,
    lag_reader: LagReader | None = None,
    sleep: Any = time.sleep,
    on_tick: Any = print,
) -> Tally:
    """Submit until stopped, until the count is reached, or until lag is deep."""
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
        submit_one(client, jwt, job_type, tally)
        lag = lag_reader.read() if lag_reader is not None else None
        if n % 10 == 0 or tally.backpressured == 1:
            suffix = f" · lag {lag}" if lag is not None else ""
            on_tick(f"  {tally.describe()}{suffix}")
        if until_lag is not None and lag is not None and lag >= until_lag:
            on_tick(f"  lag reached {lag} (>= {until_lag}) — stopping")
            break
        if stopping:
            break
        sleep(interval)
    return tally


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--email", default=DEFAULT_EMAIL)
    parser.add_argument("--password", default=DEFAULT_PASSWORD)
    parser.add_argument("--job-type", default=DEFAULT_JOB_TYPE)
    parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_INTERVAL_SECONDS,
        help=f"seconds between submissions (default {DEFAULT_INTERVAL_SECONDS}; "
        "the platform allows 30/60s and 1/2s sits exactly on that limit)",
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
        # Refuse rather than run forever: --until-lag with no way to read lag
        # is a loop with no stopping condition, which is worse than no flag.
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
        print(f"submitting {args.job_type} every {args.interval}s as {args.email} (Ctrl-C to stop)")
        tally = run(
            client,
            jwt,
            job_type=args.job_type,
            interval=args.interval,
            max_submissions=args.count,
            until_lag=args.until_lag,
            lag_reader=LagReader(args.mcp_url, smoke_token, client) if can_read_lag else None,
        )

    print(f"traffic loop finished: {tally.describe()}")
    for failure in tally.errors[:5]:
        print(f"  error: {failure}")
    # Errors that are not backpressure or rate limiting mean the traffic did
    # not actually arrive, which is the one outcome that leaves the scenario
    # asserting a fault that was never built.
    return 1 if tally.errors and tally.created == 0 else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
