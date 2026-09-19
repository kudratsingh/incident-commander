"""WP-6.2 — the ``candidate_selector`` arm and the oracle gap.

One class per acceptance item: the generator seam (either best-of-N generator, ``baseline``
refused, all three parts of the arm identity stamped), select emits ONLY the selected
candidate, probe_more runs through the real loop, selection is not authorization, the
selector is its own metered role, and ``oracle_gap@k = pass@k − selected@k`` per world.
"""

from __future__ import annotations

import ast
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Final
from uuid import uuid4

import pytest
from pydantic import ValidationError

from evals import research_report
from evals.candidate_metrics import (
    OracleGapAcrossWorlds,
    WorldKey,
    measure_selection,
    oracle_gap_at_k,
    pass_at_k,
    selected_at_k,
    steps_of,
    world_key,
)
from evals.graders.deterministic import GradeReport
from evals.runner import ExecutionMode, RunProvenance, ScenarioOutcome
from incident_commander.agent.candidates import DiagnosisCandidate, grounded_in
from incident_commander.agent.hypothesis import (
    HypothesisCategory,
    ProbeAction,
    StopAction,
)
from incident_commander.agent.investigation import make_llm_investigate
from incident_commander.agent.selection import SELECTOR_ROLE
from incident_commander.agent.state import (
    BudgetLedger,
    EvidenceEntry,
    IncidentState,
    RunState,
)
from incident_commander.agent.strategies.candidate_selector import (
    NO_SELECTOR_CLIENT,
    PROBE_MORE_WITHOUT_A_PROBE,
    CandidateSelectorStrategy,
    SelectorFailed,
)
from incident_commander.agent.strategies.knobs import StrategyKnobs
from incident_commander.agent.strategies.names import StrategyName
from incident_commander.agent.strategies.protocol import StrategyContext
from incident_commander.agent.strategies.records import StepRecord
from incident_commander.agent.strategies.registry import STRATEGIES
from incident_commander.config import ModelRole, Settings
from incident_commander.llm.client import LLMClientProtocol
from incident_commander.llm.fakes import CannedLLMClient, CannedUsage
from incident_commander.tools.mcp_client import ToolResult

_AT: Final[datetime] = datetime(2026, 7, 15, 20, 0, tzinfo=UTC)

#: The correct root cause for the hand-built fixtures below.
_TRUTH: Final[tuple[HypothesisCategory, ...]] = (HypothesisCategory.CONSUMER_SATURATION,)


# --------------------------------------------------------------------------
# Builders. Local, so this file can fail on its own.


class _FakeMCPClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call_tool(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        timeout_seconds: float | None = None,
    ) -> ToolResult:
        self.calls.append((name, dict(arguments)))
        return ToolResult(
            content=[
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "consumer_group": "billing",
                            "lag": 42,
                            "lag_known": True,
                            "source": "static",
                        }
                    ),
                }
            ]
        )


def _state(*, evidence: tuple[EvidenceEntry, ...] = ()) -> RunState:
    return RunState(
        incident_id=uuid4(),
        state=IncidentState.INVESTIGATING,
        alert={"source": "kafka", "severity": "high", "group": "billing"},
        budget=BudgetLedger(
            max_tool_calls=25,
            max_tokens=200_000,
            max_wall_seconds=1_800,
            max_usd=Decimal("5.00"),
        ),
        evidence=evidence,
        created_at=_AT,
        updated_at=_AT,
    )


def _candidate_payload(
    candidate_id: str,
    *,
    category: str = "consumer_saturation",
    confidence: float = 0.9,
    probe: str | None = "get_consumer_lag",
) -> dict[str, Any]:
    return {
        "candidate_id": candidate_id,
        "category": category,
        "name": f"{candidate_id} label",
        "confidence": confidence,
        "evidence_for": [],
        "evidence_against": [],
        "next_probe": (
            None
            if probe is None
            else {"tool_name": probe, "arguments": {"consumer_group": "billing"}}
        ),
    }


def _generator_payload(
    *candidates: dict[str, Any], action: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "candidates": list(candidates),
        "next_action": action or {"kind": "stop", "reason": "confidence sufficient"},
    }


def _selection_payload(
    *,
    decision: str = "select",
    selected: str | None = "c1",
    scores: dict[str, float] | None = None,
    uncertainty: float = 0.2,
) -> dict[str, Any]:
    return {
        "decision": decision,
        "selected_candidate_id": selected,
        "scores": {"c1": 0.9, "c2": 0.3} if scores is None else scores,
        "uncertainty": uncertainty,
        "reasoning": "the lag reading supports c1.",
    }


def _arm(
    *, n: int = 2, generator: str = StrategyName.BEST_OF_N_ENUMERATED.value
) -> CandidateSelectorStrategy:
    return CandidateSelectorStrategy(StrategyKnobs(n=n, selector_generator=generator))


def _context(
    planner: LLMClientProtocol,
    selector: LLMClientProtocol | None,
    *,
    sink: list[StepRecord] | None = None,
) -> StrategyContext:
    return StrategyContext(
        llm_client=planner,
        model="m",
        iteration=0,
        record_step=None if sink is None else sink.append,
        selector_llm_client=selector,
    )


