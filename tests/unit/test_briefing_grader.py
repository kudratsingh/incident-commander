"""BRIEFING: two briefing faults the owner ruled are lookups, not judgments (O-61, ADR 0083).

Red-before-green on the owner's own labelled briefings (committed archives, read-only): each
check fires exactly where an owner's reason names its pattern and on no briefing the owner
called useful. Then the rules one at a time, and the per-tool field table against the snapshot.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Final

import pytest

from evals.graders.briefing_grader import (
    COPY_WORD_THRESHOLD,
    READING_FRESHNESS,
    cached_reading_claims,
    copied_steps,
    run_actions,
)
from evals.graders.deterministic import (
    NO_BRIEFING_PROSE,
    GradeDimension,
    ScenarioExpectation,
    grade,
    is_vacuous_detail,
)
from evals.runner import RunReport, Trajectory, briefing_lookups
from incident_commander.agent.briefing import EscalationBriefing, ProbeSummary
from incident_commander.agent.state import EvidenceEntry, IncidentState, RunState

_REPO: Final[Path] = Path(__file__).resolve().parents[2]
_RUNS: Final[Path] = _REPO / "evals" / "runs"
_LABELS: Final[Path] = _REPO / "evals" / "judge_calibration" / "labels" / "briefings.jsonl"
_SNAPSHOT: Final[Path] = _REPO / "contracts" / "platform-tools.snapshot.json"

#: Packet-2 #15: "Calls a cached, untimestamped lag reading 'confirmed'".
_CACHED_FIRES: Final[frozenset[str]] = frozenset({"0db6fe722f7c:multi_probe_billing"})
#: Packet-1 #6, #9, #16 and packet-2 #10 (#13 in packet 2 is #9 shown again).
_COPIED_FIRES: Final[frozenset[str]] = frozenset(
    {
        "0db6fe722f7c:deploy_correlation",
        "cde5a14485c3:incidents_overview",
        "6e2def805db4:saga_stuck",
        "cde5a14485c3:deploy_correlation",
    }
)
_SMTP_ROW: Final = "97d91272-9774-5b8e-980b-f0d2fa6ed619"


def _labels() -> dict[str, str]:
    rows = [json.loads(line) for line in _LABELS.read_text().splitlines() if line.strip()]
    return {row["id"]: row["label"] for row in rows}


def _archived(briefing_id: str) -> tuple[EscalationBriefing, RunState]:
    archive, scenario = briefing_id.split(":")
    briefing = EscalationBriefing.model_validate_json(
        (_RUNS / archive / "briefings" / f"{scenario}.json").read_text()
    )
    trajectory = Trajectory.model_validate_json(
        (_RUNS / archive / "trajectories" / f"{scenario}.json").read_text()
    )
    return briefing, trajectory.checkpoints[-1]


def _copies(briefing: EscalationBriefing, run: RunState) -> list[Any]:
    return copied_steps(briefing, alert=run.alert, actions=run_actions(run.evidence, briefing))


class TestTheOwnersLabelledBriefings:
    """Every briefing the owner labelled, both packets, graded from its committed archive."""

    def test_the_label_file_holds_what_this_test_names(self) -> None:
        labels = _labels()
        assert len(labels) == 29, "29 distinct briefings over packets 1 and 2 (3 repeats)"
        for briefing_id in _CACHED_FIRES | _COPIED_FIRES:
            assert labels[briefing_id] == "not_useful", briefing_id

    @pytest.mark.parametrize("briefing_id", sorted(_labels()))
    def test_the_cached_check_fires_only_where_the_owner_said(self, briefing_id: str) -> None:
        briefing, _ = _archived(briefing_id)
        fired = bool(cached_reading_claims(briefing))
        assert fired is (briefing_id in _CACHED_FIRES), briefing_id

    @pytest.mark.parametrize("briefing_id", sorted(_labels()))
    def test_the_copied_check_fires_only_where_the_owner_said(self, briefing_id: str) -> None:
        briefing, run = _archived(briefing_id)
        fired = bool(_copies(briefing, run))
        assert fired is (briefing_id in _COPIED_FIRES), briefing_id

    @pytest.mark.parametrize(
        "briefing_id", sorted(key for key, label in _labels().items() if label == "useful")
    )
    def test_no_briefing_the_owner_called_useful_fails(self, briefing_id: str) -> None:
        briefing, run = _archived(briefing_id)
        report = grade(run, _expectation(run), briefing=briefing)
        assert _briefing_dimension(report).passed, briefing_id

    def test_the_cached_claim_names_the_reading_and_the_sentence(self) -> None:
        briefing, run = _archived("0db6fe722f7c:multi_probe_billing")
        detail = _briefing_dimension(grade(run, _expectation(run), briefing=briefing)).detail
        assert "get_consumer_lag(billing-consumer) -> 15000" in detail
        assert "source='static', measured_at=None" in detail
        assert '"billing-consumer lag is confirmed at 15,000"' in detail

    def test_the_useful_twin_says_confirmed_and_discloses_the_cache(self) -> None:
        """Packet-1 #13 reads the same kind of reading and says "confirmed" too.

        It passes for one reason, stated here so it cannot be loosened silently: the same
        clause tells the reader the number is cached and has no timestamp.
        """
        briefing, _ = _archived("0db6fe722f7c:multi_probe_hypothesis_evolution")
        assert "confirmed lag of 30,000 messages (cached reading, timestamp unknown)" in (
            briefing.findings
        )
        assert cached_reading_claims(briefing) == []

    def test_the_copied_step_names_the_row_and_the_step(self) -> None:
        briefing, run = _archived("0db6fe722f7c:deploy_correlation")
        (copy,) = _copies(briefing, run)
        assert copy.row.row_id == _SMTP_ROW
        assert copy.shared == ("egress", "smtp.mailer.internal", "vpc")
        assert copy.step.startswith("Start by checking whether v0.4.3")

    def test_a_remaining_row_named_with_its_own_hint_is_not_a_copy(self) -> None:
        """Packet-1 #8 (useful): the SMTP row's own words, after "the three remaining"."""
        briefing, run = _archived("aeadd5ef3edd:remediate_runaway_saga_success")
        assert "remaining DLQ items" in briefing.recommendation
        assert "VPC egress" in briefing.recommendation
        assert _copies(briefing, run) == []

    def test_a_row_the_run_acted_on_is_the_subject(self) -> None:
        """Packet-1 #12 (useful): the SMTP row's fix, for a row the run replayed by id."""
        briefing, run = _archived("9949c45145d4:dlq_wait_and_replay_success")
        assert "smtp.mailer.internal ECS task health" in briefing.recommendation
        assert _copies(briefing, run) == []


