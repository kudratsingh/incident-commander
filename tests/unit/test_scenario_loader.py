import json
import re
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Any

import pytest

from evals.graders.deterministic import leaf_claims
from evals.scenarios.loader import ScenarioLoadError, load_scenario, load_scenarios
from evals.scenarios.schema import Scenario, chaos_tool_schemas
from incident_commander.agent.state import IncidentState
from incident_commander.tools.mcp_client import ToolResult

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCENARIO_DIR = _REPO_ROOT / "evals" / "scenarios"
_EXAMPLE = _SCENARIO_DIR / "consumer_lag_high.yaml"


_VALID_YAML = """\
name: valid
description: a scenario
tags: [read-only]
alert:
  source: billing
  severity: high
expectation:
  name: valid
  expected_terminal_state: escalated
"""


class TestLoadScenario:
    def test_loads_shipped_example(self) -> None:
        scenario = load_scenario(_EXAMPLE)
        assert scenario.name == "consumer_lag_high"
        # WO-R2-45: the name keeps "high" (the baseline is keyed on it); the
        # severity must be one ALLOWED_SEVERITIES can emit.
        assert scenario.alert.severity == "critical"
        assert scenario.expectation.expected_terminal_state is IncidentState.ESCALATED
        # Tool-scoped since evals/evidence_audit.py: a bare substring matched more.
        group_asserts = [
            f
            for f in leaf_claims(scenario.expectation.expected_evidence_fields)
            if f.tools == ("get_consumer_lag",) and f.field == "consumer_group"
        ]
        assert [f.equals for f in group_asserts] == ["worker-dispatcher"]
        assert scenario.expectation.max_tool_calls == 5

    def test_loads_valid_string_via_tmp_file(self, tmp_path: Path) -> None:
        target = tmp_path / "s.yaml"
        target.write_text(_VALID_YAML)
        scenario = load_scenario(target)
        assert scenario.name == "valid"

    def test_missing_file_raises_scenario_load_error(self, tmp_path: Path) -> None:
        with pytest.raises(ScenarioLoadError, match="read failed"):
            load_scenario(tmp_path / "nope.yaml")

    def test_malformed_yaml_raises(self, tmp_path: Path) -> None:
        target = tmp_path / "bad.yaml"
        target.write_text("key: value\n  bad-indent:")
        with pytest.raises(ScenarioLoadError, match="YAML parse failed"):
            load_scenario(target)

    def test_non_mapping_top_level_raises(self, tmp_path: Path) -> None:
        target = tmp_path / "list.yaml"
        target.write_text("- one\n- two\n")
        with pytest.raises(ScenarioLoadError, match="mapping"):
            load_scenario(target)

    def test_schema_violation_raises(self, tmp_path: Path) -> None:
        target = tmp_path / "bad.yaml"
        target.write_text("name: bad\nalert:\n  source: billing\nexpectation:\n  name: bad\n")
        with pytest.raises(ScenarioLoadError, match="schema violation"):
            load_scenario(target)


class TestLoadScenarios:
    def test_loads_directory(self, tmp_path: Path) -> None:
        (tmp_path / "a.yaml").write_text(_VALID_YAML.replace("name: valid", "name: a"))
        (tmp_path / "b.yml").write_text(_VALID_YAML.replace("name: valid", "name: b"))
        # Non-YAML files ignored.
        (tmp_path / "readme.md").write_text("skip me")
        scenarios = load_scenarios(tmp_path)
        assert [s.name for s in scenarios] == ["a", "b"]

    def test_missing_directory_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ScenarioLoadError, match="not a directory"):
            load_scenarios(tmp_path / "does-not-exist")

    def test_bubbles_load_errors_from_any_file(self, tmp_path: Path) -> None:
        (tmp_path / "good.yaml").write_text(_VALID_YAML)
        (tmp_path / "bad.yaml").write_text("- not a mapping")
        with pytest.raises(ScenarioLoadError):
            load_scenarios(tmp_path)

    def test_shipped_scenarios_directory_loads(self) -> None:
        scenarios = load_scenarios(_SCENARIO_DIR)
        assert len(scenarios) >= 1
        assert any(s.name == "consumer_lag_high" for s in scenarios)

    def test_two_files_may_not_share_a_scenario_name(self, tmp_path: Path) -> None:
        """A name collision took the suite down mid-run, and nothing caught it.

        The archive exclusive-creates `trajectories/<name>.json`, so the second
        scenario raises `FileExistsError`.
        """
        (tmp_path / "first.yaml").write_text(_VALID_YAML.replace("name: valid", "name: twin"))
        (tmp_path / "second.yaml").write_text(_VALID_YAML.replace("name: valid", "name: twin"))
        with pytest.raises(ScenarioLoadError, match="duplicate scenario name 'twin'"):
            load_scenarios(tmp_path)

    def test_the_collision_error_names_both_files(self, tmp_path: Path) -> None:
        # Neither file is wrong on its own, so the error is useless unless it
        # says what the collision was with.
        (tmp_path / "first.yaml").write_text(_VALID_YAML.replace("name: valid", "name: twin"))
        (tmp_path / "second.yaml").write_text(_VALID_YAML.replace("name: valid", "name: twin"))
        with pytest.raises(ScenarioLoadError) as err:
            load_scenarios(tmp_path)
        assert "first.yaml" in str(err.value) and "second.yaml" in str(err.value)

    def test_the_shipped_scenario_names_are_unique(self) -> None:
        names = [s.name for s in load_scenarios(_SCENARIO_DIR)]
        assert len(names) == len(set(names))


