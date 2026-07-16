from datetime import datetime

import pytest

from incident_commander.agent.orchestrator import (
    ALLOWED_TRANSITIONS,
    TRANSITIONS,
    InvalidTransitionError,
    TerminalStateError,
    dispatch,
)
from incident_commander.agent.state import IncidentState, RunState


def _reachable_from(start: IncidentState) -> set[IncidentState]:
    seen: set[IncidentState] = {start}
    frontier: set[IncidentState] = {start}
    while frontier:
        next_frontier: set[IncidentState] = set()
        for state in frontier:
            for successor in ALLOWED_TRANSITIONS[state]:
                if successor not in seen:
                    seen.add(successor)
                    next_frontier.add(successor)
        frontier = next_frontier
    return seen


class TestAllowedTransitions:
    def test_every_state_declared(self) -> None:
        for state in IncidentState:
            assert state in ALLOWED_TRANSITIONS

    def test_terminal_states_have_no_outgoing_edges(self) -> None:
        for state in IncidentState:
            if state.is_terminal:
                assert ALLOWED_TRANSITIONS[state] == frozenset()

    def test_non_terminal_states_have_outgoing_edges(self) -> None:
        for state in IncidentState:
            if not state.is_terminal:
                assert len(ALLOWED_TRANSITIONS[state]) > 0

    def test_every_state_reachable_from_triage(self) -> None:
        reachable = _reachable_from(IncidentState.TRIAGE)
        for state in IncidentState:
            assert state in reachable, f"{state.value} unreachable from TRIAGE"

    def test_every_non_terminal_state_can_reach_a_terminal_state(self) -> None:
        for start in IncidentState:
            if start.is_terminal:
                continue
            reachable = _reachable_from(start)
            assert any(s.is_terminal for s in reachable), (
                f"{start.value} cannot reach any terminal state"
            )


class TestTransitionsRegistry:
    def test_transition_registered_for_every_non_terminal_state(self) -> None:
        for state in IncidentState:
            if state.is_terminal:
                assert state not in TRANSITIONS
            else:
                assert state in TRANSITIONS

    def test_stubs_raise_not_implemented(self, run_state: RunState, now: datetime) -> None:
        # TRIAGE is now real; every other non-terminal state is still stubbed.
        stubbed = [s for s in IncidentState if not s.is_terminal and s is not IncidentState.TRIAGE]
        for state in stubbed:
            state_run = run_state.model_copy(update={"state": state})
            with pytest.raises(NotImplementedError):
                TRANSITIONS[state](state_run, now)


class TestDispatch:
    @pytest.mark.parametrize(
        "state",
        [IncidentState.RESOLVED, IncidentState.ESCALATED, IncidentState.FAILED],
    )
    def test_terminal_state_rejected(
        self, run_state: RunState, now: datetime, state: IncidentState
    ) -> None:
        state_run = run_state.model_copy(update={"state": state})
        with pytest.raises(TerminalStateError):
            dispatch(state_run, now)

    def test_disallowed_transition_rejected(
        self,
        run_state: RunState,
        now: datetime,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def bad_transition(rs: RunState, at: datetime) -> RunState:
            return rs.with_state(IncidentState.REMEDIATING, at)

        monkeypatch.setitem(TRANSITIONS, IncidentState.TRIAGE, bad_transition)
        with pytest.raises(InvalidTransitionError, match="disallowed"):
            dispatch(run_state, now)

    def test_allowed_transition_returned(
        self,
        run_state: RunState,
        now: datetime,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def good_transition(rs: RunState, at: datetime) -> RunState:
            return rs.with_state(IncidentState.INVESTIGATING, at)

        monkeypatch.setitem(TRANSITIONS, IncidentState.TRIAGE, good_transition)
        result = dispatch(run_state, now)
        assert result.state is IncidentState.INVESTIGATING
