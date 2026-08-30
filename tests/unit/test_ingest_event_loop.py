"""/health must answer while an ingest is stuck on the database (ADR 0022).

``ingest_alert`` is an ``async def`` that used to call synchronous Postgres
straight from the event loop — the identity walk (up to 64 indexed loads) and
the ingress checkpoint. One thread runs every coroutine in the process, so a
database slow enough to matter froze the whole API, /health included. That
inverts the signal exactly when it is needed: an agent that is merely waiting
on Postgres reports as dead, and whatever watches /health restarts it,
throwing away the in-flight runs whose lease connections were the thing worth
keeping.

The blocking is simulated with ``time.sleep`` in a fake checkpointer, which is
what a pool-timeout wait looks like from the event loop's point of view: a
synchronous call that does not yield. No Postgres needed to prove a coroutine
does not yield.

Since WO-R2-86 /health probes that same database rather than answering ``ok``
unconditionally, so it is no longer free — but it is still *bounded*, by
``HEALTH_PROBE_TIMEOUT_SECONDS`` rather than by the database. Under a stall the
honest answer is ``degraded``, delivered on the health check's own schedule;
what must never happen is /health inheriting the wait. Both halves are asserted
below.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from typing import Any
from uuid import UUID

import anyio
import httpx
import pytest
from pydantic import SecretStr

from incident_commander.agent.state import RunState
from incident_commander.api import app as app_module
from incident_commander.api.app import create_app
from incident_commander.api.hmac_verify import sign
from incident_commander.config import Settings

# Each blocking DB call costs this. One ingest makes three of them (the
# identity walk's load, then the ingress load + write), so the request stays
# busy for ~1s while the probe below runs.
_DB_STALL_SECONDS = 0.35
# The probe yield. An order of magnitude below one stalled call, so "the
# ingest is still running" is unambiguous rather than a photo finish.
_YIELD_SECONDS = 0.05
# Generous next to the stall but far below it: the claim is the
# order-of-magnitude gap between a free loop and a blocked one, not a latency
# budget anybody should tune.
_HEALTH_BUDGET_SECONDS = 0.25
# What /health waits for its datastore probe. Set well inside the budget
# above, because that is the point: the endpoint answers on this schedule even
# though the store it is asking about is the thing that is stuck.
_HEALTH_PROBE_TIMEOUT_SECONDS = 0.05


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def _fresh_replay_cache() -> Iterator[None]:
    app_module._replay_cache.clear()
    yield
    app_module._replay_cache.clear()


def _test_settings(**overrides: Any) -> Settings:
    defaults: dict[str, Any] = {
        "anthropic_api_key": SecretStr("sk-ant-test"),
        "judge_model": "claude-haiku-4-5",
        "platform_mcp_url": "https://mcp.local",
        "platform_rest_url": "https://api.local",
        "platform_token": SecretStr("svc-token"),
        "platform_webhook_secret": SecretStr("hmac-secret"),
        "database_url": "postgresql://u:p@localhost:5432/db",
    }
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)  # type: ignore[call-arg]


class StallingCheckpointer:
    """A checkpointer whose every call blocks the calling thread.

    Stands in for a pool-starved Postgres: from the caller's side, waiting
    ``DB_POOL_TIMEOUT_SECONDS`` for a connection and waiting for a slow query
    are the same thing — a synchronous call that does not return.
    """

    def __init__(self, delay: float) -> None:
        self._delay = delay
        self.calls = 0

    def load(self, _incident_id: UUID) -> RunState | None:
        self.calls += 1
        time.sleep(self._delay)
        return None

    def write(self, _run_state: RunState) -> None:
        self.calls += 1
        time.sleep(self._delay)


def _signed_alert() -> tuple[bytes, dict[str, str]]:
    body = json.dumps(
        {"source": "prometheus", "severity": "high", "fingerprint": "kafka-lag-spike"}
    ).encode()
    return body, {
        "X-Alert-Signature": sign(body, "hmac-secret"),
        "Content-Type": "application/json",
    }


@pytest.mark.anyio
async def test_health_answers_while_an_ingest_is_blocked_on_the_database() -> None:
    """The regression: a stalled ingest must not take the event loop with it.

    The assertion that carries this is the *ordering* one, not the stopwatch.
    A blocked event loop cannot be caught by timing a later ``await``, because
    the block swallows that await too — every coroutine, including the one
    doing the measuring, resumes only once the loop is free again, by which
    time the stall it was trying to observe is over. So the probe is: after
    yielding, is the ingest still in flight? If the loop was blocked, the
    ingest necessarily ran to completion first, and that is visible.
    """
    checkpointer = StallingCheckpointer(_DB_STALL_SECONDS)
    app = create_app(
        settings=_test_settings(health_probe_timeout_seconds=_HEALTH_PROBE_TIMEOUT_SECONDS),
        checkpointer=checkpointer,
        run_task=lambda *_args: None,
    )
    body, headers = _signed_alert()
    finished: list[str] = []

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://agent.test") as client:

        async def ingest() -> None:
            await client.post("/alerts", content=body, headers=headers)
            finished.append("ingest")

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(ingest)

            # Yield. With the DB work in a worker thread this returns on
            # schedule with the ingest still stalled; on a blocked loop it
            # returns only after the whole ingest is done.
            await anyio.sleep(_YIELD_SECONDS)
            assert not finished, (
                f"the event loop was blocked: a {_DB_STALL_SECONDS}s-per-call ingest ran to "
                f"completion before this coroutine could resume from a {_YIELD_SECONDS}s "
                "sleep. The synchronous DB calls in ingest_alert are on the event loop, so "
                "a slow database freezes the whole API, /health included."
            )

            started = time.monotonic()
            health = await client.get("/health")
            elapsed = time.monotonic() - started
            finished.append("health")

    # Degraded, and correctly so: the store this agent needs is stalled, and
    # since WO-R2-86 /health says so instead of reporting ok. The claim under
    # test is unchanged — it answered at all, and it answered early.
    assert health.status_code == 503
    assert health.json()["status"] == "degraded"
    assert finished == ["health", "ingest"], "/health did not overtake the stalled ingest"
    assert elapsed < _HEALTH_BUDGET_SECONDS, (
        f"/health took {elapsed:.2f}s while an ingest was blocked on the database. "
        "The probe is bounded by HEALTH_PROBE_TIMEOUT_SECONDS; a wait longer than "
        "that means the endpoint inherited the database's stall instead of "
        "reporting it."
    )
    assert checkpointer.calls > 0, "the ingest never reached the database; nothing was proven"


@pytest.mark.anyio
async def test_the_stalled_ingest_still_completes_correctly() -> None:
    """Moving work off the loop must not change the answer, only where it waits."""
    checkpointer = StallingCheckpointer(_DB_STALL_SECONDS)
    spawned: list[RunState] = []
    app = create_app(
        settings=_test_settings(),
        checkpointer=checkpointer,
        run_task=lambda run, *_args: spawned.append(run),
    )
    body, headers = _signed_alert()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://agent.test") as client:
        response = await client.post("/alerts", content=body, headers=headers)

    assert response.status_code == 202
    incident_id = UUID(response.json()["incident_id"])
    assert [run.incident_id for run in spawned] == [incident_id]
