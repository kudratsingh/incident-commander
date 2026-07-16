"""Factories that build a fresh ``RunState`` from external inputs."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from uuid import UUID, uuid4

from incident_commander.agent.state import BudgetLedger, IncidentState, RunState
from incident_commander.config import Settings


def start_run(
    alert: Mapping[str, object],
    settings: Settings,
    at: datetime,
    incident_id: UUID | None = None,
) -> RunState:
    """Build a fresh TRIAGE-state run with a BudgetLedger seeded from settings."""
    return RunState(
        incident_id=incident_id or uuid4(),
        state=IncidentState.TRIAGE,
        alert=dict(alert),
        budget=BudgetLedger(
            max_tool_calls=settings.budget_max_tool_calls,
            max_tokens=settings.budget_max_tokens,
            max_wall_seconds=settings.budget_max_seconds,
            max_usd=settings.budget_max_usd,
        ),
        created_at=at,
        updated_at=at,
    )
