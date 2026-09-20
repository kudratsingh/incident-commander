"""Benchmark metadata: family, difficulty, split, template id and seed (WP-1.4).

Three claims, failing in three places: a template belongs to exactly one split (refused
by the LOADER, since a straddling ``template_id`` is invisible in either file — plan 03
§ 4); every scenario in the corpus is classified (a parameterised sweep, not a required
model field); and the promotion is reconciled against WO-R3-179's provisional rule.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.inventory import (
    INVENTORY_PATH,
    SCENARIO_DIRECTORY,
    provisional_difficulty,
    provisional_family,
)
from evals.runner import ScenarioOutcome
from evals.scenarios.loader import ScenarioLoadError, load_scenarios
from evals.scenarios.schema import (
    BenchmarkSplit,
    Scenario,
    ScenarioDifficulty,
    ScenarioFamily,
)

_MINIMAL = """\
name: {name}
alert: {{source: platform.test, severity: high, fingerprint: metadata-test}}
expectation: {{name: {name}, expected_terminal_state: escalated}}
"""


def _write(directory: Path, name: str, *, filename: str | None = None, extra: str = "") -> Path:
    path = directory / (filename or f"{name}.yaml")
    path.write_text(_MINIMAL.format(name=name) + extra, encoding="utf-8")
    return path


def _corpus() -> list[Scenario]:
    return load_scenarios(SCENARIO_DIRECTORY)


CORPUS = _corpus()
# Parameterised by name so a failure names the scenario a human has to open,
# and so a new scenario joins the sweep the moment its file lands.
BY_NAME = {scenario.name: scenario for scenario in CORPUS}


class TestLegacyDefaults:
    """The 41 shipped scenarios load unchanged; nothing was migrated for this."""

    def test_template_id_defaults_to_the_scenario_name(self, tmp_path: Path) -> None:
        _write(tmp_path, "solo")
        (scenario,) = load_scenarios(tmp_path)
        assert scenario.template_id == "solo"

    def test_seed_defaults_to_zero_and_split_to_dev(self, tmp_path: Path) -> None:
        _write(tmp_path, "solo")
        (scenario,) = load_scenarios(tmp_path)
        assert scenario.seed == 0
        assert scenario.benchmark_split is BenchmarkSplit.DEV

    def test_family_and_difficulty_are_genuinely_optional(self, tmp_path: Path) -> None:
        """Undeclared is a state the model permits and the CORPUS does not.

        A fixture with no opinion says so by omission.
        """
        _write(tmp_path, "solo")
        (scenario,) = load_scenarios(tmp_path)
        assert scenario.family is None
        assert scenario.difficulty is None

    def test_an_explicit_template_id_wins(self, tmp_path: Path) -> None:
        _write(tmp_path, "instance_a", extra="template_id: shared_template\nseed: 7\n")
        (scenario,) = load_scenarios(tmp_path)
        assert scenario.template_id == "shared_template"
        assert scenario.seed == 7

    def test_an_empty_template_id_falls_back_rather_than_grouping_everything(
        self, tmp_path: Path
    ) -> None:
        """``template_id: ''`` is "unset", not "the empty template".

        Left as ``""`` they become one template.
        """
        _write(tmp_path, "solo", extra="template_id: ''\n")
        (scenario,) = load_scenarios(tmp_path)
        assert scenario.template_id == "solo"


class TestSplitsAreByTemplate:
    """The red-before: a ``template_id`` in two splits is a LOAD error."""

    def test_loader_refuses_a_template_id_in_two_splits_and_names_both(
        self, tmp_path: Path
    ) -> None:
        _write(
            tmp_path,
            "chain_seed_0",
            filename="a.yaml",
            extra="template_id: chain\nbenchmark_split: dev\n",
        )
        _write(
            tmp_path,
            "chain_seed_1",
            filename="b.yaml",
            extra="template_id: chain\nbenchmark_split: holdout\n",
        )
        with pytest.raises(ScenarioLoadError) as err:
            load_scenarios(tmp_path)
        message = str(err.value)
        # Both scenarios, both splits, and the template they collide on.
        assert "chain_seed_0" in message
        assert "chain_seed_1" in message
        assert "'dev'" in message
        assert "'holdout'" in message
        assert "chain" in message

    def test_same_template_in_one_split_is_fine(self, tmp_path: Path) -> None:
        """Two instances of one template is the POINT; only straddling is wrong."""
        _write(tmp_path, "chain_seed_0", filename="a.yaml", extra="template_id: chain\nseed: 0\n")
        _write(tmp_path, "chain_seed_1", filename="b.yaml", extra="template_id: chain\nseed: 1\n")
        assert [s.name for s in load_scenarios(tmp_path)] == ["chain_seed_0", "chain_seed_1"]

    def test_distinct_templates_may_sit_in_different_splits(self, tmp_path: Path) -> None:
        _write(tmp_path, "one", filename="a.yaml", extra="benchmark_split: dev\n")
        _write(tmp_path, "two", filename="b.yaml", extra="benchmark_split: validation\n")
        assert {s.benchmark_split for s in load_scenarios(tmp_path)} == {
            BenchmarkSplit.DEV,
            BenchmarkSplit.VALIDATION,
        }

    def test_the_shipped_corpus_loads(self) -> None:
        """63 scenarios, no straddle. The check is inert until it is not.

        41 until WO-R3-202's four, 45 until WO-R3-214's four, 49 until WO-R3-226's
        four, 53 until WO-R3-228's two, 55 until WO-R3-236's two, 57 until
        WO-R3-229's cascade, 58 until WO-R3-221's four and 62 until WO-R3-284's
        fifth `workflow_stuck` world. A pin, not a derivation.
        """
        assert len(CORPUS) == 63


class TestClosedVocabularies:
    def test_family_outside_the_enum_is_a_load_error(self, tmp_path: Path) -> None:
        # Was `jobs_not_progressing`, then `workflow_stuck`, then `api_latency` — each in
        # turn the last of plan 01 § 7's future families, and each in turn built. With
        # WO-R3-221 the enum is closed over every family plan 01 § 7 names, so the
        # stand-in is a family this repo will never have rather than one it has not built
        # yet. That is the stronger statement anyway: the check is about the enum being
        # CLOSED, not about which packet is next.
        _write(tmp_path, "solo", extra="family: kernel_panic\n")
        with pytest.raises(ScenarioLoadError, match="family"):
            load_scenarios(tmp_path)

    def test_difficulty_outside_the_enum_is_a_load_error(self, tmp_path: Path) -> None:
        _write(tmp_path, "solo", extra="difficulty: hard\n")
        with pytest.raises(ScenarioLoadError, match="difficulty"):
            load_scenarios(tmp_path)

    def test_split_outside_the_enum_is_a_load_error(self, tmp_path: Path) -> None:
        _write(tmp_path, "solo", extra="benchmark_split: test\n")
        with pytest.raises(ScenarioLoadError, match="benchmark_split"):
            load_scenarios(tmp_path)

    def test_difficulty_vocabulary_is_exactly_the_plans_nine(self) -> None:
        """Plan 03 § 3 (03:22) names nine. Widening it is a plan change."""
        assert {member.value for member in ScenarioDifficulty} == {
            "control",
            "single",
            "ambiguous",
            "multi_hop",
            "noisy",
            "multi_fault",
            "cascading",
            "temporal",
            "tradeoff",
        }

    def test_split_vocabulary_is_exactly_the_plans_three(self) -> None:
        assert {member.value for member in BenchmarkSplit} == {"dev", "validation", "holdout"}

    def test_no_family_for_a_world_nobody_has_built(self) -> None:
        """Plan 01 § 7's future families arrive with their own packets.

        An empty group reads as a measured zero. The rule (rewritten by WO-R3-214): a family
        member lands with the scenarios that fill it.

        **The list is now EMPTY, and that is the result rather than the check going
        inert.** `api_latency` was the last unbuilt member and WO-R3-221 (WP-8.5) built it,
        so every family plan 01 § 7 names is in the enum and carries scenarios. The
        assertion stays because it is what a future packet trips: adding a member for a
        family whose worlds are not in the same change puts a name back on this list.
        """
        remaining_future_worlds: set[str] = set()
        members = {member.value for member in ScenarioFamily}
        assert not remaining_future_worlds & members, (
            "a family member arrived without the scenarios that fill it. Add the member in the "
            "same change as its worlds, and take it off this list there."
        )
        # And the rule's other half, at every family that HAS arrived: each is in the enum
        # because something manufactures that world.
        populated = {s.family.value for s in CORPUS if s.family is not None}
        for arrived in (
            "jobs_not_progressing",
            "workflow_stuck",
            "temporal_recovery",
            "api_latency",
        ):
            assert arrived in members and arrived in populated, (
                f"{arrived} is a family this corpus built; it must be in the enum AND carry "
                "scenarios, or one half of WO-R3-202's rule has come undone"
            )

    def test_the_family_that_arrived_brought_its_scenarios_with_it(self) -> None:
        """The other direction, and the one that makes the rule above a rule.

        A removal holds only while the corpus makes that world.
        """
        populated = {s.family.value for s in CORPUS if s.family is not None}
        unpopulated = sorted({m.value for m in ScenarioFamily} - populated)
        assert unpopulated == [], (
            f"these families are in the enum and in no scenario: {unpopulated}. "
            "A family for a world nobody has built is an empty group that reads "
            "as a measured zero."
        )


@pytest.mark.parametrize("name", sorted(BY_NAME))
class TestEveryScenarioIsClassified:
    """The sweep that makes a new scenario fail until somebody classifies it."""

    def test_declares_a_family(self, name: str) -> None:
        scenario = BY_NAME[name]
        assert scenario.family is not None, (
            f"{name} declares no `family`. Every report from Phase 2 onward groups by "
            "family; an unclassified scenario falls into the inventory's provisional "
            "guess instead. Add `family: <one of "
            f"{sorted(m.value for m in ScenarioFamily)}>` to "
            f"evals/scenarios/{name}.yaml."
        )

    def test_declares_a_difficulty(self, name: str) -> None:
        scenario = BY_NAME[name]
        assert scenario.difficulty is not None, (
            f"{name} declares no `difficulty`. Add one of "
            f"{sorted(m.value for m in ScenarioDifficulty)} (plan 03 § 3) to "
            f"evals/scenarios/{name}.yaml. `control` means nothing is wrong with the "
            "world, or nothing about a world is being measured."
        )

    def test_is_in_a_split(self, name: str) -> None:
        assert BY_NAME[name].benchmark_split in set(BenchmarkSplit)

    def test_has_a_template_id_and_a_seed(self, name: str) -> None:
        scenario = BY_NAME[name]
        assert scenario.template_id
        assert scenario.seed >= 0


class TestNothingIsHeldOutWithoutADecision:
    def test_no_shipped_scenario_is_in_the_holdout(self) -> None:
        """A holdout is a promise, and making it is the user's call.

        Plan 03 § 4: holdout means *never tuned against*. WP-1.4 assigns nothing to it.
        """
        held_out = sorted(s.name for s in CORPUS if s.benchmark_split is BenchmarkSplit.HOLDOUT)
        assert not held_out, (
            f"{held_out} are marked holdout. Nothing goes into the holdout without the "
            "user saying so: it is a standing promise never to tune against those "
            "templates, not a default."
        )

    def test_every_shipped_scenario_is_dev(self) -> None:
        assert {s.benchmark_split for s in CORPUS} == {BenchmarkSplit.DEV}


class TestPromotionIsReconciled:
    """Where the authoritative value differs from WO-R3-179's guess, and why.

    The table IS the record the work order asked for. A promotion nobody
    wrote down is indistinguishable from a value somebody typed, and the
    difference decides whether a surprising per-family number is a finding.
    """

    #: scenario -> (provisional family, authoritative family, why)
    FAMILY_EXCEPTIONS = {
        # No needle for these five, so the rule answered `uncategorized`.
        "incidents_overview": ("uncategorized", "incidents", "alert scope, probed via incidents"),
        "multi_probe_billing": ("uncategorized", "consumer_lag", "alert IS consumer lag"),
        "multi_probe_hypothesis_evolution": (
            "uncategorized",
            "consumer_lag",
            "alert IS consumer lag",
        ),
        "planner_stops_immediately": (
            "uncategorized",
            "harness_control",
            "no world; the planner's own stop path",
        ),
        "remediate_verify_fails": ("uncategorized", "consumer_lag", "alert IS consumer lag"),
        # WO-R3-202 (WP-4.3). No needle for an outbox or a dispatch pipeline, so the rule
        # answers `uncategorized` for three of the four.
        "jobs_not_progressing_dispatcher_stall": (
            "uncategorized",
            "jobs_not_progressing",
            "plan 01 section 7.1's Family B; the symptom is accepted-but-not-executing",
        ),
        "jobs_not_progressing_outbox_stall": (
            "uncategorized",
            "jobs_not_progressing",
            "same family, the sibling world where the backlog is in Postgres",
        ),
        "jobs_not_progressing_healthy_backlog_spike": (
            "uncategorized",
            "jobs_not_progressing",
            "same family, the level-0 control where nothing is wrong",
        ),
        # The rule DID answer here, and wrongly: the `deploy` needle matched `deploy_noise`
        # and classified the scenario as its own distractor's family.
        "jobs_not_progressing_outbox_stall_deploy_noise": (
            "deploy",
            "jobs_not_progressing",
            "the deploy in the name is the distractor, not the family",
        ),
        # WO-R3-214 (WP-7.2). The rule answered `workflow` for all four — right about the subject,
        # one word short: `workflow_stuck` is worlds sharing one alert, and it is a prefix.
        # WO-R3-284 added the fifth world, and the rule answers `workflow` for it too.
        "workflow_stuck_downstream_child_failed": (
            "workflow",
            "workflow_stuck",
            "same family, the world whose dead-lettered node is a DESCENDANT (ADR 0070)",
        ),
        "workflow_stuck_dead_lettered_root": (
            "workflow",
            "workflow_stuck",
            "plan 01 section 7.2's Family C, not the pre-family `workflow` grouping",
        ),
        "workflow_stuck_resolver_stall": (
            "workflow",
            "workflow_stuck",
            "same family, the world nothing is coming for",
        ),
        "workflow_stuck_paused_dag": (
            "workflow",
            "workflow_stuck",
            "same family, the world one boolean away from the one above",
        ),
        "workflow_stuck_healthy_chain": (
            "workflow",
            "workflow_stuck",
            "same family, the level-0 control where the chain already ran",
        ),
        # WO-R3-226 (WP-10.1). Two retry scenarios share one world and the rule has no needle
        # for `pipeline_stalled`. The family is the SYMPTOM, not the agent's behaviour.
        "retry_second_hypothesis_succeeds": (
            "uncategorized",
            "consumer_lag",
            "the world is a lagging consumer; the retry is the agent's behaviour, not the family",
        ),
        "retry_cap_escalates": (
            "uncategorized",
            "consumer_lag",
            "same world as its sibling, with the second attempt failing too",
        ),
        # WO-R3-236 (WP-14.1). The rule reads the `cache` needle and is right about the
        # subsystem; the family is the SYMPTOM, and what these worlds present is a fault
        # that is there and then is not.
        "temporal_ttl_recovers_before_action": (
            "cache_redis",
            "temporal_recovery",
            "the symptom is the recovery, not the stale entry underneath it",
        ),
        "temporal_ttl_recovers_during_verify": (
            "cache_redis",
            "temporal_recovery",
            "same family, the sibling world one TTL longer",
        ),
        # WO-R3-221 (WP-8.5). Every one of the four is an exception, and the FOUR REASONS
        # ARE THE FAMILY'S ARGUMENT: the rule reads a needle out of a name and each world's
        # needle points at the DEPENDENCY it rules in, not at the symptom they share. So the
        # rule splits one family across three groups and loses the control entirely — which
        # is exactly the "two families where there is one" failure `ScenarioFamily`'s
        # docstring exists to prevent, and the clearest demonstration in the corpus that a
        # family is a property of the ALERT and not of the fault.
        "api_latency_db_query": (
            "postgres",
            "api_latency",
            "plan 01 section 7.3's Family A; the rule sees the database it rules IN and "
            "the symptom the four worlds share is a latency page",
        ),
        "api_latency_redis": (
            "cache_redis",
            "api_latency",
            "same family, and the rule sees the cache for the same reason — one world's "
            "answer is not the group four worlds belong to",
        ),
        "api_latency_downstream": (
            "uncategorized",
            "api_latency",
            "same family; there is no needle for a failing third-party dependency, which "
            "is the rule reaching its limit rather than disagreeing",
        ),
        "api_latency_healthy_control": (
            "uncategorized",
            "api_latency",
            "same family, and the world where nothing is wrong has nothing for a needle "
            "to find — the level-0 control is invisible to a substring rule by definition",
        ),
    }

    #: scenario -> (provisional difficulty, authoritative difficulty, why)
    DIFFICULTY_EXCEPTIONS = {
        # WO-R3-221 (WP-8.5). Only the control moves; the three fault worlds are each one
        # reading away from their answer, which is `single` and what the rule says.
        "api_latency_healthy_control": (
            "single",
            "control",
            "its own header: level-0 control, every dependency reading is at baseline and "
            "the page is stale noise",
        ),
        # The provisional rule keyed on the NAME, so it missed a control
        # that does not start with `noise_`.
        "no_fault_healthy_cache": (
            "single",
            "control",
            "its own header: level-0 control, the world is healthy",
        ),
        "alert_storm": ("single", "noisy", "many alerts in a window; the distractors are the test"),
        "dlq_mixed_partial": (
            "single",
            "ambiguous",
            "mixed categories under an alert that names no slice",
        ),
        "dlq_mislabeled_replay_safe": (
            "single",
            "ambiguous",
            "the hint contradicts the error (ADR 0034)",
        ),
        "saga_stuck": ("single", "multi_hop", "dag state -> the root's own DLQ row -> fence"),
        "remediate_runaway_saga_success": (
            "single",
            "multi_hop",
            "dag state -> the root's own DLQ row -> replay",
        ),
        # WO-R3-202: the provisional rule reads the NAME for `noise_`, so both come back `single`.
        "jobs_not_progressing_healthy_backlog_spike": (
            "single",
            "control",
            "its own header: level-0 control, every reading is healthy",
        ),
        "jobs_not_progressing_outbox_stall_deploy_noise": (
            "single",
            "noisy",
            "a real but unrelated release named in the alert is the variable",
        ),
        # WO-R3-214: two of the four move — one is a control, one takes three reads.
        "workflow_stuck_healthy_chain": (
            "single",
            "control",
            "its own header: level-0 control, the chain has already drained",
        ),
        "workflow_stuck_dead_lettered_root": (
            "single",
            "multi_hop",
            "dag state -> the root's own DLQ row -> replay -> verify on the chain",
        ),
        # WO-R3-284: the fifth world takes the same three reads, and its first one answers
        # about a node other than the one it asked about.
        "workflow_stuck_downstream_child_failed": (
            "single",
            "multi_hop",
            "dag state -> the DESCENDANT's own DLQ row -> fence -> verify on that row",
        ),
        # WO-R3-226 (WP-10.1). Both are `ambiguous` and invisible from a name: the world
        # offers two readings of one symptom.
        "retry_second_hypothesis_succeeds": (
            "single",
            "ambiguous",
            "two readings of one symptom; the first action is the discriminator",
        ),
        "retry_cap_escalates": (
            "single",
            "ambiguous",
            "same two readings, and neither action clears the fault",
        ),
        # WO-R3-228 (WP-11.1). The provisional rule reads a NAME for `noise_`, so it
        # cannot see a second fault: both of these worlds hold two independent faults,
        # which is plan 03 § 3's `multi_fault` rung and the first two scenarios on it.
        "dual_fault_dlq_and_consumer_lag": (
            "single",
            "multi_fault",
            "two independent faults, two remediations, one run",
        ),
        "dual_fault_consumer_lag_and_bad_deploy": (
            "single",
            "multi_fault",
            "two independent faults where only one has a Tier-1 fix",
        ),
        # WO-R3-236 (WP-14.1). Invisible from a name: what makes these hard is a clock.
        "temporal_ttl_recovers_before_action": (
            "single",
            "temporal",
            "the fault expires mid-run; the difficulty is the timeline, not the reading",
        ),
        "temporal_ttl_recovers_during_verify": (
            "single",
            "temporal",
            "same timeline, positioned inside the verify window instead",
        ),
        # WO-R3-229 (WP-11.2). One hook, so the rule sees one fault and answers `single`;
        # what it cannot see is that three of the four things wrong with this world are
        # consequences of the fourth. The difficulty is following the chain to its root.
        "cascading_redis_starves_backpressure": (
            "single",
            "cascading",
            "one fault and four links; the alert names the last of them",
        ),
    }

    @pytest.mark.parametrize("name", sorted(BY_NAME))
    def test_family_matches_the_rule_or_a_recorded_exception(self, name: str) -> None:
        scenario = BY_NAME[name]
        assert scenario.family is not None
        guess = provisional_family(scenario)
        recorded = self.FAMILY_EXCEPTIONS.get(name)
        if recorded is None:
            assert scenario.family.value == guess, (
                f"{name} is classified {scenario.family.value!r} but WO-R3-179's rule "
                f"said {guess!r}, and no exception is recorded. Either promote the "
                "rule's value, or add a row to FAMILY_EXCEPTIONS with the reason."
            )
            return
        was, now, _why = recorded
        assert (guess, scenario.family.value) == (was, now)

    @pytest.mark.parametrize("name", sorted(BY_NAME))
    def test_difficulty_matches_the_rule_or_a_recorded_exception(self, name: str) -> None:
        scenario = BY_NAME[name]
        assert scenario.difficulty is not None
        guess = provisional_difficulty(scenario)
        recorded = self.DIFFICULTY_EXCEPTIONS.get(name)
        if recorded is None:
            assert scenario.difficulty.value == guess, (
                f"{name} is classified {scenario.difficulty.value!r} but WO-R3-179's "
                f"rule said {guess!r}, and no exception is recorded. Either promote "
                "the rule's value, or add a row to DIFFICULTY_EXCEPTIONS with the "
                "reason."
            )
            return
        was, now, _why = recorded
        assert (guess, scenario.difficulty.value) == (was, now)

    @pytest.mark.parametrize("name", sorted(BY_NAME))
    def test_every_exception_carries_a_reason(self, name: str) -> None:
        for table in (self.FAMILY_EXCEPTIONS, self.DIFFICULTY_EXCEPTIONS):
            row = table.get(name)
            if row is not None:
                assert row[2].strip(), f"{name}'s exception records no reason"

    def test_no_exception_names_a_scenario_that_is_gone(self) -> None:
        """A stale row silently excuses a scenario that no longer exists."""
        named = set(self.FAMILY_EXCEPTIONS) | set(self.DIFFICULTY_EXCEPTIONS)
        assert not named - set(BY_NAME), sorted(named - set(BY_NAME))


class TestMetadataIsEvaluatorOnly:
    """None of it reaches the agent — it is bookkeeping ABOUT the measurement."""

    @pytest.mark.parametrize(
        "field", ["template_id", "seed", "family", "difficulty", "benchmark_split"]
    )
    def test_field_is_on_the_evaluator_side_of_the_boundary(self, field: str) -> None:
        assert field in Scenario.EVALUATOR_ONLY_FIELDS
        assert field not in Scenario.AGENT_VISIBLE_FIELDS

    @pytest.mark.parametrize("name", sorted(BY_NAME))
    def test_the_projection_carries_none_of_it(self, name: str) -> None:
        """``difficulty`` in a prompt narrows the agent's search for free."""
        visible = BY_NAME[name].agent_visible().model_dump()
        assert not set(visible) & {
            "template_id",
            "seed",
            "family",
            "difficulty",
            "benchmark_split",
        }


