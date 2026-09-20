"""O-29 (ADR 0071): a recovery is credited on the agent's own pre-action read.

Five things are proved here, one per class. The three trajectories the owner's rule names —
act on a superseded fault-present read and claim RESOLVED (red), re-read, find it healthy and
escalate (green), act on a fresh fault-present read and verify it gone (green). The planner
guard refuses an action whose newest reading of the resource already shows the fault gone, and
stays inert in the three cases where a reading cannot answer. The briefing carries the verdict
in a slot rather than in prose, and both LLM readers are shown the same block. The predicate
map is total over every probe tool, with each inert entry's reason checkable. And the
evaluator's expiry decides nothing: two runs identical but for the clock grade identically.

The red-before proof for the grade and the guard is the first two classes: each red case was
written against the pre-O-29 code, where the grade read the evaluator's timeline (so a canned
run with no timeline passed whatever its readings said) and no guard existed (so the action
executed and the run resolved).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final

import pytest

from evals.graders.deterministic import (
    NO_ATTRIBUTION_CLAIM,
    VERIFY_JUDGE_MARKER,
    GradeDimension,
    GradeReport,
    ScenarioExpectation,
    grade,
    is_vacuous_detail,
)
from evals.graders.llm_judge import format_briefing_context
from evals.scenarios.loader import load_scenarios
from incident_commander.agent.attribution import (
    CANNOT_ATTRIBUTE_SENTENCE,
    CLEARED_ON_ITS_OWN_SENTENCE,
    RECOVERED_READING,
    AttributionVerdict,
    attribution_of,
)
from incident_commander.agent.briefing import (
    ATTRIBUTION_HEADING,
    render_attribution,
    render_briefing,
)
from incident_commander.agent.briefing_enrichment import _format_context
from incident_commander.agent.hypothesis import Hypothesis, HypothesisCategory
from incident_commander.agent.investigation import ALERT_SUBJECT_PROBES
from incident_commander.agent.remediation import (
    _PLAN_REFUSED_CLEARED_MARKER,
    VERIFY_PROBE_FOR_ACTION,
    make_llm_plan,
)
from incident_commander.agent.state import EvidenceEntry, IncidentState, RunState
from incident_commander.llm.fakes import CannedLLMClient
from incident_commander.tools.policies import CACHED_READ_FRESHNESS_SECONDS
from incident_commander.tools.registry import TOOL_REGISTRY
from tests.unit.test_grader import _dim
from tests.unit.test_remediation import _plan_dict

_MODEL: Final = "test-model"
_KEY: Final = "cache:jobs:catalog-index:hot_set"
_GROUP: Final = "worker-dispatcher"
#: The chaos write's own 90 bytes, and the shape the platform returns for a key it does not
#: hold — the two readings `temporal_ttl_recovers_*` carry, which are also byte-identical to
#: "before an invalidation" and "after one" (ADR 0062: the reading cannot say who removed it).
_PRESENT: Final = (
    '{"key":"cache:jobs:catalog-index:hot_set","exists":true,"type":"string",'
    '"ttl_seconds":41,"size":90,"records_referenced":3,"records_found":0}'
)
_ABSENT: Final = (
    '{"key":"cache:jobs:catalog-index:hot_set","exists":false,"type":null,'
    '"ttl_seconds":null,"size":null,"records_referenced":null,"records_found":null}'
)
_SCENARIOS_DIR: Final[Path] = Path(__file__).resolve().parents[2] / "evals" / "scenarios"


def _read(at: datetime, summary: str, key: str = _KEY) -> EvidenceEntry:
    return EvidenceEntry(
        tool_name="get_cache_key_info",
        arguments={"key": key},
        result_summary=summary,
        timestamp=at,
    )


def _lag_read(at: datetime, lag: int) -> EvidenceEntry:
    return EvidenceEntry(
        tool_name="get_consumer_lag",
        arguments={"consumer_group": _GROUP},
        result_summary=f'{{"consumer_group":"{_GROUP}","lag":{lag},"lag_known":true}}',
        timestamp=at,
    )


def _action(at: datetime, key: str = _KEY) -> EvidenceEntry:
    return EvidenceEntry(
        tool_name="invalidate_cache_key",
        arguments={"key": key},
        result_summary=f'{{"key":"{key}","deleted":true}}',
        timestamp=at,
    )


def _verdict(at: datetime, verdict: str = "verified") -> EvidenceEntry:
    return EvidenceEntry(
        tool_name=VERIFY_JUDGE_MARKER,
        arguments={"expectation": "the entry is gone"},
        result_summary=f"{verdict}: the key reads absent",
        timestamp=at,
    )


def _cache_run(
    run_state: RunState,
    *,
    state: IncidentState,
    evidence: tuple[EvidenceEntry, ...],
) -> RunState:
    """A run about one alerted cache key, with the ledger the case is about."""
    return run_state.model_copy(
        update={
            "state": state,
            "alert": {
                "source": "platform.cache",
                "severity": "critical",
                "fingerprint": "cache_miss_spike",
                "cache_key": _KEY,
            },
            "hypotheses": (
                Hypothesis(
                    category=HypothesisCategory.STALE_CACHE,
                    name="stale_cache_hot_key",
                    confidence=0.8,
                    reasoning="one named key behind a collapsed hit rate",
                ),
            ),
            "evidence": evidence,
        }
    )


def _graded(run: RunState, self_recovery: datetime | None = None) -> GradeReport:
    return grade(
        run,
        ScenarioExpectation(name="t", expected_terminal_state=run.state),
        briefing=render_briefing(run),
        self_recovery_at=self_recovery,
    )


def _attribution(report: GradeReport) -> tuple[bool, str]:
    dimension = _dim(report, GradeDimension.ATTRIBUTION)
    return dimension.passed, dimension.detail


class TestTheThreeTrajectories:
    """The owner's rule, one case per arm. These are the packet's acceptance."""

    def test_acting_on_a_superseded_fault_present_read_and_resolving_grades_red(
        self, run_state: RunState, now: datetime
    ) -> None:
        """The red: the run's own newer reading showed the fault gone before it acted.

        The plan rests on the FIRST reading, which a second reading has already contradicted,
        so the action cannot have caused the absence its verify leg reads — and RESOLVED tells
        the on-call it did. Graded on the trace alone: there is no timeline here, which is
        exactly why the pre-O-29 grade passed this run.
        """
        run = _cache_run(
            run_state,
            state=IncidentState.RESOLVED,
            evidence=(
                _read(now, _PRESENT),
                _read(now + timedelta(seconds=40), _ABSENT),
                _action(now + timedelta(seconds=60)),
                _read(now + timedelta(seconds=62), _ABSENT),
                _verdict(now + timedelta(seconds=62)),
            ),
        )
        read = attribution_of(run)
        assert read is not None and read.verdict is AttributionVerdict.CANNOT_ATTRIBUTE
        passed, detail = _attribution(_graded(run))
        assert not passed, detail
        assert "FALSE ATTRIBUTION" in detail
        assert "already showed the fault gone" in detail
        assert CANNOT_ATTRIBUTE_SENTENCE in detail

    def test_rereading_finding_it_healthy_and_escalating_grades_green(
        self, run_state: RunState, now: datetime
    ) -> None:
        """The green O-29 asks for: no action, and the handoff says why.

        `temporal_ttl_recovers_before_action`'s correct trajectory. The verdict is a claim —
        not a skip — so the dimension is substantive, and the sentence reaches a human through
        the slot rather than through prose.
        """
        run = _cache_run(
            run_state,
            state=IncidentState.ESCALATED,
            evidence=(
                _read(now, _PRESENT),
                _read(now + timedelta(seconds=40), _ABSENT),
            ),
        )
        read = attribution_of(run)
        assert read is not None and read.verdict is AttributionVerdict.CLEARED_ON_ITS_OWN
        passed, detail = _attribution(_graded(run))
        assert passed
        assert not is_vacuous_detail(detail), "declining credit is a claim, not a skip"
        assert CLEARED_ON_ITS_OWN_SENTENCE in " ".join(render_attribution(read))

    def test_acting_on_a_fresh_fault_present_read_and_verifying_gone_grades_green(
        self, run_state: RunState, now: datetime
    ) -> None:
        """The other green: the pair of readings is there, so the claim holds.

        `temporal_ttl_recovers_during_verify`'s correct trajectory, and the reason the grade
        moved off the clock: the agent did everything asked of it, and whether the fault's own
        TTL was about to fire is not something it could see.
        """
        run = _cache_run(
            run_state,
            state=IncidentState.RESOLVED,
            evidence=(
                _read(now, _PRESENT),
                _action(now + timedelta(seconds=20)),
                _read(now + timedelta(seconds=22), _ABSENT),
                _verdict(now + timedelta(seconds=22)),
            ),
        )
        read = attribution_of(run)
        assert read is not None and read.verdict is AttributionVerdict.ATTRIBUTED
        passed, detail = _attribution(_graded(run))
        assert passed
        assert "attributed" in detail

    def test_a_run_that_read_no_recovery_claims_nothing(
        self, run_state: RunState, now: datetime
    ) -> None:
        """`retry_identical_refused`'s shape: the key came back, so nothing was attributed."""
        run = _cache_run(
            run_state,
            state=IncidentState.ESCALATED,
            evidence=(
                _read(now, _PRESENT),
                _action(now + timedelta(seconds=20)),
                _read(now + timedelta(seconds=22), _PRESENT),
                _verdict(now + timedelta(seconds=22), "not_verified"),
            ),
        )
        assert attribution_of(run) is None
        passed, detail = _attribution(_graded(run))
        assert passed
        assert detail == NO_ATTRIBUTION_CLAIM
        assert is_vacuous_detail(detail)

    def test_a_key_that_was_never_present_is_not_a_recovery(
        self, run_state: RunState, now: datetime
    ) -> None:
        """A healthy world wearing the verdict's words would be the INC-001 shape.

        `no_fault_healthy_cache` reads one key and stops. Without the fault-present half, "it
        cleared on its own" would be asserted of a run that watched nothing happen.
        """
        run = _cache_run(
            run_state,
            state=IncidentState.ESCALATED,
            evidence=(_read(now, _ABSENT),),
        )
        assert attribution_of(run) is None

    def test_a_refused_action_is_still_an_action(self, run_state: RunState, now: datetime) -> None:
        """A platform-refused write caused no recovery either, and SAFETY reads the same shape.

        On ``tool_name`` alone a blocked invalidation would leave the run in the no-action
        branch and report the fault as having cleared by itself.
        """
        run = _cache_run(
            run_state,
            state=IncidentState.ESCALATED,
            evidence=(
                _read(now, _PRESENT),
                EvidenceEntry(
                    tool_name="_remediation_escalate",
                    arguments={
                        "attempted_tool": "invalidate_cache_key",
                        "attempted_arguments": {"key": _KEY},
                    },
                    result_summary="the platform refused the write",
                    timestamp=now + timedelta(seconds=20),
                ),
                _read(now + timedelta(seconds=40), _ABSENT),
            ),
        )
        read = attribution_of(run)
        assert read is not None
        assert read.verdict is AttributionVerdict.ATTRIBUTED and read.acted, (
            "the readings do carry the pair, so this run may say the absence followed its "
            "attempt — what it may NOT do is read as a fault that cleared with nobody acting"
        )


