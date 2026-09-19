"""WP-6.1 — the ``candidate_selector``'s schema, its context and its call.

One class per acceptance item: scores resolve to the set both ways (the red-before),
``selected_candidate_id`` only on ``select``, the scale declared, nothing bound is a
refusal, arguments rendered before results (INC-002), no ground truth reaching the
selector (ADR 0038), no temperature sent (O-24), and one ``chosen_candidate_id``.
"""

from __future__ import annotations

import inspect
import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from evals.inventory import SCENARIO_DIRECTORY
from evals.scenarios.loader import load_scenarios
from evals.scenarios.schema import Scenario
from incident_commander.agent.briefing import render_trail, trail_of
from incident_commander.agent.candidates import DiagnosisCandidate, grounded_in
from incident_commander.agent.hypothesis import HypothesisCategory
from incident_commander.agent.selection import (
    CANDIDATES_HEADING,
    NO_CANDIDATES_BOUND,
    SELECT_WITHOUT_SELECTION,
    SELECTION_WITHOUT_SELECT,
    SELECTOR_PROMPT,
    SELECTOR_ROLE,
    UNKNOWN_CANDIDATE_ID,
    UNSCORED_CANDIDATE,
    SelectionDecision,
    SelectionResult,
    format_selection_context,
    select_candidate,
    selecting_among,
)
from incident_commander.agent.state import BudgetLedger, EvidenceEntry, IncidentState, RunState
from incident_commander.llm.fakes import CannedLLMClient
from incident_commander.llm.prompts.loader import load_prompt
from incident_commander.llm.repair import OutputRepairExhausted

# --------------------------------------------------------------------------
# Builders. Local, for the reason the other strategy suites keep their own:
# this file must be able to fail on its own.

_LEDGER_ID: Final[UUID] = UUID("11111111-1111-1111-1111-111111111111")
_AT: Final[datetime] = datetime(2026, 7, 15, 20, 0, tzinfo=UTC)

#: Every scenario the corpus root-cause-grades — the set that HAS a key to leak. Derived.
_GRADED: Final[tuple[Scenario, ...]] = tuple(
    scenario for scenario in load_scenarios(SCENARIO_DIRECTORY) if scenario.root_cause_graded
)

#: The two labels the leak sweep cannot look for by substring: both name the ABSENCE of a
#: diagnosis, and both collide with agent-visible world text (``unknown-consumer``).
_UNSEARCHABLE_LABELS: Final[frozenset[str]] = frozenset(
    {HypothesisCategory.UNKNOWN.value, HypothesisCategory.NO_FAULT.value}
)


def _entry(
    tool: str,
    arguments: dict[str, Any],
    summary: str,
    *,
    evidence_id: UUID | None = None,
) -> EvidenceEntry:
    return EvidenceEntry(
        evidence_id=evidence_id or uuid4(),
        tool_name=tool,
        arguments=arguments,
        result_summary=summary,
        timestamp=_AT,
    )


def _candidate(
    candidate_id: str,
    *,
    category: HypothesisCategory = HypothesisCategory.CONSUMER_SATURATION,
    confidence: float = 0.8,
    cites: tuple[UUID, ...] = (),
    probe: str | None = None,
) -> DiagnosisCandidate:
    """One candidate, validated against a ledger holding exactly ``cites``."""
    payload: dict[str, Any] = {
        "candidate_id": candidate_id,
        "category": category.value,
        "name": f"{candidate_id} label",
        "confidence": confidence,
        "evidence_for": [{"evidence_id": str(value)} for value in cites],
        "evidence_against": [],
        "next_probe": (
            None
            if probe is None
            else {"tool_name": probe, "arguments": {"consumer_group": "billing"}}
        ),
    }
    with grounded_in(
        tuple(_entry("get_consumer_lag", {}, "lag 42", evidence_id=value) for value in cites)
    ):
        return DiagnosisCandidate.model_validate(payload)


def _pair() -> tuple[DiagnosisCandidate, ...]:
    return (
        _candidate("c1", probe="get_consumer_lag"),
        _candidate("c2", category=HypothesisCategory.TRANSIENT_DEPENDENCY, confidence=0.4),
    )


