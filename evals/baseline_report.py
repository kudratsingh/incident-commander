"""Assemble the Phase 0 baseline from immutable archives; never run an eval.

Every number is read out of a committed ``report.json``; nothing is typed in
and nothing is regraded. The historical archives keep their own scores,
scenario revisions and (for the nine that predate cmd #223) their absent
provenance — the report says which leg carries a stamp rather than borrowing
one from the machine that assembled it. ``--write`` persists the two versioned
halves through ``evals/artifacts.py``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from evals import artifacts
from evals.runner import RunProvenance, RunReport
from evals.scenarios.loader import load_scenarios

REPO_ROOT = Path(__file__).resolve().parents[1]
READ_ONLY_ARCHIVE = "cde5a14485c3"
REMEDIATION_ARCHIVES = (
    "16ae3c7a4c9d",
    "54ab08425f82",
    "4753c12f8132",
    "aeadd5ef3edd",
    "3c65c04326d4",
    "9949c45145d4",
    "2988f414afb4",
    "f32f023eaf33",
)
ARCHIVE_IDS = (READ_ONLY_ARCHIVE, *REMEDIATION_ARCHIVES)
NEVER_RUN_LIVE = ("dlq_mislabeled_replay_safe", "saga_stuck", "dlq_mixed_partial")


class DebtVerdict(BaseModel):
    """One row of the debt walk: which PR, how it was settled, and the evidence for that."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    row: int
    pr: str
    disposition: Literal["confirmed", "refuted", "superseded", "open"]
    observable: str
    evidence: str


def read_debt_walk(path: Path) -> list[DebtVerdict]:
    """Read the dated Markdown table as the single source of walk verdicts."""
    text = path.read_text(encoding="utf-8")
    marker = "### Restart walk (2026-09-15)"
    if marker not in text:
        raise ValueError("missing Restart walk (2026-09-15)")
    historical = text.split("## Corrections", 1)[0]
    prs = [
        line.split("|")[2].strip() for line in historical.splitlines() if line.startswith("| 20")
    ]
    rows = []
    for line in text.split(marker, 1)[1].splitlines():
        cells = [cell.strip() for cell in line.split("|")[1:-1]]
        if not cells or not cells[0].isdigit():
            continue
        if len(cells) != 5:
            raise ValueError(f"malformed debt walk row {cells[0]}")
        rows.append(
            DebtVerdict.model_validate(
                dict(
                    zip(("row", "pr", "disposition", "observable", "evidence"), cells, strict=True)
                )
            )
        )
    if [row.row for row in rows] != list(range(1, len(prs) + 1)) or [row.pr for row in rows] != prs:
        raise ValueError("debt walk must disposition every original row exactly once, in order")
    if not rows or any(not row.observable or not row.evidence for row in rows):
        raise ValueError("every debt row needs its observable and evidence")
    return rows


def summarize_report(path: Path) -> dict[str, Any]:
    """Read recorded totals and derive distributions; never regrade an archive."""
    content = path.read_bytes()
    raw = json.loads(content)
    report = RunReport.model_validate(raw)
    if (
        report.total != len(report.outcomes)
        or report.passed != sum(o.report.passed for o in report.outcomes)
        or report.failed != report.total - report.passed
    ):
        raise ValueError(f"inconsistent recorded counts in {path}")
    if len({o.scenario for o in report.outcomes}) != report.total:
        raise ValueError(f"duplicate scenario outcomes in {path}")
    dimensions: dict[str, Counter[str]] = {}
    for outcome in report.outcomes:
        for dimension in outcome.report.dimensions:
            counts = dimensions.setdefault(dimension.dimension.value, Counter())
            counts["total"] += 1
            counts["passed"] += int(dimension.passed)
    return {
        "invocation_id": report.invocation_id,
        "report_sha256": hashlib.sha256(content).hexdigest(),
        "generated_at": raw["generated_at"],
        "recorded_numbers": {
            key: raw.get(key)
            for key in (
                "total",
                "passed",
                "failed",
                "judged_count",
                "judge_useful_count",
                "judge_mean_overall",
                "degraded_count",
            )
        },
        "pass_rate": report.passed / report.total if report.total else None,
        "dimensions": {
            name: {
                "total": counts["total"],
                "passed": counts["passed"],
                "pass_rate": counts["passed"] / counts["total"],
            }
            for name, counts in sorted(dimensions.items())
        },
        "tool_calls": dict(
            sorted(Counter(str(o.tool_calls_used) for o in report.outcomes).items())
        ),
        "terminal_states": dict(
            sorted(Counter(o.final_state.value for o in report.outcomes).items())
        ),
        "execution_legs": dict(
            sorted(
                Counter(
                    f"mcp={'live' if o.live_mcp else 'canned'},"
                    f"llm={'live' if o.live_llm else 'canned'}"
                    for o in report.outcomes
                ).items()
            )
        ),
        "scenarios": [
            {
                "scenario": outcome["scenario"],
                "final_state": outcome["final_state"],
                "tool_calls_used": outcome["tool_calls_used"],
                "recorded_grade": outcome["report"],
                "recorded_failure_class": outcome.get("failure_class", "unknown"),
                "provenance": outcome.get("provenance"),
            }
            for outcome in raw["outcomes"]
        ],
    }