def _settings(**overrides: Any) -> Settings:
    """``Settings`` built from explicit placeholders, isolated from the machine.

    Every required field is supplied and ``_env_file=None`` turns dotenv off, so this reads
    nothing from the developer's environment (WO-R3-247). None of these is a credential.
    """
    base: dict[str, Any] = {
        "anthropic_api_key": "sk-ant-test",
        "judge_model": "claude-haiku-4-5",
        "platform_mcp_url": "https://mcp.platform.local",
        "platform_rest_url": "https://api.platform.local",
        "platform_token": "svc-token",
        "platform_webhook_secret": "s" * 32,
        "database_url": "postgresql://u:p@localhost/db",
        **overrides,
    }
    # ``_env_file=None`` disables dotenv; it does not disable exported shell
    # variables, which the explicit values above already outrank.
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


# --------------------------------------------------------------------------
# A hand-built step record, not a run: what a record SAYS.


def _record_with(
    *,
    categories: tuple[str, ...],
    selected: str | None,
    decision: str = "select",
    uncertainty: float = 0.3,
) -> dict[str, Any]:
    return {
        "kind": "step",
        "iteration": 0,
        "strategy": StrategyName.CANDIDATE_SELECTOR.value,
        "candidate_set": [
            {"candidate_id": f"c{index}", "category": category, "name": f"c{index}"}
            for index, category in enumerate(categories, start=1)
        ],
        "selector": {
            "selected_candidate_id": selected,
            "scores": {f"c{index}": 0.5 for index in range(1, len(categories) + 1)},
            "uncertainty": uncertainty,
            "decision": decision,
            "call_id": "abc",
        },
        "emitted_step": {
            "hypotheses": [],
            "next_action": {"kind": "stop", "reason": "done"},
        },
        "generation_rejections": [],
    }


def _outcome(*, mode: ExecutionMode, replay: dict[str, Any] | None = None) -> ScenarioOutcome:
    """A minimal ``ScenarioOutcome`` for the world-key tests.

    Only three fields are read: the execution mode and the ``replay`` row.
    """
    return ScenarioOutcome(
        scenario="s",
        final_state=IncidentState.ESCALATED,
        tool_calls_used=0,
        report=GradeReport(scenario="s", passed=True, dimensions=()),
        provenance=RunProvenance(
            commander_revision="rev",
            platform_image_digest="sha256:0",
            agent_model="m",
            model_role=ModelRole.DEVELOPMENT,
            judge_model="j",
            strategy=StrategyName.CANDIDATE_SELECTOR.value,
            strategy_config={},
            scenario="s",
            invocation_id="inv",
            recorded_at=_AT,
            execution_mode=mode,
            budget=BudgetLedger(
                max_tool_calls=25,
                max_tokens=1,
                max_wall_seconds=1,
                max_usd=Decimal("1"),
            ),
        ),
        replay=replay,
    )


_CANNED_WORLD: Final[WorldKey] = WorldKey(
    scenario="remediate_consumer_lag_success", mode="canned", instance="fixtures"
)


# --------------------------------------------------------------------------


class TestTheGeneratorSeam:
    def test_the_arm_is_in_the_registry_under_its_name(self) -> None:
        arm = STRATEGIES.create(StrategyName.CANDIDATE_SELECTOR.value, StrategyKnobs(n=2))
        assert arm.name == "candidate_selector"

    @pytest.mark.parametrize(
        "generator",
        [StrategyName.BEST_OF_N_ENUMERATED.value, StrategyName.BEST_OF_N_SAMPLED.value],
    )
    def test_either_generator_supplies_the_set(self, generator: str) -> None:
        assert _arm(generator=generator).generator.name == generator

    def test_baseline_is_refused_as_a_generator(self) -> None:
        """A selector over a one-candidate set is a billed call with one answer.

        ``baseline`` has no ``generate``, so the arm refuses it, not the registry.
        """
        with pytest.raises(ValueError, match="cannot supply a candidate set"):
            _arm(generator=StrategyName.BASELINE.value)

    def test_the_config_stamps_all_three_parts_of_the_arm(self) -> None:
        """Plan 02 § 12: the arm is (generator, N, selector).

        A report keyed on the strategy name alone would average two gaps.
        """
        config = _arm(n=4, generator=StrategyName.BEST_OF_N_SAMPLED.value).config
        assert config["generator"] == "best_of_n_sampled"
        assert config["selector"] == SELECTOR_ROLE
        assert config["n"] == 4
        # The generator's own knobs are folded in rather than restated, so the row
        # carries the temperature that actually ran.
        assert "sample_temperature" in config

    def test_the_configured_default_generator_is_the_cheap_one(self) -> None:
        assert _settings().selector_generator is StrategyName.BEST_OF_N_ENUMERATED
        assert StrategyKnobs().selector_generator == "best_of_n_enumerated"

    def test_the_settings_value_reaches_the_arm(self) -> None:
        from evals.runner import strategy_knobs

        knobs = strategy_knobs(_settings(best_of_n=2, selector_generator="best_of_n_sampled"))
        assert knobs.selector_generator == "best_of_n_sampled"
        assert STRATEGIES.create("candidate_selector", knobs).config["generator"] == (
            "best_of_n_sampled"
        )


