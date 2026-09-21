"""A ``Checkpointer`` that keeps snapshots in memory, for tests and the demo. Nothing survives exit.

Same interface as the Postgres one, so a test can run the whole loop without a database. It is not
a fallback: a real deployment that used it would lose every run it was in the middle of.
"""

from __future__ import annotations

from uuid import UUID

from incident_commander.agent.state import RunState


class InMemoryCheckpointer:
    """Keeps one list of snapshots per incident, in the order written; ``load`` returns the last."""

    def __init__(self) -> None:
        self._store: dict[UUID, list[RunState]] = {}

    def load(self, incident_id: UUID) -> RunState | None:
        entries = self._store.get(incident_id)
        return entries[-1] if entries else None

    def write(self, run_state: RunState) -> None:
        self._store.setdefault(run_state.incident_id, []).append(run_state)

    def history(self, incident_id: UUID) -> list[RunState]:
        """Every snapshot for one incident, oldest first. For tests; not on the protocol."""
        return list(self._store.get(incident_id, []))
