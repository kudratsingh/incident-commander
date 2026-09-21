from collections.abc import Callable
from datetime import datetime, timedelta
from uuid import uuid4

import pytest

from incident_commander.agent.briefing import render_briefing
from incident_commander.agent.loop import MaxStepsExceededError, run_to_completion
from incident_commander.agent.orchestrator import TRANSITIONS, TerminalStateError
from incident_commander.agent.state import (
    BudgetLedger,
    EvidenceEntry,
    IncidentState,
    RunState,
)
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

    def test_wall_meter_accrues_without_tripping(self, run_state: RunState, now: datetime) -> None:
        """B-01: the wall meter has a writer at all (it read 0.0 forever)."""
        run = _with_alert(run_state, {"source": "billing", "severity": "info"})
        clock = _make_clock(now + timedelta(seconds=5), step_seconds=5.0)
        result = run_to_completion(run, clock=clock)
        assert result.state is IncidentState.ESCALATED
        assert result.budget.wall_seconds_used == 10.0

    @pytest.mark.parametrize("terminal", [IncidentState.RESOLVED, IncidentState.ESCALATED])
    def test_terminal_transition_accrues_its_own_elapsed_time(
        self, run_state: RunState, now: datetime, terminal: IncidentState
    ) -> None:
        def finish(rs: RunState, at: datetime) -> RunState:
            return rs.with_state(terminal, at)

        state = (
            IncidentState.VERIFYING if terminal is IncidentState.RESOLVED else IncidentState.TRIAGE
        )
        run = run_state.model_copy(update={"state": state})
        clock = _make_clock(now, step_seconds=58)
        result = run_to_completion(run, clock=clock, transitions={state: finish})
        assert result.state is terminal
        assert result.budget.wall_seconds_used == 58.0

    def test_crashed_transition_checkpoints_elapsed_wall_time(
        self, run_state: RunState, now: datetime
    ) -> None:
        def crash(_rs: RunState, _at: datetime) -> RunState:
            raise RuntimeError("transition failed")

        checkpointer = InMemoryCheckpointer()
        with pytest.raises(RuntimeError, match="transition failed"):
            run_to_completion(
                run_state,
                clock=_make_clock(now, step_seconds=58),
                checkpointer=checkpointer,
                transitions={IncidentState.TRIAGE: crash},
            )
        saved = checkpointer.load(run_state.incident_id)
        assert saved is not None
        assert saved.budget.wall_seconds_used == 58.0

    def test_slow_clock_trips_the_wall_budget_and_escalates(
        self, budget: BudgetLedger, now: datetime
    ) -> None:
        """The B-01 repro shape: 61s/step against a 60s cap must escalate.

        At HEAD the meter stayed 0.0, the wall dimension never tripped, and
        the run walked on into the next transition instead.
        """
        run = RunState(
            incident_id=uuid4(),
            state=IncidentState.TRIAGE,
            alert={"source": "billing", "severity": "high"},
            budget=budget.model_copy(update={"max_wall_seconds": 60}),
            created_at=now,
            updated_at=now,
        )
        result = run_to_completion(run, clock=_make_clock(now, step_seconds=61.0))
        assert result.state is IncidentState.ESCALATED
        assert result.budget.wall_seconds_used >= 60
        reasons = [entry.arguments.get("reason") for entry in result.evidence]
        assert "budget exhausted" in reasons
        assert any(e.result_summary == "escalated: budget exhausted" for e in result.evidence)

    def test_wall_meter_survives_crash_resume(self, budget: BudgetLedger, now: datetime) -> None:
        """A ledger rebuilt from a checkpoint reports the full elapsed wall.

        The anchor is ``created_at``, not a loop-local start stamp: a
        resumed run must not hand itself a fresh wall budget (ADR 0015).
        """
        started = now - timedelta(hours=1)
        crashed = RunState(
            incident_id=uuid4(),
            state=IncidentState.TRIAGE,
            alert={"source": "billing", "severity": "high"},
            budget=budget,
            created_at=started,
            updated_at=started,
        )
        ckpt = InMemoryCheckpointer()
        ckpt.write(crashed)
        resumed = ckpt.load(crashed.incident_id)
        assert resumed is not None
        assert resumed.budget.wall_seconds_used == 0.0

        def clock() -> datetime:
            return now

        result = run_to_completion(resumed, clock=clock)
        assert result.budget.wall_seconds_used == 3600.0
        assert result.state is IncidentState.ESCALATED
        reasons = [entry.arguments.get("reason") for entry in result.evidence]
        assert "budget exhausted" in reasons

    def test_wall_meter_is_monotone_under_a_backwards_clock(
        self, budget: BudgetLedger, now: datetime
    ) -> None:
        """A clock that jumps backwards must not refund wall time."""
        run = RunState(
            incident_id=uuid4(),
            state=IncidentState.TRIAGE,
            alert={"source": "billing", "severity": "info"},
            budget=budget.model_copy(update={"wall_seconds_used": 42.0}),
            created_at=now,
            updated_at=now,
        )

        def backwards() -> datetime:
            return now - timedelta(seconds=100)

        result = run_to_completion(run, clock=backwards)
        assert result.budget.wall_seconds_used == 42.0

    def test_transition_stamps_use_clock(self, run_state: RunState, now: datetime) -> None:
        later = now + timedelta(hours=1)

        def fixed_clock() -> datetime:
            return later

        run = _with_alert(run_state, {"source": "billing", "severity": "info"})
        # The clock jumps an hour, so widen the wall cap to keep exercising triage.
        run = run.model_copy(
            update={"budget": run.budget.model_copy(update={"max_wall_seconds": 7_200})}
        )
        ckpt = InMemoryCheckpointer()
        result = run_to_completion(run, clock=fixed_clock, checkpointer=ckpt)
        history = ckpt.history(result.incident_id)
        assert history[0].updated_at == now
        assert history[1].updated_at == later
        assert result.updated_at == later


