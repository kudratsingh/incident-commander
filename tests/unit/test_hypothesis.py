import pytest
from pydantic import TypeAdapter, ValidationError

from incident_commander.agent.hypothesis import (
    Hypothesis,
    HypothesisCategory,
    InvestigationStep,
    NextAction,
    ProbeAction,
    RemediateAction,
    StopAction,
)


class TestHypothesisCategory:
    def test_enum_values_are_stable(self) -> None:
        # Adding a value is fine; renaming or removing one is a breaking
        # change to persisted trajectories and eval scenarios. If this test
        # fails, coordinate the rename across scenarios + baseline.
        assert set(HypothesisCategory) == {
            HypothesisCategory.CONSUMER_SATURATION,
            HypothesisCategory.POISON_MESSAGE,
            HypothesisCategory.STALE_CACHE,
            HypothesisCategory.RUNAWAY_SAGA,
            HypothesisCategory.TRANSIENT_DEPENDENCY,
            HypothesisCategory.PERSISTENT_DATA_BUG,
            HypothesisCategory.DEPLOY_REGRESSION,
            HypothesisCategory.UNKNOWN,
        }


class TestHypothesis:
    def test_valid_hypothesis(self) -> None:
        h = Hypothesis(
            category=HypothesisCategory.CONSUMER_SATURATION,
            name="consumer_lag",
            confidence=0.8,
            reasoning="lag observed",
        )
        assert h.name == "consumer_lag"
        assert h.category is HypothesisCategory.CONSUMER_SATURATION

    @pytest.mark.parametrize("confidence", [-0.1, 1.1, 2.0])
    def test_confidence_out_of_range_rejected(self, confidence: float) -> None:
        with pytest.raises(ValidationError):
            Hypothesis(
                category=HypothesisCategory.UNKNOWN,
                name="x",
                confidence=confidence,
                reasoning="r",
            )

    def test_empty_name_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Hypothesis(
                category=HypothesisCategory.UNKNOWN,
                name="",
                confidence=0.5,
                reasoning="r",
            )

    def test_empty_reasoning_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Hypothesis(
                category=HypothesisCategory.UNKNOWN,
                name="n",
                confidence=0.5,
                reasoning="",
            )

    def test_missing_category_rejected(self) -> None:
        # The whole point of the enum tightening — a hypothesis without a
        # structured category is a schema violation, not a "let's tolerate it".
        with pytest.raises(ValidationError):
            Hypothesis.model_validate({"name": "x", "confidence": 0.5, "reasoning": "r"})

    def test_invalid_category_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Hypothesis.model_validate(
                {
                    "category": "poison-message-bad-csv-data",  # what the live LLM invented
                    "name": "x",
                    "confidence": 0.5,
                    "reasoning": "r",
                }
            )


class TestProbeAction:
    def test_default_kind_is_probe(self) -> None:
        action = ProbeAction(tool_name="get_consumer_lag", arguments={"group": "billing"})
        assert action.kind == "probe"

    def test_unknown_tool_name_rejected(self) -> None:
        # ReadToolName is a Literal — schema enforces the allowed set.
        with pytest.raises(ValidationError):
            ProbeAction.model_validate({"tool_name": "made_up_tool", "arguments": {}})

    def test_tier_1_tool_name_rejected(self) -> None:
        # Investigation planner may only probe read tools. A Tier-1 tool
        # name is not in the ReadToolName Literal, so schema rejects it.
        with pytest.raises(ValidationError):
            ProbeAction.model_validate({"tool_name": "restart_consumer_group", "arguments": {}})

    def test_arguments_default_empty(self) -> None:
        action = ProbeAction(tool_name="get_consumer_lag")
        assert action.arguments == {}


class TestStopAction:
    def test_default_kind_is_stop(self) -> None:
        action = StopAction(reason="enough evidence")
        assert action.kind == "stop"

    def test_empty_reason_rejected(self) -> None:
        with pytest.raises(ValidationError):
            StopAction(reason="")


class TestRemediateAction:
    def test_default_kind_is_remediate(self) -> None:
        action = RemediateAction(reason="top hypothesis confirmed with fix")
        assert action.kind == "remediate"

    def test_empty_reason_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RemediateAction(reason="")


