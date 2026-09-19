"""WP-5.2 — ``best_of_n_enumerated``: the arm, its schema, and what it records.

One class per acceptance item: N is honoured and the bound is advertised as
``minItems``/``maxItems`` (the pair WP-5.1 recommended is ignored by a JSON-Schema
reader); N=1 reproduces ``baseline``'s trajectory; the whole set is recorded with
``branch_count`` following; and the WP-2.4 multiplier reaches this arm's ledger (C4).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from incident_commander.agent.accounting import RunAccounting
from incident_commander.agent.candidates import (
    DUPLICATE_CANDIDATE,
    NO_LEDGER_BOUND,
    exact_candidate_tuple,
)
from incident_commander.agent.factory import start_run
from incident_commander.agent.investigation import make_llm_investigate
from incident_commander.agent.planner_context import EVIDENCE_ID_PREFIX, format_planner_context
from incident_commander.agent.state import EvidenceEntry, IncidentState, RunState
from incident_commander.agent.strategies.baseline import BaselineStrategy
from incident_commander.agent.strategies.best_of_n_enumerated import (
    REJECTION_OTHER,
    BestOfNEnumeratedStrategy,
    candidate_step_model,
    rejection_class,
)
from incident_commander.agent.strategies.knobs import StrategyKnobs
from incident_commander.agent.strategies.names import StrategyName
from incident_commander.agent.strategies.protocol import InvestigationStrategy, StrategyContext
from incident_commander.agent.strategies.records import StepRecord
from incident_commander.agent.strategies.registry import STRATEGIES
from incident_commander.config import Settings
from incident_commander.llm.client import LLMClientProtocol
from incident_commander.llm.fakes import CannedLLMClient
from incident_commander.llm.prompts.loader import load_prompt
from incident_commander.tools.mcp_client import ToolResult

#: The Ns plan 02 § 11.3 reports pass@k for.
_REPORTED_NS: Final[tuple[int, ...]] = (1, 2, 4, 8)


# --------------------------------------------------------------------------
# Local fakes and payload builders.


class _FakeMCPClient:
    def __init__(self, handler: Callable[[str, Mapping[str, Any]], ToolResult]) -> None:
        self._handler = handler
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call_tool(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        timeout_seconds: float | None = None,
    ) -> ToolResult:
        self.calls.append((name, dict(arguments)))
        return self._handler(name, arguments)


def _lag_response(group: str = "billing", lag: int = 42) -> ToolResult:
    return ToolResult(
        content=[
            {
                "type": "text",
                "text": json.dumps(
                    {"consumer_group": group, "lag": lag, "lag_known": True, "source": "static"}
                ),
            }
        ]
    )


def _investigating(run_state: RunState) -> RunState:
    return run_state.model_copy(
        update={
            "state": IncidentState.INVESTIGATING,
            "alert": {"source": "kafka", "severity": "high", "group": "billing"},
        }
    )


def _evidence(tool: str = "get_consumer_lag", summary: str = "lag 42") -> EvidenceEntry:
    return EvidenceEntry(
        tool_name=tool,
        arguments={"consumer_group": "billing"},
        result_summary=summary,
        timestamp=datetime(2026, 7, 15, 20, 0, tzinfo=UTC),
    )


def _with_evidence(run_state: RunState, *entries: EvidenceEntry) -> RunState:
    return _investigating(run_state).model_copy(update={"evidence": entries})


def _candidate(
    candidate_id: str,
    *,
    category: str = "consumer_saturation",
    name: str | None = None,
    confidence: float = 0.9,
    cites: tuple[UUID, ...] = (),
    probe: str | None = None,
) -> dict[str, Any]:
    return {
        "candidate_id": candidate_id,
        "category": category,
        "name": name if name is not None else candidate_id,
        "confidence": confidence,
        "evidence_for": [{"evidence_id": str(value)} for value in cites],
        "evidence_against": [],
        "next_probe": (
            None
            if probe is None
            else {"tool_name": probe, "arguments": {"consumer_group": "billing"}}
        ),
    }


def _stop_set(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "candidates": candidates,
        "next_action": {"kind": "stop", "reason": "confidence sufficient"},
    }


def _probe_set(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "candidates": candidates,
        "next_action": {
            "kind": "probe",
            "tool_name": "get_consumer_lag",
            "arguments": {"consumer_group": "billing"},
        },
    }


def _baseline_payload(
    *, confidence: float = 0.9, action: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "hypotheses": [
            {
                "category": "consumer_saturation",
                "name": "c1",
                "confidence": confidence,
                "reasoning": "lag confirms saturation",
            }
        ],
        "next_action": action or {"kind": "stop", "reason": "confidence sufficient"},
    }


def _context(
    llm: LLMClientProtocol,
    *,
    iteration: int = 0,
    sink: list[StepRecord] | None = None,
) -> StrategyContext:
    return StrategyContext(
        llm_client=llm,
        model="m",
        iteration=iteration,
        record_step=None if sink is None else sink.append,
    )


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "anthropic_api_key": "sk-ant-test",
        "judge_model": "claude-haiku-4-5",
        "platform_mcp_url": "https://mcp.platform.local",
        "platform_rest_url": "https://api.platform.local",
        "platform_token": "svc-token",
        "platform_webhook_secret": "hmac-secret",
        "database_url": "postgresql://commander:commander@localhost:5432/commander",
    }
    return Settings(_env_file=None, **{**base, **overrides})  # type: ignore[call-arg]


def _trajectory(run: RunState) -> list[tuple[str, dict[str, Any]]]:
    """A run's evidence trail as (tool, arguments) — what it actually did.

    Not the whole ``RunState``: ``Hypothesis.reasoning`` cannot match between the arms
    (ADR 0042) and ids and timestamps differ per run.
    """
    return [(entry.tool_name, dict(entry.arguments)) for entry in run.evidence]


# --------------------------------------------------------------------------


class TestTheExactNSchema:
    """N is a bound, it is advertised, and a short set is refused not padded."""

    @pytest.mark.parametrize("n", _REPORTED_NS)
    def test_the_schema_demands_exactly_n(self, n: int) -> None:
        schema = candidate_step_model(n).model_json_schema()["properties"]["candidates"]
        assert schema["type"] == "array"
        # minItems/maxItems, NOT minLength/maxLength: the second pair means nothing here.
        assert (schema["minItems"], schema["maxItems"]) == (n, n)
        assert "minLength" not in schema and "maxLength" not in schema

    @pytest.mark.parametrize("n", _REPORTED_NS)
    def test_exactly_n_validates_and_the_neighbours_do_not(self, n: int) -> None:
        model = candidate_step_model(n)
        ledger = (_evidence(),)
        exact = [_candidate(f"c{i}", name=f"n{i}", confidence=0.5) for i in range(n)]
        with _grounded(ledger):
            assert len(model.model_validate(_stop_set(exact)).candidates) == n
            for wrong in (exact[:-1], [*exact, _candidate("extra", name="extra")]):
                if len(wrong) == n:
                    continue
                with pytest.raises(ValidationError):
                    model.model_validate(_stop_set(wrong))

    def test_a_short_set_is_rejected_rather_than_padded(self) -> None:
        """The whole point of ``max_length == min_length``.

        A padded set makes every pass@k over it wrong.
        """
        model = candidate_step_model(4)
        with _grounded((_evidence(),)), pytest.raises(ValidationError) as caught:
            model.model_validate(_stop_set([_candidate("c1"), _candidate("c2", name="n2")]))
        assert "too_short" in str(caught.value)

    def test_the_factory_refuses_a_set_of_none(self) -> None:
        with pytest.raises(ValueError, match="at least one candidate"):
            exact_candidate_tuple(0)

    def test_one_model_per_n(self) -> None:
        # Cached: the schema is rendered into every planner call, and rebuilding
        # it per step would make it a new object five times a run.
        assert candidate_step_model(4) is candidate_step_model(4)
        assert candidate_step_model(4) is not candidate_step_model(2)

    def test_the_generated_model_carries_no_class_docstring(self) -> None:
        """A class docstring becomes the schema's ``description`` and reaches the model."""
        model = candidate_step_model(2)
        assert model.__doc__ is None
        assert "description" not in model.model_json_schema()


