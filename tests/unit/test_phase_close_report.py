"""A phase-close report is resolvable, complete, honest about its runs, and checkable.

Four claims, from WO-R3-189's test requirement:

* the report is a versioned artifact the resolver finds;
* it carries all seven sections of plan 03 § 14 and none of them is empty;
* the leak hunt is a real grep over the trajectories the phase produced, and
  re-running it here reproduces what the committed report recorded;
* a ``development``-role run anywhere in scope marks the report NON-CLOSING.

The last one is the red-before: ``closing_verdict`` is the guard, and the test
below hands it a scope with one development run to prove the mark is derived
from the rows rather than asserted by whoever ran the assembler.

WO-R3-195 (WP-2.6) adds three, because there are now two phases in one
assembler:

* EVERY committed document regenerates byte for byte, not just the newest.
  Phase 1's document is committed evidence and generalising the assembler must
  not move a byte of it — and so is Phase 2's DRAFT, now that the FINAL has
  superseded it. ``close.committed_documents()`` pairs each one with the scope
  it was written from, and this module walks that list;
* the DRAFT mark is derived from the scope. A scope that still declares a
  pending re-run assembles a DRAFT; the same scope with those archives in
  ``live_legs`` assembles a FINAL. There is no argument that sets it;
* each phase's artifact is resolved by ITS OWN sweep id. ``artifacts.newest``
  now answers "phase 2", which is right for a reader and wrong for a test
  about phase 1.

The FINAL half of that second point is no longer hypothetical: the owner
released both re-runs, both came back green, and the two documents differ in
the way the draft said they would. So the draft-vs-final pair is checked on
both sides — the draft still says what it owed, the final says it owes nothing,
and neither one's bytes moved to make the other exist.
"""

from __future__ import annotations

import dataclasses
import difflib
import json
import re
import shutil
import subprocess
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

import pytest

from evals import artifacts
from evals import phase_close_report as close
from evals.graders.deterministic import GradeReport
from evals.runner import ExecutionMode, RunProvenance, RunReport, ScenarioOutcome
from incident_commander.agent.state import BudgetLedger, IncidentState
from incident_commander.config import ModelRole

pytestmark = pytest.mark.skipif(
    shutil.which("grep") is None, reason="the leak hunt shells out to grep"
)

#: Every phase this assembler declares. Tests that are about the PROTOCOL run
#: over all of them, so a third phase inherits the checks instead of needing
#: its own copies.
PHASES: list[int] = sorted(close.SCOPES)


def _document_cases() -> list[tuple[close.PhaseScope, Path, Path]]:
    """Every committed document beside the scope that produced it.

    Three today: Phase 1, Phase 2's DRAFT and the FINAL that supersedes it.
    Parametrizing on this rather than on the phase number is what keeps a
    superseded document under test — the whole point of invariant 9 is that it
    is still evidence after something newer exists.
    """
    return [(scope, halves[0], halves[1]) for scope, halves in close.committed_documents()]


def _case_ids() -> list[str]:
    return [json_path.stem for _, json_path, _ in _document_cases()]


#: The values a committed close report states about the REPO AS OF ASSEMBLY
#: TIME rather than about the archives it closes, with the reason each one is
#: allowed to move after the document is committed (WO-R3-263).
#:
#: This is not a loosening of "every committed document regenerates byte for
#: byte" — it is that claim, made about the half of the document that can
#: actually hold still. Two values in a close report are derived from today's
#: source tree by design and the document says so in its own prose:
#:
#: * the leak-hunt vocabulary — "all derived rather than typed: `chaos`; every
#:   `HypothesisCategory` value; every scenario name and every chaos hook name
#:   in the corpus". Adding a category widens the search, which is the whole
#:   point of deriving it; the report's FINDINGS (hits, adjudications, verdict)
#:   are about the archives and are not in this list.
#: * the judge-prompt digests, printed under the heading "Judge prompt digests
#:   at assembly time". A judge prompt edited after a close moves them, and the
#:   claim the section makes — no judge re-run was required inside the phase —
#:   is untouched by that.
#:
#: Everything else must still match to the byte, and an unexplained line is
#: reported with the line in it. The same class as the corpus-size gate cmd
#: #284 hit (LESSONS 2026-09-17): a frozen artifact regenerated from a living
#: repo can only be pinned on what the repo is not allowed to move.
_ASSEMBLY_TIME_FACTS: Final[tuple[tuple[str, str], ...]] = ()


def _unexplained_drift(committed: str, regenerated: str) -> list[str]:
    """Every differing line that is NOT an assembly-time fact.

    Line-level rather than a normalising rewrite of both sides, because a
    normaliser hides what it touched: this reports the offending line, which is
    what a reader needs in order to decide whether a committed document just
    lost its meaning or whether one more derived value needs recording above.
    """
    changed = [
        line
        for line in difflib.unified_diff(
            committed.splitlines(), regenerated.splitlines(), lineterm="", n=0
        )
        if line[:1] in {"+", "-"} and not line.startswith(("+++", "---"))
    ]
    return [
        line
        for line in changed
        if not any(re.search(pattern, line[1:]) for pattern, _ in _ASSEMBLY_TIME_FACTS)
    ]


