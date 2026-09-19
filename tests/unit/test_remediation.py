"""Unit tests for the Phase 6 remediation loop (plan → execute → verify).

Covers the three transitions in ``agent/remediation.py`` end to end with canned
clients, plus idempotency key semantics and the tier guardrails on planner output.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final
from uuid import UUID

import pytest
from pydantic import BaseModel

from incident_commander.agent.hypothesis import Hypothesis, HypothesisCategory
from incident_commander.agent.investigation import HINT_ROUTED_TOOLS
from incident_commander.agent.planner_context import (
    ALREADY_ATTEMPTED_HEADING,
    ATTEMPT_FAILED_MARKER,
    format_planner_context,
)
from incident_commander.agent.remediation import (
    _PLAN_REFUSED_SUBJECT_TARGET_MARKER,
    _ROW_DISPOSITION,
    DEAD_LETTER_ACTIONS,
    DLQ_LISTING_SCOPES,
    DLQ_ROW_SOURCE,
    RemediationPlan,
    SubjectKind,
    _evidence_value_corpus,
    _format_plan_context,
    _row_decisions_in_evidence,
    _row_source_for_subject,
    _subject_kind,
    _unsourced_resource_args,
    build_idempotency_key,
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
from incident_commander.config import DEFAULT_MAX_REMEDIATION_ATTEMPTS
from incident_commander.llm.client import LLMError, LLMResult, LLMUsage
from incident_commander.llm.fakes import CannedLLMClient, CannedUsage
from incident_commander.tools.mcp_client import MCPError, ToolResult

_MODEL = "test-model"


class _FakeMCP:
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


def _now() -> datetime:
    return datetime(2026, 7, 30, 12, 0, tzinfo=UTC)


# The default alert for this file, and it names NO subject on purpose.
#
# It carried ``consumer_group`` until ADR 0032's subject-target guard began comparing
# the alert against the plan: a subject-naming default would bind that guard on every
# test here, and seventeen of them plan a DLQ action to exercise a DIFFERENT guard, so
# each would be refused for a reason it is not about. A condition-naming alert is one
# of the three legitimate inert cases ``alert_subject`` documents; tests that mean to
# exercise the subject guard pass their own ``alert=``.
_CONDITION_ALERT: Final[dict[str, Any]] = {
    "source": "platform.kafka",
    "severity": "high",
    "fingerprint": "db_latency_high",
}

# The alert for tests whose plan acts on ``worker-dispatcher``, with two jobs. It names
# the group as the alert's SUBJECT, so a restart of that group satisfies ADR 0032's
# target test — the realistic pairing, and the one live run D made. And it puts the
# string in the evidence value corpus, which ``_unsourced_resource_args`` requires of
# any resource a plan names: without a provenance source every consumer-restart plan
# here is refused for a copy-don't-re-type violation instead.
_GROUP_ALERT: Final[dict[str, Any]] = {
    "source": "platform.kafka",
    "severity": "high",
    "fingerprint": "consumer_stalled",
    "consumer_group": "worker-dispatcher",
}


def _run_state(
    *,
    state: IncidentState,
    hypotheses: tuple[Hypothesis, ...] = (),
    remediation_plan: dict[str, Any] | None = None,
    evidence: tuple[EvidenceEntry, ...] = (),
    remediation_attempts: int = 0,
    alert: dict[str, Any] | None = None,
) -> RunState:
    return RunState(
        incident_id=UUID("11111111-1111-1111-1111-111111111111"),
        state=state,
        alert=dict(_CONDITION_ALERT if alert is None else alert),
        budget=BudgetLedger(
            max_tool_calls=25,
            max_tokens=200_000,
            max_wall_seconds=600,
            max_usd=Decimal("1.00"),
        ),
        hypotheses=hypotheses,
        remediation_plan=remediation_plan,
        remediation_attempts=remediation_attempts,
        evidence=evidence,
        created_at=_now(),
        updated_at=_now(),
    )


def _dlq_listing() -> EvidenceEntry:
    """One unfiltered ``list_dlq_messages`` reading, as the loop records it.

    Arguments are the WIRED ones — ``wire_arguments`` fills every unset optional with an
    explicit ``null``, so an unfiltered read reaches the ledger with
    ``remediation_hint: None`` rather than with the key absent.
    """
    return EvidenceEntry(
        tool_name="list_dlq_messages",
        arguments={"job_type": None, "remediation_hint": None, "limit": 50, "offset": 0},
        result_summary=(
            '{"total":1,"items":[{"id":"fc8d2a03-23b3-5371-9acb-46443c73baa5",'
            '"type":"bulk_api_sync","retry_count":3,"remediation_hint":"replay_safe",'
            '"created_at":"2026-08-31T01:21:46.584955Z"}]}'
        ),
        timestamp=_now(),
    )


def _plan_dict(**overrides: Any) -> dict[str, Any]:
    base = {
        "target_hypothesis": "consumer_saturation",
        "action_tool": "restart_consumer_group",
        "action_arguments": {"consumer_group": "worker-dispatcher"},
        "verify_tool": "get_consumer_lag",
        "verify_arguments": {"consumer_group": "worker-dispatcher"},
        "verify_expectation": "lag should drop to near-zero after the restart",
    }
    base.update(overrides)
    return base


class TestIdempotencyKey:
    def test_deterministic_for_same_inputs(self) -> None:
        key1 = build_idempotency_key(
            "incident-a", "restart_consumer_group", {"consumer_group": "billing"}
        )
        key2 = build_idempotency_key(
            "incident-a", "restart_consumer_group", {"consumer_group": "billing"}
        )
        assert key1 == key2

    def test_different_incident_yields_different_key(self) -> None:
        key1 = build_idempotency_key("incident-a", "restart_consumer_group", {})
        key2 = build_idempotency_key("incident-b", "restart_consumer_group", {})
        assert key1 != key2

    def test_different_args_yield_different_key(self) -> None:
        key1 = build_idempotency_key("i", "restart_consumer_group", {"consumer_group": "a"})
        key2 = build_idempotency_key("i", "restart_consumer_group", {"consumer_group": "b"})
        assert key1 != key2

    def test_agent_supplied_idempotency_key_is_ignored_in_hash(self) -> None:
        # Passing an already-present idempotency_key must not affect the hash.
        key1 = build_idempotency_key("i", "t", {"consumer_group": "a"})
        key2 = build_idempotency_key(
            "i", "t", {"consumer_group": "a", "idempotency_key": "should-be-ignored"}
        )
        assert key1 == key2


class TestPlanning:
    def _canned_planner(self, plan: dict[str, Any]) -> CannedLLMClient:
        return CannedLLMClient([plan])

    def test_valid_plan_transitions_to_remediating(self) -> None:
        llm = self._canned_planner(_plan_dict())
        transition = make_llm_plan(llm, model=_MODEL)
        run = _run_state(
            state=IncidentState.PLANNING,
            hypotheses=(
                Hypothesis(
                    category=HypothesisCategory.CONSUMER_SATURATION,
                    name="consumer_saturation",
                    confidence=0.85,
                    reasoning="r",
                ),
            ),
            alert=_GROUP_ALERT,
        )
        result = transition(run, _now())
        assert result.state is IncidentState.REMEDIATING
        assert result.remediation_plan is not None
        assert result.remediation_plan["action_tool"] == "restart_consumer_group"

    def test_empty_hypotheses_escalates(self) -> None:
        transition = make_llm_plan(self._canned_planner(_plan_dict()), model=_MODEL)
        run = _run_state(state=IncidentState.PLANNING, hypotheses=())
        result = transition(run, _now())
        assert result.state is IncidentState.ESCALATED
        assert any("no hypotheses" in e.result_summary for e in result.evidence)

    def test_non_tier_1_action_escalates(self) -> None:
        # Post-hardening: ``RemediationPlan.action_tool`` is Literal-typed to Tier-1 tools, so
        # a read-tool value is a schema violation Pydantic rejects, and ``make_llm_plan``'s
        # catch escalates naming the rejected value.
        bad = _plan_dict(action_tool="get_consumer_lag")
        transition = make_llm_plan(self._canned_planner(bad), model=_MODEL)
        run = _run_state(
            state=IncidentState.PLANNING,
            hypotheses=(
                Hypothesis(
                    category=HypothesisCategory.CONSUMER_SATURATION,
                    name="x",
                    confidence=0.9,
                    reasoning="r",
                ),
            ),
        )
        result = transition(run, _now())
        assert result.state is IncidentState.ESCALATED
        assert any(
            "get_consumer_lag" in e.result_summary or "invalid" in e.result_summary
            for e in result.evidence
        )

    def test_unknown_action_tool_escalates(self) -> None:
        # Post-hardening: same rejection path as the non-Tier-1 test.
        # "made_up_action" is not in the Tier1ToolName Literal.
        bad = _plan_dict(action_tool="made_up_action")
        transition = make_llm_plan(self._canned_planner(bad), model=_MODEL)
        run = _run_state(
            state=IncidentState.PLANNING,
            hypotheses=(
                Hypothesis(
                    category=HypothesisCategory.CONSUMER_SATURATION,
                    name="x",
                    confidence=0.9,
                    reasoning="r",
                ),
            ),
        )
        result = transition(run, _now())
        assert result.state is IncidentState.ESCALATED
        assert any(
            "made_up_action" in e.result_summary or "invalid" in e.result_summary
            for e in result.evidence
        )

    def test_non_read_verify_tool_escalates(self) -> None:
        # Planner picked a Tier-1 write action as the verify tool.
        bad = _plan_dict(verify_tool="restart_consumer_group")
        transition = make_llm_plan(self._canned_planner(bad), model=_MODEL)
        run = _run_state(
            state=IncidentState.PLANNING,
            hypotheses=(
                Hypothesis(
                    category=HypothesisCategory.CONSUMER_SATURATION,
                    name="x",
                    confidence=0.9,
                    reasoning="r",
                ),
            ),
        )
        result = transition(run, _now())
        assert result.state is IncidentState.ESCALATED
        # Post-hardening: ``verify_tool`` is Literal-typed to read tools, so a Tier-1 write
        # value is rejected by Pydantic and the planner catch escalates.
        assert any(
            "restart_consumer_group" in e.result_summary or "invalid" in e.result_summary
            for e in result.evidence
        )


class TestRemediating:
    def test_successful_action_transitions_to_verifying(self) -> None:
        def handler(name: str, args: Mapping[str, Any]) -> ToolResult:
            assert name == "restart_consumer_group"
            assert "idempotency_key" in args
            assert len(args["idempotency_key"]) == 32
            return ToolResult(
                content=[
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "consumer_group": args["consumer_group"],
                                "kill_key_cleared": True,
                                "latency_key_cleared": False,
                                "group_recognized": True,
                                "accepted": True,
                            }
                        ),
                    }
                ]
            )

        mcp = _FakeMCP(handler)
        transition = make_remediate(mcp)
        run = _run_state(state=IncidentState.REMEDIATING, remediation_plan=_plan_dict())
        result = transition(run, _now())
        assert result.state is IncidentState.VERIFYING
        assert result.budget.tool_calls_used == 1
        assert len(mcp.calls) == 1

    def test_missing_plan_escalates(self) -> None:
        mcp = _FakeMCP(lambda _n, _a: ToolResult(content=[]))
        transition = make_remediate(mcp)
        run = _run_state(state=IncidentState.REMEDIATING, remediation_plan=None)
        result = transition(run, _now())
        assert result.state is IncidentState.ESCALATED
        assert mcp.calls == []

    def test_tool_error_escalates(self) -> None:
        def erroring(_n: str, _a: Mapping[str, Any]) -> ToolResult:
            raise MCPError(-32000, "platform boom")

        mcp = _FakeMCP(erroring)
        transition = make_remediate(mcp)
        run = _run_state(state=IncidentState.REMEDIATING, remediation_plan=_plan_dict())
        result = transition(run, _now())
        assert result.state is IncidentState.ESCALATED
        assert any("platform boom" in e.result_summary for e in result.evidence)

    def test_is_error_result_escalates(self) -> None:
        mcp = _FakeMCP(
            lambda _n, _a: ToolResult(content=[{"type": "text", "text": "{}"}], is_error=True)
        )
        transition = make_remediate(mcp)
        run = _run_state(state=IncidentState.REMEDIATING, remediation_plan=_plan_dict())
        result = transition(run, _now())
        assert result.state is IncidentState.ESCALATED

    def test_unparseable_response_still_records_the_executed_action(self) -> None:
        # R2-38: the platform returned a NON-error result, so the Tier-1 action RAN and only
        # our parse of its response failed. Dropping attempted_tool here hid an executed
        # action from the SAFETY grader and from the human reading the briefing.
        mcp = _FakeMCP(
            lambda _n, _a: ToolResult(content=[{"type": "text", "text": "not json at all"}])
        )
        transition = make_remediate(mcp)
        run = _run_state(state=IncidentState.REMEDIATING, remediation_plan=_plan_dict())
        result = transition(run, _now())
        assert result.state is IncidentState.ESCALATED
        marker = result.evidence[-1]
        assert marker.tool_name == "_remediation_escalate"
        assert marker.arguments["attempted_tool"] == "restart_consumer_group"
        attempted = marker.arguments["attempted_arguments"]
        assert isinstance(attempted, dict)
        assert attempted["consumer_group"] == "worker-dispatcher"
        # The idempotency key travels with it: re-firing by hand without it
        # is a second real execution.
        assert "idempotency_key" in attempted

    def test_unparseable_response_charges_the_call_it_made(self) -> None:
        # The action executed, so it costs a tool call and an attempt, as on the success path.
        # Charging nothing let a run spend an unbudgeted Tier-1 action.
        mcp = _FakeMCP(lambda _n, _a: ToolResult(content=[{"type": "text", "text": "{}"}]))
        transition = make_remediate(mcp)
        run = _run_state(state=IncidentState.REMEDIATING, remediation_plan=_plan_dict())
        result = transition(run, _now())
        assert result.state is IncidentState.ESCALATED
        assert result.budget.tool_calls_used == 1
        assert result.remediation_attempts == 1

    def test_unparseable_response_is_visible_to_the_safety_grader(self) -> None:
        # docs/eval-methodology.md: "the dimension exists so that the
        # *attempt* is graded red even when the platform blocks it".
        from evals.graders.deterministic import GradeDimension, ScenarioExpectation, grade

        mcp = _FakeMCP(lambda _n, _a: ToolResult(content=[{"type": "text", "text": "<html>"}]))
        transition = make_remediate(mcp)
        run = _run_state(state=IncidentState.REMEDIATING, remediation_plan=_plan_dict())
        result = transition(run, _now())
        report = grade(
            result,
            ScenarioExpectation(
                name="safety-sees-the-attempt",
                expected_terminal_state=IncidentState.ESCALATED,
                forbidden_action_tools=("restart_consumer_group",),
            ),
        )
        safety = next(d for d in report.dimensions if d.dimension is GradeDimension.SAFETY)
        assert not safety.passed
        assert "restart_consumer_group" in safety.detail

    def test_tool_error_and_is_error_still_carry_the_attempt(self) -> None:
        def erroring(_n: str, _a: Mapping[str, Any]) -> ToolResult:
            raise MCPError(-32000, "platform boom")

        for mcp in (
            _FakeMCP(erroring),
            _FakeMCP(
                lambda _n, _a: ToolResult(content=[{"type": "text", "text": "{}"}], is_error=True)
            ),
        ):
            transition = make_remediate(mcp)
            run = _run_state(state=IncidentState.REMEDIATING, remediation_plan=_plan_dict())
            marker = transition(run, _now()).evidence[-1]
            assert marker.arguments["attempted_tool"] == "restart_consumer_group"

    def test_action_timeout_forwarded_to_call_tool(self) -> None:
        captured: list[float | None] = []

        class _TimeoutCapturingMCP:
            def call_tool(
                self,
                name: str,
                arguments: Mapping[str, Any],
                *,
                timeout_seconds: float | None = None,
            ) -> ToolResult:
                captured.append(timeout_seconds)
                return ToolResult(
                    content=[
                        {
                            "type": "text",
                            "text": json.dumps(
                                {
                                    "consumer_group": arguments["consumer_group"],
                                    "kill_key_cleared": True,
                                    "latency_key_cleared": False,
                                    "group_recognized": True,
                                    "accepted": True,
                                }
                            ),
                        }
                    ]
                )

        transition = make_remediate(_TimeoutCapturingMCP(), action_timeout_seconds=90.0)
        run = _run_state(state=IncidentState.REMEDIATING, remediation_plan=_plan_dict())
        transition(run, _now())
        assert captured == [90.0]

    def test_idempotency_key_is_deterministic_across_invocations(self) -> None:
        keys: list[str] = []

        def handler(_n: str, args: Mapping[str, Any]) -> ToolResult:
            keys.append(args["idempotency_key"])
            return ToolResult(
                content=[
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "consumer_group": args["consumer_group"],
                                "kill_key_cleared": True,
                                "kill_key": "k",
                                "latency_key_cleared": False,
                                "group_recognized": True,
                                "accepted": True,
                            }
                        ),
                    }
                ]
            )

        run = _run_state(state=IncidentState.REMEDIATING, remediation_plan=_plan_dict())
        transition = make_remediate(_FakeMCP(handler))
        transition(run, _now())
        transition(run, _now())  # same run, same args → same key
        assert len(keys) == 2
        assert keys[0] == keys[1]


def _lag_mcp(lag: int) -> _FakeMCP:
    def handler(name: str, args: Mapping[str, Any]) -> ToolResult:
        assert name == "get_consumer_lag"
        return ToolResult(
            content=[
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "consumer_group": args.get("consumer_group", "worker-dispatcher"),
                            "lag": lag,
                            "lag_known": lag is not None,
                            "source": "live",
                            "cache_key": "kafka:consumer_lag:worker-dispatcher",
                        }
                    ),
                }
            ]
        )

    return _FakeMCP(handler)


class TestVerifying:
    def _mcp(self, lag: int) -> _FakeMCP:
        return _lag_mcp(lag)

    def test_verified_transitions_to_resolved(self) -> None:
        llm = CannedLLMClient([{"verdict": "verified", "reasoning": "lag=0 after restart"}])
        transition = make_llm_verify(self._mcp(0), llm, model=_MODEL)
        run = _run_state(state=IncidentState.VERIFYING, remediation_plan=_plan_dict())
        result = transition(run, _now())
        assert result.state is IncidentState.RESOLVED

    def test_not_verified_escalates(self) -> None:
        llm = CannedLLMClient(
            [{"verdict": "not_verified", "reasoning": "lag still 50k after restart"}]
        )
        transition = make_llm_verify(self._mcp(50_000), llm, model=_MODEL)
        run = _run_state(state=IncidentState.VERIFYING, remediation_plan=_plan_dict())
        result = transition(run, _now())
        assert result.state is IncidentState.ESCALATED
        assert any("not_verified" in e.result_summary for e in result.evidence)

    def test_missing_plan_escalates(self) -> None:
        llm = CannedLLMClient([])
        transition = make_llm_verify(self._mcp(0), llm, model=_MODEL)
        run = _run_state(state=IncidentState.VERIFYING, remediation_plan=None)
        result = transition(run, _now())
        assert result.state is IncidentState.ESCALATED

    def test_verify_tool_error_escalates(self) -> None:
        def erroring(_n: str, _a: Mapping[str, Any]) -> ToolResult:
            raise MCPError(-32000, "verify probe boom")

        transition = make_llm_verify(_FakeMCP(erroring), CannedLLMClient([]), model=_MODEL)
        run = _run_state(state=IncidentState.VERIFYING, remediation_plan=_plan_dict())
        result = transition(run, _now())
        assert result.state is IncidentState.ESCALATED
        assert any("verify probe boom" in e.result_summary for e in result.evidence)


class TestPlanRoundTrip:
    def test_plan_survives_dict_round_trip(self) -> None:
        plan = RemediationPlan(
            target_hypothesis="consumer_saturation",
            action_tool="restart_consumer_group",
            action_arguments={"consumer_group": "worker-dispatcher"},
            verify_tool="get_consumer_lag",
            verify_arguments={"consumer_group": "worker-dispatcher"},
            verify_expectation="lag drops",
        )
        # Simulate the storage → load cycle used by REMEDIATING + VERIFYING.
        as_dict = plan.model_dump(mode="json")
        restored = RemediationPlan.model_validate(as_dict)
        assert restored == plan

    def test_plan_rejects_extra_fields(self) -> None:
        with pytest.raises(ValueError):
            RemediationPlan.model_validate({**_plan_dict(), "extra_field": "boom"})


class TestTheAttemptCap:
    """``MAX_REMEDIATION_ATTEMPTS`` Tier-1 attempts per incident (ADR 0056).

    A real limit, not ADR 0008's unreachability assertion: VERIFYING may hand back to
    INVESTIGATING, so PLANNING can be entered with an attempt already spent. VERIFYING
    declines the edge at the cap, and this guard is the backstop on the same number.
    """

    def test_first_attempt_transitions_normally(self) -> None:
        llm = CannedLLMClient([_plan_dict()])
        transition = make_llm_plan(llm, model=_MODEL)
        run = _run_state(
            state=IncidentState.PLANNING,
            hypotheses=(
                Hypothesis(
                    category=HypothesisCategory.CONSUMER_SATURATION,
                    name="consumer_saturation",
                    confidence=0.85,
                    reasoning="r",
                ),
            ),
            remediation_attempts=0,
            alert=_GROUP_ALERT,
        )
        result = transition(run, _now())
        assert result.state is IncidentState.REMEDIATING

    def test_the_second_attempt_is_planned(self) -> None:
        # The behaviour change ADR 0056 is: with one attempt spent and the cap at two,
        # PLANNING plans again instead of escalating on an invariant.
        llm = CannedLLMClient(
            [
                _plan_dict(
                    action_arguments={"consumer_group": "analytics"},
                    verify_arguments={"consumer_group": "analytics"},
                )
            ]
        )
        transition = make_llm_plan(llm, model=_MODEL)
        run = _run_state(
            state=IncidentState.PLANNING,
            hypotheses=(
                Hypothesis(
                    category=HypothesisCategory.CONSUMER_SATURATION,
                    name="consumer_saturation",
                    confidence=0.85,
                    reasoning="r",
                ),
            ),
            remediation_attempts=1,
            alert={**_GROUP_ALERT, "consumer_group": "analytics"},
        )
        result = transition(run, _now())
        assert result.state is IncidentState.REMEDIATING

    def test_at_the_cap_the_guard_fires_and_names_the_cap(self) -> None:
        # The LLM queue is empty: the guard must escalate before any planner tokens are
        # spent, as it did under ADR 0008. Only the number and the message moved.
        llm = CannedLLMClient([])
        transition = make_llm_plan(llm, model=_MODEL)
        run = _run_state(
            state=IncidentState.PLANNING,
            hypotheses=(
                Hypothesis(
                    category=HypothesisCategory.CONSUMER_SATURATION,
                    name="x",
                    confidence=0.9,
                    reasoning="r",
                ),
            ),
            remediation_attempts=DEFAULT_MAX_REMEDIATION_ATTEMPTS,
        )
        result = transition(run, _now())
        assert result.state is IncidentState.ESCALATED
        assert any(
            "remediation attempt cap reached (ADR 0056)" in e.result_summary
            for e in result.evidence
        )
        assert llm.calls == []  # guard fires before spending planner tokens

    def test_the_cap_is_the_configured_number(self) -> None:
        # MAX_REMEDIATION_ATTEMPTS=1 restores ADR 0008's posture exactly, which is the
        # claim .env.example makes — so the transition must read the argument, not a
        # module constant (DIVERGENCE A6).
        llm = CannedLLMClient([])
        transition = make_llm_plan(llm, model=_MODEL, max_attempts=1)
        run = _run_state(
            state=IncidentState.PLANNING,
            hypotheses=(
                Hypothesis(
                    category=HypothesisCategory.CONSUMER_SATURATION,
                    name="x",
                    confidence=0.9,
                    reasoning="r",
                ),
            ),
            remediation_attempts=1,
        )
        result = transition(run, _now())
        assert result.state is IncidentState.ESCALATED
        assert any("max_attempts=1" in e.result_summary for e in result.evidence)

    def test_successful_remediation_increments_attempts(self) -> None:
        def handler(_n: str, args: Mapping[str, Any]) -> ToolResult:
            return ToolResult(
                content=[
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "consumer_group": args["consumer_group"],
                                "kill_key_cleared": True,
                                "kill_key": "k",
                                "latency_key_cleared": False,
                                "group_recognized": True,
                                "accepted": True,
                            }
                        ),
                    }
                ]
            )

        transition = make_remediate(_FakeMCP(handler))
        run = _run_state(
            state=IncidentState.REMEDIATING,
            remediation_plan=_plan_dict(),
            remediation_attempts=0,
        )
        result = transition(run, _now())
        assert result.state is IncidentState.VERIFYING
        assert result.remediation_attempts == 1


def _two_hypotheses() -> tuple[Hypothesis, ...]:
    """A ranking with somewhere else to go: two names, both with a Tier-1 fix."""
    return (
        Hypothesis(
            category=HypothesisCategory.CONSUMER_SATURATION,
            name="consumer_saturation",
            confidence=0.85,
            reasoning="r",
        ),
        Hypothesis(
            category=HypothesisCategory.STALE_CACHE,
            name="stale_lag_sensor",
            confidence=0.4,
            reasoning="r",
        ),
    )


def _one_hypothesis() -> tuple[Hypothesis, ...]:
    """The ranking every pre-ADR-0056 scenario ends on: the attempted name, alone."""
    return _two_hypotheses()[:1]


def _attempt_marker(run: RunState) -> EvidenceEntry | None:
    return next((e for e in run.evidence if e.tool_name == ATTEMPT_FAILED_MARKER), None)


class TestTheRetryEdge:
    """VERIFYING → INVESTIGATING, and the three deterministic reasons it is declined.

    ADR 0056. Every decline that a pre-ADR-0056 run could reach leaves the escalation it
    always had, which is why the 49 committed scenarios did not move.
    """

    _NOT_VERIFIED: Final[dict[str, Any]] = {
        "verdict": "not_verified",
        "reasoning": "lag still 50k after the restart",
    }

    def _verify(self, *, max_attempts: int = DEFAULT_MAX_REMEDIATION_ATTEMPTS) -> Any:
        return make_llm_verify(
            _lag_mcp(50_000),
            CannedLLMClient([self._NOT_VERIFIED]),
            model=_MODEL,
            max_attempts=max_attempts,
        )

    def _run(self, *, hypotheses: tuple[Hypothesis, ...], attempts: int = 1) -> RunState:
        return _run_state(
            state=IncidentState.VERIFYING,
            remediation_plan=_plan_dict(),
            hypotheses=hypotheses,
            remediation_attempts=attempts,
            alert=_GROUP_ALERT,
        )

    def test_an_alternative_hypothesis_earns_a_reinvestigation(self) -> None:
        result = self._verify()(self._run(hypotheses=_two_hypotheses()), _now())
        assert result.state is IncidentState.INVESTIGATING
        marker = _attempt_marker(result)
        assert marker is not None
        assert marker.arguments["verdict"] == "not_verified"
        assert marker.arguments["action_tool"] == "restart_consumer_group"
        assert marker.arguments["verify_tool"] == "get_consumer_lag"

    def test_the_hypotheses_carry_over_and_the_spent_plan_does_not(self) -> None:
        run = self._run(hypotheses=_two_hypotheses())
        result = self._verify()(run, _now())
        assert result.hypotheses == run.hypotheses
        assert result.remediation_plan is None

    def test_budgets_are_not_reset(self) -> None:
        # The second attempt spends from the first's ledger: the only budget movement
        # across the edge is what the verify probe and judge themselves charged.
        run = self._run(hypotheses=_two_hypotheses())
        result = self._verify()(run, _now())
        assert result.budget.tool_calls_used == run.budget.tool_calls_used + 1
        assert result.remediation_attempts == run.remediation_attempts

    def test_no_alternative_escalates_exactly_as_adr_0008_did(self) -> None:
        # The byte-identical path: no marker of its own, and the judge's verdict is the
        # last entry, so the briefing reason is the one it always was.
        run = self._run(hypotheses=_one_hypothesis())
        result = self._verify()(run, _now())
        assert result.state is IncidentState.ESCALATED
        assert _attempt_marker(result) is None
        assert result.evidence[-1].tool_name == "_verify_judge"

    def test_the_cap_declines_the_edge_and_says_so(self) -> None:
        run = self._run(hypotheses=_two_hypotheses(), attempts=2)
        result = self._verify()(run, _now())
        assert result.state is IncidentState.ESCALATED
        reason = result.evidence[-1].result_summary
        assert "no fix converged after 2 Tier-1 attempts" in reason
        assert result.evidence[-1].arguments["attempted_tool"] == "restart_consumer_group"

    def test_a_budget_that_cannot_fund_a_second_attempt_declines_the_edge(self) -> None:
        run = self._run(hypotheses=_two_hypotheses())
        spent = run.budget.model_copy(update={"tool_calls_used": run.budget.max_tool_calls - 2})
        result = self._verify()(run.model_copy(update={"budget": spent}), _now())
        assert result.state is IncidentState.ESCALATED
        assert _attempt_marker(result) is None

    def test_one_attempt_configured_declines_every_edge(self) -> None:
        result = self._verify(max_attempts=1)(self._run(hypotheses=_two_hypotheses()), _now())
        assert result.state is IncidentState.ESCALATED


class TestAVerifiedStabilizerMayReinvestigate:
    """ADR 0026 survives ADR 0056: a stabilizer never resolves, retry or no retry."""

    _FENCE_PLAN: Final[dict[str, Any]] = {
        "target_hypothesis": "poison",
        "action_tool": "mark_dlq_permanent",
        "action_arguments": {
            "job_id": "eb798430-c3ad-5a44-b7d7-d15ab54d3f76",
            "reason": "payload is missing a required field",
        },
        "verify_tool": "list_dlq_messages",
        "verify_arguments": {"limit": 50},
        "verify_expectation": "the row carries human_required and a non-null fenced_at",
    }

    def _mcp(self) -> _FakeMCP:
        def handler(_n: str, _a: Mapping[str, Any]) -> ToolResult:
            return ToolResult(
                content=[
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "total": 1,
                                "items": [
                                    {
                                        "id": "eb798430-c3ad-5a44-b7d7-d15ab54d3f76",
                                        "type": "bulk_api_sync",
                                        "error_message": "SchemaValidationError: no keys",
                                        "retry_count": 3,
                                        "remediation_hint": "human_required",
                                        "created_at": "2026-07-28T10:06:00Z",
                                        "updated_at": "2026-07-28T10:06:00Z",
                                        "dead_lettered_at": None,
                                        "fenced_at": "2026-07-28T10:07:14Z",
                                        "fenced_by": "agent",
                                        "trace_id": None,
                                        "triage": None,
                                        "extra": None,
                                    }
                                ],
                            }
                        ),
                    }
                ]
            )

        return _FakeMCP(handler)

    def _verify(self) -> Any:
        return make_llm_verify(
            self._mcp(),
            CannedLLMClient([{"verdict": "verified", "reasoning": "the fence landed"}]),
            model=_MODEL,
        )

    def _run(self, hypotheses: tuple[Hypothesis, ...]) -> RunState:
        return _run_state(
            state=IncidentState.VERIFYING,
            remediation_plan=self._FENCE_PLAN,
            hypotheses=hypotheses,
            remediation_attempts=1,
        )

    def _poison(self) -> Hypothesis:
        return Hypothesis(
            category=HypothesisCategory.POISON_MESSAGE,
            name="poison",
            confidence=0.9,
            reasoning="r",
        )

    def test_it_reinvestigates_carrying_adr_0026s_sentence(self) -> None:
        result = self._verify()(self._run((self._poison(), *_two_hypotheses())), _now())
        assert result.state is IncidentState.INVESTIGATING
        marker = _attempt_marker(result)
        assert marker is not None
        assert marker.arguments["verdict"] == "verified_stabilizer"
        # The ADR 0026 wording a human is owed follows the attempt onto the ledger, so
        # reinvestigating cannot lose it.
        assert "STABILIZED, NOT RESOLVED" in marker.result_summary

    def test_with_nowhere_else_to_go_it_escalates_unchanged(self) -> None:
        result = self._verify()(self._run((self._poison(),)), _now())
        assert result.state is IncidentState.ESCALATED
        assert "STABILIZED, NOT RESOLVED" in result.evidence[-1].result_summary
        assert _attempt_marker(result) is None


class TestTheIdenticalAttemptRefusal:
    """A second attempt must be a different call, compared on the WIRED form (ADR 0056)."""

    def _restart_evidence(self, group: str) -> tuple[EvidenceEntry, ...]:
        # What ``make_remediate`` leaves on the ledger: the WIRED arguments it sent,
        # idempotency key included.
        return (
            EvidenceEntry(
                tool_name="restart_consumer_group",
                arguments={
                    "consumer_group": group,
                    "idempotency_key": build_idempotency_key(
                        "11111111-1111-1111-1111-111111111111",
                        "restart_consumer_group",
                        {"consumer_group": group},
                    ),
                },
                result_summary=json.dumps({"consumer_group": group, "kill_key_cleared": True}),
                timestamp=_now(),
            ),
        )

    def _run(self, evidence: tuple[EvidenceEntry, ...]) -> RunState:
        return _run_state(
            state=IncidentState.PLANNING,
            hypotheses=_two_hypotheses(),
            evidence=evidence,
            remediation_attempts=1,
            alert=_GROUP_ALERT,
        )

    def test_the_same_call_again_is_refused_without_a_re_ask(self) -> None:
        llm = CannedLLMClient([_plan_dict()])
        result = make_llm_plan(llm, model=_MODEL)(
            self._run(self._restart_evidence("worker-dispatcher")), _now()
        )
        assert result.state is IncidentState.ESCALATED
        assert "identical second attempt refused (ADR 0056)" in result.evidence[-1].result_summary
        # One planner call: a repeat is not a plan that a re-ask would improve.
        assert len(llm.calls) == 1
        # Nothing was executed, so nothing is recorded as attempted — SAFETY must not
        # grade a write that never happened.
        assert "attempted_tool" not in result.evidence[-1].arguments

    def test_a_different_resource_is_a_different_attempt(self) -> None:
        llm = CannedLLMClient([_plan_dict()])
        result = make_llm_plan(llm, model=_MODEL)(
            self._run(self._restart_evidence("analytics-consumer")), _now()
        )
        assert result.state is IncidentState.REMEDIATING

    def test_an_omitted_optional_is_the_same_call_on_the_wire(self) -> None:
        # ``pause_dag.ttl_seconds`` defaults to 600, so a plan that omits it and one that
        # names 600 are one call once ``wire_arguments`` has filled it in. Comparing the
        # plans as written would let this one through.
        root = "5a8e6bc0-2f6a-5a4b-9a51-0d8f6a2c4b11"
        executed = (
            EvidenceEntry(
                tool_name="pause_dag",
                arguments={
                    "root_job_id": root,
                    "ttl_seconds": 600,
                    "idempotency_key": "k" * 32,
                },
                result_summary=json.dumps({"root_job_id": root, "paused": True}),
                timestamp=_now(),
            ),
        )
        plan = _plan_dict(
            target_hypothesis="consumer_saturation",
            action_tool="pause_dag",
            action_arguments={"root_job_id": root},
            verify_tool="get_dag_state",
            verify_arguments={"job_id": root},
        )
        result = make_llm_plan(CannedLLMClient([plan]), model=_MODEL)(
            _run_state(
                state=IncidentState.PLANNING,
                hypotheses=_two_hypotheses(),
                evidence=executed,
                remediation_attempts=1,
                # The root has to be a value the platform produced, or the ADR 0030
                # argument guard refuses the plan before this one is reached.
                alert={
                    "source": "platform.dag",
                    "severity": "high",
                    "fingerprint": "chain_root_stuck",
                    "job_id": root,
                },
            ),
            _now(),
        )
        assert result.state is IncidentState.ESCALATED
        assert "identical second attempt refused (ADR 0056)" in result.evidence[-1].result_summary

    def test_a_first_attempt_is_never_refused(self) -> None:
        llm = CannedLLMClient([_plan_dict()])
        result = make_llm_plan(llm, model=_MODEL)(
            _run_state(
                state=IncidentState.PLANNING,
                hypotheses=_one_hypothesis(),
                remediation_attempts=0,
                alert=_GROUP_ALERT,
            ),
            _now(),
        )
        assert result.state is IncidentState.REMEDIATING


class TestTheAlreadyAttemptedBlock:
    """One renderer, two planner contexts, and the briefing (ADR 0056)."""

    def _run_with_attempt(self) -> RunState:
        verify = make_llm_verify(
            _lag_mcp(50_000),
            CannedLLMClient([{"verdict": "not_verified", "reasoning": "still 50k"}]),
            model=_MODEL,
        )
        return verify(
            _run_state(
                state=IncidentState.VERIFYING,
                remediation_plan=_plan_dict(),
                hypotheses=_two_hypotheses(),
                remediation_attempts=1,
                alert=_GROUP_ALERT,
            ),
            _now(),
        )

    def test_the_remediation_planner_is_shown_it_once_and_whole(self) -> None:
        run = self._run_with_attempt()
        context = _format_plan_context(run, "stale_lag_sensor")
        assert ALREADY_ATTEMPTED_HEADING in context
        marker = _attempt_marker(run)
        assert marker is not None
        assert marker.result_summary in context
        # Pulled OUT of the evidence dump, where the 200-character truncation would cut it.
        assert f"[{ATTEMPT_FAILED_MARKER}]" not in context

    def test_the_investigation_planner_is_shown_it_too(self) -> None:
        context = format_planner_context(self._run_with_attempt())
        assert ALREADY_ATTEMPTED_HEADING in context
        assert f"[{ATTEMPT_FAILED_MARKER}]" not in context

    def test_a_run_with_no_failed_attempt_renders_nothing(self) -> None:
        run = _run_state(state=IncidentState.PLANNING, hypotheses=_one_hypothesis())
        assert ALREADY_ATTEMPTED_HEADING not in _format_plan_context(run, "consumer_saturation")
        assert ALREADY_ATTEMPTED_HEADING not in format_planner_context(run)


class TestEvidenceSourcedArgs:
    """Copy, don't re-type: plan resource args must be platform-produced.

    Campaign exhibit: the planner rebuilt an alert-provided cache key without its
    ``cache:jobs:`` prefix and the platform allowlist refused it. Because the bad value
    is a SUBSTRING of the true key, matching must be exact-value, not containment.
    """

    _TRUE_KEY = "cache:jobs:worker-dispatcher:hot_set"

    def _cache_run(self) -> RunState:
        run = _run_state(
            state=IncidentState.PLANNING,
            hypotheses=(
                Hypothesis(
                    category=HypothesisCategory.STALE_CACHE,
                    name="stale-cache",
                    confidence=0.85,
                    reasoning="r",
                ),
            ),
        )
        return run.model_copy(
            update={
                "alert": {
                    "source": "platform.cache",
                    "severity": "high",
                    "cache_key": self._TRUE_KEY,
                }
            }
        )

    def _cache_plan(self, key: str) -> dict[str, Any]:
        # Verifies through get_cache_key_info on the same key. This class is about SOURCING and
        # the verify leg is scaffolding: it used to be `get_redis_health` with no arguments,
        # which ADR 0025 now refuses, and keeping it would mean these assertions passing or
        # failing for a reason that has nothing to do with sourcing.
        return _plan_dict(
            target_hypothesis="stale-cache",
            action_tool="invalidate_cache_key",
            action_arguments={"key": key},
            verify_tool="get_cache_key_info",
            verify_arguments={"key": key},
        )

    def test_verbatim_key_from_alert_passes(self) -> None:
        llm = CannedLLMClient([self._cache_plan(self._TRUE_KEY)])
        result = make_llm_plan(llm, model=_MODEL)(self._cache_run(), _now())
        assert result.state is IncidentState.REMEDIATING

    def test_retyped_key_rejected_even_as_substring_of_truth(self) -> None:
        # "worker-dispatcher:hot_set" is inside the true key — containment matching would
        # fake-green this exact campaign failure. TWO canned plans since ADR 0030: the guard
        # refuses the first offence and re-asks, so a one-plan client escalates on "no more
        # canned responses" and the assertion passes without the guard deciding anything.
        bad = self._cache_plan("worker-dispatcher:hot_set")
        llm = CannedLLMClient([bad, bad])
        result = make_llm_plan(llm, model=_MODEL)(self._cache_run(), _now())
        assert result.state is IncidentState.ESCALATED
        reasons = " ".join(e.result_summary for e in result.evidence)
        assert "not evidence-sourced" in reasons
        assert "worker-dispatcher:hot_set" in reasons
        assert "no more canned responses" not in reasons
        assert len(llm.calls) == 2

    def test_value_from_tool_result_json_passes(self) -> None:
        run = _run_state(
            state=IncidentState.PLANNING,
            hypotheses=(
                Hypothesis(
                    category=HypothesisCategory.RUNAWAY_SAGA,
                    name="runaway-saga",
                    confidence=0.9,
                    reasoning="r",
                ),
            ),
            evidence=(
                EvidenceEntry(
                    tool_name="get_dag_state",
                    arguments={},
                    result_summary='{"seed_id": "33333333-3333-3333-3333-333333333333"}',
                    timestamp=_now(),
                ),
            ),
        )
        plan = _plan_dict(
            target_hypothesis="runaway-saga",
            action_tool="pause_dag",
            action_arguments={
                "root_job_id": "33333333-3333-3333-3333-333333333333",
                "ttl_seconds": 600,
            },
            verify_tool="get_dag_state",
            verify_arguments={"job_id": "33333333-3333-3333-3333-333333333333"},
        )
        result = make_llm_plan(CannedLLMClient([plan]), model=_MODEL)(run, _now())
        assert result.state is IncidentState.REMEDIATING

    def test_invented_job_id_in_list_rejected_and_named(self) -> None:
        known = "44444444-4444-4444-4444-444444444444"
        invented = "99999999-9999-9999-9999-999999999999"
        run = _run_state(
            state=IncidentState.PLANNING,
            hypotheses=(
                Hypothesis(
                    category=HypothesisCategory.POISON_MESSAGE,
                    name="poison",
                    confidence=0.9,
                    reasoning="r",
                ),
            ),
            evidence=(
                EvidenceEntry(
                    tool_name="list_dlq_messages",
                    arguments={},
                    result_summary=f'{{"items": [{{"id": "{known}"}}]}}',
                    timestamp=_now(),
                ),
            ),
        )
        plan = _plan_dict(
            target_hypothesis="poison",
            action_tool="replay_dlq_by_ids",
            action_arguments={"job_ids": [known, invented]},
            verify_tool="list_dlq_messages",
            verify_arguments={},
        )
        # Twice, per ADR 0030 — the first offence is a re-ask.
        llm = CannedLLMClient([plan, plan])
        result = make_llm_plan(llm, model=_MODEL)(run, _now())
        assert result.state is IncidentState.ESCALATED
        reasons = " ".join(e.result_summary for e in result.evidence)
        assert invented in reasons
        assert "no more canned responses" not in reasons
        # Cut on the message's real delimiters. Splitting on "." stopped at the dot inside
        # "replay_dlq_by_ids.job_ids=", so the inspected slice could never hold a UUID and the
        # negative assertion could not fail whatever the rejection named (WO-R2-102).
        named = reasons.split("not evidence-sourced: ", 1)[1].split(". Resource names")[0]
        assert invented in named, "the rejection must name the id it refused"
        assert known not in named, "the evidence-sourced id must not be blamed"
        # ...but the id that WAS read is offered back as a candidate, which is the point of
        # ADR 0030. Blaming it and offering it are opposites; the message does exactly one.
        assert known in reasons

    def test_non_resource_fields_are_unconstrained(self) -> None:
        # category / max_replays / delay_seconds are parameters, not resource names. The
        # listing is ADR 0028's requirement rather than this guard's: without it the plan is
        # refused before this check is reached and the test would pass for the wrong reason.
        run = _run_state(
            state=IncidentState.PLANNING,
            hypotheses=(
                Hypothesis(
                    category=HypothesisCategory.POISON_MESSAGE,
                    name="poison",
                    confidence=0.9,
                    reasoning="r",
                ),
            ),
            evidence=(_dlq_listing(),),
        )
        plan = _plan_dict(
            target_hypothesis="poison",
            action_tool="replay_dlq_by_category",
            action_arguments={"category": "replay_safe", "max_replays": 20},
            verify_tool="list_dlq_messages",
            verify_arguments={},
        )
        result = make_llm_plan(CannedLLMClient([plan]), model=_MODEL)(run, _now())
        assert result.state is IncidentState.REMEDIATING

    # -- B-08: probe-argument laundering ---------------------------------
    # # get_consumer_lag accepts ANY group name, returns lag:null for unknown ones and
    # # echoes the group back in its output — so a hallucinated name used once as a probe
    # # argument must not whitelist itself for the Tier-1 action, by ingestion or by echo.

    _HALLUCINATED_GROUP = "worker-dispatchr"  # note the missing 'e'

    def _laundering_run(self, alert: dict[str, Any]) -> RunState:
        run = _run_state(
            state=IncidentState.PLANNING,
            hypotheses=(
                Hypothesis(
                    category=HypothesisCategory.CONSUMER_SATURATION,
                    name="consumer-saturation",
                    confidence=0.85,
                    reasoning="r",
                ),
            ),
            evidence=(
                EvidenceEntry(
                    tool_name="get_consumer_lag",
                    arguments={"consumer_group": self._HALLUCINATED_GROUP},
                    result_summary=(
                        '{"consumer_group":"worker-dispatchr","lag":null,"lag_known":false,"source":"unrecognized",'
                        '"cache_key":"kafka:consumer_lag:worker-dispatchr"}'
                    ),
                    timestamp=_now(),
                ),
            ),
        )
        return run.model_copy(update={"alert": alert})

    def _laundering_plan(self) -> dict[str, Any]:
        return _plan_dict(
            action_arguments={"consumer_group": self._HALLUCINATED_GROUP},
            verify_arguments={"consumer_group": self._HALLUCINATED_GROUP},
        )

    def test_probe_argument_laundering_rejected(self) -> None:
        # Alert does NOT name the group; its only occurrences in evidence
        # are the LLM-authored probe argument and the same call's echo.
        run = self._laundering_run({"source": "platform.kafka", "severity": "high"})
        # Twice, per ADR 0030 — the first offence is a re-ask.
        plan = self._laundering_plan()
        llm = CannedLLMClient([plan, plan])
        result = make_llm_plan(llm, model=_MODEL)(run, _now())
        assert result.state is IncidentState.ESCALATED
        reasons = " ".join(e.result_summary for e in result.evidence)
        assert "not evidence-sourced" in reasons
        assert self._HALLUCINATED_GROUP in reasons
        assert "no more canned responses" not in reasons
        # A consumer group has no source-row listing, so there is nothing to
        # offer back and the refusal says so rather than inventing options.
        assert "nothing to copy from yet" in reasons

    def test_probed_group_named_by_alert_passes(self) -> None:
        # Positive control: identical probe flow, but the alert names the
        # group — the alert payload remains a corpus source.
        run = self._laundering_run(
            {
                "source": "platform.kafka",
                "severity": "high",
                "consumer_group": self._HALLUCINATED_GROUP,
            }
        )
        result = make_llm_plan(CannedLLMClient([self._laundering_plan()]), model=_MODEL)(
            run, _now()
        )
        assert result.state is IncidentState.REMEDIATING

    def test_result_discovered_id_survives_later_argument_echo(self) -> None:
        # Echo exclusion is per entry, not global: a job id the platform produced in one
        # result stays corpus-eligible even after a later probe echoes it back.
        discovered = "44444444-4444-4444-4444-444444444444"
        run = _run_state(
            state=IncidentState.PLANNING,
            hypotheses=(
                Hypothesis(
                    category=HypothesisCategory.POISON_MESSAGE,
                    name="poison",
                    confidence=0.9,
                    reasoning="r",
                ),
            ),
            evidence=(
                EvidenceEntry(
                    tool_name="list_dlq_messages",
                    arguments={},
                    result_summary=f'{{"items": [{{"id": "{discovered}"}}]}}',
                    timestamp=_now(),
                ),
                EvidenceEntry(
                    tool_name="get_dag_state",
                    arguments={"job_id": discovered},
                    result_summary=f'{{"seed_id": "{discovered}"}}',
                    timestamp=_now(),
                ),
            ),
        )
        plan = _plan_dict(
            target_hypothesis="poison",
            action_tool="replay_dlq_by_ids",
            action_arguments={"job_ids": [discovered]},
            verify_tool="list_dlq_messages",
            verify_arguments={},
        )
        result = make_llm_plan(CannedLLMClient([plan]), model=_MODEL)(run, _now())
        assert result.state is IncidentState.REMEDIATING


class TestNamedResourceArgs:
    """WO-R2-15 / ADR 0024: a plan must NAME the resource on both legs.

    ``GetConsumerLagInput.consumer_group`` carries a default mirroring the platform's
    published schema, so when the planner omitted the group on the verify leg
    ``_unsourced_resource_args`` skipped the absent field, ``wire_arguments``
    default-filled it, and the run restarted one group while verifying a different
    healthy one — then reported RESOLVED on a still-broken consumer.
    """

    _REMEDIATED = "billing-consumer"
    _DEFAULT_FILLED = "worker-dispatcher"  # GetConsumerLagInput's default

    def _run(self, **overrides: Any) -> RunState:
        # The alert names the group these plans remediate, so it is both the provenance source
        # ``_unsourced_resource_args`` demands and the subject ADR 0032 compares the action
        # against. A test whose plan acts on something else passes its own ``alert=``.
        overrides.setdefault(
            "alert",
            {
                "source": "platform.kafka",
                "severity": "high",
                "consumer_group": self._REMEDIATED,
            },
        )
        return _run_state(
            state=IncidentState.PLANNING,
            hypotheses=(
                Hypothesis(
                    category=HypothesisCategory.CONSUMER_SATURATION,
                    name="consumer_saturation",
                    confidence=0.9,
                    reasoning="lag climbing",
                ),
            ),
            **overrides,
        )

    def _plan(self, **overrides: Any) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "action_arguments": {"consumer_group": self._REMEDIATED},
            "verify_arguments": {"consumer_group": self._REMEDIATED},
        }
        fields.update(overrides)
        return _plan_dict(**fields)

    # -- absence -------------------------------------------------------

    def test_omitted_verify_group_is_rejected_before_execution(self) -> None:
        # The confirmed defect: pre-fix this reached REMEDIATING and the verify probe read
        # `worker-dispatcher`, a group the incident never touched.
        result = make_llm_plan(CannedLLMClient([self._plan(verify_arguments={})]), model=_MODEL)(
            self._run(), _now()
        )

        assert result.state is IncidentState.ESCALATED
        reasons = " ".join(e.result_summary for e in result.evidence)
        assert "not named by the plan" in reasons
        assert "verify get_consumer_lag.consumer_group" in reasons

    def test_rejection_names_the_leg_so_the_trajectory_is_readable(self) -> None:
        result = make_llm_plan(CannedLLMClient([self._plan(action_arguments={})]), model=_MODEL)(
            self._run(), _now()
        )

        assert result.state is IncidentState.ESCALATED
        reasons = " ".join(e.result_summary for e in result.evidence)
        assert "action restart_consumer_group.consumer_group" in reasons

    def test_omitted_required_field_is_caught_at_plan_time_not_wire_time(self) -> None:
        # `get_dag_state.job_id` is REQUIRED, so wire_arguments would have raised — but only
        # inside VERIFYING, after the Tier-1 pause executed. Absence is a planning-time
        # rejection for every resource field, not just the default-carrying ones.
        seed = "33333333-3333-3333-3333-333333333333"
        run = _run_state(
            state=IncidentState.PLANNING,
            hypotheses=(
                Hypothesis(
                    category=HypothesisCategory.RUNAWAY_SAGA,
                    name="runaway-saga",
                    confidence=0.9,
                    reasoning="r",
                ),
            ),
            evidence=(
                EvidenceEntry(
                    tool_name="get_dag_state",
                    arguments={},
                    result_summary=f'{{"seed_id": "{seed}"}}',
                    timestamp=_now(),
                ),
            ),
        )
        plan = _plan_dict(
            target_hypothesis="runaway-saga",
            action_tool="pause_dag",
            action_arguments={"root_job_id": seed, "ttl_seconds": 600},
            verify_tool="get_dag_state",
            verify_arguments={},
        )
        result = make_llm_plan(CannedLLMClient([plan]), model=_MODEL)(run, _now())

        assert result.state is IncidentState.ESCALATED
        assert result.remediation_attempts == 0
        reasons = " ".join(e.result_summary for e in result.evidence)
        assert "verify get_dag_state.job_id" in reasons

    def test_fully_named_plan_passes(self) -> None:
        # Positive control: same shape, group named on both legs.
        result = make_llm_plan(CannedLLMClient([self._plan()]), model=_MODEL)(self._run(), _now())
        assert result.state is IncidentState.REMEDIATING

    def test_resource_free_verify_tool_needs_no_arguments(self) -> None:
        # `list_dlq_messages` names no resource, so an empty verify leg is still a legal plan:
        # the absence check reads RESOURCE_ARG_FIELDS rather than demanding arguments per se.
        #
        # This was asserted with `invalidate_cache_key` verified by `get_redis_health`, which
        # ADR 0025 now refuses, so the example moved to an action where the property still
        # holds — a bulk category replay, which names a category rather than a row. The listing
        # is ADR 0028's requirement, not this guard's, and the condition-naming alert is
        # deliberate: under a consumer alert ADR 0032 would refuse the plan for acting on
        # something the alert never reported.
        run = self._run(evidence=(_dlq_listing(),), alert=dict(_CONDITION_ALERT))
        plan = _plan_dict(
            target_hypothesis="consumer_saturation",
            action_tool="replay_dlq_by_category",
            action_arguments={"category": "replay_safe"},
            verify_tool="list_dlq_messages",
            verify_arguments={},
        )
        result = make_llm_plan(CannedLLMClient([plan]), model=_MODEL)(run, _now())
        assert result.state is IncidentState.REMEDIATING

    # -- verify targets the action ---------------------------------------

    def _run_with_both_groups_in_corpus(self) -> RunState:
        # Both names are legitimately platform-produced, so the evidence-sourcing guard has no
        # objection to either. Only the cross-leg check can catch this plan.
        return self._run(
            evidence=(
                EvidenceEntry(
                    tool_name="get_consumer_lag",
                    arguments={},
                    result_summary=(
                        f'{{"consumer_group": "{self._DEFAULT_FILLED}", "lag": 0, '
                        f'"lag_known": true, "source": "live", '
                        f'"cache_key": "kafka:consumer_lag:{self._DEFAULT_FILLED}"}}'
                    ),
                    timestamp=_now(),
                ),
            )
        )

    def test_verify_naming_a_different_group_than_the_action_is_refused(self) -> None:
        run = self._run_with_both_groups_in_corpus()
        plan = self._plan(verify_arguments={"consumer_group": self._DEFAULT_FILLED})

        # Precondition: both values ARE evidence-sourced, so this plan
        # survives the copy-don't-re-type guard.
        assert not _unsourced_resource_args(
            RemediationPlan.model_validate(plan), _evidence_value_corpus(run)
        )

        result = make_llm_plan(CannedLLMClient([plan]), model=_MODEL)(run, _now())
        assert result.state is IncidentState.ESCALATED
        reasons = " ".join(e.result_summary for e in result.evidence)
        assert "verify probe targets resource(s) the action does not" in reasons
        assert self._DEFAULT_FILLED in reasons

    def test_verify_may_name_the_action_resource_under_a_different_field(self) -> None:
        # pause_dag.root_job_id is verified through get_dag_state.job_id.
        # Values must line up; field names need not.
        seed = "33333333-3333-3333-3333-333333333333"
        run = _run_state(
            state=IncidentState.PLANNING,
            hypotheses=(
                Hypothesis(
                    category=HypothesisCategory.RUNAWAY_SAGA,
                    name="runaway-saga",
                    confidence=0.9,
                    reasoning="r",
                ),
            ),
            evidence=(
                EvidenceEntry(
                    tool_name="get_dag_state",
                    arguments={},
                    result_summary=f'{{"seed_id": "{seed}"}}',
                    timestamp=_now(),
                ),
            ),
        )
        plan = _plan_dict(
            target_hypothesis="runaway-saga",
            action_tool="pause_dag",
            action_arguments={"root_job_id": seed, "ttl_seconds": 600},
            verify_tool="get_dag_state",
            verify_arguments={"job_id": seed},
        )
        result = make_llm_plan(CannedLLMClient([plan]), model=_MODEL)(run, _now())
        assert result.state is IncidentState.REMEDIATING

    def test_verify_subset_of_a_multi_resource_action_is_allowed(self) -> None:
        # Acting on two ids and verifying one of them observes the action.
        first = "44444444-4444-4444-4444-444444444444"
        second = "55555555-5555-5555-5555-555555555555"
        run = _run_state(
            state=IncidentState.PLANNING,
            hypotheses=(
                Hypothesis(
                    category=HypothesisCategory.POISON_MESSAGE,
                    name="poison",
                    confidence=0.9,
                    reasoning="r",
                ),
            ),
            evidence=(
                EvidenceEntry(
                    tool_name="list_dlq_messages",
                    arguments={},
                    result_summary=(f'{{"items": [{{"id": "{first}"}}, {{"id": "{second}"}}]}}'),
                    timestamp=_now(),
                ),
            ),
        )
        plan = _plan_dict(
            target_hypothesis="poison",
            action_tool="replay_dlq_by_ids",
            action_arguments={"job_ids": [first, second]},
            verify_tool="get_dag_state",
            verify_arguments={"job_id": first},
        )
        result = make_llm_plan(CannedLLMClient([plan]), model=_MODEL)(run, _now())
        assert result.state is IncidentState.REMEDIATING


class TestLLMUsageAccrual:
    """C-06 + ADR 0015: every LLM call charges total token volume and dollars.

    ``client.py`` puts the system prompt behind ``cache_control``, so on a live call most
    input volume arrives on the cache counters — summing only input+output metered the
    un-cached remainder and under-enforced ``BUDGET_MAX_TOKENS`` when caching worked well.
    """

    _USAGE = CannedUsage(
        input_tokens=100,
        output_tokens=50,
        cache_creation_tokens=2_000,
        cache_read_tokens=8_000,
    )

    def _hypotheses(self) -> tuple[Hypothesis, ...]:
        return (
            Hypothesis(
                category=HypothesisCategory.CONSUMER_SATURATION,
                name="consumer_saturation",
                confidence=0.85,
                reasoning="r",
            ),
        )

    def test_planning_charges_cache_tokens_and_dollars(self) -> None:
        llm = CannedLLMClient([_plan_dict()], usage=self._USAGE)
        transition = make_llm_plan(llm, model="claude-sonnet-4-6")
        run = _run_state(
            state=IncidentState.PLANNING, hypotheses=self._hypotheses(), alert=_GROUP_ALERT
        )
        result = transition(run, _now())

        assert result.state is IncidentState.REMEDIATING
        # 100 + 50 + 2000 + 8000 — not 150.
        assert result.budget.tokens_used == 10_150
        # (100*3.00 + 50*15.00 + 2000*3.75 + 8000*0.30) / 1e6
        assert result.budget.usd_used == Decimal("0.010950")

    def test_verify_charges_cache_tokens_and_dollars_per_poll_attempt(self) -> None:
        mcp = _lag_mcp(50_000)
        llm = CannedLLMClient(
            [{"verdict": "not_verified", "reasoning": "still lagging"}] * 2,
            usage=self._USAGE,
        )
        transition = make_llm_verify(
            mcp, llm, model="claude-sonnet-4-6", probe_attempts=2, sleep=lambda _s: None
        )
        run = _run_state(state=IncidentState.VERIFYING, remediation_plan=_plan_dict())
        result = transition(run, _now())

        assert result.state is IncidentState.ESCALATED
        # Two judge calls, each charging full volume; two verify probes.
        assert result.budget.tokens_used == 20_300
        assert result.budget.tool_calls_used == 2
        assert result.budget.usd_used == Decimal("0.021900")

    def test_zero_usage_canned_client_leaves_the_ledger_untouched(self) -> None:
        """Canned scenarios stay byte-identical: no usage, no charge."""
        llm = CannedLLMClient([_plan_dict()])
        transition = make_llm_plan(llm, model="claude-sonnet-4-6")
        run = _run_state(
            state=IncidentState.PLANNING, hypotheses=self._hypotheses(), alert=_GROUP_ALERT
        )
        result = transition(run, _now())
        assert result.budget.tokens_used == 0
        assert result.budget.usd_used == Decimal("0")

    def test_usd_ceiling_trips_from_a_single_expensive_plan(self) -> None:
        """The USD dimension is reachable: one call can exhaust the ledger."""
        llm = CannedLLMClient([_plan_dict()], usage=CannedUsage(output_tokens=200_000))
        transition = make_llm_plan(llm, model="claude-sonnet-4-6")
        run = _run_state(
            state=IncidentState.PLANNING, hypotheses=self._hypotheses(), alert=_GROUP_ALERT
        )
        result = transition(run, _now())
        # 200_000 * 15.00 / 1e6 = 3.00 against the fixture's 1.00 cap.
        assert result.budget.usd_used == Decimal("3.000000")
        assert result.budget.is_exhausted


class TestRejectedPlansAreStillBilled:
    """A plan the agent throws away is a plan the platform charged for.

    The accrual used to sit after six validation branches that all return early, so the
    runs making the most LLM calls were the ones whose spend the ledger saw least of.
    ADR 0015: over-report, never under-report.
    """

    _USAGE = CannedUsage(input_tokens=100, output_tokens=50)

    def _hypotheses(self) -> tuple[Hypothesis, ...]:
        return (
            Hypothesis(
                category=HypothesisCategory.CONSUMER_SATURATION,
                name="consumer_saturation",
                confidence=0.85,
                reasoning="r",
            ),
        )

    def _plan(self, **overrides: Any) -> RunState:
        llm = CannedLLMClient([_plan_dict(**overrides)], usage=self._USAGE)
        transition = make_llm_plan(llm, model="claude-sonnet-4-6")
        run = _run_state(
            state=IncidentState.PLANNING, hypotheses=self._hypotheses(), alert=_GROUP_ALERT
        )
        return transition(run, _now())

    # Only the branches a schema-valid plan can reach. The tier/registry branches above are
    # defence in depth against registry drift: ``action_tool`` and ``verify_tool`` are
    # Literal-typed, so a bad tool name arrives as the LLMError path.
    @pytest.mark.parametrize(
        ("label", "overrides"),
        [
            ("absent resource argument", {"action_arguments": {}}),
            (
                "misdirected verify probe",
                {"verify_arguments": {"consumer_group": "some-other-group"}},
            ),
        ],
    )
    def test_every_rejection_branch_still_charges_the_call(
        self, label: str, overrides: dict[str, Any]
    ) -> None:
        result = self._plan(**overrides)
        assert result.state is IncidentState.ESCALATED, label
        assert result.budget.tokens_used == 150, label
        # 100*3.00/1e6 + 50*15.00/1e6
        assert result.budget.usd_used == Decimal("0.001050"), label

    def test_the_argument_refusal_charges_both_the_plan_and_the_re_ask(self) -> None:
        """ADR 0030's cost, stated as a number rather than left to be inferred.

        The unsourced-argument case used to sit in the parametrize above asserting ONE billed
        call; it no longer escalates on the first offence, so leaving it there would have
        asserted that a re-asked plan is free. Two calls, two charges.
        """
        bad = _plan_dict(
            action_arguments={"consumer_group": "never-mentioned"},
            verify_arguments={"consumer_group": "never-mentioned"},
        )
        llm = CannedLLMClient([bad, bad], usage=self._USAGE)
        result = make_llm_plan(llm, model="claude-sonnet-4-6")(
            _run_state(
                state=IncidentState.PLANNING, hypotheses=self._hypotheses(), alert=_GROUP_ALERT
            ),
            _now(),
        )
        assert result.state is IncidentState.ESCALATED
        assert len(llm.calls) == 2
        assert result.budget.tokens_used == 300
        assert result.budget.usd_used == Decimal("0.002100")

    def test_a_repaired_plan_charges_both_calls_and_then_proceeds(self) -> None:
        """The success path costs the same two calls. Worth pinning separately:
        a refusal that only billed when it ended in an escalation would make
        the cheap-looking outcome the one the meter under-reports."""
        llm = CannedLLMClient(
            [
                _plan_dict(
                    action_arguments={"consumer_group": "never-mentioned"},
                    verify_arguments={"consumer_group": "never-mentioned"},
                ),
                _plan_dict(),
            ],
            usage=self._USAGE,
        )
        result = make_llm_plan(llm, model="claude-sonnet-4-6")(
            _run_state(
                state=IncidentState.PLANNING, hypotheses=self._hypotheses(), alert=_GROUP_ALERT
            ),
            _now(),
        )
        assert result.state is IncidentState.REMEDIATING
        assert result.budget.tokens_used == 300
        # No tool-call budget was spent: a refusal is bookkeeping, not a probe.
        assert result.budget.tool_calls_used == 0

    def test_an_accepted_plan_is_charged_exactly_once(self) -> None:
        """Moving the accrual earlier must not double-charge the happy path."""
        result = self._plan()
        assert result.state is IncidentState.REMEDIATING
        assert result.budget.tokens_used == 150

    def test_a_failed_planner_call_charges_what_it_billed(self) -> None:
        class _Truncating:
            """Bills in full, returns nothing — a max_tokens truncation."""

            def call[T: BaseModel](
                self,
                system_prompt: str,
                user_message: str,
                output_model: type[T],
                model: str,
                max_tokens: int = 4096,
                *,
                repair_of: str | None = None,
                temperature: float | None = None,
            ) -> LLMResult[T]:
                raise LLMError(
                    "no record_output tool_use in response; stop_reason=max_tokens",
                    usage=LLMUsage(input_tokens=100, output_tokens=4096),
                )

        transition = make_llm_plan(_Truncating(), model="claude-sonnet-4-6")
        run = _run_state(
            state=IncidentState.PLANNING, hypotheses=self._hypotheses(), alert=_GROUP_ALERT
        )
        result = transition(run, _now())
        assert result.state is IncidentState.ESCALATED
        assert result.budget.tokens_used == 4196


class TestVerifyLegObservesTheAction:
    """ADR 0025: the verify leg must be able to OBSERVE what the action changed.

    ``_misdirected_verify_args`` (ADR 0024) refuses a verify leg naming a resource the
    action left alone, and is inert when the verify leg names no resource — an escape
    hatch that ADR recorded deliberately. This class is the record that it was too wide.

    The exhibit is the 2026-09-07 paid run of ``remediate_stale_cache_success``: chaos
    planted a 90-byte stale value, the agent invalidated exactly that key, and then
    verified with ``get_redis_health``, whose keyspace counters are server-wide and were
    dominated by unrelated traffic. The judge honestly answered ``not_verified`` six
    times and the agent escalated. The agent was right; the plan asked a question the
    world could not answer.
    """

    _KEY = "cache:jobs:worker-dispatcher:hot_set"

    def _cache_run(self, **overrides: Any) -> RunState:
        run = _run_state(
            state=IncidentState.PLANNING,
            hypotheses=(
                Hypothesis(
                    category=HypothesisCategory.STALE_CACHE,
                    name="stale_cache_hot_key",
                    confidence=0.9,
                    reasoning="hit rate collapsed; named key is present",
                ),
            ),
            **overrides,
        )
        return run.model_copy(
            update={
                "alert": {
                    "source": "platform.cache",
                    "severity": "critical",
                    "cache_key": self._KEY,
                }
            }
        )

    def _cache_plan(self, **overrides: Any) -> dict[str, Any]:
        base: dict[str, Any] = {
            "target_hypothesis": "stale_cache_hot_key",
            "action_tool": "invalidate_cache_key",
            "action_arguments": {"key": self._KEY},
            "verify_tool": "get_cache_key_info",
            "verify_arguments": {"key": self._KEY},
        }
        base.update(overrides)
        return _plan_dict(**base)

    # -- red before --------------------------------------------------------

    def test_the_live_runs_plan_is_refused_then_escalated(self) -> None:
        # THE regression: this exact plan, verbatim from archive 7acd2b441961's trajectory,
        # was ADMITTED before ADR 0025 and executed to an unverifiable outcome.
        live_plan = self._cache_plan(verify_tool="get_redis_health", verify_arguments={})
        llm = CannedLLMClient([live_plan, live_plan])
        result = make_llm_plan(llm, model=_MODEL)(self._cache_run(), _now())

        assert result.state is IncidentState.ESCALATED
        # Refused BEFORE execution: nothing fired, so the incident is
        # recoverable by a human rather than half-remediated.
        assert result.remediation_attempts == 0
        assert result.remediation_plan is None
        reasons = " ".join(e.result_summary for e in result.evidence)
        assert "get_cache_key_info" in reasons
        assert "cannot observe" in reasons

    def test_first_bad_plan_is_a_steer_not_a_verdict(self) -> None:
        # Refuse-and-steer: the planner is asked again, with the probe it
        # should have used named for it, and a corrected second plan wins.
        llm = CannedLLMClient(
            [
                self._cache_plan(verify_tool="get_redis_health", verify_arguments={}),
                self._cache_plan(),
            ]
        )
        result = make_llm_plan(llm, model=_MODEL)(self._cache_run(), _now())

        assert result.state is IncidentState.REMEDIATING
        assert result.remediation_plan is not None
        assert result.remediation_plan["verify_tool"] == "get_cache_key_info"
        refusals = [e for e in result.evidence if e.tool_name == "_plan_refused"]
        assert len(refusals) == 1

    def test_the_steer_reaches_the_planner_in_full(self) -> None:
        # A steer that arrives truncated is not a steer. Evidence lines are cut at 200 chars
        # and the refusal is rendered whole and separately, so assert the planner's SECOND
        # context carries the required call — including the key, which sits past that cut.
        llm = CannedLLMClient(
            [
                self._cache_plan(verify_tool="get_redis_health", verify_arguments={}),
                self._cache_plan(),
            ]
        )
        make_llm_plan(llm, model=_MODEL)(self._cache_run(), _now())

        assert len(llm.calls) == 2
        second_context = llm.calls[1][1]
        assert "REFUSED" in second_context
        assert f"get_cache_key_info(key='{self._KEY}')" in second_context

    def test_second_refusal_escalates_naming_the_gap(self) -> None:
        bad = self._cache_plan(verify_tool="get_redis_health", verify_arguments={})
        llm = CannedLLMClient([bad, bad])
        result = make_llm_plan(llm, model=_MODEL)(self._cache_run(), _now())

        assert result.state is IncidentState.ESCALATED
        reason = result.evidence[-1].result_summary
        # The briefing a human reads must say which resource went
        # unobserved and which probe would have observed it.
        assert self._KEY in reason
        assert "get_redis_health" in reason
        assert "get_cache_key_info" in reason
        assert "was NOT executed" in reason

    # -- positive control --------------------------------------------------

    def test_same_key_on_both_legs_is_admitted(self) -> None:
        llm = CannedLLMClient([self._cache_plan()])
        result = make_llm_plan(llm, model=_MODEL)(self._cache_run(), _now())

        assert result.state is IncidentState.REMEDIATING
        assert not [e for e in result.evidence if e.tool_name == "_plan_refused"]
        # Exactly one planner call: a good plan is never re-asked.
        assert len(llm.calls) == 1

    def test_restart_verified_by_lag_on_the_same_group_is_admitted(self) -> None:
        # The other value-matched pair, unchanged by this ADR.
        llm = CannedLLMClient([_plan_dict()])
        result = make_llm_plan(llm, model=_MODEL)(
            _run_state(
                state=IncidentState.PLANNING,
                alert=_GROUP_ALERT,
                hypotheses=(
                    Hypothesis(
                        category=HypothesisCategory.CONSUMER_SATURATION,
                        name="consumer_saturation",
                        confidence=0.9,
                        reasoning="lag climbing",
                    ),
                ),
            ),
            _now(),
        )
        assert result.state is IncidentState.REMEDIATING

    # -- inert controls ----------------------------------------------------

    def test_bulk_category_replay_names_no_resource_and_stays_legal(self) -> None:
        # `replay_dlq_by_category` names a category, not a row, so there is no resource to
        # demand an observation of — declared inert in the map rather than merely absent. The
        # listing is ADR 0028's requirement: without it the refusal lands before the
        # verify-target check and the control would prove nothing.
        llm = CannedLLMClient(
            [
                _plan_dict(
                    action_tool="replay_dlq_by_category",
                    action_arguments={"category": "replay_safe"},
                    verify_tool="list_dlq_messages",
                    verify_arguments={},
                )
            ]
        )
        result = make_llm_plan(llm, model=_MODEL)(
            _run_state(
                state=IncidentState.PLANNING,
                hypotheses=(
                    Hypothesis(
                        category=HypothesisCategory.POISON_MESSAGE,
                        name="poison",
                        confidence=0.9,
                        reasoning="dlq rows",
                    ),
                ),
                evidence=(_dlq_listing(),),
            ),
            _now(),
        )
        assert result.state is IncidentState.REMEDIATING

    def test_targeted_replay_verified_by_the_listing_stays_legal(self) -> None:
        # `list_dlq_messages` observes DLQ rows without being able to name one, so picking it
        # IS the requirement: there is no value to match, and demanding one would refuse every
        # correct replay plan.
        job_id = "af67d1b1-13f8-5a2c-8c44-66ec5564597d"
        run = _run_state(
            state=IncidentState.PLANNING,
            hypotheses=(
                Hypothesis(
                    category=HypothesisCategory.POISON_MESSAGE,
                    name="poison",
                    confidence=0.9,
                    reasoning="dlq rows",
                ),
            ),
            evidence=(
                EvidenceEntry(
                    tool_name="list_dlq_messages",
                    arguments={},
                    result_summary=f'{{"items": [{{"id": "{job_id}"}}]}}',
                    timestamp=_now(),
                ),
            ),
        )
        llm = CannedLLMClient(
            [
                _plan_dict(
                    action_tool="replay_dlq_by_ids",
                    action_arguments={"job_ids": [job_id]},
                    verify_tool="list_dlq_messages",
                    verify_arguments={},
                )
            ]
        )
        assert make_llm_plan(llm, model=_MODEL)(run, _now()).state is IncidentState.REMEDIATING

    def test_action_naming_no_resource_value_is_left_to_the_absence_guard(self) -> None:
        # An action whose resource field is missing entirely is ``_absent_resource_args``'
        # business. This guard must not also fire and bury the actionable message.
        llm = CannedLLMClient(
            [
                self._cache_plan(
                    action_arguments={}, verify_tool="get_redis_health", verify_arguments={}
                )
            ]
        )
        result = make_llm_plan(llm, model=_MODEL)(self._cache_run(), _now())

        assert result.state is IncidentState.ESCALATED
        reason = result.evidence[-1].result_summary
        assert "not named by the plan" in reason
        assert "cannot observe" not in reason

    def test_refusal_marker_spends_no_tool_call_budget(self) -> None:
        llm = CannedLLMClient(
            [
                self._cache_plan(verify_tool="get_redis_health", verify_arguments={}),
                self._cache_plan(),
            ]
        )
        run = self._cache_run()
        result = make_llm_plan(llm, model=_MODEL)(run, _now())

        assert result.budget.tool_calls_used == run.budget.tool_calls_used


_STUCK_ROOT = "a2412a54-65f0-5258-95ab-5c168a15df64"


def _dag_mcp(*, paused: bool, root_status: str, child_status: str) -> _FakeMCP:
    """A ``get_dag_state`` probe over a three-node chain: parent → root → child."""

    payload = {
        "seed_id": _STUCK_ROOT,
        "nodes": [
            {
                "id": _STUCK_ROOT,
                "type": "bulk_api_sync",
                "status": root_status,
                "retry_count": 3 if root_status == "dead_letter" else 0,
                "created_at": "2026-08-31T01:21:46.584955Z",
            },
            {
                "id": "dbfb7a0c-cccb-5ae7-b2ac-f386f830a9e9",
                "type": "bulk_api_sync",
                "status": "completed",
                "retry_count": 0,
                "created_at": "2026-08-31T01:21:46.584955Z",
            },
            {
                "id": "3e3bd4c1-21f6-5b84-af0c-0d921ff711ca",
                "type": "bulk_api_sync",
                "status": child_status,
                "retry_count": 0,
                "created_at": "2026-08-31T01:21:46.584955Z",
            },
        ],
        "edges": [
            {"from_id": _STUCK_ROOT, "to_id": "dbfb7a0c-cccb-5ae7-b2ac-f386f830a9e9"},
            {"from_id": "3e3bd4c1-21f6-5b84-af0c-0d921ff711ca", "to_id": _STUCK_ROOT},
        ],
        "paused": paused,
        "paused_expires_in_seconds": 600 if paused else None,
        "paused_by": _STUCK_ROOT if paused else None,
    }

    def handler(_name: str, _args: Mapping[str, Any]) -> ToolResult:
        return ToolResult(content=[{"type": "text", "text": json.dumps(payload)}], is_error=False)

    return _FakeMCP(handler)


class TestStabilizeOnlyActionsNeverResolve:
    """A verified stabilizer escalates. It does not resolve.

    The judge is asked "did the action work?", which is not "is the incident over?", and
    for ``pause_dag`` the two come apart: the platform's own description says a
    successful pause "reads as paused=true with children still in `waiting`", so a judge
    holding that expectation answers ``verified`` on a chain exactly as stuck as it was
    and stuck again when the TTL lapses. No judge prompt fixes this, because the judge is
    not wrong. ``policies.RESOLUTION_CLASS`` is the separation and this is the proof it
    reaches the one RESOLVED transition the machine has. Found by a pre-spend sweep
    before the scenario's first paid run: reachable and unexercised.
    """

    def _pause_plan(self, **overrides: Any) -> dict[str, Any]:
        base = {
            "target_hypothesis": "stuck_saga_node",
            "action_tool": "pause_dag",
            "action_arguments": {"root_job_id": _STUCK_ROOT},
            "verify_tool": "get_dag_state",
            "verify_arguments": {"job_id": _STUCK_ROOT},
            "verify_expectation": "paused=true with the children still waiting",
        }
        base.update(overrides)
        return base

    def _verified(self) -> CannedLLMClient:
        return CannedLLMClient(
            [
                {
                    "verdict": "verified",
                    "reasoning": (
                        "paused=true with paused_expires_in_seconds set and the "
                        "descendant still in waiting — the pause landed exactly "
                        "as the tool documents it."
                    ),
                }
            ]
        )

    def test_verified_pause_escalates_instead_of_resolving(self) -> None:
        transition = make_llm_verify(
            _dag_mcp(paused=True, root_status="dead_letter", child_status="waiting"),
            self._verified(),
            model=_MODEL,
        )
        run = _run_state(state=IncidentState.VERIFYING, remediation_plan=self._pause_plan())

        result = transition(run, _now())

        # Before `RESOLUTION_CLASS`, these exact inputs produced
        # `IncidentState.RESOLVED`.
        assert result.state is IncidentState.ESCALATED

    def test_the_judge_still_answered_verified(self) -> None:
        """The escalation is a policy decision, not a re-judged verdict.

        If a later change made this pass by making the judge say ``not_verified``, the class
        would go green while "a working stabilizer still escalates" had been replaced by "a
        stabilizer is graded as a failure", which is a different and wrong claim.
        """
        transition = make_llm_verify(
            _dag_mcp(paused=True, root_status="dead_letter", child_status="waiting"),
            self._verified(),
            model=_MODEL,
        )
        run = _run_state(state=IncidentState.VERIFYING, remediation_plan=self._pause_plan())

        result = transition(run, _now())

        judge_entries = [e for e in result.evidence if e.tool_name == "_verify_judge"]
        assert judge_entries, "the verify judge should still have run"
        assert judge_entries[-1].result_summary.startswith("verified:")

    def test_escalation_reason_says_stabilized_and_names_the_root(self) -> None:
        transition = make_llm_verify(
            _dag_mcp(paused=True, root_status="dead_letter", child_status="waiting"),
            self._verified(),
            model=_MODEL,
        )
        run = _run_state(state=IncidentState.VERIFYING, remediation_plan=self._pause_plan())

        result = transition(run, _now())

        reason = result.evidence[-1].result_summary
        assert "STABILIZED, NOT RESOLVED" in reason
        # The id a human has to make a decision about, not a generic noun.
        assert _STUCK_ROOT in reason
        # And why: the rationale is quoted from the policy, so the briefing
        # carries the TTL and the replay refusal rather than "escalated".
        assert "self-cleans on its TTL" in reason
        assert "refuses to replay any job inside a paused DAG" in reason

    def test_the_executed_action_reaches_the_briefing(self) -> None:
        """``attempted_action`` must survive a stabilize-only escalation.

        ``briefing.py`` reads ``attempted_tool`` off the terminal marker to tell the on-call
        which action already fired, and the writer is told never to recommend repeating it.
        This is where that matters most: a pause is holding now and expires on a timer, so
        "pause it" is the one recommendation that must not come back.
        """
        transition = make_llm_verify(
            _dag_mcp(paused=True, root_status="dead_letter", child_status="waiting"),
            self._verified(),
            model=_MODEL,
        )
        run = _run_state(state=IncidentState.VERIFYING, remediation_plan=self._pause_plan())

        result = transition(run, _now())

        marker = result.evidence[-1]
        assert marker.tool_name == "_remediation_escalate"
        assert marker.arguments["attempted_tool"] == "pause_dag"
        assert marker.arguments["attempted_arguments"] == {"root_job_id": _STUCK_ROOT}

    def test_the_action_is_not_billed_twice(self) -> None:
        """``make_remediate`` already charged the action; this leg must not.

        Only the verify probe is new spend. ``_escalate_remediation`` takes
        ``executed=True`` for the one branch where the platform acted and no entry was
        written — not this branch — and charging it would bill work already paid for and
        push ``remediation_attempts`` past the single-attempt invariant.
        """
        transition = make_llm_verify(
            _dag_mcp(paused=True, root_status="dead_letter", child_status="waiting"),
            self._verified(),
            model=_MODEL,
        )
        run = _run_state(
            state=IncidentState.VERIFYING,
            remediation_plan=self._pause_plan(),
            remediation_attempts=1,
        )

        result = transition(run, _now())

        assert result.budget.tool_calls_used == run.budget.tool_calls_used + 1
        assert result.remediation_attempts == 1

    def test_a_resolving_action_on_the_same_probe_still_resolves(self) -> None:
        """The control. The class is about the ACTION, not about DAG reads.

        Same verify tool, same probe shape, same verdict — only the action differs. Without
        it, a regression that stopped ``get_dag_state`` verifications from ever resolving
        would leave every assertion above green.
        """
        plan = self._pause_plan(
            action_tool="replay_dlq_by_ids",
            action_arguments={"job_ids": [_STUCK_ROOT]},
            verify_expectation="no node left in dead_letter and the descendant promoted",
        )
        transition = make_llm_verify(
            _dag_mcp(paused=False, root_status="completed", child_status="completed"),
            CannedLLMClient(
                [
                    {
                        "verdict": "verified",
                        "reasoning": "root out of dead_letter, held descendant promoted",
                    }
                ]
            ),
            model=_MODEL,
        )
        run = _run_state(state=IncidentState.VERIFYING, remediation_plan=plan)

        result = transition(run, _now())

        assert result.state is IncidentState.RESOLVED


_FENCED_ROW = "f030f975-974e-5ce3-aa6b-444136507d86"


def _dlq_mcp(*, hint: str = "human_required") -> _FakeMCP:
    """A ``list_dlq_messages`` verify probe that still carries the fenced row.

    Presence-with-hint is the platform's documented success reading for a mark:
    ``job.status`` stays ``dead_letter`` and only ``remediation_hint`` moves.
    """

    payload = {
        "total": 1,
        "items": [
            {
                "id": _FENCED_ROW,
                "type": "csv_upload",
                "error_message": (
                    "ValueError: invalid literal for int() with base 10: "
                    "'not-a-number' at row 15,382"
                ),
                "retry_count": 3,
                "remediation_hint": hint,
                "created_at": "2026-07-28T09:48:00Z",
            }
        ],
    }

    def handler(_name: str, _args: Mapping[str, Any]) -> ToolResult:
        return ToolResult(content=[{"type": "text", "text": json.dumps(payload)}], is_error=False)

    return _FakeMCP(handler)


class TestAVerifiedFenceEscalatesRatherThanResolving:
    """WO-R2-140: `mark_dlq_permanent` is the second stabilize-only action.

    The fence is the case where the judge is MOST obviously right and the old terminal
    state most obviously wrong: the platform says the mark "doesn't change job.status —
    the entry stays in DLQ", so a working fence reads back as the row still sitting
    there, a judge holding that expectation answers ``verified``, and the run reported
    RESOLVED on a job still dead with its source data still wrong.

    Its own class rather than a parametrize, because the two differ in what the
    escalation has to TELL the human — a pause is on a clock, a fence is permanent — and
    that text is the whole product of the class.
    """

    def _fence_plan(self, **overrides: Any) -> dict[str, Any]:
        base = {
            "target_hypothesis": "persistent_data_bug",
            "action_tool": "mark_dlq_permanent",
            "action_arguments": {
                "job_id": _FENCED_ROW,
                "reason": (
                    "Row 15,382 of the upload carries a non-numeric value in an "
                    "integer column; a replay re-reads the same byte and fails."
                ),
            },
            "verify_tool": "list_dlq_messages",
            "verify_arguments": {"remediation_hint": "human_required", "limit": 50},
            "verify_expectation": ("the fenced job_id is still listed in the human_required slice"),
        }
        base.update(overrides)
        return base

    def _verified(self) -> CannedLLMClient:
        return CannedLLMClient(
            [
                {
                    "verdict": "verified",
                    "reasoning": (
                        "the job_id is still present in the human_required-filtered "
                        "listing, which is the documented post-mark state"
                    ),
                }
            ]
        )

    def test_a_verified_fence_escalates_instead_of_resolving(self) -> None:
        transition = make_llm_verify(_dlq_mcp(), self._verified(), model=_MODEL)
        run = _run_state(state=IncidentState.VERIFYING, remediation_plan=self._fence_plan())

        result = transition(run, _now())

        # Before WO-R2-140 these exact inputs produced RESOLVED, and
        # `dlq_human_required_escalates` asserted that.
        assert result.state is IncidentState.ESCALATED

    def test_the_judge_still_answered_verified(self) -> None:
        """The escalation is the policy's decision, not a re-judged verdict.

        The pause class's anti-vacuity check: if a later change made this pass by making the
        judge say ``not_verified``, "a working fence still escalates" would have become "a
        fence is graded as a failure", and the scenario would measure a botched remediation
        rather than a deliberate handoff.
        """
        transition = make_llm_verify(_dlq_mcp(), self._verified(), model=_MODEL)
        run = _run_state(state=IncidentState.VERIFYING, remediation_plan=self._fence_plan())

        result = transition(run, _now())

        judge_entries = [e for e in result.evidence if e.tool_name == "_verify_judge"]
        assert judge_entries, "the verify judge should still have run"
        assert judge_entries[-1].result_summary.startswith("verified:")

    def test_the_reason_opens_stabilized_and_names_the_fenced_row(self) -> None:
        """`expect_briefing_contains` in the scenario grades this text.

        ``startswith``, not ``in``: the scenario claims the reason OPENS with the phrase, and
        neither a substring assertion nor `expect_briefing_contains` can say that. A reader
        who cannot tell a deliberate handoff from a failed remediation discounts both.
        """
        transition = make_llm_verify(_dlq_mcp(), self._verified(), model=_MODEL)
        run = _run_state(state=IncidentState.VERIFYING, remediation_plan=self._fence_plan())

        result = transition(run, _now())

        reason = result.evidence[-1].result_summary
        assert reason.startswith("STABILIZED, NOT RESOLVED.")
        # The row a human has to make a decision about.
        assert _FENCED_ROW in reason
        # And the policy rationale, quoted: what the fence did not do.
        assert "doesn't change job.status" in reason
        assert "a human still has to act" in reason

    def test_the_reason_does_not_promise_a_clock_the_fence_does_not_have(self) -> None:
        """A fence is not a pause, and the briefing must not read like one.

        ``_stabilized_reason``'s closing sentence is "Treat the stabilization as a clock, not
        an outcome" — true of a pause, misleading for a fence, which never expires. The
        rationale is the per-tool half of that text, pinned because an on-call told a
        permanent fence expires will go looking for the expiry.
        """
        transition = make_llm_verify(_dlq_mcp(), self._verified(), model=_MODEL)
        run = _run_state(state=IncidentState.VERIFYING, remediation_plan=self._fence_plan())

        result = transition(run, _now())

        reason = result.evidence[-1].result_summary
        assert "the fence does not expire" in reason
        assert "self-cleans on its TTL" not in reason

    def test_the_executed_fence_reaches_the_briefing(self) -> None:
        """``attempted_action`` survives, so the briefing writer sees the fence.

        Its own prompt rule forbids recommending a repeat, and "mark it permanent" must not
        come back to a human whose row is already fenced.
        """
        transition = make_llm_verify(_dlq_mcp(), self._verified(), model=_MODEL)
        run = _run_state(state=IncidentState.VERIFYING, remediation_plan=self._fence_plan())

        result = transition(run, _now())

        marker = result.evidence[-1]
        assert marker.tool_name == "_remediation_escalate"
        assert marker.arguments["attempted_tool"] == "mark_dlq_permanent"
        attempted = marker.arguments["attempted_arguments"]
        assert isinstance(attempted, Mapping)
        assert attempted["job_id"] == _FENCED_ROW

    def test_the_fence_is_not_billed_twice(self) -> None:
        transition = make_llm_verify(_dlq_mcp(), self._verified(), model=_MODEL)
        run = _run_state(
            state=IncidentState.VERIFYING,
            remediation_plan=self._fence_plan(),
            remediation_attempts=1,
        )

        result = transition(run, _now())

        assert result.budget.tool_calls_used == run.budget.tool_calls_used + 1
        assert result.remediation_attempts == 1

    def test_a_replay_verified_on_the_same_listing_still_resolves(self) -> None:
        """The control: the class is about the ACTION, not about DLQ listings.

        Same verify tool, same probe shape, same verdict — only the action differs. Without
        it, a regression that stopped every ``list_dlq_messages``-verified plan from
        resolving would leave every assertion above green while breaking three scenarios.
        """
        plan = self._fence_plan(
            action_tool="replay_dlq_by_ids",
            action_arguments={"job_ids": [_FENCED_ROW]},
            verify_expectation="the replayed id leaves the listing",
        )
        transition = make_llm_verify(
            _dlq_mcp(hint="replay_safe"),
            CannedLLMClient(
                [{"verdict": "verified", "reasoning": "the slice the replay targeted is empty"}]
            ),
            model=_MODEL,
        )
        run = _run_state(state=IncidentState.VERIFYING, remediation_plan=plan)

        result = transition(run, _now())

        assert result.state is IncidentState.RESOLVED


class TestReplayRequiresTheJobsDeadLetterRow:
    """ADR 0027: a dead-lettered job may not be replayed until its own dead-letter row is in
    the evidence.

    The gap was invisible to every existing guard: ``_unsourced_resource_args`` requires
    the job id to be platform-produced and it is (the alert carries it, ``get_dag_state``
    echoes it), and ``_unobserved_action_resource`` requires a verify probe that can
    observe it, which ``get_dag_state`` genuinely can. So a run could read a chain, see
    ``"status": "dead_letter"`` on its root and replay it having learned nothing about
    whether that was safe — because ``remediation_hint`` is not one of the node model's
    five fields. The platform's own rule: "A null hint is UNKNOWN, not replay-safe", and
    an unread row is strictly less than a null one.
    """

    _ROOT = "a2412a54-65f0-5258-95ab-5c168a15df64"
    _OTHER = "fc8d2a03-23b3-5371-9acb-46443c73baa5"

    def _saga_run(self, evidence: tuple[EvidenceEntry, ...]) -> RunState:
        run = _run_state(
            state=IncidentState.PLANNING,
            hypotheses=(
                Hypothesis(
                    category=HypothesisCategory.RUNAWAY_SAGA,
                    name="stuck_saga_node",
                    confidence=0.85,
                    reasoning="dead-lettered root holding waiting descendants",
                ),
            ),
            evidence=evidence,
        )
        return run.model_copy(
            update={
                "alert": {
                    "source": "platform.dag",
                    "severity": "critical",
                    "job_id": self._ROOT,
                }
            }
        )

    def _chain_probe(self) -> EvidenceEntry:
        """The reading that says the root STOPPED the chain and nothing more.

        Five fields per node, exactly as the platform emits them — the
        absence of ``remediation_hint`` here is the whole premise.
        """
        return EvidenceEntry(
            tool_name="get_dag_state",
            arguments={"job_id": self._ROOT},
            result_summary=(
                f'{{"seed_id":"{self._ROOT}","nodes":[{{"id":"{self._ROOT}",'
                '"type":"bulk_api_sync","status":"dead_letter","retry_count":3,'
                '"created_at":"2026-08-31T01:21:46.584955Z"}],"edges":[],'
                '"paused":false,"paused_expires_in_seconds":null,"paused_by":null}'
            ),
            timestamp=_now(),
        )

    def _dlq_probe(self, *ids: str, hint: str = "replay_safe") -> EvidenceEntry:
        items = ",".join(
            f'{{"id":"{job_id}","type":"bulk_api_sync","retry_count":3,'
            f'"remediation_hint":"{hint}","created_at":"2026-08-31T01:21:46.584955Z"}}'
            for job_id in ids
        )
        return EvidenceEntry(
            tool_name="list_dlq_messages",
            arguments={},
            result_summary=f'{{"total":{len(ids)},"items":[{items}]}}',
            timestamp=_now(),
        )

    def _replay_plan(self, *ids: str, verify_id: str | None = None) -> dict[str, Any]:
        return _plan_dict(
            target_hypothesis="stuck_saga_node",
            action_tool="replay_dlq_by_ids",
            action_arguments={"job_ids": list(ids)},
            verify_tool="get_dag_state",
            verify_arguments={"job_id": verify_id or ids[0]},
            verify_expectation="no node left in dead_letter and the descendants promoted",
        )

    # -- red before --------------------------------------------------------

    def test_the_chain_probe_alone_no_longer_admits_a_replay(self) -> None:
        # THE regression, and it is the trajectory `remediate_runaway_saga_success`
        # graded green until 2026-09-07: probe the chain, replay the root.
        plan = self._replay_plan(self._ROOT)
        llm = CannedLLMClient([plan, plan])
        result = make_llm_plan(llm, model=_MODEL)(self._saga_run((self._chain_probe(),)), _now())

        assert result.state is IncidentState.ESCALATED
        # Refused BEFORE execution — nothing was replayed.
        assert result.remediation_attempts == 0
        assert result.remediation_plan is None

    def test_the_refusal_names_the_id_and_the_read(self) -> None:
        plan = self._replay_plan(self._ROOT)
        llm = CannedLLMClient([plan, plan])
        result = make_llm_plan(llm, model=_MODEL)(self._saga_run((self._chain_probe(),)), _now())

        refusals = [e for e in result.evidence if e.tool_name == "_plan_refused_unread_row"]
        assert len(refusals) == 1
        reason = refusals[0].result_summary
        assert self._ROOT in reason
        assert "list_dlq_messages" in reason
        # The steer has to say what the read is FOR, not merely which tool.
        assert "remediation_hint" in reason
        assert "UNKNOWN, not replay-safe" in reason
        assert refusals[0].arguments["unread_ids"] == [self._ROOT]

    def test_the_steer_reaches_the_planner_in_full(self) -> None:
        # Evidence lines are cut at 200 chars in the planner context; the
        # refusal has to arrive whole or it is not a steer.
        plan = self._replay_plan(self._ROOT)
        llm = CannedLLMClient([plan, plan])
        make_llm_plan(llm, model=_MODEL)(self._saga_run((self._chain_probe(),)), _now())

        assert len(llm.calls) == 2
        second_context = llm.calls[1][1]
        assert "REFUSED" in second_context
        assert "list_dlq_messages" in second_context

    def test_second_refusal_escalates_naming_the_gap(self) -> None:
        plan = self._replay_plan(self._ROOT)
        llm = CannedLLMClient([plan, plan])
        result = make_llm_plan(llm, model=_MODEL)(self._saga_run((self._chain_probe(),)), _now())

        reason = result.evidence[-1].result_summary
        assert self._ROOT in reason
        assert "list_dlq_messages" in reason
        assert "was NOT executed" in reason

    # -- the repair the re-ask exists for ----------------------------------

    def test_dropping_the_unlisted_id_is_a_repair_the_planner_can_make(self) -> None:
        """The narrow but real case for refusing rather than escalating.

        PLANNING is one LLM call with no tool budget, so the planner cannot fetch a row it is
        missing — but it CAN drop the ids it has no row for, and a batch carrying one listed
        job and one unlisted one is repaired by replaying the first.

        WHICH id survives is the half this test used to get backwards: the batch was
        ``[_OTHER, _ROOT]`` with only ``_OTHER``'s row read, so the repair kept a
        dead-lettered row that is not the alerted chain root — a green test asserting the
        harness would admit a repair aimed at the wrong job. ADR 0032 refuses that now.
        """
        llm = CannedLLMClient(
            [
                self._replay_plan(self._ROOT, self._OTHER, verify_id=self._ROOT),
                self._replay_plan(self._ROOT),
            ]
        )
        result = make_llm_plan(llm, model=_MODEL)(
            self._saga_run((self._chain_probe(), self._dlq_probe(self._ROOT))), _now()
        )

        assert result.state is IncidentState.REMEDIATING
        assert result.remediation_plan is not None
        replanned = RemediationPlan.model_validate(result.remediation_plan)
        assert replanned.action_arguments["job_ids"] == [self._ROOT]

    # -- positive control --------------------------------------------------

    def test_a_replay_whose_row_was_read_is_admitted(self) -> None:
        llm = CannedLLMClient([self._replay_plan(self._ROOT)])
        result = make_llm_plan(llm, model=_MODEL)(
            self._saga_run((self._chain_probe(), self._dlq_probe(self._ROOT))), _now()
        )

        assert result.state is IncidentState.REMEDIATING
        assert not [e for e in result.evidence if e.tool_name == "_plan_refused_unread_row"]
        # A good plan is never re-asked.
        assert len(llm.calls) == 1

    def test_the_row_counts_whatever_its_hint_says(self) -> None:
        """The guard demands the READ, never a particular verdict.

        ``wait_and_replay`` warrants a deferred replay and ``replay_safe`` an immediate one,
        and choosing between them is the planner's job: a structural rule admitting one value
        would make that decision and refuse the correct plan for every such scenario.
        """
        llm = CannedLLMClient([self._replay_plan(self._ROOT)])
        result = make_llm_plan(llm, model=_MODEL)(
            self._saga_run(
                (self._chain_probe(), self._dlq_probe(self._ROOT, hint="human_required"))
            ),
            _now(),
        )

        assert result.state is IncidentState.REMEDIATING

    def test_an_id_echoed_by_the_alert_is_not_a_row(self) -> None:
        """The precise hole: ``_unsourced_resource_args`` is satisfied by the alert's own
        job_id, so before this guard the evidence corpus said "the platform produced this
        string" and nothing said "somebody read this job's classification".
        """
        from incident_commander.agent.remediation import (
            _evidence_value_corpus,
            _unread_action_rows,
        )

        run = self._saga_run((self._chain_probe(),))
        plan = RemediationPlan.model_validate(self._replay_plan(self._ROOT))
        assert self._ROOT in _evidence_value_corpus(run)
        assert _unread_action_rows(plan, run) is not None

    # -- inertness ---------------------------------------------------------

    def test_a_category_replay_is_inert(self) -> None:
        """Declared inert HERE: the tool names a filter, not ids, so there is no row for THIS
        guard to look up.

        Until 2026-09-07 this also passed with ``evidence=()``, which was the whole of
        WO-R2-143: "no row to look up" was read as "no read required", so a category replay
        by a run that had listed nothing reached REMEDIATING. The listing below is what
        ADR 0028's sibling guard now requires; this test's claim is narrower than it looks —
        the by-id marker must not appear.
        """
        llm = CannedLLMClient(
            [
                _plan_dict(
                    target_hypothesis="transient_dependency_failure",
                    action_tool="replay_dlq_by_category",
                    action_arguments={"category": "replay_safe"},
                    verify_tool="list_dlq_messages",
                    verify_arguments={},
                    verify_expectation="the replay_safe rows leave the listing",
                )
            ]
        )
        run = _run_state(
            state=IncidentState.PLANNING,
            hypotheses=(
                Hypothesis(
                    category=HypothesisCategory.POISON_MESSAGE,
                    name="transient_dependency_failure",
                    confidence=0.85,
                    reasoning="retryable DLQ backlog",
                ),
            ),
            evidence=(self._dlq_probe(self._OTHER),),
        )
        result = make_llm_plan(llm, model=_MODEL)(run, _now())

        assert result.state is IncidentState.REMEDIATING
        assert not [e for e in result.evidence if e.tool_name == "_plan_refused_unread_row"]
        assert len(llm.calls) == 1

    def test_a_consumer_restart_is_inert(self) -> None:
        # No listing classifies a consumer group, so the default plan — which
        # reads no DLQ at all — must still be admitted.
        llm = CannedLLMClient([_plan_dict()])
        run = _run_state(
            state=IncidentState.PLANNING,
            hypotheses=(
                Hypothesis(
                    category=HypothesisCategory.CONSUMER_SATURATION,
                    name="consumer_saturation",
                    confidence=0.9,
                    reasoning="lag climbing on the alerted group",
                ),
            ),
            alert=_GROUP_ALERT,
        )
        result = make_llm_plan(llm, model=_MODEL)(run, _now())

        assert result.state is IncidentState.REMEDIATING
        assert len(llm.calls) == 1


class TestBulkReplayRequiresTheListing:
    """ADR 0028: a replay that names a CATEGORY may not run until some listing in evidence
    covered the slice the platform will expand it to.

    The other half of ADR 0027's rule, against the other call shape: that guard matches
    the action's resource arguments against ids a listing returned, and
    ``replay_dlq_by_category`` has none, so it was inert by construction. The rows a
    category replay touches are chosen by the PLATFORM at execution time from whatever
    the queue holds, so "which rows did I just replay, and how many" is unanswerable
    unless the agent looked first.
    """

    _SAFE = "fc8d2a03-23b3-5371-9acb-46443c73baa5"

    def _run(self, evidence: tuple[EvidenceEntry, ...]) -> RunState:
        return _run_state(
            state=IncidentState.PLANNING,
            hypotheses=(
                Hypothesis(
                    category=HypothesisCategory.POISON_MESSAGE,
                    name="transient_dependency_failure",
                    confidence=0.85,
                    reasoning="retryable DLQ backlog",
                ),
            ),
            evidence=evidence,
        )

    def _listing(self, **arguments: Any) -> EvidenceEntry:
        """One `list_dlq_messages` reading, recorded as the loop records it.

        ``arguments`` are the WIRED ones: ``wire_arguments`` fills every unset optional with
        an explicit ``null``, which is why an unfiltered read reaches the ledger as
        ``remediation_hint: None`` and why the guard treats the two the same.
        """
        wired: dict[str, Any] = {
            "job_type": None,
            "remediation_hint": None,
            "limit": 50,
            "offset": 0,
        }
        wired.update(arguments)
        return EvidenceEntry(
            tool_name="list_dlq_messages",
            arguments=wired,
            result_summary=(
                f'{{"total":1,"items":[{{"id":"{self._SAFE}","type":"bulk_api_sync",'
                '"retry_count":3,"remediation_hint":"replay_safe",'
                '"created_at":"2026-08-31T01:21:46.584955Z"}]}'
            ),
            timestamp=_now(),
        )

    def _category_plan(self, **action_arguments: Any) -> dict[str, Any]:
        return _plan_dict(
            target_hypothesis="transient_dependency_failure",
            action_tool="replay_dlq_by_category",
            action_arguments=action_arguments or {"category": "replay_safe"},
            verify_tool="list_dlq_messages",
            verify_arguments={},
            verify_expectation="the replayed rows leave the listing",
        )

    # -- red before --------------------------------------------------------

    def test_a_category_replay_with_no_listing_is_refused(self) -> None:
        """THE regression. Until ADR 0028 this exact plan — a bulk replay by a run that had read
        nothing — reached REMEDIATING, and a test asserted that it did.
        """
        plan = self._category_plan()
        llm = CannedLLMClient([plan, plan])
        result = make_llm_plan(llm, model=_MODEL)(self._run(()), _now())

        assert result.state is IncidentState.ESCALATED
        # Refused BEFORE execution — nothing was replayed.
        assert result.remediation_attempts == 0
        assert result.remediation_plan is None

    def test_the_refusal_names_the_read_and_why_a_filter_is_not_rows(self) -> None:
        plan = self._category_plan()
        llm = CannedLLMClient([plan, plan])
        result = make_llm_plan(llm, model=_MODEL)(self._run(()), _now())

        refusals = [e for e in result.evidence if e.tool_name == "_plan_refused_unlisted_category"]
        assert len(refusals) == 1
        reason = refusals[0].result_summary
        # The steer is a call the planner can copy, not a shape to fill in.
        assert "list_dlq_messages(remediation_hint='replay_safe')" in reason
        assert "names a FILTER, not rows" in reason
        assert "no list_dlq_messages reading at all" in reason
        # And it says what the read is FOR.
        assert "confirm each one is a row you mean to replay" in reason
        assert "UNKNOWN, not replay-safe" in reason
        assert refusals[0].arguments["readings_in_evidence"] == []
        assert refusals[0].arguments["required_tool"] == "list_dlq_messages"

    def test_reading_one_slice_does_not_licence_sweeping_another(self) -> None:
        """The case with a real failure mode behind it, and why coverage is judged per scope.

        A run that filtered to `replay_safe` has read nothing about the `wait_and_replay`
        rows — not their error texts, not how many — and those are precisely the rows whose
        dependency has not recovered. Two scenarios forbid that category outright; this
        refuses it one layer earlier and for every scenario, graded or not.
        """
        plan = self._category_plan(category="wait_and_replay")
        llm = CannedLLMClient([plan, plan])
        result = make_llm_plan(llm, model=_MODEL)(
            self._run((self._listing(remediation_hint="replay_safe"),)), _now()
        )

        assert result.state is IncidentState.ESCALATED
        reason = result.evidence[-1].result_summary
        assert "list_dlq_messages(remediation_hint='replay_safe')" in reason
        assert "cover a different slice" in reason
        assert "was NOT executed" in reason

    def test_a_listing_narrower_by_job_type_does_not_cover_a_wider_replay(self) -> None:
        """The second scope, and not decoration: a reading of one job type's rows says nothing
        about the other three the same category holds, and the action names no `job_type` —
        so the read is strictly narrower than the sweep.
        """
        plan = self._category_plan(category="replay_safe")
        llm = CannedLLMClient([plan, plan])
        result = make_llm_plan(llm, model=_MODEL)(
            self._run((self._listing(remediation_hint="replay_safe", job_type="csv_upload"),)),
            _now(),
        )

        assert result.state is IncidentState.ESCALATED

    def test_a_category_replay_missing_its_category_demands_an_unfiltered_read(
        self,
    ) -> None:
        """Fail-closed on the invalid call. `category` is required, so `wire_arguments` would
        reject this later anyway — but of the two readings available, "spans every category"
        is the safe one.
        """
        plan = self._category_plan(max_replays=50)
        llm = CannedLLMClient([plan, plan])
        result = make_llm_plan(llm, model=_MODEL)(
            self._run((self._listing(remediation_hint="replay_safe"),)), _now()
        )

        assert result.state is IncidentState.ESCALATED

    def test_the_steer_reaches_the_planner_in_full(self) -> None:
        # Evidence lines are cut at 200 chars in the planner context, so a refusal under a
        # marker missing from _PLAN_REFUSAL_MARKERS arrives cut mid-sentence.
        plan = self._category_plan()
        llm = CannedLLMClient([plan, plan])
        make_llm_plan(llm, model=_MODEL)(self._run(()), _now())

        assert len(llm.calls) == 2
        second_context = llm.calls[1][1]
        assert "REFUSED" in second_context
        assert "confirm each one is a row you mean to replay" in second_context

    # -- the repair the re-ask exists for ----------------------------------

    def test_re_planning_onto_the_slice_that_was_read_is_admitted(self) -> None:
        """Why this refuses rather than escalating. PLANNING is one LLM call with no tool
        budget, so the planner cannot fetch a listing it is missing — but when the run HAS
        listed a slice and the plan reached for a different one, the refusal names the slices
        in evidence and the planner can aim at one.
        """
        llm = CannedLLMClient(
            [
                self._category_plan(category="wait_and_replay"),
                self._category_plan(category="replay_safe"),
            ]
        )
        result = make_llm_plan(llm, model=_MODEL)(
            self._run((self._listing(remediation_hint="replay_safe"),)), _now()
        )

        assert result.state is IncidentState.REMEDIATING
        assert result.remediation_plan is not None
        replanned = RemediationPlan.model_validate(result.remediation_plan)
        assert replanned.action_arguments["category"] == "replay_safe"

    # -- positive controls -------------------------------------------------

    def test_an_unfiltered_listing_covers_every_slice(self) -> None:
        """The trajectory every canned DLQ flow in the suite already runs:
        the investigation planner probes `list_dlq_messages` unfiltered, then
        the remediation planner picks a category."""
        llm = CannedLLMClient([self._category_plan()])
        result = make_llm_plan(llm, model=_MODEL)(self._run((self._listing(),)), _now())

        assert result.state is IncidentState.REMEDIATING
        assert not [e for e in result.evidence if e.tool_name == "_plan_refused_unlisted_category"]
        # A good plan is never re-asked.
        assert len(llm.calls) == 1

    def test_a_listing_filtered_to_the_same_slice_covers_it(self) -> None:
        """The other legitimate way to have looked: filter the page to the
        category you intend to act on. Refusing this would make the guard a
        rule about HOW to read rather than about having read."""
        llm = CannedLLMClient([self._category_plan()])
        result = make_llm_plan(llm, model=_MODEL)(
            self._run((self._listing(remediation_hint="replay_safe"),)), _now()
        )

        assert result.state is IncidentState.REMEDIATING
        assert len(llm.calls) == 1

    def test_an_empty_listing_still_covers_the_slice_it_read(self) -> None:
        """Deliberately NOT "the listing must have returned a row in that category". A category
        that emptied between the read and the plan makes the replay a no-op, and refusing it
        would red a correct, cautious run for the world's timing.
        """
        empty = EvidenceEntry(
            tool_name="list_dlq_messages",
            arguments={"job_type": None, "remediation_hint": None, "limit": 50, "offset": 0},
            result_summary='{"total":0,"items":[]}',
            timestamp=_now(),
        )
        llm = CannedLLMClient([self._category_plan()])
        result = make_llm_plan(llm, model=_MODEL)(self._run((empty,)), _now())

        assert result.state is IncidentState.REMEDIATING

    def test_an_entry_that_is_not_a_reading_does_not_count(self) -> None:
        """Coverage needs a reading, not a name. The rows field has to be
        there and be a list — belt to the braces of the investigation loop,
        which escalates on `is_error` before an entry is ever written."""
        not_a_reading = EvidenceEntry(
            tool_name="list_dlq_messages",
            arguments={"job_type": None, "remediation_hint": None, "limit": 50, "offset": 0},
            result_summary="tool reported is_error=True",
            timestamp=_now(),
        )
        plan = self._category_plan()
        llm = CannedLLMClient([plan, plan])
        result = make_llm_plan(llm, model=_MODEL)(self._run((not_a_reading,)), _now())

        assert result.state is IncidentState.ESCALATED

    # -- inertness ---------------------------------------------------------

    def test_a_by_id_replay_is_inert_here(self) -> None:
        """The two read-before-act guards are non-inert over DISJOINT tool sets.

        A by-id replay's question is "is that row in evidence?", and asking a coverage
        question of it too would refuse a correct replay whose listing was filtered to a
        different hint than the row's — a legitimate way to have read a row.
        """
        from incident_commander.agent.remediation import _unlisted_action_scope

        plan = RemediationPlan.model_validate(
            _plan_dict(
                target_hypothesis="transient_dependency_failure",
                action_tool="replay_dlq_by_ids",
                action_arguments={"job_ids": [self._SAFE]},
                verify_tool="list_dlq_messages",
                verify_arguments={},
                verify_expectation="the row leaves the listing",
            )
        )
        assert _unlisted_action_scope(plan, self._run(())) is None

    def test_a_consumer_restart_is_inert(self) -> None:
        # No listing enumerates consumer groups, so the default plan — which
        # reads no DLQ at all — must still be admitted, unchanged.
        llm = CannedLLMClient([_plan_dict()])
        run = _run_state(
            state=IncidentState.PLANNING,
            hypotheses=(
                Hypothesis(
                    category=HypothesisCategory.CONSUMER_SATURATION,
                    name="consumer_saturation",
                    confidence=0.9,
                    reasoning="lag climbing on the alerted group",
                ),
            ),
            alert=_GROUP_ALERT,
        )
        result = make_llm_plan(llm, model=_MODEL)(run, _now())

        assert result.state is IncidentState.REMEDIATING
        assert len(llm.calls) == 1

    def test_the_bulk_sweep_is_covered_only_by_an_unfiltered_read(self) -> None:
        """`replay_dlq_messages` replays uncategorised rows too, so no filtered reading can have
        covered it. Declaring it here does not make it permissible — every DLQ scenario still
        forbids it — it says what the agent would have had to read.
        """
        from incident_commander.agent.remediation import _unlisted_action_scope

        plan = RemediationPlan.model_validate(
            _plan_dict(
                target_hypothesis="transient_dependency_failure",
                action_tool="replay_dlq_messages",
                action_arguments={},
                verify_tool="list_dlq_messages",
                verify_arguments={},
                verify_expectation="the queue drains",
            )
        )
        filtered = self._run((self._listing(remediation_hint="replay_safe"),))
        assert _unlisted_action_scope(plan, filtered) is not None
        assert _unlisted_action_scope(plan, self._run((self._listing(),))) is None


class TestAMangledIdIsRePlannedWithCandidates:
    """ADR 0030: a plan refused for a mis-transcribed resource id gets one re-ask carrying
    the ids the run actually read.

    Live exhibit `5c8895771fbd` (2026-09-07): the planner did everything
    `dlq_wait_and_replay_success` asks — listed the DLQ, filtered `wait_and_replay`,
    grouped the rows by dependency, derived a 300 s delay and wrote the derivation into
    `action_rationale`, picked a verify leg expecting the rows to REMAIN listed — and
    then emitted the second job id with its trailing blocks zero-filled. The run escalated
    after ONE planner call, on "resource argument(s) not evidence-sourced", and graded red
    on outcome, action and evidence. Nothing about its understanding was wrong; its own
    rationale, briefing and the judge's reasoning all quote the id correctly. It was a
    copying slip, which is the one planner error a re-ask carrying the candidate list can
    repair. What must NOT happen is the HARNESS fixing it: substituting the nearest
    evidence id would be the agent choosing which job to replay from a guess about intent.
    """

    _RATE_LIMITED = "af67d1b1-13f8-5a2c-8c44-66ec5564597d"
    _SMTP = "97d91272-9774-5b8e-980b-f0d2fa6ed619"
    _MANGLED = "97d91272-0000-0000-0000-000000000000"

    def _run(self) -> RunState:
        """The world as the live run had it: one filtered listing, two rows."""
        run = _run_state(
            state=IncidentState.PLANNING,
            hypotheses=(
                Hypothesis(
                    category=HypothesisCategory.POISON_MESSAGE,
                    name="dlq-bulk-api-sync-wait-and-replay",
                    confidence=0.92,
                    reasoning="two wait_and_replay rows against two different dependencies",
                ),
            ),
            evidence=(
                EvidenceEntry(
                    tool_name="list_dlq_messages",
                    arguments={
                        "job_type": None,
                        "remediation_hint": "wait_and_replay",
                        "limit": 50,
                        "offset": 0,
                    },
                    result_summary=json.dumps(
                        {
                            "total": 2,
                            "items": [
                                {
                                    "id": self._RATE_LIMITED,
                                    "type": "bulk_api_sync",
                                    "error_message": (
                                        "RateLimited: partner-api.internal answered 429 "
                                        "(retry-after: 120s)"
                                    ),
                                    "remediation_hint": "wait_and_replay",
                                },
                                {
                                    "id": self._SMTP,
                                    "type": "bulk_api_sync",
                                    "error_message": (
                                        "send_email downstream call failed: "
                                        "ConnectionRefusedError('smtp.mailer.internal:587')"
                                    ),
                                    "remediation_hint": "wait_and_replay",
                                },
                            ],
                        }
                    ),
                    timestamp=_now(),
                ),
            ),
        )
        return run.model_copy(
            update={
                "alert": {
                    "source": "platform.dlq",
                    "severity": "critical",
                    "fingerprint": "dlq_depth_warning_wait_replay",
                }
            }
        )

    def _plan(self, *job_ids: str) -> dict[str, Any]:
        """The live plan, verbatim apart from which ids it carries."""
        return _plan_dict(
            target_hypothesis="dlq-bulk-api-sync-wait-and-replay",
            action_tool="replay_dlq_by_ids",
            action_arguments={"job_ids": list(job_ids), "delay_seconds": 300},
            verify_tool="list_dlq_messages",
            verify_arguments={"remediation_hint": "wait_and_replay"},
            verify_expectation="both entries remain listed with a future execute_at",
            action_rationale="SMTP row states no wait; dependency-down default of 300s applies.",
        )

    # -- red before --------------------------------------------------------

    def test_the_live_plan_is_no_longer_a_one_call_escalation(self) -> None:
        """THE regression. Before ADR 0030 this exact plan ended the run after
        a single planner call; now the planner is asked again and a corrected
        second plan is admitted."""
        llm = CannedLLMClient(
            [
                self._plan(self._RATE_LIMITED, self._MANGLED),
                self._plan(self._RATE_LIMITED, self._SMTP),
            ]
        )
        result = make_llm_plan(llm, model=_MODEL)(self._run(), _now())

        assert len(llm.calls) == 2
        assert result.state is IncidentState.REMEDIATING
        assert result.remediation_plan is not None
        replanned = RemediationPlan.model_validate(result.remediation_plan)
        # Both rows, which is what the scenario grades (`scheduled` sums to 2).
        assert replanned.action_arguments["job_ids"] == [self._RATE_LIMITED, self._SMTP]
        # And the rest of the plan the model got right is untouched.
        assert replanned.action_arguments["delay_seconds"] == 300

    def test_the_refusal_enumerates_the_ids_the_run_read(self) -> None:
        llm = CannedLLMClient([self._plan(self._RATE_LIMITED, self._MANGLED)] * 2)
        result = make_llm_plan(llm, model=_MODEL)(self._run(), _now())

        refusals = [e for e in result.evidence if e.tool_name == "_plan_refused_argument"]
        assert len(refusals) == 1
        reason = refusals[0].result_summary
        assert self._MANGLED in reason
        assert self._SMTP in reason
        assert self._RATE_LIMITED in reason
        # Sorted, so the offer reads the same on every run — and it is the rows that were READ,
        # so nothing else in the listing is offered as an id.
        assert refusals[0].arguments["candidates"] == [self._SMTP, self._RATE_LIMITED]

    def test_the_refusal_names_the_single_near_match(self) -> None:
        """`97d91272-0000-…` shares its first block with exactly one row that
        was read, and with neither of the ids the OTHER row carries. Saying so
        is the difference between a list to search and an answer to check."""
        llm = CannedLLMClient([self._plan(self._RATE_LIMITED, self._MANGLED)] * 2)
        result = make_llm_plan(llm, model=_MODEL)(self._run(), _now())

        reason = next(
            e.result_summary for e in result.evidence if e.tool_name == "_plan_refused_argument"
        )
        assert f"did you mean {self._SMTP}?" in reason

    def test_the_steer_reaches_the_planner_in_full(self) -> None:
        """Evidence lines are cut at 200 characters and this refusal is far longer, so the
        marker has to be in ``_PLAN_REFUSAL_MARKERS`` or the candidate list arrives
        truncated — which is to say, absent exactly where it matters.
        """
        llm = CannedLLMClient(
            [
                self._plan(self._RATE_LIMITED, self._MANGLED),
                self._plan(self._RATE_LIMITED, self._SMTP),
            ]
        )
        make_llm_plan(llm, model=_MODEL)(self._run(), _now())

        second_context = llm.calls[1][1]
        assert "REFUSED" in second_context
        assert self._SMTP in second_context
        assert f"did you mean {self._SMTP}?" in second_context

    def test_a_second_mangled_plan_escalates_naming_the_mismatch(self) -> None:
        llm = CannedLLMClient([self._plan(self._RATE_LIMITED, self._MANGLED)] * 2)
        result = make_llm_plan(llm, model=_MODEL)(self._run(), _now())

        assert result.state is IncidentState.ESCALATED
        assert result.remediation_attempts == 0
        assert result.remediation_plan is None
        reason = result.evidence[-1].result_summary
        assert self._MANGLED in reason
        assert self._SMTP in reason
        assert "was NOT executed" in reason

    # -- the harness offers, it never corrects ------------------------------

    def test_the_harness_never_substitutes_the_candidate_itself(self) -> None:
        """The one behaviour this design refuses. A planner that repeats the mangled id gets an
        escalation, not a silently repaired replay: the run must not replay the real row on
        the strength of the harness deciding that is what the mangled id meant.
        """
        llm = CannedLLMClient([self._plan(self._RATE_LIMITED, self._MANGLED)] * 2)
        result = make_llm_plan(llm, model=_MODEL)(self._run(), _now())

        assert result.state is IncidentState.ESCALATED
        assert result.remediation_plan is None
        # No plan was stored at all, so nothing downstream can read a
        # corrected id off the run state either.
        assert not [e for e in result.evidence if e.tool_name == "_planner_plan"]

    def test_no_tool_call_budget_is_spent_on_the_re_ask(self) -> None:
        """A refusal is bookkeeping, not a probe — it costs planner tokens and
        nothing on the tool-call ledger, so the re-ask cannot be what pushes a
        run past the budget that pays for action+verify."""
        llm = CannedLLMClient(
            [
                self._plan(self._RATE_LIMITED, self._MANGLED),
                self._plan(self._RATE_LIMITED, self._SMTP),
            ]
        )
        result = make_llm_plan(llm, model=_MODEL)(self._run(), _now())

        assert result.budget.tool_calls_used == 0

    def test_the_refusal_is_not_a_tool_in_the_graded_trail(self) -> None:
        """Underscore-prefixed, so the grader's called-tools set and the briefing's trail both
        skip it. A refusal that graded as a tool call would turn this fix into a new way to
        fail the budget and evidence dimensions.
        """
        llm = CannedLLMClient([self._plan(self._RATE_LIMITED, self._MANGLED)] * 2)
        result = make_llm_plan(llm, model=_MODEL)(self._run(), _now())

        called = {e.tool_name for e in result.evidence if not e.tool_name.startswith("_")}
        assert called == {"list_dlq_messages"}

    # -- the did-you-mean is a claim, and it is withheld when it cannot be made

    def test_no_near_match_is_offered_when_two_candidates_share_the_prefix(self) -> None:
        """Two rows whose ids share the rejected value's first block cannot be
        told apart by a prefix, so the harness does not guess between them. The
        enumeration still goes out; only the claim is withheld."""
        twin = "97d91272-1111-5b8e-980b-f0d2fa6ed619"
        run = self._run()
        listing = run.evidence[0]
        rows = json.loads(listing.result_summary)
        rows["items"].append({"id": twin, "remediation_hint": "wait_and_replay"})
        rows["total"] = 3
        run = run.model_copy(
            update={"evidence": (listing.model_copy(update={"result_summary": json.dumps(rows)}),)}
        )
        llm = CannedLLMClient([self._plan(self._MANGLED)] * 2)
        result = make_llm_plan(llm, model=_MODEL)(run, _now())

        reason = next(
            e.result_summary for e in result.evidence if e.tool_name == "_plan_refused_argument"
        )
        assert "did you mean" not in reason
        assert self._SMTP in reason
        assert twin in reason

    def test_a_wholly_invented_id_gets_the_list_but_no_near_match(self) -> None:
        invented = "deadbeef-0000-0000-0000-000000000000"
        llm = CannedLLMClient([self._plan(invented)] * 2)
        result = make_llm_plan(llm, model=_MODEL)(self._run(), _now())

        reason = next(
            e.result_summary for e in result.evidence if e.tool_name == "_plan_refused_argument"
        )
        assert "did you mean" not in reason
        assert self._SMTP in reason

    # -- schema hardening (the cheap half) ---------------------------------

    def test_a_truncated_id_is_refused_for_its_SHAPE_not_its_provenance(self) -> None:
        """The targeted message ADR 0030's schema half buys. `_unsourced` would have caught this
        too, but "the platform never produced that string" leaves a truncated id looking like
        an invented one; "that is not the shape of a job id" points at the wrong characters.
        """
        llm = CannedLLMClient([self._plan("97d91272-9774-5b8e")] * 2)
        result = make_llm_plan(llm, model=_MODEL)(self._run(), _now())

        refusals = [e for e in result.evidence if e.tool_name == "_plan_refused_argument"]
        assert refusals[0].arguments["kind"] == "malformed"
        assert "8-4-4-4-12 hexadecimal" in refusals[0].result_summary
        # Still offered the real ids to copy from.
        assert self._SMTP in refusals[0].result_summary

    def test_the_zero_filled_id_falls_through_to_the_evidence_check(self) -> None:
        """Stated so nobody re-reads the schema half as a fix for the live run. The zero-filled
        id IS canonical 8-4-4-4-12 hex, so no regex can reject it and provenance is what
        catches it. The two checks cover the field between them and neither would alone.
        """
        from incident_commander.agent.remediation import _malformed_resource_args

        plan = RemediationPlan.model_validate(self._plan(self._RATE_LIMITED, self._MANGLED))
        assert _malformed_resource_args(plan) == []

        llm = CannedLLMClient([self._plan(self._RATE_LIMITED, self._MANGLED)] * 2)
        result = make_llm_plan(llm, model=_MODEL)(self._run(), _now())
        refusals = [e for e in result.evidence if e.tool_name == "_plan_refused_argument"]
        assert refusals[0].arguments["kind"] == "unsourced"

    def test_a_non_uuid_resource_field_is_never_shape_checked(self) -> None:
        """A cache key and a trace id have no canonical form — the platform
        accepts any string of the right length — so the shape check must be
        inert on them or every legitimate `invalidate_cache_key` plan dies."""
        from incident_commander.agent.remediation import _malformed_resource_args

        plan = RemediationPlan.model_validate(
            _plan_dict(
                target_hypothesis="stale-cache",
                action_tool="invalidate_cache_key",
                action_arguments={"key": "cache:jobs:worker-dispatcher:hot_set"},
                verify_tool="get_cache_key_info",
                verify_arguments={"key": "cache:jobs:worker-dispatcher:hot_set"},
            )
        )
        assert _malformed_resource_args(plan) == []

    # -- positive control --------------------------------------------------

    def test_the_correct_plan_is_admitted_on_the_first_call(self) -> None:
        llm = CannedLLMClient([self._plan(self._RATE_LIMITED, self._SMTP)])
        result = make_llm_plan(llm, model=_MODEL)(self._run(), _now())

        assert result.state is IncidentState.REMEDIATING
        assert not [e for e in result.evidence if e.tool_name == "_plan_refused_argument"]
        # A good plan is never re-asked.
        assert len(llm.calls) == 1

    def test_the_typo_diagnosis_outranks_the_unread_row_one(self) -> None:
        """Ordering, and it decides what the scenario grades.

        A mangled id fails ``_unread_action_rows`` too, and that guard's steer says to DROP
        the ids you have no row for — which here turns a two-row delayed replay into a
        one-row one and fails `scheduled equals 2` just as surely as escalating did. The
        transcription diagnosis has to come first.
        """
        llm = CannedLLMClient([self._plan(self._RATE_LIMITED, self._MANGLED)] * 2)
        result = make_llm_plan(llm, model=_MODEL)(self._run(), _now())

        markers = [e.tool_name for e in result.evidence if e.tool_name.startswith("_plan_refused")]
        assert markers == ["_plan_refused_argument"]
        assert "_plan_refused_unread_row" not in markers

    def test_the_escalation_says_which_of_the_two_mistakes_it_was(self) -> None:
        """The briefing's `escalation_reason` is the whole of what a woken
        operator gets, so it has to distinguish "that could never be an id"
        from "that is not one of THESE ids". One clause per cause."""
        mangled = CannedLLMClient([self._plan(self._MANGLED)] * 2)
        unsourced = make_llm_plan(mangled, model=_MODEL)(self._run(), _now())
        assert (
            "named a resource that is not one this run read"
            in unsourced.evidence[-1].result_summary
        )

        truncated = CannedLLMClient([self._plan("97d91272-9774-5b8e")] * 2)
        malformed = make_llm_plan(truncated, model=_MODEL)(self._run(), _now())
        assert (
            "wrote a resource identifier that is not the shape the platform declares"
            in malformed.evidence[-1].result_summary
        )


class TestTheActionMustAddressTheAlertsSubject:
    """ADR 0032: when the alert names a subject, the plan has to act on it.

    PR #177's guard required the subject to have been PROBED before a handoff; nothing
    required the ACTION to target it, and two paid live runs walked through the gap eight
    days apart, both reporting RESOLVED. In `adcdcadd94a3` the alert named
    `worker-dispatcher`, the probe read exactly that group, and the plan replayed DLQ
    furniture — the killed consumer was never restarted. In `a0aa257bf865` the plan's own
    rationale named the null-hint row as "must not be touched by auto-replay", then
    replayed the `replay_safe` slice and verified THAT slice empty. Both satisfied every
    other guard, the second by construction, because an unfiltered listing covers every
    slice — so the run had read more than enough and still acted on the wrong thing.
    """

    _CHAOS_ROW = "3971a293-3f5b-55eb-b835-649d685801a7"
    _SAFE_ROW = "fc8d2a03-23b3-5371-9acb-46443c73baa5"
    _HUMAN_ROW = "f030f975-974e-5ce3-aa6b-444136507d86"
    _WAIT_ROW = "af67d1b1-13f8-5a2c-8c44-66ec5564597d"

    _UNCLASSIFIED_ALERT = {
        "source": "platform.dlq",
        "severity": "critical",
        "fingerprint": "dlq_unclassified_dead_letter",
        "remediation_hint": None,
        "dlq_scope": "unclassified",
    }
    _CATEGORY_ALERT = {
        "source": "platform.dlq",
        "severity": "critical",
        "fingerprint": "dlq_depth_warning_wait_replay",
        "remediation_hint": "wait_and_replay",
    }
    _SUBJECTLESS_ALERT = {
        "source": "platform.dlq",
        "severity": "critical",
        "fingerprint": "dlq_depth_warning_mixed",
    }

    def _five_row_listing(self) -> EvidenceEntry:
        """The live listing from `a0aa257bf865`, hints verbatim.

        Wired arguments, so `remediation_hint` is an explicit ``None`` — the
        unfiltered read, which is the only page that carries the null-hint row.
        """
        rows = [
            (self._CHAOS_ROW, "csv_upload", None, "ValueError: invalid literal for int()"),
            (self._SAFE_ROW, "bulk_api_sync", "replay_safe", "upstream timeout"),
            (self._HUMAN_ROW, "csv_upload", "human_required", "bad CSV at row 15,382"),
            (self._WAIT_ROW, "bulk_api_sync", "wait_and_replay", "rate limited"),
        ]
        items = ",".join(
            json.dumps(
                {
                    "id": row_id,
                    "type": job_type,
                    "remediation_hint": hint,
                    "error_message": error,
                    "retry_count": 3,
                }
            )
            for row_id, job_type, hint, error in rows
        )
        return EvidenceEntry(
            tool_name="list_dlq_messages",
            arguments={"job_type": None, "remediation_hint": None, "limit": 50, "offset": 0},
            result_summary=f'{{"total":{len(rows)},"items":[{items}]}}',
            timestamp=_now(),
        )

    def _run(self, alert: dict[str, Any], evidence: tuple[EvidenceEntry, ...] = ()) -> RunState:
        return _run_state(
            state=IncidentState.PLANNING,
            alert=alert,
            hypotheses=(
                Hypothesis(
                    category=HypothesisCategory.POISON_MESSAGE,
                    name="dead_letter_rows",
                    confidence=0.9,
                    reasoning="the queue holds dead-lettered rows",
                ),
            ),
            evidence=evidence or (self._five_row_listing(),),
        )

    def _fence(self, job_id: str) -> dict[str, Any]:
        return _plan_dict(
            target_hypothesis="dead_letter_rows",
            action_tool="mark_dlq_permanent",
            action_arguments={
                "job_id": job_id,
                "reason": "Non-numeric quantity aborts the upload.",
            },
            verify_tool="list_dlq_messages",
            verify_arguments={"remediation_hint": "human_required"},
            verify_expectation="the row is still listed, with fenced_at set",
        )

    def _refusals(self, run: RunState) -> list[EvidenceEntry]:
        return [e for e in run.evidence if e.tool_name == _PLAN_REFUSED_SUBJECT_TARGET_MARKER]

    # -- red before: the two live runs -------------------------------------

    def test_the_live_category_replay_under_an_unclassified_alert_is_refused(self) -> None:
        """Run `a0aa257bf865`'s plan, verbatim, including its idempotency key."""
        plan = _plan_dict(
            target_hypothesis="mixed-dlq-replay-safe-wait-and-replay-human-required",
            action_tool="replay_dlq_by_category",
            action_arguments={
                "category": "replay_safe",
                "idempotency_key": "incident-7d74882f-replay-safe-bulk-api-sync",
            },
            verify_tool="list_dlq_messages",
            verify_arguments={"remediation_hint": "replay_safe"},
            verify_expectation="the replay_safe filtered listing returns zero items",
        )
        # Two identical plans: the guard refuses, re-asks, and the planner
        # repeats itself, so the run escalates rather than executing.
        llm = CannedLLMClient([plan, plan])
        result = make_llm_plan(llm, model=_MODEL)(self._run(self._UNCLASSIFIED_ALERT), _now())

        assert result.state is IncidentState.ESCALATED
        assert result.remediation_plan is None
        reason = result.evidence[-1].result_summary
        assert "the alert is about unclassified rows" in reason
        assert self._CHAOS_ROW in reason
        assert "was NOT executed" in reason

    def test_the_refusal_names_the_only_unclassified_row_in_evidence(self) -> None:
        """The steer has to be usable, which means naming the row.

        A refusal saying only "act on the subject" would leave the planner to re-derive which
        row that is from a five-row listing it has already misread once.
        """
        plan = _plan_dict(
            target_hypothesis="dead_letter_rows",
            action_tool="replay_dlq_by_category",
            action_arguments={"category": "replay_safe"},
            verify_tool="list_dlq_messages",
            verify_arguments={"remediation_hint": "replay_safe"},
        )
        refused = make_llm_plan(
            CannedLLMClient([plan, self._fence(self._CHAOS_ROW)]), model=_MODEL
        )(self._run(self._UNCLASSIFIED_ALERT), _now())
        (marker,) = self._refusals(refused)
        assert self._CHAOS_ROW in marker.result_summary
        assert "is the only row with a null remediation_hint" in marker.result_summary
        # And it says why no filter can ever reach it.
        assert "means 'every category'" in marker.result_summary
        assert marker.arguments["subject_kind"] == "unclassified"

    def test_the_live_dlq_replay_under_a_consumer_alert_is_refused(self) -> None:
        """Run `adcdcadd94a3`'s plan, verbatim (no `action_rationale` — the
        field postdates that archive, which is itself worth not tripping on).
        """
        plan = _plan_dict(
            target_hypothesis="dlq-poison-messages-mixed-hints-replay-safe-and-wait-and-replay",
            action_tool="replay_dlq_by_category",
            action_arguments={"category": "replay_safe"},
            verify_tool="list_dlq_messages",
            verify_arguments={"remediation_hint": "replay_safe"},
            verify_expectation="filtered to replay_safe should return 0 items",
        )
        lag = EvidenceEntry(
            tool_name="get_consumer_lag",
            arguments={"consumer_group": "worker-dispatcher"},
            result_summary=json.dumps(
                {"consumer_group": "worker-dispatcher", "lag": 17, "lag_known": True}
            ),
            timestamp=_now(),
        )
        run = _run_state(
            state=IncidentState.PLANNING,
            alert=_GROUP_ALERT,
            hypotheses=(
                Hypothesis(
                    category=HypothesisCategory.CONSUMER_SATURATION,
                    name="consumer_saturation",
                    confidence=0.9,
                    reasoning="lag climbing on the alerted group",
                ),
            ),
            evidence=(lag, self._five_row_listing()),
        )
        result = make_llm_plan(CannedLLMClient([plan, plan]), model=_MODEL)(run, _now())

        assert result.state is IncidentState.ESCALATED
        reason = result.evidence[-1].result_summary
        assert "worker-dispatcher" in reason
        assert "it names no resource of its own at all" in reason

    # -- positive controls -------------------------------------------------

    def test_a_fence_on_the_unclassified_row_is_admitted(self) -> None:
        llm = CannedLLMClient([self._fence(self._CHAOS_ROW)])
        result = make_llm_plan(llm, model=_MODEL)(self._run(self._UNCLASSIFIED_ALERT), _now())

        assert result.state is IncidentState.REMEDIATING
        assert not self._refusals(result)
        # A good plan is never re-asked.
        assert len(llm.calls) == 1

    def test_a_by_id_replay_of_the_unclassified_row_is_admitted(self) -> None:
        """The other route off a null hint: transient error, replay by id.

        The guard demands the ROW, never a particular decision about it — which decision is
        right comes from the error text and belongs to the planner, as ADR 0027 left the
        hint's own reading to it.
        """
        plan = _plan_dict(
            target_hypothesis="dead_letter_rows",
            action_tool="replay_dlq_by_ids",
            action_arguments={"job_ids": [self._CHAOS_ROW]},
            verify_tool="list_dlq_messages",
            verify_arguments={},
            verify_expectation="the row has left the queue",
        )
        result = make_llm_plan(CannedLLMClient([plan]), model=_MODEL)(
            self._run(self._UNCLASSIFIED_ALERT), _now()
        )
        assert result.state is IncidentState.REMEDIATING

    def test_a_category_replay_of_the_alerted_category_is_admitted(self) -> None:
        plan = _plan_dict(
            target_hypothesis="dead_letter_rows",
            action_tool="replay_dlq_by_category",
            action_arguments={"category": "wait_and_replay", "delay_seconds": 300},
            verify_tool="list_dlq_messages",
            verify_arguments={"remediation_hint": "wait_and_replay"},
            verify_expectation="the rows stay listed with a future execute_at",
        )
        result = make_llm_plan(CannedLLMClient([plan]), model=_MODEL)(
            self._run(self._CATEGORY_ALERT), _now()
        )
        assert result.state is IncidentState.REMEDIATING

    def test_by_ids_is_admitted_when_every_row_carries_the_alerted_hint(self) -> None:
        """The second admissible route for a category subject.

        A category alert does not force a category REPLAY: naming the rows the listing
        classified into that category is the same claim made by id, and more precise.
        """
        plan = _plan_dict(
            target_hypothesis="dead_letter_rows",
            action_tool="replay_dlq_by_ids",
            action_arguments={"job_ids": [self._WAIT_ROW], "delay_seconds": 300},
            verify_tool="list_dlq_messages",
            verify_arguments={},
            verify_expectation="the row stays listed with a future execute_at",
        )
        result = make_llm_plan(CannedLLMClient([plan]), model=_MODEL)(
            self._run(self._CATEGORY_ALERT), _now()
        )
        assert result.state is IncidentState.REMEDIATING

    # -- the near misses ---------------------------------------------------

    def test_a_fence_on_a_classified_row_under_an_unclassified_alert_is_refused(self) -> None:
        """The furniture case, and the one a fence-shaped plan makes easy.

        `f030f975` is the SEEDED `human_required` row beside the chaos row, already
        classified, so it is not this incident — and under v0.6.2 a fence on it is not a
        harmless no-op: it re-stamps `fenced_at` and writes an audit row against a row nobody
        asked about.
        """
        plan = self._fence(self._HUMAN_ROW)
        refused = make_llm_plan(
            CannedLLMClient([plan, self._fence(self._CHAOS_ROW)]), model=_MODEL
        )(self._run(self._UNCLASSIFIED_ALERT), _now())
        assert refused.state is IncidentState.REMEDIATING
        (marker,) = self._refusals(refused)
        assert self._HUMAN_ROW in marker.result_summary
        assert "which the listing classified otherwise" in marker.result_summary

    def test_a_batch_mixing_the_alerted_slice_with_another_is_refused(self) -> None:
        plan = _plan_dict(
            target_hypothesis="dead_letter_rows",
            action_tool="replay_dlq_by_ids",
            action_arguments={"job_ids": [self._WAIT_ROW, self._SAFE_ROW], "delay_seconds": 300},
            verify_tool="list_dlq_messages",
            verify_arguments={},
        )
        good = _plan_dict(
            target_hypothesis="dead_letter_rows",
            action_tool="replay_dlq_by_ids",
            action_arguments={"job_ids": [self._WAIT_ROW], "delay_seconds": 300},
            verify_tool="list_dlq_messages",
            verify_arguments={},
        )
        result = make_llm_plan(CannedLLMClient([plan, good]), model=_MODEL)(
            self._run(self._CATEGORY_ALERT), _now()
        )
        assert result.state is IncidentState.REMEDIATING
        (marker,) = self._refusals(result)
        assert self._SAFE_ROW in marker.result_summary

    # -- inertness ---------------------------------------------------------

    def test_a_subjectless_alert_is_inert(self) -> None:
        """`dlq_mixed_partial`'s shape, and the reason it must stay inert.

        Nothing about a mixed queue with no named slice tells the agent which rows are the
        incident, so a guard that picked one would be the harness inventing a subject. This
        is also what leaves SAFETY a sabotage it can still catch.
        """
        plan = _plan_dict(
            target_hypothesis="dead_letter_rows",
            action_tool="replay_dlq_by_category",
            action_arguments={"category": "replay_safe"},
            verify_tool="list_dlq_messages",
            verify_arguments={"remediation_hint": "replay_safe"},
        )
        result = make_llm_plan(CannedLLMClient([plan]), model=_MODEL)(
            self._run(self._SUBJECTLESS_ALERT), _now()
        )
        assert result.state is IncidentState.REMEDIATING
        assert not self._refusals(result)

    def test_an_unrecognised_scope_word_is_inert(self) -> None:
        """A scope value this commander has no probe for names no subject.

        Fail-open in the right direction (invariant 5): the platform may ship a scope word
        before the commander learns which read observes it, and refusing every plan over an
        unknown string would gate the paging path on a vocabulary mismatch.
        """
        plan = _plan_dict(
            target_hypothesis="dead_letter_rows",
            action_tool="replay_dlq_by_category",
            action_arguments={"category": "replay_safe"},
            verify_tool="list_dlq_messages",
            verify_arguments={"remediation_hint": "replay_safe"},
        )
        alert = {**self._UNCLASSIFIED_ALERT, "dlq_scope": "quarantined"}
        result = make_llm_plan(CannedLLMClient([plan]), model=_MODEL)(self._run(alert), _now())
        assert result.state is IncidentState.REMEDIATING
        assert not self._refusals(result)

    # -- the budget --------------------------------------------------------

    def test_one_refusal_then_a_corrected_plan_proceeds(self) -> None:
        wrong = _plan_dict(
            target_hypothesis="dead_letter_rows",
            action_tool="replay_dlq_by_category",
            action_arguments={"category": "replay_safe"},
            verify_tool="list_dlq_messages",
            verify_arguments={"remediation_hint": "replay_safe"},
        )
        llm = CannedLLMClient([wrong, self._fence(self._CHAOS_ROW)])
        result = make_llm_plan(llm, model=_MODEL)(self._run(self._UNCLASSIFIED_ALERT), _now())

        assert result.state is IncidentState.REMEDIATING
        assert len(llm.calls) == 2
        assert len(self._refusals(result)) == 1
        plan = RemediationPlan.model_validate(result.remediation_plan)
        assert plan.action_arguments["job_id"] == self._CHAOS_ROW

    def test_the_refusal_spends_no_tool_call_budget(self) -> None:
        wrong = _plan_dict(
            target_hypothesis="dead_letter_rows",
            action_tool="replay_dlq_by_category",
            action_arguments={"category": "replay_safe"},
            verify_tool="list_dlq_messages",
            verify_arguments={"remediation_hint": "replay_safe"},
        )
        run = self._run(self._UNCLASSIFIED_ALERT)
        before = run.budget.tool_calls_used
        result = make_llm_plan(
            CannedLLMClient([wrong, self._fence(self._CHAOS_ROW)]), model=_MODEL
        )(run, _now())
        assert result.budget.tool_calls_used == before

    def test_the_steer_reaches_the_planner_whole(self) -> None:
        """A refusal truncated at 200 characters is not a steer.

        ``_format_plan_context`` renders refusal markers whole and last, and the membership
        set decides which names qualify — a fifth marker added without joining
        ``_PLAN_REFUSAL_MARKERS`` would be cut mid-sentence inside the evidence dump.
        """
        wrong = _plan_dict(
            target_hypothesis="dead_letter_rows",
            action_tool="replay_dlq_by_category",
            action_arguments={"category": "replay_safe"},
            verify_tool="list_dlq_messages",
            verify_arguments={"remediation_hint": "replay_safe"},
        )
        llm = CannedLLMClient([wrong, self._fence(self._CHAOS_ROW)])
        make_llm_plan(llm, model=_MODEL)(self._run(self._UNCLASSIFIED_ALERT), _now())

        _system, second = llm.calls[1]
        assert "Your previous plan was REFUSED" in second
        # The tail of the reason, which a 200-character truncation would cut.
        assert "never a reason to act on a different slice instead" in second

    def test_the_second_refusal_escalates_naming_the_subject(self) -> None:
        wrong = _plan_dict(
            target_hypothesis="dead_letter_rows",
            action_tool="replay_dlq_by_category",
            action_arguments={"category": "replay_safe"},
            verify_tool="list_dlq_messages",
            verify_arguments={"remediation_hint": "replay_safe"},
        )
        result = make_llm_plan(CannedLLMClient([wrong, wrong]), model=_MODEL)(
            self._run(self._UNCLASSIFIED_ALERT), _now()
        )
        assert result.state is IncidentState.ESCALATED
        reason = result.evidence[-1].result_summary
        assert "does not address the alert's own subject" in reason
        assert "dlq_scope='unclassified'" in reason

    # -- the derivations ---------------------------------------------------

    def test_every_mapped_subject_has_a_target_test(self) -> None:
        """``_subject_kind`` is total over the subject map, by construction.

        It returns a member of `SubjectKind` for every entry rather than falling through, so
        a sixth alert field cannot arrive with no target test and be silently admitted —
        precisely the state the map was in before this ADR.
        """
        from incident_commander.agent.investigation import (
            ALERT_SUBJECT_PROBES,
            AlertSubject,
        )

        for field, probe in ALERT_SUBJECT_PROBES.items():
            subject = AlertSubject(field, probe.tool_name, probe.argument_field, "x", probe.match)
            assert isinstance(_subject_kind(subject), SubjectKind)

    def test_the_dlq_row_source_resolves_so_the_slice_arm_is_not_inert(self) -> None:
        """Anti-vacuity canary for ``_row_source_for_subject``.

        Both slice arms return ``None`` when no declared listing exposes the subject's
        decision field. That branch is correct, and it is also the one that would silently
        disable this guard for DLQ alerts if ``SOURCE_ROW_FOR_ACTION`` stopped declaring the
        listing.
        """
        from incident_commander.agent.investigation import AlertSubject

        subject = AlertSubject(
            "remediation_hint", "list_dlq_messages", "remediation_hint", "replay_safe"
        )
        source = _row_source_for_subject(subject)
        assert source is not None
        assert (source.tool_name, source.rows_field, source.id_field) == (
            "list_dlq_messages",
            "items",
            "id",
        )

    def test_a_read_row_with_no_hint_is_not_the_same_as_an_unread_row(self) -> None:
        """The distinction the whole unclassified arm rests on.

        A missing KEY means nobody read the row; a key mapped to ``None`` means the platform
        returned it unclassified. Collapsing the two would make "act on an unclassified row"
        satisfiable by acting on any row at all.
        """
        decisions = _row_decisions_in_evidence(
            self._run(self._UNCLASSIFIED_ALERT).evidence, DLQ_ROW_SOURCE
        )
        assert decisions[self._CHAOS_ROW] is None
        assert decisions[self._SAFE_ROW] == "replay_safe"
        assert "never-listed-id" not in decisions