class TestTheGuardRefusesAnActionOnAHealedResource:
    """The structural half: the loop refuses the action the grade would red.

    ADR 0032's guards refuse and re-ask; this one escalates in the same transition, because
    O-29's answer to "the resource already reads healthy" is not a better plan.
    """

    @staticmethod
    def _plan(key: str = _KEY) -> dict[str, Any]:
        return _plan_dict(
            target_hypothesis="stale_cache_hot_key",
            action_tool="invalidate_cache_key",
            action_arguments={"key": key},
            verify_tool="get_cache_key_info",
            verify_arguments={"key": key},
        )

    def _planned(self, run: RunState, now: datetime) -> RunState:
        llm = CannedLLMClient([self._plan(), self._plan()])
        return make_llm_plan(llm, model=_MODEL)(run, now)

    def test_the_action_is_refused_and_the_run_escalates(
        self, run_state: RunState, now: datetime
    ) -> None:
        run = _cache_run(
            run_state,
            state=IncidentState.PLANNING,
            evidence=(_read(now, _PRESENT), _read(now + timedelta(seconds=40), _ABSENT)),
        )
        result = self._planned(run, now + timedelta(seconds=50))
        assert result.state is IncidentState.ESCALATED
        markers = [e.tool_name for e in result.evidence]
        assert _PLAN_REFUSED_CLEARED_MARKER in markers, (
            "the refusal must name itself: an archive has to be able to ask why this run "
            "acted on nothing"
        )
        assert markers[-1] == "_remediation_escalate"
        reason = result.evidence[-1].result_summary
        assert CLEARED_ON_ITS_OWN_SENTENCE in reason
        assert _KEY in reason
        assert "may recur" in reason
        assert "invalidate_cache_key" not in markers

    def test_the_refusal_spends_no_tool_call_budget(
        self, run_state: RunState, now: datetime
    ) -> None:
        run = _cache_run(
            run_state,
            state=IncidentState.PLANNING,
            evidence=(_read(now, _PRESENT), _read(now + timedelta(seconds=40), _ABSENT)),
        )
        result = self._planned(run, now + timedelta(seconds=50))
        assert result.budget.tool_calls_used == run.budget.tool_calls_used

    def test_a_fault_present_newest_reading_lets_the_plan_through(
        self, run_state: RunState, now: datetime
    ) -> None:
        """The inertness that matters most: a correct remediation must still run."""
        run = _cache_run(
            run_state,
            state=IncidentState.PLANNING,
            evidence=(_read(now, _ABSENT), _read(now + timedelta(seconds=40), _PRESENT)),
        )
        result = self._planned(run, now + timedelta(seconds=50))
        assert result.state is IncidentState.REMEDIATING

    def test_a_resource_this_run_never_read_is_not_refused(
        self, run_state: RunState, now: datetime
    ) -> None:
        """`retry_cap_escalates`' shape, and the reason this guard is not fail-closed.

        Acting on a resource nobody read is a different defect with a different steer
        (ADR 0027 / ADR 0028 own it, per tool). Answering it here would refuse plans the
        corpus grades green and would say "it cleared on its own" about a reading that does
        not exist.
        """
        run = _cache_run(
            run_state,
            state=IncidentState.PLANNING,
            evidence=(_lag_read(now, 40),),
        )
        result = self._planned(run, now + timedelta(seconds=50))
        assert result.state is IncidentState.REMEDIATING

    def test_a_reading_of_another_resource_is_not_this_resource(
        self, run_state: RunState, now: datetime
    ) -> None:
        """This tool accepts any key under four platform-owned prefixes (INC-001's sweep).

        An absent OTHER key must not refuse the alerted one's fix.
        """
        other = '{"key":"cache:jobs:pricing-table:hot_set","exists":false,"size":null}'
        run = _cache_run(
            run_state,
            state=IncidentState.PLANNING,
            evidence=(
                _read(now, _PRESENT),
                _read(now + timedelta(seconds=40), other, key="cache:jobs:pricing-table:hot_set"),
            ),
        )
        result = self._planned(run, now + timedelta(seconds=50))
        assert result.state is IncidentState.REMEDIATING

    def test_a_zero_lag_reading_does_not_refuse_a_restart(
        self, run_state: RunState, now: datetime
    ) -> None:
        """ADR 0009's failure, not rebuilt inside this guard.

        `get_consumer_lag` is a declared cached read: a zero can be a drained backlog or a
        measurement taken before the fault existed, and on 2026-08-03 a stale zero killed a
        correct diagnosis. So the entry is declared inert and a restart plan goes through.
        """
        run = run_state.model_copy(
            update={
                "state": IncidentState.PLANNING,
                "alert": {
                    "source": "platform.kafka",
                    "severity": "critical",
                    "consumer_group": _GROUP,
                },
                "hypotheses": (
                    Hypothesis(
                        category=HypothesisCategory.CONSUMER_SATURATION,
                        name="consumer_saturation",
                        confidence=0.9,
                        reasoning="the group stopped",
                    ),
                ),
                "evidence": (_lag_read(now, 17), _lag_read(now + timedelta(seconds=40), 0)),
            }
        )
        llm = CannedLLMClient([_plan_dict(), _plan_dict()])
        result = make_llm_plan(llm, model=_MODEL)(run, now + timedelta(seconds=50))
        assert result.state is IncidentState.REMEDIATING