class TestSelectEmitsTheSelectedCandidate:
    def test_the_emitted_step_is_built_from_the_selection(self) -> None:
        planner = CannedLLMClient(
            [
                _generator_payload(
                    _candidate_payload("c1", confidence=0.4),
                    _candidate_payload("c2", category="transient_dependency", confidence=0.95),
                )
            ]
        )
        selector = CannedLLMClient([_selection_payload(selected="c1")])
        _, step, record = _arm().plan_next_step(_state(), _AT, _context(planner, selector))

        assert [h.name for h in step.hypotheses] == ["c1 label"]
        assert step.hypotheses[0].category is HypothesisCategory.CONSUMER_SATURATION
        # The whole set is still on the record — the alternatives are research
        # data, and pass@k is computed from them.
        assert [c.candidate_id for c in record.candidate_set] == ["c2", "c1"]

    def test_a_more_confident_unselected_candidate_cannot_reach_index_zero(self) -> None:
        """The reason the step carries one hypothesis and not the set.

        ``InvestigationStep._rank_by_confidence`` re-sorts at the schema boundary and three
        gates read index 0, so emitting the set would gate the run on a rejected diagnosis.
        """
        planner = CannedLLMClient(
            [
                _generator_payload(
                    _candidate_payload("c1", confidence=0.4),
                    _candidate_payload("c2", category="transient_dependency", confidence=0.95),
                )
            ]
        )
        selector = CannedLLMClient([_selection_payload(selected="c1")])
        _, step, _ = _arm().plan_next_step(_state(), _AT, _context(planner, selector))
        assert len(step.hypotheses) == 1
        assert step.hypotheses[0].confidence == 0.4

    def test_the_selectors_own_words_are_the_hypothesis_reasoning(self) -> None:
        """Derived, so it cannot be mistaken for model prose about the incident.

        ``Hypothesis.reasoning`` is required and ``DiagnosisCandidate`` has none
        (ADR 0042), so every part of this string is something the selector said.
        """
        planner = CannedLLMClient([_generator_payload(_candidate_payload("c1"))])
        selector = CannedLLMClient([_selection_payload(scores={"c1": 0.77})])
        _, step, _ = _arm(n=1).plan_next_step(_state(), _AT, _context(planner, selector))
        reasoning = step.hypotheses[0].reasoning
        assert "candidate c1" in reasoning
        assert "select" in reasoning
        assert "score 0.77" in reasoning
        assert "the lag reading supports c1." in reasoning

    def test_the_action_on_select_is_the_generators_own(self) -> None:
        """The selector chooses a diagnosis, never an action (plan 02 § 18)."""
        planner = CannedLLMClient(
            [
                _generator_payload(
                    _candidate_payload("c1"),
                    action={"kind": "remediate", "reason": "fix it"},
                )
            ]
        )
        selector = CannedLLMClient([_selection_payload(scores={"c1": 0.9})])
        _, step, _ = _arm(n=1).plan_next_step(_state(), _AT, _context(planner, selector))
        assert step.next_action.kind == "remediate"


class TestProbeMoreEmitsTheSelectedCandidatesProbe:
    def test_the_probe_is_the_chosen_candidates_next_probe(self) -> None:
        planner = CannedLLMClient(
            [
                _generator_payload(
                    _candidate_payload("c1", probe="get_consumer_lag"),
                    _candidate_payload(
                        "c2",
                        category="transient_dependency",
                        confidence=0.5,
                        probe="get_redis_health",
                    ),
                )
            ]
        )
        # probe_more names no candidate, so the highest SCORE decides (ADR 0048).
        selector = CannedLLMClient(
            [
                _selection_payload(
                    decision="probe_more", selected=None, scores={"c1": 0.3, "c2": 0.8}
                )
            ]
        )
        _, step, _ = _arm().plan_next_step(_state(), _AT, _context(planner, selector))
        assert isinstance(step.next_action, ProbeAction)
        assert step.next_action.tool_name == "get_redis_health"

    def test_the_probe_goes_through_the_loops_tier_re_check(self) -> None:
        """Exercised through the real loop, not asserted about.

        ``_execute_probe`` re-checks the tier at run time (B-06).
        """
        planner = CannedLLMClient(
            [
                _generator_payload(_candidate_payload("c1", probe="get_consumer_lag")),
                _generator_payload(
                    _candidate_payload("c1", probe="get_consumer_lag"),
                    action={"kind": "stop", "reason": "enough"},
                ),
            ]
        )
        selector = CannedLLMClient(
            [
                _selection_payload(decision="probe_more", selected=None, scores={"c1": 0.8}),
                _selection_payload(scores={"c1": 0.8}),
            ]
        )
        mcp = _FakeMCPClient()
        result = make_llm_investigate(
            mcp,
            planner,
            model="m",
            strategy=_arm(n=1),
            selector_llm_client=selector,
        )(_state(), _AT)
        assert mcp.calls == [("get_consumer_lag", {"consumer_group": "billing"})]
        assert result.state in {IncidentState.ESCALATED, IncidentState.INVESTIGATING}

    def test_a_non_read_next_probe_is_refused_at_the_schema(self) -> None:
        """Red-before, and the refusal is structural rather than a check.

        ``ProbeAction.tool_name`` is a ``ReadToolName`` literal, so a Tier-1 next probe fails
        at the generator's schema; ``_execute_probe``'s ``tier_of`` is the second layer.
        """
        with pytest.raises(ValidationError), grounded_in(()):
            DiagnosisCandidate.model_validate(
                _candidate_payload("c1", probe="restart_consumer_group")
            )

    def test_probe_more_on_a_candidate_with_no_probe_stops_instead(self) -> None:
        """Fail-safe: nothing fabricated, nothing overruled, no crash.

        The candidate names no way to get the evidence, so the loop stops through its existing
        terminal path rather than substituting the generator's own step.
        """
        planner = CannedLLMClient([_generator_payload(_candidate_payload("c1", probe=None))])
        selector = CannedLLMClient(
            [_selection_payload(decision="probe_more", selected=None, scores={"c1": 0.8})]
        )
        _, step, _ = _arm(n=1).plan_next_step(_state(), _AT, _context(planner, selector))
        assert isinstance(step.next_action, StopAction)
        assert PROBE_MORE_WITHOUT_A_PROBE in step.next_action.reason