def _payload(
    *,
    decision: str = "select",
    selected: str | None = "c1",
    scores: dict[str, float] | None = None,
    uncertainty: float = 0.2,
) -> dict[str, Any]:
    return {
        "decision": decision,
        "selected_candidate_id": selected,
        "scores": {"c1": 0.9, "c2": 0.2} if scores is None else scores,
        "uncertainty": uncertainty,
        "reasoning": "the lag reading supports c1 and nothing supports c2.",
    }


def _validate(
    payload: dict[str, Any], candidates: tuple[DiagnosisCandidate, ...]
) -> SelectionResult:
    with selecting_among(candidates):
        return SelectionResult.model_validate(payload)


def _state(*entries: EvidenceEntry, alert: dict[str, Any] | None = None) -> RunState:
    return RunState(
        incident_id=uuid4(),
        state=IncidentState.INVESTIGATING,
        alert=alert if alert is not None else {"source": "kafka", "severity": "high"},
        budget=BudgetLedger(
            max_tool_calls=25,
            max_tokens=200_000,
            max_wall_seconds=1_800,
            max_usd=Decimal("5.00"),
        ),
        evidence=entries,
        created_at=_AT,
        updated_at=_AT,
    )


# --------------------------------------------------------------------------


class TestScoresResolveToTheSet:
    """Plan 02 § 12's validator, both directions, and the message names the id."""

    def test_an_unknown_scored_id_is_refused_by_name(self) -> None:
        """The red-before case: this is what the schema exists to reject.

        A ``scores`` key naming no candidate corrupts the ranking.
        """
        with pytest.raises(ValidationError) as caught:
            _validate(_payload(scores={"c1": 0.9, "c2": 0.2, "c9": 0.1}), _pair())
        message = str(caught.value)
        assert "'c9'" in message
        assert UNKNOWN_CANDIDATE_ID in message

    def test_an_unscored_candidate_is_refused_by_name(self) -> None:
        """The strengthening, and it is the half that keeps the ranking total.

        A candidate with no score reads as one that scored zero.
        """
        with pytest.raises(ValidationError) as caught:
            _validate(_payload(scores={"c1": 0.9}), _pair())
        message = str(caught.value)
        assert "'c2'" in message
        assert UNSCORED_CANDIDATE in message

    def test_a_set_scored_exactly_validates(self) -> None:
        result = _validate(_payload(), _pair())
        assert result.scores == {"c1": 0.9, "c2": 0.2}
        assert result.decision is SelectionDecision.SELECT


class TestASelectionIsStatedExactlyWhenThereIsOne:
    def test_select_requires_an_id(self) -> None:
        with pytest.raises(ValidationError) as caught:
            _validate(_payload(selected=None), _pair())
        assert SELECT_WITHOUT_SELECTION in str(caught.value)

    @pytest.mark.parametrize("decision", ["probe_more", "escalate"])
    def test_a_non_select_decision_refuses_an_id(self, decision: str) -> None:
        """An id on a decision that does not act on it reads as a diagnosis.

        It reads as a run that committed and did not act.
        """
        with pytest.raises(ValidationError) as caught:
            _validate(_payload(decision=decision, selected="c1"), _pair())
        assert SELECTION_WITHOUT_SELECT in str(caught.value)

    @pytest.mark.parametrize("decision", ["probe_more", "escalate"])
    def test_a_non_select_decision_with_no_id_validates(self, decision: str) -> None:
        result = _validate(_payload(decision=decision, selected=None), _pair())
        assert result.selected_candidate_id is None

    def test_a_selected_id_outside_the_set_is_refused_by_name(self) -> None:
        with pytest.raises(ValidationError) as caught:
            _validate(_payload(selected="c9"), _pair())
        message = str(caught.value)
        assert "'c9'" in message
        assert UNKNOWN_CANDIDATE_ID in message


