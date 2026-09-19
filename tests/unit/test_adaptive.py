"""WP-13.2 — the `adaptive` ladder (plan 02 § 15, plan 00 § 7 item 10, ADR 0064).

The packet's claim is two-sided — easy steps stay cheap AND hard steps get more compute — so the
tests are grouped by the half they can falsify:

* ``TestAnEasyStepStaysOnTheBaselineRung`` — one planner call, no selector call, and the step,
  the state and the record are ``baseline``'s. The cheapness claim as a count, not a sentence.
* ``TestAHardStepClimbsTheLadder`` — each rung is entered by the signal that fired below it, every
  transition is in the ``StepRecord``, and the climb stops at the first rung that clears.
* ``TestTheTailIsSearchOnlyWhereABranchMayRead`` — ``search`` where a prober exists, ``escalate``
  where none does (ADR 0060), and the escalate rung buys nothing.
* ``TestNoRungWidensWhatMayBeActedOn`` — the module mints no action but a stop, every emitted
  action is the object a planner call produced, and the loop's gates still fire.
* ``TestTheFailedRemediationTriggerNeedsTheEdge`` — it fires on ADR 0056's attempt record and
  nothing else writes one, so it cannot fire before the retry edge.
* ``TestTheLadderIsDrivenByTheThresholds`` — an override moves where a step terminates, and the
  module holds no number of its own.
* ``TestTheArmIsRegisteredAndOffByDefault`` / ``TestTheBillIsCarriedWhenARungFails`` /
  ``TestTheRecordTellsTheArmsApart`` / ``TestTheDecisionIsRecorded``.
"""

from __future__ import annotations

import ast
import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Final
from uuid import uuid4

import pytest

from evals.runner import ExecutionMode, build_provenance, search_mode_refusal, strategy_knobs
from incident_commander.agent.hypothesis import (
    HypothesisCategory,
    InvestigationStep,
    RemediateAction,
    StopAction,
)
from incident_commander.agent.investigation import (
    REMEDIATE_CONFIDENCE_THRESHOLD,
    make_branch_prober,
    make_llm_investigate,
)
from incident_commander.agent.planner_context import ATTEMPT_FAILED_MARKER
from incident_commander.agent.search import SEARCH_IS_RECORDED_MODE_ONLY
from incident_commander.agent.selection import SELECTOR_ROLE
from incident_commander.agent.state import BudgetLedger, EvidenceEntry, IncidentState, RunState
from incident_commander.agent.strategies.adaptive import (
    CLIMB,
    LADDER_EXHAUSTED,
    LADDER_N,
    NO_SELECTOR_CLIENT,
    SEARCH_RUNG_UNAVAILABLE,
    AdaptiveFailed,
    AdaptiveStrategy,
    Rung,
    ladder_for,
)
from incident_commander.agent.strategies.baseline import BaselineStrategy
from incident_commander.agent.strategies.knobs import StrategyKnobs
from incident_commander.agent.strategies.names import StrategyName
from incident_commander.agent.strategies.policy import (
    EscalationSignal,
    ThresholdName,
    UncertaintyThresholds,
    declared_default,
    failed_attempts,
    provenance_rows,
)
from incident_commander.agent.strategies.protocol import StrategyContext
from incident_commander.agent.strategies.records import StepRecord
from incident_commander.agent.strategies.registry import STRATEGIES
from incident_commander.config import ModelRole, Settings
from incident_commander.llm.client import LLMError
from incident_commander.llm.fakes import CannedLLMClient, CannedUsage
from incident_commander.tools.mcp_client import ToolResult

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
_SRC: Final[Path] = _REPO_ROOT / "src" / "incident_commander"
_ADAPTIVE: Final[Path] = _SRC / "agent" / "strategies" / "adaptive.py"

_NOW: Final[datetime] = datetime(2026, 9, 19, 12, tzinfo=UTC)

#: A step the declared thresholds leave alone: well clear of the 0.75 floor and the 0.15 margin.
_EASY: Final[tuple[float, float]] = (0.9, 0.5)
#: A step they do not: under the floor and inside the margin.
_HARD: Final[tuple[float, float]] = (0.6, 0.55)


# --------------------------------------------------------------------------
# Fakes and payloads. Local copies, so this file can fail on its own.


class _FakeMCPClient:
    def __init__(self, handler: Callable[[str, Mapping[str, Any]], ToolResult]) -> None:
        self._handler = handler
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call_tool(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        timeout_seconds: float | None = None,
    ) -> ToolResult:
        self.calls.append((name, dict(arguments)))
        return self._handler(name, arguments)


def _lag_response(group: str = "billing", lag: int = 42) -> ToolResult:
    return ToolResult(
        content=[
            {
                "type": "text",
                "text": json.dumps(
                    {
                        "consumer_group": group,
                        "lag": lag,
                        "lag_known": True,
                        "source": "static",
                        "cache_key": f"kafka:consumer_lag:{group}",
                    }
                ),
            }
        ]
    )


def _probe(group: str = "billing") -> dict[str, Any]:
    return {
        "kind": "probe",
        "tool_name": "get_consumer_lag",
        "arguments": {"consumer_group": group},
    }