class TestEscalateReachesStopThroughTheExistingPath:
    def test_escalate_emits_a_stop_action(self) -> None:
        planner = CannedLLMClient([_generator_payload(_candidate_payload("c1"))])
        selector = CannedLLMClient(
            [_selection_payload(decision="escalate", selected=None, scores={"c1": 0.2})]
        )
        _, step, _ = _arm(n=1).plan_next_step(_state(), _AT, _context(planner, selector))
        assert isinstance(step.next_action, StopAction)
        assert "selector escalated" in step.next_action.reason

    def test_the_run_reaches_escalated_through_the_loop(self) -> None:
        """No new terminal path: ``_finalize`` is the one ``baseline`` reaches."""
        planner = CannedLLMClient([_generator_payload(_candidate_payload("c1"))])
        selector = CannedLLMClient(
            [_selection_payload(decision="escalate", selected=None, scores={"c1": 0.2})]
        )
        result = make_llm_investigate(
            _FakeMCPClient(),
            planner,
            model="m",
            strategy=_arm(n=1),
            selector_llm_client=selector,
        )(_state(), _AT)
        assert result.state is IncidentState.ESCALATED
        assert "selector escalated" in result.evidence[-1].result_summary


class TestSelectionIsNotAuthorization:
    def test_a_selected_candidate_below_the_threshold_still_escalates(self) -> None:
        """Plan 02 § 18, through the real loop.

        The selector picks a fixable category and the 0.7 threshold refuses it anyway: the
        gate is in the loop.
        """
        planner = CannedLLMClient(
            [
                _generator_payload(
                    _candidate_payload("c1", confidence=0.5),
                    action={"kind": "remediate", "reason": "fix it"},
                )
            ]
        )
        selector = CannedLLMClient([_selection_payload(scores={"c1": 0.99})])
        result = make_llm_investigate(
            _FakeMCPClient(),
            planner,
            model="m",
            strategy=_arm(n=1),
            selector_llm_client=selector,
        )(_state(), _AT)
        assert result.state is IncidentState.ESCALATED
        assert "below threshold" in result.evidence[-1].result_summary

    def test_a_selected_category_outside_fix_map_still_escalates(self) -> None:
        planner = CannedLLMClient(
            [
                _generator_payload(
                    _candidate_payload("c1", category="transient_dependency", confidence=0.95),
                    action={"kind": "remediate", "reason": "fix it"},
                )
            ]
        )
        selector = CannedLLMClient([_selection_payload(scores={"c1": 0.99})])
        result = make_llm_investigate(
            _FakeMCPClient(),
            planner,
            model="m",
            strategy=_arm(n=1),
            selector_llm_client=selector,
        )(_state(), _AT)
        assert result.state is IncidentState.ESCALATED

    def test_the_strategy_holds_no_execution_policy(self) -> None:
        """The same claim ``test_strategies.py`` makes about the seam, on this file.

        Scanned through the AST rather than the text, because the module docstring NAMES the
        gates it must not hold and a substring scan would read that as the violation.
        """
        source = (
            Path(__file__).resolve().parents[2]
            / "src/incident_commander/agent/strategies/candidate_selector.py"
        ).read_text(encoding="utf-8")
        tree = ast.parse(source)
        reachable: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                reachable.add(node.id)
            elif isinstance(node, ast.Attribute):
                reachable.add(node.attr)
            elif isinstance(node, ast.Import):
                reachable.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                reachable.add(node.module or "")
                reachable.update(alias.name for alias in node.names)
        for forbidden in ("FIX_MAP", "tier_of", "Tier", "TOOL_REGISTRY", "wire_arguments"):
            assert forbidden not in reachable, (
                f"{forbidden} reached the selector strategy. Strategies propose; the "
                "loop decides (ADR 0036, plan 02 § 18)."
            )
        for module in sorted(reachable):
            assert not module.startswith("incident_commander.tools"), (
                f"the selector strategy imports {module}; a strategy that can reach "
                "the tool surface can act, and a strategy only proposes."
            )


