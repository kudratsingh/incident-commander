"""The run reporter: the whole run in reports, fail-open, and off the planner's page.

ADR 0068 and its WO-R3-329 amendment. Four properties were load-bearing from the start and
each has a class here: reporting happens once per checkpoint *at least* and the briefing
exactly once; every failure is swallowed with a log line; a report is NOT a tool call and
cannot move the budget; and neither reporting tool is anything a model could choose.

The widening adds five more, each its own class below: one report per tool call carrying one
step; the ranked hypotheses on every report; the plan once per distinct plan; one
verification per verify poll; and the budget meter on every report. Plus the two properties
the widening itself created — an older platform narrows the payload once rather than
refusing every report, and no credential reaches the wire in a report body.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from incident_commander.agent.attribution import AttributionRead, AttributionVerdict
from incident_commander.agent.briefing import render_briefing
from incident_commander.agent.hypothesis import Hypothesis, HypothesisCategory
from incident_commander.agent.loop import run_to_completion
from incident_commander.agent.planner_context import (
    ATTEMPT_FAILED_MARKER,
    VERIFY_JUDGE_MARKER,
    format_tool_block,
)
from incident_commander.agent.run_reporting import (
    NARROW_FIELDS,
    REPORT_BRIEFING_TOOL,
    REPORT_RUN_TOOL,
    ReportingCheckpointer,
    RunReporter,
    ToolCallLog,
    budget_payload,
    last_step,
    ranked_hypotheses,
    summarize,
    top_hypothesis,
)
from incident_commander.agent.state import (
    BudgetLedger,
    EvidenceEntry,
    IncidentState,
    RunState,
)
from incident_commander.persistence.memory import InMemoryCheckpointer
from incident_commander.tools.mcp_client import MCPClient, MCPError, ToolResult
from incident_commander.tools.registry import TOOL_REGISTRY

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SNAPSHOT_PATH = _REPO_ROOT / "contracts" / "platform-tools.snapshot.json"


class _RecordingClient:
    """Stands in for the MCP client: records every call, answers success."""

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
        return ToolResult(content=[{"type": "text", "text": "{}"}], is_error=False)

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def arguments_for(self, tool: str) -> list[dict[str, Any]]:
        return [args for name, args in self.calls if name == tool]


class _RaisingClient:
    """Every call raises — the platform down, or the token missing the scope."""

    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error or MCPError(-32000, "HTTP 403 from MCP endpoint: forbidden")
        self.attempts = 0

    def call_tool(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        timeout_seconds: float | None = None,
    ) -> ToolResult:
        self.attempts += 1
        raise self.error


class _RefusingClient:
    """Every call comes back a 200 carrying ``isError`` — a tool-level refusal."""

    def __init__(self, text: str = "agent_run_already_finished") -> None:
        self.text = text
        self.attempts = 0

    def call_tool(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        timeout_seconds: float | None = None,
    ) -> ToolResult:
        self.attempts += 1
        return ToolResult(content=[{"type": "text", "text": self.text}], is_error=True)


def _reporter(client: Any, **kwargs: Any) -> RunReporter:
    return RunReporter(client, run_id=kwargs.pop("run_id", uuid4()), **kwargs)


def _make_clock(start: datetime, step_seconds: float = 1.0) -> Callable[[], datetime]:
    ticks = {"i": 0}

    def clock() -> datetime:
        i = ticks["i"]
        ticks["i"] = i + 1
        return start + timedelta(seconds=i * step_seconds)

    return clock


class _OldPlatformClient:
    """Platform v0.6.15: its input model forbids unknown fields, so a widened report is
    refused whole — state included, which is the failure the fallback exists for.

    Refuses the way that platform ACTUALLY refuses, which the 2026-09-20 rehearsal
    measured: a JSON-RPC error carrying ``-32602: invalid tool arguments``, not a 200
    carrying ``isError``. ``as_tool_error=True`` is the other shape, kept because the
    platform uses it for things it understood and declined and a future version could
    validate either way. Anything built from ``NARROW_FIELDS`` alone is accepted.
    """

    def __init__(self, *, as_tool_error: bool = False) -> None:
        self.as_tool_error = as_tool_error
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.accepted: list[dict[str, Any]] = []

    def call_tool(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        timeout_seconds: float | None = None,
    ) -> ToolResult:
        self.calls.append((name, dict(arguments)))
        unknown = sorted(set(arguments) - set(NARROW_FIELDS))
        if name == REPORT_RUN_TOOL and unknown:
            detail = (
                f"Input validation failed for ReportAgentRunInput: "
                f"extra_forbidden ({', '.join(unknown)})"
            )
            if self.as_tool_error:
                return ToolResult(content=[{"type": "text", "text": detail}], is_error=True)
            raise MCPError(-32602, "invalid tool arguments", {"detail": detail})
        self.accepted.append(dict(arguments))
        return ToolResult(content=[{"type": "text", "text": "{}"}], is_error=False)


def _traced(
    tool: str,
    arguments: Mapping[str, Any] | None = None,
    *,
    text: str = "{}",
    is_error: bool = False,
    error: str | None = None,
    seconds: float = 0.25,
) -> dict[str, Any]:
    """One record in the exact shape ``MCPClient`` hands its tracer hook."""
    record: dict[str, Any] = {
        "tool_name": tool,
        "arguments": dict(arguments or {}),
        "duration_seconds": seconds,
    }
    if error is not None:
        record["error"] = error
        return record
    record["result"] = {"content": [{"type": "text", "text": text}], "is_error": is_error}
    return record


def _entry(tool: str, at: datetime, summary: str = "{}", **arguments: Any) -> EvidenceEntry:
    return EvidenceEntry(
        tool_name=tool, arguments=dict(arguments), result_summary=summary, timestamp=at
    )


def _with(run_state: RunState, **update: Any) -> RunState:
    return run_state.model_copy(update=update)


def _hypothesis(name: str, confidence: float, reasoning: str = "the lag is climbing") -> Hypothesis:
    return Hypothesis(
        category=HypothesisCategory.CONSUMER_SATURATION,
        name=name,
        confidence=confidence,
        reasoning=reasoning,
    )


#: A plan in the shape ``RunState.remediation_plan`` holds one (dumped, not typed).
_PLAN: dict[str, Any] = {
    "target_hypothesis": "consumer_saturation",
    "action_tool": "restart_consumer_group",
    "action_arguments": {"consumer_group": "worker-dispatcher"},
    "verify_tool": "get_consumer_lag",
    "verify_arguments": {"consumer_group": "worker-dispatcher"},
    "verify_expectation": "lag back under 5",
    "action_rationale": "the group is dead and the backlog is the symptom",
}


class TestOneReportPerTransition:
    """The seam reports exactly as often as the loop checkpoints, and no more."""

    def test_a_report_lands_for_every_checkpoint_the_loop_writes(
        self, run_state: RunState, now: datetime
    ) -> None:
        # A noise alert runs TRIAGE -> ESCALATED: the loop writes on entry and once after
        # the transition, so two checkpoints and two reports, with the states in order.
        run = run_state.model_copy(update={"alert": {"source": "billing", "severity": "info"}})
        client = _RecordingClient()
        reporter = _reporter(client)
        store = InMemoryCheckpointer()

        final = run_to_completion(
            run,
            clock=_make_clock(now),
            checkpointer=ReportingCheckpointer(store, reporter),
        )

        assert final.state is IncidentState.ESCALATED
        checkpoints = store.history(final.incident_id)
        assert client.names() == [REPORT_RUN_TOOL] * len(checkpoints)
        reported = [args["state"] for args in client.arguments_for(REPORT_RUN_TOOL)]
        assert reported == [state.state.value for state in checkpoints]
        assert reported[0] == "triage" and reported[-1] == "escalated"
        assert reporter.reports_sent == len(checkpoints)

    def test_the_checkpoint_is_written_before_the_report(self, run_state: RunState) -> None:
        """The order is the fail-open guarantee, so it is asserted and not assumed."""
        order: list[str] = []

        class _Store:
            def load(self, incident_id: UUID) -> RunState | None:
                return None

            def write(self, state: RunState) -> None:
                order.append("checkpoint")

        class _Client(_RecordingClient):
            def call_tool(self, name: str, arguments: Mapping[str, Any], **kw: Any) -> ToolResult:
                order.append("report")
                return super().call_tool(name, arguments, **kw)

        ReportingCheckpointer(_Store(), _reporter(_Client())).write(run_state)

        assert order == ["checkpoint", "report"]

    def test_every_report_carries_the_same_run_id(self, run_state: RunState) -> None:
        client = _RecordingClient()
        run_id = uuid4()
        seam = ReportingCheckpointer(InMemoryCheckpointer(), _reporter(client, run_id=run_id))

        seam.write(run_state)
        seam.write(run_state.with_state(IncidentState.INVESTIGATING, run_state.updated_at))

        assert {args["run_id"] for args in client.arguments_for(REPORT_RUN_TOOL)} == {str(run_id)}

    def test_the_run_label_is_run_label_and_never_scenario(self, run_state: RunState) -> None:
        """`extra="forbid"` on the platform side: the wrong spelling is a refusal."""
        client = _RecordingClient()
        _reporter(client, run_label="remediate_dlq_backlog_success").report(run_state)

        sent = client.arguments_for(REPORT_RUN_TOOL)[0]
        assert sent["run_label"] == "remediate_dlq_backlog_success"
        assert "scenario" not in sent

    def test_the_agents_own_clock_is_what_it_reports(self, run_state: RunState) -> None:
        # `at` is when the run MOVED, not when the report landed — the platform stamps its
        # own clock when `at` is absent, which is a different fact.
        client = _RecordingClient()
        _reporter(client).report(run_state)

        assert client.arguments_for(REPORT_RUN_TOOL)[0]["at"] == run_state.updated_at.isoformat()

    def test_load_delegates_untouched(self, run_state: RunState) -> None:
        store = InMemoryCheckpointer()
        store.write(run_state)
        seam = ReportingCheckpointer(store, _reporter(_RecordingClient()))

        assert seam.load(run_state.incident_id) == run_state


class TestTheBriefingGoesOnce:
    def test_a_second_briefing_is_not_sent(self, run_state: RunState) -> None:
        client = _RecordingClient()
        reporter = _reporter(client)
        escalated = run_state.with_state(IncidentState.ESCALATED, run_state.updated_at)
        briefing = render_briefing(escalated)

        reporter.report_briefing(briefing)
        reporter.report_briefing(briefing)

        assert client.names().count(REPORT_BRIEFING_TOOL) == 1
        assert reporter.briefing_sent is True

    def test_the_briefing_travels_as_the_object_plus_its_prose(self, run_state: RunState) -> None:
        client = _RecordingClient()
        escalated = run_state.with_state(IncidentState.ESCALATED, run_state.updated_at)
        briefing = render_briefing(escalated).model_copy(
            update={"findings": "lag climbed", "recommendation": "watch it"}
        )

        _reporter(client).report_briefing(briefing, prose="Findings: lag climbed")

        sent = client.arguments_for(REPORT_BRIEFING_TOOL)[0]
        # The whole briefing as an object — the platform stores it verbatim and interprets
        # nothing, so every field the ADR 0065 slots carry survives the trip.
        assert sent["briefing"]["final_state"] == "escalated"
        assert "incidents" in sent["briefing"]
        assert sent["prose"] == "Findings: lag climbed"
        # It must be JSON the wire can carry: a datetime left in place would raise inside
        # httpx rather than at the seam, which is a crash the fail-open guard never sees.
        json.dumps(sent)

    def test_the_attribution_verdict_and_the_incident_slots_both_travel(
        self, run_state: RunState
    ) -> None:
        """The briefing card renders both from the record, so a trimmed dump blanks it.

        The console reads ADR 0071's verdict out of `briefing.attribution` by these exact
        five field names and ADR 0065's remainder out of `briefing.incidents`. Pinned here
        because the whole-object dump is what carries them: nothing in this module names
        either field, so nothing but a test would notice them going missing.
        """
        client = _RecordingClient()
        escalated = run_state.with_state(IncidentState.ESCALATED, run_state.updated_at)
        briefing = render_briefing(escalated).model_copy(
            update={
                "attribution": AttributionRead(
                    verdict=AttributionVerdict.ATTRIBUTED,
                    resource="worker-dispatcher",
                    probe_tool="get_consumer_lag",
                    acted=True,
                    detail="lag 42 before the restart, 0 after it",
                )
            }
        )

        _reporter(client).report_briefing(briefing)

        sent = client.arguments_for(REPORT_BRIEFING_TOOL)[0]["briefing"]
        assert set(sent["attribution"]) == {
            "verdict",
            "resource",
            "probe_tool",
            "acted",
            "detail",
        }
        assert sent["attribution"]["verdict"] == AttributionVerdict.ATTRIBUTED.value
        assert "incidents" in sent

    def test_prose_is_absent_rather_than_empty_on_an_unenriched_run(
        self, run_state: RunState
    ) -> None:
        client = _RecordingClient()
        escalated = run_state.with_state(IncidentState.ESCALATED, run_state.updated_at)
        briefing = render_briefing(escalated)

        _reporter(client).report_briefing(briefing, prose=None)

        assert "prose" not in client.arguments_for(REPORT_BRIEFING_TOOL)[0]

    def test_over_long_prose_is_truncated_rather_than_refused(self, run_state: RunState) -> None:
        """The tool caps `prose` at 20,000; losing the whole briefing to that is worse."""
        client = _RecordingClient()
        escalated = run_state.with_state(IncidentState.ESCALATED, run_state.updated_at)
        briefing = render_briefing(escalated)

        _reporter(client).report_briefing(briefing, prose="x" * 25_000)

        assert len(client.arguments_for(REPORT_BRIEFING_TOOL)[0]["prose"]) == 20_000


class TestFailuresAreSwallowedAndLogged:
    """Invariant 5's shape: telemetry never gates the run it describes."""

    def test_a_raising_platform_does_not_raise_at_the_seam(
        self, run_state: RunState, caplog: pytest.LogCaptureFixture
    ) -> None:
        client = _RaisingClient()
        reporter = _reporter(client)

        with caplog.at_level(logging.WARNING):
            reporter.report(run_state)

        assert client.attempts == 1
        assert reporter.reports_sent == 0
        assert len(reporter.failures) == 1
        assert "the run is unaffected" in caplog.text
        assert "403" in caplog.text, "the log must say what went wrong, not just that it did"

    def test_a_refusal_carrying_is_error_counts_as_a_failure(
        self, run_state: RunState, caplog: pytest.LogCaptureFixture
    ) -> None:
        # A tool-level refusal is a 200 with `isError`, so reading only the transport would
        # count a refused report as delivered — the C-02 lesson, here as a test.
        client = _RefusingClient()
        reporter = _reporter(client)

        with caplog.at_level(logging.WARNING):
            reporter.report(run_state)

        assert reporter.reports_sent == 0
        assert "agent_run_already_finished" in caplog.text

    def test_a_failed_briefing_leaves_it_unsent_so_a_later_call_may_retry(
        self, run_state: RunState
    ) -> None:
        client = _RaisingClient()
        reporter = _reporter(client)
        escalated = run_state.with_state(IncidentState.ESCALATED, run_state.updated_at)
        briefing = render_briefing(escalated)

        reporter.report_briefing(briefing)

        assert reporter.briefing_sent is False, (
            "a report that never landed must not latch the once-per-run guard, or a "
            "transport blip would silently cost the console the whole handoff"
        )

    def test_a_run_completes_through_a_dead_platform(
        self, run_state: RunState, now: datetime
    ) -> None:
        """The property that matters: the run's outcome is identical either way."""
        run = run_state.model_copy(update={"alert": {"source": "billing", "severity": "info"}})
        store = InMemoryCheckpointer()

        reported = run_to_completion(
            run,
            clock=_make_clock(now),
            checkpointer=ReportingCheckpointer(store, _reporter(_RaisingClient())),
        )
        unreported = run_to_completion(run, clock=_make_clock(now), checkpointer=store)

        assert reported.state is unreported.state is IncidentState.ESCALATED
        assert reported.budget == unreported.budget
        # By tool name and summary, not by entry: `evidence_id` is a fresh uuid4 per
        # entry, so comparing whole entries would fail on two identical runs.
        assert [(e.tool_name, e.result_summary) for e in reported.evidence] == [
            (e.tool_name, e.result_summary) for e in unreported.evidence
        ]

    def test_a_keyboard_interrupt_is_not_swallowed(self, run_state: RunState) -> None:
        """`Exception`, not `BaseException`: an operator stopping the run must win."""
        client = _RaisingClient(KeyboardInterrupt())

        with pytest.raises(KeyboardInterrupt):
            _reporter(client).report(run_state)


