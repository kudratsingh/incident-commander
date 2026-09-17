"""The candidate schema: grounded, deduplicated, ranked, repairable (WP-5.1).

Four claims, one class each, and the negative half of every one of them is in
the same file:

* **Grounding is structural.** A candidate citing an ``evidence_id`` that names
  no ledger entry fails validation and the message names the id. With no ledger
  bound, validation refuses — including for a set that cites nothing, which is
  the only case ``EvidenceRef``'s own validator cannot see.
* **A set has no duplicates.** Same ``(category, name)`` rejected; same
  ``candidate_id`` rejected; and the deliberate non-normalisations (a name
  differing only in case, the same name under another category) are pinned as
  *accepted*, so the comparison cannot be quietly widened later.
* **Ranking is normalised at the schema boundary.** Index 0 is the top
  candidate for any input order, ties keep the model's stated order.
* **A parse failure is a harness event (ADR 0035).** A candidate set that
  arrived as a JSON string decodes, and an ungrounded one buys exactly one
  re-ask before ``OutputRepairExhausted`` — asserted on the repair's own
  marker and on the guard's own message, not on the fact that something raised
  (F-007).

**The stringified payload is not hand-written.** There is no live candidate-set
payload yet — the strategy that would emit one is WP-5.2 — so the malformation
is taken from the payload we *have* observed: run ``779b19a287a7``'s
``next_action``, imported from ``test_structured_output`` so it stays byte-
identical to the evidence, with its stray closer read off the string rather
than retyped. The candidate content is the live run's own two diagnoses, read
out of the same payload. That is as close to "pinned with the observed payload,
byte for byte" as a shape nothing has emitted yet can be, and the alternative —
inventing a malformation — is the thing LESSONS 2026-09-08 is about: a
``json.loads`` coercion that read as solved for six weeks because nothing had
ever fed it a real malformed reply.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from itertools import permutations
from typing import Annotated, Any, Final
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel, Field, ValidationError

from incident_commander.agent.candidates import (
    DUPLICATE_CANDIDATE,
    DUPLICATE_CANDIDATE_ID,
    NO_LEDGER_BOUND,
    UNKNOWN_EVIDENCE_ID,
    CandidateSet,
    CandidateTuple,
    DiagnosisCandidate,
    EvidenceRef,
    grounded_in,
)
from incident_commander.agent.hypothesis import HypothesisCategory, ProbeAction
from incident_commander.agent.state import EvidenceEntry
from incident_commander.llm.client import LLMError, LLMOutputError, LLMResult, LLMUsage
from incident_commander.llm.repair import (
    MAX_OUTPUT_REPAIRS,
    OutputRepairExhausted,
    call_with_output_repair,
)
from incident_commander.llm.structured import TRAILING_DELIMITERS, StructuredOutput
from tests.unit.test_structured_output import LIVE_RECORD_OUTPUT_INPUT

_AT: Final[datetime] = datetime(2026, 9, 8, 9, 15, tzinfo=UTC)


def _ledger(count: int = 2) -> tuple[EvidenceEntry, ...]:
    """A ledger of real ``EvidenceEntry`` rows, ids minted as the agent mints them."""
    return tuple(
        EvidenceEntry(
            tool_name="list_dlq_messages",
            arguments={"limit": 50},
            result_summary=f"total=5 page={index}",
            timestamp=_AT + timedelta(seconds=index),
        )
        for index in range(count)
    )


def _candidate(
    candidate_id: str = "c1",
    *,
    category: str = "poison_message",
    name: str = "replay-safe row",
    confidence: float = 0.9,
    evidence_for: tuple[UUID, ...] = (),
    evidence_against: tuple[UUID, ...] = (),
    next_probe: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "candidate_id": candidate_id,
        "category": category,
        "name": name,
        "confidence": confidence,
        "evidence_for": [{"evidence_id": str(value)} for value in evidence_for],
        "evidence_against": [{"evidence_id": str(value)} for value in evidence_against],
        "next_probe": next_probe,
    }


# --------------------------------------------------------------------------
# The observed malformation, read off run 779b19a287a7's own payload.


def _observed_stray_closer() -> str:
    """What run ``779b19a287a7`` left after the JSON value in its string field.

    Derived from the imported evidence rather than retyped: if the archived
    payload ever changes, every test built on this changes with it instead of
    silently pinning a paraphrase.
    """
    text = str(LIVE_RECORD_OUTPUT_INPUT["next_action"])
    _, end = json.JSONDecoder().raw_decode(text)
    return text[end:]


_LIVE_TOP: Final[dict[str, Any]] = dict(LIVE_RECORD_OUTPUT_INPUT["hypotheses"][0])


def _live_candidates(ledger: tuple[EvidenceEntry, ...]) -> list[dict[str, Any]]:
    """The two diagnoses run ``779b19a287a7``'s payload actually contains.

    The first is its ranked hypothesis verbatim. The second is the alternative
    its own ``next_action`` reason states — the unclassified row carrying a
    permanent schema error, which is a ``persistent_data_bug`` and not the
    replay-safe row it acted on. Both are the run's content, not invented
    furniture.
    """
    return [
        _candidate(
            "live-1",
            category=str(_LIVE_TOP["category"]),
            name=str(_LIVE_TOP["name"]),
            confidence=float(_LIVE_TOP["confidence"]),
            evidence_for=(ledger[0].evidence_id,),
        ),
        _candidate(
            "live-2",
            category="persistent_data_bug",
            name="unclassified row missing required field job_id",
            confidence=0.4,
            evidence_for=(ledger[1].evidence_id,),
        ),
    ]


class TestTheObservedMalformation:
    """A candidate set that arrived as a JSON string, malformed as the live one was."""

    def test_the_live_field_really_did_arrive_as_a_string(self) -> None:
        """Anti-vacuity: the evidence this file is built on is still a string."""
        assert isinstance(LIVE_RECORD_OUTPUT_INPUT["next_action"], str)

    def test_the_stray_closer_is_one_tolerated_delimiter(self) -> None:
        """The malformation is a single container terminator, nothing wider."""
        closer = _observed_stray_closer()
        assert closer
        assert set(closer) <= set(TRAILING_DELIMITERS)

    def test_a_strict_json_load_of_the_stringified_set_still_fails(self) -> None:
        """Why plain ``json.loads`` is not what saves this — the stray closer."""
        ledger = _ledger()
        payload = json.dumps(_live_candidates(ledger)) + _observed_stray_closer()
        with pytest.raises(json.JSONDecodeError):
            json.loads(payload)

    def test_the_stringified_candidate_set_decodes(self) -> None:
        ledger = _ledger()
        payload = json.dumps(_live_candidates(ledger)) + _observed_stray_closer()
        assert isinstance(payload, str)
        with grounded_in(ledger):
            result = CandidateSet.model_validate({"candidates": payload})
        assert len(result.candidates) == 2

    def test_the_candidates_survive_the_decode_unchanged(self) -> None:
        """The decode must not change what the model said, only its wrapping."""
        ledger = _ledger()
        candidates = _live_candidates(ledger)
        payload = json.dumps(candidates) + _observed_stray_closer()
        with grounded_in(ledger):
            result = CandidateSet.model_validate({"candidates": payload})
        assert result.top.candidate_id == "live-1"
        assert result.top.category is HypothesisCategory.POISON_MESSAGE
        assert result.top.name == _LIVE_TOP["name"]
        assert result.top.confidence == pytest.approx(float(_LIVE_TOP["confidence"]))
        assert result.top.evidence_for[0].evidence_id == ledger[0].evidence_id
        assert [c.candidate_id for c in result.candidates] == ["live-1", "live-2"]


class TestTheCoercionsToleranceIsFenced:
    """A coercion with no negative tests is a coercion nobody has bounded."""

    @pytest.mark.parametrize("closer", list(TRAILING_DELIMITERS))
    def test_each_tolerated_closer_decodes(self, closer: str) -> None:
        ledger = _ledger()
        payload = json.dumps([_candidate()]) + closer
        with grounded_in(ledger):
            assert len(CandidateSet.model_validate({"candidates": payload}).candidates) == 1

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param(json.dumps([_candidate()]) + ",", id="trailing-comma"),
            pytest.param(json.dumps([_candidate()]) + " and one more", id="prose-after"),
            pytest.param(
                json.dumps([_candidate()]) + json.dumps([_candidate("c2")]), id="two-values"
            ),
            pytest.param(json.dumps([_candidate()])[:-8], id="truncated"),
            pytest.param("the candidate set is in my previous message", id="not-json"),
            pytest.param("4", id="a-number"),
            pytest.param(json.dumps(_candidate()), id="object-where-array-expected"),
            pytest.param("", id="empty-string"),
        ],
    )
    def test_everything_else_still_raises(self, payload: str) -> None:
        """Left exactly as it arrived, so the model's own error is the one that fires."""
        with grounded_in(_ledger()), pytest.raises(ValidationError):
            CandidateSet.model_validate({"candidates": payload})

    def test_extra_forbid_survives_the_decode(self) -> None:
        """A decoded candidate is still held to ``extra='forbid'``."""
        rogue = {**_candidate(), "authorized": True}
        with grounded_in(_ledger()), pytest.raises(ValidationError, match="authorized"):
            CandidateSet.model_validate({"candidates": json.dumps([rogue])})

    def test_the_category_enum_survives_the_decode(self) -> None:
        """A decoded candidate cannot smuggle in a category that does not exist."""
        invented = _candidate(category="definitely_the_network")
        with grounded_in(_ledger()), pytest.raises(ValidationError):
            CandidateSet.model_validate({"candidates": json.dumps([invented])})


