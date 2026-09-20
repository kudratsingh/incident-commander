"""The corpus's root-cause decisions, pinned — including the deliberate abstentions.

``Scenario.ground_truth`` is optional, which is right for the model and wrong for the
corpus, so WO-R3-261 wrote a decision for all 41. ``_DECIDED`` records what each label
IS (hand-maintained on purpose), and "no label" is admissible only for a scenario that
cannot be graded on diagnosis — derived from the scenario, not from a second hand-list.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Final

from evals.scenarios.loader import load_scenarios
from evals.scenarios.schema import Scenario, ScenarioFamily
from incident_commander.agent.hypothesis import HypothesisCategory as Category

_SCENARIOS_DIR: Final[Path] = Path(__file__).resolve().parents[2] / "evals" / "scenarios"

#: Scenario name → the labels its world declares, or ``None`` for a recorded decision
#: NOT to grade it. The reasoning sits in the YAML itself.
_DECIDED: Final[Mapping[str, tuple[Category, ...] | None]] = {
    "alert_storm": (Category.DEPLOY_REGRESSION,),
    "consumer_lag_analytics_critical": (Category.CONSUMER_SATURATION,),
    "consumer_lag_healthy_zero": (Category.NO_FAULT,),
    "consumer_lag_high": (Category.CONSUMER_SATURATION,),
    "consumer_lag_medium": (Category.CONSUMER_SATURATION,),
    "consumer_lag_missing_group": (Category.UNKNOWN,),
    "consumer_lag_null_unknown_state": (Category.UNKNOWN,),
    "consumer_lag_orders_high": (Category.CONSUMER_SATURATION,),
    "consumer_lag_payments_critical": (Category.CONSUMER_SATURATION,),
    "consumer_lag_shipping_extreme": (Category.CONSUMER_SATURATION,),
    "deploy_correlation": (Category.DEPLOY_REGRESSION,),
    "dlq_backlog": (Category.POISON_MESSAGE,),
    "dlq_human_required_escalates": (Category.POISON_MESSAGE,),
    "dlq_mislabeled_replay_safe": (Category.POISON_MESSAGE,),
    "dlq_mixed_partial": (Category.POISON_MESSAGE,),
    "dlq_poison_unclassified": (Category.POISON_MESSAGE,),
    "dlq_replay_safe_success": (Category.POISON_MESSAGE,),
    "dlq_wait_and_replay_success": (Category.POISON_MESSAGE,),
    "failed_traces_scan": (Category.UNKNOWN,),
    "incidents_overview": (Category.UNKNOWN,),
    # WO-R3-202 (WP-4.3), Family B: one symptom, four worlds, three answers. The pair of
    # `outbox_stall` rows is the measurement: same world, different alert.
    "jobs_not_progressing_dispatcher_stall": (Category.CONSUMER_SATURATION,),
    "jobs_not_progressing_healthy_backlog_spike": (Category.NO_FAULT,),
    "jobs_not_progressing_outbox_stall": (Category.OUTBOX_STALL,),
    "jobs_not_progressing_outbox_stall_deploy_noise": (Category.OUTBOX_STALL,),
    "multi_probe_billing": (Category.CONSUMER_SATURATION,),
    "multi_probe_hypothesis_evolution": (Category.CONSUMER_SATURATION,),
    "no_fault_healthy_cache": (Category.NO_FAULT,),
    "noise_info_orders": None,
    "noise_info_severity": None,
    "noise_low_analytics": None,
    "noise_low_severity": None,
    "noise_missing_severity": None,
    "planner_stops_immediately": None,
    "postgres_slow": (Category.DB_QUERY_LATENCY,),
    "redis_saturation": (Category.REDIS_SATURATION,),
    "remediate_consumer_lag_success": (Category.CONSUMER_SATURATION,),
    "remediate_dlq_backlog_success": (Category.POISON_MESSAGE,),
    "remediate_runaway_saga_success": (Category.RUNAWAY_SAGA,),
    "remediate_stale_cache_success": (Category.STALE_CACHE,),
    "remediate_verify_fails": (Category.CONSUMER_SATURATION,),
    "saga_stuck": (Category.RUNAWAY_SAGA,),
    "tool_missing_response": None,
    "tool_output_schema_mismatch": None,
    "tool_result_marked_error": None,
    # Moved from UNKNOWN by WO-R3-263 (O-19, ADR 0054): the world is a `report_gen` worker
    # that ran out of memory, and the taxonomy now has a member for that.
    "trace_investigation": (Category.RESOURCE_EXHAUSTION,),
    # WO-R3-214 (WP-7.2, ADR 0053). Family C: one chain under four faults, and `resolver_stall`
    # vs `dag_paused` — one boolean apart in `get_dag_state` — is separated by the labels alone.
    "workflow_stuck_dead_lettered_root": (Category.RUNAWAY_SAGA,),
    # WO-R3-284 (ADR 0070). The family's fifth world, and `poison_message` rather than
    # `runaway_saga`: the root ran and finished, so nothing in the chain stopped stepping —
    # one job's stored payload cannot be processed, which is what the label names. It is
    # also the family's fifth distinct answer, which its one-alert property requires.
    "workflow_stuck_downstream_child_failed": (Category.POISON_MESSAGE,),
    "workflow_stuck_healthy_chain": (Category.NO_FAULT,),
    "workflow_stuck_paused_dag": (Category.DAG_PAUSED,),
    "workflow_stuck_resolver_stall": (Category.RESOLVER_STALL,),
    # WO-R3-226 (WP-10.1, ADR 0056). All four retry scenarios are diagnosable and carry
    # a label read off their fixtures; in `retry_identical_refused` the diagnosis is right.
    "retry_cap_escalates": (Category.CONSUMER_SATURATION,),
    "retry_identical_refused": (Category.STALE_CACHE,),
    "retry_second_hypothesis_succeeds": (Category.CONSUMER_SATURATION,),
    "stabilizer_then_reinvestigate": (Category.POISON_MESSAGE,),
    # WO-R3-228 (WP-11.1, ADR 0059). The first two labels with TWO causes in them, and the
    # first place the set arithmetic grades a set. Each label is read off the world's two
    # hooks and the fixtures they produce: a killed group's climbing lag beside, in one
    # world, a dead-lettered row whose hint and error agree, and in the other a prod
    # release whose own deploy marker is annotated as correlated with the failures. Order
    # is irrelevant to the grade (the score is set-shaped) and alphabetical here.
    "dual_fault_consumer_lag_and_bad_deploy": (
        Category.CONSUMER_SATURATION,
        Category.DEPLOY_REGRESSION,
    ),
    "dual_fault_dlq_and_consumer_lag": (
        Category.CONSUMER_SATURATION,
        Category.POISON_MESSAGE,
    ),
    # WO-R3-236 (WP-14.1, ADR 0062). Both temporal templates seed one stale hot key, so
    # the diagnosis is the same in each; that the entry then expires on its own is a fact
    # about the TIMELINE, graded on ATTRIBUTION, and not a second root cause.
    "temporal_ttl_recovers_before_action": (Category.STALE_CACHE,),
    "temporal_ttl_recovers_during_verify": (Category.STALE_CACHE,),
    # WO-R3-221 (WP-8.5, ADR 0066). Family A: one page, four worlds, four labels, and
    # every label read off the DEPENDENCY reading that separates its world rather than off
    # the page the four share — which is the whole reason this family exists. All four
    # already existed in the enum (WO-R3-188 wrote them for this plan section), so this is
    # the first family to add none.
    "api_latency_db_query": (Category.DB_QUERY_LATENCY,),
    "api_latency_downstream": (Category.DOWNSTREAM_DEPENDENCY,),
    "api_latency_redis": (Category.REDIS_SATURATION,),
    # The level-0 control. `no_fault` is an ANSWER rather than a fault, and it is the one
    # label in this family whose world is the seeded baseline with no hook at all.
    "api_latency_healthy_control": (Category.NO_FAULT,),
    # WO-R3-229 (WP-11.2, ADR 0067). ONE label for a four-link cascade: the hook degrades
    # Redis and everything else in the world follows from it, so the absent lag reading,
    # the open admission throttle and the burning dispatch objective are consequences
    # rather than causes. The chain itself is recorded in `ground_truth.causal_chain`,
    # which nothing grades; what is graded is that the run names the ROOT and nothing
    # beside it.
    "cascading_redis_starves_backpressure": (Category.REDIS_SATURATION,),
}

#: Families that measure the harness rather than a world: ``TOOL_FAULT``
#: breaks the probe before a reading exists, and ``HARNESS_CONTROL`` has none.
_UNDIAGNOSABLE_FAMILIES: Final[frozenset[ScenarioFamily]] = frozenset(
    {ScenarioFamily.HARNESS_CONTROL, ScenarioFamily.TOOL_FAULT}
)


def may_abstain(scenario: Scenario) -> bool:
    """Whether "no ground truth" is an admissible decision for this scenario.

    Two derived shapes: a harness or tool-failure family, or a zero tool-call budget.
    """
    if scenario.family in _UNDIAGNOSABLE_FAMILIES:
        return True
    return scenario.expectation.max_tool_calls == 0


def undecided(scenarios: Iterable[Scenario]) -> list[str]:
    """Scenarios the record says nothing about. A new scenario lands here."""
    return sorted(s.name for s in scenarios if s.name not in _DECIDED)


def stale_records(scenarios: Iterable[Scenario]) -> list[str]:
    """Record entries naming a scenario the corpus no longer holds."""
    return sorted(set(_DECIDED) - {s.name for s in scenarios})


def disagreements(scenarios: Iterable[Scenario]) -> list[str]:
    """Scenarios whose declared label is not the one the record pins."""
    problems: list[str] = []
    for scenario in scenarios:
        if scenario.name not in _DECIDED:
            continue  # reported by ``undecided``; not two failures for one cause
        recorded = _DECIDED[scenario.name]
        declared = None if scenario.ground_truth is None else scenario.ground_truth.root_causes
        if declared != recorded:
            problems.append(
                f"{scenario.name}: YAML declares "
                f"{None if declared is None else [c.value for c in declared]}, the "
                f"record pins {None if recorded is None else [c.value for c in recorded]}"
            )
    return problems


def unjustified_abstentions(scenarios: Iterable[Scenario]) -> list[str]:
    """Scenarios that skip the label without being un-gradeable on diagnosis."""
    return sorted(s.name for s in scenarios if s.ground_truth is None and not may_abstain(s))


def _corpus() -> list[Scenario]:
    return load_scenarios(_SCENARIOS_DIR)


class TestEveryScenarioCarriesADecision:
    def test_the_corpus_is_not_empty(self) -> None:
        """Anti-vacuity: every check below is a sweep, and a sweep over nothing passes."""
        assert len(_corpus()) >= 41

    def test_no_scenario_is_without_a_recorded_decision(self) -> None:
        missing = undecided(_corpus())
        assert not missing, (
            f"{missing} carry no entry in this file's record. A new scenario needs a "
            "root-cause decision before it ships: derive the label from the world it "
            "manufactures (its chaos hook and its canned fixtures, never its canned "
            "planner), add the ground_truth block to the YAML with the evidence in a "
            "comment above it, and add the entry here. Deciding NOT to grade it is a "
            "valid decision and is recorded as None."
        )

    def test_the_record_names_no_scenario_that_has_left(self) -> None:
        stale = stale_records(_corpus())
        assert not stale, (
            f"{stale} are recorded here but are not in the corpus — a renamed or "
            "deleted scenario left a stale entry behind."
        )

    def test_every_declared_label_is_the_one_on_record(self) -> None:
        problems = disagreements(_corpus())
        assert not problems, (
            "a scenario's ground truth and this record disagree:\n  "
            + "\n  ".join(problems)
            + "\nA label is a reviewed judgement about a world. Moving one means "
            "moving both, in the same PR, with the evidence in the YAML comment."
        )

    def test_no_scenario_abstains_without_a_reason_the_corpus_can_show(self) -> None:
        problems = unjustified_abstentions(_corpus())
        assert not problems, (
            f"{problems} declare no ground_truth, but they are neither a harness or "
            "tool-failure family nor capped at zero tool calls — so they do produce a "
            "diagnosis and it can be graded. Label them, or change what makes them "
            "un-gradeable."
        )

    def test_the_coverage_this_packet_reports(self) -> None:
        """The number in the PR body, checked against the corpus that produced it."""
        corpus = _corpus()
        graded = [s for s in corpus if s.root_cause_graded]
        assert (len(graded), len(corpus)) == (54, 63), (
            f"{len(graded)} of {len(corpus)} scenarios are root-cause graded; "
            "WO-R3-261 landed 32 of 41, WO-R3-202 took it to 36 of 45, WO-R3-214 "
            "to 40 of 49, WO-R3-226 to 44 of 53, WO-R3-228 to 46 of 55, WO-R3-236 to "
            "48 of 57, WO-R3-229 to 49 of 58, WO-R3-221 to 53 of 62 and WO-R3-284 to "
            "54 of 63 — every "
            "`jobs_not_progressing`, `temporal_recovery`, `workflow_stuck`, "
            "`api_latency`, retry, multi-fault and cascading scenario carries a label, "
            "because ADR 0038 makes one mandatory for a new scenario. Update this "
            "number and the run summary's coverage line together."
        )


class TestThePinItselfFails:
    """Red-before. Each check above is exercised on input that must break it.

    The corpus is green by construction once the packet lands, so the only way
    to watch these fire is to hand them a scenario that violates the rule. They
    are module-level functions taking scenarios for exactly that reason.
    """

    @staticmethod
    def _bare(name: str, **kwargs: object) -> Scenario:
        return Scenario.model_validate(
            {
                "name": name,
                "family": "consumer_lag",
                "difficulty": "single",
                "alert": {"source": "platform.kafka", "severity": "critical"},
                "expectation": {
                    "name": name,
                    "expected_terminal_state": "escalated",
                    "max_tool_calls": 5,
                },
                **kwargs,
            }
        )

    def test_a_new_scenario_with_no_record_is_reported(self) -> None:
        assert undecided([self._bare("brand_new_world")]) == ["brand_new_world"]

    def test_a_scenario_on_record_is_not_reported(self) -> None:
        assert undecided([self._bare("postgres_slow")]) == []

    def test_a_record_entry_with_no_scenario_is_reported(self) -> None:
        assert "postgres_slow" in stale_records([self._bare("brand_new_world")])

    def test_a_label_that_moved_without_the_record_is_reported(self) -> None:
        moved = self._bare(
            "postgres_slow",
            ground_truth={"incident_count": 1, "root_causes": ["db_pool_saturation"]},
        )
        problems = disagreements([moved])
        assert len(problems) == 1
        assert "db_pool_saturation" in problems[0]
        assert "db_query_latency" in problems[0]

    def test_a_label_that_matches_the_record_is_not_reported(self) -> None:
        kept = self._bare(
            "postgres_slow",
            ground_truth={"incident_count": 1, "root_causes": ["db_query_latency"]},
        )
        assert disagreements([kept]) == []

    def test_a_diagnosable_scenario_that_abstains_is_reported(self) -> None:
        assert unjustified_abstentions([self._bare("brand_new_world")]) == ["brand_new_world"]

    def test_a_zero_budget_scenario_may_abstain(self) -> None:
        quiet = Scenario.model_validate(
            {
                "name": "quiet",
                "family": "noise_control",
                "difficulty": "control",
                "alert": {"source": "platform.healthcheck", "severity": "info"},
                "expectation": {
                    "name": "quiet",
                    "expected_terminal_state": "escalated",
                    "max_tool_calls": 0,
                },
            }
        )
        assert may_abstain(quiet)
        assert unjustified_abstentions([quiet]) == []

    def test_a_tool_fault_scenario_may_abstain(self) -> None:
        broken = self._bare("broken_probe")
        broken = broken.model_copy(update={"family": ScenarioFamily.TOOL_FAULT})
        assert may_abstain(broken)
        assert unjustified_abstentions([broken]) == []
