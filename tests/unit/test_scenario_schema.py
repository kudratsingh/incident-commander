import pytest
from pydantic import ValidationError

from evals.graders.deterministic import ScenarioExpectation
from evals.scenarios.schema import ChaosHook, Scenario
from incident_commander.agent.state import IncidentState
from incident_commander.api.schemas import AlertPayload


class TestScenario:
    def test_minimal_scenario_validates(self) -> None:
        scenario = Scenario(
            name="s",
            alert=AlertPayload(source="billing"),
            expectation=ScenarioExpectation(
                name="s", expected_terminal_state=IncidentState.ESCALATED
            ),
        )
        assert scenario.name == "s"
        assert scenario.tags == ()
        assert scenario.description == ""

    def test_empty_name_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Scenario(
                name="",
                alert=AlertPayload(source="billing"),
                expectation=ScenarioExpectation(
                    name="s", expected_terminal_state=IncidentState.ESCALATED
                ),
            )

    def test_extra_fields_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Scenario.model_validate(
                {
                    "name": "s",
                    "alert": {"source": "billing"},
                    "expectation": {
                        "name": "s",
                        "expected_terminal_state": "escalated",
                    },
                    "unknown_key": "boom",
                }
            )

    def test_frozen_mutation_rejected(self) -> None:
        scenario = Scenario(
            name="s",
            alert=AlertPayload(source="billing"),
            expectation=ScenarioExpectation(
                name="s", expected_terminal_state=IncidentState.ESCALATED
            ),
        )
        with pytest.raises(ValidationError):
            scenario.name = "changed"

    def test_chaos_setup_defaults_to_none(self) -> None:
        scenario = Scenario(
            name="s",
            alert=AlertPayload(source="billing"),
            expectation=ScenarioExpectation(
                name="s", expected_terminal_state=IncidentState.ESCALATED
            ),
        )
        assert scenario.chaos_setup is None

    def test_chaos_setup_loads_from_yaml_shape(self) -> None:
        scenario = Scenario.model_validate(
            {
                "name": "s",
                "alert": {"source": "billing"},
                "expectation": {
                    "name": "s",
                    "expected_terminal_state": "resolved",
                },
                "chaos_setup": {
                    "name": "inject_latency",
                    "arguments": {"consumer_group": "wd", "latency_ms": 2000},
                },
            }
        )
        assert scenario.chaos_setup is not None
        assert scenario.chaos_setup.name == "inject_latency"
        assert scenario.chaos_setup.arguments["latency_ms"] == 2000


class TestChaosHook:
    def test_empty_name_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ChaosHook(name="")

    def test_extra_fields_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ChaosHook.model_validate({"name": "kill_consumer", "unexpected": 1})