class TestGroundingIsStructural:
    """A ref that names no ledger entry fails validation, and says which id."""

    def test_an_unknown_evidence_id_is_rejected_and_named(self) -> None:
        ledger = _ledger()
        unknown = uuid4()
        with grounded_in(ledger), pytest.raises(ValidationError) as excinfo:
            CandidateSet.model_validate({"candidates": [_candidate(evidence_for=(unknown,))]})
        message = str(excinfo.value)
        assert str(unknown) in message
        assert UNKNOWN_EVIDENCE_ID in message

    def test_a_ref_that_resolves_is_accepted(self) -> None:
        ledger = _ledger()
        with grounded_in(ledger):
            result = CandidateSet.model_validate(
                {"candidates": [_candidate(evidence_for=(ledger[0].evidence_id,))]}
            )
        assert result.top.evidence_for[0].evidence_id == ledger[0].evidence_id

    def test_evidence_against_is_held_to_the_same_rule(self) -> None:
        """The rule is on ``EvidenceRef``, so both fields are covered by one line."""
        ledger = _ledger()
        unknown = uuid4()
        with grounded_in(ledger), pytest.raises(ValidationError) as excinfo:
            CandidateSet.model_validate({"candidates": [_candidate(evidence_against=(unknown,))]})
        assert str(unknown) in str(excinfo.value)

    def test_citing_nothing_is_allowed_inside_a_binding(self) -> None:
        """An empty citation list is the honest reading of "it did not say"."""
        with grounded_in(_ledger()):
            result = CandidateSet.model_validate({"candidates": [_candidate()]})
        assert result.top.evidence_for == ()
        assert result.top.evidence_against == ()

    def test_an_empty_ledger_resolves_nothing(self) -> None:
        """Bound-but-empty is a different refusal from not bound at all."""
        with grounded_in(()), pytest.raises(ValidationError) as excinfo:
            CandidateSet.model_validate({"candidates": [_candidate(evidence_for=(uuid4(),))]})
        message = str(excinfo.value)
        assert UNKNOWN_EVIDENCE_ID in message
        assert "0 entries" in message
        assert NO_LEDGER_BOUND not in message

    def test_with_no_ledger_bound_a_cited_set_is_refused(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            CandidateSet.model_validate({"candidates": [_candidate(evidence_for=(uuid4(),))]})
        assert NO_LEDGER_BOUND in str(excinfo.value)

    def test_with_no_ledger_bound_an_uncited_set_is_refused_too(self) -> None:
        """The hole a permissive default would have left.

        ``EvidenceRef``'s validator never runs on a set that cites nothing, so
        without the set-level check a forgotten ``grounded_in`` would switch
        grounding off silently for exactly the payloads that cite nothing.
        """
        with pytest.raises(ValidationError) as excinfo:
            CandidateSet.model_validate({"candidates": [_candidate()]})
        assert NO_LEDGER_BOUND in str(excinfo.value)

    def test_a_bare_evidence_ref_is_refused_with_no_ledger_bound(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            EvidenceRef.model_validate({"evidence_id": str(uuid4())})
        assert NO_LEDGER_BOUND in str(excinfo.value)

    def test_the_binding_is_released_on_the_way_out(self) -> None:
        ledger = _ledger()
        with grounded_in(ledger):
            CandidateSet.model_validate({"candidates": [_candidate()]})
        with pytest.raises(ValidationError, match=NO_LEDGER_BOUND):
            CandidateSet.model_validate({"candidates": [_candidate()]})

    def test_the_binding_is_released_after_an_exception(self) -> None:
        with pytest.raises(RuntimeError), grounded_in(_ledger()):
            raise RuntimeError("boom")
        with pytest.raises(ValidationError, match=NO_LEDGER_BOUND):
            CandidateSet.model_validate({"candidates": [_candidate()]})

    def test_a_nested_binding_restores_the_outer_one(self) -> None:
        outer, inner = _ledger(1), _ledger(2)
        with grounded_in(outer):
            with grounded_in(inner):
                CandidateSet.model_validate(
                    {"candidates": [_candidate(evidence_for=(inner[1].evidence_id,))]}
                )
            with pytest.raises(ValidationError, match=UNKNOWN_EVIDENCE_ID):
                CandidateSet.model_validate(
                    {"candidates": [_candidate(evidence_for=(inner[1].evidence_id,))]}
                )

    def test_a_malformed_uuid_is_rejected_by_the_type(self) -> None:
        with grounded_in(_ledger()), pytest.raises(ValidationError):
            CandidateSet.model_validate(
                {"candidates": [{**_candidate(), "evidence_for": [{"evidence_id": "not-a-uuid"}]}]}
            )


class TestDuplicatesAreRejected:
    """One diagnosis stated twice is one diagnosis (plan 02 § 11.1)."""

    def test_a_duplicate_category_and_name_is_rejected_and_named(self) -> None:
        payload = [_candidate("c1"), _candidate("c2")]
        with grounded_in(_ledger()), pytest.raises(ValidationError) as excinfo:
            CandidateSet.model_validate({"candidates": payload})
        message = str(excinfo.value)
        assert DUPLICATE_CANDIDATE in message
        assert "poison_message" in message
        assert "replay-safe row" in message

    def test_a_duplicate_candidate_id_is_rejected_and_named(self) -> None:
        """The selector's ``scores`` mapping is keyed by this id (plan 02 § 12)."""
        payload = [_candidate("same", name="one"), _candidate("same", name="two")]
        with grounded_in(_ledger()), pytest.raises(ValidationError) as excinfo:
            CandidateSet.model_validate({"candidates": payload})
        message = str(excinfo.value)
        assert DUPLICATE_CANDIDATE_ID in message
        assert "same" in message

    def test_the_same_name_under_another_category_is_two_candidates(self) -> None:
        payload = [
            _candidate("c1", category="poison_message", name="the dead-letter queue"),
            _candidate("c2", category="persistent_data_bug", name="the dead-letter queue"),
        ]
        with grounded_in(_ledger()):
            assert len(CandidateSet.model_validate({"candidates": payload}).candidates) == 2

    def test_the_same_category_with_another_name_is_two_candidates(self) -> None:
        payload = [_candidate("c1", name="row a"), _candidate("c2", name="row b")]
        with grounded_in(_ledger()):
            assert len(CandidateSet.model_validate({"candidates": payload}).candidates) == 2

    def test_names_differing_only_in_case_are_not_folded_together(self) -> None:
        """A deliberate non-normalisation, pinned so it cannot drift.

        ``name`` is operator-facing free text. Case-folding it would make two
        labels a reader can tell apart collide, and the duplicate *rate* WP-5.2
        reports is a measurement of what the model produced rather than of what
        a normaliser could hide.
        """
        payload = [_candidate("c1", name="Stale cache"), _candidate("c2", name="stale cache")]
        with grounded_in(_ledger()):
            assert len(CandidateSet.model_validate({"candidates": payload}).candidates) == 2


class TestRankingIsNormalisedAtTheSchemaBoundary:
    """Index 0 is the top candidate, whatever order the model emitted."""

    @pytest.mark.parametrize("order", list(permutations((0.2, 0.9, 0.55))))
    def test_index_zero_is_the_top_candidate_for_any_input_order(
        self, order: tuple[float, ...]
    ) -> None:
        payload = [
            _candidate(f"c{index}", name=f"n{index}", confidence=confidence)
            for index, confidence in enumerate(order)
        ]
        with grounded_in(_ledger()):
            result = CandidateSet.model_validate({"candidates": payload})
        assert result.candidates[0].confidence == pytest.approx(0.9)
        assert [c.confidence for c in result.candidates] == pytest.approx([0.9, 0.55, 0.2])

    def test_ties_preserve_the_models_stated_order(self) -> None:
        payload = [
            _candidate("first", name="first", confidence=0.7),
            _candidate("second", name="second", confidence=0.7),
            _candidate("third", name="third", confidence=0.7),
        ]
        with grounded_in(_ledger()):
            result = CandidateSet.model_validate({"candidates": payload})
        assert [c.candidate_id for c in result.candidates] == ["first", "second", "third"]

    def test_top_is_index_zero(self) -> None:
        payload = [_candidate("low", name="low", confidence=0.1), _candidate("high", name="high")]
        with grounded_in(_ledger()):
            result = CandidateSet.model_validate({"candidates": payload})
        assert result.top is result.candidates[0]
        assert result.top.candidate_id == "high"

    def test_an_empty_set_is_rejected(self) -> None:
        with grounded_in(_ledger()), pytest.raises(ValidationError):
            CandidateSet.model_validate({"candidates": []})


class _ScriptedLLM:
    """Plays a fixed script of payloads-or-exceptions, one per ``call``.

    ``model_validate`` runs *inside* ``call``, which is the whole point: that
    is where ``LLMClient._parse`` validates, so a grounding failure raised by a
    validator lands where ``call_with_output_repair`` can see it.

    A rejection is wrapped as ``LLMOutputError`` carrying this call's
    ``record_id``, because that is what ``_parse`` does with a
    ``ValidationError`` — and it is the field the re-ask reads to correlate
    itself to the call it repairs. A fake that let the bare ``ValidationError``
    out would still exercise the repair, but the correlation it asserts would
    be fiction.
    """

    def __init__(self, script: list[Any]) -> None:
        self._script = list(script)
        self.calls: list[tuple[str, str]] = []
        self.repair_of: list[str | None] = []

    def call[T: BaseModel](
        self,
        system_prompt: str,
        user_message: str,
        output_model: type[T],
        model: str,
        max_tokens: int = 4096,
        *,
        repair_of: str | None = None,
        temperature: float | None = None,
    ) -> LLMResult[T]:
        self.calls.append((system_prompt, user_message))
        self.repair_of.append(repair_of)
        if not self._script:
            raise AssertionError(
                f"call {len(self.calls)} was made with an empty script — the cap is not holding"
            )
        item = self._script.pop(0)
        record_id = f"rec{len(self.calls)}"
        if isinstance(item, BaseException):
            raise item
        try:
            output = output_model.model_validate(item)
        except ValidationError as err:
            raise LLMOutputError(
                f"output failed schema validation for {output_model.__name__}: {err}",
                usage=LLMUsage(),
                record_id=record_id,
            ) from err
        return LLMResult(output=output, stop_reason="scripted", record_id=record_id)


def _ask(client: _ScriptedLLM) -> Any:
    return call_with_output_repair(
        client,
        system_prompt="s",
        user_message="u",
        output_model=CandidateSet,
        model="claude-sonnet-4-6",
    )


class TestTheRepairPathAppliesToACandidateSet:
    """An ungrounded set is a harness event: one re-ask, then escalate (ADR 0035)."""

    def test_an_ungrounded_set_buys_one_repair_and_then_succeeds(self) -> None:
        ledger = _ledger()
        bad = {"candidates": [_candidate(evidence_for=(uuid4(),))]}
        good = {"candidates": [_candidate(evidence_for=(ledger[0].evidence_id,))]}
        client = _ScriptedLLM([bad, good])
        with grounded_in(ledger):
            repaired = _ask(client)
        assert repaired.was_repaired
        assert len(repaired.failures) == MAX_OUTPUT_REPAIRS
        assert len(client.calls) == MAX_OUTPUT_REPAIRS + 1
        assert UNKNOWN_EVIDENCE_ID in str(repaired.failures[0])
        # The re-ask is correlated to the call it repairs, so a trace reader
        # sees one repaired step rather than two unrelated planner calls.
        assert client.repair_of == [None, "rec1"]

    def test_a_second_ungrounded_set_exhausts_the_repair_and_names_the_cause(self) -> None:
        """F-007: the assertion is the guard's own marker, not just "it raised".

        ``OutputRepairExhausted`` is an ``LLMError``, which is what the
        investigation loop's existing ``except`` arm turns into an escalation
        with the reason in the evidence trail — pinned where that call site
        exists (``tests/unit/test_output_repair.py``). WP-5.1 has no call site
        yet, so the claim proven here ends at the exception the loop reads.
        """
        ledger = _ledger()
        unknown = uuid4()
        bad = {"candidates": [_candidate(evidence_for=(unknown,))]}
        client = _ScriptedLLM([bad, bad])
        with grounded_in(ledger), pytest.raises(OutputRepairExhausted) as excinfo:
            _ask(client)
        message = str(excinfo.value)
        assert f"repair 1 of {MAX_OUTPUT_REPAIRS} also failed validation" in message
        assert UNKNOWN_EVIDENCE_ID in message
        assert str(unknown) in message
        assert len(client.calls) == MAX_OUTPUT_REPAIRS + 1

    def test_a_duplicate_set_is_repairable_too(self) -> None:
        ledger = _ledger()
        duplicated = {"candidates": [_candidate("c1"), _candidate("c2")]}
        distinct = {"candidates": [_candidate("c1"), _candidate("c2", name="other")]}
        client = _ScriptedLLM([duplicated, distinct])
        with grounded_in(ledger):
            repaired = _ask(client)
        assert repaired.was_repaired
        assert DUPLICATE_CANDIDATE in str(repaired.failures[0])

    def test_a_transport_failure_is_not_repaired(self) -> None:
        """Scope is unchanged: only an output failure is repairable."""
        client = _ScriptedLLM([LLMError("429 rate limited", usage=LLMUsage())])
        with grounded_in(_ledger()), pytest.raises(LLMError) as excinfo:
            _ask(client)
        assert not isinstance(excinfo.value, OutputRepairExhausted)
        assert len(client.calls) == 1


class TestTheExactNBoundWpFiveTwoWillNeed:
    """N is configuration, so the exact-N bound is WP-5.2's — and it composes.

    Plan 02 § 11.1 asks one planner call for exactly N candidates. Proving the
    composition here is what stops WP-5.2 from re-declaring a bare tuple and
    silently losing grounding, deduplication and the ranking normalisation.
    """

    def test_an_exact_n_schema_keeps_every_rule_and_adds_the_bound(self) -> None:
        class _ExactlyTwo(StructuredOutput):
            candidates: Annotated[CandidateTuple, Field(min_length=2, max_length=2)]

        ledger = _ledger()
        two = [_candidate("c1", name="a", confidence=0.3), _candidate("c2", name="b")]
        with grounded_in(ledger):
            result = _ExactlyTwo.model_validate({"candidates": two})
            # The ranking normalisation is inherited.
            assert [c.candidate_id for c in result.candidates] == ["c2", "c1"]
            for wrong_size in ([two[0]], [*two, _candidate("c3", name="c")]):
                with pytest.raises(ValidationError):
                    _ExactlyTwo.model_validate({"candidates": wrong_size})
            # And so are the duplicate rules.
            with pytest.raises(ValidationError, match=DUPLICATE_CANDIDATE_ID):
                _ExactlyTwo.model_validate({"candidates": [two[0], _candidate("c1", name="b")]})
        # And so is grounding.
        with pytest.raises(ValidationError, match=NO_LEDGER_BOUND):
            _ExactlyTwo.model_validate({"candidates": two})


class TestTheSchemaShownToTheModel:
    """The contract half: what ``record_output`` would advertise for these models."""

    @pytest.mark.parametrize(
        "model", [EvidenceRef, DiagnosisCandidate, CandidateSet], ids=lambda m: m.__name__
    )
    def test_no_model_carries_a_class_docstring(self, model: type[StructuredOutput]) -> None:
        """A class docstring becomes the schema's ``description`` and reaches the model.

        These three schemas are shown to the model through ``record_output``,
        so their prose lives in the module docstring instead. This is the test
        that keeps it there.
        """
        assert model.__doc__ is None
        assert "description" not in model.model_json_schema()

    def test_container_fields_are_declared_as_containers(self) -> None:
        """If a nested field were declared ``string`` the model would be right
        to send one, and the ADR-0035 decoder would be papering over a schema bug."""
        schema = CandidateSet.model_json_schema()
        assert schema["properties"]["candidates"]["type"] == "array"
        candidate = schema["$defs"]["DiagnosisCandidate"]["properties"]
        assert candidate["evidence_for"]["type"] == "array"
        assert candidate["evidence_against"]["type"] == "array"

    def test_the_category_enum_is_the_closed_set(self) -> None:
        schema = CandidateSet.model_json_schema()
        assert set(schema["$defs"]["HypothesisCategory"]["enum"]) == {
            member.value for member in HypothesisCategory
        }

    def test_next_probe_is_required_and_nullable(self) -> None:
        """Absent and null do not mean the same thing here — see the field comment."""
        schema = CandidateSet.model_json_schema()
        assert "next_probe" in schema["$defs"]["DiagnosisCandidate"]["required"]
        with grounded_in(_ledger()), pytest.raises(ValidationError, match="next_probe"):
            payload = _candidate()
            del payload["next_probe"]
            CandidateSet.model_validate({"candidates": [payload]})

    def test_the_citation_fields_are_optional(self) -> None:
        required = CandidateSet.model_json_schema()["$defs"]["DiagnosisCandidate"]["required"]
        assert "evidence_for" not in required
        assert "evidence_against" not in required


class TestItMirrorsTheHypothesisSchema:
    """The same choices ``Hypothesis`` made, not new ones (ADR 0005)."""

    def test_the_category_is_the_closed_enum(self) -> None:
        with grounded_in(_ledger()), pytest.raises(ValidationError):
            CandidateSet.model_validate({"candidates": [_candidate(category="network_blip")]})

    def test_the_name_stays_free_form(self) -> None:
        with grounded_in(_ledger()):
            result = CandidateSet.model_validate(
                {"candidates": [_candidate(name="worker-dispatcher lag 15k sustained 5min")]}
            )
        assert result.top.name == "worker-dispatcher lag 15k sustained 5min"

    def test_an_empty_name_is_rejected(self) -> None:
        with grounded_in(_ledger()), pytest.raises(ValidationError):
            CandidateSet.model_validate({"candidates": [_candidate(name="")]})

    def test_an_empty_candidate_id_is_rejected(self) -> None:
        with grounded_in(_ledger()), pytest.raises(ValidationError):
            CandidateSet.model_validate({"candidates": [_candidate("")]})

    @pytest.mark.parametrize("confidence", [-0.1, 1.1])
    def test_confidence_is_bounded(self, confidence: float) -> None:
        with grounded_in(_ledger()), pytest.raises(ValidationError):
            CandidateSet.model_validate({"candidates": [_candidate(confidence=confidence)]})

    def test_the_probe_tool_name_is_the_read_tier_literal(self) -> None:
        """An invented tool name is structurally impossible, as in ``ProbeAction``."""
        with grounded_in(_ledger()), pytest.raises(ValidationError):
            CandidateSet.model_validate(
                {
                    "candidates": [
                        _candidate(next_probe={"kind": "probe", "tool_name": "get_everything"})
                    ]
                }
            )

    def test_a_real_probe_is_accepted(self) -> None:
        with grounded_in(_ledger()):
            result = CandidateSet.model_validate(
                {
                    "candidates": [
                        _candidate(
                            next_probe={
                                "kind": "probe",
                                "tool_name": "get_consumer_lag",
                                "arguments": {"group": "worker-dispatcher"},
                            }
                        )
                    ]
                }
            )
        probe = result.top.next_probe
        assert isinstance(probe, ProbeAction)
        assert probe.tool_name == "get_consumer_lag"

    def test_the_models_are_frozen(self) -> None:
        with grounded_in(_ledger()):
            result = CandidateSet.model_validate({"candidates": [_candidate()]})
        with pytest.raises(ValidationError):
            result.top.confidence = 0.1

    def test_a_candidate_is_a_structured_output(self) -> None:
        """Inheritance is the whole ADR-0035 claim; assert it rather than assume it."""
        for model in (EvidenceRef, DiagnosisCandidate, CandidateSet):
            assert issubclass(model, StructuredOutput)


class TestGroundedInTakesTheLedgerItIsGiven:
    def test_it_binds_exactly_the_ids_in_the_entries(self) -> None:
        ledger = _ledger(3)
        with grounded_in(ledger[:2]):
            for entry in ledger[:2]:
                CandidateSet.model_validate(
                    {"candidates": [_candidate(evidence_for=(entry.evidence_id,))]}
                )
            with pytest.raises(ValidationError, match=UNKNOWN_EVIDENCE_ID):
                CandidateSet.model_validate(
                    {"candidates": [_candidate(evidence_for=(ledger[2].evidence_id,))]}
                )

    def test_the_refusal_reports_the_ledger_size(self) -> None:
        with grounded_in(_ledger(3)), pytest.raises(ValidationError) as excinfo:
            CandidateSet.model_validate({"candidates": [_candidate(evidence_for=(uuid4(),))]})
        assert "3 entries" in str(excinfo.value)