class TestNIsOneReproducesBaseline:
    """The cheapest proof the seam is honest (order text, gotchas)."""

    def test_the_same_canned_world_produces_the_same_trajectory(
        self, run_state: RunState, now: datetime
    ) -> None:
        def world(_name: str, _arguments: Mapping[str, Any]) -> ToolResult:
            return _lag_response()

        probe = {
            "kind": "probe",
            "tool_name": "get_consumer_lag",
            "arguments": {"consumer_group": "billing"},
        }

        baseline_mcp = _FakeMCPClient(world)
        baseline_run = make_llm_investigate(
            baseline_mcp,
            CannedLLMClient([_baseline_payload(action=probe), _baseline_payload()]),
            model="m",
            strategy=BaselineStrategy(),
        )(_investigating(run_state), now)

        arm_mcp = _FakeMCPClient(world)
        arm_run = make_llm_investigate(
            arm_mcp,
            CannedLLMClient(
                [
                    _probe_set([_candidate("c1", confidence=0.9)]),
                    _stop_set([_candidate("c1", confidence=0.9)]),
                ]
            ),
            model="m",
            strategy=_arm(n=1),
        )(_investigating(run_state), now)

        assert arm_mcp.calls == baseline_mcp.calls
        assert _trajectory(arm_run) == _trajectory(baseline_run)
        assert arm_run.state is baseline_run.state is IncidentState.ESCALATED
        assert [(h.category, h.name, h.confidence) for h in arm_run.hypotheses] == [
            (h.category, h.name, h.confidence) for h in baseline_run.hypotheses
        ]

    def test_the_one_field_that_cannot_match_is_the_derived_reasoning(
        self, run_state: RunState, now: datetime
    ) -> None:
        """Named rather than papered over (ADR 0043).

        ``DiagnosisCandidate`` has no ``reasoning``; the arm derives it from citations.
        """
        llm = CannedLLMClient([_stop_set([_candidate("c1")])])
        _, step, _ = _arm(n=1).plan_next_step(_investigating(run_state), now, _context(llm))
        reasoning = step.hypotheses[0].reasoning
        assert reasoning.startswith("candidate c1 (enumerated)")
        assert "evidence_for: none" in reasoning


