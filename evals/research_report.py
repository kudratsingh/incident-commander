"""Assemble the aggregate research report (plan 03 § 15) from committed archives.

One leaderboard per model, the corpus sliced by WP-2.5's seven keys, every
difference beside its paired-trial count. Nothing here runs an eval and nothing is
typed in — every number comes out of a committed ``evals/runs/<id>/report.json``, so
a test can regenerate the artifact byte for byte. Five load-bearing decisions: ONE
MODEL per table, enforced by ``regression.model_refusal`` (WO-R3-181) rather than a
footnote; every difference is a record with its paired count, its bootstrap CI and a
flag for plan 03 § 10's floor of ~100 trials (§ 12: do not overstate small deltas);
``SCOPE`` is PINNED, not scanned, so an unrelated archive PR cannot restate a closed
phase (``--scan`` lists candidates); the re-blessable ``baseline.json`` is NOT an
input, so regressions are arm-against-arm over locked archives via
``regression.compare``; and ``SUPERSEDED_ROOT_CAUSE`` substitutes the committed
offline re-grade for the verdicts ADR 0040 withdrew (INC-003), leaving the archive
untouched. What the report cannot say is written into ``limits``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

from evals import artifacts, regression
from evals.candidate_metrics import (
    REPORTED_KS,
    measure,
    measure_selection,
    steps_of,
    world_key,
)
from evals.graders.deterministic import GradeDimension, is_vacuous_detail
from evals.graders.root_cause import coverage_over
from evals.runner import RunReport, ScenarioOutcome
from evals.scenarios.loader import load_scenarios
from evals.tracing import TraceKind
from incident_commander.agent.hypothesis import HypothesisCategory

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[1]

#: The archives this version covers, oldest first: committed, locked (ADR 0021) and
#: carrying the provenance record (ADR 0013, WP-0.3), without which a row cannot name
#: its own model, strategy or mode. The 37 older archives predate it — still evidence,
#: not rows in this table.
SCOPE: Final[tuple[str, ...]] = (
    # Canned full suite, development role: the committed twin of the sweep that
    # produced the blessed baseline on 2026-09-15 (cmd #238).
    "2408b07ef532",
    # Canned full suite, benchmark role — the Phase 1 close sweep (cmd #248).
    "32ae38f6b38b",
    # The four live legs of the Phase 1 reduced close, in release order. The first is
    # the RED: a leaderboard of only the greens is a selected sample of itself.
    "42000dfda188",
    "845bdae22195",
    "ee183c85429c",
    "47abb70a2b9e",
    # --- added by the Phase 2 close (WO-R3-195) ---
    # Canned full suite, benchmark role, the first sweep AFTER the ground-truth labels
    # (cmd #260) — the archive that ends "no root-cause number": 32 of 41 rows graded.
    "b75527784077",
    # The live read-only pass. Its own ROOT_CAUSE verdicts are superseded —
    # see SUPERSEDED_ROOT_CAUSE below — and every other dimension on it stands.
    "0db6fe722f7c",
    # The three seeded live legs of the Phase 2 close, in the order the owner
    # released them. Two are red; both stay, for the reason above.
    "759e198cdd27",
    "648a32f2339d",
    "fc896b25a09c",
    # --- added by the FINAL Phase 2 close (WO-R3-195) ---
    # The two re-runs of those reds after their fixes merged (v0.6.8's stale-cache
    # sensor, cmd #269; ADR 0041's whole-queue rule, cmd #270). Both green, and BESIDE
    # the reds rather than instead of them — so the seeded legs pass 3 of 5, not 3 of 3.
    "d16aa18dce08",
    "42c675d9c145",
)

#: Archives whose ROOT_CAUSE verdicts were withdrawn, mapped to the committed re-grade
#: that replaces them. `0db6fe722f7c` graded canned-world labels against an unseeded
#: LIVE world (INC-003), so seven correct "nothing is wrong here" answers read as
#: misdiagnoses; ADR 0040 scoped a label to its world. The archive stays untouched
#: (invariant 9) and the substitution happens here, with the re-grade's verdicts READ
#: rather than recomputed — a second opinion assembled here would be quiet re-scoring.
SUPERSEDED_ROOT_CAUSE: Final[dict[str, str]] = {
    "0db6fe722f7c": "evals/reports/regrades/regrade_report.20260917T133824Z.0db6fe722f7c.json",
}

#: Plan 03 § 10: ~100 paired trials per arm to detect a 15-point difference at 80%
#: power. Every difference below it is labelled, in the artifact and the document.
PAIRED_TRIAL_FLOOR: Final[int] = 100

#: Paired bootstrap over the per-scenario deltas, fixed seed and count: the report is
#: a function of the archives, so a CI that moved between two assemblies of the same
#: scope would not be a fact about the runs (and would break the regeneration test).
BOOTSTRAP_RESAMPLES: Final[int] = 2000
BOOTSTRAP_SEED: Final[int] = 194
BOOTSTRAP_CONFIDENCE: Final[float] = 0.95

#: What identifies an arm — not ``regression.GROUPING_KEYS``, which slices the
#: RESULTS. ``agent_model`` is deliberately absent: it is the table's, not the arm's,
#: and the refusal above is what keeps that true.
ARM_KEYS: Final[tuple[str, ...]] = ("strategy", "model_role", "execution_mode")

#: Plan 03 § 15's aggregate contents in its order, plus WP-2.5's grouping. A closed
#: list: ``assemble`` refuses a section outside it and refuses to leave one empty,
#: because a vanished section reads as "nothing to report" and means "nobody computed".
SECTION_KEYS: Final[tuple[str, ...]] = (
    "grouping",
    "strategy_leaderboard",
    "accuracy_by_difficulty",
    "accuracy_by_family",
    "safety_by_strategy",
    "tokens_vs_accuracy",
    "tools_vs_accuracy",
    "pass_at_k_vs_selected_at_k",
    "oracle_gap",
    "calibration",
    "scenario_level_regressions",
    "paired_differences",
)

SECTION_TITLES: Final[dict[str, str]] = {
    "grouping": "1. Grouping — the seven keys",
    "strategy_leaderboard": "2. Strategy leaderboard (one model per table)",
    "accuracy_by_difficulty": "3. Accuracy by difficulty",
    "accuracy_by_family": "4. Accuracy by family",
    "safety_by_strategy": "5. Safety by strategy",
    "tokens_vs_accuracy": "6. Tokens vs accuracy",
    "tools_vs_accuracy": "7. Tools vs accuracy, and the budget beside it",
    "pass_at_k_vs_selected_at_k": "8. pass@k vs selected@k",
    "oracle_gap": "9. Oracle gap",
    "calibration": "10. Calibration",
    "scenario_level_regressions": "11. Scenario-level regressions",
    "paired_differences": "12. Paired differences",
}

_REFUSAL_REMEDY: Final[str] = (
    "Split the scope and write one report per model — this artifact is versioned, "
    "so two of them can coexist without either overwriting the other."
)


class TwoModelsRefused(ValueError):
    """The scope names two ``agent_model`` ids, so it has no one table."""


# --------------------------------------------------------------------------
# Reading the archives
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Source:
    """One committed archive report, and the exact bytes that were parsed."""

    archive: str
    path: Path
    sha256: str
    report: RunReport

    @property
    def filtered(self) -> bool:
        """Whether the run was produced under ``--only``.

        Excluded from the suite diff for the gate's own reason — its absent scenarios
        are missing, not failed — and from nothing else: a live leg IS a one-scenario
        run, so its rows are still rows.
        """
        return bool(self.report.only_patterns)


def archive_dir(root: Path, archive_id: str) -> Path:
    return root / "evals/runs" / archive_id


def read_source(root: Path, archive_id: str) -> Source:
    path = archive_dir(root, archive_id) / "report.json"
    content = path.read_bytes()
    return Source(
        archive=archive_id,
        path=path,
        sha256=hashlib.sha256(content).hexdigest(),
        report=RunReport.model_validate_json(content),
    )


def read_scope(root: Path, archives: Sequence[str] = SCOPE) -> list[Source]:
    return [read_source(root, archive_id) for archive_id in archives]


def provenance_carrying_archives(root: Path) -> list[str]:
    """Every committed archive whose rows all carry provenance, sorted.

    The reading behind ``SCOPE``, as code, so extending it is a command rather than a
    hand-audit of 44 directories. Not used by ``assemble`` — the scope is pinned.
    """
    found = []
    for directory in sorted((root / "evals/runs").iterdir()):
        path = directory / "report.json"
        if not path.is_file():
            continue
        try:
            report = RunReport.model_validate_json(path.read_bytes())
        except ValueError:
            continue
        if report.outcomes and all(o.provenance is not None for o in report.outcomes):
            found.append(directory.name)
    return found


# --------------------------------------------------------------------------
# One row
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Row:
    """One scenario-run, flattened out of a ``ScenarioOutcome``.

    Every field comes off the row itself, never joined back to today's corpus: the row
    says what the scenario WAS when it ran, so a reclassification cannot re-label
    history (``ScenarioOutcome.template_id``).
    """

    archive: str
    scenario: str
    agent_model: str
    strategy: str
    model_role: str
    execution_mode: str
    group: dict[str, str]
    passed: bool
    #: ``(dimension, passed, detail)`` per graded dimension. The detail travels with
    #: the verdict because ``safety_kind`` has to read it.
    dimensions: tuple[tuple[str, bool, str], ...]
    root_cause_graded: bool
    root_cause_correct: bool
    tool_calls: int
    max_tool_calls: int
    tokens: int
    usd: Decimal
    wall_seconds: float
    judge_overall: float | None

    @property
    def arm(self) -> tuple[str, str, str]:
        return (self.strategy, self.model_role, self.execution_mode)

    @property
    def arm_label(self) -> str:
        return "/".join(self.arm)


def _root_cause_verdict(outcome: ScenarioOutcome) -> tuple[bool, bool]:
    """``(graded, correct)`` for one row's ROOT_CAUSE dimension.

    "Graded" means a substantive detail, as ``runner.root_cause_coverage`` defines it:
    reusing ``is_vacuous_detail`` keeps this number, the run summary and the gate's
    vacated-assertion check on one signal.
    """
    for dimension in outcome.report.dimensions:
        if dimension.dimension is GradeDimension.ROOT_CAUSE:
            return (not is_vacuous_detail(dimension.detail), dimension.passed)
    return (False, False)


@dataclass(frozen=True)
class Regraded:
    """One scenario's verdict as the superseding re-grade states it."""

    passed: bool
    dimensions: tuple[tuple[str, bool, str], ...]
    root_cause_graded: bool
    root_cause_correct: bool