class TestNextActionDiscriminator:
    _adapter: TypeAdapter[NextAction] = TypeAdapter(NextAction)

    def test_dispatches_probe(self) -> None:
        result = self._adapter.validate_python(
            {"kind": "probe", "tool_name": "get_consumer_lag", "arguments": {}}
        )
        assert isinstance(result, ProbeAction)

    def test_dispatches_stop(self) -> None:
        result = self._adapter.validate_python({"kind": "stop", "reason": "done"})
        assert isinstance(result, StopAction)

    def test_dispatches_remediate(self) -> None:
        result = self._adapter.validate_python(
            {"kind": "remediate", "reason": "consumer_saturation confirmed"}
        )
        assert isinstance(result, RemediateAction)

    def test_unknown_kind_rejected(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter.validate_python({"kind": "wat", "tool_name": "x"})

    def test_missing_kind_rejected(self) -> None:
        with pytest.raises(ValidationError):
            self._adapter.validate_python({"tool_name": "x"})


def _valid_hypothesis_dict() -> dict[str, object]:
    return {
        "category": HypothesisCategory.CONSUMER_SATURATION.value,
        "name": "a",
        "confidence": 0.9,
        "reasoning": "r",
    }


class TestInvestigationStep:
    def test_valid_with_probe(self) -> None:
        step = InvestigationStep(
            hypotheses=(
                Hypothesis(
                    category=HypothesisCategory.CONSUMER_SATURATION,
                    name="a",
                    confidence=0.9,
                    reasoning="r",
                ),
            ),
            next_action=ProbeAction(tool_name="get_consumer_lag"),
        )
        assert step.hypotheses[0].name == "a"

    def test_valid_with_stop(self) -> None:
        step = InvestigationStep(
            hypotheses=(
                Hypothesis(
                    category=HypothesisCategory.UNKNOWN,
                    name="a",
                    confidence=0.9,
                    reasoning="r",
                ),
            ),
            next_action=StopAction(reason="enough"),
        )
        assert isinstance(step.next_action, StopAction)

    def test_empty_hypotheses_rejected(self) -> None:
        with pytest.raises(ValidationError):
            InvestigationStep(
                hypotheses=(),
                next_action=StopAction(reason="x"),
            )

    def test_extra_fields_rejected(self) -> None:
        with pytest.raises(ValidationError):
            InvestigationStep.model_validate(
                {
                    "hypotheses": [_valid_hypothesis_dict()],
                    "next_action": {"kind": "stop", "reason": "x"},
                    "extra_field": "boom",
                }
            )

    def test_next_action_coerced_from_json_string(self) -> None:
        """Anthropic tool-use sometimes emits nested oneOf fields as JSON strings."""
        step = InvestigationStep.model_validate(
            {
                "hypotheses": [_valid_hypothesis_dict()],
                "next_action": (
                    '{"kind": "probe", "tool_name": "get_consumer_lag", '
                    '"arguments": {"consumer_group": "worker-dispatcher"}}'
                ),
            }
        )
        assert isinstance(step.next_action, ProbeAction)
        assert step.next_action.tool_name == "get_consumer_lag"

    def test_next_action_coerced_from_json_string_stop(self) -> None:
        step = InvestigationStep.model_validate(
            {
                "hypotheses": [_valid_hypothesis_dict()],
                "next_action": '{"kind": "stop", "reason": "done"}',
            }
        )
        assert isinstance(step.next_action, StopAction)
        assert step.next_action.reason == "done"


class TestInvestigationStepOrdering:
    """B-07: ranking is normalized at the schema boundary.

    Three gates read ``hypotheses[0]`` as the top pick (remediate gate,
    ADR-0009 reprobe prior, remediation-planner target). The validator
    guarantees index 0 is the highest-confidence hypothesis regardless
    of the order the model listed them in.
    """

    def _hyp(self, category: str, name: str, confidence: float) -> dict[str, object]:
        return {
            "category": category,
            "name": name,
            "confidence": confidence,
            "reasoning": "r",
        }

    def test_unordered_input_sorted_confidence_descending(self) -> None:
        step = InvestigationStep.model_validate(
            {
                "hypotheses": [
                    self._hyp("consumer_saturation", "saturation", 0.5),
                    self._hyp("deploy_regression", "regression", 0.9),
                ],
                "next_action": {"kind": "stop", "reason": "x"},
            }
        )
        assert step.hypotheses[0].confidence == 0.9
        assert step.hypotheses[0].category is HypothesisCategory.DEPLOY_REGRESSION
        assert [h.confidence for h in step.hypotheses] == [0.9, 0.5]

    def test_equal_confidence_preserves_model_order(self) -> None:
        # Stability contract: ties keep the model's stated order. A future
        # "cleanup" to a non-stable sorting scheme must fail here.
        step = InvestigationStep.model_validate(
            {
                "hypotheses": [
                    self._hyp("stale_cache", "first-tie", 0.7),
                    self._hyp("poison_message", "top", 0.9),
                    self._hyp("unknown", "second-tie", 0.7),
                ],
                "next_action": {"kind": "stop", "reason": "x"},
            }
        )
        assert [h.name for h in step.hypotheses] == ["top", "first-tie", "second-tie"]