class TestAReportIsNotAToolCall:
    def test_reporting_never_moves_the_budget(self, run_state: RunState, now: datetime) -> None:
        # Reports are not investigation steps: they cannot count against
        # `max_tool_calls`, and so cannot push a run into escalation (invariant 7 works
        # the other way — the ceiling is for the agent's own choices).
        run = run_state.model_copy(update={"alert": {"source": "billing", "severity": "info"}})
        client = _RecordingClient()
        reporter = _reporter(client)

        final = run_to_completion(
            run,
            clock=_make_clock(now),
            checkpointer=ReportingCheckpointer(InMemoryCheckpointer(), reporter),
        )

        assert reporter.reports_sent >= 2, "the reports really were sent"
        assert final.budget.tool_calls_used == run.budget.tool_calls_used
        assert final.budget.tokens_used == run.budget.tokens_used
        assert final.budget.usd_used == run.budget.usd_used

    def test_a_tight_budget_is_not_exhausted_by_reporting(
        self, run_state: RunState, now: datetime
    ) -> None:
        """The failure this would be: a demo run escalating because it was watched."""
        one_call = BudgetLedger(
            max_tool_calls=1,
            max_tokens=200_000,
            max_wall_seconds=1_800,
            max_usd=run_state.budget.max_usd,
        )
        run = run_state.model_copy(
            update={
                "alert": {"source": "billing", "severity": "info"},
                "budget": one_call,
            }
        )
        reporter = _reporter(_RecordingClient())

        final = run_to_completion(
            run,
            clock=_make_clock(now),
            checkpointer=ReportingCheckpointer(InMemoryCheckpointer(), reporter),
        )

        assert final.budget.tool_calls_used == 0
        assert not final.budget.is_exhausted

    def test_reporting_leaves_no_entry_on_the_evidence_ledger(self, run_state: RunState) -> None:
        # The ledger is what the briefing, the graders and the reward all read. A report on
        # it would become a probe in the trail a human is handed.
        seam = ReportingCheckpointer(InMemoryCheckpointer(), _reporter(_RecordingClient()))
        seam.write(run_state)

        assert run_state.evidence == ()


