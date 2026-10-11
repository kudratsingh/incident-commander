"""The oracle-gap sample (WO-R3-347): run one batch of recorded selector runs, then report it.

A batch replays every world in a committed sample plan (``evals/samples/oracle_gap.json``, or the
pilot ``oracle_gap_pilot.json``) once per pass, each pinned to its recording, under the
``candidate_selector`` arm; ``REPEATS=N`` makes N passes in the one runner invocation (WO-R3-375).
The report gives pass@k, selected@k and oracle_gap@k (plan 03 § 7.3) per world, per run and
pooled, with n beside every number and the calibration report id beside every selector number
(O-55). Why this sample and what it cannot claim: the hub's
``docs/plans/research-buildout-v2.1/samples/oracle-gap.md``.
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
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

from evals import artifacts, recorded_applies, research_report
from evals.candidate_metrics import REPORTED_KS, WorldKey, measure, measure_selection, world_key
from evals.recorded_client import matching_recordings
from evals.runner import REPEATS_FLAG, STUDY_FLAG, RunReport, ScenarioOutcome
from evals.scenarios.loader import load_scenarios
from evals.scenarios.schema import Scenario
from evals.tracing import TraceKind
from incident_commander.agent.hypothesis import HypothesisCategory
from incident_commander.config import ModelRole

REPO_ROOT: Final[Path] = artifacts.REPO_ROOT
PLAN_PATH: Final[Path] = REPO_ROOT / "evals" / "samples" / "oracle_gap.json"
#: The pilot the owner asked for first (O-55): four worlds, three repeats each, never pooled.
PILOT_PLAN_PATH: Final[Path] = REPO_ROOT / "evals" / "samples" / "oracle_gap_pilot.json"

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
    # Whether this plan's runs belong to the main sample. False makes the plan its own study (the
    # O-55 pilot): its report scores only runs stamped with its own ``study`` and is read on its
    # own, and every other plan's report leaves those runs out, so they never reach the 52.
    pooled_with_main: bool = True

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
    plan: SamplePlan, worlds: Sequence[SampleWorld], *, repeats: int = 1
) -> tuple[list[str], dict[str, str]]:
    """The runner's arguments and the environment the arm needs, for one batch over ``worlds``.

    ``--study`` stamps every row with the plan's study; ``--repeats`` (only when more than one)
    makes the runner pass over the same pinned worlds that many times in this one invocation.
    """
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
    argv += [STUDY_FLAG, plan.study]
    if repeats > 1:
        argv += [REPEATS_FLAG, str(repeats)]
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
    # Which pass of a repeated invocation made the run (``RunProvenance.repeat``); 0 otherwise.
    repeat: int = 0


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
    repeat: int = 0,
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
        repeat=repeat,
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
    # A study's report scores its own runs. A run another study's plan asked for is that study's,
    # whatever it replayed: the pilot (``pooled_with_main: false``, O-55) is reported on its own
    # and never pooled into the main sample. A run no plan stamped counts for the main sample only.
    if reason := _study_mismatch(provenance.study, plan):
        return reason
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


def _study_mismatch(made_for: str | None, plan: SamplePlan) -> str:
    """Why a run made for ``made_for`` is not this plan's to score, or ``""`` when it is."""
    if made_for == plan.study or (made_for is None and plan.pooled_with_main):
        return ""
    if made_for is None:
        return (
            f"not made for {plan.study}: the run carries no study, and a plan with "
            "pooled_with_main: false is reported on its own and scores only its own runs"
        )
    return (
        f"made for study {made_for}, not {plan.study}: a study's runs are reported by its own "
        "plan and never pooled into another's sample"
    )


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
                repeat=provenance.repeat,
            )
        )
    stamped = [o.provenance for o in source.report.outcomes if o.provenance is not None]
    scope = {
        "archive": archive,
        "sha256": source.sha256,
        "generated_at": source.report.generated_at.isoformat(),
        "runs": len(source.report.outcomes),
        # Which pass of a repeated invocation this archive is: one pass, one archive.
        "repeat": stamped[0].repeat if stamped else None,
        "repeat_of": stamped[0].repeat_of if stamped else None,
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


def per_run(rows: Sequence[RunRow], k: int) -> list[dict[str, Any]]:
    """Each run at k, in the order its archives were given, with the pass that made it."""
    return [
        {
            "archive": row.archive,
            "repeat": row.repeat,
            "pass_hit": row.pass_hit.get(k),
            "selected_hit": row.selected_hit.get(k),
            "gap": row.gap.get(k),
            "decision": row.decision or "none",
        }
        for row in rows
    ]


def agreement(rows: Sequence[RunRow], k: int) -> dict[str, Any]:
    """Whether one world's runs (its repeats) gave the same verdict at k; ``None`` below two."""
    passed = [hit for row in rows if (hit := row.pass_hit.get(k)) is not None]
    selected = [hit for row in rows if (hit := row.selected_hit.get(k)) is not None]
    return {
        "k": k,
        "runs": len(rows),
        "pass_at_k_agree": None if len(passed) < 2 else len(set(passed)) == 1,
        "selected_at_k_agree": None if len(selected) < 2 else len(set(selected)) == 1,
    }


def summarise(rows: Sequence[RunRow], *, headline_k: int) -> dict[str, Any]:
    """Per world (with each run and whether its repeats agree) and pooled at every reported k."""
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
                "agreement": agreement(members, headline_k),
                "per_run": per_run(members, headline_k),
            }
            for (scenario, recording), members in sorted(per_world.items())
        ],
        "pooled": {
            "runs": len(rows),
            "worlds": len(per_world),
            "at_k": [
                at_k(rows, k) | {"gap_interval_95": gap_interval(rows, k)} for k in REPORTED_KS
            ],
            "gap_by_decision": {"k": headline_k, "counts": gap_by_decision(rows, headline_k)},
            "selector_vs_top_candidate": {
                "k": headline_k,
                "counts": selector_vs_top_candidate(rows, headline_k),
            },
        },
    }


