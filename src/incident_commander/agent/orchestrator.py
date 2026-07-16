"""Explicit state-machine dispatch for the incident run loop (ADR-0002).

Transitions are functions that take a ``RunState`` and return the next ``RunState``.
Dispatch validates that the returned state is in the allowed-successor set for the
current state; anything else is a bug that surfaces as a raised exception, not a
silent transition. Transition bodies are stubbed here and land in follow-on PRs.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol
from uuid import UUID

from incident_commander.agent.state import IncidentState, RunState

Transition = Callable[[RunState], RunState]


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
    IncidentState.VERIFYING: frozenset(
        {
            IncidentState.RESOLVED,
            IncidentState.PLANNING,
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
    def transition(run_state: RunState) -> RunState:
        raise NotImplementedError(
            f"{name} transition not implemented; see docs/ADR/0002 and Phase 0 exit criteria"
        )

    return transition


TRANSITIONS: dict[IncidentState, Transition] = {
    IncidentState.TRIAGE: _stub("triage"),
    IncidentState.INVESTIGATING: _stub("investigate"),
    IncidentState.PLANNING: _stub("plan"),
    IncidentState.AWAITING_APPROVAL: _stub("await_approval"),
    IncidentState.REMEDIATING: _stub("remediate"),
    IncidentState.VERIFYING: _stub("verify"),
}


def dispatch(run_state: RunState) -> RunState:
    """Run one transition from the current state.

    Raises ``TerminalStateError`` if called on a terminal state,
    ``InvalidTransitionError`` if the transition produces a disallowed successor.
    """
    if run_state.state.is_terminal:
        raise TerminalStateError(f"dispatch called on terminal state {run_state.state.value}")
    transition = TRANSITIONS[run_state.state]
    next_run_state = transition(run_state)
    allowed = ALLOWED_TRANSITIONS[run_state.state]
    if next_run_state.state not in allowed:
        raise InvalidTransitionError(
            f"transition from {run_state.state.value} produced disallowed state "
            f"{next_run_state.state.value}; allowed="
            f"{sorted(s.value for s in allowed)}"
        )
    return next_run_state
