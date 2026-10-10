"""The oracle-gap sample (WO-R3-347): run one batch of recorded selector runs, then report it.

A batch replays every world in ``evals/samples/oracle_gap.json`` once, each pinned to its
recording, under the ``candidate_selector`` arm. The report gives pass@k, selected@k and
oracle_gap@k (plan 03 § 7.3) per world and pooled, with n beside every number. Why this sample
and what it cannot claim: the hub's ``docs/plans/research-buildout-v2.1/samples/oracle-gap.md``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import statistics
import subprocess
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

from evals import artifacts, recorded_applies, research_report
from evals.candidate_metrics import REPORTED_KS, WorldKey, measure, measure_selection, world_key
from evals.recorded_client import matching_recordings
from evals.runner import ScenarioOutcome
from evals.scenarios.loader import load_scenarios
from evals.scenarios.schema import Scenario
from evals.tracing import TraceKind
from incident_commander.agent.hypothesis import HypothesisCategory
from incident_commander.config import ModelRole

REPO_ROOT: Final[Path] = artifacts.REPO_ROOT
PLAN_PATH: Final[Path] = REPO_ROOT / "evals" / "samples" / "oracle_gap.json"

#: A pinned recording is its invocation id. A scenario name would resolve to whatever is newest.
RECORDING_ID: Final[re.Pattern[str]] = re.compile(r"\A[0-9a-f]{12}\Z")
#: Where the runner writes traces; the archive keeps this invocation's slice of them.
TRACE_DIR: Final[str] = "evals/traces"
#: The runner's exit codes that mean "it ran": 0 all passed, 1 some failed their grade.
RAN: Final[frozenset[int]] = frozenset({0, 1})

#: Worlds are resampled, not runs: four runs of one world are not four independent worlds.
BOOTSTRAP_RESAMPLES: Final[int] = 2000
BOOTSTRAP_SEED: Final[int] = 347
BOOTSTRAP_CONFIDENCE: Final[float] = 0.95

#: The prefix of the detail a crashed run's only dimension carries (``runner._crashed_result``).
_CRASHED: Final[str] = "scenario crashed:"


class SampleWorld(BaseModel):
    """One world of the sample: a scenario and the recording it is pinned to."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scenario: str = Field(min_length=1)
    recording: str = Field(min_length=1)


