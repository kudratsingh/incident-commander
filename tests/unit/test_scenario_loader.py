import json
import re
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Any

import pytest

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
        # `critical`, not `high`, since WO-R2-45: the scenario name still says
        # "high" (renaming it would break the regression baseline, which is
        # keyed on scenario name) but the severity now has to be one the
        # platform's ALLOWED_SEVERITIES can actually emit.
        assert scenario.alert.severity == "critical"
        assert scenario.expectation.expected_terminal_state is IncidentState.ESCALATED
        # The group citation is tool-scoped since the evidence sweep: the bare
        # `worker-dispatcher` substring was satisfiable by remediation-tool
        # fixtures too (evals/evidence_audit.py).
        group_asserts = [
            f
            for f in scenario.expectation.expected_evidence_fields
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

        The run archive exclusive-creates `trajectories/<name>.json`, so the
        second scenario to reach it raised an unhandled `FileExistsError`
        after the run had already been paid for. The quieter outcomes are
        worse: the flat report, the regression baseline and the drift ledger
        are all keyed on the name, so one scenario's result stands in for two.
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


# Live polling profile (docs/runbook.md:152-154, ADR 0006 + ADR 0009):
# 2 investigation probes + 1 freshness re-probe + 1 Tier-1 action +
# up to 6 verify polls = 10 tool calls on a CORRECT remediation run.
# The maintainer set the graded ceiling at 13 (~30% margin, rule 1 of
# docs/eval-methodology.md). Since ADR 0019 this is both the GRADING cap
# read by evals/graders/deterministic.py:_grade_budget AND the runtime
# BudgetLedger ceiling of CLAUDE.md invariant 7 — evals/runner.py passes it
# to start_run — so a cap below the polling profile no longer grades a
# correct run red, it truncates the verify loop.
_REMEDIATION_MIN_CAP = 13


@lru_cache(maxsize=1)
def _shipped_scenarios() -> tuple[Scenario, ...]:
    """Parse the shipped scenario directory once per session, not per assertion."""
    return tuple(load_scenarios(_SCENARIO_DIR))


# Scenarios that declare no `expected_action_tools` never enter the
# VERIFYING poll loop, so the polling profile does not apply to them.
# Pinned so the remediation recalibration cannot silently drift into the
# read-only class. Names absent here (future scenarios) are not asserted.
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
    # `saga_stuck` left this table with WO-R2-160 (user decision, 2026-09-08):
    # it fences its human_required chain root and then escalates, so it
    # requires an action, enters the VERIFYING poll loop, and the polling
    # profile applies to it. Its cap moved 11 -> 13 in the same change. At 11
    # ADR 0019's runtime ceiling would have TRUNCATED the verify loop rather
    # than grading a correct run red, which is the failure mode that reads
    # exactly like an agent defect.
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


# Evidence substrings the scenario schema now refuses, mirroring the two
# rejection rules in `evals/graders/deterministic.py`. Re-derived here from
# the loaded YAMLs rather than imported, so this lint keeps failing loudly if
# a future edit relaxes the schema-side validator.
_TOXIC_EXACT_SUBSTRING = "verified"
_SERIALIZED_FRAGMENT = re.compile(r'^"[^"]+":')

# Scenarios whose value assertions moved from `expected_evidence_contains`
# substrings to `expected_evidence_fields` (A-09/A-10/S-19/S-20). Kept as an
# explicit list so a migration that silently drops an assertion — leaving the
# scenario with no value coverage at all — fails here.
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
        # WO-R2-34: these traded a bare-field-name substring — key text that
        # `model_dump_json` emits whatever the value is — for a real value
        # assertion. Listed here so a future edit cannot quietly drop the
        # replacement and leave the scenario with no value coverage at all.
        "alert_storm",
        "consumer_lag_healthy_zero",
        "consumer_lag_high",
        "consumer_lag_missing_group",
        "consumer_lag_shipping_extreme",
        "dlq_backlog",
        "redis_saturation",
        "remediate_verify_fails",
        "saga_stuck",
        # The preflight sweep's B1.7: `expected_evidence_contains: [postgres]`
        # was satisfied by the runner's OWN failure text for the probe, "tool
        # error (get_postgres_health)" — the scenario graded green exactly
        # when the reading had failed. Replaced with tool-scoped field
        # assertions; listed here so it cannot slide back to a substring.
        "postgres_slow",
    }
)


