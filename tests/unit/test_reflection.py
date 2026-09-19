"""WP-9.1 — the ``reflection`` strategy (plan 02 § 13, ADR 0055).

The packet's whole claim is a bound and a measurement, so the tests are grouped that way:

* ``TestTheCapIsStructural`` — a critic that asks for another pass forever gets ONE. Proved
  three ways that do not share a mechanism: the pass token refuses a second ``spend``, the
  module holds no loop over the critic, and a run against an always-revise fake bills exactly
  two planner calls and one critic call per step.
* ``TestTheVerdictFollowsTheFindings`` — a critique that names a contradiction and keeps the
  step anyway is refused by the schema (LESSONS 2026-09-17), not asked not to in the prompt.
* ``TestTheStrategyWidensNothing`` — the revised step meets the same gates: below the
  threshold it escalates, outside ``FIX_MAP`` it escalates, and the critic cannot name a
  non-read tool at all.
* ``TestBothStepsAreOnTheRecord`` — initial and emitted, or the pass cannot be measured.
* ``TestTheReportTellsFixedFromHarmed`` / ``TestThePairedComparison`` — the numbers, on
  fixtures of each case.
"""

from __future__ import annotations

import ast
import json
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, Final
from uuid import uuid4

import pytest
from pydantic import ValidationError

from evals.candidate_metrics import WorldKey
from evals.reflection_metrics import (
    RevisionOutcome,
    RunCost,
    TwoRunsOfOneWorld,
    compare,
    measure_revision,
    outcome_of,
    revised_step_of,
)
from incident_commander.agent.accounting import RunAccounting
from incident_commander.agent.hypothesis import HypothesisCategory, InvestigationStep
from incident_commander.agent.investigation import make_llm_investigate
from incident_commander.agent.reflection import (
    CAP_ALREADY_SPENT,
    CRITIC_PROMPT,
    CRITIC_ROLE,
    KEEP_WITH_FINDINGS,
    MAX_REVISION_PASSES,
    REVISE_WITHOUT_FINDINGS,
    ReflectionCapExceeded,
    RevisionPass,
    StepCritique,
    format_critique_context,
    format_revision_context,
    render_critique,
)
from incident_commander.agent.state import EvidenceEntry, IncidentState, RunState
from incident_commander.agent.strategies.protocol import StrategyContext
from incident_commander.agent.strategies.records import StepRecord
from incident_commander.agent.strategies.reflection import (
    NO_CRITIC_CLIENT,
    ReflectionFailed,
    ReflectionStrategy,
)
from incident_commander.agent.strategies.registry import STRATEGIES
from incident_commander.llm.client import LLMError, LLMUsage
from incident_commander.llm.fakes import CannedLLMClient, CannedUsage
from incident_commander.llm.prompts.loader import load_prompt
from incident_commander.tools.mcp_client import ToolResult

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
_STRATEGY_MODULE: Final[Path] = (
    _REPO_ROOT / "src" / "incident_commander" / "agent" / "strategies" / "reflection.py"
)


# --------------------------------------------------------------------------
# Fakes. Local copies, for the reason ``test_strategies.py`` keeps its own: this
# file must be able to fail on its own.


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


class _AlwaysRevises:
    """A critic that asks for another pass on every call, forever.

    The red-before fake: nothing in its answers ever says "stop", so a cap that lived in the
    prompt or in a verdict would let it run the planner as many times as it liked.
    """

    def __init__(self, probe: str | None = "get_consumer_lag") -> None:
        self.calls: list[tuple[str, str]] = []
        self._probe = probe

    def call[T: Any](
        self,
        system_prompt: str,
        user_message: str,
        output_model: type[T],
        model: str,
        max_tokens: int = 4096,
        *,
        repair_of: str | None = None,
        temperature: float | None = None,
    ) -> Any:
        self.calls.append((system_prompt, user_message))
        from incident_commander.llm.client import LLMResult

        return LLMResult(
            output=output_model.model_validate(
                {
                    "unsupported_assumptions": ["the step assumes the lag is climbing"],
                    "contradictions": [],
                    "unexplained_symptoms": [],
                    "missing_probe": self._probe,
                    "verdict": "revise",
                    "reasoning": "One reading cannot show a trend; re-read it.",
                }
            ),
            stop_reason="canned",
        )


def _lag_response(lag: int = 42) -> ToolResult:
    return ToolResult(
        content=[
            {
                "type": "text",
                "text": json.dumps(
                    {
                        "consumer_group": "billing",
                        "lag": lag,
                        "lag_known": True,
                        "source": "static",
                        "cache_key": "kafka:consumer_lag:billing",
                    }
                ),
            }
        ]
    )


