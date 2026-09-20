"""One bounded repair, then escalate — and both calls billed (ADR 0035).

A ``record_output`` payload the schema rejects buys exactly one re-ask carrying the
validation error, and the second failure escalates with both. Three claims: two calls
never three; both reach the ledger (a repair that charged only on success would make the
expensive path look cheap); and only an OUTPUT failure is repairable, never a transport one.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final
from uuid import uuid4

import pytest
from pydantic import BaseModel, ValidationError

from evals.graders.deterministic import DimensionResult, GradeDimension, GradeReport
from evals.graders.llm_judge import JudgeScore, judge_briefing
from evals.runner import _classify_failure
from incident_commander.agent.briefing import EscalationBriefing
from incident_commander.agent.briefing_enrichment import BriefingContent, enrich_briefing
from incident_commander.agent.hypothesis import InvestigationStep, without_probe
from incident_commander.agent.investigation import make_llm_investigate
from incident_commander.agent.remediation import RemediationPlan, make_llm_verify
from incident_commander.agent.state import BudgetLedger, EvidenceEntry, IncidentState, RunState
from incident_commander.llm.client import LLMError, LLMOutputError, LLMResult, LLMUsage
from incident_commander.llm.fakes import CannedLLMClient
from incident_commander.llm.prompts.loader import load_prompt
from incident_commander.llm.repair import (
    INVESTIGATION_PLANNER_INVALID,
    MAX_OUTPUT_REPAIRS,
    OUTPUT_INVALID_PREFIXES,
    PLANNER_OUTPUT_INVALID_CLASS,
    VERIFY_JUDGE_INVALID,
    OutputNotOffered,
    OutputRepairExhausted,
    call_with_output_repair,
    repair_message,
)
from incident_commander.tools.mcp_client import ToolResult

_GOOD_STEP: dict[str, Any] = {
    "hypotheses": [{"category": "unknown", "name": "n", "confidence": 0.5, "reasoning": "r"}],
    "next_action": {"kind": "stop", "reason": "enough evidence"},
}
# The live shape in miniature: the nested union arrives as an undecodable string.
_BAD_STEP: dict[str, Any] = {
    "hypotheses": [{"category": "unknown", "name": "n", "confidence": 0.5, "reasoning": "r"}],
    "next_action": "remediate the replay_safe row",
}


class _ScriptedLLM:
    """Plays a fixed script of payloads-or-exceptions, one per ``call``."""

    def __init__(self, script: list[Any], usage: LLMUsage | None = None) -> None:
        self._script = list(script)
        self._usage = usage or LLMUsage()
        self.calls: list[tuple[str, str]] = []
        self.repair_of: list[str | None] = []

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
        self.calls.append((system_prompt, user_message))
        self.repair_of.append(repair_of)
        if not self._script:
            raise AssertionError(
                f"call {len(self.calls)} was made with an empty script — the cap is not holding"
            )
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return LLMResult(
            output=output_model.model_validate(item),
            stop_reason="scripted",
            record_id=f"rec{len(self.calls)}",
            input_tokens=self._usage.input_tokens,
            output_tokens=self._usage.output_tokens,
        )


def _output_error(message: str = "output failed schema validation", **kw: Any) -> LLMOutputError:
    return LLMOutputError(message, **kw)


def _briefing(run_state: RunState) -> EscalationBriefing:
    return EscalationBriefing(
        incident_id=str(run_state.incident_id),
        final_state=IncidentState.ESCALATED,
        alert_summary="source=platform.dlq severity=critical",
        escalation_reason="one replay_safe row remains",
        budget_used={"tool_calls": 3},
    )


def _investigating(run_state: RunState, whole_queue_read: datetime | None = None) -> RunState:
    """An INVESTIGATING state on a subject-less alert.

    ``whole_queue_read`` seeds the unfiltered listing ADR 0041 requires before a
    dead-letter handoff.
    """
    update: dict[str, Any] = {
        "state": IncidentState.INVESTIGATING,
        "alert": {"source": "kafka", "severity": "high"},
    }
    if whole_queue_read is not None:
        update["evidence"] = (
            EvidenceEntry(
                tool_name="list_dlq_messages",
                arguments={"remediation_hint": None, "job_type": None, "limit": 50, "offset": 0},
                result_summary='{"total":5,"items":[]}',
                timestamp=whole_queue_read,
            ),
        )
    return run_state.model_copy(update=update)


class _NoMCP:
    def call_tool(
        self,
        name: str,
        arguments: Any,
        *,
        timeout_seconds: float | None = None,
    ) -> Any:
        raise AssertionError(f"no tool call expected, got {name}")


class _SequencedMCP:
    """Returns one canned ``ToolResult`` per call, in order."""

    def __init__(self, results: list[ToolResult]) -> None:
        self._results = list(results)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call_tool(
        self,
        name: str,
        arguments: Any,
        *,
        timeout_seconds: float | None = None,
    ) -> ToolResult:
        self.calls.append((name, dict(arguments)))
        return self._results.pop(0)


def _lag_result(lag: int) -> ToolResult:
    payload = {
        "consumer_group": "worker-dispatcher",
        "lag": lag,
        "lag_known": True,
        "source": "live",
        "cache_key": "kafka:consumer_lag:worker-dispatcher",
    }
    return ToolResult(content=[{"type": "text", "text": json.dumps(payload)}], is_error=False)


_PLAN: Final[RemediationPlan] = RemediationPlan(
    target_hypothesis="consumer_saturation",
    action_tool="restart_consumer_group",
    action_arguments={"consumer_group": "worker-dispatcher"},
    verify_tool="get_consumer_lag",
    verify_arguments={"consumer_group": "worker-dispatcher"},
    verify_expectation="lag should drop toward zero",
)

_GOOD_JUDGMENT: dict[str, Any] = {"verdict": "verified", "reasoning": "lag is 0"}
# ``verdict`` is a two-value Literal, so a third word is a shape the schema
# rejects and the decoder cannot rescue — the judge-side twin of _BAD_STEP.
_BAD_JUDGMENT: dict[str, Any] = {"verdict": "probably fine", "reasoning": "lag is 0"}

_GOOD_SCORE: dict[str, Any] = {
    "groundedness": 0.9,
    "actionability": 0.8,
    "reasoning": "every claim traces to the trail",
}
# Out of the 0-1 band ``JudgeScore`` declares. Constrained decoding does not
# guarantee those bounds, which is why the runner catches ValidationError.
_BAD_SCORE: dict[str, Any] = {
    "groundedness": 7.0,
    "actionability": 0.8,
    "reasoning": "scored out of ten",
}


def _verifying() -> RunState:
    now = datetime(2026, 7, 15, 20, 0, tzinfo=UTC)
    return RunState(
        incident_id=uuid4(),
        state=IncidentState.VERIFYING,
        alert={"source": "test", "severity": "high"},
        budget=BudgetLedger(
            max_tool_calls=25,
            max_tokens=200_000,
            max_wall_seconds=1_800,
            max_usd=Decimal("5.00"),
        ),
        created_at=now,
        updated_at=now,
        remediation_plan=_PLAN.model_dump(mode="json"),
    )


class TestTheCap:
    def test_the_cap_is_one_and_matches_the_argument_refusal_cap(self) -> None:
        """ADR 0030's cap and this one are the same number for the same reason."""
        from incident_commander.agent.remediation import _MAX_ARGUMENT_REFUSALS

        assert MAX_OUTPUT_REPAIRS == 1
        assert MAX_OUTPUT_REPAIRS == _MAX_ARGUMENT_REFUSALS

    def test_two_failures_make_exactly_two_calls(self) -> None:
        llm = _ScriptedLLM([_BAD_STEP, _BAD_STEP])
        with pytest.raises(OutputRepairExhausted):
            call_with_output_repair(
                llm,
                system_prompt="s",
                user_message="u",
                output_model=InvestigationStep,
                model="m",
            )
        assert len(llm.calls) == MAX_OUTPUT_REPAIRS + 1 == 2

    def test_a_clean_call_makes_exactly_one(self) -> None:
        llm = _ScriptedLLM([_GOOD_STEP])
        call = call_with_output_repair(
            llm,
            system_prompt="s",
            user_message="u",
            output_model=InvestigationStep,
            model="m",
        )
        assert len(llm.calls) == 1
        assert call.failures == ()
        assert call.was_repaired is False