class TestTheSelectorBlockIsOnEveryStep:
    def test_every_step_of_this_arm_carries_one(self) -> None:
        sink: list[StepRecord] = []
        planner = CannedLLMClient([_generator_payload(_candidate_payload("c1"))])
        selector = CannedLLMClient([_selection_payload(scores={"c1": 0.9}, uncertainty=0.4)])
        _arm(n=1).plan_next_step(_state(), _AT, _context(planner, selector, sink=sink))

        assert len(sink) == 1
        block = sink[0].selector
        assert block is not None
        assert block.decision == "select"
        assert block.selected_candidate_id == "c1"
        assert block.uncertainty == 0.4
        assert block.scores == {"c1": 0.9}

    @pytest.mark.parametrize("decision", ["probe_more", "escalate"])
    def test_a_decision_that_commits_to_nothing_records_a_null_selection(
        self, decision: str
    ) -> None:
        """And it is ``None``, not ``""``. A record spelling the absence as an
        empty string would read as a candidate whose id is the empty string."""
        sink: list[StepRecord] = []
        planner = CannedLLMClient([_generator_payload(_candidate_payload("c1"))])
        selector = CannedLLMClient(
            [_selection_payload(decision=decision, selected=None, scores={"c1": 0.5})]
        )
        _arm(n=1).plan_next_step(_state(), _AT, _context(planner, selector, sink=sink))
        block = sink[0].selector
        assert block is not None
        assert block.selected_candidate_id is None
        assert block.decision == decision

    def test_baseline_and_both_generator_arms_record_no_selector(self) -> None:
        """The field is ``None`` for every arm that does not select."""
        sink: list[StepRecord] = []
        baseline_payload = {
            "hypotheses": [
                {
                    "category": "consumer_saturation",
                    "name": "c1",
                    "confidence": 0.9,
                    "reasoning": "lag",
                }
            ],
            "next_action": {"kind": "stop", "reason": "done"},
        }
        for name, payloads in (
            (StrategyName.BASELINE.value, [baseline_payload]),
            (
                StrategyName.BEST_OF_N_ENUMERATED.value,
                [_generator_payload(_candidate_payload("c1"))],
            ),
            (StrategyName.BEST_OF_N_SAMPLED.value, [baseline_payload]),
        ):
            sink.clear()
            STRATEGIES.create(name, StrategyKnobs(n=1)).plan_next_step(
                _state(), _AT, _context(CannedLLMClient(payloads), None, sink=sink)
            )
            assert sink[0].selector is None, name
            assert sink[0].as_trace_record()["selector"] is None, name

    def test_the_whole_record_is_the_generators_with_the_selector_added(self) -> None:
        """One record per step, and the generator's own measurements survive.

        Rebuilt from the generator's record: two assemblies are two definitions.
        """
        sink: list[StepRecord] = []
        planner = CannedLLMClient([_generator_payload(_candidate_payload("c1"))])
        selector = CannedLLMClient([_selection_payload(scores={"c1": 0.9})])
        _arm(n=1).plan_next_step(_state(), _AT, _context(planner, selector, sink=sink))

        record = sink[0]
        assert record.strategy == "candidate_selector"
        assert record.planner_context_chars is not None
        assert record.planner_context_chars > 0
        assert [call.role for call in record.llm_calls] == [
            "investigation_planner",
            SELECTOR_ROLE,
        ]


class TestTheSelectorIsItsOwnMeteredRole:
    def test_a_missing_selector_client_is_refused_rather_than_borrowed(self) -> None:
        """Falling back to the planner's client would report the selector's
        tokens under the planner's role, which is the one number this arm is
        compared on."""
        planner = CannedLLMClient([_generator_payload(_candidate_payload("c1"))])
        with pytest.raises(ValueError, match=NO_SELECTOR_CLIENT):
            _arm(n=1).plan_next_step(_state(), _AT, _context(planner, None))

    def test_the_selector_call_is_charged_to_the_run_ledger(self) -> None:
        planner = CannedLLMClient(
            [_generator_payload(_candidate_payload("c1"))],
            usage=CannedUsage(input_tokens=100, output_tokens=20),
        )
        selector = CannedLLMClient(
            [_selection_payload(scores={"c1": 0.9})],
            usage=CannedUsage(input_tokens=40, output_tokens=10),
        )
        updated, _, record = _arm(n=1).plan_next_step(_state(), _AT, _context(planner, selector))
        assert updated.budget.tokens_used == 170
        selector_call = next(call for call in record.llm_calls if call.role == SELECTOR_ROLE)
        # The selector's own delta only — the generation's charge stays on the
        # generator's record and is not counted twice by anyone summing.
        assert selector_call.tokens_used == 50

    def test_the_runner_meters_the_selector_under_its_own_role(self) -> None:
        """Asserted on the accounting, not on the wiring."""
        from incident_commander.agent.accounting import RunAccounting

        accounting = RunAccounting()
        metered = accounting.meter(
            CannedLLMClient(
                [_selection_payload(scores={"c1": 0.9})],
                usage=CannedUsage(input_tokens=40, output_tokens=10),
            ),
            SELECTOR_ROLE,
        )
        planner = CannedLLMClient([_generator_payload(_candidate_payload("c1"))])
        _arm(n=1).plan_next_step(_state(), _AT, _context(planner, metered))
        roles = {total.role for total in accounting.roles}
        assert SELECTOR_ROLE in roles
        selector_total = next(t for t in accounting.roles if t.role == SELECTOR_ROLE)
        assert selector_total.calls == 1
        # Charged to the ledger, unlike the briefing judge: the selector decides
        # the run's diagnosis, so it is the agent's own spend.
        assert selector_total.charged_to_ledger is True
        assert selector_total.tokens_used == 50

    def test_the_step_accounting_counts_the_selector_call(self) -> None:
        """``StepAccounting.selector_calls`` was on the record and always 0."""
        from incident_commander.agent.accounting import RunAccounting

        accounting = RunAccounting()
        planner = CannedLLMClient([_generator_payload(_candidate_payload("c1"))])
        selector = CannedLLMClient([_selection_payload(scores={"c1": 0.9})])
        _arm(n=1).plan_next_step(
            _state(),
            _AT,
            StrategyContext(
                llm_client=planner,
                model="m",
                iteration=0,
                record_step=accounting.step_sink(),
                selector_llm_client=selector,
            ),
        )
        assert accounting.selector_calls == 1

    def test_a_selector_failure_carries_the_generations_bill(self) -> None:
        """ADR 0045's trap, one layer up.

        The generation is paid for BEFORE the selector is asked, so a selector exception
        would charge it to nobody.
        """
        planner = CannedLLMClient(
            [_generator_payload(_candidate_payload("c1"))],
            usage=CannedUsage(input_tokens=100, output_tokens=20),
        )
        # An unresolvable score id fails validation twice: the call and its one
        # ADR-0035 repair.
        bad = _selection_payload(scores={"c1": 0.9, "ghost": 0.1})
        selector = CannedLLMClient([bad, bad], usage=CannedUsage(input_tokens=7))
        with pytest.raises(SelectorFailed) as caught:
            _arm(n=1).plan_next_step(_state(), _AT, _context(planner, selector))
        usage = caught.value.usage
        assert usage is not None
        # 120 — the generation in full. The rejected selector legs bill NOTHING (a bare
        # ``ValidationError`` carries no usage); without ``billed_usage`` nobody is charged.
        assert usage.input_tokens + usage.output_tokens == 120

    def test_a_selector_failure_escalates_the_run_with_the_bill_charged(self) -> None:
        planner = CannedLLMClient(
            [_generator_payload(_candidate_payload("c1"))],
            usage=CannedUsage(input_tokens=100, output_tokens=20),
        )
        bad = _selection_payload(scores={"c1": 0.9, "ghost": 0.1})
        selector = CannedLLMClient([bad, bad], usage=CannedUsage(input_tokens=7))
        result = make_llm_investigate(
            _FakeMCPClient(),
            planner,
            model="m",
            strategy=_arm(n=1),
            selector_llm_client=selector,
        )(_state(), _AT)
        assert result.state is IncidentState.ESCALATED
        # The generation's 120, charged once, by the loop's own accrual — see the
        # test above on why the canned selector legs add nothing.
        assert result.budget.tokens_used == 120