def _step_payload(
    *,
    category: str = "consumer_saturation",
    confidence: float = 0.55,
    action: dict[str, Any] | None = None,
    name: str | None = None,
) -> dict[str, Any]:
    return {
        "hypotheses": [
            {
                "category": category,
                "name": name or category,
                "confidence": confidence,
                "reasoning": "Alert severity suggests it.",
            }
        ],
        "next_action": action
        or {
            "kind": "probe",
            "tool_name": "get_consumer_lag",
            "arguments": {"consumer_group": "billing"},
        },
    }


def _keep_payload() -> dict[str, Any]:
    return {
        "unsupported_assumptions": [],
        "contradictions": [],
        "unexplained_symptoms": [],
        "missing_probe": None,
        "verdict": "keep",
        "reasoning": "The cited reading says what the step claims.",
    }


def _revise_payload(*, probe: str = "get_redis_health") -> dict[str, Any]:
    return {
        "unsupported_assumptions": [],
        "contradictions": [],
        "unexplained_symptoms": [],
        "missing_probe": probe,
        "verdict": "revise",
        "reasoning": "A read is left that would separate the leaders.",
    }


def _investigating(run_state: RunState) -> RunState:
    return run_state.model_copy(
        update={
            "state": IncidentState.INVESTIGATING,
            "alert": {"source": "kafka", "severity": "high", "group": "billing"},
        }
    )


def _with_evidence(run_state: RunState, now: datetime) -> RunState:
    return run_state.model_copy(
        update={
            "evidence": (
                EvidenceEntry(
                    tool_name="get_consumer_lag",
                    arguments={"consumer_group": "billing"},
                    result_summary="lag 42",
                    timestamp=now,
                ),
            )
        }
    )


def _context(
    planner: Any,
    critic: Any,
    *,
    iteration: int = 0,
    sink: list[StepRecord] | None = None,
) -> StrategyContext:
    return StrategyContext(
        llm_client=planner,
        model="claude-sonnet-4-6",
        iteration=iteration,
        record_step=None if sink is None else sink.append,
        critic_llm_client=critic,
    )


# --------------------------------------------------------------------------


