from datetime import datetime

import pytest
from pydantic import ValidationError

from evals.graders.llm_judge import USEFUL_THRESHOLD, JudgeScore, judge_briefing
from incident_commander.agent.briefing import (
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


class TestUnusedProbeSummary:
    # Guard against accidental removal of ProbeSummary import.
    def test_probe_summary_still_exported(self, now: datetime) -> None:
        _ = ProbeSummary(tool="get_consumer_lag", summary="lag=0")