def regraded_verdicts(root: Path, archive: str) -> dict[str, Regraded]:
    """``{scenario: Regraded}`` from the re-grade that supersedes an archive.

    Read out of the committed document, not recomputed, and scored with
    ``_root_cause_verdict``'s own ``is_vacuous_detail`` test. The WHOLE row is
    replaced: a withdrawn dimension changes whether the row passed, and mixing the two
    would print two different runs side by side.
    """
    document = json.loads((root / SUPERSEDED_ROOT_CAUSE[archive]).read_text())
    if document["archive"] != archive:
        raise ValueError(f"re-grade for {archive} describes {document['archive']}")
    verdicts: dict[str, Regraded] = {}
    for entry in document["scenarios"]:
        dimensions = tuple(
            (name, dimension["regraded"]["passed"], dimension["regraded"]["detail"])
            for name, dimension in sorted(entry["dimensions"].items())
        )
        root_cause = entry["dimensions"].get("root_cause", {}).get("regraded")
        verdicts[entry["scenario"]] = Regraded(
            passed=entry["regraded"]["passed"],
            dimensions=dimensions,
            root_cause_graded=(
                root_cause is not None and not is_vacuous_detail(root_cause["detail"])
            ),
            root_cause_correct=bool(root_cause is not None and root_cause["passed"]),
        )
    return verdicts


def build_row(
    source: Source,
    outcome: ScenarioOutcome,
    *,
    regraded: dict[str, Regraded] | None = None,
) -> Row:
    provenance = outcome.provenance
    if provenance is None:  # pragma: no cover - SCOPE is provenance-carrying
        raise ValueError(
            f"{source.archive}/{outcome.scenario} carries no provenance record, so it cannot "
            "name its own model, strategy or execution mode — remove it from SCOPE"
        )
    graded, correct = _root_cause_verdict(outcome)
    override = None if regraded is None else regraded.get(outcome.scenario)
    if override is not None:
        graded, correct = override.root_cause_graded, override.root_cause_correct
    return Row(
        archive=source.archive,
        scenario=outcome.scenario,
        agent_model=provenance.agent_model,
        strategy=provenance.strategy,
        model_role=provenance.model_role.value,
        execution_mode=provenance.execution_mode.value,
        group=regression.grouping_values(outcome),
        passed=outcome.report.passed if override is None else override.passed,
        dimensions=override.dimensions
        if override is not None
        else tuple(
            (d.dimension.value, d.passed, d.detail)
            for d in sorted(outcome.report.dimensions, key=lambda d: d.dimension.value)
        ),
        root_cause_graded=graded,
        root_cause_correct=correct,
        tool_calls=outcome.tool_calls_used,
        max_tool_calls=provenance.budget.max_tool_calls,
        tokens=provenance.budget.tokens_used,
        usd=provenance.budget.usd_used,
        wall_seconds=provenance.budget.wall_seconds_used,
        judge_overall=outcome.judge_score.overall if outcome.judge_score else None,
    )


def build_rows(root: Path, sources: Iterable[Source]) -> list[Row]:
    rows: list[Row] = []
    for source in sources:
        regraded = (
            regraded_verdicts(root, source.archive)
            if source.archive in SUPERSEDED_ROOT_CAUSE
            else None
        )
        rows.extend(
            build_row(source, outcome, regraded=regraded) for outcome in source.report.outcomes
        )
    return rows


def refusal_for(rows: Sequence[Row]) -> str | None:
    """The cross-model refusal over a whole scope, by archive.

    ``regression.model_refusal`` writes the sentence; this names the sides, and they
    are archives because the reader's next move is to drop or re-run one.
    """
    by_archive: dict[str, set[str]] = {}
    for row in rows:
        by_archive.setdefault(row.archive, set()).add(row.agent_model)
    return regression.model_refusal(
        {archive: frozenset(models) for archive, models in sorted(by_archive.items())},
        remedy=_REFUSAL_REMEDY,
    )


# --------------------------------------------------------------------------
# Small arithmetic, with one definition each
# --------------------------------------------------------------------------


def _rate(numerator: int, denominator: int) -> float | None:
    """A rate rounded for rendering, or ``None`` when the denominator is 0.

    ``None``, never 0.0: "nothing was measured" and "everything measured was wrong"
    are different claims about the agent.
    """
    return None if denominator == 0 else round(numerator / denominator, 4)


def _mean(values: Sequence[float]) -> float | None:
    return None if not values else round(sum(values) / len(values), 4)


def _usd(total: Decimal) -> str:
    """Money as the archives write it — a string, six places, never a float."""
    return f"{total:.6f}"


def _bootstrap_ci(deltas: Sequence[float]) -> list[float] | None:
    """Percentile CI of the mean paired delta, or ``None`` below two pairs.

    The resampling unit is the PAIR, which is what makes the interval a statement
    about the difference rather than about two independent samples (plan 03 § 12).
    """
    if len(deltas) < 2:
        return None
    rng = random.Random(BOOTSTRAP_SEED)  # noqa: S311 - resampling, not cryptography
    size = len(deltas)
    means = sorted(
        sum(deltas[rng.randrange(size)] for _ in range(size)) / size
        for _ in range(BOOTSTRAP_RESAMPLES)
    )
    tail = (1.0 - BOOTSTRAP_CONFIDENCE) / 2.0
    low = means[int(tail * BOOTSTRAP_RESAMPLES)]
    high = means[min(int((1.0 - tail) * BOOTSTRAP_RESAMPLES), BOOTSTRAP_RESAMPLES - 1)]
    return [round(low, 4), round(high, 4)]


# --------------------------------------------------------------------------
# Arms and their summaries
# --------------------------------------------------------------------------


def arms_of(rows: Sequence[Row]) -> list[tuple[str, str, str]]:
    return sorted({row.arm for row in rows})


def arm_label(arm: tuple[str, str, str]) -> str:
    return "/".join(arm)


def _dimension_rates(rows: Sequence[Row]) -> dict[str, float | None]:
    graded: Counter[str] = Counter()
    passed: Counter[str] = Counter()
    for row in rows:
        for name, ok, _detail in row.dimensions:
            graded[name] += 1
            passed[name] += int(ok)
    return {name: _rate(passed[name], graded[name]) for name in sorted(graded)}


#: Phrases ``_grade_safety`` writes for the half of the dimension about what the agent
#: DID. The other half (``expected_action_arguments``) fails whenever the sanctioned
#: action did not fire, which on an escalated run is missing, not unsafe.
_FORBIDDEN_MARKERS: Final[tuple[str, ...]] = (
    "forbidden tool(s) called or attempted",
    "forbidden job_ids",
    "platform refuses this too",
    "puts out of scope",
)


def safety_kind(detail: str) -> str:
    """Which half of a failed SAFETY dimension fired.

    Plan 03 § 7.7 wants a forbidden-action RATE, and SAFETY is two rules in one: did it
    touch something forbidden, and was the sanctioned action aimed right? A run that
    escalated without acting fails the second and cannot have broken the first, so
    calling it a violation would be untrue about the agent. The detail string is the
    only place a committed archive distinguishes them, so the marker list stays narrow
    and the detail is carried into the report for a reader to check.
    """
    return (
        "forbidden_action"
        if any(marker in detail for marker in _FORBIDDEN_MARKERS)
        else "action_argument_assertion"
    )


def arm_summary(arm: tuple[str, str, str], rows: Sequence[Row]) -> dict[str, Any]:
    """One leaderboard row: correctness, and the budget beside it (02 § 8).

    ``judge_mean_overall`` is a statement about a MODEL'S OPINION, so it is withheld
    until ``LEADERBOARD_JUDGE`` has an id in ``JUDGE_CALIBRATION_REPORTS`` (WP-6.3).
    ``judged_runs`` is NOT gated, on ``_selector_number``'s split: it is a coverage
    fact, and hiding it would leave "not calibrated" indistinguishable from
    "never judged".
    """
    coverage = coverage_over(
        ((row.root_cause_graded, row.root_cause_correct) for row in rows), total=len(rows)
    )
    judged = [row.judge_overall for row in rows if row.judge_overall is not None]
    calibration_id = judge_calibration_report_for(LEADERBOARD_JUDGE)
    return {
        **dict(zip(ARM_KEYS, arm, strict=True)),
        "arm": arm_label(arm),
        "archives": sorted({row.archive for row in rows}),
        "runs": len(rows),
        "scenarios": len({row.scenario for row in rows}),
        "reps_per_scenario": sorted(Counter(row.scenario for row in rows).values()),
        "passed": sum(row.passed for row in rows),
        "pass_rate": _rate(sum(row.passed for row in rows), len(rows)),
        "dimension_pass_rate": _dimension_rates(rows),
        "root_cause": {
            "graded": coverage.graded,
            "correct": coverage.correct,
            "accuracy": None if coverage.accuracy is None else round(coverage.accuracy, 4),
            "describe": coverage.describe(),
        },
        "mean_tool_calls": _mean([float(row.tool_calls) for row in rows]),
        "mean_tokens": _mean([float(row.tokens) for row in rows]),
        "usd_total": _usd(sum((row.usd for row in rows), Decimal("0"))),
        "mean_wall_seconds": _mean([row.wall_seconds for row in rows]),
        "judge_mean_overall": (
            _mean([float(value) for value in judged])
            if calibration_id is not None or not judged
            else WITHHELD_JUDGE
        ),
        "judged_runs": len(judged),
        "judge": LEADERBOARD_JUDGE,
        "judge_calibration_report_id": calibration_id,
        "judge_gate": JUDGE_GATE_RULE,
    }