#: Every field of ``RunProvenance`` that has to be present and answered in the
#: report's stamp. Read off the model rather than typed out, so a field added
#: to the record cannot quietly go unstamped here.
PROVENANCE_FIELDS: Final[tuple[str, ...]] = tuple(RunProvenance.model_fields)

#: Values that look like an answer and are not one. ``"unknown"`` is honest in
#: a run archive (ADR 0013: a claim a reader can act on), but a baseline whose
#: stamp says "unknown" is the un-attributable artifact divergence D3 names, so
#: the writer refuses it here.
_PLACEHOLDERS: Final[frozenset[str]] = frozenset({"", "unknown", "none", "null", "n/a", "tbd"})

#: Provenance fields that legitimately differ row to row within one run.
_PER_SCENARIO: Final[frozenset[str]] = frozenset({"scenario", "budget", "recorded_at"})


def stamp_of(offline_path: Path) -> dict[str, Any]:
    """The one provenance record every row of the offline suite agrees on.

    Read from the run's own archive, never from today's ``.env``: the stamp has
    to describe the run that produced the numbers, and a config read at
    assembly time describes the machine that happened to assemble them. Per
    ADR 0013 provenance is attached per scenario, so this collapses the 41
    records to one and refuses if they disagree — two models in one report is
    not a baseline, it is two.
    """
    raw = json.loads(offline_path.read_text())
    records = [outcome.get("provenance") for outcome in raw["outcomes"]]
    if not records or any(record is None for record in records):
        raise ValueError(f"{offline_path.name}: every outcome must carry a provenance record")
    # Per-scenario by design: the scenario's own name, its own budget ledger,
    # and the moment IT was recorded (each row is stamped as it finishes, so
    # the 41 timestamps differ by milliseconds). Everything else — the code,
    # the platform image, the models, the role, the strategy, the invocation,
    # the execution mode — has to be one answer for the whole run.
    shared = [
        {key: value for key, value in record.items() if key not in _PER_SCENARIO}
        for record in records
    ]
    if any(record != shared[0] for record in shared[1:]):
        raise ValueError(f"{offline_path.name}: outcomes disagree about what produced them")
    validated = RunProvenance.model_validate(records[0])
    missing = [
        name
        for name in PROVENANCE_FIELDS
        if name not in _PER_SCENARIO
        and str(getattr(validated, name)).strip().lower() in _PLACEHOLDERS
    ]
    if missing:
        raise ValueError(f"provenance field(s) unanswered: {', '.join(sorted(missing))}")
    stamp = dict(shared[0])
    # The run's own timestamp, not one row's: a baseline is dated by the run
    # that produced it, and picking a row would date it by whichever scenario
    # happened to finish first.
    stamp["recorded_at"] = raw["generated_at"]
    stamp["budgets_seeded_and_used"] = [
        {"scenario": record["scenario"], **record["budget"]} for record in records
    ]
    return stamp