def _expectation(run: RunState) -> ScenarioExpectation:
    return ScenarioExpectation(name="labelled", expected_terminal_state=run.state)


def _briefing_dimension(report: Any) -> Any:
    return next(d for d in report.dimensions if d.dimension is GradeDimension.BRIEFING)


# --- the rules, one at a time ------------------------------------------------------------------


def _lag(lag: int, *, source: str = "static", measured_at: str | None = None) -> ProbeSummary:
    return ProbeSummary(
        tool="get_consumer_lag",
        summary=json.dumps(
            {
                "consumer_group": "billing-consumer",
                "lag": lag,
                "lag_known": True,
                "source": source,
                "measured_at": measured_at,
            }
        ),
        arguments={"consumer_group": "billing-consumer"},
    )


def _briefing(
    *trail: ProbeSummary, findings: str = "", recommendation: str = ""
) -> EscalationBriefing:
    return EscalationBriefing(
        incident_id="i",
        final_state=IncidentState.ESCALATED,
        alert_summary="a",
        investigation_trail=trail,
        findings=findings,
        recommendation=recommendation,
    )


class TestCachedReadingCalledConfirmed:
    @pytest.mark.parametrize(
        "sentence",
        [
            "billing-consumer lag is confirmed at 15,000.",
            "The lag is currently 15000 messages.",
            "A live reading shows lag at 15k.",
            "billing-consumer was verified at 15,000 lag.",
        ],
    )
    def test_a_freshness_word_on_a_static_value_fires(self, sentence: str) -> None:
        (claim,) = cached_reading_claims(_briefing(_lag(15000), findings=sentence))
        assert claim.reading.value == 15000
        assert claim.clause == sentence.strip()

    def test_a_missing_timestamp_is_cached_even_when_the_source_says_live(self) -> None:
        reading = _lag(15000, source="live")
        assert cached_reading_claims(_briefing(reading, findings="lag confirmed at 15,000"))

    def test_a_live_timestamped_reading_never_fires(self) -> None:
        reading = _lag(15000, source="live", measured_at="2026-10-11T10:00:00Z")
        assert not cached_reading_claims(_briefing(reading, findings="lag confirmed at 15,000"))

    @pytest.mark.parametrize(
        "sentence",
        [
            "lag confirmed at 15,000 (cached, no timestamp)",
            "lag is 15,000 from a static reading, confirmed only as recorded",
            "lag of 15,000 was never confirmed",
            "the lag is 15,000; whether it is still growing was not confirmed",
        ],
    )
    def test_a_disclosure_a_negation_or_another_clause_does_not_fire(self, sentence: str) -> None:
        assert cached_reading_claims(_briefing(_lag(15000), findings=sentence)) == []

    def test_the_rule_reads_clauses_not_grammar(self) -> None:
        """A known over-reach, pinned so it is never mistaken for a feature: the value, what
        was read and the word in one clause fire, whatever the word was attached to."""
        briefing = _briefing(_lag(15000), findings="lag was 15,000 and the restart was confirmed")
        assert cached_reading_claims(briefing)

    def test_the_value_must_be_the_readings_own_number(self) -> None:
        briefing = _briefing(_lag(15000), findings="lag confirmed at 150,000 and 1500")
        assert cached_reading_claims(briefing) == []

    def test_the_recommendation_is_read_too(self) -> None:
        briefing = _briefing(_lag(30000), recommendation="Drain the confirmed 30k lag first.")
        assert cached_reading_claims(briefing)

    def test_the_escalation_reason_is_not_the_writers_text(self) -> None:
        briefing = _briefing(_lag(15000)).model_copy(
            update={"escalation_reason": "lag is 15,000 (confirmed saturation)"}
        )
        assert cached_reading_claims(briefing) == []