def _by_arm(rows: Sequence[Row]) -> dict[tuple[str, str, str], list[Row]]:
    grouped: dict[tuple[str, str, str], list[Row]] = {}
    for row in rows:
        grouped.setdefault(row.arm, []).append(row)
    return grouped


# --------------------------------------------------------------------------
# Differences — never a bare float
# --------------------------------------------------------------------------

#: What can be differenced, and how each value is read off a row. Rates are 0/1 per
#: run, so "accuracy" and "mean tokens" share one code path and one paired count.
METRICS: Final[dict[str, Callable[[Row], float]]] = {
    "pass_rate": lambda row: float(row.passed),
    "tool_calls": lambda row: float(row.tool_calls),
    "tokens": lambda row: float(row.tokens),
    "usd": lambda row: float(row.usd),
    "wall_seconds": lambda row: row.wall_seconds,
}


def _per_scenario(rows: Sequence[Row], metric: str) -> dict[str, list[float]]:
    read = METRICS[metric]
    values: dict[str, list[float]] = {}
    for row in rows:
        values.setdefault(row.scenario, []).append(read(row))
    return values


def paired_difference(
    metric: str,
    left: tuple[str, str, str],
    right: tuple[str, str, str],
    grouped: dict[tuple[str, str, str], list[Row]],
) -> dict[str, Any]:
    """One difference, with everything a reader needs to not overstate it.

    Paired on the scenario NAME, the strongest instance identity today's evidence
    carries (``template_id``/``seed`` are absent pre-WP-1.4, and § 12's recorded world
    id arrives with Phase 3). Reps inside one arm are averaged first, so an arm that
    ran a scenario twice does not weigh it twice.
    """
    left_values = _per_scenario(grouped[left], metric)
    right_values = _per_scenario(grouped[right], metric)
    shared = sorted(set(left_values) & set(right_values))
    pairs = [
        (sum(left_values[s]) / len(left_values[s]), sum(right_values[s]) / len(right_values[s]))
        for s in shared
    ]
    deltas = [a - b for a, b in pairs]
    value_left = _mean([a for a, _ in pairs])
    value_right = _mean([b for _, b in pairs])
    return {
        "metric": metric,
        "left": arm_label(left),
        "right": arm_label(right),
        "differs_in": [key for key, a, b in zip(ARM_KEYS, left, right, strict=True) if a != b],
        "value_left": value_left,
        "value_right": value_right,
        "delta": _mean(deltas),
        "paired_trials": len(pairs),
        "paired_on": "scenario",
        "scenarios": shared,
        "reps_left": sum(len(left_values[s]) for s in shared),
        "reps_right": sum(len(right_values[s]) for s in shared),
        "sample_floor": PAIRED_TRIAL_FLOOR,
        "below_sample_floor": len(pairs) < PAIRED_TRIAL_FLOOR,
        "bootstrap_ci": _bootstrap_ci(deltas),
        "bootstrap": {
            "resamples": BOOTSTRAP_RESAMPLES,
            "seed": BOOTSTRAP_SEED,
            "confidence": BOOTSTRAP_CONFIDENCE,
            "unit": "pair",
        },
    }


def comparable_arm_pairs(
    arms: Sequence[tuple[str, str, str]],
) -> list[tuple[tuple[str, str, str], tuple[str, str, str]]]:
    """Arms that differ in exactly ONE arm key, so a delta has one candidate cause.

    Two arms apart in both role and mode give a number nothing can attribute — the
    cross-model refusal one level down. Those are listed as skipped, keys named.
    """
    pairs = []
    for index, left in enumerate(arms):
        for right in arms[index + 1 :]:
            if sum(a != b for a, b in zip(left, right, strict=True)) == 1:
                pairs.append((left, right))
    return pairs


def _skipped_arm_pairs(arms: Sequence[tuple[str, str, str]]) -> list[dict[str, Any]]:
    skipped = []
    for index, left in enumerate(arms):
        for right in arms[index + 1 :]:
            differing = [key for key, a, b in zip(ARM_KEYS, left, right, strict=True) if a != b]
            if len(differing) > 1:
                skipped.append(
                    {
                        "left": arm_label(left),
                        "right": arm_label(right),
                        "differs_in": differing,
                        "why": (
                            "these two arms differ in more than one key, so a delta between "
                            "them has more than one candidate cause and cannot be attributed"
                        ),
                    }
                )
    return skipped


# --------------------------------------------------------------------------
# The sections
# --------------------------------------------------------------------------


def _grouping(rows: Sequence[Row]) -> dict[str, Any]:
    groups: dict[tuple[str, ...], list[Row]] = {}
    for row in rows:
        groups.setdefault(tuple(row.group[key] for key in regression.GROUPING_KEYS), []).append(row)
    distinct = {
        key: dict(sorted(Counter(row.group[key] for row in rows).items()))
        for key in regression.GROUPING_KEYS
    }
    return {
        "keys": list(regression.GROUPING_KEYS),
        "read_from": (
            "the row itself (WP-1.4 metadata + ADR 0013 provenance), never from today's "
            "scenario corpus — a row says what the scenario was when it ran"
        ),
        "unknown_value": regression.UNKNOWN_GROUP,
        "group_count": len(groups),
        "distinct_values": distinct,
        "unknown_counts": {
            key: counts.get(regression.UNKNOWN_GROUP, 0) for key, counts in distinct.items()
        },
        "groups": [
            {
                **dict(zip(regression.GROUPING_KEYS, key, strict=True)),
                "runs": len(members),
                "passed": sum(row.passed for row in members),
                "pass_rate": _rate(sum(row.passed for row in members), len(members)),
            }
            for key, members in sorted(groups.items())
        ],
    }


def _leaderboard(rows: Sequence[Row], model: str) -> dict[str, Any]:
    grouped = _by_arm(rows)
    return {
        "model": model,
        "one_model_per_table": (
            "enforced by refusal in evals/regression.py::model_refusal, not by a footnote"
        ),
        "arms": [arm_summary(arm, grouped[arm]) for arm in arms_of(rows)],
    }


def _accuracy_by(rows: Sequence[Row], key: str) -> dict[str, Any]:
    grouped: dict[tuple[str, str], list[Row]] = {}
    for row in rows:
        grouped.setdefault((row.arm_label, row.group[key]), []).append(row)
    graded = [row for row in rows if row.root_cause_graded]
    correct = sum(1 for row in graded if row.root_cause_correct)
    slices: list[dict[str, Any]] = []
    thin = 0
    for (arm, value), members in sorted(grouped.items()):
        slice_graded = sum(1 for row in members if row.root_cause_graded)
        slice_correct = sum(
            1 for row in members if row.root_cause_graded and row.root_cause_correct
        )
        if 0 < slice_graded < 5:
            thin += 1
        slices.append(
            {
                "arm": arm,
                key: value,
                "runs": len(members),
                "passed": sum(row.passed for row in members),
                "pass_rate": _rate(sum(row.passed for row in members), len(members)),
                "root_cause_graded": slice_graded,
                "root_cause_correct": slice_correct,
                "root_cause_accuracy": _rate(slice_correct, slice_graded),
            }
        )
    return {
        "key": key,
        "rows": slices,
        "root_cause_accuracy": _rate(correct, len(graded)),
        "root_cause_note": (
            "not measurable from this scope: no archived run carries a graded ROOT_CAUSE "
            "dimension (see limits)"
            if not graded
            else (
                f"{correct}/{len(graded)} over the rows that carry a graded diagnosis, which is "
                f"{len(graded)} of {len(rows)} — see limits for the three reasons the rest do "
                f"not. Per-slice accuracy is beside each row and {thin} slice(s) rest on "
                "fewer than five graded rows; those are counts, not rates, and nothing here is "
                "a per-difficulty or per-family finding"
            )
        ),
    }


def _safety_failures(rows: Sequence[Row]) -> list[dict[str, str]]:
    return [
        {
            "archive": row.archive,
            "scenario": row.scenario,
            "kind": safety_kind(detail),
            "detail": detail,
        }
        for row in rows
        for name, ok, detail in row.dimensions
        if name == "safety" and not ok
    ]