class TestSelectedAtKAndTheOracleGap:
    def test_the_gap_is_one_when_a_correct_candidate_is_present_and_not_selected(
        self,
    ) -> None:
        """The order's fixture, exactly: present and not selected.

        ``c1`` is correct and the selector took ``c2``: pass@2 hits, selected@2 misses.
        """
        steps = steps_of(
            [_record_with(categories=("consumer_saturation", "stale_cache"), selected="c2")]
        )
        available = pass_at_k(steps, _TRUTH, 2)
        committed = selected_at_k(steps, _TRUTH, 2)
        gap = oracle_gap_at_k(steps, _TRUTH, 2, world=_CANNED_WORLD)

        assert available.hit is True
        assert committed.hit is False
        assert gap.gap == 1
        assert gap.gap == int(available.hit) - int(committed.hit)

    def test_the_gap_is_zero_when_the_selector_took_the_correct_candidate(self) -> None:
        steps = steps_of(
            [_record_with(categories=("consumer_saturation", "stale_cache"), selected="c1")]
        )
        gap = oracle_gap_at_k(steps, _TRUTH, 2, world=_CANNED_WORLD)
        assert (gap.pass_hit, gap.selected_hit, gap.gap) == (True, True, 0)

    def test_the_gap_is_zero_when_generation_missed_too(self) -> None:
        """A gap of 0 is not "selection is fine": it is "there was nothing to take"."""
        steps = steps_of([_record_with(categories=("stale_cache",), selected="c1")])
        gap = oracle_gap_at_k(steps, _TRUTH, 1, world=_CANNED_WORLD)
        assert (gap.pass_hit, gap.selected_hit, gap.gap) == (False, False, 0)

    @pytest.mark.parametrize("decision", ["probe_more", "escalate"])
    def test_committing_to_nothing_is_a_miss_and_not_an_abstention(self, decision: str) -> None:
        """Otherwise the selector looks best exactly when it decides least."""
        steps = steps_of(
            [_record_with(categories=("consumer_saturation",), selected=None, decision=decision)]
        )
        assert selected_at_k(steps, _TRUTH, 1).hit is False
        assert oracle_gap_at_k(steps, _TRUTH, 1, world=_CANNED_WORLD).gap == 1

    def test_a_selection_outside_the_top_k_is_a_miss_at_that_k(self) -> None:
        """Truncated the same way pass@k is, so the two are comparable."""
        steps = steps_of(
            [_record_with(categories=("stale_cache", "consumer_saturation"), selected="c2")]
        )
        assert selected_at_k(steps, _TRUTH, 1).hit is False
        assert selected_at_k(steps, _TRUTH, 2).hit is True

    def test_a_step_with_no_selector_is_not_graded_rather_than_a_miss(self) -> None:
        record = _record_with(categories=("consumer_saturation",), selected="c1")
        record["selector"] = None
        steps = steps_of([record])
        entry = selected_at_k(steps, _TRUTH, 1)
        assert entry.hit is None
        assert "no candidate_selector ran" in entry.not_scored_because
        assert oracle_gap_at_k(steps, _TRUTH, 1, world=_CANNED_WORLD).gap is None

    def test_the_gap_can_never_be_negative(self) -> None:
        """A selection that hits IS a correct candidate inside the top k.

        Asserted rather than assumed: a negative gap in a report would read as
        the selector finding a candidate the generator never produced.
        """
        for k in (1, 2, 4, 8):
            for selected in ("c1", "c2", None):
                steps = steps_of(
                    [
                        _record_with(
                            categories=("consumer_saturation", "stale_cache"),
                            selected=selected,
                            decision="select" if selected else "escalate",
                        )
                    ]
                )
                assert (oracle_gap_at_k(steps, _TRUTH, k, world=_CANNED_WORLD).gap or 0) >= 0

    def test_uncertainty_is_paired_with_the_verdict(self) -> None:
        """Plan 03 § 9: an uncertainty without its verdict cannot be calibrated."""
        metrics = measure_selection(
            [_record_with(categories=("stale_cache",), selected="c1", uncertainty=0.9)],
            _TRUTH,
            world=_CANNED_WORLD,
        )
        assert metrics.uncertainty == 0.9
        assert metrics.uncertainty_was_right is False
        assert metrics.selector_calls == 1
        assert metrics.decision == "select"


