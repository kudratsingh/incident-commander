from datetime import datetime

import pytest
from pydantic import ValidationError

from incident_commander.tools.registry import (
    TOOL_REGISTRY,
    GetConsumerLagInput,
    GetConsumerLagOutput,
    ToolSpec,
)


class TestGetConsumerLagInput:
    def test_valid_group(self) -> None:
        model = GetConsumerLagInput(group="billing-consumer")
        assert model.group == "billing-consumer"

    def test_empty_group_rejected(self) -> None:
        with pytest.raises(ValidationError):
            GetConsumerLagInput(group="")

    def test_extra_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            GetConsumerLagInput.model_validate({"group": "x", "extra": "y"})


class TestGetConsumerLagOutput:
    def test_valid_payload(self, now: datetime) -> None:
        model = GetConsumerLagOutput(group="billing", lag=42, timestamp=now)
        assert model.lag == 42

    def test_negative_lag_rejected(self, now: datetime) -> None:
        with pytest.raises(ValidationError):
            GetConsumerLagOutput(group="billing", lag=-1, timestamp=now)

    def test_extra_field_ignored(self, now: datetime) -> None:
        model = GetConsumerLagOutput.model_validate(
            {"group": "billing", "lag": 0, "timestamp": now, "unknown_field": True}
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