# ADR 0006/0009 + docs/runbook.md: a correct run makes 10 calls, graded ceiling 13. Since
# ADR 0019 that is also the runtime BudgetLedger ceiling (invariant 7): lower truncates.
_REMEDIATION_MIN_CAP = 13


@lru_cache(maxsize=1)
def _shipped_scenarios() -> tuple[Scenario, ...]:
    """Parse the shipped scenario directory once per session, not per assertion."""
    return tuple(load_scenarios(_SCENARIO_DIR))


# No `expected_action_tools` means no VERIFYING poll loop, so the profile
# does not apply. Names absent here are not asserted.
_NON_REMEDIATION_CAPS: dict[str, int | None] = {
    "alert_storm": 10,
    "consumer_lag_analytics_critical": 11,
    "consumer_lag_healthy_zero": 5,
    "consumer_lag_high": 5,
    "consumer_lag_medium": 11,
    "consumer_lag_missing_group": 5,
    "consumer_lag_null_unknown_state": 5,
    "consumer_lag_orders_high": 5,
    "consumer_lag_payments_critical": 12,
    "consumer_lag_shipping_extreme": 10,
    "deploy_correlation": 5,
    "dlq_backlog": 5,
    "failed_traces_scan": 5,
    "incidents_overview": 6,
    "multi_probe_billing": 5,
    "multi_probe_hypothesis_evolution": 5,
    "noise_info_orders": 0,
    "noise_info_severity": 0,
    "noise_low_analytics": 0,
    "noise_low_severity": 0,
    "noise_missing_severity": 0,
    "planner_stops_immediately": 0,
    "postgres_slow": 5,
    "redis_saturation": 5,
    # `saga_stuck` left this table with WO-R2-160: it acts, so it polls, and its cap moved
    # 11 -> 13 — at 11 ADR 0019's runtime ceiling would truncate the verify loop.
    "tool_missing_response": 0,
    "tool_output_schema_mismatch": 0,
    "tool_result_marked_error": 0,
    "trace_investigation": 5,
}


class TestShippedScenarioBudgetCaps:
    """Cap calibration is a CLASS rule, not nine hand-checked numbers.

    A future remediation scenario copied from an old template must not be
    able to reintroduce a cap that a correct live run cannot meet (A-02).
    """

    @staticmethod
    def _shipped() -> tuple[Scenario, ...]:
        return _shipped_scenarios()

    def test_remediation_class_carries_the_live_polling_cap(self) -> None:
        remediation = [s for s in self._shipped() if s.expectation.expected_action_tools]
        # Guard against the assertion going vacuous if the class empties out.
        assert len(remediation) >= 9, "expected the shipped remediation scenario class"
        too_tight = {
            s.name: s.expectation.max_tool_calls
            for s in remediation
            if s.expectation.max_tool_calls is None
            or s.expectation.max_tool_calls < _REMEDIATION_MIN_CAP
        }
        assert too_tight == {}, (
            f"remediation scenarios must allow >= {_REMEDIATION_MIN_CAP} tool calls "
            f"(2 probes + 1 re-probe + 1 action + up to 6 verify polls); too tight: {too_tight}"
        )

    def test_non_remediation_caps_are_unchanged(self) -> None:
        actual = {
            s.name: s.expectation.max_tool_calls
            for s in self._shipped()
            if not s.expectation.expected_action_tools and s.name in _NON_REMEDIATION_CAPS
        }
        assert actual == _NON_REMEDIATION_CAPS

    def test_read_only_scenarios_declare_no_action_tools(self) -> None:
        # `consumer_lag_healthy_zero` reads like a remediation case but is the
        # healthy-lag, no-action scenario — it must stay out of the class.
        by_name = {s.name: s for s in self._shipped()}
        assert by_name["consumer_lag_healthy_zero"].expectation.expected_action_tools == ()