class TestTheReporterIsNotOnThePlannersPage:
    def test_neither_tool_is_in_the_typed_registry(self) -> None:
        assert REPORT_RUN_TOOL not in TOOL_REGISTRY
        assert REPORT_BRIEFING_TOOL not in TOOL_REGISTRY

    def test_neither_tool_appears_in_the_block_the_planner_reads(self) -> None:
        # The end-to-end statement, and worth being precise about what holds it up. The
        # block is rendered from TOOL_REGISTRY, which is hand-written, so the two tools are
        # absent because nobody registered them — deleting `[commander:` from the exclusion
        # prefixes does NOT make this test fail (measured). What that deletion does is make
        # `test_registry_covers_every_snapshot_tool` DEMAND they be registered, and this is
        # the test that fails the moment somebody complies. Both are needed: the filter
        # removes the pressure, and this removes the escape.
        block = format_tool_block()
        assert REPORT_RUN_TOOL not in block
        assert REPORT_BRIEFING_TOOL not in block
        assert "agent_runs" not in block

    def test_the_tools_it_calls_are_the_ones_the_snapshot_declares(self) -> None:
        """A rename on the platform side must fail here, not at the first live report."""
        declared = {t["name"] for t in json.loads(_SNAPSHOT_PATH.read_text())["tools"]}
        assert {REPORT_RUN_TOOL, REPORT_BRIEFING_TOOL} <= declared


