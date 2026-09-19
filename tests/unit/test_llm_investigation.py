from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest

from incident_commander.agent.hypothesis import HypothesisCategory
from incident_commander.agent.investigation import alert_subject, make_llm_investigate
from incident_commander.agent.state import IncidentState, RunState
from incident_commander.llm.client import LLMResult
from incident_commander.llm.fakes import CannedLLMClient, CannedUsage
from incident_commander.tools import policies
from incident_commander.tools.mcp_client import MCPError, ToolResult


class _FakeMCPClient:
    def __init__(
        self,
        handler: Callable[[str, Mapping[str, Any]], ToolResult],
    ) -> None:
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


def _consumer_lag_response(group: str, lag: int) -> ToolResult:
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
                        "cache_key": "kafka:consumer_lag:worker-dispatcher",
                    }
                ),
            }
        ]
    )


def _probe_then_stop_llm(
    group: str = "billing", usage: CannedUsage | None = None
) -> CannedLLMClient:
    return CannedLLMClient(
        [
            {
                "hypotheses": [
                    {
                        "category": "consumer_saturation",
                        "name": "consumer_saturation",
                        "confidence": 0.55,
                        "reasoning": "Alert severity suggests saturation.",
                    }
                ],
                "next_action": {
                    "kind": "probe",
                    "tool_name": "get_consumer_lag",
                    "arguments": {"consumer_group": group},
                },
            },
            {
                "hypotheses": [
                    {
                        "category": "consumer_saturation",
                        "name": "consumer_saturation",
                        "confidence": 0.9,
                        "reasoning": "Lag reading confirms saturation.",
                    }
                ],
                "next_action": {
                    "kind": "stop",
                    "reason": "confidence sufficient for handoff",
                },
            },
        ],
        usage=usage,
    )


def _investigating(run_state: RunState, group: str = "billing") -> RunState:
    return run_state.model_copy(
        update={
            "state": IncidentState.INVESTIGATING,
            "alert": {"source": "kafka", "severity": "high", "group": group},
        }
    )


class TestHappyPath:
    def test_probe_then_stop_escalates_with_hypotheses(
        self, run_state: RunState, now: datetime
    ) -> None:
        mcp = _FakeMCPClient(lambda _n, _a: _consumer_lag_response("billing", 42))
        llm = _probe_then_stop_llm()
        transition = make_llm_investigate(mcp, llm, model="claude-sonnet-4-6")
        result = transition(_investigating(run_state), now)

        assert result.state is IncidentState.ESCALATED
        assert result.hypotheses[0].name == "consumer_saturation"
        assert result.hypotheses[0].confidence == 0.9
        assert result.budget.tool_calls_used == 1

    def test_evidence_records_probe_output(self, run_state: RunState, now: datetime) -> None:
        mcp = _FakeMCPClient(lambda _n, _a: _consumer_lag_response("billing", 42))
        llm = _probe_then_stop_llm()
        transition = make_llm_investigate(mcp, llm, model="m")
        result = transition(_investigating(run_state), now)
        tool_evidence = [e for e in result.evidence if e.tool_name == "get_consumer_lag"]
        assert len(tool_evidence) == 1
        assert '"lag":42' in tool_evidence[0].result_summary

    def test_planner_stop_recorded(self, run_state: RunState, now: datetime) -> None:
        mcp = _FakeMCPClient(lambda _n, _a: _consumer_lag_response("billing", 42))
        transition = make_llm_investigate(mcp, _probe_then_stop_llm(), model="m")
        result = transition(_investigating(run_state), now)
        stops = [e for e in result.evidence if e.tool_name == "_planner_stop"]
        assert len(stops) == 1
        assert "confidence sufficient" in stops[0].result_summary


class TestImmediateStop:
    def test_planner_stops_without_probing(self, run_state: RunState, now: datetime) -> None:
        mcp = _FakeMCPClient(lambda _n, _a: _consumer_lag_response("billing", 0))
        llm = CannedLLMClient(
            [
                {
                    "hypotheses": [
                        {
                            "category": "unknown",
                            "name": "false_positive",
                            "confidence": 0.8,
                            "reasoning": "Alert without actionable signal.",
                        }
                    ],
                    "next_action": {
                        "kind": "stop",
                        "reason": "no discriminating probe available",
                    },
                }
            ]
        )
        transition = make_llm_investigate(mcp, llm, model="m")
        result = transition(_investigating(run_state), now)
        assert result.state is IncidentState.ESCALATED
        assert result.budget.tool_calls_used == 0
        assert mcp.calls == []


class TestRemediateHandoff:
    def test_planner_remediate_transitions_to_planning(
        self, run_state: RunState, now: datetime
    ) -> None:
        # The probe step is load-bearing: the alert names group "billing" and the alert-subject
        # guard (TestAlertSubjectProbeGuard) refuses a handoff while that group sits unread.
        mcp = _FakeMCPClient(lambda _n, _a: _consumer_lag_response("billing", 42))
        llm = CannedLLMClient(
            [
                {
                    "hypotheses": [
                        {
                            "category": "consumer_saturation",
                            "name": "consumer_saturation",
                            "confidence": 0.9,
                            "reasoning": "top hypothesis clear",
                        }
                    ],
                    "next_action": {
                        "kind": "probe",
                        "tool_name": "get_consumer_lag",
                        "arguments": {"consumer_group": "billing"},
                    },
                },
                {
                    "hypotheses": [
                        {
                            "category": "consumer_saturation",
                            "name": "consumer_saturation",
                            "confidence": 0.9,
                            "reasoning": "top hypothesis clear",
                        }
                    ],
                    "next_action": {
                        "kind": "remediate",
                        "reason": "consumer_saturation confirmed; restart_consumer_group applies",
                    },
                },
            ]
        )
        transition = make_llm_investigate(mcp, llm, model="m")
        result = transition(_investigating(run_state), now)
        assert result.state is IncidentState.PLANNING
        handoffs = [e for e in result.evidence if e.tool_name == "_planner_remediate"]
        assert len(handoffs) == 1
        assert "consumer_saturation confirmed" in handoffs[0].result_summary
        # Hypotheses carried forward for the remediation planner to consume.
        assert result.hypotheses[0].name == "consumer_saturation"


