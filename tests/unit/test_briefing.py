from datetime import datetime

from incident_commander.agent.briefing import (
    EscalationBriefing,
    ProbeSummary,
    render_briefing,
)
from incident_commander.agent.state import EvidenceEntry, IncidentState, RunState


def _evidence(now: datetime, tool: str, summary: str) -> EvidenceEntry:
    return EvidenceEntry(
        tool_name=tool,
        arguments={},
        result_summary=summary,
        timestamp=now,
    )


class TestRenderBriefing:
    def test_alert_summary_captures_named_fields(self, run_state: RunState, now: datetime) -> None:
        run = run_state.model_copy(
            update={
                "state": IncidentState.ESCALATED,
                "alert": {
                    "source": "platform.kafka",
                    "severity": "high",
                    "fingerprint": "consumer_lag_high",
                    "group": "billing-consumer",
                },
            }
        )
        briefing = render_briefing(run)
        assert "source=platform.kafka" in briefing.alert_summary
        assert "severity=high" in briefing.alert_summary
        assert "fingerprint=consumer_lag_high" in briefing.alert_summary
        assert "group=billing-consumer" in briefing.alert_summary

    def test_alert_summary_falls_back_to_unknown(self, run_state: RunState) -> None:
        run = run_state.model_copy(update={"state": IncidentState.ESCALATED, "alert": {}})
        briefing = render_briefing(run)
        assert "source=unknown" in briefing.alert_summary
        assert "severity=unknown" in briefing.alert_summary

    def test_investigation_trail_excludes_triage_and_escalate_markers(
        self, run_state: RunState, now: datetime
    ) -> None:
        evidence = (
            _evidence(now, "_triage", "severity=high classified as investigating"),
            _evidence(now, "get_consumer_lag", '{"group":"billing","lag":42}'),
            _evidence(now, "_escalate", "budget exhausted"),
        )
        run = run_state.model_copy(
            update={
                "state": IncidentState.ESCALATED,
                "evidence": evidence,
            }
        )
        briefing = render_briefing(run)
        assert briefing.investigation_trail == (
            ProbeSummary(
                tool="get_consumer_lag",
                summary='{"group":"billing","lag":42}',
            ),
        )

    def test_investigation_trail_empty_when_only_triage(
        self, run_state: RunState, now: datetime
    ) -> None:
        evidence = (_evidence(now, "_triage", "severity=info classified as escalated"),)
        run = run_state.model_copy(
            update={
                "state": IncidentState.ESCALATED,
                "evidence": evidence,
            }
        )
        briefing = render_briefing(run)
        assert briefing.investigation_trail == ()

    def test_findings_and_recommendation_are_empty_placeholders(
        self, run_state: RunState, now: datetime
    ) -> None:
        run = run_state.model_copy(update={"state": IncidentState.ESCALATED})
        briefing = render_briefing(run)
        # Findings and recommendation are LLM territory — the shape is here,
        # the strings are empty. Later PRs fill them via the hypothesis engine.
        assert briefing.findings == ""
        assert briefing.recommendation == ""

    def test_budget_used_reports_all_four_dimensions(
        self, run_state: RunState, now: datetime
    ) -> None:
        used = run_state.budget.model_copy(update={"tool_calls_used": 3, "tokens_used": 1500})
        run = run_state.model_copy(update={"state": IncidentState.ESCALATED, "budget": used})
        briefing = render_briefing(run)
        assert briefing.budget_used["tool_calls"] == 3
        assert briefing.budget_used["tokens"] == 1500
        assert briefing.budget_used["wall_seconds"] == 0.0
        assert briefing.budget_used["usd"] == "0"

    def test_final_state_captured(self, run_state: RunState) -> None:
        for terminal in (
            IncidentState.RESOLVED,
            IncidentState.ESCALATED,
            IncidentState.FAILED,
        ):
            run = run_state.model_copy(update={"state": terminal})
            assert render_briefing(run).final_state is terminal

    def test_incident_id_stringified(self, run_state: RunState) -> None:
        briefing = render_briefing(run_state)
        assert briefing.incident_id == str(run_state.incident_id)

    def test_round_trip_json(self, run_state: RunState) -> None:
        briefing = render_briefing(run_state)
        loaded = EscalationBriefing.model_validate_json(briefing.model_dump_json())
        assert loaded == briefing
