"""The offline re-grade of a locked archive (WO-R3-265, INC-003).

A paid archive is evidence and is never rewritten (CLAUDE.md invariant 9,
ADR 0021), but a grading rule that was wrong when the archive was written
leaves seven false reds sitting in it. `scripts/regrade_archive.py` is the
answer to both facts at once: it reads the archive, re-grades every row from
that run's own trajectories under TODAY's rules, and writes a NEW versioned
report beside it. Nothing in the archive is touched, and the re-grade is a
document about the archive rather than a replacement for it.

What is asserted here:

* the verdict actually moves where INC-003 says it should — a live row that
  seeded no fault stops being graded on a canned-world label;
* it is not a whitewash: a canned row's red is still red, and a row that
  fails another dimension still fails;
* the archive is byte-identical afterwards, checked by digest rather than by
  reading the code and believing it;
* the output is versioned through `evals/artifacts.py`, so a second re-grade
  of the same archive cannot overwrite the first.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from evals import artifacts
from evals.graders.deterministic import DimensionResult, GradeDimension, GradeReport
from evals.graders.root_cause import NOT_GRADED_PREFIX
from evals.runner import ChaosHookRecord, RunReport, ScenarioOutcome, Trajectory
from incident_commander.agent.hypothesis import Hypothesis, HypothesisCategory
from incident_commander.agent.state import (
    BudgetLedger,
    EvidenceEntry,
    IncidentState,
    RunState,
)
from scripts import regrade_archive

_SCENARIOS_DIR = Path(__file__).resolve().parents[2] / "evals" / "scenarios"
_ARCHIVE_ID = "0123456789ab"
_RAN_AT = datetime(2026, 9, 17, 13, 38, 24, tzinfo=UTC)

#: The reading `postgres_slow`'s evidence expectations are written against.
#: Values are the LIVE ones the read-only pass saw — a healthy database — so
#: the row's evidence dimension passes and ROOT_CAUSE is the only thing the
#: re-grade can move.
_HEALTHY_POSTGRES = json.dumps(
    {
        "ok": True,
        "ping_latency_ms": 1.6,
        "active_connections": 1,
        "dialect": "postgresql",
        "error": None,
    }
)


def _run(
    *,
    diagnosis: HypothesisCategory,
    evidence: tuple[EvidenceEntry, ...],
    state: IncidentState = IncidentState.ESCALATED,
) -> RunState:
    return RunState(
        incident_id=uuid4(),
        state=state,
        alert={"source": "platform.db", "severity": "critical"},
        budget=BudgetLedger(
            max_tool_calls=5,
            max_tokens=200_000,
            max_wall_seconds=1_800,
            max_usd=Decimal("5.00"),
            tool_calls_used=1,
        ),
        evidence=evidence,
        hypotheses=(
            Hypothesis(
                category=diagnosis,
                name=diagnosis.value,
                confidence=0.8,
                reasoning="fixture",
            ),
        ),
        created_at=_RAN_AT,
        updated_at=_RAN_AT,
    )


def _dimensions(root_cause: DimensionResult) -> tuple[DimensionResult, ...]:
    """The five dimensions a passing read-only row carried, plus ROOT_CAUSE."""
    return (
        DimensionResult(
            dimension=GradeDimension.OUTCOME,
            passed=True,
            detail="terminal state escalated matched expectation",
        ),
        DimensionResult(
            dimension=GradeDimension.EVIDENCE,
            passed=True,
            detail="all 2 evidence field assertion(s) satisfied",
        ),
        DimensionResult(
            dimension=GradeDimension.BUDGET, passed=True, detail="used 1 tool calls, cap 5"
        ),
        DimensionResult(
            dimension=GradeDimension.ACTION, passed=True, detail="no action expectation set"
        ),
        DimensionResult(
            dimension=GradeDimension.SAFETY, passed=True, detail="no safety expectations set"
        ),
        root_cause,
    )


def _archive(
    tmp_path: Path,
    *,
    live_mcp: bool = True,
    chaos_hooks: tuple[ChaosHookRecord, ...] = (),
    diagnosis: HypothesisCategory = HypothesisCategory.NO_FAULT,
) -> Path:
    """One archived `postgres_slow` row, in the shape `0db6fe722f7c` holds it."""
    runs_dir = tmp_path / "evals" / "runs"
    target = runs_dir / _ARCHIVE_ID
    (target / "trajectories").mkdir(parents=True)
    run = _run(
        diagnosis=diagnosis,
        evidence=(
            EvidenceEntry(
                tool_name="get_postgres_health",
                arguments={},
                result_summary=_HEALTHY_POSTGRES,
                timestamp=_RAN_AT,
            ),
        ),
    )
    archived_root_cause = DimensionResult(
        dimension=GradeDimension.ROOT_CAUSE,
        passed=False,
        detail=(
            f"diagnosed {diagnosis.value}; ground truth db_query_latency — not the "
            "declared cause (precision 0.00, recall 0.00, F1 0.00)"
        ),
    )
    report = RunReport(
        generated_at=_RAN_AT,
        total=1,
        passed=0,
        failed=1,
        invocation_id=_ARCHIVE_ID,
        outcomes=(
            ScenarioOutcome(
                scenario="postgres_slow",
                final_state=IncidentState.ESCALATED,
                tool_calls_used=1,
                live_mcp=live_mcp,
                live_llm=live_mcp,
                chaos_hooks=chaos_hooks,
                report=GradeReport(
                    scenario="postgres_slow",
                    passed=False,
                    dimensions=_dimensions(archived_root_cause),
                ),
            ),
        ),
    )
    (target / "report.json").write_text(report.model_dump_json(indent=2))
    (target / "trajectories" / "postgres_slow.json").write_text(
        Trajectory(
            scenario="postgres_slow",
            incident_id=str(run.incident_id),
            checkpoints=(run,),
            invocation_id=_ARCHIVE_ID,
        ).model_dump_json(indent=2)
    )
    return runs_dir


def _digest(directory: Path) -> dict[str, str]:
    return {
        path.relative_to(directory).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def _regrade(runs_dir: Path) -> dict[str, Any]:
    return regrade_archive.regrade(_ARCHIVE_ID, runs_dir=runs_dir, scenarios_dir=_SCENARIOS_DIR)


class TestTheLabelsWorldDecidesTheVerdict:
    def test_a_live_unseeded_row_stops_being_graded(self, tmp_path: Path) -> None:
        """`postgres_slow` in `0db6fe722f7c`, reproduced end to end."""
        document = _regrade(_archive(tmp_path))
        row = document["scenarios"][0]
        assert row["archived"]["passed"] is False
        assert row["regraded"]["passed"] is True
        assert row["world"] == "live, no fault seeded"
        root_cause = row["dimensions"]["root_cause"]
        assert root_cause["changed"] is True
        assert root_cause["regraded"]["detail"].startswith("not graded:")

    def test_a_live_row_that_seeded_its_fault_is_still_graded(self, tmp_path: Path) -> None:
        """The three remediation scenarios keep their verdict, red or green."""
        runs_dir = _archive(
            tmp_path,
            chaos_hooks=(ChaosHookRecord(phase="setup", name="inject_db_latency"),),
        )
        row = _regrade(runs_dir)["scenarios"][0]
        assert row["world"] == "live, fault seeded"
        assert row["regraded"]["passed"] is False
        assert row["dimensions"]["root_cause"]["changed"] is False

    def test_a_canned_row_keeps_its_red(self, tmp_path: Path) -> None:
        """Canned fixtures ARE the label's world; the re-grade is not an amnesty."""
        row = _regrade(_archive(tmp_path, live_mcp=False))["scenarios"][0]
        assert row["world"] == "canned"
        assert row["regraded"]["passed"] is False

    def test_the_totals_say_how_much_moved(self, tmp_path: Path) -> None:
        document = _regrade(_archive(tmp_path))
        assert document["totals"] == {
            "scenarios": 1,
            "archived_passed": 0,
            "regraded_passed": 1,
            "verdicts_changed": 1,
        }
        assert document["root_cause"]["regraded"]["graded"] == 0
        assert document["root_cause"]["regraded"]["not_graded_world"] == 1
        assert document["root_cause"]["archived"]["graded"] == 1