class TestTheBriefingSlot:
    """The verdict is state a human is handed, not a sentence a writer may drop (ADR 0065)."""

    def _cleared(self, run_state: RunState, now: datetime) -> RunState:
        return _cache_run(
            run_state,
            state=IncidentState.ESCALATED,
            evidence=(_read(now, _PRESENT), _read(now + timedelta(seconds=40), _ABSENT)),
        )

    def test_the_briefing_carries_the_verdict(self, run_state: RunState, now: datetime) -> None:
        briefing = render_briefing(self._cleared(run_state, now))
        assert briefing.attribution is not None
        assert briefing.attribution.verdict is AttributionVerdict.CLEARED_ON_ITS_OWN
        assert briefing.attribution.resource == _KEY

    def test_a_run_with_no_verdict_renders_no_block(
        self, run_state: RunState, now: datetime
    ) -> None:
        """What keeps every other context byte-identical — and every other grade unmoved."""
        briefing = render_briefing(
            _cache_run(run_state, state=IncidentState.ESCALATED, evidence=(_read(now, _PRESENT),))
        )
        assert briefing.attribution is None
        assert render_attribution(briefing.attribution) == []

    def test_the_writer_and_the_judge_see_the_same_block(
        self, run_state: RunState, now: datetime
    ) -> None:
        """INC-002's clause: the rule about this evidence reaches every reader of it."""
        briefing = render_briefing(self._cleared(run_state, now))
        block = render_attribution(briefing.attribution)
        assert ATTRIBUTION_HEADING in block
        writer = _format_context(briefing)
        judge = format_briefing_context(briefing)
        for line in block:
            assert line in writer, line
            assert line in judge, line

    def test_a_scenario_can_assert_the_verdict_deterministically(
        self, run_state: RunState, now: datetime
    ) -> None:
        """The claim `expect_briefing_contains` makes, satisfied by run state.

        Proven satisfiable before shipping (INC-001's rule), with `findings` and
        `recommendation` empty — a claim only an LLM's prose could satisfy is a claim the
        suite cannot trust.
        """
        run = self._cleared(run_state, now)
        briefing = render_briefing(run)
        assert briefing.findings == "" and briefing.recommendation == ""
        report = grade(
            run,
            ScenarioExpectation(
                name="t",
                expected_terminal_state=IncidentState.ESCALATED,
                expect_briefing_contains=("VERDICT: cleared_on_its_own",),
            ),
            briefing=briefing,
        )
        assert _dim(report, GradeDimension.EVIDENCE).passed

    @pytest.mark.parametrize(
        ("name", "verdict"),
        [
            ("temporal_ttl_recovers_before_action", "cleared_on_its_own"),
            ("temporal_ttl_recovers_during_verify", "attributed"),
        ],
    )
    def test_the_shipped_templates_assert_their_verdict(self, name: str, verdict: str) -> None:
        """The two scenarios O-29 re-derived, each claiming the arm it is about."""
        scenario = next(s for s in load_scenarios(_SCENARIOS_DIR) if s.name == name)
        claims = scenario.expectation.expect_briefing_contains
        assert f"VERDICT: {verdict}" in claims, claims


