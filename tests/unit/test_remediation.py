"""Unit tests for the Phase 6 remediation loop (plan → execute → verify).

Covers the three transitions in ``agent/remediation.py`` end-to-end with
canned LLM + canned MCP clients. Also asserts idempotency key semantics
and the tier-policy guardrails on planner output.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest

from incident_commander.agent.hypothesis import Hypothesis
from incident_commander.agent.remediation import (
    RemediationPlan,
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
from incident_commander.llm.fakes import CannedLLMClient
from incident_commander.tools.mcp_client import MCPError, ToolResult

_MODEL = "test-model"


class _FakeMCP:
    def __init__(self, handler: Callable[[str, Mapping[str, Any]], ToolResult]) -> None:
        self._handler = handler
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call_tool(self, name: str, arguments: Mapping[str, Any]) -> ToolResult:
        self.calls.append((name, dict(arguments)))
        return self._handler(name, arguments)


def _now() -> datetime:
    return datetime(2026, 7, 30, 12, 0, tzinfo=UTC)


def _run_state(
    *,
    state: IncidentState,
    hypotheses: tuple[Hypothesis, ...] = (),
    remediation_plan: dict[str, Any] | None = None,
    evidence: tuple[EvidenceEntry, ...] = (),
    remediation_attempts: int = 0,
) -> RunState:
    return RunState(
        incident_id=UUID("11111111-1111-1111-1111-111111111111"),
        state=state,
        alert={"source": "platform.kafka", "severity": "high"},
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
            hypotheses=(Hypothesis(name="consumer_saturation", confidence=0.85, reasoning="r"),),
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
        # Planner picked a read tool as the action — guardrail should catch it.
        bad = _plan_dict(action_tool="get_consumer_lag")
        transition = make_llm_plan(self._canned_planner(bad), model=_MODEL)
        run = _run_state(
            state=IncidentState.PLANNING,
            hypotheses=(Hypothesis(name="x", confidence=0.9, reasoning="r"),),
        )
        result = transition(run, _now())
        assert result.state is IncidentState.ESCALATED
        assert any("non-Tier-1" in e.result_summary for e in result.evidence)

    def test_unknown_action_tool_escalates(self) -> None:
        bad = _plan_dict(action_tool="made_up_action")
        transition = make_llm_plan(self._canned_planner(bad), model=_MODEL)
        run = _run_state(
            state=IncidentState.PLANNING,
            hypotheses=(Hypothesis(name="x", confidence=0.9, reasoning="r"),),
        )
        result = transition(run, _now())
        assert result.state is IncidentState.ESCALATED
        assert any("unknown action tool" in e.result_summary for e in result.evidence)

    def test_non_read_verify_tool_escalates(self) -> None:
        # Planner picked a Tier-1 write action as the verify tool.
        bad = _plan_dict(verify_tool="restart_consumer_group")
        transition = make_llm_plan(self._canned_planner(bad), model=_MODEL)
        run = _run_state(
            state=IncidentState.PLANNING,
            hypotheses=(Hypothesis(name="x", confidence=0.9, reasoning="r"),),
        )
        result = transition(run, _now())
        assert result.state is IncidentState.ESCALATED
        assert any("verify tool must be read-only" in e.result_summary for e in result.evidence)


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
                                "kill_key": f"chaos:kill:{args['consumer_group']}",
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


class TestVerifying:
    def _mcp(self, lag: int) -> _FakeMCP:
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
                                "cache_key": "kafka:consumer_lag:worker-dispatcher",
                            }
                        ),
                    }
                ]
            )

        return _FakeMCP(handler)

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


class TestCrashRecoveryReconciliation:
    """REMEDIATING must be safe against re-entry after a mid-flight crash.

    Even though the platform's idempotency store makes re-execution safe,
    we short-circuit when evidence already shows the action ran — that
    keeps the trajectory clean and avoids a duplicate audit entry.
    """

    def _mock_action_evidence(self, tool_name: str) -> EvidenceEntry:
        return EvidenceEntry(
            tool_name=tool_name,
            arguments={
                "consumer_group": "worker-dispatcher",
                "idempotency_key": "pretend-this-was-real",
            },
            result_summary='{"consumer_group":"worker-dispatcher","accepted":true}',
            timestamp=_now(),
        )

    def test_reconciles_when_action_already_in_evidence(self) -> None:
        mcp = _FakeMCP(lambda _n, _a: ToolResult(content=[]))  # would blow up
        transition = make_remediate(mcp)
        run = _run_state(
            state=IncidentState.REMEDIATING,
            remediation_plan=_plan_dict(),
            evidence=(self._mock_action_evidence("restart_consumer_group"),),
        )
        result = transition(run, _now())
        # Skipped execution — MCP was never called.
        assert mcp.calls == []
        # But still advanced to VERIFYING with a reconciliation note.
        assert result.state is IncidentState.VERIFYING
        reconciles = [e for e in result.evidence if e.tool_name == "_remediate_reconciled"]
        assert len(reconciles) == 1
        assert "already invoked" in reconciles[0].result_summary

    def test_does_not_reconcile_when_evidence_is_for_different_action(self) -> None:
        # Evidence has an entry for a DIFFERENT Tier-1 tool → still execute.
        captured: list[str] = []

        def handler(name: str, args: Mapping[str, Any]) -> ToolResult:
            captured.append(name)
            return ToolResult(
                content=[
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "consumer_group": args["consumer_group"],
                                "kill_key_cleared": True,
                                "kill_key": "k",
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
            evidence=(self._mock_action_evidence("invalidate_cache_key"),),
        )
        result = transition(run, _now())
        assert captured == ["restart_consumer_group"]
        assert result.state is IncidentState.VERIFYING

    def test_re_entry_does_not_double_count_attempts(self) -> None:
        # First REMEDIATING attempt already happened (evidence + attempts=1).
        # Re-entering must NOT bump the counter — no new action was taken.
        mcp = _FakeMCP(lambda _n, _a: ToolResult(content=[]))
        transition = make_remediate(mcp)
        run = _run_state(
            state=IncidentState.REMEDIATING,
            remediation_plan=_plan_dict(),
            evidence=(self._mock_action_evidence("restart_consumer_group"),),
            remediation_attempts=1,
        )
        result = transition(run, _now())
        assert result.remediation_attempts == 1


class TestAttemptCap:
    """One Tier-1 attempt per incident. PLANNING refuses to propose a retry."""

    def test_first_attempt_transitions_normally(self) -> None:
        llm = CannedLLMClient([_plan_dict()])
        transition = make_llm_plan(llm, model=_MODEL)
        run = _run_state(
            state=IncidentState.PLANNING,
            hypotheses=(Hypothesis(name="consumer_saturation", confidence=0.85, reasoning="r"),),
            remediation_attempts=0,
        )
        result = transition(run, _now())
        assert result.state is IncidentState.REMEDIATING

    def test_second_attempt_escalates_without_calling_llm(self) -> None:
        # LLM queue is EMPTY — if PLANNING calls it, we'd get a fake error
        # rather than a real plan. Test guards that no LLM call happens.
        llm = CannedLLMClient([])
        transition = make_llm_plan(llm, model=_MODEL)
        run = _run_state(
            state=IncidentState.PLANNING,
            hypotheses=(Hypothesis(name="x", confidence=0.9, reasoning="r"),),
            remediation_attempts=1,
        )
        result = transition(run, _now())
        assert result.state is IncidentState.ESCALATED
        assert any("attempt cap reached" in e.result_summary for e in result.evidence)

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
