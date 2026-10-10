"""WO-R3-372 (O-49, ADR 00XX): a stuck chain's resolver is read before any verdict, and repaired.

INC-008: `workflow_stuck_resolver_stall`'s label was a coin flip against `saga_coordinator_stall`
on identical evidence, because nothing the agent could read separated them. Platform v0.6.24 made
the resolver and the background loops readable; this packet makes the family READ them before a
verdict (ADR 0078's mechanism, a read pinned to one consumer group), routes `resolver_stall` to
`restart_consumer_group`, lets a chain alert's action name that one group once the run has read
it, and turns the scenario from escalate-only into a repair.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Final
from uuid import UUID

import pytest
from pydantic import ValidationError

from evals import runner
from evals.dossier import derive_probes
from evals.fakes import CannedMCPClient
from evals.graders.deterministic import GradeDimension
from evals.recorder import recording_probes
from evals.scenarios.loader import load_scenarios
from evals.scenarios.schema import FAMILY_REQUIRED_BEFORE_VERDICT, Scenario, ScenarioFamily
from incident_commander.agent.hypothesis import Hypothesis, HypothesisCategory
from incident_commander.agent.investigation import (
    FIX_MAP,
    VERDICT_REFUSED_MARKER,
    alert_subject,
    make_llm_investigate,
    required_read_made,
)
from incident_commander.agent.remediation import (
    CHAIN_SERVICES_FOR_SUBJECT,
    RemediationPlan,
    _unaddressed_alert_subject,
    make_llm_plan,
)
from incident_commander.agent.required_reads import (
    CHAIN_RESOLVER_GROUP,
    RequiredReading,
    VerdictCondition,
)
from incident_commander.agent.state import BudgetLedger, EvidenceEntry, IncidentState, RunState
from incident_commander.llm.fakes import CannedLLMClient
from incident_commander.tools.registry import TOOL_REGISTRY

_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
_CORPUS: Final[dict[str, Scenario]] = {
    s.name: s for s in load_scenarios(_ROOT / "evals" / "scenarios")
}
_NAME: Final[str] = "workflow_stuck_resolver_stall"
_CHAIN: Final[str] = "4a30546f-d3c5-549f-a772-633c0b26219d"
_CHAIN_ALERT: Final[dict[str, Any]] = {
    "source": "platform.dag",
    "severity": "critical",
    "fingerprint": "workflow_not_advancing",
    "job_id": _CHAIN,
}
_RESOLVER_READ: Final[str] = f"get_consumer_lag(consumer_group={CHAIN_RESOLVER_GROUP})"
_SWEEP: Final[tuple[RequiredReading, ...]] = FAMILY_REQUIRED_BEFORE_VERDICT[
    ScenarioFamily.WORKFLOW_STUCK
]


def _now() -> datetime:
    return datetime(2026, 10, 10, 16, 0, tzinfo=UTC)


def _step(
    category: str, confidence: float, action: dict[str, Any], name: str | None = None
) -> dict[str, Any]:
    return {
        "hypotheses": [
            {
                "category": category,
                "name": name or category,
                "confidence": confidence,
                "reasoning": "r",
            }
        ],
        "next_action": action,
    }


def _probe(tool: str, **arguments: Any) -> dict[str, Any]:
    return {"kind": "probe", "tool_name": tool, "arguments": arguments}


def _stop(reason: str = "done") -> dict[str, Any]:
    return {"kind": "stop", "reason": reason}


def _remediate(reason: str = "act") -> dict[str, Any]:
    return {"kind": "remediate", "reason": reason}


def _run(*evidence: EvidenceEntry, state: IncidentState = IncidentState.PLANNING) -> RunState:
    return RunState(
        incident_id=UUID("22222222-2222-2222-2222-222222222222"),
        state=state,
        alert=dict(_CHAIN_ALERT),
        budget=BudgetLedger(
            max_tool_calls=25,
            max_tokens=200_000,
            max_wall_seconds=600,
            max_usd=Decimal("1.00"),
        ),
        hypotheses=(
            Hypothesis(
                category=HypothesisCategory.RESOLVER_STALL,
                name="nothing is promoting the waiting descendant",
                confidence=0.9,
                reasoning="the resolver stopped polling",
            ),
        ),
        evidence=evidence,
        created_at=_now(),
        updated_at=_now(),
    )


def _lag_reading(group: str, *, last_poll_age: int = 300) -> EvidenceEntry:
    return EvidenceEntry(
        tool_name="get_consumer_lag",
        arguments={"consumer_group": group},
        result_summary=json.dumps(
            {
                "consumer_group": group,
                "lag": 0,
                "lag_known": True,
                "source": "live",
                "age_seconds": 2,
                "last_poll_age_seconds": last_poll_age,
            }
        ),
        timestamp=_now(),
    )


def _restart(group: str) -> RemediationPlan:
    return RemediationPlan(
        target_hypothesis="nothing is promoting the waiting descendant",
        action_tool="restart_consumer_group",
        action_arguments={"consumer_group": group},
        verify_tool="get_consumer_lag",
        verify_arguments={"consumer_group": group},
        verify_expectation="last_poll_age_seconds falls back to within a few seconds",
    )


def _rescripted(script: list[dict[str, Any]], **update: Any) -> Scenario:
    scenario = _CORPUS[_NAME]
    responses = {**scenario.canned_llm_responses, "investigation_planner": script}
    return scenario.model_copy(update={"canned_llm_responses": responses, **update})


def _result(scenario: Scenario) -> runner.ScenarioResult:
    return runner.run_scenario(scenario, runner._eval_defaults())


def _final(result: runner.ScenarioResult) -> RunState:
    return result.trajectory.checkpoints[-1]


def _grades(result: runner.ScenarioResult) -> dict[GradeDimension, bool]:
    return {d.dimension: d.passed for d in result.outcome.report.dimensions}


def _refusals(run_state: RunState) -> list[EvidenceEntry]:
    return [e for e in run_state.evidence if e.tool_name == VERDICT_REFUSED_MARKER]


# The coin flip INC-008 recorded: chain, queue, then the coordinator's label on the same reads
# that once produced the right one.
_COIN_FLIP: Final[list[dict[str, Any]]] = [
    _step("runaway_saga", 0.5, _probe("get_dag_state", job_id=_CHAIN)),
    _step("resolver_stall", 0.55, _probe("list_dlq_messages")),
    _step("saga_coordinator_stall", 0.8, _stop("the coordinator stopped stepping the chain")),
]


class TestTheReadIsPinnedToTheResolver:
    def test_the_family_declares_the_resolver_by_name_and_the_loops(self) -> None:
        rendered = [reading.rendered() for reading in _SWEEP]
        assert rendered == ["list_dlq_messages", _RESOLVER_READ, "get_control_loops"]
        assert all(reading.when is VerdictCondition.STUCK_CHAIN for reading in _SWEEP)

    def test_every_world_of_the_family_declares_the_same_reads(self) -> None:
        """The declaration names no cause: it is identical in all five worlds (ADR 0078)."""
        family = [s for s in _CORPUS.values() if s.family is ScenarioFamily.WORKFLOW_STUCK]
        assert len(family) == 5
        assert all(s.required_before_verdict == _SWEEP for s in family)

    def test_a_read_of_another_group_does_not_count(self) -> None:
        resolver = _SWEEP[1]
        assert not required_read_made(_run(_lag_reading("worker-dispatcher")), resolver)
        assert required_read_made(_run(_lag_reading(CHAIN_RESOLVER_GROUP)), resolver)

    def test_a_pinned_argument_the_tool_does_not_take_is_refused_at_load(self) -> None:
        with pytest.raises(ValidationError, match="takes no argument named"):
            RequiredReading.model_validate(
                {"tool": "get_consumer_lag", "when": "stuck_chain", "arguments": {"group": "x"}}
            )

    def test_a_blank_pinned_value_is_refused_at_load(self) -> None:
        with pytest.raises(ValidationError, match="blank"):
            RequiredReading.model_validate(
                {
                    "tool": "get_consumer_lag",
                    "when": "stuck_chain",
                    "arguments": {"consumer_group": " "},
                }
            )

    def test_a_probe_of_the_wrong_group_does_not_pay_the_owed_read(
        self, run_state: RunState, now: datetime
    ) -> None:
        """The narrowed schema can only bind the TOOL; the group is checked by the loop."""
        llm = CannedLLMClient(
            [
                _step("runaway_saga", 0.5, _probe("get_dag_state", job_id=_CHAIN)),
                _step("saga_coordinator_stall", 0.8, _stop()),
                _step("saga_coordinator_stall", 0.8, _probe("list_dlq_messages")),
                _step("saga_coordinator_stall", 0.8, _probe("get_consumer_lag")),
                _step("saga_coordinator_stall", 0.8, _probe("get_control_loops")),
            ]
        )
        result = make_llm_investigate(
            CannedMCPClient(_CORPUS[_NAME].canned_tool_responses),
            llm,
            model="m",
            required_before_verdict=_SWEEP,
        )(
            run_state.model_copy(
                update={"state": IncidentState.INVESTIGATING, "alert": _CHAIN_ALERT}
            ),
            now,
        )

        refusals = _refusals(result)
        assert refusals[0].arguments["missing_reads"] == [
            "list_dlq_messages",
            _RESOLVER_READ,
            "get_control_loops",
        ]
        # The unpinned `get_consumer_lag` call (the dispatcher, by default) was refused before it
        # went out, naming the group the read is pinned to.
        assert refusals[1].arguments["refused"] == "probe of get_consumer_lag"
        assert "consumer_group='dependency-resolver'" in refusals[1].result_summary


class TestTheChainAlertMayNameItsResolver:
    def test_a_restart_of_the_resolver_the_run_has_read_is_admitted(self) -> None:
        plan = _restart(CHAIN_RESOLVER_GROUP)
        assert _unaddressed_alert_subject(plan, _run(_lag_reading(CHAIN_RESOLVER_GROUP))) is None

    def test_a_restart_nobody_read_first_is_refused_and_told_the_route(self) -> None:
        miss = _unaddressed_alert_subject(_restart(CHAIN_RESOLVER_GROUP), _run())
        assert miss is not None
        assert "restart_consumer_group(consumer_group='dependency-resolver')" in miss.reason
        assert "once this run has read it" in miss.reason

    @pytest.mark.parametrize("group", ["worker-dispatcher", "saga-coordinator"])
    def test_another_group_is_still_refused_even_when_read(self, group: str) -> None:
        """The admission is ONE named service, not "any consumer group the run read"."""
        miss = _unaddressed_alert_subject(_restart(group), _run(_lag_reading(group)))
        assert miss is not None

    def test_the_admission_is_inert_for_an_alert_about_a_consumer_group(self) -> None:
        """Keyed on the subject's read, so a lag alert on the dispatcher gains nothing."""
        run = _run(_lag_reading(CHAIN_RESOLVER_GROUP)).model_copy(
            update={"alert": {"source": "platform.kafka", "consumer_group": "worker-dispatcher"}}
        )
        assert _unaddressed_alert_subject(_restart(CHAIN_RESOLVER_GROUP), run) is not None

    def test_the_plan_reaches_remediating_with_the_resolvers_declared_name(self) -> None:
        """The rehearsal's find (archive d70a7429f182): the resolver's name reaches a run only as
        its own read's echo, which B-08 refuses as a source, so the restart was refused as
        "unsourced". The declared name, once read, is a source; a model's retyping is not."""
        plan = _restart(CHAIN_RESOLVER_GROUP).model_dump(mode="json")
        result = make_llm_plan(CannedLLMClient([plan]), model="m")(
            _run(_lag_reading(CHAIN_RESOLVER_GROUP)), _now()
        )
        assert result.state is IncidentState.REMEDIATING
        assert not [e for e in result.evidence if e.tool_name.startswith("_plan_refused")]

    def test_a_retyped_name_is_still_unsourced(self) -> None:
        typo = _restart("dependency_resolver").model_dump(mode="json")
        result = make_llm_plan(CannedLLMClient([typo, typo]), model="m")(
            _run(_lag_reading(CHAIN_RESOLVER_GROUP)), _now()
        )
        assert result.state is IncidentState.ESCALATED
        refused = [e for e in result.evidence if e.tool_name == "_plan_refused_argument"]
        assert refused and refused[0].arguments["kind"] == "unsourced"

    def test_the_service_fields_are_the_tools_own_input_schema(self) -> None:
        for subject_tool, services in CHAIN_SERVICES_FOR_SUBJECT.items():
            subject = alert_subject(_CHAIN_ALERT)
            assert subject is not None and subject.tool_name == subject_tool
            for service in services:
                assert (
                    service.action_field
                    in TOOL_REGISTRY[service.action_tool].input_model.model_fields
                )
                assert (
                    service.read_field in TOOL_REGISTRY[service.read_tool].input_model.model_fields
                )

    def test_the_category_routes_at_the_restart(self) -> None:
        assert FIX_MAP[HypothesisCategory.RESOLVER_STALL] == "restart_consumer_group"
        assert HypothesisCategory.SAGA_COORDINATOR_STALL not in FIX_MAP