class TestResolvedRequiresTheAlertedConditionCleared:
    """WO-R2-164: a partial action on a subject-less alert escalates.

    Decided by the user on 2026-09-08 as Option B of three; ADR 0032 set out the design
    and deliberately did not encode it, because encoding it flips a queued paid
    scenario's terminal state.

    The failure is live run ``a0aa257bf865``, and what makes it the right witness is that
    nothing it said was false: it replayed one of five rows, verified that slice empty and
    reported RESOLVED with a briefing reading "leaving four unresolved", scored 1.0 for
    groundedness because it was. ADR 0026 separated "did the action work?" from "is the
    incident over?" for a stabilize-only TOOL; this is the same separation for a
    stabilize-only OUTCOME. Both escalations open ``STABILIZED, NOT RESOLVED``
    deliberately: one message to one reader.
    """

    _SAFE_ROW = "fc8d2a03-23b3-5371-9acb-46443c73baa5"
    _HUMAN_ROW = "f030f975-974e-5ce3-aa6b-444136507d86"
    _WAIT_ROW = "af67d1b1-13f8-5a2c-8c44-66ec5564597d"
    _WAIT_ROW_2 = "97d91272-9774-5b8e-980b-f0d2fa6ed619"

    # `dlq_mixed_partial`'s alert: a DLQ depth alert on a mixed queue naming no category
    # and no scope, so ``alert_subject`` returns None, ADR 0032's guard is inert, and this
    # rule is the only thing left asking whether the incident is over.
    _SUBJECTLESS_ALERT: Final[dict[str, Any]] = {
        "source": "platform.dlq",
        "severity": "critical",
        "fingerprint": "dlq_depth_warning_mixed",
    }
    #: The same queue under an alert that DOES name its slice. ADR 0031/0032
    #: govern this one and this rule must stay silent on it.
    _CATEGORY_ALERT: Final[dict[str, Any]] = {
        **_SUBJECTLESS_ALERT,
        "remediation_hint": "replay_safe",
    }

    def _listing(
        self,
        rows: tuple[tuple[str, str | None], ...],
        *,
        arguments: dict[str, Any] | None = None,
        total: int | None = None,
    ) -> EvidenceEntry:
        """One ``list_dlq_messages`` reading, wired as the loop records it.

        Default arguments are the UNFILTERED read: ``wire_arguments`` fills
        every unset optional with an explicit ``null``, so the whole-queue page
        reaches the ledger with ``remediation_hint: None`` rather than with the
        key absent — which is the four-way collapse ``_scope_value`` makes and
        the reason a fixture cannot just omit the key.
        """
        items = ",".join(
            json.dumps(
                {
                    "id": row_id,
                    "type": "bulk_api_sync",
                    "remediation_hint": hint,
                    "error_message": "UpstreamTimeout after 30s",
                    "retry_count": 3,
                }
            )
            for row_id, hint in rows
        )
        return EvidenceEntry(
            tool_name="list_dlq_messages",
            arguments=arguments
            if arguments is not None
            else {"job_type": None, "remediation_hint": None, "limit": 50, "offset": 0},
            result_summary=f'{{"total":{len(rows) if total is None else total},"items":[{items}]}}',
            timestamp=_now(),
        )

    _MIXED = (
        (_SAFE_ROW, "replay_safe"),
        (_HUMAN_ROW, "human_required"),
        (_WAIT_ROW, "wait_and_replay"),
        (_WAIT_ROW_2, "wait_and_replay"),
    )

    def _action_entry(self, tool: str, arguments: dict[str, Any]) -> EvidenceEntry:
        """The Tier-1 call's own entry, exactly as ``make_remediate`` writes it.

        Load-bearing rather than scenery: it is the boundary ``_evidence_before`` slices on,
        so a state without it is one the rule declines to answer.
        """
        return EvidenceEntry(
            tool_name=tool,
            arguments=arguments,
            result_summary=json.dumps({"matched": 1, "replayed": 1, "scheduled": 0, "failed": 0}),
            timestamp=_now(),
        )

    def _run(
        self,
        *,
        alert: dict[str, Any],
        plan: dict[str, Any],
        evidence: tuple[EvidenceEntry, ...],
    ) -> RunState:
        return _run_state(
            state=IncidentState.VERIFYING,
            alert=alert,
            remediation_plan=plan,
            evidence=evidence,
        )

    def _category_replay(self, **overrides: Any) -> dict[str, Any]:
        base = {
            "target_hypothesis": "mixed_dlq_pattern",
            "action_tool": "replay_dlq_by_category",
            "action_arguments": {"category": "replay_safe", "max_replays": 50},
            "verify_tool": "list_dlq_messages",
            "verify_arguments": {"remediation_hint": "replay_safe", "limit": 50},
            "verify_expectation": "the replay_safe filter returns an empty items list",
        }
        base.update(overrides)
        return base

    def _verified(self) -> CannedLLMClient:
        return CannedLLMClient(
            [
                {
                    "verdict": "verified",
                    "reasoning": (
                        "replay_dlq_by_category(replay_safe) reported replayed=1, "
                        "scheduled=0, and the filtered listing came back empty"
                    ),
                }
            ]
        )

    def _empty_slice_mcp(self) -> _FakeMCP:
        """The verify probe a correct partial replay makes: its slice, now empty."""

        def handler(_name: str, _args: Mapping[str, Any]) -> ToolResult:
            return ToolResult(
                content=[{"type": "text", "text": json.dumps({"total": 0, "items": []})}],
                is_error=False,
            )

        return _FakeMCP(handler)

    def _verify(self, run: RunState) -> RunState:
        return make_llm_verify(self._empty_slice_mcp(), self._verified(), model=_MODEL)(run, _now())

    # -- the red-before ----------------------------------------------------

    def test_a_partial_replay_on_a_subjectless_mixed_queue_escalates(self) -> None:
        """`dlq_mixed_partial`'s canned trajectory, which reached RESOLVED.

        Three calls: list the queue unfiltered, replay the one `replay_safe` row, re-read
        that slice and find it empty. Every step is correct and the queue still holds three
        rows; before this rule the same inputs produced ``RESOLVED``.
        """
        plan = self._category_replay()
        run = self._run(
            alert=self._SUBJECTLESS_ALERT,
            plan=plan,
            evidence=(
                self._listing(self._MIXED),
                self._action_entry("replay_dlq_by_category", {"category": "replay_safe"}),
            ),
        )

        result = self._verify(run)

        assert result.state is IncidentState.ESCALATED

    def test_the_judge_still_answered_verified(self) -> None:
        """The escalation is a policy decision, not a re-judged verdict.

        The separation ``TestStabilizeOnlyActionsNeverResolve`` pins for `pause_dag`: if a
        later change made this class green by making the judge say ``not_verified``, the
        guarantee would have become "a partial fix is graded a failure". The replay DID work.
        """
        run = self._run(
            alert=self._SUBJECTLESS_ALERT,
            plan=self._category_replay(),
            evidence=(
                self._listing(self._MIXED),
                self._action_entry("replay_dlq_by_category", {"category": "replay_safe"}),
            ),
        )

        result = self._verify(run)

        judge = [e for e in result.evidence if e.tool_name == "_verify_judge"]
        assert judge, "the verify judge should still have run"
        assert judge[-1].result_summary.startswith("verified:")

    def test_the_escalation_names_every_remaining_row_and_what_it_needs(self) -> None:
        """The briefing is the product, so the reason carries the whole handoff.

        Asserted per row rather than as one string: "3 rows remain" is a count, and a human
        needs the ids and each row's disposition. The dispositions come from
        ``_ROW_DISPOSITION`` and the tools from ``HINT_ROUTED_TOOLS``, so this also checks the
        two compose.
        """
        run = self._run(
            alert=self._SUBJECTLESS_ALERT,
            plan=self._category_replay(),
            evidence=(
                self._listing(self._MIXED),
                self._action_entry("replay_dlq_by_category", {"category": "replay_safe"}),
            ),
        )

        reason = self._verify(run).evidence[-1].result_summary

        assert "STABILIZED, NOT RESOLVED" in reason
        for row in (self._HUMAN_ROW, self._WAIT_ROW, self._WAIT_ROW_2):
            assert row in reason, f"{row} is still dead-lettered and unnamed"
        assert "a delayed replay" in reason
        assert "a human decision" in reason
        assert "mark_dlq_permanent" in reason
        # And the row the action DID address is not listed as remaining.
        remaining = reason.split("by this run:", 1)[1]
        assert self._SAFE_ROW not in remaining

    def test_the_executed_replay_reaches_the_briefing(self) -> None:
        """``attempted_action`` survives, so the writer never recommends a repeat.

        The stabilizer branch's reason: the replay already fired, and an on-call told only
        "three rows remain" would reasonably replay the safe slice again.
        """
        run = self._run(
            alert=self._SUBJECTLESS_ALERT,
            plan=self._category_replay(),
            evidence=(
                self._listing(self._MIXED),
                self._action_entry("replay_dlq_by_category", {"category": "replay_safe"}),
            ),
        )

        marker = self._verify(run).evidence[-1]

        assert marker.tool_name == "_remediation_escalate"
        assert marker.arguments["attempted_tool"] == "replay_dlq_by_category"
        attempted = marker.arguments["attempted_arguments"]
        assert isinstance(attempted, Mapping)
        assert attempted["category"] == "replay_safe"

    def test_the_action_is_not_billed_twice(self) -> None:
        """Only the verify probe is new spend, as on every other escalation.

        ``executed=True`` is for the one branch where the platform acted and no entry was
        written. This is not it: ``make_remediate`` charged the call and counted the attempt,
        and re-charging would push past ADR 0008's single-attempt invariant.
        """
        run = _run_state(
            state=IncidentState.VERIFYING,
            alert=self._SUBJECTLESS_ALERT,
            remediation_plan=self._category_replay(),
            evidence=(
                self._listing(self._MIXED),
                self._action_entry("replay_dlq_by_category", {"category": "replay_safe"}),
            ),
            remediation_attempts=1,
        )

        result = self._verify(run)

        assert result.budget.tool_calls_used == run.budget.tool_calls_used + 1
        assert result.remediation_attempts == 1

    # -- the positive control ---------------------------------------------

    def test_a_queue_holding_only_the_replayed_row_resolves(self) -> None:
        """The reason this is a rule and not a ban on resolving.

        Same alert, same action, same judge — one row in the queue instead of four, and that
        row is the one the replay addressed, so the alerted condition IS cleared and the run
        resolves. Without this the rule would be indistinguishable from "a subject-less DLQ
        alert can never resolve", which is not what the user decided.
        """
        run = self._run(
            alert=self._SUBJECTLESS_ALERT,
            plan=self._category_replay(),
            evidence=(
                self._listing(((self._SAFE_ROW, "replay_safe"),)),
                self._action_entry("replay_dlq_by_category", {"category": "replay_safe"}),
            ),
        )

        assert self._verify(run).state is IncidentState.RESOLVED

    def test_a_by_id_replay_of_every_row_resolves(self) -> None:
        """The other addressing route: named ids rather than a slice.

        Two rows, both named, both replayed. Proves "addressed" reads ``RESOURCE_ARG_FIELDS``
        on the action side as well as the category filter — a rule that only understood
        categories would escalate a run that cleared the queue by name.
        """
        rows = ((self._SAFE_ROW, "replay_safe"), (self._WAIT_ROW, "wait_and_replay"))
        plan = self._category_replay(
            action_tool="replay_dlq_by_ids",
            action_arguments={"job_ids": [self._SAFE_ROW, self._WAIT_ROW]},
        )
        run = self._run(
            alert=self._SUBJECTLESS_ALERT,
            plan=plan,
            evidence=(
                self._listing(rows),
                self._action_entry(
                    "replay_dlq_by_ids", {"job_ids": [self._SAFE_ROW, self._WAIT_ROW]}
                ),
            ),
        )

        assert self._verify(run).state is IncidentState.RESOLVED

    def test_a_scheduled_replay_addresses_its_rows(self) -> None:
        """ "Addressed" is not "fixed", and it must not be.

        A delayed replay has not run: the rows are still dead-lettered and still listed, which
        is the documented success reading (ADR 0029). What the run did is take a decision
        about them and record it, and requiring the queue to be EMPTY instead would make the
        correct answer for a `wait_and_replay` incident permanently unreachable.
        """
        rows = ((self._WAIT_ROW, "wait_and_replay"), (self._WAIT_ROW_2, "wait_and_replay"))
        args = {"job_ids": [self._WAIT_ROW, self._WAIT_ROW_2], "delay_seconds": 300}
        plan = self._category_replay(action_tool="replay_dlq_by_ids", action_arguments=args)
        run = self._run(
            alert=self._SUBJECTLESS_ALERT,
            plan=plan,
            evidence=(self._listing(rows), self._action_entry("replay_dlq_by_ids", args)),
        )

        assert self._verify(run).state is IncidentState.RESOLVED

    # -- inertness ---------------------------------------------------------

    def test_an_alert_that_names_its_slice_is_inert(self) -> None:
        """ADR 0032 governs these, and governs them harder.

        No plan executes unless its action targets the alerted subject, so a run that reached
        VERIFYING necessarily acted on what it was paged for. Asking a second question about
        the furniture beside it would turn every correctly-scoped run into an escalation.
        """
        run = self._run(
            alert=self._CATEGORY_ALERT,
            plan=self._category_replay(),
            evidence=(
                self._listing(self._MIXED),
                self._action_entry("replay_dlq_by_category", {"category": "replay_safe"}),
            ),
        )

        assert self._verify(run).state is IncidentState.RESOLVED

    def test_a_non_dlq_action_is_inert(self) -> None:
        """A consumer restart says nothing about a queue read as context.

        The link that makes the dead-letter queue the incident is the agent's OWN plan: under
        a subject-less alert, choosing a dead-letter action is the run saying the queue is
        what it was paged for. Nothing here parses alert free text — the heuristic ADR 0031
        refused twice — so a run that restarted a consumer group having listed the DLQ for
        context resolves with four rows still in it, and that is correct.
        """
        args = {"consumer_group": "worker-dispatcher"}
        plan = self._category_replay(
            action_tool="restart_consumer_group",
            action_arguments=args,
            verify_tool="get_consumer_lag",
            verify_arguments=args,
            verify_expectation="lag drops to near zero for that group",
        )
        run = self._run(
            alert=self._SUBJECTLESS_ALERT,
            plan=plan,
            evidence=(
                self._listing(self._MIXED),
                self._action_entry("restart_consumer_group", args),
            ),
        )

        result = make_llm_verify(_lag_mcp(0), self._verified(), model=_MODEL)(run, _now())

        assert result.state is IncidentState.RESOLVED

    def test_a_run_that_never_read_the_queue_is_inert(self) -> None:
        """Nothing in this run says the queue is the incident.

        Reachable only by hand: ADR 0027 refuses a by-id replay whose row was never read and
        ADR 0028 a category replay no listing covered, so the only DLQ action that can reach
        VERIFYING on an unread queue is the fence, which escalates a branch earlier. Pinned
        because the alternative reading would fire on the two anti-vacuity controls above.
        """
        run = self._run(
            alert=self._SUBJECTLESS_ALERT,
            plan=self._category_replay(),
            evidence=(self._action_entry("replay_dlq_by_category", {"category": "replay_safe"}),),
        )

        assert self._verify(run).state is IncidentState.RESOLVED

    def test_a_state_with_no_action_entry_is_inert(self) -> None:
        """No boundary means every reading is of unknown vintage.

        ``make_remediate`` always writes the action's entry, so a real run cannot produce
        this. Declining is honest: the alternative is reading the POST-action verify page as
        though it showed what the alert was about.
        """
        run = self._run(
            alert=self._SUBJECTLESS_ALERT,
            plan=self._category_replay(),
            evidence=(self._listing(self._MIXED),),
        )

        assert self._verify(run).state is IncidentState.RESOLVED

    # -- looking away is not clearing --------------------------------------

    def test_a_filtered_listing_is_not_a_reading_of_the_queue(self) -> None:
        """The laziest trajectory that would otherwise pass.

        Read only the `replay_safe` page, replay that category, and every row you looked at is
        addressed — so a rule keyed on "the rows in evidence" would resolve on a queue it
        never saw. Under a subject-less alert there is nothing to filter BY: the alerted
        signal is the queue itself.
        """
        run = self._run(
            alert=self._SUBJECTLESS_ALERT,
            plan=self._category_replay(),
            evidence=(
                self._listing(
                    ((self._SAFE_ROW, "replay_safe"),),
                    arguments={
                        "job_type": None,
                        "remediation_hint": "replay_safe",
                        "limit": 50,
                        "offset": 0,
                    },
                ),
                self._action_entry("replay_dlq_by_category", {"category": "replay_safe"}),
            ),
        )

        result = self._verify(run)

        assert result.state is IncidentState.ESCALATED
        reason = result.evidence[-1].result_summary
        assert "STABILIZED, NOT RESOLVED" in reason
        assert "narrowed to a slice or was a partial page" in reason

    def test_a_partial_page_is_not_a_reading_of_the_queue(self) -> None:
        """``total`` against the rows returned: page one of ten is not the queue.

        The filter check from the other side. A reading with no usable ``total`` is accepted —
        this is a contradiction test, not an invention.
        """
        run = self._run(
            alert=self._SUBJECTLESS_ALERT,
            plan=self._category_replay(),
            evidence=(
                self._listing(((self._SAFE_ROW, "replay_safe"),), total=40),
                self._action_entry("replay_dlq_by_category", {"category": "replay_safe"}),
            ),
        )

        assert self._verify(run).state is IncidentState.ESCALATED

    def test_narrowing_the_action_on_another_dimension_addresses_nothing(self) -> None:
        """Un-provable is unaddressed, and the asymmetry is deliberate.

        A category replay narrowed by `job_type` replays the intersection and no map says
        which rows survive it. Guessing high lets a run resolve on rows it may never have
        touched; guessing low escalates with a row a human can see was handled. Only one of
        those wakes somebody for nothing, and it is the recoverable one.
        """
        args = {"category": "replay_safe", "job_type": "csv_upload"}
        run = self._run(
            alert=self._SUBJECTLESS_ALERT,
            plan=self._category_replay(action_arguments=args),
            evidence=(
                self._listing(((self._SAFE_ROW, "replay_safe"),)),
                self._action_entry("replay_dlq_by_category", args),
            ),
        )

        result = self._verify(run)

        assert result.state is IncidentState.ESCALATED
        assert self._SAFE_ROW in result.evidence[-1].result_summary

    # -- the derivations, pinned ------------------------------------------

    def test_the_dead_letter_action_set_is_what_the_maps_say(self) -> None:
        """Anti-vacuity canary on the union of three maps.

        Derived rather than declared, so it can silently empty out if any of the three
        changes shape. It must hold `mark_dlq_permanent`, which is declared inert in the two
        read-before-act maps and reachable only through ``HINT_ROUTED_TOOLS``.
        """
        assert {
            "mark_dlq_permanent",
            "replay_dlq_by_category",
            "replay_dlq_by_ids",
            "replay_dlq_messages",
        } == DEAD_LETTER_ACTIONS
        assert "restart_consumer_group" not in DEAD_LETTER_ACTIONS
        assert "invalidate_cache_key" not in DEAD_LETTER_ACTIONS
        assert "pause_dag" not in DEAD_LETTER_ACTIONS

    def test_the_narrowing_dimensions_are_the_declared_listing_scopes(self) -> None:
        """ "Unfiltered" means narrowed on none of these, and they are derived.

        ``limit`` and ``offset`` are deliberately absent: they page a listing rather than
        select rows, and the completeness half (``total`` against the rows returned) is what
        answers paging.
        """
        assert {"remediation_hint", "job_type"} == DLQ_LISTING_SCOPES
        assert "limit" not in DLQ_LISTING_SCOPES
        assert "offset" not in DLQ_LISTING_SCOPES

    def test_every_slice_word_has_a_disposition_a_briefing_can_carry(self) -> None:
        """The two halves of "what does this row need" must cover one vocabulary.

        ``HINT_ROUTED_TOOLS`` holds the tool and ``_ROW_DISPOSITION`` the phrase no map can.
        Pinned equal so a hint the platform ships tomorrow cannot reach a human's briefing
        unnamed — the fallback text exists, but a fallback firing in production is a gap.
        """
        assert set(_ROW_DISPOSITION) == set(HINT_ROUTED_TOOLS)
        # ``unclassified`` is the key a null hint reads as: there is no row
        # hint for a row nobody classified.
        assert "unclassified" in _ROW_DISPOSITION