class TestTheRepairTurn:
    def test_the_re_ask_repeats_the_original_and_adds_the_error(self) -> None:
        llm = _ScriptedLLM([_BAD_STEP, _GOOD_STEP])
        call = call_with_output_repair(
            llm,
            system_prompt="s",
            user_message="ORIGINAL CONTEXT",
            output_model=InvestigationStep,
            model="m",
        )
        assert call.was_repaired
        second = llm.calls[1][1]
        assert second.startswith("ORIGINAL CONTEXT")
        assert "next_action" in second
        assert "nested objects as objects" not in second  # the file, not a paraphrase
        assert "Nested objects are objects" in second

    def test_the_repair_turn_is_a_versioned_prompt_file(self) -> None:
        """CLAUDE.md: prompts live in files with snapshot tests, never inline."""
        prompt = load_prompt("output_repair")
        message = repair_message("u", ValueError("boom"))
        # Every line of the file reaches the model, with the placeholder
        # filled — a paraphrase in code would defeat the snapshot suite.
        for line in prompt.splitlines():
            if line.strip() and "{error}" not in line:
                assert line in message
        assert "{error}" not in message
        assert "boom" in message

    def test_a_long_pydantic_error_is_trimmed(self) -> None:
        message = repair_message("u", ValueError("x" * 5000))
        assert "(error truncated)" in message
        assert len(message) < 5000

    def test_the_re_ask_names_the_record_it_repairs(self) -> None:
        """``repair_of`` is what pairs the two trace records (ADR 0035)."""
        llm = _ScriptedLLM([_output_error(record_id="abc123"), _GOOD_STEP])
        call_with_output_repair(
            llm,
            system_prompt="s",
            user_message="u",
            output_model=InvestigationStep,
            model="m",
        )
        assert llm.repair_of == [None, "abc123"]