def _planner(
    confidence: float,
    second: float | None = None,
    *,
    action: dict[str, Any] | None = None,
    category: str = "consumer_saturation",
) -> dict[str, Any]:
    """One ``baseline``-shaped planner answer: a ranking and one next action."""
    hypotheses = [
        {
            "category": category,
            "name": "worker-dispatcher backlog",
            "confidence": confidence,
            "reasoning": "lag climbing while the group holds one member",
        }
    ]
    if second is not None:
        hypotheses.append(
            {
                "category": "poison_message",
                "name": "one bad row",
                "confidence": second,
                "reasoning": "the dead-letter queue is growing",
            }
        )
    return {"hypotheses": hypotheses, "next_action": action or _probe()}


def _candidates(
    n: int = LADDER_N,
    *,
    confidences: tuple[float, ...] = (0.9, 0.8, 0.7, 0.6),
    action: dict[str, Any] | None = None,
    categories: tuple[str, ...] = (
        "consumer_saturation",
        "consumer_saturation",
        "poison_message",
        "consumer_saturation",
    ),
) -> dict[str, Any]:
    """One ``best_of_n_enumerated`` answer: N candidates, each proposing a distinct read.

    The default set agrees with its leader 3 ways to 1 — a quarter of it dissenting, under the
    0.5 ceiling — so a test that wants the disagreement signal asks for it by category.
    """
    return {
        "candidates": [
            {
                "candidate_id": f"c{index}",
                "category": categories[index % len(categories)],
                "name": f"candidate {index}",
                "confidence": confidences[index % len(confidences)],
                "next_probe": _probe(f"group-{index}"),
            }
            for index in range(n)
        ],
        "next_action": action or _probe(),
    }


def _selection(
    *,
    decision: str = "select",
    selected: str | None = "c0",
    uncertainty: float = 0.1,
    scores: Mapping[str, float] | None = None,
) -> dict[str, Any]:
    return {
        "decision": decision,
        "selected_candidate_id": selected if decision == "select" else None,
        "scores": dict(scores or {"c0": 0.9, "c1": 0.2, "c2": 0.1, "c3": 0.05}),
        "uncertainty": uncertainty,
        "reasoning": "the lag reading separates the leaders",
    }


def _walk_selection(**overrides: Any) -> dict[str, Any]:
    """A selection over a ``search`` node's set, which is the branch factor wide (3), not 4."""
    return _selection(scores={"c0": 0.9, "c1": 0.2, "c2": 0.1}, **overrides)


def _ledger(**overrides: Any) -> BudgetLedger:
    base: dict[str, Any] = {
        "max_tool_calls": 25,
        "max_tokens": 200_000,
        "max_wall_seconds": 1_800,
        "max_usd": Decimal("5.00"),
    }
    return BudgetLedger(**{**base, **overrides})


def _state(**update: Any) -> RunState:
    run_state = RunState(
        incident_id=uuid4(),
        state=IncidentState.INVESTIGATING,
        alert={"source": "kafka", "severity": "high", "group": "billing"},
        budget=_ledger(),
        created_at=_NOW,
        updated_at=_NOW,
    )
    return run_state.model_copy(update=update) if update else run_state


def _attempt_entry() -> EvidenceEntry:
    """The record the remediation loop appends when an attempt did not resolve (ADR 0056)."""
    return EvidenceEntry(
        tool_name=ATTEMPT_FAILED_MARKER,
        arguments={"attempt": 1, "of": 2, "action_tool": "restart_consumer_group"},
        result_summary="attempt 1 of 2: restart_consumer_group executed, the backlog did not move",
        timestamp=_NOW,
    )


def _context(
    planner: CannedLLMClient,
    selector: CannedLLMClient | None = None,
    *,
    prober: Any = None,
    sink: list[StepRecord] | None = None,
    iteration: int = 0,
) -> StrategyContext:
    return StrategyContext(
        llm_client=planner,
        model="claude-sonnet-4-6",
        iteration=iteration,
        record_step=None if sink is None else sink.append,
        selector_llm_client=selector if selector is not None else CannedLLMClient([]),
        branch_prober=prober,
    )


def _arm(**knobs: Any) -> AdaptiveStrategy:
    arm = STRATEGIES.create(StrategyName.ADAPTIVE.value, StrategyKnobs(**knobs))
    assert isinstance(arm, AdaptiveStrategy)
    return arm


def _climb(
    planner_payloads: list[dict[str, Any]],
    selector_payloads: list[dict[str, Any]] | None = None,
    *,
    run_state: RunState | None = None,
    prober: Any = None,
    usage: CannedUsage | None = None,
    **knobs: Any,
) -> tuple[RunState, InvestigationStep, StepRecord, CannedLLMClient, CannedLLMClient]:
    """One planner step through the ladder, with both clients handed back for counting."""
    planner = CannedLLMClient(planner_payloads, usage=usage)
    selector = CannedLLMClient(selector_payloads or [], usage=usage)
    after, step, record = _arm(**knobs).plan_next_step(
        run_state if run_state is not None else _state(),
        _NOW,
        _context(planner, selector, prober=prober),
    )
    return after, step, record, planner, selector


def _ladder(record: StepRecord) -> Any:
    assert record.ladder is not None, "an adaptive step must carry its ladder record"
    return record.ladder


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "anthropic_api_key": "sk-ant-test",
        "judge_model": "claude-haiku-4-5",
        "platform_mcp_url": "https://mcp.platform.local",
        "platform_rest_url": "https://api.platform.local",
        "platform_token": "svc-token",
        "platform_webhook_secret": "hmac-secret",
        "database_url": "postgresql://commander:commander@localhost:5432/commander",
    }
    return Settings(_env_file=None, **{**base, **overrides})  # type: ignore[call-arg]


