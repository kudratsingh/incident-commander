import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import pytest

from evals.scenarios.loader import ScenarioLoadError, load_scenario, load_scenarios
from evals.scenarios.schema import Scenario
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
    "saga_stuck": 11,
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
    it is waiting on: a stuck-DAG chaos hook for runaway_saga, a
    get_cache_key_info read tool for stale_cache, a seedable dead consumer
    group for verify_fails.
    """

    _CANNED_ONLY: frozenset[str] = frozenset(
        {
            "remediate_runaway_saga_success",
            "remediate_stale_cache_success",
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
