"""Where a run's progress is saved: one Postgres row per snapshot, only ever appended to.

A snapshot is never updated or deleted, so the whole history of an incident stays readable and a
crashed run can be resumed from its newest row.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import IntegrityError

from incident_commander.agent.state import RunState


class PostgresCheckpointer:
    """Writes each snapshot of a run as a new row, identified by incident and version number.

    The version is what makes the log append-only: two writers cannot take the same one, so a
    snapshot can never overwrite another (ADR 0016).
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def load(self, incident_id: UUID) -> RunState | None:
        """The newest snapshot for this incident, or ``None`` if none was ever written."""
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
        """Append one snapshot of a run, retrying if another writer took the version first.

        Each try holds ONE connection for ONE transaction, because a run already has a connection
        pinned for its lease and asking the same pool for a second one can deadlock (ADR 0022).
        """
        # 1. Serialise the run through Pydantic first, so a model that cannot be dumped fails here
        #    rather than halfway through a database transaction.
        payload = json.loads(run_state.model_dump_json())
        # 2. Read the next free version number and insert the row inside ONE transaction. Doing the
        #    read inside it is the point: no other writer can claim the same version (ADR 0016).
        for attempt in range(3):
            try:
                with self._engine.begin() as conn:
                    next_version = self._next_version(conn, run_state.incident_id)
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
            # 3. Another writer took that version between our read and our insert, so the unique
            #    constraint rejected the row: read the next version again and retry, up to 3 times.
            except IntegrityError:
                if attempt == 2:
                    raise

    def history(self, incident_id: UUID) -> list[RunState]:
        """Every snapshot for one incident, oldest first. For tests and debugging only.

        Deliberately not part of the ``Checkpointer`` protocol: the run loop reads the newest
        snapshot and nothing else, and should not be able to reach the whole history.
        """
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
        """Where resuming a run will one day check the world as well as our own records.

        Today it is exactly ``load``. Phase 6 adds the other half: reading the platform's audit log
        to find out whether an action this run proposed was actually carried out.
        """
        return self.load(incident_id)

    def _next_version(self, conn: Connection, incident_id: UUID) -> int:
        """The next free version number for this incident, read on the CALLER's connection.

        Taking the connection as an argument is what puts this read in the same transaction as the
        insert that uses it; on its own connection it would be a guess by the time it was used.
        """
        row = conn.execute(
            text(
                "SELECT COALESCE(MAX(version), -1) + 1 AS next "
                "FROM run_snapshots WHERE incident_id = :incident_id"
            ),
            {"incident_id": str(incident_id)},
        ).first()
        assert row is not None
        return int(row[0])
