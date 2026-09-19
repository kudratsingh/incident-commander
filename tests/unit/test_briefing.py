from datetime import datetime
from pathlib import Path

from incident_commander.agent.briefing import (
    INCIDENTS_HEADING,
    REMAINDER_HEADING,
    EscalationBriefing,
    ProbeSummary,
    render_briefing,
    render_incidents,
)
from incident_commander.agent.hypothesis import Hypothesis, HypothesisCategory
from incident_commander.agent.investigation import REMEDIATE_CONFIDENCE_THRESHOLD
from incident_commander.agent.planner_context import ATTEMPT_FAILED_MARKER, PLAN_MARKER
from incident_commander.agent.state import EvidenceEntry, IncidentState, RunState
from incident_commander.tools.registry import TOOL_REGISTRY


def _evidence(now: datetime, tool: str, summary: str) -> EvidenceEntry:
    return EvidenceEntry(
        tool_name=tool,
        arguments={},
        result_summary=summary,
        timestamp=now,
    )


def _hypothesis(category: HypothesisCategory, name: str, confidence: float) -> Hypothesis:
    return Hypothesis(category=category, name=name, confidence=confidence, reasoning="fixture")


def _targeted(now: datetime, marker: str, target: str) -> EvidenceEntry:
    """A ledger marker saying an attempt in this run aimed at ``target``."""
    return EvidenceEntry(
        tool_name=marker,
        arguments={"target_hypothesis": target},
        result_summary=f"plan targeting {target}",
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
        # B-10: the platform spells it `consumer_group`; the summary read only `group`.
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
        # B-11: the filter is structural (startswith "_"), matching the grader.
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

    def test_the_trail_carries_each_probes_arguments(
        self, run_state: RunState, now: datetime
    ) -> None:
        # INC-002: `list_dlq_messages` is the whole queue OR one slice under one name, so a
        # result recorded without its arguments cannot be read correctly downstream.
        evidence = (
            EvidenceEntry(
                tool_name="list_dlq_messages",
                arguments={"remediation_hint": "replay_safe", "limit": 50},
                result_summary='{"total":0,"items":[]}',
                timestamp=now,
            ),
        )
        run = run_state.model_copy(update={"state": IncidentState.RESOLVED, "evidence": evidence})
        assert render_briefing(run).investigation_trail == (
            ProbeSummary(
                tool="list_dlq_messages",
                summary='{"total":0,"items":[]}',
                arguments={"remediation_hint": "replay_safe", "limit": 50},
            ),
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
        # R2-38: the filter is right about the trail, wrong about the reason.
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

    def test_a_retried_run_tells_the_human_about_the_first_attempt(
        self, run_state: RunState, now: datetime
    ) -> None:
        # ADR 0056: an escalation can be two attempts deep, so the first write would go untold.
        evidence = (
            EvidenceEntry(
                tool_name=ATTEMPT_FAILED_MARKER,
                arguments={"attempt": 1, "of": 2, "action_tool": "invalidate_cache_key"},
                result_summary="attempt 1 of 2: invalidate_cache_key(...) — verdict not_verified.",
                timestamp=now,
            ),
            EvidenceEntry(
                tool_name="_investigate_escalate",
                arguments={"reason": "nothing else actionable"},
                result_summary="nothing else actionable",
                timestamp=now,
            ),
        )
        run = run_state.model_copy(update={"state": IncidentState.ESCALATED, "evidence": evidence})
        reason = render_briefing(run).escalation_reason
        assert reason.startswith("nothing else actionable")
        assert "invalidate_cache_key" in reason
        # And the trail stays a trail: the attempt record is a marker, not a probe.
        assert render_briefing(run).investigation_trail == ()

    def test_a_run_with_no_failed_attempt_carries_only_its_reason(
        self, run_state: RunState, now: datetime
    ) -> None:
        run = run_state.model_copy(
            update={
                "state": IncidentState.ESCALATED,
                "evidence": (
                    EvidenceEntry(
                        tool_name="_investigate_escalate",
                        arguments={"reason": "budget exhausted"},
                        result_summary="budget exhausted",
                        timestamp=now,
                    ),
                ),
            }
        )
        assert render_briefing(run).escalation_reason == "budget exhausted"

    def test_attempted_tier_1_action_reaches_the_human(
        self, run_state: RunState, now: datetime
    ) -> None:
        # Safety: a human not told the action fired may fire it again.
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


class TestTheBriefingCarriesItsIncidentsBySlot:
    """WP-11.3 (ADR 0065): primary / secondary / unresolved-extra, structurally.

    The rule being made structural is WO-R2-164's — "stabilize what one action can, then
    escalate naming every remainder". As prose it is advice the writer may ignore; as a slot
    filled from the run's own ranking and its own attempts it is in the handoff either way.
    """

    _SATURATION = HypothesisCategory.CONSUMER_SATURATION
    _DEPLOY = HypothesisCategory.DEPLOY_REGRESSION

    def _dual_fault(
        self, run_state: RunState, now: datetime, *, targets: tuple[str, ...]
    ) -> RunState:
        """The shipped dual-fault shape: two causes at the bar, some of them acted on."""
        return run_state.model_copy(
            update={
                "state": IncidentState.ESCALATED,
                "hypotheses": (
                    _hypothesis(self._SATURATION, "dispatcher_saturation", 0.85),
                    _hypothesis(self._DEPLOY, "billing_release_regression", 0.75),
                ),
                "evidence": tuple(_targeted(now, PLAN_MARKER, target) for target in targets),
            }
        )

    def test_the_second_asserted_cause_is_a_secondary_and_a_remainder(
        self, run_state: RunState, now: datetime
    ) -> None:
        slots = render_briefing(
            self._dual_fault(run_state, now, targets=("dispatcher_saturation",))
        ).incidents
        assert slots.primary is not None
        assert slots.primary.name == "dispatcher_saturation"
        assert slots.primary.addressed is True
        assert [slot.name for slot in slots.secondary] == ["billing_release_regression"]
        assert [slot.name for slot in slots.unresolved_extra] == ["billing_release_regression"]

    def test_the_remainder_comes_from_run_state_and_not_from_prose(
        self, run_state: RunState, now: datetime
    ) -> None:
        # The whole point of the slot. `findings` and `recommendation` are the writer's and are
        # empty in the deterministic template, so a run that leaves a cause standing cannot
        # render an empty remainder by writing nothing about it.
        briefing = render_briefing(
            self._dual_fault(run_state, now, targets=("dispatcher_saturation",))
        )
        assert briefing.findings == ""
        assert briefing.recommendation == ""
        assert briefing.incidents.unresolved_extra != ()
        assert REMAINDER_HEADING in "\n".join(render_incidents(briefing.incidents))

    def test_a_cause_the_run_acted_on_is_not_a_remainder(
        self, run_state: RunState, now: datetime
    ) -> None:
        # The other direction: a run that fixed BOTH faults must be able to say so, or the
        # sibling template could never resolve (ADR 0059).
        slots = render_briefing(
            self._dual_fault(
                run_state,
                now,
                targets=("dispatcher_saturation", "billing_release_regression"),
            )
        ).incidents
        assert slots.unresolved_extra == ()
        assert slots.addressed_any is True

    def test_a_cause_is_addressed_by_its_category_spelling_too(
        self, run_state: RunState, now: datetime
    ) -> None:
        # `RemediationPlan.target_hypothesis` is a free string and the corpus spells it both
        # ways; reading one spelling would call a remediated cause unaddressed (ADR 0059).
        slots = render_briefing(
            self._dual_fault(run_state, now, targets=("consumer_saturation",))
        ).incidents
        assert slots.primary is not None
        assert slots.primary.addressed is True

    def test_a_failed_attempt_also_counts_as_addressed(
        self, run_state: RunState, now: datetime
    ) -> None:
        # ADR 0056's attempt record: a cause aimed at and missed is a cause the human is
        # already told about under "already attempted", not a silent remainder.
        slots = render_briefing(
            run_state.model_copy(
                update={
                    "state": IncidentState.ESCALATED,
                    "hypotheses": (_hypothesis(self._SATURATION, "dispatcher_saturation", 0.85),),
                    "evidence": (_targeted(now, ATTEMPT_FAILED_MARKER, "dispatcher_saturation"),),
                }
            )
        ).incidents
        assert slots.unresolved_extra == ()

    def test_a_hedge_below_the_bar_is_neither_secondary_nor_remainder(
        self, run_state: RunState, now: datetime
    ) -> None:
        # The anti-hedging half of ADR 0059, in the slots: a cause under the bar is one the
        # agent is considering, and a remainder block padded with them tells a human nothing.
        slots = render_briefing(
            run_state.model_copy(
                update={
                    "state": IncidentState.ESCALATED,
                    "hypotheses": (
                        _hypothesis(self._SATURATION, "dispatcher_saturation", 0.85),
                        _hypothesis(
                            self._DEPLOY,
                            "a-hedge",
                            REMEDIATE_CONFIDENCE_THRESHOLD - 0.01,
                        ),
                    ),
                    "evidence": (_targeted(now, PLAN_MARKER, "dispatcher_saturation"),),
                }
            )
        ).incidents
        assert slots.secondary == ()
        assert slots.unresolved_extra == ()

    def test_a_cause_exactly_at_the_bar_is_asserted(
        self, run_state: RunState, now: datetime
    ) -> None:
        slots = render_briefing(
            run_state.model_copy(
                update={
                    "state": IncidentState.ESCALATED,
                    "hypotheses": (
                        _hypothesis(self._SATURATION, "dispatcher_saturation", 0.85),
                        _hypothesis(self._DEPLOY, "at-the-bar", REMEDIATE_CONFIDENCE_THRESHOLD),
                    ),
                }
            )
        ).incidents
        assert [slot.name for slot in slots.secondary] == ["at-the-bar"]

    def test_the_top_cause_is_the_primary_whatever_its_confidence(
        self, run_state: RunState, now: datetime
    ) -> None:
        # Same reading as `final_diagnosis`: an escalating run's best guess at 0.55 is still
        # what it concluded, and a human handed it needs to be told so.
        slots = render_briefing(
            run_state.model_copy(
                update={
                    "state": IncidentState.ESCALATED,
                    "hypotheses": (_hypothesis(self._SATURATION, "a-weak-guess", 0.55),),
                }
            )
        ).incidents
        assert slots.primary is not None
        assert slots.primary.name == "a-weak-guess"
        assert [slot.name for slot in slots.unresolved_extra] == ["a-weak-guess"]
        assert slots.addressed_any is False

    def test_a_run_with_no_ranking_has_no_slots_and_renders_nothing(
        self, run_state: RunState
    ) -> None:
        # Every context that predates WP-11.3 renders byte-identically, which is what keeps
        # the prompt snapshots and the canned suite where they were.
        slots = render_briefing(run_state).incidents
        assert slots.primary is None
        assert slots.asserted == ()
        assert render_incidents(slots) == []

    def test_the_block_names_the_heading_and_every_remaining_cause(
        self, run_state: RunState, now: datetime
    ) -> None:
        rendered = "\n".join(
            render_incidents(
                render_briefing(
                    self._dual_fault(run_state, now, targets=("dispatcher_saturation",))
                ).incidents
            )
        )
        assert INCIDENTS_HEADING in rendered
        assert REMAINDER_HEADING in rendered
        assert "billing_release_regression" in rendered
        assert "deploy_regression" in rendered
        assert "confidence 0.75" in rendered

    def test_the_slots_survive_the_json_round_trip(
        self, run_state: RunState, now: datetime
    ) -> None:
        # The briefing is archived as JSON (invariant 9) and read back by the regrade path.
        briefing = render_briefing(
            self._dual_fault(run_state, now, targets=("dispatcher_saturation",))
        )
        loaded = EscalationBriefing.model_validate_json(briefing.model_dump_json())
        assert loaded.incidents == briefing.incidents

    def test_an_archive_written_before_the_slots_reads_back_as_empty_ones(self) -> None:
        # Invariant 9: archives are append-only, so every briefing written before WP-11.3 has
        # no `incidents` key. Empty slots are the honest reading — the projection did not exist
        # when that run happened — and a required field would make old evidence unreadable.
        older = {
            "incident_id": "11111111-1111-1111-1111-111111111111",
            "final_state": "escalated",
            "alert_summary": "source=platform.kafka severity=high",
        }
        loaded = EscalationBriefing.model_validate(older)
        assert loaded.incidents.primary is None
        assert loaded.incidents.unresolved_extra == ()


class TestTheDecisionIsRecorded:
    """ADR 0065 exists, is accepted, and is in the index — the repo's own convention."""

    def test_the_adr_exists_and_is_indexed(self) -> None:
        adr = Path(__file__).resolve().parents[2] / "docs" / "ADR"
        matches = sorted(adr.glob("0065-*.md"))
        assert len(matches) == 1
        assert "accepted" in matches[0].read_text(encoding="utf-8").lower()
        index = (adr / "README.md").read_text(encoding="utf-8")
        assert matches[0].name.removesuffix(".md").split("-", 1)[1] in index
