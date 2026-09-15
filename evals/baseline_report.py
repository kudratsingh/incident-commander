"""Assemble a stage-1 baseline preview from immutable reports; never run evals.

The historical reports retain their own scores, scenario versions and unknown
provenance. Today's config is labelled as assembly config, never attributed to
old runs. Stage 2 registers and persists the versioned baseline artifact.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from evals import artifacts
from evals.runner import RunReport
from evals.scenarios.loader import load_scenarios
from incident_commander.config import Settings

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


def assemble(root: Path, offline_path: Path, settings: Settings) -> dict[str, Any]:
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
    debt_path = root / "docs/eval-debt.md"
    debt = read_debt_walk(debt_path)
    return {
        "stage": 1,
        "status": "preview; not blessed",
        "closing": False,
        "non_closing_reason": (
            "Stage 1 preview; legacy runs lack model/strategy provenance and debt remains open."
        ),
        "assembly_model_config": {
            "note": "Current config only; not the model config of historical archives.",
            "agent_model": settings.agent_model,
            "development_model": settings.development_model,
            "benchmark_model": settings.benchmark_model,
            "judge_model": settings.judge_model,
        },
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
            "cde5a14485c3/consumer_lag_missing_group is a REAL agent finding "
            "(study/findings.md F-005); its recorded grader-brittleness label is retained, "
            "not endorsed.",
            "Dimension rates include recorded vacuous passes; "
            "they are not rates of substantive assertions.",
            "The machine regression baseline remains the 37-scenario 2026-07-31 report "
            "(cmd #46); O-2/O-3 remain owner/coordinator decisions.",
        ],
    }


def render_json(document: dict[str, Any]) -> str:
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def render_markdown(document: dict[str, Any]) -> str:
    lines = [
        "# Phase 0 baseline — stage 1 preview",
        "",
        document["non_closing_reason"],
        "",
        "## Recorded results",
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
            "## Assembly model config (not historical provenance)",
            "",
            "```json",
            json.dumps(document["assembly_model_config"], indent=2),
            "```",
            "",
            "## Ledger walk",
            "",
        ]
    )
    for row in document["debt_walk"]["rows"]:
        lines.append(
            f"- Row {row['row']} ({row['pr']}): **{row['disposition']}** — "
            f"{row['observable']} {row['evidence']}"
        )
    lines.extend(["", "## Exclusions and interpretation", ""])
    for entry in document["exclusions"]:
        lines.append(f"- {entry.get('scenario', entry.get('archive'))}: {entry['reason']}")
    lines.extend(f"- {note}" for note in document["interpretation"])
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--offline-report", type=Path, help="defaults to artifacts.newest('report')"
    )
    parser.add_argument("--format", choices=("json", "md"), default="md")
    args = parser.parse_args(argv)
    try:
        settings = Settings()  # type: ignore[call-arg]
        offline_path = args.offline_report or artifacts.newest("report")
        document = assemble(REPO_ROOT, offline_path, settings)
    except ValidationError:
        print("BASELINE REPORT FAIL: invalid settings or source report; no output written")
        return 2
    except (OSError, ValueError) as error:
        print(f"BASELINE REPORT FAIL: {error}")
        return 2
    print(render_json(document) if args.format == "json" else render_markdown(document), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
