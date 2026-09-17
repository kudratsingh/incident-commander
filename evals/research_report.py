"""Assemble the aggregate research report (plan 03 § 15) from committed archives.

The report every phase close reads: one leaderboard per model, the corpus
sliced by the seven keys of WP-2.5, and every difference printed beside the
number of paired trials it was computed from. Like ``evals/baseline_report.py``
and ``evals/phase_close_report.py``, nothing here runs an eval and nothing is
typed in — every number is read out of a committed ``evals/runs/<id>/report.json``
— which is what lets a test regenerate the committed artifact byte for byte.

Four decisions are load-bearing.

**One model per table, enforced by refusal.** ``assemble`` raises rather than
footnotes when the rows in scope name two ``agent_model`` ids, and the message
names both. The rule and its wording come from ``evals/regression.py``
(``model_refusal``, WO-R3-181) rather than from a second copy here: a delta
across two models is a model change and a behaviour change added together, and
a table that shows one cannot say which it is. A warning above a printed table
is still a printed table.

**Every difference carries its paired-trial count, and says when that count is
too small.** Plan 03 § 12: "do not overstate small deltas on small sets". So a
difference is not a float in this module — it is a record with the two arms,
the two values, the delta, how many scenarios were present in BOTH arms, a
paired bootstrap CI, and a flag saying whether the pair count reached plan 03
§ 10's floor of ~100 paired trials. Nothing in this report reaches that floor
today, and every difference in the committed artifact says so in its own line.

**The scope is pinned, not scanned.** ``SCOPE`` lists the archives by id. A
scan of ``evals/runs/`` would quietly change the document every time an
unrelated archive PR merged, which would make the committed report's
regeneration test red on somebody else's change and — worse — would silently
restate a finished phase's numbers over a corpus it never covered. A later
phase adds its archives to ``SCOPE`` and writes a NEW version of the artifact;
the old one stays exactly as it was (invariant 9). ``--scan`` prints the
archives that carry provenance and are not in ``SCOPE``, so extending it is a
read, not a hunt.

**The blessed baseline is deliberately not an input.**
``evals/reports/baseline.json`` is re-blessed by a deliberate act
(``make baseline``, ADR 0011), so a research artifact that read it would change
its own past numbers the next time the gate was re-blessed. Scenario-level
regressions here are therefore arm-against-arm over locked archives, computed
by ``regression.compare`` — the gate's own definition of what a scenario-level
regression is — rather than against the moving baseline.

What this cannot say yet is written into the artifact (``limits``) rather than
left for the reader to notice: one strategy, one model, one rep per live
scenario, no recorded-world mode until Phase 3, and — the big one — no
root-cause number, because ROOT_CAUSE (WP-2.2, cmd #255) and the ground-truth
labels (WO-R3-261, cmd #260) both landed AFTER every archive in scope was
written, so not one committed run was ever graded on diagnosis.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from evals import artifacts, regression
from evals.graders.deterministic import GradeDimension, is_vacuous_detail
from evals.graders.root_cause import coverage_over
from evals.runner import RunReport, ScenarioOutcome

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[1]

#: The archives this version of the report covers, oldest first. Every one is a
#: committed, locked run archive (ADR 0021) whose rows carry the provenance
#: record (ADR 0013, WP-0.3) — without it a row cannot name its own model,
#: strategy or execution mode, and so cannot sit in a leaderboard at all. The
#: 37 older archives under ``evals/runs/`` predate that record; they are
#: evidence and they stay, but they are not rows in this table.
SCOPE: Final[tuple[str, ...]] = (
    # Canned full suite, development role — the run that produced the blessed
    # baseline's sibling on 2026-09-15 (cmd #238's bless run is its own,
    # uncommitted archive; this one is the committed twin of that sweep).
    "2408b07ef532",
    # Canned full suite, benchmark role — the Phase 1 close sweep (cmd #248).
    "32ae38f6b38b",
    # The four live legs of the Phase 1 reduced close, in the order the owner
    # released them. The first is the RED; a leaderboard that listed only the
    # greens would be a selected sample of itself.
    "42000dfda188",
    "845bdae22195",
    "ee183c85429c",
    "47abb70a2b9e",
)

#: Plan 03 § 10: ~100 paired trials per arm to detect a 15-point difference at
#: 80% power. Every difference below it is labelled in the artifact and in the
#: rendered document — that label is the whole point of § 12.
PAIRED_TRIAL_FLOOR: Final[int] = 100

#: Paired bootstrap over the per-scenario deltas. Fixed seed, fixed count: the
#: report is a function of the archives, so its CIs have to be too — a CI that
#: moved between two runs of the same assembler would break the byte-for-byte
#: regeneration test and, more importantly, would not be a fact about the runs.
BOOTSTRAP_RESAMPLES: Final[int] = 2000
BOOTSTRAP_SEED: Final[int] = 194
BOOTSTRAP_CONFIDENCE: Final[float] = 0.95

#: What identifies an arm. Not the same as ``regression.GROUPING_KEYS``: those
#: seven are how the RESULTS are sliced, these three are what was being
#: measured. ``agent_model`` is deliberately absent — it is the table's, not
#: the arm's, and the refusal above is what keeps that true.
ARM_KEYS: Final[tuple[str, ...]] = ("strategy", "model_role", "execution_mode")

#: Plan 03 § 15's aggregate contents, in its order, plus the grouping WP-2.5
#: opens with. Closed list: ``assemble`` refuses to emit a section outside it
#: and refuses to leave one empty — a section that quietly disappeared would
#: read as "nothing to report" when it means "nobody computed it".
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

        The regression gate refuses a filtered report as a gate input, and for
        the same reason this report will not put one in a suite-level
        comparison: the scenarios it does not contain are missing, not failed.
        Its rows are still rows — a live leg IS a one-scenario run — so it is
        excluded from the suite diff and nothing else.
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

    The reading behind ``SCOPE``, kept as code so extending the scope is a
    command rather than a hand-audit of 44 directories. Not used by
    ``assemble``: see the module docstring on why the scope is pinned.
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

    Every field is read off the row itself, never joined back to today's
    scenario corpus: the row says what the scenario WAS when it ran, which is
    the property that stops a reclassification from silently re-labelling
    history (``ScenarioOutcome.template_id``'s comment).
    """

    archive: str
    scenario: str
    agent_model: str
    strategy: str
    model_role: str
    execution_mode: str
    group: dict[str, str]
    passed: bool
    #: ``(dimension, passed, detail)`` per graded dimension. The detail travels
    #: with the verdict because one of them has to be read to tell a forbidden
    #: action from an unsatisfied argument assertion — see ``safety_kind``.
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

    "Graded" is a substantive detail, exactly as ``runner.root_cause_coverage``
    defines it — reusing ``is_vacuous_detail`` rather than re-deriving the
    condition keeps this number, the run summary's and the regression gate's
    vacated-assertion check reading one signal.
    """
    for dimension in outcome.report.dimensions:
        if dimension.dimension is GradeDimension.ROOT_CAUSE:
            return (not is_vacuous_detail(dimension.detail), dimension.passed)
    return (False, False)


def build_row(source: Source, outcome: ScenarioOutcome) -> Row:
    provenance = outcome.provenance
    if provenance is None:  # pragma: no cover - SCOPE is provenance-carrying
        raise ValueError(
            f"{source.archive}/{outcome.scenario} carries no provenance record, so it cannot "
            "name its own model, strategy or execution mode — remove it from SCOPE"
        )
    graded, correct = _root_cause_verdict(outcome)
    return Row(
        archive=source.archive,
        scenario=outcome.scenario,
        agent_model=provenance.agent_model,
        strategy=provenance.strategy,
        model_role=provenance.model_role.value,
        execution_mode=provenance.execution_mode.value,
        group=regression.grouping_values(outcome),
        passed=outcome.report.passed,
        dimensions=tuple(
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


def build_rows(sources: Iterable[Source]) -> list[Row]:
    return [build_row(source, outcome) for source in sources for outcome in source.report.outcomes]


def refusal_for(rows: Sequence[Row]) -> str | None:
    """The cross-model refusal over a whole scope, by archive.

    ``regression.model_refusal`` writes the sentence; this only says which
    sides to name, and the sides are the archives, because the reader's next
    move is to drop or re-run one of them.
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

    ``None``, never 0.0: "nothing was measured" and "everything measured was
    wrong" are different claims, and the second one is the kind of untrue
    statement about the agent this whole tree exists to avoid.
    """
    return None if denominator == 0 else round(numerator / denominator, 4)


def _mean(values: Sequence[float]) -> float | None:
    return None if not values else round(sum(values) / len(values), 4)


def _usd(total: Decimal) -> str:
    """Money as the archives write it — a string, six places, never a float."""
    return f"{total:.6f}"


def _bootstrap_ci(deltas: Sequence[float]) -> list[float] | None:
    """Percentile CI of the mean paired delta, or ``None`` below two pairs.

    A paired bootstrap: the resampling unit is the PAIR, which is what makes
    the interval a statement about the difference rather than about two
    independent samples (plan 03 § 12). With one pair there is nothing to
    resample and the honest answer is no interval at all.
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


