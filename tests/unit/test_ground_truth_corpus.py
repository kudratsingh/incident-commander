"""The corpus's root-cause decisions, pinned — including the deliberate abstentions.

``Scenario.ground_truth`` is optional, which is right for the model and wrong
for the corpus: an optional field that nobody has to fill is a field a new
scenario silently skips, and root-cause coverage then drifts down while every
suite stays green. WO-R3-261 wrote a decision for all 41 scenarios, and this
file is what keeps a decision mandatory.

Two guards, and the second is the one that matters:

1. **The value pin.** ``_DECIDED`` records what each scenario's label IS —
   or that it deliberately has none. A YAML whose label is edited without the
   record moving fails here, and so does a record entry naming a scenario
   that has left the corpus. This is a hand-maintained list on purpose: the
   labels are a reviewed judgement about 45 worlds, and a test that derived
   them from the YAMLs would assert that the files equal themselves.

2. **The abstention rule.** "No label" is only admissible for a scenario that
   cannot be graded on diagnosis at all: the tool-failure tests, the harness
   control, and the noise controls that are filtered at TRIAGE with a budget
   of zero tool calls and so never produce a hypothesis ranking. Any other
   scenario must decide. Derived from the scenario rather than from a second
   hand-list, so a future author cannot abstain on a diagnosable world by
   adding a name to a set.

Why a labelled noise control would be wrong, since it is the one entry a
reader is likely to want to "fix": ``_grade_root_cause`` fails a labelled
scenario whose run named no cause, and the graded behaviour of every
``noise_*`` scenario is to reach ESCALATED from TRIAGE without a planner call.
The dimension would go red for the agent doing exactly the right thing, which
is a statement about the grader rather than about the agent (``INCIDENTS.md``,
grader drift).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Final

from evals.scenarios.loader import load_scenarios
from evals.scenarios.schema import Scenario, ScenarioFamily
from incident_commander.agent.hypothesis import HypothesisCategory as Category

_SCENARIOS_DIR: Final[Path] = Path(__file__).resolve().parents[2] / "evals" / "scenarios"

#: Scenario name → the labels its world declares, or ``None`` for a recorded
#: decision NOT to grade it on diagnosis. Every entry was decided from the
#: scenario's chaos hook and canned fixtures, and the reasoning sits in the
#: comment above each ``ground_truth`` block in the YAML itself.
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
    # WO-R3-202 (WP-4.3), plan 01 section 7.1's Family B. One symptom, four
    # worlds, three answers — and the pair of `outbox_stall` rows is the
    # measurement: same world, same label, different alert, so only this
    # dimension can tell a run that read the evidence from one that blamed the
    # release the alert happened to name.
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
    "trace_investigation": (Category.UNKNOWN,),
}

#: Families whose scenarios measure the harness rather than a world. Neither
#: manufactures a fault to name: ``TOOL_FAULT`` breaks the probe before any
#: reading exists, and ``HARNESS_CONTROL`` is documented on the enum itself as
#: "the harness under test rather than a world ... with no fault to diagnose".
_UNDIAGNOSABLE_FAMILIES: Final[frozenset[ScenarioFamily]] = frozenset(
    {ScenarioFamily.HARNESS_CONTROL, ScenarioFamily.TOOL_FAULT}
)


def may_abstain(scenario: Scenario) -> bool:
    """Whether "no ground truth" is an admissible decision for this scenario.

    Two admissible shapes, both derived from the scenario rather than named:
    a harness or tool-failure family, or a budget of zero tool calls — which
    is how the corpus spells "this run is expected to terminate before it ever
    probes", and a run that never probes never ranks a hypothesis to grade.
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
        assert (len(graded), len(corpus)) == (36, 45), (
            f"{len(graded)} of {len(corpus)} scenarios are root-cause graded; "
            "WO-R3-261 landed 32 of 41 and WO-R3-202 took it to 36 of 45 — all "
            "four `jobs_not_progressing` worlds carry a label, because ADR 0038 "
            "makes one mandatory for a new scenario. Update this number and the "
            "run summary's coverage line together."
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
