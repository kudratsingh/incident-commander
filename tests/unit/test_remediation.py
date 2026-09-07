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
from pydantic import BaseModel

from incident_commander.agent.hypothesis import Hypothesis, HypothesisCategory
from incident_commander.agent.remediation import (
    RemediationPlan,
    _evidence_value_corpus,
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
        alert={
            "source": "platform.kafka",
            "severity": "high",
            "consumer_group": "worker-dispatcher",
        },
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
            hypotheses=(
                Hypothesis(
                    category=HypothesisCategory.CONSUMER_SATURATION,
                    name="consumer_saturation",
                    confidence=0.85,
                    reasoning="r",
                ),
            ),
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
        # Post-hardening: RemediationPlan.action_tool is Literal-typed to
        # Tier-1 tools. A read-tool value is a schema violation; Pydantic
        # rejects at validation time. The make_llm_plan ValidationError
        # catch escalates with a "planner LLM invalid" reason mentioning
        # the rejected value.
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
        # Post-hardening: RemediationPlan.verify_tool is Literal-typed
        # to read tools. A Tier-1 write value is rejected by Pydantic; the
        # planner LLM catch escalates.
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
        # R2-38: the platform returned a NON-error result, so the Tier-1
        # action ran; only our parse of its response failed. Dropping
        # attempted_tool here hid an executed action from the SAFETY grader
        # and from the human reading the briefing.
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
        # The action executed, so it costs a tool call and a remediation
        # attempt — same as the success path. Charging nothing let a run
        # spend an unbudgeted Tier-1 action.
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


class TestSingleAttemptInvariant:
    """One Tier-1 attempt per incident (ADR 0008).

    Under the current allowed-transition graph, PLANNING is only
    reachable from INVESTIGATING (attempts == 0). VERIFYING has no
    PLANNING successor. The check in ``make_llm_plan`` is therefore
    an *invariant guard*, not a soft limit — it fires only if the
    state machine graph is mutated or a RunState is constructed
    directly bypassing dispatch. Practice 8 requires that guard to
    have a matching test.
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
        )
        result = transition(run, _now())
        assert result.state is IncidentState.REMEDIATING

    def test_impossible_state_fires_invariant_guard(self) -> None:
        # Construct the state directly (bypassing dispatch, which would
        # never allow this reachable). LLM queue empty — the guard must
        # escalate before any planner tokens are spent.
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
            remediation_attempts=1,
        )
        result = transition(run, _now())
        assert result.state is IncidentState.ESCALATED
        assert any("invariant violation (ADR 0008)" in e.result_summary for e in result.evidence)
        assert llm.calls == []  # guard fires before spending planner tokens

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


class TestEvidenceSourcedArgs:
    """Copy, don't re-type: plan resource args must be platform-produced.

    Campaign exhibit: the planner rebuilt an alert-provided cache key as
    `worker-dispatcher:hot_set` (dropping the `cache:jobs:` prefix); the
    platform allowlist refused it. This validator rejects the plan before
    any call is attempted — and because the bad value is a SUBSTRING of
    the true key, matching must be exact-value, not containment.
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
        # Verifies through get_cache_key_info on the same key. This class is
        # about SOURCING — whether the key came from the platform's own
        # words — and the verify leg is scaffolding for that question. It
        # used to be `get_redis_health` with no arguments, which ADR 0025
        # now refuses: server-wide keyspace counters cannot observe one key
        # (the 2026-09-07 live run). Keeping the old leg here would have
        # meant these sourcing assertions passing or failing for a reason
        # that has nothing to do with sourcing.
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
        # "worker-dispatcher:hot_set" is inside the true key — containment
        # matching would fake-green this exact campaign failure.
        llm = CannedLLMClient([self._cache_plan("worker-dispatcher:hot_set")])
        result = make_llm_plan(llm, model=_MODEL)(self._cache_run(), _now())
        assert result.state is IncidentState.ESCALATED
        reasons = " ".join(e.result_summary for e in result.evidence)
        assert "not evidence-sourced" in reasons
        assert "worker-dispatcher:hot_set" in reasons

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
        result = make_llm_plan(CannedLLMClient([plan]), model=_MODEL)(run, _now())
        assert result.state is IncidentState.ESCALATED
        reasons = " ".join(e.result_summary for e in result.evidence)
        assert invented in reasons
        # Cut on the message's real delimiters. Splitting on "." stopped at
        # the dot inside "replay_dlq_by_ids.job_ids=", so the inspected slice
        # was ": replay_dlq_by_ids" — a span that can never hold a UUID, and
        # a negative assertion that could not fail whatever the rejection
        # named (WO-R2-102).
        named = reasons.split("not evidence-sourced: ", 1)[1].split(". Resource names")[0]
        assert invented in named, "the rejection must name the id it refused"
        assert known not in named, "the evidence-sourced id must not be blamed"

    def test_non_resource_fields_are_unconstrained(self) -> None:
        # category / max_replays / delay_seconds are parameters, not
        # resource names — the planner may choose them freely.
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
    # get_consumer_lag accepts ANY group name and returns lag:null for
    # unknown ones (platform: backend/app/mcp/tools/consumer_lag.py), and
    # its output echoes consumer_group back (cache_key embeds it). A
    # hallucinated name used once as a probe argument must not whitelist
    # itself for the Tier-1 action — neither via argument ingestion nor
    # via the platform's echo of it in the result.

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
        result = make_llm_plan(CannedLLMClient([self._laundering_plan()]), model=_MODEL)(
            run, _now()
        )
        assert result.state is IncidentState.ESCALATED
        reasons = " ".join(e.result_summary for e in result.evidence)
        assert "not evidence-sourced" in reasons
        assert self._HALLUCINATED_GROUP in reasons

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
        # Echo exclusion is per-entry, not global: a job id the platform
        # produced in one result stays corpus-eligible even after a later
        # probe echoes it back as an argument.
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

    The hole this closes: ``GetConsumerLagInput.consumer_group`` carries
    ``default="worker-dispatcher"`` (mirroring the platform's published
    input schema, which the contract snapshot pins). When the planner
    omitted the group on the verify leg, ``_unsourced_resource_args``
    skipped the absent field, ``wire_arguments`` default-filled it, and
    the run restarted one consumer group while verifying a *different*,
    healthy one — then reported RESOLVED on a still-broken consumer.
    """

    _REMEDIATED = "billing-consumer"
    _DEFAULT_FILLED = "worker-dispatcher"  # GetConsumerLagInput's default

    def _run(self, **overrides: Any) -> RunState:
        run = _run_state(
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
        return run.model_copy(
            update={
                "alert": {
                    "source": "platform.kafka",
                    "severity": "high",
                    "consumer_group": self._REMEDIATED,
                }
            }
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
        # The confirmed defect. Pre-fix this reached REMEDIATING and the
        # verify probe read `worker-dispatcher` — a group the incident
        # never touched.
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
        # `get_dag_state.job_id` is REQUIRED, so wire_arguments would have
        # raised — but only inside VERIFYING, i.e. after the Tier-1 pause
        # already executed. Absence is a planning-time rejection for every
        # resource field, not just the default-carrying ones.
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
        # `list_dlq_messages` names no resource, so an empty verify leg is
        # still a legal plan — the absence check reads RESOURCE_ARG_FIELDS,
        # it does not demand arguments per se.
        #
        # This used to be asserted with `invalidate_cache_key` verified by
        # `get_redis_health`, which ADR 0025 now refuses: a read that names
        # no resource is only legal when NO read observes the acted-on
        # resource, and `get_cache_key_info` observes a cache key. The
        # property under test here is unchanged and still true; only the
        # example had to move to an action where it still holds. Bulk
        # category replay is that action — it names a category, not a row.
        run = self._run()
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
        # Both names are legitimately platform-produced, so the
        # evidence-sourcing guard has no objection to either one. Only the
        # cross-leg check can catch this plan.
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

    ``client.py`` puts the system prompt behind ``cache_control``, so on a
    live call most input volume arrives on ``cache_creation_tokens`` /
    ``cache_read_tokens``. Summing only input+output metered the un-cached
    remainder and under-enforced ``BUDGET_MAX_TOKENS`` exactly when caching
    worked well.
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
        run = _run_state(state=IncidentState.PLANNING, hypotheses=self._hypotheses())
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
        run = _run_state(state=IncidentState.PLANNING, hypotheses=self._hypotheses())
        result = transition(run, _now())
        assert result.budget.tokens_used == 0
        assert result.budget.usd_used == Decimal("0")

    def test_usd_ceiling_trips_from_a_single_expensive_plan(self) -> None:
        """The USD dimension is reachable: one call can exhaust the ledger."""
        llm = CannedLLMClient([_plan_dict()], usage=CannedUsage(output_tokens=200_000))
        transition = make_llm_plan(llm, model="claude-sonnet-4-6")
        run = _run_state(state=IncidentState.PLANNING, hypotheses=self._hypotheses())
        result = transition(run, _now())
        # 200_000 * 15.00 / 1e6 = 3.00 against the fixture's 1.00 cap.
        assert result.budget.usd_used == Decimal("3.000000")
        assert result.budget.is_exhausted


class TestRejectedPlansAreStillBilled:
    """A plan the agent throws away is a plan the platform charged for.

    The accrual used to sit after six validation branches, every one of
    which returns early. So the runs that made the most LLM calls — a
    planner producing rejectable plans — were the runs whose spend the
    ledger saw least of. ADR 0015: over-report, never under-report.
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
        run = _run_state(state=IncidentState.PLANNING, hypotheses=self._hypotheses())
        return transition(run, _now())

    # Only the branches a schema-valid plan can reach. The tier/registry
    # branches above them are defense-in-depth against registry drift:
    # ``action_tool`` and ``verify_tool`` are Literal-typed, so a payload
    # naming a bad tool is rejected by pydantic inside the client and
    # arrives as the LLMError path below, not as a rejected plan.
    @pytest.mark.parametrize(
        ("label", "overrides"),
        [
            ("absent resource argument", {"action_arguments": {}}),
            (
                "unsourced resource argument",
                {
                    "action_arguments": {"consumer_group": "never-mentioned"},
                    "verify_arguments": {"consumer_group": "never-mentioned"},
                },
            ),
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
            ) -> LLMResult[T]:
                raise LLMError(
                    "no record_output tool_use in response; stop_reason=max_tokens",
                    usage=LLMUsage(input_tokens=100, output_tokens=4096),
                )

        transition = make_llm_plan(_Truncating(), model="claude-sonnet-4-6")
        run = _run_state(state=IncidentState.PLANNING, hypotheses=self._hypotheses())
        result = transition(run, _now())
        assert result.state is IncidentState.ESCALATED
        assert result.budget.tokens_used == 4196


class TestVerifyLegObservesTheAction:
    """ADR 0025: the verify leg must be able to OBSERVE what the action changed.

    ``_misdirected_verify_args`` (ADR 0024) already refuses a verify leg
    that names a resource the action left alone. It is inert when the
    verify leg names no resource at all, and ADR 0024 recorded that
    inertness as a deliberate escape hatch: "the escape hatch is a
    resource-free verify tool". This class is the record that the escape
    hatch was too wide.

    The 2026-09-07 paid run of ``remediate_stale_cache_success`` is the
    exhibit. Chaos planted a 90-byte stale value; the agent probed the
    exact key, invalidated exactly that key (``deleted: true``), and then
    verified with ``get_redis_health``, whose ``keyspace_hits`` /
    ``keyspace_misses`` are server-wide. Nothing in the eval world reads
    that hot set, so the counters were dominated by unrelated traffic —
    hits frozen at 209 while misses climbed 437635 → 442480. The judge
    honestly answered ``not_verified`` six times and the agent escalated.
    Every other dimension was green. The agent was right; the plan asked a
    question the world could not answer.
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
        # THE regression. This exact plan — verbatim from the trajectory of
        # archive 7acd2b441961 — was ADMITTED before ADR 0025 and executed
        # to an unverifiable outcome.
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
        # A steer that arrives truncated is not a steer. Evidence lines are
        # cut at 200 chars; the refusal is rendered whole and separately, so
        # assert the planner's SECOND context actually carries the required
        # call — including the key, which sits past that cut.
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
        # `replay_dlq_by_category` names a category, not a row, so there is
        # no resource to demand an observation of. Declared inert in the
        # map rather than merely absent from it.
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
            ),
            _now(),
        )
        assert result.state is IncidentState.REMEDIATING

    def test_targeted_replay_verified_by_the_listing_stays_legal(self) -> None:
        # `list_dlq_messages` observes DLQ rows without being able to name
        # one, so picking it IS the requirement — there is no value to
        # match, and demanding one would refuse every correct replay plan.
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
        # An action whose resource field is missing entirely is
        # `_absent_resource_args`' business. This guard must not also fire
        # and bury the actionable message under a second one.
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

    The judge is asked "did the action work?", which is not "is the
    incident over?". For ``pause_dag`` the two answers come apart, and the
    platform's own tool description is what pulls them apart: it says a
    successful pause "reads as paused=true with children still in
    `waiting`". Hand that reading to a judge holding an expectation of
    "children stop advancing" and the honest verdict is ``verified`` — on a
    chain that is exactly as stuck as it was, and that will be stuck again
    the moment the 10-minute TTL lapses. There is no judge prompt that
    fixes this, because the judge is not wrong.

    ``policies.RESOLUTION_CLASS`` is the separation, and this class is the
    proof that it reaches the one RESOLVED transition the state machine
    has (``remediation.transition_verify``). Before it, the same inputs
    produced ``IncidentState.RESOLVED``.

    Found by a pre-spend sweep of ``remediate_runaway_saga_success`` on
    2026-09-07, before the scenario's first paid run. Nothing had executed
    a ``pause_dag`` plan live; the defect was reachable and unexercised.
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

        Worth pinning separately: if a later change made this pass by
        making the judge say ``not_verified``, the class would go green
        while the actual guarantee — "a working stabilizer still
        escalates" — had been replaced by "a stabilizer is graded as a
        failure", which is a different and wrong claim.
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

        ``agent/briefing.py`` reads ``attempted_tool`` off the terminal
        marker to tell the on-call which Tier-1 action already fired, and
        the briefing writer is told never to recommend repeating it. This
        is the escalation where that matters most: a pause is holding
        right now and expires on a timer, so "pause it" is the one
        recommendation that must not come back.
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

        Only the verify probe is new spend here. ``_escalate_remediation``
        takes ``executed=True`` for the one branch where the platform acted
        and no entry was written — this is not that branch, and charging it
        would bill a run for work it already paid for and inflate
        ``remediation_attempts`` past the single-attempt invariant.
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

        Same verify tool, same probe response shape, same judge verdict —
        only the action differs. Without this, a regression that simply
        stopped ``get_dag_state`` verifications from ever resolving would
        leave every assertion above green.
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


class TestReplayRequiresTheJobsDeadLetterRow:
    """ADR 0027: a dead-lettered job may not be replayed until its own
    dead-letter row is in the evidence.

    The gap this closes is narrow and was invisible to every existing
    guard. ``_unsourced_resource_args`` requires the job id to be a string
    the platform produced, and it is: the alert carries it, and
    ``get_dag_state`` echoes it back as ``seed_id`` and as a node ``id``.
    ``_unobserved_action_resource`` requires a verify probe that can observe
    it, and ``get_dag_state`` genuinely can. So a run could read a chain,
    see ``"status": "dead_letter"`` on its root, and replay that root having
    learned nothing whatsoever about whether restarting it was safe —
    because ``get_dag_state``'s node model is five fields and
    ``remediation_hint`` is not one of them.

    The platform's own rule, in ``list_dlq_messages``' description: "A null
    hint is UNKNOWN, not replay-safe: do not feed those to a categorised
    replay. Read the error, then replay by explicit id, or fence it with
    ``mark_dlq_permanent``." An unread row is strictly less than a null one.
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

        PLANNING is one LLM call with no tool budget, so the planner cannot
        go and fetch a row it is missing — but it CAN drop the ids it has no
        row for. A batch carrying one listed job and one unlisted one is
        repaired by replaying the first.
        """
        llm = CannedLLMClient(
            [
                self._replay_plan(self._OTHER, self._ROOT, verify_id=self._OTHER),
                self._replay_plan(self._OTHER),
            ]
        )
        result = make_llm_plan(llm, model=_MODEL)(
            self._saga_run((self._chain_probe(), self._dlq_probe(self._OTHER))), _now()
        )

        assert result.state is IncidentState.REMEDIATING
        assert result.remediation_plan is not None
        replanned = RemediationPlan.model_validate(result.remediation_plan)
        assert replanned.action_arguments["job_ids"] == [self._OTHER]

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

        ``wait_and_replay`` warrants a deferred replay and ``replay_safe`` an
        immediate one — both legitimate, and choosing between them is the
        planner's job. A structural rule that only admitted one value would
        be making that decision, and would refuse the correct plan for every
        `wait_and_replay` scenario in the suite.
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
        """The precise hole. ``_unsourced_resource_args`` is satisfied by the
        alert's own job_id, so before this guard the evidence corpus said
        "the platform produced this string" and nothing said "somebody read
        this job's classification"."""
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
        """Declared inert: the tool names a filter, not ids, so there is
        nothing to look up. `remediate_dlq_backlog_success` and the two
        `dlq_*` category scenarios stay admitted."""
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
        )
        result = make_llm_plan(llm, model=_MODEL)(run, _now())

        assert result.state is IncidentState.REMEDIATING
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
        )
        result = make_llm_plan(llm, model=_MODEL)(run, _now())

        assert result.state is IncidentState.REMEDIATING
        assert len(llm.calls) == 1