def test_the_field_table_is_every_tool_whose_reading_can_be_stale() -> None:
    """From the snapshot: a top-level ``measured_at`` that may be null or absent, or a top-level
    ``source`` whose description names a ``live`` value. A required ``measured_at`` is always a
    timestamp, so those readings are never cached by this rule's definition."""
    can_be_stale = set()
    for tool in json.loads(_SNAPSHOT.read_text())["tools"]:
        schema = tool["outputSchema"]
        properties, required = schema.get("properties", {}), set(schema.get("required", []))
        stamp = properties.get("measured_at")
        source = properties.get("source", {})
        if (stamp is not None and "measured_at" not in required) or (
            "`live`" in source.get("description", "")
        ):
            can_be_stale.add(tool["name"])
    assert set(READING_FRESHNESS) == can_be_stale
    for name, fields in READING_FRESHNESS.items():
        tool = next(t for t in json.loads(_SNAPSHOT.read_text())["tools"] if t["name"] == name)
        properties = tool["outputSchema"]["properties"]
        for field in (fields.value, fields.subject, fields.source, fields.timestamp):
            assert field in properties, f"{name} has no output field {field!r}"


# --- copied from a non-subject dead-letter row -------------------------------------------------

_FIX: Final = (
    "Check smtp.mailer.internal ECS task health; recent billing hotfix (v0.4.2) may have "
    "changed VPC egress rules. Re-run once the dependency answers."
)


def _dlq(*, hint: str = "wait_and_replay", row_id: str = _SMTP_ROW) -> ProbeSummary:
    item = {
        "id": row_id,
        "error_message": "send_email failed: ConnectionRefusedError",
        "remediation_hint": hint,
        "triage": {"summary": "SMTP relay unreachable.", "suggested_fix": _FIX},
    }
    return ProbeSummary(
        tool="list_dlq_messages", summary=json.dumps({"total": 1, "items": [item]}), arguments={}
    )


_ALERT: Final[dict[str, object]] = {"source": "platform.api", "fingerprint": "error_spike"}
_STEP: Final = "Start by looking at whether VPC egress broke smtp.mailer.internal."


