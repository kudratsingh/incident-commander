from __future__ import annotations

import socket
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from incident_commander.agent.state import BudgetLedger, IncidentState, RunState


class OutboundSocketBlocked(RuntimeError):
    """A unit test tried to open a network connection."""


@pytest.fixture(autouse=True)
def no_outbound_sockets(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No unit test may open a network connection. Enforced, not remembered.

    ``TestOneMutatingScenarioPerInvocation.test_one_mutating_scenario_is_allowed``
    ran ``main()`` under a real-looking live env without stubbing
    ``make_client``, so it reached the write-scope principal guard and fired
    a real MCP ``tools/call`` — a Tier-1-capable payload — at
    ``http://real.host:8001/mcp`` from the local suite. The stub was one
    ``monkeypatch.setattr`` away, which is exactly why per-test discipline is
    the wrong control: the next test to forget it fails the same way, silently.

    The attempt is RECORDED and failed at teardown rather than only raised.
    Raising alone is not enough: the code paths that reach the network here
    are safety guards that fail closed on any exception, so a refused
    connection would be swallowed into a ``PrincipalGuardError`` and an exit
    code the test already tolerates. A recorded violation cannot be caught.
    """
    attempts: list[str] = []

    def _record(address: Any) -> OutboundSocketBlocked:
        attempts.append(repr(address))
        return OutboundSocketBlocked(
            f"unit test attempted an outbound connection to {address!r}. Unit "
            "tests are hermetic: stub the client (or the transport) instead."
        )

    def _blocked_connect(_self: socket.socket, address: Any, *_a: Any, **_kw: Any) -> None:
        raise _record(address)

    def _blocked_create_connection(address: Any, *_a: Any, **_kw: Any) -> None:
        raise _record(address)

    monkeypatch.setattr(socket.socket, "connect", _blocked_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked_connect)
    monkeypatch.setattr(socket, "create_connection", _blocked_create_connection)
    yield
    if attempts:
        pytest.fail(
            f"unit test opened {len(attempts)} outbound connection(s): "
            f"{', '.join(attempts)}. A unit test that talks to the network is "
            "a live-fire hazard — stub the client at its seam."
        )


@pytest.fixture
def budget() -> BudgetLedger:
    return BudgetLedger(
        max_tool_calls=25,
        max_tokens=200_000,
        max_wall_seconds=1_800,
        max_usd=Decimal("5.00"),
    )


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 7, 15, 20, 0, tzinfo=UTC)


@pytest.fixture
def run_state(budget: BudgetLedger, now: datetime) -> RunState:
    return RunState(
        incident_id=uuid4(),
        state=IncidentState.TRIAGE,
        alert={"source": "test", "severity": "high"},
        budget=budget,
        created_at=now,
        updated_at=now,
    )
