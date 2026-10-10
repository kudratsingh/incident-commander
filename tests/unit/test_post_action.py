"""ADR 0077 (INC-005): RESOLVED needs a reading of the alerted metric taken after the action.

The owner's seventh take resolved on a lag reading of 55 measured three seconds BEFORE the
restart, because the verification judge read a newest-first sample list backwards. Three parts
are pinned here: the verify loop's gate, the judge's view of the reading, and the evidence grade.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Final
from uuid import UUID

from evals.graders.deterministic import GradeDimension, ScenarioExpectation, grade
from evals.scenarios.loader import load_scenarios
from evals.scenarios.schema import Scenario
from incident_commander.agent.investigation import ALERT_SUBJECT_PROBES
from incident_commander.agent.post_action import (
    METRIC_READING,
    judge_view,
    metric_expectation,
)
from incident_commander.agent.remediation import (
    RemediationPlan,
    format_verify_context,
    make_llm_verify,
)
from incident_commander.agent.state import BudgetLedger, EvidenceEntry, IncidentState, RunState
from incident_commander.agent.thinking import ObservedThinking, PlannerLog
from incident_commander.llm.fakes import CannedLLMClient
from incident_commander.llm.prompts.loader import load_prompt
from incident_commander.tools.mcp_client import ToolResult

_MODEL: Final[str] = "test-model"
# Spelled out rather than imported, so this file collects on a checkout without the gate.
_GATE_MARKER: Final[str] = "_verify_reading_gate"
_SCENARIOS_DIR: Final[Path] = Path(__file__).resolve().parents[2] / "evals" / "scenarios"

# The take's own clock: the restart at 08:52:11.030919 (agent), the verify read 48 ms later.
_ACTION_AT: Final[datetime] = datetime(2026, 9, 27, 8, 52, 11, 30919, tzinfo=UTC)

#: The platform's own page from the take, as the agent received it.
_PLATFORM_ALERT: Final[dict[str, Any]] = {
    "lag": 23,
    "group": "worker-dispatcher",
    "source": "kafka:consumer_lag",
    "severity": "critical",
    "threshold": 20,
    "fingerprint": "consumer_stalled",
    "consumer_group": "worker-dispatcher",
}
#: The same page with no threshold, the shape of every hand-written lag alert in the corpus.
_HAND_WRITTEN_ALERT: Final[dict[str, Any]] = {
    key: value for key, value in _PLATFORM_ALERT.items() if key != "threshold"
}

_CLIMB: Final[list[tuple[int, str]]] = [
    (55, "2026-09-27T08:52:08.296763Z"),
    (49, "2026-09-27T08:52:03.182670Z"),
    (42, "2026-09-27T08:51:57.552426Z"),
    (36, "2026-09-27T08:51:52.435301Z"),
    (29, "2026-09-27T08:51:47.337691Z"),
    (0, "2026-09-27T08:51:21.586957Z"),
]


def _lag_reading(lag: int, measured_at: str, age: int, samples: list[tuple[int, str]]) -> str:
    """A v0.6.20 ``get_consumer_lag`` answer; samples are given NEWEST FIRST, as served."""
    return json.dumps(
        {
            "consumer_group": "worker-dispatcher",
            "lag": lag,
            "lag_known": True,
            "source": "live",
            "cache_key": "kafka:consumer_lag:worker-dispatcher",
            "measured_at": measured_at,
            "age_seconds": age,
            "recent_samples": [{"lag": v, "measured_at": t} for v, t in samples],
        },
        separators=(",", ":"),
    )


#: The take's verify read: 55, served 2 s after it was measured, 48 ms after the restart.
_STALE: Final[str] = _lag_reading(55, "2026-09-27T08:52:08.296763Z", 2, _CLIMB)
#: The next platform tick: 0, measured at the moment it was served.
_FRESH: Final[str] = _lag_reading(
    0, "2026-09-27T08:52:26.100000Z", 0, [(0, "2026-09-27T08:52:26.100000Z"), *_CLIMB]
)
#: A reading taken after the action that is still above the threshold of 20.
_FRESH_BUT_HIGH: Final[str] = _lag_reading(
    31, "2026-09-27T08:52:26.100000Z", 0, [(31, "2026-09-27T08:52:26.100000Z"), *_CLIMB]
)


def _plan() -> dict[str, Any]:
    return {
        "target_hypothesis": "consumer_saturation",
        "action_tool": "restart_consumer_group",
        "action_arguments": {"consumer_group": "worker-dispatcher"},
        "verify_tool": "get_consumer_lag",
        "verify_arguments": {"consumer_group": "worker-dispatcher"},
        "verify_expectation": "lag drops below 20 and the samples drain",
    }


def _action_entry() -> EvidenceEntry:
    return EvidenceEntry(
        tool_name="restart_consumer_group",
        arguments={"consumer_group": "worker-dispatcher", "idempotency_key": "k"},
        result_summary='{"consumer_group":"worker-dispatcher","accepted":true}',
        timestamp=_ACTION_AT,
    )


def _run(
    *,
    alert: Mapping[str, Any] = _PLATFORM_ALERT,
    state: IncidentState = IncidentState.VERIFYING,
    evidence: tuple[EvidenceEntry, ...] | None = None,
) -> RunState:
    return RunState(
        incident_id=UUID("22222222-2222-2222-2222-222222222222"),
        state=state,
        alert=dict(alert),
        budget=BudgetLedger(
            max_tool_calls=25, max_tokens=200_000, max_wall_seconds=600, max_usd=Decimal("1.00")
        ),
        remediation_plan=_plan(),
        remediation_attempts=1,
        evidence=(_action_entry(),) if evidence is None else evidence,
        created_at=_ACTION_AT,
        updated_at=_ACTION_AT,
    )


class _Queue:
    """Serves ``get_consumer_lag`` answers in order and records every call."""

    def __init__(self, *answers: str) -> None:
        self._answers = list(answers)
        self.calls: list[str] = []

    def call_tool(
        self, name: str, arguments: Mapping[str, Any], *, timeout_seconds: float | None = None
    ) -> ToolResult:
        self.calls.append(name)
        text = self._answers.pop(0) if len(self._answers) > 1 else self._answers[0]
        return ToolResult(content=[{"type": "text", "text": text}])


def _clock(*offsets: float) -> Iterator[datetime]:
    """The agent's clock at each poll, in seconds after the restart."""
    return iter(_ACTION_AT + timedelta(seconds=s) for s in offsets)


