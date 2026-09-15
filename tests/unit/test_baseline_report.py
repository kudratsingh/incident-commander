"""Baseline counts come from archives, and the ledger cannot silently lose debt."""

from __future__ import annotations

import json
import shutil
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest

from evals import baseline_report as baseline
from evals.runner import RunReport


@pytest.mark.parametrize("archive_id", baseline.ARCHIVE_IDS)
def test_each_named_archive_exists_and_recorded_numbers_are_preserved(archive_id: str) -> None:
    path = baseline.REPO_ROOT / "evals/runs" / archive_id / "report.json"
    raw = json.loads(path.read_text())
    result = baseline.summarize_report(path)
    assert result["invocation_id"] == archive_id
    assert json.dumps(result["recorded_numbers"], sort_keys=True) == json.dumps(
        {key: raw.get(key) for key in result["recorded_numbers"]},
        sort_keys=True,
    )
    for recorded, assembled in zip(raw["outcomes"], result["scenarios"], strict=True):
        assert assembled["tool_calls_used"] == recorded["tool_calls_used"]
        assert assembled["final_state"] == recorded["final_state"]
        assert assembled["recorded_grade"] == recorded["report"]


def test_perturbing_fixture_changes_computed_counts(tmp_path: Path) -> None:
    source = baseline.REPO_ROOT / "evals/runs" / baseline.REMEDIATION_ARCHIVES[0] / "report.json"
    raw = json.loads(source.read_text())
    path = tmp_path / "report.json"
    path.write_text(json.dumps(raw))
    before = baseline.summarize_report(path)
    raw["outcomes"][0]["tool_calls_used"] = 12
    raw["outcomes"][0]["report"]["dimensions"][0]["passed"] = False
    raw["outcomes"][0]["report"]["passed"] = False
    raw["passed"], raw["failed"] = 0, 1
    path.write_text(json.dumps(raw))
    after = baseline.summarize_report(path)
    assert before["pass_rate"] == 1 and after["pass_rate"] == 0
    assert after["tool_calls"] == {"12": 1}
    assert after["dimensions"]["outcome"]["passed"] == 0


def test_all_eight_rows_have_verdicts_and_held_rows_stay_open() -> None:
    rows = baseline.read_debt_walk(baseline.REPO_ROOT / "docs/eval-debt.md")
    assert len(rows) == 8
    assert {row.pr for row in rows if row.disposition == "open"} >= {"#102", "#112"}
    row7 = rows[6]
    assert "VERIFY" in row7.observable and "RESOLVED is superseded" in row7.observable
    assert "2988f414afb4" in row7.evidence


@pytest.mark.parametrize("defect", ["missing", "invalid", "duplicate"])
def test_incomplete_or_invalid_walk_is_rejected(tmp_path: Path, defect: str) -> None:
    text = (baseline.REPO_ROOT / "docs/eval-debt.md").read_text()
    line = next(line for line in text.splitlines() if line.startswith("| 4 | #102 |"))
    text = text.replace(
        line,
        {
            "missing": "",
            "invalid": line.replace("open", "probably"),
            "duplicate": line + "\n" + line,
        }[defect],
    )
    path = tmp_path / "debt.md"
    path.write_text(text)
    with pytest.raises(ValueError):
        baseline.read_debt_walk(path)


