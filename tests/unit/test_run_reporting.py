"""The run reporter: one report per transition, fail-open, and off the planner's page.

ADR 0068. Four properties are load-bearing and each has a class here: reporting happens
once per transition and the briefing exactly once; every failure is swallowed with a log
line; a report is NOT a tool call and cannot move the budget; and neither reporting tool is
anything a model could choose.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from incident_commander.agent.briefing import render_briefing
from incident_commander.agent.hypothesis import Hypothesis, HypothesisCategory
from incident_commander.agent.loop import run_to_completion
from incident_commander.agent.planner_context import format_tool_block
from incident_commander.agent.run_reporting import (
    REPORT_BRIEFING_TOOL,
    REPORT_RUN_TOOL,
    ReportingCheckpointer,
    RunReporter,
    last_step,
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
from incident_commander.tools.mcp_client import MCPError, ToolResult
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
