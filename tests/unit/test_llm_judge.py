from datetime import datetime

import pytest
from pydantic import ValidationError

from evals.graders.llm_judge import USEFUL_THRESHOLD, JudgeScore, judge_briefing
from incident_commander.agent.briefing import (
    AttemptedAction,
    EscalationBriefing,
    ProbeSummary,
    render_briefing,
)
from incident_commander.agent.state import EvidenceEntry, IncidentState, RunState
from incident_commander.llm.fakes import CannedLLMClient


def _evidence(now: datetime, tool: str, summary: str) -> EvidenceEntry:
    return EvidenceEntry(
        tool_name=tool,
        arguments={},
        result_summary=summary,
        timestamp=now,
    )


def _sample_briefing(run_state: RunState, now: datetime) -> EscalationBriefing:
    evidence = (_evidence(now, "get_consumer_lag", '{"lag":42}'),)
    run = run_state.model_copy(update={"state": IncidentState.ESCALATED, "evidence": evidence})
    template = render_briefing(run)
    return template.model_copy(
        update={
            "findings": "billing lag observed at 42 messages",
            "recommendation": "verify billing consumer pod is running",
        }
    )


class TestJudgeScore:
    @pytest.mark.parametrize(
        ("groundedness", "actionability", "expected_overall", "expected_useful"),
        [
            (1.0, 1.0, 1.0, True),
            (0.7, 0.7, 0.7, True),
            (0.5, 0.9, 0.7, True),
            (0.6, 0.7, pytest.approx(0.65), False),
            (0.0, 0.0, 0.0, False),
        ],
    )
    def test_overall_and_useful_derived(
        self,
        groundedness: float,
        actionability: float,
        expected_overall: float,
        expected_useful: bool,
    ) -> None:
        score = JudgeScore(
            groundedness=groundedness,
            actionability=actionability,
            reasoning="r",
        )
        assert score.overall == expected_overall
        assert score.is_useful is expected_useful

    def test_useful_threshold_matches_module_constant(self) -> None:
        assert USEFUL_THRESHOLD == 0.7
        score = JudgeScore(
            groundedness=USEFUL_THRESHOLD,
            actionability=USEFUL_THRESHOLD,
            reasoning="r",
        )
        assert score.is_useful is True

    @pytest.mark.parametrize("bad", [-0.1, 1.1, 2.0])
    def test_groundedness_out_of_range_rejected(self, bad: float) -> None:
        with pytest.raises(ValidationError):
            JudgeScore(groundedness=bad, actionability=0.5, reasoning="r")

    @pytest.mark.parametrize("bad", [-0.1, 1.1, 2.0])
    def test_actionability_out_of_range_rejected(self, bad: float) -> None:
        with pytest.raises(ValidationError):
            JudgeScore(groundedness=0.5, actionability=bad, reasoning="r")

    def test_empty_reasoning_rejected(self) -> None:
        with pytest.raises(ValidationError):
            JudgeScore(groundedness=0.5, actionability=0.5, reasoning="")

    def test_extra_fields_rejected(self) -> None:
        with pytest.raises(ValidationError):
            JudgeScore.model_validate(
                {
                    "groundedness": 0.5,
                    "actionability": 0.5,
                    "reasoning": "r",
                    "extra": "boom",
                }
            )


class TestJudgeBriefing:
    def test_returns_parsed_judge_score(self, run_state: RunState, now: datetime) -> None:
        client = CannedLLMClient(
            [
                {
                    "groundedness": 0.85,
                    "actionability": 0.9,
                    "reasoning": "Findings and recommendation both trace to evidence.",
                }
            ]
        )
        briefing = _sample_briefing(run_state, now)
        score = judge_briefing(briefing, client, model="claude-haiku-4-5")
        assert score.groundedness == 0.85
        assert score.actionability == 0.9
        assert score.is_useful is True

    def test_context_includes_findings_and_recommendation(
        self, run_state: RunState, now: datetime
    ) -> None:
        client = CannedLLMClient(
            [
                {
                    "groundedness": 0.8,
                    "actionability": 0.8,
                    "reasoning": "ok",
                }
            ]
        )
        briefing = _sample_briefing(run_state, now)
        judge_briefing(briefing, client, model="m")
        _system, user = client.calls[0]
        assert "Findings:" in user
        assert "billing lag observed" in user
        assert "Recommendation:" in user
        assert "verify billing consumer pod" in user

    def test_context_includes_investigation_trail(self, run_state: RunState, now: datetime) -> None:
        client = CannedLLMClient(
            [
                {
                    "groundedness": 0.8,
                    "actionability": 0.8,
                    "reasoning": "ok",
                }
            ]
        )
        briefing = _sample_briefing(run_state, now)
        judge_briefing(briefing, client, model="m")
        _system, user = client.calls[0]
        assert "get_consumer_lag" in user

    def test_context_flags_missing_trail(self, run_state: RunState) -> None:
        client = CannedLLMClient(
            [
                {
                    "groundedness": 0.6,
                    "actionability": 0.6,
                    "reasoning": "no trail",
                }
            ]
        )
        # A briefing with no probes.
        template = render_briefing(run_state.model_copy(update={"state": IncidentState.ESCALATED}))
        briefing = template.model_copy(
            update={
                "findings": "no probes ran",
                "recommendation": "check the raw alert",
            }
        )
        judge_briefing(briefing, client, model="m")
        _system, user = client.calls[0]
        assert "No probes were run" in user


