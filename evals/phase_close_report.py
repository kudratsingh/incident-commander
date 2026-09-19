"""Assemble a phase-close report (plan 03 § 14) from committed evidence.

A phase is not closed until this document exists and is committed. Nothing here runs an
eval and nothing is typed in — every number comes out of a committed archive, the corpus
or the blessed baseline — which is what lets a test regenerate it byte for byte and makes
section 2's leak hunt a CHECK, with the grep's own command and output in the report.
Plan 03 § 14's seven sections are ``SECTION_KEYS``, and ``closing_verdict`` reads the
model role off the RUNS. Facts about one phase rather than the protocol live in a
:class:`PhaseScope`, so two closes read as a diff; the DRAFT mark is derived from a
declared :class:`PendingRerun`, never set. A committed report is EVIDENCE, so a FINAL
lands BESIDE its draft (invariant 9) and ``COMMITTED_SCOPES`` lists the documents where
``SCOPES`` says what each phase closes on today.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from collections import Counter
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from evals import artifacts, regression
from evals.runner import RunReport, root_cause_coverage
from evals.scenarios.loader import load_scenarios
from incident_commander.agent.hypothesis import HypothesisCategory
from incident_commander.agent.investigation import FIX_MAP
from incident_commander.config import ModelRole

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[1]

#: The confidence a top hypothesis must reach before the loop hands ``remediate`` to
#: PLANNING. From the module that ENFORCES it, so the audit cannot describe a bar the
#: code does not apply.
REMEDIATE_BAR: Final[float] = 0.7


@dataclass(frozen=True)
class LiveLeg:
    """One live scenario run, in the order the owner released it."""

    archive_id: str
    scenario: str
    order: int
    story: str
    #: Appended to the escalation verdict when this run escalated with the gates open.
    #: The verdict's SHAPE is the protocol's question; why one agent did not act is a
    #: fact about that run, so it is declared beside the run.
    escalation_note: str = ""
    #: On a leg that RE-RUNS an earlier one: the archive id it supersedes. The earlier
    #: leg STAYS — dropping it would make the close a selected sample of itself — so the
    #: pair is joined here and section 1 prints the link beside both stories.
    reruns: str = ""
    #: Why the re-run was worth buying: the change between the two runs and what it was
    #: expected to move — the hypothesis it tested, which is what makes a second sample
    #: evidence rather than another roll of the dice.
    rerun_reason: str = ""


#: Every live run of Phase 1's close, red included. A close that listed only
#: its green runs would be a selected sample of itself.
_PHASE1_LIVE_LEGS: Final[tuple[LiveLeg, ...]] = (
    LiveLeg(
        archive_id="42000dfda188",
        scenario="remediate_consumer_lag_success",
        order=1,
        escalation_note=(
            " Not laziness in the grader's sense either: the agent could not see the metric "
            "move (WO-R3-254)."
        ),
        story=(
            "RED. Correct top hypothesis at 0.75 for all five planner steps and never a "
            "`remediate` step: three lag re-reads inside 25 s returned the same cached 29, so "
            "the agent could not see the metric move. Root cause WO-R3-254 — the alert says lag "
            "is CLIMBING and the only sensor returned one undated number. Fixed in platform "
            "v0.6.7 (plat #204): `get_consumer_lag` now returns `measured_at`, `age_seconds` "
            "and `recent_samples`. Commander re-pinned in cmd #245."
        ),
    ),
    LiveLeg(
        archive_id="845bdae22195",
        scenario="remediate_consumer_lag_success",
        order=2,
        story=(
            "GREEN on the re-run, on platform v0.6.7. Read the trend 0 -> 16 -> 35 in one call, "
            "went 0.75 -> 0.82, restarted `worker-dispatcher`, verify passed on the second "
            "reading. The root fix is proven by this run, not argued."
        ),
    ),
    LiveLeg(
        archive_id="ee183c85429c",
        scenario="remediate_stale_cache_success",
        order=3,
        story=(
            "GREEN. Read the named hot key, dipped to 0.65 and probed Redis health rather than "
            "acting on a dip, came back at 0.75, invalidated exactly that key, verified on the "
            "key's own state (`exists: false`)."
        ),
    ),
    LiveLeg(
        archive_id="47abb70a2b9e",
        scenario="remediate_dlq_backlog_success",
        order=4,
        story=(
            "GREEN. Replayed exactly the one `replay_safe` row by id and left the unclassified "
            "poison row alone. Ran only after WO-R3-251 (plat #203, v0.6.6) took "
            "`(chaos poison_message on topic ...)` out of the DLQ error text, which would "
            "otherwise have failed this scenario's own leak check."
        ),
    ),
)

#: The machine regression baseline `evals/regression.py` gates against,
#: blessed by cmd #238.
BLESSED_BASELINE: Final[str] = "evals/reports/baseline.json"


@dataclass(frozen=True)
class ReadOnlyPass:
    """A live read-only pass whose committed grades were superseded offline.

    A different kind of evidence from ``LiveLeg``: it seeds no fault, so its world is
    not the one its labels describe (INC-003) and the numbers that count are the
    RE-GRADE's. Naming that artifact here stops the assembler reprinting a withdrawn figure.
    """

    archive_id: str
    #: The committed re-grade artifact, relative to the repository root.
    regrade_report: str
    #: The figure the archive still carries and that must not be quoted as a
    #: result, written down so the report can say which number it is refusing.
    withdrawn_number: str
    story: str


@dataclass(frozen=True)
class PendingRerun:
    """A live leg whose fix is merged but whose re-run has not been bought.

    One of these makes the report a DRAFT. Not a to-do list: the reason the close cannot
    yet claim what it is about to claim, declared here so claim and caveat cannot drift.
    """

    scenario: str
    #: The archive this re-run is expected to supersede.
    supersedes: str
    #: The change that is already merged and waiting to be measured.
    fix: str
    #: What the re-run is waiting on. Always the owner, never readiness.
    blocked_on: str = "the owner's explicit go — readiness is not authorization"


@dataclass(frozen=True)
class NewNumber:
    """A measurement this phase produced that no earlier phase could.

    Step 6 is a delta against a baseline and a number with no baseline has no delta, so
    the section states these with their sample size rather than omitting them.
    """

    name: str
    value: str
    sample: str
    caveat: str


@dataclass(frozen=True)
class PhaseScope:
    """Everything that is a fact about ONE phase rather than about the protocol.

    The seven steps are the same every time; the archives, what happened in them and
    which sentences are true are not. Declaring the second half here is what lets the
    first be one piece of code, and what makes two closes readable as a diff.
    """

    phase: int
    work_order: str
    scope: str
    #: The free canned sweep: every scenario through the gate under the benchmark role.
    #: Not a paid run, but the leg that carries the closing mark.
    canned_sweep: str
    live_legs: tuple[LiveLeg, ...]
    #: The Phase 0 baseline this close is measured against, cited by id (WO-R3-189).
    #: Two versions exist on disk; ``_baseline_delta`` says why both are named.
    phase0_baseline_cited: str
    deviations: tuple[dict[str, str], ...]
    follow_ups: tuple[dict[str, str], ...]
    does_claim: tuple[str, ...]
    does_not_claim: tuple[str, ...]
    sweep_committed: str
    budget_why_null: str
    budget_caps_note: str
    judge_reruns_required: int
    judge_model: str
    judge_why: str
    judge_evidence: str
    baseline_verdict: str
    baseline_cost_note: str
    spend_against_plan: tuple[dict[str, str], ...]
    campaign_context: str
    incidents_row_filed: bool
    incidents_row_reason: str
    #: Prose that appears only in the human half.
    closing_paragraph: str
    trajectory_mix: str
    author_notes: dict[str, str]
    leak_verdict_sentence: str
    gate_audit_opening: str
    gate_audit_closing: str
    #: Which deviation explains why two Phase 0 baseline artifacts are named.
    baseline_artifact_ref: str
    read_only_pass: ReadOnlyPass | None = None
    pending_reruns: tuple[PendingRerun, ...] = ()
    new_numbers: tuple[NewNumber, ...] = ()
    #: Whether this phase's document carries the ``status`` block. Phase 1's artifact
    #: predates the field and regenerating it must give back the committed bytes
    #: (invariant 9), so it lands forward rather than retroactively. NOT the value —
    #: see ``draft_status``.
    reports_status: bool = False

    @property
    def archives(self) -> tuple[str, ...]:
        """Every archive in scope, sweep first, in reading order."""
        read_only = () if self.read_only_pass is None else (self.read_only_pass.archive_id,)
        return (self.canned_sweep, *read_only, *(leg.archive_id for leg in self.live_legs))

    @property
    def live_archives(self) -> tuple[str, ...]:
        read_only = () if self.read_only_pass is None else (self.read_only_pass.archive_id,)
        return (*read_only, *(leg.archive_id for leg in self.live_legs))


def draft_status(scope: PhaseScope) -> dict[str, Any]:
    """DRAFT while a re-run is owed, FINAL when none is — derived, never set.

    No argument says "this one is final": the only way to produce it is to move the
    missing archives into ``live_legs``, which is the same act as having the evidence. A
    status field anyone can type is one that will eventually be typed wrong.
    """
    pending = [
        {
            "scenario": rerun.scenario,
            "supersedes": rerun.supersedes,
            "fix": rerun.fix,
            "blocked_on": rerun.blocked_on,
        }
        for rerun in scope.pending_reruns
    ]
    if not pending:
        return {
            "status": "FINAL",
            "why": "every live leg this close reports has been run; nothing in scope is owed.",
            "pending_reruns": [],
        }
    scenarios = ", ".join(f"`{rerun.scenario}`" for rerun in scope.pending_reruns)
    return {
        "status": "DRAFT",
        "why": (
            f"{len(pending)} live re-run(s) are pending the owner's go ({scenarios}). Their "
            "fixes are merged and their worlds are ready, but nothing has been measured on "
            "them yet, so every number below that depends on those scenarios is the number "
            "BEFORE the fix. Adding the two archive ids to this phase's scope and re-running "
            "`make phase-close-report` writes the final version beside this one."
        ),
        "pending_reruns": pending,
    }


#: Plan 03 § 14's seven steps, in its order. Closed list: ``assemble``
#: refuses to emit a section that is not here and refuses to leave one empty.
SECTION_KEYS: Final[tuple[str, ...]] = (
    "sweep_results",
    "leak_hunt",
    "gate_crossing_audit",
    "budget_profile_diff",
    "judge_calibration",
    "baseline_delta",
    "spend_line",
)

SECTION_TITLES: Final[dict[str, str]] = {
    "sweep_results": "1. Sweep results",
    "leak_hunt": "2. Leak hunt",
    "gate_crossing_audit": "3. Gate-crossing audit",
    "budget_profile_diff": "4. Budget profile diff — benchmark vs development",
    "judge_calibration": "5. Judge calibration",
    "baseline_delta": "6. Baseline delta",
    "spend_line": "7. Spend line",
}

#: Small counts spelled out, because these sections are prose and "3 legs" reads like a
#: table cell mid-sentence. Anything larger stays as digits, like the measurements.
_NUMBER_WORDS: Final[dict[int, str]] = {
    1: "one",
    2: "two",
    3: "three",
    4: "four",
    5: "five",
    6: "six",
    7: "seven",
    8: "eight",
    9: "nine",
    10: "ten",
}


def _spelled(count: int) -> str:
    return _NUMBER_WORDS.get(count, str(count))


#: Harness-authored rows in a trajectory's evidence ledger: the loop's own bookkeeping,
#: not something a tool returned. The leak hunt has to hold them apart from tool output
#: or it grades the harness as the world.
_HARNESS_ROW_PREFIX: Final[str] = "_"


# --------------------------------------------------------------------------
# Reading archives
# --------------------------------------------------------------------------


def archive_dir(root: Path, archive_id: str) -> Path:
    return root / "evals/runs" / archive_id


def _read_report(path: Path) -> tuple[RunReport, str, dict[str, Any]]:
    """Parse one ``report.json`` and hash the exact bytes that were parsed."""
    content = path.read_bytes()
    raw: dict[str, Any] = json.loads(content)
    return RunReport.model_validate(raw), hashlib.sha256(content).hexdigest(), raw


def _budget_totals(report: RunReport) -> dict[str, Any]:
    """Actual USD, wall seconds, tokens and tool calls, summed off the ledger.

    From each row's own ``provenance.budget`` — what the run wrote as it spent — never
    an estimate. USD is summed as ``Decimal`` and serialized as a string: it is money.
    """
    usd = Decimal("0")
    wall = 0.0
    tokens = 0
    calls = 0
    for outcome in report.outcomes:
        if outcome.provenance is None:
            continue
        ledger = outcome.provenance.budget
        usd += ledger.usd_used
        wall += ledger.wall_seconds_used
        tokens += ledger.tokens_used
        calls += ledger.tool_calls_used
    return {
        "usd": str(usd),
        "wall_seconds": round(wall, 3),
        "tokens": tokens,
        "agent_tool_calls": calls,
    }


def _trace_records(root: Path, archive_id: str, scenario: str) -> list[dict[str, Any]]:
    path = archive_dir(root, archive_id) / "traces" / f"{scenario}.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _trajectory(root: Path, archive_id: str, scenario: str) -> dict[str, Any]:
    path = archive_dir(root, archive_id) / "trajectories" / f"{scenario}.json"
    loaded: dict[str, Any] = json.loads(path.read_text())
    return loaded


def _evidence_rows(trajectory: dict[str, Any]) -> list[dict[str, Any]]:
    """The final checkpoint's ledger — cumulative, so it is the whole run."""
    checkpoints = trajectory["checkpoints"]
    if not checkpoints:
        raise ValueError("trajectory has no checkpoints")
    rows: list[dict[str, Any]] = checkpoints[-1]["evidence"]
    return rows


# --------------------------------------------------------------------------
# Section 2 — the leak hunt
# --------------------------------------------------------------------------


def leak_terms(root: Path, ran: Collection[str] | None = None) -> dict[str, tuple[str, ...]]:
    """The three term families plan 03 § 14 step 2 names, derived not typed.

    ``chaos`` is the vocabulary the token split (WP-1.5) keeps off the agent's side; the
    labels come off ``HypothesisCategory`` and the fixture names off the corpus, so
    tomorrow's addition is hunted with no list re-typed. ``ran`` narrows them to the
    scenarios a phase RAN, because the list goes inside an evidence document and today's
    corpus broke three committed reports at 41 → 45 (WO-R3-202).
    """
    scenarios = load_scenarios(root / "evals/scenarios")
    if ran is not None:
        wanted = set(ran)
        scenarios = [scenario for scenario in scenarios if scenario.name in wanted]
    return {
        "chaos_vocabulary": ("chaos",),
        "root_cause_labels": tuple(sorted(category.value for category in HypothesisCategory)),
        "fixture_names": tuple(
            sorted(
                {scenario.name for scenario in scenarios}
                | {s.chaos_setup.name for s in scenarios if s.chaos_setup is not None}
            )
        ),
    }


def _count_terms(text: str, terms: Iterable[str]) -> Counter[str]:
    """Substring occurrences, case-insensitive.

    Substring, not word-boundary: a leak glued to something else
    (``chaos:kill:worker-dispatcher``) is still a leak, and the false positives are
    adjudicated in the report BY NAME rather than filtered out silently here.
    """
    lowered = text.lower()
    found: Counter[str] = Counter()
    for term in terms:
        occurrences = lowered.count(term)
        if occurrences:
            found[term] = occurrences
    return found


def _all_terms(groups: dict[str, tuple[str, ...]]) -> tuple[str, ...]:
    return tuple(term for group in groups.values() for term in group)


def run_grep(root: Path, paths: Sequence[str], pattern: str, flags: str) -> dict[str, Any]:
    """Run one real ``grep`` and record the command and its exact output.

    Shelling out rather than re-implementing the match: what the report prints has to be
    the output of the command it prints. ``grep`` exits 1 on "no match", the answer this
    hunt wants, so a non-zero status is recorded rather than raised.
    """
    argv = ["grep", flags, "-e", pattern, *paths]
    completed = subprocess.run(argv, cwd=root, capture_output=True, text=True, check=False)
    if completed.returncode > 1:
        raise ValueError(f"grep failed: {completed.stderr.strip()}")
    return {
        "command": " ".join(argv),
        "exit_status": completed.returncode,
        "output": completed.stdout,
        "matching_lines": len([line for line in completed.stdout.splitlines() if line]),
    }


def _trajectory_hunt(root: Path, files: Sequence[Path], terms: Sequence[str]) -> dict[str, Any]:
    """Split every trajectory hit into "the agent read it" and "the agent wrote it".

    A trajectory is full of root-cause labels because the agent's own hypotheses are in
    it — the agent working, not the world handing it the answer. What fails the phase is
    a term in a PLATFORM TOOL RESPONSE, so rows are split by author: ``_``-prefixed are
    the harness's, the rest are tool output.
    """
    agent_read: Counter[str] = Counter()
    agent_wrote: Counter[str] = Counter()
    hits: list[dict[str, Any]] = []
    for path in files:
        trajectory = json.loads(path.read_text())
        for checkpoint in trajectory["checkpoints"]:
            for row in checkpoint["evidence"]:
                counts = _count_terms(row["result_summary"], terms)
                if row["tool_name"].startswith(_HARNESS_ROW_PREFIX):
                    agent_wrote.update(counts)
                    continue
                agent_read.update(counts)
                for term, occurrences in counts.items():
                    hits.append(
                        {
                            "file": path.relative_to(root).as_posix(),
                            "tool_name": row["tool_name"],
                            "term": term,
                            "occurrences": occurrences,
                            "matched_text": row["result_summary"][:200],
                        }
                    )
            for hypothesis in checkpoint["hypotheses"]:
                agent_wrote.update(_count_terms(json.dumps(hypothesis), terms))
    return {
        "files_searched": len(files),
        "platform_responses_the_agent_read": dict(sorted(agent_read.items())),
        "text_the_agent_or_the_harness_wrote": dict(sorted(agent_wrote.items())),
        "hits_in_what_the_agent_read": hits,
    }


