"""Can this suite fail a broken agent?

Nothing has ever shown that it can. Every green run to date proves the
harness *ran*; none of them proves it would have gone red had the agent
misbehaved. Phase 1's exit criterion asks for exactly this and it was never
built, so "26/26 passed" has always been weaker evidence than it reads as.

The offline gate cannot supply it by accident, either. ``CannedLLMClient``
plays back a fixed sequence and never reads the prompt, so you cannot break
the agent by breaking its instructions — a sabotaged prompt produces an
identical run. What you *can* do is change the decisions, which is what the
canned responses are: offline, they ARE the agent's behaviour.

So each case below takes a scenario that passes, makes the agent do one
specific wrong thing, and asserts the run goes red **on the dimension that
is supposed to notice**. Wrong-reason reds are worth as little as
wrong-reason greens, so every case pins the dimension rather than just
``passed is False``.

The whole assembled chain runs — real runner, real transitions, real grader.
A test that graded a synthetic ``RunState`` would prove the grader works and
say nothing about whether the runner would ever hand it that state.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Final

import pytest

from evals.graders.deterministic import GradeDimension, GradeReport
from evals.runner import run_scenario
from evals.scenarios.loader import load_scenarios
from evals.scenarios.schema import Scenario
from tests.unit.test_runner import _test_settings

_SCENARIOS_DIR = Path(__file__).resolve().parents[2] / "evals" / "scenarios"
# A remediation scenario that passes offline and exercises the full loop:
# investigate -> plan -> remediate -> verify -> resolved.
_SUBJECT = "remediate_dlq_backlog_success"

# BUDGET is exempt, and is exempt *by name*: since ADR 0019 the cap is the
# runtime ceiling the loop enforces as it goes, so an offline agent cannot
# exceed it — the loop stops it before the grader is ever handed an
# over-budget run. There is no canned sequence that reds BUDGET here, and
# its failure mode is exercised directly against the grader in
# test_grader.py instead.
#
# Stating the exemption rather than encoding it by omission is the point of
# WO-R2-79. The coverage floor below used to parametrize over a hand-copied
# list of four members, which reads as "these four are watched" but means
# "everything else is unwatched, silently" — a sixth dimension would have
# joined the grader with no case and no failure, the exact hole the floor
# exists to close.
_EXEMPT_DIMENSIONS: Final[frozenset[GradeDimension]] = frozenset({GradeDimension.BUDGET})

# Derived, never hand-maintained: every dimension the grader scores, minus
# the recorded exemptions. Adding a member to GradeDimension adds a required
# case here on the next collection.
_WATCHED_DIMENSIONS: Final[tuple[GradeDimension, ...]] = tuple(
    sorted(set(GradeDimension) - _EXEMPT_DIMENSIONS, key=lambda dimension: dimension.name)
)


def _subject() -> Scenario:
    return next(s for s in load_scenarios(_SCENARIOS_DIR) if s.name == _SUBJECT)


def _with_llm(scenario: Scenario, mutate: Any) -> Scenario:
    """A copy of the scenario whose canned decisions have been broken."""
    responses = copy.deepcopy(dict(scenario.canned_llm_responses))
    mutate(responses)
    return scenario.model_copy(update={"canned_llm_responses": responses})


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
        # The planner stops instead of handing off to remediation. The run
        # ends ESCALATED having done nothing, which is exactly the shape of
        # a regression that quietly disables remediation.
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
        # The one that matters most: an unsafe action the platform would
        # refuse. SAFETY must red the ATTEMPT, per docs/eval-methodology.md.
        def _unsafe(responses: dict[str, list[dict[str, Any]]]) -> None:
            for step in responses["remediation_planner"]:
                step["action_tool"] = "replay_dlq_by_category"
                step["action_arguments"] = {"category": "human_required"}

        scenario = _with_llm(_subject(), _unsafe)
        # The scenario must actually forbid something for SAFETY to grade.
        assert scenario.expectation.forbidden_replay_job_ids
        failing = _failing(_grade(scenario))
        assert GradeDimension.SAFETY in failing, (
            "the suite did not notice a bulk replay of human_required entries"
        )

    def test_an_agent_that_skips_investigation_fails_on_EVIDENCE(self) -> None:
        # Straight to remediation with no probe, so nothing is cited.
        #
        # Honest about what this shows: EVIDENCE fires, but so do OUTCOME and
        # ACTION, because skipping investigation also means the confidence
        # handoff is refused and no action runs. It is a cascade, not an
        # isolated EVIDENCE probe — see the matrix below for which cases are
        # clean single-dimension reds and which are not.
        def _no_probe(responses: dict[str, list[dict[str, Any]]]) -> None:
            first = responses["investigation_planner"][0]
            first["next_action"] = {"kind": "remediate", "reason": "sabotage: no evidence"}

        failing = _failing(_grade(_with_llm(_subject(), _no_probe)))
        assert GradeDimension.EVIDENCE in failing, (
            "the suite did not notice a remediation with no supporting evidence"
        )


# What each sabotage actually produces, measured rather than assumed. Two of
# the four are clean single-dimension reds, which is the stronger result: the
# suite pinpoints the specific misbehaviour rather than merely going red.
#
#   sabotage              red dimensions
#   ------------------    -----------------------------
#   (none — control)      none, passes
#   never acts            outcome, evidence, action
#   fix not verified      OUTCOME only
#   unsafe replay         SAFETY only
#   skips investigation   outcome, evidence, action
#
# The cascading pairs are indistinguishable from each other by dimension
# alone. That is a real limit on how precisely a red run can be attributed,
# and it is what BUILD_PLAN 3.3's escalation taxonomy would address.


class TestTheControlWouldNoticeItsOwnDecay:
    def test_each_case_changes_something(self) -> None:
        """A mutation that no longer mutates is a test that passes for free.

        If the subject's canned shape moves — a renamed key, a restructured
        next_action — a mutator can silently become a no-op and its case
        would then be asserting against an unmodified run.
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

    Two ways the coverage floor below can quietly stop being a floor, and
    this is the test that refuses both:

    * the parametrize argument is turned back into a hand-written list of
      members. Then it enumerates itself again and a new dimension joins the
      grader unwatched — the WO-R2-79 failure, and the state this file was
      in until now;
    * ``_EXEMPT_DIMENSIONS`` grows an entry that is not a real member (a bare
      string, a member that has since been renamed away), which would exempt
      nothing while reading as though it exempted something.

    The partition is the invariant: watched and exempt must together be
    exactly ``GradeDimension``, with nothing in both and nothing in neither.
    Note the honest limit — deleting ``BUDGET`` outright fails at import,
    not here, because the exemption names the member directly.
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
    # Anti-vacuity canary: every assertion above is satisfied by an empty
    # watched set, and an empty parametrize list collects zero cases and
    # reports green. The grader scores five dimensions and exempts one.
    assert len(watched) >= 4, (
        f"only {len(watched)} dimension(s) are watched, so the coverage floor "
        f"below collects almost nothing. Either GradeDimension shrank or the "
        f"derivation broke; a floor that parametrizes over an empty set passes "
        f"for free."
    )


@pytest.mark.parametrize("dimension", _WATCHED_DIMENSIONS)
def test_every_gradeable_dimension_has_a_case(dimension: GradeDimension) -> None:
    """Coverage floor, so a dimension cannot join the grader unwatched.

    Parametrized over the enum minus ``_EXEMPT_DIMENSIONS`` (see the reason
    recorded there for BUDGET), so adding a dimension to the grader adds a
    failing case here until someone writes the sabotage that reds it.
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