class TestTheArchiveIsNotTouched:
    def test_every_file_is_byte_identical_afterwards(self, tmp_path: Path) -> None:
        runs_dir = _archive(tmp_path)
        before = _digest(runs_dir / _ARCHIVE_ID)
        _regrade(runs_dir)
        assert _digest(runs_dir / _ARCHIVE_ID) == before

    def test_the_document_carries_the_digest_it_read(self, tmp_path: Path) -> None:
        """So a reader can check the claim instead of taking it on trust."""
        runs_dir = _archive(tmp_path)
        document = _regrade(runs_dir)
        assert document["archive_digest"] == _digest(runs_dir / _ARCHIVE_ID)

    def test_a_changed_archive_under_the_re_grade_is_refused(self, tmp_path: Path) -> None:
        """The check is real: it fires when the bytes move."""
        runs_dir = _archive(tmp_path)
        document = _regrade(runs_dir)
        (runs_dir / _ARCHIVE_ID / "report.json").write_text("{}")
        with pytest.raises(regrade_archive.ArchiveChanged):
            regrade_archive.verify_unchanged(
                runs_dir / _ARCHIVE_ID,
                document["archive_digest"],
            )


class TestTheReportIsVersionedEvidence:
    def test_it_writes_the_pair_under_the_reports_tree(self, tmp_path: Path) -> None:
        runs_dir = _archive(tmp_path)
        document = _regrade(runs_dir)
        json_path, md_path = regrade_archive.write(document, root=tmp_path)
        assert json_path.parent == tmp_path / "evals" / "reports" / "regrades"
        assert json_path.name.startswith(f"regrade_report.20260917T133824Z.{_ARCHIVE_ID}")
        assert json.loads(json_path.read_text())["archive"] == _ARCHIVE_ID
        assert f"# Re-grade of `{_ARCHIVE_ID}`" in md_path.read_text()

    def test_it_resolves_as_the_newest_of_its_kind(self, tmp_path: Path) -> None:
        document = _regrade(_archive(tmp_path))
        json_path, _ = regrade_archive.write(document, root=tmp_path)
        assert artifacts.newest("regrade_report", root=tmp_path) == json_path

    def test_re_running_it_refuses_rather_than_replacing(self, tmp_path: Path) -> None:
        """Invariant 9 on the output as well as on the input."""
        document = _regrade(_archive(tmp_path))
        regrade_archive.write(document, root=tmp_path)
        with pytest.raises(FileExistsError):
            regrade_archive.write(document, root=tmp_path)

    def test_the_document_is_a_function_of_the_archive_alone(self, tmp_path: Path) -> None:
        """No clock, no environment: two re-grades produce the same bytes."""
        runs_dir = _archive(tmp_path)
        assert regrade_archive.render_json(_regrade(runs_dir)) == regrade_archive.render_json(
            _regrade(runs_dir)
        )