class TestJudgeSeesTheDeterministicFields:
    """The judge must see what the writer saw (WO-R2-34, after #152).

    ``escalation_reason`` and ``attempted_action`` are deterministic fields
    the briefing writer is given (``briefing_enrichment._format_context``).
    The judge grades ``groundedness`` — does every claim derive from the
    context — against a rendering that omitted both, so a recommendation
    correctly built on them read as invented, and a recommendation that
    told the human to re-run an already-attempted Tier-1 action could not
    be marked down for it. Grading a briefing on less than it was written
    from is the same vacuous-assertion shape this order is closing.
    """

    @staticmethod
    def _judged(briefing: EscalationBriefing) -> str:
        client = CannedLLMClient([{"groundedness": 0.8, "actionability": 0.8, "reasoning": "ok"}])
        judge_briefing(briefing, client, model="m")
        _system, user = client.calls[0]
        return user

    def test_escalation_reason_is_shown(self, run_state: RunState, now: datetime) -> None:
        briefing = _sample_briefing(run_state, now).model_copy(
            update={"escalation_reason": "budget exhausted before verify could run"}
        )
        assert "budget exhausted before verify could run" in self._judged(briefing)

    def test_attempted_action_is_shown(self, run_state: RunState, now: datetime) -> None:
        briefing = _sample_briefing(run_state, now).model_copy(
            update={
                "attempted_action": AttemptedAction(
                    tool="restart_consumer_group",
                    arguments={"consumer_group": "billing"},
                )
            }
        )
        user = self._judged(briefing)
        assert "restart_consumer_group" in user
        assert "billing" in user

    def test_an_attempted_action_is_marked_as_already_attempted(
        self, run_state: RunState, now: datetime
    ) -> None:
        # Not just present — labelled. An unlabelled tool name in the context
        # reads as one more thing the agent could do next.
        briefing = _sample_briefing(run_state, now).model_copy(
            update={"attempted_action": AttemptedAction(tool="pause_dag", arguments={})}
        )
        assert "ALREADY ATTEMPTED" in self._judged(briefing)

    def test_the_judge_is_shown_what_the_writer_was_shown(
        self, run_state: RunState, now: datetime
    ) -> None:
        # The anti-drift pin. Two renderings exist on purpose (the writer
        # also gets the budget, the judge also gets findings/recommendation),
        # but the halves they share must stay word-for-word identical — a
        # judge grading groundedness against different phrasing than the
        # writer received is grading a different briefing.
        from incident_commander.agent.briefing_enrichment import _format_context

        briefing = _sample_briefing(run_state, now).model_copy(
            update={
                "escalation_reason": "budget exhausted before verify could run",
                "attempted_action": AttemptedAction(
                    tool="restart_consumer_group", arguments={"consumer_group": "billing"}
                ),
            }
        )
        writer_lines = set(_format_context(briefing).splitlines())
        judge_lines = set(self._judged(briefing).splitlines())
        shared = {
            line
            for line in writer_lines
            if line.startswith(("Why the run ended:", "Tier-1 action ALREADY ATTEMPTED"))
        }
        assert len(shared) == 2
        assert shared <= judge_lines

    def test_absent_fields_add_no_lines(self, run_state: RunState, now: datetime) -> None:
        # The default briefing carries neither; the rendering must not grow
        # empty "Why the run ended:" noise the judge would have to ignore.
        user = self._judged(_sample_briefing(run_state, now))
        assert "Why the run ended:" not in user
        assert "ALREADY ATTEMPTED" not in user


class TestUnusedProbeSummary:
    # Guard against accidental removal of ProbeSummary import.
    def test_probe_summary_still_exported(self, now: datetime) -> None:
        _ = ProbeSummary(tool="get_consumer_lag", summary="lag=0")