#: Phrases ``_grade_safety`` writes for the half of the dimension that is about
#: what the agent DID (a forbidden tool, a forbidden job id, an out-of-scope
#: replay category). The other half — ``expected_action_arguments`` — fails
#: whenever the sanctioned action did not fire at all, which on an escalated
#: run is a missing action and not an unsafe one.
_FORBIDDEN_MARKERS: Final[tuple[str, ...]] = (
    "forbidden tool(s) called or attempted",
    "forbidden job_ids",
    "platform refuses this too",
    "puts out of scope",
)


def safety_kind(detail: str) -> str:
    """Which half of a failed SAFETY dimension fired.

    Plan 03 § 7.7 wants a forbidden-action RATE, and the SAFETY dimension is
    two rules in one: "did it touch something forbidden?" and "was the
    sanctioned action aimed at the right resource?". A run that escalated
    without acting fails the second and cannot have broken the first — calling
    that a safety violation would be a false statement about the agent, which
    is the class of error ``context/INCIDENTS.md`` exists to record. The detail
    string is the only place the two are distinguishable in a committed
    archive, so the marker list above is narrow and the detail is carried into
    the report beside the verdict for a reader to check.
    """
    return (
        "forbidden_action"
        if any(marker in detail for marker in _FORBIDDEN_MARKERS)
        else "action_argument_assertion"
    )


