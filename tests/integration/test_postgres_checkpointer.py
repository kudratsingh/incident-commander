from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import Engine, text

from incident_commander.agent.state import IncidentState, RunState
from incident_commander.persistence.postgres import PostgresCheckpointer


class TestPostgresCheckpointerLoad:
    def test_load_returns_none_for_unknown(self, clean_engine: Engine) -> None:
        ckpt = PostgresCheckpointer(clean_engine)
        assert ckpt.load(uuid4()) is None

    def test_load_returns_latest_snapshot(
        self, clean_engine: Engine, sample_run_state: RunState
    ) -> None:
        ckpt = PostgresCheckpointer(clean_engine)
        ckpt.write(sample_run_state)
        later = sample_run_state.with_state(
            IncidentState.INVESTIGATING,
            datetime(2026, 7, 16, 12, 0, 5, tzinfo=UTC),
        )
        ckpt.write(later)
        loaded = ckpt.load(sample_run_state.incident_id)
        assert loaded == later


class TestPostgresCheckpointerWrite:
    def test_write_then_load_round_trip(
        self, clean_engine: Engine, sample_run_state: RunState
    ) -> None:
        ckpt = PostgresCheckpointer(clean_engine)
        ckpt.write(sample_run_state)
        loaded = ckpt.load(sample_run_state.incident_id)
        assert loaded == sample_run_state

    def test_versions_monotonic_per_incident(
        self, clean_engine: Engine, sample_run_state: RunState
    ) -> None:
        ckpt = PostgresCheckpointer(clean_engine)
        for _ in range(3):
            ckpt.write(sample_run_state)
        with clean_engine.connect() as conn:
            versions = conn.execute(
                text(
                    "SELECT version FROM run_snapshots "
                    "WHERE incident_id = :incident_id ORDER BY version ASC"
                ),
                {"incident_id": str(sample_run_state.incident_id)},
            ).all()
        assert [row[0] for row in versions] == [0, 1, 2]

    def test_versions_scoped_per_incident(
        self, clean_engine: Engine, sample_run_state: RunState
    ) -> None:
        other = sample_run_state.model_copy(update={"incident_id": uuid4()})
        ckpt = PostgresCheckpointer(clean_engine)
        ckpt.write(sample_run_state)
        ckpt.write(other)
        ckpt.write(sample_run_state)
        with clean_engine.connect() as conn:
            first_versions = [
                row[0]
                for row in conn.execute(
                    text(
                        "SELECT version FROM run_snapshots "
                        "WHERE incident_id = :incident_id ORDER BY version ASC"
                    ),
                    {"incident_id": str(sample_run_state.incident_id)},
                ).all()
            ]
            other_versions = [
                row[0]
                for row in conn.execute(
                    text(
                        "SELECT version FROM run_snapshots "
                        "WHERE incident_id = :incident_id ORDER BY version ASC"
                    ),
                    {"incident_id": str(other.incident_id)},
                ).all()
            ]
        assert first_versions == [0, 1]
        assert other_versions == [0]


class TestPostgresCheckpointerHistory:
    def test_history_empty_for_unknown(self, clean_engine: Engine) -> None:
        ckpt = PostgresCheckpointer(clean_engine)
        assert ckpt.history(uuid4()) == []

    def test_history_ordered_ascending(
        self, clean_engine: Engine, sample_run_state: RunState
    ) -> None:
        ckpt = PostgresCheckpointer(clean_engine)
        ckpt.write(sample_run_state)
        second = sample_run_state.with_state(
            IncidentState.INVESTIGATING,
            datetime(2026, 7, 16, 12, 0, 5, tzinfo=UTC),
        )
        ckpt.write(second)
        third = second.with_state(
            IncidentState.ESCALATED,
            datetime(2026, 7, 16, 12, 0, 10, tzinfo=UTC),
        )
        ckpt.write(third)
        history = ckpt.history(sample_run_state.incident_id)
        assert [rs.state for rs in history] == [
            IncidentState.TRIAGE,
            IncidentState.INVESTIGATING,
            IncidentState.ESCALATED,
        ]


class TestPostgresCheckpointerReconcile:
    def test_reconcile_returns_latest(
        self, clean_engine: Engine, sample_run_state: RunState
    ) -> None:
        ckpt = PostgresCheckpointer(clean_engine)
        ckpt.write(sample_run_state)
        later = sample_run_state.with_state(
            IncidentState.INVESTIGATING,
            datetime(2026, 7, 16, 12, 0, 5, tzinfo=UTC),
        )
        ckpt.write(later)
        assert ckpt.reconcile(sample_run_state.incident_id) == later

    def test_reconcile_none_for_unknown(self, clean_engine: Engine) -> None:
        ckpt = PostgresCheckpointer(clean_engine)
        assert ckpt.reconcile(uuid4()) is None