def _committed(phase: int) -> dict[str, Any]:
    """The CURRENT committed document for a phase: the newest version."""
    document: dict[str, Any] = json.loads(close.committed(phase)[0].read_text())
    return document


def _rendered(phase: int) -> str:
    return close.committed(phase)[1].read_text()


def _superseded(phase: int, index: int = 0) -> tuple[dict[str, Any], str]:
    """An earlier version of a phase's document, JSON and Markdown."""
    json_path, md_path = close.committed_versions(phase)[index]
    document: dict[str, Any] = json.loads(json_path.read_text())
    return document, md_path.read_text()


def _report(invocation_id: str, role: ModelRole, *, predates_roles: bool = False) -> RunReport:
    """One-row report, stamped like a real run, for the closing-mark tests."""
    when = datetime(2026, 9, 17, tzinfo=UTC)
    return RunReport(
        generated_at=when,
        total=1,
        passed=1,
        failed=0,
        invocation_id=invocation_id,
        closing=None if predates_roles else role is ModelRole.BENCHMARK,
        outcomes=(
            ScenarioOutcome(
                scenario="remediate_consumer_lag_success",
                final_state=IncidentState.RESOLVED,
                tool_calls_used=5,
                report=GradeReport(
                    scenario="remediate_consumer_lag_success", passed=True, dimensions=()
                ),
                provenance=RunProvenance(
                    commander_revision="0" * 40,
                    platform_image_digest="sha256:" + "1" * 64,
                    agent_model="claude-sonnet-4-6",
                    model_role=role,
                    judge_model="claude-haiku-4-5",
                    strategy="baseline",
                    scenario="remediate_consumer_lag_success",
                    invocation_id=invocation_id,
                    recorded_at=when,
                    execution_mode=ExecutionMode.LIVE,
                    budget=BudgetLedger(
                        max_tool_calls=13,
                        max_tokens=200_000,
                        max_wall_seconds=600,
                        max_usd=Decimal("1.00"),
                    ),
                ),
            ),
        ),
    )


# --------------------------------------------------------------------------
# The artifact kind
# --------------------------------------------------------------------------


def test_the_phase_close_report_resolves_through_the_artifact_resolver() -> None:
    """Divergence D2: without a KINDS entry the report cannot be resolved at all."""
    for kind in ("phase_close_report", "phase_close_report_md"):
        assert kind in artifacts.KINDS
        path = artifacts.newest(kind)
        assert path.is_file()
        assert path.parent == close.REPO_ROOT / "evals/reports/phase-close"


@pytest.mark.parametrize("phase", PHASES)
def test_each_phase_resolves_its_own_artifact_by_its_own_sweep(phase: int) -> None:
    """Two phases share one kind, so "newest" stopped meaning "mine"."""
    scope = close.SCOPES[phase]
    for path in close.committed(phase):
        assert path.is_file()
        assert scope.canned_sweep in path.name
    assert json.loads(close.committed(phase)[0].read_text())["phase"] == phase
    # The newest version of the kind is the latest phase, which is what a
    # reader following `artifacts.newest` should get.
    assert artifacts.newest("phase_close_report") == close.committed(max(PHASES))[0]


def test_every_committed_document_has_a_declared_scope_and_vice_versa() -> None:
    """The registry that keeps a superseded document regenerable.

    Red before ``COMMITTED_SCOPES``: writing the FINAL left the DRAFT's bytes
    with nothing in the module that could reproduce them, because ``SCOPES[2]``
    had moved on. The pairing is asserted rather than assumed, so publishing a
    version without declaring the scope behind it fails here.
    """
    cases = _document_cases()
    assert len(cases) == len(close.COMMITTED_SCOPES) == 3
    assert [scope.phase for scope, _, _ in cases] == [1, 2, 2]
    # Phase 2's two versions are two files, oldest first, and the newest is
    # what `committed` — and therefore every reader — resolves to.
    draft_json, final_json = (case[1] for case in cases[1:])
    assert draft_json != final_json
    assert close.committed(2)[0] == final_json
    assert close.COMMITTED_SCOPES[1] is close.PHASE2_DRAFT
    assert close.COMMITTED_SCOPES[2] is close.PHASE2 is close.SCOPES[2]