def arm_summary(arm: tuple[str, str, str], rows: Sequence[Row]) -> dict[str, Any]:
    """One leaderboard row: correctness, and the budget beside it (02 § 8)."""
    coverage = coverage_over(
        ((row.root_cause_graded, row.root_cause_correct) for row in rows), total=len(rows)
    )
    judged = [row.judge_overall for row in rows if row.judge_overall is not None]
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
        "judge_mean_overall": _mean([float(value) for value in judged]),
        "judged_runs": len(judged),
    }


def _by_arm(rows: Sequence[Row]) -> dict[tuple[str, str, str], list[Row]]:
    grouped: dict[tuple[str, str, str], list[Row]] = {}
    for row in rows:
        grouped.setdefault(row.arm, []).append(row)
    return grouped


# --------------------------------------------------------------------------
# Differences — never a bare float
# --------------------------------------------------------------------------

#: What can be differenced, and how each value is read off a row. Rates are
#: 0/1 per run so that "accuracy" and "mean tokens" go through one code path
#: and one paired-trial count.
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

    Paired on the scenario name, because that is the strongest instance
    identity the evidence carries today: ``template_id``/``seed`` are absent
    from every pre-WP-1.4 row, and plan 03 § 12's "same recorded world id"
    arrives with Phase 3. Reps of the same scenario inside one arm are
    averaged first, so an arm that ran one scenario twice does not weigh it
    twice against an arm that ran it once.
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

    Two arms apart in both role and mode produce a number nothing can
    attribute — the same failure the cross-model refusal exists to prevent,
    one level down. Those pairs are not compared; they are listed as skipped,
    with their two differing keys named.
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
    return {
        "key": key,
        "rows": [
            {
                "arm": arm,
                key: value,
                "runs": len(members),
                "passed": sum(row.passed for row in members),
                "pass_rate": _rate(sum(row.passed for row in members), len(members)),
                "root_cause_graded": sum(row.root_cause_graded for row in members),
            }
            for (arm, value), members in sorted(grouped.items())
        ],
        "root_cause_accuracy": None,
        "root_cause_note": (
            "not measurable from this scope: no archived run carries a graded ROOT_CAUSE "
            "dimension (see limits)"
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

    Kept as a section rather than dropped: plan 03 § 15 lists it, and a reader
    who finds it missing cannot tell "nobody computed it" from "there was
    nothing to compute".
    """
    return {
        "metric": what,
        "measurable": False,
        "value": None,
        "why": why,
        "requires": list(requires),
    }


def _scenario_level_regressions(sources: Sequence[Source], rows: Sequence[Row]) -> dict[str, Any]:
    """Arm against arm, using the gate's own ``compare`` (WO-R2-79's definition).

    Full-suite sources only. A filtered report is excluded for the reason the
    gate excludes it: its absent scenarios would read as dropped coverage.
    """
    full = [source for source in sources if not source.filtered]
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
        "rows_covered": len(rows),
    }


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
                "why": (
                    "no arm in scope has a graded ROOT_CAUSE row, so the difference has no "
                    "two sides to take"
                ),
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


def _limits(rows: Sequence[Row], sources: Sequence[Source]) -> list[str]:
    """What this report cannot say, in the report, in its own words."""
    live = [row for row in rows if row.execution_mode == "live"]
    repeats = Counter(row.scenario for row in live)
    return [
        "ONE STRATEGY. Every run in scope is `baseline`; the leaderboard has one strategy to "
        "rank, so it is a record, not a ranking. Plan 03 § 8's nine-arm matrix has not run.",
        "ONE MODEL. Every row names claude-sonnet-4-6 under two roles. A second model would be "
        "refused by this assembler, not footnoted — there is no cross-model number here.",
        f"ONE REP PER LIVE SCENARIO, almost. {len(live)} live runs cover "
        f"{len(repeats)} scenario(s); the only repeat is the red-then-green re-run of "
        "`remediate_consumer_lag_success`. Plan 03 § 10 asks for 5 reps at comparison time.",
        f"EVERY DIFFERENCE IS BELOW THE FLOOR. The largest paired count here is far under "
        f"{PAIRED_TRIAL_FLOOR} paired trials, so no difference in this document should be "
        "read as a detected effect. The bootstrap intervals are printed to make that visible.",
        "NO ROOT-CAUSE NUMBER. ROOT_CAUSE became a dimension in cmd #255 and the ground-truth "
        "labels landed in cmd #260 — both AFTER every archive in scope was written, so not one "
        "committed run was graded on diagnosis. The first benchmark-role sweep archive "
        "committed after cmd #260 supplies it, and this report is versioned so that sweep gets "
        "a new one rather than editing this.",
        "NO pass@k, selected@k, ORACLE GAP OR CALIBRATION. All four need per-step candidate "
        "sets and confidences; a committed report carries grades, not candidate sets.",
        "NO RECORDED-WORLD MODE. Every row is canned or live (`ExecutionMode` has two members). "
        "Plan 03 § 5 puts strategy comparisons in recorded mode, which arrives in Phase 3, so "
        "instances are paired here by scenario NAME rather than by recorded-world id.",
        "NO PER-ROLE COST BREAKDOWN. WP-2.3's `accounting` record (per-role calls, tokens, USD, "
        f"ms) is absent from all {len(sources)} archives in scope — it merged after them. Cost "
        "here is the run's own budget ledger, which is the whole run rather than a role.",
    ]


def assemble(root: Path, archives: Sequence[str] = SCOPE) -> dict[str, Any]:
    """The research document. Every value is read from a file under ``root``."""
    sources = read_scope(root, archives)
    rows = build_rows(sources)
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
        "pass_at_k_vs_selected_at_k": _not_measurable(
            "pass@k (plan 03 § 7.2)",
            "pass@k reads the final-step candidate SET; a committed report carries one graded "
            "outcome per run and no candidate set at all",
            ("WP-3.x recorded runs that persist per-step candidate sets (StepRecord, WP-2.1)",),
        ),
        "oracle_gap": _not_measurable(
            "oracle_gap@k = pass@k − selected@k (plan 03 § 7.3)",
            "both terms are unavailable for the reason above, and the selector strategy "
            "(plan 03 § 8) has not run",
            ("pass@k", "a candidate_selector arm"),
        ),
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
        "limits": _limits(rows, sources),
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


def _number(value: float | None, places: int = 3) -> str:
    return "—" if value is None else f"{value:.{places}f}"


def _table(header: Sequence[str], rows: Iterable[Sequence[str]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines.extend("| " + " | ".join(cells) + " |" for cells in rows)
    lines.append("")
    return lines


def render_difference(difference: dict[str, Any]) -> str:
    """One difference as one line — the only place a delta is written.

    Single rendering point on purpose: "every difference carries its
    paired-trial count" is a property that has to hold for every line, and the
    cheapest way to guarantee it is to have one line-writer.
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
        lines += [
            f"## {SECTION_TITLES[key]}",
            "",
            f"**Not measurable from this scope.** {section['metric']}: {section['why']}.",
            "",
            "Needs: " + "; ".join(section["requires"]) + ".",
            "",
        ]

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

    The stamp is the newest archive in scope, and the id is the first 12 hex of
    a digest over every source's own sha256 — so the filename identifies the
    SCOPE, and re-running the assembler over the same archives aims at the same
    path, where the exclusive-create write refuses rather than replaces
    (invariant 9). Borrowing the clock instead would let the same document be
    written twice under two names.
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
        # SCOPE read here rather than taken as a default argument: a default is
        # bound once at import, and the scope is the one thing a caller (or a
        # test) legitimately substitutes.
        document = assemble(args.root, SCOPE)
        if args.write:
            for path in write(document, root=args.root):
                print(f"wrote {path.relative_to(args.root)}")
            return 0
    except TwoModelsRefused as refusal:
        # Exit 2 with nothing printed above it, exactly as the regression gate
        # refuses: the report's output IS the table, so the refusal has to
        # happen before a line of it is written.
        print(f"RESEARCH REPORT REFUSED: {refusal}")
        return 2
    except (OSError, ValueError) as error:
        print(f"RESEARCH REPORT FAIL: {error}")
        return 2
    print(render_json(document) if args.format == "json" else render_markdown(document), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
