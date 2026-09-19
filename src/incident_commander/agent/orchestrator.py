"""Explicit state-machine dispatch for the incident run loop (ADR-0002).

A transition returns the next ``RunState``; dispatch raises on any successor
outside ``ALLOWED_TRANSITIONS``.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Protocol
from uuid import UUID

from incident_commander.agent.state import IncidentState, RunState
from incident_commander.agent.triage import transition_triage

Transition = Callable[[RunState, datetime], RunState]


ALLOWED_TRANSITIONS: dict[IncidentState, frozenset[IncidentState]] = {
    IncidentState.TRIAGE: frozenset(
        {IncidentState.INVESTIGATING, IncidentState.ESCALATED, IncidentState.FAILED}
    ),
    IncidentState.INVESTIGATING: frozenset(
        {IncidentState.PLANNING, IncidentState.ESCALATED, IncidentState.FAILED}
    ),
    IncidentState.PLANNING: frozenset(
        {
            IncidentState.AWAITING_APPROVAL,
            IncidentState.REMEDIATING,
            IncidentState.ESCALATED,
            IncidentState.FAILED,
        }
    ),
    IncidentState.AWAITING_APPROVAL: frozenset(
        {IncidentState.REMEDIATING, IncidentState.ESCALATED, IncidentState.FAILED}
    ),
    IncidentState.REMEDIATING: frozenset(
        {IncidentState.VERIFYING, IncidentState.ESCALATED, IncidentState.FAILED}
    ),
    # INVESTIGATING, never PLANNING (ADR 0056, superseding ADR 0008): a failed
    # attempt says the diagnosis was wrong, so the retry gathers evidence rather
    # than re-planning against the ledger that produced the failure.
    IncidentState.VERIFYING: frozenset(
        {
            IncidentState.INVESTIGATING,
            IncidentState.RESOLVED,
            IncidentState.ESCALATED,
            IncidentState.FAILED,
        }
    ),
    IncidentState.RESOLVED: frozenset(),
    IncidentState.ESCALATED: frozenset(),
    IncidentState.FAILED: frozenset(),
}


class Checkpointer(Protocol):
    """Persistence port for run state. Implementations write transactionally."""

    def load(self, incident_id: UUID) -> RunState | None: ...

    def write(self, run_state: RunState) -> None: ...


class InvalidTransitionError(RuntimeError):
    """A transition produced a next state not in ``ALLOWED_TRANSITIONS[current]``."""


class TerminalStateError(RuntimeError):
    """``dispatch`` was called on a terminal state; the run is done."""


def _stub(name: str) -> Transition:
    def transition(run_state: RunState, at: datetime) -> RunState:
        raise NotImplementedError(
            f"{name} transition not implemented; see docs/ADR/0002 and Phase 0 exit criteria"
        )

    return transition


TRANSITIONS: dict[IncidentState, Transition] = {
    IncidentState.TRIAGE: transition_triage,
    IncidentState.INVESTIGATING: _stub("investigate"),
    IncidentState.PLANNING: _stub("plan"),
    IncidentState.AWAITING_APPROVAL: _stub("await_approval"),
    IncidentState.REMEDIATING: _stub("remediate"),
    IncidentState.VERIFYING: _stub("verify"),
}


def dispatch(
    run_state: RunState,
    at: datetime,
    transitions: dict[IncidentState, Transition] | None = None,
) -> RunState:
    """Run one transition from the current state.

    ``transitions`` overrides the module registry without mutating the global.
    """
    if run_state.state.is_terminal:
        raise TerminalStateError(f"dispatch called on terminal state {run_state.state.value}")
    registry = TRANSITIONS if transitions is None else transitions
    transition = registry[run_state.state]
    next_run_state = transition(run_state, at)
    allowed = ALLOWED_TRANSITIONS[run_state.state]
    if next_run_state.state not in allowed:
        raise InvalidTransitionError(
            f"transition from {run_state.state.value} produced disallowed state "
            f"{next_run_state.state.value}; allowed="
            f"{sorted(s.value for s in allowed)}"
        )
    return next_run_state