class TestTheEmittedStepIsOrdinary:
    """Every downstream gate behaves the same — exercised, not asserted."""

    def test_the_fix_map_gate_and_threshold_still_fire(
        self, run_state: RunState, now: datetime
    ) -> None:
        low = {
            "candidates": [_candidate("c1", confidence=0.5)],
            "next_action": {"kind": "remediate", "reason": "fix it"},
        }
        result = make_llm_investigate(
            _FakeMCPClient(lambda _n, _a: _lag_response()),
            CannedLLMClient([low]),
            model="m",
            strategy=_arm(n=1),
        )(_investigating(run_state), now)
        assert result.state is IncidentState.ESCALATED
        assert "below threshold" in result.evidence[-1].result_summary

    def test_an_unmapped_category_still_escalates_from_the_shared_gate(
        self, run_state: RunState, now: datetime
    ) -> None:
        """A top candidate outside ``FIX_MAP`` is stopped by the existing gate.

        Not special-cased inside the strategy.
        """
        unmapped = {
            "candidates": [
                _candidate("c1", category="db_query_latency", confidence=0.95),
                _candidate("c2", category="consumer_saturation", name="n2", confidence=0.2),
            ],
            "next_action": {"kind": "remediate", "reason": "fix it"},
        }
        result = make_llm_investigate(
            _FakeMCPClient(lambda _n, _a: _lag_response()),
            CannedLLMClient([unmapped]),
            model="m",
            strategy=_arm(n=2),
        )(_investigating(run_state), now)
        assert result.state is IncidentState.ESCALATED
        assert "no Tier-1 fix" in result.evidence[-1].result_summary

    def test_the_top_candidate_by_confidence_is_the_one_emitted(
        self, run_state: RunState, now: datetime
    ) -> None:
        # Stated out of order: the set is ranked at the schema boundary.
        payload = _stop_set(
            [
                _candidate("weak", name="weak", confidence=0.2),
                _candidate("strong", name="strong", confidence=0.95),
            ]
        )
        llm = CannedLLMClient([payload])
        _, step, record = _arm(n=2).plan_next_step(_investigating(run_state), now, _context(llm))
        assert step.hypotheses[0].name == "strong"
        assert [candidate.candidate_id for candidate in record.candidate_set] == ["strong", "weak"]


