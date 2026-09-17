"""Assemble the aggregate research report (plan 03 § 15) from committed archives.

The report every phase close reads: one leaderboard per model, the corpus
sliced by the seven keys of WP-2.5, and every difference printed beside the
number of paired trials it was computed from. Like ``evals/baseline_report.py``
and ``evals/phase_close_report.py``, nothing here runs an eval and nothing is
typed in — every number is read out of a committed ``evals/runs/<id>/report.json``
— which is what lets a test regenerate the committed artifact byte for byte.

Five decisions are load-bearing.

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
left for the reader to notice: one strategy, one model, roughly one rep per
live scenario, no recorded-world mode until Phase 3, and a root-cause column
that now has numbers in it but a small denominator — the first version of this
report had none at all, because ROOT_CAUSE (WP-2.2, cmd #255) and the
ground-truth labels (WO-R3-261, cmd #260) landed after every archive it
covered. The Phase 2 archives supply it, and ``limits`` says exactly which
rows are graded and which of three reasons excuses the rest.

**One archive's diagnosis grades are read from somewhere else.** The live
read-only pass graded canned-world labels against an unseeded live world
(INC-003); ADR 0040 withdrew those verdicts and an offline re-grade replaced
them. ``SUPERSEDED_ROOT_CAUSE`` maps that archive to the re-grade, and
``build_rows`` substitutes on the way in — the archive is never edited
(invariant 9) and the withdrawn figure never reaches a table.
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
from evals.candidate_metrics import measure, steps_of
from evals.graders.deterministic import GradeDimension, is_vacuous_detail
from evals.graders.root_cause import coverage_over
from evals.runner import RunReport, ScenarioOutcome
from evals.scenarios.loader import load_scenarios
from evals.tracing import TraceKind
from incident_commander.agent.hypothesis import HypothesisCategory

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
    # --- added by the Phase 2 close (WO-R3-195) ---
    # Canned full suite, benchmark role, the first sweep run AFTER the
    # ground-truth labels landed (cmd #260). This is the archive that ends
    # "no root-cause number": 32 of its 41 rows carry a graded diagnosis.
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
    # The two re-runs of those reds, after their fixes merged: the stale-cache
    # sensor (platform v0.6.8, commander cmd #269) and ADR 0041's whole-queue
    # rule (cmd #270). Both green. They sit BESIDE the reds rather than in place
    # of them — same arm, same scenario, one change between the two runs — so a
    # per-scenario pairing in this table has two rows for each of them and the
    # pass rate over the seeded legs is 3 of 5, not 3 of 3.
    "d16aa18dce08",
    "42c675d9c145",
)

#: Archives whose ROOT_CAUSE verdicts have been withdrawn and replaced by a
#: committed offline re-grade, mapped to the document that replaces them.
#:
#: `0db6fe722f7c` graded its diagnoses against labels written for each
#: scenario's CANNED world while running against an unseeded LIVE one
#: (INC-003), which made seven correct "nothing is wrong here" answers read as
#: misdiagnoses. ADR 0040 scoped a label to the world it describes, and the
#: archive was re-graded offline at no cost. The archive itself is untouched
#: and stays untouched (invariant 9) — the substitution happens here, on the
#: way into the table, so that this report never restates a number the project
#: has formally withdrawn. The re-grade's verdicts are read, not recomputed:
#: it is the reviewed answer for that run and a second opinion assembled here
#: would be exactly the kind of quiet re-scoring this module exists to avoid.
SUPERSEDED_ROOT_CAUSE: Final[dict[str, str]] = {
    "0db6fe722f7c": "evals/reports/regrades/regrade_report.20260917T133824Z.0db6fe722f7c.json",
}

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


@dataclass(frozen=True)
class Regraded:
    """One scenario's verdict as the superseding re-grade states it."""

    passed: bool
    dimensions: tuple[tuple[str, bool, str], ...]
    root_cause_graded: bool
    root_cause_correct: bool


