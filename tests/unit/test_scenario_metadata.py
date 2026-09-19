"""Benchmark metadata: family, difficulty, split, template id and seed (WP-1.4).

Three claims, and they fail in three different places on purpose.

1. **A template belongs to exactly one split** — refused by the LOADER, over
   the whole directory, because a straddling ``template_id`` is invisible in
   either scenario's own file. Plan 03 § 4 puts the enforcement there
   ("the loader refuses") rather than in a report, because by the time a
   report footnote is read the number it footnotes has been quoted.
2. **Every scenario in the corpus is classified** — a parameterised sweep
   over ``evals/scenarios/``, so a new scenario fails here, by name, until
   somebody gives it a family and a difficulty. Not a required field on the
   model: thirteen inline ``Scenario(...)`` fixtures and a dozen inline YAML
   blobs in this suite have no opinion about family, and a required field
   they all have to fill is a field they all fill with whatever loads.
3. **The promotion is reconciled, not asserted** — every authoritative value
   either equals what WO-R3-179's provisional rule produced or is a recorded
   exception with a reason. "We promoted the provisional values" is then a
   checkable claim rather than a sentence in a PR body.
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

        The corpus sweep below is what makes a real scenario carry them. A
        fixture that has no opinion says so by omission rather than by
        picking a family at random to satisfy a constructor.
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

        Left as ``""`` it would make every such scenario one template in
        every report, and would make the loader's split check compare two
        unrelated scenarios.
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
        # Neither file is wrong on its own, so an error naming one of them
        # sends the reader to a file that looks fine.
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
        """49 scenarios, no straddle. The check is inert until it is not.

        41 until WO-R3-202 (WP-4.3) added the four `jobs_not_progressing`
        worlds, and 45 until WO-R3-214 (WP-7.2) added the four `workflow_stuck`
        ones. The number is a pin rather than a derivation on purpose: a
        scenario that appears without anybody noticing is the thing this
        catches.
        """
        assert len(CORPUS) == 49


class TestClosedVocabularies:
    def test_family_outside_the_enum_is_a_load_error(self, tmp_path: Path) -> None:
        # Was `jobs_not_progressing`, which WO-R3-202 made real, then
        # `workflow_stuck`, which WO-R3-214 (WP-7.2) did. `api_latency` is the
        # last of plan 01 § 7's future families, so it is what stands here now;
        # the test below is what forces the swap when its packet lands.
        _write(tmp_path, "solo", extra="family: api_latency\n")
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

        An empty group in a report reads as a measured zero, which is worse
        than an absent one.

        REWRITTEN BY WO-R3-214 (WP-7.2), because its premise was that
        `workflow_stuck` was one of those worlds and it now is not. The rule the
        test was protecting has not moved: a family member lands in the SAME
        change as the scenarios that fill it, never before. So the assertion is
        now the rule itself, in both directions —

        * the families that have arrived (`jobs_not_progressing` in WO-R3-202,
          `workflow_stuck` in WO-R3-214) are in the enum AND populated, which
          `test_the_family_that_arrived_brought_its_scenarios_with_it` below
          checks over the whole enum; and
        * `api_latency`, the one world of plan 01 § 7 nobody has built, is in
          neither.

        Deleting this test when `workflow_stuck` landed would have removed the
        second half with nothing left to keep an unpopulated member out. Naming
        the remaining witness is what keeps it a real check rather than a
        tautology: when `api_latency` ships, this list empties and the test's own
        docstring says so out loud.
        """
        remaining_future_worlds = {"api_latency"}
        members = {member.value for member in ScenarioFamily}
        assert not remaining_future_worlds & members, (
            "a family member arrived without the scenarios that fill it. Add the member in the "
            "same change as its worlds, and take it off this list there."
        )
        # And the rule's other half, at the two families that HAVE arrived: each
        # is in the enum because something manufactures that world.
        populated = {s.family.value for s in CORPUS if s.family is not None}
        for arrived in ("jobs_not_progressing", "workflow_stuck"):
            assert arrived in members and arrived in populated, (
                f"{arrived} is a family this corpus built; it must be in the enum AND carry "
                "scenarios, or one half of WO-R3-202's rule has come undone"
            )

    def test_the_family_that_arrived_brought_its_scenarios_with_it(self) -> None:
        """The other direction, and the one that makes the rule above a rule.

        A member removed from the list above is only legitimate while something
        in the corpus actually manufactures that world — otherwise the exemption
        was just deleted and the empty group is back.
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

        Plan 03 § 4: holdout means *never tuned against*. Assigning a
        template to it commits every future session to leaving it alone, and
        that is a scope decision rather than a builder's default. WP-1.4
        builds the mechanism and assigns nothing to it.
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
        # The substring rule had no needle for these five and answered
        # `uncategorized`, which is not a family — it is the rule saying it
        # could not tell.
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
        # WO-R3-202 (WP-4.3). The substring rule has no needle for an outbox or
        # a dispatch pipeline, so it answers `uncategorized` for three of the
        # four — the rule saying it cannot tell, which is exactly what it should
        # say about a world that did not exist when it was written.
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
        # This one the rule DID answer, and answered wrongly in the most
        # instructive way available: the name carries `deploy_noise`, the
        # `deploy` needle matched, and the rule classified the scenario as the
        # family of its own distractor. The noise is the point of the scenario
        # and not its subject.
        "jobs_not_progressing_outbox_stall_deploy_noise": (
            "deploy",
            "jobs_not_progressing",
            "the deploy in the name is the distractor, not the family",
        ),
        # WO-R3-214 (WP-7.2). The rule DID answer for all four, and answered
        # `workflow` — the family that groups the saga/chain scenarios written
        # one at a time before families existed. It is not wrong about the
        # subject, it is one word short of the distinction: `workflow_stuck` is
        # the family of FOUR worlds sharing one alert, and folding them into
        # `workflow` would average a family's per-world numbers into a grouping
        # that is not one. The substring `workflow` is a prefix of
        # `workflow_stuck`, so the rule will keep answering this way for every
        # future member.
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
    }

    #: scenario -> (provisional difficulty, authoritative difficulty, why)
    DIFFICULTY_EXCEPTIONS = {
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
        # WO-R3-202 (WP-4.3). Same miss as `no_fault_healthy_cache`: the
        # provisional rule reads the NAME for `noise_`, so a control and a noise
        # variant that spell themselves otherwise both come back `single`.
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
        # WO-R3-214 (WP-7.2). Two of the four move, and each for a reason the
        # provisional rule cannot see from a name: one is a control, and one
        # takes three reads to answer.
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

        They are append-only evidence and are never rewritten, so the reader
        tolerates the absence — the same precedent ADR 0013 set for
        ``live_mcp`` / ``live_llm``, and ``provenance`` after it.
        """
        assert ScenarioOutcome.model_fields[field].default is None