class TestAStatesTimestampIsTheMomentItWasEntered:
    """ADR 0075, from the owner's fourth take.

    ``dispatch`` is handed the iteration's START time and every transition stamps the state it
    returns with it, so an investigation that ran 22 seconds and made three planner calls
    reported ``investigating`` for 10 ms and gave ``planning`` the moment ``investigating``
    began. The loop re-reads the clock after the transition returns and stamps with that.
    """

    @staticmethod
    def _slow_clock(start: datetime, elapsed: timedelta) -> Callable[[], datetime]:
        """A clock that advances only ACROSS the transition.

        Reading 1 is the iteration's start (what ``dispatch`` is handed), reading 2 is the
        moment it returned. Every later reading stays there, so a second iteration adds no
        time and the assertions are about one transition.
        """
        reads = {"n": 0}

        def clock() -> datetime:
            reads["n"] += 1
            return start if reads["n"] == 1 else start + elapsed

        return clock

    def test_the_new_state_carries_the_moment_the_transition_returned(
        self, run_state: RunState, now: datetime
    ) -> None:
        """Red before ADR 0075: ``updated_at`` was ``now``, so the state read 0 ms old."""
        elapsed = timedelta(seconds=20)

        def slow(rs: RunState, at: datetime) -> RunState:
            return rs.with_state(IncidentState.ESCALATED, at)

        run = run_state.model_copy(
            update={"budget": run_state.budget.model_copy(update={"max_wall_seconds": 7_200})}
        )
        result = run_to_completion(
            run,
            clock=self._slow_clock(now, elapsed),
            transitions={IncidentState.TRIAGE: slow},
        )
        assert result.updated_at == now + elapsed

    def test_the_investigating_span_is_the_real_one_and_planning_is_stamped_after_it(
        self, run_state: RunState, now: datetime
    ) -> None:
        """The take's own shape: a long INVESTIGATING, then PLANNING.

        The checkpoint history is what the reporter turns into the platform's
        ``phase_history``, so the two stamps here ARE that page's two durations: before
        ADR 0075 both read ``now`` and the page showed "investigating · 0 ms".
        """
        investigating_took = timedelta(seconds=22)
        reads: list[datetime] = [
            now,  # iteration 1 starts (TRIAGE)
            now,  # TRIAGE returned immediately
            now,  # iteration 2 starts (INVESTIGATING)
            now + investigating_took,  # the investigation returned 22 s later
            now + investigating_took,  # iteration 3 starts (PLANNING)
            now + investigating_took,  # PLANNING returned immediately
        ]
        ticks = {"n": 0}

        def clock() -> datetime:
            value = reads[min(ticks["n"], len(reads) - 1)]
            ticks["n"] += 1
            return value

        def investigate(rs: RunState, at: datetime) -> RunState:
            return rs.with_state(IncidentState.PLANNING, at)

        def plan(rs: RunState, at: datetime) -> RunState:
            return rs.with_state(IncidentState.ESCALATED, at)

        run = _with_alert(run_state, {"source": "billing", "severity": "high"})
        run = run.model_copy(
            update={"budget": run.budget.model_copy(update={"max_wall_seconds": 7_200})}
        )
        ckpt = InMemoryCheckpointer()
        result = run_to_completion(
            run,
            clock=clock,
            checkpointer=ckpt,
            transitions={
                **TRANSITIONS,
                IncidentState.INVESTIGATING: investigate,
                IncidentState.PLANNING: plan,
            },
        )
        history = ckpt.history(result.incident_id)
        stamps = {rs.state: rs.updated_at for rs in history}
        assert stamps[IncidentState.INVESTIGATING] == now
        # PLANNING was entered when the investigation RETURNED, not when it started.
        assert stamps[IncidentState.PLANNING] == now + investigating_took
        investigating_span = (
            stamps[IncidentState.PLANNING] - stamps[IncidentState.INVESTIGATING]
        ).total_seconds()
        assert investigating_span == investigating_took.total_seconds()

    def test_the_transitions_own_bookkeeping_entry_is_stamped_with_it_too(
        self, run_state: RunState, now: datetime
    ) -> None:
        elapsed = timedelta(seconds=20)

        def slow(rs: RunState, at: datetime) -> RunState:
            entry = EvidenceEntry(
                tool_name="_planner_stop",
                arguments={"reason": "done"},
                result_summary="planner stop: done",
                timestamp=at,
            )
            return rs.model_copy(
                update={
                    "state": IncidentState.ESCALATED,
                    "updated_at": at,
                    "evidence": (*rs.evidence, entry),
                }
            )

        run = run_state.model_copy(
            update={"budget": run_state.budget.model_copy(update={"max_wall_seconds": 7_200})}
        )
        result = run_to_completion(
            run,
            clock=self._slow_clock(now, elapsed),
            transitions={IncidentState.TRIAGE: slow},
        )
        assert result.evidence[-1].tool_name == "_planner_stop"
        assert result.evidence[-1].timestamp == now + elapsed

    def test_a_real_tool_entry_keeps_the_time_the_read_happened(
        self, run_state: RunState, now: datetime
    ) -> None:
        """A probe's timestamp is about the READ, and the loop does not move it.

        The restamp is for the row that records the transition, not for the calls the
        transition made on the way there — a read at 19:56 stays at 19:56.
        """
        elapsed = timedelta(seconds=20)

        def slow(rs: RunState, at: datetime) -> RunState:
            entry = EvidenceEntry(
                tool_name="get_consumer_lag",
                arguments={"consumer_group": "worker-dispatcher"},
                result_summary='{"lag": 30}',
                timestamp=at,
            )
            return rs.model_copy(
                update={
                    "state": IncidentState.ESCALATED,
                    "updated_at": at,
                    "evidence": (*rs.evidence, entry),
                }
            )

        run = run_state.model_copy(
            update={"budget": run_state.budget.model_copy(update={"max_wall_seconds": 7_200})}
        )
        result = run_to_completion(
            run,
            clock=self._slow_clock(now, elapsed),
            transitions={IncidentState.TRIAGE: slow},
        )
        assert result.evidence[-1].timestamp == now
        assert result.updated_at == now + elapsed

    def test_an_entry_the_transition_did_not_append_is_left_alone(
        self, run_state: RunState, now: datetime
    ) -> None:
        """A transition that appends nothing must not have an older marker restamped."""
        earlier = EvidenceEntry(
            tool_name="_handoff_refused",
            arguments={},
            result_summary="handoff refused",
            timestamp=now,
        )
        run = run_state.model_copy(
            update={
                "evidence": (earlier,),
                "budget": run_state.budget.model_copy(update={"max_wall_seconds": 7_200}),
            }
        )

        def silent(rs: RunState, at: datetime) -> RunState:
            return rs.model_copy(update={"state": IncidentState.ESCALATED, "updated_at": at})

        result = run_to_completion(
            run,
            clock=self._slow_clock(now, timedelta(seconds=20)),
            transitions={IncidentState.TRIAGE: silent},
        )
        assert result.evidence[-1].timestamp == now