def regraded_verdicts(root: Path, archive: str) -> dict[str, Regraded]:
    """``{scenario: Regraded}`` from the re-grade that supersedes an archive.

    Read out of the committed re-grade document rather than recomputed, and
    scored with the same ``is_vacuous_detail`` test ``_root_cause_verdict``
    uses, so "graded" means the same thing on both sides of the substitution.
    The whole row is replaced, not only the ROOT_CAUSE cell: a withdrawn
    dimension verdict changes whether the ROW passed, and a pass rate computed
    from one and a diagnosis column computed from the other would be two
    different runs printed side by side.
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


def step_records_in(root: Path, archive: str) -> dict[str, list[dict[str, Any]]]:
    """Every ``step`` trace record in one archive, by scenario name (WP-5.2).

    An archive's ``traces/<scenario>.jsonl`` holds the per-step research records
    when the run had ``EVAL_TRACE_DIR`` set; a canned sweep does not, so most
    archives have no ``traces/`` directory at all and this returns nothing. That
    absence is why ``_pass_at_k`` below still reports the metric as not
    measurable over today's scope — the code path exists, and there is no data
    in scope for it to read.

    Malformed lines are skipped rather than raising. A trace file is append-only
    evidence written across a run that can be killed mid-line (F-002's cousin),
    and a half-written last line must not take out a report over ten archives.
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

    Read from the corpus rather than from an archive: the labels are the
    evaluator's (ADR 0038) and the archive holds the agent's answers. A scenario
    that declares none is absent here, and a run of it is *not graded* rather
    than scored a miss (ADR 0040).
    """
    return {
        scenario.name: scenario.ground_truth.root_causes
        for scenario in load_scenarios(root / "evals" / "scenarios")
        if scenario.ground_truth is not None
    }


#: The smallest candidate set this section will report a pass@k over.
#:
#: Three live archives in scope already carry ``step`` records — ``baseline``
#: wrote them, one candidate each — so "no candidate sets exist" stopped being
#: the reason this section is unmeasurable the moment WP-2.1 landed. The reason
#: now is sharper and it is this constant: **pass@1 over a one-candidate set is
#: the ROOT_CAUSE dimension under a second name.** It asks "was the top
#: diagnosis correct", which the report already answers, and printing it here as
#: pass@k would put one measurement in the table twice — the same relabelling
#: the ``calibration`` section refuses when it declines to call the briefing
#: judge's scores a calibration. pass@k exists to say what ENUMERATION bought,
#: so it is reported for the arms that enumerate.
#:
#: Pinned by ``tests/unit/test_candidate_metrics.py::
#: TestTheReportSectionScopesItselfToEnumeratingArms``, both ways.
ENUMERATING_SET_SIZE: Final[int] = 2


def _candidate_rows(root: Path, sources: Sequence[Source]) -> list[dict[str, Any]]:
    """One pass@k row per (archive, scenario) whose trace carries enumerated sets.

    Empty over today's scope: every ``step`` record in it was written by
    ``baseline``, whose sets hold one candidate — see ``ENUMERATING_SET_SIZE``.
    The section below therefore still reports itself unmeasurable, and becomes
    measurable the moment a best-of-N arm's archive enters the scope, without
    another packet editing this file.
    """
    rows: list[dict[str, Any]] = []
    truths: dict[str, tuple[HypothesisCategory, ...]] | None = None
    for source in sources:
        for scenario, records in step_records_in(root, source.archive).items():
            if truths is None:
                truths = _ground_truths(root)
            expected = truths.get(scenario)
            if expected is None:
                # No label: not graded, not a miss. Counted nowhere rather than
                # counted as zero — the mistake INC-003 cost $2.15 to learn.
                continue
            metrics = measure(records, expected)
            if metrics.candidates_generated == 0:
                continue
            if max(len(step.candidates) for step in steps_of(records)) < ENUMERATING_SET_SIZE:
                continue
            rows.append(
                {
                    "archive": source.archive,
                    "scenario": scenario,
                    "strategy": metrics.strategy,
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
                }
            )
    return rows


def _pass_at_k(root: Path, sources: Sequence[Source]) -> dict[str, Any]:
    """pass@k, appeared-at-any-step and the two duplicate rates (plan 03 § 7.2).

    Measured when an archive in scope persisted per-step candidate sets; the
    same "not measurable, and here is what it needs" block as before when none
    did, which is every archive in scope today. Selected@k is absent from both
    branches for the reason the ``oracle_gap`` section gives: no selector arm
    has run (plan 02 § 12, Phase 6).
    """
    rows = _candidate_rows(root, sources)
    if not rows:
        return _not_measurable(
            "pass@k (plan 03 § 7.2)",
            "pass@k reads the final-step candidate SET; a committed report carries one graded "
            "outcome per run and no candidate set at all",
            ("WP-3.x recorded runs that persist per-step candidate sets (StepRecord, WP-2.1)",),
        )
    return {
        "metric": "pass@k (plan 03 § 7.2)",
        "measurable": True,
        "value": {
            "rows": rows,
            "selected_at_k": None,
            "selected_at_k_why": (
                "no candidate_selector arm has run (plan 02 § 12, Phase 6), so the "
                "oracle gap has one term and is reported as unmeasurable beside it"
            ),
        },
        "why": "",
        "requires": [],
    }


def _scenario_level_regressions(sources: Sequence[Source], rows: Sequence[Row]) -> dict[str, Any]:
    """Arm against arm, using the gate's own ``compare`` (WO-R2-79's definition).

    Full-suite sources only, and "full" is two exclusions rather than one.

    A FILTERED report is excluded for the reason the gate excludes it: its
    absent scenarios would read as dropped coverage.

    A PARTIAL report is excluded for the same reason one layer out. A run that
    covers a subset of the corpus without being filtered — the read-only smoke
    pass is the example: it selects the read-only scenarios by scope, not by
    ``--only`` — produces the identical distortion. Comparing it to a full
    sweep printed "7 regressions, 14 dropped scenarios", and those seven were
    precisely the verdicts INC-003 withdrew: a live unseeded world graded
    against labels written for a canned one. The scenarios it does not contain
    are missing, not failed, and the world it ran in is not the world the other
    side ran in.
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

    It used to be "nothing is graded". That stopped being true in Phase 2, and
    a stale reason is worse than none — so the sentence is rebuilt from the
    rows, and it now names the real obstacle: the graded rows do not PAIR.
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

    Every number in this sentence is counted, not typed, because the point of
    the sentence is that the denominator is small and the reasons it is small
    are three different things.
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
    # Only archives actually in scope: the map is a repository-wide fact, the
    # count is a fact about this document.
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


def _render_candidate_rows(value: dict[str, Any]) -> list[str]:
    """The pass@k table, when there was a candidate set to compute one from.

    Prints every k that was asked for beside the ``effective_k`` the run could
    answer, because pass@8 over a 4-candidate set is pass@4 wearing a bigger
    number, and a table that hid the cap would invite exactly that reading. The
    two duplicate rates are printed as two columns for the reason
    ``evals/candidate_metrics.py`` keeps them apart: one is a schema refusal and
    one is a modelling finding.
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
