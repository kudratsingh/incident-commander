import pytest
from pydantic import TypeAdapter, ValidationError

from incident_commander.agent.hypothesis import (
    SETTLED_CHOICE_DESCRIPTION,
    Hypothesis,
    HypothesisCategory,
    InvestigationStep,
    NextAction,
    ProbeAction,
    RemediateAction,
    StopAction,
    asked_for_a_probe,
    without_probe,
)
from incident_commander.llm.client import LLMOutputError

#: The eight values the enum shipped with, and their exact spellings. A literal table
#: rather than a derivation, because what it protects is that these strings never move.
_ORIGINAL_EIGHT: dict[str, str] = {
    "CONSUMER_SATURATION": "consumer_saturation",
    "POISON_MESSAGE": "poison_message",
    "STALE_CACHE": "stale_cache",
    "RUNAWAY_SAGA": "runaway_saga",
    "TRANSIENT_DEPENDENCY": "transient_dependency",
    "PERSISTENT_DATA_BUG": "persistent_data_bug",
    "DEPLOY_REGRESSION": "deploy_regression",
    "UNKNOWN": "unknown",
}

#: The nine WP-1.6 additions (plan 02 § 5). All are outside ``FIX_MAP``, asserted in
#: ``test_policies.py::TestEveryNewCategoryIsEscalateOnly``.
_WP_1_6_ADDITIONS: dict[str, str] = {
    "NO_FAULT": "no_fault",
    "OUTBOX_STALL": "outbox_stall",
    "RESOLVER_STALL": "resolver_stall",
    "SAGA_COORDINATOR_STALL": "saga_coordinator_stall",
    "DB_QUERY_LATENCY": "db_query_latency",
    "DB_POOL_SATURATION": "db_pool_saturation",
    "DOWNSTREAM_DEPENDENCY": "downstream_dependency",
    "REDIS_SATURATION": "redis_saturation",
    "READ_MODEL_DRIFT": "read_model_drift",
}

#: WO-R3-263's addition (O-19, ADR 0054), its own table because a label's provenance is what
#: a reader wants: a gap the ground-truth pass found on ``trace_investigation``.
_WO_R3_263_ADDITION: dict[str, str] = {
    "RESOURCE_EXHAUSTION": "resource_exhaustion",
}

#: WO-R3-214's addition (WP-7.2, ADR 0053), its own table for the same reason: a chain held
#: by a DAG pause is not broken, so the other three labels would misdirect a human.
_WO_R3_214_ADDITION: dict[str, str] = {
    "DAG_PAUSED": "dag_paused",
}


class TestHypothesisCategory:
    def test_enum_values_are_stable(self) -> None:
        # Adding a value is fine; a rename breaks persisted trajectories and scenarios.
        assert {member.name: member.value for member in HypothesisCategory} == {
            **_ORIGINAL_EIGHT,
            **_WP_1_6_ADDITIONS,
            **_WO_R3_263_ADDITION,
            **_WO_R3_214_ADDITION,
        }

    @pytest.mark.parametrize(("name", "value"), sorted(_ORIGINAL_EIGHT.items()))
    def test_an_original_value_is_untouched(self, name: str, value: str) -> None:
        """WP-1.6's first rule: the existing eight keep their values.

        Per member rather than one set comparison, so a rename names the member it broke.
        """
        assert HypothesisCategory[name].value == value

    @pytest.mark.parametrize(("name", "value"), sorted(_WP_1_6_ADDITIONS.items()))
    def test_a_new_category_exists_with_its_planned_value(self, name: str, value: str) -> None:
        """The nine labels plan 02 § 5 names, spelled as it names them.

        The root-cause grader is written against them.
        """
        assert HypothesisCategory[name].value == value

    @pytest.mark.parametrize(("name", "value"), sorted(_WO_R3_263_ADDITION.items()))
    def test_the_taxonomy_gap_label_exists_with_its_decided_value(
        self, name: str, value: str
    ) -> None:
        """The spelling O-19 decided, which ``trace_investigation`` now declares.

        A drifted value fails the corpus as "unknown category", which says less.
        """
        assert HypothesisCategory[name].value == value

    def test_no_fault_is_the_only_category_that_is_not_a_fault(self) -> None:
        """The level-0 control's label, named as its own thing.

        ``NO_FAULT`` can never gain a Tier-1 fix: there is nothing to act on.
        """
        assert HypothesisCategory.NO_FAULT.value == "no_fault"
        assert HypothesisCategory.NO_FAULT not in set(_ORIGINAL_EIGHT.values())


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

    Three gates read ``hypotheses[0]`` as the top pick, whatever order the model listed.
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


