import json
from datetime import datetime
from pathlib import Path
from typing import Final

import pytest
from pydantic import ValidationError

from evals.graders.llm_judge import (
    USEFUL_THRESHOLD,
    JudgeScore,
    _format_briefing,
    judge_briefing,
)
from incident_commander.agent.briefing import (
    AttemptedAction,
    EscalationBriefing,
    ProbeSummary,
    render_briefing,
)
from incident_commander.agent.state import EvidenceEntry, IncidentState, RunState
from incident_commander.llm.fakes import CannedLLMClient

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
_RUN_E: Final = "54ab08425f82"
_RUN_E_TRAJECTORY: Final[Path] = (
    _REPO_ROOT / "evals" / "runs" / _RUN_E / "trajectories" / "remediate_dlq_backlog_success.json"
)

_POISON_ROW: Final = "eb798430-c3ad-5a44-b7d7-d15ab54d3f76"
_HUMAN_ROW: Final = "f030f975-974e-5ce3-aa6b-444136507d86"
_RATE_LIMIT_ROW: Final = "af67d1b1"


def _trail_lines(context: str, tool: str) -> list[str]:
    """Every rendered trail line for one tool, in order."""
    return [line for line in context.splitlines() if line.startswith(f"  - {tool}(")]


def _trail_line(context: str, tool: str, *, last: bool = False) -> str:
    lines = _trail_lines(context, tool)
    assert lines, f"no trail line for {tool!r} in:\n{context}"
    return lines[-1] if last else lines[0]


def _run_e_briefing() -> EscalationBriefing:
    """Run E's briefing, re-rendered from its archived final checkpoint.

    The archive is evidence and is opened read-only; nothing here writes to
    ``evals/`` and nothing here calls a model.
    """
    checkpoints = json.loads(_RUN_E_TRAJECTORY.read_text())["checkpoints"]
    run = RunState.model_validate(checkpoints[-1])
    return render_briefing(run).model_copy(
        update={
            # The two free-form strings the judge grades, quoted from the
            # archived briefing so the context under test is run E's own.
            "findings": (
                "fc8d2a03 replayed; four rows remain: f030f975 needs the uploader, "
                "97d91272 the SMTP relay, af67d1b1 the rate-limit window, and "
                "eb798430 is poisoned/unclassified."
            ),
            "recommendation": "Notify the uploader for f030f975 and classify eb798430.",
        }
    )


def _run_e_final_probe() -> ProbeSummary:
    return _run_e_briefing().investigation_trail[-1]


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
            if line.startswith(
                (
                    "Why the run ended:",
                    "Tier-1 action ALREADY ATTEMPTED",
                    "Investigation trail:",
                    "  - ",
                )
            )
        }
        # Two deterministic fields, the trail heading, and one probe line.
        assert len(shared) == 4
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