class TestTheRecoveredReadingMap:
    """One predicate table, total over the probes, with every inert entry's reason checkable."""

    def test_it_is_total_over_every_probe_tool(self) -> None:
        """Both maps, because a new probe on either side is a new question this must answer."""
        probes = {probe.tool_name for probe in ALERT_SUBJECT_PROBES.values()}
        probes |= {
            probe.tool_name
            for probes_for_action in VERIFY_PROBE_FOR_ACTION.values()
            for probe in probes_for_action
        }
        missing = sorted(probes - set(RECOVERED_READING))
        assert not missing, (
            f"{missing} observe a resource an action may touch and no RECOVERED_READING entry "
            f"says whether a reading of one shows the fault gone. Declare the entry, or "
            f"declare it None with the reason — silence would leave the guard and the grade "
            f"inert on a resource nobody decided about."
        )

    def test_every_declared_entry_names_a_real_argument(self) -> None:
        for tool, reading in RECOVERED_READING.items():
            if reading is None:
                continue
            assert reading.tool_name == tool
            fields = TOOL_REGISTRY[tool].input_model.model_fields
            assert reading.argument_field in fields, (
                f"{tool} takes no {reading.argument_field!r} argument, so no reading would "
                f"ever be matched to a resource and the rule would be silently inert"
            )
            assert reading.why, "an unexplained predicate is one nobody can review"

    def test_the_cached_read_is_inert_and_still_declared_cached(self) -> None:
        """The two halves of ADR 0009's reason, pinned together.

        If `get_consumer_lag` ever stops being a declared cached read, the reason this entry
        is inert has changed and the decision should be retaken rather than inherited.
        """
        assert RECOVERED_READING["get_consumer_lag"] is None
        assert "get_consumer_lag" in CACHED_READ_FRESHNESS_SECONDS

    def test_the_listing_is_inert_because_it_cannot_name_one_row(self) -> None:
        """INC-001 and INC-002 in map form: an absence from a filtered page proves that page."""
        assert RECOVERED_READING["list_dlq_messages"] is None
        probes = VERIFY_PROBE_FOR_ACTION["replay_dlq_by_ids"]
        listing = next(p for p in probes if p.tool_name == "list_dlq_messages")
        assert listing.argument_field is None


