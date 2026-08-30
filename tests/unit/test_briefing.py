from datetime import datetime

from incident_commander.agent.briefing import (
    EscalationBriefing,
    ProbeSummary,
    render_briefing,
)
from incident_commander.agent.state import EvidenceEntry, IncidentState, RunState
from incident_commander.tools.registry import TOOL_REGISTRY


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

    def test_alert_summary_prefers_consumer_group_spelling(self, run_state: RunState) -> None:
        # B-10: the platform's tool arg (and every consumer_group-keyed
        # scenario alert) spells it `consumer_group`; the summary read only
        # the legacy `group`, so the group silently vanished from briefings.
        run = run_state.model_copy(
            update={
                "state": IncidentState.ESCALATED,
                "alert": {
                    "source": "platform.kafka",
                    "severity": "high",
                    "consumer_group": "billing-consumer",
                },
            }
        )
        assert "group=billing-consumer" in render_briefing(run).alert_summary

    def test_alert_summary_still_accepts_legacy_group_spelling(self, run_state: RunState) -> None:
        run = run_state.model_copy(
            update={
                "state": IncidentState.ESCALATED,
                "alert": {
                    "source": "platform.kafka",
                    "severity": "high",
                    "group": "legacy-consumer",
                },
            }
        )
        assert "group=legacy-consumer" in render_briefing(run).alert_summary

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

    def test_investigation_trail_excludes_every_underscore_pseudo_tool(
        self, run_state: RunState, now: datetime
    ) -> None:
        # B-11: the filter is structural (startswith "_"), matching the
        # grader — a hand-list drifted once the Phase-6 evidence writers
        # landed and these five leaked into the trail as "probes".
        evidence = (
            _evidence(now, "_triage", "severity=high classified as investigating"),
            _evidence(now, "_planner_remediate", "planner chose remediation"),
            _evidence(now, "_planner_plan", "restart the consumer"),
            _evidence(now, "get_consumer_lag", '{"group":"billing","lag":42}'),
            _evidence(now, "_remediation_escalate", "verification failed twice"),
            _evidence(now, "_verify_judge", "resolved=false"),
            _evidence(now, "_freshness_reprobe", "lag unchanged after re-probe"),
        )
        run = run_state.model_copy(update={"state": IncidentState.ESCALATED, "evidence": evidence})
        briefing = render_briefing(run)
        assert briefing.investigation_trail == (
            ProbeSummary(tool="get_consumer_lag", summary='{"group":"billing","lag":42}'),
        )

    def test_no_registry_tool_is_hidden_by_the_underscore_filter(self) -> None:
        # Registry names mirror platform tool names, so the structural filter
        # can never swallow a real probe.
        assert [name for name in TOOL_REGISTRY if name.startswith("_")] == []

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

    def test_escalation_reason_reaches_the_human(self, run_state: RunState, now: datetime) -> None:
        # R2-38: the underscore filter is right about the *trail* and wrong
        # about the reason — it ate the one line telling the human why the
        # agent gave up.
        evidence = (
            _evidence(now, "get_consumer_lag", '{"group":"billing","lag":42}'),
            EvidenceEntry(
                tool_name="_remediation_escalate",
                arguments={"from_state": "verifying", "reason": "lag unchanged after restart"},
                result_summary="lag unchanged after restart",
                timestamp=now,
            ),
        )
        run = run_state.model_copy(update={"state": IncidentState.ESCALATED, "evidence": evidence})
        briefing = render_briefing(run)
        assert briefing.escalation_reason == "lag unchanged after restart"
        # And the trail stays a trail — the marker is still filtered out of it.
        assert briefing.investigation_trail == (
            ProbeSummary(tool="get_consumer_lag", summary='{"group":"billing","lag":42}'),
        )

    def test_escalation_reason_read_from_every_terminal_marker(
        self, run_state: RunState, now: datetime
    ) -> None:
        # One structural rule, not a name list: whichever writer escalated,
        # the terminal marker it appended is the last evidence entry.
        for marker, reason in (
            ("_remediation_escalate", "remediation tool error"),
            ("_escalate", "step budget exhausted"),
            ("_planner_escalate", "planner LLM invalid"),
            ("_planner_stop", "no Tier-1 fix for the top hypothesis"),
            ("_investigate_escalate", "tool error: -32602 invalid group"),
        ):
            run = run_state.model_copy(
                update={
                    "state": IncidentState.ESCALATED,
                    "evidence": (
                        EvidenceEntry(
                            tool_name=marker,
                            arguments={"reason": reason},
                            result_summary=reason,
                            timestamp=now,
                        ),
                    ),
                }
            )
            assert render_briefing(run).escalation_reason == reason

    def test_attempted_tier_1_action_reaches_the_human(
        self, run_state: RunState, now: datetime
    ) -> None:
        # Safety: a human who is not told the action already fired may fire
        # it again. The attempt lives only on the marker's arguments, because
        # a refused call writes no evidence entry of its own.
        evidence = (
            EvidenceEntry(
                tool_name="_remediation_escalate",
                arguments={
                    "from_state": "remediating",
                    "reason": "remediation output parse failed (restart_consumer_group)",
                    "attempted_tool": "restart_consumer_group",
                    "attempted_arguments": {"consumer_group": "billing-consumer"},
                },
                result_summary="remediation output parse failed (restart_consumer_group)",
                timestamp=now,
            ),
        )
        run = run_state.model_copy(update={"state": IncidentState.ESCALATED, "evidence": evidence})
        briefing = render_briefing(run)
        assert briefing.attempted_action is not None
        assert briefing.attempted_action.tool == "restart_consumer_group"
        assert briefing.attempted_action.arguments == {"consumer_group": "billing-consumer"}

    def test_no_attempted_action_when_nothing_was_attempted(
        self, run_state: RunState, now: datetime
    ) -> None:
        run = run_state.model_copy(
            update={
                "state": IncidentState.ESCALATED,
                "evidence": (
                    EvidenceEntry(
                        tool_name="_planner_stop",
                        arguments={"reason": "no Tier-1 fix"},
                        result_summary="planner stop: no Tier-1 fix",
                        timestamp=now,
                    ),
                ),
            }
        )
        assert render_briefing(run).attempted_action is None

    def test_mid_run_render_carries_no_escalation_reason(
        self, run_state: RunState, now: datetime
    ) -> None:
        # `_planner_remediate` reads like a reason and is not one — the run
        # is still going.
        run = run_state.model_copy(
            update={
                "state": IncidentState.PLANNING,
                "evidence": (_evidence(now, "_planner_remediate", "planner handoff to PLANNING"),),
            }
        )
        assert render_briefing(run).escalation_reason == ""

    def test_resolved_run_carries_no_escalation_reason(
        self, run_state: RunState, now: datetime
    ) -> None:
        # A resolved run's last marker is `_verify_judge`; its summary is a
        # verdict, not a reason to hand a human.
        run = run_state.model_copy(
            update={
                "state": IncidentState.RESOLVED,
                "evidence": (_evidence(now, "_verify_judge", "verified: lag is zero"),),
            }
        )
        briefing = render_briefing(run)
        assert briefing.escalation_reason == ""
        assert briefing.attempted_action is None

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