class TestWhatTheConsoleIsShown:
    def test_the_top_hypothesis_is_the_rankings_first_entry(self, run_state: RunState) -> None:
        # `hypotheses[0]`, the same projection `remediation.py`, `incidents.py` and
        # `evals/reward.py` read, so the console shows the run's actual diagnosis.
        ranked = run_state.model_copy(
            update={
                "hypotheses": (
                    Hypothesis(
                        category=HypothesisCategory.CONSUMER_SATURATION,
                        name="consumer_saturation",
                        confidence=0.85,
                        reasoning="lag climbing",
                    ),
                    Hypothesis(
                        category=HypothesisCategory.POISON_MESSAGE,
                        name="poison_message",
                        confidence=0.20,
                        reasoning="a DLQ row",
                    ),
                )
            }
        )

        assert top_hypothesis(ranked) == {
            "name": "consumer_saturation",
            "category": "consumer_saturation",
            "confidence": 0.85,
        }

    def test_no_hypothesis_yet_reports_none_rather_than_a_guess(self, run_state: RunState) -> None:
        assert top_hypothesis(run_state) is None

    def test_the_last_step_skips_bookkeeping_markers(
        self, run_state: RunState, now: datetime
    ) -> None:
        # Underscore entries are not calls — the same structural filter `trail_of` and the
        # deterministic grader apply, so the step shown is one a human can look up in the
        # audit log.
        with_marker = run_state.model_copy(
            update={
                "evidence": (
                    EvidenceEntry(
                        tool_name="get_consumer_lag",
                        arguments={"consumer_group": "worker-dispatcher"},
                        result_summary='{"lag": 40}',
                        timestamp=now,
                    ),
                    EvidenceEntry(
                        tool_name="_escalate",
                        arguments={"reason": "budget exhausted"},
                        result_summary="escalated",
                        timestamp=now,
                    ),
                )
            }
        )

        assert last_step(with_marker) == {
            "kind": "read",
            "tool": "get_consumer_lag",
            "at": now.isoformat(),
        }

    def test_a_tier1_step_reports_as_an_action(self, run_state: RunState, now: datetime) -> None:
        acted = run_state.model_copy(
            update={
                "evidence": (
                    EvidenceEntry(
                        tool_name="restart_consumer_group",
                        arguments={"consumer_group": "worker-dispatcher"},
                        result_summary='{"accepted": true}',
                        timestamp=now,
                    ),
                )
            }
        )

        step = last_step(acted)
        assert step is not None and step["kind"] == "action"

    def test_an_unrecognized_tool_reports_as_an_action(
        self, run_state: RunState, now: datetime
    ) -> None:
        """`read` is the stronger claim — it says nothing changed — so unknown is `action`.

        Over-reporting a change is the safe direction for the person watching.
        """
        unknown = run_state.model_copy(
            update={
                "evidence": (
                    EvidenceEntry(
                        tool_name="some_tool_the_registry_never_heard_of",
                        arguments={},
                        result_summary="{}",
                        timestamp=now,
                    ),
                )
            }
        )

        step = last_step(unknown)
        assert step is not None and step["kind"] == "action"

    def test_no_step_yet_reports_none(self, run_state: RunState) -> None:
        assert last_step(run_state) is None


