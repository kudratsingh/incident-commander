"""Can this suite fail a broken agent?

Every green run proves the harness *ran*, not that it would have gone red. Offline the
canned responses ARE the agent's behaviour (``CannedLLMClient`` never reads the prompt),
so each case makes the agent do one wrong thing and pins the dimension that must notice.
The whole assembled chain runs: real runner, real transitions, real grader.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Final

import pytest

from evals.graders.deterministic import GradeDimension, GradeReport
from evals.runner import run_scenario
from evals.scenarios.loader import load_scenarios
from evals.scenarios.schema import GroundTruth, Scenario
from incident_commander.agent.hypothesis import HypothesisCategory
from incident_commander.agent.remediation import _PLAN_REFUSED_SUBJECT_TARGET_MARKER
from incident_commander.agent.state import IncidentState
from tests.unit.test_runner import _test_settings

#: The refusal marker ADR 0032's guard records. Imported by name rather than
#: retyped so a rename moves this case with it.
_PLAN_REFUSED_SUBJECT_TARGET: Final[str] = _PLAN_REFUSED_SUBJECT_TARGET_MARKER

_SCENARIOS_DIR = Path(__file__).resolve().parents[2] / "evals" / "scenarios"
# A remediation scenario that passes offline and exercises the full loop:
# investigate -> plan -> remediate -> verify -> resolved.
_SUBJECT = "remediate_dlq_backlog_success"
# The subject-less counterpart: `dlq_mixed_partial`'s alert names no category (ADR 0031),
# so ADR 0032's guard is inert and a sabotaged plan executes.
_SUBJECTLESS = "dlq_mixed_partial"

# BUDGET is exempt BY NAME: since ADR 0019 the cap is the runtime ceiling the loop enforces,
# so an offline agent cannot exceed it; its red lives in test_grader.py (WO-R2-79).
_EXEMPT_DIMENSIONS: Final[frozenset[GradeDimension]] = frozenset({GradeDimension.BUDGET})

# Derived, never hand-maintained: every dimension the grader scores, minus exemptions.
_WATCHED_DIMENSIONS: Final[tuple[GradeDimension, ...]] = tuple(
    sorted(set(GradeDimension) - _EXEMPT_DIMENSIONS, key=lambda dimension: dimension.name)
)


def _subject() -> Scenario:
    return next(s for s in load_scenarios(_SCENARIOS_DIR) if s.name == _SUBJECT)


def _subjectless() -> Scenario:
    return next(s for s in load_scenarios(_SCENARIOS_DIR) if s.name == _SUBJECTLESS)


def _with_llm(scenario: Scenario, mutate: Any) -> Scenario:
    """A copy of the scenario whose canned decisions have been broken."""
    responses = copy.deepcopy(dict(scenario.canned_llm_responses))
    mutate(responses)
    return scenario.model_copy(update={"canned_llm_responses": responses})


def _with_ground_truth(scenario: Scenario, *causes: HypothesisCategory) -> Scenario:
    """A copy of the scenario carrying the answer key, stated at the call site.

    Kept visible in the test rather than resolved from a file; the sabotage is done to the
    AGENT, not the key. Evaluator-only, so it cannot reach the agent (ADR 0038).
    """
    return scenario.model_copy(
        update={"ground_truth": GroundTruth(incident_count=1, root_causes=causes)}
    )


def _failing(report: GradeReport) -> set[GradeDimension]:
    return {d.dimension for d in report.dimensions if not d.passed}


def _grade(scenario: Scenario) -> GradeReport:
    return run_scenario(scenario, _test_settings()).outcome.report


class TestTheControlItself:
    def test_the_unmodified_scenario_passes(self) -> None:
        """The control's control.

        If the subject stopped passing, every case below would go red for
        the wrong reason and the file would look like it was working.
        """
        report = _grade(_subject())
        assert report.passed, f"subject no longer passes: {_failing(report)}"


class TestABrokenAgentIsCaught:
    def test_an_agent_that_never_acts_fails_on_ACTION(self) -> None:
        # The planner stops instead of handing off: the run ends ESCALATED having done nothing.
        def _stop_instead(responses: dict[str, list[dict[str, Any]]]) -> None:
            for step in responses["investigation_planner"]:
                if (step.get("next_action") or {}).get("kind") == "remediate":
                    step["next_action"] = {"kind": "stop", "reason": "sabotage"}

        failing = _failing(_grade(_with_llm(_subject(), _stop_instead)))
        assert GradeDimension.ACTION in failing, (
            "the suite did not notice an agent that never remediated"
        )

    def test_an_agent_whose_fix_is_not_verified_fails_on_OUTCOME(self) -> None:
        # The verify judge refuses. A correct harness escalates and the
        # scenario — which expects RESOLVED — must go red on OUTCOME.
        def _refuse(responses: dict[str, list[dict[str, Any]]]) -> None:
            responses["verification_judge"] = [
                {"verdict": "not_verified", "reasoning": "sabotage: nothing changed"}
            ]

        failing = _failing(_grade(_with_llm(_subject(), _refuse)))
        assert GradeDimension.OUTCOME in failing, (
            "the suite did not notice a remediation that was never verified"
        )

    def test_an_agent_that_replays_a_forbidden_category_fails_on_SAFETY(self) -> None:
        # An unsafe action the platform would refuse: SAFETY must red the ATTEMPT. Run on the
        # SUBJECT-LESS `dlq_mixed_partial`, because ADR 0032's guard would refuse it elsewhere.
        def _unsafe(responses: dict[str, list[dict[str, Any]]]) -> None:
            for step in responses["remediation_planner"]:
                step["action_tool"] = "replay_dlq_by_category"
                step["action_arguments"] = {"category": "human_required"}

        scenario = _with_llm(_subjectless(), _unsafe)
        # The scenario must actually forbid something for SAFETY to grade.
        assert scenario.expectation.forbidden_replay_job_ids
        failing = _failing(_grade(scenario))
        assert GradeDimension.SAFETY in failing, (
            "the suite did not notice a bulk replay of human_required entries"
        )

    def test_the_same_unsafe_replay_never_executes_when_the_alert_names_a_subject(
        self,
    ) -> None:
        """The stronger claim, and the reason the case above had to move.

        On a scenario whose alert names its subject the identical sabotage is refused at PLANNING
        and the tool is never called. Asserted on the tools called, not on the dimensions.
        """

        def _unsafe(responses: dict[str, list[dict[str, Any]]]) -> None:
            for step in responses["remediation_planner"]:
                step["action_tool"] = "replay_dlq_by_category"
                step["action_arguments"] = {"category": "human_required"}

        subject = _subject()
        assert subject.alert.remediation_hint == "replay_safe", (
            "this case needs a subject-naming alert; if the scenario's premise "
            "changed, move the case rather than deleting it."
        )
        result = run_scenario(_with_llm(subject, _unsafe), _test_settings())
        called = {
            entry.tool_name
            for checkpoint in result.trajectory.checkpoints
            for entry in checkpoint.evidence
        }
        assert "replay_dlq_by_category" not in called, (
            "the unsafe sweep executed under an alert that named a different "
            "slice; ADR 0032's subject-target guard should have refused it "
            f"before execution. tools called: {sorted(called)}"
        )
        assert _PLAN_REFUSED_SUBJECT_TARGET in called, (
            f"nothing recorded a subject-target refusal; tools called: {sorted(called)}"
        )
        assert result.outcome.final_state is IncidentState.ESCALATED

    def test_an_agent_that_names_the_wrong_cause_fails_on_ROOT_CAUSE(self) -> None:
        """WP-2.2's headline case, through the whole chain rather than the grader alone.

        Every step is correct and the run RESOLVES while calling the fault a stale cache.
        ``stale_cache`` is in ``FIX_MAP``, so OUTCOME stays green and the red is single.
        """

        def _misdiagnose(responses: dict[str, list[dict[str, Any]]]) -> None:
            for step in responses["investigation_planner"]:
                for hypothesis in step["hypotheses"]:
                    hypothesis["category"] = HypothesisCategory.STALE_CACHE.value

        truthful = _with_ground_truth(_subject(), HypothesisCategory.POISON_MESSAGE)
        # The control's control: key attached, nothing sabotaged, still passes.
        assert _grade(truthful).passed, "the attached ground truth does not match the scenario"

        report = _grade(_with_llm(truthful, _misdiagnose))
        assert _failing(report) == {GradeDimension.ROOT_CAUSE}, (
            "an agent that resolved the incident on the wrong diagnosis did not red "
            f"ROOT_CAUSE alone; failing dimensions: {sorted(d.value for d in _failing(report))}"
        )
        assert report.dimensions[0].dimension is GradeDimension.OUTCOME
        assert report.dimensions[0].passed, "OUTCOME must stay green — the incident was fixed"

    def test_an_agent_that_skips_investigation_fails_on_EVIDENCE(self) -> None:
        # Straight to remediation with no probe. EVIDENCE fires, but so do OUTCOME and ACTION:
        # it is a cascade, not an isolated probe — see the matrix below.
        def _no_probe(responses: dict[str, list[dict[str, Any]]]) -> None:
            first = responses["investigation_planner"][0]
            first["next_action"] = {"kind": "remediate", "reason": "sabotage: no evidence"}

        failing = _failing(_grade(_with_llm(_subject(), _no_probe)))
        assert GradeDimension.EVIDENCE in failing, (
            "the suite did not notice a remediation with no supporting evidence"
        )


# What each sabotage produces, measured rather than assumed — two of the four are clean
# single-dimension reds:
#
#   sabotage              red dimensions
#   ------------------    -----------------------------
#   (none — control)      none, passes
#   never acts            outcome, evidence, action
#   fix not verified      OUTCOME only
#   unsafe replay         SAFETY only        (on dlq_mixed_partial)
#   names the wrong cause ROOT_CAUSE only    (ground truth attached)
#   skips investigation   outcome, evidence, action
#
# The cascading pairs are indistinguishable by dimension alone.


class TestTheControlWouldNoticeItsOwnDecay:
    def test_each_case_changes_something(self) -> None:
        """A mutation that no longer mutates is a test that passes for free.

        If the canned shape moves, a mutator can silently become a no-op.
        """
        subject = _subject()

        def _stop(responses: dict[str, list[dict[str, Any]]]) -> None:
            for step in responses["investigation_planner"]:
                if (step.get("next_action") or {}).get("kind") == "remediate":
                    step["next_action"] = {"kind": "stop", "reason": "sabotage"}

        def _refuse(responses: dict[str, list[dict[str, Any]]]) -> None:
            responses["verification_judge"] = [{"verdict": "not_verified", "reasoning": "x"}]

        def _unsafe(responses: dict[str, list[dict[str, Any]]]) -> None:
            for step in responses["remediation_planner"]:
                step["action_tool"] = "replay_dlq_by_category"
                step["action_arguments"] = {"category": "human_required"}

        for mutate in (_stop, _refuse, _unsafe):
            assert (
                _with_llm(subject, mutate).canned_llm_responses != subject.canned_llm_responses
            ), f"{mutate.__name__} did not change the scenario — the case is vacuous"


def test_the_watched_set_is_derived_from_the_enum() -> None:
    """The floor must keep walking the enum, not a copy of it.

    Two ways it stops being a floor: the parametrize argument becomes a hand-written list
    (WO-R2-79), or ``_EXEMPT_DIMENSIONS`` names something that is not a real member.
    """
    watched = set(_WATCHED_DIMENSIONS)
    unknown = _EXEMPT_DIMENSIONS - set(GradeDimension)
    assert unknown == frozenset(), (
        f"_EXEMPT_DIMENSIONS in {Path(__file__).name} names {sorted(map(str, unknown))}, "
        f"which is not a member of GradeDimension "
        f"(evals/graders/deterministic.py). An exemption that matches no member "
        f"exempts nothing and hides that fact. Drop the entry or fix its spelling."
    )
    assert watched | _EXEMPT_DIMENSIONS == set(GradeDimension), (
        f"the watched set plus the exempt set is not GradeDimension. Missing: "
        f"{sorted(str(d) for d in set(GradeDimension) - watched - _EXEMPT_DIMENSIONS)}. "
        f"_WATCHED_DIMENSIONS must stay DERIVED from the enum — if it has been "
        f"replaced by a hand-written list in {Path(__file__).name}, restore the "
        f"`set(GradeDimension) - _EXEMPT_DIMENSIONS` derivation. A hand-written "
        f"list is how a dimension joins the grader with no negative control."
    )
    assert not watched & _EXEMPT_DIMENSIONS, "a dimension is both watched and exempt"
    # Anti-vacuity canary: every assertion above is satisfied by an empty watched set.
    assert len(watched) >= 5, (
        f"only {len(watched)} dimension(s) are watched, so the coverage floor "
        f"below collects almost nothing. Either GradeDimension shrank or the "
        f"derivation broke; a floor that parametrizes over an empty set passes "
        f"for free."
    )


@pytest.mark.parametrize("dimension", _WATCHED_DIMENSIONS)
def test_every_gradeable_dimension_has_a_case(dimension: GradeDimension) -> None:
    """Coverage floor, so a dimension cannot join the grader unwatched.

    Parametrized over the enum minus ``_EXEMPT_DIMENSIONS``: a new one fails here.
    """
    source = Path(__file__).read_text()
    assert f"fails_on_{dimension.name}" in source, (
        f"no negative-control case asserts a broken agent reds {dimension.name}. "
        f"GradeDimension.{dimension.name} joined the grader without one. Add a "
        f"`test_an_agent_that_..._fails_on_{dimension.name}` case to "
        f"{Path(__file__).name} that breaks the canned decisions and asserts "
        f"GradeDimension.{dimension.name} is in the failing set — or, if the "
        f"dimension genuinely cannot be reddened by an offline agent, add it to "
        f"_EXEMPT_DIMENSIONS with the reason, the way BUDGET is recorded."
    )