def test_a_committed_version_with_no_declared_scope_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard, exercised: two files and one scope is an error, not a zip."""
    monkeypatch.setattr(close, "COMMITTED_SCOPES", (close.PHASE1, close.PHASE2))
    with pytest.raises(ValueError, match="committed version"):
        close.committed_documents()


def test_the_new_kind_does_not_adopt_or_get_adopted_by_its_neighbours(tmp_path: Path) -> None:
    """Three families share ``evals/reports/``; each resolves only its own stem."""
    reports = tmp_path / "evals" / "reports"
    reports.mkdir(parents=True)
    names = {
        "report": "report.20260917T000000Z.aaaaaaaaaaaa.json",
        "baseline_report": "baseline_report.20260917T000000Z.bbbbbbbbbbbb.json",
        "phase_close_report": "phase_close_report.20260917T000000Z.cccccccccccc.json",
    }
    for name in names.values():
        (reports / name).write_text("{}")
    for kind, expected in names.items():
        assert artifacts.newest(kind, root=tmp_path).name == expected
    assert len(artifacts.versions("phase_close_report", root=tmp_path)) == 1


def test_writing_twice_refuses_rather_than_replacing(tmp_path: Path) -> None:
    """Invariant 9 at the filesystem: a second write raises, never overwrites."""
    scope = close.SCOPES[max(PHASES)]
    document = _committed(scope.phase)
    first = close.write(document, scope, root=tmp_path)
    assert all(path.is_file() for path in first)
    with pytest.raises(FileExistsError):
        close.write(document, scope, root=tmp_path)


def test_rendering_a_document_against_another_phases_scope_is_refused() -> None:
    """The markdown carries prose the JSON does not, so the pairing matters.

    Red before ``render_markdown`` took the scope as an argument: it looked the
    scope up by phase number, which quietly rendered Phase 2's DRAFT with the
    FINAL's closing paragraph once ``SCOPES[2]`` moved on.
    """
    draft = close.assemble(close.REPO_ROOT, close.PHASE2_DRAFT)
    with pytest.raises(ValueError, match="phase 1"):
        close.render_markdown(draft, close.PHASE1)
    # Same phase, different scope: the two documents are told apart by their
    # own prose, which is exactly what the byte-for-byte test below pins.
    assert close.render_markdown(draft, close.PHASE2_DRAFT) != close.render_markdown(
        draft, close.PHASE2
    )


# --------------------------------------------------------------------------
# Seven sections, none empty
# --------------------------------------------------------------------------


@pytest.mark.parametrize("phase", PHASES)
def test_the_committed_report_carries_all_seven_sections_and_none_is_empty(phase: int) -> None:
    document = _committed(phase)
    assert tuple(document["sections"]) == close.SECTION_KEYS
    assert len(close.SECTION_KEYS) == 7
    for key in close.SECTION_KEYS:
        section = document["sections"][key]
        assert section, f"section {key} is empty"
        assert any(value not in (None, "", [], {}) for value in section.values()), key


@pytest.mark.parametrize("phase", PHASES)
def test_every_section_reaches_the_human_half(phase: int) -> None:
    rendered = _rendered(phase)
    for title in close.SECTION_TITLES.values():
        assert f"## {title}" in rendered, title
    assert "What this close does and does not claim" in rendered
    assert "Deviations from the protocol as written" in rendered


def test_an_empty_section_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """The completeness rule is enforced at assembly, not left to a reviewer."""
    monkeypatch.setattr(close, "_spend_line", lambda root, scope: {})
    with pytest.raises(ValueError, match="empty section"):
        close.assemble(close.REPO_ROOT, close.PHASE2)


# --------------------------------------------------------------------------
# The closing mark — the red-before
# --------------------------------------------------------------------------


def test_a_development_role_run_in_scope_marks_the_report_non_closing() -> None:
    """Plan 03 § 14: any development run in a phase report makes it non-closing.

    Red before ``closing_verdict`` existed: the assembler would have written
    ``closing`` from the canned sweep's own flag, which is ``True``, and a
    development live leg beside it would have gone unremarked.
    """
    benchmark = _report("32ae38f6b38b", ModelRole.BENCHMARK)
    development = _report("deadbeefcafe", ModelRole.DEVELOPMENT)

    # The naive implementation — take the sweep's own mark, which is what
    # `make eval-reg` prints — says CLOSING for both scopes below.
    assert benchmark.closing is True

    clean = close.closing_verdict([benchmark])
    assert clean["closing"] is True
    assert clean["development_runs"] == []

    tainted = close.closing_verdict([benchmark, development])
    assert tainted["closing"] is False
    assert "deadbeefcafe/remediate_consumer_lag_success" in tainted["development_runs"]
    assert "non-closing" in tainted["reason"]
    assert ModelRole.DEVELOPMENT.value in tainted["reason"]


def test_a_run_that_predates_model_roles_is_also_non_closing() -> None:
    predating = _report("0011223344ff", ModelRole.BENCHMARK, predates_roles=True)
    verdict = close.closing_verdict([predating])
    assert verdict["closing"] is False
    assert "predate model roles" in verdict["reason"]


@pytest.mark.parametrize("phase", PHASES)
def test_the_committed_report_is_closing_and_says_why(phase: int) -> None:
    scope = close.SCOPES[phase]
    document = _committed(phase)
    assert document["closing"] is True
    assert document["closing_reason"].strip()
    assert set(document["runs_in_scope"]) == set(scope.archives)
    assert "CLOSING" in _rendered(phase)


# --------------------------------------------------------------------------
# The leak hunt
# --------------------------------------------------------------------------


@pytest.mark.parametrize("phase", PHASES)
def test_the_gating_grep_is_reproducible_and_still_finds_nothing(phase: int) -> None:
    """The committed command is re-run here, against the committed evidence.

    A grep whose output is pasted into a document is a claim; a grep the test
    suite re-runs is a check. If a later commit puts ``chaos`` into a
    trajectory either phase produced, this goes red.
    """
    document = _committed(phase)
    recorded = document["sections"]["leak_hunt"]["committed_commands"]["trajectories"]
    completed = subprocess.run(
        recorded["command"].split(),
        cwd=close.REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.stdout == recorded["output"]
    assert completed.returncode == recorded["exit_status"] == 1
    assert recorded["matching_lines"] == 0


@pytest.mark.parametrize(("phase", "trajectories"), [(1, 45), (2, 73)])
def test_nothing_the_agent_read_from_the_platform_carries_an_unadjudicated_term(
    phase: int, trajectories: int
) -> None:
    """WP-1.5's acceptance, claimed here rather than in WO-R3-187."""
    hunt = _committed(phase)["sections"]["leak_hunt"]
    assert hunt["verdict"] == "PASS"
    assert hunt["unadjudicated_hits"] == []
    assert hunt["trajectories"]["files_searched"] == trajectories
    read = hunt["trajectories"]["platform_responses_the_agent_read"]
    assert "chaos" not in read
    for term in read:
        assert term in hunt["adjudications"], term