class TestOnlyOutputFailuresAreRepaired:
    def test_a_transport_error_is_not_re_asked(self) -> None:
        llm = _ScriptedLLM([LLMError("LLM transport failure after 3 attempts"), _GOOD_STEP])
        with pytest.raises(LLMError) as excinfo:
            call_with_output_repair(
                llm,
                system_prompt="s",
                user_message="u",
                output_model=InvestigationStep,
                model="m",
            )
        assert not isinstance(excinfo.value, OutputRepairExhausted)
        assert len(llm.calls) == 1

    def test_an_output_error_is_re_asked(self) -> None:
        llm = _ScriptedLLM([_output_error(), _GOOD_STEP])
        call = call_with_output_repair(
            llm,
            system_prompt="s",
            user_message="u",
            output_model=InvestigationStep,
            model="m",
        )
        assert call.was_repaired
        assert len(llm.calls) == 2

    def test_a_raw_validation_error_is_re_asked(self) -> None:
        """The canned client validates directly and raises pydantic's own error."""
        llm = _ScriptedLLM([_BAD_STEP, _GOOD_STEP])
        call = call_with_output_repair(
            llm,
            system_prompt="s",
            user_message="u",
            output_model=InvestigationStep,
            model="m",
        )
        assert isinstance(call.failures[0], ValidationError)


class TestBothErrorsReachTheEscalation:
    def test_the_exhausted_error_names_both_failures(self) -> None:
        llm = _ScriptedLLM([_output_error("first complaint"), _output_error("second complaint")])
        with pytest.raises(OutputRepairExhausted) as excinfo:
            call_with_output_repair(
                llm,
                system_prompt="s",
                user_message="u",
                output_model=InvestigationStep,
                model="m",
            )
        text = str(excinfo.value)
        assert "first complaint" in text
        assert "second complaint" in text
        assert f"repair 1 of {MAX_OUTPUT_REPAIRS}" in text

    def test_the_exhausted_error_sums_what_both_calls_billed(self) -> None:
        llm = _ScriptedLLM(
            [
                _output_error(usage=LLMUsage(input_tokens=100, output_tokens=10)),
                _output_error(usage=LLMUsage(input_tokens=200, output_tokens=20)),
            ]
        )
        with pytest.raises(OutputRepairExhausted) as excinfo:
            call_with_output_repair(
                llm,
                system_prompt="s",
                user_message="u",
                output_model=InvestigationStep,
                model="m",
            )
        usage = excinfo.value.usage
        assert usage is not None
        assert usage.input_tokens == 300
        assert usage.output_tokens == 30