class TestCopiedFromANonSubjectRow:
    def test_a_step_carrying_the_rows_own_words_is_a_copy(self) -> None:
        (copy,) = copied_steps(_briefing(_dlq(), recommendation=_STEP), alert=_ALERT, actions=[])
        assert copy.shared == ("egress", "smtp.mailer.internal", "vpc")
        assert len(copy.shared) == COPY_WORD_THRESHOLD

    def test_two_shared_words_are_not_a_copy(self) -> None:
        step = "Look at task health for the payments service."
        assert copied_steps(_briefing(_dlq(), recommendation=step), alert=_ALERT, actions=[]) == []

    def test_a_word_the_incidents_own_readings_carry_is_not_the_rows(self) -> None:
        deploy = ProbeSummary(
            tool="get_deploy_history", summary='{"notes": "VPC egress rules changed"}'
        )
        briefing = _briefing(_dlq(), deploy, recommendation=_STEP)
        assert copied_steps(briefing, alert=_ALERT, actions=[]) == []

    @pytest.mark.parametrize(
        "alert",
        [
            {"source": "platform.api", "job_id": _SMTP_ROW},
            {"source": "platform.dlq", "remediation_hint": "wait_and_replay"},
            {"source": "platform.dlq"},
        ],
    )
    def test_a_row_the_alert_names_is_the_subject(self, alert: dict[str, object]) -> None:
        briefing = _briefing(_dlq(), recommendation=_STEP)
        assert copied_steps(briefing, alert=alert, actions=[]) == []

    def test_a_dlq_page_for_another_slice_does_not_make_the_row_its_subject(self) -> None:
        alert = {"source": "platform.dlq", "remediation_hint": "replay_safe"}
        assert copied_steps(_briefing(_dlq(), recommendation=_STEP), alert=alert, actions=[])

    @pytest.mark.parametrize(
        "action",
        [
            ("replay_dlq_by_ids", {"job_ids": [_SMTP_ROW]}),
            ("mark_dlq_permanent", {"job_id": _SMTP_ROW}),
            ("replay_dlq_by_category", {"category": "wait_and_replay"}),
        ],
    )
    def test_a_row_the_run_acted_on_is_the_subject(
        self, action: tuple[str, dict[str, object]]
    ) -> None:
        briefing = _briefing(_dlq(), recommendation=_STEP)
        assert copied_steps(briefing, alert=_ALERT, actions=[action]) == []

    def test_a_category_replay_of_another_slice_does_not_act_on_the_row(self) -> None:
        action = ("replay_dlq_by_category", {"category": "replay_safe"})
        assert copied_steps(_briefing(_dlq(), recommendation=_STEP), alert=_ALERT, actions=[action])

    @pytest.mark.parametrize(
        "step",
        [
            f"For {_SMTP_ROW[:8]}, check VPC egress rules on smtp.mailer.internal before replay.",
            "The remaining SMTP row needs its VPC egress rules for smtp.mailer.internal checked.",
            "Unrelated to this page: VPC egress rules for smtp.mailer.internal may have changed.",
        ],
    )
    def test_a_row_named_as_remaining_is_not_a_copy(self, step: str) -> None:
        assert copied_steps(_briefing(_dlq(), recommendation=step), alert=_ALERT, actions=[]) == []

    def test_findings_are_not_recommendation_steps(self) -> None:
        briefing = _briefing(_dlq(), findings=_STEP)
        assert copied_steps(briefing, alert=_ALERT, actions=[]) == []

    def test_each_step_is_read_on_its_own(self) -> None:
        recommendation = f"Roll back v0.4.3 first; {_STEP[0].lower()}{_STEP[1:]}"
        (copy,) = copied_steps(
            _briefing(_dlq(), recommendation=recommendation), alert=_ALERT, actions=[]
        )
        assert copy.step.startswith("start by looking")

    def test_run_actions_reads_markers_and_the_attempted_action(self, now: datetime) -> None:
        marker = EvidenceEntry(
            tool_name="_remediation_escalate",
            arguments={
                "attempted_tool": "replay_dlq_by_category",
                "attempted_arguments": {"category": "replay_safe"},
            },
            result_summary="escalated",
            timestamp=now,
        )
        read = EvidenceEntry(
            tool_name="get_consumer_lag", arguments={}, result_summary="{}", timestamp=now
        )
        actions = run_actions((marker, read), _briefing())
        assert actions == [("replay_dlq_by_category", {"category": "replay_safe"})]


# --- the dimension --------------------------------------------------------------------------


class TestTheDimension:
    def test_no_prose_is_vacuous(self, run_state: RunState) -> None:
        report = grade(
            run_state,
            ScenarioExpectation(name="s", expected_terminal_state=run_state.state),
            briefing=_briefing(_lag(15000)),
        )
        dimension = _briefing_dimension(report)
        assert dimension.passed and dimension.detail == NO_BRIEFING_PROSE
        assert is_vacuous_detail(dimension.detail)

    def test_no_briefing_is_vacuous(self, run_state: RunState) -> None:
        report = grade(
            run_state, ScenarioExpectation(name="s", expected_terminal_state=run_state.state)
        )
        assert _briefing_dimension(report).detail == NO_BRIEFING_PROSE

    def test_a_fault_fails_the_run(self, run_state: RunState) -> None:
        briefing = _briefing(_lag(15000), findings="lag confirmed at 15,000")
        report = grade(
            run_state,
            ScenarioExpectation(name="s", expected_terminal_state=run_state.state),
            briefing=briefing,
        )
        assert not _briefing_dimension(report).passed
        assert report.passed is False

    def test_the_run_summary_counts_written_briefings(self) -> None:
        report = RunReport.model_validate_json((_RUNS / "0db6fe722f7c" / "report.json").read_text())
        assert briefing_lookups(report) is None, "an archive graded before ADR 0083"