@pytest.mark.parametrize("phase", PHASES)
def test_the_chaos_mentions_are_ours_and_the_harness_s_and_none_are_the_platform_s(
    phase: int,
) -> None:
    """The point of the classification: which side of the boundary each hit is on."""
    hunt = _committed(phase)["sections"]["leak_hunt"]
    totals = hunt["traces_by_author"]["totals"]
    assert totals["platform_response"].get("chaos", 0) == 0
    assert totals["harness_record"]["chaos"] > 0  # the evaluator's own setup record
    for run in hunt["traces_by_author"]["per_run"]:
        assert run["platform_response"].get("chaos", 0) == 0, run["archive"]


def test_the_last_chaos_token_on_our_own_side_is_gone_by_phase_2() -> None:
    """The one bucket that moved between the two closes.

    Phase 1 found `chaos` in our own `remediation_planner.md` and filed
    WO-R3-255 for it. cmd #258 removed it. This is where that removal is
    checked against live traces rather than against the diff that made it.
    """
    assert (
        _committed(1)["sections"]["leak_hunt"]["traces_by_author"]["totals"]["commander_prompt"][
            "chaos"
        ]
        > 0
    )
    assert (
        _committed(2)["sections"]["leak_hunt"]["traces_by_author"]["totals"][
            "commander_prompt"
        ].get("chaos", 0)
        == 0
    )


def test_the_terms_are_derived_from_the_corpus_not_typed_out() -> None:
    groups = close.leak_terms(close.REPO_ROOT)
    assert groups["chaos_vocabulary"] == ("chaos",)
    assert "consumer_saturation" in groups["root_cause_labels"]
    assert "kill_consumer" in groups["fixture_names"]
    assert "remediate_dlq_backlog_success" in groups["fixture_names"]
    assert len(close._all_terms(groups)) > 50


# --------------------------------------------------------------------------
# The other five sections, checked against the archives they summarize
# --------------------------------------------------------------------------


@pytest.mark.parametrize(("phase", "live_total", "live_passed"), [(1, 4, 3), (2, 5, 3)])
def test_the_sweep_section_matches_the_committed_archives(
    phase: int, live_total: int, live_passed: int
) -> None:
    scope = close.SCOPES[phase]
    sweep = _committed(phase)["sections"]["sweep_results"]
    raw = json.loads(
        (close.archive_dir(close.REPO_ROOT, scope.canned_sweep) / "report.json").read_text()
    )
    assert sweep["canned_sweep"]["archive"] == scope.canned_sweep
    assert sweep["canned_sweep"]["total"] == raw["total"] == 41
    assert sweep["canned_sweep"]["passed"] == raw["passed"] == 41
    assert sweep["canned_sweep"]["model_roles"] == [ModelRole.BENCHMARK.value]
    assert "no changes vs baseline" in sweep["canned_sweep"]["gate_lines"]
    assert sweep["live_total"] == live_total
    assert sweep["live_passed"] == live_passed