class TestTheInventoryReadsTheRealValues:
    def test_committed_inventory_records_the_declared_family_and_difficulty(self) -> None:
        rows = {row["name"]: row for row in json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))}
        assert set(rows) == set(BY_NAME)
        for name, scenario in BY_NAME.items():
            assert scenario.family is not None
            assert scenario.difficulty is not None
            assert rows[name]["family"] == {"value": scenario.family.value, "provisional": False}
            assert rows[name]["difficulty"] == {
                "value": scenario.difficulty.value,
                "provisional": False,
            }

    def test_committed_inventory_records_template_seed_and_split(self) -> None:
        rows = {row["name"]: row for row in json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))}
        for name, scenario in BY_NAME.items():
            assert rows[name]["template_id"] == scenario.template_id
            assert rows[name]["seed"] == scenario.seed
            assert rows[name]["benchmark_split"] == scenario.benchmark_split.value

    def test_nothing_in_the_committed_inventory_is_still_provisional(self) -> None:
        rows = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
        unclassified = sorted(
            row["name"]
            for row in rows
            if row["family"]["provisional"] or row["difficulty"]["provisional"]
        )
        assert not unclassified, (
            f"{unclassified} still carry a guessed family or difficulty. Classify them "
            "in the YAML and run `make inventory`."
        )


class TestTheReportCanGroupWithoutASchemaChange:
    """WP-2.5 groups by family, difficulty and split; the row already carries them."""

    @pytest.mark.parametrize(
        "field", ["template_id", "seed", "family", "difficulty", "benchmark_split"]
    )
    def test_outcome_carries_the_grouping_key(self, field: str) -> None:
        assert field in ScenarioOutcome.model_fields

    @pytest.mark.parametrize(
        "field", ["template_id", "seed", "family", "difficulty", "benchmark_split"]
    )
    def test_the_key_defaults_to_none_so_archived_reports_still_parse(self, field: str) -> None:
        """Archived reports and the committed baseline predate this record.

        Append-only evidence, so absence is tolerated (ADR 0013).
        """
        assert ScenarioOutcome.model_fields[field].default is None