def _verify(
    mcp: _Queue, llm: CannedLLMClient, *, polls: int, at: tuple[float, ...], log: Any = None
) -> Any:
    ticks = _clock(*at)
    return make_llm_verify(
        mcp,
        llm,
        model=_MODEL,
        probe_attempts=polls,
        probe_delay_seconds=0.0,
        sleep=lambda _s: None,
        clock=lambda: next(ticks),
        planner_log=log,
    )


def _verified(n: int) -> CannedLLMClient:
    return CannedLLMClient([{"verdict": "verified", "reasoning": "lag declined 55 -> 0"}] * n)


def _gate_rows(run: RunState) -> list[EvidenceEntry]:
    return [e for e in run.evidence if e.tool_name == _GATE_MARKER]


class TestTheLoopResolvesOnlyOnAPostActionReading:
    """Part 1: the judge's ``verified`` on an older reading does not end the loop."""

    def test_a_verified_on_the_stale_reading_polls_again_and_resolves_on_the_fresh_one(
        self,
    ) -> None:
        mcp = _Queue(_STALE, _FRESH)
        transition = _verify(mcp, _verified(2), polls=2, at=(0.048, 15.0))

        result = transition(_run(), _ACTION_AT)

        assert result.state is IncidentState.RESOLVED
        assert mcp.calls == ["get_consumer_lag", "get_consumer_lag"]
        (row,) = _gate_rows(result)
        assert row.arguments["verdict"] == "verified_on_stale_reading"
        assert row.arguments["attempt"] == 1
        assert "2026-09-27T08:52:08.296763" in row.result_summary

    def test_exhausted_polls_escalate_naming_the_last_reading_and_its_time(self) -> None:
        transition = _verify(_Queue(_STALE), _verified(2), polls=2, at=(0.048, 0.1))

        result = transition(_run(), _ACTION_AT)

        assert result.state is IncidentState.ESCALATED
        reason = result.evidence[-1].result_summary
        assert "RESOLVED needs a reading taken after the action" in reason
        assert "lag 55 measured at 2026-09-27T08:52:08.296763" in reason
        assert "ADR 0077" in reason
        assert [r.arguments["verdict"] for r in _gate_rows(result)] == [
            "verified_on_stale_reading",
            "verified_on_stale_reading",
        ]

    def test_a_fresh_reading_above_the_threshold_does_not_resolve(self) -> None:
        transition = _verify(_Queue(_FRESH_BUT_HIGH), _verified(1), polls=1, at=(15.0,))

        result = transition(_run(), _ACTION_AT)

        assert result.state is IncidentState.ESCALATED
        (row,) = _gate_rows(result)
        assert row.arguments["verdict"] == "verified_above_threshold"
        assert "31 is not below the alert's threshold 20" in row.result_summary

    def test_a_fresh_reading_inside_the_threshold_resolves_on_the_first_poll(self) -> None:
        transition = _verify(_Queue(_FRESH), _verified(1), polls=1, at=(15.0,))

        result = transition(_run(), _ACTION_AT)

        assert result.state is IncidentState.RESOLVED
        assert _gate_rows(result) == []

    def test_an_alert_without_a_threshold_keeps_the_judge_decides_path(self) -> None:
        """The hand-written corpus alerts carry no threshold, which is why no canned grade moved."""
        transition = _verify(_Queue(_STALE), _verified(1), polls=1, at=(0.048,))

        result = transition(_run(alert=_HAND_WRITTEN_ALERT), _ACTION_AT)

        assert result.state is IncidentState.RESOLVED
        assert _gate_rows(result) == []

    def test_a_not_verified_on_the_stale_reading_is_todays_path(self) -> None:
        llm = CannedLLMClient([{"verdict": "not_verified", "reasoning": "still climbing"}])
        transition = _verify(_Queue(_STALE), llm, polls=1, at=(0.048,))

        result = transition(_run(), _ACTION_AT)

        assert result.state is IncidentState.ESCALATED
        assert _gate_rows(result) == []

    def test_the_console_is_sent_the_gate_verdict_with_the_reading(self) -> None:
        log = PlannerLog()
        seen: list[ObservedThinking] = []

        def sink(thinking: ObservedThinking) -> bool:
            seen.append(thinking)
            return True

        log.subscribe(sink)
        transition = _verify(_Queue(_STALE, _FRESH), _verified(2), polls=2, at=(0.048, 15), log=log)

        transition(_run(), _ACTION_AT)

        assert [t.headline for t in seen] == [
            "verify 1/2 verified_on_stale_reading",
            "verify 2/2 verified",
        ]
        assert "measured before restart_consumer_group ran" in (seen[0].reason or "")