# Substrings the schema refuses, mirroring evals/graders/deterministic.py.
# Re-derived from the YAMLs so a relaxed validator still fails.
_TOXIC_EXACT_SUBSTRING = "verified"
_SERIALIZED_FRAGMENT = re.compile(r'^"[^"]+":')

# Value assertions moved from `expected_evidence_contains` to
# `expected_evidence_fields` (A-09/A-10/S-19/S-20); listed so a drop fails.
_STRUCTURED_EVIDENCE_SCENARIOS: frozenset[str] = frozenset(
    {
        "consumer_lag_null_unknown_state",
        "dlq_human_required_escalates",
        "dlq_mixed_partial",
        "dlq_replay_safe_success",
        "remediate_consumer_lag_success",
        "remediate_dlq_backlog_success",
        "remediate_runaway_saga_success",
        "remediate_stale_cache_success",
        # WO-R2-34: these traded a bare-field-name substring (key text
        # `model_dump_json` emits regardless) for a real value assertion.
        "alert_storm",
        "consumer_lag_healthy_zero",
        "consumer_lag_high",
        "consumer_lag_missing_group",
        "consumer_lag_shipping_extreme",
        "dlq_backlog",
        "redis_saturation",
        "remediate_verify_fails",
        "saga_stuck",
        # B1.7: `expected_evidence_contains: [postgres]` was satisfied by the runner's
        # own "tool error (get_postgres_health)" text — green exactly when the read failed.
        "postgres_slow",
    }
)


class TestCannedOnlyMarking:
    """The unrunnable remediation scenarios stay marked canned-only.

    Each name is a fault the live platform cannot manufacture, so a live run would
    grade a premise that does not exist; the runner refuses a --live selection
    containing one (exit 8). Removing a name needs the platform capability it waits on.
    """

    _CANNED_ONLY: frozenset[str] = frozenset(
        {
            "alert_storm",
            "remediate_verify_fails",
        }
    )

    def test_unmanufacturable_scenarios_are_canned_only(self) -> None:
        by_name = {s.name: s for s in _shipped_scenarios()}
        not_marked = sorted(name for name in self._CANNED_ONLY if not by_name[name].canned_only)
        assert not_marked == [], (
            f"scenarios whose fault the live platform cannot manufacture have "
            f"regained a live leg: {not_marked}. If the platform capability "
            "shipped and is pinned, remove the name here and in the YAML "
            "comment; otherwise flip use_live_mcp/use_live_llm back to false."
        )


class TestEvidenceExpectationHygiene:
    """No shipped scenario may assert a grader-toxic evidence substring.

    Two shapes are fake-green or brittle by construction (A-09, A-10): bare
    `verified` matches the `not_verified: ...` a failed verify writes, and a
    serialized-JSON fragment pins the serializer, field order and one value.
    """

    def test_no_shipped_scenario_asserts_a_toxic_evidence_substring(self) -> None:
        offenders = [
            f"{s.name}: {item!r}"
            for s in _shipped_scenarios()
            for item in s.expectation.expected_evidence_contains
            if item == _TOXIC_EXACT_SUBSTRING or _SERIALIZED_FRAGMENT.match(item)
        ]
        assert not offenders, (
            "scenarios assert grader-toxic evidence substrings; move value "
            f"assertions to expected_evidence_fields: {offenders}"
        )

    def test_migrated_scenarios_carry_structured_field_assertions(self) -> None:
        by_name = {s.name: s for s in _shipped_scenarios()}
        without = sorted(
            name
            for name in _STRUCTURED_EVIDENCE_SCENARIOS
            if not by_name[name].expectation.expected_evidence_fields
        )
        assert without == [], (
            "these scenarios traded a substring value assertion for a "
            f"structured one and must still carry it: {without}"
        )

    def test_structured_assertions_name_a_tool_the_scenario_can_call(self) -> None:
        # A field expectation on a tool the scenario never cans is dead coverage.
        offenders: list[str] = []
        for name in sorted(_STRUCTURED_EVIDENCE_SCENARIOS):
            scenario = {s.name: s for s in _shipped_scenarios()}[name]
            canned = set(scenario.canned_tool_responses)
            for expectation in leaf_claims(scenario.expectation.expected_evidence_fields):
                if not canned.intersection(expectation.tools):
                    offenders.append(f"{name}: {expectation.field} on {list(expectation.tools)}")
        assert offenders == [], (
            f"evidence field expectations name tools the scenario never cans: {offenders}"
        )


