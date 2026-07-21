import pytest
from pydantic import ValidationError

from incident_commander.tools.registry import (
    TOOL_REGISTRY,
    GetConsumerLagInput,
    GetConsumerLagOutput,
    ToolSpec,
)


class TestGetConsumerLagInput:
    def test_default_group_matches_platform_default(self) -> None:
        model = GetConsumerLagInput()
        assert model.consumer_group == "worker-dispatcher"

    def test_explicit_group(self) -> None:
        model = GetConsumerLagInput(consumer_group="event-log")
        assert model.consumer_group == "event-log"

    def test_empty_group_rejected(self) -> None:
        with pytest.raises(ValidationError):
            GetConsumerLagInput(consumer_group="")

    def test_extra_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            GetConsumerLagInput.model_validate({"consumer_group": "x", "extra": "y"})


class TestGetConsumerLagOutput:
    def test_valid_payload(self) -> None:
        model = GetConsumerLagOutput(
            consumer_group="worker-dispatcher",
            lag=42,
            cache_key="kafka:consumer_lag:worker-dispatcher",
        )
        assert model.lag == 42

    def test_null_lag_accepted(self) -> None:
        # Platform returns null when cache is empty or expired.
        model = GetConsumerLagOutput(
            consumer_group="worker-dispatcher",
            lag=None,
            cache_key="(no cache key for group 'worker-dispatcher')",
        )
        assert model.lag is None

    def test_extra_field_ignored(self) -> None:
        model = GetConsumerLagOutput.model_validate(
            {
                "consumer_group": "worker-dispatcher",
                "lag": 0,
                "cache_key": "kafka:consumer_lag:worker-dispatcher",
                "unknown_field": True,
            }
        )
        assert model.lag == 0


class TestToolRegistry:
    def test_get_consumer_lag_present(self) -> None:
        assert "get_consumer_lag" in TOOL_REGISTRY
        spec = TOOL_REGISTRY["get_consumer_lag"]
        assert isinstance(spec, ToolSpec)
        assert spec.name == "get_consumer_lag"
        assert spec.input_model is GetConsumerLagInput
        assert spec.output_model is GetConsumerLagOutput