class TestRedBeforeGreenOnTheCannedWorld:
    """The packet's acceptance, on the canned world (brief step 4)."""

    def test_without_the_reads_the_coin_flip_concludes_and_the_repair_is_never_reached(
        self,
    ) -> None:
        """RED: the loop as it was before this packet — nothing owed beyond the queue — lets the
        coordinator's label through on the chain and queue alone, so no action is ever taken."""
        result = _result(_rescripted(_COIN_FLIP, required_before_verdict=_SWEEP[:1]))
        assert _final(result).state is IncidentState.ESCALATED
        assert _refusals(_final(result)) == []
        grades = _grades(result)
        assert not grades[GradeDimension.OUTCOME]
        assert not grades[GradeDimension.ROOT_CAUSE]

    def test_the_coin_flip_is_refused_until_the_resolver_and_the_loops_are_read(self) -> None:
        """The same planner, refused: a verdict naming a stalled component without the reads."""
        script = [*_COIN_FLIP, *[_step("saga_coordinator_stall", 0.8, _stop())] * 2]
        result = _result(_rescripted(script))
        [first, *_] = _refusals(_final(result))
        assert first.arguments["missing_reads"] == [_RESOLVER_READ, "get_control_loops"]
        assert _final(result).state is IncidentState.ESCALATED
        assert "without making the reads this alert requires" in (
            _final(result).evidence[-1].result_summary
        )

    def test_reading_them_turns_the_coin_flip_into_the_repair(self) -> None:
        """GREEN: refused once, the planner reads the resolver (stopped) and the loops (sweep
        paused), re-ranks, and the run restarts the resolver, verifies it and resolves."""
        script = [
            *_COIN_FLIP,
            _step(
                "saga_coordinator_stall",
                0.6,
                _probe("get_consumer_lag", consumer_group=CHAIN_RESOLVER_GROUP),
            ),
            _step(
                "resolver_stall",
                0.8,
                _probe("get_control_loops"),
                name="nothing is promoting the waiting descendant",
            ),
            _step(
                "resolver_stall",
                0.9,
                _remediate(),
                name="nothing is promoting the waiting descendant",
            ),
        ]
        result = _result(_rescripted(script))
        assert len(_refusals(_final(result))) == 1
        assert _final(result).state is IncidentState.RESOLVED
        grades = _grades(result)
        assert all(grades.values()), grades

    def test_the_scenarios_own_script_reads_restarts_verifies_and_resolves(self) -> None:
        result = _result(_CORPUS[_NAME])
        assert _final(result).state is IncidentState.RESOLVED
        tools = [e.tool_name for e in _final(result).evidence if not e.tool_name.startswith("_")]
        assert tools.count("restart_consumer_group") == 1
        assert tools.index("get_control_loops") < tools.index("restart_consumer_group")
        grades = _grades(result)
        assert all(grades.values()), grades


class TestTheRecordingAndTheDossierMakeTheReads:
    @pytest.mark.parametrize(
        "name", sorted(n for n, s in _CORPUS.items() if s.family is ScenarioFamily.WORKFLOW_STUCK)
    )
    def test_every_family_world_records_the_resolver_and_the_loops(self, name: str) -> None:
        """The value pool takes the pinned group from the declaration, so a world that never
        mentions the resolver anywhere else still records the read its verdict owes."""
        probes, _notes = recording_probes(_CORPUS[name])
        calls = {(p.tool, tuple(sorted(dict(p.arguments).items()))) for p in probes}
        assert ("get_consumer_lag", (("consumer_group", CHAIN_RESOLVER_GROUP),)) in calls
        assert ("get_control_loops", ()) in calls

    def test_the_dossier_derives_the_required_reads(self) -> None:
        probes, _notes = derive_probes(_CORPUS["workflow_stuck_paused_dag"])
        required = [p for p in probes if any("required_before_verdict" in o for o in p.origins)]
        assert [(p.tool, dict(p.arguments)) for p in required] == [
            ("list_dlq_messages", {}),
            ("get_consumer_lag", {"consumer_group": CHAIN_RESOLVER_GROUP}),
            ("get_control_loops", {}),
        ]