def assemble(root: Path, offline_path: Path) -> dict[str, Any]:
    """The baseline document. Every value is read from a file under ``root``.

    Nothing here reads the clock, the environment or ``.env``: the whole
    document is a function of the committed archives plus the offline report,
    which is what lets a test regenerate it byte for byte.
    """
    sources = []
    for archive_id in ARCHIVE_IDS:
        relative = Path("evals/runs") / archive_id / "report.json"
        summary = summarize_report(root / relative)
        if summary["invocation_id"] != archive_id:
            raise ValueError(f"archive identity mismatch: {archive_id}")
        sources.append({"source": relative.as_posix(), **summary})
    offline = summarize_report(offline_path)
    parsed_offline = RunReport.model_validate_json(offline_path.read_text())
    if parsed_offline.only_patterns or any(
        o.live_mcp or o.live_llm for o in parsed_offline.outcomes
    ):
        raise ValueError("offline baseline input must be a full canned report")
    expected_names = {s.name for s in load_scenarios(root / "evals/scenarios")}
    if {o.scenario for o in parsed_offline.outcomes} != expected_names:
        raise ValueError("offline baseline input must cover the current scenario corpus exactly")
    if parsed_offline.passed != parsed_offline.total:
        raise ValueError("the offline leg of a baseline must be a clean full-suite pass")
    debt_path = root / "docs/eval-debt.md"
    debt = read_debt_walk(debt_path)
    return {
        "stage": 2,
        "status": "the Phase 0 baseline, assembled from committed evidence",
        "closing": False,
        "non_closing_reason": (
            "Assembled under the development model role from archives that predate provenance "
            "stamping; three debt rows remain open. A closing report is a benchmark-role run, "
            "which this is not."
        ),
        "provenance": stamp_of(offline_path),
        "runs_make_baseline": False,
        "runs_make_baseline_reason": (
            "Open user decision. `evals/reports/baseline.json` is still the 37-scenario "
            "2026-07-31 report from cmd #46 while the corpus is 41, and ADR 0011's status is "
            "split: its sunset fired at the restart, its Status line still reads accepted. "
            "This packet supplies the ledger walk the sunset requires and stops there; "
            "re-blessing the regression baseline is a deliberate act nobody has authorised."
        ),
        "historical_sources": sources,
        "offline_source": {"source": offline_path.name, **offline},
        "debt_walk": {
            "source": "docs/eval-debt.md#restart-walk-2026-09-15",
            "sha256": hashlib.sha256(debt_path.read_bytes()).hexdigest(),
            "rows": [row.model_dump() for row in debt],
        },
        "exclusions": [
            {
                "scenario": name,
                "reason": "Built and offline-green; never run live by owner decision.",
            }
            for name in NEVER_RUN_LIVE
        ]
        + [
            {
                "archive": "e8404306138c",
                "reason": "Pre-re-derivation scenario-2 pass; superseded by 54ab08425f82.",
            },
            {
                "scenario": "remediate_verify_fails",
                "reason": "Canned-only; no live fault survives remediation (WO-R2-165).",
            },
            {
                "scenario": "consumer_lag_high",
                "reason": "Live-capable but outside the completed live stages (WO-R2-134).",
            },
        ],
        "interpretation": [
            "Historical sources use their recorded graders and scenario revisions; "
            "no pooled cross-model comparison is claimed.",
            "Only the offline leg carries a provenance record. The nine live archives "
            "predate provenance stamping (cmd #223), so their model, strategy and platform "
            "digest are recorded in STATE.md and the runbook, not in the archive itself.",
            "cde5a14485c3/consumer_lag_missing_group is a REAL agent finding "
            "(study/findings.md F-005); its recorded grader-brittleness label is retained, "
            "not endorsed.",
            "Dimension rates include recorded vacuous passes; "
            "they are not rates of substantive assertions.",
            "The machine regression baseline remains the 37-scenario 2026-07-31 report "
            "(cmd #46); O-2/O-3 remain owner/coordinator decisions.",
            "Zero live invocations produced this document. No archive was written or "
            "modified to make it; every number was read.",
        ],
    }