class TestTheWholeSetIsRecorded:
    """All N candidates in the ``StepRecord``, not just the winner."""

    @pytest.mark.parametrize("n", _REPORTED_NS)
    def test_every_candidate_reaches_the_record(
        self, n: int, run_state: RunState, now: datetime
    ) -> None:
        sink: list[StepRecord] = []
        payload = _stop_set(
            [_candidate(f"c{i}", name=f"n{i}", confidence=0.5) for i in range(n)],
        )
        _arm(n=n).plan_next_step(
            _investigating(run_state), now, _context(CannedLLMClient([payload]), sink=sink)
        )
        assert len(sink) == 1
        assert len(sink[0].candidate_set) == n
        assert sink[0].strategy == StrategyName.BEST_OF_N_ENUMERATED.value

    def test_the_citations_and_the_probe_reach_the_record(
        self, run_state: RunState, now: datetime
    ) -> None:
        entry = _evidence()
        state = _with_evidence(run_state, entry)
        payload = _stop_set(
            [_candidate("c1", cites=(entry.evidence_id,), probe="get_consumer_lag")]
        )
        _, _, record = _arm(n=1).plan_next_step(state, now, _context(CannedLLMClient([payload])))
        recorded = record.candidate_set[0]
        assert recorded.evidence_for == (str(entry.evidence_id),)
        assert recorded.proposed_probe == "get_consumer_lag"

    def test_branch_count_follows_from_the_set_with_no_new_accounting(
        self, run_state: RunState, now: datetime
    ) -> None:
        """WP-2.3's ``branch_count`` is candidates beyond the emitted one."""
        accounting = RunAccounting()
        payload = _stop_set([_candidate(f"c{i}", name=f"n{i}", confidence=0.5) for i in range(4)])
        make_llm_investigate(
            _FakeMCPClient(lambda _n, _a: _lag_response()),
            CannedLLMClient([payload]),
            model="m",
            strategy=_arm(n=4),
            record_step=accounting.step_sink(),
        )(_investigating(run_state), now)
        assert accounting.branch_count == 3
        # And no selector ran: this arm generates, it does not select.
        assert accounting.selector_calls == 0

    def test_the_record_serialises_for_a_tracer(self, run_state: RunState, now: datetime) -> None:
        payload = _stop_set([_candidate("c1"), _candidate("c2", name="n2", confidence=0.4)])
        _, _, record = _arm(n=2).plan_next_step(
            _investigating(run_state), now, _context(CannedLLMClient([payload]))
        )
        as_json = json.loads(json.dumps(record.as_trace_record(), default=str))
        assert len(as_json["candidate_set"]) == 2
        assert as_json["generation_rejections"] == []
        assert as_json["selector"] is None


