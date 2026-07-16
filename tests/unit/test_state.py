from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from incident_commander.agent.state import (
    BudgetLedger,
    EvidenceEntry,
    IncidentState,
    RunState,
)


class TestIncidentState:
    @pytest.mark.parametrize(
        "state",
        [IncidentState.RESOLVED, IncidentState.ESCALATED, IncidentState.FAILED],
    )
    def test_terminal_states(self, state: IncidentState) -> None:
        assert state.is_terminal

    @pytest.mark.parametrize(
        "state",
        [
            IncidentState.TRIAGE,
            IncidentState.INVESTIGATING,
            IncidentState.PLANNING,
            IncidentState.AWAITING_APPROVAL,
            IncidentState.REMEDIATING,
            IncidentState.VERIFYING,
        ],
    )
    def test_non_terminal_states(self, state: IncidentState) -> None:
        assert not state.is_terminal


class TestBudgetLedger:
    def test_fresh_ledger_not_exhausted(self, budget: BudgetLedger) -> None:
        assert not budget.is_exhausted

    def test_exhausted_when_tool_calls_hit(self, budget: BudgetLedger) -> None:
        exhausted = budget.model_copy(update={"tool_calls_used": budget.max_tool_calls})
        assert exhausted.is_exhausted

    def test_exhausted_when_tokens_hit(self, budget: BudgetLedger) -> None:
        exhausted = budget.model_copy(update={"tokens_used": budget.max_tokens})
        assert exhausted.is_exhausted

    def test_exhausted_when_wall_seconds_hit(self, budget: BudgetLedger) -> None:
        exhausted = budget.model_copy(update={"wall_seconds_used": float(budget.max_wall_seconds)})
        assert exhausted.is_exhausted

    def test_exhausted_when_usd_hit(self, budget: BudgetLedger) -> None:
        exhausted = budget.model_copy(update={"usd_used": budget.max_usd})
        assert exhausted.is_exhausted

    def test_negative_used_rejected(self) -> None:
        with pytest.raises(ValidationError):
            BudgetLedger(
                max_tool_calls=10,
                tool_calls_used=-1,
                max_tokens=1,
                max_wall_seconds=1,
                max_usd=Decimal("1"),
            )


class TestRunState:
    def test_round_trip_isomorphic(self, run_state: RunState) -> None:
        dumped = run_state.model_dump_json()
        loaded = RunState.model_validate_json(dumped)
        assert loaded == run_state

    def test_evidence_appended_via_copy_leaves_original_untouched(
        self, run_state: RunState, now: datetime
    ) -> None:
        entry = EvidenceEntry(
            tool_name="get_consumer_lag",
            arguments={"group": "billing"},
            result_summary="lag=42",
            timestamp=now,
        )
        updated = run_state.model_copy(update={"evidence": (*run_state.evidence, entry)})
        assert len(updated.evidence) == 1
        assert len(run_state.evidence) == 0

    def test_frozen_direct_mutation_rejected(self, run_state: RunState) -> None:
        with pytest.raises(ValidationError):
            run_state.state = IncidentState.INVESTIGATING

    def test_with_state_returns_new_instance(self, run_state: RunState, now: datetime) -> None:
        later = now + timedelta(seconds=5)
        new_state = run_state.with_state(IncidentState.INVESTIGATING, later)
        assert new_state.state is IncidentState.INVESTIGATING
        assert new_state.updated_at == later
        assert run_state.state is IncidentState.TRIAGE
        assert run_state.updated_at == now
