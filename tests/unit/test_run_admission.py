"""The concurrency ceiling's arithmetic and the semaphore that enforces it (ADR 0022).

The ceiling is not a number somebody picked; it is a division. These tests pin
the division, and — more importantly — pin the refusals, because the way this
fix fails is not by computing a wrong ceiling but by an operator raising
``AGENT_MAX_CONCURRENT_RUNS`` past what the pool can serve and reintroducing
the deadlock with a config change.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from incident_commander.config import Settings
from incident_commander.persistence.pool import RunSlots


def _settings(**overrides: Any) -> Settings:
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


class TestPoolDefaults:
    def test_defaults_are_explicit_and_not_sqlalchemy_s(self) -> None:
        """5 + 10 with a 30s timeout is what nobody choosing looks like."""
        settings = _settings()
        assert (settings.db_pool_size, settings.db_max_overflow) == (10, 10)
        assert settings.db_pool_capacity == 20
        assert settings.db_pool_timeout_seconds < 30.0

    def test_default_ceiling_is_the_documented_arithmetic(self) -> None:
        """(20 capacity - 4 reserved for ingest) // 2 per run = 8 runs."""
        settings = _settings()
        assert settings.db_ingest_reserved_connections == 4
        assert settings.max_concurrent_runs == 8

    def test_every_admitted_run_can_hold_its_lease_and_still_write(self) -> None:
        """The property the whole ADR exists for, asserted as arithmetic.

        Peak demand is every live run holding its lease AND writing a
        checkpoint at the same instant. That must fit inside the pool with the
        ingest reservation still intact, or a checkpoint write can block on a
        connection only another lease holder could release.
        """
        settings = _settings()
        peak = settings.max_concurrent_runs * 2
        assert peak + settings.db_ingest_reserved_connections <= settings.db_pool_capacity


class TestCeilingArithmetic:
    @pytest.mark.parametrize(
        ("pool_size", "overflow", "reserved", "expected"),
        [
            (2, 0, 0, 1),  # the integration tier's tiny pool
            (10, 10, 4, 8),  # the shipped default
            (5, 10, 0, 7),  # SQLAlchemy's old implicit pool, bounded honestly
            (20, 20, 8, 16),
            (3, 0, 1, 1),  # rounds down; a partial run is not a run
        ],
    )
    def test_ceiling_is_capacity_minus_reservation_over_two(
        self, pool_size: int, overflow: int, reserved: int, expected: int
    ) -> None:
        settings = _settings(
            db_pool_size=pool_size,
            db_max_overflow=overflow,
            db_ingest_reserved_connections=reserved,
        )
        assert settings.max_concurrent_runs == expected

    def test_an_override_may_lower_the_bound(self) -> None:
        """Fewer runs than the pool allows is always safe — cost, not capacity."""
        settings = _settings(agent_max_concurrent_runs=3)
        assert settings.max_concurrent_runs == 3


class TestUnsafeConfigurationIsRefusedAtStartup:
    """Structural, not documented: an unsafe pool must not boot.

    A pool too small for its bound does not announce itself. It shows up later
    as a stall under load, during an incident, which is the exact failure this
    work removed. Refusing at construction is the only honest time to find out.
    """

    def test_override_above_the_ceiling_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="exceeds the 8 runs"):
            _settings(agent_max_concurrent_runs=9)

    def test_a_pool_too_small_for_one_run_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="No run could finish"):
            _settings(db_pool_size=1, db_max_overflow=0)

    def test_a_reservation_that_eats_the_pool_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="No run could finish"):
            _settings(db_pool_size=4, db_max_overflow=0, db_ingest_reserved_connections=3)

    def test_the_error_names_the_knobs_that_fix_it(self) -> None:
        """An operator reading this at 3am needs the lever, not just the verdict."""
        with pytest.raises(ValidationError) as caught:
            _settings(agent_max_concurrent_runs=99)
        message = str(caught.value)
        assert "DB_POOL_SIZE" in message and "DB_MAX_OVERFLOW" in message


class TestRunSlots:
    def test_admits_up_to_the_ceiling(self) -> None:
        slots = RunSlots(2)
        with slots.acquire() as first, slots.acquire() as second:
            assert (first, second) == (True, True)

    def test_refuses_beyond_the_ceiling(self) -> None:
        slots = RunSlots(2)
        with slots.acquire(), slots.acquire(), slots.acquire() as third:
            assert third is False

    def test_refusal_does_not_consume_a_slot(self) -> None:
        """A bounded semaphore over-released would raise; a leaked one would starve."""
        slots = RunSlots(1)
        with slots.acquire() as held:
            assert held is True
            with slots.acquire() as refused:
                assert refused is False
        with slots.acquire() as again:
            assert again is True

    def test_a_slot_is_released_when_the_body_raises(self) -> None:
        slots = RunSlots(1)
        with pytest.raises(RuntimeError, match="boom"), slots.acquire():
            raise RuntimeError("boom")
        with slots.acquire() as again:
            assert again is True, "a crashed run kept its slot forever"

    def test_a_ceiling_below_one_is_refused(self) -> None:
        with pytest.raises(ValueError, match="at least 1"):
            RunSlots(0)
