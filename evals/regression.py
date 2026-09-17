"""Regression gate: compare a fresh RunReport against the committed baseline.

Regression = a scenario that passed in the baseline and fails in ``latest``.
Improvements and new scenarios are noted for transparency and never fail the
gate. Regressions fail it (exit 1) — and so do DROPPED scenarios (baseline
scenarios missing from ``latest``): coverage loss is a gate failure, not a
pass, and genuinely removing a scenario requires a deliberate re-bless via
``make baseline`` (A-03). A report produced under ``--only`` is refused
outright (exit 2) — a filtered report is not a comparable gate input. So is a
comparison whose two sides name DIFFERENT ``agent_model`` ids: a leaderboard
row built across two models is a statement about neither of them (WP-0.3,
plan 02 § 9). A baseline/latest provenance mismatch (``degraded_count``, ADR
0013) warns and never gates (S-14), and a report containing a
``development``-role run is MARKED non-closing — printed, never gated: a
development run is a legitimate comparison input, it just cannot close a
phase (plan 03 § 14).

"latest" is the NEWEST versioned report under ``evals/reports/``, resolved
by ``evals.artifacts.newest("report")``. Reports are never overwritten, so
the file the gate read is still on disk afterwards and the verdict stays
reproducible; the gate prints which one it used.

Coverage loss also means the two shapes that keep every scenario green
while the suite proves less (WO-R2-79): a DROPPED DIMENSION (the grader
stopped scoring something the baseline scored) and a VACATED ASSERTION (a
dimension that carried a real expectation now passes on an empty one,
because the expectation left the scenario YAML). ``GradeReport.passed`` is
an ``all()`` over the dimensions, so deleting a check can only make the
roll-up greener — a gate that reads pass/fail alone reports "no changes"
in exactly the case it exists to catch.

Exit codes — the gate's slice of the ADR 0013 contract: 0 = comparable
full-suite input with no regressions and no coverage loss; 1 = gate failed
(regression, dropped scenario, dropped dimension, or vacated assertion);
2 = not a comparable input (missing file, filtered report, two models).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from evals import artifacts
from evals.graders.deterministic import DimensionResult, is_vacuous_detail
from evals.runner import RunReport, ScenarioOutcome

_REPO_ROOT = Path(__file__).resolve().parents[1]
_BASELINE = _REPO_ROOT / "evals" / "reports" / "baseline.json"
# Reports are versioned and never overwritten, so "latest" is resolved, not
# a fixed path: ``artifacts.newest("report")`` orders by the stamp in the
# filename and then by invocation_id. Deliberately NOT mtime — a restored or
# copied reports directory would otherwise gate against whichever file the
# filesystem happened to touch last.
_REPORTS_DIR = _REPO_ROOT / "evals" / "reports"


@dataclass(frozen=True)
class ComparisonResult:
    """Per-scenario deltas between two RunReports."""

    regressions: tuple[str, ...]
    improvements: tuple[str, ...]
    new_scenarios: tuple[str, ...]
    dropped_scenarios: tuple[str, ...]
    # Coverage losses that leave every scenario's roll-up green. Both gate.
    # Defaulted so a partial construction in a future test cannot silently
    # assert their absence; ``compare`` always sets them explicitly.
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

    Scenario pass/fail alone cannot see the failure this gate exists to
    catch. ``GradeReport.passed`` is ``all(d.passed for d in dimensions)``,
    so anything that removes a check makes the roll-up *more* likely to be
    green: delete a grading dimension and it stops being ANDed in; delete an
    expectation from a scenario YAML and its dimension keeps passing on an
    empty assertion. Either way every scenario still passes, the diff is
    empty, and the gate prints "no changes vs baseline" over a suite that
    now proves strictly less than it did.

    So the diff also walks the dimensions inside each scenario and compares
    what they actually asserted, not just how they scored.
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


def _models_in(report: RunReport) -> frozenset[str]:
    """Every ``agent_model`` id the report's rows name, ignoring the unknown.

    A row with no provenance predates the record (the committed baseline and
    every archived report do). Absence is not a second model: it is one less
    thing known about the same one, so it is skipped rather than counted.
    """
    return frozenset(
        outcome.provenance.agent_model
        for outcome in report.outcomes
        if outcome.provenance is not None and outcome.provenance.agent_model
    )


def cross_model_refusal(baseline: RunReport, latest: RunReport) -> str | None:
    """Why these two reports cannot share a table, or ``None`` if they can.

    A leaderboard row is a claim about one model. Put two models' runs on it
    and every delta it shows — a regression, an improvement, a token count —
    is unattributable: the reader cannot tell a behaviour change from a model
    change, which is precisely the class of untrue statement about the agent
    that ``INCIDENTS.md`` exists to record after the fact. So this REFUSES
    (exit 2, "not a comparable input") instead of warning: a warning above a
    printed table is still a printed table.

    Both ids are named in the message, because the next action depends on
    which side is wrong — re-run ``latest`` under the baseline's model, or
    re-bless the baseline on the new one.
    """
    models = _models_in(baseline) | _models_in(latest)
    if len(models) < 2:
        return None
    where = "; ".join(
        f"{name}: {', '.join(sorted(_models_in(report))) or 'unknown'}"
        for name, report in (("baseline", baseline), ("latest", latest))
    )
    return (
        f"two agent models in one comparison: {', '.join(sorted(models))} ({where}). "
        "A leaderboard row holds one model — a delta across two is a model change "
        "and a behaviour change added together, and the table cannot say which. "
        "Re-run the new report under the other model, or re-bless the baseline on "
        "this one via 'make baseline'."
    )


def _print_closing_status(latest: RunReport) -> None:
    """Mark a report that cannot close a phase, without gating on it.

    Plan 03 § 14: a phase report generated with any ``development`` run in it
    is non-closing. That is a statement about what the report may be USED
    for, not about whether the suite regressed — development runs are the
    normal way this gate is exercised — so it prints and never changes the
    exit code. The mark is in the artifact too (``RunReport.closing``); this
    is the line that puts it in front of whoever ran the gate.
    """
    if reason := latest.non_closing_reason:
        print(f"NON-CLOSING: latest cannot close a phase — {reason}")
    else:
        print("closing: every run in latest was made under the benchmark model role")


def _print_provenance(baseline: RunReport, latest: RunReport) -> None:
    """Warn-only provenance check (S-14; ADR 0013).

    The baseline blessed on 2026-09-15 carries provenance stamps. This
    check still only warns on differing ``degraded_count`` values, or on
    older reports that lack the field. Cross-model comparisons are a
    separate hard refusal in ``cross_model_refusal``; they require a
    deliberate re-bless, not a warning override.
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
        # Refused, not diffed: comparing a filtered run against the full
        # baseline would read the missing scenarios as "dropped" at best
        # and as green coverage at worst (A-03). Exit 2 = not a comparable
        # input, same class as a missing file.
        print(
            f"{latest_path.name} is a filtered run (--only={list(latest.only_patterns)}); "
            "the gate requires a full-suite report — re-run 'make eval' without ONLY",
            file=sys.stderr,
        )
        return 2
    if (refusal := cross_model_refusal(baseline, latest)) is not None:
        # Exit 2 with the comparison unprinted, for the same reason a
        # filtered report is refused above: the gate's output IS the table,
        # so refusing has to happen before it is written.
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