class TestTheCapIsStructural:
    """One pass per step, held in code. Three independent proofs, on purpose.

    The gotcha this packet was filed with: "the cap is the safety property — enforce it in
    code, not in the critic's instructions; a prompt-enforced bound is a bound that fails
    open." A single test of a single mechanism would not show that.
    """

    def test_a_second_pass_on_one_step_raises(self) -> None:
        budget = RevisionPass()
        budget.spend()
        assert budget.exhausted
        with pytest.raises(ReflectionCapExceeded) as raised:
            budget.spend()
        assert CAP_ALREADY_SPENT in str(raised.value)

    def test_the_cap_is_one_and_is_not_configurable(self) -> None:
        # No ``Settings`` field and no ``StrategyKnobs`` field reaches it: a bound an
        # operator can raise is not a bound.
        from incident_commander.agent.strategies.knobs import StrategyKnobs
        from incident_commander.config import Settings

        assert MAX_REVISION_PASSES == 1
        assert RevisionPass().allowed == MAX_REVISION_PASSES
        knobs = set(StrategyKnobs.__dataclass_fields__)
        settings = set(Settings.model_fields)
        offenders = sorted(
            name
            for name in knobs | settings
            if "pass" in name or "reflect" in name or "critic" in name
        )
        assert offenders == [], (
            f"{offenders} would let configuration change the reflection cap. The pass "
            "count is the safety property; it is stamped into strategy_config, not read "
            "from the environment."
        )

    def test_a_critic_that_always_asks_again_gets_exactly_one_pass(
        self, run_state: RunState, now: datetime
    ) -> None:
        # RED BEFORE: with no structural cap, this fake drives the planner forever.
        planner = CannedLLMClient([_step_payload(), _step_payload(confidence=0.6)])
        critic = _AlwaysRevises(probe=None)
        strategy = ReflectionStrategy()
        sink: list[StepRecord] = []
        _, step, record = strategy.plan_next_step(
            _with_evidence(run_state, now), now, _context(planner, critic, sink=sink)
        )

        assert len(planner.calls) == 2, "the planner ran more than twice on one step"
        assert len(critic.calls) == 1, "the critic ran more than once on one step"
        assert step.hypotheses[0].confidence == 0.6, "the revised step was not the one emitted"
        assert record.revision is not None
        assert (record.revision.passes_used, record.revision.passes_allowed) == (1, 1)
        assert len(sink) == 1, "one step must produce exactly one record"

    def test_two_planner_role_calls_is_the_maximum_over_a_whole_run(
        self, run_state: RunState, now: datetime
    ) -> None:
        # Two iterations, both revised: six planner payloads would mean three per step.
        planner = CannedLLMClient(
            [
                _step_payload(),
                _step_payload(),
                _step_payload(action={"kind": "stop", "reason": "enough"}),
                _step_payload(action={"kind": "stop", "reason": "enough"}),
            ]
        )
        critic = CannedLLMClient([_revise_payload(), _revise_payload()])
        sink: list[StepRecord] = []
        make_llm_investigate(
            _FakeMCPClient(lambda _n, _a: _lag_response()),
            planner,
            model="claude-sonnet-4-6",
            strategy=ReflectionStrategy(),
            record_step=sink.append,
            critic_llm_client=critic,
        )(_investigating(run_state), now)

        assert len(sink) == 2
        for record in sink:
            roles = [call.role for call in record.llm_calls]
            assert roles.count("investigation_planner") <= 2, roles
            assert roles.count(CRITIC_ROLE) == 1, roles
        assert not planner.has_remaining, "a planner payload went unused"

    def test_the_module_holds_no_loop_over_the_critic(self) -> None:
        """Anti-vacuity for the two tests above: there is no code path that could loop.

        The token refuses a second pass, and this says nothing tries: ``critique_step`` and
        ``revise_step`` are each called exactly once in the module, and neither call sits
        inside a ``for``, a ``while`` or a comprehension.
        """
        tree = ast.parse(_STRATEGY_MODULE.read_text(encoding="utf-8"))
        loops = [
            node
            for node in ast.walk(tree)
            if isinstance(
                node, ast.For | ast.While | ast.AsyncFor | ast.ListComp | ast.GeneratorExp
            )
        ]
        called: list[str] = [
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        ]
        assert called.count("critique_step") == 1, called
        assert called.count("revise_step") == 1, called
        looped = {
            name
            for loop in loops
            for name in _called_names(loop)
            if name in {"critique_step", "revise_step"}
        }
        assert looped == set(), (
            f"{sorted(looped)} is called inside a loop in {_STRATEGY_MODULE.name}. One "
            "bounded pass per step means one call site, not a loop with a counter."
        )

    def test_the_critique_schema_offers_no_way_to_ask_for_another_pass(self) -> None:
        # The critic's only lever is the verdict, and the verdict does not carry a count.
        fields = set(StepCritique.model_fields)
        assert fields == {
            "unsupported_assumptions",
            "contradictions",
            "unexplained_symptoms",
            "missing_probe",
            "verdict",
            "reasoning",
        }
        with pytest.raises(ValidationError):
            StepCritique.model_validate({**_keep_payload(), "passes_requested": 3})


def _called_names(node: ast.AST) -> set[str]:
    return {
        child.func.id
        for child in ast.walk(node)
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
    }


class TestTheVerdictFollowsTheFindings:
    """LESSONS 2026-09-17, as a validator rather than as a plea in the prompt.

    "Quoting a fact and drawing the opposite conclusion from it is a real failure mode." A
    critique that lists a contradiction and approves the step is exactly that, so the schema
    refuses it and ADR 0035 re-asks once.
    """

    @staticmethod
    def _contradiction(evidence_id: str) -> dict[str, Any]:
        return {
            "evidence": {"evidence_id": evidence_id},
            "contradicted_claim": "the entry exists, so it cannot be missing",
        }

    def test_keep_with_a_finding_named_is_refused(self, run_state: RunState, now: datetime) -> None:
        from incident_commander.agent.candidates import grounded_in

        state = _with_evidence(run_state, now)
        entry = state.evidence[0]
        with grounded_in(state.evidence), pytest.raises(ValidationError) as raised:
            StepCritique.model_validate(
                {
                    **_keep_payload(),
                    "contradictions": [self._contradiction(str(entry.evidence_id))],
                }
            )
        assert KEEP_WITH_FINDINGS in str(raised.value)

    def test_keep_with_only_a_missing_probe_named_is_refused(self) -> None:
        with pytest.raises(ValidationError) as raised:
            StepCritique.model_validate({**_keep_payload(), "missing_probe": "get_redis_health"})
        assert KEEP_WITH_FINDINGS in str(raised.value)

    def test_revise_with_nothing_named_is_refused(self) -> None:
        with pytest.raises(ValidationError) as raised:
            StepCritique.model_validate({**_keep_payload(), "verdict": "revise"})
        assert REVISE_WITHOUT_FINDINGS in str(raised.value)

    def test_a_contradiction_must_cite_a_real_ledger_entry(
        self, run_state: RunState, now: datetime
    ) -> None:
        from incident_commander.agent.candidates import UNKNOWN_EVIDENCE_ID, grounded_in

        state = _with_evidence(run_state, now)
        with grounded_in(state.evidence), pytest.raises(ValidationError) as raised:
            StepCritique.model_validate(
                {
                    **_keep_payload(),
                    "verdict": "revise",
                    "contradictions": [self._contradiction(str(uuid4()))],
                }
            )
        assert UNKNOWN_EVIDENCE_ID in str(raised.value)

    def test_a_kept_critique_with_nothing_named_validates(self) -> None:
        critique = StepCritique.model_validate(_keep_payload())
        assert critique.findings == ()