# The eight groups the platform can resolve: `worker-dispatcher` plus the seven the eval seed
# populates, mirroring platform `SEEDED_CONSUMER_GROUPS`. Any other is answered `lag: null`.
_PLATFORM_RESOLVABLE_GROUPS: frozenset[str] = frozenset(
    {
        "worker-dispatcher",
        "billing-consumer",
        "orders-consumer",
        "notifications-consumer",
        "analytics-consumer",
        "payments-consumer",
        "shipping-consumer",
        "healthy-consumer",
    }
)

_LAG_TOOL = "get_consumer_lag"
_CACHE_KEY_PREFIX = "kafka:consumer_lag:"


def _canned_lag_payloads() -> list[tuple[str, dict[str, Any]]]:
    """Every canned ``get_consumer_lag`` text block, as (scenario name, parsed JSON)."""
    payloads: list[tuple[str, dict[str, Any]]] = []
    for scenario in _shipped_scenarios():
        canned = scenario.canned_tool_responses.get(_LAG_TOOL)
        if canned is None:
            continue
        results: tuple[ToolResult, ...] = (
            (canned,) if isinstance(canned, ToolResult) else tuple(canned)
        )
        for result in results:
            # Error results carry prose, not a payload — nothing to lint.
            if result.is_error:
                continue
            for block in result.content:
                if block.get("type") != "text":
                    continue
                parsed = json.loads(block["text"])
                assert isinstance(parsed, dict), f"{scenario.name}: canned block is not an object"
                payloads.append((scenario.name, parsed))
    return payloads


class TestCannedConsumerLagContract:
    """Lint the canned ``get_consumer_lag`` fixtures against the platform contract.

    Canned responses must be recordings of real ones; the unknown-group fixture
    drifted into fabricating ``lag: 42`` with another group's cache_key (A-11).
    """

    def test_at_least_one_lag_fixture_is_linted(self) -> None:
        # Guards against the lint silently covering nothing if the canned
        # shape ever changes.
        assert len(_canned_lag_payloads()) >= 10

    def test_unresolvable_group_lag_is_null(self) -> None:
        # Platform contract: an uncached group returns `lag: null`, not a number:
        # a fabricated 0 reads as healthy (consumer_lag.py:54).
        offenders = [
            f"{name}: consumer_group={payload.get('consumer_group')!r} lag={payload.get('lag')!r}"
            for name, payload in _canned_lag_payloads()
            if payload.get("consumer_group") not in _PLATFORM_RESOLVABLE_GROUPS
            and payload.get("lag") is not None
        ]
        assert not offenders, (
            "canned get_consumer_lag fixtures fabricate a numeric lag for a group "
            f"the platform would answer with null: {offenders}"
        )

    def test_cache_key_is_derived_from_the_requested_group(self) -> None:
        # Platform contract: cache_key echoes `_redis_key(inp.consumer_group)`
        # (consumer_lag.py:26-27): it never names another group.
        offenders = [
            f"{name}: consumer_group={payload['consumer_group']!r} "
            f"cache_key={payload['cache_key']!r}"
            for name, payload in _canned_lag_payloads()
            if "cache_key" in payload
            and payload["cache_key"] != f"{_CACHE_KEY_PREFIX}{payload['consumer_group']}"
        ]
        assert not offenders, (
            f"canned get_consumer_lag cache_key does not echo the requested group: {offenders}"
        )

    def test_null_lag_is_exercised_end_to_end_by_a_scenario(self) -> None:
        # S-21: one scenario must drive a null reading through the whole loop and
        # must not grade as healthy.
        null_lag_scenarios = {
            name for name, payload in _canned_lag_payloads() if payload.get("lag") is None
        }
        assert null_lag_scenarios, "no scenario cans a `lag: null` get_consumer_lag response"
        by_name: dict[str, Scenario] = {s.name: s for s in _shipped_scenarios()}
        for name in sorted(null_lag_scenarios):
            expectation = by_name[name].expectation
            assert expectation.expected_terminal_state is not IncidentState.RESOLVED, (
                f"{name}: a null lag reading is unknown-not-healthy; the scenario "
                "must not expect a resolved run"
            )
            assert expectation.expected_terminal_state is IncidentState.ESCALATED, (
                f"{name}: expected the null-lag scenario to escalate, got "
                f"{expectation.expected_terminal_state}"
            )