class TestTheScaleIsDeclared:
    """WP-6.3 calibrates these numbers, so the scale cannot be a convention."""

    @pytest.mark.parametrize("uncertainty", [-0.1, 1.1])
    def test_uncertainty_outside_the_range_is_refused(self, uncertainty: float) -> None:
        with pytest.raises(ValidationError):
            _validate(_payload(uncertainty=uncertainty), _pair())

    @pytest.mark.parametrize("score", [-0.5, 1.5, 100.0])
    def test_a_score_outside_the_range_is_refused(self, score: float) -> None:
        with pytest.raises(ValidationError) as caught:
            _validate(_payload(scores={"c1": score, "c2": 0.2}), _pair())
        assert "0.0-1.0" in str(caught.value)

    @pytest.mark.parametrize("edge", [0.0, 1.0])
    def test_the_endpoints_are_inside(self, edge: float) -> None:
        result = _validate(_payload(scores={"c1": edge, "c2": edge}, uncertainty=edge), _pair())
        assert result.uncertainty == edge


class TestNothingBoundIsARefusal:
    """Fail-closed, which is the only reason a context variable is tolerable.

    With nothing bound a ``SelectionResult`` could name any id.
    """

    def test_validation_with_no_set_bound_refuses(self) -> None:
        with pytest.raises(ValidationError) as caught:
            SelectionResult.model_validate(_payload())
        assert NO_CANDIDATES_BOUND in str(caught.value)

    def test_the_binding_is_restored_on_exit(self) -> None:
        with selecting_among(_pair()):
            with selecting_among((_candidate("inner"),)):
                inner = SelectionResult.model_validate(
                    _payload(selected="inner", scores={"inner": 0.5})
                )
                assert inner.selected_candidate_id == "inner"
            # Back to the outer set — not to nothing, and not to the inner one.
            assert SelectionResult.model_validate(_payload()).scores == {"c1": 0.9, "c2": 0.2}
        with pytest.raises(ValidationError) as caught:
            SelectionResult.model_validate(_payload())
        assert NO_CANDIDATES_BOUND in str(caught.value)

    def test_an_empty_candidate_set_is_refused_before_any_call(self) -> None:
        """A generator that produced nothing is a generation failure.

        Asked of ``select_candidate``, not the schema: it should cost no call.
        """
        llm = CannedLLMClient([])
        with pytest.raises(ValueError, match="empty candidate set"):
            select_candidate(llm, run_state=_state(), candidates=(), model="m")
        assert llm.calls == []


class TestTheContextRendersArgumentsBeforeResults:
    """INC-002, on the payload that produced it, through the shared renderer."""

    def test_the_inc_002_payload_renders_its_filter_before_its_total(self) -> None:
        """``remediation_hint='replay_safe'`` before ``'total': 0``.

        The filter is what makes ``total 0`` mean "that slice is drained" rather than "the
        queue is empty".
        """
        state = _state(
            _entry(
                "list_dlq_messages",
                {"remediation_hint": "replay_safe"},
                json.dumps({"total": 0, "items": []}),
            )
        )
        rendered = format_selection_context(state, _pair())
        assert rendered.index("remediation_hint='replay_safe'") < rendered.index('"total": 0')

    def test_an_unfiltered_read_is_distinguishable_from_a_filtered_one(self) -> None:
        """The absence of a filter is itself the fact, so it is rendered.

        A dropped key makes two lines look alike.
        """
        state = _state(
            _entry("list_dlq_messages", {"remediation_hint": None}, json.dumps({"total": 4})),
            _entry(
                "list_dlq_messages", {"remediation_hint": "replay_safe"}, json.dumps({"total": 1})
            ),
        )
        rendered = format_selection_context(state, _pair())
        assert "list_dlq_messages(remediation_hint=None)" in rendered
        assert "list_dlq_messages(remediation_hint='replay_safe')" in rendered

    def test_the_trail_comes_from_the_shared_renderer(self) -> None:
        """Not a fourth renderer: the same function, byte for byte.

        The selector's context is assembled through ``briefing.render_trail``.
        """
        state = _state(
            _entry("get_consumer_lag", {"consumer_group": "billing"}, "lag 42"),
            _entry("get_redis_health", {}, "clients 11"),
        )
        rendered = format_selection_context(state, _pair())
        for line in render_trail(trail_of(state.evidence)):
            assert line in rendered

    def test_bookkeeping_markers_are_not_shown_as_probes(self) -> None:
        state = _state(
            _entry("_planner_stop", {"reason": "done"}, "planner stop: done"),
            _entry("get_consumer_lag", {"consumer_group": "billing"}, "lag 42"),
        )
        assert "_planner_stop" not in format_selection_context(state, _pair())

    def test_every_candidate_is_on_the_page_with_its_citations(self) -> None:
        cited = _candidate("c1", cites=(_LEDGER_ID,), probe="get_consumer_lag")
        rendered = format_selection_context(_state(), (cited, _candidate("c2")))
        assert "candidate_id=c1" in rendered
        assert "candidate_id=c2" in rendered
        assert str(_LEDGER_ID) in rendered
        # A candidate that cited nothing says so in a word, rather than
        # rendering as a blank a reader would read as a gap.
        assert "evidence_against: none" in rendered
        assert "next_probe: get_consumer_lag(" in rendered


