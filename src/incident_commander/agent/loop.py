"""Drive an incident run from an initial state to a terminal state (ADR-0002)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Final

from incident_commander.agent.orchestrator import (
    Checkpointer,
    TerminalStateError,
    Transition,
    dispatch,
)
from incident_commander.agent.state import EvidenceEntry, IncidentState, RunState

_DEFAULT_MAX_STEPS: Final[int] = 100


class MaxStepsExceededError(RuntimeError):
    """A run did not reach a terminal state within ``max_steps``."""


def _escalate(run_state: RunState, reason: str, at: datetime) -> RunState:
    entry = EvidenceEntry(
        tool_name="_escalate",
        arguments={"reason": reason},
        result_summary=f"escalated: {reason}",
        timestamp=at,
    )
    return run_state.model_copy(
        update={
            "state": IncidentState.ESCALATED,
            "updated_at": at,
            "evidence": (*run_state.evidence, entry),
        }
    )


def _accrue_wall_time(run_state: RunState, now: datetime) -> RunState:
    """Advance the wall meter to the elapsed time since ``created_at``.

    Anchored on ``run_state.created_at``, not a loop-local start stamp:
    a run resumed from a checkpoint after a crash must keep every second
    the first process burned, and a local anchor silently resets the
    meter to zero on resume (ADR 0015).

    The monotone guard keeps ``model_copy`` churn down on a clock that
    has not moved, and makes a clock that jumps backwards a no-op rather
    than a meter rewind.
    """
    elapsed = (now - run_state.created_at).total_seconds()
    if elapsed <= run_state.budget.wall_seconds_used:
        return run_state
    return run_state.model_copy(
        update={"budget": run_state.budget.model_copy(update={"wall_seconds_used": elapsed})}
    )


def run_to_completion(
    run_state: RunState,
    clock: Callable[[], datetime],
    checkpointer: Checkpointer | None = None,
    max_steps: int = _DEFAULT_MAX_STEPS,
    transitions: dict[IncidentState, Transition] | None = None,
) -> RunState:
    """Dispatch until the run reaches a terminal state.

    Writes a checkpoint on entry and after every subsequent transition.
    Exhausted budget short-circuits to ``ESCALATED`` with an evidence entry.
    ``max_steps`` bounds runaway bugs; 100 is generous for a real investigation.
    ``transitions`` overrides the module-level registry for dependency wiring.
    """
    if run_state.state.is_terminal:
        raise TerminalStateError(
            f"run_to_completion called on terminal state {run_state.state.value}"
        )
    if checkpointer is not None:
        checkpointer.write(run_state)

    steps = 0
    while not run_state.state.is_terminal:
        if steps >= max_steps:
            raise MaxStepsExceededError(f"run did not terminate within {max_steps} steps")
        # One clock read per iteration, shared by the wall meter and the
        # transition stamp — the meter costs no extra reads.
        now = clock()
        run_state = _accrue_wall_time(run_state, now)
        if run_state.budget.is_exhausted and run_state.state is not IncidentState.VERIFYING:
            # VERIFYING is exempt: once a Tier-1 action executed, the run
            # must always verify it — an executed-but-unverified action is
            # worse than one extra probe over budget. The exemption now
            # covers wall/USD exhaustion too, consistent with ADR 0006.
            run_state = _escalate(run_state, "budget exhausted", now)
        else:
            run_state = dispatch(run_state, now, transitions=transitions)
        if checkpointer is not None:
            checkpointer.write(run_state)
        steps += 1
    return run_state