class TestStuckDagChainIdsArePinnedCorrectly:
    """The saga scenarios hard-code ids the chaos hook derives.

    ``create_stuck_dag`` derives each row's key as ``uuid5(ns,
    f"{tenant_id}:{chain_name}:{role}")``, and plat #184 publishes that namespace so
    a scenario can pin ids. Recomputing here fails offline, not mid-paid-run.
    """

    # backend/app/mcp/tools/chaos/create_stuck_dag.py::_NAMESPACE
    _NAMESPACE = uuid.UUID("cccccccc-57ac-4000-8000-000000000000")
    # backend/app/models/tenant.py::DEFAULT_TENANT_ID, seeded by migration
    # f8a1c4e23507_multi_tenancy, so it is identical on every stack.
    _DEFAULT_TENANT = "d3fa17de-7a17-de7a-17de-7a17de7a17de"

    def _root_id(self, chain_name: str) -> str:
        return str(uuid.uuid5(self._NAMESPACE, f"{self._DEFAULT_TENANT}:{chain_name}:root"))

    @pytest.mark.parametrize(
        ("scenario_name", "chain_name"),
        [
            ("saga_stuck", "saga-stuck-eval"),
            ("remediate_runaway_saga_success", "runaway-saga-eval"),
        ],
    )
    def test_the_pinned_root_id_is_the_hook_derivation(
        self, scenario_name: str, chain_name: str
    ) -> None:
        by_name = {s.name: s for s in _shipped_scenarios()}
        scenario = by_name[scenario_name]
        assert scenario.chaos_setup is not None
        assert scenario.chaos_setup.name == "create_stuck_dag"
        assert scenario.chaos_setup.arguments["chain_name"] == chain_name

        expected = self._root_id(chain_name)
        assert scenario.alert.model_extra is not None
        assert scenario.alert.model_extra["job_id"] == expected, (
            f"{scenario_name}: the alert pins a job_id that create_stuck_dag "
            f"would not produce for chain_name={chain_name!r}"
        )
        probed = {
            probe.arguments.get("job_id")
            for probe in scenario.expected_precondition
            if probe.tool == "get_dag_state"
        }
        assert probed == {expected}, (
            f"{scenario_name}: the precondition probes {probed}, not the "
            f"chain's derived root {expected}"
        )

    def test_every_site_naming_the_saga_stuck_root_is_the_hook_derivation(self) -> None:
        """WO-R2-160 made this scenario grade WHICH job it fenced.

        The root id is now in the action pin, two ``where`` selectors, the briefing
        claim and the forbidden-replay list, so a single-site check would let them drift.
        """
        scenario = {s.name: s for s in _shipped_scenarios()}["saga_stuck"]
        expected = self._root_id("saga-stuck-eval")
        expectation = scenario.expectation

        pinned: dict[str, set[str]] = {
            "expected_action_arguments": {
                str(a.equals) for a in expectation.expected_action_arguments
            },
            "evidence where-selectors": {
                str(e.where.equals)
                for e in leaf_claims(expectation.expected_evidence_fields)
                if e.where
            },
            "get_dag_state seed_id claim": {
                str(e.equals)
                for e in leaf_claims(expectation.expected_evidence_fields)
                if e.field == "seed_id"
            },
        }
        for site, values in pinned.items():
            assert values == {expected}, (
                f"saga_stuck: {site} pins {sorted(values)}, but "
                f"create_stuck_dag(chain_name='saga-stuck-eval') produces {expected}"
            )

        assert expected in expectation.expect_briefing_contains, (
            "saga_stuck: the briefing claim does not name the fenced root"
        )
        assert expected in expectation.forbidden_replay_job_ids, (
            "saga_stuck: the chain root must be forbidden as a replay target — "
            "a human_required payload re-fails on every replay"
        )

    def test_the_two_scenarios_do_not_share_a_chain(self) -> None:
        """Sharing one would make each run depend on the other's order.

        ``create_stuck_dag`` refuses a drifted chain (409 `stuck_chain_name_in_use`).
        """
        by_name = {s.name: s for s in _shipped_scenarios()}
        names = [
            by_name[n].chaos_setup.arguments["chain_name"]  # type: ignore[union-attr]
            for n in ("saga_stuck", "remediate_runaway_saga_success")
        ]
        assert len(set(names)) == len(names), f"saga scenarios share a chain_name: {names}"