# --------------------------------------------------------------------------


class TestAnEasyStepStaysOnTheBaselineRung:
    """Plan 00 § 7 item 10, first half: an easy step costs what the control group costs."""

    def test_it_makes_one_planner_call_and_no_other_call(self) -> None:
        _, _, record, planner, selector = _climb([_planner(*_EASY)])
        assert len(planner.calls) == 1
        assert selector.calls == []
        assert len(record.llm_calls) == 1
        assert record.llm_calls[0].role == "investigation_planner"

    def test_the_record_counts_the_extra_calls_at_zero(self) -> None:
        # The falsifiable form of "easy cases stay cheap": a subtraction in the record, so a
        # ladder that quietly climbed could not report this number.
        _, _, record, _, _ = _climb([_planner(*_EASY)])
        ladder = _ladder(record)
        assert ladder.terminated_on == Rung.BASELINE.value
        assert ladder.extra_llm_calls == 0
        assert ladder.rungs_used == 1
        assert ladder.rungs[0].llm_calls == 1
        assert ladder.rungs[0].entered_because == ()
        assert ladder.rungs[0].fired == ()
        assert ladder.rungs[0].emitted is True
        assert ladder.rungs[0].climbed is False

    def test_the_step_and_the_state_are_the_ones_baseline_would_have_produced(self) -> None:
        run_state = _state()
        payload = _planner(*_EASY)
        adapted, adaptive_step, adaptive_record = _arm().plan_next_step(
            run_state, _NOW, _context(CannedLLMClient([payload]), CannedLLMClient([]))
        )
        control, control_step, control_record = BaselineStrategy().plan_next_step(
            run_state, _NOW, _context(CannedLLMClient([payload]))
        )
        assert adaptive_step == control_step
        assert adapted == control
        assert adaptive_record.candidate_set[0].category == control_record.candidate_set[0].category
        assert adaptive_record.candidate_set[0].confidence == (
            control_record.candidate_set[0].confidence
        )
        assert adaptive_record.candidate_set[0].proposed_probe == (
            control_record.candidate_set[0].proposed_probe
        )
        assert adaptive_record.llm_calls == control_record.llm_calls
        assert adaptive_record.planner_input_tokens == control_record.planner_input_tokens
        assert adaptive_record.planner_context_chars == control_record.planner_context_chars

    def test_the_baseline_rung_sends_the_control_groups_own_bytes(self) -> None:
        # Not a re-implementation: the rung is ``investigation._plan_next_step`` verbatim, so
        # the prompt and the rendered context are the control group's (ADR 0044 — no ids).
        payload = _planner(*_EASY)
        run_state = _state()
        _, _, _, planner, _ = _climb([payload], run_state=run_state)
        control = CannedLLMClient([payload])
        BaselineStrategy().plan_next_step(run_state, _NOW, _context(control))
        assert planner.calls == control.calls

    def test_a_whole_loop_iteration_costs_one_call(self) -> None:
        # The claim at the level a report reads: through the real loop, an easy step that stops
        # spends one planner call and the run finishes on it.
        planner = CannedLLMClient(
            [_planner(*_EASY, action={"kind": "stop", "reason": "nothing to fix"})]
        )
        result = make_llm_investigate(
            _FakeMCPClient(lambda _n, _a: _lag_response()),
            planner,
            model="m",
            strategy=_arm(),
            selector_llm_client=CannedLLMClient([]),
        )(_state(), _NOW)
        assert len(planner.calls) == 1
        assert result.state is IncidentState.ESCALATED


