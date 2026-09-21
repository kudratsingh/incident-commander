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

    Both protect one invariant: an executed Tier-1 action is always verified or escalated
    with that fact declared.
    """
    if run_state.state is IncidentState.VERIFYING:
        return "verify-after-execute"
    if (
        resuming
        and run_state.state is IncidentState.REMEDIATING
        # No stored plan means nothing was dispatched: a corrupt checkpoint, not a resume.
        and run_state.remediation_plan is not None
    ):
        return "reinvoke-after-crash-resume"
    return None


def _accrue_wall_time(run_state: RunState, now: datetime) -> RunState:
    """Advance the wall meter to the elapsed time since ``created_at``.

    Anchored there so a resumed run keeps what the crashed process burned (ADR 0015);
    the monotone guard blocks meter rewinds.
    """
    elapsed = (now - run_state.created_at).total_seconds()
    if elapsed <= run_state.budget.wall_seconds_used:
        return run_state
    return run_state.model_copy(
        update={"budget": run_state.budget.model_copy(update={"wall_seconds_used": elapsed})}
    )


def _stamp_entered(
    run_state: RunState, *, dispatched_at: datetime, entered: datetime, evidence_before: int
) -> RunState:
    """Stamp the state a transition produced with the moment it was ENTERED (ADR 0075).

    The loop owns the stamp because only the loop observes that moment, and ``entered`` reuses
    ``_accrue_wall_time``'s reading. The transition's own evidence row is restamped only when it
    is an underscore marker this dispatch appended at ``dispatched_at``; a tool row keeps its own.
    """
    entries = run_state.evidence
    update: dict[str, object] = {"updated_at": entered}
    if len(entries) > evidence_before:
        last = entries[-1]
        if last.timestamp == dispatched_at and last.tool_name.startswith("_"):
            update["evidence"] = (*entries[:-1], last.model_copy(update={"timestamp": entered}))
    return run_state.model_copy(update=update)


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
    # 1. Refuse a run that is already over, then checkpoint the state we were handed.
    if run_state.state.is_terminal:
        raise TerminalStateError(
            f"run_to_completion called on terminal state {run_state.state.value}"
        )
    if checkpointer is not None:
        checkpointer.write(run_state)

    steps = 0
    # First iteration only: api/app.py resumes from the latest checkpoint, so an entry state
    # of REMEDIATING means a crash mid-remediation.
    resuming = True
    while not run_state.state.is_terminal:
        # 2. The step ceiling, which is a defect guard rather than a budget.
        if steps >= max_steps:
            raise MaxStepsExceededError(f"run did not terminate within {max_steps} steps")
        # 3. The iteration's START, which the transition stamps its own reads with (ADR 0075).
        now = clock()
        run_state = _accrue_wall_time(run_state, now)
        # 4. Budget, unless ADR 0006 exempts this step — wall and USD included, not just calls.
        exemption = _budget_exemption(run_state, resuming=resuming)
        if run_state.budget.is_exhausted and exemption is None:
            run_state = _escalate(run_state, "budget exhausted", now)
        else:
            # 5. Dispatch one transition. A crash still charges the wall meter and checkpoints,
            #    or the meter becomes a lower bound.
            evidence_before = len(run_state.evidence)
            try:
                run_state = dispatch(run_state, now, transitions=transitions)
            except BaseException:
                run_state = _accrue_wall_time(run_state, clock())
                if checkpointer is not None:
                    checkpointer.write(run_state)
                raise
            # 6. ONE read shared by the meter and the stamp: a terminal transition's duration
            #    and the moment its state was entered are the same fact (ADR 0075).
            entered = clock()
            run_state = _stamp_entered(
                run_state,
                dispatched_at=now,
                entered=entered,
                evidence_before=evidence_before,
            )
            run_state = _accrue_wall_time(run_state, entered)
        # 7. Checkpoint what the transition produced.
        if checkpointer is not None:
            checkpointer.write(run_state)
        steps += 1
        resuming = False
    return run_state