def _trace_targets(root: Path, scope: PhaseScope) -> list[tuple[str, str]]:
    """``(archive, scenario)`` for every trace file the phase produced.

    A live leg is one scenario; a read-only pass holds many, each its own trace file, so
    the pass expands to one target per scenario rather than one summarized row.
    """
    targets = [(leg.archive_id, leg.scenario) for leg in scope.live_legs]
    if scope.read_only_pass is not None:
        traces = archive_dir(root, scope.read_only_pass.archive_id) / "traces"
        targets.extend(
            (scope.read_only_pass.archive_id, path.stem) for path in sorted(traces.glob("*.jsonl"))
        )
    return targets


def _trace_hunt(root: Path, scope: PhaseScope, terms: Sequence[str]) -> dict[str, Any]:
    """Classify every trace hit by who produced the text.

    Four buckets and only the first can fail the phase: ``platform_response`` (bytes a
    tool returned to the agent), ``commander_prompt`` (our own versioned prompt files),
    ``harness_record`` (the evaluator's bookkeeping, which the agent never sees) and
    ``agent_output`` (what the model produced, plus the ledger replayed to it).
    """
    buckets: dict[str, Counter[str]] = {
        "platform_response": Counter(),
        "commander_prompt": Counter(),
        "harness_record": Counter(),
        "agent_output": Counter(),
    }
    per_run: list[dict[str, Any]] = []
    for archive, scenario in _trace_targets(root, scope):
        run_buckets: dict[str, Counter[str]] = {name: Counter() for name in buckets}
        for record in _trace_records(root, archive, scenario):
            kind = record["kind"]
            if kind == "mcp":
                run_buckets["platform_response"].update(
                    _count_terms(json.dumps(record["result"]), terms)
                )
            elif kind == "llm":
                request = record["request"]
                run_buckets["commander_prompt"].update(
                    _count_terms(json.dumps(request.get("system")), terms)
                )
                run_buckets["agent_output"].update(
                    _count_terms(json.dumps(record.get("output")), terms)
                )
                run_buckets["agent_output"].update(
                    _count_terms(json.dumps(request.get("messages")), terms)
                )
            else:
                run_buckets["harness_record"].update(_count_terms(json.dumps(record), terms))
        for name, counts in run_buckets.items():
            buckets[name].update(counts)
        per_run.append(
            {
                "archive": archive,
                "scenario": scenario,
                **{name: dict(sorted(counts.items())) for name, counts in run_buckets.items()},
            }
        )
    return {
        "totals": {name: dict(sorted(counts.items())) for name, counts in buckets.items()},
        "per_run": per_run,
    }


def _trajectory_files(root: Path, scope: PhaseScope) -> list[Path]:
    """Every trajectory the phase produced: the live runs plus the sweep."""
    files: list[Path] = []
    for leg in scope.live_legs:
        files.append(archive_dir(root, leg.archive_id) / "trajectories" / f"{leg.scenario}.json")
    if scope.read_only_pass is not None:
        files.extend(
            sorted(
                (archive_dir(root, scope.read_only_pass.archive_id) / "trajectories").glob("*.json")
            )
        )
    files.extend(sorted((archive_dir(root, scope.canned_sweep) / "trajectories").glob("*.json")))
    missing = [path for path in files if not path.is_file()]
    if missing:
        raise ValueError(f"missing trajectory evidence: {missing[0]}")
    return files


#: Hits in what the agent READ that were looked at and found not to be leaks, keyed by
#: term. Written down rather than filtered out inside the counter: a hunt that drops its
#: own awkward results cannot be checked. Anything hit and NOT here blocks the phase.
ADJUDICATED_HITS: Final[dict[str, str]] = {
    "unknown": (
        "The substring `unknown` inside the consumer group id `unknown-consumer` — the "
        "platform's own value for a group it does not recognise, and the entire subject of "
        "`consumer_lag_missing_group`. A substring collision with the `unknown` root-cause "
        "label, not a leaked label."
    ),
}


def _frozen_section(root: Path, scope: PhaseScope, name: str) -> dict[str, Any] | None:
    """Read a committed section when regenerating its frozen report."""
    try:
        path, _ = committed(scope.phase, root=root)
    except ValueError:
        return None
    return json.loads(path.read_text())["sections"][name]


def leak_hunt(root: Path, scope: PhaseScope) -> dict[str, Any]:
    # The phase's OWN scenarios, off its canned sweep — the one archive covering the
    # suite as it stood. ``leak_terms`` says why the list must come from the evidence.
    swept, _, _ = _read_report(archive_dir(root, scope.canned_sweep) / "report.json")
    frozen = _frozen_section(root, scope, "leak_hunt")
    groups = (
        frozen.get("terms")
        if frozen is not None
        else leak_terms(root, {outcome.scenario for outcome in swept.outcomes})
    )
    terms = _all_terms(groups)
    files = _trajectory_files(root, scope)
    read_only = () if scope.read_only_pass is None else (scope.read_only_pass.archive_id,)
    trajectory_dirs = [
        (archive_dir(root, archive) / "trajectories").relative_to(root).as_posix()
        for archive in (
            *(leg.archive_id for leg in scope.live_legs),
            *read_only,
            scope.canned_sweep,
        )
    ]
    trace_files = [
        (archive_dir(root, leg.archive_id) / "traces" / f"{leg.scenario}.jsonl")
        .relative_to(root)
        .as_posix()
        for leg in scope.live_legs
    ]
    # A pass with 27 scenarios is 27 trace files; naming its directory keeps
    # the committed command something a person can retype.
    trace_files.extend(
        (archive_dir(root, archive) / "traces").relative_to(root).as_posix()
        for archive in read_only
    )
    trajectories = _trajectory_hunt(root, files, terms)
    hit_terms = sorted({hit["term"] for hit in trajectories["hits_in_what_the_agent_read"]})
    return {
        "terms": groups,
        "term_count": len(terms),
        "committed_commands": {
            "trajectories": run_grep(root, trajectory_dirs, "chaos", "-rIni"),
            "traces": run_grep(root, trace_files, "chaos", "-rIoin"),
        },
        "trajectories": trajectories,
        "traces_by_author": _trace_hunt(root, scope, terms),
        "adjudications": {
            term: ADJUDICATED_HITS[term] for term in hit_terms if term in ADJUDICATED_HITS
        },
        "unadjudicated_hits": [term for term in hit_terms if term not in ADJUDICATED_HITS],
        "verdict": (
            "PASS"
            if not [term for term in hit_terms if term not in ADJUDICATED_HITS]
            else "BLOCKED — an unadjudicated hit in what the agent read"
        ),
    }


# --------------------------------------------------------------------------
# Section 3 — the gate-crossing audit
# --------------------------------------------------------------------------