class TestAHardStepClimbsTheLadder:
    """Plan 00 § 7 item 10, second half — and the transitions are on the record."""

    def test_a_low_confidence_step_buys_the_enumerated_rung(self) -> None:
        _, _, record, planner, selector = _climb(
            [_planner(*_HARD), _candidates(confidences=(0.95, 0.5, 0.4, 0.3))]
        )
        ladder = _ladder(record)
        assert len(planner.calls) == 2
        assert selector.calls == []
        assert ladder.terminated_on == Rung.ENUMERATED.value
        assert ladder.extra_llm_calls == 1
        assert EscalationSignal.TOP1_CONFIDENCE_LOW.value in ladder.rungs[1].entered_because
        assert len(record.candidate_set) == LADDER_N

    def test_signals_that_survive_enumeration_buy_the_selector_rung(self) -> None:
        # The enumerated set is four candidates 0.1 apart, so the margin signal is still firing
        # after the rung that was bought to clear it; the selector commits and it clears.
        _, _, record, planner, selector = _climb([_planner(*_HARD), _candidates()], [_selection()])
        ladder = _ladder(record)
        assert len(planner.calls) == 2
        assert len(selector.calls) == 1
        assert ladder.terminated_on == Rung.SELECTOR.value
        assert ladder.extra_llm_calls == 2
        assert record.selector is not None
        assert record.selector.decision == "select"
        assert ladder.rungs[2].entered_because == (EscalationSignal.TOP1_TOP2_MARGIN_NARROW.value,)

    def test_every_rung_transition_is_in_the_record(self) -> None:
        _, _, record, _, _ = _climb([_planner(*_HARD), _candidates()], [_selection()])
        rungs = _ladder(record).rungs
        assert [rung.rung for rung in rungs] == [rung.value for rung in CLIMB]
        assert [rung.index for rung in rungs] == [0, 1, 2]
        for lower, upper in zip(rungs, rungs[1:], strict=False):
            assert upper.entered_because == lower.fired, (
                "a rung must name the signals that sent the run to it; otherwise the "
                "transition has to be inferred from which rungs are present"
            )
            assert lower.climbed is True
            assert lower.emitted is False
        assert rungs[-1].emitted is True
        assert sum(rung.emitted for rung in rungs) == 1

    def test_the_climb_stops_at_the_first_rung_whose_signals_clear(self) -> None:
        # Falsifiability for the test above: the same hard step whose enumerated set clears
        # everything never reaches the selector, and its selector client is untouched.
        _, _, record, _, selector = _climb(
            [_planner(*_HARD), _candidates(confidences=(0.95, 0.5, 0.4, 0.3))], [_selection()]
        )
        assert _ladder(record).terminated_on == Rung.ENUMERATED.value
        assert selector.calls == []
        assert selector.has_remaining

    def test_each_rung_carries_its_own_cost(self) -> None:
        after, _, record, _, _ = _climb(
            [_planner(*_HARD), _candidates()],
            [_selection()],
            usage=CannedUsage(input_tokens=100, output_tokens=50),
        )
        ladder = _ladder(record)
        assert all(rung.tokens_used > 0 for rung in ladder.rungs)
        assert sum(rung.tokens_used for rung in ladder.rungs) == after.budget.tokens_used
        assert sum(rung.usd_used for rung in ladder.rungs) == after.budget.usd_used
        assert sum(rung.llm_calls for rung in ladder.rungs) == len(record.llm_calls)

    def test_the_unmeasured_signals_are_named_rather_than_read_as_not_fired(self) -> None:
        # The baseline rung can measure four of the seven: the other three need a candidate set
        # or a selector's own number, and INC-003's lesson is that absent is not zero.
        _, _, record, _, _ = _climb([_planner(*_EASY)])
        unmeasured = _ladder(record).rungs[0].unmeasured
        assert set(unmeasured) == {
            EscalationSignal.SELECTOR_UNCERTAINTY_HIGH.value,
            EscalationSignal.CANDIDATE_DISAGREEMENT_HIGH.value,
            EscalationSignal.CONTRADICTORY_EVIDENCE.value,
        }


class TestTheTailIsSearchOnlyWhereABranchMayRead:
    """ADR 0060 reaches the top rung: a walk that cannot read must not be reported as one."""

    def test_without_a_prober_the_tail_is_escalate(self) -> None:
        assert ladder_for(_context(CannedLLMClient([]))) == (*CLIMB, Rung.ESCALATE)

    def test_with_a_prober_the_tail_is_search(self) -> None:
        prober = make_branch_prober(_FakeMCPClient(lambda _n, _a: _lag_response()))
        assert ladder_for(_context(CannedLLMClient([]), prober=prober)) == (*CLIMB, Rung.SEARCH)

    def test_an_exhausted_ladder_without_search_stops_and_says_why(self) -> None:
        _, step, record, _, _ = _climb(
            [_planner(*_HARD), _candidates()],
            [_selection(decision="probe_more", uncertainty=0.9)],
        )
        ladder = _ladder(record)
        assert ladder.terminated_on == Rung.ESCALATE.value
        assert ladder.search_available is False
        assert isinstance(step.next_action, StopAction)
        assert LADDER_EXHAUSTED in step.next_action.reason
        assert SEARCH_RUNG_UNAVAILABLE in step.next_action.reason
        assert EscalationSignal.SELECTOR_UNCERTAINTY_HIGH.value in step.next_action.reason

    def test_the_escalate_rung_buys_no_inference(self) -> None:
        _, _, record, planner, selector = _climb(
            [_planner(*_HARD), _candidates()],
            [_selection(decision="probe_more", uncertainty=0.9)],
        )
        escalate = _ladder(record).rungs[-1]
        assert escalate.rung == Rung.ESCALATE.value
        assert escalate.llm_calls == 0
        assert escalate.tokens_used == 0
        assert len(planner.calls) + len(selector.calls) == 3

    def test_in_recorded_mode_the_top_rung_walks_and_its_walk_is_recorded(self) -> None:
        mcp = _FakeMCPClient(lambda _n, a: _lag_response(str(a.get("consumer_group", "billing"))))
        planner_payloads = [_planner(*_HARD), _candidates()]
        # The walk's own generator runs at N = branch factor (3), so its sets are three-wide.
        planner_payloads += [_candidates(3, confidences=(0.9, 0.8, 0.7)) for _ in range(4)]
        selector_payloads = [_selection(decision="probe_more", uncertainty=0.9)]
        selector_payloads += [_walk_selection(uncertainty=0.05) for _ in range(12)]
        _, _, record, _, _ = _climb(
            planner_payloads, selector_payloads, prober=make_branch_prober(mcp)
        )
        ladder = _ladder(record)
        assert ladder.search_available is True
        assert ladder.terminated_on == Rung.SEARCH.value
        assert ladder.ladder[-1] == Rung.SEARCH.value
        assert record.search is not None
        assert record.search.branches_taken >= 1
        assert mcp.calls, "the search rung reads the world through the loop's own prober"
        # The walk is many calls, and the rung's count is the walk's own.
        assert ladder.rungs[-1].llm_calls > 1
        assert sum(rung.llm_calls for rung in ladder.rungs) == len(record.llm_calls)

    def test_the_runner_refuses_search_outside_recorded_mode_and_not_adaptive(self) -> None:
        # The ladder is not refused off recorded mode: it ends at ``escalate`` instead, which is
        # plan 02 § 15's own "search or escalate". ``search`` itself is still refused.
        adaptive = _settings(inference_strategy=StrategyName.ADAPTIVE.value)
        search = _settings(inference_strategy=StrategyName.SEARCH.value)
        assert search_mode_refusal(adaptive, recorded=False) is None
        assert search_mode_refusal(search, recorded=False) == SEARCH_IS_RECORDED_MODE_ONLY


