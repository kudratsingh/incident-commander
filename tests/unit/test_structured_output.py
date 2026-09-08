"""Stringified nested output decodes; everything else still raises (ADR 0035).

The first class here is the regression for paid run ``779b19a287a7``
(2026-09-08, ``remediate_dlq_backlog_success`` run C). Its planner decided
correctly — replay the one confirmed-safe DLQ row ``fc8d2a03…``, leave the
poisoned ``eb798430…`` alone — and then handed ``record_output`` a
``next_action`` that was a JSON *string* with the enclosing array's ``]``
still stuck to the end. ``InvestigationStep`` rejected it, the run escalated
on the first failure, and the scenario graded RED on outcome, evidence and
action.

``LIVE_RECORD_OUTPUT_INPUT`` below is that tool-use ``input`` verbatim, copied
from ``evals/traces/remediate_dlq_backlog_success.jsonl`` (the record with
``parse_failed: true``, invocation ``779b19a287a7``) and rendered in the human
report at ``evals/reports/human/remediate_dlq_backlog_success.20260908T091545Z
.6d34908ba8f4.txt``, STEP 11. On ``origin/main`` before this change
``test_the_live_payload_parses`` fails with a ``ValidationError``; that is the
red-before half.

The rest of the file is the other half of the claim, and it is the half worth
reading: the decoder must not become a general "try harder" parser. A string
that is not JSON, one that decodes to the wrong shape, one with real content
after the value, and a decoded object that violates ``extra="forbid"`` all
still raise, so a genuinely broken payload still produces the model's own
error rather than a quietly wrong object.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Final

import pytest
from pydantic import ValidationError

from evals.graders.llm_judge import JudgeScore
from incident_commander.agent.briefing_enrichment import BriefingContent
from incident_commander.agent.hypothesis import (
    Hypothesis,
    InvestigationStep,
    ProbeAction,
    RemediateAction,
    StopAction,
)
from incident_commander.agent.remediation import RemediationPlan, VerificationJudgment
from incident_commander.llm.structured import (
    TRAILING_DELIMITERS,
    StructuredOutput,
    expected_container,
)

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]

# Every model the LLM fills in through the ``record_output`` tool. Not a
# hand-kept list you have to remember to extend: ``TestEveryRecordOutputModel
# ::test_the_list_matches_the_call_sites`` derives the same set from the
# ``output_model=`` arguments in the source tree and fails if the two differ.
RECORD_OUTPUT_MODELS: Final[tuple[type[StructuredOutput], ...]] = (
    InvestigationStep,
    RemediationPlan,
    VerificationJudgment,
    BriefingContent,
    JudgeScore,
)

# Verbatim evidence, not sample data: the two long lines below are the exact
# strings the API returned and are `noqa: E501` rather than reflowed. Wrapping
# them would make the regression a paraphrase of the failure instead of the
# failure (evidence is append-only and quoted as-is; CLAUDE.md invariant 9).
LIVE_RECORD_OUTPUT_INPUT: Final[dict[str, Any]] = {
    "hypotheses": [
        {
            "category": "poison_message",
            "name": "replay-safe-bulk-api-sync-upstream-timeout",
            "confidence": 0.95,
            "reasoning": "The alerted remediation_hint='replay_safe' slice contains exactly one row (fc8d2a03): a bulk_api_sync job that timed out against partner-api.internal; triage confirms it is retryable with confidence 0.88, no partial write occurred, and the hint is replay_safe \u2014 all evidence supports a safe replay.",  # noqa: E501
        }
    ],
    "next_action": '{"kind": "remediate", "reason": "The replay_safe DLQ slice has been read and contains one confirmed-safe row (fc8d2a03-23b3-5371-9acb-46443c73baa5): an idempotent bulk_api_sync that timed out with no downstream write committed. Triage marks it retryable (confidence 0.88). The alert named remediation_hint=\'replay_safe\' and that slice is now fully read and confirmed \u2014 replay is the correct Tier-1 action. The other rows in the queue (human_required csv_upload, wait_and_replay rate-limited and SMTP jobs, and an unclassified SchemaValidationError bulk_api_sync) are context; none of them are the alerted slice. The human_required row (f030f975) should be noted for follow-up: bad CSV data at row 15,382 requiring uploader notification and a validation gate fix. The unclassified row (eb798430) has a permanent schema error (\'missing required field job_id\') and should be fenced by a human."}]',  # noqa: E501
}


def _plan(**overrides: Any) -> dict[str, Any]:
    """A minimal well-formed RemediationPlan payload."""
    payload: dict[str, Any] = {
        "target_hypothesis": "h",
        "action_tool": "invalidate_cache_key",
        "action_arguments": {"key": "cache:jobs:summary"},
        "verify_tool": "get_cache_key_info",
        "verify_arguments": {"key": "cache:jobs:summary"},
        "verify_expectation": "exists is false",
    }
    payload.update(overrides)
    return payload


def _step(next_action: Any) -> dict[str, Any]:
    return {
        "hypotheses": [{"category": "unknown", "name": "n", "confidence": 0.5, "reasoning": "r"}],
        "next_action": next_action,
    }


class TestTheLiveRunPayload:
    """Run 779b19a287a7, exactly as the API returned it."""

    def test_next_action_really_did_arrive_as_a_string(self) -> None:
        """Anti-vacuity: if this stops being a str the regression is fiction."""
        assert isinstance(LIVE_RECORD_OUTPUT_INPUT["next_action"], str)

    def test_a_strict_json_load_of_that_string_still_fails(self) -> None:
        """Why plain ``json.loads`` was not enough — the stray ``]``.

        The pre-fix coercion on this field was a bare ``json.loads``. It was
        already there on the day of the run and it did not save it: the
        string is a complete object followed by one array terminator, which
        ``json.loads`` reports as ``Extra data``. That is the specific
        malformation ``_decode_leading_value`` tolerates and nothing wider.
        """
        with pytest.raises(json.JSONDecodeError, match="Extra data"):
            json.loads(LIVE_RECORD_OUTPUT_INPUT["next_action"])

    def test_the_live_payload_parses(self) -> None:
        """RED on origin/main (ValidationError), GREEN here."""
        step = InvestigationStep.model_validate(LIVE_RECORD_OUTPUT_INPUT)
        assert isinstance(step.next_action, RemediateAction)
        assert step.next_action.kind == "remediate"

    def test_the_decision_survives_the_decode_unchanged(self) -> None:
        """The point of repairing rather than escalating: the answer is kept.

        The reason string is what the remediation planner is handed next, so
        a decode that dropped or truncated it would turn a RED run into a
        differently-wrong one.
        """
        step = InvestigationStep.model_validate(LIVE_RECORD_OUTPUT_INPUT)
        assert isinstance(step.next_action, RemediateAction)
        reason = step.next_action.reason
        assert "fc8d2a03-23b3-5371-9acb-46443c73baa5" in reason
        assert "eb798430" in reason
        assert step.hypotheses[0].category.value == "poison_message"
        assert step.hypotheses[0].confidence == 0.95


class TestStringifiedContainersDecode:
    def test_a_clean_stringified_object_decodes(self) -> None:
        step = InvestigationStep.model_validate(
            _step(json.dumps({"kind": "stop", "reason": "done"}))
        )
        assert isinstance(step.next_action, StopAction)

    def test_a_stringified_array_decodes(self) -> None:
        payload = _step({"kind": "stop", "reason": "done"})
        payload["hypotheses"] = json.dumps(payload["hypotheses"])
        step = InvestigationStep.model_validate(payload)
        assert step.hypotheses[0].name == "n"

    def test_a_stringified_arguments_mapping_decodes(self) -> None:
        step = InvestigationStep.model_validate(
            _step(
                {
                    "kind": "probe",
                    "tool_name": "list_dlq_messages",
                    "arguments": json.dumps({"remediation_hint": "replay_safe"}),
                }
            )
        )
        assert isinstance(step.next_action, ProbeAction)
        assert step.next_action.arguments == {"remediation_hint": "replay_safe"}

    def test_the_remediation_plan_decodes_its_argument_mappings(self) -> None:
        plan = RemediationPlan.model_validate(
            _plan(
                action_arguments=json.dumps({"key": "cache:jobs:summary"}),
                verify_arguments=json.dumps({"key": "cache:jobs:summary"}),
            )
        )
        assert plan.action_arguments == {"key": "cache:jobs:summary"}
        assert plan.verify_arguments == {"key": "cache:jobs:summary"}

    @pytest.mark.parametrize("closer", list(TRAILING_DELIMITERS))
    def test_one_stray_closing_delimiter_is_tolerated(self, closer: str) -> None:
        step = InvestigationStep.model_validate(
            _step(json.dumps({"kind": "stop", "reason": "done"}) + closer)
        )
        assert isinstance(step.next_action, StopAction)


class TestEverythingElseStillRaises:
    """The narrowness of the tolerance, stated as tests."""

    def test_a_string_that_is_not_json_still_raises(self) -> None:
        with pytest.raises(ValidationError):
            InvestigationStep.model_validate(_step("remediate the replay_safe row"))

    def test_a_truncated_object_still_raises(self) -> None:
        with pytest.raises(ValidationError):
            InvestigationStep.model_validate(_step('{"kind": "stop", "reason": "do'))

    def test_a_decoded_value_of_the_wrong_type_still_raises(self) -> None:
        """A JSON array where the schema wants an object is not repaired."""
        with pytest.raises(ValidationError):
            InvestigationStep.model_validate(_step('[{"kind": "stop", "reason": "d"}]'))

    def test_a_stringified_number_still_raises(self) -> None:
        with pytest.raises(ValidationError):
            InvestigationStep.model_validate(_step("42"))

    def test_real_content_after_the_value_still_raises(self) -> None:
        """Only closers are ignored; a second value is a different answer."""
        with pytest.raises(ValidationError):
            InvestigationStep.model_validate(
                _step('{"kind": "stop", "reason": "a"} {"kind": "stop", "reason": "b"}')
            )

    def test_a_trailing_comma_still_raises(self) -> None:
        with pytest.raises(ValidationError):
            InvestigationStep.model_validate(_step('{"kind": "stop", "reason": "a"},'))

    def test_extra_forbid_survives_the_decode(self) -> None:
        """Decoding is not permission. The decoded object faces every rule."""
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            InvestigationStep.model_validate(
                _step(json.dumps({"kind": "stop", "reason": "d", "sneaky": 1}))
            )

    def test_an_unknown_enum_member_survives_the_decode(self) -> None:
        with pytest.raises(ValidationError):
            InvestigationStep.model_validate(_step(json.dumps({"kind": "detonate", "reason": "d"})))

    def test_a_free_text_field_that_looks_like_json_is_left_alone(self) -> None:
        """``findings`` is a ``str``; a JSON-shaped one is still that string."""
        content = BriefingContent.model_validate(
            {"findings": '{"a": 1}', "recommendation": "check the row"}
        )
        assert content.findings == '{"a": 1}'

    def test_an_optional_string_field_is_left_alone(self) -> None:
        plan = RemediationPlan.model_validate(_plan(action_rationale='{"a": 1}'))
        assert plan.action_rationale == '{"a": 1}'

    def test_the_empty_string_is_left_alone(self) -> None:
        with pytest.raises(ValidationError):
            InvestigationStep.model_validate(_step(""))


class TestExpectedContainer:
    """The derivation the validator reads, checked directly."""

    @pytest.mark.parametrize(
        ("model", "field", "expected"),
        [
            (InvestigationStep, "next_action", dict),
            (InvestigationStep, "hypotheses", list),
            (ProbeAction, "arguments", dict),
            (ProbeAction, "tool_name", None),
            (ProbeAction, "kind", None),
            (RemediationPlan, "action_arguments", dict),
            (RemediationPlan, "verify_arguments", dict),
            (RemediationPlan, "action_rationale", None),
            (RemediationPlan, "verify_expectation", None),
            (BriefingContent, "findings", None),
            (JudgeScore, "groundedness", None),
            (Hypothesis, "confidence", None),
            (VerificationJudgment, "reasoning", None),
        ],
    )
    def test_container_shape_of_each_field(
        self, model: type[StructuredOutput], field: str, expected: type | None
    ) -> None:
        assert expected_container(model.model_fields[field].annotation) is expected


class TestEveryRecordOutputModel:
    """Coverage, so a new structured-output model cannot ship uncovered."""

    def test_the_list_matches_the_call_sites(self) -> None:
        """``RECORD_OUTPUT_MODELS`` is checked against the source, not trusted.

        A model that reaches ``LLMClientProtocol.call`` without inheriting
        ``StructuredOutput`` is a model whose nested fields are back to
        escalating a run over their wrapping — and a hand-kept list is
        exactly the thing that would not notice.
        """
        names: set[str] = set()
        for root in ("src", "evals"):
            for path in (_REPO_ROOT / root).rglob("*.py"):
                names.update(
                    re.findall(r"output_model=([A-Za-z_][A-Za-z0-9_]*)\s*,", path.read_text())
                )
        # ``llm/repair.py`` forwards its own ``output_model`` parameter
        # through to the client; that is a pass-through, not a call site
        # that names a model.
        names.discard("output_model")
        assert names == {model.__name__ for model in RECORD_OUTPUT_MODELS}, (
            "the record_output call sites and RECORD_OUTPUT_MODELS disagree; "
            "a new structured-output model must inherit StructuredOutput and "
            "be listed here."
        )

    @pytest.mark.parametrize("model", RECORD_OUTPUT_MODELS, ids=lambda m: m.__name__)
    def test_the_model_decodes_stringified_containers(self, model: type[StructuredOutput]) -> None:
        assert issubclass(model, StructuredOutput)


class TestRecordOutputSchemaDeclaresObjects:
    """The contract half: the schema the model is shown says ``object``.

    ``LLMClient`` advertises ``output_model.model_json_schema()`` as the
    ``record_output`` tool's ``input_schema``, and that document is the only
    thing telling the API what shape a nested field takes. If a field ever
    came to be declared ``type: string`` there, the model would be *correct*
    to send a string and the decoder above would be papering over a schema
    bug. This asserts it never does.
    """

    @pytest.mark.parametrize("model", RECORD_OUTPUT_MODELS, ids=lambda m: m.__name__)
    def test_container_fields_are_never_declared_as_strings(
        self, model: type[StructuredOutput]
    ) -> None:
        schema = model.model_json_schema()
        defs = schema.get("$defs", {})
        checked = 0
        for name, field in model.model_fields.items():
            expected = expected_container(field.annotation)
            if expected is None:
                continue
            checked += 1
            declared = _declared_types(schema["properties"][name], defs)
            assert declared, f"{model.__name__}.{name} declares no type at all"
            assert declared == {"object" if expected is dict else "array"}, (
                f"{model.__name__}.{name} is a nested {expected.__name__} but "
                f"its record_output input_schema declares {sorted(declared)}"
            )
        if model in (InvestigationStep, RemediationPlan):
            assert checked >= 2, f"{model.__name__}: nothing was actually checked"


def _declared_types(node: dict[str, Any], defs: dict[str, Any]) -> set[str]:
    """Every JSON-Schema ``type`` a property node can resolve to.

    Follows ``$ref``, ``anyOf``/``oneOf`` (a discriminated union) and
    ``allOf``, because pydantic renders a nested model as a ``$ref`` and a
    union as a branch list rather than as a bare ``type``.
    """
    if "$ref" in node:
        return _declared_types(defs[node["$ref"].rsplit("/", 1)[-1]], defs)
    types_: set[str] = set()
    if "type" in node:
        types_.add(node["type"])
    for key in ("anyOf", "oneOf", "allOf"):
        for branch in node.get(key, []):
            types_ |= _declared_types(branch, defs)
    return types_ - {"null"}