def _safety_by_strategy(rows: Sequence[Row]) -> dict[str, Any]:
    grouped: dict[str, list[Row]] = {}
    for row in rows:
        grouped.setdefault(row.strategy, []).append(row)
    table = []
    for strategy, members in sorted(grouped.items()):
        failures = _safety_failures(members)
        forbidden = [f for f in failures if f["kind"] == "forbidden_action"]
        table.append(
            {
                "strategy": strategy,
                "runs": len(members),
                "safety_graded": sum(
                    1 for row in members for n, _, _ in row.dimensions if n == "safety"
                ),
                "forbidden_action_runs": len(forbidden),
                "forbidden_action_rate": _rate(len(forbidden), len(members)),
                "action_argument_assertion_failures": len(failures) - len(forbidden),
                "action_dimension_failures": sum(
                    1 for row in members for n, ok, _ in row.dimensions if n == "action" and not ok
                ),
                "safety_dimension_pass_rate": _dimension_rates(members).get("safety"),
                "failures": failures,
            }
        )
    return {
        "graded_from": (
            "the SAFETY and ACTION dimensions of each committed row, which the deterministic "
            "grader derives from the platform audit log (invariant 6) — never from the "
            "agent's own trajectory"
        ),
        "two_rules_in_one_dimension": (
            "SAFETY fails either because the agent touched something forbidden or because the "
            "sanctioned action never fired to be checked. Only the first is a safety "
            "violation; the forbidden-action rate of plan 03 § 7.7 counts that one alone, and "
            "every failure is listed with its detail so the split can be checked"
        ),
        "rows": table,
    }


def _tokens_vs_accuracy(rows: Sequence[Row]) -> dict[str, Any]:
    grouped = _by_arm(rows)
    return {
        "note": (
            "A canned run records 0 tokens because its planner is a scripted response, not a "
            "model call. The canned rows are here to be counted, not to be read as a cheap "
            "strategy."
        ),
        "rows": [
            {
                "arm": arm_label(arm),
                "runs": len(members),
                "mean_tokens": _mean([float(row.tokens) for row in members]),
                "usd_total": _usd(sum((row.usd for row in members), Decimal("0"))),
                "pass_rate": _rate(sum(row.passed for row in members), len(members)),
            }
            for arm, members in sorted(grouped.items())
        ],
    }


def _tools_vs_accuracy(rows: Sequence[Row]) -> dict[str, Any]:
    grouped = _by_arm(rows)
    return {
        "budget_beside_correctness": (
            "plan 02 § 8: BUDGET is reported beside correctness, never folded into it. The "
            "cap is the run's own seeded ceiling (ADR 0019), which is why it differs by arm."
        ),
        "strategy_budget_multipliers": (
            "no row in scope records one: every run was `baseline`, whose WP-2.4 multipliers "
            "are 1.0, and `strategy_config` is empty on all of them"
        ),
        "rows": [
            {
                "arm": arm_label(arm),
                "runs": len(members),
                "mean_tool_calls": _mean([float(row.tool_calls) for row in members]),
                "mean_max_tool_calls": _mean([float(row.max_tool_calls) for row in members]),
                "budget_dimension_pass_rate": _dimension_rates(members).get("budget"),
                "pass_rate": _rate(sum(row.passed for row in members), len(members)),
            }
            for arm, members in sorted(grouped.items())
        ],
    }


def _not_measurable(what: str, why: str, requires: Sequence[str]) -> dict[str, Any]:
    """A section that exists, says it has no number, and says what it needs.

    Kept rather than dropped: plan 03 § 15 lists it, and a missing section cannot say
    "nobody computed it" apart from "there was nothing to compute".
    """
    return {
        "metric": what,
        "measurable": False,
        "value": None,
        "why": why,
        "requires": list(requires),
    }


def step_records_in(root: Path, archive: str) -> dict[str, list[dict[str, Any]]]:
    """Every ``step`` trace record in one archive, by scenario name (WP-5.2).

    ``traces/<scenario>.jsonl`` exists only when the run had ``EVAL_TRACE_DIR`` set, so
    most archives have none and this returns nothing — which is why ``_pass_at_k``
    still reports itself unmeasurable. Malformed lines are skipped rather than raising:
    a run killed mid-line must not take out a report over ten archives.
    """
    found: dict[str, list[dict[str, Any]]] = {}
    traces = archive_dir(root, archive) / "traces"
    if not traces.is_dir():
        return found
    for path in sorted(traces.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict) and record.get("kind") == TraceKind.STEP.value:
                found.setdefault(path.stem, []).append(record)
    return found


def _ground_truths(root: Path) -> dict[str, tuple[HypothesisCategory, ...]]:
    """Scenario name → declared root causes, for the scenarios that declare any.

    From the corpus, not an archive: the labels are the evaluator's (ADR 0038) and the
    archive holds the agent's answers. A scenario that declares none is absent, and a
    run of it is NOT GRADED rather than a miss (ADR 0040).
    """
    return {
        scenario.name: scenario.ground_truth.root_causes
        for scenario in load_scenarios(root / "evals" / "scenarios")
        if scenario.ground_truth is not None
    }


#: The smallest candidate set this section will report a pass@k over. Three archives
#: in scope carry ``step`` records, all ``baseline``'s one-candidate sets, and **pass@1
#: over a one-candidate set is the ROOT_CAUSE dimension under a second name** — the
#: same relabelling the ``calibration`` section refuses. pass@k says what ENUMERATION
#: bought, so it is reported for the arms that enumerate. Pinned both ways by
#: ``test_candidate_metrics.py::TestTheReportSectionScopesItselfToEnumeratingArms``.
ENUMERATING_SET_SIZE: Final[int] = 2


#: Calibration report id per selector arm, declared rather than discovered. Plan 02:243
#: is categorical — "NO SELECTOR NUMBER IS REPORTED BEFORE ITS CALIBRATION REPORT
#: EXISTS" — and plan 04:169 makes it WP-6.3's acceptance. Keyed by the arm identity
#: plan 02 § 12 defines (generator plus N). EMPTY today, which is correct: every
#: selector number is withheld and says so. A constant, because adding an id is a claim
#: a reviewer should see in a diff; deriving it from disk would let the gate open
#: itself. Pinned both ways by
#: ``test_candidate_selector.py::TestNoSelectorNumberWithoutACalibrationReport``.
CALIBRATION_REPORTS: Final[Mapping[str, str]] = MappingProxyType({})


#: The same register one role further out, for the JUDGE: plan 02:243's argument does
#: not depend on which model is speaking, and INC-002 is what an unchecked judge number
#: looks like when it is wrong (0.38 on a briefing that was right, on a green archive).
#: EMPTY today, which is correct — WP-6.3 built the harness and the sweep that fills
#: this is a deferred paid run (O-22) — so every judge number is withheld and says so.
#: Keyed by the role's normative name (``judge_calibration/roles.CALIBRATED_ROLES``). A
#: fake-client calibration must never be entered here; ``is_a_measurement`` is what
#: distinguishes one. Pinned both ways by
#: ``test_judge_calibration.py::TestNoJudgeNumberWithoutACalibrationReport``.
JUDGE_CALIBRATION_REPORTS: Final[Mapping[str, str]] = MappingProxyType({})

#: The judge whose scores ``judge_overall`` carries — ``evals/graders/llm_judge.py`` is
#: the only writer of that column. Named rather than spelled at the call site, so a
#: second judge column has to choose its own register key instead of inheriting this.
LEADERBOARD_JUDGE: Final[str] = "briefing_judge"


def judge_calibration_report_for(judge: str) -> str | None:
    """The calibration report id for one judge, or ``None``.

    One reader of the register: a gate with two spellings is one a section can check a
    different way.
    """
    return JUDGE_CALIBRATION_REPORTS.get(judge)


def calibration_report_for(arm: str) -> str | None:
    """The calibration report id for one selector arm, or ``None``.

    One reader of the register, so the gate has one spelling and a section cannot
    accidentally check it a different way.
    """
    return CALIBRATION_REPORTS.get(arm)


def selector_arm_key(strategy: str, config: Mapping[str, Any]) -> str:
    """The arm a selector number belongs to: strategy, generator and N.

    Plan 02 § 12's arm is ``(generator, N, selector)``: two generators under one
    selector are two arms, and keying on the strategy name would average two oracle
    gaps that answer different questions.
    """
    generator = str(config.get("generator", "")) or "unknown"
    n = config.get("n", "unknown")
    return f"{strategy}/{generator}/n={n}"


def _outcome_of(source: Source, scenario: str) -> ScenarioOutcome | None:
    """One scenario's outcome inside one archive's report, or ``None``.

    The world and the arm are properties of the RUN, not of its trace file, so
    ``selected@k`` cannot be paired without this: ``provenance`` and, on a recorded
    run, ``replay["world_fingerprint"]`` (ADR 0049).
    """
    for outcome in source.report.outcomes:
        if outcome.scenario == scenario:
            return outcome
    return None


def recorded_fingerprint(outcome: ScenarioOutcome) -> str | None:
    """The recording's identity on a recorded outcome, or ``None``.

    One reader, so the key the report pairs on and the value the runner wrote have one
    spelling. ``None`` off recorded mode, where ``replay`` is absent.
    """
    replay = outcome.replay
    if not isinstance(replay, Mapping):
        return None
    fingerprint = replay.get("world_fingerprint")
    return None if fingerprint is None else str(fingerprint)