class TestNoRungWidensWhatMayBeActedOn:
    """Plan 02 § 18 and plan 00 § 3.10: more thinking, not more privilege — at every rung."""

    def test_the_module_mints_no_action_but_a_stop(self) -> None:
        # Structural: the only action class this module constructs is ``StopAction``, so no rung
        # can invent a probe or a remediation. The emitted action comes from a planner call.
        built = sorted(
            {
                node.func.id
                for node in ast.walk(ast.parse(_ADAPTIVE.read_text(encoding="utf-8")))
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id.endswith("Action")
            }
        )
        assert built == [StopAction.__name__], (
            f"adaptive.py constructs {built}. A rung buys thinking, never privilege: the action "
            "a rung emits is the one a planner call proposed, and the only thing the ladder may "
            "mint for itself is a stop."
        )

    @pytest.mark.parametrize(
        ("rung", "planner_payloads", "selector_payloads"),
        [
            (Rung.BASELINE, [_planner(*_EASY, action={"kind": "remediate", "reason": "fix"})], []),
            (
                Rung.ENUMERATED,
                [
                    _planner(*_HARD),
                    _candidates(
                        confidences=(0.95, 0.5, 0.4, 0.3),
                        action={"kind": "remediate", "reason": "fix"},
                    ),
                ],
                [],
            ),
            (
                Rung.SELECTOR,
                [
                    _planner(*_HARD),
                    _candidates(action={"kind": "remediate", "reason": "fix"}),
                ],
                [_selection()],
            ),
        ],
        ids=lambda value: value.value if isinstance(value, Rung) else "",
    )
    def test_the_emitted_action_is_the_object_a_planner_call_produced(
        self,
        rung: Rung,
        planner_payloads: list[dict[str, Any]],
        selector_payloads: list[dict[str, Any]],
    ) -> None:
        _, step, record, _, _ = _climb(planner_payloads, selector_payloads)
        assert _ladder(record).terminated_on == rung.value
        assert isinstance(step.next_action, RemediateAction)
        assert step.next_action.reason == "fix", (
            "the rung rewrote the action it was handed; a ladder may choose a step, never "
            "compose one"
        )

    def test_the_escalate_rung_can_only_stop(self) -> None:
        _, step, _, _, _ = _climb(
            [_planner(*_HARD), _candidates(action={"kind": "remediate", "reason": "fix"})],
            [_selection(decision="probe_more", uncertainty=0.9)],
        )
        assert isinstance(step.next_action, StopAction), (
            "the tail rung must not act: it is reached because the run is still uncertain"
        )

    def test_the_remediate_gates_still_run_on_the_adaptive_path(self) -> None:
        # The 0.7 threshold, exercised through the seam: a climbed rung's remediate handoff
        # below the bar escalates, exactly as on ``baseline``. The floor is lowered to 0.62 so
        # the ladder emits that step at all — see the test below for why it otherwise cannot.
        result = make_llm_investigate(
            _FakeMCPClient(lambda _n, _a: _lag_response()),
            CannedLLMClient(
                [
                    _planner(0.6, 0.55),
                    _candidates(
                        confidences=(0.65, 0.2, 0.1, 0.05),
                        action={"kind": "remediate", "reason": "fix it"},
                    ),
                ]
            ),
            model="m",
            strategy=_arm(uncertainty_top1_confidence_floor=0.62),
            selector_llm_client=CannedLLMClient([]),
        )(_state(), _NOW)
        assert result.state is IncidentState.ESCALATED
        assert "below threshold" in result.evidence[-1].result_summary

    def test_under_the_declared_floor_a_below_bar_diagnosis_never_reaches_the_gate(self) -> None:
        # Plan 02 § 16 and the floor's own rationale: 0.75 sits ABOVE the loop's 0.7 bar, so the
        # ladder buys inference while the run is still inside the region the gate would refuse,
        # never after it has refused. A 0.65 diagnosis therefore lands on the tail rung.
        _, step, record, _, _ = _climb(
            [
                _planner(0.6, 0.55),
                _candidates(
                    confidences=(0.65, 0.2, 0.1, 0.05),
                    action={"kind": "remediate", "reason": "fix it"},
                ),
            ],
            [_selection()],
        )
        assert _ladder(record).terminated_on == Rung.ESCALATE.value
        assert isinstance(step.next_action, StopAction)
        assert (
            declared_default(ThresholdName.TOP1_CONFIDENCE_FLOOR) > REMEDIATE_CONFIDENCE_THRESHOLD
        )

    def test_a_category_outside_the_fix_map_still_escalates_from_a_climbed_rung(self) -> None:
        result = make_llm_investigate(
            _FakeMCPClient(lambda _n, _a: _lag_response()),
            CannedLLMClient(
                [
                    _planner(0.6, 0.55),
                    _candidates(
                        confidences=(0.95, 0.2, 0.1, 0.05),
                        categories=("deploy_regression",),
                        action={"kind": "remediate", "reason": "roll it back"},
                    ),
                ]
            ),
            model="m",
            strategy=_arm(),
            selector_llm_client=CannedLLMClient([]),
        )(_state(), _NOW)
        assert result.state is IncidentState.ESCALATED
        assert "no Tier-1 fix" in result.evidence[-1].result_summary

    def test_the_arm_reaches_no_tool_and_no_tier(self) -> None:
        # The package-wide scans in test_strategies.py cover every module by glob; this is the
        # same claim stated where a reader of this packet will look for it.
        source = _ADAPTIVE.read_text(encoding="utf-8")
        forbidden = ("FIX_MAP", "tier_of", "TOOL_REGISTRY", "wire_arguments", "_execute_probe")
        assert [name for name in forbidden if name in source] == []


