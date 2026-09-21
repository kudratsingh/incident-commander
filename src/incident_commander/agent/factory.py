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

# Fixed forever (ADR 0016). Regenerating it would re-point every existing
# fingerprint at a different incident id and silently un-dedupe the fleet.
_INCIDENT_NAMESPACE: Final[UUID] = UUID("d0f7dd54-e4fc-49f6-b507-f4becc6886a3")

# Cap on the recurrence walk: a pathological flapping fingerprint degrades to
# a fresh uuid4 rather than looping over an unbounded generation chain.
_MAX_RECURRENCE_GENERATIONS: Final[int] = 64


def derive_incident_id(alert: Mapping[str, object], checkpointer: Checkpointer) -> UUID:
    """``uuid5`` over ``dedup_key`` (ADR 0016), a generation per terminated run so a
    recurrence opens a new incident. No usable fingerprint means no dedupe: ``uuid4()``.
    """
    raw_fingerprint = alert.get("fingerprint")
    if not isinstance(raw_fingerprint, str) or not raw_fingerprint.strip():
        # Keyed on the RAW field: the hash would fuse every fingerprint-less alert.
        return uuid4()

    key = dedup_key(alert)
    for generation in range(_MAX_RECURRENCE_GENERATIONS):
        candidate = uuid5(_INCIDENT_NAMESPACE, key if generation == 0 else f"{key}|{generation}")
        latest = checkpointer.load(candidate)
        if latest is None or not latest.state.is_terminal:
            # Absent → fresh incident. Non-terminal → join it: a duplicate
            # delivery, or a crashed run for the single-flight lease to resume.
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