class TestTheInvestigationPlanner:
    def test_a_malformed_first_reply_does_not_end_the_run(
        self, run_state: RunState, now: datetime
    ) -> None:
        """The live failure, end to end: bad shape, one repair, run proceeds."""
        llm = _ScriptedLLM([_BAD_STEP, _GOOD_STEP])
        transition = make_llm_investigate(_NoMCP(), llm, model="m")
        result = transition(_investigating(run_state), now)
        assert result.state is IncidentState.ESCALATED  # the planner said stop
        stops = [e for e in result.evidence if e.tool_name == "_planner_stop"]
        assert len(stops) == 1
        assert "enough evidence" in stops[0].result_summary
        assert len(llm.calls) == 2

    def test_both_calls_are_billed_on_the_repaired_path(
        self, run_state: RunState, now: datetime
    ) -> None:
        llm = _ScriptedLLM(
            [
                _output_error(usage=LLMUsage(input_tokens=1000, output_tokens=100)),
                _GOOD_STEP,
            ],
            usage=LLMUsage(input_tokens=2000, output_tokens=200),
        )
        transition = make_llm_investigate(_NoMCP(), llm, model="claude-sonnet-4-6")
        result = transition(_investigating(run_state), now)
        assert result.budget.tokens_used == 1000 + 100 + 2000 + 200
        assert result.budget.usd_used > 0

    def test_two_malformed_replies_escalate_with_both_errors(
        self, run_state: RunState, now: datetime
    ) -> None:
        llm = _ScriptedLLM(
            [
                _output_error("first complaint", usage=LLMUsage(input_tokens=1000)),
                _output_error("second complaint", usage=LLMUsage(input_tokens=2000)),
            ]
        )
        transition = make_llm_investigate(_NoMCP(), llm, model="claude-sonnet-4-6")
        result = transition(_investigating(run_state), now)
        assert result.state is IncidentState.ESCALATED
        marker = [e for e in result.evidence if e.tool_name == "_planner_escalate"]
        assert len(marker) == 1
        reason = marker[0].result_summary
        assert reason.startswith(INVESTIGATION_PLANNER_INVALID)
        assert "first complaint" in reason
        assert "second complaint" in reason
        # Cap respected, and both billed calls charged.
        assert len(llm.calls) == 2
        assert result.budget.tokens_used == 3000


class TestTheBriefingWriter:
    def test_enrichment_survives_one_malformed_reply(
        self, run_state: RunState, now: datetime
    ) -> None:
        llm = _ScriptedLLM(
            [
                {"findings": "", "recommendation": "x"},
                {"findings": "one row left", "recommendation": "check fc8d2a03"},
            ]
        )
        briefing = _briefing(run_state)
        enriched, _ = enrich_briefing(briefing, llm, model="m", budget=run_state.budget)
        assert enriched.findings == "one row left"
        assert len(llm.calls) == 2

    def test_two_malformed_replies_raise_rather_than_return_a_half_briefing(
        self, run_state: RunState, now: datetime
    ) -> None:
        llm = _ScriptedLLM(
            [{"findings": "", "recommendation": "x"}, {"findings": "", "recommendation": "y"}]
        )
        briefing = _briefing(run_state)
        with pytest.raises(OutputRepairExhausted):
            enrich_briefing(briefing, llm, model="m", budget=run_state.budget)
        assert len(llm.calls) == 2


