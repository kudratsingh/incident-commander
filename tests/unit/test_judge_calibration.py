"""WP-6.3: the judge calibration harness, and the gate it opens.

Five things are pinned, the first four with a red-before: trap sets in plan 03 § 107's
shapes with every verdict stated; agreement scored on the first ask and stability over N
(no temperature — ADR 0048 / O-24); a registered, versioned, create-only report, one
family per judge; no judge number without a report id; no bare "verifier" (plan 02:25).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

import pytest

from evals import artifacts
from evals.graders.llm_judge import JUDGE_PROMPT, JudgeScore, format_briefing_context
from evals.judge_calibration import roles, track_record
from evals.judge_calibration.fakes import FakeJudgeClient, answers_for, payload_for
from evals.judge_calibration.harness import (
    ARTIFACT_KIND,
    FAKE_CLIENT,
    LIVE_CLIENT,
    SELF_AGREEMENT_REPS,
    calibrate,
    write_report,
)
from evals.judge_calibration.roles import (
    ABSENT_ROLES,
    ACTION_VERIFIER,
    BRIEFING_JUDGE,
    CALIBRATED_ROLES,
    CANDIDATE_SELECTOR,
    PLAN_APPROVAL_JUDGE,
    SELECT_PREFIX,
    briefing_verdict,
    is_approval,
)
from evals.judge_calibration.traps import MINIMUM_TRAPS_PER_JUDGE, TRAPS, TrapCase, traps_for
from evals.research_report import (
    JUDGE_CALIBRATION_REPORTS,
    LEADERBOARD_JUDGE,
    WITHHELD_JUDGE,
    Row,
    arm_summary,
)
from incident_commander.agent.remediation import (
    VERIFICATION_JUDGE_PROMPT,
    VerificationJudgment,
    judge_verification,
)
from incident_commander.agent.selection import SELECTOR_PROMPT
from incident_commander.llm.prompts.loader import load_prompt
from incident_commander.llm.repair import (
    MAX_OUTPUT_REPAIRS,
    OutputRepairExhausted,
    RepairedCall,
)

_MODEL: Final[str] = "fake-judge"
_AT: Final[datetime] = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)


def _agreeing(judge: str, **kwargs: Any) -> FakeJudgeClient:
    return FakeJudgeClient(answers_for(judge, **kwargs))


def _case(judge: str, case_id: str) -> TrapCase:
    """One trap case by id, or a clear failure if it was renamed."""
    for case in traps_for(judge):
        if case.case_id == case_id:
            return case
    raise AssertionError(f"no trap case {case_id!r} for {judge}")


def _verify_subject(case_id: str) -> roles.VerifySubject:
    subject = _case(ACTION_VERIFIER, case_id).subject
    assert isinstance(subject, roles.VerifySubject)
    return subject


def _briefing_subject(case_id: str) -> roles.BriefingSubject:
    subject = _case(BRIEFING_JUDGE, case_id).subject
    assert isinstance(subject, roles.BriefingSubject)
    return subject


class TestTheTrapSetsAreGroundTruth:
    """Plan 03 § 107's shapes, each with the verdict it asserts."""

    @pytest.mark.parametrize("judge", CALIBRATED_ROLES)
    def test_every_judge_has_at_least_the_floor(self, judge: str) -> None:
        assert len(traps_for(judge)) >= MINIMUM_TRAPS_PER_JUDGE

    @pytest.mark.parametrize("judge", CALIBRATED_ROLES)
    def test_case_ids_are_unique_within_a_judge(self, judge: str) -> None:
        ids = [case.case_id for case in traps_for(judge)]
        assert len(ids) == len(set(ids))

    @pytest.mark.parametrize("judge", CALIBRATED_ROLES)
    def test_every_case_asserts_a_verdict_in_its_role_space(self, judge: str) -> None:
        """A trap whose asserted verdict the judge cannot emit is unfalsifiable."""
        space = roles.role(judge).verdicts
        for case in traps_for(judge):
            if judge == CANDIDATE_SELECTOR and case.asserts.startswith(SELECT_PREFIX):
                chosen = case.asserts[len(SELECT_PREFIX) :]
                subject = case.subject
                assert isinstance(subject, roles.SelectionSubject)
                assert chosen in {c.candidate_id for c in subject.candidates}, case.case_id
                continue
            assert case.asserts in space, f"{case.case_id} asserts {case.asserts!r}"

    @pytest.mark.parametrize("judge", CALIBRATED_ROLES)
    def test_every_case_states_why(self, judge: str) -> None:
        """The label is only ground truth if the argument for it is on the record."""
        for case in traps_for(judge):
            assert len(case.why) > 120, case.case_id
            assert case.shape, case.case_id

    @pytest.mark.parametrize("judge", CALIBRATED_ROLES)
    def test_a_trap_set_covers_both_sides_of_its_approval_line(self, judge: str) -> None:
        """At least one case the judge should bless and one it should not.

        A one-sided set is passed by a judge that refuses, or blesses, everything.
        """
        verdicts = [case.asserts for case in traps_for(judge)]
        assert any(is_approval(judge, verdict) for verdict in verdicts)
        assert any(not is_approval(judge, verdict) for verdict in verdicts)

    @pytest.mark.parametrize("judge", CALIBRATED_ROLES)
    def test_no_case_names_hidden_lab_machinery(self, judge: str) -> None:
        """ADR 0012, applied to what a judge reads.

        The injection machinery's name in a trap would ask a question no run produces.
        """
        for case in traps_for(judge):
            assert "chaos" not in case.context().lower(), case.case_id

    def test_the_trap_contexts_are_deterministic(self) -> None:
        """Two renders of one trap are the same bytes.

        The scripted fake is keyed on the rendered context, so a fresh uuid would make the
        script miss; ``evidence_id`` defaults to ``uuid4()``.
        """
        for judge in CALIBRATED_ROLES:
            for case in traps_for(judge):
                assert case.context() == case.context(), case.case_id

    def test_the_selector_traps_cite_only_readings_in_their_own_trail(self) -> None:
        """ADR 0042's validator, exercised over the committed trap data.

        A candidate citing an id the trap's ledger does not hold raises, so the set cannot
        drift.
        """
        for case in traps_for(CANDIDATE_SELECTOR):
            subject = case.subject
            assert isinstance(subject, roles.SelectionSubject)
            ledger = {entry.evidence_id for entry in subject.run_state.evidence}
            for candidate in subject.candidates:
                cited = {ref.evidence_id for ref in candidate.evidence_for} | {
                    ref.evidence_id for ref in candidate.evidence_against
                }
                assert cited <= ledger, case.case_id

    def test_the_absent_role_has_no_trap_set_and_says_why(self) -> None:
        """Divergence B6: ``plan_approval_judge`` is not a judge."""
        assert PLAN_APPROVAL_JUDGE not in TRAPS
        assert PLAN_APPROVAL_JUDGE not in CALIBRATED_ROLES
        assert PLAN_APPROVAL_JUDGE in ABSENT_ROLES
        with pytest.raises(KeyError, match="does not exist in any form"):
            roles.role(PLAN_APPROVAL_JUDGE)