#: The selector numbers in each kind of row: what the calibration gate withholds or vouches for.
_SELECTOR_FIELDS: Final[tuple[str, ...]] = (
    "selected_at_k",
    "oracle_gap",
    "selected_rate",
    "gap_rate",
    "gap_interval_95",
)
_RUN_SELECTOR_FIELDS: Final[tuple[str, ...]] = ("selected_hit", "gap", "decision")
_AGREEMENT_SELECTOR_FIELDS: Final[tuple[str, ...]] = ("selected_at_k_agree",)
_SPLIT_SELECTOR_FIELDS: Final[tuple[str, ...]] = ("counts",)


def beside_calibration(summary: dict[str, Any], calibration: Mapping[str, Any]) -> dict[str, Any]:
    """Every row that carries a selector number, with its calibration report in the same row.

    O-55: "the calibration report id beside every selector number". An open gate puts the id in
    the row. A closed one replaces each selector number with ``research_report.WITHHELD`` (plan
    02:243) and puts the gate's reason in the row, so a row read alone still says why. The
    generation half (pass@k) is never gated: it measures the generator, not the selector.
    """

    def stamped(row: Mapping[str, Any], fields: Sequence[str]) -> dict[str, Any]:
        if calibration["opens"]:
            return {**row, "calibration_report_id": calibration["report_id"]}
        return {
            **{
                key: research_report.WITHHELD if key in fields else value
                for key, value in row.items()
            },
            "calibration_report_id": None,
            "withheld_because": calibration["why_not"],
        }

    pooled = summary["pooled"]
    return {
        "per_world": [
            world
            | {
                "at_k": [stamped(entry, _SELECTOR_FIELDS) for entry in world["at_k"]],
                "agreement": stamped(world["agreement"], _AGREEMENT_SELECTOR_FIELDS),
                "per_run": [stamped(run, _RUN_SELECTOR_FIELDS) for run in world["per_run"]],
            }
            for world in summary["per_world"]
        ],
        "pooled": pooled
        | {
            "at_k": [stamped(entry, _SELECTOR_FIELDS) for entry in pooled["at_k"]],
            "gap_by_decision": stamped(pooled["gap_by_decision"], _SPLIT_SELECTOR_FIELDS),
            "selector_vs_top_candidate": stamped(
                pooled["selector_vs_top_candidate"], _SPLIT_SELECTOR_FIELDS
            ),
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
        "Repeats of one world are the same recorded world replayed again (only the model's own "
        "sampling differs): whether they agree is a description of a handful of runs, not a rate.",
        *(
            []
            if document["pooled_with_main"]
            else [
                f"This is the {document['study']} study (pooled_with_main: false): its runs are "
                "reported here on their own and are never pooled into the main sample's numbers."
            ]
        ),
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
        "pooled_with_main": plan.pooled_with_main,
        "work_order": plan.work_order,
        "design": plan.design,
        "arm": plan.arm,
        "agent_model": agent_model,
        "headline_k": plan.n,
        "planned": {"worlds": len(plan.worlds), "batches": plan.batches},
        "scope": {"archives": scope},
        "calibration": calibration,
        "selector_gate": research_report.SELECTOR_GATE_RULE,
        # Every row carrying a selector number carries its calibration report id beside it, or
        # the reason it is withheld (O-55, WO-R3-375).
        "summary": beside_calibration(summary, calibration),
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


def _verdict(hit: Any) -> str:
    """A run's verdict at k, in words."""
    return "—" if hit is None else ("hit" if hit is True else "miss" if hit is False else str(hit))


def _agree(value: Any) -> str:
    return "—" if value is None else ("yes" if value is True else "no" if value is False else "")


def _selector(label: str, value: str, row: Mapping[str, Any]) -> str:
    """One selector number of ``row``, its calibration report beside it (shared renderer)."""
    report_id = row.get("calibration_report_id")
    return research_report.selector_number(
        label,
        value,
        report_ids=(report_id,) if report_id else (),
        why_withheld=row.get("withheld_because", ""),
    )


def _counts(counts: Any) -> str:
    """A split's counts in one line: ``escalate 1, select 2``; ``none`` when there are none."""
    if not isinstance(counts, Mapping):
        return _cell(counts)
    return ", ".join(f"{key.replace('_', ' ')} {value}" for key, value in counts.items()) or "none"


def render_markdown(document: Mapping[str, Any]) -> str:
    """The same document for a person: pooled first, then each world, each run, what is left out.

    Every selector number is written by ``research_report.selector_number`` with its calibration
    report id beside it, or with the reason it is withheld in its place (O-55).
    """
    pooled = document["summary"]["pooled"]
    k = document["headline_k"]
    calibration = document["calibration"]
    archives = document["scope"]["archives"]
    passes = [entry.get("repeat") for entry in archives]
    lines = [
        f"# Oracle gap — {document['arm']} on {document['agent_model']}",
        "",
        f"{document['work_order']}. Design: {document['design']}.",
        f"Study: {document['study']}"
        + (
            ""
            if document["pooled_with_main"]
            else " — pooled_with_main: false. Its runs are reported on their own here and are "
            "never pooled into the main sample."
        ),
        f"Archives: {', '.join(entry['archive'] for entry in archives)}"
        + (
            f" (passes {', '.join(_cell(p) for p in passes)} of one repeated invocation)"
            if any(entry.get("repeat_of") for entry in archives)
            else ""
        )
        + ".",
        f"Calibration report for this arm: {calibration['report_id'] or 'NONE'}"
        + (
            " — its id is printed beside every selector number below."
            if calibration["opens"]
            else f" — every selector number is withheld: {calibration['why_not']}."
        ),
        "",
        f"## Pooled ({pooled['runs']} scored runs, {pooled['worlds']} worlds)",
        "",
        "| k | n | pass@k | selected@k | oracle gap | gap 95% interval (worlds resampled) |",
        "|---|---|---|---|---|---|",
    ]
    for entry in pooled["at_k"]:
        at = entry["k"]
        lines.append(
            f"| {at} | {entry['n']}"
            f" | {_cell(entry['pass_at_k'])} ({_cell(entry['pass_rate'])})"
            " | "
            + _selector(
                f"selected@{at}",
                f"{entry['selected_at_k']}/{entry['n']} ({_cell(entry['selected_rate'])})",
                entry,
            )
            + " | "
            + _selector(
                f"oracle gap@{at}",
                f"{entry['oracle_gap']}/{entry['n']} ({_cell(entry['gap_rate'])})",
                entry,
            )
            + " | "
            + _selector(f"gap interval@{at}", _cell(entry["gap_interval_95"]), entry)
            + " |"
        )
    split = pooled["gap_by_decision"]
    versus = pooled["selector_vs_top_candidate"]
    lines += [
        "",
        "Runs that lost an available answer, by what the selector did at the deciding step:",
        _selector(f"gap by decision@{split['k']}", _counts(split["counts"]), split),
        "",
        "The selector's pick against the generator's own top candidate:",
        _selector(f"selector vs top candidate@{versus['k']}", _counts(versus["counts"]), versus),
        "",
        f"## Per world (k={k})",
        "",
        "| scenario | recording | runs | n | pass@k | selected@k | oracle gap"
        " | repeats agree on pass@k | repeats agree on selected@k |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for world in document["summary"]["per_world"]:
        entry = next(item for item in world["at_k"] if item["k"] == k)
        agreement = world["agreement"]
        lines.append(
            f"| {world['scenario']} | {world['recording']} | {world['runs']} | {entry['n']}"
            f" | {_cell(entry['pass_at_k'])}/{entry['n']}"
            " | "
            + _selector(f"selected@{k}", f"{entry['selected_at_k']}/{entry['n']}", entry)
            + " | "
            + _selector(f"oracle gap@{k}", f"{entry['oracle_gap']}/{entry['n']}", entry)
            + f" | {_agree(agreement['pass_at_k_agree'])}"
            " | "
            + _selector(f"selected@{k} agree", _agree(agreement["selected_at_k_agree"]), agreement)
            + " |"
        )
    lines += [
        "",
        f"## Per run (k={k})",
        "",
        "| scenario | archive | repeat | pass@k | selected@k | oracle gap | selector decision |",
        "|---|---|---|---|---|---|---|",
    ]
    for world in document["summary"]["per_world"]:
        for run in world["per_run"]:
            lines.append(
                f"| {world['scenario']} | {run['archive']} | {run['repeat']}"
                f" | {_verdict(run['pass_hit'])}"
                " | "
                + _selector(f"selected@{k}", _verdict(run["selected_hit"]), run)
                + " | "
                + _selector(f"oracle gap@{k}", _cell(run["gap"]), run)
                + " | "
                + _selector(f"selector decision@{k}", _cell(run["decision"]), run)
                + " |"
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


def passes_written(before: Collection[Path], repeats: int, *, root: Path) -> tuple[list[str], str]:
    """The archive of every pass the runner just wrote, pass 0 first, or why they cannot be named.

    Read off the reports that are new since ``before``, through the one resolver. One pass is the
    newest new report. Several are the passes whose rows name one pass 0 (``repeat_of``), so a
    report another command wrote meanwhile is never taken for one of them.
    """
    new = [path for path in artifacts.versions("report", root=root) if path not in before]
    if not new:
        return [], "the runner wrote no new report; nothing to assemble"
    reports = [RunReport.model_validate_json(path.read_text(encoding="utf-8")) for path in new]
    if repeats == 1:
        return [reports[-1].invocation_id], ""
    passes: dict[str, dict[int, str]] = {}
    series_of: dict[str, str] = {}
    for report in reports:
        stamps = {
            (o.provenance.repeat_of, o.provenance.repeat)
            for o in report.outcomes
            if o.provenance is not None
        }
        if len(stamps) == 1:
            ((series, repeat),) = stamps
            if series is not None:
                passes.setdefault(series, {})[repeat] = report.invocation_id
                series_of[report.invocation_id] = series
    # The series of the newest new report that belongs to one: this invocation's passes.
    latest = next(
        (series_of[r.invocation_id] for r in reversed(reports) if r.invocation_id in series_of),
        None,
    )
    found = passes.get(latest, {}) if latest is not None else {}
    if sorted(found) != list(range(repeats)):
        return [], (
            f"found {len(found)} of {repeats} passes ({', '.join(found.values()) or 'none'}); "
            "each one written is still in evals/runs/ — report them with "
            "make oracle-gap-report ARCHIVES=<ids>"
        )
    return [found[repeat] for repeat in range(repeats)], ""


def make_command(
    plan_path: Path, *, only: Sequence[str] = (), repeats: int = 1, yes_spend: bool = True
) -> str:
    """The make line that asks for this batch: what the owner says yes to (O-45)."""
    words = ["make oracle-gap-batch"]
    if plan_path.resolve() != PLAN_PATH.resolve():
        shown = plan_path.resolve()
        words.append(
            f"PLAN={shown.relative_to(REPO_ROOT) if shown.is_relative_to(REPO_ROOT) else shown}"
        )
    if only:
        words.append(f"WORLDS={','.join(only)}")
    if repeats > 1:
        words.append(f"REPEATS={repeats}")
    if yes_spend:
        words.append("YES_SPEND=1")
    return " ".join(words)


def run_batch(
    plan: SamplePlan,
    *,
    only: Sequence[str] = (),
    yes_spend: bool,
    repeats: int = 1,
    root: Path = REPO_ROOT,
    plan_path: Path = PLAN_PATH,
) -> int:
    """Run one batch through the runner and write its report; without ``yes_spend``, only check.

    ``repeats`` passes over the same pinned worlds, in ONE runner invocation: one paid ask.
    """
    # 1. Which worlds: the whole plan, or the named subset when re-running after a harness failure.
    worlds, refusal = worlds_of(plan, only)
    if not refusal and repeats < 1:
        refusal = f"REPEATS={repeats}: a batch makes at least one pass"
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
    # 3. Print exactly what would run, with the repeat count and the total number of paid runs.
    #    Without --yes-spend that is all: the free dry run of a batch.
    argv, env = batch_invocation(plan, worlds, repeats=repeats)
    print(
        f"batch: {len(worlds)} world(s) x {repeats} repeat(s) = {len(worlds) * repeats} recorded "
        f"runs in ONE runner invocation, arm {plan.arm}, role {plan.model_role.value}, "
        f"study {plan.study}"
        + ("" if plan.pooled_with_main else " (pooled_with_main: false — reported on its own)")
    )
    print("  " + " ".join(f"{key}={value}" for key, value in env.items()))
    print("  uv run python -m evals.runner " + " ".join(argv))
    if not yes_spend:
        print(
            "Not run: this spends money. After the owner's yes for this batch: "
            f"{make_command(plan_path, only=only, repeats=repeats)}. Nothing was spent."
        )
        return 0
    # 4. Run the runner once, with the arm in its environment. Its own refusals still apply.
    before = set(artifacts.versions("report", root=root))
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-m", "evals.runner", *argv],
        env={**os.environ, **env},
        cwd=root,
        check=False,
    )
    if completed.returncode not in RAN:
        print(f"ORACLE GAP: the runner exited {completed.returncode}; no report written.")
        return completed.returncode
    # 5. Find the archive of every pass this run wrote, through the one resolver; refuse if any
    #    pass is missing rather than report a batch smaller than the one that was paid for.
    archives, missing = passes_written(before, repeats, root=root)
    if missing:
        print(f"ORACLE GAP FAIL: {missing}.")
        return 2
    # 6. Assemble and write this batch's report over every pass.
    for path in write(assemble(archives, plan, root=root), root=root):
        print(f"wrote {path.relative_to(root)}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals.oracle_gap", description=__doc__)
    parser.add_argument("action", choices=("batch", "report"))
    parser.add_argument("--plan", type=Path, default=PLAN_PATH)
    parser.add_argument("--worlds", default="", help="batch: a subset of the plan's scenarios")
    parser.add_argument(
        "--repeats", type=int, default=1, help="batch: passes over every world, one invocation"
    )
    parser.add_argument("--yes-spend", action="store_true", help="batch: really run (paid)")
    parser.add_argument("--archives", default="", help="report: batch archive ids, comma-separated")
    parser.add_argument("--write", action="store_true", help="report: write it, not print it")
    args = parser.parse_args(argv)
    plan = load_plan(args.plan)
    if args.action == "batch":
        only = [name.strip() for name in args.worlds.split(",") if name.strip()]
        return run_batch(
            plan,
            only=only,
            yes_spend=args.yes_spend,
            repeats=args.repeats,
            plan_path=args.plan,
        )
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