class TestTheStrategyWidensNothing:
    """Plan 02 § 18: revision is not authorization. The gates are the loop's, unchanged."""

    def test_the_critic_cannot_name_a_privileged_tool(self) -> None:
        # ``missing_probe`` is a ``ReadToolName``, so the schema refuses the action tools
        # rather than the prompt asking the critic not to mention them.
        for tool in ("restart_consumer_group", "replay_dlq_messages", "rollback_deploy"):
            with pytest.raises(ValidationError):
                StepCritique.model_validate(
                    {**_revise_payload(probe="get_redis_health")} | {"missing_probe": tool}
                )

    def test_a_revised_remediate_below_the_threshold_still_escalates(
        self, run_state: RunState, now: datetime
    ) -> None:
        # The revised step asks to remediate at 0.5. The 0.7 gate is in investigation.py and
        # it does not know a critic was involved.
        planner = CannedLLMClient(
            [
                _step_payload(),
                _step_payload(confidence=0.5, action={"kind": "remediate", "reason": "fix it"}),
            ]
        )
        result = make_llm_investigate(
            _FakeMCPClient(lambda _n, _a: _lag_response()),
            planner,
            model="claude-sonnet-4-6",
            strategy=ReflectionStrategy(),
            critic_llm_client=CannedLLMClient([_revise_payload()]),
        )(_investigating(run_state), now)

        assert result.state is IncidentState.ESCALATED
        assert "below threshold" in result.evidence[-1].result_summary

    def test_a_revised_remediate_outside_the_fix_table_still_escalates(
        self, run_state: RunState, now: datetime
    ) -> None:
        planner = CannedLLMClient(
            [
                _step_payload(),
                _step_payload(
                    category="deploy_regression",
                    confidence=0.95,
                    action={"kind": "remediate", "reason": "roll it back"},
                ),
            ]
        )
        result = make_llm_investigate(
            _FakeMCPClient(lambda _n, _a: _lag_response()),
            planner,
            model="claude-sonnet-4-6",
            strategy=ReflectionStrategy(),
            critic_llm_client=CannedLLMClient([_revise_payload()]),
        )(_investigating(run_state), now)

        assert result.state is not IncidentState.REMEDIATING

    def test_the_strategy_module_holds_no_execution_policy(self) -> None:
        """``TestStrategiesHoldNoExecutionPolicy`` covers this module by parametrizing over
        the directory; asserted here too, because this file must fail on its own.

        On the AST, not the text: the module's docstring NAMES the gates it leaves in
        ``investigation.py``, and naming a thing in prose is the opposite of depending on it.
        """
        tree = ast.parse(_STRATEGY_MODULE.read_text(encoding="utf-8"))
        referenced = {
            node.id if isinstance(node, ast.Name) else node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Name | ast.Attribute)
        }
        offenders = referenced & {"FIX_MAP", "tier_of", "TOOL_REGISTRY", "wire_arguments"}
        assert offenders == set(), sorted(offenders)
        taken = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            and node.module == "incident_commander.agent.investigation"
            for alias in node.names
        }
        assert taken == {"_plan_next_step"}, taken

    def test_the_critic_never_sees_a_non_read_tool_in_its_context(
        self, run_state: RunState, now: datetime
    ) -> None:
        # The critic is shown the planner's own tool block, which is read-tier only.
        context = format_critique_context(
            _with_evidence(run_state, now), InvestigationStep.model_validate(_step_payload())
        )
        for tool in ("restart_consumer_group", "replay_dlq_messages", "pause_control_loop"):
            assert tool not in context