class TestTheHarnessMeasuresWhatItSays:
    @pytest.mark.parametrize("judge", CALIBRATED_ROLES)
    def test_a_judge_that_agrees_scores_one(self, judge: str) -> None:
        report = calibrate(judge, client=_agreeing(judge), model=_MODEL, now=_AT)
        assert report.trap_agreement["accuracy"] == 1.0
        assert report.trap_agreement["false_approves"] == 0
        assert report.trap_agreement["false_rejects"] == 0
        assert report.self_agreement["fraction_identical"] == 1.0
        assert report.trap_agreement["answered"] == len(traps_for(judge))

    def test_a_false_approve_is_counted_on_the_refusal_side(self) -> None:
        """A blessed failure: asserted ``not_verified``, judge said ``verified``."""
        case = "av-02-read-shows-no-movement"
        client = _agreeing(ACTION_VERIFIER, wrong={case: "verified"})
        report = calibrate(ACTION_VERIFIER, client=client, model=_MODEL, now=_AT)
        assert report.trap_agreement["false_approves"] == 1
        assert report.trap_agreement["false_rejects"] == 0
        assert report.trap_agreement["accuracy"] == pytest.approx(5 / 6, abs=1e-4)
        disagreed = [row for row in report.trap_agreement["by_shape"] if not row["agrees"]]
        assert [row["case_id"] for row in disagreed] == [case]

    def test_a_false_reject_is_counted_on_the_approval_side(self) -> None:
        """INC-002's shape: an honest briefing marked ungrounded."""
        case = "bj-05-inc-002-honest-after-a-filtered-read"
        client = _agreeing(
            BRIEFING_JUDGE, wrong={case: briefing_verdict(grounded=False, actionable=True)}
        )
        report = calibrate(BRIEFING_JUDGE, client=client, model=_MODEL, now=_AT)
        assert report.trap_agreement["false_rejects"] == 1
        assert report.trap_agreement["false_approves"] == 0

    def test_a_wrong_approval_is_neither_a_false_approve_nor_a_false_reject(self) -> None:
        """The selector's own error: committed, and to the wrong candidate.

        Folding ``select:c1`` for ``select:c2`` into either rate would hide it.
        """
        case = "cs-02-wrong-candidate-plausible-evidence"
        client = _agreeing(CANDIDATE_SELECTOR, wrong={case: f"{SELECT_PREFIX}c1"})
        report = calibrate(CANDIDATE_SELECTOR, client=client, model=_MODEL, now=_AT)
        assert report.trap_agreement["wrong_approvals"] == 1
        assert report.trap_agreement["false_approves"] == 0
        assert report.trap_agreement["false_rejects"] == 0
        assert report.trap_agreement["accuracy"] == pytest.approx(5 / 6, abs=1e-4)

    def test_instability_is_measured_and_named(self) -> None:
        case = "av-05-filtered-read-proves-its-slice"
        client = _agreeing(ACTION_VERIFIER, unstable={case: "not_verified"})
        report = calibrate(ACTION_VERIFIER, client=client, model=_MODEL, now=_AT)
        assert report.self_agreement["fraction_identical"] == pytest.approx(5 / 6, abs=1e-4)
        assert [row["case_id"] for row in report.self_agreement["unstable"]] == [case]
        # Agreement is scored on the FIRST ask, so a case that drifts on rep 5
        # still agreed: a judge is called once in a run.
        assert report.trap_agreement["accuracy"] == 1.0

    def test_agreement_is_the_first_ask_not_a_vote(self) -> None:
        """A judge wrong first and right later has not agreed.

        A majority over the reps would measure an ensemble nothing here runs.
        """
        case = "av-02-read-shows-no-movement"
        script = answers_for(ACTION_VERIFIER)
        target = next(c for c in traps_for(ACTION_VERIFIER) if c.case_id == case)
        script[target.context()] = [
            payload_for(target, "verified"),
            payload_for(target, "not_verified"),
        ]
        report = calibrate(ACTION_VERIFIER, client=FakeJudgeClient(script), model=_MODEL, now=_AT)
        outcome = next(row for row in report.outcomes if row.case_id == case)
        assert outcome.observed[0] == "verified"
        assert not outcome.agrees
        assert not outcome.stable

    def test_a_judge_that_cannot_answer_is_an_error_not_a_disagreement(self) -> None:
        """A malformed reply twice over is a harness event (ADR 0035).

        In neither numerator nor denominator: an envelope failure is not judgement.
        """
        case = "av-01-read-shows-recovery"
        script = answers_for(ACTION_VERIFIER)
        target = next(c for c in traps_for(ACTION_VERIFIER) if c.case_id == case)
        script[target.context()] = [{"verdict": "maybe", "reasoning": "not in the schema"}]
        report = calibrate(ACTION_VERIFIER, client=FakeJudgeClient(script), model=_MODEL, now=_AT)
        assert report.trap_agreement["answered"] == 5
        assert report.trap_agreement["accuracy"] == 1.0
        assert [row["case_id"] for row in report.trap_agreement["errors"]] == [case]

    @pytest.mark.parametrize(
        ("judge", "prompt"),
        [
            (ACTION_VERIFIER, VERIFICATION_JUDGE_PROMPT),
            (BRIEFING_JUDGE, JUDGE_PROMPT),
            (CANDIDATE_SELECTOR, SELECTOR_PROMPT),
        ],
    )
    def test_the_calibration_asks_through_the_judges_real_prompt(
        self, judge: str, prompt: str
    ) -> None:
        """Not a copy of the rubric, the rubric.

        A harness asking a prompt the run does not use calibrates a judge nobody calls.
        """
        client = _agreeing(judge)
        calibrate(judge, client=client, model=_MODEL, now=_AT)
        expected = load_prompt(prompt)
        assert client.calls
        assert {system for system, _ in client.calls} == {expected}

    @pytest.mark.parametrize("judge", CALIBRATED_ROLES)
    def test_no_temperature_is_sent(self, judge: str) -> None:
        """ADR 0048 and owner decision O-24, asserted on the call.

        A pinned temperature 0 would 400 on the next re-pin
        (``SAMPLING_REJECTED_MODELS``).
        """
        client = _agreeing(judge)
        report = calibrate(judge, client=client, model=_MODEL, now=_AT)
        assert set(client.temperatures) == {None}
        assert report.determinism["temperature_sent"] is None
        assert report.determinism["fixed_json_schema"] is True
        assert "SAMPLING_REJECTED_MODELS" in report.determinism["why_no_temperature"]

    @pytest.mark.parametrize("judge", CALIBRATED_ROLES)
    def test_every_case_is_asked_reps_times(self, judge: str) -> None:
        client = _agreeing(judge)
        calibrate(judge, client=client, model=_MODEL, reps=3, now=_AT)
        assert len(client.calls) == 3 * len(traps_for(judge))

    def test_reps_below_one_is_refused(self) -> None:
        with pytest.raises(ValueError, match="at least 1"):
            calibrate(ACTION_VERIFIER, client=_agreeing(ACTION_VERIFIER), model=_MODEL, reps=0)

    def test_the_default_reps_is_the_protocols_n(self) -> None:
        assert SELF_AGREEMENT_REPS == 5

    @pytest.mark.parametrize("judge", CALIBRATED_ROLES)
    def test_the_report_names_the_exact_rubric_it_measured(self, judge: str) -> None:
        """Plan 03 § 110's attribution rule, made checkable after the fact."""
        report = calibrate(judge, client=_agreeing(judge), model=_MODEL, now=_AT)
        rubric = load_prompt(roles.role(judge).prompt)
        assert report.rubric["lines"] == len(rubric.splitlines())
        assert len(str(report.rubric["sha256"])) == 64
        assert "one line at a time" in report.rubric["one_line_at_a_time"]

    def test_a_rubric_edit_moves_the_hash(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Otherwise the field is decoration rather than attribution."""
        before = calibrate(
            ACTION_VERIFIER, client=_agreeing(ACTION_VERIFIER), model=_MODEL, now=_AT
        ).rubric["sha256"]
        real = load_prompt

        def edited(name: str) -> str:
            text = real(name)
            return f"{text}\n- One more check, added by this test.\n"

        monkeypatch.setattr("incident_commander.llm.prompts.loader.load_prompt", edited)
        monkeypatch.setattr("evals.judge_calibration.roles.load_prompt", edited, raising=False)
        after = calibrate(
            ACTION_VERIFIER, client=_agreeing(ACTION_VERIFIER), model=_MODEL, now=_AT
        ).rubric["sha256"]
        assert after != before

    def test_a_fake_client_report_is_not_a_measurement(self) -> None:
        """The property, not a filename convention.

        A scripted report must never be entered in the register, and the thing
        that stops it has to be checkable.
        """
        fake = calibrate(ACTION_VERIFIER, client=_agreeing(ACTION_VERIFIER), model=_MODEL, now=_AT)
        assert fake.judge_client == FAKE_CLIENT
        assert not fake.is_a_measurement
        real = calibrate(
            ACTION_VERIFIER,
            client=_agreeing(ACTION_VERIFIER),
            model=_MODEL,
            client_kind=LIVE_CLIENT,
            now=_AT,
        )
        assert real.is_a_measurement

    def test_a_short_trap_set_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The floor is enforced where the number is used, not only documented."""
        monkeypatch.setattr(
            "evals.judge_calibration.harness.traps_for",
            lambda judge: traps_for(judge)[:2],
        )
        with pytest.raises(ValueError, match="at least 5"):
            calibrate(ACTION_VERIFIER, client=_agreeing(ACTION_VERIFIER), model=_MODEL)


class TestTheRepairSurvivedTheExtraction:
    """WO-R2-174's repair, through the function WP-6.3 extracted.

    ``judge_verification`` is the one spelling of the ``action_verifier``'s call, and
    losing the wrapper would measure a judge that escalates on its own parse failure.
    """

    _CASE: Final[str] = "av-01-read-shows-recovery"

    def _client(self, payloads: list[dict[str, Any]]) -> FakeJudgeClient:
        return FakeJudgeClient({_case(ACTION_VERIFIER, self._CASE).context(): payloads})

    def _ask(self, client: FakeJudgeClient) -> RepairedCall[VerificationJudgment]:
        subject = _verify_subject(self._CASE)
        return judge_verification(
            client,
            plan=subject.plan,
            probe_summary=subject.probe_summary,
            action_summary=subject.action_summary,
            model=_MODEL,
        )

    def test_one_malformed_reply_is_repaired(self) -> None:
        client = self._client(
            [
                {"verdict": "probably", "reasoning": "not a member of the Literal"},
                {"verdict": "verified", "reasoning": "lag is 2 against the 29 that alerted"},
            ]
        )
        call = self._ask(client)
        assert call.was_repaired
        assert len(call.failures) == MAX_OUTPUT_REPAIRS
        assert call.result.output.verdict == "verified"

    def test_a_second_malformed_reply_escalates(self) -> None:
        client = self._client([{"verdict": "probably", "reasoning": "still not a member"}])
        with pytest.raises(OutputRepairExhausted):
            self._ask(client)

    def test_the_verdict_field_is_still_closed(self) -> None:
        """The negative fence: nothing outside the two verdicts parses."""
        with pytest.raises(ValueError):
            VerificationJudgment.model_validate({"verdict": "maybe", "reasoning": "x"})


class TestTheReportIsEvidence:
    def test_the_kind_is_registered(self) -> None:
        """Divergence D2: ``KINDS`` is closed, so an unregistered family cannot
        be resolved by ``newest()`` and its writes would not be exclusive-create.
        """
        assert ARTIFACT_KIND in artifacts.KINDS
        assert artifacts.KINDS[ARTIFACT_KIND].per_scenario

    def test_one_family_per_judge(self, tmp_path: Path) -> None:
        """Three judges must not share one resolution.

        A flat stem has no room for the judge's name, so ``newest()`` would return whichever
        was written last.
        """
        written = {}
        for judge in CALIBRATED_ROLES:
            report = calibrate(judge, client=_agreeing(judge), model=_MODEL, now=_AT)
            written[judge] = write_report(report, directory=tmp_path)
        for judge, path in written.items():
            resolved = artifacts.newest(ARTIFACT_KIND, judge, directory=tmp_path)
            assert resolved == path
            assert json.loads(path.read_text())["judge"] == judge

    def test_a_second_calibration_lands_beside_the_first(self, tmp_path: Path) -> None:
        """Invariant 9: two calibrations are two facts."""
        first = calibrate(ACTION_VERIFIER, client=_agreeing(ACTION_VERIFIER), model=_MODEL, now=_AT)
        second = calibrate(
            ACTION_VERIFIER,
            client=_agreeing(ACTION_VERIFIER, wrong={"av-02-read-shows-no-movement": "verified"}),
            model=_MODEL,
            now=datetime(2026, 9, 18, 12, 0, tzinfo=UTC),
        )
        first_path = write_report(first, directory=tmp_path)
        second_path = write_report(second, directory=tmp_path)
        assert first_path != second_path
        assert first_path.is_file()
        versions = artifacts.versions(ARTIFACT_KIND, ACTION_VERIFIER, directory=tmp_path)
        assert versions == [first_path, second_path]

    def test_writing_the_same_report_twice_raises(self, tmp_path: Path) -> None:
        """Exclusive-create, like every other artifact write here."""
        report = calibrate(
            ACTION_VERIFIER, client=_agreeing(ACTION_VERIFIER), model=_MODEL, now=_AT
        )
        write_report(report, directory=tmp_path)
        with pytest.raises(FileExistsError):
            write_report(report, directory=tmp_path)

    def test_the_report_json_carries_every_leg(self, tmp_path: Path) -> None:
        report = calibrate(BRIEFING_JUDGE, client=_agreeing(BRIEFING_JUDGE), model=_MODEL, now=_AT)
        payload = json.loads(write_report(report, directory=tmp_path).read_text())
        for key in (
            "trap_agreement",
            "self_agreement",
            "ground_truth_agreement",
            "determinism",
            "rubric",
            "protocol",
            "absent_roles",
            "cases",
        ):
            assert key in payload, key
        assert payload["judge_client"] == FAKE_CLIENT
        assert PLAN_APPROVAL_JUDGE in payload["absent_roles"]


class TestTheTrackRecord:
    """The free leg, and the two refusals that are structural rather than a gap."""

    def test_the_selector_leg_is_refused_as_circular(self) -> None:
        leg = track_record.ground_truth_agreement(CANDIDATE_SELECTOR)
        assert not leg["measured"]
        assert "circular" in leg["why"]
        assert "selected@k" in leg["why"]

    def test_the_briefing_leg_is_refused_for_want_of_a_label(self) -> None:
        leg = track_record.ground_truth_agreement(BRIEFING_JUDGE)
        assert not leg["measured"]
        assert "no deterministic label" in leg["why"]
        assert leg["requires"]

    def test_an_unknown_judge_is_refused(self) -> None:
        with pytest.raises(KeyError):
            track_record.ground_truth_agreement("investigation_planner")

    def test_it_reads_the_committed_archives_and_pairs_something(self) -> None:
        """Against the real tree: the leg has data and every row is accounted for."""
        leg = track_record.ground_truth_agreement(ACTION_VERIFIER)
        assert leg["measured"]
        value = leg["value"]
        assert value["paired"] >= 1
        assert value["scanned"] == value["paired"] + len(value["not_paired"])
        assert all(row["because"] for row in value["not_paired"])
        assert value["spent"].startswith("nothing")

    def test_the_false_approve_direction_is_null_and_says_so(self) -> None:
        """A null that is "not measured" and never "zero" (INC-003's distinction).

        No live run in the archives was ever supposed to end ``not_verified``, so
        the dangerous direction is uncovered here and only the trap set reaches it.
        """
        value = track_record.ground_truth_agreement(ACTION_VERIFIER)["value"]
        assert value["false_approve_rate"] is None
        assert value["refusals_called_for"] == 0
        assert "never run live" in value["false_approve_is_unmeasured_because"]

    def test_a_read_only_scenario_is_not_paired(self) -> None:
        """INC-003 in judge form: a label about the loop, applied to the judge.

        Seven of the thirty scanned rows are this shape. Asserted against the real
        corpus so the refusal cannot quietly stop firing.
        """
        value = track_record.ground_truth_agreement(ACTION_VERIFIER)["value"]
        reasons = [row["because"] for row in value["not_paired"]]
        assert any("no Tier-1 action" in reason for reason in reasons)

    def test_the_stabilize_only_handover_is_not_paired(self) -> None:
        value = track_record.ground_truth_agreement(ACTION_VERIFIER)["value"]
        reasons = [row["because"] for row in value["not_paired"]]
        assert any("stabilize-only" in reason for reason in reasons)

    def test_a_canned_runs_scripted_verdict_is_never_scanned(self) -> None:
        """A fixture is not a judge.

        Most archives carry a scripted ``_verify_judge`` entry; every scanned row's archive
        has a ``live_llm`` outcome.
        """
        for row in track_record.scan():
            report = json.loads(
                (artifacts.REPO_ROOT / "evals" / "runs" / row.archive / "report.json").read_text()
            )
            outcome = next(o for o in report["outcomes"] if o["scenario"] == row.scenario)
            assert outcome["live_llm"], f"{row.archive} {row.scenario}"

    def test_an_empty_tree_reports_not_measured(self, tmp_path: Path) -> None:
        leg = track_record.ground_truth_agreement(ACTION_VERIFIER, root=tmp_path)
        assert not leg["measured"]
        assert leg["requires"]


class TestNoJudgeNumberWithoutACalibrationReport:
    """The WP-6.3 acceptance (plan 04:169), one role out: both directions.

    Red before this packet: ``arm_summary`` printed ``judge_mean_overall`` as a
    float with nothing beside it, and there was no register to consult.
    """

    _ARM: Final[tuple[str, str, str]] = ("baseline", "benchmark", "live")

    def _rows(self, *, judge_overall: float | None = 0.85) -> list[Row]:
        return [
            Row(
                archive="aaaaaaaaaaaa",
                scenario="remediate_dlq_backlog_success",
                agent_model="claude-sonnet-4-6",
                strategy="baseline",
                model_role="benchmark",
                execution_mode="live",
                group={"family": "dlq", "difficulty": "medium"},
                passed=True,
                dimensions=(("outcome", True, "terminal state resolved matched expectation"),),
                root_cause_graded=True,
                root_cause_correct=True,
                tool_calls=4,
                max_tool_calls=13,
                tokens=18_000,
                usd=Decimal("0.13"),
                wall_seconds=41.2,
                judge_overall=judge_overall,
            )
        ]

    def test_the_register_is_empty_today(self) -> None:
        """The correct state: the sweep that fills it is a paid run, deferred."""
        assert dict(JUDGE_CALIBRATION_REPORTS) == {}

    def test_the_number_is_withheld_without_an_id(self) -> None:
        summary = arm_summary(self._ARM, self._rows())
        assert summary["judge_mean_overall"] == WITHHELD_JUDGE
        assert summary["judge_calibration_report_id"] is None
        assert summary["judge"] == LEADERBOARD_JUDGE
        assert "no judge number is reported before" in summary["judge_gate"]

    def test_the_number_is_printed_once_the_judge_is_calibrated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "evals.research_report.JUDGE_CALIBRATION_REPORTS",
            {LEADERBOARD_JUDGE: "0123456789ab"},
        )
        summary = arm_summary(self._ARM, self._rows())
        assert summary["judge_mean_overall"] == pytest.approx(0.85)
        assert summary["judge_calibration_report_id"] == "0123456789ab"

    def test_the_coverage_count_is_never_withheld(self) -> None:
        """How many runs were judged is a fact about the arm, not about the judge.

        Withholding it too would hide the denominator, and a reader could no
        longer tell "not calibrated" from "never judged".
        """
        summary = arm_summary(self._ARM, self._rows())
        assert summary["judged_runs"] == 1

    def test_an_arm_with_no_judged_run_reports_none_not_withheld(self) -> None:
        """Nothing to withhold. ``None`` here is "no run carried a score"."""
        summary = arm_summary(self._ARM, self._rows(judge_overall=None))
        assert summary["judge_mean_overall"] is None
        assert summary["judged_runs"] == 0

    def test_a_withheld_number_is_a_sentence_not_a_zero(self) -> None:
        """INC-003's distinction, one level up: not-reported is not zero."""
        assert isinstance(WITHHELD_JUDGE, str)
        assert "withheld" in WITHHELD_JUDGE
        assert LEADERBOARD_JUDGE in WITHHELD_JUDGE