class TestTheFailedRemediationTriggerNeedsTheEdge:
    """Plan 04's dependency: P13's failed-action trigger requires P10 (ADR 0056)."""

    def test_a_confident_step_with_no_attempt_stays_cheap(self) -> None:
        _, _, record, planner, _ = _climb([_planner(*_EASY)], run_state=_state())
        assert failed_attempts(_state()) == 0
        assert _ladder(record).terminated_on == Rung.BASELINE.value
        assert len(planner.calls) == 1

    def test_the_same_step_after_a_failed_attempt_climbs(self) -> None:
        attempted = _state(evidence=(_attempt_entry(),), remediation_attempts=1)
        assert failed_attempts(attempted) == 1
        _, _, record, planner, selector = _climb(
            [_planner(*_EASY), _candidates(confidences=(0.95, 0.5, 0.4, 0.3))],
            [_selection()],
            run_state=attempted,
        )
        ladder = _ladder(record)
        assert ladder.terminated_on == Rung.SELECTOR.value
        assert EscalationSignal.REMEDIATION_ATTEMPT_FAILED.value in ladder.rungs[1].entered_because
        assert len(planner.calls) == 2
        assert len(selector.calls) == 1

    def test_the_signal_no_rung_can_clear_does_not_decide_the_tail(self) -> None:
        # The attempt record stays on the ledger for the rest of the run, so this signal fires at
        # every rung. Letting it decide the tail would escalate every step after one failed
        # attempt — which is ADR 0056's reinvestigation cut short on the step it was granted for.
        attempted = _state(evidence=(_attempt_entry(),), remediation_attempts=1)
        _, step, record, _, _ = _climb(
            [_planner(*_EASY), _candidates(confidences=(0.95, 0.5, 0.4, 0.3))],
            [_selection()],
            run_state=attempted,
        )
        ladder = _ladder(record)
        assert ladder.terminated_on == Rung.SELECTOR.value
        assert ladder.unclearable == (EscalationSignal.REMEDIATION_ATTEMPT_FAILED.value,)
        assert EscalationSignal.REMEDIATION_ATTEMPT_FAILED.value in ladder.rungs[-1].fired
        assert not isinstance(step.next_action, StopAction), (
            "the reinvestigation the retry edge granted must still be able to probe"
        )

    def test_a_clearable_signal_beside_it_still_reaches_the_tail(self) -> None:
        # The other half: the unclearable signal is not an exemption from the tail, it just
        # cannot cause one on its own.
        attempted = _state(evidence=(_attempt_entry(),), remediation_attempts=1)
        _, step, record, _, _ = _climb(
            [_planner(*_HARD), _candidates()],
            [_selection(decision="probe_more", uncertainty=0.9)],
            run_state=attempted,
        )
        ladder = _ladder(record)
        assert ladder.terminated_on == Rung.ESCALATE.value
        assert ladder.unclearable == (EscalationSignal.REMEDIATION_ATTEMPT_FAILED.value,)
        assert isinstance(step.next_action, StopAction)

    def test_only_the_remediation_loop_writes_the_record_the_trigger_reads(self) -> None:
        # Why the trigger cannot fire before the retry edge: one writer, and it is the loop
        # ADR 0056 added. A second writer would make this signal reachable from anywhere.
        writers = sorted(
            path.relative_to(_SRC).as_posix()
            for path in _SRC.rglob("*.py")
            if f"tool_name={ATTEMPT_FAILED_MARKER!s}" in path.read_text(encoding="utf-8")
            or "tool_name=ATTEMPT_FAILED_MARKER" in path.read_text(encoding="utf-8")
        )
        assert writers == ["agent/remediation.py"], (
            f"{writers} write the attempt-failed record. The signal is only true after "
            "ADR 0056's VERIFYING→INVESTIGATING edge, and one writer is what keeps it so."
        )

    def test_a_first_investigation_cannot_carry_the_record(self) -> None:
        fresh = _state(
            evidence=(
                EvidenceEntry(
                    tool_name="get_consumer_lag",
                    arguments={"consumer_group": "billing"},
                    result_summary='{"lag": 42}',
                    timestamp=_NOW,
                ),
            )
        )
        assert fresh.remediation_attempts == 0
        assert failed_attempts(fresh) == 0
        _, _, record, _, _ = _climb([_planner(*_EASY)], run_state=fresh)
        assert _ladder(record).rungs[0].fired == ()