class TestTheCriticIsItsOwnRole:
    """ "Added tokens" is the number this arm is judged on, so the critique is metered apart."""

    def test_a_run_with_no_critic_client_is_refused(
        self, run_state: RunState, now: datetime
    ) -> None:
        strategy = ReflectionStrategy()
        with pytest.raises(ValueError) as raised:
            strategy.plan_next_step(
                _with_evidence(run_state, now),
                now,
                StrategyContext(
                    llm_client=CannedLLMClient([_step_payload()]),
                    model="m",
                    iteration=0,
                ),
            )
        assert NO_CRITIC_CLIENT in str(raised.value)

    def test_the_record_labels_each_call_with_its_own_role(
        self, run_state: RunState, now: datetime
    ) -> None:
        planner = CannedLLMClient(
            [_step_payload(), _step_payload()], usage=CannedUsage(input_tokens=10, output_tokens=5)
        )
        critic = CannedLLMClient(
            [_revise_payload()], usage=CannedUsage(input_tokens=7, output_tokens=3)
        )
        _, _, record = ReflectionStrategy().plan_next_step(
            _with_evidence(run_state, now), now, _context(planner, critic)
        )

        assert [call.role for call in record.llm_calls] == [
            "investigation_planner",
            CRITIC_ROLE,
            "investigation_planner",
        ]
        # Each call's ledger figures are its own delta, so three calls in one step stay apart.
        assert [call.tokens_used for call in record.llm_calls] == [15, 10, 15]

    def test_a_critic_failure_carries_the_planner_leg_bill(
        self, run_state: RunState, now: datetime
    ) -> None:
        # ADR 0045's trap one layer up: the loop charges ``accrue_llm_error`` against the
        # state held BEFORE the step, so a planner call already paid for would be free.
        class _Failing:
            def call(self, *_a: Any, **_kw: Any) -> Any:
                raise LLMError("upstream refused", usage=LLMUsage(input_tokens=1, output_tokens=1))

        planner = CannedLLMClient(
            [_step_payload()], usage=CannedUsage(input_tokens=100, output_tokens=20)
        )
        with pytest.raises(ReflectionFailed) as raised:
            ReflectionStrategy().plan_next_step(
                _with_evidence(run_state, now), now, _context(planner, _Failing())
            )
        usage = raised.value.usage
        assert usage is not None
        assert usage.input_tokens == 101, "the planner call's bill was dropped"

    def test_the_accounting_counts_critiques_and_revisions(
        self, run_state: RunState, now: datetime
    ) -> None:
        accounting = RunAccounting()
        planner = CannedLLMClient(
            [
                _step_payload(),
                _step_payload(),
                _step_payload(action={"kind": "stop", "reason": "enough"}),
            ]
        )
        make_llm_investigate(
            _FakeMCPClient(lambda _n, _a: _lag_response()),
            planner,
            model="claude-sonnet-4-6",
            strategy=ReflectionStrategy(),
            record_step=accounting.step_sink(),
            critic_llm_client=CannedLLMClient([_revise_payload(), _keep_payload()]),
        )(_investigating(run_state), now)

        assert accounting.critic_calls == 2, "one critique per step"
        assert accounting.revised_steps == 1, "only the revised step counts as revised"
        assert accounting.selector_calls == 0


class TestBothStepsAreOnTheRecord:
    """A pass that recorded only its output could not be measured for harm."""

    def test_a_revised_step_records_the_step_it_replaced(
        self, run_state: RunState, now: datetime
    ) -> None:
        planner = CannedLLMClient(
            [
                _step_payload(category="stale_cache", name="cache went stale"),
                _step_payload(category="poison_message", name="one bad row"),
            ]
        )
        _, step, record = ReflectionStrategy().plan_next_step(
            _with_evidence(run_state, now),
            now,
            _context(planner, CannedLLMClient([_revise_payload()])),
        )

        assert record.revision is not None
        assert record.revision.initial_step.hypotheses[0].category is HypothesisCategory.STALE_CACHE
        assert record.emitted_step.hypotheses[0].category is HypothesisCategory.POISON_MESSAGE
        assert step is record.emitted_step
        assert record.revision.revised is True
        assert record.revision.verdict == "revise"
        assert record.revision.findings

    def test_a_kept_step_records_the_critique_and_no_revision(
        self, run_state: RunState, now: datetime
    ) -> None:
        planner = CannedLLMClient([_step_payload()])
        _, step, record = ReflectionStrategy().plan_next_step(
            _with_evidence(run_state, now),
            now,
            _context(planner, CannedLLMClient([_keep_payload()])),
        )

        assert record.revision is not None
        assert record.revision.verdict == "keep"
        assert record.revision.revised is False
        assert record.revision.findings == ()
        assert record.revision.passes_used == 0
        assert record.revision.initial_step == record.emitted_step
        assert step.hypotheses[0].category is HypothesisCategory.CONSUMER_SATURATION
        assert len(planner.calls) == 1, "a kept step must not pay for a second planner call"

    def test_the_record_serialises_for_a_tracer(self, run_state: RunState, now: datetime) -> None:
        planner = CannedLLMClient([_step_payload(), _step_payload()])
        _, _, record = ReflectionStrategy().plan_next_step(
            _with_evidence(run_state, now),
            now,
            _context(planner, CannedLLMClient([_revise_payload()])),
        )
        written = json.loads(json.dumps(record.as_trace_record(), default=str))

        assert written["revision"]["verdict"] == "revise"
        assert written["revision"]["initial_step"]["hypotheses"]
        assert written["revision"]["passes_allowed"] == 1
        assert written["selector"] is None

    def test_every_other_arm_writes_a_null_revision_block(self) -> None:
        for name in STRATEGIES.names:
            if name == "reflection":
                continue
            strategy = STRATEGIES.create(name)
            assert "revision" not in type(strategy).__dict__, name

    def test_the_revision_context_names_the_contradicted_reading_not_its_id(
        self, run_state: RunState, now: datetime
    ) -> None:
        # The reviser is shown ``baseline``'s context, which carries no ids, so a raw uuid
        # would point at something not on its page (ADR 0044, ADR 0047).
        from incident_commander.agent.candidates import grounded_in

        state = _with_evidence(run_state, now)
        entry = state.evidence[0]
        with grounded_in(state.evidence):
            critique = StepCritique.model_validate(
                {
                    **_keep_payload(),
                    "verdict": "revise",
                    "contradictions": [
                        {
                            "evidence": {"evidence_id": str(entry.evidence_id)},
                            "contradicted_claim": "the lag is climbing",
                        }
                    ],
                }
            )
        rendered = render_critique(state, critique)
        assert "lag 42" in rendered
        assert str(entry.evidence_id) not in rendered
        context = format_revision_context(
            state, InvestigationStep.model_validate(_step_payload()), critique
        )
        assert "evidence_id=" not in context, "the reviser must get baseline's context bytes"


