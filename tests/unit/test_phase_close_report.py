"""A phase-close report is resolvable, complete, honest about its runs, and checkable.

Four claims, from WO-R3-189's test requirement:

* the report is a versioned artifact that ``artifacts.newest`` resolves;
* it carries all seven sections of plan 03 § 14 and none of them is empty;
* the leak hunt is a real grep over the trajectories the phase produced, and
  re-running it here reproduces what the committed report recorded;
* a ``development``-role run anywhere in scope marks the report NON-CLOSING.

The last one is the red-before: ``closing_verdict`` is the guard, and the test
below hands it a scope with one development run to prove the mark is derived
from the rows rather than asserted by whoever ran the assembler.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

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
        assert path.parent == close.REPO_ROOT / "evals/reports"


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
    document = json.loads(artifacts.newest("phase_close_report").read_text())
    first = close.write(document, root=tmp_path)
    assert all(path.is_file() for path in first)
    with pytest.raises(FileExistsError):
        close.write(document, root=tmp_path)


# --------------------------------------------------------------------------
# Seven sections, none empty
# --------------------------------------------------------------------------


def test_the_committed_report_carries_all_seven_sections_and_none_is_empty() -> None:
    document = json.loads(artifacts.newest("phase_close_report").read_text())
    assert tuple(document["sections"]) == close.SECTION_KEYS
    assert len(close.SECTION_KEYS) == 7
    for key in close.SECTION_KEYS:
        section = document["sections"][key]
        assert section, f"section {key} is empty"
        assert any(value not in (None, "", [], {}) for value in section.values()), key


def test_every_section_reaches_the_human_half() -> None:
    rendered = artifacts.newest("phase_close_report_md").read_text()
    for title in close.SECTION_TITLES.values():
        assert f"## {title}" in rendered, title
    assert "What this close does and does not claim" in rendered
    assert "Deviations from the protocol as written" in rendered


def test_an_empty_section_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The completeness rule is enforced at assembly, not left to a reviewer."""
    monkeypatch.setattr(close, "_spend_line", lambda root: {})
    with pytest.raises(ValueError, match="empty section"):
        close.assemble(close.REPO_ROOT)


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


def test_the_committed_report_is_closing_and_says_why() -> None:
    document = json.loads(artifacts.newest("phase_close_report").read_text())
    assert document["closing"] is True
    assert document["closing_reason"].strip()
    assert set(document["runs_in_scope"]) == {
        close.CANNED_SWEEP_ARCHIVE,
        *(leg.archive_id for leg in close.LIVE_LEGS),
    }
    assert "CLOSING" in artifacts.newest("phase_close_report_md").read_text()


# --------------------------------------------------------------------------
# The leak hunt
# --------------------------------------------------------------------------


def test_the_gating_grep_is_reproducible_and_still_finds_nothing() -> None:
    """The committed command is re-run here, against the committed evidence.

    A grep whose output is pasted into a document is a claim; a grep the test
    suite re-runs is a check. If a later commit puts ``chaos`` into a
    trajectory this phase produced, this goes red.
    """
    document = json.loads(artifacts.newest("phase_close_report").read_text())
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


def test_nothing_the_agent_read_from_the_platform_carries_an_unadjudicated_term() -> None:
    """WP-1.5's acceptance, claimed here rather than in WO-R3-187."""
    hunt = json.loads(artifacts.newest("phase_close_report").read_text())["sections"]["leak_hunt"]
    assert hunt["verdict"] == "PASS"
    assert hunt["unadjudicated_hits"] == []
    assert hunt["trajectories"]["files_searched"] == 45
    read = hunt["trajectories"]["platform_responses_the_agent_read"]
    assert "chaos" not in read
    for term in read:
        assert term in hunt["adjudications"], term


def test_the_chaos_mentions_are_ours_and_the_harness_s_and_none_are_the_platform_s() -> None:
    """The point of the classification: which side of the boundary each hit is on."""
    hunt = json.loads(artifacts.newest("phase_close_report").read_text())["sections"]["leak_hunt"]
    totals = hunt["traces_by_author"]["totals"]
    assert totals["platform_response"].get("chaos", 0) == 0
    assert totals["commander_prompt"]["chaos"] > 0  # WO-R3-255, our own prompt
    assert totals["harness_record"]["chaos"] > 0  # the evaluator's own setup record
    for run in hunt["traces_by_author"]["per_run"]:
        assert run["platform_response"].get("chaos", 0) == 0, run["archive"]


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