def render_json(document: dict[str, Any]) -> str:
    """The document as deterministic JSON — the machine-readable half."""
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def render_markdown(document: dict[str, Any]) -> str:
    """The document as the report a person reads — the same numbers, laid out."""
    provenance = document["provenance"]
    lines = [
        "# The Phase 0 baseline",
        "",
        "Assembled from evidence that already existed. **Zero live invocations; no archive "
        "was written or modified.**",
        "",
        document["non_closing_reason"],
        "",
        "## What produced the offline leg",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| commander revision | `{provenance['commander_revision']}` |",
        f"| platform image digest | `{provenance['platform_image_digest']}` |",
        f"| agent model | `{provenance['agent_model']}` |",
        f"| model role | `{provenance['model_role']}` |",
        f"| judge model | `{provenance['judge_model']}` |",
        f"| strategy | `{provenance['strategy']}` |",
        f"| strategy config | `{json.dumps(provenance['strategy_config'])}` |",
        f"| invocation | `{provenance['invocation_id']}` |",
        f"| recorded at | {provenance['recorded_at']} |",
        f"| execution mode | `{provenance['execution_mode']}` |",
        "",
        "Seeded and used budgets are recorded per scenario in the JSON companion under "
        "`provenance.budgets_seeded_and_used`.",
        "",
        "## Does this packet also run `make baseline`?",
        "",
        f"**No.** {document['runs_make_baseline_reason']}",
        "",
        "## Recorded results",
        "",
        "The eight green live remediation archives are `16ae3c7a4c9d`, `54ab08425f82`, "
        "`4753c12f8132`, `aeadd5ef3edd`, `3c65c04326d4`, `9949c45145d4`, `2988f414afb4` and "
        "`f32f023eaf33`, plus the read-only stage archive `cde5a14485c3` (25/26). That is "
        "eight, not the nine PASS rows STATE.md's table shows: `e8404306138c` is scenario 2's "
        "pre-re-derivation pass and is superseded by run E `54ab08425f82`.",
        "",
        "| Source | Passed / total | Tool-call distribution | Terminal states |",
        "|---|---|---|---|",
    ]
    for source in [*document["historical_sources"], document["offline_source"]]:
        numbers = source["recorded_numbers"]
        lines.append(
            f"| {source['invocation_id']} | {numbers['passed']} / {numbers['total']} | "
            f"{json.dumps(source['tool_calls'])} | {json.dumps(source['terminal_states'])} |"
        )
    lines.extend(["", "## Dimension rates (recorded, including vacuous passes)", ""])
    for source in [*document["historical_sources"], document["offline_source"]]:
        values = ", ".join(
            f"{name}: {counts['passed']}/{counts['total']}"
            for name, counts in source["dimensions"].items()
        )
        lines.append(
            f"- {source['invocation_id']}: {values}; "
            f"execution legs: {json.dumps(source['execution_legs'])}"
        )
    lines.extend(
        [
            "",
            "## Ledger walk",
            "",
            "By reference, not by copy — the walk is appended under the Corrections heading of "
            "[`docs/eval-debt.md`](../../docs/eval-debt.md#restart-walk-2026-09-15), which is "
            "where its evidence links resolve. That file's sha256 when this was assembled was "
            f"`{document['debt_walk']['sha256']}`; each row's full observable and evidence are "
            "in the JSON companion under `debt_walk.rows`.",
            "",
            "| Row | PR | Disposition |",
            "|---|---|---|",
        ]
    )
    for row in document["debt_walk"]["rows"]:
        lines.append(f"| {row['row']} | {row['pr']} | **{row['disposition']}** |")
    lines.extend(["", "## Exclusions and interpretation", ""])
    for entry in document["exclusions"]:
        lines.append(f"- {entry.get('scenario', entry.get('archive'))}: {entry['reason']}")
    lines.extend(f"- {note}" for note in document["interpretation"])
    return "\n".join(lines) + "\n"


def write(document: dict[str, Any], *, root: Path | None = None) -> tuple[Path, Path]:
    """Write the two versioned halves and return their paths.

    The version stamp and the invocation id come from the offline leg's own
    provenance rather than from the clock, so the filename is as derived from
    the evidence as the contents are: re-running the writer on the same inputs
    aims at the same path, which the exclusive-create write then refuses
    (invariant 9). A baseline that renamed itself every time it was checked
    could not be checked at all.
    """
    provenance = document["provenance"]
    recorded_at = datetime.fromisoformat(str(provenance["recorded_at"]))
    invocation_id = str(provenance["invocation_id"])
    return (
        artifacts.write_versioned(
            "baseline_report",
            content=render_json(document),
            timestamp=recorded_at,
            invocation_id=invocation_id,
            root=root,
        ),
        artifacts.write_versioned(
            "baseline_report_md",
            content=render_markdown(document),
            timestamp=recorded_at,
            invocation_id=invocation_id,
            root=root,
        ),
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Assemble the baseline from the committed archives, then print it or write both halves."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--offline-report", type=Path, help="defaults to artifacts.newest('report')"
    )
    parser.add_argument("--format", choices=("json", "md"), default="md")
    parser.add_argument(
        "--write",
        action="store_true",
        help="write the versioned baseline artifacts instead of printing",
    )
    args = parser.parse_args(argv)
    try:
        offline_path = args.offline_report or artifacts.newest("report")
        document = assemble(REPO_ROOT, offline_path)
        if args.write:
            for path in write(document):
                print(f"wrote {path.relative_to(REPO_ROOT)}")
            return 0
    except ValidationError:
        print("BASELINE REPORT FAIL: invalid source report; no output written")
        return 2
    except (OSError, ValueError) as error:
        print(f"BASELINE REPORT FAIL: {error}")
        return 2
    print(render_json(document) if args.format == "json" else render_markdown(document), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