class TestDuplicatesAndRejections:
    """A duplicate-heavy fake shows a non-zero rate — and by which definition."""

    def test_a_repeated_label_inside_one_set_is_refused_and_classified(
        self, run_state: RunState, now: datetime
    ) -> None:
        """Rejected by the schema, so it is a billed refusal and not a set.

        This is the half a "duplicate rate" computed over accepted sets can
        never see, which is why the record carries the refusal.
        """
        duplicate = _stop_set(
            [_candidate("c1", name="same"), _candidate("c2", name="same", confidence=0.4)]
        )
        clean = _stop_set([_candidate("c1", name="a"), _candidate("c2", name="b", confidence=0.4)])
        llm = CannedLLMClient([duplicate, clean])
        _, _, record = _arm(n=2).plan_next_step(_investigating(run_state), now, _context(llm))
        assert record.generation_rejections == ("duplicate_candidate",)
        # And the repair (ADR 0035) happened: two calls, one accepted set.
        assert len(llm.calls) == 2
        assert len(record.candidate_set) == 2

    def test_an_unrepairable_duplicate_run_escalates_and_is_charged(
        self, run_state: RunState, now: datetime
    ) -> None:
        duplicate = _stop_set(
            [_candidate("c1", name="same"), _candidate("c2", name="same", confidence=0.4)]
        )
        result = make_llm_investigate(
            _FakeMCPClient(lambda _n, _a: _lag_response()),
            CannedLLMClient([duplicate, duplicate]),
            model="m",
            strategy=_arm(n=2),
        )(_investigating(run_state), now)
        assert result.state is IncidentState.ESCALATED
        assert DUPLICATE_CANDIDATE in result.evidence[-1].result_summary

    @pytest.mark.parametrize(
        ("message", "expected"),
        [
            (f"x {DUPLICATE_CANDIDATE} y", "duplicate_candidate"),
            ("type=too_short, input=[]", "short_set"),
            ("type=too_long, input=[]", "long_set"),
            ("something else entirely", REJECTION_OTHER),
        ],
    )
    def test_a_rejection_is_classified_by_marker_not_by_prose(
        self, message: str, expected: str
    ) -> None:
        assert rejection_class(ValueError(message)) == expected


class TestEvidenceIdsAreOnThePage:
    """The citation rule is askable — and ``baseline``'s bytes did not move."""

    def test_the_arm_sees_the_ids(self, run_state: RunState) -> None:
        entry = _evidence()
        rendered = format_planner_context(_with_evidence(run_state, entry), show_evidence_ids=True)
        assert f"{EVIDENCE_ID_PREFIX}{entry.evidence_id}" in rendered

    def test_baseline_does_not_and_the_default_is_off(self, run_state: RunState) -> None:
        state = _with_evidence(run_state, _evidence())
        assert EVIDENCE_ID_PREFIX not in format_planner_context(state)
        assert format_planner_context(state) == format_planner_context(
            state, show_evidence_ids=False
        )

    def test_the_id_column_is_the_only_difference(self, run_state: RunState) -> None:
        """Nothing else about the context moves with the flag.

        ADR 0043 accepts one ``evidence_id=<uuid> `` prefix per line.
        """
        entry = _evidence()
        state = _with_evidence(run_state, entry)
        off = format_planner_context(state)
        on = format_planner_context(state, show_evidence_ids=True)
        assert on.replace(f"{EVIDENCE_ID_PREFIX}{entry.evidence_id} ", "") == off

    def test_a_citation_that_names_no_entry_is_refused(
        self, run_state: RunState, now: datetime
    ) -> None:
        state = _with_evidence(run_state, _evidence())
        invented = _stop_set([_candidate("c1", cites=(uuid4(),))])
        llm = CannedLLMClient([invented, invented])
        with pytest.raises(Exception) as caught:
            _arm(n=1).plan_next_step(state, now, _context(llm))
        assert "names no entry" in str(caught.value)

    def test_the_ledger_binding_does_not_leak_past_the_call(
        self, run_state: RunState, now: datetime
    ) -> None:
        """``grounded_in`` is scoped to the call, so a later validation refuses."""
        state = _with_evidence(run_state, _evidence())
        _arm(n=1).plan_next_step(
            state, now, _context(CannedLLMClient([_stop_set([_candidate("c1")])]))
        )
        with pytest.raises(ValidationError, match=NO_LEDGER_BOUND):
            candidate_step_model(1).model_validate(_stop_set([_candidate("c1")]))

    def test_the_addendum_prompt_tells_the_model_to_cite_those_ids(self) -> None:
        addendum = load_prompt("investigation_planner_best_of_n")
        assert EVIDENCE_ID_PREFIX in addendum
        assert "fails validation" in addendum

    def test_the_arm_prompt_is_the_planner_prompt_plus_the_addendum(self) -> None:
        arm = _arm(n=2)
        prompt = arm._system_prompt
        assert prompt.startswith(load_prompt("investigation_planner").rstrip())
        assert prompt.endswith(load_prompt("investigation_planner_best_of_n"))