class TestTheJudgeSeesTheHistoryOldestFirst:
    """Part 2: ``verification_judge`` is never handed a raw ``recent_samples`` list."""

    def _context(self, reading: str, *, read_after: float = 0.048) -> str:
        return format_verify_context(
            RemediationPlan.model_validate(_plan()),
            reading,
            '{"accepted":true}',
            action_at=_ACTION_AT,
            read_at=_ACTION_AT + timedelta(seconds=read_after),
        )

    def test_the_raw_sample_list_is_gone(self) -> None:
        context = self._context(_STALE)
        assert "recent_samples" not in context
        assert '"lag":55' in context

    def test_samples_are_listed_oldest_first_with_their_times(self) -> None:
        context = self._context(_STALE)
        assert "OLDEST FIRST" in context
        order = [context.index(f"lag={v} measured_at") for v in (0, 29, 36, 42, 49, 55)]
        assert order == sorted(order)
        assert "measured_at=2026-09-27T08:52:08.296763+00:00" in context

    def test_order_comes_from_the_timestamps_not_the_list_position(self) -> None:
        shuffled = _lag_reading(55, _CLIMB[0][1], 2, [_CLIMB[3], _CLIMB[0], _CLIMB[5], _CLIMB[1]])
        view = judge_view("get_consumer_lag", shuffled, action_at=None, read_at=None)
        lines = [line for line in view.splitlines() if "measured_at=" in line]
        assert [line.split("lag=")[1].split(" ")[0] for line in lines] == ["0", "36", "49", "55"]

    def test_the_trend_on_the_takes_reading(self) -> None:
        trend = json.loads(self._context(_STALE).split("Trend: ")[1].splitlines()[0])
        assert trend == {"first": 0, "last": 55, "direction": "rising", "samples_after_action": 0}

    def test_the_trend_after_a_recovery(self) -> None:
        trend = json.loads(self._context(_FRESH, read_after=15).split("Trend: ")[1].splitlines()[0])
        assert trend == {"first": 0, "last": 0, "direction": "falling", "samples_after_action": 1}

    def test_the_actions_time_is_on_the_platforms_clock(self) -> None:
        # 08:52:08.296763 + 2 s of age is the platform's "now" 48 ms after the restart.
        assert "Action executed at (platform's clock): 2026-09-27T08:52:10.248" in self._context(
            _STALE
        )

    def test_a_reading_without_samples_is_shown_as_it_came_back(self) -> None:
        plan = RemediationPlan.model_validate(_plan())
        assert format_verify_context(plan, '{"total":4}', None) == format_verify_context(
            plan, '{"total":4}', None, action_at=_ACTION_AT, read_at=_ACTION_AT
        )
        assert 'Verify probe result:\n{"total":4}\n' in format_verify_context(
            plan, '{"total":4}', None
        )

    def test_the_transition_hands_the_judge_the_view(self) -> None:
        llm = CannedLLMClient([{"verdict": "not_verified", "reasoning": "climbing"}])
        _verify(_Queue(_STALE), llm, polls=1, at=(0.048,))(_run(), _ACTION_AT)
        (_, user_message) = llm.calls[0]
        assert "recent_samples" not in user_message
        assert '"samples_after_action":0' in user_message

    def test_the_prompt_says_how_to_read_it(self) -> None:
        prompt = load_prompt("verification_judge")
        assert "OLDEST" in prompt
        assert "`samples_after_action` is 0" in prompt