class TestTheLadderIsDrivenByTheThresholds:
    """WP-13.1's numbers decide every transition; this module declares none of its own."""

    def test_a_raised_floor_sends_a_previously_easy_step_up(self) -> None:
        _, _, cheap, _, _ = _climb([_planner(*_EASY)])
        assert _ladder(cheap).terminated_on == Rung.BASELINE.value
        _, _, climbed, planner, _ = _climb(
            [_planner(*_EASY), _candidates(confidences=(0.99, 0.2, 0.1, 0.05))],
            uncertainty_top1_confidence_floor=0.95,
        )
        assert _ladder(climbed).terminated_on == Rung.ENUMERATED.value
        assert len(planner.calls) == 2

    def test_a_lowered_floor_keeps_a_previously_hard_step_cheap(self) -> None:
        _, _, record, planner, selector = _climb(
            [_planner(*_HARD)],
            uncertainty_top1_confidence_floor=0.5,
            uncertainty_top1_top2_margin_floor=0.01,
        )
        assert _ladder(record).terminated_on == Rung.BASELINE.value
        assert len(planner.calls) == 1
        assert selector.calls == []

    def test_the_record_carries_the_values_the_climb_compared_against(self) -> None:
        _, _, record, _, _ = _climb([_planner(*_EASY)])
        thresholds = _ladder(record).thresholds
        assert thresholds == {name.value: declared_default(name) for name in ThresholdName}, (
            "the record must say which operating point decided this step"
        )

    def test_an_override_reaches_the_record_and_the_stamp(self) -> None:
        arm = _arm(uncertainty_top1_confidence_floor=0.95)
        assert arm.thresholds.top1_confidence_floor == 0.95
        row = arm.config["thresholds"][ThresholdName.TOP1_CONFIDENCE_FLOOR.value]
        assert row["value"] == 0.95
        assert row["is_declared_default"] is False
        assert row["declared_split"] == "untuned"

    def test_every_threshold_is_stamped_with_the_split_behind_it(self) -> None:
        stamped = _arm().config["thresholds"]
        assert set(stamped) == {row.name.value for row in provenance_rows()}
        for row in provenance_rows():
            entry = stamped[row.name.value]
            assert entry["declared_split"] == row.declared_split.value
            assert entry["source"] == row.source
            assert entry["signal"] == row.signal.value

    def test_the_module_holds_no_threshold_of_its_own(self) -> None:
        # Every number the ladder compares against is a declared default (ADR 0061), so a float
        # literal here would be a second, split-less declaration. ``LADDER_N`` is the one
        # integer, and it is the ladder's shape rather than a threshold.
        tree = ast.parse(_ADAPTIVE.read_text(encoding="utf-8"))
        floats = [
            f"line {node.lineno}: {node.value!r}"
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, float)
        ]
        assert floats == [], f"{floats} are thresholds written in the arm instead of declared"
        assert LADDER_N == 4

    def test_the_thresholds_are_resolved_once_at_construction(self) -> None:
        arm = _arm()
        assert isinstance(arm.thresholds, UncertaintyThresholds)
        assert arm.thresholds == UncertaintyThresholds()


class TestTheArmIsRegisteredAndOffByDefault:
    def test_the_registry_builds_it_under_its_own_name(self) -> None:
        arm = STRATEGIES.create(StrategyName.ADAPTIVE.value)
        assert arm.name == StrategyName.ADAPTIVE.value == "adaptive"
        assert isinstance(arm, AdaptiveStrategy)

    def test_it_is_not_the_default(self) -> None:
        assert _settings().inference_strategy is StrategyName.BASELINE

    def test_the_rungs_are_the_shipped_arms_rather_than_copies_of_them(self) -> None:
        arm = _arm()
        assert arm.generator.name == StrategyName.BEST_OF_N_ENUMERATED.value
        assert arm.generator.config["n"] == LADDER_N
        assert arm.search.name == StrategyName.SEARCH.value

    def test_the_config_block_cannot_be_written_through(self) -> None:
        with pytest.raises(TypeError):
            _arm().config["n"] = 1  # type: ignore[index]

    def test_the_provenance_record_stamps_the_ladder_and_serializes(self) -> None:
        provenance = build_provenance(
            "s",
            _settings(inference_strategy=StrategyName.ADAPTIVE.value),
            model_role=ModelRole.DEVELOPMENT,
            invocation_id="inv000000001",
            execution_mode=ExecutionMode.CANNED,
            budget=_ledger(),
        )
        assert provenance.strategy == StrategyName.ADAPTIVE.value
        assert provenance.strategy_config["n"] == LADDER_N
        assert provenance.strategy_config["selector"] == SELECTOR_ROLE
        assert provenance.strategy_config["cap"] == "structural"
        # The stamp has to survive the archive: a run report is JSON on disk.
        assert json.loads(provenance.model_dump_json())["strategy_config"]["ladder"] == [
            *(rung.value for rung in CLIMB),
            f"{Rung.SEARCH.value}_or_{Rung.ESCALATE.value}",
        ]

    def test_the_edge_carries_every_threshold_override_to_the_arm(self) -> None:
        knobs = strategy_knobs(_settings(uncertainty_failed_attempt_count=2))
        assert knobs.uncertainty_failed_attempt_count == 2
        unset = {
            field: getattr(knobs, field)
            for field in dir(knobs)
            if field.startswith("uncertainty_") and field != "uncertainty_failed_attempt_count"
        }
        assert set(unset.values()) == {None}
        assert _arm(**{"uncertainty_failed_attempt_count": 2}).thresholds.failed_attempt_count == 2

    def test_a_step_without_a_selector_client_is_refused_before_any_call(self) -> None:
        planner = CannedLLMClient([_planner(*_EASY)])
        context = StrategyContext(llm_client=planner, model="m", iteration=0)
        with pytest.raises(ValueError, match=NO_SELECTOR_CLIENT):
            _arm().plan_next_step(_state(), _NOW, context)
        assert planner.calls == []