class TestBadDataFixtureIdIsPinnedCorrectly:
    """`dlq_human_required_escalates` hard-codes an id its chaos hook derives.

    Same rule as ``TestStuckDagChainIdsArePinnedCorrectly``: ``create_bad_data_job``
    derives the key as ``uuid5(ns, f"{tenant_id}:{fixture_name}")``, exported as
    ``fixture_id(...)`` (plat #198). Every site the id appears is checked.
    """

    # backend/app/mcp/tools/chaos/create_bad_data_job.py::_NAMESPACE
    _NAMESPACE = uuid.UUID("dddddddd-bad0-4000-8000-000000000000")
    # backend/app/models/tenant.py::DEFAULT_TENANT_ID, from migration
    # f8a1c4e23507_multi_tenancy; confirmed off the v0.6.2 stack.
    _DEFAULT_TENANT = "d3fa17de-7a17-de7a-17de-7a17de7a17de"
    _SCENARIO = "dlq_human_required_escalates"
    _FIXTURE_NAME = "human-required-eval"

    def _fixture_id(self, fixture_name: str) -> str:
        return str(uuid.uuid5(self._NAMESPACE, f"{self._DEFAULT_TENANT}:{fixture_name}"))

    def _scenario(self) -> Scenario:
        return {s.name: s for s in _shipped_scenarios()}[self._SCENARIO]

    def test_the_hook_is_the_unclassified_bad_data_seeder(self) -> None:
        """The whole drill rests on the row arriving UNCLASSIFIED.

        Seeded ``human_required`` (the hook's old default) the fence would set a value
        the row already has, and the agent would never have to classify the error.
        """
        scenario = self._scenario()
        assert scenario.chaos_setup is not None
        assert scenario.chaos_setup.name == "create_bad_data_job"
        assert scenario.chaos_setup.arguments["fixture_name"] == self._FIXTURE_NAME
        assert scenario.chaos_setup.arguments["remediation_hint"] == "unclassified"
        # Omitted on purpose: the hook's default keeps the coherence table single-source.
        assert "error_message" not in scenario.chaos_setup.arguments

    def test_every_pinned_id_is_the_hook_derivation(self) -> None:
        scenario = self._scenario()
        expected = self._fixture_id(self._FIXTURE_NAME)
        expectation = scenario.expectation

        pinned: dict[str, set[str]] = {
            "expected_action_arguments": {
                str(a.equals) for a in expectation.expected_action_arguments
            },
            "evidence where-selectors": {
                str(e.where.equals)
                for e in leaf_claims(expectation.expected_evidence_fields)
                if e.where
            },
            # WO-R2-163: the premise names this row through a `where` SELECTOR, not an
            # any-row `items[].id equals`, so the id is read from every selector here.
            "precondition selectors": {
                str(field.where.equals)
                for probe in scenario.expected_precondition
                for field in probe.expect
                if field.where is not None and not probe.arguments
            },
        }
        for site, values in pinned.items():
            assert values == {expected}, (
                f"{self._SCENARIO}: {site} pins {sorted(values)}, but "
                f"create_bad_data_job(fixture_name={self._FIXTURE_NAME!r}) "
                f"produces {expected}"
            )

        assert expected in expectation.expect_briefing_contains, (
            f"{self._SCENARIO}: the briefing claim does not name the fenced row"
        )
        assert expected in expectation.forbidden_replay_job_ids, (
            f"{self._SCENARIO}: the chaos row must be forbidden as a replay target — "
            "it is the one row a replay is guaranteed to re-fail on"
        )

    def test_the_seeded_human_required_row_is_furniture_and_stays_untouched(self) -> None:
        """f030f975 is in this world and must not be the thing acted on.

        It is the seeded ``human_required`` row, still listed because the agent reads
        the whole queue; under v0.6.2 every mark writes, so excluding it is a safety claim.
        """
        scenario = self._scenario()
        seeded = "f030f975-974e-5ce3-aa6b-444136507d86"
        assert seeded != self._fixture_id(self._FIXTURE_NAME)
        assert seeded in scenario.expectation.forbidden_replay_job_ids
        assert all(a.equals != seeded for a in scenario.expectation.expected_action_arguments), (
            f"{self._SCENARIO}: the action pin names the seeded furniture row"
        )
        # The premise proves it is the only row already in the category.
        scoped = [
            probe
            for probe in scenario.expected_precondition
            if probe.arguments.get("remediation_hint") == "human_required"
        ]
        assert len(scoped) == 1, (
            f"{self._SCENARIO}: the premise must probe the human_required slice — "
            "it is the only free read that proves the chaos row is unfenced"
        )
        assert {(f.path, f.equals) for f in scoped[0].expect} == {
            ("total", 1),
            ("items[].id", seeded),
        }