@pytest.mark.parametrize("target", ["eval-reg", "eval-live", "eval-smoke"])
@pytest.mark.parametrize("role", [None, "benchmark"])
def test_make_targets_pass_model_role(tmp_path: Path, target: str, role: str | None) -> None:
    # Copy only the Makefile; never expand the developer's real .env in a dry run.
    shutil.copy(baseline.REPO_ROOT / "Makefile", tmp_path)
    command = ["make", "-n", target, "PLATFORM_SMOKE_TOKEN=test-only"]
    if target == "eval-live":
        command.append("ONLY=example")
    if role:
        command.append(f"MODEL_ROLE={role}")
    result = subprocess.run(command, cwd=tmp_path, text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    lines = [line for line in result.stdout.splitlines() if "python -m evals.runner" in line]
    assert len(lines) == 1
    assert f'--model-role "{role or "development"}"' in lines[0]


def _canned_full_report(invocation_id: str = "fixture00cafe") -> RunReport:
    """A full canned report over the current corpus, stamped like a real run.

    Built from source rather than copied from an archive so that a scenario
    added tomorrow is covered without anyone re-recording a fixture.
    """
    from datetime import UTC, datetime

    from evals.graders.deterministic import GradeReport
    from evals.runner import ExecutionMode, RunProvenance, RunReport, ScenarioOutcome
    from evals.scenarios.loader import load_scenarios
    from incident_commander.agent.state import BudgetLedger
    from incident_commander.config import ModelRole

    scenarios = load_scenarios(baseline.REPO_ROOT / "evals/scenarios")
    when = datetime(2026, 9, 15, tzinfo=UTC)
    return RunReport(
        generated_at=when,
        total=len(scenarios),
        passed=len(scenarios),
        failed=0,
        invocation_id=invocation_id,
        outcomes=tuple(
            ScenarioOutcome(
                scenario=s.name,
                final_state=s.expectation.expected_terminal_state,
                tool_calls_used=0,
                report=GradeReport(scenario=s.name, passed=True, dimensions=()),
                provenance=RunProvenance(
                    commander_revision="0" * 40,
                    platform_image_digest="sha256:" + "1" * 64,
                    agent_model="claude-sonnet-4-6",
                    model_role=ModelRole.DEVELOPMENT,
                    judge_model="claude-haiku-4-5",
                    strategy="baseline",
                    scenario=s.name,
                    invocation_id=invocation_id,
                    recorded_at=when,
                    execution_mode=ExecutionMode.CANNED,
                    budget=BudgetLedger(
                        max_tool_calls=10,
                        max_tokens=1,
                        max_wall_seconds=1,
                        max_usd=Decimal("1"),
                    ),
                ),
            )
            for s in scenarios
        ),
    )


def test_rendering_is_deterministic_and_provenance_is_not_borrowed(tmp_path: Path) -> None:
    offline = tmp_path / "report.json"
    offline.write_text(_canned_full_report().model_dump_json())
    document = baseline.assemble(baseline.REPO_ROOT, offline)
    assert baseline.render_json(document) == baseline.render_json(
        baseline.assemble(baseline.REPO_ROOT, offline)
    )
    assert document["closing"] is False
    # The nine live archives predate provenance stamping; nothing invents one.
    assert all(
        row["provenance"] is None
        for source in document["historical_sources"]
        for row in source["scenarios"]
    )
    rendered = baseline.render_markdown(document)
    for name in baseline.NEVER_RUN_LIVE:
        assert name in rendered
    assert "REAL agent finding" in rendered
    assert "Zero live invocations" in rendered


def test_every_provenance_field_is_stamped_and_answered(tmp_path: Path) -> None:
    """Requirement 9: a baseline that cannot name its model is not a baseline."""
    offline = tmp_path / "report.json"
    offline.write_text(_canned_full_report().model_dump_json())
    stamp = baseline.assemble(baseline.REPO_ROOT, offline)["provenance"]
    for field in baseline.PROVENANCE_FIELDS:
        if field in ("scenario", "budget"):
            continue
        assert field in stamp, field
        assert str(stamp[field]).strip().lower() not in baseline._PLACEHOLDERS, field
    # The seeded and used budgets are kept per scenario, not collapsed.
    assert {row["scenario"] for row in stamp["budgets_seeded_and_used"]} == {
        row["scenario"]
        for row in baseline.assemble(baseline.REPO_ROOT, offline)["offline_source"]["scenarios"]
    }


@pytest.mark.parametrize("field", ["commander_revision", "platform_image_digest", "agent_model"])
def test_a_placeholder_provenance_field_is_refused(tmp_path: Path, field: str) -> None:
    raw = json.loads(_canned_full_report().model_dump_json())
    for outcome in raw["outcomes"]:
        outcome["provenance"][field] = "unknown"
    offline = tmp_path / "report.json"
    offline.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="unanswered"):
        baseline.stamp_of(offline)


