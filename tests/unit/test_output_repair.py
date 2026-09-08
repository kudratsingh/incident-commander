"""One bounded repair, then escalate — and both calls billed (ADR 0035).

``llm/structured.py`` removes the one output-shape defect we have actually
seen. This is the backstop for the rest: a ``record_output`` payload the
schema rejects buys exactly one re-ask carrying the validation error, and the
second failure escalates with both errors, exactly as the first failure did
before this change.

The three claims worth holding onto, and each has a test below:

* **Cap.** Two calls, never three, whatever the model does.
* **Bill.** Both calls reach the ledger. A repair that only charged the run
  when it worked would make the expensive path the cheap-looking one
  (ADR 0015).
* **Scope.** Only an OUTPUT failure is repairable. A transport failure —
  429, dropped connection, exhausted retries — goes straight out, because
  the client has already retried it and a "your JSON was malformed" turn is
  not a useful thing to say to a rate limiter.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from evals.graders.deterministic import DimensionResult, GradeDimension, GradeReport
from evals.runner import _classify_failure
from incident_commander.agent.briefing import EscalationBriefing
from incident_commander.agent.briefing_enrichment import BriefingContent, enrich_briefing
from incident_commander.agent.hypothesis import InvestigationStep
from incident_commander.agent.investigation import make_llm_investigate
from incident_commander.agent.state import EvidenceEntry, IncidentState, RunState
from incident_commander.llm.client import LLMError, LLMOutputError, LLMResult, LLMUsage
from incident_commander.llm.prompts.loader import load_prompt
from incident_commander.llm.repair import (
    INVESTIGATION_PLANNER_INVALID,
    MAX_OUTPUT_REPAIRS,
    OUTPUT_INVALID_PREFIXES,
    PLANNER_OUTPUT_INVALID_CLASS,
    OutputRepairExhausted,
    call_with_output_repair,
    repair_message,
)

_GOOD_STEP: dict[str, Any] = {
    "hypotheses": [{"category": "unknown", "name": "n", "confidence": 0.5, "reasoning": "r"}],
    "next_action": {"kind": "stop", "reason": "enough evidence"},
}
# The live shape, in miniature: the nested union arrives as a string that is
# not decodable, so the decoder in llm/structured.py declines it and the
# repair path is what has to save the run.
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


def _investigating(run_state: RunState) -> RunState:
    return run_state.model_copy(
        update={
            "state": IncidentState.INVESTIGATING,
            "alert": {"source": "kafka", "severity": "high"},
        }
    )


class _NoMCP:
    def call_tool(
        self,
        name: str,
        arguments: Any,
        *,
        timeout_seconds: float | None = None,
    ) -> Any:
        raise AssertionError(f"no tool call expected, got {name}")


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
        enriched = enrich_briefing(briefing, llm, model="m")
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
            enrich_briefing(briefing, llm, model="m")
        assert len(llm.calls) == 2


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

        The transport heuristic below them matches on exactly those two
        words, so without an earlier and more specific bucket a schema
        rejection is filed as a network problem.
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

        This is the difference between the two halves of ADR 0035: the shape
        we have seen costs nothing extra, and the repair is there for the
        shapes we have not.
        """
        from tests.unit.test_structured_output import LIVE_RECORD_OUTPUT_INPUT

        llm = _ScriptedLLM([json.loads(json.dumps(LIVE_RECORD_OUTPUT_INPUT))])
        transition = make_llm_investigate(_NoMCP(), llm, model="m")
        result = transition(_investigating(run_state), now)
        assert len(llm.calls) == 1
        assert llm.repair_of == [None]
        # `remediate` on a POISON_MESSAGE top hypothesis hands off to
        # PLANNING; before this change the same payload escalated with
        # "planner output invalid".
        assert result.state is IncidentState.PLANNING
        markers = [e.result_summary for e in result.evidence]
        assert not any(m.startswith(INVESTIGATION_PLANNER_INVALID) for m in markers)


class TestBriefingContentStillRejectsEmptyStrings:
    """The schema rule the repair path must not have loosened."""

    def test_an_empty_findings_string_is_still_invalid(self) -> None:
        with pytest.raises(ValidationError):
            BriefingContent.model_validate({"findings": "", "recommendation": "x"})