# --------------------------------------------------------------------------
# The measurement


def _record_json(
    *,
    initial: str,
    emitted: str,
    revised: bool = True,
    verdict: str = "revise",
    action: str = "stop",
    findings: tuple[str, ...] = ("missing_probe: get_redis_health",),
) -> dict[str, Any]:
    """One ``StepRecord``'s JSON form with a chosen before/after diagnosis."""
    return {
        "iteration": 0,
        "strategy": "reflection",
        "candidate_set": [],
        "selector": None,
        "revision": {
            "initial_step": _step_payload(category=initial),
            "verdict": verdict,
            "findings": list(findings if revised else ()),
            "contradicted_evidence_ids": [],
            "revised": revised,
            "passes_used": 1 if revised else 0,
            "passes_allowed": 1,
            "critic_call_id": "c1",
            "revision_call_id": "r1" if revised else "",
        },
        "emitted_step": {
            **_step_payload(category=emitted),
            "next_action": {"kind": action, "reason": "done"},
        },
    }


class TestTheReportTellsFixedFromHarmed:
    """On fixtures of each case. The order's own acceptance: a report that cannot tell them
    apart cannot answer the question reflection is being asked."""

    _EXPECTED: Final[tuple[HypothesisCategory, ...]] = (HypothesisCategory.POISON_MESSAGE,)

    def test_a_wrong_step_revised_right_reads_as_fixed(self) -> None:
        step = revised_step_of(_record_json(initial="stale_cache", emitted="poison_message"))
        assert outcome_of(step, self._EXPECTED) is RevisionOutcome.FIXED

    def test_a_right_step_revised_wrong_reads_as_harmed(self) -> None:
        step = revised_step_of(_record_json(initial="poison_message", emitted="stale_cache"))
        assert outcome_of(step, self._EXPECTED) is RevisionOutcome.HARMED

    def test_a_right_step_revised_and_still_right_reads_as_unchanged(self) -> None:
        step = revised_step_of(_record_json(initial="poison_message", emitted="poison_message"))
        assert outcome_of(step, self._EXPECTED) is RevisionOutcome.UNCHANGED_CORRECT

    def test_a_wrong_step_revised_and_still_wrong_reads_as_unchanged(self) -> None:
        step = revised_step_of(_record_json(initial="stale_cache", emitted="runaway_saga"))
        assert outcome_of(step, self._EXPECTED) is RevisionOutcome.UNCHANGED_WRONG

    def test_a_kept_step_is_not_revised_rather_than_unchanged(self) -> None:
        step = revised_step_of(
            _record_json(
                initial="poison_message", emitted="poison_message", revised=False, verdict="keep"
            )
        )
        assert outcome_of(step, self._EXPECTED) is RevisionOutcome.NOT_REVISED

    def test_no_ground_truth_is_not_graded_rather_than_wrong(self) -> None:
        step = revised_step_of(_record_json(initial="stale_cache", emitted="poison_message"))
        assert outcome_of(step, ()) is RevisionOutcome.NOT_GRADED

    def test_the_run_report_counts_critiques_revisions_and_findings(self) -> None:
        metrics = measure_revision(
            [
                _record_json(initial="stale_cache", emitted="stale_cache", action="probe"),
                _record_json(initial="stale_cache", emitted="poison_message"),
            ],
            self._EXPECTED,
        )
        assert (metrics.steps, metrics.critiques, metrics.revisions) == (2, 2, 2)
        assert metrics.findings_by_class == {"missing_probe": 2}
        assert metrics.deciding_outcome is RevisionOutcome.FIXED
        assert metrics.cap_held
        assert (metrics.max_passes_used, metrics.passes_allowed) == (1, 1)

    def test_the_report_reads_a_live_record_object_the_same_way(
        self, run_state: RunState, now: datetime
    ) -> None:
        # The same measurement off the dataclass and off its JSON — the same data at two ages.
        planner = CannedLLMClient(
            [
                _step_payload(category="stale_cache"),
                _step_payload(category="poison_message", action={"kind": "stop", "reason": "ok"}),
            ]
        )
        _, _, record = ReflectionStrategy().plan_next_step(
            _with_evidence(run_state, now),
            now,
            _context(planner, CannedLLMClient([_revise_payload()])),
        )
        from_object = measure_revision([record], self._EXPECTED)
        from_json = measure_revision(
            [json.loads(json.dumps(record.as_trace_record(), default=str))], self._EXPECTED
        )
        assert from_object.deciding_outcome is RevisionOutcome.FIXED
        assert from_json.deciding_outcome is from_object.deciding_outcome

    def test_a_breached_cap_is_reported_rather_than_averaged(self) -> None:
        record = _record_json(initial="stale_cache", emitted="poison_message")
        record["revision"]["passes_used"] = 2
        metrics = measure_revision([record], self._EXPECTED)
        assert not metrics.cap_held
        assert any("CAP BREACHED" in line for line in metrics.describe())