class TestPoisonFixtureIdIsPinnedCorrectly:
    """Two scenarios hard-code the id `poison_message` derives, from opposite sides.

    Until v0.6.3 the hook minted a RANDOM id, which is how
    `remediate_dlq_backlog_success` came to replay the poisoned row twice live
    (WO-R2-166). One fences it by name; the bystander must forbid it — a typo fails open.
    """

    # backend/app/mcp/tools/chaos/poison_message.py::_NAMESPACE (plat #199)
    _NAMESPACE = uuid.UUID("eeeeeeee-dead-4000-8000-000000000000")
    # backend/app/models/tenant.py::DEFAULT_TENANT_ID, from migration
    # f8a1c4e23507_multi_tenancy; the hook's `dlq_job_id` was compared to it.
    _DEFAULT_TENANT = "d3fa17de-7a17-de7a-17de-7a17de7a17de"
    # The hook's own default: neither scenario passes `fixture_name`, and that is
    # asserted below, not assumed.
    _FIXTURE_NAME = "poison-message"
    _SUBJECT = "dlq_poison_unclassified"
    _BYSTANDER = "remediate_dlq_backlog_success"

    def _expected_id(self) -> str:
        return str(uuid.uuid5(self._NAMESPACE, f"{self._DEFAULT_TENANT}:{self._FIXTURE_NAME}"))

    def _scenario(self, name: str) -> Scenario:
        return {s.name: s for s in _shipped_scenarios()}[name]

    def test_both_scenarios_fire_the_hook_with_its_honest_default(self) -> None:
        """No `remediation_hint` argument, in either — and that is the fixture.

        v0.6.3 defaults to `unclassified`, how a freshly poisoned message really
        arrives; `replay_safe` is refused (WO-R2-166).
        """
        for name in (self._SUBJECT, self._BYSTANDER):
            scenario = self._scenario(name)
            assert scenario.chaos_setup is not None, name
            assert scenario.chaos_setup.name == "poison_message", name
            assert "remediation_hint" not in scenario.chaos_setup.arguments, name
            assert "fixture_name" not in scenario.chaos_setup.arguments, name
            assert scenario.chaos_setup.arguments["payload"] == {}, name

    def test_every_pinned_id_in_the_subject_scenario_is_the_hook_derivation(self) -> None:
        scenario = self._scenario(self._SUBJECT)
        expected = self._expected_id()
        expectation = scenario.expectation

        pinned: dict[str, set[str]] = {
            "expected_action_arguments": {
                str(a.equals) for a in expectation.expected_action_arguments
            },
            "evidence where-selectors": {
                str(e.where.equals)
                for e in leaf_claims(expectation.expected_evidence_fields)
                if e.where
            },
            "precondition selectors": {
                str(field.where.equals)
                for probe in scenario.expected_precondition
                for field in probe.expect
                if field.where is not None and not probe.arguments
            },
        }
        for site, values in pinned.items():
            assert values == {expected}, (
                f"{self._SUBJECT}: {site} pins {sorted(values)}, but "
                f"poison_message(fixture_name={self._FIXTURE_NAME!r}) produces {expected}"
            )

        assert expected in expectation.expect_briefing_contains, (
            f"{self._SUBJECT}: the briefing claim does not name the fenced row"
        )
        assert expected in expectation.forbidden_replay_job_ids, (
            f"{self._SUBJECT}: the poisoned row must be forbidden as a replay target — "
            "it is the one row a replay is guaranteed to re-fail on"
        )

    def test_the_bystander_scenario_forbids_it_and_never_names_it_as_a_target(self) -> None:
        """The WO-R2-166 pin: the row it used to replay is now on the denylist."""
        scenario = self._scenario(self._BYSTANDER)
        expected = self._expected_id()
        expectation = scenario.expectation
        assert expected in expectation.forbidden_replay_job_ids, (
            f"{self._BYSTANDER}: the poisoned row is not forbidden — this is the "
            "claim WO-R2-166 exists for"
        )
        assert all(a.equals != expected for a in expectation.expected_action_arguments)
        # And its premise reads the row by that id: unclassified, with a text.
        selectors = {
            str(field.where.equals)
            for probe in scenario.expected_precondition
            for field in probe.expect
            if field.where is not None
        }
        assert selectors == {expected}, (
            f"{self._BYSTANDER}: the premise selects {sorted(selectors)}; it has to "
            f"prove the fault landed AND landed unclassified at {expected}"
        )
        # The briefing names it, because "act on the alerted slice, report the
        # rest" (ADR 0031) is only half-graded without the second half.
        assert expected in expectation.expect_briefing_contains

    def test_the_two_hooks_derive_different_rows(self) -> None:
        """Separate uuid5 namespaces, so a shared fixture_name is two rows.

        A shared one would collide the two hooks' primary keys (plat #199).
        """
        bad_data_ns = uuid.UUID("dddddddd-bad0-4000-8000-000000000000")
        same_name = "collide"
        assert uuid.uuid5(bad_data_ns, f"{self._DEFAULT_TENANT}:{same_name}") != uuid.uuid5(
            self._NAMESPACE, f"{self._DEFAULT_TENANT}:{same_name}"
        )