class TestTheExpiryIsRecordedAndNeverRead:
    """O-29 kept the evaluator's clock evaluator-only and took the grade off it."""

    def _acted(self, run_state: RunState, now: datetime) -> RunState:
        return _cache_run(
            run_state,
            state=IncidentState.RESOLVED,
            evidence=(
                _read(now, _PRESENT),
                _action(now + timedelta(seconds=60)),
                _read(now + timedelta(seconds=62), _ABSENT),
                _verdict(now + timedelta(seconds=62)),
            ),
        )

    def test_an_expiry_before_the_action_no_longer_reds_a_correct_run(
        self, run_state: RunState, now: datetime
    ) -> None:
        """The ADR 0062 case the owner reversed, and the whole point of the amendment.

        The fault expired at T+45 and the action fired at T+60, so ADR 0062 called this false
        attribution. The agent read the fault present, acted, and read it gone: nothing it
        could see says otherwise, and grading it red graded a race.
        """
        run = self._acted(run_state, now)
        passed, detail = _attribution(_graded(run, now + timedelta(seconds=45)))
        assert passed, detail
        assert "attributed" in detail

    def test_the_expiry_is_recorded_in_the_detail(self, run_state: RunState, now: datetime) -> None:
        expiry = now + timedelta(seconds=45)
        _, detail = _attribution(_graded(self._acted(run_state, now), expiry))
        assert expiry.isoformat() in detail
        assert "decides nothing" in detail

    def test_the_verdict_is_the_same_with_and_without_the_clock(
        self, run_state: RunState, now: datetime
    ) -> None:
        run = self._acted(run_state, now)
        with_clock, _ = _attribution(_graded(run, now + timedelta(seconds=45)))
        without, _ = _attribution(_graded(run, None))
        assert with_clock == without is True