def _graded_run(state: IncidentState, *readings: tuple[str, float]) -> RunState:
    """A finished run: one read before the action, the action, then verify reads."""
    before = EvidenceEntry(
        tool_name="get_consumer_lag",
        arguments={"consumer_group": "worker-dispatcher"},
        result_summary=_lag_reading(29, "2026-09-27T08:51:47.337691Z", 4, _CLIMB[4:]),
        timestamp=_ACTION_AT - timedelta(seconds=25),
    )
    after = tuple(
        EvidenceEntry(
            tool_name="get_consumer_lag",
            arguments={"consumer_group": "worker-dispatcher", "attempt": 1, "of": 6},
            result_summary=text,
            timestamp=_ACTION_AT + timedelta(seconds=offset),
        )
        for text, offset in readings
    )
    return _run(state=state, evidence=(before, _action_entry(), *after))


def _evidence_of(run: RunState) -> tuple[bool, str]:
    report = grade(
        run,
        ScenarioExpectation(
            name="remediate_consumer_lag_success",
            expected_terminal_state=run.state,
            expected_action_tools=("restart_consumer_group",),
        ),
    )
    (row,) = [d for d in report.dimensions if d.dimension is GradeDimension.EVIDENCE]
    return row.passed, row.detail


class TestTheEvidenceGradeRequiresThePostActionReading:
    """Part 3: a RESOLVED run whose last reading predates its action fails EVIDENCE."""

    def test_the_takes_trajectory_fails_naming_both_times(self) -> None:
        passed, detail = _evidence_of(_graded_run(IncidentState.RESOLVED, (_STALE, 0.048)))
        assert not passed
        assert "measured at 2026-09-27T08:52:08.296763+00:00" in detail
        assert "restart_consumer_group ran at 2026-09-27T08:52:10.248" in detail
        assert "(2026-09-27T08:52:11.030919+00:00 on the agent's)" in detail
        assert "55 is not below the alert's threshold 20" in detail

    def test_a_fresh_reading_inside_the_threshold_passes_and_says_so(self) -> None:
        passed, detail = _evidence_of(
            _graded_run(IncidentState.RESOLVED, (_STALE, 0.048), (_FRESH, 15))
        )
        assert passed
        assert detail.startswith("post-action reading held: lag 0 measured at")

    def test_a_fresh_reading_above_the_threshold_fails(self) -> None:
        passed, detail = _evidence_of(_graded_run(IncidentState.RESOLVED, (_FRESH_BUT_HIGH, 15)))
        assert not passed
        assert "31 is not below the alert's threshold 20" in detail

    def test_no_reading_after_the_action_fails(self) -> None:
        passed, detail = _evidence_of(_graded_run(IncidentState.RESOLVED))
        assert not passed
        assert "no reading of get_consumer_lag(consumer_group=worker-dispatcher)" in detail

    def test_an_escalated_run_is_not_asked(self) -> None:
        passed, detail = _evidence_of(_graded_run(IncidentState.ESCALATED, (_STALE, 0.048)))
        assert passed
        assert detail == "no evidence expectations set"

    def test_an_alert_without_a_threshold_is_not_asked(self) -> None:
        run = _graded_run(IncidentState.RESOLVED, (_STALE, 0.048)).model_copy(
            update={"alert": dict(_HAND_WRITTEN_ALERT)}
        )
        assert _evidence_of(run) == (True, "no evidence expectations set")


