"""In-memory ``Checkpointer`` for tests and the demo compose. Not durable."""

from __future__ import annotations

from uuid import UUID

from incident_commander.agent.state import RunState


class InMemoryCheckpointer:
    """Stores every write in a per-incident list. ``load`` returns the latest."""

    def __init__(self) -> None:
        self._store: dict[UUID, list[RunState]] = {}

    def load(self, incident_id: UUID) -> RunState | None:
        entries = self._store.get(incident_id)
        return entries[-1] if entries else None

    def write(self, run_state: RunState) -> None:
        self._store.setdefault(run_state.incident_id, []).append(run_state)

    def history(self, incident_id: UUID) -> list[RunState]:
        """Ordered snapshots. Testing convenience — not part of the Protocol."""
        return list(self._store.get(incident_id, []))
