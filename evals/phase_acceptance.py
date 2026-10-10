"""What a Phase 3–15 close reads, and how much of it is measured yet (WO-R3-362).

Each phase declares plan 04's acceptance lines and the committed evidence each one reads;
missing evidence renders "not yet measured" rather than failing. ``as_of`` caps the evidence,
so a committed document regenerates byte for byte after newer evidence lands.
"""

from __future__ import annotations

import json
import subprocess
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from functools import cache
from pathlib import Path
from typing import Any, Final

from evals import artifacts
from evals.scenarios.loader import load_scenarios
from evals.scenarios.schema import Scenario

MEASURED: Final[str] = "measured"
PARTLY: Final[str] = "partly measured"
NOT_YET: Final[str] = "not yet measured"
BLOCKED: Final[str] = "cannot be measured yet"
LINE_STATUSES: Final[tuple[str, ...]] = (MEASURED, PARTLY, NOT_YET, BLOCKED)

#: Run modes a close reads. Canned sweeps belong to ``make eval-reg``, not to a close.
RUN_MODES: Final[tuple[str, ...]] = ("live", "recorded")

PLAN_04: Final[str] = "docs/plans/research-buildout-v2.1/04_IMPLEMENTATION_WORKPLAN.md"


# --------------------------------------------------------------------------
# What a reading covers
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Subjects:
    """The scenarios a reading covers: a family, named scenarios, or every recorded world.

    Empty means every scenario in the corpus (a strategy reading that runs on any world).
    """

    family: str | None = None
    scenarios: tuple[str, ...] = ()
    recorded_worlds: bool = False

    def resolve(self, ctx: Context) -> tuple[str, ...]:
        if self.recorded_worlds:
            return tuple(sorted(ctx.recordings))
        if self.family is not None:
            return tuple(sorted(n for n, s in ctx.corpus.items() if _family(s) == self.family))
        return self.scenarios or tuple(sorted(ctx.corpus))

    def describe(self) -> str:
        if self.recorded_worlds:
            return "every scenario with a committed recording"
        if self.family is not None:
            return f"the `{self.family}` family"
        if self.scenarios:
            return ", ".join(f"`{name}`" for name in self.scenarios)
        return "any scenario"


def _family(scenario: Scenario) -> str:
    family = scenario.family
    return str(getattr(family, "value", family))


# --------------------------------------------------------------------------
# The evidence on disk, read once per assembly
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RunRow:
    """One graded scenario out of a live or recorded run archive."""

    archive: str
    generated_at: str
    scenario: str
    mode: str
    strategy: str
    model_role: str
    passed: bool
    final_state: str
    root_cause: str
    failed_dimensions: tuple[str, ...]


@dataclass(frozen=True)
class Context:
    root: Path
    as_of: datetime | None
    corpus: Mapping[str, Scenario]
    recordings: Mapping[str, Path]
    runs: tuple[RunRow, ...]
    #: Archives whose committed grades were withdrawn, mapped to the re-grade that replaces them.
    withdrawn: Mapping[str, str] = field(default_factory=dict)
    #: Paths git tracks under ``evals/``, or ``None`` outside a checkout (then everything counts).
    tracked: frozenset[str] | None = None

    def in_window(self, when: datetime) -> bool:
        return self.as_of is None or when <= self.as_of

    def committed(self, path: Path) -> bool:
        return self.tracked is None or path.relative_to(self.root).as_posix() in self.tracked


