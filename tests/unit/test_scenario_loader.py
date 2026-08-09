from functools import lru_cache
from pathlib import Path

import pytest

from evals.scenarios.loader import ScenarioLoadError, load_scenario, load_scenarios
from evals.scenarios.schema import Scenario
from incident_commander.agent.state import IncidentState

_REPO_ROOT = Path(__file__).resolve().parents[2]
_EXAMPLE = _REPO_ROOT / "evals" / "scenarios" / "consumer_lag_high.yaml"


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
        assert scenario.alert.severity == "high"
        assert scenario.expectation.expected_terminal_state is IncidentState.ESCALATED
        assert "worker-dispatcher" in scenario.expectation.expected_evidence_contains
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
        scenarios = load_scenarios(_REPO_ROOT / "evals" / "scenarios")
        assert len(scenarios) >= 1
        assert any(s.name == "consumer_lag_high" for s in scenarios)


# Live polling profile (docs/runbook.md:152-154, ADR 0006 + ADR 0009):
# 2 investigation probes + 1 freshness re-probe + 1 Tier-1 action +
# up to 6 verify polls = 10 tool calls on a CORRECT remediation run.
# The maintainer set the graded ceiling at 13 (~30% margin, rule 1 of
# docs/eval-methodology.md). This is the GRADING cap read by
# evals/graders/deterministic.py:_grade_budget — not the runtime
# BudgetLedger ceiling of CLAUDE.md invariant 7.
_REMEDIATION_MIN_CAP = 13


@lru_cache(maxsize=1)
def _shipped_scenarios() -> tuple[Scenario, ...]:
    """Parse the shipped scenario directory once per session, not per assertion."""
    return tuple(load_scenarios(_REPO_ROOT / "evals" / "scenarios"))


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
