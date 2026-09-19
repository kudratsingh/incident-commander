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

    def test_verifying_retries_through_investigating_and_never_through_planning(self) -> None:
        # ADR 0056 replaced ADR 0008's single attempt with a capped retry, and kept the
        # half of the pin that was the point: the retry edge goes to INVESTIGATING, so a
        # second attempt is planned from evidence gathered AFTER the failure rather than
        # from the ledger that produced it. A PR that adds PLANNING here flags this test.
        assert IncidentState.PLANNING not in ALLOWED_TRANSITIONS[IncidentState.VERIFYING]
        assert ALLOWED_TRANSITIONS[IncidentState.VERIFYING] == frozenset(
            {
                IncidentState.INVESTIGATING,
                IncidentState.RESOLVED,
                IncidentState.ESCALATED,
                IncidentState.FAILED,
            }
        )

    def test_planning_is_reachable_only_from_investigating(self) -> None:
        # The property the retry edge had to preserve, asserted here as well as in
        # test_grader.py: a second Tier-1 action is reachable only by re-entering the
        # investigation loop, which is what makes it a different attempt.
        sources = sorted(
            state.value
            for state, successors in ALLOWED_TRANSITIONS.items()
            if IncidentState.PLANNING in successors
        )
        assert sources == [IncidentState.INVESTIGATING.value]

    def test_the_only_cycle_runs_through_investigating(self) -> None:
        # A cycle is now legal, so the graph tests above (reachability and
        # terminal-reachability) are no longer trivially acyclic. This names the one
        # cycle the design intends, so a second loop cannot appear unremarked.
        on_a_cycle = sorted(
            state.value
            for state in IncidentState
            if any(state in _reachable_from(s) for s in ALLOWED_TRANSITIONS[state])
        )
        assert on_a_cycle == sorted(
            [
                IncidentState.AWAITING_APPROVAL.value,
                IncidentState.INVESTIGATING.value,
                IncidentState.PLANNING.value,
                IncidentState.REMEDIATING.value,
                IncidentState.VERIFYING.value,
            ]
        ), "the retry edge is the one cycle; a second loop is a design change"


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