class SamplePlan(BaseModel):
    """The committed sample: which worlds, which arm, and the budget the arm runs under."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    study: str
    work_order: str
    design: str
    strategy: str
    generator: str
    n: int = Field(ge=2, le=8)
    model_role: ModelRole
    token_multiplier: Decimal = Field(gt=0)
    usd_multiplier: Decimal = Field(gt=0)
    batches: int = Field(ge=1)
    worlds: tuple[SampleWorld, ...] = Field(min_length=1)

    @property
    def arm(self) -> str:
        """The arm key the calibration register is keyed by (``selector_arm_key``'s spelling)."""
        return f"{self.strategy}/{self.generator}/n={self.n}"


def load_plan(path: Path = PLAN_PATH) -> SamplePlan:
    return SamplePlan.model_validate_json(path.read_text(encoding="utf-8"))


def plan_refusals(plan: SamplePlan, worlds: Sequence[SampleWorld]) -> list[str]:
    """Every reason these worlds cannot run yet; empty when they can. Spends nothing."""
    corpus = {s.name: s for s in load_scenarios(REPO_ROOT / "evals" / "scenarios")}
    refusals: list[str] = []
    names = [world.scenario for world in plan.worlds]
    for repeated in sorted({name for name in names if names.count(name) > 1}):
        refusals.append(f"{repeated} is listed twice in the plan; one world per scenario.")
    for world in worlds:
        scenario = corpus.get(world.scenario)
        if scenario is None:
            refusals.append(f"{world.scenario} is not a scenario in evals/scenarios/.")
            continue
        if scenario.ground_truth is None:
            refusals.append(f"{world.scenario} declares no ground truth, so nothing can score it.")
        if not RECORDING_ID.match(world.recording):
            refusals.append(
                f"{world.scenario}: {world.recording!r} is not a recording id (12 hex "
                "characters, the last segment of the recording's filename). Fill it in."
            )
            continue
        if world.scenario not in matching_recordings(world.recording, [world.scenario]):
            refusals.append(
                f"{world.scenario}: no recording {world.recording} under "
                f"evals/recorded_worlds/{world.scenario}/."
            )
            continue
        applies = recorded_applies.label_applies(world.scenario, world.recording)
        if applies is not True:
            refusals.append(
                f"{world.scenario}: the answer key beside recording {world.recording} does not "
                f"describe that world (applies={applies}), so its runs would not be graded "
                "(ADR 0040). Leave this world out of the sample."
            )
    return refusals


def batch_invocation(
    plan: SamplePlan, worlds: Sequence[SampleWorld]
) -> tuple[list[str], dict[str, str]]:
    """The runner's arguments and the environment the arm needs, for one batch over ``worlds``."""
    argv = [
        "--mode",
        "recorded",
        "--model-role",
        plan.model_role.value,
        "--only",
        ",".join(world.scenario for world in worlds),
    ]
    for world in worlds:
        argv += ["--world", world.recording]
    env = {
        "INFERENCE_STRATEGY": plan.strategy,
        "SELECTOR_GENERATOR": plan.generator,
        "BEST_OF_N": str(plan.n),
        "TOKEN_BUDGET_MULTIPLIER": str(plan.token_multiplier),
        "USD_BUDGET_MULTIPLIER": str(plan.usd_multiplier),
        "EVAL_TRACE_DIR": TRACE_DIR,
    }
    return argv, env


def worlds_of(plan: SamplePlan, only: Sequence[str]) -> tuple[list[SampleWorld], str]:
    """The plan's worlds, or the named subset of them (a retry), or a refusal."""
    if not only:
        return list(plan.worlds), ""
    unknown = sorted(set(only) - {world.scenario for world in plan.worlds})
    if unknown:
        return [], f"not in the sample plan: {', '.join(unknown)}"
    return [world for world in plan.worlds if world.scenario in only], ""


# --------------------------------------------------------------------------
# One run, scored
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class RunRow:
    """One scored run: both terms of the gap at every k, off the run's own step records."""

    archive: str
    scenario: str
    recording: str
    world: str
    agent_model: str
    pass_hit: Mapping[int, bool | None]
    selected_hit: Mapping[int, bool | None]
    gap: Mapping[int, int | None]
    decision: str
    uncertainty: float | None


@dataclass(frozen=True, slots=True, kw_only=True)
class Excluded:
    """A run the report does not score, and why — never silently dropped."""

    archive: str
    scenario: str
    reason: str


def run_row(
    *,
    archive: str,
    scenario: str,
    recording: str,
    world: WorldKey,
    agent_model: str,
    records: Sequence[Mapping[str, Any]],
    expected: Iterable[HypothesisCategory],
    ks: Sequence[int] = REPORTED_KS,
) -> RunRow:
    """Score one run's step records against the scenario's ground truth."""
    labels = tuple(expected)
    generation = measure(records, labels, ks)
    selection = measure_selection(records, labels, world=world, ks=ks)
    return RunRow(
        archive=archive,
        scenario=scenario,
        recording=recording,
        world=str(world),
        agent_model=agent_model,
        pass_hit={entry.k: entry.hit for entry in generation.pass_at_k},
        selected_hit={entry.k: entry.hit for entry in selection.selected_at_k},
        gap={entry.k: entry.gap for entry in selection.oracle_gap},
        decision=selection.decision,
        uncertainty=selection.uncertainty,
    )


def step_records(root: Path, archive: str, scenario: str) -> list[dict[str, Any]]:
    """The ``step`` records of one scenario in one archive, read by name (never a glob)."""
    path = research_report.archive_dir(root, archive) / "traces" / f"{scenario}.jsonl"
    if not path.is_file():
        return []
    found: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and record.get("kind") == TraceKind.STEP.value:
            found.append(record)
    return found


def billed_usd(outcome: ScenarioOutcome) -> Decimal | None:
    """What a run was billed, judge included; ``None`` when the row carries no measurement."""
    if outcome.accounting is not None:
        return Decimal(str(outcome.accounting.usd_used))
    if outcome.provenance is not None:
        return Decimal(str(outcome.provenance.budget.usd_used))
    return None


def why_not_scored(
    outcome: ScenarioOutcome,
    plan: SamplePlan,
    pins: Mapping[str, str],
    corpus: Mapping[str, Scenario],
) -> str:
    """The reason one archived run is left out of the numbers, or ``""`` when it is scored."""
    # 1. A crash is a harness failure: the row says nothing about the model.
    crashed = [d.detail for d in outcome.report.dimensions if d.detail.startswith(_CRASHED)]
    if crashed:
        return f"harness failure: {crashed[0]}"
    # 2. It has to be this study's run: a sampled world, recorded, the plan's arm and recording.
    if outcome.scenario not in pins:
        return "not a world in the sample plan"
    provenance = outcome.provenance
    if provenance is None or provenance.execution_mode.value != "recorded":
        return "not a recorded-mode run"
    arm = research_report.selector_arm_key(provenance.strategy, dict(provenance.strategy_config))
    if arm != plan.arm:
        return f"ran arm {arm}, the plan's arm is {plan.arm}"
    recording = recorded_applies.recording_of(outcome)
    if recording != pins[outcome.scenario]:
        return f"replayed recording {recording}, the plan pins {pins[outcome.scenario]}"
    # 3. A replay miss makes the run not comparable: it acted on an error the world never sent.
    if outcome.degraded:
        replay = outcome.replay or {}
        return (
            f"not comparable: the replay missed {replay.get('misses', '?')} call(s) and refused "
            f"{len(replay.get('refusals') or ())} (ADR 0047) — re-record the world wider"
        )
    # 4. A label that does not describe this recording's world is not graded (ADR 0040); the
    #    rule research_report applies too, so the two reports cannot disagree.
    if reason := recorded_applies.why_key_does_not_apply(outcome):
        return reason
    scenario = corpus.get(outcome.scenario)
    if scenario is None or scenario.ground_truth is None:
        return "no ground truth to score against"
    return ""


def rows_of(
    root: Path, archive: str, plan: SamplePlan, corpus: Mapping[str, Scenario]
) -> tuple[list[RunRow], list[Excluded], list[Decimal | None], dict[str, Any]]:
    """Every run in one archive, scored or excluded, what each was billed, and its scope entry."""
    source = research_report.read_source(root, archive)
    pins = {world.scenario: world.recording for world in plan.worlds}
    rows: list[RunRow] = []
    excluded: list[Excluded] = []
    billed: list[Decimal | None] = []
    for outcome in source.report.outcomes:
        billed.append(billed_usd(outcome))
        reason = why_not_scored(outcome, plan, pins, corpus)
        records = [] if reason else step_records(root, archive, outcome.scenario)
        if not reason and not records:
            reason = f"no step records in the archive (was {TRACE_DIR} set as EVAL_TRACE_DIR?)"
        if reason:
            excluded.append(Excluded(archive=archive, scenario=outcome.scenario, reason=reason))
            continue
        provenance = outcome.provenance
        truth = corpus[outcome.scenario].ground_truth
        assert provenance is not None and truth is not None  # checked by why_not_scored
        rows.append(
            run_row(
                archive=archive,
                scenario=outcome.scenario,
                recording=pins[outcome.scenario],
                world=world_key(
                    scenario=outcome.scenario,
                    execution_mode=provenance.execution_mode.value,
                    archive=archive,
                    world_fingerprint=research_report.recorded_fingerprint(outcome),
                ),
                agent_model=provenance.agent_model,
                records=records,
                expected=truth.root_causes,
            )
        )
    scope = {
        "archive": archive,
        "sha256": source.sha256,
        "generated_at": source.report.generated_at.isoformat(),
        "runs": len(source.report.outcomes),
    }
    return rows, excluded, billed, scope


# --------------------------------------------------------------------------
# The numbers
# --------------------------------------------------------------------------


def _rate(count: int, n: int) -> float | None:
    return None if n == 0 else round(count / n, 4)


def at_k(rows: Sequence[RunRow], k: int) -> dict[str, Any]:
    """pass@k, selected@k and the gap at one k, as counts over n scored runs and as rates."""
    scored = [row for row in rows if row.gap.get(k) is not None]
    passed = sum(1 for row in scored if row.pass_hit.get(k))
    selected = sum(1 for row in scored if row.selected_hit.get(k))
    gaps = sum(row.gap.get(k) or 0 for row in scored)
    return {
        "k": k,
        "n": len(scored),
        "pass_at_k": passed,
        "selected_at_k": selected,
        "oracle_gap": gaps,
        "pass_rate": _rate(passed, len(scored)),
        "selected_rate": _rate(selected, len(scored)),
        "gap_rate": _rate(gaps, len(scored)),
    }


def gap_interval(rows: Sequence[RunRow], k: int) -> list[float] | None:
    """95% interval of the pooled gap rate, resampling whole worlds; ``None`` below two worlds."""
    clusters: dict[str, list[int]] = {}
    for row in rows:
        gap = row.gap.get(k)
        if gap is not None:
            clusters.setdefault(row.world, []).append(gap)
    worlds = list(clusters.values())
    if len(worlds) < 2:
        return None
    rng = random.Random(BOOTSTRAP_SEED)  # noqa: S311 - resampling, not cryptography
    rates = []
    for _ in range(BOOTSTRAP_RESAMPLES):
        picks = [worlds[rng.randrange(len(worlds))] for _ in worlds]
        rates.append(sum(sum(pick) for pick in picks) / sum(len(pick) for pick in picks))
    rates.sort()
    tail = (1.0 - BOOTSTRAP_CONFIDENCE) / 2.0
    low = rates[int(tail * BOOTSTRAP_RESAMPLES)]
    high = rates[min(int((1.0 - tail) * BOOTSTRAP_RESAMPLES), BOOTSTRAP_RESAMPLES - 1)]
    return [round(low, 4), round(high, 4)]


def gap_by_decision(rows: Sequence[RunRow], k: int) -> dict[str, int]:
    """The runs that lost an available answer, by what the selector did at the deciding step."""
    counted: dict[str, int] = {}
    for row in rows:
        if row.gap.get(k) == 1:
            counted[row.decision or "none"] = counted.get(row.decision or "none", 0) + 1
    return dict(sorted(counted.items()))


def selector_vs_top_candidate(rows: Sequence[RunRow], k: int) -> dict[str, int]:
    """Was the selector's pick right where the generator's own top candidate was, and vice versa?"""
    both = [r for r in rows if r.pass_hit.get(1) is not None and r.selected_hit.get(k) is not None]
    return {
        "n": len(both),
        "both_right": sum(1 for r in both if r.pass_hit[1] and r.selected_hit[k]),
        "only_selector_right": sum(1 for r in both if not r.pass_hit[1] and r.selected_hit[k]),
        "only_top_candidate_right": sum(1 for r in both if r.pass_hit[1] and not r.selected_hit[k]),
        "both_wrong": sum(1 for r in both if not r.pass_hit[1] and not r.selected_hit[k]),
    }


def summarise(rows: Sequence[RunRow], *, headline_k: int) -> dict[str, Any]:
    """Per world and pooled at every reported k, with the pooled interval and the two splits."""
    per_world: dict[tuple[str, str], list[RunRow]] = {}
    for row in rows:
        per_world.setdefault((row.scenario, row.recording), []).append(row)
    return {
        "per_world": [
            {
                "scenario": scenario,
                "recording": recording,
                "runs": len(members),
                "at_k": [at_k(members, k) for k in REPORTED_KS],
            }
            for (scenario, recording), members in sorted(per_world.items())
        ],
        "pooled": {
            "runs": len(rows),
            "worlds": len(per_world),
            "at_k": [
                at_k(rows, k) | {"gap_interval_95": gap_interval(rows, k)} for k in REPORTED_KS
            ],
            "gap_by_decision": gap_by_decision(rows, headline_k),
            "selector_vs_top_candidate": selector_vs_top_candidate(rows, headline_k),
        },
    }


_SELECTOR_FIELDS: Final[tuple[str, ...]] = (
    "selected_at_k",
    "oracle_gap",
    "selected_rate",
    "gap_rate",
    "gap_interval_95",
)


def withhold_selector_numbers(summary: dict[str, Any]) -> dict[str, Any]:
    """The summary with every selector number replaced by the sentence saying why (plan 02:243)."""

    def gated(entry: dict[str, Any]) -> dict[str, Any]:
        return {
            key: (research_report.WITHHELD if key in _SELECTOR_FIELDS else value)
            for key, value in entry.items()
        }

    pooled = summary["pooled"]
    return {
        "per_world": [
            world | {"at_k": [gated(entry) for entry in world["at_k"]]}
            for world in summary["per_world"]
        ],
        "pooled": pooled
        | {
            "at_k": [gated(entry) for entry in pooled["at_k"]],
            "gap_by_decision": research_report.WITHHELD,
            "selector_vs_top_candidate": research_report.WITHHELD,
        },
    }


def spend(billed: Sequence[Decimal | None]) -> dict[str, Any]:
    """What the runs in scope were billed, judge included — every run, scored or not."""
    known = [value for value in billed if value is not None]
    return {
        "runs": len(billed),
        "runs_with_a_bill": len(known),
        "usd_total": str(sum(known, Decimal("0"))),
        "usd_median": None if not known else str(statistics.median(known)),
        "usd_max": None if not known else str(max(known)),
    }


def limits(document: Mapping[str, Any]) -> list[str]:
    """What this report cannot claim, in its own words, from its own numbers."""
    pooled = document["summary"]["pooled"]
    headline = next(entry for entry in pooled["at_k"] if entry["k"] == document["headline_k"])
    return [
        f"One model ({document['agent_model']}), one arm ({document['arm']}), recorded mode only: "
        "diagnosis is graded; outcome, action and safety are not claims a replay can make "
        "(ADR 0047).",
        f"n = {headline['n']} scored runs over {pooled['worlds']} worlds — below plan 03 § 10's "
        f"floor of ~{research_report.PAIRED_TRIAL_FLOOR} paired trials. The interval resamples "
        "worlds, so it speaks for worlds like these, not for the whole corpus.",
        "Per-world rows are a handful of runs each: read them as descriptions, never as rates.",
        f"pass@{document['headline_k']} is close to its ceiling by construction when "
        f"{document['headline_k']} candidates cover every cause a family can have; the gap is "
        "then mostly a statement about the selector.",
        "A gap counts every run whose correct candidate was available and not taken, "
        "including a selector that escalated or asked for more; the split by decision says which.",
        f"{len(document['excluded'])} run(s) are excluded and listed with the reason; none is "
        "counted as a miss.",
    ]


def assemble(
    archives: Sequence[str], plan: SamplePlan, *, root: Path = REPO_ROOT
) -> dict[str, Any]:
    """The report over the given batch archives: per world, pooled, excluded runs and spend."""
    # 1. Score or exclude every run in every archive, keeping what each was billed.
    corpus = {s.name: s for s in load_scenarios(REPO_ROOT / "evals" / "scenarios")}
    rows: list[RunRow] = []
    excluded: list[Excluded] = []
    billed: list[Decimal | None] = []
    scope: list[dict[str, Any]] = []
    for archive in archives:
        found, left_out, bills, entry = rows_of(root, archive, plan, corpus)
        rows += found
        excluded += left_out
        billed += bills
        scope.append(entry)
    # 2. One model per table: a gap across two models is two findings added together.
    models = sorted({row.agent_model for row in rows})
    if len(models) > 1:
        raise ValueError(f"two agent models in one oracle-gap report: {', '.join(models)}")
    # 3. Selector numbers are withheld until the arm has a calibration report made on the model
    #    the selector ran on (plan 02:243); the generation half is never withheld.
    agent_model = models[0] if models else "none scored"
    calibration = research_report.calibration_gate(plan.arm, agent_model, root=root)
    summary = summarise(rows, headline_k=plan.n)
    document: dict[str, Any] = {
        "study": plan.study,
        "work_order": plan.work_order,
        "design": plan.design,
        "arm": plan.arm,
        "agent_model": agent_model,
        "headline_k": plan.n,
        "planned": {"worlds": len(plan.worlds), "batches": plan.batches},
        "scope": {"archives": scope},
        "calibration": calibration,
        "selector_gate": research_report.SELECTOR_GATE_RULE,
        "summary": summary if calibration["opens"] else withhold_selector_numbers(summary),
        "excluded": [
            {"archive": e.archive, "scenario": e.scenario, "reason": e.reason} for e in excluded
        ],
        "spend": spend(billed),
    }
    document["limits"] = limits(document)
    return document


# --------------------------------------------------------------------------
# Rendering and writing
# --------------------------------------------------------------------------


def _cell(value: Any) -> str:
    if value is None:
        return "—"
    if value == research_report.WITHHELD:
        return "withheld"
    return str(value)


def render_markdown(document: Mapping[str, Any]) -> str:
    """The same document for a person: pooled first, then each world, then what is left out."""
    pooled = document["summary"]["pooled"]
    k = document["headline_k"]
    lines = [
        f"# Oracle gap — {document['arm']} on {document['agent_model']}",
        "",
        f"{document['work_order']}. Design: {document['design']}.",
        f"Archives: {', '.join(entry['archive'] for entry in document['scope']['archives'])}.",
        f"Calibration report for this arm: {document['calibration']['report_id'] or 'NONE'}"
        + (
            ""
            if document["calibration"]["opens"]
            else f" — every selector number is withheld: {document['calibration']['why_not']}."
        ),
        "",
        f"## Pooled ({pooled['runs']} scored runs, {pooled['worlds']} worlds)",
        "",
        "| k | n | pass@k | selected@k | oracle gap | gap 95% interval (worlds resampled) |",
        "|---|---|---|---|---|---|",
    ]
    for entry in pooled["at_k"]:
        lines.append(
            f"| {entry['k']} | {entry['n']}"
            f" | {_cell(entry['pass_at_k'])} ({_cell(entry['pass_rate'])})"
            f" | {_cell(entry['selected_at_k'])} ({_cell(entry['selected_rate'])})"
            f" | {_cell(entry['oracle_gap'])} ({_cell(entry['gap_rate'])})"
            f" | {_cell(entry['gap_interval_95'])} |"
        )
    lines += [
        "",
        f"Runs that lost an available answer at k={k}, by the selector's decision: "
        f"{_cell(pooled['gap_by_decision'])}.",
        "Selector vs the generator's own top candidate: "
        f"{_cell(pooled['selector_vs_top_candidate'])}.",
        "",
        f"## Per world (k={k})",
        "",
        "| scenario | recording | runs | n | pass@k | selected@k | oracle gap |",
        "|---|---|---|---|---|---|---|",
    ]
    for world in document["summary"]["per_world"]:
        entry = next(item for item in world["at_k"] if item["k"] == k)
        lines.append(
            f"| {world['scenario']} | {world['recording']} | {world['runs']} | {entry['n']}"
            f" | {_cell(entry['pass_at_k'])} | {_cell(entry['selected_at_k'])}"
            f" | {_cell(entry['oracle_gap'])} |"
        )
    spent = document["spend"]
    lines += [
        "",
        f"## Spend: ${spent['usd_total']} over {spent['runs']} runs "
        f"(median ${spent['usd_median']}, max ${spent['usd_max']} per run)",
        "",
        f"## Excluded ({len(document['excluded'])})",
        "",
        *(f"- {e['archive']} {e['scenario']}: {e['reason']}" for e in document["excluded"]),
        "",
        "## What this cannot claim",
        "",
        *(f"- {line}" for line in document["limits"]),
        "",
    ]
    return "\n".join(lines)


def render_json(document: Mapping[str, Any]) -> str:
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def write(document: Mapping[str, Any], *, root: Path = REPO_ROOT) -> tuple[Path, Path]:
    """Exclusive-create the JSON and Markdown halves, named for the scope (invariant 9)."""
    archives = document["scope"]["archives"]
    newest = max(entry["generated_at"] for entry in archives)
    digest = hashlib.sha256(
        "\n".join(f"{entry['archive']}:{entry['sha256']}" for entry in archives).encode()
    ).hexdigest()[:12]
    timestamp = datetime.fromisoformat(newest)
    return (
        artifacts.write_versioned(
            "oracle_gap_report",
            content=render_json(document),
            timestamp=timestamp,
            invocation_id=digest,
            root=root,
        ),
        artifacts.write_versioned(
            "oracle_gap_report_md",
            content=render_markdown(document),
            timestamp=timestamp,
            invocation_id=digest,
            root=root,
        ),
    )


# --------------------------------------------------------------------------
# The batch
# --------------------------------------------------------------------------


def run_batch(
    plan: SamplePlan, *, only: Sequence[str] = (), yes_spend: bool, root: Path = REPO_ROOT
) -> int:
    """Run one batch through the runner and write its report; without ``yes_spend``, only check."""
    # 1. Which worlds: the whole plan, or the named subset when re-running after a harness failure.
    worlds, refusal = worlds_of(plan, only)
    if refusal:
        print(f"ORACLE GAP REFUSED: {refusal}. Nothing was spent.")
        return 2
    # 2. Refuse a plan that cannot run yet: placeholder ids, unknown recordings, keys that
    #    do not apply to their recording's world.
    refusals = plan_refusals(plan, worlds)
    if refusals:
        print("ORACLE GAP REFUSED: the sample plan cannot run yet. Nothing was spent.")
        print("\n".join(f"  - {line}" for line in refusals))
        return 2
    # 3. Print exactly what would run. Without --yes-spend that is all: the free dry run of a batch.
    argv, env = batch_invocation(plan, worlds)
    print(
        f"batch: {len(worlds)} world(s), one recorded run each, arm {plan.arm}, "
        f"role {plan.model_role.value}"
    )
    print("  " + " ".join(f"{key}={value}" for key, value in env.items()))
    print("  uv run python -m evals.runner " + " ".join(argv))
    if not yes_spend:
        print(
            "Not run: this spends money. Re-run with YES_SPEND=1 after the owner's yes. "
            "Nothing was spent."
        )
        return 0
    # 4. Run the runner once, with the arm in its environment. Its own refusals still apply.
    before = artifacts.newest_or_none("report", root=root)
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-m", "evals.runner", *argv],
        env={**os.environ, **env},
        cwd=root,
        check=False,
    )
    if completed.returncode not in RAN:
        print(f"ORACLE GAP: the runner exited {completed.returncode}; no report written.")
        return completed.returncode
    # 5. Find the report this run wrote through the one resolver; refuse if there is none.
    after = artifacts.newest_or_none("report", root=root)
    if after is None or after == before:
        print("ORACLE GAP FAIL: the runner wrote no new report; nothing to assemble.")
        return 2
    archive = json.loads(after.read_text(encoding="utf-8"))["invocation_id"]
    # 6. Assemble and write this batch's report.
    for path in write(assemble([archive], plan, root=root), root=root):
        print(f"wrote {path.relative_to(root)}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals.oracle_gap", description=__doc__)
    parser.add_argument("action", choices=("batch", "report"))
    parser.add_argument("--plan", type=Path, default=PLAN_PATH)
    parser.add_argument("--worlds", default="", help="batch: a subset of the plan's scenarios")
    parser.add_argument("--yes-spend", action="store_true", help="batch: really run (paid)")
    parser.add_argument("--archives", default="", help="report: batch archive ids, comma-separated")
    parser.add_argument("--write", action="store_true", help="report: write it, not print it")
    args = parser.parse_args(argv)
    plan = load_plan(args.plan)
    if args.action == "batch":
        only = [name.strip() for name in args.worlds.split(",") if name.strip()]
        return run_batch(plan, only=only, yes_spend=args.yes_spend)
    archives = [archive.strip() for archive in args.archives.split(",") if archive.strip()]
    if not archives:
        print("ORACLE GAP REFUSED: report needs ARCHIVES=<id>[,<id>...], the batch archives.")
        return 2
    try:
        document = assemble(archives, plan)
    except (OSError, ValueError) as error:
        print(f"ORACLE GAP FAIL: {error}")
        return 2
    if args.write:
        for path in write(document):
            print(f"wrote {path.relative_to(REPO_ROOT)}")
        return 0
    print(render_markdown(document), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
