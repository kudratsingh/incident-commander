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
        # The probe step is load-bearing, not scene-setting: `_investigating`
        # gives this run an alert naming group "billing", and the
        # alert-subject guard (TestAlertSubjectProbeGuard) refuses a handoff
        # while that group sits unread. This test used to hand off from a
        # single remediate response with an empty evidence trail, which is
        # the behaviour the guard exists to stop — it was asserting the
        # defect. Probing the alerted group first is what a correct planner
        # does, and it keeps this test about the handoff transition itself.
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
        # Post-hardening: ProbeAction.tool_name is a Literal, so Pydantic
        # rejects "made_up_tool" at schema-validation time. The transition's
        # ValidationError catch escalates with a "planner output invalid"
        # reason that mentions the rejected value.
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

        Regression: an earlier `.model_dump()` (missing `mode="json"`) left
        UUID objects in the arguments dict — the live saga_stuck scenario
        crashed the entire eval batch on `get_dag_state({"job_id": UUID(...)})`.
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
        # (i) actionable hypothesis at 0.75, probes the cached lag tool ->
        # stale 0 kills it -> (ii) planner says stop. The interceptor must
        # ignore the stop, re-probe fresh, and let (iii) decide on both
        # readings. This is the exact 2026-08-03 campaign trace shape.
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
        # attempts=1: after the fresh read the planner STILL kills the
        # hypothesis -> allowance for get_consumer_lag is spent -> the
        # loop accepts the contradiction and acts on the step.
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
    """B-06: the ReadToolName Literal keeps the *schema* read-only; only a
    runtime ``tier_of`` check can catch a READ→TIER_1 reclassification made
    in ``policies.py`` after the Literal was hand-listed. The live agent
    token carries ``actions:execute``, so without the guard the platform
    would permit the call."""

    def test_reclassified_probe_tool_escalates_instead_of_executing(
        self, run_state: RunState, now: datetime, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Simulate the drift: get_trace is reclassified as Tier-1 in the
        # policy map, but it is still schema-legal in the ReadToolName
        # Literal. Patch the module state tier_of reads at call time.
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
        # The model's true top pick (deploy_regression 0.9) is an
        # escalate-only category, but it is listed second; the FIX_MAP
        # hypothesis (consumer_saturation 0.75, above threshold) is listed
        # first. Without normalization the gate hands off to PLANNING on
        # the wrong diagnosis.
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
# The alert-subject probe guard.
#
# Both halves of this class come from live runs on 2026-08-30, and both are
# the same defect: the planner under-weighting the alert's own subject.
#
# 1. `consumer_lag_missing_group` — the alert named group "unknown-consumer".
#    The agent called `get_consumer_lag` with no group, which wire_arguments
#    default-fills to the platform's `worker-dispatcher`, read a healthy
#    number off a consumer nobody had complained about, noticed a DIFFERENT
#    critical alert while it was in there, and chased that one instead. Right
#    terminal state, wrong investigation: the alert's premise was never tested.
#
# 2. `remediate_consumer_lag_success` — a real killed consumer with lag
#    climbing. The agent probed the lag, then opened the DLQ, anchored on the
#    four SEEDED fixture rows that are in that DLQ on every run, replayed one
#    of them, verified THAT, and resolved. It never restarted the consumer.
#
# The guard below is aimed at the shared root: a remediate handoff may not be
# accepted while the resource the alert names sits unread.


def _dlq_response(total: int = 4) -> ToolResult:
    """The platform's four seeded dead-letter rows — the live run's distractor.

    These are present in every run of the demo stack regardless of the
    incident, which is exactly what makes them a distractor: an agent that
    treats "the DLQ has entries" as a finding will find one every time.
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

        Before this guard the same canned sequence reached PLANNING and the
        agent went on to replay a seeded DLQ row for an incident that was
        reported as consumer lag.
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

        The planner emits `get_consumer_lag` with NO arguments.
        ``wire_arguments`` fills the platform's schema default
        (`worker-dispatcher`), so the evidence trail holds a real, successful
        probe of a consumer the alert never mentioned. Matching on tool name
        alone would call this satisfied.
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
        """A DLQ-depth alert names a condition, not a resource — never blocked.

        This is the shape of all five canned DLQ remediation flows. Note the
        explicit ``group: None``: ``AlertPayload.group`` defaults to None and
        the eval runner dumps without ``exclude_none``, so the key is PRESENT
        on every alert the agent ever sees. A guard that tested for the key
        rather than for a usable value would block the entire DLQ family.
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


class TestAlertSubjectDerivation:
    """``alert_subject`` is mechanical: a closed field map, never a heuristic."""

    @pytest.mark.parametrize(
        ("alert", "expected"),
        [
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
        ],
    )
    def test_every_mapped_field_resolves_to_its_probe(
        self, alert: dict[str, Any], expected: tuple[str, str, str]
    ) -> None:
        subject = alert_subject(alert)
        assert subject is not None
        assert (subject.tool_name, subject.argument_field, subject.value) == expected

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

        ``{"group": None}`` is the case that matters most in practice:
        ``AlertPayload.group`` defaults to None and the eval runner dumps
        without ``exclude_none``, so every alert in the corpus carries the
        key whether or not it names a group.
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

        The scenario corpus is flat and live traffic is nested (see
        ``tests/unit/test_scenario_alert_premise.py``), so a derivation that
        read only one of the two would be inert on the other — green offline
        and asleep in production, or vice versa.
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