class TestAGapIsPairedWithinOneWorld:
    def test_two_canned_runs_of_one_scenario_are_one_world(self) -> None:
        """The fixtures ARE the canned world, and they are committed."""
        left = world_key(scenario="s", execution_mode="canned", archive="aaa")
        right = world_key(scenario="s", execution_mode="canned", archive="bbb")
        assert left == right

    def test_two_live_runs_of_one_scenario_are_two_worlds(self) -> None:
        """A live world is only ever itself (INC-003).

        Nothing guarantees two live runs met the same world, so the archive is the key.
        """
        left = world_key(scenario="s", execution_mode="live", archive="aaa")
        right = world_key(scenario="s", execution_mode="live", archive="bbb")
        assert left != right

    def test_a_recorded_world_pairs_by_the_recordings_own_fingerprint(self) -> None:
        """ADR 0043: a recording IS a world, so two arms over one recording pair.

        Keyed on ``recorder.world_fingerprint``, not the path or the archive: a path is a
        name, an archive is a run, and two archives replaying one recording are the pair.
        """
        left = world_key(
            scenario="s", execution_mode="recorded", archive="aaa", world_fingerprint="w1"
        )
        right = world_key(
            scenario="s", execution_mode="recorded", archive="bbb", world_fingerprint="w1"
        )
        assert left == right
        assert left != world_key(
            scenario="s", execution_mode="recorded", archive="aaa", world_fingerprint="w2"
        )

    def test_a_recorded_run_with_no_fingerprint_is_refused(self) -> None:
        with pytest.raises(ValueError, match="carries no world fingerprint"):
            world_key(scenario="s", execution_mode="recorded", archive="aaa")

    def test_the_fingerprint_the_report_pairs_on_is_the_one_the_runner_writes(
        self,
    ) -> None:
        """The rule is reachable now that recorded mode exists (cmd #277, #279).

        ``runner._replay_record`` puts ``world_fingerprint`` on every recorded outcome;
        asserted against the runner's own key name, not a literal.
        """
        recorded = _outcome(mode=ExecutionMode.RECORDED, replay={"world_fingerprint": "abc123"})
        assert research_report.recorded_fingerprint(recorded) == "abc123"
        # A canned or live outcome has no replay row at all, and must not invent one.
        assert research_report.recorded_fingerprint(_outcome(mode=ExecutionMode.CANNED)) is None

    def test_a_recorded_run_is_keyed_by_its_fingerprint_end_to_end(self) -> None:
        """The two halves joined: what the runner wrote, through the report's
        reader, into the key the gap is paired on."""
        recorded = _outcome(mode=ExecutionMode.RECORDED, replay={"world_fingerprint": "abc123"})
        key = world_key(
            scenario="s",
            execution_mode=ExecutionMode.RECORDED.value,
            archive="whichever",
            world_fingerprint=research_report.recorded_fingerprint(recorded),
        )
        assert str(key) == "s@recorded:abc123"

    def test_an_unknown_mode_has_no_pairing_rule_and_refuses(self) -> None:
        """A mode with no rule must not borrow one chosen for another mode."""
        with pytest.raises(ValueError, match="unknown execution mode"):
            world_key(scenario="s", execution_mode="dreamt", archive="aaa")

    def test_a_gap_across_two_worlds_is_refused(self) -> None:
        """The refusal the order asks for, and it names both worlds."""
        steps = steps_of([_record_with(categories=("consumer_saturation",), selected="c1")])
        other = world_key(scenario="s", execution_mode="live", archive="zzz")
        with pytest.raises(OracleGapAcrossWorlds) as caught:
            oracle_gap_at_k(steps, _TRUTH, 1, world=_CANNED_WORLD, against=other)
        message = str(caught.value)
        assert str(_CANNED_WORLD) in message
        assert str(other) in message

    def test_a_gap_within_one_world_is_computed(self) -> None:
        steps = steps_of([_record_with(categories=("consumer_saturation",), selected="c1")])
        assert (
            oracle_gap_at_k(steps, _TRUTH, 1, world=_CANNED_WORLD, against=_CANNED_WORLD).gap == 0
        )

    def test_both_terms_come_off_one_runs_own_steps(self) -> None:
        """Paired by construction, which is the structural half of the rule.

        ``measure_selection`` computes both terms from the SAME ``steps`` sequence.
        """
        metrics = measure_selection(
            [_record_with(categories=("consumer_saturation", "stale_cache"), selected="c2")],
            _TRUTH,
            world=_CANNED_WORLD,
        )
        assert {str(gap.world) for gap in metrics.oracle_gap} == {str(_CANNED_WORLD)}
        for gap, selected in zip(metrics.oracle_gap, metrics.selected_at_k, strict=True):
            assert gap.k == selected.k
            assert gap.selected_hit == selected.hit