class TestErrorPaths:
    def test_unknown_tool_from_planner_escalates(self, run_state: RunState, now: datetime) -> None:
        mcp = _FakeMCPClient(lambda _n, _a: _consumer_lag_response("billing", 42))
        llm = CannedLLMClient(
            [
                {
                    "hypotheses": [
                        {"category": "unknown", "name": "x", "confidence": 0.5, "reasoning": "r"}
                    ],
                    "next_action": {
                        "kind": "probe",
                        "tool_name": "made_up_tool",
                        "arguments": {},
                    },
                }
            ]
        )
        transition = make_llm_investigate(mcp, llm, model="m")
        result = transition(_investigating(run_state), now)
        assert result.state is IncidentState.ESCALATED
        escalations = [e for e in result.evidence if e.tool_name == "_planner_escalate"]
        # ProbeAction.tool_name is a Literal, so Pydantic rejects
        # "made_up_tool" at validation; the escalation names it.
        assert any(
            "made_up_tool" in e.result_summary or "invalid" in e.result_summary for e in escalations
        )

    def test_tool_error_escalates(self, run_state: RunState, now: datetime) -> None:
        def erroring(_n: str, _a: Mapping[str, Any]) -> ToolResult:
            raise MCPError(-32602, "boom")

        mcp = _FakeMCPClient(erroring)
        llm = _probe_then_stop_llm()
        transition = make_llm_investigate(mcp, llm, model="m")
        result = transition(_investigating(run_state), now)
        assert result.state is IncidentState.ESCALATED
        escalations = [e for e in result.evidence if e.tool_name == "_planner_escalate"]
        assert any("tool error" in e.result_summary for e in escalations)
        assert result.budget.tool_calls_used == 0

    def test_uuid_probe_arguments_are_json_stringified(
        self, run_state: RunState, now: datetime
    ) -> None:
        """UUID fields must serialize to strings so httpx.json can encode them.

        Regression: a `.model_dump()` without `mode="json"` crashed the saga_stuck batch.
        """
        captured: dict[str, Any] = {}

        def handler(name: str, arguments: Mapping[str, Any]) -> ToolResult:
            captured["args"] = dict(arguments)
            return ToolResult(
                content=[
                    {
                        "type": "text",
                        "text": (
                            '{"seed_id":"33333333-3333-3333-3333-333333333333",'
                            '"nodes":[],"edges":[]}'
                        ),
                    }
                ]
            )

        mcp = _FakeMCPClient(handler)
        llm = CannedLLMClient(
            [
                {
                    "hypotheses": [
                        {"category": "unknown", "name": "x", "confidence": 0.6, "reasoning": "r"}
                    ],
                    "next_action": {
                        "kind": "probe",
                        "tool_name": "get_dag_state",
                        "arguments": {"job_id": "33333333-3333-3333-3333-333333333333"},
                    },
                },
                {
                    "hypotheses": [
                        {"category": "unknown", "name": "x", "confidence": 0.9, "reasoning": "r"}
                    ],
                    "next_action": {"kind": "stop", "reason": "done"},
                },
            ]
        )
        transition = make_llm_investigate(mcp, llm, model="m")
        transition(_investigating(run_state), now)
        # The arguments passed to the MCP client must be JSON-encodable —
        # a UUID object would fail httpx.json under the hood.
        assert isinstance(captured["args"]["job_id"], str)
        json.dumps(captured["args"])  # would raise TypeError if not encodable

    def test_output_schema_mismatch_escalates(self, run_state: RunState, now: datetime) -> None:
        mcp = _FakeMCPClient(
            lambda _n, _a: ToolResult(
                content=[
                    {
                        "type": "text",
                        # Missing required `cache_key` field triggers schema mismatch.
                        "text": '{"consumer_group":"billing","lag":42}',
                    }
                ]
            )
        )
        transition = make_llm_investigate(mcp, _probe_then_stop_llm(), model="m")
        result = transition(_investigating(run_state), now)
        assert result.state is IncidentState.ESCALATED
        assert any("output parse failed" in e.result_summary for e in result.evidence)


class TestBudgetGuards:
    def test_exhausted_tokens_escalates_before_llm_call(
        self, run_state: RunState, now: datetime
    ) -> None:
        run = run_state.model_copy(
            update={
                "state": IncidentState.INVESTIGATING,
                "alert": {"source": "kafka", "severity": "high", "group": "billing"},
                "budget": run_state.budget.model_copy(
                    update={"tokens_used": run_state.budget.max_tokens}
                ),
            }
        )
        mcp = _FakeMCPClient(lambda _n, _a: _consumer_lag_response("billing", 42))
        llm = _probe_then_stop_llm()
        transition = make_llm_investigate(mcp, llm, model="m")
        result = transition(run, now)
        assert result.state is IncidentState.ESCALATED
        # No LLM calls happened.
        assert llm.calls == []

    def test_max_iterations_stops_probe_loop(self, run_state: RunState, now: datetime) -> None:
        # Planner always probes, never stops.
        mcp = _FakeMCPClient(lambda _n, _a: _consumer_lag_response("billing", 42))
        endless_probe = {
            "hypotheses": [
                {
                    "category": "unknown",
                    "name": "loop",
                    "confidence": 0.3,
                    "reasoning": "keep probing",
                }
            ],
            "next_action": {
                "kind": "probe",
                "tool_name": "get_consumer_lag",
                "arguments": {"consumer_group": "billing"},
            },
        }
        llm = CannedLLMClient([endless_probe] * 10)
        transition = make_llm_investigate(mcp, llm, model="m", max_iterations=3)
        result = transition(_investigating(run_state), now)
        assert result.state is IncidentState.ESCALATED
        # Three iterations = three probes.
        assert result.budget.tool_calls_used == 3
        assert any("max iterations" in e.result_summary for e in result.evidence)

    def test_usd_ceiling_trips_and_escalates_before_the_probe(
        self, run_state: RunState, now: datetime
    ) -> None:
        """C-05/B-01: the dollar dimension is reachable from a real run.

        At HEAD ``usd_used`` had no writer, so ``BUDGET_MAX_USD`` could
        never trip: this run spent the probe and stopped normally instead.
        """
        mcp = _FakeMCPClient(lambda _n, _a: _consumer_lag_response("billing", 42))
        # One planner call at 1000 in / 1000 out on sonnet costs $0.018.
        llm = _probe_then_stop_llm(usage=CannedUsage(input_tokens=1000, output_tokens=1000))
        run = _investigating(run_state).model_copy(
            update={"budget": run_state.budget.model_copy(update={"max_usd": Decimal("0.001")})}
        )
        transition = make_llm_investigate(mcp, llm, model="claude-sonnet-4-6")
        result = transition(run, now)

        assert result.state is IncidentState.ESCALATED
        assert result.budget.usd_used == Decimal("0.018000")
        assert result.budget.is_exhausted
        assert any("budget exhausted" in e.result_summary for e in result.evidence)
        # The probe never ran: the ceiling stopped the spend.
        assert result.budget.tool_calls_used == 0
        assert mcp.calls == []

    def test_planner_call_charges_cache_tokens(self, run_state: RunState, now: datetime) -> None:
        """C-06 at the investigation accrual site."""
        mcp = _FakeMCPClient(lambda _n, _a: _consumer_lag_response("billing", 42))
        llm = _probe_then_stop_llm(
            usage=CannedUsage(
                input_tokens=100,
                output_tokens=50,
                cache_creation_tokens=2_000,
                cache_read_tokens=8_000,
            )
        )
        transition = make_llm_investigate(mcp, llm, model="claude-sonnet-4-6")
        result = transition(_investigating(run_state), now)
        # Two planner calls x (100 + 50 + 2000 + 8000) tokens, $0.010950 each.
        assert result.budget.tokens_used == 20_300
        assert result.budget.usd_used == Decimal("0.021900")

    def test_llm_tokens_billed_to_budget(self, run_state: RunState, now: datetime) -> None:
        from pydantic import BaseModel

        class _CountingLLM:
            def __init__(self) -> None:
                self.calls = 0

            def call[T: BaseModel](
                self,
                system_prompt: str,
                user_message: str,
                output_model: type[T],
                model: str,
                max_tokens: int = 2048,
                *,
                repair_of: str | None = None,
                temperature: float | None = None,
            ) -> LLMResult[T]:
                self.calls += 1
                payload = {
                    "hypotheses": [
                        {"category": "unknown", "name": "x", "confidence": 0.9, "reasoning": "r"}
                    ],
                    "next_action": {"kind": "stop", "reason": "done"},
                }
                return LLMResult(
                    output=output_model.model_validate(payload),
                    input_tokens=1000,
                    output_tokens=500,
                    cache_creation_tokens=0,
                    cache_read_tokens=0,
                    stop_reason="tool_use",
                )

        mcp = _FakeMCPClient(lambda _n, _a: _consumer_lag_response("billing", 42))
        llm = _CountingLLM()
        transition = make_llm_investigate(mcp, llm, model="m")
        result = transition(_investigating(run_state), now)
        assert result.budget.tokens_used == 1500
        assert llm.calls == 1