class TestTheRoleWordIsAlwaysPrefixed:
    """Plan 02:25, scoped to the judge-role surfaces.

    Three shapes, argued in ``roles.py``: prefixed ``action_``, qualified, or quoted.
    """

    _SURFACES: Final[tuple[str, ...]] = (
        "evals/judge_calibration/__init__.py",
        "evals/judge_calibration/__main__.py",
        "evals/judge_calibration/fakes.py",
        "evals/judge_calibration/harness.py",
        "evals/judge_calibration/roles.py",
        "evals/judge_calibration/track_record.py",
        "evals/judge_calibration/traps.py",
        "evals/graders/llm_judge.py",
        "src/incident_commander/agent/remediation.py",
        "src/incident_commander/agent/selection.py",
        "src/incident_commander/llm/prompts/verification_judge.md",
        "src/incident_commander/llm/prompts/briefing_judge.md",
        "src/incident_commander/llm/prompts/candidate_selector.md",
    )
    #: The three other things the word correctly names here, declared. Adding to
    #: this list is the reviewable act; a fourth entry should have to be argued for.
    _OTHER_VERIFIERS: Final[tuple[str, ...]] = ("signature ", "redaction ", "pool ")
    _QUOTES: Final[str] = "`\"'*"
    _WORD: Final[re.Pattern[str]] = re.compile(r"(.{0,24})verifier", re.IGNORECASE)

    def _offences(self, text: str) -> Iterator[str]:
        for match in self._WORD.finditer(text):
            before = match.group(1).lower()
            if before.endswith("action_"):
                continue
            if any(before.endswith(word) for word in self._OTHER_VERIFIERS):
                continue
            if before and before[-1] in self._QUOTES:
                continue
            yield match.group(0)

    @pytest.mark.parametrize("relative", _SURFACES)
    def test_a_judge_surface_carries_no_bare_word(self, relative: str) -> None:
        path = artifacts.REPO_ROOT / relative
        assert path.is_file(), relative
        offences = list(self._offences(path.read_text()))
        assert not offences, f"{relative}: {offences}"

    def test_a_rendered_report_carries_no_bare_word(self) -> None:
        """The artifact a reader actually opens, not only the source."""
        for judge in CALIBRATED_ROLES:
            report = calibrate(judge, client=_agreeing(judge), model=_MODEL, now=_AT)
            assert not list(self._offences(report.to_json()))

    def test_the_sweep_would_catch_one(self) -> None:
        """The test's own fence: a rule that cannot fail proves nothing."""
        assert list(self._offences("the verifier said the fix worked"))
        assert not list(self._offences("the action_verifier said the fix worked"))
        assert not list(self._offences("the signature verifier rejected the body"))
        assert not list(self._offences('the bare word "verifier" is ambiguous'))