class TestTheCommittedReGradeOfThePaidArchive:
    """The real one: `0db6fe722f7c`, the $2.15 read-only pass, INC-003's subject.

    The archive is committed and locked (cmd #262), and the re-grade of it is
    committed beside it, so both halves of the claim are checkable here rather
    than only in a PR body: the document regenerates byte for byte from the
    archive, and its headline numbers are the ones the incident record and the
    work order quote.
    """

    _PAID = "0db6fe722f7c"

    def _document(self) -> dict[str, Any]:
        return regrade_archive.regrade(self._PAID)

    def _committed(self, document: dict[str, Any]) -> Path:
        return artifacts.version_path(
            "regrade_report",
            timestamp=datetime.fromisoformat(str(document["archive_generated_at"])),
            invocation_id=self._PAID,
        )

    def test_the_committed_report_regenerates_byte_for_byte(self) -> None:
        """Anybody can re-derive it; nothing here was typed in.

        One thing it quotes is not frozen, and WO-R3-263 is where that showed
        up: the re-grade applies TODAY's rules, so it also quotes TODAY's
        ground-truth labels, and the corpus may re-decide one. It did —
        ``trace_investigation``'s world was labelled ``unknown`` because the
        taxonomy had no member for a worker running out of memory, and O-19
        added ``resource_exhaustion``. The row it appears on is one of the
        sixteen ADR 0040 does NOT grade (a live run that seeded no fault), so
        the label moved inside a detail string that says the label was held
        back — the document's verdicts, totals and every other line are
        unchanged.

        So the comparison allows a ROOT_CAUSE detail whose only difference is
        the label it names after "ground truth ", and nothing else. A moved
        verdict, a moved count or a moved archive digest still fails, and the
        failure prints the line. Same reasoning as
        ``test_phase_close_report.py::_ASSEMBLY_TIME_FACTS``; the alternative —
        rewriting a committed document to match today's corpus — is the one
        thing invariant 9 forbids.
        """
        document = self._document()
        committed = self._committed(document).read_text()
        regenerated = regrade_archive.render_json(document)
        changed = [
            line
            for line in difflib.unified_diff(
                committed.splitlines(), regenerated.splitlines(), lineterm="", n=0
            )
            if line[:1] in {"+", "-"} and not line.startswith(("+++", "---"))
        ]
        labels = "|".join(category.value for category in HypothesisCategory)
        allowed = re.compile(
            rf'^\s*"detail": "{re.escape(NOT_GRADED_PREFIX)}.*'
            rf"ground truth (?:{labels})(?:, (?:{labels}))*\",?$"
        )
        unexplained = [line for line in changed if not allowed.match(line[1:])]
        assert not unexplained, (
            "the committed re-grade no longer regenerates, and the change is "
            "not a re-decided ground-truth label on a not-graded row:\n  "
            + "\n  ".join(unexplained)
            + "\n\nRead what moved before touching anything: a verdict, a total "
            "or an archive digest moving means something has rewritten evidence."
        )

    def test_the_numbers_INC_003_is_closed_on(self) -> None:
        document = self._document()
        assert document["totals"] == {
            "scenarios": 27,
            "archived_passed": 20,
            "regraded_passed": 26,
            "verdicts_changed": 6,
        }
        assert document["root_cause"]["archived"]["graded"] == 18
        assert document["root_cause"]["archived"]["correct"] == 11
        assert document["root_cause"]["regraded"]["graded"] == 2
        assert document["root_cause"]["regraded"]["correct"] == 2
        assert document["root_cause"]["regraded"]["not_graded_world"] == 16

    def test_the_seven_false_reds_were_all_root_cause_and_six_of_them_move(self) -> None:
        """The seventh is a different bug, and it is still red on purpose."""
        document = self._document()
        moved = sorted(row["scenario"] for row in document["scenarios"] if row["changed"])
        assert moved == [
            "consumer_lag_null_unknown_state",
            "failed_traces_scan",
            "incidents_overview",
            "postgres_slow",
            "redis_saturation",
            "trace_investigation",
        ]
        assert document["still_failing"] == [
            {"scenario": "consumer_lag_missing_group", "dimensions": ["evidence"]}
        ]

    def test_nothing_but_root_cause_moved_on_any_row(self) -> None:
        """Why the re-grade is the runner's grade and not a second opinion.

        If any other dimension moved, the re-grader would be scoring the run
        differently from the runner that produced it, and the number it
        reports would be about the re-grader.
        """
        document = self._document()
        moved = [
            (row["scenario"], name)
            for row in document["scenarios"]
            for name, dimension in row["dimensions"].items()
            if name != "root_cause" and dimension["changed"]
        ]
        assert moved == []
        assert document["not_regraded"] == []

    def test_every_live_row_of_the_pass_is_the_unseeded_world(self) -> None:
        """The smoke pass seeds nothing by construction; the archive shows it."""
        document = self._document()
        assert {row["world"] for row in document["scenarios"]} == {
            "canned",
            "live, no fault seeded",
        }


class TestTheCommandLine:
    def test_it_prints_the_summary_and_writes_nothing_by_default(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        runs_dir = _archive(tmp_path)
        code = regrade_archive.main(
            [_ARCHIVE_ID, "--runs-dir", str(runs_dir), "--scenarios-dir", str(_SCENARIOS_DIR)]
        )
        assert code == 0
        assert not (tmp_path / "evals" / "reports").exists()
        out = capsys.readouterr().out
        assert "postgres_slow" in out
        assert "1 verdict(s) changed" in out

    def test_an_unknown_archive_is_an_error_not_a_traceback(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = regrade_archive.main(
            ["nosucharchive", "--runs-dir", str(tmp_path), "--scenarios-dir", str(_SCENARIOS_DIR)]
        )
        assert code == 1
        assert "nosucharchive" in capsys.readouterr().err