class TestThePairedComparison:
    """Plan 03 § 12: same world, both arms, and harmed printed beside fixed."""

    @staticmethod
    def _world(scenario: str) -> WorldKey:
        return WorldKey(scenario=scenario, mode="canned", instance="fixtures")

    def _arm(
        self,
        scenario: str,
        strategy: str,
        *,
        correct: bool | None,
        family: str = "dlq",
        difficulty: str = "medium",
        planner_tokens: int = 100,
        critic_tokens: int = 0,
        tool_calls: int = 3,
    ) -> RunCost:
        return RunCost(
            world=self._world(scenario),
            strategy=strategy,
            family=family,
            difficulty=difficulty,
            correct=correct,
            planner_tokens=planner_tokens,
            critic_tokens=critic_tokens,
            tool_calls=tool_calls,
        )

    def test_fixed_harmed_and_net_come_out_of_paired_worlds(self) -> None:
        baseline = [
            self._arm("a", "baseline", correct=False),
            self._arm("b", "baseline", correct=True),
            self._arm("c", "baseline", correct=True),
        ]
        reflection = [
            self._arm("a", "reflection", correct=True, critic_tokens=40, tool_calls=4),
            self._arm("b", "reflection", correct=False, critic_tokens=40),
            self._arm("c", "reflection", correct=True, critic_tokens=40),
        ]
        comparison = compare(baseline, reflection)

        assert (comparison.fixed, comparison.harmed, comparison.net) == (1, 1, 0)
        assert comparison.graded == 3
        assert comparison.added_tokens == 120
        assert comparison.added_tool_calls == 1
        # Harmed is on its own line, not folded into the net.
        assert "cases harmed: 1" in comparison.describe()

    def test_added_cost_is_reported_by_family_and_by_difficulty(self) -> None:
        baseline = [
            self._arm("a", "baseline", correct=False, family="dlq", difficulty="easy"),
            self._arm("b", "baseline", correct=True, family="cache_redis", difficulty="hard"),
        ]
        reflection = [
            self._arm(
                "a",
                "reflection",
                correct=True,
                family="dlq",
                difficulty="easy",
                critic_tokens=40,
                tool_calls=5,
            ),
            self._arm(
                "b",
                "reflection",
                correct=False,
                family="cache_redis",
                difficulty="hard",
                critic_tokens=10,
            ),
        ]
        comparison = compare(baseline, reflection)
        families = {row.group: row for row in comparison.by_family()}
        difficulties = {row.group: row for row in comparison.by_difficulty()}

        assert families["dlq"].fixed == 1 and families["dlq"].harmed == 0
        assert families["dlq"].added_tokens == 40
        assert families["dlq"].added_tool_calls == 2
        assert families["cache_redis"].harmed == 1
        assert families["cache_redis"].net == -1
        assert difficulties["easy"].fixed == 1
        assert difficulties["hard"].harmed == 1

    def test_a_world_only_one_arm_ran_is_reported_not_dropped_silently(self) -> None:
        comparison = compare(
            [self._arm("a", "baseline", correct=True), self._arm("b", "baseline", correct=True)],
            [self._arm("a", "reflection", correct=True)],
        )
        assert len(comparison.cases) == 1
        assert comparison.unpaired == (self._world("b"),)
        assert "unpaired: 1" in comparison.describe()[0]

    def test_two_runs_of_one_world_in_one_arm_is_refused(self) -> None:
        with pytest.raises(TwoRunsOfOneWorld):
            compare(
                [self._arm("a", "baseline", correct=True)],
                [self._arm("a", "reflection", correct=True)] * 2,
            )

    def test_two_live_runs_never_pair(self) -> None:
        # ``WorldKey`` keys a live run by its ARCHIVE, so nothing guarantees two live runs
        # met the same seeded fault (INC-003). The pairing simply finds nothing.
        left = RunCost(
            world=WorldKey(scenario="a", mode="live", instance="arch1"),
            strategy="baseline",
            correct=False,
        )
        right = RunCost(
            world=WorldKey(scenario="a", mode="live", instance="arch2"),
            strategy="reflection",
            correct=True,
        )
        comparison = compare([left], [right])
        assert comparison.cases == ()
        assert len(comparison.unpaired) == 2

    def test_recorded_worlds_pair_on_the_recordings_fingerprint(self) -> None:
        """The mode the plan asks the comparison to run in (ADR 0043: a recording IS a world).

        Two recorded runs of one recording pair; two runs of DIFFERENT recordings of the same
        scenario do not, which is the whole of ``WorldKey``'s rule for this mode.
        """
        same = WorldKey(scenario="a", mode="recorded", instance="fp-1")
        other = WorldKey(scenario="a", mode="recorded", instance="fp-2")
        paired = compare(
            [RunCost(world=same, strategy="baseline", correct=False, planner_tokens=100)],
            [
                RunCost(
                    world=same,
                    strategy="reflection",
                    correct=True,
                    planner_tokens=180,
                    critic_tokens=40,
                    family="workflow_stuck",
                    difficulty="hard",
                )
            ],
        )
        assert (paired.fixed, paired.harmed, paired.net) == (1, 0, 1)
        assert paired.added_tokens == 120
        assert paired.by_family()[0].group == "workflow_stuck"

        unpaired = compare(
            [RunCost(world=same, strategy="baseline", correct=False)],
            [RunCost(world=other, strategy="reflection", correct=True)],
        )
        assert unpaired.cases == ()
        assert set(unpaired.unpaired) == {same, other}

    def test_an_unlabelled_run_groups_under_a_named_bucket(self) -> None:
        # ``""`` would read as a group whose name is blank.
        comparison = compare(
            [self._arm("a", "baseline", correct=True, family="", difficulty="")],
            [self._arm("a", "reflection", correct=True, family="", difficulty="")],
        )
        assert [row.group for row in comparison.by_family()] == ["unlabelled"]

    def test_an_ungraded_scenario_is_paired_but_not_graded(self) -> None:
        comparison = compare(
            [self._arm("a", "baseline", correct=None)],
            [self._arm("a", "reflection", correct=None, critic_tokens=40)],
        )
        assert len(comparison.cases) == 1
        assert comparison.graded == 0
        assert comparison.cases[0].outcome is RevisionOutcome.NOT_GRADED
        assert comparison.added_tokens == 40


class TestTheCriticPromptIsWiredIn:
    def test_the_arm_is_registered_and_stamps_its_bound(self) -> None:
        strategy = STRATEGIES.create("reflection")
        assert strategy.name == "reflection"
        assert strategy.config["passes"] == MAX_REVISION_PASSES
        assert strategy.config["cap"] == "structural"
        assert strategy.config["critic"] == CRITIC_ROLE
        assert strategy.config["evidence_ids_rendered"] is False

    def test_the_config_block_cannot_be_written_through(self) -> None:
        strategy = ReflectionStrategy()
        with pytest.raises(TypeError):
            strategy.config["passes"] = 5  # type: ignore[index]

    def test_the_critic_prompt_and_the_revision_addendum_both_load(self) -> None:
        assert "record_output" in load_prompt(CRITIC_PROMPT)
        assert "revision" in load_prompt("investigation_planner_revision").lower()
