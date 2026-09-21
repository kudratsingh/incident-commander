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
        # A REMEDIATING checkpoint with no plan saved on it never dispatched an action, so it
        # is a corrupt checkpoint rather than a crash worth resuming.
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
    # 1. Raise ``TerminalStateError`` if this run has already finished; otherwise checkpoint the
    #    state we were handed, so a crash in the first transition still records where it began.
    if run_state.state.is_terminal:
        raise TerminalStateError(
            f"run_to_completion called on terminal state {run_state.state.value}"
        )
    if checkpointer is not None:
        checkpointer.write(run_state)

    steps = 0
    # True on the first iteration only. api/app.py restarts a run from its latest checkpoint,
    # so arriving here already in REMEDIATING means the previous process crashed mid-action.
    resuming = True
    while not run_state.state.is_terminal:
        # 2. Raise ``MaxStepsExceededError`` once the loop has taken more than ``max_steps``
        #    transitions. Not a budget: it catches a bug that would otherwise spin here forever.
        if steps >= max_steps:
            raise MaxStepsExceededError(f"run did not terminate within {max_steps} steps")
        # 3. Read the clock once for this iteration and charge the wall time elapsed so far.
        #    The transition stamps everything it reads with this same moment (ADR 0075).
        now = clock()
        run_state = _accrue_wall_time(run_state, now)
        # 4. Escalate instead of stepping when any budget is spent — tool calls, tokens, wall
        #    clock and dollars alike — unless ADR 0006 exempts this particular step.
        exemption = _budget_exemption(run_state, resuming=resuming)
        if run_state.budget.is_exhausted and exemption is None:
            run_state = _escalate(run_state, "budget exhausted", now)
        else:
            # 5. Run exactly one state transition. If it raises, still charge the wall clock
            #    and checkpoint before re-raising, or the crashed run under-reports its cost.
            evidence_before = len(run_state.evidence)
            try:
                run_state = dispatch(run_state, now, transitions=transitions)
            except BaseException:
                run_state = _accrue_wall_time(run_state, clock())
                if checkpointer is not None:
                    checkpointer.write(run_state)
                raise
            # 6. Read the clock once more and use that one reading twice: how long the
            #    transition took, and when the state it produced began (ADR 0075).
            entered = clock()
            run_state = _stamp_entered(
                run_state,
                dispatched_at=now,
                entered=entered,
                evidence_before=evidence_before,
            )
            run_state = _accrue_wall_time(run_state, entered)
        # 7. Save the state the transition produced, so a crash resumes from here.
        if checkpointer is not None:
            checkpointer.write(run_state)
        steps += 1
        resuming = False
    return run_state