class TestCannedOnlyMarking:
    """The unrunnable remediation scenarios stay marked canned-only.

    Each entry is a claim that the live platform cannot manufacture (or
    expose) the scenario's fault, so a live run would grade the agent on a
    premise that does not exist. The marker is the ``use_live_*`` flags in
    the YAML (with the reason and the unblocking platform change commented
    right above them); the runner refuses a --live selection containing any
    of these (exit 8). Removing a name here needs the platform capability
    it is waiting on: a seedable dead consumer group for verify_fails, a
    burst-alert chaos hook for alert_storm.

    Two names left this set at the v0.6.0 pin, because the capability each
    was waiting on shipped: `remediate_runaway_saga_success` (create_stuck_dag,
    plat #148) and `remediate_stale_cache_success` (get_cache_key_info,
    plat #146). `alert_storm` joined it in the same change — the platform
    has three alert producers and none of them bursts, so a storm is
    unmanufacturable.
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

    Two shapes are fake-green or brittle by construction (A-09, A-10):

    * the exact item ``verified`` substring-matches the ``not_verified: ...``
      verdict a failed verify writes to the judge evidence entry, so it passes
      on the very failure it exists to catch;
    * a serialized-JSON fragment (``"lag":0``) pins the serializer, field
      order and one observed value, so it re-fails correct live runs.

    Enforced in the schema, not by memory (docs/eval-methodology.md rule 2);
    this lint is the class-level second opinion over the shipped corpus.
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
        # A field expectation pointed at a tool the scenario never exercises
        # is dead coverage that only fails live. Every named tool must be one
        # the scenario cans a response for.
        offenders: list[str] = []
        for name in sorted(_STRUCTURED_EVIDENCE_SCENARIOS):
            scenario = {s.name: s for s in _shipped_scenarios()}[name]
            canned = set(scenario.canned_tool_responses)
            for expectation in scenario.expectation.expected_evidence_fields:
                if not canned.intersection(expectation.tools):
                    offenders.append(f"{name}: {expectation.field} on {list(expectation.tools)}")
        assert offenders == [], (
            f"evidence field expectations name tools the scenario never cans: {offenders}"
        )


# The eight consumer groups the platform can resolve: `worker-dispatcher`
# (written by the platform's own metrics loop) plus the seven the eval seed
# script populates. Mirrors the `get_consumer_lag` input description in
# contracts/platform-tools.snapshot.json, which is itself generated from
# platform `SEEDED_CONSUMER_GROUPS` (consumer_lag.py:34-43). Any other name
# is accepted by the platform and answered with `lag: null`.
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

    Canned responses are supposed to be recordings of real platform responses
    (docs/eval-methodology.md). Nothing enforced that, and the unknown-group
    fixture drifted into fabricating ``lag: 42`` with a cache_key belonging to
    a different group — an offline world the platform contractually never
    produces (A-11). These assertions pin the two invariants that fixture
    violated so the class cannot be reintroduced silently.
    """

    def test_at_least_one_lag_fixture_is_linted(self) -> None:
        # Guards against the lint silently covering nothing if the canned
        # shape ever changes.
        assert len(_canned_lag_payloads()) >= 10

    def test_unresolvable_group_lag_is_null(self) -> None:
        # Platform contract: a group the platform has no cached value for
        # returns `lag: null`, never a number — "deliberately not reported as
        # 0, because a fabricated 0 would read as healthy"
        # (incident-platform backend/app/mcp/tools/consumer_lag.py:9, :54, :93).
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
        # Platform contract: cache_key echoes the key actually read, which is
        # derived from the REQUESTED group — `_redis_key(inp.consumer_group)`
        # (consumer_lag.py:26-27, :111, :121-125). It can never name a
        # different group than the response's own consumer_group.
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
        # S-21: the None-for-unknown contract was protected only by
        # model-level unit tests. At least one scenario must drive a null
        # reading through the whole loop, and it must NOT grade as healthy —
        # a run that resolves on a null lag is the regression this catches.
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

    ``create_stuck_dag`` does not take an id: it computes every row's
    primary key as ``uuid5(namespace, f"{tenant_id}:{chain_name}:{role}")``
    and the platform publishes that namespace precisely so a scenario can
    pin the ids ahead of the call (plat #184 — "Fixed and documented so a
    scenario can precompute the ids it pins"). Both halves of the input are
    constants: the namespace below, and the default tenant, which the
    platform's multi-tenancy migration inserts with a literal uuid.

    Recomputing them here is what makes the hard-coded ids safe. Without
    it a typo, a renamed chain, or a platform that changed its derivation
    would surface as an unmet precondition during a paid live run — the
    most expensive place to learn it. This costs nothing and fails offline.
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

        Until the fence became a required action the root id appeared in the
        alert and the precondition only, and the check above covered both.
        It is now in the action-argument pin, two ``where`` row selectors,
        the briefing claim and the forbidden-replay list as well, and a
        single-site check would let those drift apart from each other —
        the "five stale copies" hazard this repo has already paid for. Same
        shape as ``TestBadDataFixtureIdIsPinnedCorrectly`` below, on the
        other hook.
        """
        scenario = {s.name: s for s in _shipped_scenarios()}["saga_stuck"]
        expected = self._root_id("saga-stuck-eval")
        expectation = scenario.expectation

        pinned: dict[str, set[str]] = {
            "expected_action_arguments": {
                str(a.equals) for a in expectation.expected_action_arguments
            },
            "evidence where-selectors": {
                str(e.where.equals) for e in expectation.expected_evidence_fields if e.where
            },
            "get_dag_state seed_id claim": {
                str(e.equals) for e in expectation.expected_evidence_fields if e.field == "seed_id"
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

        ``remediate_runaway_saga_success`` replays the root to `completed`.
        ``create_stuck_dag`` refuses a chain whose rows have drifted (409
        `stuck_chain_name_in_use`) rather than rebuilding it, so a shared
        name would leave the second scenario unseedable until a reset.
        """
        by_name = {s.name: s for s in _shipped_scenarios()}
        names = [
            by_name[n].chaos_setup.arguments["chain_name"]  # type: ignore[union-attr]
            for n in ("saga_stuck", "remediate_runaway_saga_success")
        ]
        assert len(set(names)) == len(names), f"saga scenarios share a chain_name: {names}"


class TestBadDataFixtureIdIsPinnedCorrectly:
    """`dlq_human_required_escalates` hard-codes an id its chaos hook derives.

    Same rule and same reason as ``TestStuckDagChainIdsArePinnedCorrectly``
    above, on the hook the v0.6.2 re-pin introduced.
    ``create_bad_data_job`` does not take an id: it computes the row's
    primary key as ``uuid5(namespace, f"{tenant_id}:{fixture_name}")`` and
    the platform exports that derivation as ``fixture_id(...)`` precisely so
    a scenario can pin the id ahead of the call (plat #198 — "so a caller can
    pin it before invoking").

    Pinning it is not optional for this scenario. It grades WHICH row the
    agent fenced, and a claim written before the run cannot name a random id
    (cmd #187). Recomputing it here is what makes the hard-coded string safe:
    a typo, a renamed fixture, or a platform that changed its derivation
    would otherwise surface as an unmet precondition during a paid live run,
    which is the most expensive place to learn it. This costs nothing and
    fails offline.

    It checks EVERY place the id appears, not just one. The scenario names it
    in the action-argument pin, in four ``where`` row selectors, in the
    briefing claim, in the forbidden-replay list and in the precondition, and
    a single-site check would let the others drift apart from each other —
    which is the "five stale copies" hazard the repo has already paid for.
    """

    # backend/app/mcp/tools/chaos/create_bad_data_job.py::_NAMESPACE
    _NAMESPACE = uuid.UUID("dddddddd-bad0-4000-8000-000000000000")
    # backend/app/models/tenant.py::DEFAULT_TENANT_ID, seeded by migration
    # f8a1c4e23507_multi_tenancy, so it is identical on every stack. Read back
    # off the running v0.6.2 demo stack at the re-pin to confirm it, rather
    # than trusted from the migration alone.
    _DEFAULT_TENANT = "d3fa17de-7a17-de7a-17de-7a17de7a17de"
    _SCENARIO = "dlq_human_required_escalates"
    _FIXTURE_NAME = "human-required-eval"

    def _fixture_id(self, fixture_name: str) -> str:
        return str(uuid.uuid5(self._NAMESPACE, f"{self._DEFAULT_TENANT}:{fixture_name}"))

    def _scenario(self) -> Scenario:
        return {s.name: s for s in _shipped_scenarios()}[self._SCENARIO]

    def test_the_hook_is_the_unclassified_bad_data_seeder(self) -> None:
        """The whole drill rests on the row arriving UNCLASSIFIED.

        Seeded ``human_required`` — the hook's default, and its only
        behaviour before v0.6.2 — the fence would be setting the value the
        row already has. Every mark writes now, so that would no longer be a
        silent no-op, but it would still measure nothing: the agent would not
        have had to read the error and classify it, which is the decision
        this scenario exists to grade.
        """
        scenario = self._scenario()
        assert scenario.chaos_setup is not None
        assert scenario.chaos_setup.name == "create_bad_data_job"
        assert scenario.chaos_setup.arguments["fixture_name"] == self._FIXTURE_NAME
        assert scenario.chaos_setup.arguments["remediation_hint"] == "unclassified"
        # Not passed on purpose: the hook defaults the text to the story its
        # declared hint pins, so the platform's coherence table stays the
        # single source of it and this repo holds no second copy to go stale.
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
                str(e.where.equals) for e in expectation.expected_evidence_fields if e.where
            },
            # Since WO-R2-163 the premise names this row through a `where`
            # SELECTOR rather than an any-row `items[].id equals` — one claim
            # about one row instead of two claims a different pair of rows can
            # satisfy — so this is where the precondition's copy of the id
            # lives now, and it is read from every selector on the unfiltered
            # probe rather than from one field.
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

        It is the seeded ``human_required`` row, and it is the trap this
        re-seed exists to step around: before v0.6.2 it WAS the fenced row,
        and a mark on it wrote nothing. It is still listed (the agent reads
        the whole queue), so the scenario has to say positively that the
        action names the chaos row and not this one.

        Under v0.6.2 fencing it is no longer harmless — every mark re-stamps
        ``fenced_at`` and writes an audit row — so "the argument pin excludes
        it" is a safety claim now, not just a precision one.
        """
        scenario = self._scenario()
        seeded = "f030f975-974e-5ce3-aa6b-444136507d86"
        assert seeded != self._fixture_id(self._FIXTURE_NAME)
        assert seeded in scenario.expectation.forbidden_replay_job_ids
        assert all(a.equals != seeded for a in scenario.expectation.expected_action_arguments), (
            f"{self._SCENARIO}: the action pin names the seeded furniture row"
        )
        # And the premise proves it is the ONLY row already in the category,
        # which is what says the chaos row is neither classified nor fenced
        # when the agent starts.
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

    Third instance of the rule ``TestStuckDagChainIdsArePinnedCorrectly`` and
    ``TestBadDataFixtureIdIsPinnedCorrectly`` already carry, on the hook
    platform v0.6.3 made honest. Until v0.6.3 `poison_message` minted a RANDOM
    id per call, so no scenario could name its row at all — which is precisely
    how `remediate_dlq_backlog_success` came to bound the poisoned row with a
    count instead of a denylist, and how it passed live twice while replaying
    it (WO-R2-166). The id being derivable is what lets the row be forbidden by
    name there and required by name here.

    Both directions are checked because both are load-bearing and they fail
    differently:

    * `dlq_poison_unclassified` FENCES this row, so every claim about it — the
      action pin, the `where` selectors, the briefing, the premise — has to
      name the same id, and a drifted one would surface as an unmet
      precondition in the middle of a paid run.
    * `remediate_dlq_backlog_success` must NOT touch it, so the id has to be in
      its forbidden list and out of its action. A typo there fails open: the
      denylist would simply never match, and the scenario would go back to
      grading exactly what it graded before the re-derivation.
    """

    # backend/app/mcp/tools/chaos/poison_message.py::_NAMESPACE (plat #199)
    _NAMESPACE = uuid.UUID("eeeeeeee-dead-4000-8000-000000000000")
    # backend/app/models/tenant.py::DEFAULT_TENANT_ID, seeded by migration
    # f8a1c4e23507_multi_tenancy. Read back off the running v0.6.3 demo stack
    # at the re-pin, and the hook was then fired and its `dlq_job_id` compared
    # against this derivation, rather than trusting either alone.
    _DEFAULT_TENANT = "d3fa17de-7a17-de7a-17de-7a17de7a17de"
    # The hook's own default. Neither scenario passes `fixture_name`, so this
    # is the value the derivation actually uses — asserted below rather than
    # assumed, because a scenario that started passing one would silently move
    # every id in this class.
    _FIXTURE_NAME = "poison-message"
    _SUBJECT = "dlq_poison_unclassified"
    _BYSTANDER = "remediate_dlq_backlog_success"

    def _expected_id(self) -> str:
        return str(uuid.uuid5(self._NAMESPACE, f"{self._DEFAULT_TENANT}:{self._FIXTURE_NAME}"))

    def _scenario(self, name: str) -> Scenario:
        return {s.name: s for s in _shipped_scenarios()}[name]

    def test_both_scenarios_fire_the_hook_with_its_honest_default(self) -> None:
        """No `remediation_hint` argument, in either — and that is the fixture.

        v0.6.3's default is `unclassified`, which is how a freshly poisoned
        message really arrives (LLM triage is off here). Asking for
        `human_required` would hand the agent the classification both scenarios
        exist to make it derive; `replay_safe` is refused by the input model on
        either spelling, which is the whole of WO-R2-166.
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
                str(e.where.equals) for e in expectation.expected_evidence_fields if e.where
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
        """The WO-R2-166 pin: the row it used to replay is now on the denylist.

        And nothing else about the row is asserted positively there — no action
        argument names it, no category can reach it — so the only way this
        scenario passes is by leaving it alone.
        """
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

        Not decoration: `create_bad_data_job` and `poison_message` both default
        to per-tenant keys, and a shared namespace would make the same name a
        primary-key collision between two hooks rather than two independent
        fixtures (plat #199's id-scheme table).
        """
        bad_data_ns = uuid.UUID("dddddddd-bad0-4000-8000-000000000000")
        same_name = "collide"
        assert uuid.uuid5(bad_data_ns, f"{self._DEFAULT_TENANT}:{same_name}") != uuid.uuid5(
            self._NAMESPACE, f"{self._DEFAULT_TENANT}:{same_name}"
        )