def test_the_root_cause_column_appears_only_where_something_was_graded() -> None:
    """A row of zeroes would read as "every diagnosis was wrong"."""
    phase1 = _committed(1)["sections"]["sweep_results"]
    assert "root_cause" not in phase1["canned_sweep"]  # the labels did not exist yet
    assert all("root_cause" not in leg for leg in phase1["live_legs"])

    phase2 = _committed(2)["sections"]["sweep_results"]
    diagnosis = phase2["canned_sweep"]["root_cause"]
    assert (diagnosis["graded"], diagnosis["correct"], diagnosis["of_total"]) == (32, 32, 41)
    assert all(leg["root_cause"]["graded"] == 1 for leg in phase2["live_legs"])
    assert phase2["live_root_cause_on_seeded_legs"] == {
        "correct": 5,
        "graded": 5,
        "why_only_these": phase2["live_root_cause_on_seeded_legs"]["why_only_these"],
    }
    # The draft's own number, still 3/3 in the document that reported it.
    draft, _ = _superseded(2)
    assert draft["sections"]["sweep_results"]["live_root_cause_on_seeded_legs"]["graded"] == 3


def test_the_read_only_pass_reports_the_regrade_and_never_the_withdrawn_figure() -> None:
    """INC-003: the archive's own headline must not be quoted as a result."""
    document = _committed(2)
    block = document["sections"]["sweep_results"]["read_only_pass"]
    assert block["archive"] == "0db6fe722f7c"
    assert block["as_archived_passed"] == 20
    assert block["regrade"]["totals"]["regraded_passed"] == 26
    assert block["regrade"]["root_cause"]["regraded"] == {
        "graded": 2,
        "correct": 2,
        "not_graded_world": 16,
        "accuracy": 1.0,
        "describe": block["regrade"]["root_cause"]["regraded"]["describe"],
    }
    assert block["regrade"]["files_verified_unchanged"] == 82

    # The withdrawn figure appears exactly where it is labelled as withdrawn,
    # and nowhere else in the human half.
    rendered = _rendered(2)
    quoted = [line for line in rendered.splitlines() if "11/18" in line]
    assert len(quoted) == 3  # does-not-claim, the section-1 note, deviation D6
    assert "WITHDRAWN" in rendered
    for line in quoted:
        assert "withdrawn" in line.lower(), line


def test_the_gate_crossing_audit_covers_every_handoff_including_the_red_run_s_none() -> None:
    audit = _committed(1)["sections"]["gate_crossing_audit"]
    assert audit["bar"] == close.REMEDIATE_BAR == 0.7
    by_archive = {run["archive"]: run for run in audit["runs"]}
    assert set(by_archive) == {leg.archive_id for leg in close.PHASE1.live_legs}

    red = by_archive["42000dfda188"]
    assert red["final_state"] == "escalated"
    assert red["handoffs_attempted"] == 0
    assert len(red["planner_steps"]) == red["non_crossings"] == 5
    assert all(step["in_fix_map"] and step["meets_bar"] for step in red["planner_steps"])
    assert "NOT justified" in red["escalation_verdict"]

    poison = by_archive["47abb70a2b9e"]
    assert poison["handoffs_refused"] == 1
    assert poison["handoffs_crossed"] == 1
    assert poison["executed"] == [
        {
            "tool": "replay_dlq_by_ids",
            "arguments": poison["executed"][0]["arguments"],
        }
    ]
    assert [j["verdict"] for j in poison["verify_judgments"]] == ["verified"]

    for archive in ("845bdae22195", "ee183c85429c", "47abb70a2b9e"):
        run = by_archive[archive]
        assert run["handoffs_crossed"] == 1
        assert run["final_state"] == run["expected_terminal_state"] == "resolved"
        assert run["verify_judgments"][-1]["verdict"] == "verified"


@pytest.mark.parametrize("phase", PHASES)
def test_the_budget_section_reports_counts_and_says_why_the_diff_is_null(phase: int) -> None:
    budget = _committed(phase)["sections"]["budget_profile_diff"]
    assert budget["diff"] == "null"
    assert "O-14" in budget["why_null"]
    assert budget["budget_trips"] == []
    assert len(budget["per_run"]) == len(close.SCOPES[phase].live_legs)
    for row in budget["per_run"]:
        assert row["llm_calls_total"] == sum(row["llm_calls_by_role"].values())
        assert row["agent_tool_calls_billed"] <= row["max_tool_calls"]
        assert row["tokens_used"] < row["max_tokens"]


def test_the_per_role_split_appears_only_once_the_records_exist() -> None:
    """WP-2.3's column. A table of blanks would invite a comparison it cannot support."""
    assert "per_role_totals" not in _committed(1)["sections"]["budget_profile_diff"]

    budget = _committed(2)["sections"]["budget_profile_diff"]
    roles = {row["role"]: row for row in budget["per_role_totals"]}
    assert set(roles) == {
        "investigation_planner",
        "remediation_planner",
        "briefing_writer",
        "briefing_judge",
        "verification_judge",
    }
    # Sorted by spend, planner first — the point of the column.
    assert budget["per_role_totals"][0]["role"] == "investigation_planner"
    assert roles["briefing_judge"]["charged_to_ledger"] is False  # the evaluator's own
    # cmd #264 changed the answer mid-phase, so the column says so instead of
    # picking whichever run it read first.
    assert roles["briefing_writer"]["charged_to_ledger"] == "mixed"
    assert "cmd #264" in budget["per_role_note"]
    for row in budget["per_role_totals"]:
        assert row["calls"] > 0 and row["tokens"] > 0 and row["elapsed_ms"] > 0