def test_the_sweep_section_matches_the_committed_archives() -> None:
    document = json.loads(artifacts.newest("phase_close_report").read_text())
    sweep = document["sections"]["sweep_results"]
    raw = json.loads(
        (close.archive_dir(close.REPO_ROOT, close.CANNED_SWEEP_ARCHIVE) / "report.json").read_text()
    )
    assert sweep["canned_sweep"]["total"] == raw["total"] == 41
    assert sweep["canned_sweep"]["passed"] == raw["passed"] == 41
    assert sweep["canned_sweep"]["model_roles"] == [ModelRole.BENCHMARK.value]
    assert "no changes vs baseline" in sweep["canned_sweep"]["gate_lines"]
    assert sweep["live_total"] == 4
    assert sweep["live_passed"] == 3


def test_the_gate_crossing_audit_covers_every_handoff_including_the_red_run_s_none() -> None:
    audit = json.loads(artifacts.newest("phase_close_report").read_text())["sections"][
        "gate_crossing_audit"
    ]
    assert audit["bar"] == close.REMEDIATE_BAR == 0.7
    by_archive = {run["archive"]: run for run in audit["runs"]}
    assert set(by_archive) == {leg.archive_id for leg in close.LIVE_LEGS}

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


def test_the_budget_section_reports_counts_and_says_why_the_diff_is_null() -> None:
    budget = json.loads(artifacts.newest("phase_close_report").read_text())["sections"][
        "budget_profile_diff"
    ]
    assert budget["diff"] == "null"
    assert "O-14" in budget["why_null"]
    assert budget["budget_trips"] == []
    assert len(budget["per_run"]) == len(close.LIVE_LEGS)
    for row in budget["per_run"]:
        assert row["llm_calls_total"] == sum(row["llm_calls_by_role"].values())
        assert row["agent_tool_calls_billed"] <= row["max_tool_calls"]
        assert row["tokens_used"] < row["max_tokens"]


def test_no_judge_calibration_was_owed_and_the_prompts_are_hashed() -> None:
    judge = json.loads(artifacts.newest("phase_close_report").read_text())["sections"][
        "judge_calibration"
    ]
    assert judge["reruns_required"] == 0
    assert set(judge["judge_prompts_unchanged"]) == {"briefing_judge.md", "verification_judge.md"}
    assert judge["judge_model"] == "claude-haiku-4-5"
    assert judge["why"].strip() and judge["evidence"].strip()


def test_the_baseline_delta_cites_the_phase_0_artifact_by_id_and_finds_no_movement() -> None:
    delta = json.loads(artifacts.newest("phase_close_report").read_text())["sections"][
        "baseline_delta"
    ]
    assert delta["phase0_baseline_cited_by_id"] == close.PHASE0_BASELINE_CITED
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
        assert comparison["row_level_differences"] == []
    assert delta["unexplained_movement"] == []


def test_the_spend_line_is_the_archives_own_ledgers_not_an_estimate() -> None:
    spend = json.loads(artifacts.newest("phase_close_report").read_text())["sections"]["spend_line"]
    total = Decimal("0")
    for row in spend["live_runs"]:
        raw = json.loads(
            (close.archive_dir(close.REPO_ROOT, row["archive"]) / "report.json").read_text()
        )
        ledger = raw["outcomes"][0]["provenance"]["budget"]
        assert row["usd"] == ledger["usd_used"]
        assert row["agent_loop_wall_seconds"] == round(ledger["wall_seconds_used"], 3)
        assert row["tokens"] == ledger["tokens_used"]
        assert row["start_to_finish_seconds"] >= row["agent_loop_wall_seconds"]
        total += Decimal(str(row["usd"]))
    assert Decimal(spend["live_total_usd"]) == total
    assert spend["canned_sweep"]["usd"] == "0.000000"
    assert len(spend["against_plan_03_section_11"]) == 3


# --------------------------------------------------------------------------
# The whole document
# --------------------------------------------------------------------------


def test_the_committed_report_regenerates_byte_for_byte() -> None:
    """Every input is committed, so the document is a function of the repo.

    If this fails, something the report READ has changed — a judge prompt, the
    blessed baseline, an archive. That is worth a look rather than a re-write:
    the report's claims are about the state those inputs were in.
    """
    document = close.assemble(close.REPO_ROOT)
    assert close.render_json(document) == artifacts.newest("phase_close_report").read_text()
    assert close.render_markdown(document) == artifacts.newest("phase_close_report_md").read_text()


def test_the_report_states_the_reduction_and_its_open_follow_ups() -> None:
    document = json.loads(artifacts.newest("phase_close_report").read_text())
    rendered = artifacts.newest("phase_close_report_md").read_text()
    assert {entry["id"] for entry in document["deviations"]} >= {"D1", "D2", "D3", "D4"}
    assert "O-15" in rendered and "O-14" in rendered
    assert {entry["id"] for entry in document["follow_ups"]} == {
        "WO-R3-253",
        "WO-R3-255",
        "O-8",
    }
    assert document["incidents_row_filed"] is False
    assert document["incidents_row_reason"].strip()
    assert document["claims"]["does_claim"] and document["claims"]["does_not_claim"]
