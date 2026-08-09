"""Single-flight lease and crash-resume against real Postgres (ADR 0016).

ADR 0002 promised "a single-flight lease per incident id ... guarantees one
live run per incident" and named the integration test it wanted — "race two
alerts for one incident" (line 52) — which was never written. This is it.

`pg_try_advisory_lock` is session-scoped, so the whole point is that the lock
lives on ONE pinned connection for the duration of the run; nothing but a real
Postgres session can prove that, hence the testcontainers tier. Skips cleanly
without a Docker daemon like every other test in this tree (see conftest).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine

from incident_commander.agent.factory import start_run
from incident_commander.agent.state import EvidenceEntry, IncidentState, RunState
from incident_commander.api import app as app_module
from incident_commander.config import Settings
from incident_commander.persistence.lease import incident_lease
from incident_commander.persistence.postgres import PostgresCheckpointer

_WAIT_SECONDS = 15.0


def _test_settings() -> Settings:
    defaults: dict[str, Any] = {
        "anthropic_api_key": SecretStr("sk-ant-test"),
        "judge_model": "claude-haiku-4-5",
        "platform_mcp_url": "https://mcp.local",
        "platform_rest_url": "https://api.local",
        "platform_token": SecretStr("svc-token"),
        "platform_webhook_secret": SecretStr("hmac-secret"),
        "database_url": "postgresql://u:p@localhost:5432/db",
    }
    return Settings(_env_file=None, **defaults)  # type: ignore[call-arg]


def _run_state(incident_id: UUID) -> RunState:
    return start_run(
        {"source": "billing", "severity": "high", "fingerprint": "kafka-lag-spike"},
        _test_settings(),
        datetime.now(UTC),
        incident_id=incident_id,
    )


@contextmanager
def _fake_client(_settings: Settings) -> Iterator[object]:
    """Stand in for ``make_client``: the lease, not the transport, is under test."""
    yield object()


@pytest.fixture
def offline_mcp(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_module, "make_client", _fake_client)


class TestIncidentLease:
    def test_second_holder_is_refused_while_the_first_holds_it(self, clean_engine: Engine) -> None:
        incident_id = uuid4()
        with incident_lease(clean_engine, incident_id) as first:
            assert first is True
            with incident_lease(clean_engine, incident_id) as second:
                assert second is False

    def test_lease_is_released_on_exit(self, clean_engine: Engine) -> None:
        incident_id = uuid4()
        with incident_lease(clean_engine, incident_id) as first:
            assert first is True
        with incident_lease(clean_engine, incident_id) as again:
            assert again is True

    def test_lease_is_released_when_the_body_raises(self, clean_engine: Engine) -> None:
        incident_id = uuid4()
        with pytest.raises(RuntimeError, match="boom"), incident_lease(clean_engine, incident_id):
            raise RuntimeError("boom")
        with incident_lease(clean_engine, incident_id) as again:
            assert again is True

    def test_distinct_incidents_do_not_block_each_other(self, clean_engine: Engine) -> None:
        with (
            incident_lease(clean_engine, uuid4()) as first,
            incident_lease(clean_engine, uuid4()) as second,
        ):
            assert (first, second) == (True, True)


class TestSingleFlightRace:
    """Two concurrent ``_run_investigation`` calls for one incident id."""

    def test_loser_writes_nothing_and_history_has_one_writer(
        self,
        clean_engine: Engine,
        offline_mcp: None,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        checkpointer = PostgresCheckpointer(clean_engine)
        run = _run_state(uuid4())
        checkpointer.write(run)  # the ingress TRIAGE row

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
            args=(run, _test_settings(), checkpointer),
            kwargs={"engine": clean_engine},
        )
        winner.start()
        try:
            assert holding.wait(_WAIT_SECONDS), "the first run never took the lease"
            before = [snap.state for snap in checkpointer.history(run.incident_id)]

            # The redelivery's task: same incident, lease already held.
            with caplog.at_level(logging.INFO, logger="incident_commander.api.app"):
                app_module._run_investigation(
                    run, _test_settings(), checkpointer, engine=clean_engine
                )

            assert [snap.state for snap in checkpointer.history(run.incident_id)] == before
            assert any("lease" in record.getMessage() for record in caplog.records)
        finally:
            release.set()
            winner.join(_WAIT_SECONDS)

        assert not winner.is_alive()
        assert [snap.state for snap in checkpointer.history(run.incident_id)] == [
            IncidentState.TRIAGE,
            IncidentState.INVESTIGATING,
            IncidentState.ESCALATED,
        ]

    def test_lease_is_available_again_after_the_winner_finishes(
        self, clean_engine: Engine, offline_mcp: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        checkpointer = PostgresCheckpointer(clean_engine)
        run = _run_state(uuid4())
        checkpointer.write(run)
        monkeypatch.setattr(
            app_module,
            "run_to_completion",
            lambda state, **_kwargs: state.with_state(IncidentState.ESCALATED, datetime.now(UTC)),
        )

        app_module._run_investigation(run, _test_settings(), checkpointer, engine=clean_engine)

        with incident_lease(clean_engine, run.incident_id) as acquired:
            assert acquired is True, "the run's connection kept the advisory lock after it ended"


class TestCrashResume:
    def test_reinvocation_continues_from_the_investigating_checkpoint(
        self, clean_engine: Engine, offline_mcp: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Kill after an INVESTIGATING checkpoint, re-invoke, and the run must
        continue from it rather than restarting at a fresh TRIAGE."""
        checkpointer = PostgresCheckpointer(clean_engine)
        run = _run_state(uuid4())
        checkpointer.write(run)
        crashed = run.with_state(IncidentState.INVESTIGATING, datetime.now(UTC)).model_copy(
            update={
                "evidence": (
                    EvidenceEntry(
                        tool_name="get_consumer_lag",
                        arguments={"consumer_group": "worker-dispatcher"},
                        result_summary="lag=12000",
                        timestamp=datetime.now(UTC),
                    ),
                )
            }
        )
        checkpointer.write(crashed)

        seen: list[RunState] = []

        def record(state: RunState, **_kwargs: Any) -> RunState:
            seen.append(state)
            return state.with_state(IncidentState.ESCALATED, datetime.now(UTC))

        monkeypatch.setattr(app_module, "run_to_completion", record)

        # A redelivery hands the task a freshly-minted TRIAGE state for the
        # same incident — exactly what ingress does after a crash.
        app_module._run_investigation(run, _test_settings(), checkpointer, engine=clean_engine)

        assert len(seen) == 1
        assert seen[0].state is IncidentState.INVESTIGATING
        assert [entry.tool_name for entry in seen[0].evidence] == ["get_consumer_lag"]
        assert [snap.state for snap in checkpointer.history(run.incident_id)] == [
            IncidentState.TRIAGE,
            IncidentState.INVESTIGATING,
        ], "resume must not append a fresh TRIAGE row over the crashed run"

    def test_terminal_incident_is_not_resumed(
        self, clean_engine: Engine, offline_mcp: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        checkpointer = PostgresCheckpointer(clean_engine)
        run = _run_state(uuid4())
        checkpointer.write(run.with_state(IncidentState.FAILED, datetime.now(UTC)))

        def explode(_state: RunState, **_kwargs: Any) -> RunState:
            raise AssertionError("a terminal incident must never be re-run")

        monkeypatch.setattr(app_module, "run_to_completion", explode)

        app_module._run_investigation(run, _test_settings(), checkpointer, engine=clean_engine)

        assert [snap.state for snap in checkpointer.history(run.incident_id)] == [
            IncidentState.FAILED
        ]