class TestTheRoleNamesAreTheNormativeOnes:
    """Plan 02 § 3's vocabulary, landed in code as the packet asks."""

    def test_action_verifier_is_the_existing_verification_judge(self) -> None:
        assert ACTION_VERIFIER == "action_verifier"
        assert (
            roles.role(ACTION_VERIFIER).prompt == VERIFICATION_JUDGE_PROMPT == "verification_judge"
        )

    def test_the_three_calibrated_roles_are_the_ones_that_exist(self) -> None:
        assert CALIBRATED_ROLES == (ACTION_VERIFIER, BRIEFING_JUDGE, CANDIDATE_SELECTOR)
        for judge in CALIBRATED_ROLES:
            assert load_prompt(roles.role(judge).prompt)

    def test_approval_is_one_rule_for_all_three(self) -> None:
        assert is_approval(ACTION_VERIFIER, "verified")
        assert not is_approval(ACTION_VERIFIER, "not_verified")
        assert is_approval(BRIEFING_JUDGE, briefing_verdict(grounded=True, actionable=True))
        assert not is_approval(BRIEFING_JUDGE, briefing_verdict(grounded=True, actionable=False))
        assert is_approval(CANDIDATE_SELECTOR, f"{SELECT_PREFIX}c1")
        assert not is_approval(CANDIDATE_SELECTOR, "probe_more")
        assert not is_approval(CANDIDATE_SELECTOR, "escalate")

    def test_the_briefing_verdict_thresholds_at_the_graders_own_bar(self) -> None:
        """One threshold in the system, used here rather than a second one."""
        from evals.graders.llm_judge import USEFUL_THRESHOLD

        score = JudgeScore(
            groundedness=USEFUL_THRESHOLD, actionability=USEFUL_THRESHOLD - 0.01, reasoning="x"
        )
        subject = _briefing_subject("bj-01-grounded-and-actionable")
        client = FakeJudgeClient({subject.context(): [score.model_dump()]})
        assert subject.ask(client=client, model=_MODEL) == briefing_verdict(
            grounded=True, actionable=False
        )

    def test_the_briefing_context_is_the_judges_own(self) -> None:
        """The subject renders through the grader's function, not a copy."""
        case = next(
            case
            for case in traps_for(BRIEFING_JUDGE)
            if case.case_id == "bj-05-inc-002-honest-after-a-filtered-read"
        )
        subject = case.subject
        assert isinstance(subject, roles.BriefingSubject)
        assert case.context() == format_briefing_context(subject.briefing)
        # INC-002's own property: the filtered read's arguments are beside its
        # result, so the judge can see what scoped it.
        assert "remediation_hint" in case.context()