def _hyp(category: str, confidence: float) -> dict[str, Any]:
    return {
        "category": category,
        "name": f"{category}-hypothesis",
        "confidence": confidence,
        "reasoning": "r",
    }


def _lag_probe(group: str = "worker-dispatcher") -> dict[str, Any]:
    return {
        "kind": "probe",
        "tool_name": "get_consumer_lag",
        "arguments": {"consumer_group": group},
    }


class TestFreshnessReprobe:
    """ADR 0009: a cached read that kills an actionable hypothesis gets one
    fresh re-read before the loop accepts the contradiction."""

    def _lag_sequence_mcp(self, readings: list[int]) -> _FakeMCPClient:
        """get_consumer_lag returns readings in order; other tools return a stub."""
        seen = {"lag_calls": 0}

        def handler(name: str, _args: Mapping[str, Any]) -> ToolResult:
            if name == "get_consumer_lag":
                lag = readings[min(seen["lag_calls"], len(readings) - 1)]
                seen["lag_calls"] += 1
                return _consumer_lag_response("worker-dispatcher", lag)
            return ToolResult(content=[{"type": "text", "text": '{"total":0,"items":[]}'}])

        return _FakeMCPClient(handler)

    def test_stale_kill_intercepts_even_a_stop_step(
        self, run_state: RunState, now: datetime
    ) -> None:
        # Stale 0 kills the hypothesis, the planner says stop, and the interceptor must
        # re-probe fresh — the 2026-08-03 campaign trace.
        llm = CannedLLMClient(
            [
                {
                    "hypotheses": [_hyp("consumer_saturation", 0.75)],
                    "next_action": _lag_probe(),
                },
                {
                    "hypotheses": [_hyp("poison_message", 0.4), _hyp("consumer_saturation", 0.2)],
                    "next_action": {"kind": "stop", "reason": "lag is zero, nothing to fix"},
                },
                {
                    "hypotheses": [_hyp("consumer_saturation", 0.9)],
                    "next_action": {"kind": "stop", "reason": "fresh read confirms saturation"},
                },
            ]
        )
        mcp = self._lag_sequence_mcp([0, 15000])
        slept: list[float] = []
        transition = make_llm_investigate(
            mcp,
            llm,
            model="m",
            reprobe_attempts=1,
            reprobe_delay_seconds=20.0,
            sleep=slept.append,
        )
        result = transition(_investigating(run_state), now)

        lag_calls = [c for c in mcp.calls if c[0] == "get_consumer_lag"]
        assert len(lag_calls) == 2
        assert slept == [20.0]
        markers = [e for e in result.evidence if e.tool_name == "_freshness_reprobe"]
        assert len(markers) == 1
        assert "consumer_saturation" in markers[0].result_summary
        # Step (iii) saw both readings and was acted on.
        assert result.hypotheses[0].confidence == 0.9
        assert result.budget.tool_calls_used == 2

    def test_default_zero_attempts_accepts_contradiction(
        self, run_state: RunState, now: datetime
    ) -> None:
        # Canned posture: no re-probe, the stop in (ii) terminates the run.
        llm = CannedLLMClient(
            [
                {
                    "hypotheses": [_hyp("consumer_saturation", 0.75)],
                    "next_action": _lag_probe(),
                },
                {
                    "hypotheses": [_hyp("consumer_saturation", 0.2)],
                    "next_action": {"kind": "stop", "reason": "lag is zero"},
                },
            ]
        )
        mcp = self._lag_sequence_mcp([0])
        transition = make_llm_investigate(mcp, llm, model="m")
        result = transition(_investigating(run_state), now)

        assert len([c for c in mcp.calls if c[0] == "get_consumer_lag"]) == 1
        assert not [e for e in result.evidence if e.tool_name == "_freshness_reprobe"]
        assert result.state is IncidentState.ESCALATED

    def test_non_cached_probe_never_triggers(self, run_state: RunState, now: datetime) -> None:
        # list_dlq_messages is a live read; a contradiction after it is real.
        llm = CannedLLMClient(
            [
                {
                    "hypotheses": [_hyp("poison_message", 0.8)],
                    "next_action": {
                        "kind": "probe",
                        "tool_name": "list_dlq_messages",
                        "arguments": {},
                    },
                },
                {
                    "hypotheses": [_hyp("poison_message", 0.1)],
                    "next_action": {"kind": "stop", "reason": "dlq is empty"},
                },
            ]
        )
        mcp = self._lag_sequence_mcp([0])
        slept: list[float] = []
        transition = make_llm_investigate(
            mcp, llm, model="m", reprobe_attempts=1, sleep=slept.append
        )
        result = transition(_investigating(run_state), now)

        assert slept == []
        assert not [e for e in result.evidence if e.tool_name == "_freshness_reprobe"]
        assert result.state is IncidentState.ESCALATED

    def test_below_threshold_prior_never_triggers(self, run_state: RunState, now: datetime) -> None:
        # 0.55 was never actionable, so its death changes no decision.
        llm = CannedLLMClient(
            [
                {
                    "hypotheses": [_hyp("consumer_saturation", 0.55)],
                    "next_action": _lag_probe(),
                },
                {
                    "hypotheses": [_hyp("consumer_saturation", 0.1)],
                    "next_action": {"kind": "stop", "reason": "healthy"},
                },
            ]
        )
        mcp = self._lag_sequence_mcp([0])
        slept: list[float] = []
        transition = make_llm_investigate(
            mcp, llm, model="m", reprobe_attempts=1, sleep=slept.append
        )
        transition(_investigating(run_state), now)
        assert slept == []

    def test_unmapped_category_never_triggers(self, run_state: RunState, now: datetime) -> None:
        # deploy_regression has no Tier-1 fix: it escalates at any
        # confidence, so a stale kill changes the briefing, not the action.
        llm = CannedLLMClient(
            [
                {
                    "hypotheses": [_hyp("deploy_regression", 0.9)],
                    "next_action": _lag_probe(),
                },
                {
                    "hypotheses": [_hyp("deploy_regression", 0.1)],
                    "next_action": {"kind": "stop", "reason": "rolled back already"},
                },
            ]
        )
        mcp = self._lag_sequence_mcp([0])
        slept: list[float] = []
        transition = make_llm_investigate(
            mcp, llm, model="m", reprobe_attempts=1, sleep=slept.append
        )
        transition(_investigating(run_state), now)
        assert slept == []

    def test_allowance_is_per_tool_and_capped(self, run_state: RunState, now: datetime) -> None:
        # attempts=1: the allowance for get_consumer_lag is spent, so the loop acts.
        llm = CannedLLMClient(
            [
                {
                    "hypotheses": [_hyp("consumer_saturation", 0.8)],
                    "next_action": _lag_probe(),
                },
                {
                    "hypotheses": [_hyp("consumer_saturation", 0.2)],
                    "next_action": {"kind": "stop", "reason": "zero lag"},
                },
                {
                    "hypotheses": [_hyp("consumer_saturation", 0.2)],
                    "next_action": {"kind": "stop", "reason": "still zero, accepting"},
                },
            ]
        )
        mcp = self._lag_sequence_mcp([0, 0])
        slept: list[float] = []
        transition = make_llm_investigate(
            mcp,
            llm,
            model="m",
            reprobe_attempts=1,
            reprobe_delay_seconds=5.0,
            sleep=slept.append,
        )
        result = transition(_investigating(run_state), now)

        assert len([c for c in mcp.calls if c[0] == "get_consumer_lag"]) == 2
        assert slept == [5.0]
        assert result.state is IncidentState.ESCALATED
        assert len([e for e in result.evidence if e.tool_name == "_freshness_reprobe"]) == 1