def test_no_judge_calibration_was_owed_and_the_prompts_are_hashed() -> None:
    judge = json.loads(artifacts.newest("phase_close_report").read_text())["sections"][
        "judge_calibration"
    ]
    assert judge["reruns_required"] == 0
    assert set(judge["judge_prompts_unchanged"]) == {"briefing_judge.md", "verification_judge.md"}
    assert judge["judge_model"] == "claude-haiku-4-5"
    assert judge["why"].strip() and judge["evidence"].strip()


@pytest.mark.parametrize("phase", PHASES)
def test_the_baseline_delta_cites_the_phase_0_artifact_by_id_and_blocks_on_nothing(
    phase: int,
) -> None:
    delta = _committed(phase)["sections"]["baseline_delta"]
    assert delta["phase0_baseline_cited_by_id"] == close.SCOPES[phase].phase0_baseline_cited
    assert (
        delta["phase0_baseline_resolved_by_artifacts_newest"]
        == artifacts.newest("baseline_report").name
    )
    assert len(delta["comparisons"]) == 2
    for comparison in delta["comparisons"]:
        assert comparison["regressions"] == []
        assert comparison["dropped_scenarios"] == []
        assert comparison["dropped_dimensions"] == []
        assert comparison["vacated_assertions"] == []
    # Plan 03 § 14 step 6 blocks on UNEXPLAINED movement, which is derived,
    # not declared: whatever a comparison could not account for lands here.
    assert delta["unexplained_movement"] == []


def test_row_movement_is_classified_rather_than_asserted_away() -> None:
    """Phase 1 saw zero differing rows; Phase 2 sees all 41 and must explain them.

    Red before ``_classify_row_differences``: the section printed "41 differing
    rows" beside a paragraph saying nothing had moved, and a reader had no way
    to tell an added dimension from a changed verdict.
    """
    for comparison in _committed(1)["sections"]["baseline_delta"]["comparisons"]:
        assert comparison["row_level_differences"] == []
        assert "movement_by_a_dimension_this_phase_added" not in comparison

    for comparison in _committed(2)["sections"]["baseline_delta"]["comparisons"]:
        assert len(comparison["row_level_differences"]) == 41
        explained = comparison["movement_by_a_dimension_this_phase_added"]
        assert explained["rows"] == 41
        assert explained["dimensions_added"] == {"root_cause": 41}
        assert explained["all_passing"] is True
        assert comparison["unexplained_row_differences"] == []


@pytest.mark.parametrize(("phase", "plan_rows"), [(1, 3), (2, 4)])
def test_the_spend_line_is_the_archives_own_ledgers_not_an_estimate(
    phase: int, plan_rows: int
) -> None:
    spend = _committed(phase)["sections"]["spend_line"]
    total = Decimal("0")
    for row in spend["live_runs"]:
        raw = json.loads(
            (close.archive_dir(close.REPO_ROOT, row["archive"]) / "report.json").read_text()
        )
        ledger = raw["outcomes"][0]["provenance"]["budget"]
        assert row["usd"] == ledger["usd_used"]
        assert row["agent_loop_wall_seconds"] == round(ledger["wall_seconds_used"], 3)
        assert row["tokens"] == ledger["tokens_used"]
        total += Decimal(str(row["usd"]))
    if (pass_row := spend.get("read_only_pass")) is not None:
        total += Decimal(pass_row["usd"])
    assert Decimal(spend["live_total_usd"]) == total
    assert spend["canned_sweep"]["usd"] == "0.000000"
    assert len(spend["against_plan_03_section_11"]) == plan_rows


def test_the_bill_separates_the_agents_spend_from_the_evaluators() -> None:
    """Two real numbers, and the difference between them is checkable.

    The agent's ledger is what invariant 7 caps; the eval harness's briefing
    judge is real money that would fail the ledger reconciliation if it were
    folded in. Phase 1's archives carry no per-role record, so it reports one
    total and the key is absent rather than equal to the other.
    """
    assert "live_total_usd_including_evaluator" not in _committed(1)["sections"]["spend_line"]

    spend = _committed(2)["sections"]["spend_line"]
    ledgers = Decimal(spend["live_total_usd"])
    bill = Decimal(spend["live_total_usd_including_evaluator"])
    assert bill > ledgers
    assert bill - ledgers == Decimal(spend["evaluator_share_usd"])

    # And the bill is the sum of every per-role row, evaluator roles included.
    roles = _committed(2)["sections"]["budget_profile_diff"]["per_role_totals"]
    assert sum((Decimal(role["usd"]) for role in roles), Decimal("0")) == bill


