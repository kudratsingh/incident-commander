from datetime import datetime

import pytest
from pydantic import ValidationError

from incident_commander.agent.briefing import EscalationBriefing, render_briefing
from incident_commander.agent.briefing_enrichment import (
    BriefingContent,
    enrich_briefing,
)
from incident_commander.agent.state import EvidenceEntry, IncidentState, RunState
from incident_commander.llm.fakes import CannedLLMClient


def _evidence(now: datetime, tool: str, summary: str) -> EvidenceEntry:
    return EvidenceEntry(
        tool_name=tool,
        arguments={},
        result_summary=summary,
        timestamp=now,
    )


def _briefing_with_probe(run_state: RunState, now: datetime) -> EscalationBriefing:
    evidence = (_evidence(now, "get_consumer_lag", '{"lag":42}'),)
    run = run_state.model_copy(update={"state": IncidentState.ESCALATED, "evidence": evidence})
    return render_briefing(run)


class TestEnrichBriefing:
    def test_fills_findings_and_recommendation(self, run_state: RunState, now: datetime) -> None:
        client = CannedLLMClient(
            [
                {
                    "findings": "Consumer lag on billing crossed the paging threshold.",
                    "recommendation": "Verify the billing consumer is running.",
                }
            ]
        )
        briefing = _briefing_with_probe(run_state, now)
        enriched = enrich_briefing(briefing, client, model="claude-sonnet-4-6")
        assert "billing" in enriched.findings
        assert "consumer" in enriched.recommendation

    def test_original_briefing_preserved_when_llm_only_writes_two_fields(
        self, run_state: RunState, now: datetime
    ) -> None:
        client = CannedLLMClient([{"findings": "a finding", "recommendation": "a rec"}])
        briefing = _briefing_with_probe(run_state, now)
        enriched = enrich_briefing(briefing, client, model="m")
        assert enriched.incident_id == briefing.incident_id
        assert enriched.alert_summary == briefing.alert_summary
        assert enriched.investigation_trail == briefing.investigation_trail
        assert enriched.budget_used == briefing.budget_used

    def test_context_message_includes_trail_entries(
        self, run_state: RunState, now: datetime
    ) -> None:
        client = CannedLLMClient([{"findings": "f", "recommendation": "r"}])
        briefing = _briefing_with_probe(run_state, now)
        enrich_briefing(briefing, client, model="m")
        assert len(client.calls) == 1
        _, user_message = client.calls[0]
        assert "get_consumer_lag" in user_message
        assert '"lag":42' in user_message

    def test_empty_trail_context_flagged_to_llm(self, run_state: RunState) -> None:
        client = CannedLLMClient(
            [{"findings": "no probes ran", "recommendation": "check the raw alert"}]
        )
        briefing = render_briefing(run_state.model_copy(update={"state": IncidentState.ESCALATED}))
        enrich_briefing(briefing, client, model="m")
        _, user_message = client.calls[0]
        assert "No probes were run" in user_message

    def test_context_carries_the_escalation_reason(
        self, run_state: RunState, now: datetime
    ) -> None:
        client = CannedLLMClient([{"findings": "f", "recommendation": "r"}])
        run = run_state.model_copy(
            update={
                "state": IncidentState.ESCALATED,
                "evidence": (
                    EvidenceEntry(
                        tool_name="_remediation_escalate",
                        arguments={
                            "reason": "lag unchanged after restart",
                            "attempted_tool": "restart_consumer_group",
                            "attempted_arguments": {"consumer_group": "billing"},
                        },
                        result_summary="lag unchanged after restart",
                        timestamp=now,
                    ),
                ),
            }
        )
        enrich_briefing(render_briefing(run), client, model="m")
        _, user_message = client.calls[0]
        assert "lag unchanged after restart" in user_message
        # Safety: the writer must not recommend re-firing an action that
        # already fired, so it has to be told the action fired.
        assert "ALREADY ATTEMPTED" in user_message
        assert "restart_consumer_group" in user_message

    def test_empty_string_output_rejected_by_schema(
        self, run_state: RunState, now: datetime
    ) -> None:
        client = CannedLLMClient([{"findings": "", "recommendation": "x"}])
        briefing = _briefing_with_probe(run_state, now)
        with pytest.raises(ValidationError):
            enrich_briefing(briefing, client, model="m")


class TestBriefingContent:
    def test_forbids_extra_fields(self) -> None:
        with pytest.raises(ValidationError):
            BriefingContent.model_validate({"findings": "f", "recommendation": "r", "extra": "x"})

    def test_findings_min_length(self) -> None:
        with pytest.raises(ValidationError):
            BriefingContent(findings="", recommendation="r")

    def test_recommendation_min_length(self) -> None:
        with pytest.raises(ValidationError):
            BriefingContent(findings="f", recommendation="")


class TestServiceAndEvalPathParity:
    """R2-38: the eval graded a briefing shape production never produced.

    Enrichment stays eval-only (see the module docstring and
    ``docs/safety-model.md``). What must hold is that the *difference* is
    bounded to the two LLM-written strings, so nothing a human needs can
    quietly become eval-only again.
    """

    def test_enrichment_changes_only_findings_and_recommendation(
        self, run_state: RunState, now: datetime
    ) -> None:
        client = CannedLLMClient([{"findings": "a finding", "recommendation": "a rec"}])
        service_path = _briefing_with_probe(run_state, now)
        eval_path = enrich_briefing(service_path, client, model="m")

        assert set(service_path.model_dump()) == set(eval_path.model_dump())
        differing = {
            field
            for field in service_path.model_dump()
            if getattr(service_path, field) != getattr(eval_path, field)
        }
        assert differing == {"findings", "recommendation"}

    def test_reason_and_attempted_action_are_deterministic_not_enriched(
        self, run_state: RunState, now: datetime
    ) -> None:
        # The two facts the handoff exists to deliver must survive with no
        # LLM in the loop at all — that is what makes an eval-only writer an
        # acceptable decision rather than a hole.
        run = run_state.model_copy(
            update={
                "state": IncidentState.ESCALATED,
                "evidence": (
                    EvidenceEntry(
                        tool_name="_remediation_escalate",
                        arguments={
                            "reason": "remediation output parse failed",
                            "attempted_tool": "restart_consumer_group",
                            "attempted_arguments": {"consumer_group": "billing"},
                        },
                        result_summary="remediation output parse failed",
                        timestamp=now,
                    ),
                ),
            }
        )
        briefing = render_briefing(run)
        assert briefing.escalation_reason == "remediation output parse failed"
        assert briefing.attempted_action is not None
        assert briefing.attempted_action.tool == "restart_consumer_group"
        assert briefing.findings == ""
        assert briefing.recommendation == ""
