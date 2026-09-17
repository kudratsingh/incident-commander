#!/usr/bin/env python3
"""Re-grade one locked run archive from its own trajectories. Never touches it.

A run archive is evidence: read-only on disk, `uchg` where the filesystem
supports it, never rewritten (CLAUDE.md invariant 9, ADR 0021). That rule is
what makes an archive worth anything, and it is also why a grading rule that
was wrong when the archive was written cannot be fixed in place.

INC-003 is the case this exists for. The paid read-only pass `0db6fe722f7c`
($2.15, 27 scenarios) failed seven of them on ROOT_CAUSE against ground-truth
labels that were read off each scenario's CANNED fixtures, while the pass ran
against the UNSEEDED live stack — `postgres_slow` was graded wrong for saying
`no_fault` about a database that answered in 1.6 ms. WO-R3-265 scoped the
label to the world it describes. This script says what that archive's numbers
are under the fixed rule, and writes the answer as a NEW versioned report
beside the untouched original.

Three properties, each load-bearing:

**It re-grades, it does not re-run.** Every input is already in the archive:
the final `RunState` is the last checkpoint of that scenario's trajectory, the
briefing is the archived briefing, and the world the run was in is
`ScenarioOutcome.live_mcp` plus its `chaos_hooks`. Nothing is replayed, no
model is called, no platform is touched, and nothing is spent.

**The rule is imported, never restated.** `label_describes_this_world` and
`grade()` are the same functions `evals/runner.py` calls, so a re-grade is the
grade the runner would give today — a second copy of the rule here would be a
second definition of what a correct diagnosis is.

**The archive is verified unchanged.** Every file is sha256'd before the
re-grade and again after, and the digests go into the document, so "nothing
was touched" is something a reader can check rather than a claim to trust.

Usage:

    make regrade-archive ARCHIVE=0db6fe722f7c            # print the summary
    make regrade-archive ARCHIVE=0db6fe722f7c WRITE=1    # and persist the pair

The archive may live in another checkout (the one that holds the locked
original): `--runs-dir` points at its `evals/runs/`, while `--root` decides
where the report is written.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from evals import artifacts
from evals.graders.deterministic import (
    GradeDimension,
    GradeReport,
    grade,
    is_vacuous_detail,
)
from evals.graders.root_cause import (
    coverage_over,
    is_not_graded_detail,
    label_describes_this_world,
)
from evals.runner import RunReport, ScenarioOutcome, Trajectory, seeded_chaos
from evals.scenarios.loader import load_scenarios
from evals.scenarios.schema import Scenario
from incident_commander.agent.briefing import EscalationBriefing
from incident_commander.agent.state import RunState

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
RUNS_DIR: Final[Path] = REPO_ROOT / "evals" / "runs"
SCENARIOS_DIR: Final[Path] = REPO_ROOT / "evals" / "scenarios"

#: What the row's world is called in the document. Three values, because
#: there are three worlds a run can be in and the middle one is the whole
#: reason this script exists.
_CANNED: Final[str] = "canned"
_LIVE_SEEDED: Final[str] = "live, fault seeded"
_LIVE_UNSEEDED: Final[str] = "live, no fault seeded"


class ArchiveChanged(RuntimeError):
    """The archive's bytes moved while it was being read."""


class ArchiveNotFound(FileNotFoundError):
    """No archive of that id under the runs directory given."""