class TestNoGroundTruthReachesTheSelector:
    """ADR 0038's projection, asserted one layer further in, over the corpus.

    Written against the projection rather than a list of secret field names, so a future
    evaluator-only field is covered on the day it lands.
    """

    @pytest.mark.parametrize("scenario", _GRADED, ids=lambda scenario: scenario.name)
    def test_no_root_cause_label_reaches_the_rendered_context(self, scenario: Scenario) -> None:
        visible = scenario.agent_visible()
        state = _state(
            *(
                _entry(tool, {}, repr(result))
                for tool, result in visible.canned_tool_responses.items()
            ),
            alert=visible.alert,
        )
        assert scenario.ground_truth is not None
        labels = {cause.value for cause in scenario.ground_truth.root_causes}
        # A candidate's own category is legitimate agent vocabulary, so the fixture carries a
        # label this scenario's ground truth does NOT.
        neutral = next(member for member in HypothesisCategory if member.value not in labels)
        rendered = format_selection_context(state, (_candidate("c1", category=neutral),))
        assert "ground_truth" not in rendered
        assert "discriminating_probes" not in rendered
        for label in labels - _UNSEARCHABLE_LABELS:
            assert label not in rendered, (
                f"the root-cause label {label!r} for scenario {scenario.name!r} "
                f"reached the selector's context. Ground truth is evaluator-only "
                f"(ADR 0038): it is not on AgentVisibleScenario, so it cannot "
                f"arrive through the projection — check what else this render "
                f"was handed."
            )

    def test_the_sweep_is_not_vacuous(self) -> None:
        """Anti-vacuity canary for a derived parameterisation.

        A corpus that loaded empty would collect zero cases and report green.
        """
        assert len(_GRADED) >= 30, (
            f"only {len(_GRADED)} scenarios declare a ground truth; the sweep "
            f"above is parameterised over them, so a short list quietly "
            f"narrows what is checked."
        )
        searchable = {
            label
            for scenario in _GRADED
            if scenario.ground_truth is not None
            for label in (cause.value for cause in scenario.ground_truth.root_causes)
        } - _UNSEARCHABLE_LABELS
        assert len(searchable) >= 5, (
            f"the sweep can only look for {sorted(searchable)}; if the skip set "
            f"grew to cover most of the corpus's labels the sweep would be "
            f"green by construction."
        )

    def test_the_skip_set_is_exactly_the_two_non_diagnoses(self) -> None:
        """Widening it must be a visible edit, not a passing test.

        Every other label names a specific fault, so it IS an answer key.
        """
        assert sorted(_UNSEARCHABLE_LABELS) == ["no_fault", "unknown"]

    def test_the_renderer_cannot_be_handed_an_answer_key(self) -> None:
        """The structural half: the function's arguments cannot carry a label.

        ``format_selection_context`` takes a ``RunState`` and a candidate set.
        """
        joined = " ".join(
            str(parameter.annotation)
            for parameter in inspect.signature(format_selection_context).parameters.values()
        )
        for forbidden in ("Scenario", "GroundTruth", "DiscriminatingProbe"):
            assert forbidden not in joined

    def test_the_selector_is_its_own_accounting_role(self) -> None:
        """So the cost of selection is a number, not a share of the planner's.

        ``StepAccounting.selector_calls`` counts the calls (WP-6.2).
        """
        assert SELECTOR_ROLE == "candidate_selector"
        assert SELECTOR_ROLE != "investigation_planner"


