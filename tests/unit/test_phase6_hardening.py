"""Regression tests for the live-eval hardening fixes.

Covers four behavior changes:

1. ``run_to_completion`` exempts VERIFYING from the budget short-circuit —
   an executed Tier-1 action must always be verified, even over budget.
2. ``make_llm_plan`` refuses to enter REMEDIATING without headroom for
   the action + verify pair (>= 2 tool calls remaining).
3. ``make_llm_verify`` can poll the verify probe: ``not_verified`` on an
   eventually-consistent read retries after a delay instead of
   escalating on the first instant read.
4. That polling window re-checks the budget ledger between attempts
   (B-12) — the first attempt is always allowed, every later one is
   gated — and stamps each attempt with its own clock read when one is
   wired in.

Plus one calibration assertion that is not a behavior change: at the
documented live knobs a CORRECT remediation run spends 10 tool calls, and
every shipped remediation scenario's *grading* cap must admit that (A-02).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from typing import Any
from uuid import uuid4

from evals.graders.deterministic import GradeDimension, grade
from evals.scenarios.loader import load_scenarios
from incident_commander.agent import remediation
from incident_commander.agent.hypothesis import Hypothesis, HypothesisCategory
from incident_commander.agent.loop import run_to_completion
from incident_commander.agent.remediation import (
    RemediationPlan,
    make_llm_plan,
    make_llm_verify,
    make_remediate,
)
from incident_commander.agent.state import (
    BudgetLedger,
    EvidenceEntry,
    IncidentState,
    RunState,
)
from incident_commander.llm.fakes import CannedLLMClient
from incident_commander.tools.mcp_client import MCPError, ToolResult

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _clock() -> datetime:
    return datetime.now(UTC)


def _budget(max_tool_calls: int = 10, used: int = 0) -> BudgetLedger:
    return BudgetLedger(
        max_tool_calls=max_tool_calls,
        tool_calls_used=used,
        max_tokens=100_000,
        max_wall_seconds=600,
        max_usd=Decimal("5.00"),
    )


def _run_state(state: IncidentState, budget: BudgetLedger, **extra: Any) -> RunState:
    now = _clock()
    return RunState(
        incident_id=uuid4(),
        state=state,
        alert={"source": "test", "severity": "high"},
        budget=budget,
        created_at=now,
        updated_at=now,
        **extra,
    )


_PLAN = RemediationPlan(
    target_hypothesis="consumer_saturation",
    action_tool="restart_consumer_group",
    action_arguments={"consumer_group": "worker-dispatcher"},
    verify_tool="get_consumer_lag",
    verify_arguments={"consumer_group": "worker-dispatcher"},
    verify_expectation="lag should drop toward zero",
)


class _SequencedMCP:
    """Returns one canned ToolResult per call, in order."""

    def __init__(self, results: list[ToolResult]) -> None:
        self._results = list(results)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call_tool(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        timeout_seconds: float | None = None,
    ) -> ToolResult:
        self.calls.append((name, dict(arguments)))
        return self._results.pop(0)


def _lag_result(lag: int) -> ToolResult:
    payload = {
        "consumer_group": "worker-dispatcher",
        "lag": lag,
        "cache_key": "kafka:consumer_lag:worker-dispatcher",
    }
    return ToolResult(content=[{"type": "text", "text": json.dumps(payload)}], is_error=False)


class TestBudgetExemptsVerifying:
    def test_verifying_dispatches_even_when_budget_exhausted(self) -> None:
        run = _run_state(
            IncidentState.VERIFYING,
            _budget(max_tool_calls=3, used=3),  # exhausted
            remediation_plan=_PLAN.model_dump(mode="json"),
        )

        def verify_stub(rs: RunState, at: datetime) -> RunState:
            return rs.with_state(IncidentState.RESOLVED, at)

        final = run_to_completion(
            run, clock=_clock, transitions={IncidentState.VERIFYING: verify_stub}
        )
        assert final.state is IncidentState.RESOLVED
        assert not any(e.tool_name == "_escalate" for e in final.evidence)

    def test_non_verifying_states_still_short_circuit(self) -> None:
        run = _run_state(IncidentState.INVESTIGATING, _budget(max_tool_calls=3, used=3))
        final = run_to_completion(run, clock=_clock, transitions={})
        assert final.state is IncidentState.ESCALATED
        assert any("budget exhausted" in e.result_summary for e in final.evidence)


class TestPlanningHeadroom:
    def test_plan_escalates_without_room_for_action_plus_verify(self) -> None:
        llm = CannedLLMClient([])  # must never be consulted
        transition = make_llm_plan(llm, model="test-model")
        run = _run_state(
            IncidentState.PLANNING,
            _budget(max_tool_calls=5, used=4),  # only 1 call left
            hypotheses=(
                Hypothesis(
                    category=HypothesisCategory.CONSUMER_SATURATION,
                    name="consumer_saturation",
                    confidence=0.9,
                    reasoning="lag sustained",
                ),
            ),
        )
        result = transition(run, _clock())
        assert result.state is IncidentState.ESCALATED
        assert any("insufficient tool-call budget" in e.result_summary for e in result.evidence)
        assert llm.calls == []  # escalated before spending planner tokens


class TestVerifyPolling:
    def test_not_verified_then_verified_resolves_on_second_probe(self) -> None:
        mcp = _SequencedMCP([_lag_result(15_000), _lag_result(0)])
        judge = CannedLLMClient(
            [
                {"verdict": "not_verified", "reasoning": "lag still 15000"},
                {"verdict": "verified", "reasoning": "lag recovered to 0"},
            ]
        )
        slept: list[float] = []
        transition = make_llm_verify(
            mcp,
            judge,
            model="test-model",
            probe_attempts=3,
            probe_delay_seconds=15.0,
            sleep=slept.append,
        )
        run = _run_state(
            IncidentState.VERIFYING,
            _budget(max_tool_calls=10, used=2),
            remediation_plan=_PLAN.model_dump(mode="json"),
        )
        result = transition(run, _clock())
        assert result.state is IncidentState.RESOLVED
        assert slept == [15.0]  # one wait between the two probes
        assert result.budget.tool_calls_used == 4  # +2 probes
        judge_entries = [e for e in result.evidence if e.tool_name == "_verify_judge"]
        assert len(judge_entries) == 2
        assert judge_entries[-1].result_summary.startswith("verified")

    def test_exhausting_attempts_escalates_with_full_probe_history(self) -> None:
        mcp = _SequencedMCP([_lag_result(15_000), _lag_result(14_000)])
        judge = CannedLLMClient(
            [
                {"verdict": "not_verified", "reasoning": "still high"},
                {"verdict": "not_verified", "reasoning": "barely moved"},
            ]
        )
        transition = make_llm_verify(
            mcp,
            judge,
            model="test-model",
            probe_attempts=2,
            probe_delay_seconds=0.0,
            sleep=lambda _s: None,
        )
        run = _run_state(
            IncidentState.VERIFYING,
            _budget(max_tool_calls=10, used=2),
            remediation_plan=_PLAN.model_dump(mode="json"),
        )
        result = transition(run, _clock())
        assert result.state is IncidentState.ESCALATED
        assert len(mcp.calls) == 2
        assert len([e for e in result.evidence if e.tool_name == "_verify_judge"]) == 2

    def test_default_single_probe_preserves_legacy_behavior(self) -> None:
        mcp = _SequencedMCP([_lag_result(15_000)])
        judge = CannedLLMClient([{"verdict": "not_verified", "reasoning": "still high"}])
        transition = make_llm_verify(mcp, judge, model="test-model")
        run = _run_state(
            IncidentState.VERIFYING,
            _budget(max_tool_calls=10, used=2),
            remediation_plan=_PLAN.model_dump(mode="json"),
        )
        result = transition(run, _clock())
        assert result.state is IncidentState.ESCALATED
        assert len(mcp.calls) == 1


def _escalation_reasons(run: RunState) -> list[str]:
    return [e.result_summary for e in run.evidence if e.tool_name == "_remediation_escalate"]


def _verify_window(run: RunState) -> list[EvidenceEntry]:
    """Probe + judge entries of the VERIFYING window, in emission order."""
    return [e for e in run.evidence if e.tool_name in ("get_consumer_lag", "_verify_judge")]


class TestVerifyBudgetGate:
    """B-12: the polling window re-checks the ledger between attempts.

    ADR 0006 exempts VERIFYING from the loop-level short-circuit and blesses
    *one* extra probe over budget; without an in-loop gate a run entering
    VERIFYING already exhausted could spend ``verify_probe_attempts`` (up to
    10) probes plus as many judge calls past a hard limit.
    """

    def test_entering_verifying_exhausted_allows_exactly_one_attempt(self) -> None:
        mcp = _SequencedMCP([_lag_result(15_000) for _ in range(10)])
        judge = CannedLLMClient([{"verdict": "not_verified", "reasoning": "still high"}] * 10)
        slept: list[float] = []
        transition = make_llm_verify(
            mcp,
            judge,
            model="test-model",
            probe_attempts=10,
            probe_delay_seconds=15.0,
            sleep=slept.append,
        )
        run = _run_state(
            IncidentState.VERIFYING,
            _budget(max_tool_calls=4, used=4),  # already exhausted on entry
            remediation_plan=_PLAN.model_dump(mode="json"),
        )
        result = transition(run, _clock())

        assert len(mcp.calls) == 1  # the ADR-0006 guaranteed probe, and no more
        assert len(judge.calls) == 1
        assert slept == []  # never sleep into an escalation
        assert result.state is IncidentState.ESCALATED
        reasons = _escalation_reasons(result)
        assert len(reasons) == 1
        assert "budget exhausted after verify attempt 1/10" in reasons[0]
        # The executed-but-unverified fact must be explicit for the briefing.
        assert "verification incomplete" in reasons[0]
        # Full probe history still rides along on the escalated state.
        assert len([e for e in result.evidence if e.tool_name == "_verify_judge"]) == 1

    def test_budget_exhausted_mid_window_escalates_without_sleeping(self) -> None:
        mcp = _SequencedMCP([_lag_result(15_000 - 1_000 * i) for i in range(6)])
        judge = CannedLLMClient([{"verdict": "not_verified", "reasoning": "still high"}] * 6)
        slept: list[float] = []
        transition = make_llm_verify(
            mcp,
            judge,
            model="test-model",
            probe_attempts=6,
            probe_delay_seconds=15.0,
            sleep=slept.append,
        )
        run = _run_state(
            IncidentState.VERIFYING,
            # Two calls of headroom: attempt 2's charge exhausts the ledger.
            _budget(max_tool_calls=2, used=0),
            remediation_plan=_PLAN.model_dump(mode="json"),
        )
        result = transition(run, _clock())

        assert len(mcp.calls) == 2
        assert len(judge.calls) == 2
        assert len([e for e in result.evidence if e.tool_name == "_verify_judge"]) == 2
        assert result.budget.tool_calls_used == 2
        # One wait between attempts 1 and 2 — and none between attempt 2 and
        # the escalation (the gate runs before the sleep).
        assert slept == [15.0]
        assert result.state is IncidentState.ESCALATED
        assert "budget exhausted after verify attempt 2/6" in _escalation_reasons(result)[0]


class TestVerifyAttemptTimestamps:
    """B-12 secondary: each poll attempt gets its own clock read when wired."""

    @staticmethod
    def _polling_transition(
        mcp: _SequencedMCP,
        judge: CannedLLMClient,
        clock: Callable[[], datetime] | None,
    ) -> Callable[[RunState, datetime], RunState]:
        return make_llm_verify(
            mcp,
            judge,
            model="test-model",
            probe_attempts=3,
            probe_delay_seconds=0.0,
            sleep=lambda _s: None,
            clock=clock,
        )

    @staticmethod
    def _run() -> RunState:
        return _run_state(
            IncidentState.VERIFYING,
            _budget(max_tool_calls=10, used=2),
            remediation_plan=_PLAN.model_dump(mode="json"),
        )

    def test_injected_clock_stamps_each_attempt_distinctly(self) -> None:
        base = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
        ticks = iter([base + timedelta(seconds=i) for i in range(1, 32)])
        mcp = _SequencedMCP([_lag_result(15_000 - 1_000 * i) for i in range(3)])
        judge = CannedLLMClient([{"verdict": "not_verified", "reasoning": "still high"}] * 3)
        transition = self._polling_transition(mcp, judge, lambda: next(ticks))

        result = transition(self._run(), base)

        probes = [e.timestamp for e in result.evidence if e.tool_name == "get_consumer_lag"]
        judges = [e.timestamp for e in result.evidence if e.tool_name == "_verify_judge"]
        assert len(probes) == 3
        assert all(later > earlier for earlier, later in pairwise(probes))
        assert all(stamp > base for stamp in probes)
        # Judge shares its own attempt's stamp — the pair stays coherent.
        assert judges == probes
        assert result.updated_at == probes[-1]

    def test_clock_none_preserves_the_single_transition_timestamp(self) -> None:
        at = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
        mcp = _SequencedMCP([_lag_result(15_000 - 1_000 * i) for i in range(3)])
        judge = CannedLLMClient([{"verdict": "not_verified", "reasoning": "still high"}] * 3)
        transition = self._polling_transition(mcp, judge, None)

        result = transition(self._run(), at)

        window = _verify_window(result)
        assert len(window) == 6
        assert [e.timestamp for e in window] == [at] * 6
        assert result.updated_at == at

    def test_ordinals_survive_on_both_probe_and_judge_entries(self) -> None:
        base = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
        ticks = iter([base + timedelta(seconds=i) for i in range(1, 32)])
        mcp = _SequencedMCP([_lag_result(15_000 - 1_000 * i) for i in range(3)])
        judge = CannedLLMClient([{"verdict": "not_verified", "reasoning": "still high"}] * 3)
        transition = self._polling_transition(mcp, judge, lambda: next(ticks))

        result = transition(self._run(), base)

        window = _verify_window(result)
        ordinals = [(e.arguments["attempt"], e.arguments["of"]) for e in window]
        assert ordinals == [(1, 3), (1, 3), (2, 3), (2, 3), (3, 3), (3, 3)]


# Live knobs from docs/runbook.md:152-154 — the values the live campaign runs
# with, and the ones the graded scenario caps must be sized for.
_LIVE_VERIFY_PROBE_ATTEMPTS = 6
_LIVE_INVESTIGATE_REPROBE_ATTEMPTS = 1
# Tool calls already spent when a correct run enters VERIFYING: two
# investigation probes, one ADR-0009 freshness re-probe, one Tier-1 action.
_CALLS_BEFORE_VERIFYING = 2 + _LIVE_INVESTIGATE_REPROBE_ATTEMPTS + 1


class TestLivePollingBudgetArithmetic:
    """A-02: verify polls and freshness re-probes are charged to the GRADED cap.

    Every poll increments ``tool_calls_used`` (ADR 0006 accounting), as does
    the ADR-0009 re-probe. At the documented live knobs a CORRECT remediation
    run therefore spends ~10 tool calls — which the shipped remediation
    scenario caps must admit. Eight of them sat at 8, so a correct run failed
    the BUDGET dimension. This is the arithmetic assertion that would have
    caught it. Since ADR 0019 the scenario cap is also the runtime
    ``BudgetLedger`` ceiling, which raises the stakes: a cap that cannot
    admit this profile no longer grades a correct run red, it cuts the verify
    loop short and changes what the run does.
    """

    @staticmethod
    def _worst_case_correct_run() -> RunState:
        """Drive VERIFYING at live knobs: 5 stale reads then a drained one."""
        attempts = _LIVE_VERIFY_PROBE_ATTEMPTS
        mcp = _SequencedMCP([_lag_result(15_000)] * (attempts - 1) + [_lag_result(0)])
        judge = CannedLLMClient(
            [{"verdict": "not_verified", "reasoning": "cached read"}] * (attempts - 1)
            + [{"verdict": "verified", "reasoning": "lag drained to 0"}]
        )
        transition = make_llm_verify(
            mcp,
            judge,
            model="test-model",
            probe_attempts=attempts,
            probe_delay_seconds=0.0,
            sleep=lambda _s: None,
        )
        run = _run_state(
            IncidentState.VERIFYING,
            # 25 here is the arithmetic's own headroom, not a scenario cap:
            # this test measures how many calls a correct run SPENDS, which
            # is the input to choosing a cap (ADR 0019), so it must not be
            # bounded by one.
            _budget(max_tool_calls=25, used=_CALLS_BEFORE_VERIFYING),
            remediation_plan=_PLAN.model_dump(mode="json"),
        )
        return transition(run, _clock())

    def test_correct_run_at_live_knobs_spends_ten_tool_calls(self) -> None:
        result = self._worst_case_correct_run()
        assert result.state is IncidentState.RESOLVED
        assert result.budget.tool_calls_used == (
            _CALLS_BEFORE_VERIFYING + _LIVE_VERIFY_PROBE_ATTEMPTS
        )
        assert result.budget.tool_calls_used == 10

    def test_every_remediation_scenario_cap_admits_that_run(self) -> None:
        result = self._worst_case_correct_run()
        scenarios = load_scenarios(_REPO_ROOT / "evals" / "scenarios")
        remediation = [s for s in scenarios if s.expectation.expected_action_tools]
        assert len(remediation) >= 9, "expected the shipped remediation scenario class"
        failures: dict[str, str] = {}
        for scenario in remediation:
            report = grade(result, scenario.expectation)
            budget = next(d for d in report.dimensions if d.dimension is GradeDimension.BUDGET)
            if not budget.passed:
                failures[scenario.name] = budget.detail
        assert failures == {}, f"correct live run fails the BUDGET dimension: {failures}"


class TestVerifyJudgeSeesTheActionResult:
    """Some fixes report their effect rather than show it.

    A delayed replay reports `scheduled` / `execute_at` in the ACTION
    response, and the platform then holds the timer — so the DLQ cannot
    shrink inside the ~100s verify window by design. The judge was shown
    only the plan and the probe, told by its own prompt to err toward
    `not_verified`, and a correct agent escalated instead of resolving.
    """

    @staticmethod
    def _plan() -> RemediationPlan:
        return RemediationPlan(
            target_hypothesis="transient_dependency_failure",
            action_tool="replay_dlq_by_ids",
            action_arguments={"job_ids": ["j1"], "delay_seconds": 300},
            verify_tool="list_dlq_messages",
            verify_arguments={},
            verify_expectation="the replay is scheduled",
        )

    def test_the_action_result_reaches_the_judge(self) -> None:
        now = _clock()
        plan = self._plan()
        run = _run_state(
            IncidentState.VERIFYING,
            _budget(),
            remediation_plan=plan.model_dump(mode="json"),
            evidence=(
                EvidenceEntry(
                    tool_name="replay_dlq_by_ids",
                    arguments={},
                    result_summary='{"scheduled":1,"execute_at":1753868400.0}',
                    timestamp=now,
                ),
            ),
        )
        context = remediation._format_verify_context(
            plan, '{"total":4}', remediation._action_result_of(run, plan)
        )
        # The only success signal for a delayed replay lives here.
        assert "scheduled" in context
        assert "execute_at" in context

    def test_a_missing_action_result_says_so_rather_than_vanishing(self) -> None:
        context = remediation._format_verify_context(self._plan(), '{"total":4}', None)
        assert "not recorded" in context

    def test_the_probe_result_is_still_shown(self) -> None:
        context = remediation._format_verify_context(self._plan(), '{"total":4}', '{"scheduled":1}')
        assert '{"total":4}' in context

    def test_the_transition_actually_passes_it_through(self) -> None:
        """The wiring, not just the formatter.

        Testing `_format_verify_context` alone would pass even if the call
        site never looked the action result up — which is exactly the shape
        of the original bug.
        """
        plan = self._plan()
        judge = CannedLLMClient([{"verdict": "verified", "reasoning": "scheduled"}])
        mcp = _SequencedMCP(
            [ToolResult(content=[{"type": "text", "text": '{"total":4,"items":[]}'}])]
        )
        transition = make_llm_verify(mcp, judge, model="test-model")
        run = _run_state(
            IncidentState.VERIFYING,
            _budget(),
            remediation_plan=plan.model_dump(mode="json"),
            evidence=(
                EvidenceEntry(
                    tool_name="replay_dlq_by_ids",
                    arguments={},
                    result_summary='{"scheduled":1,"execute_at":1753868400.0}',
                    timestamp=_clock(),
                ),
            ),
        )
        transition(run, _clock())
        _system, user_message = judge.calls[0]
        assert "execute_at" in user_message, (
            "the judge was not shown the action's own result — for a delayed "
            "replay that is the only success signal there is"
        )

    def test_the_latest_call_of_the_action_tool_wins(self) -> None:
        now = _clock()
        # The remediation loop is single-attempt per incident, but the ledger
        # can carry an earlier probe of the same tool name; the action that
        # was just executed is the last one.
        plan = self._plan()
        run = _run_state(
            IncidentState.VERIFYING,
            _budget(),
            remediation_plan=plan.model_dump(mode="json"),
            evidence=(
                EvidenceEntry(
                    tool_name="replay_dlq_by_ids",
                    arguments={},
                    result_summary='{"scheduled":0}',
                    timestamp=now,
                ),
                EvidenceEntry(
                    tool_name="replay_dlq_by_ids",
                    arguments={},
                    result_summary='{"scheduled":9}',
                    timestamp=now,
                ),
            ),
        )
        assert remediation._action_result_of(run, plan) == '{"scheduled":9}'


class TestRefusedTier1AttemptsAreRecorded:
    """The remediation loop must record WHAT it tried, not just that it failed."""

    @staticmethod
    def _plan() -> RemediationPlan:
        return RemediationPlan(
            target_hypothesis="dlq_poison_message",
            action_tool="replay_dlq_by_ids",
            # A well-formed id: the seeded human_required row. A malformed
            # one is rejected by local argument validation BEFORE any call,
            # which is the right behaviour and a different path — there is no
            # attempt to record when nothing was attempted.
            action_arguments={"job_ids": ["f030f975-974e-5ce3-aa6b-444136507d86"]},
            verify_tool="list_dlq_messages",
            verify_arguments={},
            verify_expectation="the DLQ shrinks",
        )

    def _run_refused(self, error: MCPError) -> RunState:
        class _Refusing:
            def call_tool(self, name: str, arguments: Any, **_kw: Any) -> ToolResult:
                raise error

        transition = make_remediate(_Refusing())
        run = _run_state(
            IncidentState.REMEDIATING,
            _budget(),
            remediation_plan=self._plan().model_dump(mode="json"),
        )
        return transition(run, _clock())

    def test_the_attempted_tool_and_arguments_survive(self) -> None:
        result = self._run_refused(MCPError(-32002, "missing required scope: actions:execute"))
        entry = result.evidence[-1]
        assert entry.arguments["attempted_tool"] == "replay_dlq_by_ids"
        attempted = entry.arguments["attempted_arguments"]
        assert isinstance(attempted, dict)
        assert attempted["job_ids"] == ["f030f975-974e-5ce3-aa6b-444136507d86"]

    def test_the_run_still_escalates(self) -> None:
        # Recording the attempt must not change the outcome — a refused
        # action is still a failed remediation.
        result = self._run_refused(MCPError(-32002, "refused"))
        assert result.state is IncidentState.ESCALATED