def test_wall_time_comes_from_the_traces_because_one_ledger_meter_is_wrong() -> None:
    """A finding, pinned: `648a32f2339d` records 0.057 s for a 58-second run.

    The report says so rather than printing the figure straight, and this test
    is what stops the caveat from being quietly dropped if the number changes.
    """
    spend = _committed(2)["sections"]["spend_line"]
    row = next(r for r in spend["live_runs"] if r["archive"] == "648a32f2339d")
    assert row["agent_loop_wall_seconds"] < 1
    assert row["start_to_finish_seconds"] > 50
    caps = _committed(2)["sections"]["budget_profile_diff"]["caps_note"]
    assert "should NOT be trusted" in caps and "648a32f2339d" in caps


# --------------------------------------------------------------------------
# The whole document
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("scope", "committed_json", "committed_md"), _document_cases(), ids=_case_ids()
)
def test_every_committed_report_regenerates_byte_for_byte(
    scope: close.PhaseScope, committed_json: Path, committed_md: Path
) -> None:
    """Every input is committed, so each document is a function of the repo.

    If this fails, something the report READ has changed — a judge prompt, the
    blessed baseline, an archive. That is worth a look rather than a re-write:
    the report's claims are about the state those inputs were in.

    Phase 1 is the load-bearing case for WO-R3-195: generalising the assembler
    for a second phase must not move one byte of a document that is already
    committed evidence. Phase 2's DRAFT is the second such case — a report that
    has been superseded is still evidence, and the FINAL was not allowed to
    reach back and change what the draft said about the runs it had.

    The look WO-R3-263 took, recorded here because the next one will be the
    same: two values in these documents are derived from the source tree at
    ASSEMBLY TIME and nothing can freeze them — the leak-hunt vocabulary (every
    ``HypothesisCategory`` value) and the judge-prompt digests. Adding a
    category and putting a shared rule into the briefing judge moved exactly
    those and nothing else. They are enumerated in ``_ASSEMBLY_TIME_FACTS``
    with their reasons; every other line of every committed document is still
    pinned to the byte, and the failure below names the line.
    """
    document = close.assemble(close.REPO_ROOT, scope)
    for regenerated, committed in (
        (close.render_json(document), committed_json.read_text()),
        (close.render_markdown(document, scope), committed_md.read_text()),
    ):
        drift = _unexplained_drift(committed, regenerated)
        assert not drift, (
            "a committed close report no longer regenerates, and the change is "
            "not one of the assembly-time values this test allows:\n  "
            + "\n  ".join(drift)
            + "\n\nThe document's claims are about the inputs as they were. Read "
            "what moved before touching anything: if an ARCHIVE, a baseline or a "
            "spend figure moved, something has rewritten evidence. If it is one "
            "more value the assembler derives from today's tree, add it to "
            "_ASSEMBLY_TIME_FACTS with the reason."
        )


@pytest.mark.parametrize("phase", PHASES)
def test_the_report_states_its_reduction_and_its_open_follow_ups(phase: int) -> None:
    document = _committed(phase)
    rendered = _rendered(phase)
    assert {entry["id"] for entry in document["deviations"]} >= {"D1", "D2", "D3", "D4"}
    assert "O-14" in rendered
    assert document["follow_ups"]
    assert all(
        entry["what"].strip() and entry["status"].strip() for entry in document["follow_ups"]
    )
    assert document["incidents_row_reason"].strip()
    assert document["claims"]["does_claim"] and document["claims"]["does_not_claim"]


def test_each_phase_names_its_own_scope_decision_and_incident_answer() -> None:
    assert "O-15" in _rendered(1)
    assert _committed(1)["incidents_row_filed"] is False
    assert {e["id"] for e in _committed(1)["follow_ups"]} == {
        "WO-R3-253",
        "WO-R3-255",
        "O-8",
    }

    assert "O-20" in _rendered(2)
    # INC-003 was filed BEFORE its fix, which is the protocol; the close says so.
    assert _committed(2)["incidents_row_filed"] is True
    assert "INC-003" in _committed(2)["incidents_row_reason"]
    assert {"WO-R3-266", "WO-R3-268 / O-21"} <= {e["id"] for e in _committed(2)["follow_ups"]}


# --------------------------------------------------------------------------
# DRAFT vs FINAL — derived from the scope, never set
# --------------------------------------------------------------------------


