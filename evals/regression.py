"""Regression gate: compare a fresh RunReport against the committed baseline.

Regression = a scenario that passed in the baseline and fails in ``latest``.
Improvements and new scenarios are noted for transparency but never fail the
gate — only regressions do.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from evals.runner import RunReport

_REPO_ROOT = Path(__file__).resolve().parents[1]
_BASELINE = _REPO_ROOT / "evals" / "reports" / "baseline.json"
_LATEST = _REPO_ROOT / "evals" / "reports" / "latest.json"


@dataclass(frozen=True)
class ComparisonResult:
    """Per-scenario deltas between two RunReports."""

    regressions: tuple[str, ...]
    improvements: tuple[str, ...]
    new_scenarios: tuple[str, ...]
    dropped_scenarios: tuple[str, ...]

    @property
    def has_regressions(self) -> bool:
        return bool(self.regressions)


def compare(baseline: RunReport, latest: RunReport) -> ComparisonResult:
    """Diff two reports by scenario name."""
    baseline_passed = {o.scenario for o in baseline.outcomes if o.report.passed}
    baseline_all = {o.scenario for o in baseline.outcomes}
    latest_by_name = {o.scenario: o for o in latest.outcomes}
    latest_all = set(latest_by_name)

    regressions = sorted(
        name for name in baseline_passed & latest_all if not latest_by_name[name].report.passed
    )
    improvements = sorted(
        name
        for name in (baseline_all - baseline_passed) & latest_all
        if latest_by_name[name].report.passed
    )
    new_scenarios = sorted(latest_all - baseline_all)
    dropped_scenarios = sorted(baseline_all - latest_all)

    return ComparisonResult(
        regressions=tuple(regressions),
        improvements=tuple(improvements),
        new_scenarios=tuple(new_scenarios),
        dropped_scenarios=tuple(dropped_scenarios),
    )


def _load_report(path: Path) -> RunReport:
    return RunReport.model_validate_json(path.read_text())


def _print_comparison(result: ComparisonResult) -> None:
    if result.regressions:
        print(f"REGRESSIONS ({len(result.regressions)}):")
        for name in result.regressions:
            print(f"  - {name}")
    if result.improvements:
        print(f"improvements ({len(result.improvements)}):")
        for name in result.improvements:
            print(f"  + {name}")
    if result.new_scenarios:
        print(f"new scenarios ({len(result.new_scenarios)}):")
        for name in result.new_scenarios:
            print(f"  * {name}")
    if result.dropped_scenarios:
        print(f"dropped scenarios ({len(result.dropped_scenarios)}):")
        for name in result.dropped_scenarios:
            print(f"  x {name}")
    if not (
        result.regressions
        or result.improvements
        or result.new_scenarios
        or result.dropped_scenarios
    ):
        print("no changes vs baseline")


def main() -> int:
    if not _BASELINE.exists():
        print(f"baseline not found at {_BASELINE}", file=sys.stderr)
        return 2
    if not _LATEST.exists():
        print(f"latest report not found at {_LATEST}; run make eval first", file=sys.stderr)
        return 2
    baseline = _load_report(_BASELINE)
    latest = _load_report(_LATEST)
    result = compare(baseline, latest)
    _print_comparison(result)
    return 1 if result.has_regressions else 0


if __name__ == "__main__":
    sys.exit(main())
