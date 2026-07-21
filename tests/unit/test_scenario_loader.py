from pathlib import Path

import pytest

from evals.scenarios.loader import ScenarioLoadError, load_scenario, load_scenarios
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
