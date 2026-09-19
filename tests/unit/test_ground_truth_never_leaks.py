"""The benchmark's trust boundary, asserted: hidden ground truth stays hidden.

``ground_truth`` and ``discriminating_probes`` are the answer key (plan 00 § 3.1). Two
mechanisms keep them out: ``Scenario.agent_visible`` is an allow-list projection onto an
``extra="forbid"`` model, so a new field is invisible by construction; and every
agent-visible prompt renders identically with and without a maximal ground truth.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Final
from uuid import UUID

import pytest

from evals.runner import _eval_defaults
from evals.scenarios.loader import load_scenarios
from evals.scenarios.schema import (
    AgentVisibleScenario,
    DiscriminatingProbe,
    GroundTruth,
    Scenario,
)
from incident_commander.agent.briefing import render_briefing
from incident_commander.agent.briefing_enrichment import _format_context as _format_writer_context
from incident_commander.agent.factory import start_run
from incident_commander.agent.hypothesis import Hypothesis, HypothesisCategory
from incident_commander.agent.planner_context import format_planner_context
from incident_commander.agent.remediation import (
    RemediationPlan,
    _action_result_of,
    _format_plan_context,
    format_verify_context,
)
from incident_commander.agent.state import EvidenceEntry, IncidentState, RunState

_SCENARIOS_DIR: Final[Path] = Path(__file__).resolve().parents[2] / "evals" / "scenarios"

# Fixed, so two renderings differ only where the subject under test makes them differ.
_AT: Final[datetime] = datetime(2026, 9, 15, 12, 0, 0, tzinfo=UTC)
_INCIDENT_ID: Final[UUID] = UUID("00000000-0000-4000-8000-00000000d0d0")

_HYPOTHESIS: Final[Hypothesis] = Hypothesis(
    category=HypothesisCategory.UNKNOWN,
    name="placeholder ranking so the remediation planner has a target",
    confidence=0.9,
    reasoning="fixed across renderings; the subject under test is the scenario, not this",
)
_PLAN: Final[RemediationPlan] = RemediationPlan(
    target_hypothesis=_HYPOTHESIS.name,
    action_tool="replay_dlq_by_ids",
    action_arguments={"job_ids": ["00000000-0000-4000-8000-000000000001"]},
    verify_tool="list_dlq_messages",
    verify_arguments={"limit": 10},
    verify_expectation="the replayed row leaves the queue",
)

# Values that cannot occur in a scenario by accident, so a substring hit is
# a leak and never a coincidence.
_SENTINEL_COMPONENT: Final[str] = "gt-canary-component-1f4c9a"
_SENTINEL_CHAIN: Final[str] = "gt-canary-chain-1f4c9a"
_SENTINEL_PATTERN: Final[str] = "^gt-canary-probe-1f4c9a$"
_SENTINELS: Final[tuple[str, ...]] = (
    _SENTINEL_COMPONENT,
    _SENTINEL_CHAIN,
    "gt-canary-probe-1f4c9a",
)
# The field names themselves. A dump of the evaluator record would carry
# these even if every value in it were innocuous.
_LEAK_KEYS: Final[tuple[str, ...]] = (
    "ground_truth",
    "discriminating_probes",
    "incident_count",
    "root_causes",
    "affected_components",
    "causal_chain",
    "argument_pattern",
)

# Every category except NO_FAULT, which GroundTruth refuses to pair with a fault:
# the per-label assertion covers the taxonomy in one pass.
_ALL_FAULT_CATEGORIES: Final[tuple[HypothesisCategory, ...]] = tuple(
    category for category in HypothesisCategory if category is not HypothesisCategory.NO_FAULT
)

_MAXIMAL_GROUND_TRUTH: Final[GroundTruth] = GroundTruth(
    incident_count=len(_ALL_FAULT_CATEGORIES),
    root_causes=_ALL_FAULT_CATEGORIES,
    affected_components=(_SENTINEL_COMPONENT,),
    causal_chain=(_SENTINEL_CHAIN,),
)
_MAXIMAL_PROBES: Final[tuple[DiscriminatingProbe, ...]] = (
    DiscriminatingProbe(
        tool="list_dlq_messages",
        argument_pattern={"category": _SENTINEL_PATTERN},
    ),
)

_CORPUS: Final[list[Scenario]] = load_scenarios(_SCENARIOS_DIR)
_CORPUS_IDS: Final[list[str]] = [scenario.name for scenario in _CORPUS]


def _run_state_for(scenario: Scenario) -> RunState:
    """A run built from the scenario's projection, carrying the world it serves.

    The canned tool responses go onto the evidence ledger, as in a real offline run, because
    the ledger is rendered verbatim into three prompts.
    """
    visible = scenario.agent_visible()
    run = start_run(
        visible.alert,
        _eval_defaults(),
        _AT,
        incident_id=_INCIDENT_ID,
        max_tool_calls=visible.max_tool_calls,
    )
    evidence = tuple(
        EvidenceEntry(
            evidence_id=UUID(int=index + 1),
            tool_name=tool,
            arguments={},
            result_summary=json.dumps(
                [response.model_dump(mode="json")]
                if not isinstance(response, tuple)
                else [item.model_dump(mode="json") for item in response],
                sort_keys=True,
            ),
            timestamp=_AT,
        )
        for index, (tool, response) in enumerate(sorted(visible.canned_tool_responses.items()))
    )
    return run.model_copy(
        update={
            "state": IncidentState.ESCALATED,
            "evidence": evidence,
            "hypotheses": (_HYPOTHESIS,),
        }
    )


def rendered_agent_contexts(scenario: Scenario) -> dict[str, str]:
    """Every prompt context an LLM playing the agent is shown, for one scenario.

    Keyed by the prompt each one is paired with, so a failure names the
    prompt a reviewer has to open.
    """
    run = _run_state_for(scenario)
    probe_summary = "\n".join(entry.result_summary for entry in run.evidence)
    briefing = render_briefing(run)
    return {
        "investigation_planner": format_planner_context(run),
        # The best-of-N arm's context is the same render with the evidence-id column on
        # (WP-5.2, ADR 0043): the flag adds text to every evidence line.
        "investigation_planner_best_of_n": format_planner_context(run, show_evidence_ids=True),
        "remediation_planner": _format_plan_context(run, _HYPOTHESIS.name),
        "verification_judge": format_verify_context(
            _PLAN, probe_summary, _action_result_of(run, _PLAN)
        ),
        "briefing_writer": _format_writer_context(briefing),
    }


def assert_no_ground_truth_leak(scenario: Scenario) -> None:
    """Attaching an answer key to ``scenario`` must change nothing the agent sees.

    Shared by the corpus sweep and by the red-before, so the two cannot drift into
    checking different things.
    """
    plain = rendered_agent_contexts(scenario)
    with_answer_key = rendered_agent_contexts(
        scenario.model_copy(
            update={
                "ground_truth": _MAXIMAL_GROUND_TRUTH,
                "discriminating_probes": _MAXIMAL_PROBES,
            }
        )
    )

    for prompt, text in sorted(with_answer_key.items()):
        for token in (*_LEAK_KEYS, *_SENTINELS):
            assert token not in text, (
                f"scenario {scenario.name!r}: the {prompt} context contains "
                f"{token!r}, which only exists on the evaluator's record of what "
                f"was actually wrong. Ground truth reached a prompt — plan 00 "
                f"§ 3.1's trust boundary is the benchmark."
            )

    for prompt, text in sorted(plain.items()):
        assert with_answer_key[prompt] == text, (
            f"scenario {scenario.name!r}: the {prompt} context changed when a "
            f"ground truth was attached to the scenario, so something the agent "
            f"reads is derived from the answer key. Build the context from "
            f"Scenario.agent_visible, never from the Scenario itself."
        )

    for prompt, text in sorted(with_answer_key.items()):
        for category in HypothesisCategory:
            if category.value in text:
                assert category.value in plain[prompt], (
                    f"scenario {scenario.name!r}: the root-cause label "
                    f"{category.value!r} appears in the {prompt} context only "
                    f"once the scenario declares a ground truth. The agent is "
                    f"being told the answer it is graded on naming."
                )


@pytest.mark.parametrize("scenario", _CORPUS, ids=_CORPUS_IDS)
def test_no_scenario_leaks_its_ground_truth_into_any_prompt(scenario: Scenario) -> None:
    """The corpus sweep. Parameterised, so a new scenario is covered on arrival.

    Deliberately indifferent to whether the scenario ships a ground truth: none do yet, so
    the test supplies one rather than passing vacuously.
    """
    assert_no_ground_truth_leak(scenario)


def test_the_corpus_is_not_empty() -> None:
    """Canary: a parameterised sweep over an empty list is a green no-op."""
    assert len(_CORPUS) >= 41, (
        f"only {len(_CORPUS)} scenarios loaded from {_SCENARIOS_DIR}; the sweep "
        "above would be nearly vacuous. The suite had 41 when this was written."
    )


class TestTheProjectionIsTheOnlyWayIn:
    """The structural half: what makes the sweep above stay true tomorrow."""

    def test_agent_visible_carries_only_allow_listed_fields(self) -> None:
        projected = set(AgentVisibleScenario.model_fields)
        # ``max_tool_calls`` is lifted out of the graded expectation, not a Scenario field.
        assert projected - {"max_tool_calls"} == Scenario.AGENT_VISIBLE_FIELDS, (
            f"AgentVisibleScenario projects {sorted(projected)} but "
            f"Scenario.AGENT_VISIBLE_FIELDS declares "
            f"{sorted(Scenario.AGENT_VISIBLE_FIELDS)}. The two are the same list "
            "written twice; keep them equal or the declaration stops describing "
            "the projection."
        )

    def test_the_projection_refuses_an_undeclared_field(self) -> None:
        """``extra='forbid'`` is what makes widening the boundary deliberate."""
        with pytest.raises(ValueError, match="ground_truth"):
            AgentVisibleScenario(
                alert={},
                ground_truth={"incident_count": 1},  # type: ignore[call-arg]
            )

    def test_ground_truth_is_on_the_evaluator_side(self) -> None:
        assert "ground_truth" in Scenario.EVALUATOR_ONLY_FIELDS
        assert "discriminating_probes" in Scenario.EVALUATOR_ONLY_FIELDS
        assert "ground_truth" not in Scenario.AGENT_VISIBLE_FIELDS
        assert "discriminating_probes" not in Scenario.AGENT_VISIBLE_FIELDS

    def test_a_field_on_neither_side_fails_at_import(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The import-time partition check, exercised.

        ``_classify_every_scenario_field`` runs at import, so seeing it fire means calling
        it again.
        """
        from evals.scenarios.schema import _classify_every_scenario_field

        monkeypatch.setattr(
            Scenario,
            "EVALUATOR_ONLY_FIELDS",
            Scenario.EVALUATOR_ONLY_FIELDS - {"ground_truth"},
        )
        with pytest.raises(RuntimeError, match="ground_truth"):
            _classify_every_scenario_field()


class TestTheLeakCheckItselfFails:
    """Red-before. A test that cannot fail proves nothing about what it guards."""

    def test_a_projection_that_carries_ground_truth_is_caught(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Break the projection the way an exclusion-list design would break.

        The realistic regression is a projection built by *dumping* the scenario and removing
        known-secret keys, where the next field added is included by default.
        """
        honest = Scenario.agent_visible

        def leaky(self: Scenario) -> AgentVisibleScenario:
            visible = honest(self)
            if self.ground_truth is None:
                return visible
            return visible.model_copy(
                update={
                    "alert": {
                        **visible.alert,
                        "ground_truth": self.ground_truth.model_dump(mode="json"),
                    }
                }
            )

        monkeypatch.setattr(Scenario, "agent_visible", leaky)
        with pytest.raises(AssertionError, match="ground_truth"):
            assert_no_ground_truth_leak(_CORPUS[0])

    def test_the_sweep_is_green_on_the_honest_projection(self) -> None:
        """The other half of the red-before pair: unpatched, the same call passes."""
        assert_no_ground_truth_leak(_CORPUS[0])
