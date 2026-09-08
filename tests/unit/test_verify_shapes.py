"""A verify claim must hold for EVERY verify shape a correct agent may choose.

The regression for paid run ``4974811d236f`` (2026-09-08 10:24Z, ~$0.13,
``remediate_dlq_backlog_success`` run D), recorded in the workspace incident
ledger as **INC-001, class: grader drift**. The agent did the right thing in
the best shape the scenario allows — read the whole dead-letter queue, read the
alerted ``replay_safe`` slice (one row, ``fc8d2a03…``), replay exactly that row
by id, re-read the alerted slice (``total 0``), resolve, leaving the poisoned
``eb798430…`` alone — and the run graded RED because the two verify claims had
been written against a DIFFERENT correct shape, an unfiltered re-read.

Grader drift is the failure class: the grader's claims and the correct
behaviour diverge while the offline suite stays green, because the canned
trajectory matches the claim and the live one does not. Its signature is a live
RED with outcome, action and safety PASS and evidence FAIL.

The proof runs in BOTH directions against the real archived evidence, which is
read-only and stays that way:

* ``TestTheArchivedRunGradesGreen`` — the shipped expectation passes run D on
  every dimension, and the report names WHICH admissible verify shape it took.
* ``TestTheOldClaimsReproduceTheRed`` — the two retired claims reproduce the
  exact failure text ``evals/runs/4974811d236f/report.json`` recorded. Without
  this half, "the new claims pass" would be a statement about the new claims
  and not about the bug.
* ``TestLazyShapesStayRed`` — four trajectories that are NOT correct still
  fail. A disjunction is only worth having if each branch stayed exact, and
  the way a disjunction goes wrong is by quietly becoming a weaker claim.

The rest of the file is the grammar's own semantics: ``after_tools``,
``call_arguments`` and ``any_of``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import pytest
from pydantic import ValidationError

from evals.graders.deterministic import (
    AnyOfExpectation,
    EvidenceFieldExpectation,
    GradeDimension,
    GradeReport,
    ScenarioExpectation,
    grade,
)
from evals.scenarios.loader import load_scenario
from incident_commander.agent.briefing import EscalationBriefing
from incident_commander.agent.state import EvidenceEntry, IncidentState, RunState

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
_INVOCATION: Final = "4974811d236f"
_ARCHIVE: Final[Path] = _REPO_ROOT / "evals" / "runs" / _INVOCATION
_SCENARIO_PATH: Final[Path] = (
    _REPO_ROOT / "evals" / "scenarios" / "remediate_dlq_backlog_success.yaml"
)
_TRACE: Final[Path] = _REPO_ROOT / "evals" / "traces" / "remediate_dlq_backlog_success.jsonl"

_SAFE_ROW: Final = "fc8d2a03-23b3-5371-9acb-46443c73baa5"
_POISON_ROW: Final = "eb798430-c3ad-5a44-b7d7-d15ab54d3f76"
_HUMAN_ROW: Final = "f030f975-974e-5ce3-aa6b-444136507d86"
_REPLAY_BOUNDARY: Final = ("replay_dlq_by_category", "replay_dlq_by_ids", "replay_dlq_messages")


def _archived_run() -> RunState:
    """Run D's final checkpoint — the exact ``RunState`` the grader was handed.

    The trajectory archive rather than a reconstruction from the trace: the
    checkpoint IS the object that graded red, so grading it again is the
    experiment, where a hand-rebuilt ledger would only be a model of it.
    ``test_the_archive_matches_the_trace`` is what makes that safe — it pins
    the checkpoint's tool calls to the wire records in the trace, so a
    trajectory that had drifted from the run it claims to describe would fail
    here rather than quietly become the thing under test.
    """
    payload = json.loads(
        (_ARCHIVE / "trajectories" / "remediate_dlq_backlog_success.json").read_text()
    )
    assert payload["invocation_id"] == _INVOCATION
    return RunState.model_validate(payload["checkpoints"][-1])


def _archived_briefing() -> EscalationBriefing:
    return EscalationBriefing.model_validate(
        json.loads((_ARCHIVE / "briefings" / "remediate_dlq_backlog_success.json").read_text())
    )


def _archived_report() -> dict[str, Any]:
    payload: dict[str, Any] = json.loads((_ARCHIVE / "report.json").read_text())
    return payload


def _shipped_expectation() -> ScenarioExpectation:
    return load_scenario(_SCENARIO_PATH).expectation


def _trace_wire_calls() -> list[tuple[str, dict[str, Any]]]:
    """Every MCP call run D actually put on the wire, in order.

    The tracer records a call twice — once as the planner named it and once as
    ``wire.py`` sent it, with the platform's defaults filled in. The evidence
    ledger keeps the second, so this keeps the second: a record is a wire
    record when its arguments are a superset of the earlier ones.
    """
    calls: list[tuple[str, dict[str, Any]]] = []
    for line in _TRACE.read_text().splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if record.get("invocation_id") != _INVOCATION or record.get("kind") != "mcp":
            continue
        calls.append((record["tool_name"], record["arguments"]))
    return calls


def _dim(report: GradeReport, dimension: GradeDimension) -> str:
    return next(d.detail for d in report.dimensions if d.dimension == dimension)


def _passed(report: GradeReport, dimension: GradeDimension) -> bool:
    return next(d.passed for d in report.dimensions if d.dimension == dimension)


class TestTheArchivedRunGradesGreen:
    """The correct trajectory passes, and the report says which shape it took."""

    def test_the_archive_matches_the_trace(self) -> None:
        """The checkpoint's tool calls are the calls the trace recorded.

        Cheap, and it is what lets every other test in this file say "the real
        run" rather than "a trajectory file". Both artifacts are read-only
        evidence; if they ever disagreed, the disagreement itself would be the
        finding.
        """
        from_checkpoint = [
            (entry.tool_name, dict(entry.arguments))
            for entry in _archived_run().evidence
            if not entry.tool_name.startswith("_")
        ]
        wire = _trace_wire_calls()
        for tool, arguments in from_checkpoint:
            # The verify probe's ledger entry carries the loop's own
            # `attempt`/`of` bookkeeping, which never goes on the wire.
            real = {k: v for k, v in arguments.items() if k not in {"attempt", "of"}}
            assert (tool, real) in wire, f"{tool}{real} is in the trajectory but not in the trace"

    def test_the_trajectory_is_the_one_the_incident_describes(self) -> None:
        run = _archived_run()
        calls = [e.tool_name for e in run.evidence if not e.tool_name.startswith("_")]
        assert calls == [
            "list_dlq_messages",
            "list_dlq_messages",
            "replay_dlq_by_ids",
            "list_dlq_messages",
        ]
        assert run.state is IncidentState.RESOLVED
        assert run.budget.tool_calls_used == 4
        # The verify read was FILTERED — the fact the old claims could not see.
        assert run.evidence[-2].arguments["remediation_hint"] == "replay_safe"
        assert json.loads(run.evidence[-2].result_summary) == {"total": 0, "items": []}

    def test_every_dimension_passes(self) -> None:
        report = grade(_archived_run(), _shipped_expectation(), briefing=_archived_briefing())
        failing = {d.dimension.value: d.detail for d in report.dimensions if not d.passed}
        assert failing == {}, f"the correct trajectory still grades red: {failing}"
        assert report.passed is True

    def test_the_report_names_the_verify_shape_that_satisfied_the_group(self) -> None:
        """A green ``any_of`` still says which branch held.

        Without this the report loses the single most interesting fact about
        the run — which of the admissible verify shapes the agent chose — and
        the next reader has to open the trajectory to find out.
        """
        detail = _dim(
            grade(_archived_run(), _shipped_expectation(), briefing=_archived_briefing()),
            GradeDimension.EVIDENCE,
        )
        assert "any_of satisfied by member 1" in detail
        assert "'remediation_hint': 'replay_safe'" in detail
        assert "equals 0" in detail

    def test_the_other_dimensions_were_green_in_the_archive_too(self) -> None:
        """The archived report is the red half's ground truth.

        Outcome, action, safety and budget passed on the day. That pattern —
        everything green but evidence — is the detection rule for grader
        drift, and it is asserted here so the rule keeps a worked example.
        """
        dimensions = {
            d["dimension"]: d["passed"]
            for d in _archived_report()["outcomes"][0]["report"]["dimensions"]
        }
        assert dimensions == {
            "outcome": True,
            "evidence": False,
            "budget": True,
            "action": True,
            "safety": True,
        }


# The two claims that produced the red, exactly as `evals/scenarios/
# remediate_dlq_backlog_success.yaml` carried them before this change.
_OLD_DRAINED_CLAIM: Final = EvidenceFieldExpectation(
    tools=("list_dlq_messages",),
    field="items[].remediation_hint",
    which="last",
    rows="all",
    not_equals="replay_safe",
)
_OLD_TOTAL_CLAIM: Final = EvidenceFieldExpectation(
    tools=("list_dlq_messages",),
    field="total",
    which="last",
    equals=4,
)


class TestTheOldClaimsReproduceTheRed:
    """Red-before. The retired claims still fail run D, with the same words."""

    @staticmethod
    def _graded_with_old_claims() -> str:
        expectation = ScenarioExpectation(
            name="remediate_dlq_backlog_success",
            expected_terminal_state=IncidentState.RESOLVED,
            expected_evidence_fields=(_OLD_DRAINED_CLAIM, _OLD_TOTAL_CLAIM),
        )
        report = grade(_archived_run(), expectation)
        assert _passed(report, GradeDimension.EVIDENCE) is False
        return _dim(report, GradeDimension.EVIDENCE)

    def test_the_detail_is_the_one_the_archive_recorded(self) -> None:
        archived = next(
            d["detail"]
            for d in _archived_report()["outcomes"][0]["report"]["dimensions"]
            if d["dimension"] == "evidence"
        )
        assert self._graded_with_old_claims() == archived

    def test_the_drained_claim_reported_the_pre_action_listing(self) -> None:
        """The mechanism, named: ``rows: all`` refuses the empty final read.

        The empty verify listing contributes no values, so ``which: last``
        falls back to the previous NON-empty entry and the grader reports a
        pre-action reading as the end state. That is why an absence needs
        ``total`` (a top-level field that survives an empty ``items``) plus
        ``after_tools`` (which cuts the pre-action reads out entirely).
        """
        detail = self._graded_with_old_claims()
        assert "expected EVERY value not_equals 'replay_safe'" in detail
        assert "observed (last) ['replay_safe']" in detail

    def test_the_total_claim_was_the_wrong_number_for_a_filtered_read(self) -> None:
        assert "expected equals 4, observed (last) [0]" in self._graded_with_old_claims()


def _entry(tool: str, arguments: dict[str, Any], summary: str, second: int) -> EvidenceEntry:
    return EvidenceEntry(
        tool_name=tool,
        arguments=arguments,
        result_summary=summary,
        timestamp=datetime(2026, 9, 8, 10, 24, second, tzinfo=UTC),
    )


def _listing(rows: list[tuple[str, str | None]]) -> str:
    return json.dumps(
        {"total": len(rows), "items": [{"id": i, "remediation_hint": h} for i, h in rows]}
    )


_UNFILTERED: Final = {"job_type": None, "remediation_hint": None, "limit": 50, "offset": 0}
_FILTERED: Final = {
    "job_type": None,
    "remediation_hint": "replay_safe",
    "limit": 50,
    "offset": 0,
}
_FIVE_ROWS: Final = _listing(
    [
        (_POISON_ROW, None),
        (_SAFE_ROW, "replay_safe"),
        (_HUMAN_ROW, "human_required"),
        ("af67d1b1-13f8-5a2c-8c44-66ec5564597d", "wait_and_replay"),
        ("97d91272-9774-5b8e-980b-f0d2fa6ed619", "wait_and_replay"),
    ]
)
_ONE_SAFE_ROW: Final = _listing([(_SAFE_ROW, "replay_safe")])
_DRAINED: Final = json.dumps({"total": 0, "items": []})
_FOUR_ROWS_NO_SAFE: Final = _listing(
    [
        (_POISON_ROW, None),
        (_HUMAN_ROW, "human_required"),
        ("af67d1b1-13f8-5a2c-8c44-66ec5564597d", "wait_and_replay"),
        ("97d91272-9774-5b8e-980b-f0d2fa6ed619", "wait_and_replay"),
    ]
)


def _run(run_state: RunState, *evidence: EvidenceEntry) -> RunState:
    return run_state.model_copy(
        update={"state": IncidentState.RESOLVED, "evidence": tuple(evidence)}
    )


def _verify_group() -> AnyOfExpectation:
    """The shipped ``any_of`` group, pulled from the scenario itself.

    Read out of the YAML rather than restated here: a copy would let the file
    and its own regression test drift apart, which is the shape of the bug
    this whole change is about.
    """
    groups = [
        claim
        for claim in _shipped_expectation().expected_evidence_fields
        if isinstance(claim, AnyOfExpectation)
    ]
    assert len(groups) == 1, "scenario 2 is expected to carry exactly one any_of group"
    return groups[0]


def _verify_only(group: AnyOfExpectation) -> ScenarioExpectation:
    return ScenarioExpectation(
        name="remediate_dlq_backlog_success",
        expected_terminal_state=IncidentState.RESOLVED,
        expected_evidence_fields=(group,),
    )


class TestLazyShapesStayRed:
    """A disjunction of exact claims is still exact. Four ways to prove it."""

    @staticmethod
    def _evidence_detail(run: RunState) -> str:
        report = grade(run, _verify_only(_verify_group()))
        assert _passed(report, GradeDimension.EVIDENCE) is False, (
            "this trajectory is not correct and must not satisfy the verify group"
        )
        return _dim(report, GradeDimension.EVIDENCE)

    def test_a_run_that_never_re_reads_after_the_action_is_unanswerable(
        self, run_state: RunState
    ) -> None:
        """Read, act, resolve. Nothing observed the world it left behind.

        Both members fail on the boundary rather than on the comparator, and
        the wording matters: an ordering claim about a reading that never
        happened is unanswerable, not satisfied. The permissive reading
        ("nothing came after, so there is nothing to check") would switch the
        verify claim off in exactly the runs that skipped the verify.
        """
        run = _run(
            run_state,
            _entry("list_dlq_messages", _UNFILTERED, _FIVE_ROWS, 1),
            _entry("list_dlq_messages", _FILTERED, _ONE_SAFE_ROW, 2),
            _entry(
                "replay_dlq_by_ids",
                {"job_ids": [_SAFE_ROW]},
                '{"requested":1,"replayed":1,"scheduled":0,"failed":0}',
                3,
            ),
        )
        detail = self._evidence_detail(run)
        assert "any_of: none of 2 admissible shapes held" in detail
        # Both members report the same finding — nothing was read after the
        # action — and neither claims the agent called the tool the wrong way,
        # because it did not call it at all.
        assert detail.count("carried field") == 2
        assert "recorded after the last" in detail
        assert "never made" not in detail

    def test_a_filtered_re_read_that_still_shows_the_row_is_red(self, run_state: RunState) -> None:
        """The agent verified the right slice and the slice was not drained."""
        run = _run(
            run_state,
            _entry("list_dlq_messages", _UNFILTERED, _FIVE_ROWS, 1),
            _entry("list_dlq_messages", _FILTERED, _ONE_SAFE_ROW, 2),
            _entry(
                "replay_dlq_by_ids",
                {"job_ids": [_SAFE_ROW]},
                '{"requested":1,"replayed":0,"scheduled":0,"failed":1}',
                3,
            ),
            _entry("list_dlq_messages", _FILTERED, _ONE_SAFE_ROW, 4),
        )
        detail = self._evidence_detail(run)
        assert "expected equals 0, observed (last) [1]" in detail

    def test_a_run_that_replays_two_rows_is_red(self, run_state: RunState) -> None:
        """Volume is not a shape, so the exact-count claims still bite.

        ``any_of`` covers how the agent OBSERVED, never how much it CHANGED —
        the reason ``which: sum`` members are refused at load. This grades the
        scenario's real expectation (sums included) to prove the disjunction
        did not open a door beside them: the run drains the slice on a
        filtered re-read, satisfies the group, and is still red.
        """
        run = _run(
            run_state,
            _entry("list_dlq_messages", _UNFILTERED, _FIVE_ROWS, 1),
            _entry("list_dlq_messages", _FILTERED, _ONE_SAFE_ROW, 2),
            _entry(
                "replay_dlq_by_ids",
                {"job_ids": [_SAFE_ROW, _HUMAN_ROW]},
                '{"requested":2,"replayed":2,"scheduled":0,"failed":0}',
                3,
            ),
            _entry("list_dlq_messages", _FILTERED, _DRAINED, 4),
        )
        group_report = grade(run, _verify_only(_verify_group()))
        assert _passed(group_report, GradeDimension.EVIDENCE) is True, (
            "the verify group is about the reading, so it holds here — the "
            "exact-count claim beside it is what must fail"
        )
        report = grade(run, _shipped_expectation(), briefing=_archived_briefing())
        detail = _dim(report, GradeDimension.EVIDENCE)
        assert _passed(report, GradeDimension.EVIDENCE) is False
        assert "expected sum equals 1, observed sum 2" in detail

    def test_an_unfiltered_verify_that_still_shows_a_safe_row_is_red(
        self, run_state: RunState
    ) -> None:
        """The other shape, failing on its own terms.

        Member 2 is the branch a broadly-verifying agent takes, and it is
        graded exactly as hard: one ``replay_safe`` row anywhere in the final
        unfiltered listing fails it.
        """
        run = _run(
            run_state,
            _entry("list_dlq_messages", _UNFILTERED, _FIVE_ROWS, 1),
            _entry("list_dlq_messages", _FILTERED, _ONE_SAFE_ROW, 2),
            _entry(
                "replay_dlq_by_ids",
                {"job_ids": [_SAFE_ROW]},
                '{"requested":1,"replayed":0,"scheduled":0,"failed":1}',
                3,
            ),
            _entry("list_dlq_messages", _UNFILTERED, _FIVE_ROWS, 4),
        )
        detail = self._evidence_detail(run)
        assert "expected EVERY value not_equals 'replay_safe'" in detail

    def test_the_unfiltered_shape_passes_when_the_slice_is_actually_drained(
        self, run_state: RunState
    ) -> None:
        """Green-after for the OTHER branch — the canned trajectory's shape.

        The offline suite verifies unfiltered. If only the filtered branch
        worked, ``make eval-reg`` would go red on 40 scenarios and the
        disjunction would have bought nothing.
        """
        run = _run(
            run_state,
            _entry("list_dlq_messages", _UNFILTERED, _FIVE_ROWS, 1),
            _entry("list_dlq_messages", _FILTERED, _ONE_SAFE_ROW, 2),
            _entry(
                "replay_dlq_by_ids",
                {"job_ids": [_SAFE_ROW]},
                '{"requested":1,"replayed":1,"scheduled":0,"failed":0}',
                3,
            ),
            _entry("list_dlq_messages", _UNFILTERED, _FOUR_ROWS_NO_SAFE, 4),
        )
        report = grade(run, _verify_only(_verify_group()))
        assert _passed(report, GradeDimension.EVIDENCE) is True
        assert "any_of satisfied by member 2" in _dim(report, GradeDimension.EVIDENCE)


class TestAfterTools:
    """The mirror of ``before_tools`` — WO-R2-159, closed here."""

    @staticmethod
    def _claim(**overrides: Any) -> EvidenceFieldExpectation:
        base: dict[str, Any] = {
            "tools": ("list_dlq_messages",),
            "field": "total",
            "which": "last",
            "equals": 0,
            "after_tools": _REPLAY_BOUNDARY,
        }
        return EvidenceFieldExpectation(**(base | overrides))

    def test_it_cuts_at_the_last_boundary_not_the_first(self, run_state: RunState) -> None:
        """A run that acted twice was only finished after the second action.

        This is the one place ``after_tools`` is not a mirror image of
        ``before_tools``, and getting it backwards would grade the listing
        taken BETWEEN two replays as the end state — a reading that says the
        queue was drained when the second replay had not happened yet.
        """
        run = _run(
            run_state,
            _entry("replay_dlq_by_ids", {"job_ids": [_SAFE_ROW]}, '{"replayed":1}', 1),
            _entry("list_dlq_messages", _FILTERED, _ONE_SAFE_ROW, 2),
            _entry("replay_dlq_by_ids", {"job_ids": [_SAFE_ROW]}, '{"replayed":1}', 3),
            _entry("list_dlq_messages", _FILTERED, _DRAINED, 4),
        )
        expectation = ScenarioExpectation(
            name="s",
            expected_terminal_state=IncidentState.RESOLVED,
            expected_evidence_fields=(self._claim(),),
        )
        assert _passed(grade(run, expectation), GradeDimension.EVIDENCE) is True

    def test_a_boundary_that_never_fired_is_unanswerable(self, run_state: RunState) -> None:
        run = _run(run_state, _entry("list_dlq_messages", _FILTERED, _DRAINED, 1))
        expectation = ScenarioExpectation(
            name="s",
            expected_terminal_state=IncidentState.RESOLVED,
            expected_evidence_fields=(self._claim(),),
        )
        report = grade(run, expectation)
        assert _passed(report, GradeDimension.EVIDENCE) is False
        detail = _dim(report, GradeDimension.EVIDENCE)
        assert "the ordering boundary never occurred" in detail
        assert "unanswerable rather than satisfied" in detail

    def test_a_pre_action_reading_cannot_satisfy_it(self, run_state: RunState) -> None:
        """The exact fallback that produced INC-001, refused."""
        run = _run(
            run_state,
            _entry("list_dlq_messages", _FILTERED, _DRAINED, 1),
            _entry("replay_dlq_by_ids", {"job_ids": [_SAFE_ROW]}, '{"replayed":1}', 2),
        )
        expectation = ScenarioExpectation(
            name="s",
            expected_terminal_state=IncidentState.RESOLVED,
            expected_evidence_fields=(self._claim(),),
        )
        assert _passed(grade(run, expectation), GradeDimension.EVIDENCE) is False

    def test_an_unregistered_boundary_is_a_load_error(self) -> None:
        with pytest.raises(ValidationError, match="not a registered tool"):
            self._claim(after_tools=("replay_dlq_by_idz",))

    def test_a_bookkeeping_marker_is_a_load_error(self) -> None:
        with pytest.raises(ValidationError, match="bookkeeping marker"):
            self._claim(after_tools=("_planner_plan",))

    def test_a_tool_cannot_be_its_own_after_boundary(self) -> None:
        with pytest.raises(ValidationError, match="both tools and after_tools"):
            self._claim(after_tools=("list_dlq_messages",))

    def test_the_two_boundaries_are_not_written_together(self) -> None:
        with pytest.raises(ValidationError, match="describe a window"):
            self._claim(before_tools=("replay_dlq_by_ids",), after_tools=("list_dlq_messages",))


class TestCallArguments:
    """Selecting the entry by what the agent ASKED for."""

    @staticmethod
    def _claim(**overrides: Any) -> EvidenceFieldExpectation:
        base: dict[str, Any] = {
            "tools": ("list_dlq_messages",),
            "field": "total",
            "which": "last",
            "equals": 0,
            "call_arguments": {"remediation_hint": "replay_safe"},
        }
        return EvidenceFieldExpectation(**(base | overrides))

    @staticmethod
    def _expectation(claim: EvidenceFieldExpectation) -> ScenarioExpectation:
        return ScenarioExpectation(
            name="s",
            expected_terminal_state=IncidentState.RESOLVED,
            expected_evidence_fields=(claim,),
        )

    def test_it_separates_two_shapes_of_the_same_read(self, run_state: RunState) -> None:
        """The unfiltered listing is present and later; the claim ignores it."""
        run = _run(
            run_state,
            _entry("list_dlq_messages", _FILTERED, _DRAINED, 1),
            _entry("list_dlq_messages", _UNFILTERED, _FOUR_ROWS_NO_SAFE, 2),
        )
        assert _passed(grade(run, self._expectation(self._claim())), GradeDimension.EVIDENCE)

    def test_null_means_absent_or_null(self, run_state: RunState) -> None:
        """One reading, because the platform cannot tell the two apart.

        ``wire.py`` fills the platform's defaults, so an argument the agent
        omitted and one it sent as ``null`` reach the server identically. A
        claim that distinguished them would be asserting on the harness.
        """
        claim = self._claim(call_arguments={"remediation_hint": None}, equals=4)
        omitted = _run(run_state, _entry("list_dlq_messages", {}, _FOUR_ROWS_NO_SAFE, 1))
        explicit = _run(run_state, _entry("list_dlq_messages", _UNFILTERED, _FOUR_ROWS_NO_SAFE, 1))
        for run in (omitted, explicit):
            assert _passed(grade(run, self._expectation(claim)), GradeDimension.EVIDENCE)

    def test_extra_bookkeeping_arguments_do_not_break_the_match(self, run_state: RunState) -> None:
        """The verify loop stamps ``attempt``/``of`` on its own probes.

        The selector is a SUBSET test, not an equality test, so the run that
        produced INC-001 — whose verify entry carries both — still matches.
        """
        arguments = _FILTERED | {"attempt": 1, "of": 6}
        run = _run(run_state, _entry("list_dlq_messages", arguments, _DRAINED, 1))
        assert _passed(grade(run, self._expectation(self._claim())), GradeDimension.EVIDENCE)

    def test_selecting_no_entry_fails_closed_naming_what_was_seen(
        self, run_state: RunState
    ) -> None:
        """ "That call was never made" and "it returned the wrong thing" differ.

        A detail that rendered identically for both would send the reader
        after an agent defect that is not there — which is how INC-001 nearly
        went.
        """
        run = _run(run_state, _entry("list_dlq_messages", _UNFILTERED, _FOUR_ROWS_NO_SAFE, 1))
        report = grade(run, self._expectation(self._claim()))
        assert _passed(report, GradeDimension.EVIDENCE) is False
        detail = _dim(report, GradeDimension.EVIDENCE)
        assert "the call this claim is about was never made" in detail
        assert "calls seen:" in detail
        assert "'remediation_hint': None" in detail

    def test_a_bool_is_never_a_number(self, run_state: RunState) -> None:
        claim = EvidenceFieldExpectation(
            tools=("replay_dlq_by_ids",),
            field="replayed",
            equals=1,
            call_arguments={"delay_seconds": True},
        )
        run = _run(
            run_state,
            _entry("replay_dlq_by_ids", {"delay_seconds": 1}, '{"replayed":1}', 1),
        )
        assert _passed(grade(run, self._expectation(claim)), GradeDimension.EVIDENCE) is False

    def test_an_argument_no_named_tool_accepts_is_a_load_error(self) -> None:
        with pytest.raises(ValidationError, match="accepts"):
            self._claim(call_arguments={"remediation_hnit": "replay_safe"})

    def test_an_empty_selector_is_a_load_error(self) -> None:
        with pytest.raises(ValidationError, match="empty one selects"):
            self._claim(call_arguments={})


class TestAnyOf:
    """The group: one level, exact members, and a readable red."""

    @staticmethod
    def _member(**overrides: Any) -> EvidenceFieldExpectation:
        base: dict[str, Any] = {
            "tools": ("list_dlq_messages",),
            "field": "total",
            "which": "last",
            "equals": 0,
        }
        return EvidenceFieldExpectation(**(base | overrides))

    def test_a_failing_group_shows_every_member(self, run_state: RunState) -> None:
        group = AnyOfExpectation(
            any_of=(self._member(equals=0), self._member(field="items[].id", equals=_SAFE_ROW))
        )
        run = _run(run_state, _entry("list_dlq_messages", _UNFILTERED, _FOUR_ROWS_NO_SAFE, 1))
        report = grade(
            run,
            ScenarioExpectation(
                name="s",
                expected_terminal_state=IncidentState.RESOLVED,
                expected_evidence_fields=(group,),
            ),
        )
        detail = _dim(report, GradeDimension.EVIDENCE)
        assert _passed(report, GradeDimension.EVIDENCE) is False
        assert "member 1 [" in detail
        assert "member 2 [" in detail
        assert "equals 0" in detail
        assert _SAFE_ROW in detail

    def test_one_member_is_not_a_group(self) -> None:
        with pytest.raises(ValidationError):
            AnyOfExpectation(any_of=(self._member(),))

    def test_a_member_may_not_be_another_group(self) -> None:
        with pytest.raises(ValidationError):
            AnyOfExpectation.model_validate(
                {"any_of": [{"any_of": []}, self._member().model_dump()]}
            )

    def test_a_volume_claim_may_not_be_a_member(self) -> None:
        with pytest.raises(ValidationError, match="which: sum has no business"):
            AnyOfExpectation(
                any_of=(
                    self._member(tools=("replay_dlq_by_ids",), field="replayed", which="sum"),
                    self._member(),
                )
            )

    def test_a_typo_reports_the_arm_it_belongs_to(self) -> None:
        """The tag function's whole purpose.

        Under a smart union a claim with a bad key reports both arms' errors,
        and half of that message points at ``any_of``, which the author never
        wrote.
        """
        with pytest.raises(ValidationError) as caught:
            ScenarioExpectation.model_validate(
                {
                    "name": "s",
                    "expected_terminal_state": "resolved",
                    "expected_evidence_fields": [
                        {"tools": ["list_dlq_messages"], "feild": "total", "equals": 0}
                    ],
                }
            )
        assert "any_of" not in str(caught.value)
