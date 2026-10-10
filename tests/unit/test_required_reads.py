"""ADR 0078: a verdict is refused until the reads its alert requires are in the evidence.

INC-006 / O-44: five live runs concluded on one or two reads and graded EVIDENCE red. The rule
is the alert's burden of proof, declared by the scenario, and enforced by the loop.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, Final

import pytest
from pydantic import ValidationError

from evals import runner
from evals.fakes import CannedMCPClient
from evals.graders.deterministic import GradeDimension
from evals.scenarios.loader import load_scenarios
from evals.scenarios.schema import FAMILY_REQUIRED_BEFORE_VERDICT, Scenario, ScenarioFamily
from incident_commander.agent.hypothesis import (
    InvestigationStep,
    asked_for_a_verdict,
    only_probes,
)
from incident_commander.agent.investigation import (
    _MAX_VERDICT_REFUSALS,
    VERDICT_REFUSED_MARKER,
    make_llm_investigate,
)
from incident_commander.agent.required_reads import RequiredReading, VerdictCondition
from incident_commander.agent.state import IncidentState, RunState
from incident_commander.agent.strategies.protocol import StrategyContext
from incident_commander.agent.thinking import VERDICT_GATE_TOOL, ObservedThinking, PlannerLog
from incident_commander.llm.fakes import CannedLLMClient

_SCENARIOS_DIR: Final[Path] = Path(__file__).resolve().parents[2] / "evals" / "scenarios"
_CORPUS: Final[dict[str, Scenario]] = {s.name: s for s in load_scenarios(_SCENARIOS_DIR)}
_CHAIN: Final[str] = "4a30546f-d3c5-549f-a772-633c0b26219d"
_SLICE: Final[str] = "replay_safe"

_LATENCY_SWEEP: Final[tuple[RequiredReading, ...]] = FAMILY_REQUIRED_BEFORE_VERDICT[
    ScenarioFamily.API_LATENCY
]
# The workflow_stuck family's declaration: the whole queue (ADR 0078), the resolver BY NAME and
# the background loops (O-49, ADR 00XX).
_CHAIN_SWEEP: Final[tuple[RequiredReading, ...]] = FAMILY_REQUIRED_BEFORE_VERDICT[
    ScenarioFamily.WORKFLOW_STUCK
]
# ADR 0078's mechanism on its own, as the tests below were written against it: one read of the
# whole queue before any chain verdict. Declared here rather than taken from the family, so the
# mechanism's tests do not move when the family's sweep grows.
_WHOLE_QUEUE: Final[tuple[RequiredReading, ...]] = (
    RequiredReading(tool="list_dlq_messages", when=VerdictCondition.STUCK_CHAIN),
)
_RESOLVER: Final[str] = "dependency-resolver"
_RESOLVER_READ: Final[str] = f"get_consumer_lag(consumer_group={_RESOLVER})"


def _step(category: str, confidence: float, action: dict[str, Any]) -> dict[str, Any]:
    return {
        "hypotheses": [
            {"category": category, "name": category, "confidence": confidence, "reasoning": "r"}
        ],
        "next_action": action,
    }


def _probe(tool: str, **arguments: Any) -> dict[str, Any]:
    return {"kind": "probe", "tool_name": tool, "arguments": arguments}


def _stop(reason: str = "done") -> dict[str, Any]:
    return {"kind": "stop", "reason": reason}


def _investigating(run_state: RunState, alert: Mapping[str, Any]) -> RunState:
    return run_state.model_copy(update={"state": IncidentState.INVESTIGATING, "alert": alert})


def _latency_world() -> CannedMCPClient:
    return CannedMCPClient(_CORPUS["api_latency_healthy_control"].canned_tool_responses)


def _chain_world() -> CannedMCPClient:
    return CannedMCPClient(_CORPUS["workflow_stuck_paused_dag"].canned_tool_responses)


_LATENCY_ALERT: Final[dict[str, Any]] = {"source": "monitoring.slo", "severity": "critical"}
_CHAIN_ALERT: Final[dict[str, Any]] = {"source": "platform.dag", "job_id": _CHAIN}


def _reads(run_state: RunState) -> list[str]:
    return [e.tool_name for e in run_state.evidence if not e.tool_name.startswith("_")]


def _refusals(run_state: RunState) -> list[Any]:
    return [e for e in run_state.evidence if e.tool_name == VERDICT_REFUSED_MARKER]


class TestTheVerdictIsRefusedUntilTheReadsAreMade:
    def test_no_fault_after_one_read_is_refused_and_the_sweep_is_forced(
        self, run_state: RunState, now: datetime
    ) -> None:
        llm = CannedLLMClient(
            [
                _step("unknown", 0.4, _probe("get_slo_status")),
                _step("no_fault", 0.85, _stop("the objective is whole")),
                _step("no_fault", 0.85, _probe("get_postgres_health")),
                _step("no_fault", 0.85, _probe("get_redis_health")),
                _step("no_fault", 0.85, _probe("get_circuit_breakers")),
                _step("no_fault", 0.9, _stop("every dependency is at baseline")),
            ]
        )
        result = make_llm_investigate(
            _latency_world(), llm, model="m", required_before_verdict=_LATENCY_SWEEP
        )(_investigating(run_state, _LATENCY_ALERT), now)

        assert result.state is IncidentState.ESCALATED
        assert _reads(result) == [
            "get_slo_status",
            "get_postgres_health",
            "get_redis_health",
            "get_circuit_breakers",
        ]
        [refusal] = _refusals(result)
        assert refusal.arguments["missing_reads"] == [
            "get_postgres_health",
            "get_redis_health",
            "get_circuit_breakers",
        ]
        assert refusal.arguments["offered"] == ["probe"]
        assert "stop refused" in refusal.result_summary
        assert "every dependency is at baseline" in result.evidence[-1].result_summary

    def test_a_verdict_other_than_no_fault_is_not_held_to_the_sweep(
        self, run_state: RunState, now: datetime
    ) -> None:
        llm = CannedLLMClient(
            [
                _step("unknown", 0.4, _probe("get_slo_status")),
                _step("db_query_latency", 0.9, _stop("a query is slow")),
            ]
        )
        result = make_llm_investigate(
            _latency_world(), llm, model="m", required_before_verdict=_LATENCY_SWEEP
        )(_investigating(run_state, _LATENCY_ALERT), now)

        assert result.state is IncidentState.ESCALATED
        assert _refusals(result) == []
        assert _reads(result) == ["get_slo_status"]

    def test_no_declaration_means_no_rule(self, run_state: RunState, now: datetime) -> None:
        llm = CannedLLMClient(
            [
                _step("unknown", 0.4, _probe("get_slo_status")),
                _step("no_fault", 0.9, _stop()),
            ]
        )
        result = make_llm_investigate(_latency_world(), llm, model="m")(
            _investigating(run_state, _LATENCY_ALERT), now
        )
        assert _refusals(result) == []
        assert _reads(result) == ["get_slo_status"]

    def test_any_verdict_on_a_stuck_chain_needs_the_whole_queue(
        self, run_state: RunState, now: datetime
    ) -> None:
        llm = CannedLLMClient(
            [
                _step("runaway_saga", 0.5, _probe("get_dag_state", job_id=_CHAIN)),
                _step("dag_paused", 0.9, _stop("the chain is paused")),
                _step("dag_paused", 0.9, _probe("list_dlq_messages")),
                _step("dag_paused", 0.9, _stop("paused, nothing dead-lettered")),
            ]
        )
        result = make_llm_investigate(
            _chain_world(), llm, model="m", required_before_verdict=_WHOLE_QUEUE
        )(_investigating(run_state, _CHAIN_ALERT), now)

        assert result.state is IncidentState.ESCALATED
        assert _reads(result) == ["get_dag_state", "list_dlq_messages"]
        assert len(_refusals(result)) == 1

    def test_stuck_chain_is_inert_on_an_alert_that_names_no_chain(
        self, run_state: RunState, now: datetime
    ) -> None:
        llm = CannedLLMClient([_step("unknown", 0.6, _stop())])
        result = make_llm_investigate(
            _chain_world(), llm, model="m", required_before_verdict=_WHOLE_QUEUE
        )(_investigating(run_state, _LATENCY_ALERT), now)
        assert _refusals(result) == []

    def test_a_filtered_listing_does_not_pay_the_whole_queue_read(
        self, run_state: RunState, now: datetime
    ) -> None:
        llm = CannedLLMClient(
            [
                _step("dag_paused", 0.9, _probe("list_dlq_messages", remediation_hint=_SLICE)),
                _step("dag_paused", 0.9, _stop()),
                _step("dag_paused", 0.9, _probe("list_dlq_messages")),
                _step("dag_paused", 0.9, _stop()),
            ]
        )
        result = make_llm_investigate(
            _chain_world(), llm, model="m", required_before_verdict=_WHOLE_QUEUE
        )(_investigating(run_state, _CHAIN_ALERT), now)
        [refusal] = _refusals(result)
        assert "no job_type and no remediation_hint filter" in refusal.result_summary
        assert _reads(result) == ["list_dlq_messages", "list_dlq_messages"]


class TestTheNarrowedStepOffersOnlyTheOwedReads:
    def test_the_schema_offers_only_a_probe_of_the_missing_tools(self) -> None:
        narrowed = StrategyContext(
            llm_client=CannedLLMClient([]),
            model="m",
            iteration=0,
            required_probes=("get_redis_health", "get_postgres_health"),
        ).step_model(InvestigationStep)
        schema = narrowed.model_json_schema()
        probe = schema["$defs"]["RequiredProbe"]
        assert probe["properties"]["tool_name"]["enum"] == [
            "get_postgres_health",
            "get_redis_health",
        ]
        assert schema["properties"]["next_action"]["$ref"] == "#/$defs/RequiredProbe"
        assert "StopAction" not in schema["$defs"]
        assert "RemediateAction" not in schema["$defs"]

    @pytest.mark.parametrize("verdict", [_stop(), {"kind": "remediate", "reason": "r"}])
    def test_a_verdict_under_it_is_refused_not_re_asked(self, verdict: dict[str, Any]) -> None:
        narrowed = only_probes(InvestigationStep, ["get_redis_health"])
        with pytest.raises(ValidationError) as caught:
            narrowed.model_validate(_step("no_fault", 0.9, verdict))
        assert asked_for_a_verdict(caught.value)

    def test_a_wrong_tool_is_unreadable_output_not_a_refusal(self) -> None:
        narrowed = only_probes(InvestigationStep, ["get_redis_health"])
        with pytest.raises(ValidationError) as caught:
            narrowed.model_validate(_step("no_fault", 0.9, _probe("get_slo_status")))
        assert not asked_for_a_verdict(caught.value)

    def test_the_narrowed_model_is_built_once_per_tool_set(self) -> None:
        first = only_probes(InvestigationStep, ["get_redis_health", "get_slo_status"])
        assert first is only_probes(InvestigationStep, ["get_slo_status", "get_redis_health"])

    def test_a_planner_that_keeps_concluding_escalates_naming_the_missing_reads(
        self, run_state: RunState, now: datetime
    ) -> None:
        stop = _step("no_fault", 0.9, _stop())
        llm = CannedLLMClient([_step("unknown", 0.4, _probe("get_slo_status"))] + [stop] * 5)
        result = make_llm_investigate(
            _latency_world(), llm, model="m", required_before_verdict=_LATENCY_SWEEP
        )(_investigating(run_state, _LATENCY_ALERT), now)

        assert result.state is IncidentState.ESCALATED
        assert len(_refusals(result)) == _MAX_VERDICT_REFUSALS
        reason = result.evidence[-1].result_summary
        assert "without making the reads this alert requires" in reason
        assert "get_postgres_health, get_redis_health, get_circuit_breakers" in reason
        assert _reads(result) == ["get_slo_status"]

    def test_owed_steps_do_not_count_against_max_iterations(
        self, run_state: RunState, now: datetime
    ) -> None:
        # Three free steps (two probes and the refused stop), three owed reads, one free verdict.
        llm = CannedLLMClient(
            [
                _step("unknown", 0.4, _probe("get_slo_status")),
                _step("unknown", 0.5, _probe("list_active_alerts")),
                _step("no_fault", 0.85, _stop()),
                _step("no_fault", 0.85, _probe("get_postgres_health")),
                _step("no_fault", 0.85, _probe("get_redis_health")),
                _step("no_fault", 0.85, _probe("get_circuit_breakers")),
                _step("no_fault", 0.9, _stop("swept")),
            ]
        )
        result = make_llm_investigate(
            _latency_world(),
            llm,
            model="m",
            max_iterations=4,
            required_before_verdict=_LATENCY_SWEEP,
        )(_investigating(run_state, _LATENCY_ALERT), now)
        assert result.evidence[-1].result_summary == "planner stop: swept"


class TestBudgetAware:
    def test_a_budget_that_cannot_pay_for_the_reads_escalates_with_that_reason(
        self, run_state: RunState, now: datetime
    ) -> None:
        tight = run_state.model_copy(
            update={"budget": run_state.budget.model_copy(update={"max_tool_calls": 2})}
        )
        llm = CannedLLMClient(
            [
                _step("unknown", 0.4, _probe("get_slo_status")),
                _step("no_fault", 0.9, _stop()),
            ]
        )
        result = make_llm_investigate(
            _latency_world(), llm, model="m", required_before_verdict=_LATENCY_SWEEP
        )(_investigating(tight, _LATENCY_ALERT), now)

        assert result.state is IncidentState.ESCALATED
        assert _refusals(result) == []
        reason = result.evidence[-1].result_summary
        assert "3 more tool call(s), and the tool budget has 1 left" in reason


class TestThePageShowsTheRefusal:
    def test_the_refusal_is_a_thinking_row_of_its_own(
        self, run_state: RunState, now: datetime
    ) -> None:
        log = PlannerLog(clock=lambda: now)
        seen: list[ObservedThinking] = []

        def sink(thinking: ObservedThinking) -> bool:
            seen.append(thinking)
            return True

        log.subscribe(sink)
        llm = CannedLLMClient(
            [
                _step("dag_paused", 0.9, _stop()),
                _step("dag_paused", 0.9, _probe("list_dlq_messages")),
                _step("dag_paused", 0.9, _stop()),
            ]
        )
        make_llm_investigate(
            _chain_world(),
            llm,
            model="m",
            planner_log=log,
            required_before_verdict=_WHOLE_QUEUE,
        )(_investigating(run_state, _CHAIN_ALERT), now)

        gates = [t for t in seen if t.tool == VERDICT_GATE_TOOL]
        assert len(gates) == 1
        assert (
            gates[0]
            .sentence()
            .startswith("top dag_paused 0.90 → stop refused, read first: list_dlq_messages")
        )


class TestTheScenarioDeclaresIt:
    def test_only_the_two_broad_page_families_declare_a_sweep(self) -> None:
        declaring = {name for name, s in _CORPUS.items() if s.required_before_verdict}
        assert declaring == {
            name
            for name, s in _CORPUS.items()
            if s.family in {ScenarioFamily.API_LATENCY, ScenarioFamily.WORKFLOW_STUCK}
        }
        assert len(declaring) == 9

    def test_the_family_defaults(self) -> None:
        assert [(r.tool, r.when) for r in _LATENCY_SWEEP] == [
            (tool, VerdictCondition.NO_FAULT)
            for tool in (
                "get_slo_status",
                "get_postgres_health",
                "get_redis_health",
                "get_circuit_breakers",
            )
        ]
        assert [(r.tool, r.when, dict(r.arguments)) for r in _CHAIN_SWEEP] == [
            ("list_dlq_messages", VerdictCondition.STUCK_CHAIN, {}),
            ("get_consumer_lag", VerdictCondition.STUCK_CHAIN, {"consumer_group": _RESOLVER}),
            ("get_control_loops", VerdictCondition.STUCK_CHAIN, {}),
        ]

    def test_an_explicit_empty_list_opts_out(self) -> None:
        payload = _CORPUS["api_latency_redis"].model_dump(mode="json")
        payload["required_before_verdict"] = []
        assert Scenario.model_validate(payload).required_before_verdict == ()

    def test_the_agent_visible_projection_carries_it(self) -> None:
        scenario = _CORPUS["workflow_stuck_paused_dag"]
        assert scenario.agent_visible().required_before_verdict == _CHAIN_SWEEP

    def test_only_read_tools_may_be_required(self) -> None:
        with pytest.raises(ValidationError):
            RequiredReading.model_validate({"tool": "restart_consumer_group", "when": "any"})


def _rescripted(name: str, script: list[dict[str, Any]], **update: Any) -> Scenario:
    scenario = _CORPUS[name]
    responses = {**scenario.canned_llm_responses, "investigation_planner": script}
    return scenario.model_copy(update={"canned_llm_responses": responses, **update})


def _grade(scenario: Scenario) -> dict[GradeDimension, bool]:
    result = runner.run_scenario(scenario, runner._eval_defaults())
    return {d.dimension: d.passed for d in result.outcome.report.dimensions}


class TestRedBeforeGreenAfterOnTheCannedFamilies:
    """INC-006's shape on the canned worlds: a planner that concludes after one read.

    ``required_before_verdict=()`` is the loop as it was before ADR 0078; the same script is
    EVIDENCE red there and green under the family's declaration.
    """

    _CONTROL: Final[list[dict[str, Any]]] = [
        _step("unknown", 0.4, _probe("get_slo_status")),
        _step("no_fault", 0.85, _stop("the objective is whole")),
        _step("no_fault", 0.85, _probe("get_postgres_health")),
        _step("no_fault", 0.85, _probe("get_redis_health")),
        _step("no_fault", 0.85, _probe("get_circuit_breakers")),
        _step("no_fault", 0.9, _stop("the objective and every dependency are at baseline")),
    ]
    _PAUSED: Final[list[dict[str, Any]]] = [
        _step("runaway_saga", 0.5, _probe("get_dag_state", job_id=_CHAIN)),
        _step("dag_paused", 0.9, _stop("the chain is paused by its root")),
        _step("dag_paused", 0.9, _probe("list_dlq_messages")),
        _step("dag_paused", 0.9, _probe("get_consumer_lag", consumer_group=_RESOLVER)),
        _step("dag_paused", 0.9, _probe("get_control_loops")),
        _step("dag_paused", 0.9, _stop("paused by its root; nothing of it is dead-lettered")),
    ]

    @pytest.mark.parametrize(
        ("name", "script"),
        [
            ("api_latency_healthy_control", _CONTROL),
            ("workflow_stuck_paused_dag", _PAUSED),
        ],
    )
    def test_without_the_rule_the_run_concludes_early_and_evidence_fails(
        self, name: str, script: list[dict[str, Any]]
    ) -> None:
        grades = _grade(_rescripted(name, script, required_before_verdict=()))
        assert grades[GradeDimension.OUTCOME]
        assert not grades[GradeDimension.EVIDENCE]

    @pytest.mark.parametrize(
        ("name", "script"),
        [
            ("api_latency_healthy_control", _CONTROL),
            ("workflow_stuck_paused_dag", _PAUSED),
        ],
    )
    def test_with_the_rule_the_reads_are_forced_and_every_dimension_passes(
        self, name: str, script: list[dict[str, Any]]
    ) -> None:
        grades = _grade(_rescripted(name, script))
        assert all(grades.values()), grades


class TestTheStepLimitIsAVerdictToo:
    """O-44: "any workflow_stuck verdict, escalation included". Three of INC-006's live runs never
    concluded; they ran out of steps, and that escalation owes the same reads."""

    def test_running_out_of_steps_forces_the_owed_reads_then_escalates(
        self, run_state: RunState, now: datetime
    ) -> None:
        llm = CannedLLMClient(
            [
                _step("resolver_stall", 0.6, _probe("get_dag_state", job_id=_CHAIN)),
                _step("resolver_stall", 0.7, _probe("get_dag_state", job_id=_CHAIN)),
                _step("resolver_stall", 0.75, _probe("list_dlq_messages")),
            ]
        )
        result = make_llm_investigate(
            _chain_world(),
            llm,
            model="m",
            max_iterations=2,
            required_before_verdict=_WHOLE_QUEUE,
        )(_investigating(run_state, _CHAIN_ALERT), now)

        assert result.state is IncidentState.ESCALATED
        assert _reads(result) == ["get_dag_state", "get_dag_state", "list_dlq_messages"]
        [refusal] = _refusals(result)
        assert refusal.arguments["refused"] == "escalation at the step limit"
        assert result.evidence[-1].result_summary.startswith("max iterations (2) exceeded")

    def test_without_a_declaration_the_step_limit_escalates_as_before(
        self, run_state: RunState, now: datetime
    ) -> None:
        llm = CannedLLMClient(
            [_step("resolver_stall", 0.6, _probe("get_dag_state", job_id=_CHAIN))] * 2
        )
        result = make_llm_investigate(_chain_world(), llm, model="m", max_iterations=2)(
            _investigating(run_state, _CHAIN_ALERT), now
        )
        assert _refusals(result) == []
        assert _reads(result) == ["get_dag_state", "get_dag_state"]