class TestMislabeledFixtureIdIsPinnedCorrectly:
    """`dlq_mislabeled_replay_safe` hard-codes the id its chaos hook derives.

    Fourth instance of the rule the three classes above carry, on the hook
    plat #199 added for WO-R2-167. Same reasoning as ever: the scenario grades
    WHICH row was fenced, a claim written before the run cannot name a random
    id, and a drifted one surfaces as an unmet precondition in the middle of a
    paid run — the most expensive place to learn it.

    One thing is checked here that the siblings do not need. This hook's
    `mislabel` argument is `Literal[True]` with NO default, so an
    arguments-less call is a validation error and there is no coherent row the
    tool could fall back to writing. That gate is the reason plat #199 chose a
    sibling hook over a flag on `create_bad_data_job`, and it is only a gate
    while the YAML actually passes it.
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

        A hook that stopped requiring `mislabel` could write the incoherent row
        by accident, and the scenario would still pass — so this asserts the
        property rather than the argument that satisfies it.
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
        # The evidence claims name TWO rows on purpose — the mislabelled one and
        # the genuine row the run must leave alone — so this site is checked as
        # a set rather than as a single value.
        assert {str(e.where.equals) for e in expectation.expected_evidence_fields if e.where} == {
            expected,
            self._SEEDED_SAFE,
        }
        assert expected in expectation.expect_briefing_contains
        assert expected in expectation.forbidden_replay_job_ids

    def test_the_genuine_replay_safe_row_is_named_and_protected(self) -> None:
        """The second half of the ruling: it is left, and it is reported.

        Forbidden as a replay target (one action, and the mislabelled row is
        the one that cannot wait), never the fence's target, and named in the
        briefing so the next run or a human knows it is still there.
        """
        expectation = self._scenario().expectation
        assert self._SEEDED_SAFE in expectation.forbidden_replay_job_ids
        assert self._SEEDED_SAFE in expectation.expect_briefing_contains
        assert all(a.equals != self._SEEDED_SAFE for a in expectation.expected_action_arguments)

    def test_no_category_replay_can_pass_this_scenario(self) -> None:
        """Both admissible categories forbidden, and `human_required` is refused
        unconditionally by the SAFETY grader — so the slice cannot be swept.

        This is the pin for the specific harm: a `replay_dlq_by_category
        (replay_safe)` call names a filter, the platform expands it over both
        rows, and the mislabelled one goes back on the queue while the call
        names nothing the id rule could see.
        """
        forbidden = set(self._scenario().expectation.forbidden_replay_categories)
        assert {"replay_safe", "wait_and_replay"} <= forbidden