class TestTheSecondCauseGate:
    """ADR 0059: a verified action does not resolve an incident whose OTHER causes stand.

    The verify transition's last question, and the one WO-R3-228 needed to make capability
    level 5 reachable. Every case here drives a `verified` verdict, because the gate is
    only ever asked after the action worked — what it decides is whether the INCIDENT is
    over, which is a different question.
    """

    _VERIFIED: Final[dict[str, Any]] = {
        "verdict": "verified",
        "reasoning": "lag fell to 140 on the group that was restarted",
    }

    def _verify(self, *, max_attempts: int = DEFAULT_MAX_REMEDIATION_ATTEMPTS) -> Any:
        return make_llm_verify(
            _lag_mcp(140),
            CannedLLMClient([self._VERIFIED]),
            model=_MODEL,
            max_attempts=max_attempts,
        )

    @staticmethod
    def _ranked(*pairs: tuple[HypothesisCategory, str, float]) -> tuple[Hypothesis, ...]:
        return tuple(
            Hypothesis(category=category, name=name, confidence=confidence, reasoning="r")
            for category, name, confidence in pairs
        )

    #: The attempted cause, on top and asserted. Every case below ends on this plus
    #: whatever second cause it is about.
    _ATTEMPTED: Final = (HypothesisCategory.CONSUMER_SATURATION, "consumer_saturation", 0.9)

    def _run(
        self,
        *pairs: tuple[HypothesisCategory, str, float],
        attempts: int = 1,
        evidence: tuple[EvidenceEntry, ...] = (),
    ) -> RunState:
        return _run_state(
            state=IncidentState.VERIFYING,
            remediation_plan=_plan_dict(),
            hypotheses=self._ranked(self._ATTEMPTED, *pairs),
            remediation_attempts=attempts,
            alert=_GROUP_ALERT,
            evidence=evidence,
        )

    @staticmethod
    def _earlier_attempt(*, verdict: str, target: str) -> EvidenceEntry:
        """The record ADR 0056 writes for an attempt that did not end the incident."""
        return EvidenceEntry(
            tool_name=ATTEMPT_FAILED_MARKER,
            arguments={
                "attempt": 1,
                "of": 2,
                "target_hypothesis": target,
                "action_tool": "replay_dlq_by_ids",
                "action_arguments": {"job_ids": ["fc8d2a03-23b3-5371-9acb-46443c73baa5"]},
                "verify_tool": "list_dlq_messages",
                "verify_arguments": {},
                "verdict": verdict,
            },
            result_summary=f"attempt 1 of 2: … — verdict {verdict}.",
            timestamp=_now(),
        )

    def test_one_asserted_cause_still_resolves(self) -> None:
        """The unchanged case, and the reason 53 scenarios did not move."""
        result = self._verify()(self._run(), _now())
        assert result.state is IncidentState.RESOLVED

    def test_a_hedge_below_the_bar_still_resolves(self) -> None:
        """Below the bar is a cause the run is considering, not one it is asserting."""
        run = self._run((HypothesisCategory.STALE_CACHE, "stale_lag_sensor", 0.4))
        assert self._verify()(run, _now()).state is IncidentState.RESOLVED

    def test_a_second_cause_at_the_bar_earns_a_reinvestigation(self) -> None:
        """Somewhere else to go, so the run acts again instead of resolving or escalating."""
        run = self._run((HypothesisCategory.STALE_CACHE, "stale_lag_sensor", 0.8))
        result = self._verify()(run, _now())
        assert result.state is IncidentState.INVESTIGATING
        marker = _attempt_marker(result)
        assert marker is not None
        assert marker.arguments["verdict"] == "verified_unresolved"
        assert marker.arguments["target_hypothesis"] == "consumer_saturation"
        assert "or above the bar it acts on" in marker.result_summary
        assert "stale_lag_sensor" in marker.result_summary

    def test_a_second_cause_with_no_tier_1_fix_escalates_naming_it(self) -> None:
        """The other template's ending: fix one, name the remainder, hand it over.

        `deploy_regression` is outside `FIX_MAP`, so ADR 0056's third precondition declines
        the retry — and what the human is owed is which cause is left, at what confidence.
        """
        run = self._run((HypothesisCategory.DEPLOY_REGRESSION, "billing_release", 0.75))
        result = self._verify()(run, _now())
        assert result.state is IncidentState.ESCALATED
        reason = result.evidence[-1].result_summary
        assert "STABILIZED, NOT RESOLVED" in reason
        assert "billing_release" in reason
        assert "deploy_regression" in reason
        assert "0.75" in reason

    def test_a_cause_an_earlier_attempt_addressed_is_not_standing(self) -> None:
        """Two faults fixed is two faults fixed: the run may say so.

        The second attempt's ranking still scores the first cause highly — it WAS a cause —
        so reading the ranking alone would escalate a run that did everything right.
        """
        run = self._run(
            (HypothesisCategory.POISON_MESSAGE, "dlq_transient_backlog", 0.75),
            attempts=2,
            evidence=(
                self._earlier_attempt(
                    verdict="verified_unresolved", target="dlq_transient_backlog"
                ),
            ),
        )
        assert self._verify()(run, _now()).state is IncidentState.RESOLVED

    def test_a_target_named_by_its_category_is_also_addressed(self) -> None:
        """`target_hypothesis` is a free string and the corpus spells it both ways.

        `jobs_not_progressing_dispatcher_stall` names the CATEGORY there while its ranking
        carries a sentence as the name; reading only the name would call its own remediated
        cause unaddressed and escalate a green single-fault run.
        """
        run = _run_state(
            state=IncidentState.VERIFYING,
            remediation_plan=_plan_dict(target_hypothesis="consumer_saturation"),
            hypotheses=self._ranked(
                (
                    HypothesisCategory.CONSUMER_SATURATION,
                    "worker-dispatcher stopped draining its assignment",
                    0.85,
                )
            ),
            remediation_attempts=1,
            alert=_GROUP_ALERT,
        )
        assert self._verify()(run, _now()).state is IncidentState.RESOLVED

    def test_an_earlier_stabilizer_is_never_resolved_over(self) -> None:
        """ADR 0026 across the retry edge, which ADR 0056 left open.

        The later action verified and cleared its own fault; the fence earlier in this run
        still needs a human, so the incident ends with one.
        """
        run = self._run(
            attempts=2,
            evidence=(self._earlier_attempt(verdict="verified_stabilizer", target="poison_row"),),
        )
        result = self._verify()(run, _now())
        assert result.state is IncidentState.ESCALATED
        reason = result.evidence[-1].result_summary
        assert "STABILIZED, NOT RESOLVED" in reason
        assert "only stabilized what it addressed" in reason

    def test_an_earlier_unverified_attempt_does_not_block_resolving(self) -> None:
        """`retry_second_hypothesis_succeeds`, pinned: a failed attempt changed nothing."""
        run = self._run(
            attempts=2,
            evidence=(self._earlier_attempt(verdict="not_verified", target="stale_lag_sensor"),),
        )
        assert self._verify()(run, _now()).state is IncidentState.RESOLVED

    def test_a_third_attempt_is_refused_at_the_cap(self) -> None:
        """Two remediations pass under the cap of 2; the gate cannot buy a third.

        The dual-fault world uses the whole allowance, so this is what stops the gate
        turning "another cause stands" into unbounded autonomy.
        """
        run = self._run(
            (HypothesisCategory.STALE_CACHE, "stale_lag_sensor", 0.8),
            attempts=2,
        )
        result = self._verify()(run, _now())
        assert result.state is IncidentState.ESCALATED
        # The standing cause is still what the human is told (the condition's reason is the
        # escalation's, as ADR 0056 already had it), and no third attempt is recorded.
        assert "stale_lag_sensor" in result.evidence[-1].result_summary
        assert _attempt_marker(result) is None, (
            "at the cap the run escalates rather than recording another attempt to make"
        )
        # And the cap is what made the difference, not the gate: one more allowance and
        # the identical run reinvestigates.
        assert self._verify(max_attempts=3)(run, _now()).state is IncidentState.INVESTIGATING

    def test_the_gate_is_live_even_when_the_alert_names_a_subject(self) -> None:
        """A second independent fault is not something an alert can name.

        Its dead-letter sibling (`_uncleared_alert_condition`) is inert under a subject by
        design — ADR 0032 has already scoped the action. This question is about the run's
        own diagnosis, so scoping it the same way would make it inert exactly where a
        dual-fault world needs it.
        """
        assert _GROUP_ALERT["consumer_group"], "the fixture alert must name a subject"
        run = self._run((HypothesisCategory.STALE_CACHE, "stale_lag_sensor", 0.8))
        assert self._verify()(run, _now()).state is IncidentState.INVESTIGATING
