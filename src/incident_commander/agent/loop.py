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


def _budget_exemption(run_state: RunState, *, resuming: bool) -> str | None:
    """Name the ADR 0006 exemption that lets this step run over budget, or None.

    Both exemptions protect the same invariant — *an executed Tier-1 action
    is always verified, or escalated with the fact declared* — and both are
    named rather than expressed as a status list, because the reason is the
    load-bearing part: a future state added to the machine must argue its
    way in, not inherit an exemption by sitting in a tuple.

    ``verify-after-execute``
        REMEDIATING committed, so the action landed. One extra probe over
        budget is cheaper than an executed-but-unverified action.

    ``reinvoke-after-crash-resume``
        The run was rebuilt from a checkpoint that reads REMEDIATING. That
        checkpoint is written on *entry* to the state — before the action
        tool is called and before any VERIFYING checkpoint exists — so the
        state alone cannot say whether the Tier-1 action executed. Short-
        circuiting here escalates a possibly-landed action with no verify
        pass and no disclosure of the attempt in the briefing, which is the
        precise harm the VERIFYING exemption was written to prevent.

        Re-invoking instead is safe rather than reckless: REMEDIATING
        rebuilds the same deterministic idempotency key from (incident,
        tool, args), so the platform replays the cached response instead of
        re-executing the effect (ADR 0008, which removed the client-side
        execute-once guard on the strength of that wire contract).

        This matters far more often than "crashes are rare" suggests: ADR
        0015 anchors the wall meter on ``created_at``, so a resumed run is
        frequently already exhausted at the moment it resumes.

    ``resuming`` is true only for the first iteration of a run that entered
    the loop already in REMEDIATING. A run that reaches REMEDIATING from
    PLANNING inside this process has dispatched nothing yet, so an
    exhausted ledger must still stop it before the Tier-1 call — escalating
    pre-execution is the safe direction, and ``make_llm_plan``'s headroom
    check is what keeps that from happening in the first place.
    """
    if run_state.state is IncidentState.VERIFYING:
        return "verify-after-execute"
    if (
        resuming
        and run_state.state is IncidentState.REMEDIATING
        # No stored plan means PLANNING never committed one, so nothing was
        # ever dispatched and there is nothing to re-invoke. REMEDIATING is
        # only reachable through PLANNING, so this is a corrupt checkpoint
        # rather than a crash-resume; the transition would escalate on the
        # missing plan anyway.
        and run_state.remediation_plan is not None
    ):
        return "reinvoke-after-crash-resume"
    return None


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
    Exhausted budget short-circuits to ``ESCALATED`` with an evidence entry,
    except for the two named ADR 0006 exemptions in ``_budget_exemption``.
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
    # True for the first iteration only. The caller handed us a state it
    # loaded from somewhere: for a fresh run that is TRIAGE, but api/app.py's
    # resume path passes the latest checkpoint verbatim, so an entry state of
    # REMEDIATING means a previous process died mid-remediation.
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
            run_state = dispatch(run_state, now, transitions=transitions)
        if checkpointer is not None:
            checkpointer.write(run_state)
        steps += 1
        resuming = False
    return run_state