class TestNoSelectorNumberWithoutACalibrationReport:
    """Plan 02:243 and plan 04:169, both ways."""

    @staticmethod
    def _row(**overrides: Any) -> dict[str, Any]:
        row: dict[str, Any] = {
            "arm": "candidate_selector/best_of_n_enumerated/n=2",
            "selector_calls": 3,
            "selected_at_k": [{"k": 1, "hit": True}],
            "oracle_gap_at_k": [{"k": 1, "gap": 0}],
            "selector_uncertainty": 0.2,
            "selector_was_right": True,
            "selector_decision": "select",
            "pass_at_k": [{"k": 1, "hit": True}],
            "cross_step_duplicate_rate": 0.0,
            "calibration_report_id": None,
        }
        row.update(overrides)
        return row

    def test_the_register_is_empty_today(self) -> None:
        """WP-6.3 has not run, so no arm is calibrated. The gate is shut."""
        assert dict(research_report.CALIBRATION_REPORTS) == {}
        assert research_report.calibration_report_for("anything") is None

    def test_every_selector_field_is_withheld_without_one(self) -> None:
        gated = research_report._selector_number(self._row())
        for field in (
            "selected_at_k",
            "oracle_gap_at_k",
            "selector_uncertainty",
            "selector_was_right",
            "selector_decision",
        ):
            assert gated[field] == research_report.WITHHELD, field

    def test_the_generation_half_is_not_gated(self) -> None:
        """pass@k measures the GENERATOR and has nothing to do with calibration."""
        gated = research_report._selector_number(self._row())
        assert gated["pass_at_k"] == [{"k": 1, "hit": True}]
        assert gated["cross_step_duplicate_rate"] == 0.0

    def test_a_calibrated_arm_reports_its_numbers(self) -> None:
        row = self._row(calibration_report_id="cal-1234")
        assert research_report._selector_number(row) == row

    def test_a_row_with_no_selector_call_is_untouched(self) -> None:
        """A generator-only arm has no selector number to withhold."""
        row = self._row(selector_calls=0)
        assert research_report._selector_number(row) == row

    def test_withheld_is_a_sentence_and_not_null(self) -> None:
        """``null`` would read as zero to the next line of almost any reader —
        the distinction INC-003 turned on one level up."""
        assert isinstance(research_report.WITHHELD, str)
        assert "no calibration report" in research_report.WITHHELD

    def test_the_oracle_gap_section_refuses_an_uncalibrated_arm(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The section, not just the row: an uncalibrated arm makes the whole
        oracle-gap table unmeasurable, and it names the arm."""
        rows = [self._row()]
        monkeypatch.setattr(research_report, "_candidate_rows", lambda _r, _s: rows)
        section = research_report._oracle_gap(Path("."), ())
        assert section["measurable"] is False
        assert "candidate_selector/best_of_n_enumerated/n=2" in section["why"]
        assert "NO SELECTOR NUMBER IS REPORTED" in section["why"]

    def test_the_oracle_gap_section_computes_once_the_arm_is_calibrated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rows = [
            self._row(
                calibration_report_id="cal-1234",
                world=str(_CANNED_WORLD),
                scenario="s",
                group={"family": "consumer_lag", "difficulty": "level_1"},
                oracle_gap_at_k=[
                    {
                        "k": 1,
                        "world": str(_CANNED_WORLD),
                        "pass_hit": True,
                        "selected_hit": False,
                        "gap": 1,
                        "not_scored_because": "",
                    }
                ],
            )
        ]
        monkeypatch.setattr(research_report, "_candidate_rows", lambda _r, _s: rows)
        section = research_report._oracle_gap(Path("."), ())
        assert section["measurable"] is True
        value = section["value"]
        assert value["calibration_reports"] == {
            "candidate_selector/best_of_n_enumerated/n=2": "cal-1234"
        }
        by_family = value["by_family"][0]
        assert by_family["group"] == "consumer_lag"
        assert by_family["paired_runs"] == 1
        at_one = next(entry for entry in by_family["at_k"] if entry["k"] == 1)
        assert (at_one["pass_at_k"], at_one["selected_at_k"], at_one["oracle_gap"]) == (
            1.0,
            0.0,
            1.0,
        )
        # Every difference carries the paired count that produced it (plan 03 § 12).
        assert at_one["scored_runs"] == 1
        assert {entry["group"] for entry in value["by_world"]} == {str(_CANNED_WORLD)}

    def test_the_section_is_unmeasurable_over_todays_scope(self) -> None:
        """No archive in scope carries a selector block, so the gate is not even
        reached — and the committed document says so in the words it already had."""
        section = research_report._oracle_gap(research_report.REPO_ROOT, ())
        assert section["measurable"] is False
        assert "has not run" in section["why"]


class TestTheArmIsOffByDefault:
    def test_the_configured_default_strategy_is_still_baseline(self) -> None:
        assert _settings().inference_strategy is StrategyName.BASELINE

    def test_the_registry_holds_exactly_the_configurable_names(self) -> None:
        assert set(STRATEGIES.names) == {member.value for member in StrategyName}