class TestOneStepPerCall:
    """The console's action ledger is one row per tool call, so one report carries one step."""

    def test_two_calls_in_one_transition_are_two_reports_one_step_each(
        self, run_state: RunState, now: datetime
    ) -> None:
        log = ToolCallLog(clock=_make_clock(now))
        log.observe(
            _traced(
                "get_consumer_lag",
                {"consumer_group": "worker-dispatcher"},
                text='{"lag": 42, "lag_known": true}',
            )
        )
        log.observe(_traced("list_dlq_messages", {"limit": 10}, text='{"messages": []}'))
        client = _RecordingClient()
        reporter = _reporter(client, tool_log=log)
        moved = _with(
            run_state,
            state=IncidentState.INVESTIGATING,
            evidence=(
                _entry("get_consumer_lag", now, '{"lag": 42}'),
                _entry("list_dlq_messages", now, '{"messages": []}'),
            ),
        )

        reporter.report(moved)

        sent = client.arguments_for(REPORT_RUN_TOOL)
        assert len(sent) == 2, "one report per call — the platform appends one step per call"
        assert [args["step"]["seq"] for args in sent] == [1, 2]
        assert [args["step"]["tool"] for args in sent] == ["get_consumer_lag", "list_dlq_messages"]
        assert reporter.steps_sent == 2

    def test_the_step_carries_the_wired_arguments_the_excerpt_and_the_latency(
        self, run_state: RunState, now: datetime
    ) -> None:
        # The three facts the audit log cannot answer: an `agent.tool_invoked` row has the
        # tool, the arguments and the latency but NO result, so "what did the agent see"
        # only exists here.
        log = ToolCallLog(clock=_make_clock(now))
        log.observe(
            _traced(
                "restart_consumer_group",
                {"consumer_group": "worker-dispatcher", "idempotency_key": "e80bfc0c"},
                text='{"restarted": true}',
                seconds=1.5,
            )
        )
        client = _RecordingClient()
        acted = _with(
            run_state,
            state=IncidentState.VERIFYING,
            evidence=(_entry("restart_consumer_group", now, "restarted"),),
        )

        _reporter(client, tool_log=log).report(acted)

        step = client.arguments_for(REPORT_RUN_TOOL)[0]["step"]
        assert step["kind"] == "action"
        assert step["arguments"] == {
            "consumer_group": "worker-dispatcher",
            "idempotency_key": "e80bfc0c",
        }
        assert step["result_excerpt"] == '{"restarted": true}'
        assert step["outcome"] == "ok"
        assert step["latency_ms"] == 1500

    def test_a_call_the_platform_refused_is_still_a_step(
        self, run_state: RunState, now: datetime
    ) -> None:
        """No ledger entry exists for a refused call, and the console must still show it."""
        log = ToolCallLog(clock=_make_clock(now))
        log.observe(
            _traced("replay_dlq_by_ids", {"job_ids": ["x"]}, text="not_found", is_error=True)
        )
        client = _RecordingClient()

        _reporter(client, tool_log=log).report(run_state)

        step = client.arguments_for(REPORT_RUN_TOOL)[0]["step"]
        assert step["tool"] == "replay_dlq_by_ids"
        assert step["outcome"] == "refused"
        assert step["result_excerpt"] == "not_found"

    def test_a_call_that_never_landed_is_still_a_step(
        self, run_state: RunState, now: datetime
    ) -> None:
        log = ToolCallLog(clock=_make_clock(now))
        log.observe(_traced("get_consumer_lag", error="MCPError: MCP error -32000: HTTP 503"))
        client = _RecordingClient()

        _reporter(client, tool_log=log).report(run_state)

        step = client.arguments_for(REPORT_RUN_TOOL)[0]["step"]
        assert step["outcome"].startswith("error: MCPError")
        assert step["result_excerpt"] is None, "there was nothing to see — not an empty reading"

    def test_bookkeeping_markers_are_not_steps(self, run_state: RunState, now: datetime) -> None:
        client = _RecordingClient()
        triaged = _with(
            run_state,
            state=IncidentState.INVESTIGATING,
            evidence=(_entry("_triage", now, "severity=high"),),
        )

        _reporter(client, tool_log=ToolCallLog()).report(triaged)

        sent = client.arguments_for(REPORT_RUN_TOOL)
        assert len(sent) == 1
        assert "step" not in sent[0]

    def test_the_seq_is_monotonic_across_the_whole_run(
        self, run_state: RunState, now: datetime
    ) -> None:
        log = ToolCallLog(clock=_make_clock(now))
        client = _RecordingClient()
        reporter = _reporter(client, tool_log=log)
        first = _entry("get_consumer_lag", now, "{}")
        second = _entry("get_dag_state", now, "{}")

        log.observe(_traced("get_consumer_lag"))
        reporter.report(_with(run_state, state=IncidentState.INVESTIGATING, evidence=(first,)))
        log.observe(_traced("get_dag_state"))
        reporter.report(_with(run_state, state=IncidentState.PLANNING, evidence=(first, second)))

        sent = client.arguments_for(REPORT_RUN_TOOL)
        seqs = [args["step"]["seq"] for args in sent if "step" in args]
        assert seqs == [1, 2], "the ledger is ordered by seq, so it never restarts"

    def test_an_entry_is_reported_once_even_though_the_ledger_only_grows(
        self, run_state: RunState, now: datetime
    ) -> None:
        client = _RecordingClient()
        reporter = _reporter(client, tool_log=ToolCallLog(clock=_make_clock(now)))
        entry = _entry("get_consumer_lag", now, "{}")
        state = _with(run_state, state=IncidentState.INVESTIGATING, evidence=(entry,))

        reporter.report(state)
        reporter.report(state)

        steps = [args for args in client.arguments_for(REPORT_RUN_TOOL) if "step" in args]
        assert len(steps) == 1

    def test_the_reporters_own_reports_are_never_steps(self) -> None:
        """Otherwise every report would carry the last one, forever."""
        log = ToolCallLog()
        log.observe(_traced(REPORT_RUN_TOOL, {"run_id": "x"}))
        log.observe(_traced(REPORT_BRIEFING_TOOL, {"run_id": "x"}))
        log.observe(_traced("get_consumer_lag"))

        assert [call.tool for call in log.drain()] == ["get_consumer_lag"]

    def test_the_backlog_of_a_terminal_transition_is_reported_before_the_run_closes(
        self, run_state: RunState, now: datetime
    ) -> None:
        # A terminal state CLOSES the run (`agent_run_already_finished` after it), so the
        # steps a terminal transition took have to travel under the state the run was in
        # while it took them. That is also the honest stamp: the verify poll happened while
        # the run was VERIFYING.
        log = ToolCallLog(clock=_make_clock(now))
        client = _RecordingClient()
        reporter = _reporter(client, tool_log=log)
        verifying = _with(run_state, state=IncidentState.VERIFYING, remediation_plan=_PLAN)
        reporter.report(verifying)

        log.observe(_traced("get_consumer_lag", text='{"lag": 0}'))
        resolved = _with(
            verifying,
            state=IncidentState.RESOLVED,
            evidence=(
                _entry("get_consumer_lag", now, '{"lag": 0}', attempt=1, of=4),
                _entry(VERIFY_JUDGE_MARKER, now, "verified: the backlog drained", attempt=1, of=4),
            ),
        )
        reporter.report(resolved)

        states = [args["state"] for args in client.arguments_for(REPORT_RUN_TOOL)]
        assert states == ["verifying", "verifying", "resolved"]
        assert states.count("resolved") == 1, "exactly one report may close the run"
        assert "verification" in client.arguments_for(REPORT_RUN_TOOL)[-1]

    def test_the_old_last_step_field_walks_forward_with_the_new_one(
        self, run_state: RunState, now: datetime
    ) -> None:
        """Both fields are sent, so an operator reading either sees the same call."""
        log = ToolCallLog(clock=_make_clock(now))
        log.observe(_traced("get_consumer_lag"))
        log.observe(_traced("restart_consumer_group"))
        client = _RecordingClient()
        moved = _with(
            run_state,
            state=IncidentState.VERIFYING,
            evidence=(
                _entry("get_consumer_lag", now, "{}"),
                _entry("restart_consumer_group", now, "{}"),
            ),
        )

        _reporter(client, tool_log=log).report(moved)

        sent = client.arguments_for(REPORT_RUN_TOOL)
        assert [args["last_step"]["tool"] for args in sent] == [
            "get_consumer_lag",
            "restart_consumer_group",
        ]

    def test_a_long_reading_is_excerpted_not_sent_whole(
        self, run_state: RunState, now: datetime
    ) -> None:
        log = ToolCallLog(clock=_make_clock(now))
        log.observe(_traced("list_dlq_messages", text="x" * 5_000))
        client = _RecordingClient()

        _reporter(client, tool_log=log).report(run_state)

        excerpt = client.arguments_for(REPORT_RUN_TOOL)[0]["step"]["result_excerpt"]
        assert len(excerpt) == 400 and excerpt.endswith("…")

    def test_a_garbage_tracer_record_never_fails_the_tool_call(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # The hook runs INSIDE `MCPClient.call_tool`, so an observer that raises would turn
        # telemetry into a failed tool call — the one thing fail-open forbids.
        log = ToolCallLog()
        hook = log.tee(None)

        with caplog.at_level(logging.WARNING):
            hook({"tool_name": "get_consumer_lag", "arguments": "not a mapping"})

        assert log.drain() == []
        assert "dropped one observed call" in caplog.text

    def test_the_inner_tracer_still_runs_first(self) -> None:
        """The JSONL trace is the run's own record; telemetry does not displace it."""
        order: list[str] = []
        log = ToolCallLog()
        hook = log.tee(lambda _record: order.append("trace"))

        hook(_traced("get_consumer_lag"))

        assert order == ["trace"]
        assert [call.tool for call in log.drain()] == ["get_consumer_lag"]

    def test_forget_drops_what_happened_before_the_run(self) -> None:
        """The precondition probes go through the agent's client and are not its steps."""
        log = ToolCallLog()
        log.observe(_traced("get_consumer_lag"))

        log.forget()

        assert log.drain() == []


class TestTheRankedHypothesesTravel:
    """The agent panel was empty on the first take. This is the field that fills it."""

    def test_a_report_carries_the_whole_ranking_with_its_reasoning(
        self, run_state: RunState, now: datetime
    ) -> None:
        client = _RecordingClient()
        ranked = _with(
            run_state,
            state=IncidentState.INVESTIGATING,
            hypotheses=(
                _hypothesis("consumer_saturation", 0.85, "lag 42 and climbing, no consumer"),
                _hypothesis("poison_message", 0.2, "one DLQ row, unrelated group"),
            ),
        )

        _reporter(client).report(ranked)

        sent = client.arguments_for(REPORT_RUN_TOOL)[0]
        assert [h["name"] for h in sent["hypotheses"]] == ["consumer_saturation", "poison_message"]
        assert [h["confidence"] for h in sent["hypotheses"]] == [0.85, 0.2]
        assert sent["hypotheses"][0]["reasoning_excerpt"] == "lag 42 and climbing, no consumer"

    def test_the_current_hypothesis_keeps_the_three_fields_the_platform_declares(
        self, run_state: RunState
    ) -> None:
        # `HypothesisReport` forbids unknown keys, so a `reasoning_excerpt` here would have
        # the whole report refused. The reasoning travels on the ranked list instead.
        client = _RecordingClient()
        ranked = _with(run_state, hypotheses=(_hypothesis("consumer_saturation", 0.85),))

        _reporter(client).report(ranked)

        assert set(client.arguments_for(REPORT_RUN_TOOL)[0]["current_hypothesis"]) == {
            "name",
            "category",
            "confidence",
        }

    def test_a_reranking_is_reported_on_the_next_report(self, run_state: RunState) -> None:
        client = _RecordingClient()
        reporter = _reporter(client)
        first = _with(run_state, hypotheses=(_hypothesis("consumer_saturation", 0.4),))
        reporter.report(first)

        reporter.report(_with(first, hypotheses=(_hypothesis("consumer_saturation", 0.9),)))

        sent = client.arguments_for(REPORT_RUN_TOOL)
        assert [args["hypotheses"][0]["confidence"] for args in sent] == [0.4, 0.9]

    def test_no_ranking_yet_is_null_rather_than_an_empty_list(self, run_state: RunState) -> None:
        """An empty list reads as "it considered nothing", which is a different claim."""
        assert ranked_hypotheses(run_state) is None

    def test_a_long_reasoning_is_excerpted(self, run_state: RunState) -> None:
        ranked = _with(run_state, hypotheses=(_hypothesis("saturation", 0.5, "y" * 900),))

        excerpt = (ranked_hypotheses(ranked) or [{}])[0]["reasoning_excerpt"]

        assert len(excerpt) == 280 and excerpt.endswith("…")

    def test_an_over_long_hypothesis_name_is_capped_rather_than_refused(
        self, run_state: RunState
    ) -> None:
        """`Hypothesis.name` is free-form model output and the platform caps it at 128.

        Over the cap the platform refuses the WHOLE report as invalid params (plat #230) —
        which this module would read as an older platform and answer by narrowing every
        later report of the run. So a long name costs a truncation, never the console.
        """
        client = _RecordingClient()
        wordy = _with(run_state, hypotheses=(_hypothesis("saturation " * 40, 0.5),))

        _reporter(client).report(wordy)

        sent = client.arguments_for(REPORT_RUN_TOOL)[0]
        assert len(sent["current_hypothesis"]["name"]) == 128
        assert sent["current_hypothesis"]["name"].endswith("…")
        assert len(sent["hypotheses"][0]["name"]) == 128
        assert len(sent["hypotheses"][0]["category"]) <= 64


class TestThePlanTravelsOnce:
    def test_the_plan_is_reported_when_the_run_enters_remediating(
        self, run_state: RunState
    ) -> None:
        client = _RecordingClient()
        planning = _with(run_state, state=IncidentState.REMEDIATING, remediation_plan=_PLAN)

        _reporter(client).report(planning)

        plan = client.arguments_for(REPORT_RUN_TOOL)[0]["plan"]
        assert plan["action_tool"] == "restart_consumer_group"
        assert plan["action_arguments"] == {"consumer_group": "worker-dispatcher"}
        assert plan["target_hypothesis"] == "consumer_saturation"
        assert plan["rationale_excerpt"] == "the group is dead and the backlog is the symptom"

    def test_the_same_plan_is_not_sent_again(self, run_state: RunState) -> None:
        client = _RecordingClient()
        reporter = _reporter(client)
        remediating = _with(run_state, state=IncidentState.REMEDIATING, remediation_plan=_PLAN)

        reporter.report(remediating)
        reporter.report(_with(remediating, state=IncidentState.VERIFYING))

        assert [("plan" in args) for args in client.arguments_for(REPORT_RUN_TOOL)] == [True, False]

    def test_a_second_attempts_different_plan_is_reported(self, run_state: RunState) -> None:
        # ADR 0056: a failed attempt may reinvestigate and plan again. The console must show
        # the plan the run is on, not the one it abandoned.
        client = _RecordingClient()
        reporter = _reporter(client)
        first = _with(run_state, state=IncidentState.REMEDIATING, remediation_plan=_PLAN)
        reporter.report(first)
        second_plan = {**_PLAN, "action_arguments": {"consumer_group": "worker-dispatcher-2"}}

        reporter.report(_with(first, remediation_plan=second_plan))

        plans = [args["plan"] for args in client.arguments_for(REPORT_RUN_TOOL) if "plan" in args]
        assert [plan["action_arguments"]["consumer_group"] for plan in plans] == [
            "worker-dispatcher",
            "worker-dispatcher-2",
        ]

    def test_an_over_long_target_hypothesis_is_capped(self, run_state: RunState) -> None:
        """It is a hypothesis NAME, so it is free-form too, and capped at the same 128."""
        client = _RecordingClient()
        wordy = _with(
            run_state,
            state=IncidentState.REMEDIATING,
            remediation_plan={**_PLAN, "target_hypothesis": "saturation " * 40},
        )

        _reporter(client).report(wordy)

        assert len(client.arguments_for(REPORT_RUN_TOOL)[0]["plan"]["target_hypothesis"]) == 128

    def test_no_plan_yet_sends_no_plan_field(self, run_state: RunState) -> None:
        client = _RecordingClient()

        _reporter(client).report(run_state)

        assert "plan" not in client.arguments_for(REPORT_RUN_TOOL)[0]


class TestAVerificationPerPoll:
    def test_each_poll_is_its_own_verification_with_its_ordinals(
        self, run_state: RunState, now: datetime
    ) -> None:
        client = _RecordingClient()
        polled = _with(
            run_state,
            state=IncidentState.VERIFYING,
            remediation_plan=_PLAN,
            evidence=(
                _entry(VERIFY_JUDGE_MARKER, now, "not_verified: lag still 42", attempt=1, of=4),
                _entry(VERIFY_JUDGE_MARKER, now, "verified: lag reads 0", attempt=2, of=4),
            ),
        )

        _reporter(client, tool_log=ToolCallLog()).report(polled)

        verdicts = [
            args["verification"]
            for args in client.arguments_for(REPORT_RUN_TOOL)
            if "verification" in args
        ]
        assert [v["verdict"] for v in verdicts] == ["not_verified", "verified"]
        assert [(v["attempt"], v["of"]) for v in verdicts] == [(1, 4), (2, 4)]
        assert verdicts[0]["reasoning_excerpt"] == "lag still 42"

    def test_a_stabilizer_verdict_comes_from_the_attempt_record(
        self, run_state: RunState, now: datetime
    ) -> None:
        # `verified_stabilizer` exists nowhere else: the judge says `verified` and ADR 0026
        # then decides the action did not end the incident.
        client = _RecordingClient()
        stabilized = _with(
            run_state,
            state=IncidentState.INVESTIGATING,
            evidence=(
                _entry(
                    ATTEMPT_FAILED_MARKER,
                    now,
                    "attempt 1 of 2: pause_dag({}) executed, then get_dag_state read … — "
                    "verdict verified_stabilizer. A pause stabilizes and does not resolve.",
                    attempt=1,
                    of=2,
                    verdict="verified_stabilizer",
                ),
            ),
        )

        _reporter(client, tool_log=ToolCallLog()).report(stabilized)

        verification = client.arguments_for(REPORT_RUN_TOOL)[0]["verification"]
        assert verification["verdict"] == "verified_stabilizer"
        assert verification["attempt"] == 1 and verification["of"] == 2

    def test_a_verdict_is_not_a_step(self, run_state: RunState, now: datetime) -> None:
        client = _RecordingClient()
        judged = _with(
            run_state,
            evidence=(_entry(VERIFY_JUDGE_MARKER, now, "verified: drained", attempt=1, of=1),),
        )

        _reporter(client, tool_log=ToolCallLog()).report(judged)

        sent = client.arguments_for(REPORT_RUN_TOOL)[0]
        assert "verification" in sent and "step" not in sent


class TestTheBudgetMeter:
    def test_every_report_carries_the_ledger_as_a_meter(self, run_state: RunState) -> None:
        client = _RecordingClient()
        spent = _with(
            run_state,
            budget=run_state.budget.model_copy(
                update={
                    "tool_calls_used": 3,
                    "tokens_used": 12_400,
                    "usd_used": Decimal("0.0421"),
                    "wall_seconds_used": 42.5,
                }
            ),
        )

        _reporter(client).report(spent)

        assert client.arguments_for(REPORT_RUN_TOOL)[0]["budget"] == {
            "tool_calls_used": 3,
            "tool_calls_max": 25,
            "tokens_used": 12_400,
            "usd_used": 0.0421,
            "wall_seconds": 42.5,
        }

    def test_the_dollar_figure_is_a_number_the_wire_can_carry(self, run_state: RunState) -> None:
        """`Decimal` is not JSON: left in place it would raise inside httpx, past the guard."""
        spent = _with(
            run_state,
            budget=run_state.budget.model_copy(update={"usd_used": Decimal("1.234567")}),
        )

        json.dumps(budget_payload(spent))

    def test_the_meter_still_never_moves_the_budget(
        self, run_state: RunState, now: datetime
    ) -> None:
        # The widening reports MORE and must still cost nothing: reporting the budget is not
        # spending it.
        run = _with(run_state, alert={"source": "billing", "severity": "info"})
        reporter = _reporter(_RecordingClient(), tool_log=ToolCallLog())

        final = run_to_completion(
            run,
            clock=_make_clock(now),
            checkpointer=ReportingCheckpointer(InMemoryCheckpointer(), reporter),
        )

        assert reporter.reports_sent >= 2, "the reports really were sent"
        assert final.budget.tool_calls_used == run.budget.tool_calls_used
        assert final.budget.tokens_used == run.budget.tokens_used
        assert final.budget.usd_used == run.budget.usd_used


class TestAnOlderPlatformNarrowsOnceAndKeepsTheOldFields:
    """The re-pin is the coordinator's, so the reporter meets both platforms."""

    @pytest.mark.parametrize(
        "as_tool_error",
        [False, True],
        ids=["json-rpc-error-32602", "200-with-isError"],
    )
    def test_either_refusal_shape_narrows_and_the_old_fields_land(
        self, run_state: RunState, now: datetime, as_tool_error: bool
    ) -> None:
        """Both routes, because the MEASURED one was the route the code did not read.

        On 2026-09-20 the whole rehearsal reported nothing: v0.6.15 answers a report
        carrying an undeclared field with a JSON-RPC ``-32602``, which arrives as an
        ``MCPError`` and not as the 200-with-``isError`` the refusal path was reading.
        """
        platform = _OldPlatformClient(as_tool_error=as_tool_error)
        log = ToolCallLog(clock=_make_clock(now))
        log.observe(_traced("get_consumer_lag", text='{"lag": 42}'))
        reporter = _reporter(platform, tool_log=log, run_label="demo")
        moved = _with(
            run_state,
            state=IncidentState.INVESTIGATING,
            hypotheses=(_hypothesis("consumer_saturation", 0.85),),
            evidence=(_entry("get_consumer_lag", now, '{"lag": 42}'),),
        )

        reporter.report(moved)

        assert reporter.widened is False
        assert [args["state"] for args in platform.accepted] == ["investigating"]
        assert set(platform.accepted[0]) <= set(NARROW_FIELDS)

    def test_a_missing_scope_does_not_narrow_anything(self, run_state: RunState) -> None:
        # An HTTP 403 arrives as an `MCPError` too — a token minted before
        # `agent_runs:write` existed. Narrowing would not help and would hide the real
        # cause behind a thinner console; the runbook's re-mint line is the fix.
        client = _RaisingClient()
        reporter = _reporter(client, tool_log=ToolCallLog())

        reporter.report(run_state)

        assert reporter.widened is True
        assert client.attempts == 1, "no retry: the scope is missing, not the schema old"

    def test_the_old_fields_still_land_when_the_widened_ones_are_refused(
        self, run_state: RunState, now: datetime
    ) -> None:
        platform = _OldPlatformClient()
        log = ToolCallLog(clock=_make_clock(now))
        log.observe(_traced("get_consumer_lag", text='{"lag": 42}'))
        reporter = _reporter(platform, tool_log=log, run_label="demo")
        moved = _with(
            run_state,
            state=IncidentState.INVESTIGATING,
            hypotheses=(_hypothesis("consumer_saturation", 0.85),),
            evidence=(_entry("get_consumer_lag", now, '{"lag": 42}'),),
        )

        reporter.report(moved)

        assert reporter.widened is False
        assert len(platform.accepted) == 1
        landed = platform.accepted[0]
        assert landed["state"] == "investigating"
        assert landed["current_hypothesis"]["name"] == "consumer_saturation"
        assert landed["last_step"]["tool"] == "get_consumer_lag"
        assert landed["run_label"] == "demo"
        assert set(landed) <= set(NARROW_FIELDS)
        assert reporter.reports_sent == 1

    def test_the_refusal_is_logged_once_and_not_per_call(
        self, run_state: RunState, now: datetime, caplog: pytest.LogCaptureFixture
    ) -> None:
        platform = _OldPlatformClient()
        log = ToolCallLog(clock=_make_clock(now))
        reporter = _reporter(platform, tool_log=log)
        for tool in ("get_consumer_lag", "get_dag_state", "get_redis_health"):
            log.observe(_traced(tool))

        with caplog.at_level(logging.WARNING):
            reporter.report(_with(run_state, state=IncidentState.INVESTIGATING))
            reporter.report(_with(run_state, state=IncidentState.PLANNING))

        assert caplog.text.count("Falling back to the fields platform v0.6.15 accepts") == 1
        widened = [args for _name, args in platform.calls if set(args) - set(NARROW_FIELDS)]
        assert len(widened) == 1, "one refusal is the whole cost; every later report is narrow"
        # Two states, one report each: the narrow contract carries no step, so N pending
        # items do not become N identical calls.
        assert [args["state"] for args in platform.accepted] == ["investigating", "planning"]

    def test_a_finished_run_refusal_does_not_narrow_anything(self, run_state: RunState) -> None:
        # `agent_run_already_finished` is about the RUN. Narrowing would not help, and
        # latching on it would silently thin every later run report.
        client = _RefusingClient("agent_run_already_finished")
        reporter = _reporter(client, tool_log=ToolCallLog())

        reporter.report(run_state)

        assert reporter.widened is True
        assert client.attempts == 1, "no retry: the run is closed, not the schema old"

    def test_the_narrow_field_set_is_one_the_snapshot_accepts(self) -> None:
        """A rename on the platform side must fail here, not at the first live report."""
        declared = {tool["name"]: tool for tool in json.loads(_SNAPSHOT_PATH.read_text())["tools"]}
        properties = set(declared[REPORT_RUN_TOOL]["inputSchema"]["properties"])
        assert set(NARROW_FIELDS) <= properties

    def test_a_narrowed_run_still_completes_and_still_says_what_it_lost(
        self, run_state: RunState, now: datetime
    ) -> None:
        platform = _OldPlatformClient()
        reporter = _reporter(platform, tool_log=ToolCallLog())
        run = _with(run_state, alert={"source": "billing", "severity": "info"})

        final = run_to_completion(
            run,
            clock=_make_clock(now),
            checkpointer=ReportingCheckpointer(InMemoryCheckpointer(), reporter),
        )

        assert final.state is IncidentState.ESCALATED
        assert "NARROWED" in summarize(reporter)
        # One refused report that then landed narrow is ONE failure. Counting the fallback
        # as a second made the rehearsal's summary line read as two lost reports.
        assert len(reporter.failures) == 1
        assert reporter.narrowed_because is not None


class TestNothingLeaksASecret:
    def test_no_report_body_carries_the_credential(
        self, run_state: RunState, now: datetime
    ) -> None:
        """End to end through the real client: the token is a header and never a field.

        Worth pinning now that a step carries wired ARGUMENTS and a result excerpt — two
        fields that did not exist when this module only sent a state.
        """
        token = "plat_tok_never_in_a_body"
        bodies: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            bodies.append(json.loads(request.content))
            assert request.headers["Authorization"] == f"Bearer {token}"
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {"content": [{"type": "text", "text": "{}"}], "isError": False},
                },
            )

        client = MCPClient(
            "http://platform.invalid/mcp", token, transport=httpx.MockTransport(handler)
        )
        log = ToolCallLog(clock=_make_clock(now))
        log.observe(_traced("restart_consumer_group", {"consumer_group": "worker-dispatcher"}))
        reporter = RunReporter(client, run_id=uuid4(), run_label="demo", tool_log=log)
        rich = _with(
            run_state,
            state=IncidentState.VERIFYING,
            remediation_plan=_PLAN,
            hypotheses=(_hypothesis("consumer_saturation", 0.85),),
            evidence=(_entry("restart_consumer_group", now, "restarted"),),
        )

        reporter.report(rich)
        reporter.report_briefing(render_briefing(rich.with_state(IncidentState.ESCALATED, now)))
        client.close()

        assert bodies, "the reports really were sent"
        serialized = json.dumps(bodies)
        assert token not in serialized
        assert "Authorization" not in serialized
        assert reporter.failures == []

    def test_the_whole_payload_is_json_the_wire_can_carry(
        self, run_state: RunState, now: datetime
    ) -> None:
        client = _RecordingClient()
        log = ToolCallLog(clock=_make_clock(now))
        log.observe(_traced("get_consumer_lag", text='{"lag": 42}'))
        rich = _with(
            run_state,
            state=IncidentState.REMEDIATING,
            remediation_plan=_PLAN,
            hypotheses=(_hypothesis("consumer_saturation", 0.85),),
            evidence=(_entry("get_consumer_lag", now, "{}"),),
        )

        _reporter(client, tool_log=log).report(rich)

        for args in client.arguments_for(REPORT_RUN_TOOL):
            json.dumps(args)


class TestTheSummaryLine:
    def test_it_names_the_run_and_the_failures(self, run_state: RunState) -> None:
        # The line exists so "the console was empty" has an answer other than "the
        # frontend is broken".
        reporter = _reporter(_RaisingClient())
        reporter.report(run_state)

        line = summarize(reporter)
        assert reporter.run_id in line
        assert "1 FAILED" in line
        assert "403" in line
        assert "briefing NOT sent" in line

    def test_off_says_off(self) -> None:
        assert summarize(None) == "agent-run reporting: off"
