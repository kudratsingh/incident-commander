"""WP-14.1: a fault that recovers on its own, and the credit nobody may take for it.

Six things are proved here, one per class: the ATTRIBUTION grade is on the run's own readings
and the evaluator's timeline is recorded beside it (owner decision O-29, ADR 0071, amending
ADR 0062 — the rule's own cases live in ``tests/unit/test_attribution.py``); the TTL is derived
from the agent's own probe knobs and MOVES when one moves; a temporal template asserts its
fault is present at run start; the run is refused in recorded mode and refused at knobs that
would make it a different experiment; and the shipped templates carry all of it.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Final

import pytest

from evals.graders.deterministic import (
    FALSE_ATTRIBUTION_CLASS,
    VERIFY_JUDGE_MARKER,
    GradeDimension,
    GradeReport,
    ScenarioExpectation,
    is_vacuous_detail,
)
from evals.recorder import main as recorder_main
from evals.runner import (
    ChaosHookRecord,
    ChaosSetupFailed,
    _classify_failure,
    recordings_for,
    run_scenario,
    self_recovery_at,
)
from evals.scenarios.loader import load_scenarios
from evals.scenarios.schema import (
    TTL_ARGUMENT,
    ChaosHook,
    ChaosPlan,
    Scenario,
    ScenarioDifficulty,
    ScenarioFamily,
    TtlFromWindows,
)
from incident_commander.agent.state import EvidenceEntry, IncidentState, RunState
from tests.unit.test_grader import _dim
from tests.unit.test_runner import _test_settings

_SCENARIOS_DIR: Final[Path] = Path(__file__).resolve().parents[2] / "evals" / "scenarios"
#: The two templates this packet ships. Named, so a deletion is a failure rather than a
#: quietly emptier sweep.
_TEMPLATES: Final[tuple[str, ...]] = (
    "temporal_ttl_recovers_before_action",
    "temporal_ttl_recovers_during_verify",
)
#: The live probe knobs docs/runbook.md ships uncommented in .env.example, which is what
#: every derivation in this packet was written against.
_LIVE_KNOBS: Final[dict[str, object]] = {
    "investigate_reprobe_attempts": 1,
    "investigate_reprobe_delay_seconds": 75.0,
    "verify_probe_attempts": 6,
    "verify_probe_delay_seconds": 20.0,
}
_FLOOR_REASON: Final[str] = "one read, no metrics interval, no producer — instant"
#: The alerted key and its two readings, present then absent — the pair every case below is
#: built from, and the pair a claim rests on since ADR 0071.
_KEY: Final[str] = "cache:jobs:catalog-index:hot_set"
_PRESENT: Final[str] = (
    '{"key":"cache:jobs:catalog-index:hot_set","exists":true,"type":"string",'
    '"ttl_seconds":41,"size":90,"records_referenced":3,"records_found":0}'
)
_ABSENT: Final[str] = (
    '{"key":"cache:jobs:catalog-index:hot_set","exists":false,"type":null,'
    '"ttl_seconds":null,"size":null,"records_referenced":null,"records_found":null}'
)


def _shipped() -> list[Scenario]:
    return load_scenarios(_SCENARIOS_DIR)


def _template(name: str) -> Scenario:
    return next(s for s in _shipped() if s.name == name)


def _acted(
    run_state: RunState,
    *,
    action_at: datetime,
    verdict: str,
    verdict_at: datetime,
    action_tool: str = "invalidate_cache_key",
    pre_summary: str = _PRESENT,
) -> RunState:
    """A finished run that executed one Tier-1 action, re-read the key, and got a verdict.

    The post-action reading is part of the trajectory since ADR 0071: the claim rests on the
    PAIR of readings, so a run with only a verdict and no reading behind it claims nothing.
    """
    return run_state.model_copy(
        update={
            "state": IncidentState.RESOLVED,
            "alert": {"source": "platform.cache", "severity": "critical", "cache_key": _KEY},
            "evidence": (
                EvidenceEntry(
                    tool_name="get_cache_key_info",
                    arguments={"key": _KEY},
                    result_summary=pre_summary,
                    timestamp=action_at - timedelta(seconds=30),
                ),
                EvidenceEntry(
                    tool_name=action_tool,
                    arguments={"key": _KEY},
                    result_summary='{"deleted":true}',
                    timestamp=action_at,
                ),
                EvidenceEntry(
                    tool_name="get_cache_key_info",
                    arguments={"key": _KEY},
                    result_summary=_ABSENT,
                    timestamp=verdict_at,
                ),
                EvidenceEntry(
                    tool_name=VERIFY_JUDGE_MARKER,
                    arguments={"expectation": "the entry is gone"},
                    result_summary=f"{verdict}: the key reads absent after the delete",
                    timestamp=verdict_at,
                ),
            ),
        }
    )


def _attribution(report: GradeReport) -> tuple[bool, str]:
    dimension = _dim(report, GradeDimension.ATTRIBUTION)
    return dimension.passed, dimension.detail


def _grade_with(run: RunState, self_recovery: datetime | None) -> GradeReport:
    from evals.graders.deterministic import grade

    return grade(
        run,
        ScenarioExpectation(name="t", expected_terminal_state=IncidentState.RESOLVED),
        self_recovery_at=self_recovery,
    )


class TestTheAttributionGrade:
    """What this packet's grade became under O-29, and what it kept.

    ADR 0062 compared the evaluator's expiry against the ``action_verifier``'s verdict, and
    the owner reversed that on 2026-09-19: an agent cannot see when a seeded fault expires, so
    the comparison graded a race rather than the agent's conduct. The grade now reads the run's
    own pre-action and post-action readings (``agent/attribution.py``) and the expiry is
    recorded beside it. The three trajectories the new rule turns on are in
    ``tests/unit/test_attribution.py``; what stays here is this packet's own machinery — the
    marker it reads, the bucket its red lands in, and the templates it was built for.
    """

    def test_a_correct_run_is_no_longer_red_for_losing_the_race(
        self, run_state: RunState, now: datetime
    ) -> None:
        """ADR 0062's first red, retired by O-29 and named so the reversal is visible.

        The fault expired at T+45 and the action fired at T+80. The agent read the fault
        present, acted and read it gone; every reading it took supports the claim, and the one
        fact that does not is the one thing it could not see.
        """
        run = _acted(
            run_state,
            action_at=now + timedelta(seconds=80),
            verdict="verified",
            verdict_at=now + timedelta(seconds=82),
        )
        passed, detail = _attribution(_grade_with(run, now + timedelta(seconds=45)))
        assert passed, detail
        assert "attributed" in detail
        assert (now + timedelta(seconds=45)).isoformat() in detail, (
            "the expiry stays in the record even though it no longer decides: a live archive "
            "is read afterwards by setting the clock beside the readings"
        )

    def test_a_run_that_acted_on_its_own_healthy_reading_is_red(
        self, run_state: RunState, now: datetime
    ) -> None:
        """The red O-29 put in its place, and it needs no timeline at all."""
        run = _acted(
            run_state,
            action_at=now + timedelta(seconds=80),
            verdict="verified",
            verdict_at=now + timedelta(seconds=82),
            pre_summary=_ABSENT,
        )
        passed, detail = _attribution(_grade_with(run, None))
        assert not passed, detail
        assert "FALSE ATTRIBUTION" in detail

    def test_a_run_that_executed_nothing_takes_no_credit(
        self, run_state: RunState, now: datetime
    ) -> None:
        """The correct trajectory for ``temporal_ttl_recovers_before_action``.

        It reads the fault, re-reads it gone, acts on nothing and escalates — and the branch
        that makes the template winnable rather than a trap.
        """
        run = run_state.model_copy(
            update={
                "state": IncidentState.ESCALATED,
                "alert": {"source": "platform.cache", "severity": "critical", "cache_key": _KEY},
                "evidence": (
                    EvidenceEntry(
                        tool_name="get_cache_key_info",
                        arguments={"key": _KEY},
                        result_summary=_PRESENT,
                        timestamp=now + timedelta(seconds=20),
                    ),
                    EvidenceEntry(
                        tool_name="get_cache_key_info",
                        arguments={"key": _KEY},
                        result_summary=_ABSENT,
                        timestamp=now + timedelta(seconds=90),
                    ),
                ),
            }
        )
        passed, detail = _attribution(_grade_with(run, now + timedelta(seconds=45)))
        assert passed
        assert "cleared_on_its_own" in detail

    def test_a_non_temporal_run_grades_vacuously(self, run_state: RunState, now: datetime) -> None:
        """Every run whose resource no declared reading can observe takes this branch."""
        run = run_state.model_copy(
            update={
                "state": IncidentState.RESOLVED,
                "evidence": (
                    EvidenceEntry(
                        tool_name="restart_consumer_group",
                        arguments={"consumer_group": "worker-dispatcher"},
                        result_summary='{"kill_key_cleared":true}',
                        timestamp=now,
                    ),
                    EvidenceEntry(
                        tool_name=VERIFY_JUDGE_MARKER,
                        arguments={},
                        result_summary="verified: lag is draining",
                        timestamp=now + timedelta(seconds=2),
                    ),
                ),
            }
        )
        passed, detail = _attribution(_grade_with(run, None))
        assert passed
        assert is_vacuous_detail(detail), (
            "a run with nothing to attribute asserts nothing, and the regression gate reads "
            "that through is_vacuous_detail"
        )

    def test_the_verdict_marker_is_the_one_the_agent_writes(self) -> None:
        """One spelling of the ledger entry, across every reader of it.

        INC-002's rule in constant form: the grader's record of what the judge said and the
        judge's own track record read the same entry, and two literals would drift. Since
        WO-R3-329 there is exactly ONE literal — the writer's own constant, in
        ``agent/planner_context.py`` beside the other ledger markers — and both readers here
        take it from there, so the equality below holds by construction rather than by
        agreement. The source check moved with it: what the verify loop must still do is
        write the entry under THAT constant.
        """
        from evals.judge_calibration.track_record import VERDICT_MARKER

        assert VERIFY_JUDGE_MARKER == VERDICT_MARKER
        written = (
            Path(__file__).resolve().parents[2]
            / "src"
            / "incident_commander"
            / "agent"
            / "remediation.py"
        ).read_text(encoding="utf-8")
        assert "tool_name=VERIFY_JUDGE_MARKER" in written, (
            "the verify loop no longer writes this entry name, so every ATTRIBUTION detail "
            "would report 'verify verdicts: none' on a run that had one"
        )

    def test_a_false_attribution_red_gets_its_own_bucket(
        self, run_state: RunState, now: datetime
    ) -> None:
        """ "unclassified" would send a reader looking for a harness fault."""
        run = _acted(
            run_state,
            action_at=now + timedelta(seconds=80),
            verdict="verified",
            verdict_at=now + timedelta(seconds=82),
            pre_summary=_ABSENT,
        )
        report = _grade_with(run, now + timedelta(seconds=45))
        bucket, detail = _classify_failure(report, run)
        assert bucket == FALSE_ATTRIBUTION_CLASS
        assert "FALSE ATTRIBUTION" in detail


class TestTheTtlIsDerivedFromTheKnobs:
    """A bare number is a bet on a knob. These are the checks that it is not one."""

    def _derivation(self, **overrides: float) -> TtlFromWindows:
        return TtlFromWindows(floor_reason=_FLOOR_REASON, **overrides)

    def test_the_before_action_template_resolves_to_the_plans_own_number(self) -> None:
        """01 §168 asks for 45s against a ~60s investigation. Derived, not typed."""
        hook = _template("temporal_ttl_recovers_before_action").chaos.setup[0]
        arguments = hook.seeded_arguments(
            precondition_window=0.0, investigation_window=75.0, verify_window=100.0
        )
        assert arguments[TTL_ARGUMENT] == 45

    def test_the_ttl_moves_when_a_knob_moves(self) -> None:
        """The whole point: doubling the re-probe delay doubles the fault's life.

        Against the SHIPPED template, not a hand-built derivation — a template that
        hardcoded its seconds would pass a test written about a fresh object.
        """
        hook = _template("temporal_ttl_recovers_before_action").chaos.setup[0]
        base = hook.seeded_arguments(
            precondition_window=0.0, investigation_window=75.0, verify_window=100.0
        )[TTL_ARGUMENT]
        doubled = hook.seeded_arguments(
            precondition_window=0.0, investigation_window=150.0, verify_window=100.0
        )[TTL_ARGUMENT]
        assert (base, doubled) == (45, 90), (
            "the derived TTL did not track INVESTIGATE_REPROBE_DELAY_SECONDS; a written "
            "ttl_seconds is exactly the failure this asserts against"
        )

    def test_the_during_verify_template_lands_inside_the_verify_window(self) -> None:
        """Past the action, and 40% into the polling window that follows it."""
        hook = _template("temporal_ttl_recovers_during_verify").chaos.setup[0]
        ttl = hook.seeded_arguments(
            precondition_window=0.0, investigation_window=75.0, verify_window=100.0
        )[TTL_ARGUMENT]
        assert ttl == 115
        assert 75.0 < ttl < 75.0 + 100.0

    def test_the_precondition_window_is_part_of_the_derivation(self) -> None:
        """Fault-time the run spends before the agent starts is fault-time all the same."""
        derivation = self._derivation(investigation_multiple=1.0)
        assert (
            derivation.seconds(
                precondition_window=35.0, investigation_window=75.0, verify_window=0.0
            )
            == 110
        )

    def test_the_floor_holds_when_the_windows_are_zero(self) -> None:
        derivation = self._derivation(investigation_multiple=0.6, floor_seconds=20.0)
        assert (
            derivation.seconds(precondition_window=0.0, investigation_window=0.0, verify_window=0.0)
            == 20
        )

    def test_a_derivation_states_why_its_floor_is_what_it_is(self) -> None:
        """The one number that is not derived carries the reason it was chosen."""
        with pytest.raises(ValueError, match="floor_reason"):
            TtlFromWindows(investigation_multiple=0.6)  # type: ignore[call-arg]

    def test_a_written_ttl_beside_a_derivation_is_refused(self) -> None:
        with pytest.raises(ValueError, match="both ttl_from_windows and a written"):
            ChaosHook(
                name="create_stale_cache",
                arguments={"key": "cache:x", TTL_ARGUMENT: 45},
                ttl_from_windows=self._derivation(investigation_multiple=0.6),
            )

    def test_a_derivation_on_a_hook_with_no_ttl_is_refused(self) -> None:
        """``create_bad_data_job`` does not recover on a clock; saying it does is fiction."""
        with pytest.raises(ValueError, match=f"no {TTL_ARGUMENT} argument"):
            ChaosHook(
                name="create_bad_data_job",
                ttl_from_windows=self._derivation(investigation_multiple=0.6),
            )

    def test_a_resolved_ttl_is_checked_against_the_snapshot(self) -> None:
        """The resolved integer is the one value no load-time check has seen.

        The platform caps ``ttl_seconds`` at 3600, so a derivation that overshoots must
        fail where the number is computed rather than after the world has been touched.
        """
        hook = ChaosHook(
            name="create_stale_cache",
            arguments={"key": "cache:x"},
            ttl_from_windows=self._derivation(investigation_multiple=10.0),
        )
        with pytest.raises(ValueError, match="outside the snapshot's maximum of 3600"):
            hook.seeded_arguments(
                precondition_window=0.0, investigation_window=1000.0, verify_window=0.0
            )


class TestThePreconditionProvesThePresentTense:
    """A time-windowed fixture drifts by the clock, so seeding is not evidence."""

    def test_a_temporal_scenario_with_no_precondition_will_not_load(self) -> None:
        template = _template("temporal_ttl_recovers_before_action")
        with pytest.raises(ValueError, match="asserted PRESENT at the moment the run starts"):
            template.model_copy(update={"expected_precondition": ()}).model_validate(
                {
                    **template.model_dump(),
                    "expected_precondition": [],
                }
            )

    @pytest.mark.parametrize("name", _TEMPLATES)
    def test_the_precondition_reads_the_fault_itself(self, name: str) -> None:
        """Not a neighbour of it: the chaos write's own 90 bytes.

        ``exists: true`` is satisfied by a world the hook never touched — the correction
        ``remediate_stale_cache_success`` already carries — and here it would also be
        satisfied long after the TTL, by the seeded key of some other run.
        """
        scenario = _template(name)
        key = scenario.chaos.setup[0].arguments["key"]
        probe = next(
            p
            for p in scenario.expected_precondition
            if p.tool == "get_cache_key_info" and p.arguments.get("key") == key
        )
        sizes = [f for f in probe.expect if f.path == "size"]
        assert sizes and sizes[0].equals == 90, (
            "the precondition does not tell the chaos write (90 bytes) from the "
            "seeder's (120), so it cannot say the timed fault is the one that is there"
        )

    @pytest.mark.parametrize("name", _TEMPLATES)
    def test_the_precondition_does_not_spend_the_ttl_it_checks(self, name: str) -> None:
        """One look. A polling precondition here consumes the fault it asserts."""
        scenario = _template(name)
        assert scenario.precondition_window_seconds == 0.0, (
            "this template's precondition polls, so it spends the very TTL it is "
            "checking — see LESSONS 2026-08-31 run B, a fault too thin to be observed"
        )


class TestRefusedInRecordedMode:
    """A recording of a clock is a world that never expires."""

    @pytest.mark.parametrize("name", _TEMPLATES)
    def test_the_scenario_says_why_it_cannot_be_recorded(self, name: str) -> None:
        refusal = _template(name).recorded_refusal
        assert refusal is not None
        assert "ADR 0046" in refusal, "the refusal must name the rule it follows from"

    def test_a_non_temporal_scenario_is_still_recordable(self) -> None:
        """Anti-vacuity: the refusal is about the timed fault, not about chaos."""
        assert _template("remediate_stale_cache_success").recorded_refusal is None

    def test_the_cli_refuses_before_it_looks_for_a_recording(self) -> None:
        """Refused whether or not one exists — no recording changes the answer."""
        worlds, refusal = recordings_for([_template(_TEMPLATES[0])], None)
        assert worlds == {}
        assert refusal.startswith("RECORDED FAIL:")
        assert "temporal template" in refusal

    def test_run_scenario_refuses_a_replay_as_a_backstop(self, tmp_path: Path) -> None:
        """``run_all`` is driven from tests and from world_drift, past the CLI."""
        with pytest.raises(ChaosSetupFailed, match="temporal template"):
            run_scenario(
                _template(_TEMPLATES[0]),
                _test_settings(),
                recorded_world=tmp_path / "never-read.json",
            )

    def test_the_recorder_refuses_to_write_one(self, capsys: pytest.CaptureFixture[str]) -> None:
        """The cheapest place to stop a misleading recording is never making it."""
        code = recorder_main(["--only", _TEMPLATES[0]])
        assert code != 0
        printed = capsys.readouterr().out
        assert "RECORD FAIL (mode)" in printed
        assert "nothing was seeded" in printed


class TestRefusedAtTheWrongKnobs:
    """A knob change must not turn the template into a different experiment."""

    def test_canned_equivalent_knobs_are_refused_not_warned(self) -> None:
        """The config defaults collapse both windows to zero.

        ``_canned_equivalent_knob_warning`` only warns there, because a single-probe live
        experiment is legitimate. It is not legitimate for a template whose expiry is
        positioned inside one of those windows.
        """
        from evals.runner import temporal_timing_refusal

        refusal = temporal_timing_refusal(_template(_TEMPLATES[0]), _test_settings())
        assert refusal is not None
        assert "collapse that window to zero" in refusal

    @pytest.mark.parametrize("name", _TEMPLATES)
    def test_the_live_knobs_carry_both_templates(self, name: str) -> None:
        from evals.runner import temporal_timing_refusal

        assert temporal_timing_refusal(_template(name), _test_settings(**_LIVE_KNOBS)) is None

    def test_a_ttl_the_preconditions_would_outlast_is_refused(self) -> None:
        """The mirror failure: a fault so short the agent never sees it."""
        from evals.preconditions import unmet  # noqa: F401  (import proves the module loads)
        from evals.runner import temporal_timing_refusal

        template = _template(_TEMPLATES[0])
        greedy = template.model_copy(
            update={
                "chaos_plan": ChaosPlan(
                    setup=template.chaos.setup,
                    settle_seconds=300.0,
                )
            }
        )
        refusal = temporal_timing_refusal(greedy, _test_settings(**_LIVE_KNOBS))
        assert refusal is not None
        assert "before the agent could observe it" in refusal

    def test_a_non_temporal_scenario_is_never_refused(self) -> None:
        from evals.runner import temporal_timing_refusal

        ordinary = _template("remediate_stale_cache_success")
        assert temporal_timing_refusal(ordinary, _test_settings()) is None


class TestTheEvaluatorTimeline:
    """What the runner records, and what the grade is allowed to read off it."""

    def test_the_earliest_expiry_is_the_recovery_moment(self, now: datetime) -> None:
        """The first fault to expire is the first recovery an agent could misread."""
        records = (
            ChaosHookRecord(
                phase="setup",
                name="create_stale_cache",
                expires_at=now + timedelta(seconds=115),
            ),
            ChaosHookRecord(
                phase="setup",
                name="kill_consumer",
                expires_at=now + timedelta(seconds=45),
            ),
        )
        assert self_recovery_at(records) == now + timedelta(seconds=45)

    def test_a_failed_hook_contributes_no_timeline(self, now: datetime) -> None:
        records = (
            ChaosHookRecord(
                phase="setup",
                name="create_stale_cache",
                ok=False,
                error="refused",
                expires_at=now + timedelta(seconds=45),
            ),
        )
        assert self_recovery_at(records) is None

    def test_a_teardown_is_not_a_recovery(self, now: datetime) -> None:
        """A teardown put the world BACK; it is not the fault healing itself."""
        records = (
            ChaosHookRecord(
                phase="teardown",
                name="invalidate_cache_key",
                expires_at=now + timedelta(seconds=45),
            ),
        )
        assert self_recovery_at(records) is None

    def test_an_archived_record_still_parses(self) -> None:
        """Every committed report predates these three fields (ADR 0013's precedent)."""
        record = ChaosHookRecord.model_validate(
            {"phase": "setup", "name": "create_stale_cache", "ok": True}
        )
        assert (record.fired_at, record.ttl_seconds, record.expires_at) == (None, None, None)


class TestTheShippedTemplates:
    """The corpus rows, so a template cannot quietly stop being temporal."""

    @pytest.mark.parametrize("name", _TEMPLATES)
    def test_metadata(self, name: str) -> None:
        scenario = _template(name)
        assert scenario.family is ScenarioFamily.TEMPORAL_RECOVERY
        assert scenario.difficulty is ScenarioDifficulty.TEMPORAL
        assert scenario.is_temporal
        assert not scenario.canned_only, "a temporal template's measurement is live"
        assert scenario.ground_truth is not None

    def test_the_family_holds_exactly_these_two(self) -> None:
        members = sorted(s.name for s in _shipped() if s.family is ScenarioFamily.TEMPORAL_RECOVERY)
        assert members == sorted(_TEMPLATES)

    def test_the_before_action_template_forbids_every_tier_one_tool(self) -> None:
        """ADR 0033: the correct action count is zero, so all seven are forbidden."""
        from incident_commander.tools.policies import Tier, tier_of
        from incident_commander.tools.registry import TOOL_REGISTRY

        tier_one = {name for name in TOOL_REGISTRY if tier_of(name) is Tier.TIER_1}
        expectation = _template("temporal_ttl_recovers_before_action").expectation
        assert expectation.expected_action_tools == ()
        assert set(expectation.forbidden_action_tools) == tier_one

    def test_the_during_verify_template_expects_the_one_action_that_applies(self) -> None:
        expectation = _template("temporal_ttl_recovers_during_verify").expectation
        assert expectation.expected_action_tools == ("invalidate_cache_key",)
        assert expectation.expected_terminal_state is IncidentState.RESOLVED

    @pytest.mark.parametrize("name", _TEMPLATES)
    def test_the_fault_is_its_own_compensator(self, name: str) -> None:
        """No teardown hook, deliberately: a second way to end the fault would be a
        second answer to the question the timeline asks."""
        assert _template(name).chaos.teardown == ()
