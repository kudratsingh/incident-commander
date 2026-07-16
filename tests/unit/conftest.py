from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from incident_commander.agent.state import BudgetLedger, IncidentState, RunState


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
