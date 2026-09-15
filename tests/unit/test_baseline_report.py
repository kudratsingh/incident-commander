"""Baseline counts come from archives, and the ledger cannot silently lose debt."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from evals import baseline_report as baseline
from evals.runner import _eval_defaults


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


def test_rendering_is_deterministic_and_config_is_not_historical(tmp_path: Path) -> None:
    # A full canned fixture report from source, not another eval invocation.
    from datetime import UTC, datetime

    from evals.graders.deterministic import GradeReport
    from evals.runner import RunReport, ScenarioOutcome
    from evals.scenarios.loader import load_scenarios

    scenarios = load_scenarios(baseline.REPO_ROOT / "evals/scenarios")
    report = RunReport(
        generated_at=datetime(2026, 9, 15, tzinfo=UTC),
        total=len(scenarios),
        passed=len(scenarios),
        failed=0,
        invocation_id="fixture",
        outcomes=tuple(
            ScenarioOutcome(
                scenario=s.name,
                final_state=s.expectation.expected_terminal_state,
                tool_calls_used=0,
                report=GradeReport(scenario=s.name, passed=True, dimensions=()),
            )
            for s in scenarios
        ),
    )
    offline = tmp_path / "report.json"
    offline.write_text(report.model_dump_json())
    document = baseline.assemble(baseline.REPO_ROOT, offline, _eval_defaults())
    assert baseline.render_json(document) == baseline.render_json(
        baseline.assemble(baseline.REPO_ROOT, offline, _eval_defaults())
    )
    assert document["closing"] is False
    assert all(
        row["provenance"] is None
        for source in document["historical_sources"]
        for row in source["scenarios"]
    )
    rendered = baseline.render_markdown(document)
    for name in baseline.NEVER_RUN_LIVE:
        assert name in rendered
    assert "REAL agent finding" in rendered
    assert "not historical provenance" in rendered
