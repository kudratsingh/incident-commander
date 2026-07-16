from collections.abc import Callable
from datetime import datetime, timedelta
from uuid import uuid4

import pytest

from incident_commander.agent.loop import MaxStepsExceededError, run_to_completion
from incident_commander.agent.orchestrator import TRANSITIONS, TerminalStateError
from incident_commander.agent.state import BudgetLedger, IncidentState, RunState
from incident_commander.persistence.memory import InMemoryCheckpointer


def _make_clock(start: datetime, step_seconds: float = 1.0) -> Callable[[], datetime]:
    ticks = {"i": 0}

    def clock() -> datetime:
        i = ticks["i"]
        ticks["i"] = i + 1
        return start + timedelta(seconds=i * step_seconds)

    return clock


def _with_alert(run_state: RunState, alert: dict[str, object]) -> RunState:
    return run_state.model_copy(update={"alert": alert})


class TestRunToCompletion:
    def test_noise_alert_terminates_at_escalated(self, run_state: RunState, now: datetime) -> None:
        run = _with_alert(run_state, {"source": "billing", "severity": "info"})
        result = run_to_completion(run, clock=_make_clock(now))
        assert result.state is IncidentState.ESCALATED

    def test_actionable_alert_hits_stubbed_investigate(
        self, run_state: RunState, now: datetime
    ) -> None:
        run = _with_alert(run_state, {"source": "billing", "severity": "high"})
        with pytest.raises(NotImplementedError):
            run_to_completion(run, clock=_make_clock(now))

    @pytest.mark.parametrize(
        "state",
        [IncidentState.RESOLVED, IncidentState.ESCALATED, IncidentState.FAILED],
    )
    def test_terminal_start_rejected(
        self, run_state: RunState, now: datetime, state: IncidentState
    ) -> None:
        terminal = run_state.model_copy(update={"state": state})
        with pytest.raises(TerminalStateError):
            run_to_completion(terminal, clock=_make_clock(now))

    def test_checkpoints_initial_and_after_each_transition(
        self, run_state: RunState, now: datetime
    ) -> None:
        run = _with_alert(run_state, {"source": "billing", "severity": "info"})
        ckpt = InMemoryCheckpointer()
        result = run_to_completion(run, clock=_make_clock(now), checkpointer=ckpt)
        history = ckpt.history(result.incident_id)
        assert [rs.state for rs in history] == [
            IncidentState.TRIAGE,
            IncidentState.ESCALATED,
        ]

    def test_runs_without_checkpointer(self, run_state: RunState, now: datetime) -> None:
        run = _with_alert(run_state, {"source": "billing", "severity": "info"})
        result = run_to_completion(run, clock=_make_clock(now))
        assert result.state is IncidentState.ESCALATED

    def test_exhausted_budget_escalates_before_dispatch(
        self, budget: BudgetLedger, now: datetime
    ) -> None:
        exhausted = budget.model_copy(update={"tool_calls_used": budget.max_tool_calls})
        run = RunState(
            incident_id=uuid4(),
            state=IncidentState.TRIAGE,
            alert={"source": "billing", "severity": "high"},
            budget=exhausted,
            created_at=now,
            updated_at=now,
        )
        result = run_to_completion(run, clock=_make_clock(now))
        assert result.state is IncidentState.ESCALATED
        reasons = [entry.arguments.get("reason") for entry in result.evidence]
        assert "budget exhausted" in reasons

    def test_max_steps_guard(
        self,
        run_state: RunState,
        now: datetime,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def to_planning(rs: RunState, at: datetime) -> RunState:
            return rs.with_state(IncidentState.PLANNING, at)

        monkeypatch.setitem(TRANSITIONS, IncidentState.INVESTIGATING, to_planning)
        run = _with_alert(run_state, {"source": "billing", "severity": "high"})
        with pytest.raises(MaxStepsExceededError):
            run_to_completion(run, clock=_make_clock(now), max_steps=1)

    def test_transition_stamps_use_clock(self, run_state: RunState, now: datetime) -> None:
        later = now + timedelta(hours=1)

        def fixed_clock() -> datetime:
            return later

        run = _with_alert(run_state, {"source": "billing", "severity": "info"})
        ckpt = InMemoryCheckpointer()
        result = run_to_completion(run, clock=fixed_clock, checkpointer=ckpt)
        history = ckpt.history(result.incident_id)
        assert history[0].updated_at == now
        assert history[1].updated_at == later
        assert result.updated_at == later
