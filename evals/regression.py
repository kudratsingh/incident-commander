"""Regression gate: compare a fresh RunReport against the committed baseline.

A regression is a baseline pass that now fails. Coverage loss gates too (A-03):
dropped scenarios, DROPPED DIMENSIONS and VACATED ASSERTIONS, which keep every
roll-up green while the suite proves less (WO-R2-79). "latest" is the newest
versioned report via ``artifacts.newest("report")``, never overwritten, so the
verdict stays reproducible. ``compare``, ``model_refusal`` and ``GROUPING_KEYS``
are shared with ``evals/research_report.py`` (WP-2.5). A provenance mismatch warns
(S-14, ADR 0013); a ``development``-role run is marked non-closing (plan 03 § 14).
Exits: 0 clean; 1 gate failed; 2 not comparable (missing, filtered, two models).
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from evals import artifacts
from evals.graders.deterministic import DimensionResult, is_vacuous_detail
from evals.runner import RunReport, ScenarioOutcome

_REPO_ROOT = Path(__file__).resolve().parents[1]
_BASELINE = _REPO_ROOT / "evals" / "reports" / "baseline.json"
# Resolved, not a fixed path: ``artifacts.newest("report")`` orders by the stamp
# in the filename — never mtime, which a restored directory would re-order.
_REPORTS_DIR = _REPO_ROOT / "evals" / "reports"

#: The seven keys a result may be grouped by (plan 04 WP-2.5, 03 § 15). Here, not
#: in the report, because they answer ``compare``'s question — which rows may be
#: set beside which. The middle five come off the row (``ScenarioOutcome``,
#: WP-1.4), never off today's YAML, which would re-label history.
GROUPING_KEYS: Final[tuple[str, ...]] = (
    "strategy",
    "scenario",
    "template_id",
    "family",
    "difficulty",
    "benchmark_split",
    "execution_mode",
)

#: What a grouping key reads as when the row does not carry it. A permanent value,
#: not a transitional one, and a bucket rather than a drop — a dropped row would
#: take its pass or fail out of the totals with it.
UNKNOWN_GROUP: Final[str] = "unknown"


def grouping_values(outcome: ScenarioOutcome) -> dict[str, str]:
    """The seven grouping keys of one row, every value a string.

    Rendered here so no two readers disagree about ``seed`` 0 versus ``"0"``.
    """
    provenance = outcome.provenance
    values = {
        "strategy": provenance.strategy if provenance else None,
        "scenario": outcome.scenario,
        "template_id": outcome.template_id,
        "family": outcome.family,
        "difficulty": outcome.difficulty,
        "benchmark_split": outcome.benchmark_split,
        "execution_mode": provenance.execution_mode.value if provenance else None,
    }
    return {
        key: UNKNOWN_GROUP if value in (None, "") else str(value) for key, value in values.items()
    }


def group_key(outcome: ScenarioOutcome) -> tuple[str, ...]:
    """``grouping_values`` as a tuple in ``GROUPING_KEYS`` order — a dict key."""
    values = grouping_values(outcome)
    return tuple(values[key] for key in GROUPING_KEYS)


@dataclass(frozen=True)
class ComparisonResult:
    """Per-scenario deltas between two RunReports."""

    regressions: tuple[str, ...]
    improvements: tuple[str, ...]
    new_scenarios: tuple[str, ...]
    dropped_scenarios: tuple[str, ...]
    # Coverage losses that leave every roll-up green. Both gate. Defaulted so a
    # partial construction cannot silently assert their absence.
    dropped_dimensions: tuple[str, ...] = ()
    vacated_assertions: tuple[str, ...] = ()

    @property
    def has_regressions(self) -> bool:
        return bool(self.regressions)

    @property
    def has_coverage_loss(self) -> bool:
        """Coverage shrank without any scenario going red."""
        return bool(self.dropped_scenarios or self.dropped_dimensions or self.vacated_assertions)

    @property
    def gate_failed(self) -> bool:
        """A regression or any coverage loss fails the gate."""
        return self.has_regressions or self.has_coverage_loss


def _dimensions_by_name(outcome: ScenarioOutcome) -> dict[str, DimensionResult]:
    return {d.dimension.value: d for d in outcome.report.dimensions}


def compare(baseline: RunReport, latest: RunReport) -> ComparisonResult:
    """Diff two reports by scenario name, and by what each scenario checked.

    ``GradeReport.passed`` is an ``all()`` over the dimensions, so removing a check
    makes the roll-up greener and the diff empty. Hence the dimension-level walk.
    """
    baseline_passed = {o.scenario for o in baseline.outcomes if o.report.passed}
    baseline_by_name = {o.scenario: o for o in baseline.outcomes}
    baseline_all = set(baseline_by_name)
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

    dropped_dimensions: list[str] = []
    vacated_assertions: list[str] = []
    for name in sorted(baseline_all & latest_all):
        before = _dimensions_by_name(baseline_by_name[name])
        after = _dimensions_by_name(latest_by_name[name])
        for dimension in sorted(set(before) - set(after)):
            dropped_dimensions.append(f"{name}:{dimension}")
        for dimension in sorted(set(before) & set(after)):
            was_substantive = not is_vacuous_detail(before[dimension].detail)
            now_vacuous = is_vacuous_detail(after[dimension].detail)
            if was_substantive and now_vacuous:
                vacated_assertions.append(f"{name}:{dimension}")

    return ComparisonResult(
        regressions=tuple(regressions),
        improvements=tuple(improvements),
        new_scenarios=tuple(new_scenarios),
        dropped_scenarios=tuple(dropped_scenarios),
        dropped_dimensions=tuple(dropped_dimensions),
        vacated_assertions=tuple(vacated_assertions),
    )


def _load_report(path: Path) -> RunReport:
    return RunReport.model_validate_json(path.read_text())


def _print_comparison(result: ComparisonResult) -> None:
    """Print the diff, section by section, or say nothing changed."""
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
    if result.dropped_dimensions:
        print(f"DROPPED DIMENSIONS ({len(result.dropped_dimensions)}):")
        for name in result.dropped_dimensions:
            print(f"  x {name}")
    if result.vacated_assertions:
        print(f"VACATED ASSERTIONS ({len(result.vacated_assertions)}):")
        for name in result.vacated_assertions:
            print(f"  ! {name}")
    if not (
        result.regressions
        or result.improvements
        or result.new_scenarios
        or result.dropped_scenarios
        or result.dropped_dimensions
        or result.vacated_assertions
    ):
        print("no changes vs baseline")


def models_in(report: RunReport) -> frozenset[str]:
    """Every ``agent_model`` id the report's rows name, ignoring the unknown.

    A row with no provenance predates the record; absence is not a second model.
    """
    return frozenset(
        outcome.provenance.agent_model
        for outcome in report.outcomes
        if outcome.provenance is not None and outcome.provenance.agent_model
    )


def model_refusal(sides: Mapping[str, frozenset[str]], *, remedy: str) -> str | None:
    """Why these named sides cannot share a table, or ``None`` if they can.

    REFUSES rather than warns: a delta across two models is a model change and a
    behaviour change added together. ``remedy`` is the caller's sentence — the gate
    re-runs or re-blesses where the research report splits its scope (WP-2.5).
    """
    models = frozenset[str]().union(*sides.values()) if sides else frozenset[str]()
    if len(models) < 2:
        return None
    where = "; ".join(
        f"{name}: {', '.join(sorted(found)) or 'unknown'}" for name, found in sides.items()
    )
    return (
        f"two agent models in one comparison: {', '.join(sorted(models))} ({where}). "
        "A leaderboard row holds one model — a delta across two is a model change "
        f"and a behaviour change added together, and the table cannot say which. {remedy}"
    )


def cross_model_refusal(baseline: RunReport, latest: RunReport) -> str | None:
    """The gate's half of ``model_refusal``: baseline vs latest, exit 2."""
    return model_refusal(
        {"baseline": models_in(baseline), "latest": models_in(latest)},
        remedy=(
            "Re-run the new report under the other model, or re-bless the baseline on "
            "this one via 'make baseline'."
        ),
    )