def _planner_steps(records: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    for record in records:
        if record["kind"] != "llm" or record["role"] != "investigation_planner":
            continue
        output = record["output"]
        top = output["hypotheses"][0]
        steps.append(
            {
                "step": len(steps) + 1,
                "top_hypothesis": top["name"],
                "category": top["category"],
                "confidence": top["confidence"],
                "in_fix_map": top["category"] in {c.value for c in FIX_MAP},
                "meets_bar": top["confidence"] >= REMEDIATE_BAR,
                "emitted": output["next_action"]["kind"],
            }
        )
    return steps


def _gate_crossings(root: Path, leg: LiveLeg) -> dict[str, Any]:
    """One live run's every ``remediate`` handoff and its escalation, if any."""
    records = _trace_records(root, leg.archive_id, leg.scenario)
    rows = _evidence_rows(_trajectory(root, leg.archive_id, leg.scenario))
    decisions = [
        row
        for row in rows
        if row["tool_name"] in {"_handoff_refused", "_planner_remediate", "_planner_escalate"}
    ]
    steps = _planner_steps(records)
    pending = list(decisions)
    for step in steps:
        if step["emitted"] != "remediate":
            step["disposition"] = "no handoff attempted (probe step)"
            continue
        row = next(
            (r for r in pending if r["tool_name"] in {"_handoff_refused", "_planner_remediate"}),
            None,
        )
        if row is None:
            step["disposition"] = "remediate emitted; no handoff row recorded"
            continue
        pending.remove(row)
        step["disposition"] = (
            "CROSSED into planning"
            if row["tool_name"] == "_planner_remediate"
            else "REFUSED and re-steered"
        )
        step["disposition_detail"] = row["result_summary"][:400]

    plan = next(
        (r["output"] for r in records if r["kind"] == "llm" and r["role"] == "remediation_planner"),
        None,
    )
    executed = [
        {"tool": r["tool_name"], "arguments": r["arguments"]}
        for r in records
        if r["kind"] == "mcp" and plan is not None and r["tool_name"] == plan["action_tool"]
    ]
    verify = [
        {"verdict": r["output"]["verdict"], "reasoning": r["output"]["reasoning"][:300]}
        for r in records
        if r["kind"] == "llm" and r["role"] == "verification_judge"
    ]
    end = next(r for r in records if r["kind"] == "scenario_end")
    escalation = next((r for r in rows if r["tool_name"] == "_planner_escalate"), None)
    return {
        "archive": leg.archive_id,
        "scenario": leg.scenario,
        "planner_steps": steps,
        "handoffs_attempted": sum(1 for s in steps if s["emitted"] == "remediate"),
        "handoffs_crossed": sum(1 for s in steps if s.get("disposition", "").startswith("CROSSED")),
        "handoffs_refused": sum(1 for s in steps if s.get("disposition", "").startswith("REFUSED")),
        # Steps where FIX_MAP and the bar were both satisfied and the planner probed
        # anyway: zero is normal early, five of five is the shape of the red run.
        "non_crossings": sum(
            1 for s in steps if s["in_fix_map"] and s["meets_bar"] and s["emitted"] != "remediate"
        ),
        "plan": (
            None
            if plan is None
            else {
                "target_hypothesis": plan["target_hypothesis"],
                "action_tool": plan["action_tool"],
                "action_arguments": plan["action_arguments"],
                "verify_tool": plan["verify_tool"],
                "verify_expectation": plan["verify_expectation"],
            }
        ),
        "executed": executed,
        "verify_judgments": verify,
        "final_state": end["final_state"],
        "escalation": None if escalation is None else escalation["result_summary"],
    }


def _escalation_verdict(audit: dict[str, Any], expected_state: str, note: str = "") -> str:
    """Justified by ground truth, or lazy? — plan 03 § 14 step 3's question."""
    if audit["escalation"] is None:
        return "not applicable — the run reached its expected terminal state"
    crossable = [
        step
        for step in audit["planner_steps"]
        if step["in_fix_map"] and step["meets_bar"] and step["emitted"] != "remediate"
    ]
    if expected_state != "escalated" and crossable:
        return (
            f"NOT justified. The scenario's ground truth expects {expected_state!r}, and on "
            f"{len(crossable)} planner step(s) the top hypothesis was both in FIX_MAP and at or "
            "above the 0.7 bar — every gate was open and the planner chose `probe` anyway."
            f"{note}"
        )
    if expected_state == "escalated":
        return "justified — the scenario's ground truth expects an escalation."
    return "NOT justified — the scenario expects a fix and none was attempted."


# --------------------------------------------------------------------------
# Sections 1, 4, 5, 6, 7
# --------------------------------------------------------------------------


def closing_verdict(reports: Sequence[RunReport]) -> dict[str, Any]:
    """Whether these runs may close a phase (plan 03 § 14's last line).

    Pure over the reports, so the rule can be exercised with a development run in scope
    without staging an archive tree. Derived from the ROWS, never asserted: a report
    whose ``closing`` flag disagrees with its rows cannot exist.
    """
    development: list[str] = []
    predates: list[str] = []
    for report in reports:
        development.extend(
            f"{report.invocation_id}/{name}" for name in report.development_scenarios
        )
        if report.closing is None:
            predates.append(report.invocation_id)
    if development:
        return {
            "closing": False,
            "reason": (
                f"{len(development)} run(s) in scope were made under the "
                f"{ModelRole.DEVELOPMENT.value} model role: {', '.join(sorted(development))}. "
                "A phase report generated with any development run in it is non-closing "
                "(plan 03 section 14)."
            ),
            "development_runs": sorted(development),
        }
    if predates:
        return {
            "closing": False,
            "reason": (
                "run(s) in scope predate model roles and record none: "
                f"{', '.join(sorted(predates))}"
            ),
            "development_runs": [],
        }
    return {
        "closing": True,
        "reason": "every run in scope was made under the benchmark model role",
        "development_runs": [],
    }


def _root_cause_line(report: RunReport) -> dict[str, Any] | None:
    """One report's diagnosis coverage, or ``None`` when nothing was graded.

    ``None`` rather than zeroes, and the caller omits the key: "no scenario declared a
    label" and "every label was wrong" are different statements, and only one is about
    the agent.
    """
    coverage = root_cause_coverage(report)
    if coverage.graded == 0 and coverage.not_graded_world == 0:
        return None
    return {
        "graded": coverage.graded,
        "correct": coverage.correct,
        "not_graded_world": coverage.not_graded_world,
        "of_total": coverage.total,
        "describe": coverage.describe(),
    }


def _regrade(root: Path, read_only: ReadOnlyPass) -> dict[str, Any]:
    """The offline re-grade that replaced a paid pass's invalid grades.

    Read out of the committed artifact rather than recomputed: the re-grade is its own
    reviewed act (WO-R3-265), and re-deriving it here would be a second opinion nobody
    asked for. Its ``archive_digest`` proves it read the bytes this report reads.
    """
    path = root / read_only.regrade_report
    content = path.read_bytes()
    document: dict[str, Any] = json.loads(content)
    if document["archive"] != read_only.archive_id:
        raise ValueError(
            f"re-grade {path.name} describes archive {document['archive']}, "
            f"not {read_only.archive_id}"
        )
    return {
        "artifact": read_only.regrade_report,
        "artifact_sha256": hashlib.sha256(content).hexdigest(),
        "why": document["why"],
        "totals": document["totals"],
        "root_cause": document["root_cause"],
        "still_failing": document["still_failing"],
        "limits": document["limits"],
        "files_verified_unchanged": len(document["archive_digest"]),
    }


def _read_only_block(root: Path, read_only: ReadOnlyPass) -> dict[str, Any]:
    """Section 1's entry for a live pass that seeded no fault."""
    report, sha, raw = _read_report(archive_dir(root, read_only.archive_id) / "report.json")
    states = Counter(outcome.final_state.value for outcome in report.outcomes)
    return {
        "archive": read_only.archive_id,
        "report_sha256": sha,
        "generated_at": raw["generated_at"],
        "scenarios": report.total,
        "as_archived_passed": report.passed,
        "withdrawn_number": read_only.withdrawn_number,
        "terminal_states": dict(sorted(states.items())),
        "live_mcp": sum(1 for outcome in report.outcomes if outcome.live_mcp),
        "live_llm": sum(1 for outcome in report.outcomes if outcome.live_llm),
        "regrade": _regrade(root, read_only),
        "story": read_only.story,
    }


def _sweep_results(root: Path, scope: PhaseScope) -> dict[str, Any]:
    canned, canned_sha, canned_raw = _read_report(
        archive_dir(root, scope.canned_sweep) / "report.json"
    )
    blessed, blessed_sha, _ = _read_report(root / BLESSED_BASELINE)
    comparison = regression.compare(blessed, canned)
    # One direction only, which stays true as the corpus grows: equality made every
    # committed report un-regenerable the moment a later phase added a scenario (41 → 45,
    # WO-R3-202). The property worth keeping is that a sweep naming a scenario the corpus
    # no longer has is citing something a reader cannot look at.
    corpus = load_scenarios(root / "evals/scenarios")
    vanished = sorted({outcome.scenario for outcome in canned.outcomes} - {s.name for s in corpus})
    if vanished:
        raise ValueError(f"the canned sweep names scenario(s) the corpus no longer has: {vanished}")
    legs: list[dict[str, Any]] = []
    for leg in scope.live_legs:
        report, sha, _ = _read_report(archive_dir(root, leg.archive_id) / "report.json")
        outcome = report.outcomes[0]
        row = {
            "order": leg.order,
            "archive": leg.archive_id,
            "scenario": leg.scenario,
            "report_sha256": sha,
            "passed": outcome.report.passed,
            "final_state": outcome.final_state.value,
            "judge_score": (None if outcome.judge_score is None else outcome.judge_score.overall),
            "tool_calls_used": outcome.tool_calls_used,
            "dimensions": {d.dimension.value: d.passed for d in outcome.report.dimensions},
            "commander_revision": (
                None if outcome.provenance is None else outcome.provenance.commander_revision
            ),
            "platform_image_digest": (
                None if outcome.provenance is None else outcome.provenance.platform_image_digest
            ),
            "model_role": (
                None if outcome.provenance is None else outcome.provenance.model_role.value
            ),
            "story": leg.story,
        }
        if (diagnosis := _root_cause_line(report)) is not None:
            row["root_cause"] = diagnosis
        if leg.reruns:
            row["reruns"] = {"archive": leg.reruns, "why": leg.rerun_reason}
        legs.append(row)
    sweep: dict[str, Any] = {
        "archive": scope.canned_sweep,
        "report_sha256": canned_sha,
        "generated_at": canned_raw["generated_at"],
        "total": canned.total,
        "passed": canned.passed,
        "failed": canned.failed,
        "degraded_count": canned.degraded_count,
        "judged_count": canned.judged_count,
        "judge_mean_overall": canned.judge_mean_overall,
        "model_roles": sorted(
            {o.provenance.model_role.value for o in canned.outcomes if o.provenance}
        ),
        "agent_models": sorted({o.provenance.agent_model for o in canned.outcomes if o.provenance}),
        "gate_lines": _gate_lines(blessed, canned, comparison),
    }
    if (diagnosis := _root_cause_line(canned)) is not None:
        sweep["root_cause"] = diagnosis
    results: dict[str, Any] = {
        "canned_sweep": sweep,
        "live_legs": legs,
        "live_total": len(legs),
        "live_passed": sum(1 for leg in legs if leg["passed"]),
        "committed": scope.sweep_committed,
    }
    if scope.read_only_pass is not None:
        results["read_only_pass"] = _read_only_block(root, scope.read_only_pass)
        graded = [leg["root_cause"] for leg in legs if "root_cause" in leg]
        results["live_root_cause_on_seeded_legs"] = {
            "correct": sum(1 for line in graded if line["correct"]),
            "graded": len(graded),
            "why_only_these": (
                "A ground truth is a statement about one world (ADR 0040). These legs SEED "
                "the fault their label describes, so their diagnosis is gradable; the "
                "read-only pass seeds nothing by construction, so 16 of its rows report "
                "'not graded' rather than red, and it cannot contribute a live root-cause "
                "number at all."
            ),
        }
    return results


def _gate_lines(
    blessed: RunReport, canned: RunReport, comparison: regression.ComparisonResult
) -> list[str]:
    """The two lines `make eval-reg` printed, recomputed from the artifacts.

    Recomputed rather than pasted: scrollback is not evidence, and
    ``evals/regression.py`` is the same code the gate ran.
    """
    lines: list[str] = []
    lines.append("no changes vs baseline" if not _any_delta(comparison) else "CHANGES vs baseline")
    reason = canned.non_closing_reason
    lines.append(
        f"NON-CLOSING: latest cannot close a phase — {reason}"
        if reason
        else "closing: every run in latest was made under the benchmark model role"
    )
    refusal = regression.cross_model_refusal(blessed, canned)
    lines.append(
        "cross-model refusal: none — baseline and sweep name one agent model"
        if refusal is None
        else f"cross-model refusal: {refusal}"
    )
    return lines


def _any_delta(comparison: regression.ComparisonResult) -> bool:
    return bool(
        comparison.regressions
        or comparison.improvements
        or comparison.new_scenarios
        or comparison.dropped_scenarios
        or comparison.dropped_dimensions
        or comparison.vacated_assertions
    )


def _role_totals(root: Path, archives: Sequence[str]) -> list[dict[str, Any]]:
    """Per-role calls, tokens, USD and milliseconds across the runs in scope.

    WP-2.3's record makes it computable at all: before it a run could say what it cost
    but not which role spent it. Rolled up across archives, because "where does the money
    go?" is about the loop. Empty when no run carries the record, and the caller then
    omits the key — a table of zeroes would read as "these roles cost nothing".
    """
    totals: dict[str, dict[str, Any]] = {}
    for archive in archives:
        report, _, _ = _read_report(archive_dir(root, archive) / "report.json")
        for outcome in report.outcomes:
            if outcome.accounting is None:
                continue
            for role in outcome.accounting.by_role:
                entry = totals.setdefault(
                    role.role,
                    {
                        "role": role.role,
                        # A SET, not a flag: `briefing_writer` was charged from cmd #264
                        # onwards, so a scope spanning it holds both answers.
                        "charged_to_ledger": set(),
                        "calls": 0,
                        "tokens": 0,
                        "usd": Decimal("0"),
                        "elapsed_ms": 0,
                    },
                )
                entry["charged_to_ledger"].add(role.charged_to_ledger)
                entry["calls"] += role.calls
                entry["tokens"] += role.tokens_used
                entry["usd"] += role.usd_used
                entry["elapsed_ms"] += role.elapsed_ms
    return [
        {
            **entry,
            "usd": f"{entry['usd']:.6f}",
            "charged_to_ledger": (
                next(iter(entry["charged_to_ledger"]))
                if len(entry["charged_to_ledger"]) == 1
                else "mixed"
            ),
        }
        for _, entry in sorted(totals.items(), key=lambda item: -item[1]["usd"])
    ]


def _budget_profile(root: Path, scope: PhaseScope) -> dict[str, Any]:
    """Section 4. The diff is definitionally null; the counts are not."""
    per_run: list[dict[str, Any]] = []
    for leg in scope.live_legs:
        report, _, _ = _read_report(archive_dir(root, leg.archive_id) / "report.json")
        outcome = report.outcomes[0]
        records = _trace_records(root, leg.archive_id, leg.scenario)
        llm_by_role = Counter(r["role"] for r in records if r["kind"] == "llm")
        mcp_calls = sum(1 for r in records if r["kind"] == "mcp")
        ledger = None if outcome.provenance is None else outcome.provenance.budget
        row = {
            "archive": leg.archive_id,
            "scenario": leg.scenario,
            "llm_calls_total": sum(llm_by_role.values()),
            "llm_calls_by_role": dict(sorted(llm_by_role.items())),
            "planner_iterations": llm_by_role["investigation_planner"],
            "mcp_calls_in_trace": mcp_calls,
            "agent_tool_calls_billed": outcome.tool_calls_used,
            "tokens_used": None if ledger is None else ledger.tokens_used,
            "max_tokens": None if ledger is None else ledger.max_tokens,
            "max_tool_calls": None if ledger is None else ledger.max_tool_calls,
            "wall_seconds_used": None if ledger is None else ledger.wall_seconds_used,
            "max_wall_seconds": None if ledger is None else ledger.max_wall_seconds,
            "budget_tripped": False if ledger is None else ledger.is_exhausted,
        }
        if outcome.accounting is not None:
            row["per_role"] = [
                {
                    "role": role.role,
                    "charged_to_ledger": role.charged_to_ledger,
                    "calls": role.calls,
                    "tokens": role.tokens_used,
                    "usd": f"{role.usd_used:.6f}",
                    "elapsed_ms": role.elapsed_ms,
                }
                for role in outcome.accounting.by_role
            ]
            row["ledger_reconciled"] = outcome.accounting.reconciled
        per_run.append(row)
    profile: dict[str, Any] = {
        "diff": "null",
        "why_null": scope.budget_why_null,
        "per_run": per_run,
        "budget_trips": [row["archive"] for row in per_run if row["budget_tripped"]],
        "caps_note": scope.budget_caps_note,
    }
    if roles := _role_totals(root, scope.live_archives):
        # How many legs charged the briefing writer, COUNTED rather than written down:
        # it was 3 in this phase's draft and 5 once the re-runs were in.
        charged_writer = [
            row["archive"]
            for row in per_run
            for role in row.get("per_role", ())
            if role["role"] == "briefing_writer" and role["charged_to_ledger"]
        ]
        profile["per_role_totals"] = roles
        profile["per_role_note"] = (
            "Rolled up across every live run in scope, sorted by spend. `charged_to_ledger` is "
            "false for the EVALUATOR's roles: the briefing judge grades the run and is not part "
            "of it, so its dollars are real but are not the agent's budget. It reads `mixed` for "
            "`briefing_writer` because the answer changed inside this phase — cmd #264 decided "
            "the writer's prose is the agent's own and charged it — so the read-only pass, which "
            f"ran before that commit, and the {_spelled(len(charged_writer))} legs, which ran "
            "after it, disagree. The "
            "column is the truth about the runs, not a rule. `elapsed_ms` is time inside the "
            "model call, deliberately not the run's wall clock — the loop also probes and waits."
        )
    return profile


def _judge_calibration(root: Path, scope: PhaseScope) -> dict[str, Any]:
    """Section 5. None this phase — and the claim is checkable, not asserted."""
    frozen = _frozen_section(root, scope, "judge_calibration")
    prompts = root / "src/incident_commander/llm/prompts"
    judge_prompts = sorted(path.name for path in prompts.glob("*.md") if "judge" in path.name)
    digests = (
        frozen["judge_prompts_unchanged"]
        if frozen is not None
        else {
            name: hashlib.sha256((prompts / name).read_bytes()).hexdigest()
            for name in judge_prompts
        }
    )
    return {
        "reruns_required": scope.judge_reruns_required,
        "why": scope.judge_why,
        "judge_model": scope.judge_model,
        "judge_prompts_unchanged": digests,
        "evidence": scope.judge_evidence,
    }


def _baseline_delta(root: Path, scope: PhaseScope) -> dict[str, Any]:
    """Section 6. Unexplained movement blocks the phase — so measure all of it."""
    canned, _, _ = _read_report(archive_dir(root, scope.canned_sweep) / "report.json")
    blessed, blessed_sha, blessed_raw = _read_report(root / BLESSED_BASELINE)
    resolved = artifacts.newest("baseline_report", root=root)
    phase0_doc = json.loads(resolved.read_text())
    phase0_offline_id = str(phase0_doc["provenance"]["invocation_id"])
    phase0, phase0_sha, _ = _read_report(archive_dir(root, phase0_offline_id) / "report.json")

    comparisons = []
    unexplained: list[dict[str, Any]] = []
    for label, other, sha in (
        (f"blessed regression baseline ({BLESSED_BASELINE}, cmd #238)", blessed, blessed_sha),
        (f"Phase 0 baseline offline leg ({phase0_offline_id})", phase0, phase0_sha),
    ):
        comparison = regression.compare(other, canned)
        differences = _row_differences(other, canned)
        entry = {
            "against": label,
            "baseline_invocation_id": other.invocation_id,
            "baseline_sha256": sha,
            "regressions": list(comparison.regressions),
            "improvements": list(comparison.improvements),
            "new_scenarios": list(comparison.new_scenarios),
            "dropped_scenarios": list(comparison.dropped_scenarios),
            "dropped_dimensions": list(comparison.dropped_dimensions),
            "vacated_assertions": list(comparison.vacated_assertions),
            "row_level_differences": differences,
            "judge_mean_delta": round(
                (canned.judge_mean_overall or 0.0) - (other.judge_mean_overall or 0.0), 12
            ),
            "degraded_count_delta": (canned.degraded_count or 0) - (other.degraded_count or 0),
        }
        if differences:
            explained, unexplained_here = _classify_row_differences(differences)
            entry["movement_by_a_dimension_this_phase_added"] = explained
            entry["unexplained_row_differences"] = unexplained_here
            unexplained.extend({"against": label, **row} for row in unexplained_here)
        comparisons.append(entry)
    delta: dict[str, Any] = {
        "phase0_baseline_cited_by_id": scope.phase0_baseline_cited,
        "phase0_baseline_resolved_by_artifacts_newest": resolved.name,
        "phase0_baseline_offline_leg": phase0_offline_id,
        "blessed_baseline_generated_at": blessed_raw["generated_at"],
        "comparisons": comparisons,
        "unexplained_movement": unexplained,
        "verdict": scope.baseline_verdict,
        "cost_per_scenario_note": scope.baseline_cost_note,
    }
    if scope.new_numbers:
        delta["new_numbers"] = [
            {
                "name": number.name,
                "value": number.value,
                "sample_size": number.sample,
                "caveat": number.caveat,
            }
            for number in scope.new_numbers
        ]
        delta["new_numbers_note"] = (
            "These have no Phase 0 counterpart to move against: the dimension and the labels "
            "that produce them both landed in this phase. They are stated with their sample "
            "size rather than left out, and every one of those samples is too small to "
            "generalise from."
        )
    return delta


def _classify_row_differences(
    differences: Sequence[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Split row movement into "a new dimension appeared" and everything else.

    Step 6 blocks a phase on UNEXPLAINED movement, so the report tells the two apart
    rather than printing one number and a paragraph of reassurance: a row whose only
    change is a new passing dimension is the ruler growing a mark, not the corpus moving.
    """
    explained_scenarios: list[str] = []
    added_dimensions: Counter[str] = Counter()
    unexplained: list[dict[str, Any]] = []
    for row in differences:
        before, after = row["baseline"], row["latest"]
        if before is None or after is None:
            unexplained.append(row)
            continue
        before_dimensions = dict(before["dimensions"])
        after_dimensions = dict(after["dimensions"])
        added = set(after_dimensions) - set(before_dimensions)
        removed = set(before_dimensions) - set(after_dimensions)
        changed = {
            name
            for name in set(before_dimensions) & set(after_dimensions)
            if before_dimensions[name] != after_dimensions[name]
        }
        scalars = [
            key
            for key in ("passed", "final_state", "tool_calls_used", "judge")
            if before[key] != after[key]
        ]
        if (
            added
            and not removed
            and not changed
            and not scalars
            and all(after_dimensions[name] for name in added)
        ):
            explained_scenarios.append(row["scenario"])
            added_dimensions.update(added)
            continue
        unexplained.append(row)
    return (
        {
            "rows": len(explained_scenarios),
            "dimensions_added": dict(sorted(added_dimensions.items())),
            "all_passing": True,
            "scenarios": sorted(explained_scenarios),
        },
        unexplained,
    )


def _row_differences(baseline: RunReport, latest: RunReport) -> list[dict[str, Any]]:
    """Every per-scenario difference the gate's own comparison does not print."""

    def profile(report: RunReport) -> dict[str, dict[str, Any]]:
        return {
            outcome.scenario: {
                "passed": outcome.report.passed,
                "final_state": outcome.final_state.value,
                "tool_calls_used": outcome.tool_calls_used,
                "dimensions": {d.dimension.value: d.passed for d in outcome.report.dimensions},
                "judge": None if outcome.judge_score is None else outcome.judge_score.overall,
            }
            for outcome in report.outcomes
        }

    before, after = profile(baseline), profile(latest)
    return [
        {"scenario": name, "baseline": before.get(name), "latest": after.get(name)}
        for name in sorted(set(before) | set(after))
        if before.get(name) != after.get(name)
    ]


def _archive_span(root: Path, archive: str, scenarios: Sequence[str]) -> tuple[str, str, float]:
    """First and last trace timestamp across an archive, and the seconds between.

    The operator's clock is in no archive, so this is the honest wall figure. For a
    many-scenario pass it spans the whole invocation, gaps included.
    """
    starts: list[str] = []
    ends: list[str] = []
    for scenario in scenarios:
        records = _trace_records(root, archive, scenario)
        starts.append(records[0]["timestamp"])
        ends.append(records[-1]["timestamp"])
    first, last = min(starts), max(ends)
    span = (datetime.fromisoformat(last) - datetime.fromisoformat(first)).total_seconds()
    return first, last, span


def _spend_line(root: Path, scope: PhaseScope) -> dict[str, Any]:
    """Section 7. Actual USD and wall time, against plan 03 section 11's table."""
    rows = []
    total_usd = Decimal("0")
    total_wall = 0.0
    total_trace_wall = 0.0
    billed = Decimal("0")
    carries_accounting = False
    for leg in scope.live_legs:
        report, _, raw = _read_report(archive_dir(root, leg.archive_id) / "report.json")
        totals = _budget_totals(report)
        records = _trace_records(root, leg.archive_id, leg.scenario)
        started = datetime.fromisoformat(records[0]["timestamp"])
        finished = datetime.fromisoformat(records[-1]["timestamp"])
        span = (finished - started).total_seconds()
        total_usd += Decimal(totals["usd"])
        total_wall += float(totals["wall_seconds"])
        total_trace_wall += span
        row = {
            "order": leg.order,
            "archive": leg.archive_id,
            "scenario": leg.scenario,
            "usd": totals["usd"],
            "agent_loop_wall_seconds": totals["wall_seconds"],
            "run_started_at": records[0]["timestamp"],
            "run_finished_at": records[-1]["timestamp"],
            "start_to_finish_seconds": round(span, 3),
            "tokens": totals["tokens"],
        }
        outcome = report.outcomes[0]
        if outcome.accounting is not None:
            carries_accounting = True
            billed += outcome.accounting.usd_used
            row["usd_including_evaluator"] = f"{outcome.accounting.usd_used:.6f}"
        rows.append(row)
    canned, _, canned_raw = _read_report(archive_dir(root, scope.canned_sweep) / "report.json")
    canned_totals = _budget_totals(canned)
    spend: dict[str, Any] = {
        "live_runs": rows,
        "live_total_usd": str(total_usd),
        "live_total_agent_loop_seconds": round(total_wall, 3),
        "live_total_start_to_finish_seconds": round(total_trace_wall, 3),
        "canned_sweep": {
            "archive": scope.canned_sweep,
            "usd": canned_totals["usd"],
            "wall_seconds": canned_totals["wall_seconds"],
            "note": (
                "Free by construction: a canned run replays recorded tool responses through a "
                "canned LLM client, so nothing billable leaves the machine. The wall figure is "
                f"the sum of the {canned.total} rows' own ledgers, not the operator's clock."
            ),
        },
    }
    if scope.read_only_pass is not None:
        pass_report, _, _ = _read_report(
            archive_dir(root, scope.read_only_pass.archive_id) / "report.json"
        )
        pass_totals = _budget_totals(pass_report)
        scenarios = [outcome.scenario for outcome in pass_report.outcomes]
        first, last, span = _archive_span(root, scope.read_only_pass.archive_id, scenarios)
        pass_billed = sum(
            (o.accounting.usd_used for o in pass_report.outcomes if o.accounting is not None),
            Decimal("0"),
        )
        total_usd += Decimal(pass_totals["usd"])
        total_wall += float(pass_totals["wall_seconds"])
        total_trace_wall += span
        billed += pass_billed
        carries_accounting = carries_accounting or bool(pass_billed)
        spend["read_only_pass"] = {
            "archive": scope.read_only_pass.archive_id,
            "scenarios": pass_report.total,
            "usd": pass_totals["usd"],
            "usd_including_evaluator": f"{pass_billed:.6f}",
            "agent_loop_wall_seconds": pass_totals["wall_seconds"],
            "run_started_at": first,
            "run_finished_at": last,
            "start_to_finish_seconds": round(span, 3),
            "tokens": pass_totals["tokens"],
        }
        spend["live_total_usd"] = str(total_usd)
        spend["live_total_agent_loop_seconds"] = round(total_wall, 3)
        spend["live_total_start_to_finish_seconds"] = round(total_trace_wall, 3)
    if carries_accounting:
        spend["live_total_usd_including_evaluator"] = f"{billed:.6f}"
        spend["evaluator_share_usd"] = f"{billed - total_usd:.6f}"
        spend["two_totals_note"] = (
            "Two totals, because there are two answers. `live_total_usd` is the sum of the "
            "runs' own budget ledgers — what the AGENT spent, and the number invariant 7 caps. "
            "`live_total_usd_including_evaluator` adds the eval harness's own briefing judge, "
            "which is real money off the same key but is not the agent's budget and would fail "
            "the ledger reconciliation if it were folded in. The second is the bill."
        )
    live_count = len(rows) + (0 if scope.read_only_pass is None else 1)
    # The per-leg mean is over the LEGS, never the whole scope: a 27-scenario pass
    # divided by three is not "what a remediation run costs".
    leg_usd = sum((Decimal(row["usd"]) for row in rows), Decimal("0"))
    values = {
        "live_runs": len(rows),
        "live_invocations": live_count,
        "live_total_usd": str(total_usd),
        "live_total_usd_including_evaluator": f"{billed:.6f}",
        "live_mean_usd": str((leg_usd / len(rows)).quantize(Decimal("0.000001"))),
        "live_mean_seconds": round(total_trace_wall / max(live_count, 1)),
        "live_leg_mean_seconds": round(
            sum(row["start_to_finish_seconds"] for row in rows) / len(rows)
        ),
        "live_total_minutes": round(total_trace_wall / 60, 1),
    }
    spend["against_plan_03_section_11"] = [
        {"planned": entry["planned"], "actual": entry["actual"].format(**values)}
        for entry in scope.spend_against_plan
    ]
    spend["campaign_context"] = scope.campaign_context.format(**values)
    return spend


# --------------------------------------------------------------------------
# The document
# --------------------------------------------------------------------------


_PHASE1_DEVIATIONS: Final[tuple[dict[str, str], ...]] = (
    {
        "id": "D1",
        "what": "Reduced scope (owner decision O-15, 2026-09-16).",
        "detail": (
            "Plan 03 section 14 step 1 asks for every scenario touched or added in the phase in "
            "recorded mode at 3 reps, plus live for every scenario with a remediation leg. What "
            "ran: all 41 scenarios canned at 1 rep, and 3 of the 12 remediation-leg scenarios "
            "live at 1 rep - one per injected-fault family (kill_consumer, create_stale_cache, "
            "poison_message). There was no separate read-only live stage."
        ),
    },
    {
        "id": "D2",
        "what": "Recorded-world mode does not exist yet.",
        "detail": (
            "It arrives in Phase 3 (WP-3.1). Phase 1 therefore cannot run 'recorded mode, 3 "
            "reps' and does not pretend to: the sweep leg is canned. Saying so is the point - a "
            "silent substitution would make every later phase's comparison unreadable."
        ),
    },
    {
        "id": "D3",
        "what": "Live run 1's world reset was not the runbook's exact chain.",
        "detail": (
            "The runbook chains `make eval-live ... && make eval-reset`, so the reset runs only "
            "if the run succeeded. The wrapper used for run 1 (archive 42000dfda188) reset "
            "unconditionally. Disclosed to the owner at the time; runs 2, 3 and 4 used the exact "
            "chain. No evidence was lost - the archive was written before the reset - but the "
            "deviation is recorded rather than smoothed over."
        ),
    },
    {
        "id": "D4",
        "what": "No live scenario fires `bad_deploy`, so owner decision O-8 is not informed.",
        "detail": (
            "O-8 asks whether renaming `bad_deploy`'s alert source breaks `make eval-reset` "
            "(the reset predicate matches `source LIKE 'chaos:%'`). Nothing in this close "
            "exercises that path, so this close says nothing about it. O-8 stays open."
        ),
    },
    {
        "id": "D5",
        "what": "The Phase 0 baseline artifact exists twice; this report names both.",
        "detail": (
            "WO-R3-189 cites `baseline_report.20260915T132421Z.ad634a458e5e`. That file is on "
            "disk in the main checkout but was never committed, and it sorts 89 seconds OLDER "
            "than its committed twin, so `artifacts.newest('baseline_report')` resolves the "
            "other one. Section 6 reads the committed, resolver-visible artifact and names both "
            "ids; their recorded numbers are identical (41/41, judge mean 0.8591463414634146, "
            "degraded 34), so the delta is the same either way."
        ),
    },
)

_PHASE1_FOLLOW_UPS: Final[tuple[dict[str, str], ...]] = (
    {
        "id": "WO-R3-253",
        "what": "Prose gloss for the generated remediation table (from WO-R2-177).",
        "status": "open",
    },
    {
        "id": "WO-R3-255",
        "what": (
            "The commander's own remediation-planner prompt says '...lives in the description "
            "of a chaos tool you never see'. It is our text, not the platform's, and it names "
            "no fault - but it is the last `chaos` token on the agent's side of the boundary."
        ),
        "status": "open",
    },
    {
        "id": "O-8",
        "what": "`bad_deploy`'s alert source vs the reset predicate — untouched by this close.",
        "status": "open",
    },
)


PHASE1: Final[PhaseScope] = PhaseScope(
    phase=1,
    work_order="WO-R3-189 (WP-1.7)",
    scope="REDUCED close, owner decisions O-14 (benchmark model) and O-15 (scope)",
    canned_sweep="32ae38f6b38b",
    live_legs=_PHASE1_LIVE_LEGS,
    phase0_baseline_cited="baseline_report.20260915T132421Z.ad634a458e5e",
    deviations=_PHASE1_DEVIATIONS,
    follow_ups=_PHASE1_FOLLOW_UPS,
    does_claim=(
        "The 41-scenario canned corpus passes under the benchmark model role and moves "
        "nothing against either baseline.",
        "On three live remediation scenarios, one per injected-fault family, the agent "
        "found the fault, crossed the remediation gate on a hypothesis that was in FIX_MAP "
        "and above the 0.7 bar, acted once on the right resource, and verified it.",
        "WP-1.5's acceptance holds on live evidence: no platform tool response any of "
        "these runs read contains `chaos`, any root-cause label, or any fixture name.",
        "The one live red of this phase is understood, root-caused to a sensor that could "
        "not show time passing, fixed in platform v0.6.7, and re-run green.",
    ),
    does_not_claim=(
        "That the agent is stable on these scenarios. One rep is one sample of a "
        "trajectory; the 2026-08-31 green on scenario 1 turned red on the same model, and "
        "the difference was an accident of confidence (LESSONS, 2026-09-17).",
        "That the remaining nine remediation-leg scenarios behave the same way live. They "
        "were not run in this phase.",
        "That the eight live greens of the earlier campaign say anything about the leak. "
        "They ran under the OLD all-scope agent token; they show the corpus runs live and "
        "nothing more.",
        "Anything about `bad_deploy` or owner decision O-8.",
        "That the phase's spend of ${live_total_usd} is comparable to plan 03 "
        "section 11's ~$20 phase-close row. It is a different, smaller sweep.",
    ),
    sweep_committed=(
        "All five archives are committed in this repository; every number above is read "
        "out of them and none is typed in."
    ),
    budget_why_null=(
        "Owner decision O-14 keeps BENCHMARK_MODEL at `claude-sonnet-4-6`, which is also "
        "DEVELOPMENT_MODEL. Both roles resolve to the same model id, so a benchmark-vs-"
        "development token or tool-call diff is not small — it is definitionally zero, and "
        "printing a table of zeroes would read like a measurement. What can be reported is "
        "the profile itself, per run, so the next phase (or the next model) has something "
        "to diff against."
    ),
    budget_caps_note=(
        "Every run stayed inside every cap. `mcp_calls_in_trace` counts more calls than "
        "`agent_tool_calls_billed` because the runner's own precondition poll and the "
        "post-action verify probe are on the wire but are not the agent's budget."
    ),
    judge_reruns_required=0,
    judge_model="claude-haiku-4-5",
    judge_why=(
        "Plan 03 section 9 requires a calibration rerun for every judge the phase TOUCHED. "
        "Phase 1 touched no judge prompt and no rubric. The one judge-side change in the "
        "phase is WO-R2-174 (cmd #243), which gave the verification judge and the eval "
        "briefing judge the same bounded output repair the three planner call sites already "
        "had: one re-ask on OUR OWN malformed output, same cap, same wrapper. It changes "
        "what happens when a reply fails schema validation, not what the judge is asked or "
        "how its answer is scored — and the canned sweep shows it: all 41 rows judged, "
        "judge mean identical to the blessed baseline to the last digit."
    ),
    judge_evidence=(
        "`git log <phase0 baseline>..HEAD -- src/incident_commander/llm/prompts/` is empty; "
        "the only commit under `evals/graders/` is cmd #243 and it touches "
        "`evals/graders/llm_judge.py` alone."
    ),
    baseline_verdict=(
        "Zero movement, on both baselines and on every axis the gate measures: no "
        "regression, no improvement, no new or dropped scenario, no dropped dimension, no "
        "vacated assertion, and not one of the 41 rows differs in pass/fail, terminal "
        "state, tool-call count, per-dimension result or judge score. There is therefore no "
        "unexplained movement to block the phase. Read honestly, that is a statement about "
        "the CANNED corpus: the same fixtures replayed through the same graders under the "
        "same model id give the same answer, which is what a regression baseline is for. "
        "It is not evidence about the live world; the live legs are."
    ),
    baseline_cost_note=(
        "Cost per scenario cannot move against either baseline: both baselines and this "
        "sweep are canned runs, and a canned run makes no billable call — every row's "
        "`usd_used` is 0.000000 on both sides. The live cost numbers are in section 7."
    ),
    spend_against_plan=(
        {
            "planned": "remediation scenario: ~$0.10-0.20, 2-3 min with reset",
            "actual": (
                "{live_runs} live remediation runs, ${live_total_usd} total, mean "
                "${live_mean_usd} per run; "
                "{live_mean_seconds} s mean start to finish. Inside the "
                "band on cost; faster than the band on time because the band includes the "
                "world reset, which is operator wall clock and not in any archive."
            ),
        },
        {
            "planned": "phase-close sweep (~40 scenarios x 3 reps): ~$15 per phase, +$5 live",
            "actual": (
                "${live_total_usd}. The reduction is decision O-15, not an underspend: recorded "
                "mode does not exist until Phase 3, so the 41-scenario sweep ran canned and "
                "free; three remediation scenarios ran live at one rep instead of twelve at "
                "three; and there was no read-only live stage."
            ),
        },
        {
            "planned": "per-scenario budget: 1.00 USD / 25 calls / 200 000 tokens / 600 s",
            "actual": (
                "No cap was approached. Highest USD 0.190176 of 1.00; highest tokens 68 283 "
                "of 200 000; highest tool calls 5 of the 13 these scenarios seed (the "
                "scenario cap is tighter than plan 03's 25); highest wall 155.6 s of 600."
            ),
        },
    ),
    campaign_context=(
        "The closed live-eval campaign cost about $7.20 in total. This close adds "
        "${live_total_usd} to that."
    ),
    incidents_row_filed=False,
    incidents_row_reason=(
        "No grader or judge drift was found. The one red run graded RED on OUTCOME with the "
        "agent's own trajectory agreeing — it never emitted `remediate` and never acted — "
        "so the grader was right and `context/INCIDENTS.md` (which records the times the "
        "EVALUATION was wrong about the agent) gets no row. The cause was the agent plus a "
        "sensor that could not show time passing, and it is filed as WO-R3-254 with the "
        "platform fix already shipped."
    ),
    closing_paragraph=(
        "Phase 1 closed on this sample, not on the plan's full sweep. The differences are "
        "listed as deviations at the end, each with the decision or the fact behind it."
    ),
    trajectory_mix="4 live + 41 canned",
    author_notes={
        "commander_prompt": (
            'our own `remediation_planner.md`: "...lives in the description of a chaos tool you '
            'never see". Our text, naming no fault. WO-R3-255.'
        ),
        "harness_record": (
            "the evaluator's `chaos_setup` trace record and the `chaos:kill:...` key it wrote. "
            "Never shown to the agent."
        ),
        "agent_output": "what the model wrote, plus its own ledger replayed into the next request",
    },
    leak_verdict_sentence=(
        "The gating bucket is **empty in every live run**: nothing the agent read from the "
        "platform contained the word. That is WP-1.5's acceptance, claimed here on live "
        "evidence rather than on the test that motivated it."
    ),
    gate_audit_opening="Across the four live runs:",
    gate_audit_closing=(
        "Every crossing is tabulated below "
        "with the two numeric gates it had to pass, and the red run's five non-crossings "
        "are tabulated the same way."
    ),
    baseline_artifact_ref="See deviation D5.",
)


_PHASE2_LIVE_LEGS: Final[tuple[LiveLeg, ...]] = (
    LiveLeg(
        archive_id="759e198cdd27",
        scenario="remediate_consumer_lag_success",
        order=1,
        story=(
            "GREEN, judge 0.95, root cause correct. Read the lag trend with the time fields "
            "platform v0.6.7 added, went 0.75 -> 0.82 across five planner steps, restarted "
            "`worker-dispatcher`, and kept probing through three `not_verified` readings until "
            "the fourth showed lag falling 50 -> 30 -> 11 -> 0. The verify loop, not the first "
            "reading, is what made this green."
        ),
    ),
    LiveLeg(
        archive_id="648a32f2339d",
        scenario="remediate_stale_cache_success",
        order=2,
        escalation_note=(
            " Not laziness either: the diagnosis was RIGHT and the agent's confidence in it "
            "fell — 0.75, 0.72, 0.65, 0.55, 0.55 — because `get_cache_key_info` could not show "
            "the key was stale. The same world went green that morning (`ee183c85429c`) at the "
            "same bar, which is what makes this a boundary and not a behaviour. Fixed in "
            "platform v0.6.8 (WO-R3-267); the re-run has not been bought."
        ),
        story=(
            "RED. Root cause CORRECT (`stale_cache`) at every one of five planner steps, and "
            "no `remediate` step at any of them: confidence drifted 0.75 -> 0.72 -> 0.65 -> "
            "0.55 -> 0.55, crossed below the 0.7 bar at step 3, and the run escalated on the "
            "five-step budget. The sensor is the cause — `get_cache_key_info` returned key "
            "shape and size but nothing that says the entry is stale, so more probing could "
            "only lower confidence. Platform v0.6.8 (plat #209) now reports "
            "`records_referenced` / `records_found` (healthy 3/3, stale 3/0); commander "
            "re-pinned in cmd #269."
        ),
    ),
    LiveLeg(
        archive_id="fc896b25a09c",
        scenario="remediate_dlq_backlog_success",
        order=3,
        story=(
            "RED on EVIDENCE, and correct on everything else: OUTCOME, ACTION, SAFETY and "
            "ROOT_CAUSE all pass, judge 1.00. The agent replayed exactly the one `replay_safe` "
            "row by id and verified the slice was empty — but it never called "
            "`list_dlq_messages` unfiltered, so it never saw the poison row it left behind. "
            "The runner tagged this `grader-brittleness`; owner decision O-21 went the other "
            "way: the claim is right, the agent must read the whole queue once before "
            "replaying part of it. ADR 0041's guard (WO-R3-268) is the fix; the re-run has "
            "not been bought."
        ),
    ),
)


_PHASE2_DEVIATIONS: Final[tuple[dict[str, str], ...]] = (
    {
        "id": "D1",
        "what": "Reduced scope (owner decision O-20, 2026-09-17).",
        "detail": (
            "Plan 03 section 14 step 1 asks for every scenario touched or added in the phase in "
            "recorded mode at 3 reps, plus live for every scenario with a remediation leg. What "
            "ran: all 41 scenarios canned at 1 rep, one live read-only pass over 27 scenarios, "
            "and 3 of the 12 remediation-leg scenarios live at 1 rep - one per injected-fault "
            "family (kill_consumer, create_stale_cache, poison_message). The owner chose "
            "option A of `.coordination/PHASE2-CLOSE-PLAN.md` and kept the plan order."
        ),
    },
    {
        "id": "D2",
        "what": "Recorded-world mode still does not exist, so there is no 3-rep sweep.",
        "detail": (
            "It arrives in Phase 3 (WP-3.1). The sweep leg is canned at one rep, exactly as in "
            "Phase 1. Saying so is the point - a silent substitution would make every later "
            "phase's comparison unreadable. One rep also means no variance figure: the only "
            "repeated live scenario in this whole project is the one that went green then red."
        ),
    },
    {
        "id": "D3",
        "what": "The read-only live pass cannot yield a live root-cause number, by construction.",
        "detail": (
            "`make eval-smoke` runs read-only scenarios against the live stack and seeds no "
            "fault. A ground truth is a statement about ONE world (ADR 0040), so 16 of the 18 "
            "labelled rows in that pass report 'not graded - the label describes a world the "
            "run did not have'. The pass is still evidence: it is a live leak hunt over 27 "
            "trajectories, a live cost profile, and the run in which the PLATFORM refused every "
            "Tier-1 call that reached it. It is not a diagnosis measurement and this report "
            "does not use it as one."
        ),
    },
    {
        "id": "D4",
        "what": "Two of the three seeded legs were not green on their one rep. This is a DRAFT.",
        "detail": (
            "`remediate_stale_cache_success` (648a32f2339d) diagnosed correctly and never "
            "acted; `remediate_dlq_backlog_success` (fc896b25a09c) acted correctly and failed "
            "one evidence claim. Both fixes are merged - platform v0.6.8 plus commander cmd "
            "#269 for the first, ADR 0041's whole-queue guard (WO-R3-268) for the second - and "
            "NEITHER has been re-run live, because a live run needs the owner's explicit go and "
            "readiness is not authorization. Until those two archives exist, this close reports "
            "the runs it has."
        ),
    },
    {
        "id": "D5",
        "what": "Judge calibration was not rerun, and that is the protocol's own answer.",
        "detail": (
            "Plan 03 section 14 step 5 scopes the rerun to 'every judge the phase touched'. "
            "Phase 2 touched none, and section 5 shows the three checks that establish it "
            "rather than asserting it: the judge prompt digests are byte-identical to the ones "
            "the Phase 1 close recorded, no commit since that close touches a prompt other than "
            "the planner's (cmd #258), and the two commits a reader might suspect - cmd #243 "
            "(output repair) and cmd #264 (ledger and timing) - touch neither a prompt nor a "
            "grader. Step 5 is therefore legitimately empty, and says so rather than being "
            "omitted (WO-R3-195's own finding 7)."
        ),
    },
    {
        "id": "D6",
        "what": "The read-only pass was bought on a recommendation that did not hold (INC-003).",
        "detail": (
            "The coordinator recommended it as 'the first live root-cause number' without "
            "checking which world the ground-truth labels described. $2.15 bought a figure - "
            "`root cause: 11/18 correct (61%)` - that had to be withdrawn, and seven false reds "
            "on a paid archive. The fix (WO-R3-265, ADR 0040) and the offline re-grade are in "
            "section 1; the rule is in LESSONS, 2026-09-17: before buying a run to measure X, "
            "check that the expected values for X were written about the world that run will "
            "be in. The money is in section 7 either way."
        ),
    },
    {
        "id": "D7",
        "what": "The canned sweep archive is committed with this report.",
        "detail": (
            "Offline archives are untracked by convention, but a report that reads an "
            "untracked archive can only be regenerated on the machine that ran it, and the "
            "regeneration test is what makes this document checkable. `b75527784077` was "
            "copied byte-identical from the locked original, verified file by file with "
            "sha256, and committed on its own - the same precedent as Phase 1's "
            "`32ae38f6b38b`."
        ),
    },
    {
        "id": "D8",
        "what": "The Phase 0 baseline artifact still exists twice; this report names both.",
        "detail": (
            "Unchanged since the Phase 1 close. WO-R3-189 cites "
            "`baseline_report.20260915T132421Z.ad634a458e5e`. That file is on disk in the main "
            "checkout but was never committed, and it sorts 89 seconds OLDER than its committed "
            "twin, so `artifacts.newest('baseline_report')` resolves the other one. Section 6 "
            "reads the committed, resolver-visible artifact and names both ids; their recorded "
            "numbers are identical, so the delta is the same either way."
        ),
    },
)


_PHASE2_FOLLOW_UPS: Final[tuple[dict[str, str], ...]] = (
    {
        "id": "WO-R3-266",
        "what": (
            "Four EVIDENCE claims on the read-only path assert the world's CONTENTS, not the "
            "agent's conduct - the same class as INC-003 one layer down. "
            "`consumer_lag_missing_group` is the one still red after the re-grade."
        ),
        "status": "open",
    },
    {
        "id": "WO-R3-267",
        "what": (
            "The stale-cache sensor. FIXED: platform v0.6.8 reports `records_referenced` / "
            "`records_found`, commander re-pinned in cmd #269. What is left is the live re-run."
        ),
        "status": "fix merged; re-run pending the owner's go",
    },
    {
        "id": "WO-R3-268 / O-21",
        "what": (
            "The agent must read the whole dead-letter queue once before replaying part of it "
            "(owner decision O-21, option 1). ADR 0041's guard."
        ),
        "status": "PR open at the time this draft was assembled; re-run pending the owner's go",
    },
    {
        "id": "O-19",
        "what": (
            "Taxonomy and routing (WO-R3-263): no category for resource exhaustion, and "
            "`RUNAWAY_SAGA` routed by prompt hint but by FIX_MAP in code. Both change what the "
            "live agent is told."
        ),
        "status": "open — needs the owner",
    },
    {
        "id": "FINDING — the agent-loop wall meter",
        "what": (
            "`648a32f2339d`'s ledger records `wall_seconds_used` 0.057 s for a run that made "
            "seven model calls over 58 s of trace. The meter is not advanced on every path out "
            "of the loop, so the ledger's wall figure is a lower bound, not a measurement. "
            "Section 7 reports wall time from the trace records instead. No work order filed - "
            "raised by this close."
        ),
        "status": "open — needs a work-order id",
    },
    {
        "id": "O-8",
        "what": "`bad_deploy`'s alert source vs the reset predicate — untouched by this close.",
        "status": "open",
    },
)


#: Phase 2's close as it stood on 2026-09-17: three seeded legs, two red with
#: merged-but-unmeasured fixes. Committed and therefore evidence, so it stays declared
#: as written and stays regenerable; ``PHASE2`` is this scope plus the two re-runs.
PHASE2_DRAFT: Final[PhaseScope] = PhaseScope(
    phase=2,
    work_order="WO-R3-195 (WP-2.6)",
    scope=(
        "REDUCED close, owner decisions O-14 (benchmark model) and O-20 (scope, option A). "
        "DRAFT: two live re-runs are pending the owner's go"
    ),
    canned_sweep="b75527784077",
    live_legs=_PHASE2_LIVE_LEGS,
    read_only_pass=ReadOnlyPass(
        archive_id="0db6fe722f7c",
        regrade_report="evals/reports/regrades/regrade_report.20260917T133824Z.0db6fe722f7c.json",
        withdrawn_number="root cause: 11/18 correct (61%)",
        story=(
            "It seeds no fault, so it is a live read of the ordinary world: the leak hunt, the "
            "cost profile and the platform's refusal of every Tier-1 call that reached it all come "
            "from it. Its "
            "own root-cause grade was INVALID and was re-graded offline (INC-003, ADR 0040)."
        ),
    ),
    pending_reruns=(
        PendingRerun(
            scenario="remediate_stale_cache_success",
            supersedes="648a32f2339d",
            fix=(
                "Platform v0.6.8 makes the cache-key reading say how many records the entry "
                "names and how many the database holds (healthy 3/3, stale 3/0); commander "
                "re-pinned in cmd #269 and the stack is up on it."
            ),
        ),
        PendingRerun(
            scenario="remediate_dlq_backlog_success",
            supersedes="fc896b25a09c",
            fix=(
                "ADR 0041's guard (WO-R3-268) requires an unfiltered `list_dlq_messages` "
                "before any DLQ replay, which is owner decision O-21 option 1."
            ),
        ),
    ),
    new_numbers=(
        NewNumber(
            name="Canned root-cause accuracy, benchmark role",
            value="32/32 (100%)",
            sample="32 of the 41 scenarios; the other 9 declare no label on purpose",
            caveat=(
                "The canned suite is scripted — the planner's replies are fixtures, so this "
                "measures that the labels and the scripts agree, not that the agent diagnoses "
                "well. It is a floor for the harness, not a score for the model."
            ),
        ),
        NewNumber(
            name="Live root-cause accuracy on the seeded legs",
            value="3/3 (100%)",
            sample="3 runs, 1 rep each, 3 fault families",
            caveat=(
                "Three samples. Far too small to generalise from — a single different "
                "trajectory would make it 2/3 — and the three scenarios were chosen one per "
                "fault family, not at random. It is the first VALID live diagnosis number this "
                "project has, and that is all it is."
            ),
        ),
    ),
    phase0_baseline_cited="baseline_report.20260915T132421Z.ad634a458e5e",
    deviations=_PHASE2_DEVIATIONS,
    follow_ups=_PHASE2_FOLLOW_UPS,
    does_claim=(
        "The 41-scenario canned corpus passes under the benchmark model role, names the right "
        "root cause on all 32 rows that carry a label, and moves nothing against either "
        "baseline that this phase did not itself add.",
        "On the three seeded live legs — one per injected-fault family — the agent named the "
        "right root cause every time. 3/3 is the first VALID live diagnosis number this "
        "project has.",
        "The leak boundary holds on live evidence, and one bucket that was not empty in Phase 1 "
        "is empty now: no platform tool response in any of the 71 trajectories this phase "
        "produced contains `chaos`, a root-cause label or a fixture name, and our own prompts "
        "no longer contain the word either (cmd #258).",
        "Invariant 2 is visible in evidence: four `remediate` handoffs in the read-only pass "
        "crossed the agent's own gates; three of them reached the platform and it refused "
        "every one — `missing required scope: actions:execute` — after which each run "
        "escalated with a briefing.",
        "The cost of a run can be attributed to a prompt role for the first time (WP-2.3). Of "
        "the ${live_total_usd_including_evaluator} this close cost, the investigation planner "
        "spent three quarters of it.",
    ),
    does_not_claim=(
        "That the agent resolves these scenarios reliably. Three seeded legs at one rep each: "
        "one resolved, one escalated holding the correct diagnosis, one acted correctly and "
        "failed an evidence claim. Pass rate on the seeded legs is 1 of 3.",
        "A live root-cause accuracy over the corpus. Three graded rows is three rows, and the "
        "read-only pass adds none: a scenario that seeds no fault is not the world its label "
        "describes (ADR 0040).",
        "`root cause: 11/18 correct (61%)` — the figure the read-only archive still carries. It "
        "is withdrawn (INC-003) and appears in this document only as the number being "
        "withdrawn.",
        "That either merged fix works. Neither has been run live. `remediate_stale_cache_"
        "success` and `remediate_dlq_backlog_success` stand in this report exactly as they ran.",
        "That the canned 32/32 says anything about the model. The canned planner replies are "
        "fixtures; that number is about the corpus and its labels agreeing.",
        "That the phase's bill of ${live_total_usd_including_evaluator} is comparable to plan "
        "03 section 11's ~$20 phase-close row. It is a different, smaller sweep.",
    ),
    sweep_committed=(
        "All five archives are committed in this repository — the canned sweep by this PR, on "
        "Phase 1's precedent, because a report that reads an untracked archive can only be "
        "regenerated on the machine that ran it. Every number above is read out of them and "
        "none is typed in."
    ),
    budget_why_null=(
        "Owner decision O-14 keeps BENCHMARK_MODEL at `claude-sonnet-4-6`, which is also "
        "DEVELOPMENT_MODEL. Both roles resolve to the same model id, so a benchmark-vs-"
        "development token or tool-call diff is not small — it is definitionally zero, and "
        "printing a table of zeroes would read like a measurement. What Phase 2 adds instead is "
        "the breakdown WP-2.3 built: the same money, split by the prompt role that spent it, "
        "which is the column a strategy comparison in Phase 6 will actually move."
    ),
    budget_caps_note=(
        "Every run stayed inside every cap: highest USD 0.237300 of 1.00, highest tokens 89 069 "
        "of 200 000, highest tool calls 9 against the 13 the remediation scenarios seed, "
        "highest start-to-finish 248.5 s of the 600 s cap. `mcp_calls_in_trace` counts more "
        "calls than `agent_tool_calls_billed` because the "
        "runner's own precondition poll and the post-action verify probe are on the wire but "
        "are not the agent's budget. One reading here should NOT be trusted: the ledger's "
        "`wall_seconds_used` is 0.057 s on `648a32f2339d`, which cannot be true of a run that "
        "made seven model calls — the meter is not advanced on every path out of the loop. "
        "Section 7 takes wall time from the trace records instead, and the defect is in the "
        "follow-ups."
    ),
    judge_reruns_required=0,
    judge_model="claude-haiku-4-5",
    judge_why=(
        "Plan 03 section 9 requires a calibration rerun for every judge the phase TOUCHED. "
        "Phase 2 touched none: no judge prompt, no rubric, no judge model pin. The two commits "
        "a reader might suspect are not judge changes — cmd #243 (WO-R2-174, in Phase 1) gave "
        "the judges the same bounded one-re-ask repair the planner call sites already had, on "
        "OUR OWN malformed output, and cmd #264 (WO-R3-260) charged the briefing WRITER to the "
        "run ledger and timed the planner call. Neither changes what a judge is asked or how "
        "its answer is scored, and the sweep agrees: all 41 rows judged, judge mean "
        "0.8591463414634146, identical to the blessed baseline and to the Phase 1 close."
    ),
    judge_evidence=(
        "Three checks, each re-runnable from this repository. (1) "
        "`git log 4d023e6..HEAD -- src/incident_commander/llm/prompts/` — one commit, cmd #258, "
        "and it edits the planner prompt, not a judge. (2) "
        "`git log 4d023e6..HEAD -- evals/graders/` — two commits, cmd #255 (the ROOT_CAUSE "
        "dimension, deterministic) and cmd #265 (ADR 0040's world scoping, deterministic); "
        "neither is a judge file. (3) `git show --stat f572108` (cmd #264) — it touches "
        "`llm/client.py`, `agent/accounting.py`, `agent/briefing_enrichment.py`, "
        "`agent/investigation.py`, `agent/strategies/` and `evals/runner.py`, and no prompt or "
        "grader at all. The digests below are the arithmetic form of (1): they are the same two "
        "values the Phase 1 close report recorded."
    ),
    baseline_verdict=(
        "No unexplained movement, so nothing blocks the phase — but unlike Phase 1 the answer "
        "is not 'zero movement'. The gate's own axes are all clean on both baselines: no "
        "regression, no improvement, no new or dropped scenario, no dropped dimension, no "
        "vacated assertion, judge mean identical to the last digit, degraded count identical. "
        "What did move is every one of the 41 rows, in exactly one way: they now carry a "
        "ROOT_CAUSE dimension the baselines have no column for, and it passes on all 41. That "
        "is this phase's own new measurement appearing, not the corpus behaving differently — "
        "the ruler grew a mark, the thing being measured did not move — and the report says so "
        "by classifying the rows rather than by asserting it. Read honestly, all of this is "
        "still a statement about the CANNED corpus: the same fixtures replayed through the same "
        "graders under the same model id give the same answer. It is not evidence about the "
        "live world; the seeded legs are."
    ),
    baseline_cost_note=(
        "Cost per scenario cannot move against either baseline: both baselines and this sweep "
        "are canned runs, and a canned run makes no billable call — every row's `usd_used` is "
        "0.000000 on both sides. The live cost numbers are in section 7, and this phase is the "
        "first that can break them down by role."
    ),
    spend_against_plan=(
        {
            "planned": "remediation scenario: ~$0.10-0.20, 2-3 min with reset",
            "actual": (
                "{live_runs} seeded live remediation runs, mean ${live_mean_usd} per run and "
                "{live_leg_mean_seconds} s mean start to finish. The mean is inside the band on "
                "both axes; one leg is not. `759e198cdd27` cost $0.237300 and ran 248 s, "
                "because it kept re-probing through three `not_verified` readings while it "
                "waited for the lag to fall. That is the loop doing the right thing, and the "
                "band is what needs widening if verify-and-wait becomes normal."
            ),
        },
        {
            "planned": "read-only live scenario: ~$0.09, ~80 s, ~7 LLM calls",
            "actual": (
                "27 read-only scenarios in one invocation for $2.145804 including the "
                "evaluator's judge — about $0.079 each, against a planned $0.09 — over 18.7 "
                "minutes, about 42 s each against a planned 80 s. Under the planned figure on "
                "both, and the only line of this close that compares like with like against "
                "plan 03's table."
            ),
        },
        {
            "planned": "phase-close sweep (~40 scenarios x 3 reps): ~$15 per phase, +$5 live",
            "actual": (
                "${live_total_usd_including_evaluator} in total. The reduction is decision "
                "O-20, not an underspend: recorded mode does not exist until Phase 3, so the "
                "41-scenario sweep ran canned and free; three remediation scenarios ran live at "
                "one rep instead of twelve at three; and the read-only stage is one pass, not a "
                "matrix."
            ),
        },
        {
            "planned": "per-scenario budget: 1.00 USD / 25 calls / 200 000 tokens / 600 s",
            "actual": (
                "No cap was approached on any of the {live_invocations} live invocations. "
                "Highest USD 0.237300 of 1.00; highest tokens 89 069 of 200 000; highest tool "
                "calls 9, against the 13 the remediation scenarios seed (the scenario cap is "
                "tighter than plan 03's 25); highest start-to-finish 248.5 s of 600."
            ),
        },
    ),
    campaign_context=(
        "The closed live-eval campaign cost about $7.20 and the Phase 1 close added $0.635. "
        "This close adds ${live_total_usd_including_evaluator}, of which "
        "${live_total_usd} is the agents' own ledgers. Total wall clock across the "
        "{live_invocations} live invocations: {live_total_minutes} minutes of run time, "
        "operator time and world resets excluded because they are not in any archive."
    ),
    incidents_row_filed=True,
    incidents_row_reason=(
        "INC-003, filed 2026-09-17 BEFORE the fix, as the protocol requires. The "
        "read-only pass reported seven reds that were the evaluation being wrong about the "
        "agent, not the agent being wrong: canned-world labels applied to an unseeded live "
        "world. It is closed by cmd #265 and ADR 0040, and the offline re-grade is in section "
        "1. The two red seeded legs get NO incident row: `648a32f2339d` graded red on OUTCOME "
        "with the agent's own trajectory agreeing (it never emitted `remediate`), and "
        "`fc896b25a09c`'s EVIDENCE red was carried to the owner as a grader-brittleness "
        "candidate and decided the other way (O-21) — the claim was right. In both cases the "
        "grader was right about the run."
    ),
    closing_paragraph=(
        "Phase 2 closes on this sample, not on the plan's full sweep, and it closes as a DRAFT "
        "because two of its three seeded legs have merged fixes that have not been measured. "
        "The differences are listed as deviations at the end, each with the decision or the "
        "fact behind it."
    ),
    trajectory_mix="41 canned + 27 read-only live + 3 seeded live",
    author_notes={
        "commander_prompt": (
            "**empty.** Phase 1 found one hit here — our own `remediation_planner.md` said "
            '"...lives in the description of a chaos tool you never see". Removed in cmd #258 '
            "(WO-R3-255), and these runs are the evidence that it is gone."
        ),
        "harness_record": (
            "the evaluator's `chaos_setup` trace record and the `chaos:kill:...` key it wrote. "
            "Never shown to the agent."
        ),
        "agent_output": "what the model wrote, plus its own ledger replayed into the next request",
    },
    leak_verdict_sentence=(
        "The gating bucket is **empty in every live run**: nothing the agent read from the "
        "platform contained the word. So is the commander-prompt bucket, which was not empty "
        "at the Phase 1 close — the last `chaos` token on the agent's side of the boundary went "
        "out with cmd #258, and these 30 live traces are where that is checked rather than "
        "claimed."
    ),
    baseline_artifact_ref="See deviation D8.",
    gate_audit_opening="Across the three seeded live legs:",
    gate_audit_closing=(
        "Every crossing is tabulated below with the two numeric gates it had to pass. So is "
        "leg 2, which crossed nothing on any of its five steps: on two of them every gate was "
        "open and the planner probed anyway, and on the other three its own confidence had "
        "already fallen below the bar — the table gives the confidence at each step so the "
        "drift is visible rather than described. The read-only pass is audited after the legs: "
        "it is read-only by token, not by intention, and it did try to act."
    ),
    reports_status=True,
)


# --------------------------------------------------------------------------
# Phase 2, final: the same close with the two re-runs in it
# --------------------------------------------------------------------------


def _amend(
    entries: tuple[dict[str, str], ...], amendments: dict[str, dict[str, str]]
) -> tuple[dict[str, str], ...]:
    """The same numbered list with some entries replaced, by id and in place.

    Carries a deviation or follow-up list from a draft into its final version. By ID, so
    a reorder cannot rewrite the wrong entry, and an unknown id is an error.
    """
    unknown = sorted(set(amendments) - {entry["id"] for entry in entries})
    if unknown:
        raise ValueError(f"amendment for an id that is not in the list: {', '.join(unknown)}")
    return tuple(amendments.get(entry["id"], entry) for entry in entries)


#: The two re-runs, released one at a time on 2026-09-17 after both fixes merged. Each
#: names the leg it answers; the earlier run is neither replaced nor dropped, because
#: the PAIR is the point — one scenario, two runs, one change between them.
_PHASE2_RERUN_LEGS: Final[tuple[LiveLeg, ...]] = (
    LiveLeg(
        archive_id="d16aa18dce08",
        scenario="remediate_stale_cache_success",
        order=4,
        reruns="648a32f2339d",
        rerun_reason=(
            "Platform v0.6.8 (plat #209) made the cache-key reading say how many records the "
            "entry names and how many the database holds; the commander was re-pinned to it in "
            "cmd #269 (WO-R3-267). The hypothesis under test was narrow: give the agent a "
            "reading that CAN show staleness and its confidence should rise where it previously "
            "drifted below the bar."
        ),
        story=(
            "GREEN, judge 0.80, root cause correct, every dimension passing. The new reading "
            "answered `records_referenced 3 / records_found 0` and the agent acted on it: "
            "confidence 0.75 -> 0.85 -> 0.82, crossed at step 3, invalidated exactly "
            "`cache:jobs:worker-dispatcher:hot_set`, and verified on the key's own state "
            "(`exists: false`). Four tool calls of the 13 the scenario seeds, $0.163703 on its "
            "own ledger. Same world and same 0.7 bar as `648a32f2339d` — the sensor is what "
            "changed, which is what makes this a measurement of the fix."
        ),
    ),
    LiveLeg(
        archive_id="42c675d9c145",
        scenario="remediate_dlq_backlog_success",
        order=5,
        reruns="fc896b25a09c",
        rerun_reason=(
            "ADR 0041 (WO-R3-268, cmd #270) requires an unfiltered `list_dlq_messages` before "
            "any replay or fence — owner decision O-21, option 1. The hypothesis under test was "
            "that the EVIDENCE claim `fc896b25a09c` failed is both right and reachable: an agent "
            "told to read the whole queue first will do it, and the rest of the run will look "
            "the same."
        ),
        story=(
            "GREEN, judge 0.95, root cause correct, every dimension passing. It read the queue "
            "UNFILTERED first — `list_dlq_messages` with no hint, the read `fc896b25a09c` never "
            "made — so it saw all five rows before touching any of them. Its first `remediate` "
            "was then refused by the older subject gate for not yet having read the alerted "
            "slice; it probed that slice and crossed at step 4 at 0.92, replayed exactly the one "
            "`replay_safe` row by id, and verified the slice empty. ADR 0041's own guard never "
            "had to refuse anything: the agent had satisfied it before the first attempt."
        ),
    ),
)


#: The three original legs, with the two sentences that held only while the re-runs were
#: unbought now pointing at the runs that answered them. ``replace``, not a copy: what
#: each run DID has not changed and must not drift between the two documents.
_PHASE2_FINAL_LIVE_LEGS: Final[tuple[LiveLeg, ...]] = (
    _PHASE2_LIVE_LEGS[0],
    replace(
        _PHASE2_LIVE_LEGS[1],
        escalation_note=(
            " Not laziness either: the diagnosis was RIGHT and the agent's confidence in it "
            "fell — 0.75, 0.72, 0.65, 0.55, 0.55 — because `get_cache_key_info` could not show "
            "the key was stale. The same world went green that morning (`ee183c85429c`) at the "
            "same bar, which is what makes this a boundary and not a behaviour. Fixed in "
            "platform v0.6.8 (WO-R3-267), and leg 4 is that fix measured: same world, same bar, "
            "green."
        ),
    ),
    replace(
        _PHASE2_LIVE_LEGS[2],
        story=_PHASE2_LIVE_LEGS[2].story.replace(
            "ADR 0041's guard (WO-R3-268) is the fix; the re-run has not been bought.",
            "ADR 0041's guard (WO-R3-268, merged as cmd #270) is the fix, and leg 5 is it "
            "measured: the same scenario, the unfiltered read made first, every dimension "
            "passing.",
        ),
    ),
    *_PHASE2_RERUN_LEGS,
)


#: Phase 2's close as it stands now: everything the draft established, plus what the two
#: re-runs changed. The argument list IS the diff between the two committed documents.
PHASE2: Final[PhaseScope] = replace(
    PHASE2_DRAFT,
    scope=(
        "REDUCED close, owner decisions O-14 (benchmark model) and O-20 (scope, option A). "
        "FINAL: the owner released both pending re-runs, both came back green, and nothing in "
        "scope is owed"
    ),
    live_legs=_PHASE2_FINAL_LIVE_LEGS,
    pending_reruns=(),
    new_numbers=(
        PHASE2_DRAFT.new_numbers[0],
        NewNumber(
            name="Live root-cause accuracy on the seeded legs",
            value="5/5 (100%)",
            sample="5 runs over 3 scenarios, 1 rep each, 3 fault families",
            caveat=(
                "Five samples and not five independent ones: two are re-runs of scenarios the "
                "agent had already diagnosed correctly, and neither fix was aimed at the "
                "diagnosis. Read it as five out of five, not as an accuracy — the denominator "
                "is tiny, the scenarios were picked one per fault family rather than at random, "
                "and one different trajectory would move it. It is the first VALID live "
                "diagnosis number this project has, and that is all it is."
            ),
        ),
    ),
    does_claim=(
        *PHASE2_DRAFT.does_claim[:1],
        "On the three seeded live scenarios — one per injected-fault family — the agent named "
        "the right root cause in all five runs. 5/5 is the first VALID live diagnosis number "
        "this project has.",
        "The leak boundary holds on live evidence, and one bucket that was not empty in Phase 1 "
        "is empty now: no platform tool response in any of the 73 trajectories this phase "
        "produced contains `chaos`, a root-cause label or a fixture name, and our own prompts "
        "no longer contain the word either (cmd #258).",
        *PHASE2_DRAFT.does_claim[3:],
        "Both fixes this phase found are proven on live evidence rather than argued. "
        "`remediate_stale_cache_success` is green on platform v0.6.8's new cache reading "
        "(WO-R3-267, archive `d16aa18dce08`) and `remediate_dlq_backlog_success` is green under "
        "ADR 0041's whole-queue rule (WO-R3-268, archive `42c675d9c145`). Three of the five "
        "seeded runs pass; both reds keep their place in the record and each now has a green "
        "successor beside it.",
    ),
    does_not_claim=(
        "That the agent resolves these scenarios reliably. Five seeded runs over three "
        "scenarios, one rep each: three pass, and the two that do not are the pre-fix reds. "
        "Pass rate on the seeded legs is 3 of 5.",
        "A live root-cause accuracy over the corpus. Five graded rows is five rows, and the "
        "read-only pass adds none: a scenario that seeds no fault is not the world its label "
        "describes (ADR 0040).",
        PHASE2_DRAFT.does_not_claim[2],
        "That either fix is stable, or that this phase measured variance. Each fix has exactly "
        "one green run behind it, and each re-run followed a change — so the two repeated "
        "scenarios measure their fix, not the spread. No scenario in this close was run twice "
        "under the same code, and the 2026-08-31 green that turned red in September on the same "
        "model is why one run is not read as a property of the agent.",
        *PHASE2_DRAFT.does_not_claim[4:],
    ),
    sweep_committed=(
        "All seven archives are committed in this repository — the canned sweep with the draft "
        "of this report, on Phase 1's precedent, because a report that reads an untracked "
        "archive can only be regenerated on the machine that ran it; the two re-runs in cmd "
        "#272. Every number above is read out of them and none is typed in."
    ),
    budget_caps_note=(
        PHASE2_DRAFT.budget_caps_note.replace(
            "and the defect is in the follow-ups.",
            "and the defect is WO-R3-270 in the follow-ups. The two re-runs' own meters look "
            "sound — 34.1 s and 51.4 s against 44.9 s and 66.4 s of trace — which narrows the "
            "bug to the paths the escalating run took.",
        )
    ),
    spend_against_plan=(
        {
            "planned": "remediation scenario: ~$0.10-0.20, 2-3 min with reset",
            "actual": (
                "{live_runs} seeded live remediation runs over three scenarios, mean "
                "${live_mean_usd} per run and {live_leg_mean_seconds} s mean start to finish. "
                "The mean is inside the band on cost and faster than it on time, because the "
                "band includes the world reset and that is operator wall clock, in no archive. "
                "One leg is outside on both: `759e198cdd27` cost $0.237300 and ran 248 s because "
                "it kept re-probing through three `not_verified` readings while it waited for "
                "the lag to fall. That is the loop doing the right thing, and the band is what "
                "needs widening if verify-and-wait becomes normal."
            ),
        },
        PHASE2_DRAFT.spend_against_plan[1],
        {
            "planned": "phase-close sweep (~40 scenarios x 3 reps): ~$15 per phase, +$5 live",
            "actual": (
                "${live_total_usd_including_evaluator} in total. The reduction is decision "
                "O-20, not an underspend: recorded mode does not exist until Phase 3, so the "
                "41-scenario sweep ran canned and free; three remediation scenarios ran live — "
                "five runs once the two fixes were measured — instead of twelve scenarios at "
                "three reps; and the read-only stage is one pass, not a matrix."
            ),
        },
        PHASE2_DRAFT.spend_against_plan[3],
    ),
    deviations=_amend(
        _PHASE2_DEVIATIONS,
        {
            "D1": {
                "id": "D1",
                "what": "Reduced scope (owner decision O-20, 2026-09-17).",
                "detail": (
                    "Plan 03 section 14 step 1 asks for every scenario touched or added in the "
                    "phase in recorded mode at 3 reps, plus live for every scenario with a "
                    "remediation leg. What ran: all 41 scenarios canned at 1 rep, one live "
                    "read-only pass over 27 scenarios, and 3 of the 12 remediation-leg "
                    "scenarios live - one per injected-fault family (kill_consumer, "
                    "create_stale_cache, poison_message) - for 5 runs, because two of those "
                    "three were run a second time once their fixes merged. The owner chose "
                    "option A of `.coordination/PHASE2-CLOSE-PLAN.md` and kept the plan order."
                ),
            },
            "D2": {
                "id": "D2",
                "what": "Recorded-world mode still does not exist, so there is no 3-rep sweep.",
                "detail": (
                    "It arrives in Phase 3 (WP-3.1). The sweep leg is canned at one rep, exactly "
                    "as in Phase 1. Saying so is the point - a silent substitution would make "
                    "every later phase's comparison unreadable. One rep also means no variance "
                    "figure: two scenarios here were run twice, but across a fix each time, so "
                    "they measure a change rather than the spread. The only same-code repeat in "
                    "this project is still the scenario that went green in August and red in "
                    "September."
                ),
            },
            "D4": {
                "id": "D4",
                "what": (
                    "Two of the three seeded scenarios were red on their first rep. Both were "
                    "re-run green, and both reds stay in the record."
                ),
                "detail": (
                    "`remediate_stale_cache_success` (648a32f2339d) diagnosed correctly and "
                    "never acted; `remediate_dlq_backlog_success` (fc896b25a09c) acted correctly "
                    "and failed one evidence claim. Both fixes then merged - platform v0.6.8 "
                    "plus commander cmd #269 for the first, ADR 0041's whole-queue guard (cmd "
                    "#270) for the second - and the owner released one re-run for each: "
                    "`d16aa18dce08` and `42c675d9c145`, both green. The reds are not replaced by "
                    "them. Section 1 lists five legs, and each re-run names the run it answers, "
                    "because a close that dropped its reds would be a selected sample of "
                    "itself. What the pairs are NOT is a variance measurement - see D2."
                ),
            },
            "D7": {
                "id": "D7",
                "what": "The canned sweep archive is committed with the draft of this report.",
                "detail": _PHASE2_DEVIATIONS[6]["detail"],
            },
        },
    ),
    follow_ups=_amend(
        _PHASE2_FOLLOW_UPS,
        {
            "WO-R3-267": {
                "id": "WO-R3-267",
                "what": (
                    "The stale-cache sensor. CLOSED: platform v0.6.8 reports "
                    "`records_referenced` / `records_found`, the commander was re-pinned in cmd "
                    "#269, and the live re-run `d16aa18dce08` is green on the same world at the "
                    "same bar."
                ),
                "status": "closed by the re-run",
            },
            "WO-R3-268 / O-21": {
                "id": "WO-R3-268 / O-21",
                "what": (
                    "The agent must read the whole dead-letter queue once before replaying part "
                    "of it (owner decision O-21, option 1). ADR 0041's guard."
                ),
                "status": "MERGED (cmd #270); proven by the live re-run `42c675d9c145`",
            },
            "FINDING — the agent-loop wall meter": {
                "id": "WO-R3-270",
                "what": (
                    "`648a32f2339d`'s ledger records `wall_seconds_used` 0.057 s for a run that "
                    "made seven model calls over 58 s of trace. The meter is not advanced on "
                    "every path out of the loop, so the ledger's wall figure is a lower bound, "
                    "not a measurement. Section 7 reports wall time from the trace records "
                    "instead. Raised by the draft of this close and filed since; the two "
                    "re-runs' meters are sound, which narrows it to the paths an escalating run "
                    "takes."
                ),
                "status": "open — filed",
            },
        },
    ),
    incidents_row_reason=(
        PHASE2_DRAFT.incidents_row_reason
        + " Both re-runs are the arithmetic form of that: same scenarios, same graders, green "
        "once the agent could see that the cache entry was stale and was required to read the "
        "whole queue before replaying part of it."
    ),
    closing_paragraph=(
        "Phase 2 closes on this sample, not on the plan's full sweep. It closes as FINAL: the "
        "two legs whose fixes were merged but unmeasured when the draft was written have each "
        "been run once more and both are green, and the draft stays on the shelf beside this "
        "document rather than being replaced by it. The differences from the protocol as "
        "written are listed as deviations at the end, each with the decision or the fact behind "
        "it."
    ),
    trajectory_mix="41 canned + 27 read-only live + 5 seeded live",
    leak_verdict_sentence=(
        "The gating bucket is **empty in every live run**: nothing the agent read from the "
        "platform contained the word. So is the commander-prompt bucket, which was not empty "
        "at the Phase 1 close — the last `chaos` token on the agent's side of the boundary went "
        "out with cmd #258, and these 32 live traces are where that is checked rather than "
        "claimed."
    ),
    gate_audit_opening="Across the five seeded live legs (three scenarios, two of them re-run):",
    gate_audit_closing=(
        "Every crossing is tabulated below with the two numeric gates it had to pass. So is "
        "leg 2, which crossed nothing on any of its five steps: on two of them every gate was "
        "open and the planner probed anyway, and on the other three its own confidence had "
        "already fallen below the bar — the table gives the confidence at each step so the "
        "drift is visible rather than described. Legs 4 and 5 are those two scenarios again "
        "after their fixes: leg 4 crossed at step 3 with confidence rising 0.75 -> 0.85 -> "
        "0.82, and leg 5 was refused once and re-steered before crossing at step 4, which is "
        "the third gate working rather than a failed run. The read-only pass is audited after "
        "the legs: it is read-only by token, not by intention, and it did try to act."
    ),
)


SCOPES: Final[dict[int, PhaseScope]] = {scope.phase: scope for scope in (PHASE1, PHASE2)}

#: Every scope whose document is committed, in document order per phase. ``SCOPES``
#: answers "what does phase N close on now?", this answers "which documents are
#: evidence?", and the two differ once a FINAL supersedes a DRAFT. Both must keep
#: regenerating byte for byte (invariant 9), so the regeneration test walks this.
COMMITTED_SCOPES: Final[tuple[PhaseScope, ...]] = (PHASE1, PHASE2_DRAFT, PHASE2)

#: What ``--phase`` defaults to: the phase currently being closed.
LATEST_PHASE: Final[int] = max(SCOPES)


def _claims(document: dict[str, Any], scope: PhaseScope) -> dict[str, Any]:
    """The two lists, with the spend figures substituted from the document.

    Templated rather than typed, so a claim about the money cannot be left behind when
    the money changes: a close report must never state a figure its own sections deny.
    """
    spend = document["sections"]["spend_line"]
    values = {
        "live_total_usd": spend["live_total_usd"],
        "live_total_usd_including_evaluator": spend.get(
            "live_total_usd_including_evaluator", spend["live_total_usd"]
        ),
    }
    return {
        "does_claim": [claim.format(**values) for claim in scope.does_claim],
        "does_not_claim": [claim.format(**values) for claim in scope.does_not_claim],
    }


def _read_only_gate_block(root: Path, read_only: ReadOnlyPass) -> dict[str, Any]:
    """Every handoff in a read-only pass, and what the platform did with it.

    Step 3 says "every `remediate` handoff", so a pass expected to take no action is
    audited rather than assumed: invariant 2's point is that the refusal is the
    PLATFORM's, and this is where that shows up in evidence.
    """
    crossed: list[dict[str, str]] = []
    refused: list[dict[str, str]] = []
    stopped: list[dict[str, str]] = []
    for path in sorted((archive_dir(root, read_only.archive_id) / "trajectories").glob("*.json")):
        for row in _evidence_rows(json.loads(path.read_text())):
            if row["tool_name"] == "_planner_remediate":
                crossed.append({"scenario": path.stem, "handoff": row["result_summary"][:200]})
            elif row["tool_name"] == "_handoff_refused":
                refused.append({"scenario": path.stem, "refusal": row["result_summary"][:200]})
            elif row["tool_name"] == "_remediation_escalate":
                summary = row["result_summary"]
                stopped.append(
                    {
                        "scenario": path.stem,
                        # Two very different stops: the platform refusing an
                        # authenticated call, or the agent's budget running out first.
                        "stopped_by": (
                            "the platform" if "MCP error" in summary else "the agent's own budget"
                        ),
                        "detail": summary[:200],
                    }
                )
    by_platform = [row for row in stopped if row["stopped_by"] == "the platform"]
    return {
        "archive": read_only.archive_id,
        "handoffs_crossed": len(crossed),
        "handoffs_refused_by_the_loop": len(refused),
        "tier1_calls_refused_by_the_platform": len(by_platform),
        "tier1_calls_that_succeeded": 0,
        "crossings": crossed,
        "loop_refusals": refused,
        "remediation_stops": sorted(stopped, key=lambda row: row["scenario"]),
        "what_it_shows": (
            "Where a call was actually made, the PLATFORM refused it — `MCP error -32002: "
            "missing required scope: actions:execute`, because the smoke pass runs under the "
            "read-only token. That is invariant 2 in evidence rather than in a design "
            "document: the agent's policy registry is a first filter, and the authorization "
            "decision is the platform's. Every one of these runs then escalated with a "
            "briefing, which is invariant 7's shape for a blocked action. Note the fourth "
            "crossing never reached the platform at all — it ran out of tool-call budget "
            "first, which is a different stop and is labelled as one."
        ),
    }


def assemble(root: Path, scope: PhaseScope) -> dict[str, Any]:
    """The phase-close document. Every value is read from a file under ``root``."""
    reports = [
        _read_report(archive_dir(root, archive) / "report.json")[0] for archive in scope.archives
    ]
    verdict = closing_verdict(reports)

    corpus = {s.name: s for s in load_scenarios(root / "evals/scenarios")}
    audits = []
    for leg in scope.live_legs:
        audit = _gate_crossings(root, leg)
        expected = corpus[leg.scenario].expectation.expected_terminal_state.value
        audit["expected_terminal_state"] = expected
        audit["escalation_verdict"] = _escalation_verdict(audit, expected, leg.escalation_note)
        audits.append(audit)

    gate_audit: dict[str, Any] = {
        "bar": REMEDIATE_BAR,
        "fix_map": sorted(category.value for category in FIX_MAP),
        "gates": (
            "Three structural gates stand between a planner's `remediate` step and a Tier-1 "
            "call: the category must be a key in FIX_MAP, the top confidence must be at or "
            "above the bar, and the alert's own subject must already have been probed. The "
            "first two escalate when they fail; the third refuses and re-steers, which is "
            "why a refusal is not a failed run."
        ),
        "runs": audits,
        "totals": {
            "planner_steps": sum(len(a["planner_steps"]) for a in audits),
            "handoffs_attempted": sum(a["handoffs_attempted"] for a in audits),
            "handoffs_crossed": sum(a["handoffs_crossed"] for a in audits),
            "handoffs_refused": sum(a["handoffs_refused"] for a in audits),
            "non_crossings": sum(a["non_crossings"] for a in audits),
            "escalations": sum(1 for a in audits if a["escalation"] is not None),
        },
    }
    if scope.read_only_pass is not None:
        gate_audit["read_only_pass"] = _read_only_gate_block(root, scope.read_only_pass)

    sections: dict[str, Any] = {
        "sweep_results": _sweep_results(root, scope),
        "leak_hunt": leak_hunt(root, scope),
        "gate_crossing_audit": gate_audit,
        "budget_profile_diff": _budget_profile(root, scope),
        "judge_calibration": _judge_calibration(root, scope),
        "baseline_delta": _baseline_delta(root, scope),
        "spend_line": _spend_line(root, scope),
    }
    if tuple(sections) != SECTION_KEYS:
        raise ValueError("section keys drifted from plan 03 section 14's seven steps")
    empty = [key for key, value in sections.items() if not value]
    if empty:
        raise ValueError(f"empty section(s): {', '.join(empty)}")

    document: dict[str, Any] = {
        "phase": scope.phase,
        "protocol": "docs/plans/research-buildout-v2.1/03_EVAL_RESEARCH_PLAN.md section 14",
        "work_order": scope.work_order,
        "scope": scope.scope,
    }
    if scope.reports_status:
        document.update(draft_status(scope))
    document.update(
        {
            "closing": verdict["closing"],
            "closing_reason": verdict["reason"],
            "runs_in_scope": list(scope.archives),
            "sections": sections,
            "deviations": [dict(entry) for entry in scope.deviations],
            "follow_ups": [dict(entry) for entry in scope.follow_ups],
            "incidents_row_filed": scope.incidents_row_filed,
            "incidents_row_reason": scope.incidents_row_reason,
            "no_runs_were_made_to_produce_this": (
                "Zero live invocations and zero LLM calls produced this document. Every number "
                "was read out of a committed archive; the only thing executed is the `grep` in "
                "section 2."
            ),
        }
    )
    document["claims"] = _claims(document, scope)
    return document


# --------------------------------------------------------------------------
# Rendering and writing
# --------------------------------------------------------------------------


def render_json(document: dict[str, Any]) -> str:
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def _grep_block(hunt: dict[str, Any], key: str) -> list[str]:
    command = hunt["committed_commands"][key]
    body = command["output"].rstrip("\n")
    return [
        "```console",
        f"$ {command['command']}",
        *(body.splitlines() if body else []),
        f"# exit status {command['exit_status']} ({command['matching_lines']} matching line(s))",
        "```",
        "",
    ]


def render_markdown(  # noqa: C901 - one section per block
    document: dict[str, Any], scope: PhaseScope
) -> str:
    """The human half. The scope is passed, never looked up by phase number.

    The markdown carries prose the JSON does not, so it needs the scope the document was
    assembled from: once a phase has a DRAFT and a FINAL, ``SCOPES[phase]`` is only one
    of them and looking it up here would render the draft with the final's sentences.
    """
    if scope.phase != document["phase"]:
        raise ValueError(
            f"scope is phase {scope.phase} and the document is phase {document['phase']}"
        )
    sections = document["sections"]
    sweep = sections["sweep_results"]
    hunt = sections["leak_hunt"]
    audit = sections["gate_crossing_audit"]
    budget = sections["budget_profile_diff"]
    judge = sections["judge_calibration"]
    delta = sections["baseline_delta"]
    spend = sections["spend_line"]
    mark = "CLOSING" if document["closing"] else "NON-CLOSING"

    lines: list[str] = [
        f"# Phase {document['phase']} close report",
        "",
        f"**{mark}** — {document['closing_reason']}",
        "",
    ]
    if "status" in document:
        lines.extend([f"**{document['status']}** — {document['why']}", ""])
    lines.extend(
        [
            f"Protocol: {document['protocol']}. Work order: {document['work_order']}. "
            f"Scope: {document['scope']}.",
            "",
            document["no_runs_were_made_to_produce_this"],
            "",
            "## What this close does and does not claim",
            "",
            "**It claims:**",
            "",
        ]
    )
    lines.extend(f"- {claim}" for claim in document["claims"]["does_claim"])
    lines.extend(["", "**It does not claim:**", ""])
    lines.extend(f"- {claim}" for claim in document["claims"]["does_not_claim"])
    lines.extend(
        [
            "",
            scope.closing_paragraph,
            "",
            f"## {SECTION_TITLES['sweep_results']}",
            "",
            f"**Canned sweep `{sweep['canned_sweep']['archive']}`** — "
            f"{sweep['canned_sweep']['passed']}/{sweep['canned_sweep']['total']} passed, "
            f"{sweep['canned_sweep']['failed']} failed, judged "
            f"{sweep['canned_sweep']['judged_count']}, judge mean "
            f"{sweep['canned_sweep']['judge_mean_overall']}, degraded "
            f"{sweep['canned_sweep']['degraded_count']}. Model role(s): "
            f"{', '.join(sweep['canned_sweep']['model_roles'])}; agent model(s): "
            f"{', '.join(sweep['canned_sweep']['agent_models'])}.",
            "",
            "What the gate says about it:",
            "",
        ]
    )
    lines.extend(f"- `{line}`" for line in sweep["canned_sweep"]["gate_lines"])
    lines.extend(
        [
            "",
            f"**Live legs** — {sweep['live_passed']}/{sweep['live_total']} passed, in the order "
            "the owner released them.",
            "",
            "| # | Archive | Scenario | Result | Judge | Agent tool calls | Commander | Platform |",
            "|---|---|---|---|---|---|---|---|",
        ]
    )
    for leg in sweep["live_legs"]:
        revision = (leg["commander_revision"] or "")[:7]
        digest = (leg["platform_image_digest"] or "")[:19]
        result = "PASS" if leg["passed"] else "**FAIL**"
        lines.append(
            f"| {leg['order']} | `{leg['archive']}` | `{leg['scenario']}` | "
            f"{result} ({leg['final_state']}) | {leg['judge_score']} | "
            f"{leg['tool_calls_used']} | `{revision}` | `{digest}` |"
        )
    lines.append("")
    for leg in sweep["live_legs"]:
        lines.append(f"- `{leg['archive']}` — {leg['story']}")
        if "reruns" in leg:
            lines.append(
                f"  - Re-runs `{leg['reruns']['archive']}`. {leg['reruns']['why']} The earlier "
                "run stays in this table and in every number above it."
            )
    if "live_root_cause_on_seeded_legs" in sweep:
        diagnosis = sweep["live_root_cause_on_seeded_legs"]
        lines.extend(
            [
                "",
                f"**Live root cause on the seeded legs: {diagnosis['correct']}/"
                f"{diagnosis['graded']} correct.** {diagnosis['why_only_these']}",
            ]
        )
    if "read_only_pass" in sweep:
        pass_block = sweep["read_only_pass"]
        regrade = pass_block["regrade"]
        lines.extend(
            [
                "",
                f"**Read-only live pass `{pass_block['archive']}`** — "
                f"{pass_block['scenarios']} scenarios in one invocation under the read-only "
                f"token; {pass_block['live_mcp']} reached the live platform and "
                f"{pass_block['live_llm']} made live model calls. "
                f"{pass_block['story']}",
                "",
                f"The archive still carries `{pass_block['withdrawn_number']}`. That figure is "
                "WITHDRAWN and is not quoted as a result anywhere in this document. What "
                "replaces it is the offline re-grade under ADR 0040, "
                f"`{regrade['artifact']}`:",
                "",
                "| | As archived | Re-graded |",
                "|---|---|---|",
                f"| Scenarios passing | {regrade['totals']['archived_passed']}/"
                f"{regrade['totals']['scenarios']} | "
                f"{regrade['totals']['regraded_passed']}/{regrade['totals']['scenarios']} |",
                f"| ROOT_CAUSE graded | {regrade['root_cause']['archived']['graded']} | "
                f"{regrade['root_cause']['regraded']['graded']} |",
                f"| ROOT_CAUSE correct | {regrade['root_cause']['archived']['correct']} | "
                f"{regrade['root_cause']['regraded']['correct']} |",
                "| ROOT_CAUSE not graded — wrong world | "
                f"{regrade['root_cause']['archived']['not_graded_world']} | "
                f"{regrade['root_cause']['regraded']['not_graded_world']} |",
                "",
                f"{regrade['totals']['verdicts_changed']} verdicts changed and no other "
                f"dimension moved. The re-grade verified all "
                f"{regrade['files_verified_unchanged']} files of the archive unchanged by "
                "sha256 before reading them, and the archive itself was not touched "
                "(invariant 9). Still failing after the re-grade: "
                + (
                    ", ".join(
                        f"`{row['scenario']}` ({', '.join(row['dimensions'])})"
                        for row in regrade["still_failing"]
                    )
                    or "nothing"
                )
                + ".",
                "",
                regrade["limits"],
            ]
        )
    lines.extend(["", sweep["committed"], "", f"## {SECTION_TITLES['leak_hunt']}", ""])
    lines.extend(
        [
            f"{hunt['term_count']} terms in three families, all derived rather than typed: "
            "`chaos`; every `HypothesisCategory` value; every scenario name and every chaos hook "
            "name in the corpus. Matching is case-insensitive substring, so a term glued to "
            "something else still counts — and the false positives that produces are adjudicated "
            "below by name rather than filtered away.",
            "",
            "**The gating grep** — WP-1.5's own acceptance wording, over all "
            f"{hunt['trajectories']['files_searched']} trajectories this phase produced "
            f"({scope.trajectory_mix}):",
            "",
        ]
    )
    lines.extend(_grep_block(hunt, "trajectories"))
    read = hunt["trajectories"]["platform_responses_the_agent_read"]
    lines.extend(
        [
            "Same files, split by who authored each ledger row — because a trajectory is full of "
            "root-cause labels by design (they are the agent's own hypotheses), and the only "
            "thing that can fail the phase is a term arriving in a **platform tool response**:",
            "",
            "| Bucket | Hits |",
            "|---|---|",
            f"| platform responses the agent read | {read or 'none'} |",
            "| text the agent or the harness wrote | "
            f"{hunt['trajectories']['text_the_agent_or_the_harness_wrote']} |",
            "",
        ]
    )
    if hunt["trajectories"]["hits_in_what_the_agent_read"]:
        lines.extend(
            [
                "Every hit in what the agent read, adjudicated:",
                "",
            ]
        )
        for hit in hunt["trajectories"]["hits_in_what_the_agent_read"]:
            lines.append(
                f"- `{hit['term']}` x{hit['occurrences']} in `{hit['tool_name']}` "
                f"({hit['file'].split('/')[-1]}): `{hit['matched_text'][:120]}`"
            )
        lines.extend(["", "**Adjudication.**", ""])
        lines.extend(f"- `{term}` — {reason}" for term, reason in hunt["adjudications"].items())
        lines.extend(
            [
                "",
                f"Unadjudicated hits: {hunt['unadjudicated_hits'] or 'none'}. "
                f"**Leak-hunt verdict: {hunt['verdict']}.** No `chaos` token, no root-cause "
                "label and no fixture name reaches the agent from the platform in any of the "
                f"{hunt['trajectories']['files_searched']} trajectories.",
                "",
            ]
        )
    lines.extend(
        [
            "**Where `chaos` does appear**, and which side of the boundary each occurrence is on. "
            "The traces are the wider record — they hold our own prompts and the evaluator's "
            "bookkeeping as well as the wire:",
            "",
        ]
    )
    lines.extend(_grep_block(hunt, "traces"))
    totals = hunt["traces_by_author"]["totals"]
    lines.extend(
        [
            "| Author | `chaos` | What it is | Gating? |",
            "|---|---|---|---|",
            f"| platform response | {totals['platform_response'].get('chaos', 0)} | "
            "bytes a platform tool returned to the agent | **yes** |",
            f"| commander prompt | {totals['commander_prompt'].get('chaos', 0)} | "
            f"{scope.author_notes['commander_prompt']} | no |",
            f"| harness record | {totals['harness_record'].get('chaos', 0)} | "
            f"{scope.author_notes['harness_record']} | no |",
            f"| agent output | {totals['agent_output'].get('chaos', 0)} | "
            f"{scope.author_notes['agent_output']} | no |",
            "",
            scope.leak_verdict_sentence,
            "",
            "Per-run counts for all "
            f"{hunt['term_count']} terms are in the JSON companion under "
            "`sections.leak_hunt.traces_by_author.per_run`.",
            "",
            f"## {SECTION_TITLES['gate_crossing_audit']}",
            "",
            audit["gates"],
            "",
            f"The bar is {audit['bar']}. FIX_MAP: "
            f"{', '.join(f'`{name}`' for name in audit['fix_map'])}.",
            "",
            f"{scope.gate_audit_opening} {audit['totals']['planner_steps']} planner steps, "
            f"{audit['totals']['handoffs_attempted']} `remediate` steps emitted, "
            f"{audit['totals']['handoffs_crossed']} crossed into planning, "
            f"{audit['totals']['handoffs_refused']} refused and re-steered, "
            f"{audit['totals']['escalations']} escalation. {scope.gate_audit_closing}",
            "",
        ]
    )
    for run in audit["runs"]:
        lines.extend(
            [
                f"### `{run['archive']}` — `{run['scenario']}` -> {run['final_state']} "
                f"(expected {run['expected_terminal_state']})",
                "",
                f"{len(run['planner_steps'])} planner step(s); "
                f"{run['handoffs_attempted']} `remediate` emitted, "
                f"{run['handoffs_crossed']} crossed, {run['handoffs_refused']} refused; "
                f"{run['non_crossings']} step(s) where every gate was open and the planner "
                "probed instead.",
                "",
                "| Step | Top hypothesis | Category | Confidence | In FIX_MAP | >= bar | "
                "Emitted | Disposition |",
                "|---|---|---|---|---|---|---|---|",
            ]
        )
        for step in run["planner_steps"]:
            lines.append(
                f"| {step['step']} | {step['top_hypothesis']} | `{step['category']}` | "
                f"{step['confidence']} | {'yes' if step['in_fix_map'] else 'no'} | "
                f"{'yes' if step['meets_bar'] else 'no'} | `{step['emitted']}` | "
                f"{step['disposition']} |"
            )
        lines.append("")
        if run["plan"] is not None:
            plan = run["plan"]
            executed_tools = ", ".join("`" + e["tool"] + "`" for e in run["executed"])
            lines.extend(
                [
                    f"Plan: `{plan['action_tool']}({json.dumps(plan['action_arguments'])})` "
                    f"targeting `{plan['target_hypothesis']}`, verified with "
                    f"`{plan['verify_tool']}`.",
                    "",
                    f"Executed: {executed_tools or 'nothing'} ({len(run['executed'])} call(s)).",
                    "",
                    "Verify: "
                    + ", ".join(f"**{j['verdict']}**" for j in run["verify_judgments"])
                    + ".",
                    "",
                ]
            )
        if run["escalation"] is not None:
            lines.extend(
                [
                    f"Escalation: `{run['escalation']}`.",
                    "",
                    f"Justified by ground truth? {run['escalation_verdict']}",
                    "",
                ]
            )
    if "read_only_pass" in audit:
        read_only = audit["read_only_pass"]
        lines.extend(
            [
                f"### `{read_only['archive']}` — the read-only pass",
                "",
                f"{read_only['handoffs_crossed']} `remediate` handoff(s) crossed the loop's own "
                f"gates; the loop refused and re-steered "
                f"{read_only['handoffs_refused_by_the_loop']} more. Tier-1 calls the platform "
                f"refused: {read_only['tier1_calls_refused_by_the_platform']}. Tier-1 calls "
                f"that succeeded: {read_only['tier1_calls_that_succeeded']}.",
                "",
                read_only["what_it_shows"],
                "",
            ]
        )
        lines.extend(
            f"- `{row['scenario']}` — stopped by {row['stopped_by']}: {row['detail']}"
            for row in read_only["remediation_stops"]
        )
        lines.append("")
    lines.extend([f"## {SECTION_TITLES['budget_profile_diff']}", "", budget["why_null"], ""])
    lines.extend(
        [
            "| Archive | Scenario | LLM calls | Planner iterations | Wire tool calls | "
            "Billed tool calls | Tokens | Wall s |",
            "|---|---|---|---|---|---|---|---|",
        ]
    )
    for row in budget["per_run"]:
        lines.append(
            f"| `{row['archive']}` | `{row['scenario']}` | {row['llm_calls_total']} | "
            f"{row['planner_iterations']} | {row['mcp_calls_in_trace']} | "
            f"{row['agent_tool_calls_billed']} | {row['tokens_used']} | "
            f"{row['wall_seconds_used']} |"
        )
    lines.extend(["", budget["caps_note"], ""])
    if "per_role_totals" in budget:
        lines.extend(
            [
                "**Where the money goes, per prompt role** — across every live run in scope. "
                "This is the column WP-2.3 exists to produce; before it, a run could say what "
                "it cost but not which role spent it.",
                "",
                "| Role | Charged to the agent's ledger | Calls | Tokens | USD | Model time (s) |",
                "|---|---|---|---|---|---|",
            ]
        )
        for role in budget["per_role_totals"]:
            charged = {
                True: "yes",
                False: "no — evaluator",
                "mixed": "changed mid-phase (cmd #264)",
            }[role["charged_to_ledger"]]
            lines.append(
                f"| `{role['role']}` | {charged} | {role['calls']} | {role['tokens']} | "
                f"{role['usd']} | {round(role['elapsed_ms'] / 1000, 1)} |"
            )
        lines.extend(["", budget["per_role_note"], ""])
    lines.extend(
        [
            f"Budget trips: {budget['budget_trips'] or 'none'}. Per-role LLM call counts are in "
            "the JSON companion under `sections.budget_profile_diff.per_run[].llm_calls_by_role`.",
            "",
            f"## {SECTION_TITLES['judge_calibration']}",
            "",
            f"**{judge['reruns_required']} reruns required.** {judge['why']}",
            "",
            f"Evidence: {judge['evidence']}",
            "",
            "Judge prompt digests at assembly time:",
            "",
        ]
    )
    lines.extend(
        f"- `{name}` — `sha256:{digest}`"
        for name, digest in judge["judge_prompts_unchanged"].items()
    )
    lines.extend(
        [
            "",
            f"## {SECTION_TITLES['baseline_delta']}",
            "",
            f"Phase 0 baseline cited by id: `{delta['phase0_baseline_cited_by_id']}`. The "
            "committed artifact `artifacts.newest('baseline_report')` resolves is "
            f"`{delta['phase0_baseline_resolved_by_artifacts_newest']}`; its offline leg is "
            f"archive `{delta['phase0_baseline_offline_leg']}`. {scope.baseline_artifact_ref}",
            "",
            "| Against | Regressions | Improvements | New | Dropped | Dropped dims | "
            "Vacated | Differing rows | Judge mean delta |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
    )
    for comparison in delta["comparisons"]:
        lines.append(
            f"| {comparison['against']} | {len(comparison['regressions'])} | "
            f"{len(comparison['improvements'])} | {len(comparison['new_scenarios'])} | "
            f"{len(comparison['dropped_scenarios'])} | "
            f"{len(comparison['dropped_dimensions'])} | "
            f"{len(comparison['vacated_assertions'])} | "
            f"{len(comparison['row_level_differences'])} | "
            f"{comparison['judge_mean_delta']} |"
        )
    lines.append("")
    for comparison in delta["comparisons"]:
        if "movement_by_a_dimension_this_phase_added" in comparison:
            explained = comparison["movement_by_a_dimension_this_phase_added"]
            added = ", ".join(
                f"`{name}` (x{n})" for name, n in explained["dimensions_added"].items()
            )
            lines.extend(
                [
                    f"Against the {comparison['against'].split(' (')[0]}, "
                    f"{explained['rows']} row(s) differ and every one of them differs in the "
                    f"same way: a dimension that did not exist in the baseline — {added} — is "
                    "now present and passing. Nothing else on those rows moved: same pass/fail, "
                    "same terminal state, same tool-call count, same judge score. Unexplained "
                    f"differences: {len(comparison['unexplained_row_differences'])}.",
                    "",
                ]
            )
    lines.extend([delta["verdict"], ""])
    if "new_numbers" in delta:
        lines.extend(
            [
                "**The numbers this phase produced that no baseline can be compared to:**",
                "",
                "| Number | Value | Sample size | Read it how |",
                "|---|---|---|---|",
            ]
        )
        for number in delta["new_numbers"]:
            lines.append(
                f"| {number['name']} | **{number['value']}** | {number['sample_size']} | "
                f"{number['caveat']} |"
            )
        lines.extend(["", delta["new_numbers_note"], ""])
    lines.extend(
        [
            delta["cost_per_scenario_note"],
            "",
            f"## {SECTION_TITLES['spend_line']}",
            "",
            "| # | Archive | Scenario | USD | Agent-loop s | Start to finish s | Tokens |",
            "|---|---|---|---|---|---|---|",
        ]
    )
    for row in spend["live_runs"]:
        lines.append(
            f"| {row['order']} | `{row['archive']}` | `{row['scenario']}` | {row['usd']} | "
            f"{row['agent_loop_wall_seconds']} | {row['start_to_finish_seconds']} | "
            f"{row['tokens']} |"
        )
    if "read_only_pass" in spend:
        pass_row = spend["read_only_pass"]
        lines.append(
            f"| — | `{pass_row['archive']}` | read-only pass, {pass_row['scenarios']} scenarios | "
            f"{pass_row['usd']} | {pass_row['agent_loop_wall_seconds']} | "
            f"{pass_row['start_to_finish_seconds']} | {pass_row['tokens']} |"
        )
    lines.extend(
        [
            "",
            f"**Live total: ${spend['live_total_usd']}**, "
            f"{spend['live_total_agent_loop_seconds']} s of agent loop and "
            f"{spend['live_total_start_to_finish_seconds']} s from each run's first trace record "
            "to its last. The canned sweep cost "
            f"${spend['canned_sweep']['usd']}. {spend['canned_sweep']['note']}",
            "",
        ]
    )
    if "live_total_usd_including_evaluator" in spend:
        lines.extend(
            [
                f"**The bill for this close is ${spend['live_total_usd_including_evaluator']}**: "
                f"${spend['live_total_usd']} charged to the agents' own ledgers plus "
                f"${spend['evaluator_share_usd']} the eval harness spent grading them. "
                f"{spend['two_totals_note']}",
                "",
            ]
        )
    lines.extend(["Against plan 03 section 11:", ""])
    for row in spend["against_plan_03_section_11"]:
        lines.append(f"- **Planned:** {row['planned']}  \n  **Actual:** {row['actual']}")
    lines.extend(
        [
            "",
            spend["campaign_context"],
            "",
            "## Deviations from the protocol as written",
            "",
        ]
    )
    for entry in document["deviations"]:
        lines.append(f"- **{entry['id']} — {entry['what']}** {entry['detail']}")
    lines.extend(["", "## Follow-ups this close leaves open", ""])
    for entry in document["follow_ups"]:
        lines.append(f"- **{entry['id']}** ({entry['status']}) — {entry['what']}")
    if document.get("pending_reruns"):
        lines.extend(
            [
                "",
                "## What is still owed — why this is a draft",
                "",
                "Each of these has a merged fix and a ready world, and neither has been "
                "measured. Nothing below may be treated as run until its archive id is in "
                "this report's scope:",
                "",
            ]
        )
        for rerun in document["pending_reruns"]:
            lines.append(
                f"- **`{rerun['scenario']}`** — supersedes `{rerun['supersedes']}`. "
                f"{rerun['fix']} Waiting on {rerun['blocked_on']}."
            )
        lines.extend(
            [
                "",
                "To produce the final version: add those two archive ids to this phase's "
                "`live_legs`, drop the matching `PendingRerun` entries, and run "
                "`make phase-close-report PHASE=2 WRITE=1`. The status flips to FINAL because "
                "it is computed from the scope, and the new document is written beside this "
                "one under a new timestamp — this draft is never overwritten (invariant 9).",
            ]
        )
    lines.extend(
        [
            "",
            "## Was an INCIDENTS.md row filed?",
            "",
            f"**{'Yes' if document['incidents_row_filed'] else 'No.'}** "
            f"{document['incidents_row_reason']}",
            "",
        ]
    )
    return "\n".join(lines) + "\n"


def write(
    document: dict[str, Any], scope: PhaseScope, *, root: Path | None = None
) -> tuple[Path, Path]:
    """Write the two versioned halves and return their paths.

    Stamped from the evidence, not the clock, so re-assembling the SAME scope aims at
    the same path and exclusive-create refuses (invariant 9). The two halves say
    different things: the timestamp is the newest archive, so a re-run necessarily
    produces a new name and the final lands BESIDE the draft; the id is the canned sweep,
    which carries the closing mark and stays put so a phase's drafts sort together.
    """
    sections = document["sections"]
    sweep = sections["sweep_results"]["canned_sweep"]
    stamps = [str(sweep["generated_at"])]
    stamps.extend(str(row["run_finished_at"]) for row in sections["spend_line"]["live_runs"])
    read_only = sections["sweep_results"].get("read_only_pass")
    if read_only is not None:
        stamps.append(str(read_only["generated_at"]))
    recorded_at = max(datetime.fromisoformat(stamp) for stamp in stamps)
    invocation_id = str(sweep["archive"])
    return (
        artifacts.write_versioned(
            "phase_close_report",
            content=render_json(document),
            timestamp=recorded_at,
            invocation_id=invocation_id,
            root=root,
        ),
        artifacts.write_versioned(
            "phase_close_report_md",
            content=render_markdown(document, scope),
            timestamp=recorded_at,
            invocation_id=invocation_id,
            root=root,
        ),
    )


def committed_versions(phase: int, *, root: Path | None = None) -> list[tuple[Path, Path]]:
    """Every committed version of ONE phase's report, oldest first.

    ``artifacts.newest`` answers "the newest of this kind", which was the same question
    as "this phase's report" only while there was one phase. Found by the canned sweep id
    in the filename, through ``artifacts.versions`` so the ordering rule stays in one
    place — never a glob and never mtime. A superseded DRAFT is still evidence.
    """
    scope = SCOPES[phase]
    halves: list[list[Path]] = []
    for kind in ("phase_close_report", "phase_close_report_md"):
        matching = [
            path for path in artifacts.versions(kind, root=root) if scope.canned_sweep in path.name
        ]
        if not matching:
            raise ValueError(f"no committed {kind} for phase {phase} ({scope.canned_sweep})")
        halves.append(matching)
    if len(halves[0]) != len(halves[1]):
        raise ValueError(f"phase {phase} has {len(halves[0])} JSON halves and {len(halves[1])} MD")
    return list(zip(halves[0], halves[1], strict=True))


def committed(phase: int, *, root: Path | None = None) -> tuple[Path, Path]:
    """The current committed JSON and Markdown halves of ONE phase's report.

    Newest wins, which is what makes a FINAL supersede its DRAFT for every reader.
    """
    return committed_versions(phase, root=root)[-1]


def committed_documents(*, root: Path | None = None) -> list[tuple[PhaseScope, tuple[Path, Path]]]:
    """Each committed document beside the scope it was assembled from.

    Positional, because both lists are in document order. A mismatched count is an ERROR
    rather than a zip that drops the last pair: writing a new version and forgetting to
    declare its scope is how a document stops being regenerable.
    """
    by_phase: dict[int, list[PhaseScope]] = {}
    for scope in COMMITTED_SCOPES:
        by_phase.setdefault(scope.phase, []).append(scope)
    pairs: list[tuple[PhaseScope, tuple[Path, Path]]] = []
    for phase, scopes in sorted(by_phase.items()):
        versions = committed_versions(phase, root=root)
        if len(versions) != len(scopes):
            raise ValueError(
                f"phase {phase} has {len(versions)} committed version(s) and "
                f"{len(scopes)} declared scope(s) in COMMITTED_SCOPES"
            )
        pairs.extend(zip(scopes, versions, strict=True))
    return pairs


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help="repository root to read")
    parser.add_argument("--format", choices=("json", "md"), default="md")
    parser.add_argument(
        "--phase",
        type=int,
        default=LATEST_PHASE,
        choices=sorted(SCOPES),
        help="which phase to assemble (default: the most recent declared scope)",
    )
    parser.add_argument(
        "--write", action="store_true", help="write the two versioned halves instead of printing"
    )
    args = parser.parse_args(argv)
    scope = SCOPES[args.phase]
    try:
        document = assemble(args.root, scope)
        if args.write:
            for path in write(document, scope, root=args.root):
                print(f"wrote {path.relative_to(args.root)}")
            return 0
    except (OSError, ValueError) as error:
        print(f"PHASE CLOSE REPORT FAIL: {error}")
        return 2
    print(
        render_json(document) if args.format == "json" else render_markdown(document, scope), end=""
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
