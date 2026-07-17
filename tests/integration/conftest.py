"""Integration-test fixtures.

Requires a running Docker daemon. Every test in this tree is skipped when
Docker isn't reachable — CI runs them, local runs skip cleanly unless the
developer starts Docker Desktop first.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import Engine, create_engine, text

from incident_commander.agent.state import BudgetLedger, IncidentState, RunState

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _docker_reachable() -> bool:
    try:
        import docker  # type: ignore[import-untyped]

        docker.from_env().ping()
    except Exception:
        return False
    return True


@pytest.fixture(scope="session")
def _postgres_container() -> Iterator[str]:
    if not _docker_reachable():
        pytest.skip("Docker daemon not reachable; skipping Postgres integration tests")
    from testcontainers.postgres import PostgresContainer  # type: ignore[import-untyped]

    container = PostgresContainer("postgres:16-alpine")
    container.start()
    try:
        raw_url = container.get_connection_url()
        # testcontainers hands back a psycopg2 URL; swap the driver.
        url = raw_url.replace("postgresql+psycopg2://", "postgresql+psycopg://")
        yield url
    finally:
        container.stop()


@pytest.fixture(scope="session")
def _migrated_engine(_postgres_container: str) -> Iterator[Engine]:
    from alembic.config import Config

    from alembic import command

    os.environ["DATABASE_URL"] = _postgres_container
    cfg = Config(str(_PROJECT_ROOT / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_engine(_postgres_container)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def clean_engine(_migrated_engine: Engine) -> Iterator[Engine]:
    with _migrated_engine.begin() as conn:
        conn.execute(text("TRUNCATE TABLE run_snapshots"))
    yield _migrated_engine


@pytest.fixture
def sample_run_state() -> RunState:
    now = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
    return RunState(
        incident_id=uuid4(),
        state=IncidentState.TRIAGE,
        alert={"source": "billing", "severity": "high"},
        budget=BudgetLedger(
            max_tool_calls=25,
            max_tokens=200_000,
            max_wall_seconds=1_800,
            max_usd=Decimal("5.00"),
        ),
        created_at=now,
        updated_at=now,
    )