class TestTheBudgetMultiplierReachesTheArm:
    """Decision C4: an N-arm on a one-candidate ceiling reads as a BUDGET failure."""

    def test_the_multiplier_seeds_the_arms_ledger(self, now: datetime) -> None:
        settings = _settings(
            inference_strategy="best_of_n_enumerated",
            best_of_n=4,
            token_budget_multiplier="4",
            usd_budget_multiplier="4",
        )
        assert settings.seeded_max_tokens == settings.budget_max_tokens * 4
        assert settings.seeded_max_usd == settings.budget_max_usd * 4
        run = start_run({"source": "kafka"}, settings, now)
        assert run.budget.max_tokens == settings.budget_max_tokens * 4

    def test_the_arm_stamps_the_n_a_budget_result_has_to_be_read_beside(self) -> None:
        """Not the ratio: that has one reader, ``config.py`` (see ``knobs.py``)."""
        arm = STRATEGIES.create(StrategyName.BEST_OF_N_ENUMERATED, StrategyKnobs(n=8))
        assert dict(arm.config) == {"n": 8, "evidence_ids_rendered": True}

    def test_the_stamp_cannot_be_written_through(self) -> None:
        arm = _arm(n=2)
        with pytest.raises(TypeError):
            arm.config["n"] = 8  # type: ignore[index]


class TestTheArmIsConfigured:
    """N and the arm both come from config, and neither defaults to spending."""

    def test_the_registry_builds_it_with_the_knobs(self) -> None:
        arm = STRATEGIES.create("best_of_n_enumerated", StrategyKnobs(n=4))
        assert isinstance(arm, BestOfNEnumeratedStrategy)
        assert arm.n == 4

    def test_no_knobs_means_the_control_groups_shape(self) -> None:
        # A default of 8 would make a caller who forgot the configuration spend
        # eight times as much and believe it had measured something.
        assert STRATEGIES.create("best_of_n_enumerated").config["n"] == 1
        assert BestOfNEnumeratedStrategy().n == 1

    def test_the_setting_is_bounded_and_defaults_to_one(self) -> None:
        assert _settings().best_of_n == 1
        for out_of_range in (0, 9):
            with pytest.raises(ValidationError):
                _settings(best_of_n=out_of_range)

    def test_the_bare_env_var_sets_n(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BEST_OF_N", "4")
        assert _settings().best_of_n == 4

    def test_the_runner_builds_the_knobs_from_settings(self) -> None:
        from evals.runner import strategy_knobs

        knobs = strategy_knobs(_settings(best_of_n=2, token_budget_multiplier="2"))
        assert knobs.n == 2
        # And the multiplier did NOT come with it: one reader, in config.py.
        assert not hasattr(knobs, "token_budget_multiplier")

    def test_it_satisfies_the_protocol(self) -> None:
        strategy: InvestigationStrategy = BestOfNEnumeratedStrategy(StrategyKnobs(n=2))
        assert strategy.name == "best_of_n_enumerated"

    def test_baseline_is_still_the_default(self) -> None:
        assert _settings().inference_strategy is StrategyName.BASELINE


# --------------------------------------------------------------------------
# Helpers that need the imports above.


def _arm(*, n: int) -> BestOfNEnumeratedStrategy:
    return BestOfNEnumeratedStrategy(StrategyKnobs(n=n))


def _grounded(evidence: tuple[EvidenceEntry, ...]) -> Any:
    from incident_commander.agent.candidates import grounded_in

    return grounded_in(evidence)