class TestRuntimeTierGuard:
    """B-06: only a runtime ``tier_of`` check catches a READ→TIER_1 reclassification
    made in ``policies.py`` after ``ReadToolName`` was hand-listed.
    """

    def test_reclassified_probe_tool_escalates_instead_of_executing(
        self, run_state: RunState, now: datetime, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Drift: get_trace is Tier-1 in the policy map but still schema-legal.
        monkeypatch.setattr(policies, "_TIER_1_TOOLS", policies._TIER_1_TOOLS | {"get_trace"})
        llm = CannedLLMClient(
            [
                {
                    "hypotheses": [_hyp("unknown", 0.5)],
                    "next_action": {
                        "kind": "probe",
                        "tool_name": "get_trace",
                        "arguments": {"trace_id": "trace-0001"},
                    },
                },
                {
                    "hypotheses": [_hyp("unknown", 0.5)],
                    "next_action": {"kind": "stop", "reason": "done"},
                },
            ]
        )
        mcp = _FakeMCPClient(
            lambda _n, _a: ToolResult(
                content=[
                    {
                        "type": "text",
                        "text": '{"trace_id":"trace-0001","jobs":[],"audit_events":[]}',
                    }
                ]
            )
        )
        transition = make_llm_investigate(mcp, llm, model="m")
        result = transition(_investigating(run_state), now)

        assert result.state is IncidentState.ESCALATED
        assert any("non-read tool as probe" in e.result_summary for e in result.evidence)
        # The reclassified tool was never executed.
        assert mcp.calls == []


class TestUnorderedRankingGate:
    """B-07 gate-level regression: the remediate gate reads hypotheses[0];
    the schema-boundary sort must make that read correct when the model
    emits an unordered ranking."""

    def test_unordered_ranking_escalates_on_true_top_without_fix(
        self, run_state: RunState, now: datetime
    ) -> None:
        # The top pick (deploy_regression 0.9) is escalate-only but listed second;
        # without normalization the gate hands off on the wrong one.
        llm = CannedLLMClient(
            [
                {
                    "hypotheses": [
                        _hyp("consumer_saturation", 0.75),
                        _hyp("deploy_regression", 0.9),
                    ],
                    "next_action": {
                        "kind": "remediate",
                        "reason": "lag pattern matches saturation",
                    },
                }
            ]
        )
        mcp = _FakeMCPClient(lambda _n, _a: _consumer_lag_response("billing", 42))
        transition = make_llm_investigate(mcp, llm, model="m")
        result = transition(_investigating(run_state), now)

        assert result.state is IncidentState.ESCALATED
        assert any("no Tier-1 fix" in e.result_summary for e in result.evidence)
        # The normalized ranking is what got persisted for the briefing.
        assert result.hypotheses[0].category is HypothesisCategory.DEPLOY_REGRESSION


# ---------------------------------------------------------------------------
# The alert-subject probe guard (live runs, 2026-08-30): the planner under-weighted the
# alert's own subject. A remediate handoff is refused while that resource sits unread.


def _dlq_response(total: int = 4) -> ToolResult:
    """The platform's four seeded dead-letter rows — the live run's distractor.

    Present in every run whatever the incident, so the DLQ always has entries.
    """
    return ToolResult(
        content=[
            {
                "type": "text",
                "text": json.dumps(
                    {
                        "total": total,
                        "items": [
                            {
                                "id": f"0000000{i}-0000-5000-8000-000000000000",
                                "type": "csv_upload",
                                "error_message": "seeded fixture row",
                                "retry_count": 1,
                                "created_at": "2026-08-30T12:00:00Z",
                            }
                            for i in range(total)
                        ],
                    }
                ),
            }
        ]
    )


def _multi_tool_mcp(group: str = "worker-dispatcher", lag: int = 15000) -> _FakeMCPClient:
    def handler(name: str, _args: Mapping[str, Any]) -> ToolResult:
        if name == "list_dlq_messages":
            return _dlq_response()
        return _consumer_lag_response(group, lag)

    return _FakeMCPClient(handler)


def _hypotheses(category: str, confidence: float) -> list[dict[str, Any]]:
    return [
        {
            "category": category,
            "name": f"{category}-candidate",
            "confidence": confidence,
            "reasoning": "canned",
        }
    ]


def _probe_step(
    tool_name: str, arguments: dict[str, Any], category: str = "poison_message"
) -> dict[str, Any]:
    return {
        "hypotheses": _hypotheses(category, 0.9),
        "next_action": {"kind": "probe", "tool_name": tool_name, "arguments": arguments},
    }


def _remediate_step(category: str = "poison_message") -> dict[str, Any]:
    return {
        "hypotheses": _hypotheses(category, 0.9),
        "next_action": {"kind": "remediate", "reason": f"{category} confirmed"},
    }


class TestAlertSubjectProbeGuard:
    """A remediate handoff requires a probe of the resource the alert names."""

    def test_handoff_refused_when_alert_subject_never_probed(
        self, run_state: RunState, now: datetime
    ) -> None:
        """The marquee case: DLQ distractor chased, alerted consumer never read.

        Before the guard the same canned sequence reached PLANNING.
        """
        llm = CannedLLMClient([_probe_step("list_dlq_messages", {}), _remediate_step()])
        mcp = _multi_tool_mcp()
        transition = make_llm_investigate(mcp, llm, model="m")
        result = transition(_investigating(run_state, group="unknown-consumer"), now)

        assert result.state is not IncidentState.PLANNING
        refusals = [e for e in result.evidence if e.tool_name == "_handoff_refused"]
        assert len(refusals) == 1
        assert "unknown-consumer" in refusals[0].result_summary
        assert "get_consumer_lag" in refusals[0].result_summary
        # Nothing ever read the alerted consumer — that is the whole finding.
        assert [name for name, _ in mcp.calls] == ["list_dlq_messages"]

    def test_the_refusal_steers_rather_than_ends_the_run(
        self, run_state: RunState, now: datetime
    ) -> None:
        """Refused, told what to probe, and the planner recovers to a handoff."""
        llm = CannedLLMClient(
            [
                _probe_step("list_dlq_messages", {}),
                _remediate_step(),
                _probe_step(
                    "get_consumer_lag",
                    {"consumer_group": "unknown-consumer"},
                    category="consumer_saturation",
                ),
                _remediate_step("consumer_saturation"),
            ]
        )
        mcp = _multi_tool_mcp(group="unknown-consumer")
        transition = make_llm_investigate(mcp, llm, model="m")
        result = transition(_investigating(run_state, group="unknown-consumer"), now)

        assert result.state is IncidentState.PLANNING
        assert len([e for e in result.evidence if e.tool_name == "_handoff_refused"]) == 1
        assert len([e for e in result.evidence if e.tool_name == "_planner_remediate"]) == 1
        assert ("get_consumer_lag", {"consumer_group": "unknown-consumer"}) in mcp.calls

    def test_probing_the_default_group_does_not_satisfy_the_guard(
        self, run_state: RunState, now: datetime
    ) -> None:
        """The live failure exactly: right tool, wrong resource.

        No arguments, so ``wire_arguments`` fills the schema default
        (`worker-dispatcher`) — a real probe of a consumer the alert never named.
        """
        llm = CannedLLMClient([_probe_step("get_consumer_lag", {}), _remediate_step()])
        mcp = _multi_tool_mcp()
        transition = make_llm_investigate(mcp, llm, model="m")
        result = transition(_investigating(run_state, group="unknown-consumer"), now)

        assert mcp.calls == [("get_consumer_lag", {"consumer_group": "worker-dispatcher"})]
        assert result.state is not IncidentState.PLANNING
        assert any(e.tool_name == "_handoff_refused" for e in result.evidence)

    def test_positive_control_subject_probed_admits_the_handoff(
        self, run_state: RunState, now: datetime
    ) -> None:
        llm = CannedLLMClient(
            [
                _probe_step(
                    "get_consumer_lag",
                    {"consumer_group": "billing"},
                    category="consumer_saturation",
                ),
                _remediate_step("consumer_saturation"),
            ]
        )
        transition = make_llm_investigate(_multi_tool_mcp(group="billing"), llm, model="m")
        result = transition(_investigating(run_state), now)

        assert result.state is IncidentState.PLANNING
        assert not [e for e in result.evidence if e.tool_name == "_handoff_refused"]

    def test_subjectless_alert_leaves_the_guard_inert(
        self, run_state: RunState, now: datetime
    ) -> None:
        """A whole-queue DLQ alert names a condition, not a resource.

        `dlq_backlog` and `dlq_mixed_partial` name no category. ``group: None`` is PRESENT on
        every alert (no ``exclude_none``), so a guard keyed on the key blocks the whole family.
        """
        alert = {
            "source": "platform.dlq",
            "severity": "critical",
            "fingerprint": "dlq_depth_warning",
            "group": None,
            "queue": "dlq",
        }
        llm = CannedLLMClient([_probe_step("list_dlq_messages", {}), _remediate_step()])
        transition = make_llm_investigate(_multi_tool_mcp(), llm, model="m")
        result = transition(
            run_state.model_copy(update={"state": IncidentState.INVESTIGATING, "alert": alert}),
            now,
        )

        assert result.state is IncidentState.PLANNING
        assert not [e for e in result.evidence if e.tool_name == "_handoff_refused"]

    def test_repeated_refusals_escalate_naming_the_unread_subject(
        self, run_state: RunState, now: datetime
    ) -> None:
        llm = CannedLLMClient([_remediate_step() for _ in range(5)])
        transition = make_llm_investigate(_multi_tool_mcp(), llm, model="m")
        result = transition(_investigating(run_state, group="unknown-consumer"), now)

        assert result.state is IncidentState.ESCALATED
        stops = [e for e in result.evidence if e.tool_name == "_planner_stop"]
        assert len(stops) == 1
        assert "without ever probing the alert's subject" in stops[0].result_summary
        assert "unknown-consumer" in stops[0].result_summary

    def test_a_refusal_spends_no_tool_call_budget(self, run_state: RunState, now: datetime) -> None:
        """The refusal is bookkeeping, not a probe."""
        llm = CannedLLMClient([_remediate_step(), _remediate_step(), _remediate_step()])
        transition = make_llm_investigate(_multi_tool_mcp(), llm, model="m")
        result = transition(_investigating(run_state, group="unknown-consumer"), now)

        assert result.budget.tool_calls_used == 0
        assert result.evidence[0].tool_name == "_handoff_refused"

    def test_refusal_marker_is_excluded_from_the_briefing_trail(
        self, run_state: RunState, now: datetime
    ) -> None:
        """Underscore prefix, per the repo-wide marker convention."""
        llm = CannedLLMClient([_remediate_step(), _remediate_step(), _remediate_step()])
        transition = make_llm_investigate(_multi_tool_mcp(), llm, model="m")
        result = transition(_investigating(run_state, group="unknown-consumer"), now)
        markers = [e for e in result.evidence if e.tool_name == "_handoff_refused"]
        assert markers and all(e.tool_name.startswith("_") for e in markers)


def _dlq_alert(hint: str | None) -> dict[str, Any]:
    """A DLQ alert as the eval runner dumps one, with or without a category.

    ``group`` and ``remediation_hint`` are present-and-None when unset: key on a value.
    """
    return {
        "source": "platform.dlq",
        "severity": "critical",
        "fingerprint": "dlq_depth_warning_wait_replay",
        "group": None,
        "remediation_hint": hint,
    }


class TestDlqCategoryIsTheAlertSubject:
    """A DLQ alert naming a category is investigated through that slice.

    Live run `06e14be3e7b1` (`dlq_wait_and_replay_success`, 2026-09-07) listed the DLQ
    unfiltered, reasoned correctly about all four rows and stopped: ADR 0008 gives one
    action, so a four-row scope cannot degrade into a partial fix. The subject is a SLICE.
    """

    def test_the_unfiltered_listing_does_not_satisfy_a_category_subject(
        self, run_state: RunState, now: datetime
    ) -> None:
        """Run B's trajectory, exactly: whole-queue read, then handoff.

        The red-before: the same two steps reached PLANNING while the guard was inert.
        """
        llm = CannedLLMClient([_probe_step("list_dlq_messages", {}), _remediate_step()])
        mcp = _multi_tool_mcp()
        transition = make_llm_investigate(mcp, llm, model="m")
        result = transition(
            run_state.model_copy(
                update={
                    "state": IncidentState.INVESTIGATING,
                    "alert": _dlq_alert("wait_and_replay"),
                }
            ),
            now,
        )

        assert result.state is not IncidentState.PLANNING
        refusals = [e for e in result.evidence if e.tool_name == "_handoff_refused"]
        assert len(refusals) == 1
        assert "wait_and_replay" in refusals[0].result_summary
        assert "list_dlq_messages" in refusals[0].result_summary
        # The whole queue is not the slice, and the refusal has to say so.
        assert "does not count" in refusals[0].result_summary
        assert mcp.calls == [
            (
                "list_dlq_messages",
                {"job_type": None, "remediation_hint": None, "limit": 50, "offset": 0},
            )
        ]

    def test_the_scoped_listing_admits_the_handoff(
        self, run_state: RunState, now: datetime
    ) -> None:
        """The green-after, and the shape the four scenarios now script."""
        llm = CannedLLMClient(
            [
                _probe_step("list_dlq_messages", {}),
                _probe_step("list_dlq_messages", {"remediation_hint": "wait_and_replay"}),
                _remediate_step(),
            ]
        )
        mcp = _multi_tool_mcp()
        transition = make_llm_investigate(mcp, llm, model="m")
        result = transition(
            run_state.model_copy(
                update={
                    "state": IncidentState.INVESTIGATING,
                    "alert": _dlq_alert("wait_and_replay"),
                }
            ),
            now,
        )

        assert result.state is IncidentState.PLANNING
        assert not [e for e in result.evidence if e.tool_name == "_handoff_refused"]

    def test_the_refusal_steers_the_planner_to_the_scoped_read(
        self, run_state: RunState, now: datetime
    ) -> None:
        """Refused once, told which call to make, recovers inside the run.

        The claim the guard rests on: it steers rather than ends.
        """
        llm = CannedLLMClient(
            [
                _probe_step("list_dlq_messages", {}),
                _remediate_step(),
                _probe_step("list_dlq_messages", {"remediation_hint": "wait_and_replay"}),
                _remediate_step(),
            ]
        )
        mcp = _multi_tool_mcp()
        transition = make_llm_investigate(mcp, llm, model="m")
        result = transition(
            run_state.model_copy(
                update={
                    "state": IncidentState.INVESTIGATING,
                    "alert": _dlq_alert("wait_and_replay"),
                }
            ),
            now,
        )

        assert result.state is IncidentState.PLANNING
        assert len([e for e in result.evidence if e.tool_name == "_handoff_refused"]) == 1
        assert (
            "list_dlq_messages",
            {
                "job_type": None,
                "remediation_hint": "wait_and_replay",
                "limit": 50,
                "offset": 0,
            },
        ) in mcp.calls

    def test_reading_a_different_category_does_not_satisfy_it(
        self, run_state: RunState, now: datetime
    ) -> None:
        """Value-matched, like every other entry in the map.

        ADR 0028's failure from the other side: read `replay_safe`, claim the wait backlog.
        """
        llm = CannedLLMClient(
            [
                _probe_step("list_dlq_messages", {"remediation_hint": "replay_safe"}),
                _remediate_step(),
            ]
        )
        transition = make_llm_investigate(_multi_tool_mcp(), llm, model="m")
        result = transition(
            run_state.model_copy(
                update={
                    "state": IncidentState.INVESTIGATING,
                    "alert": _dlq_alert("wait_and_replay"),
                }
            ),
            now,
        )

        assert result.state is not IncidentState.PLANNING
        assert any(e.tool_name == "_handoff_refused" for e in result.evidence)

    def test_a_hintless_dlq_alert_is_unaffected(self, run_state: RunState, now: datetime) -> None:
        """`dlq_mixed_partial` and `dlq_backlog`: same payload, hint None.

        The inert case stays inert for the right reason — a mixed queue names no slice. Its
        steering lives in the planner prompt, which is why it keeps no category.
        """
        llm = CannedLLMClient([_probe_step("list_dlq_messages", {}), _remediate_step()])
        transition = make_llm_investigate(_multi_tool_mcp(), llm, model="m")
        result = transition(
            run_state.model_copy(
                update={"state": IncidentState.INVESTIGATING, "alert": _dlq_alert(None)}
            ),
            now,
        )

        assert result.state is IncidentState.PLANNING
        assert not [e for e in result.evidence if e.tool_name == "_handoff_refused"]

    def test_a_named_resource_outranks_a_slice(self) -> None:
        """Declaration order is priority order, and the hint is last.

        An alert with both a `job_id` and its category is about the job, so this is pinned.
        """
        subject = alert_subject(
            {
                "source": "platform.dag",
                "job_id": "a2412a54-65f0-5258-95ab-5c168a15df64",
                "remediation_hint": "replay_safe",
            }
        )
        assert subject is not None
        assert (subject.alert_field, subject.tool_name) == ("job_id", "get_dag_state")

    def test_a_hint_inside_extra_data_is_found(self) -> None:
        """Where a real webhook would put it.

        ``alert_subject`` reads one level into ``extra_data``: the corpus is flat, the
        platform's real alert body is not.
        """
        subject = alert_subject(
            {"source": "platform.dlq", "extra_data": {"remediation_hint": "human_required"}}
        )
        assert subject is not None
        assert subject.tool_name == "list_dlq_messages"
        assert subject.argument_field == "remediation_hint"
        assert subject.value == "human_required"


# One case per ``ALERT_SUBJECT_PROBES`` entry. A module constant, not an inline
# parametrize list, so the totality test below can compare it against the map.
class TestWholeQueueBeforeDlqAction:
    """ADR 0041: a dead-letter handoff needs the whole queue in evidence.

    Live run ``fc896b25a09c`` (`remediate_dlq_backlog_success`, 2026-09-17) read only the
    alerted slice and resolved 1.00, never seeing the unclassified poison row beside the
    row it replayed — safe by luck. Mirror of ADR 0031's guard, which the slice satisfies.
    """

    def test_the_filtered_slice_alone_does_not_admit_the_handoff(
        self, run_state: RunState, now: datetime
    ) -> None:
        """Run ``fc896b25a09c``'s trajectory exactly. This is the red-before.

        The subject guard was satisfied by the alerted slice.
        """
        llm = CannedLLMClient(
            [
                _probe_step("list_dlq_messages", {"remediation_hint": "replay_safe"}),
                _remediate_step(),
            ]
        )
        mcp = _multi_tool_mcp()
        transition = make_llm_investigate(mcp, llm, model="m")
        result = transition(
            run_state.model_copy(
                update={
                    "state": IncidentState.INVESTIGATING,
                    "alert": _dlq_alert("replay_safe"),
                }
            ),
            now,
        )

        assert result.state is not IncidentState.PLANNING
        refusals = [e for e in result.evidence if e.tool_name == "_handoff_refused_unlisted_queue"]
        assert len(refusals) == 1
        assert "unfiltered" in refusals[0].result_summary
        assert "list_dlq_messages()" in refusals[0].result_summary
        # The subject guard is silent: the alerted slice WAS read.
        assert not [e for e in result.evidence if e.tool_name == "_handoff_refused"]

    def test_positive_control_the_whole_queue_read_admits_the_handoff(
        self, run_state: RunState, now: datetime
    ) -> None:
        """Run ``47abb70a2b9e``'s trajectory: whole queue, then the slice."""
        llm = CannedLLMClient(
            [
                _probe_step("list_dlq_messages", {}),
                _probe_step("list_dlq_messages", {"remediation_hint": "replay_safe"}),
                _remediate_step(),
            ]
        )
        transition = make_llm_investigate(_multi_tool_mcp(), llm, model="m")
        result = transition(
            run_state.model_copy(
                update={
                    "state": IncidentState.INVESTIGATING,
                    "alert": _dlq_alert("replay_safe"),
                }
            ),
            now,
        )

        assert result.state is IncidentState.PLANNING
        assert not [e for e in result.evidence if e.tool_name == "_handoff_refused_unlisted_queue"]

    def test_the_refusal_steers_once_and_the_planner_recovers(
        self, run_state: RunState, now: datetime
    ) -> None:
        """Refused, told which call, reads the queue, hands off. One refusal."""
        llm = CannedLLMClient(
            [
                _probe_step("list_dlq_messages", {"remediation_hint": "replay_safe"}),
                _remediate_step(),
                _probe_step("list_dlq_messages", {}),
                _remediate_step(),
            ]
        )
        mcp = _multi_tool_mcp()
        transition = make_llm_investigate(mcp, llm, model="m")
        result = transition(
            run_state.model_copy(
                update={
                    "state": IncidentState.INVESTIGATING,
                    "alert": _dlq_alert("replay_safe"),
                }
            ),
            now,
        )

        assert result.state is IncidentState.PLANNING
        assert (
            len([e for e in result.evidence if e.tool_name == "_handoff_refused_unlisted_queue"])
            == 1
        )
        assert len([e for e in result.evidence if e.tool_name == "_planner_remediate"]) == 1
        assert ("list_dlq_messages", "remediation_hint") in [
            (name, "remediation_hint") for name, args in mcp.calls if args.get("remediation_hint")
        ]

    def test_a_second_refusal_escalates_naming_the_missing_read(
        self, run_state: RunState, now: datetime
    ) -> None:
        """One steer, then the run ends rather than asking a third time.

        The budget is ONE here: ``list_dlq_messages()`` takes no argument to get wrong.
        """
        llm = CannedLLMClient(
            [
                _probe_step("list_dlq_messages", {"remediation_hint": "replay_safe"}),
                _remediate_step(),
                _remediate_step(),
                _remediate_step(),
            ]
        )
        transition = make_llm_investigate(_multi_tool_mcp(), llm, model="m")
        result = transition(
            run_state.model_copy(
                update={
                    "state": IncidentState.INVESTIGATING,
                    "alert": _dlq_alert("replay_safe"),
                }
            ),
            now,
        )

        assert result.state is IncidentState.ESCALATED
        stops = [e for e in result.evidence if e.tool_name == "_planner_stop"]
        assert len(stops) == 1
        assert "without ever reading the dead-letter queue unfiltered" in stops[0].result_summary
        assert "poison_message" in stops[0].result_summary

    def test_budget_exhaustion_during_the_re_steer_escalates_cleanly(
        self, run_state: RunState, now: datetime
    ) -> None:
        """The steer costs nothing, so the run that cannot afford the read says so.

        A refusal spends no tool call but the planner turn does, so a run refused on its last
        affordable turn escalates on the budget with the steer in evidence.
        """
        # Two planner calls at 1000/1000 spend 4000 tokens; the ceiling is
        # crossed by the second, which is the call the guard refuses.
        llm = CannedLLMClient(
            [
                _probe_step("list_dlq_messages", {"remediation_hint": "replay_safe"}),
                _remediate_step(),
                _remediate_step(),
            ],
            usage=CannedUsage(input_tokens=1000, output_tokens=1000),
        )
        transition = make_llm_investigate(_multi_tool_mcp(), llm, model="claude-sonnet-4-6")
        result = transition(
            run_state.model_copy(
                update={
                    "state": IncidentState.INVESTIGATING,
                    "alert": _dlq_alert("replay_safe"),
                    "budget": run_state.budget.model_copy(update={"max_tokens": 3000}),
                }
            ),
            now,
        )

        assert result.state is IncidentState.ESCALATED
        assert [e for e in result.evidence if e.tool_name == "_handoff_refused_unlisted_queue"], (
            "the steer is recorded even when the run cannot afford to act on it"
        )
        escalations = [e for e in result.evidence if e.tool_name == "_planner_escalate"]
        assert escalations and "budget exhausted" in escalations[-1].result_summary

    def test_a_job_type_filtered_listing_does_not_count(
        self, run_state: RunState, now: datetime
    ) -> None:
        """The other filter on the same call, and it narrows just as much."""
        llm = CannedLLMClient(
            [
                _probe_step("list_dlq_messages", {"job_type": "csv_upload"}),
                _remediate_step(),
            ]
        )
        transition = make_llm_investigate(_multi_tool_mcp(), llm, model="m")
        result = transition(
            run_state.model_copy(
                update={"state": IncidentState.INVESTIGATING, "alert": _dlq_alert(None)}
            ),
            now,
        )

        assert result.state is not IncidentState.PLANNING
        assert [e for e in result.evidence if e.tool_name == "_handoff_refused_unlisted_queue"]

    def test_a_paged_unfiltered_listing_counts(self, run_state: RunState, now: datetime) -> None:
        """Paging is how a long queue is read, not a way of reading less of it."""
        llm = CannedLLMClient(
            [
                _probe_step("list_dlq_messages", {"limit": 50, "offset": 50}),
                _remediate_step(),
            ]
        )
        transition = make_llm_investigate(_multi_tool_mcp(), llm, model="m")
        result = transition(
            run_state.model_copy(
                update={"state": IncidentState.INVESTIGATING, "alert": _dlq_alert(None)}
            ),
            now,
        )

        assert result.state is IncidentState.PLANNING
        assert not [e for e in result.evidence if e.tool_name == "_handoff_refused_unlisted_queue"]

    def test_the_guard_is_inert_for_a_category_that_reaches_no_dlq_action(
        self, run_state: RunState, now: datetime
    ) -> None:
        """A consumer-lag handoff has no queue to read whole.

        ``DLQ_ACTING_CATEGORIES`` is derived from the routing maps, so it excludes as
        well as includes.
        """
        llm = CannedLLMClient(
            [
                _probe_step(
                    "get_consumer_lag",
                    {"consumer_group": "billing"},
                    category="consumer_saturation",
                ),
                _remediate_step("consumer_saturation"),
            ]
        )
        transition = make_llm_investigate(_multi_tool_mcp(group="billing"), llm, model="m")
        result = transition(_investigating(run_state), now)

        assert result.state is IncidentState.PLANNING
        assert not [e for e in result.evidence if e.tool_name == "_handoff_refused_unlisted_queue"]

    def test_a_dead_lettered_chain_root_needs_the_queue_too(
        self, run_state: RunState, now: datetime
    ) -> None:
        """`runaway_saga` routes at `replay_dlq_by_ids`, so it is in scope.

        It reaches the queue through `FIX_MAP`, and the rule is the same: the row is a
        dead-letter row.
        """
        llm = CannedLLMClient(
            [
                _probe_step(
                    "list_dlq_messages",
                    {"remediation_hint": "human_required"},
                    category="runaway_saga",
                ),
                _remediate_step("runaway_saga"),
            ]
        )
        transition = make_llm_investigate(_multi_tool_mcp(), llm, model="m")
        result = transition(
            run_state.model_copy(
                update={"state": IncidentState.INVESTIGATING, "alert": _dlq_alert(None)}
            ),
            now,
        )

        assert result.state is not IncidentState.PLANNING
        assert [e for e in result.evidence if e.tool_name == "_handoff_refused_unlisted_queue"]

    def test_the_refusal_spends_no_tool_call_budget_and_is_a_marker(
        self, run_state: RunState, now: datetime
    ) -> None:
        """Bookkeeping, not a probe — the repo-wide underscore convention."""
        llm = CannedLLMClient(
            [
                _probe_step("list_dlq_messages", {"remediation_hint": "replay_safe"}),
                _remediate_step(),
                _remediate_step(),
            ]
        )
        transition = make_llm_investigate(_multi_tool_mcp(), llm, model="m")
        result = transition(
            run_state.model_copy(
                update={
                    "state": IncidentState.INVESTIGATING,
                    "alert": _dlq_alert("replay_safe"),
                }
            ),
            now,
        )

        markers = [e for e in result.evidence if e.tool_name == "_handoff_refused_unlisted_queue"]
        assert markers and all(e.tool_name.startswith("_") for e in markers)
        # One probe, and the refusal added nothing to the meter.
        assert result.budget.tool_calls_used == 1


_MAPPED_FIELD_CASES: list[tuple[dict[str, Any], tuple[str, str, str]]] = [
    (
        {"group": "orders-consumer"},
        ("get_consumer_lag", "consumer_group", "orders-consumer"),
    ),
    (
        {"consumer_group": "worker-dispatcher"},
        ("get_consumer_lag", "consumer_group", "worker-dispatcher"),
    ),
    (
        {"cache_key": "cache:jobs:worker-dispatcher:hot_set"},
        ("get_cache_key_info", "key", "cache:jobs:worker-dispatcher:hot_set"),
    ),
    (
        {"job_id": "a2412a54-65f0-5258-95ab-5c168a15df64"},
        ("get_dag_state", "job_id", "a2412a54-65f0-5258-95ab-5c168a15df64"),
    ),
    (
        {"trace_id": "0e24ca29-1d47-57e9-b898-4d79bb6da981"},
        ("get_trace", "trace_id", "0e24ca29-1d47-57e9-b898-4d79bb6da981"),
    ),
    (
        {"remediation_hint": "wait_and_replay"},
        ("list_dlq_messages", "remediation_hint", "wait_and_replay"),
    ),
    # The unfiltered arm (ADR 0032): same tool and argument as the entry above, but the
    # probe that satisfies this subject did NOT narrow on `remediation_hint`.
    (
        {"dlq_scope": "unclassified"},
        ("list_dlq_messages", "remediation_hint", "unclassified"),
    ),
]


class TestAlertSubjectDerivation:
    """``alert_subject`` is mechanical: a closed field map, never a heuristic."""

    @pytest.mark.parametrize(("alert", "expected"), _MAPPED_FIELD_CASES)
    def test_every_mapped_field_resolves_to_its_probe(
        self, alert: dict[str, Any], expected: tuple[str, str, str]
    ) -> None:
        subject = alert_subject(alert)
        assert subject is not None
        assert (subject.tool_name, subject.argument_field, subject.value) == expected

    def test_the_case_list_covers_every_mapped_field(self) -> None:
        """Anti-vacuity for the name of the test above.

        "Every mapped field" is a claim about the map, so the case list has to
        be held against the map rather than trusted to have kept up with it.
        """
        from incident_commander.agent.investigation import ALERT_SUBJECT_PROBES

        covered = {field for alert, _ in _MAPPED_FIELD_CASES for field in alert}
        assert covered == set(ALERT_SUBJECT_PROBES), (
            "_MAPPED_FIELD_CASES does not cover every ALERT_SUBJECT_PROBES field: "
            f"missing {sorted(set(ALERT_SUBJECT_PROBES) - covered)}, "
            f"extra {sorted(covered - set(ALERT_SUBJECT_PROBES))}."
        )

    @pytest.mark.parametrize(
        "alert",
        [
            {},
            {"group": None},
            {"group": "   "},
            {"source": "platform.dlq", "queue": "dlq", "job_type": "csv_upload"},
            {"service": "billing-api"},
            {"fingerprint": "consumer_lag_high"},
            {"group": {"nested": "payload"}},
            {"group": 42},
        ],
        ids=[
            "empty",
            "group-is-none",
            "group-is-whitespace",
            "dlq-condition-alert",
            "service-not-probeable",
            "fingerprint-alone",
            "group-is-a-mapping",
            "group-is-a-number",
        ],
    )
    def test_unmappable_alerts_are_inert(self, alert: dict[str, Any]) -> None:
        """No subject means no opinion. Fabricating one would block the innocent.

        ``{"group": None}`` is the common case: the key is present on every alert.
        """
        assert alert_subject(alert) is None

    def test_top_level_beats_nested_and_declaration_order_breaks_ties(self) -> None:
        alert = {
            "consumer_group": "worker-dispatcher",
            "group": "other-consumer",
            "cache_key": "cache:jobs:x",
        }
        subject = alert_subject(alert)
        assert subject is not None
        assert (subject.alert_field, subject.value) == ("consumer_group", "worker-dispatcher")

    def test_reads_the_wire_shaped_alert_through_extra_data(self) -> None:
        """The platform's webhook nests everything under ``extra_data``.

        The corpus is flat and live traffic nested, so reading one would be green
        offline and asleep in production.
        """
        alert = {
            "alert_id": "a-1",
            "severity": "critical",
            "source": "platform.kafka",
            "extra_data": {"consumer_group": "worker-dispatcher"},
        }
        subject = alert_subject(alert)
        assert subject is not None
        assert (subject.tool_name, subject.value) == ("get_consumer_lag", "worker-dispatcher")

    def test_uuid_values_are_accepted(self) -> None:
        job_id = UUID("a2412a54-65f0-5258-95ab-5c168a15df64")
        subject = alert_subject({"job_id": job_id})
        assert subject is not None
        assert subject.value == str(job_id)