# ---------------------------------------------------------------------------
# WO-R2-39: the budget short-circuit and a crash-resumed REMEDIATING run.

_PLAN: dict[str, object] = {
    "target_hypothesis": "consumer_saturation",
    "action_tool": "restart_consumer_group",
    "action_arguments": {"consumer_group": "worker-dispatcher"},
    "verify_tool": "get_consumer_lag",
    "verify_arguments": {"consumer_group": "worker-dispatcher"},
    "verify_expectation": "lag should drop toward zero",
}


def _exhausted(budget: BudgetLedger) -> BudgetLedger:
    """A ledger with no tool calls left — the resume-time normal case.

    Anchored on ``created_at`` (ADR 0015).
    """
    return budget.model_copy(update={"tool_calls_used": budget.max_tool_calls})


def _resumed_remediating(budget: BudgetLedger, now: datetime) -> RunState:
    """What ``load()`` hands the loop after a crash inside REMEDIATING.

    Written on *entry*, so it cannot say whether the action executed.
    """
    return RunState(
        incident_id=uuid4(),
        state=IncidentState.REMEDIATING,
        alert={"source": "billing", "severity": "high"},
        budget=_exhausted(budget),
        remediation_plan=_PLAN,
        created_at=now,
        updated_at=now,
    )


class TestBudgetExemptsResumedRemediating:
    """A crash-resumed REMEDIATING run must re-invoke before it escalates.

    Safe because the idempotency key is deterministic: the platform replays (ADR 0008).
    """

    @staticmethod
    def _stubs(calls: list[str]) -> dict[IncidentState, Callable[[RunState, datetime], RunState]]:
        def remediate(rs: RunState, at: datetime) -> RunState:
            calls.append("remediate")
            entry = EvidenceEntry(
                tool_name="restart_consumer_group",
                arguments={"consumer_group": "worker-dispatcher"},
                result_summary='{"restarted": true}',
                timestamp=at,
            )
            return rs.model_copy(
                update={
                    "state": IncidentState.VERIFYING,
                    "evidence": (*rs.evidence, entry),
                    "remediation_attempts": rs.remediation_attempts + 1,
                    "updated_at": at,
                }
            )

        def verify(rs: RunState, at: datetime) -> RunState:
            calls.append("verify")
            entry = EvidenceEntry(
                tool_name="get_consumer_lag",
                arguments={"consumer_group": "worker-dispatcher"},
                result_summary='{"lag": 0}',
                timestamp=at,
            )
            return rs.model_copy(
                update={
                    "state": IncidentState.RESOLVED,
                    "evidence": (*rs.evidence, entry),
                    "updated_at": at,
                }
            )

        return {
            IncidentState.REMEDIATING: remediate,
            IncidentState.VERIFYING: verify,
        }

    def test_resumed_remediating_reinvokes_and_verifies_over_budget(
        self, budget: BudgetLedger, now: datetime
    ) -> None:
        """RED at HEAD: the run escalates without ever re-invoking or verifying.

        HEAD exempts only VERIFYING, so an action that may already have executed is left
        unverified.
        """
        calls: list[str] = []
        resumed = _resumed_remediating(budget, now)
        assert resumed.budget.is_exhausted

        final = run_to_completion(resumed, clock=_make_clock(now), transitions=self._stubs(calls))

        assert calls == ["remediate", "verify"]
        assert final.state is IncidentState.RESOLVED
        assert not any(e.tool_name == "_escalate" for e in final.evidence)

    def test_resumed_remediating_discloses_the_attempt_in_the_briefing(
        self, budget: BudgetLedger, now: datetime
    ) -> None:
        """The briefing must name the Tier-1 action that was re-invoked.

        RED at HEAD: the short-circuit writes a filtered-out marker.
        """
        calls: list[str] = []
        resumed = _resumed_remediating(budget, now)

        final = run_to_completion(resumed, clock=_make_clock(now), transitions=self._stubs(calls))

        trail = [probe.tool for probe in render_briefing(final).investigation_trail]
        assert "restart_consumer_group" in trail

    def test_fresh_remediating_still_short_circuits(
        self, budget: BudgetLedger, now: datetime
    ) -> None:
        """The exemption is for RESUME only, not for REMEDIATING in general.

        A run that reached REMEDIATING in-process has dispatched nothing, so escalating is safe.
        """
        calls: list[str] = []
        fresh = RunState(
            incident_id=uuid4(),
            state=IncidentState.PLANNING,
            alert={"source": "billing", "severity": "high"},
            budget=_exhausted(budget),
            remediation_plan=_PLAN,
            created_at=now,
            updated_at=now,
        )

        def plan(rs: RunState, at: datetime) -> RunState:
            calls.append("plan")
            return rs.with_state(IncidentState.REMEDIATING, at)

        transitions = {IncidentState.PLANNING: plan, **self._stubs(calls)}
        final = run_to_completion(fresh, clock=_make_clock(now), transitions=transitions)

        assert final.state is IncidentState.ESCALATED
        assert "remediate" not in calls
        assert any("budget exhausted" in e.result_summary for e in final.evidence)

    def test_resumed_remediating_without_a_plan_still_short_circuits(
        self, budget: BudgetLedger, now: datetime
    ) -> None:
        """No stored plan means nothing was ever dispatched — nothing to re-invoke.

        REMEDIATING is only reachable through PLANNING, so this is a corrupt row.
        """
        calls: list[str] = []
        resumed = _resumed_remediating(budget, now).model_copy(update={"remediation_plan": None})

        final = run_to_completion(resumed, clock=_make_clock(now), transitions=self._stubs(calls))

        assert final.state is IncidentState.ESCALATED
        assert calls == []