def digest_tree(directory: Path) -> dict[str, str]:
    """sha256 of every file under ``directory``, keyed by relative path."""
    return {
        path.relative_to(directory).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def verify_unchanged(directory: Path, expected: dict[str, str]) -> None:
    """Refuse to report a re-grade whose input moved under it."""
    found = digest_tree(directory)
    if found != expected:
        moved = sorted(
            set(expected) ^ set(found)
            | {name for name in set(expected) & set(found) if expected[name] != found[name]}
        )
        raise ArchiveChanged(
            f"{directory} changed while it was being re-graded: {', '.join(moved)}. "
            "An archive is append-only evidence (invariant 9); the re-grade is void."
        )


def read_report(archive: Path) -> RunReport:
    path = archive / "report.json"
    if not path.is_file():
        raise ArchiveNotFound(f"no report.json under {archive}")
    return RunReport.model_validate_json(path.read_text())


def _final_state(archive: Path, scenario: str) -> RunState | None:
    """The last checkpoint of a scenario's archived trajectory.

    The final checkpoint IS the graded run: the runner grades the state the
    loop returned, and the checkpointer's history ends with it. ``None`` when
    the trajectory is absent or empty — a scenario that crashed before its
    first checkpoint cannot be re-graded, and the row says so rather than
    being silently dropped.
    """
    path = archive / "trajectories" / f"{scenario}.json"
    if not path.is_file():
        return None
    checkpoints = Trajectory.model_validate_json(path.read_text()).checkpoints
    return checkpoints[-1] if checkpoints else None


def _briefing(archive: Path, scenario: str) -> EscalationBriefing | None:
    """The archived briefing, for the ``expect_briefing_contains`` assertions."""
    path = archive / "briefings" / f"{scenario}.json"
    if not path.is_file():
        return None
    return EscalationBriefing.model_validate_json(path.read_text())


def world_of(outcome: ScenarioOutcome) -> str:
    """Which of the three worlds this row ran in, as the document names it."""
    if not outcome.live_mcp:
        return _CANNED
    return _LIVE_SEEDED if seeded_chaos(outcome.chaos_hooks) else _LIVE_UNSEEDED


def regrade_outcome(
    outcome: ScenarioOutcome, scenario: Scenario, run: RunState, briefing: EscalationBriefing | None
) -> GradeReport:
    """One row, graded again under today's rules and today's corpus.

    The world fact comes off the ROW (``live_mcp`` + ``chaos_hooks``), never
    off the scenario file: the question is what world that run was actually
    in, and a scenario may have gained or lost a chaos hook since. The label
    itself comes off the corpus, because re-grading under today's rules means
    today's labels.
    """
    return grade(
        run,
        scenario.expectation,
        briefing=briefing,
        ground_truth=(None if scenario.ground_truth is None else scenario.ground_truth.root_causes),
        world_matches_ground_truth=label_describes_this_world(
            live_mcp=outcome.live_mcp,
            chaos_seeded=seeded_chaos(outcome.chaos_hooks),
        ),
    )


def _dimension_rows(archived: GradeReport, regraded: GradeReport) -> dict[str, Any]:
    """Every dimension, both verdicts, and whether it moved."""
    before = {row.dimension.value: row for row in archived.dimensions}
    after = {row.dimension.value: row for row in regraded.dimensions}
    rows: dict[str, Any] = {}
    for name in sorted(set(before) | set(after)):
        old, new = before.get(name), after.get(name)
        rows[name] = {
            "archived": None if old is None else {"passed": old.passed, "detail": old.detail},
            "regraded": None if new is None else {"passed": new.passed, "detail": new.detail},
            "changed": (old is None or new is None)
            or (old.passed, old.detail)
            != (
                new.passed,
                new.detail,
            ),
        }
    return rows


def _coverage(dimensions: Sequence[dict[str, Any]], total: int) -> dict[str, Any]:
    """Root-cause coverage over one side of the comparison."""
    coverage = coverage_over(
        [(not is_vacuous_detail(row["detail"]), bool(row["passed"])) for row in dimensions],
        total=total,
        world_mismatch=sum(1 for row in dimensions if is_not_graded_detail(row["detail"])),
    )
    return {
        "graded": coverage.graded,
        "correct": coverage.correct,
        "not_graded_world": coverage.not_graded_world,
        "accuracy": None if coverage.accuracy is None else round(coverage.accuracy, 4),
        "describe": coverage.describe(),
    }


def regrade(
    archive_id: str,
    *,
    runs_dir: Path = RUNS_DIR,
    scenarios_dir: Path = SCENARIOS_DIR,
) -> dict[str, Any]:
    """Re-grade one archive and return the document. Writes nothing."""
    archive = runs_dir / archive_id
    if not archive.is_dir():
        raise ArchiveNotFound(f"no archive {archive_id!r} under {runs_dir}")
    before = digest_tree(archive)
    report = read_report(archive)
    corpus = {scenario.name: scenario for scenario in load_scenarios(scenarios_dir)}

    rows: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for outcome in report.outcomes:
        scenario = corpus.get(outcome.scenario)
        run = _final_state(archive, outcome.scenario)
        if scenario is None or run is None:
            skipped.append(
                {
                    "scenario": outcome.scenario,
                    "reason": (
                        "no scenario of that name in the corpus today"
                        if scenario is None
                        else "the archived trajectory carries no checkpoint to grade"
                    ),
                }
            )
            continue
        regraded = regrade_outcome(outcome, scenario, run, _briefing(archive, outcome.scenario))
        rows.append(
            {
                "scenario": outcome.scenario,
                "world": world_of(outcome),
                "archived": {"passed": outcome.report.passed},
                "regraded": {"passed": regraded.passed},
                "changed": outcome.report.passed != regraded.passed,
                "dimensions": _dimension_rows(outcome.report, regraded),
            }
        )

    archived_root_cause = [
        row["dimensions"][GradeDimension.ROOT_CAUSE.value]["archived"]
        for row in rows
        if row["dimensions"].get(GradeDimension.ROOT_CAUSE.value, {}).get("archived")
    ]
    regraded_root_cause = [
        row["dimensions"][GradeDimension.ROOT_CAUSE.value]["regraded"]
        for row in rows
        if row["dimensions"].get(GradeDimension.ROOT_CAUSE.value, {}).get("regraded")
    ]
    document: dict[str, Any] = {
        "kind": "regrade_report",
        "archive": archive_id,
        "archive_generated_at": report.generated_at.isoformat(),
        "why": (
            "INC-003 / WO-R3-265: the ground-truth labels describe each scenario's canned "
            "world, and a live run that seeds no fault is not in that world. ROOT_CAUSE is "
            "now graded only where the label applies; elsewhere it reports 'not graded'. "
            "This document re-grades the archive under that rule. The archive is untouched."
        ),
        "totals": {
            "scenarios": len(rows),
            "archived_passed": sum(1 for row in rows if row["archived"]["passed"]),
            "regraded_passed": sum(1 for row in rows if row["regraded"]["passed"]),
            "verdicts_changed": sum(1 for row in rows if row["changed"]),
        },
        "root_cause": {
            "archived": _coverage(archived_root_cause, total=len(rows)),
            "regraded": _coverage(regraded_root_cause, total=len(rows)),
        },
        # Said in the document rather than left for a reader to work out. The
        # re-grade REMOVES an invalid number; it does not supply a valid one
        # in its place, and a small denominator printed as a percentage is how
        # the invalid number got quoted in the first place.
        "limits": (
            "A re-graded root-cause accuracy covers only the rows whose world carries their "
            f"label — {_coverage(regraded_root_cause, total=len(rows))['graded']} of "
            f"{len(rows)} here. A live root-cause number is not recoverable from a pass that "
            "seeded no fault, and this document does not offer one."
        ),
        "still_failing": [
            {
                "scenario": row["scenario"],
                "dimensions": sorted(
                    name
                    for name, dimension in row["dimensions"].items()
                    if dimension["regraded"] is not None and not dimension["regraded"]["passed"]
                ),
            }
            for row in rows
            if not row["regraded"]["passed"]
        ],
        "scenarios": rows,
        "not_regraded": skipped,
        "archive_digest": before,
    }
    verify_unchanged(archive, before)
    return document


def render_json(document: dict[str, Any]) -> str:
    return json.dumps(document, indent=2, sort_keys=False) + "\n"


def render_markdown(document: dict[str, Any]) -> str:
    """The same document for a human, in the shape a PR body quotes."""
    totals = document["totals"]
    lines = [
        f"# Re-grade of `{document['archive']}`",
        "",
        document["why"],
        "",
        f"- Run recorded at {document['archive_generated_at']}",
        f"- Scenarios re-graded: {totals['scenarios']}",
        f"- Passed: {totals['archived_passed']} archived → "
        f"{totals['regraded_passed']} re-graded ({totals['verdicts_changed']} verdict(s) changed)",
        f"- Root cause, as archived: {document['root_cause']['archived']['describe']}",
        f"- Root cause, re-graded: {document['root_cause']['regraded']['describe']}",
        "",
        document["limits"],
        "",
        "## Rows whose verdict moved",
        "",
        "| Scenario | World | Archived | Re-graded | Why |",
        "|---|---|---|---|---|",
    ]
    moved = [row for row in document["scenarios"] if row["changed"]]
    for row in moved or []:
        root_cause = row["dimensions"].get(GradeDimension.ROOT_CAUSE.value, {})
        detail = (root_cause.get("regraded") or {}).get("detail", "")
        lines.append(
            f"| `{row['scenario']}` | {row['world']} | "
            f"{'pass' if row['archived']['passed'] else 'FAIL'} | "
            f"{'pass' if row['regraded']['passed'] else 'FAIL'} | {detail} |"
        )
    if not moved:
        lines.append("| — | — | — | — | no verdict changed |")
    lines += ["", "## Still failing after the re-grade", ""]
    if document["still_failing"]:
        for row in document["still_failing"]:
            lines.append(f"- `{row['scenario']}` — {', '.join(row['dimensions'])}")
    else:
        lines.append("- none")
    lines += [
        "",
        "## The archive was not touched",
        "",
        f"{len(document['archive_digest'])} file(s), sha256 unchanged before and after the "
        "re-grade. The digests are in the JSON half of this report.",
        "",
    ]
    return "\n".join(lines) + "\n"


def write(document: dict[str, Any], *, root: Path | None = None) -> tuple[Path, Path]:
    """Write the two versioned halves and return their paths.

    Stamped from the ARCHIVE's own recorded time and id rather than from the
    clock, exactly as ``phase_close_report.write`` is: the filename is then as
    derived from the evidence as the contents are, and a second re-grade of
    the same archive under the same rules aims at the same path, where the
    exclusive-create write refuses it (invariant 9).
    """
    timestamp = datetime.fromisoformat(str(document["archive_generated_at"]))
    invocation_id = str(document["archive"])
    return (
        artifacts.write_versioned(
            "regrade_report",
            content=render_json(document),
            timestamp=timestamp,
            invocation_id=invocation_id,
            root=root,
        ),
        artifacts.write_versioned(
            "regrade_report_md",
            content=render_markdown(document),
            timestamp=timestamp,
            invocation_id=invocation_id,
            root=root,
        ),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", help="the run archive id, e.g. 0db6fe722f7c")
    parser.add_argument(
        "--runs-dir",
        type=Path,
        default=RUNS_DIR,
        help="where the archives live (another checkout's evals/runs is fine)",
    )
    parser.add_argument("--scenarios-dir", type=Path, default=SCENARIOS_DIR)
    parser.add_argument(
        "--root",
        type=Path,
        default=REPO_ROOT,
        help="repository root the versioned report is written under",
    )
    parser.add_argument(
        "--write", action="store_true", help="persist the two versioned halves as well"
    )
    parser.add_argument("--format", choices=("md", "json"), default="md")
    args = parser.parse_args(argv)

    try:
        document = regrade(args.archive, runs_dir=args.runs_dir, scenarios_dir=args.scenarios_dir)
    except (ArchiveNotFound, ArchiveChanged, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(render_json(document) if args.format == "json" else render_markdown(document), end="")
    if args.write:
        for path in write(document, root=args.root):
            print(f"wrote {path}")
    return 0


if __name__ == "__main__":  # pragma: no cover - thin CLI wrapper
    raise SystemExit(main())