def _print_closing_status(latest: RunReport) -> None:
    """Mark a report that cannot close a phase, without gating on it.

    Plan 03 § 14: any ``development`` run makes the report non-closing — a
    statement about use, not about regression, so it prints and never exits.
    """
    if reason := latest.non_closing_reason:
        print(f"NON-CLOSING: latest cannot close a phase — {reason}")
    else:
        print("closing: every run in latest was made under the benchmark model role")


def _print_provenance(baseline: RunReport, latest: RunReport) -> None:
    """Warn-only provenance check (S-14; ADR 0013).

    Warns on differing ``degraded_count``, or on a report predating the field.
    Cross-model comparisons are refused hard in ``cross_model_refusal``.
    """
    if baseline.degraded_count is None or latest.degraded_count is None:
        unknown = "|".join(
            name
            for name, report in (("baseline", baseline), ("latest", latest))
            if report.degraded_count is None
        )
        print(f"PROVENANCE: {unknown} predates provenance stamping (degraded_count unknown)")
    elif baseline.degraded_count != latest.degraded_count:
        print(
            f"PROVENANCE WARNING: baseline ran {baseline.degraded_count} degraded, "
            f"latest {latest.degraded_count} — pass/fail deltas may reflect "
            "canned/live divergence, not agent change"
        )


