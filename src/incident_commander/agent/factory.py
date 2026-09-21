"""Build a fresh ``RunState``; derive the ingress incident identity (ADR 0016)."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime
from typing import Final
from uuid import UUID, uuid4, uuid5

from incident_commander.agent.orchestrator import Checkpointer
from incident_commander.agent.state import BudgetLedger, IncidentState, RunState
from incident_commander.agent.triage import dedup_key
from incident_commander.config import Settings

_log = logging.getLogger(__name__)

# This namespace must never change: incident ids are derived from it, so a new one would point
# every existing alert fingerprint at a different incident and quietly break deduplication.
_INCIDENT_NAMESPACE: Final[UUID] = UUID("d0f7dd54-e4fc-49f6-b507-f4becc6886a3")

# How far the search below will walk for a recurrence. An alert that flaps endlessly falls back
# to a random id rather than walking an unbounded chain of past incidents.
_MAX_RECURRENCE_GENERATIONS: Final[int] = 64


def derive_incident_id(alert: Mapping[str, object], checkpointer: Checkpointer) -> UUID:
    """``uuid5`` over ``dedup_key`` (ADR 0016), a generation per terminated run so a
    recurrence opens a new incident. No usable fingerprint means no dedupe: ``uuid4()``.
    """
    raw_fingerprint = alert.get("fingerprint")
    if not isinstance(raw_fingerprint, str) or not raw_fingerprint.strip():
        # Checked on the raw field before hashing: hashing an absent fingerprint would give
        # every alert without one the same incident id.
        return uuid4()

    key = dedup_key(alert)
    for generation in range(_MAX_RECURRENCE_GENERATIONS):
        candidate = uuid5(_INCIDENT_NAMESPACE, key if generation == 0 else f"{key}|{generation}")
        latest = checkpointer.load(candidate)
        if latest is None or not latest.state.is_terminal:
            # No run under this id means a new incident. A run still in progress means join
            # it: either the alert was delivered twice, or a crashed run needs resuming.
            return candidate
    _log.warning(
        "recurrence chain for dedup key %s exhausted %d generations; "
        "falling back to a non-deterministic incident id",
        key,
        _MAX_RECURRENCE_GENERATIONS,
    )
    return uuid4()


def start_run(
    alert: Mapping[str, object],
    settings: Settings,
    at: datetime,
    incident_id: UUID | None = None,
    *,
    max_tool_calls: int | None = None,
) -> RunState:
    """Build a fresh TRIAGE run with a BudgetLedger seeded from settings.

    The ``uuid4`` default must stay (ADR 0016): non-ingress callers need a distinct
    incident each time. ``max_tool_calls`` overrides the setting for this run only
    (ADR 0019); 0 is ignored.
    """
    return RunState(
        incident_id=incident_id or uuid4(),
        state=IncidentState.TRIAGE,
        alert=dict(alert),
        budget=BudgetLedger(
            max_tool_calls=max_tool_calls or settings.budget_max_tool_calls,
            max_tokens=settings.seeded_max_tokens,
            max_wall_seconds=settings.budget_max_seconds,
            max_usd=settings.seeded_max_usd,
        ),
        created_at=at,
        updated_at=at,
    )