def _candidate_rows(root: Path, sources: Sequence[Source]) -> list[dict[str, Any]]:
    """One row per (archive, scenario) whose trace carries enumerated sets.

    Empty over today's scope (every ``step`` record is ``baseline``'s one-candidate
    set, ``ENUMERATING_SET_SIZE``), and measurable the moment a best-of-N or selector
    archive enters it, with no edit here. Each row carries its ``world`` (WP-6.2),
    which is what makes ``pass@k`` and ``selected@k`` a PAIRED pair.
    """
    rows: list[dict[str, Any]] = []
    truths: dict[str, tuple[HypothesisCategory, ...]] | None = None
    for source in sources:
        for scenario, records in step_records_in(root, source.archive).items():
            if truths is None:
                truths = _ground_truths(root)
            expected = truths.get(scenario)
            if expected is None:
                # No label: not graded, not a miss — counted nowhere rather than as
                # zero, the mistake INC-003 cost $2.15 to learn.
                continue
            metrics = measure(records, expected)
            if metrics.candidates_generated == 0:
                continue
            if max(len(step.candidates) for step in steps_of(records)) < ENUMERATING_SET_SIZE:
                continue
            outcome = _outcome_of(source, scenario)
            if outcome is None or outcome.provenance is None:  # pragma: no cover
                continue
            provenance = outcome.provenance
            world = world_key(
                scenario=scenario,
                execution_mode=provenance.execution_mode.value,
                archive=source.archive,
                world_fingerprint=recorded_fingerprint(outcome),
            )
            selection = measure_selection(records, expected, world=world)
            arm = selector_arm_key(metrics.strategy, dict(provenance.strategy_config))
            rows.append(
                {
                    "archive": source.archive,
                    "scenario": scenario,
                    "strategy": metrics.strategy,
                    # The arm identity plan 02 § 12 defines, and the key the
                    # calibration register is read with.
                    "arm": arm,
                    "world": str(world),
                    "group": _group_of_scenario(root, scenario),
                    "steps": metrics.steps,
                    "candidates_generated": metrics.candidates_generated,
                    "pass_at_k": [
                        {"k": entry.k, "effective_k": entry.effective_k, "hit": entry.hit}
                        for entry in metrics.pass_at_k
                    ],
                    "appeared_at_any_step": metrics.appeared_at_any_step,
                    "first_appeared_at_iteration": metrics.first_appeared_at_iteration,
                    "cross_step_duplicate_rate": metrics.cross_step_duplicate_rate,
                    "within_step_rejection_rate": metrics.within_step_rejection_rate,
                    "rejections_by_class": dict(metrics.rejections_by_class),
                    # --- WP-6.2 -------------------------------------------
                    "selector_calls": selection.selector_calls,
                    "selector_decision": selection.decision,
                    "selected_at_k": [
                        {
                            "k": entry.k,
                            "effective_k": entry.effective_k,
                            "hit": entry.hit,
                            "not_scored_because": entry.not_scored_because,
                        }
                        for entry in selection.selected_at_k
                    ],
                    "oracle_gap_at_k": [
                        {
                            "k": entry.k,
                            "world": str(entry.world),
                            "pass_hit": entry.pass_hit,
                            "selected_hit": entry.selected_hit,
                            "gap": entry.gap,
                            "not_scored_because": entry.not_scored_because,
                        }
                        for entry in selection.oracle_gap
                    ],
                    "selector_uncertainty": selection.uncertainty,
                    "selector_was_right": selection.uncertainty_was_right,
                    "calibration_report_id": calibration_report_for(arm),
                }
            )
    return rows


def _group_of_scenario(root: Path, scenario: str) -> dict[str, str]:
    """This scenario's family and difficulty, for the by-group breakdowns.

    From the corpus, like ``_ground_truths``: the grouping is the evaluator's.
    ``unknown`` rather than an omission, so no row silently leaves a group table.
    """
    for candidate in load_scenarios(root / "evals" / "scenarios"):
        if candidate.name == scenario:
            return {
                "family": candidate.family.value if candidate.family else "unknown",
                "difficulty": (candidate.difficulty.value if candidate.difficulty else "unknown"),
            }
    return {"family": "unknown", "difficulty": "unknown"}


def _pass_at_k(root: Path, sources: Sequence[Source]) -> dict[str, Any]:
    """pass@k, appeared-at-any-step, the two duplicate rates, and selected@k.

    Measured when an archive in scope persisted per-step candidate sets, which none in
    scope does today. ``selected@k`` shares this section rather than taking its own,
    because both terms have to come off one run's own steps in one world (plan 03 § 12);
    it is WITHHELD row by row until that arm has a calibration report (``_selector_number``).
    """
    rows = _candidate_rows(root, sources)
    if not rows:
        # Every string here is the one the committed document already carries: the
        # report is versioned evidence (invariant 9), so a re-render differing only in
        # a heading would spend a version on nothing. Pinned by test_research_report.
        return _not_measurable(
            "pass@k (plan 03 § 7.2)",
            "pass@k reads the final-step candidate SET; a committed report carries one graded "
            "outcome per run and no candidate set at all",
            ("WP-3.x recorded runs that persist per-step candidate sets (StepRecord, WP-2.1)",),
        )
    return {
        "metric": "pass@k (plan 03 § 7.2) and selected@k (plan 03 § 7.3)",
        "measurable": True,
        "value": {
            "rows": [_selector_number(row) for row in rows],
            "selector_gate": SELECTOR_GATE_RULE,
        },
        "why": "",
        "requires": [],
    }


#: The rule every selector number here is subject to, in the document, in its own
#: words (plan 02:243, plan 04:169).
SELECTOR_GATE_RULE: Final[str] = (
    "plan 02:243 — NO SELECTOR NUMBER IS REPORTED BEFORE ITS CALIBRATION REPORT "
    "EXISTS. Every selected@k, oracle gap and selector-uncertainty value below is "
    "withheld unless its arm has an id in research_report.CALIBRATION_REPORTS, and "
    "each row says which case it is in. An uncalibrated selector's confidence is a "
    "number whose scale nobody has checked, and an oracle gap read beside it would "
    "attribute to selection whatever the miscalibration did."
)

#: What a withheld selector field reads as. A string, not ``None``, so a reader of the
#: JSON cannot mistake "not reported" for "zero" — INC-003's distinction one level up.
WITHHELD: Final[str] = "withheld: no calibration report for this arm (plan 02:243)"

#: The same rule for a JUDGE number, in the document, in its own words (WP-6.3).
JUDGE_GATE_RULE: Final[str] = (
    "plan 02:243's rule, applied to the judge: no judge number is reported before "
    "its calibration report exists. `judge_mean_overall` is withheld unless "
    f"{LEADERBOARD_JUDGE} has an id in research_report.JUDGE_CALIBRATION_REPORTS, "
    "and every row says which case it is in. An uncalibrated judge's mean is a "
    "number whose scale nobody has checked — INC-002 is one of them being wrong "
    "by 0.38 about a briefing that was right, on a green archive. `judged_runs` is "
    "not withheld: how many runs were judged is a coverage fact about the arm, not "
    "a statement about the judge."
)

#: What a withheld judge number reads as: a string, for the reason its selector
#: sibling gives — "not reported" must not read as "zero".
WITHHELD_JUDGE: Final[str] = (
    f"withheld: no calibration report for {LEADERBOARD_JUDGE} (plan 02:243, plan 04:169)"
)


def _selector_number(row: dict[str, Any]) -> dict[str, Any]:
    """One candidate row with its selector fields gated on a calibration report.

    The generation half (``pass@k``, the duplicate rates) is NOT gated — it measures
    the generator. Only statements about the SELECTOR are withheld, each replaced by a
    sentence rather than by ``null``.
    """
    if row["calibration_report_id"] is not None or row["selector_calls"] == 0:
        return row
    return {
        **row,
        "selected_at_k": WITHHELD,
        "oracle_gap_at_k": WITHHELD,
        "selector_uncertainty": WITHHELD,
        "selector_was_right": WITHHELD,
        "selector_decision": WITHHELD,
    }


def _oracle_gap(root: Path, sources: Sequence[Source]) -> dict[str, Any]:
    """``oracle_gap@k = pass@k − selected@k``, by family and difficulty.

    The buildout's headline analysis (plan 02:241, 03 § 7.3): a small gap says
    generation limits the agent, a large one says selection does. Three refusals keep
    it honest — it is PAIRED within one world (rows carry their ``WorldKey`` and a
    cross-world gap raises ``OracleGapAcrossWorlds``, because INC-003 is what applying
    one world's labels to another cost), it is gated on a calibration report (02:243),
    and it is the EVALUATOR's number: both terms need ``ground_truth``, which reaches
    neither the planner nor the selector (ADR 0038, ADR 0049).
    """
    rows = [row for row in _candidate_rows(root, sources) if row["selector_calls"] > 0]
    if not rows:
        # Byte-identical to the committed document, for ``_pass_at_k``'s reason.
        return _not_measurable(
            "oracle_gap@k = pass@k − selected@k (plan 03 § 7.3)",
            "both terms are unavailable for the reason above, and the selector strategy "
            "(plan 03 § 8) has not run",
            ("pass@k", "a candidate_selector arm"),
        )
    uncalibrated = sorted({row["arm"] for row in rows if row["calibration_report_id"] is None})
    if uncalibrated:
        return _not_measurable(
            "oracle_gap@k = pass@k − selected@k (plan 03 § 7.3)",
            f"{len(rows)} selector row(s) are in scope and {len(uncalibrated)} arm(s) "
            f"have no calibration report: {', '.join(uncalibrated)}. " + SELECTOR_GATE_RULE,
            ("a calibration report id in research_report.CALIBRATION_REPORTS",),
        )
    return {
        "metric": "oracle_gap@k = pass@k − selected@k (plan 03 § 7.3)",
        "measurable": True,
        "value": {
            "rule": SELECTOR_GATE_RULE,
            "calibration_reports": {row["arm"]: row["calibration_report_id"] for row in rows},
            "by_world": _gap_by(rows, lambda row: row["world"]),
            "by_family": _gap_by(rows, lambda row: row["group"]["family"]),
            "by_difficulty": _gap_by(rows, lambda row: row["group"]["difficulty"]),
            "uncertainty_vs_correctness": [
                {
                    "arm": row["arm"],
                    "world": row["world"],
                    "scenario": row["scenario"],
                    "decision": row["selector_decision"],
                    "uncertainty": row["selector_uncertainty"],
                    "was_right": row["selector_was_right"],
                    "calibration_report_id": row["calibration_report_id"],
                }
                for row in rows
            ],
        },
        "why": "",
        "requires": [],
    }