def _parse(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def _file_stamp(path: Path, suffix: str) -> datetime:
    parsed = artifacts.parse_version_name(path.name, suffix=suffix)
    if parsed is None:
        raise ValueError(f"{path.name} is not a versioned artifact name")
    return parsed.timestamp()


def _dimension(report: Mapping[str, Any], name: str) -> str:
    for row in report.get("dimensions", ()):
        if row.get("dimension") == name:
            if not row.get("applicable", True):
                return "n/a"
            return "PASS" if row.get("passed") else "FAIL"
    return "not graded"


@cache
def _all_runs(root: Path) -> tuple[RunRow, ...]:
    """Every live or recorded outcome under ``evals/runs``, oldest first. Archives never change."""
    rows: list[RunRow] = []
    for path in sorted((root / "evals" / "runs").glob("*/report.json")):
        raw = json.loads(path.read_text())
        for outcome in raw.get("outcomes", ()):
            provenance = outcome.get("provenance") or {}
            mode = provenance.get("execution_mode") or (
                "live" if outcome.get("live_llm") and outcome.get("live_mcp") else "canned"
            )
            if mode not in RUN_MODES:
                continue
            report = outcome.get("report") or {}
            rows.append(
                RunRow(
                    archive=path.parent.name,
                    generated_at=str(raw.get("generated_at", "")),
                    scenario=outcome["scenario"],
                    mode=mode,
                    strategy=str(provenance.get("strategy") or "unrecorded"),
                    model_role=str(provenance.get("model_role") or "unrecorded"),
                    passed=bool(report.get("passed")),
                    final_state=str(outcome.get("final_state")),
                    root_cause=_dimension(report, "root_cause"),
                    failed_dimensions=tuple(
                        row["dimension"]
                        for row in report.get("dimensions", ())
                        if row.get("applicable", True) and not row.get("passed")
                    ),
                )
            )
    return tuple(sorted(rows, key=lambda row: (row.generated_at, row.archive, row.scenario)))


def _tracked(root: Path) -> frozenset[str] | None:
    """What git tracks under ``evals/``: a close reads committed evidence, not a local run."""
    try:
        listed = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", "--", "evals"],
            capture_output=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    return frozenset(name for name in listed.decode().split("\0") if name)


def context(
    root: Path, *, as_of: datetime | None = None, withdrawn: Mapping[str, str] | None = None
) -> Context:
    ctx = Context(root, as_of, {}, {}, (), dict(withdrawn or {}), _tracked(root))
    corpus = {s.name: s for s in load_scenarios(root / "evals" / "scenarios")}
    recordings: dict[str, Path] = {}
    folder = root / "evals" / "recorded_worlds"
    for scenario in sorted(p.name for p in folder.iterdir() if p.is_dir()):
        kept = [
            path
            for path in artifacts.versions("recorded_world", scenario, root=root)
            if ctx.committed(path) and ctx.in_window(_file_stamp(path, ".json"))
        ]
        if kept:
            recordings[scenario] = kept[-1]
    runs = tuple(
        run
        for run in _all_runs(root)
        if ctx.committed(root / "evals" / "runs" / run.archive / "report.json")
        and ctx.in_window(_parse(run.generated_at))
    )
    return replace(ctx, corpus=corpus, recordings=recordings, runs=runs)


def _newest_in_window(ctx: Context, kind: str, scenario: str | None = None) -> Path | None:
    suffix = artifacts.KINDS[kind].suffix
    kept = [
        path
        for path in artifacts.versions(kind, scenario, root=ctx.root)
        if ctx.committed(path) and ctx.in_window(_file_stamp(path, suffix))
    ]
    return kept[-1] if kept else None


def _result(
    what: str, status: str, summary: str, *, rows: Sequence[Mapping[str, Any]] = (), at: str = ""
) -> dict[str, Any]:
    return {"reads": what, "status": status, "summary": summary, "rows": list(rows), "at": at}


# --------------------------------------------------------------------------
# Readings
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Recordings:
    """Does each subject have a committed recording? (``make world-record``)"""

    subjects: Subjects

    def read(self, ctx: Context) -> dict[str, Any]:
        names = self.subjects.resolve(ctx)
        rows = [
            {"scenario": n, "recording": ctx.recordings[n].name if n in ctx.recordings else None}
            for n in names
        ]
        have = [row for row in rows if row["recording"]]
        stamps = [_file_stamp(ctx.recordings[n], ".json") for n in names if n in ctx.recordings]
        status = MEASURED if have else NOT_YET
        summary = f"{len(have)} of {len(rows)} recorded — {self.subjects.describe()}"
        at = max(stamps).isoformat() if stamps else ""
        return _result("recordings", status, summary, rows=rows, at=at)


@dataclass(frozen=True)
class DriftVerdicts:
    """Each subject's verdict in the newest committed ``make world-drift-all`` table."""

    subjects: Subjects

    def read(self, ctx: Context) -> dict[str, Any]:
        names = self.subjects.resolve(ctx)
        path = _newest_in_window(ctx, "world_drift_table")
        if path is None:
            return _result(
                "drift verdicts",
                NOT_YET,
                "no drift table is committed (`make world-drift-all WRITE=1`)",
            )
        table = json.loads(path.read_text())
        by_scenario = {row["scenario"]: row for row in table["rows"]}
        rows = [
            {
                "scenario": n,
                "verdict": by_scenario[n]["verdict"] if n in by_scenario else NOT_YET,
                "disagreements": by_scenario[n]["disagreements"] if n in by_scenario else None,
                "detail": by_scenario[n]["detail"] if n in by_scenario else "",
            }
            for n in names
        ]
        counts = Counter(row["verdict"] for row in rows)
        tally = ", ".join(f"{count} {verdict}" for verdict, count in sorted(counts.items()))
        green = bool(rows) and counts.get("CLEAN", 0) == len(rows)
        summary = f"{tally} — table `{path.name}`, against platform {table['platform_pin']}; " + (
            "drift check green" if green else "drift check NOT green"
        )
        status = MEASURED if NOT_YET not in counts else (PARTLY if len(counts) > 1 else NOT_YET)
        return _result("drift verdicts", status, summary, rows=rows, at=table["checked_at"])


@dataclass(frozen=True)
class Runs:
    """Every graded run of the subjects in one mode, optionally under named strategies."""

    subjects: Subjects
    mode: str
    strategies: tuple[str, ...] = ()

    def read(self, ctx: Context) -> dict[str, Any]:
        names = set(self.subjects.resolve(ctx))
        found = [
            run
            for run in ctx.runs
            if run.mode == self.mode
            and run.scenario in names
            and (not self.strategies or run.strategy in self.strategies)
        ]
        what = f"{self.mode} runs" + (f" ({', '.join(self.strategies)})" if self.strategies else "")
        if not found:
            return _result(what, NOT_YET, f"no {what} of {self.subjects.describe()} committed")
        rows: list[dict[str, Any]] = []
        for run in found:
            regrade = ctx.withdrawn.get(run.archive)
            rows.append(
                {
                    "archive": run.archive,
                    "scenario": run.scenario,
                    "strategy": run.strategy,
                    "model_role": run.model_role,
                    "final_state": run.final_state,
                    "passed": None if regrade else run.passed,
                    "root_cause": "withdrawn" if regrade else run.root_cause,
                    "failed": [] if regrade else list(run.failed_dimensions),
                    "note": f"grade withdrawn; see {regrade}" if regrade else "",
                }
            )
        graded = [row for row in rows if row["passed"] is not None]
        root_cause = [row for row in graded if row["root_cause"] in ("PASS", "FAIL")]
        summary = (
            f"{len(rows)} {what} over {len({row['scenario'] for row in rows})} scenario(s): "
            f"{sum(row['passed'] for row in graded)} of {len(graded)} passed, root cause "
            f"{sum(row['root_cause'] == 'PASS' for row in root_cause)} of {len(root_cause)}"
        )
        return _result(what, MEASURED, summary, rows=rows, at=found[-1].generated_at)


@dataclass(frozen=True)
class Calibrations:
    """Each judge's newest calibration report id (``make judge-calibration``)."""

    judges: tuple[str, ...]

    def read(self, ctx: Context) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        stamps: list[str] = []
        for judge in self.judges:
            path = _newest_in_window(ctx, "judge_calibration", judge)
            if path is None:
                rows.append({"judge": judge, "report_id": None, "client": NOT_YET})
                continue
            report = json.loads(path.read_text())
            trap = report["trap_agreement"]
            stability = report["self_agreement"]
            stamps.append(str(report["generated_at"]))
            rows.append(
                {
                    "judge": judge,
                    "report_id": report["report_id"],
                    "client": report["judge_client"],
                    "traps": f"{trap['agreed']} of {trap['cases']}",
                    "stable": f"{stability['identical']} of {stability['cases']}",
                }
            )
        have = [row for row in rows if row["report_id"]]
        status = MEASURED if len(have) == len(rows) else (PARTLY if have else NOT_YET)
        summary = f"{len(have)} of {len(rows)} judge(s) have a calibration report id"
        at = max(stamps, default="")
        return _result("calibration report ids", status, summary, rows=rows, at=at)


@dataclass(frozen=True)
class Artefact:
    """The newest committed artefact of one ``evals/artifacts.py`` kind."""

    kind: str

    def read(self, ctx: Context) -> dict[str, Any]:
        path = _newest_in_window(ctx, self.kind)
        if path is None:
            return _result(f"`{self.kind}`", NOT_YET, f"no `{self.kind}` artefact committed")
        at = _file_stamp(path, artifacts.KINDS[self.kind].suffix).isoformat()
        return _result(f"`{self.kind}`", MEASURED, f"newest: `{path.name}`", at=at)


@dataclass(frozen=True)
class AlertTexts:
    """Do subjects that share one alert text have different root causes? Read from the corpus."""

    subjects: Subjects

    def read(self, ctx: Context) -> dict[str, Any]:
        groups: dict[tuple[str, str], list[Scenario]] = {}
        for name in self.subjects.resolve(ctx):
            scenario = ctx.corpus[name]
            alert = scenario.alert.model_dump()
            key = (str(alert.get("fingerprint")), str(alert.get("summary")))
            groups.setdefault(key, []).append(scenario)
        rows = []
        for (fingerprint, _summary), members in sorted(groups.items()):
            causes = sorted(
                {
                    "+".join(sorted(str(c.value) for c in m.ground_truth.root_causes))
                    for m in members
                    if m.ground_truth is not None
                }
            )
            rows.append(
                {
                    "fingerprint": fingerprint,
                    "scenarios": [m.name for m in members],
                    "root_causes": causes,
                }
            )
        undecidable = [row for row in rows if len(row["root_causes"]) > 1]
        summary = (
            f"{len(rows)} distinct alert text(s) over {sum(len(r['scenarios']) for r in rows)} "
            f"scenario(s); {len(undecidable)} text(s) carry more than one root cause"
            + (" — the text alone cannot decide" if undecidable else " — the text decides")
        )
        return _result("alert texts (corpus)", MEASURED, summary, rows=rows)


@dataclass(frozen=True)
class UnitTests:
    """An acceptance line that unit tests hold; ``make test-unit`` runs them, this does not."""

    path: str

    def read(self, ctx: Context) -> dict[str, Any]:
        if not (ctx.root / self.path).is_file():
            return _result("unit tests", NOT_YET, f"`{self.path}` does not exist")
        return _result("unit tests", MEASURED, f"held by `{self.path}` (not re-run here)")


@dataclass(frozen=True)
class Blocked:
    """A reading the repo cannot produce yet, and the work that would unblock it."""

    reason: str

    def read(self, ctx: Context) -> dict[str, Any]:
        return _result("—", BLOCKED, self.reason)


Reading = (
    Recordings | DriftVerdicts | Runs | Calibrations | Artefact | AlertTexts | UnitTests | Blocked
)


# --------------------------------------------------------------------------
# Scopes
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class AcceptanceLine:
    """One acceptance line of plan 04, quoted, and the evidence that answers it."""

    packet: str
    text: str
    reads: tuple[Reading, ...]


@dataclass(frozen=True)
class Pending:
    """Work that must land before this close can claim what it is about to claim."""

    work_order: str
    what: str


@dataclass(frozen=True)
class AcceptanceScope:
    """What one phase's close reads. No prose about results: those come from the evidence."""

    phase: int
    title: str
    close_packet: str
    work_order: str
    lines: tuple[AcceptanceLine, ...]
    pending: tuple[Pending, ...] = ()

    @property
    def document_id(self) -> str:
        """The invocation id in a committed document's filename; one per phase."""
        return f"phase{self.phase:02d}"


def _line_status(results: Sequence[Mapping[str, Any]]) -> str:
    statuses = {result["status"] for result in results}
    if statuses == {BLOCKED}:
        return BLOCKED
    if statuses == {MEASURED}:
        return MEASURED
    if MEASURED in statuses or PARTLY in statuses:
        return PARTLY
    return NOT_YET


def assemble(
    root: Path,
    scope: AcceptanceScope,
    *,
    as_of: datetime | None = None,
    withdrawn: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """The acceptance document. Every value is read from a file under ``root``."""
    ctx = context(root, as_of=as_of, withdrawn=withdrawn)
    lines: list[dict[str, Any]] = []
    for line in scope.lines:
        results = [reading.read(ctx) for reading in line.reads]
        lines.append(
            {
                "packet": line.packet,
                "text": line.text,
                "status": _line_status(results),
                "readings": results,
            }
        )
    stamps = [_parse(result["at"]) for line in lines for result in line["readings"] if result["at"]]
    counts = Counter(line["status"] for line in lines)
    return {
        "phase": scope.phase,
        "kind": "acceptance status",
        "title": scope.title,
        "work_order": scope.work_order,
        "protocol": f"{PLAN_04} § Phase {scope.phase}",
        "as_of": max(stamps).isoformat() if stamps else None,
        "closing": False,
        "closing_reason": (
            f"An acceptance status, not the close itself: {scope.close_packet} "
            f"({scope.work_order}) runs plan 03 § 14 on BENCHMARK_MODEL and needs the "
            "owner's go."
        ),
        "counts": {status: counts.get(status, 0) for status in LINE_STATUSES},
        "lines": lines,
        "pending": [{"work_order": p.work_order, "what": p.what} for p in scope.pending],
        "no_runs_were_made_to_produce_this": (
            "Zero live invocations and zero LLM calls produced this document; every value "
            "was read out of a committed file."
        ),
    }


def _cell(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, list):
        return ", ".join(str(item) for item in value) or "—"
    return str(value).replace("|", "\\|")


def render_markdown(document: Mapping[str, Any]) -> str:
    counts = document["counts"]
    lines = [
        f"# Phase {document['phase']} — {document['title']}: acceptance status",
        "",
        f"Work order {document['work_order']} · {document['protocol']} · evidence as of "
        f"{document['as_of'] or 'nothing committed yet'}",
        "",
        f"**Not closing.** {document['closing_reason']}",
        "",
        ", ".join(f"{counts[status]} {status}" for status in LINE_STATUSES) + " (lines).",
        "",
        "| packet | acceptance line | status | reads |",
        "|---|---|---|---|",
    ]
    for line in document["lines"]:
        reads = "; ".join(result["reads"] for result in line["readings"])
        lines.append(
            f"| {line['packet']} | {_cell(line['text'])} | {line['status']} | {_cell(reads)} |"
        )
    for line in document["lines"]:
        lines += ["", f"## {line['packet']} — {line['text']}", ""]
        for result in line["readings"]:
            lines.append(f"- **{result['reads']}** — {result['status']}: {result['summary']}")
            if result["rows"]:
                header = list(result["rows"][0])
                lines += [
                    "",
                    "  | " + " | ".join(header) + " |",
                    "  |" + "---|" * len(header),
                ]
                lines += [
                    "  | " + " | ".join(_cell(row.get(key)) for key in header) + " |"
                    for row in result["rows"]
                ]
                lines.append("")
    if document["pending"]:
        lines += ["", "## Pending before this close can claim it", ""]
        lines += [f"- {item['work_order']}: {item['what']}" for item in document["pending"]]
    lines += ["", document["no_runs_were_made_to_produce_this"]]
    return "\n".join(lines) + "\n"