class TestTheVerificationJudge:
    """WO-R2-174: the same wrapper and cap on the judge that says "did it work?".

    A schema-rejected reply answers nothing, and the run escalated with a Tier-1 done.
    """

    def test_a_malformed_first_judgment_does_not_end_the_run(
        self,
    ) -> None:
        llm = _ScriptedLLM([_BAD_JUDGMENT, _GOOD_JUDGMENT])
        transition = make_llm_verify(_SequencedMCP([_lag_result(0)]), llm, model="m")

        result = transition(_verifying(), datetime(2026, 7, 15, 20, 0, tzinfo=UTC))

        assert result.state is IncidentState.RESOLVED
        assert len(llm.calls) == 2
        # The re-ask carries the original context plus the complaint.
        assert llm.calls[1][1].startswith(llm.calls[0][1])
        judged = [e for e in result.evidence if e.tool_name == "_verify_judge"]
        assert len(judged) == 1
        assert judged[0].result_summary.startswith("verified")

    def test_both_calls_are_billed_on_the_repaired_path(self) -> None:
        """A repaired judgment is two billed calls; charging one is ADR 0015's under-report."""
        llm = _ScriptedLLM(
            [
                _output_error(usage=LLMUsage(input_tokens=1000, output_tokens=100)),
                _GOOD_JUDGMENT,
            ],
            usage=LLMUsage(input_tokens=2000, output_tokens=200),
        )
        transition = make_llm_verify(
            _SequencedMCP([_lag_result(0)]), llm, model="claude-sonnet-4-6"
        )

        result = transition(_verifying(), datetime(2026, 7, 15, 20, 0, tzinfo=UTC))

        assert result.state is IncidentState.RESOLVED
        assert result.budget.tokens_used == 1000 + 100 + 2000 + 200
        assert result.budget.usd_used > 0

    def test_two_malformed_judgments_escalate_with_both_errors(self) -> None:
        """Never a silent verdict: the run ends on the reason it always did."""
        llm = _ScriptedLLM(
            [
                _output_error("first complaint", usage=LLMUsage(input_tokens=1000)),
                _output_error("second complaint", usage=LLMUsage(input_tokens=2000)),
            ]
        )
        transition = make_llm_verify(
            _SequencedMCP([_lag_result(0)]), llm, model="claude-sonnet-4-6"
        )

        result = transition(_verifying(), datetime(2026, 7, 15, 20, 0, tzinfo=UTC))

        assert result.state is IncidentState.ESCALATED
        marker = [e for e in result.evidence if e.tool_name == "_remediation_escalate"]
        assert len(marker) == 1
        reason = marker[0].result_summary
        assert reason.startswith(VERIFY_JUDGE_INVALID)
        assert "first complaint" in reason
        assert "second complaint" in reason
        # No judgment was reached, so no judge evidence may claim one.
        assert not [e for e in result.evidence if e.tool_name == "_verify_judge"]
        # Cap respected, and both billed calls charged.
        assert len(llm.calls) == MAX_OUTPUT_REPAIRS + 1 == 2
        assert result.budget.tokens_used == 3000

    def test_a_transport_failure_is_still_not_re_asked(self) -> None:
        """Scope is unchanged: a 429 goes straight out, as it does everywhere else."""
        llm = _ScriptedLLM([LLMError("429 rate limited")])
        transition = make_llm_verify(_SequencedMCP([_lag_result(0)]), llm, model="m")

        result = transition(_verifying(), datetime(2026, 7, 15, 20, 0, tzinfo=UTC))

        assert result.state is IncidentState.ESCALATED
        assert len(llm.calls) == 1