class TestThePaidLegIsGuarded:
    """PROTOCOL step 0: readiness is not authorization."""

    def test_live_without_yes_spend_refuses(self, capsys: pytest.CaptureFixture[str]) -> None:
        from evals.judge_calibration.__main__ import main

        assert main(["--live"]) == 2
        assert "refusing" in capsys.readouterr().err

    def test_the_default_path_asks_no_real_model(self, capsys: pytest.CaptureFixture[str]) -> None:
        """No flags = the scripted fake, and it says so on every report."""
        from evals.judge_calibration.__main__ import main

        assert main(["--judge", ACTION_VERIFIER]) == 0
        out = capsys.readouterr().out
        assert "client fake" in out
        assert "SCRIPTED FAKE JUDGE" in out

    def test_scan_asks_nothing_and_names_the_absent_role(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from evals.judge_calibration.__main__ import main

        assert main(["--scan"]) == 0
        out = capsys.readouterr().out
        assert PLAN_APPROVAL_JUDGE in out
        assert "divergence B6" in out


class TestTheConventionIsWrittenDown:
    """Plan 03 § 110's rule is a review convention, so it has to be readable.

    A test cannot refuse a two-line rubric edit — that is a judgement about a
    commit, made by a reviewer. What a test CAN do is fail when the convention
    stops being written down anywhere, which is how a review rule quietly lapses.
    """

    def test_eval_methodology_carries_the_one_line_at_a_time_rule(self) -> None:
        doc = (artifacts.REPO_ROOT / "docs" / "eval-methodology.md").read_text()
        assert "Judge calibration" in doc
        assert "one line at a time" in doc

    def test_the_adr_exists_and_is_indexed(self) -> None:
        adr = artifacts.REPO_ROOT / "docs" / "ADR"
        matches = sorted(adr.glob("0052-*.md"))
        assert len(matches) == 1
        assert "accepted" in matches[0].read_text().lower()
        assert (
            matches[0].name.removesuffix(".md").split("-", 1)[1] in (adr / "README.md").read_text()
        )