class TestWithoutProbe:
    """ADR 0074: the step schema with ``probe`` withdrawn, and how it says so.

    The structural half of F1. ADR 0073's refusal arrived AFTER the planner had chosen, which
    left "probe a different tool" open — and the third live take took it twice. A model that is
    never offered the move cannot take it, so the narrowing is in the schema the call is made
    with.
    """

    def test_the_narrowed_schema_offers_two_moves_and_no_probe(self) -> None:
        schema = without_probe(InvestigationStep).model_json_schema()
        next_action = schema["properties"]["next_action"]

        assert set(next_action["discriminator"]["mapping"]) == {"remediate", "stop"}
        # Not merely undiscriminated: the probe's own definition is gone, so nothing in the
        # schema describes the shape of a move the model is not offered.
        assert "ProbeAction" not in schema.get("$defs", {})

    def test_the_narrowed_schema_says_why_on_the_field(self) -> None:
        """The reason travels in the schema, not in a class docstring.

        A docstring on a model whose schema reaches a prompt is a silent schema change
        (CLAUDE.md); a field description is the deliberate one. The trail carries the same
        reason in the loop's own words, so a model reading either is told the same thing.
        """
        next_action = without_probe(InvestigationStep).model_json_schema()["properties"][
            "next_action"
        ]

        assert next_action["description"] == SETTLED_CHOICE_DESCRIPTION
        assert "no probe" in SETTLED_CHOICE_DESCRIPTION
        assert "what you would need to SEE to act" in SETTLED_CHOICE_DESCRIPTION

    def test_it_is_the_same_model_otherwise(self) -> None:
        """A subclass, so every gate that reads a step still reads this one.

        The ranking validator included: three gates read ``hypotheses[0]`` as the top pick, and
        a narrowed step that skipped the sort would hand them an unsorted list.
        """
        narrowed = without_probe(InvestigationStep)
        step = narrowed.model_validate(
            {
                "hypotheses": [
                    {
                        "category": "unknown",
                        "name": "second",
                        "confidence": 0.2,
                        "reasoning": "r",
                    },
                    {
                        "category": "consumer_saturation",
                        "name": "first",
                        "confidence": 0.9,
                        "reasoning": "r",
                    },
                ],
                "next_action": {"kind": "remediate", "reason": "act"},
            }
        )

        assert issubclass(narrowed, InvestigationStep)
        assert isinstance(step, InvestigationStep)
        assert step.hypotheses[0].name == "first"
        assert isinstance(step.next_action, RemediateAction)

    def test_a_probe_is_refused_by_the_narrowed_schema(self) -> None:
        with pytest.raises(ValidationError) as raised:
            without_probe(InvestigationStep).model_validate(
                {
                    "hypotheses": [
                        {
                            "category": "consumer_saturation",
                            "name": "x",
                            "confidence": 0.9,
                            "reasoning": "r",
                        }
                    ],
                    "next_action": {
                        "kind": "probe",
                        "tool_name": "get_consumer_lag",
                        "arguments": {},
                    },
                }
            )

        assert asked_for_a_probe(raised.value)

    def test_the_narrowed_model_is_built_once_per_model(self) -> None:
        """Cached, because ``model_json_schema()`` is cached per CLASS.

        A fresh class per step would regenerate the schema on every planner call and send a
        different tool definition each time, which is a cache miss on every step.
        """
        assert without_probe(InvestigationStep) is without_probe(InvestigationStep)

    def test_it_narrows_a_model_that_is_not_the_investigation_step(self) -> None:
        """Every strategy's own schema, not just the control group's.

        ``best_of_n_enumerated`` asks with a generated ``CandidateStep<n>``; a narrowing that
        only knew ``InvestigationStep`` would leave that arm able to probe exactly where the
        loop said it may not.
        """
        from incident_commander.agent.strategies.best_of_n_enumerated import candidate_step_model

        narrowed = without_probe(candidate_step_model(2))
        mapping = narrowed.model_json_schema()["properties"]["next_action"]["discriminator"]
        assert set(mapping["mapping"]) == {"remediate", "stop"}
        # The arm's own bound survives the narrowing: still exactly N candidates.
        assert narrowed.model_json_schema()["properties"]["candidates"]["minItems"] == 2


class TestAskedForAProbe:
    """Which validation failures are a REFUSED move, and which are ADR 0035's re-ask."""

    def _refusal(self) -> ValidationError:
        with pytest.raises(ValidationError) as raised:
            without_probe(InvestigationStep).model_validate(
                {
                    "hypotheses": [
                        {
                            "category": "consumer_saturation",
                            "name": "x",
                            "confidence": 0.9,
                            "reasoning": "r",
                        }
                    ],
                    "next_action": {"kind": "probe", "tool_name": "get_consumer_lag"},
                }
            )
        return raised.value

    def test_the_probe_tag_is_recognised(self) -> None:
        assert asked_for_a_probe(self._refusal()) is True

    def test_it_reads_the_cause_as_well(self) -> None:
        """``LLMClient`` wraps a ``ValidationError`` in ``LLMOutputError`` (ADR 0007).

        So on the live path the real complaint arrives one link down, and a predicate that
        looked only at the exception it was handed would send every live refusal through the
        re-ask instead.
        """
        wrapped = LLMOutputError("output failed schema validation")
        wrapped.__cause__ = self._refusal()

        assert asked_for_a_probe(wrapped) is True

    def test_an_invented_tag_is_not_a_refusal(self) -> None:
        """A model that names a move nobody offers is malformed output, not a refusal.

        It keeps ADR 0035's one bounded re-ask, because "your output was invalid" is exactly
        the right sentence for it.
        """
        with pytest.raises(ValidationError) as raised:
            InvestigationStep.model_validate(
                {
                    "hypotheses": [
                        {
                            "category": "consumer_saturation",
                            "name": "x",
                            "confidence": 0.9,
                            "reasoning": "r",
                        }
                    ],
                    "next_action": {"kind": "restart", "reason": "go"},
                }
            )

        assert asked_for_a_probe(raised.value) is False

    def test_an_ordinary_missing_field_is_not_a_refusal(self) -> None:
        with pytest.raises(ValidationError) as raised:
            without_probe(InvestigationStep).model_validate(
                {"hypotheses": [], "next_action": {"kind": "stop"}}
            )

        assert asked_for_a_probe(raised.value) is False

    def test_a_model_that_narrows_nothing_refuses_nothing(self) -> None:
        """The default hook, so every other output model keeps ADR 0035 exactly."""
        assert InvestigationStep.output_refused(self._refusal()) is False
        assert without_probe(InvestigationStep).output_refused(self._refusal()) is True