class TestTheEvalBriefingJudge:
    """WO-R2-174: the grader's judge repairs once, and never invents a score."""

    def test_a_malformed_first_score_is_repaired(self, run_state: RunState) -> None:
        llm = _ScriptedLLM([_BAD_SCORE, _GOOD_SCORE])

        score = judge_briefing(_briefing(run_state), llm, model="m")

        assert score.groundedness == 0.9
        assert score.is_useful
        assert len(llm.calls) == 2
        # The re-ask carries the original briefing plus the complaint.
        assert llm.calls[1][1].startswith(llm.calls[0][1])

    def test_the_cap_is_one_here_too(self, run_state: RunState) -> None:
        llm = _ScriptedLLM([_BAD_SCORE, _BAD_SCORE])

        with pytest.raises(OutputRepairExhausted):
            judge_briefing(_briefing(run_state), llm, model="m")

        assert len(llm.calls) == MAX_OUTPUT_REPAIRS + 1 == 2

    def test_a_twice_malformed_score_is_a_harness_event_not_a_number(
        self, run_state: RunState
    ) -> None:
        """The column stays EMPTY and says why.

        ``OutputRepairExhausted`` is an ``LLMError``, which the runner catches to leave
        ``judge_score`` ``None``.
        """
        llm = _ScriptedLLM([_BAD_SCORE, _BAD_SCORE])
        judge_score: JudgeScore | None = None
        judge_error: str | None = None

        # The runner's own arms, quoted.
        try:
            judge_score = judge_briefing(_briefing(run_state), llm, model="m")
        except (LLMError, ValidationError) as err:
            judge_error = f"judge call failed: {err}"

        assert judge_score is None
        assert judge_error is not None
        assert "repair 1 of 1 also failed validation" in judge_error

    def test_a_clean_score_still_costs_exactly_one_call(self, run_state: RunState) -> None:
        """The canned path must not consume an extra payload (ADR 0035 § Neutral)."""
        llm = _ScriptedLLM([_GOOD_SCORE])

        score = judge_briefing(_briefing(run_state), llm, model="m")

        assert score.overall == pytest.approx(0.85)
        assert len(llm.calls) == 1

    def test_the_canned_client_exhausting_its_script_is_not_repairable(
        self, run_state: RunState
    ) -> None:
        """``CannedLLMClient`` raises a plain ``LLMError``; a clean canned run never repairs."""
        canned = CannedLLMClient([])

        with pytest.raises(LLMError) as caught:
            judge_briefing(_briefing(run_state), canned, model="m")

        assert not isinstance(caught.value, OutputRepairExhausted)
        assert len(canned.calls) == 1


class TestTheFailureClass:
    """``report.json`` said ``unclassified`` for the live run. It should not."""

    @staticmethod
    def _failing_report() -> GradeReport:
        return GradeReport(
            scenario="remediate_dlq_backlog_success",
            passed=False,
            dimensions=(
                DimensionResult(dimension=GradeDimension.OUTCOME, passed=False, detail="escalated"),
            ),
        )

    def _final(self, run_state: RunState, now: datetime, reason: str) -> RunState:
        return run_state.model_copy(
            update={
                "state": IncidentState.ESCALATED,
                "evidence": (
                    EvidenceEntry(
                        tool_name="_planner_escalate",
                        arguments={},
                        result_summary=reason,
                        timestamp=now,
                    ),
                ),
            }
        )

    @pytest.mark.parametrize("prefix", OUTPUT_INVALID_PREFIXES)
    def test_each_reason_prefix_classifies_as_a_parse_failure(
        self, run_state: RunState, now: datetime, prefix: str
    ) -> None:
        final = self._final(run_state, now, f"{prefix}: 1 validation error for InvestigationStep")
        assert _classify_failure(self._failing_report(), final)[0] == PLANNER_OUTPUT_INVALID_CLASS

    def test_it_is_not_reported_as_a_transport_failure(
        self, run_state: RunState, now: datetime
    ) -> None:
        """Two of the three prefixes contain both "LLM" and "invalid".

        Without a specific bucket it is filed as network.
        """
        final = self._final(run_state, now, "planner LLM invalid: output failed schema validation")
        assert _classify_failure(self._failing_report(), final)[0] != "transport"

    def test_a_real_transport_failure_still_classifies_as_transport(
        self, run_state: RunState, now: datetime
    ) -> None:
        final = self._final(run_state, now, "MCP error -32000 while calling list_dlq_messages")
        assert _classify_failure(self._failing_report(), final)[0] == "transport"


class TestTheLiveRunEndToEnd:
    """The 779b19a287a7 payload, driven through the real transition."""

    def test_the_live_payload_no_longer_costs_a_repair_call(
        self, run_state: RunState, now: datetime
    ) -> None:
        """The decoder handles it, so the repair budget is never touched.

        The seen shape is free (ADR 0035).
        """
        from tests.unit.test_structured_output import LIVE_RECORD_OUTPUT_INPUT

        llm = _ScriptedLLM([json.loads(json.dumps(LIVE_RECORD_OUTPUT_INPUT))])
        transition = make_llm_investigate(_NoMCP(), llm, model="m")
        # The whole-queue reading that run had already taken (ADR 0041): the payload's own text
        # names rows only an unfiltered listing shows, so a state without it is a different run.
        result = transition(_investigating(run_state, whole_queue_read=now), now)
        assert len(llm.calls) == 1
        assert llm.repair_of == [None]
        # `remediate` on a POISON_MESSAGE top hypothesis hands off to PLANNING.
        assert result.state is IncidentState.PLANNING
        markers = [e.result_summary for e in result.evidence]
        assert not any(m.startswith(INVESTIGATION_PLANNER_INVALID) for m in markers)