def _gap_by(
    rows: Sequence[dict[str, Any]], key: Callable[[dict[str, Any]], str]
) -> list[dict[str, Any]]:
    """The gap at each k, grouped, with the paired count that produced it.

    Plan 03 § 12's reason: a gap of 1.0 over one paired trial and over fifty are not
    the same claim, and only one is a finding.
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(key(row), []).append(row)
    out: list[dict[str, Any]] = []
    for name, members in sorted(grouped.items()):
        entry: dict[str, Any] = {"group": name, "paired_runs": len(members), "at_k": []}
        for k in REPORTED_KS:
            scored = [
                gap
                for row in members
                for gap in row["oracle_gap_at_k"]
                if gap["k"] == k and gap["gap"] is not None
            ]
            entry["at_k"].append(
                {
                    "k": k,
                    "scored_runs": len(scored),
                    "pass_at_k": (
                        None
                        if not scored
                        else sum(1 for gap in scored if gap["pass_hit"]) / len(scored)
                    ),
                    "selected_at_k": (
                        None
                        if not scored
                        else sum(1 for gap in scored if gap["selected_hit"]) / len(scored)
                    ),
                    "oracle_gap": (
                        None if not scored else sum(gap["gap"] for gap in scored) / len(scored)
                    ),
                }
            )
        out.append(entry)
    return out


def _scenario_level_regressions(sources: Sequence[Source], rows: Sequence[Row]) -> dict[str, Any]:
    """Arm against arm, using the gate's own ``compare`` (WO-R2-79's definition).

    Full-suite sources only, and "full" excludes two things. A FILTERED report, for the
    gate's reason: its absent scenarios read as dropped coverage. And a PARTIAL one —
    the read-only smoke pass selects by scope, not ``--only``, and comparing it to a
    full sweep printed "7 regressions, 14 dropped scenarios", which were exactly the
    verdicts INC-003 withdrew.
    """
    widest = max((len(source.report.outcomes) for source in sources), default=0)
    full = [
        source
        for source in sources
        if not source.filtered and len(source.report.outcomes) == widest
    ]
    comparisons = []
    for index, left in enumerate(full):
        for right in full[index + 1 :]:
            result = regression.compare(left.report, right.report)
            comparisons.append(
                {
                    "baseline_side": left.archive,
                    "latest_side": right.archive,
                    "regressions": list(result.regressions),
                    "improvements": list(result.improvements),
                    "new_scenarios": list(result.new_scenarios),
                    "dropped_scenarios": list(result.dropped_scenarios),
                    "dropped_dimensions": list(result.dropped_dimensions),
                    "vacated_assertions": list(result.vacated_assertions),
                    "gate_would_fail": result.gate_failed,
                }
            )
    return {
        "computed_by": "evals/regression.py::compare",
        "reference": (
            "arm against arm over locked archives — deliberately NOT the blessed "
            "evals/reports/baseline.json, which a re-bless rewrites (see the module docstring)"
        ),
        "comparisons": comparisons,
        "excluded_filtered_runs": [
            {
                "archive": source.archive,
                "only_patterns": list(source.report.only_patterns),
                "why": "a filtered report is not a suite-level input (evals/regression.py)",
            }
            for source in sources
            if source.filtered
        ],
        "excluded_partial_runs": [
            {
                "archive": source.archive,
                "scenarios": len(source.report.outcomes),
                "full_suite_is": widest,
                "why": (
                    "a run covering part of the corpus is not a suite-level input either: its "
                    "absent scenarios read as dropped coverage, and where it also ran in a "
                    "different world its rows are not comparable at all (INC-003)"
                ),
            }
            for source in sources
            if not source.filtered and len(source.report.outcomes) != widest
        ],
        "rows_covered": len(rows),
    }


def _root_cause_difference_refusal(rows: Sequence[Row]) -> str:
    """Why no arm-vs-arm diagnosis difference is printed, in today's terms.

    "Nothing is graded" stopped being true in Phase 2, and a stale reason is worse than
    none, so the sentence is rebuilt from the rows: the graded rows do not PAIR.
    """
    by_arm: dict[str, list[Row]] = {}
    for row in rows:
        if row.root_cause_graded:
            by_arm.setdefault(row.arm_label, []).append(row)
    if not by_arm:
        return (
            "no arm in scope has a graded ROOT_CAUSE row, so the difference has no "
            "two sides to take"
        )
    sizes = ", ".join(f"{arm} ({len(members)})" for arm, members in sorted(by_arm.items()))
    shared: set[str] | None = None
    for members in by_arm.values():
        names = {row.scenario for row in members}
        shared = names if shared is None else (shared & names)
    return (
        f"{len(by_arm)} arm(s) carry graded ROOT_CAUSE rows — {sizes} — but a difference here is "
        f"PAIRED by scenario, and only {len(shared or set())} scenario name(s) appear in both. "
        "A canned fixture and a seeded live world are not two measurements of the same thing, "
        "so pairing them by name would invent a comparison neither run supports — and even if "
        "it were fair, three pairs is two orders of magnitude below the floor"
    )


def _paired_differences(rows: Sequence[Row]) -> dict[str, Any]:
    grouped = _by_arm(rows)
    arms = arms_of(rows)
    differences = [
        paired_difference(metric, left, right, grouped)
        for left, right in comparable_arm_pairs(arms)
        for metric in METRICS
    ]
    return {
        "rule": (
            "plan 03 § 12: every difference carries the number of paired trials behind it, "
            f"and says so when that number is under the § 10 floor of {PAIRED_TRIAL_FLOOR}"
        ),
        "floor": PAIRED_TRIAL_FLOOR,
        "differences": differences,
        "all_below_floor": all(d["below_sample_floor"] for d in differences),
        "skipped_arm_pairs": _skipped_arm_pairs(arms),
        "skipped_metrics": [
            {
                "metric": "root_cause_accuracy",
                "why": _root_cause_difference_refusal(rows),
            },
            {
                "metric": "pass_at_k / selected@k / oracle gap",
                "why": "no candidate set is recorded in any archive in scope",
            },
        ],
    }


# --------------------------------------------------------------------------
# The document
# --------------------------------------------------------------------------


def _root_cause_limit(root: Path, rows: Sequence[Row], sources: Sequence[Source]) -> str:
    """Which rows carry a diagnosis verdict, which do not, and why not.

    Every number is counted, not typed: the point is that the denominator is small and
    the three reasons it is small are different things.
    """
    graded = [row for row in rows if row.root_cause_graded]
    correct = sum(1 for row in graded if row.root_cause_correct)
    live_graded = [row for row in graded if row.execution_mode == "live"]
    dimensionless = sum(
        1
        for source in sources
        for outcome in source.report.outcomes
        if not any(d.dimension is GradeDimension.ROOT_CAUSE for d in outcome.report.dimensions)
    )
    # Only archives in scope: the map is repository-wide, the count is this document's.
    world_mismatch = sum(
        json.loads((root / SUPERSEDED_ROOT_CAUSE[source.archive]).read_text())["root_cause"][
            "regraded"
        ]["not_graded_world"]
        for source in sources
        if source.archive in SUPERSEDED_ROOT_CAUSE
    )
    unlabelled = len(rows) - len(graded) - dimensionless - world_mismatch
    return (
        f"A ROOT-CAUSE NUMBER AT LAST, OVER {len(graded)} OF {len(rows)} ROWS — and the other "
        f"{len(rows) - len(graded)} are ungraded for three different reasons, which is why the "
        f"column is not an accuracy over the corpus. GRADED: {len(graded)} rows, {correct} "
        f"correct, of which {len(live_graded)} are live. NOT GRADED: {dimensionless} rows sit "
        "in archives written before ROOT_CAUSE was a dimension (cmd #255) or before the labels "
        f"existed (cmd #260), so they carry no verdict at all; {unlabelled} rows are scenarios "
        f"that deliberately declare no ground truth; and {world_mismatch} rows are held back by "
        "ADR 0040 — their label describes a world that run did not have, because the read-only "
        "pass seeds no fault. Those last ones are the withdrawn grades of INC-003: this report "
        "reads them from the committed offline re-grade rather than from the archive, so the "
        "figure the archive still carries appears nowhere in this document."
    )


def _limits(root: Path, rows: Sequence[Row], sources: Sequence[Source]) -> list[str]:
    """What this report cannot say, in the report, in its own words."""
    live = [row for row in rows if row.execution_mode == "live"]
    repeats = Counter(row.scenario for row in live)
    repeated = sorted(name for name, count in repeats.items() if count > 1)
    with_accounting = [
        source.archive
        for source in sources
        if any(outcome.accounting is not None for outcome in source.report.outcomes)
    ]
    return [
        "ONE STRATEGY. Every run in scope is `baseline`; the leaderboard has one strategy to "
        "rank, so it is a record, not a ranking. Plan 03 § 8's nine-arm matrix has not run.",
        "ONE MODEL. Every row names claude-sonnet-4-6 under two roles. A second model would be "
        "refused by this assembler, not footnoted — there is no cross-model number here.",
        f"ONE REP PER LIVE SCENARIO, almost. {len(live)} live rows cover "
        f"{len(repeats)} scenario(s); only {len(repeated)} of them ran more than once "
        f"({', '.join('`' + name + '`' for name in repeated)}), and never more than twice "
        "against the same commander. Plan 03 § 10 asks for 5 reps at comparison time, so "
        "nothing here supports a variance claim.",
        f"EVERY DIFFERENCE IS BELOW THE FLOOR. The largest paired count here is far under "
        f"{PAIRED_TRIAL_FLOOR} paired trials, so no difference in this document should be "
        "read as a detected effect. The bootstrap intervals are printed to make that visible.",
        _root_cause_limit(root, rows, sources),
        "NO pass@k, selected@k, ORACLE GAP OR CALIBRATION. All four need per-step candidate "
        "sets and confidences; a committed report carries grades, not candidate sets.",
        "NO RECORDED-WORLD MODE. Every row is canned or live (`ExecutionMode` has two members). "
        "Plan 03 § 5 puts strategy comparisons in recorded mode, which arrives in Phase 3, so "
        "instances are paired here by scenario NAME rather than by recorded-world id.",
        "PER-ROLE COST IS NOT IN THIS TABLE, THOUGH IT NOW EXISTS. WP-2.3's `accounting` record "
        f"(per-role calls, tokens, USD, ms) is present in {len(with_accounting)} of "
        f"{len(sources)} archives in scope and absent from the rest, which merged before it. "
        "Cost here is therefore still the run's own budget ledger — the whole run rather than a "
        "role — because a column populated for some rows and blank for others would invite "
        "exactly the comparison it cannot support. The Phase 2 close report breaks the live "
        "runs down by role; this table will, once every archive in scope carries the record.",
    ]


def assemble(root: Path, archives: Sequence[str] = SCOPE) -> dict[str, Any]:
    """The research document. Every value is read from a file under ``root``."""
    sources = read_scope(root, archives)
    rows = build_rows(root, sources)
    if not rows:
        raise ValueError("no rows in scope: a research report over nothing is not a report")
    if (refusal := refusal_for(rows)) is not None:
        raise TwoModelsRefused(refusal)
    model = next(iter({row.agent_model for row in rows}))

    sections: dict[str, Any] = {
        "grouping": _grouping(rows),
        "strategy_leaderboard": _leaderboard(rows, model),
        "accuracy_by_difficulty": _accuracy_by(rows, "difficulty"),
        "accuracy_by_family": _accuracy_by(rows, "family"),
        "safety_by_strategy": _safety_by_strategy(rows),
        "tokens_vs_accuracy": _tokens_vs_accuracy(rows),
        "tools_vs_accuracy": _tools_vs_accuracy(rows),
        "pass_at_k_vs_selected_at_k": _pass_at_k(root, sources),
        "oracle_gap": _oracle_gap(root, sources),
        "calibration": _not_measurable(
            "Brier / ECE / accuracy by confidence bucket (plan 03 § 7.9)",
            "calibration pairs a stated confidence with a correctness verdict; the archives "
            "carry the verdict but not the confidence, and the briefing judge's scores are a "
            "different measurement that must not be relabelled as calibration",
            ("per-step confidences (StepRecord, WP-2.1)", "a graded ROOT_CAUSE dimension"),
        ),
        "scenario_level_regressions": _scenario_level_regressions(sources, rows),
        "paired_differences": _paired_differences(rows),
    }
    if tuple(sections) != SECTION_KEYS:
        raise ValueError("section keys drifted from plan 03 section 15's aggregate contents")
    if empty := [key for key, value in sections.items() if not value]:
        raise ValueError(f"empty section(s): {', '.join(empty)}")

    non_closing = [
        {"archive": source.archive, "reason": reason}
        for source in sources
        if (reason := source.report.non_closing_reason)
    ]
    return {
        "work_order": "WO-R3-194 (WP-2.5)",
        "plan": (
            "docs/plans/research-buildout-v2.1/03_EVAL_RESEARCH_PLAN.md section 15; "
            "04_IMPLEMENTATION_WORKPLAN.md WP-2.5"
        ),
        "model": model,
        "closing": not non_closing,
        "non_closing": non_closing,
        "scope": {
            "archives": [
                {
                    "archive": source.archive,
                    "path": str(source.path.relative_to(root)),
                    "sha256": source.sha256,
                    "generated_at": source.report.generated_at.isoformat().replace("+00:00", "Z"),
                    "rows": source.report.total,
                    "passed": source.report.passed,
                    "filtered": source.filtered,
                    "only_patterns": list(source.report.only_patterns),
                }
                for source in sources
            ],
            "rows": len(rows),
            "scenarios": len({row.scenario for row in rows}),
            "arms": [arm_label(arm) for arm in arms_of(rows)],
            "selection_rule": (
                "pinned by id. Every archive here is committed, locked and carries a "
                "provenance record on every row; the scope is not scanned, so an unrelated "
                "archive merging cannot restate a finished report's numbers"
            ),
        },
        "sections": sections,
        "limits": _limits(root, rows, sources),
        "no_runs_were_made_to_produce_this": (
            "Zero live invocations, zero LLM calls and zero platform calls produced this "
            "document. Every number was read out of a committed archive under evals/runs/."
        ),
    }


# --------------------------------------------------------------------------
# Rendering and writing
# --------------------------------------------------------------------------


def render_json(document: dict[str, Any]) -> str:
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def _capitalized(sentence: str) -> str:
    """First letter up, the rest untouched — ``str.capitalize`` lowercases ids."""
    return sentence[:1].upper() + sentence[1:]


def _number(value: float | None, places: int = 3) -> str:
    return "—" if value is None else f"{value:.{places}f}"


def _table(header: Sequence[str], rows: Iterable[Sequence[str]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines.extend("| " + " | ".join(cells) + " |" for cells in rows)
    lines.append("")
    return lines


def render_difference(difference: dict[str, Any]) -> str:
    """One difference as one line — the only place a delta is written.

    One line-writer, because "every difference carries its paired-trial count" has to
    hold for every line.
    """
    ci = difference["bootstrap_ci"]
    interval = (
        "no interval (one pair)"
        if ci is None
        else f"95% paired bootstrap CI [{ci[0]:+.3f}, {ci[1]:+.3f}]"
    )
    floor = (
        f"BELOW the {difference['sample_floor']}-trial floor of plan 03 § 10"
        if difference["below_sample_floor"]
        else f"at or above the {difference['sample_floor']}-trial floor"
    )
    delta = difference["delta"]
    return (
        f"- **{difference['metric']}**, `{difference['left']}` vs `{difference['right']}` "
        f"(differs in {', '.join(difference['differs_in'])}): "
        f"{_number(difference['value_left'])} vs {_number(difference['value_right'])}, "
        f"delta {delta:+.3f} — **{difference['paired_trials']} paired trials** "
        f"({difference['reps_left']} vs {difference['reps_right']} runs, paired on "
        f"{difference['paired_on']}), {floor}, {interval}."
    )


def _render_candidate_rows(value: dict[str, Any]) -> list[str]:
    """The pass@k table, when there was a candidate set to compute one from.

    Every k asked for sits beside the ``effective_k`` the run could answer, because
    pass@8 over a 4-candidate set is pass@4 wearing a bigger number. The two duplicate
    rates stay two columns: one is a schema refusal, one a modelling finding.
    """
    ks = [entry["k"] for entry in value["rows"][0]["pass_at_k"]]
    lines = [
        f"Selected@k: not available — {value['selected_at_k_why']}.",
        "",
    ]
    lines.extend(
        _table(
            (
                "archive",
                "scenario",
                "arm",
                "steps",
                "candidates",
                *(f"pass@{k}" for k in ks),
                "any step",
                "dup rate (cross-step)",
                "refused sets",
            ),
            (
                (
                    f"`{row['archive']}`",
                    row["scenario"],
                    f"`{row['strategy']}`",
                    str(row["steps"]),
                    str(row["candidates_generated"]),
                    *(_pass_cell(entry) for entry in row["pass_at_k"]),
                    "yes" if row["appeared_at_any_step"] else "no",
                    _number(row["cross_step_duplicate_rate"]),
                    _number(row["within_step_rejection_rate"]),
                )
                for row in value["rows"]
            ),
        )
    )
    lines.append("")
    return lines


def _pass_cell(entry: dict[str, Any]) -> str:
    """One pass@k cell: the verdict, and the cap when k was larger than the set."""
    if entry["hit"] is None:
        return "n/a"
    verdict = "hit" if entry["hit"] else "miss"
    return verdict if entry["effective_k"] == entry["k"] else f"{verdict} (@{entry['effective_k']})"


def render_markdown(document: dict[str, Any]) -> str:
    sections = document["sections"]
    lines: list[str] = [
        "# Aggregate research report",
        "",
        f"**Model: {document['model']}** — one model per table, enforced by refusal.",
        "",
        f"{document['work_order']} · {document['plan']}",
        "",
        f"{document['no_runs_were_made_to_produce_this']}",
        "",
        "## Scope",
        "",
        f"{document['scope']['rows']} rows over {document['scope']['scenarios']} scenario(s), "
        f"from {len(document['scope']['archives'])} committed archives. "
        f"{document['scope']['selection_rule']}.",
        "",
    ]
    lines.extend(
        _table(
            ("archive", "generated", "rows", "passed", "filtered"),
            (
                (
                    f"`{a['archive']}`",
                    a["generated_at"][:19],
                    str(a["rows"]),
                    str(a["passed"]),
                    "yes" if a["filtered"] else "no",
                )
                for a in document["scope"]["archives"]
            ),
        )
    )
    if document["non_closing"]:
        lines.append(
            "**NON-CLOSING.** Plan 03 § 14: a report carrying any of these cannot close a phase."
        )
        lines.append("")
        lines.extend(
            f"- `{entry['archive']}` — {entry['reason']}" for entry in document["non_closing"]
        )
        lines.append("")

    grouping = sections["grouping"]
    lines += [f"## {SECTION_TITLES['grouping']}", "", f"{grouping['read_from']}.", ""]
    lines.append(
        f"{grouping['group_count']} distinct groups over "
        f"{len(grouping['keys'])} keys; `{grouping['unknown_value']}` is a bucket, not a drop."
    )
    lines.append("")
    lines.extend(
        _table(
            ("key", "distinct values", "values (runs)", "unknown rows"),
            (
                (
                    f"`{key}`",
                    str(len(grouping["distinct_values"][key])),
                    ", ".join(
                        f"{value} ({count})"
                        for value, count in list(grouping["distinct_values"][key].items())[:6]
                    )
                    + (" …" if len(grouping["distinct_values"][key]) > 6 else ""),
                    str(grouping["unknown_counts"][key]),
                )
                for key in grouping["keys"]
            ),
        )
    )

    board = sections["strategy_leaderboard"]
    lines += [
        f"## {SECTION_TITLES['strategy_leaderboard']}",
        "",
        f"{board['one_model_per_table']}.",
        "",
    ]
    lines.extend(
        _table(
            (
                "arm",
                "runs",
                "scenarios",
                "pass rate",
                "root cause",
                "mean tools",
                "mean tokens",
                "USD",
            ),
            (
                (
                    f"`{arm['arm']}`",
                    str(arm["runs"]),
                    str(arm["scenarios"]),
                    _number(arm["pass_rate"]),
                    f"{arm['root_cause']['correct']}/{arm['root_cause']['graded']}",
                    _number(arm["mean_tool_calls"], 2),
                    _number(arm["mean_tokens"], 0),
                    arm["usd_total"],
                )
                for arm in board["arms"]
            ),
        )
    )

    for key in ("accuracy_by_difficulty", "accuracy_by_family"):
        section = sections[key]
        lines += [f"## {SECTION_TITLES[key]}", "", f"Root cause: {section['root_cause_note']}.", ""]
        lines.extend(
            _table(
                ("arm", section["key"], "runs", "passed", "pass rate"),
                (
                    (
                        f"`{row['arm']}`",
                        row[section["key"]],
                        str(row["runs"]),
                        str(row["passed"]),
                        _number(row["pass_rate"]),
                    )
                    for row in section["rows"]
                ),
            )
        )

    safety = sections["safety_by_strategy"]
    lines += [
        f"## {SECTION_TITLES['safety_by_strategy']}",
        "",
        f"{safety['graded_from']}. {safety['two_rules_in_one_dimension']}.",
        "",
    ]
    lines.extend(
        _table(
            (
                "strategy",
                "runs",
                "forbidden-action runs",
                "forbidden-action rate",
                "action-argument failures",
                "action dimension failures",
            ),
            (
                (
                    f"`{row['strategy']}`",
                    str(row["runs"]),
                    str(row["forbidden_action_runs"]),
                    _number(row["forbidden_action_rate"]),
                    str(row["action_argument_assertion_failures"]),
                    str(row["action_dimension_failures"]),
                )
                for row in safety["rows"]
            ),
        )
    )
    for row in safety["rows"]:
        lines.extend(
            f"- SAFETY failure ({failure['kind']}) in `{failure['archive']}` / "
            f"`{failure['scenario']}`: {failure['detail']}"
            for failure in row["failures"]
        )
    lines.append("")

    tokens = sections["tokens_vs_accuracy"]
    lines += [f"## {SECTION_TITLES['tokens_vs_accuracy']}", "", tokens["note"], ""]
    lines.extend(
        _table(
            ("arm", "runs", "mean tokens", "USD", "pass rate"),
            (
                (
                    f"`{row['arm']}`",
                    str(row["runs"]),
                    _number(row["mean_tokens"], 0),
                    row["usd_total"],
                    _number(row["pass_rate"]),
                )
                for row in tokens["rows"]
            ),
        )
    )

    tools = sections["tools_vs_accuracy"]
    lines += [
        f"## {SECTION_TITLES['tools_vs_accuracy']}",
        "",
        f"{tools['budget_beside_correctness']}",
        "",
        f"Multipliers: {tools['strategy_budget_multipliers']}.",
        "",
    ]
    lines.extend(
        _table(
            ("arm", "runs", "mean tool calls", "mean cap", "budget pass rate", "pass rate"),
            (
                (
                    f"`{row['arm']}`",
                    str(row["runs"]),
                    _number(row["mean_tool_calls"], 2),
                    _number(row["mean_max_tool_calls"], 2),
                    _number(row["budget_dimension_pass_rate"]),
                    _number(row["pass_rate"]),
                )
                for row in tools["rows"]
            ),
        )
    )

    for key in ("pass_at_k_vs_selected_at_k", "oracle_gap", "calibration"):
        section = sections[key]
        lines += [f"## {SECTION_TITLES[key]}", ""]
        if not section["measurable"]:
            lines += [
                f"**Not measurable from this scope.** {section['metric']}: {section['why']}.",
                "",
                "Needs: " + "; ".join(section["requires"]) + ".",
                "",
            ]
            continue
        lines.extend(_render_candidate_rows(section["value"]))

    regressions = sections["scenario_level_regressions"]
    lines += [
        f"## {SECTION_TITLES['scenario_level_regressions']}",
        "",
        f"Computed by `{regressions['computed_by']}`. Reference: {regressions['reference']}.",
        "",
    ]
    lines.extend(
        _table(
            ("left", "right", "regressions", "improvements", "dropped", "vacated"),
            (
                (
                    f"`{c['baseline_side']}`",
                    f"`{c['latest_side']}`",
                    str(len(c["regressions"])),
                    str(len(c["improvements"])),
                    str(len(c["dropped_scenarios"]) + len(c["dropped_dimensions"])),
                    str(len(c["vacated_assertions"])),
                )
                for c in regressions["comparisons"]
            ),
        )
    )
    if regressions["excluded_filtered_runs"]:
        lines.append(
            "Excluded from the suite diff (filtered runs, one scenario each): "
            + ", ".join(f"`{e['archive']}`" for e in regressions["excluded_filtered_runs"])
            + "."
        )
        lines.append("")
    if regressions["excluded_partial_runs"]:
        lines.append(
            "Also excluded (part of the corpus, not filtered — the read-only pass selects by "
            "token scope): "
            + ", ".join(
                f"`{e['archive']}` ({e['scenarios']} of {e['full_suite_is']})"
                for e in regressions["excluded_partial_runs"]
            )
            + ". "
            + _capitalized(regressions["excluded_partial_runs"][0]["why"])
            + "."
        )
        lines.append("")

    differences = sections["paired_differences"]
    lines += [f"## {SECTION_TITLES['paired_differences']}", "", f"{differences['rule']}.", ""]
    lines.extend(render_difference(difference) for difference in differences["differences"])
    lines.append("")
    for skipped in differences["skipped_arm_pairs"]:
        lines.append(
            f"- Not compared: `{skipped['left']}` vs `{skipped['right']}` — {skipped['why']}."
        )
    for skipped in differences["skipped_metrics"]:
        lines.append(f"- Not computed: {skipped['metric']} — {skipped['why']}.")
    lines.append("")

    lines += ["## What this report cannot say yet", ""]
    lines.extend(f"- {limit}" for limit in document["limits"])
    lines.append("")
    return "\n".join(lines) + "\n"


def scope_stamp(document: dict[str, Any]) -> tuple[datetime, str]:
    """The artifact's timestamp and id, both derived from the evidence.

    The newest archive in scope, and a digest over every source's sha256, so the
    filename identifies the SCOPE and a re-assembly of the same archives aims at the
    same path — where exclusive-create refuses (invariant 9). A clock would let one
    document be written twice under two names.
    """
    archives = document["scope"]["archives"]
    newest = max(entry["generated_at"] for entry in archives)
    digest = hashlib.sha256(
        "\n".join(f"{entry['archive']}:{entry['sha256']}" for entry in archives).encode()
    ).hexdigest()[:12]
    return datetime.fromisoformat(newest.replace("Z", "+00:00")), digest


def write(document: dict[str, Any], *, root: Path | None = None) -> tuple[Path, Path]:
    """Write the two versioned halves and return their paths."""
    timestamp, invocation_id = scope_stamp(document)
    return (
        artifacts.write_versioned(
            "research_report",
            content=render_json(document),
            timestamp=timestamp,
            invocation_id=invocation_id,
            root=root,
        ),
        artifacts.write_versioned(
            "research_report_md",
            content=render_markdown(document),
            timestamp=timestamp,
            invocation_id=invocation_id,
            root=root,
        ),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help="repository root to read")
    parser.add_argument("--format", choices=("json", "md"), default="md")
    parser.add_argument(
        "--write", action="store_true", help="write the two versioned halves instead of printing"
    )
    parser.add_argument(
        "--scan",
        action="store_true",
        help="list committed archives that carry provenance and are not in SCOPE",
    )
    args = parser.parse_args(argv)
    if args.scan:
        for archive in provenance_carrying_archives(args.root):
            mark = "in scope" if archive in SCOPE else "NOT in SCOPE"
            print(f"{archive}  {mark}")
        return 0
    try:
        # SCOPE read here, not as a default argument: a default binds once at import,
        # and the scope is the one thing a caller or a test legitimately substitutes.
        document = assemble(args.root, SCOPE)
        if args.write:
            for path in write(document, root=args.root):
                print(f"wrote {path.relative_to(args.root)}")
            return 0
    except TwoModelsRefused as refusal:
        # Exit 2 with nothing printed above it, as the gate refuses: the report's
        # output IS the table, so the refusal precedes the first line of it.
        print(f"RESEARCH REPORT REFUSED: {refusal}")
        return 2
    except (OSError, ValueError) as error:
        print(f"RESEARCH REPORT FAIL: {error}")
        return 2
    print(render_json(document) if args.format == "json" else render_markdown(document), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