class TestTheJudgeReadsEachProbesArguments:
    """INC-002: a probe result rendered without its arguments has no scope.

    Paid run ``54ab08425f82`` (``remediate_dlq_backlog_success`` run E,
    2026-09-08 11:13Z, GREEN on all five deterministic dimensions) is the
    reference case. The agent read the whole dead-letter queue, read the
    alerted ``remediation_hint='replay_safe'`` slice, replayed its single row
    by id, re-read the SAME filtered slice to verify (``total 0``), and wrote
    an honest briefing: one row replayed, four rows remaining, each with what
    it still needs. The judge was shown that final probe as
    ``list_dlq_messages: {"total":0,"items":[]}`` — the filter that scoped it
    stripped away — concluded "all 5 messages are gone", and scored
    groundedness 0.0 / overall 0.38 / "0/1 useful" on a briefing that was
    right. It made the exact overclaim cmd #218 had just forbidden the writer.

    Two halves, and both are needed. The rubric half (a filtered read proves
    its slice and nothing outside it) lives in
    ``test_prompts_snapshot.py::TestBriefingJudgeInvariants``; a rule the
    judge cannot apply because the fact is missing from its context is not a
    rule. This file is the context half.
    """

    @staticmethod
    def _judged(briefing: EscalationBriefing) -> str:
        client = CannedLLMClient([{"groundedness": 0.8, "actionability": 0.8, "reasoning": "ok"}])
        judge_briefing(briefing, client, model="m")
        _system, user = client.calls[0]
        return user

    def test_a_filtered_read_is_rendered_with_its_filter(
        self, run_state: RunState, now: datetime
    ) -> None:
        evidence = (
            EvidenceEntry(
                tool_name="list_dlq_messages",
                arguments={"job_type": None, "remediation_hint": "replay_safe", "limit": 50},
                result_summary='{"total":0,"items":[]}',
                timestamp=now,
            ),
        )
        run = run_state.model_copy(update={"state": IncidentState.RESOLVED, "evidence": evidence})
        briefing = render_briefing(run).model_copy(
            update={"findings": "the replay_safe slice is drained", "recommendation": "spot-check"}
        )
        line = _trail_line(self._judged(briefing), "list_dlq_messages")
        assert "remediation_hint='replay_safe'" in line
        assert '{"total":0,"items":[]}' in line
        # Arguments BEFORE the result: the rubric tells the judge to read the
        # scope first, and a context that renders it afterwards asks the judge
        # to have already interpreted the number by the time it learns what
        # was counted.
        assert line.index("remediation_hint='replay_safe'") < line.index('"total":0')

    def test_an_unfiltered_read_says_so_rather_than_saying_nothing(
        self, run_state: RunState, now: datetime
    ) -> None:
        # The absence of a filter is itself the distinguishing fact. Rendering
        # nothing for it would make the whole-queue read and the slice read
        # indistinguishable again, which is the bug.
        evidence = (
            EvidenceEntry(
                tool_name="list_dlq_messages",
                arguments={"remediation_hint": None, "limit": 50},
                result_summary='{"total":5}',
                timestamp=now,
            ),
        )
        run = run_state.model_copy(update={"state": IncidentState.RESOLVED, "evidence": evidence})
        briefing = render_briefing(run).model_copy(
            update={"findings": "five rows", "recommendation": "spot-check"}
        )
        assert "remediation_hint=None" in _trail_line(self._judged(briefing), "list_dlq_messages")

    def test_a_probe_with_no_arguments_renders_empty_parentheses(
        self, run_state: RunState, now: datetime
    ) -> None:
        line = _trail_line(self._judged(_sample_briefing(run_state, now)), "get_consumer_lag")
        assert line == '  - get_consumer_lag() -> {"lag":42}'


class TestTheArchivedJudgeContextCarriesTheFilter:
    """Run E's own trail, rebuilt from the archive. Read-only, and no live judge.

    The archived trajectory's final checkpoint IS the ``RunState`` the
    briefing was rendered from, so rendering it again is the experiment; a
    hand-written trail would only be a model of it, and a model is what the
    fix must not be graded against. Nothing here calls a model: the context
    string is built and inspected, which is the whole of what INC-002 was
    about.
    """

    def test_the_archive_is_present(self) -> None:
        # Anti-vacuity. Every assertion below is derived from this file; if it
        # ever goes missing, the class must go red rather than skip green.
        assert _RUN_E_TRAJECTORY.is_file(), (
            f"Run E's archived trajectory is missing: {_RUN_E_TRAJECTORY}. "
            f"Eval artifacts are append-only (CLAUDE.md invariant 9) — this "
            f"file is evidence and is never deleted or moved."
        )

    def test_the_final_verify_is_the_filtered_read_that_returned_zero(self) -> None:
        # Pins the premise the rest of the class rests on, so a trajectory
        # that had drifted from the run it claims to describe fails here
        # rather than quietly becoming the thing under test.
        probe = _run_e_final_probe()
        assert probe.tool == "list_dlq_messages"
        assert probe.arguments["remediation_hint"] == "replay_safe"
        assert probe.summary == '{"total":0,"items":[]}'

    def test_the_filter_is_rendered_beside_the_zero(self) -> None:
        context = _format_briefing(_run_e_briefing())
        line = _trail_line(context, "list_dlq_messages", last=True)
        assert "remediation_hint='replay_safe'" in line
        assert '"total":0' in line
        assert line.index("remediation_hint='replay_safe'") < line.index('"total":0')

    def test_the_earlier_unfiltered_read_stays_distinguishable(self) -> None:
        # Run E read the queue twice under one tool name. If both lines
        # rendered identically apart from their results, the judge would still
        # have to guess which read the `total 0` belonged to.
        lines = _trail_lines(_format_briefing(_run_e_briefing()), "list_dlq_messages")
        assert len(lines) == 3
        assert "remediation_hint=None" in lines[0]
        assert all("remediation_hint='replay_safe'" in line for line in lines[1:])

    def test_the_untouched_rows_the_briefing_named_are_in_the_context(self) -> None:
        # The judge called the briefing ungrounded for naming four remaining
        # rows. Three of them are visible in the trail's first, unfiltered
        # read; the briefing was reporting the context, not inventing.
        context = _format_briefing(_run_e_briefing())
        for row in (_POISON_ROW, _HUMAN_ROW, _RATE_LIMIT_ROW):
            assert row in context
