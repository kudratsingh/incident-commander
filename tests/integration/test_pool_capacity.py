"""Run admission vs. connection-pool capacity against real Postgres (ADR 0022).

The failure these tests pin down needs no broken database to reproduce, which
is what makes it nasty: Postgres is healthy, the CPU is idle, and the agent is
wedged anyway. A run holds one pooled connection for its entire life (the
single-flight lease, ADR 0016) and asks the same pool for a second one every
time it checkpoints. Let enough runs start and every connection is held by a
lease whose owner is blocked waiting for a connection — hold-and-wait, and
nobody is coming.

Reproducing it does not need fifteen runs; it needs a pool small enough that
two runs exhaust it. The pool here is 2 connections with a 1-second timeout,
so "the second run wedges the pool" takes a second to demonstrate instead of
thirty, and the arithmetic is the same arithmetic.

Real Postgres rather than a fake pool because the lease is a session-scoped
advisory lock — the pinning that causes the problem only exists if the
connection is a real session. Skips cleanly without Docker like every test in
this tree (see conftest).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine, event

from incident_commander.agent.factory import start_run
from incident_commander.agent.state import IncidentState, RunState
from incident_commander.api import app as app_module
from incident_commander.config import Settings
from incident_commander.persistence.lease import incident_lease
from incident_commander.persistence.pool import RunSlots, create_pooled_engine
from incident_commander.persistence.postgres import PostgresCheckpointer

_WAIT_SECONDS = 15.0
# Small enough that two runs exhaust it, so the ceiling below works out to
# exactly one concurrent run: (2 capacity - 0 reserved) // 2 per run.
_POOL_SIZE = 2
_POOL_TIMEOUT_SECONDS = 1.0


def _test_settings(database_url: str, **overrides: Any) -> Settings:
    defaults: dict[str, Any] = {
        "anthropic_api_key": SecretStr("sk-ant-test"),
        "judge_model": "claude-haiku-4-5",
        "platform_mcp_url": "https://mcp.local",
        "platform_rest_url": "https://api.local",
        "platform_token": SecretStr("svc-token"),
        "platform_webhook_secret": SecretStr("hmac-secret"),
        "database_url": database_url,
        "db_pool_size": _POOL_SIZE,
        "db_max_overflow": 0,
        "db_ingest_reserved_connections": 0,
        "db_pool_timeout_seconds": _POOL_TIMEOUT_SECONDS,
    }
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)  # type: ignore[call-arg]


def _run_state(settings: Settings, incident_id: UUID) -> RunState:
    return start_run(
        {"source": "billing", "severity": "high", "fingerprint": str(incident_id)},
        settings,
        datetime.now(UTC),
        incident_id=incident_id,
    )


@contextmanager
def _fake_client(_settings: Settings) -> Iterator[object]:
    """Stand in for ``make_client``: the pool, not the transport, is under test."""
    yield object()


@pytest.fixture
def offline_mcp(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_module, "make_client", _fake_client)


@pytest.fixture
def tiny_settings(_postgres_container: str, clean_engine: Engine) -> Settings:
    """Settings whose pool holds two connections. ``clean_engine`` migrates + truncates."""
    return _test_settings(_postgres_container)


@pytest.fixture
def tiny_engine(tiny_settings: Settings) -> Iterator[Engine]:
    engine = create_pooled_engine(tiny_settings)
    try:
        yield engine
    finally:
        engine.dispose()


class TestPoolSizing:
    def test_engine_pool_is_sized_from_settings_not_sqlalchemy_defaults(
        self, tiny_settings: Settings, tiny_engine: Engine
    ) -> None:
        """The bug's precondition was an engine nobody configured.

        ``create_engine(url)`` alone yields QueuePool(5, overflow 10,
        timeout 30) — numbers no one chose, sitting right where a modest
        incident burst wedges the lease.
        """
        pool = tiny_engine.pool
        assert pool.size() == _POOL_SIZE  # type: ignore[attr-defined]
        assert tiny_settings.db_pool_capacity == _POOL_SIZE

    def test_ceiling_leaves_a_connection_for_the_checkpoint_write(
        self, tiny_settings: Settings
    ) -> None:
        """One run per two connections: the pinned lease plus one for writing."""
        assert tiny_settings.max_concurrent_runs == 1


class TestCheckpointWriteConnectionCost:
    """The per-run cost the ceiling is derived from has to be the real one."""

    def test_write_takes_exactly_one_connection(
        self, tiny_engine: Engine, tiny_settings: Settings
    ) -> None:
        """One checkout per write, so a run's peak demand is 2 and not 3.

        This is what makes ``_CONNECTIONS_PER_RUN = 2`` true. ``write`` used
        to read the next version on its own connection and then take a second
        one for the INSERT — two checkouts, and a window between them where
        another writer could claim the version just read.
        """
        checkpointer = PostgresCheckpointer(tiny_engine)
        run = _run_state(tiny_settings, uuid4())
        checkouts: list[object] = []

        def _count(*_args: object) -> None:
            checkouts.append(object())

        event.listen(tiny_engine, "checkout", _count)
        try:
            checkpointer.write(run)
        finally:
            event.remove(tiny_engine, "checkout", _count)

        assert len(checkouts) == 1, (
            f"one checkpoint write took {len(checkouts)} pooled connections; the "
            "concurrency ceiling is derived from this number being 1"
        )

    def test_a_checkpoint_write_succeeds_while_the_lease_is_held(
        self, tiny_engine: Engine, tiny_settings: Settings
    ) -> None:
        """The two-connection peak, exercised: lease pinned, write proceeds."""
        checkpointer = PostgresCheckpointer(tiny_engine)
        run = _run_state(tiny_settings, uuid4())

        with incident_lease(tiny_engine, run.incident_id) as acquired:
            assert acquired is True
            checkpointer.write(run.with_state(IncidentState.INVESTIGATING, datetime.now(UTC)))

        assert [snap.state for snap in checkpointer.history(run.incident_id)] == [
            IncidentState.INVESTIGATING
        ]


class TestConcurrencyBound:
    """The regression that matters: one run over the bound must not wedge the pool."""

    def test_run_beyond_the_bound_is_shed_fast_and_the_live_run_keeps_writing(
        self,
        tiny_engine: Engine,
        tiny_settings: Settings,
        offline_mcp: None,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Two runs, two DIFFERENT incidents, a pool that fits one of them.

        Different incident ids on purpose: the lease would serialize two runs
        of the SAME incident, and that would prove nothing about the pool.
        These two have no reason to contend except the connections they hold.

        Without the bound the second run takes the last connection for its own
        lease, then blocks for the pool timeout on the very first checkpoint
        load and dies of ``sqlalchemy.exc.TimeoutError`` — and the failure rail
        that would record why needs a connection too. With it, the second run
        never starts, says so, and the first run is untouched.
        """
        checkpointer = PostgresCheckpointer(tiny_engine)
        slots = RunSlots(tiny_settings.max_concurrent_runs)
        live = _run_state(tiny_settings, uuid4())
        excess = _run_state(tiny_settings, uuid4())
        checkpointer.write(live)  # the ingress TRIAGE rows
        checkpointer.write(excess)

        holding = threading.Event()
        release = threading.Event()

        def slow_run(state: RunState, **kwargs: Any) -> RunState:
            ckpt: PostgresCheckpointer = kwargs["checkpointer"]
            ckpt.write(state.with_state(IncidentState.INVESTIGATING, datetime.now(UTC)))
            holding.set()
            release.wait(_WAIT_SECONDS)
            final = state.with_state(IncidentState.ESCALATED, datetime.now(UTC))
            ckpt.write(final)
            return final

        monkeypatch.setattr(app_module, "run_to_completion", slow_run)
        winner = threading.Thread(
            target=app_module._run_investigation,
            args=(live, tiny_settings, checkpointer),
            kwargs={"engine": tiny_engine, "slots": slots},
        )
        winner.start()
        try:
            assert holding.wait(_WAIT_SECONDS), "the first run never took its slot"

            with caplog.at_level(logging.WARNING, logger="incident_commander.api.app"):
                started = time.monotonic()
                app_module._run_investigation(
                    excess, tiny_settings, checkpointer, engine=tiny_engine, slots=slots
                )
                elapsed = time.monotonic() - started

            # Fast refusal, not a pool-timeout wait. Under the pre-fix path
            # this line is never reached — the call raises TimeoutError — and
            # if it ever were, it would have taken at least the pool timeout.
            assert elapsed < _POOL_TIMEOUT_SECONDS, (
                f"the run over the bound took {elapsed:.2f}s to be refused; it waited on "
                "the pool instead of being shed"
            )
            assert any("at capacity" in record.getMessage() for record in caplog.records), (
                "an alert the agent had no capacity for must say so; silence here is the "
                "alert vanishing"
            )
            # Shed, not failed: no FAILED record, still sitting at its
            # durable ingress TRIAGE row (invariant 5 — the platform pages a
            # human off this alert regardless).
            assert [snap.state for snap in checkpointer.history(excess.incident_id)] == [
                IncidentState.TRIAGE
            ]
        finally:
            release.set()
            winner.join(_WAIT_SECONDS)

        assert not winner.is_alive()
        # The healthy run was never collateral damage.
        assert [snap.state for snap in checkpointer.history(live.incident_id)] == [
            IncidentState.TRIAGE,
            IncidentState.INVESTIGATING,
            IncidentState.ESCALATED,
        ]

    def test_the_slot_is_released_when_the_run_ends(
        self,
        tiny_engine: Engine,
        tiny_settings: Settings,
        offline_mcp: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A bound that leaked slots would be a slower version of the same wedge."""
        checkpointer = PostgresCheckpointer(tiny_engine)
        slots = RunSlots(tiny_settings.max_concurrent_runs)

        def finish(state: RunState, **kwargs: Any) -> RunState:
            ckpt: PostgresCheckpointer = kwargs["checkpointer"]
            final = state.with_state(IncidentState.ESCALATED, datetime.now(UTC))
            ckpt.write(final)
            return final

        monkeypatch.setattr(app_module, "run_to_completion", finish)

        for _ in range(3):
            run = _run_state(tiny_settings, uuid4())
            checkpointer.write(run)
            app_module._run_investigation(
                run, tiny_settings, checkpointer, engine=tiny_engine, slots=slots
            )
            assert [snap.state for snap in checkpointer.history(run.incident_id)] == [
                IncidentState.TRIAGE,
                IncidentState.ESCALATED,
            ], "a sequential run was refused — the previous run never gave its slot back"