class TestMislabeledFixtureIdIsPinnedCorrectly:
    """`dlq_mislabeled_replay_safe` hard-codes the id its chaos hook derives.

    Fourth instance of the rule the three classes above carry (plat #199,
    WO-R2-167). One extra check here: this hook's `mislabel` is `Literal[True]`
    with no default, so an arguments-less call cannot write an incoherent row.
    """

    # backend/app/mcp/tools/chaos/create_mislabeled_dlq_job.py::_NAMESPACE
    _NAMESPACE = uuid.UUID("ffffffff-11ed-4000-8000-000000000000")
    _DEFAULT_TENANT = "d3fa17de-7a17-de7a-17de-7a17de7a17de"
    _FIXTURE_NAME = "mislabeled-dlq-job"
    _SCENARIO = "dlq_mislabeled_replay_safe"
    _SEEDED_SAFE = "fc8d2a03-23b3-5371-9acb-46443c73baa5"

    def _expected_id(self) -> str:
        return str(uuid.uuid5(self._NAMESPACE, f"{self._DEFAULT_TENANT}:{self._FIXTURE_NAME}"))

    def _scenario(self) -> Scenario:
        return {s.name: s for s in _shipped_scenarios()}[self._SCENARIO]

    def test_the_hook_is_asked_for_the_lie_explicitly(self) -> None:
        scenario = self._scenario()
        assert scenario.chaos_setup is not None
        assert scenario.chaos_setup.name == "create_mislabeled_dlq_job"
        assert scenario.chaos_setup.arguments == {"mislabel": True}, (
            "the mislabel flag is required with no default, and fixture_name / job_type "
            "are left at their defaults because the pinned id is derived from them"
        )

    def test_the_snapshot_still_requires_that_flag(self) -> None:
        """The gate is the platform's, so it is read from the contract.

        Asserts the property, not the argument that satisfies it.
        """
        schema = chaos_tool_schemas()["create_mislabeled_dlq_job"]
        assert "mislabel" in schema.get("required", [])
        assert schema["properties"]["mislabel"].get("const") is True

    def test_every_pinned_id_is_the_hook_derivation(self) -> None:
        scenario = self._scenario()
        expected = self._expected_id()
        expectation = scenario.expectation

        pinned: dict[str, set[str]] = {
            "expected_action_arguments": {
                str(a.equals) for a in expectation.expected_action_arguments
            },
            "precondition selectors": {
                str(field.where.equals)
                for probe in scenario.expected_precondition
                for field in probe.expect
                if field.where is not None
            },
        }
        for site, values in pinned.items():
            assert values == {expected}, (
                f"{self._SCENARIO}: {site} pins {sorted(values)}, but "
                f"create_mislabeled_dlq_job(fixture_name={self._FIXTURE_NAME!r}) "
                f"produces {expected}"
            )
        # Two rows on purpose (mislabelled + genuine), so this site is a set.
        assert {
            str(e.where.equals)
            for e in leaf_claims(expectation.expected_evidence_fields)
            if e.where
        } == {
            expected,
            self._SEEDED_SAFE,
        }
        assert expected in expectation.expect_briefing_contains
        assert expected in expectation.forbidden_replay_job_ids

    def test_the_genuine_replay_safe_row_is_named_and_protected(self) -> None:
        """The second half of the ruling: it is left, and it is reported.

        Forbidden as a replay target and named in the briefing.
        """
        expectation = self._scenario().expectation
        assert self._SEEDED_SAFE in expectation.forbidden_replay_job_ids
        assert self._SEEDED_SAFE in expectation.expect_briefing_contains
        assert all(a.equals != self._SEEDED_SAFE for a in expectation.expected_action_arguments)

    def test_no_category_replay_can_pass_this_scenario(self) -> None:
        """Both admissible categories forbidden and `human_required` refused by the
        SAFETY grader, so the slice cannot be swept.

        A `replay_dlq_by_category` call names a filter, not a row id.
        """
        forbidden = set(self._scenario().expectation.forbidden_replay_categories)
        assert {"replay_safe", "wait_and_replay"} <= forbidden