def test_the_draft_mark_is_derived_from_the_scope_not_hand_set() -> None:
    """A status field anyone can type is one that will be typed wrong.

    The only way to reach FINAL is to hold the evidence: put the pending
    archives in ``live_legs`` and the ``PendingRerun`` entries go away with
    them. There is no argument to ``assemble`` or ``draft_status`` that says
    "this one is final".
    """
    drafted = close.draft_status(close.PHASE2_DRAFT)
    assert drafted["status"] == "DRAFT"
    assert len(drafted["pending_reruns"]) == 2
    assert {entry["scenario"] for entry in drafted["pending_reruns"]} == {
        "remediate_stale_cache_success",
        "remediate_dlq_backlog_success",
    }
    for entry in drafted["pending_reruns"]:
        assert "owner" in entry["blocked_on"]
        assert entry["supersedes"] in {leg.archive_id for leg in close.PHASE2_DRAFT.live_legs}

    # Phase 1 owes nothing and derives FINAL from the same function.
    assert close.draft_status(close.PHASE1)["status"] == "FINAL"

    # And so does Phase 2, now that its two re-runs are in and nothing else
    # about the scope was touched to say so.
    assert close.draft_status(close.PHASE2)["status"] == "FINAL"
    assert close.draft_status(close.PHASE2)["pending_reruns"] == []
    assert close.draft_status(dataclasses.replace(close.PHASE2, pending_reruns=()))["status"] == (
        "FINAL"
    )


def test_the_superseded_phase_2_report_is_still_a_draft_and_still_says_what_it_owed() -> None:
    """The draft is not corrected in place; it is answered by a later document."""
    document, rendered = _superseded(2)
    assert document["status"] == "DRAFT"
    assert len(document["pending_reruns"]) == 2
    assert "**DRAFT**" in rendered
    assert "What is still owed" in rendered
    for entry in document["pending_reruns"]:
        assert f"`{entry['scenario']}`" in rendered

    # Phase 1's document predates the field and must not grow one.
    assert "status" not in _committed(1)


def test_the_current_phase_2_report_is_final_and_owes_nothing() -> None:
    document = _committed(2)
    rendered = _rendered(2)
    assert document["status"] == "FINAL"
    assert document["pending_reruns"] == []
    assert "**FINAL**" in rendered
    assert "What is still owed" not in rendered
    assert "3 of 5" in rendered  # the pass rate it does NOT round up


def test_a_rerun_leg_names_the_leg_it_re_runs_and_the_red_stays_in_the_record() -> None:
    """Both halves of the same rule.

    A close that replaced its reds with their re-runs would be a selected
    sample of itself, so the reds stay; and a table with five rows over three
    scenarios is unreadable unless each re-run says which run it answers.
    """
    legs = _committed(2)["sections"]["sweep_results"]["live_legs"]
    assert [leg["archive"] for leg in legs] == [
        "759e198cdd27",
        "648a32f2339d",
        "fc896b25a09c",
        "d16aa18dce08",
        "42c675d9c145",
    ]
    reruns = {leg["archive"]: leg["reruns"] for leg in legs if "reruns" in leg}
    assert reruns.keys() == {"d16aa18dce08", "42c675d9c145"}
    assert reruns["d16aa18dce08"]["archive"] == "648a32f2339d"
    assert reruns["42c675d9c145"]["archive"] == "fc896b25a09c"
    for archive, link in reruns.items():
        assert link["why"].strip()
        # The run it re-runs is still a row, and still red.
        superseded = next(leg for leg in legs if leg["archive"] == link["archive"])
        assert superseded["passed"] is False
        assert next(leg for leg in legs if leg["archive"] == archive)["passed"] is True

    # A first run carries no link at all, in either phase.
    assert all(
        "reruns" not in leg
        for phase in PHASES
        for leg in _committed(phase)["sections"]["sweep_results"]["live_legs"]
        if leg["archive"] not in reruns
    )


def test_the_final_version_was_written_beside_the_draft_not_over_it(tmp_path: Path) -> None:
    """Invariant 9: adding the re-runs did not aim at the draft's own path.

    The stamp is the newest moment any evidence in scope was written, so the
    scope with two newer archives in it landed on a new filename. Both files
    are on disk, they carry the same canned-sweep id and different timestamps,
    and the draft's bytes are the ones the draft's own scope produces — up to
    the assembly-time values nothing can freeze, which is the same allowance
    ``test_every_committed_report_regenerates_byte_for_byte`` documents and
    which is why the comparison goes through the same function.
    """
    (draft_json, draft_md), (final_json, final_md) = close.committed_versions(2)
    assert draft_json != final_json and draft_md != final_md
    assert close.PHASE2.canned_sweep in draft_json.name
    assert close.PHASE2.canned_sweep in final_json.name
    assert draft_json.name < final_json.name  # the timestamp, and the sort order
    draft = close.assemble(close.REPO_ROOT, close.PHASE2_DRAFT)
    assert not _unexplained_drift(draft_json.read_text(), close.render_json(draft))

    # And a re-write of either aims at its own path and refuses.
    for scope in (close.PHASE2_DRAFT, close.PHASE2):
        document = close.assemble(close.REPO_ROOT, scope)
        written = close.write(document, scope, root=tmp_path)
        assert all(path.is_file() for path in written)
        with pytest.raises(FileExistsError):
            close.write(document, scope, root=tmp_path)
    assert len(artifacts.versions("phase_close_report", root=tmp_path)) == 2
