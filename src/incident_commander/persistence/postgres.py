"""Postgres-backed ``Checkpointer`` — append-only per-incident snapshot log."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from incident_commander.agent.state import RunState


class PostgresCheckpointer:
    """Writes each RunState snapshot as a new row keyed by (incident_id, version)."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def load(self, incident_id: UUID) -> RunState | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT run_state FROM run_snapshots "
                    "WHERE incident_id = :incident_id "
                    "ORDER BY version DESC LIMIT 1"
                ),
                {"incident_id": str(incident_id)},
            ).first()
        if row is None:
            return None
        payload: Any = row[0]
        return RunState.model_validate(payload)

    def write(self, run_state: RunState) -> None:
        payload = json.loads(run_state.model_dump_json())
        for attempt in range(3):
            next_version = self._next_version(run_state.incident_id)
            try:
                with self._engine.begin() as conn:
                    conn.execute(
                        text(
                            "INSERT INTO run_snapshots "
                            "(incident_id, version, state, run_state) "
                            "VALUES (:incident_id, :version, :state, "
                            "CAST(:run_state AS JSONB))"
                        ),
                        {
                            "incident_id": str(run_state.incident_id),
                            "version": next_version,
                            "state": run_state.state.value,
                            "run_state": json.dumps(payload),
                        },
                    )
                return
            except IntegrityError:
                # Concurrent writer took our version. Retry with a fresh one.
                if attempt == 2:
                    raise

    def history(self, incident_id: UUID) -> list[RunState]:
        """Ordered snapshots. Testing / debugging convenience — not on the Protocol."""
        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT run_state FROM run_snapshots "
                    "WHERE incident_id = :incident_id "
                    "ORDER BY version ASC"
                ),
                {"incident_id": str(incident_id)},
            ).all()
        return [RunState.model_validate(row[0]) for row in rows]

    def reconcile(self, incident_id: UUID) -> RunState | None:
        """Reconciliation entry point.

        Today: returns the latest snapshot (same as ``load``). When Tier 1 actions
        land in Phase 6, this also queries the platform's audit log to check
        whether the last proposed action actually executed before resume-planning.
        """
        return self.load(incident_id)

    def _next_version(self, incident_id: UUID) -> int:
        with self._engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT COALESCE(MAX(version), -1) + 1 AS next "
                    "FROM run_snapshots WHERE incident_id = :incident_id"
                ),
                {"incident_id": str(incident_id)},
            ).first()
        assert row is not None
        return int(row[0])