class TestTheBillIsCarriedWhenARungFails:
    """ADR 0045 one layer up: the loop charges against the state held before the step."""

    def test_a_failing_rung_carries_every_earlier_rungs_usage(self) -> None:
        with pytest.raises(AdaptiveFailed) as failure:
            _climb(
                [_planner(*_HARD), _candidates()],
                [],
                usage=CannedUsage(input_tokens=100, output_tokens=50),
            )
        usage = failure.value.usage
        assert usage is not None
        # Two billed planner legs before the selector call that had no answer left.
        assert usage.input_tokens == 200
        assert usage.output_tokens == 100

    def test_the_loop_escalates_and_charges_it(self) -> None:
        result = make_llm_investigate(
            _FakeMCPClient(lambda _n, _a: _lag_response()),
            CannedLLMClient(
                [_planner(*_HARD), _candidates()],
                usage=CannedUsage(input_tokens=100, output_tokens=50),
            ),
            model="claude-sonnet-4-6",
            strategy=_arm(),
            selector_llm_client=CannedLLMClient([]),
        )(_state(), _NOW)
        assert result.state is IncidentState.ESCALATED
        assert result.budget.tokens_used > 0

    def test_a_baseline_rung_failure_propagates_as_it_does_on_the_control_group(self) -> None:
        with pytest.raises(LLMError) as failure:
            _climb([])
        assert not isinstance(failure.value, AdaptiveFailed)


class TestTheRecordTellsTheArmsApart:
    def test_the_ladder_block_is_absent_on_every_other_arm(self) -> None:
        _, _, control = BaselineStrategy().plan_next_step(
            _state(), _NOW, _context(CannedLLMClient([_planner(*_EASY)]))
        )
        assert control.ladder is None

    def test_the_record_serializes_for_a_tracer(self) -> None:
        _, _, record, _, _ = _climb([_planner(*_HARD), _candidates()], [_selection()])
        payload = json.loads(json.dumps(record.as_trace_record()))
        ladder = payload["ladder"]
        assert ladder["terminated_on"] == Rung.SELECTOR.value
        assert ladder["extra_llm_calls"] == 2
        assert [rung["rung"] for rung in ladder["rungs"]] == [rung.value for rung in CLIMB]
        assert isinstance(ladder["rungs"][0]["usd_used"], str)
        assert ladder["thresholds"][ThresholdName.TOP1_CONFIDENCE_FLOOR.value] == declared_default(
            ThresholdName.TOP1_CONFIDENCE_FLOOR
        )

    def test_the_sink_is_called_exactly_once_per_step(self) -> None:
        # Two rungs of the ladder are whole strategies; each writes a record of its own when it
        # runs alone, and a step that produced two records would double every count downstream.
        sink: list[StepRecord] = []
        _arm().plan_next_step(
            _state(),
            _NOW,
            _context(
                CannedLLMClient([_planner(*_HARD), _candidates()]),
                CannedLLMClient([_selection()]),
                sink=sink,
            ),
        )
        assert len(sink) == 1
        assert sink[0].strategy == StrategyName.ADAPTIVE.value

    def test_the_search_rungs_walk_does_not_write_a_second_record(self) -> None:
        sink: list[StepRecord] = []
        planner_payloads = [_planner(*_HARD), _candidates()]
        planner_payloads += [_candidates(3, confidences=(0.9, 0.8, 0.7)) for _ in range(4)]
        selector_payloads = [_selection(decision="probe_more", uncertainty=0.9)]
        selector_payloads += [_walk_selection(uncertainty=0.05) for _ in range(12)]
        _arm().plan_next_step(
            _state(),
            _NOW,
            _context(
                CannedLLMClient(planner_payloads),
                CannedLLMClient(selector_payloads),
                prober=make_branch_prober(_FakeMCPClient(lambda _n, _a: _lag_response())),
                sink=sink,
            ),
        )
        assert len(sink) == 1
        assert sink[0].strategy == StrategyName.ADAPTIVE.value
        assert sink[0].search is not None


class TestTheDecisionIsRecorded:
    """ADR 0064 exists, is accepted, and is in the index — the repo's own convention."""

    def test_the_adr_exists_and_is_indexed(self) -> None:
        adr = _REPO_ROOT / "docs" / "ADR"
        matches = sorted(adr.glob("0064-*.md"))
        assert len(matches) == 1
        assert "accepted" in matches[0].read_text(encoding="utf-8").lower()
        index = (adr / "README.md").read_text(encoding="utf-8")
        assert matches[0].name.removesuffix(".md").split("-", 1)[1] in index

    def test_the_methodology_documents_the_ladder(self) -> None:
        text = (_REPO_ROOT / "docs" / "eval-methodology.md").read_text(encoding="utf-8")
        assert "## The adaptive ladder and its frontier" in text
        for rung in Rung:
            assert f"`{rung.value}`" in text


def test_the_category_used_by_these_fixtures_is_still_a_fix_map_category() -> None:
    # Anti-vacuity for the gate tests above: they need a category that WOULD remediate.
    from incident_commander.agent.investigation import FIX_MAP

    assert HypothesisCategory.CONSUMER_SATURATION in FIX_MAP
    assert HypothesisCategory.DEPLOY_REGRESSION not in FIX_MAP