def test_two_models_in_one_report_are_refused(tmp_path: Path) -> None:
    raw = json.loads(_canned_full_report().model_dump_json())
    raw["outcomes"][1]["provenance"]["agent_model"] = "some-other-model"
    offline = tmp_path / "report.json"
    offline.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="disagree"):
        baseline.stamp_of(offline)


def test_an_unstamped_report_cannot_become_a_baseline(tmp_path: Path) -> None:
    raw = json.loads(_canned_full_report().model_dump_json())
    for outcome in raw["outcomes"]:
        outcome["provenance"] = None
    offline = tmp_path / "report.json"
    offline.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="provenance record"):
        baseline.assemble(baseline.REPO_ROOT, offline)


def test_the_three_never_run_scenarios_are_excluded_with_a_reason(tmp_path: Path) -> None:
    """Requirement 10."""
    offline = tmp_path / "report.json"
    offline.write_text(_canned_full_report().model_dump_json())
    exclusions = baseline.assemble(baseline.REPO_ROOT, offline)["exclusions"]
    for name in ("dlq_mislabeled_replay_safe", "saga_stuck", "dlq_mixed_partial"):
        entry = next(e for e in exclusions if e.get("scenario") == name)
        assert entry["reason"].strip()
        assert "never run live" in entry["reason"]


def test_the_committed_baseline_resolves_through_the_artifact_resolver() -> None:
    """Requirement 11: one registered kind, one resolver, no globbing."""
    from evals import artifacts

    for kind in ("baseline_report", "baseline_report_md"):
        assert kind in artifacts.KINDS
        path = artifacts.newest(kind)
        assert path.is_file()
        assert path.parent == baseline.REPO_ROOT / "evals/reports"


def test_the_committed_baseline_regenerates_byte_for_byte() -> None:
    """Requirement 8: the committed report is a function of committed archives.

    Nothing is compared against a recorded expectation: the assembler is run
    again over the same inputs and the bytes on disk must come back out. The
    offline leg is itself a committed archive, so every input to the
    regeneration is in the repository.
    """
    from evals import artifacts

    committed = artifacts.newest("baseline_report")
    document = json.loads(committed.read_text())
    offline_id = document["provenance"]["invocation_id"]
    offline_path = baseline.REPO_ROOT / "evals/runs" / offline_id / "report.json"
    assert offline_path.is_file(), "the baseline's offline leg must be a committed archive"
    rebuilt = baseline.assemble(baseline.REPO_ROOT, offline_path)
    assert baseline.render_json(rebuilt) == committed.read_text()
    assert baseline.render_markdown(rebuilt) == artifacts.newest("baseline_report_md").read_text()


def test_the_baseline_names_all_nine_archives_and_says_no_to_make_baseline() -> None:
    from evals import artifacts

    rendered = artifacts.newest("baseline_report_md").read_text()
    for archive_id in baseline.ARCHIVE_IDS:
        assert archive_id in rendered, archive_id
    assert "e8404306138c" in rendered
    document = json.loads(artifacts.newest("baseline_report").read_text())
    assert document["runs_make_baseline"] is False
    assert document["runs_make_baseline_reason"].strip()


def test_writing_twice_refuses_rather_than_replacing(tmp_path: Path) -> None:
    """Invariant 9 at the filesystem: a second write raises, never overwrites."""
    offline = tmp_path / "report.json"
    offline.write_text(_canned_full_report().model_dump_json())
    document = baseline.assemble(baseline.REPO_ROOT, offline)
    first = baseline.write(document, root=tmp_path)
    assert all(path.is_file() for path in first)
    with pytest.raises(FileExistsError):
        baseline.write(document, root=tmp_path)
