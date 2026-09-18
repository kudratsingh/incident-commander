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
    """End the run at ESCALATED, with the reason recorded on the evidence ledger."""
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


def _budget_exemption(run_state: RunState, *, resuming: bool) -> str | None:
    """Name the ADR 0006 exemption that lets this step run over budget, or None.

    Both protect one invariant: an executed Tier-1 action is always verified, or
    escalated with the fact declared. Re-invoking after a crash resume is safe
    because the rebuilt idempotency key makes the platform replay (ADR 0008).
    """
    if run_state.state is IncidentState.VERIFYING:
        return "verify-after-execute"
    if (
        resuming
        and run_state.state is IncidentState.REMEDIATING
        # No stored plan means nothing was ever dispatched, so there is nothing
        # to re-invoke — a corrupt checkpoint, not a crash-resume.
        and run_state.remediation_plan is not None
    ):
        return "reinvoke-after-crash-resume"
    return None


def _accrue_wall_time(run_state: RunState, now: datetime) -> RunState:
    """Advance the wall meter to the elapsed time since ``created_at``.

    Anchored on ``created_at`` so a resumed run keeps what the crashed process
    burned (ADR 0015). The monotone guard blocks meter rewinds.
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

    Checkpoints on entry and after every transition; an exhausted budget
    short-circuits to ``ESCALATED`` unless ``_budget_exemption`` names one.
    """
    if run_state.state.is_terminal:
        raise TerminalStateError(
            f"run_to_completion called on terminal state {run_state.state.value}"
        )
    if checkpointer is not None:
        checkpointer.write(run_state)

    steps = 0
    # First iteration only: api/app.py resumes from the latest checkpoint, so
    # an entry state of REMEDIATING means a crash mid-remediation.
    resuming = True
    while not run_state.state.is_terminal:
        if steps >= max_steps:
            raise MaxStepsExceededError(f"run did not terminate within {max_steps} steps")
        # One clock read per iteration, shared by the wall meter and the
        # transition stamp — the meter costs no extra reads.
        now = clock()
        run_state = _accrue_wall_time(run_state, now)
        # Both exemptions cover wall/USD exhaustion too, not just tool calls,
        # consistent with ADR 0006.
        exemption = _budget_exemption(run_state, resuming=resuming)
        if run_state.budget.is_exhausted and exemption is None:
            run_state = _escalate(run_state, "budget exhausted", now)
        else:
            try:
                run_state = dispatch(run_state, now, transitions=transitions)
            except BaseException:
                # Otherwise a crash turns the wall meter into a lower bound.
                run_state = _accrue_wall_time(run_state, clock())
                if checkpointer is not None:
                    checkpointer.write(run_state)
                raise
            # Read again so a terminal transition records its own duration.
            run_state = _accrue_wall_time(run_state, clock())
        if checkpointer is not None:
            checkpointer.write(run_state)
        steps += 1
        resuming = False
    return run_state