class TestBriefingContentStillRejectsEmptyStrings:
    """The schema rule the repair path must not have loosened."""

    def test_an_empty_findings_string_is_still_invalid(self) -> None:
        with pytest.raises(ValidationError):
            BriefingContent.model_validate({"findings": "", "recommendation": "x"})


class TestARefusedMoveIsNotRepaired:
    """ADR 0074: one failure is never re-asked — the move the schema did not offer.

    ADR 0035's re-ask exists for output nobody can read, and its turn says so ("your output
    was invalid"). A ``probe`` under a schema whose ``next_action`` offers ``remediate`` and
    ``stop`` is not that: it was perfectly readable and the move was withdrawn. Two reasons to
    refuse instead of re-asking, and both are practical — a second billed call to be told the
    same thing, and, against a scripted planner, a correction served from the NEXT step's answer.
    """

    _PROBE: Final[dict[str, Any]] = {
        "hypotheses": [{"category": "unknown", "name": "n", "confidence": 0.5, "reasoning": "r"}],
        "next_action": {"kind": "probe", "tool_name": "get_consumer_lag", "arguments": {}},
    }

    def test_a_withdrawn_move_raises_without_a_second_call(self) -> None:
        llm = _ScriptedLLM([self._PROBE, _GOOD_STEP])

        with pytest.raises(OutputNotOffered):
            call_with_output_repair(
                llm,
                system_prompt="s",
                user_message="u",
                output_model=without_probe(InvestigationStep),
                model="m",
            )

        assert len(llm.calls) == 1, "the withdrawn move was re-asked instead of refused"

    def test_the_refusal_carries_what_the_call_billed(self) -> None:
        """A refused call is a billed call (ADR 0015): the ledger charges what it reports.

        This is the LIVE shape — ``LLMClient`` raises ``LLMOutputError`` with the usage and the
        ``ValidationError`` as its cause (ADR 0007) — so it also pins that the refusal is read
        through the cause rather than off the message text.
        """
        narrowed = without_probe(InvestigationStep)
        try:
            narrowed.model_validate(self._PROBE)
        except ValidationError as cause:
            failure = _output_error(
                usage=LLMUsage(input_tokens=11, output_tokens=3), record_id="r1"
            )
            failure.__cause__ = cause
        llm = _ScriptedLLM([failure])

        with pytest.raises(OutputNotOffered) as raised:
            call_with_output_repair(
                llm,
                system_prompt="s",
                user_message="u",
                output_model=narrowed,
                model="m",
            )

        assert len(llm.calls) == 1
        assert raised.value.usage == LLMUsage(input_tokens=11, output_tokens=3)
        assert raised.value.record_id == "r1"

    def test_a_malformed_payload_still_gets_its_one_re_ask(self) -> None:
        """The narrowing changes nothing about the failure ADR 0035 is about."""
        llm = _ScriptedLLM([_BAD_STEP, {**_GOOD_STEP}])

        call = call_with_output_repair(
            llm,
            system_prompt="s",
            user_message="u",
            output_model=without_probe(InvestigationStep),
            model="m",
        )

        assert len(llm.calls) == 2
        assert call.was_repaired
        assert load_prompt("output_repair").split("{error}")[0].strip() in llm.calls[1][1]

    def test_the_whole_schema_re_asks_a_probe_like_anything_else(self) -> None:
        """Nothing changed for a call the loop did not narrow: a probe is a valid answer."""
        llm = _ScriptedLLM([self._PROBE])

        call = call_with_output_repair(
            llm,
            system_prompt="s",
            user_message="u",
            output_model=InvestigationStep,
            model="m",
        )

        assert len(llm.calls) == 1
        assert not call.was_repaired