class TestTheCallSendsNoTemperature:
    """Decision O-24, and the divergence from plan 04:161's "temperature 0".

    The newest model families reject ``temperature`` with a 400
    (``llm/client.SAMPLING_REJECTED_MODELS``), so requiring it would break a newer pin.
    """

    def test_neither_leg_of_the_call_sends_a_temperature(self) -> None:
        llm = CannedLLMClient([_payload()])
        select_candidate(llm, run_state=_state(), candidates=_pair(), model="m")
        assert llm.temperatures == [None]

    def test_there_is_no_parameter_to_send_one_with(self) -> None:
        """Not merely unset — unsettable, so no later caller can reintroduce it."""
        assert "temperature" not in inspect.signature(select_candidate).parameters

    def test_the_call_is_made_with_the_selector_prompt(self) -> None:
        llm = CannedLLMClient([_payload()])
        select_candidate(llm, run_state=_state(), candidates=_pair(), model="m")
        system_prompt, user_message = llm.calls[0]
        assert system_prompt == load_prompt(SELECTOR_PROMPT)
        assert CANDIDATES_HEADING in user_message

    def test_an_unresolvable_id_buys_one_repair_and_then_escalates(self) -> None:
        """ADR 0035 covers the selector because the validator raises inside the call.

        Two billed legs, then ``OutputRepairExhausted``.
        """
        bad = _payload(scores={"c1": 0.9, "c2": 0.2, "ghost": 0.1})
        llm = CannedLLMClient([bad, bad])
        with pytest.raises(OutputRepairExhausted):
            select_candidate(llm, run_state=_state(), candidates=_pair(), model="m")
        assert len(llm.calls) == 2
        assert llm.repair_of[0] is None

    def test_a_repaired_call_reports_its_failure(self) -> None:
        llm = CannedLLMClient([_payload(scores={"c1": 0.9}), _payload()])
        call = select_candidate(llm, run_state=_state(), candidates=_pair(), model="m")
        assert call.was_repaired
        assert len(call.failures) == 1
        assert call.result.output.decision is SelectionDecision.SELECT


class TestTheSelectorPointsAtOneCandidate:
    """``chosen_candidate_id`` — one spelling of the rule WP-6.2 reads.

    Plan 02's two rules cannot both be true; resolved here: ``select`` names a
    candidate, ``probe_more`` the highest-scored, ``escalate`` nothing.
    """

    def test_select_points_at_the_candidate_it_named(self) -> None:
        assert _validate(_payload(), _pair()).chosen_candidate_id == "c1"

    def test_probe_more_points_at_the_highest_scored_candidate(self) -> None:
        result = _validate(
            _payload(decision="probe_more", selected=None, scores={"c1": 0.3, "c2": 0.6}),
            _pair(),
        )
        assert result.chosen_candidate_id == "c2"

    def test_a_tie_on_probe_more_takes_the_first_stated(self) -> None:
        """Deterministic, and the order is the model's own.

        ``scores`` arrives as a JSON object, so iteration order is the order the model wrote.
        """
        result = _validate(
            _payload(decision="probe_more", selected=None, scores={"c1": 0.5, "c2": 0.5}),
            _pair(),
        )
        assert result.chosen_candidate_id == "c1"

    def test_escalate_points_at_nothing(self) -> None:
        result = _validate(_payload(decision="escalate", selected=None), _pair())
        assert result.chosen_candidate_id is None