class TestWhatIsMetricShaped:
    def test_the_map_is_total_over_every_subject_probe(self) -> None:
        probes = {probe.tool_name for probe in ALERT_SUBJECT_PROBES.values()}
        assert probes <= set(METRIC_READING)

    def test_only_the_consumer_lag_reading_is_active_today(self) -> None:
        assert {tool for tool, entry in METRIC_READING.items() if entry} == {"get_consumer_lag"}

    def test_the_platforms_page_is_metric_shaped_and_a_bool_threshold_is_not(self) -> None:
        assert metric_expectation(_PLATFORM_ALERT) is not None
        assert metric_expectation(_HAND_WRITTEN_ALERT) is None
        assert metric_expectation({**_PLATFORM_ALERT, "threshold": True}) is None

    def test_a_dlq_depth_page_stays_on_the_judge_path(self) -> None:
        page = {"source": "dlq", "remediation_hint": "replay_safe", "threshold": 5}
        assert metric_expectation(page) is None


class TestTheCannedReproduction:
    """Part 4's scenario polls twice canned; WO-R3-372's resolver world is the one other."""

    def _corpus(self) -> dict[str, Scenario]:
        return {scenario.name: scenario for scenario in load_scenarios(_SCENARIOS_DIR)}

    def test_the_scenario_declares_two_polls_and_the_takes_page(self) -> None:
        scenario = self._corpus()["verify_judge_reads_history_backwards"]
        assert scenario.canned_verify_polls == 2
        assert metric_expectation(scenario.alert.model_dump()) is not None

    def test_no_other_scenario_polls_more_than_once_canned(self) -> None:
        polling = {name for name, s in self._corpus().items() if s.canned_verify_polls != 1}
        # WO-R3-372: the restarted resolver's first post-action poll reads the frozen value and
        # the second reads it polling, measured on rehearsal 11b24360e455 — the canned world
        # replays that timing, so it declares two polls.
        assert polling == {"verify_judge_reads_history_backwards", "workflow_stuck_resolver_stall"}

    def test_the_knob_is_evaluator_only(self) -> None:
        assert "canned_verify_polls" in Scenario.EVALUATOR_ONLY_FIELDS


def test_the_marker_spelling_matches_the_loops() -> None:
    from incident_commander.agent.remediation import POST_ACTION_GATE_MARKER

    assert POST_ACTION_GATE_MARKER == _GATE_MARKER
