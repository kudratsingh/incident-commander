from datetime import datetime
from uuid import uuid4

from incident_commander.agent.state import IncidentState, RunState
from incident_commander.persistence.memory import InMemoryCheckpointer


class TestInMemoryCheckpointer:
    def test_load_returns_none_for_unknown(self) -> None:
        ckpt = InMemoryCheckpointer()
        assert ckpt.load(uuid4()) is None

    def test_write_then_load_returns_latest(self, run_state: RunState, now: datetime) -> None:
        ckpt = InMemoryCheckpointer()
        ckpt.write(run_state)
        later = run_state.with_state(IncidentState.INVESTIGATING, now)
        ckpt.write(later)
        loaded = ckpt.load(run_state.incident_id)
        assert loaded == later

    def test_history_returns_ordered_snapshots(self, run_state: RunState, now: datetime) -> None:
        ckpt = InMemoryCheckpointer()
        ckpt.write(run_state)
        later = run_state.with_state(IncidentState.INVESTIGATING, now)
        ckpt.write(later)
        history = ckpt.history(run_state.incident_id)
        assert [rs.state for rs in history] == [
            IncidentState.TRIAGE,
            IncidentState.INVESTIGATING,
        ]

    def test_history_empty_for_unknown(self) -> None:
        ckpt = InMemoryCheckpointer()
        assert ckpt.history(uuid4()) == []

    def test_writes_per_incident_isolated(self, run_state: RunState, now: datetime) -> None:
        ckpt = InMemoryCheckpointer()
        other = run_state.model_copy(update={"incident_id": uuid4()})
        ckpt.write(run_state)
        ckpt.write(other)
        assert ckpt.load(run_state.incident_id) == run_state
        assert ckpt.load(other.incident_id) == other