def main() -> int:
    """Resolve the newest report, refuse anything not comparable, diff it, and set the exit code."""
    if not _BASELINE.exists():
        print(f"baseline not found at {_BASELINE}", file=sys.stderr)
        return 2
    latest_path = artifacts.newest_or_none("report", directory=_REPORTS_DIR)
    if latest_path is None:
        print(f"no report found under {_REPORTS_DIR}; run make eval first", file=sys.stderr)
        return 2
    baseline = _load_report(_BASELINE)
    latest = _load_report(latest_path)
    print(f"gating against {latest_path.name}")
    if latest.only_patterns:
        # Refused, not diffed: against the full baseline the missing scenarios
        # read as dropped at best and as green coverage at worst (A-03).
        print(
            f"{latest_path.name} is a filtered run (--only={list(latest.only_patterns)}); "
            "the gate requires a full-suite report — re-run 'make eval' without ONLY",
            file=sys.stderr,
        )
        return 2
    if (refusal := cross_model_refusal(baseline, latest)) is not None:
        # Unprinted, like the filtered refusal above: the gate's output IS the
        # table, so refusing has to happen before it is written.
        print(f"GATE REFUSED: {refusal}", file=sys.stderr)
        return 2
    _print_provenance(baseline, latest)
    _print_closing_status(latest)
    result = compare(baseline, latest)
    _print_comparison(result)
    if result.dropped_scenarios:
        print(
            f"GATE FAIL: {len(result.dropped_scenarios)} baseline scenario(s) missing "
            "from latest — coverage shrank; if intentional, re-bless via 'make baseline'",
            file=sys.stderr,
        )
    if result.dropped_dimensions:
        print(
            f"GATE FAIL: {len(result.dropped_dimensions)} dimension(s) present in the "
            "baseline are no longer graded — the grader stopped scoring something it "
            "used to score. Every scenario can still pass while proving less; if "
            "intentional, re-bless via 'make baseline'",
            file=sys.stderr,
        )
    if result.vacated_assertions:
        print(
            f"GATE FAIL: {len(result.vacated_assertions)} dimension(s) now pass on an "
            "empty assertion that carried a real one in the baseline — an expectation "
            "was removed from the scenario YAML, so the dimension is green because "
            "nothing is checked; if intentional, re-bless via 'make baseline'",
            file=sys.stderr,
        )
    return 1 if result.gate_failed else 0


if __name__ == "__main__":
    sys.exit(main())
