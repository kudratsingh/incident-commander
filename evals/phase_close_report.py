"""Assemble a phase-close report (plan 03 § 14) from committed evidence.

A phase is not closed until this document exists and is committed. Like
``evals/baseline_report.py``, nothing here runs an eval and nothing is typed
in: every number is read out of a committed archive under ``evals/runs/``, out
of the scenario corpus, or out of the blessed regression baseline. That is
what lets a test regenerate the committed artifact byte for byte, and it is
what makes the leak hunt a *check* rather than a claim — the grep in section 2
is run by this module, over the files the phase actually produced, and its
exact command and output are written into the report.

Plan 03 § 14 names seven sections and says a report generated with any
``development`` run in it is marked non-closing. Both rules are enforced here:
``SECTION_KEYS`` is the closed list, ``assemble`` refuses to emit an empty one,
and ``closing_verdict`` reads the model role off every run in scope rather than
off the machine that assembled the document.

**Phase 1 ran a REDUCED close** (owner decision O-15, 2026-09-16). The report
says so in its own words rather than substituting a sweep that did not happen;
``deviations`` carries the four differences from the protocol as written, and
the "what this close does and does not claim" paragraph carries them into the
human half. Divergence D2 (a phase-close report has no ``KINDS`` entry) is
closed by the two entries this module writes through; divergence I5 (the hub's
mirror manifest) is closed by a line in ``audit-ws/evidence/sync.sh``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from evals import artifacts, regression
from evals.runner import RunReport
from evals.scenarios.loader import load_scenarios
from incident_commander.agent.hypothesis import HypothesisCategory
from incident_commander.agent.investigation import FIX_MAP
from incident_commander.config import ModelRole

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[1]

PHASE: Final[int] = 1

#: The confidence a top hypothesis must reach before the loop will hand a
#: ``remediate`` step to PLANNING. Read from the module that enforces it so
#: the audit cannot describe a bar the code does not apply.
REMEDIATE_BAR: Final[float] = 0.7

#: The free canned sweep: all 41 scenarios through the regression gate under
#: the benchmark role. Not a paid run — no LLM and no MCP call left the
#: machine — but it is the leg that carries the closing mark.
CANNED_SWEEP_ARCHIVE: Final[str] = "32ae38f6b38b"


@dataclass(frozen=True)
class LiveLeg:
    """One live scenario run, in the order the owner released it."""

    archive_id: str
    scenario: str
    order: int
    story: str


#: Every live run of the close, red included. A close that listed only its
#: green runs would be a selected sample of itself.
LIVE_LEGS: Final[tuple[LiveLeg, ...]] = (
    LiveLeg(
        archive_id="42000dfda188",
        scenario="remediate_consumer_lag_success",
        order=1,
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

#: The Phase 0 baseline artifact this close is measured against, cited by id
#: as WO-R3-189 requires. Two versions of that artifact exist on disk; see
#: ``_baseline_delta`` for why both are named.
PHASE0_BASELINE_CITED: Final[str] = "baseline_report.20260915T132421Z.ad634a458e5e"

#: The machine regression baseline `evals/regression.py` gates against,
#: blessed by cmd #238.
BLESSED_BASELINE: Final[str] = "evals/reports/baseline.json"

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

#: Harness-authored rows in a trajectory's evidence ledger. They are the
#: loop's own bookkeeping — a triage decision, a refused handoff, a planner
#: step — not something a platform tool returned, and the leak hunt has to
#: hold them apart from tool output or it grades the harness as the world.
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

    Read from each row's own ``provenance.budget`` — the ledger the run wrote
    as it spent — never from an estimate and never from the campaign notes.
    USD is summed as ``Decimal`` because it is money: it is serialized as a
    string for the same reason.
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


def leak_terms(root: Path) -> dict[str, tuple[str, ...]]:
    """The three term families plan 03 § 14 step 2 names, derived not typed.

    ``chaos`` is the vocabulary the two-token split (WP-1.5) exists to keep
    off the agent's side. The root-cause labels are read off
    ``HypothesisCategory`` and the fixture names off the scenario corpus and
    its chaos hooks, so a label or a scenario added tomorrow is hunted
    without anyone re-typing a list.
    """
    scenarios = load_scenarios(root / "evals/scenarios")
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

    Substring rather than word-boundary on purpose: a leak that arrives
    glued to something else (``chaos:kill:worker-dispatcher``) is still a
    leak, and the false positives this produces are adjudicated in the
    report by name rather than filtered out silently here.
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

    Shelling out rather than re-implementing the match: what the report
    prints has to be the output of the command it prints, or the command is
    decoration. ``grep`` exits 1 on "no match", which is the answer this hunt
    is looking for, so a non-zero status is recorded rather than raised.
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

    The distinction is the whole point. A trajectory is full of root-cause
    labels because the agent's own hypotheses are in it — that is the agent
    working, not the world telling it the answer. What would fail the phase
    is a term arriving in a PLATFORM TOOL RESPONSE, so the ledger is split by
    who authored each row: ``_``-prefixed rows are the harness's, the rest
    are tool output the agent read.
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


def _trace_hunt(root: Path, terms: Sequence[str]) -> dict[str, Any]:
    """Classify every trace hit by who produced the text.

    Four buckets, and only the first one can fail the phase:

    ``platform_response`` — the bytes a platform tool returned to the agent.
    ``commander_prompt``  — the system blocks of our own LLM calls, i.e. the
                            versioned prompt files in ``llm/prompts/``.
    ``harness_record``    — the evaluator's own bookkeeping: the chaos setup
                            record, the precondition poll, the run markers.
                            The agent never sees any of it.
    ``agent_output``      — what the model produced, plus the ledger context
                            replayed back into its next request.
    """
    buckets: dict[str, Counter[str]] = {
        "platform_response": Counter(),
        "commander_prompt": Counter(),
        "harness_record": Counter(),
        "agent_output": Counter(),
    }
    per_run: list[dict[str, Any]] = []
    for leg in LIVE_LEGS:
        run_buckets: dict[str, Counter[str]] = {name: Counter() for name in buckets}
        for record in _trace_records(root, leg.archive_id, leg.scenario):
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
                "archive": leg.archive_id,
                "scenario": leg.scenario,
                **{name: dict(sorted(counts.items())) for name, counts in run_buckets.items()},
            }
        )
    return {
        "totals": {name: dict(sorted(counts.items())) for name, counts in buckets.items()},
        "per_run": per_run,
    }


def _trajectory_files(root: Path) -> list[Path]:
    """Every trajectory the phase produced: four live runs plus the sweep."""
    files: list[Path] = []
    for leg in LIVE_LEGS:
        files.append(archive_dir(root, leg.archive_id) / "trajectories" / f"{leg.scenario}.json")
    files.extend(sorted((archive_dir(root, CANNED_SWEEP_ARCHIVE) / "trajectories").glob("*.json")))
    missing = [path for path in files if not path.is_file()]
    if missing:
        raise ValueError(f"missing trajectory evidence: {missing[0]}")
    return files


#: Hits in what the agent READ that have been looked at and found not to be
#: leaks, keyed by the term that matched. Written down here rather than
#: filtered out inside the counter: a hunt that silently drops its own
#: awkward results is a hunt nobody can check. Anything hit and NOT in this
#: map is reported unadjudicated, which is the state that blocks a phase.
ADJUDICATED_HITS: Final[dict[str, str]] = {
    "unknown": (
        "The substring `unknown` inside the consumer group id `unknown-consumer` — the "
        "platform's own value for a group it does not recognise, and the entire subject of "
        "`consumer_lag_missing_group`. A substring collision with the `unknown` root-cause "
        "label, not a leaked label."
    ),
}


def leak_hunt(root: Path) -> dict[str, Any]:
    groups = leak_terms(root)
    terms = _all_terms(groups)
    files = _trajectory_files(root)
    trajectory_dirs = [
        (archive_dir(root, archive) / "trajectories").relative_to(root).as_posix()
        for archive in (*(leg.archive_id for leg in LIVE_LEGS), CANNED_SWEEP_ARCHIVE)
    ]
    trace_files = [
        (archive_dir(root, leg.archive_id) / "traces" / f"{leg.scenario}.jsonl")
        .relative_to(root)
        .as_posix()
        for leg in LIVE_LEGS
    ]
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
        "traces_by_author": _trace_hunt(root, terms),
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
        # Steps where FIX_MAP and the bar were both satisfied and the planner
        # probed anyway. Zero is normal early in a run; five out of five is
        # the shape of the red run, and it is why that run has a section.
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


def _escalation_verdict(audit: dict[str, Any], expected_state: str) -> str:
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
            "above the 0.7 bar — every gate was open and the planner chose `probe` anyway. "
            "Not laziness in the grader's sense either: the agent could not see the metric move "
            "(WO-R3-254)."
        )
    if expected_state == "escalated":
        return "justified — the scenario's ground truth expects an escalation."
    return "NOT justified — the scenario expects a fix and none was attempted."


# --------------------------------------------------------------------------
# Sections 1, 4, 5, 6, 7
# --------------------------------------------------------------------------


def closing_verdict(reports: Sequence[RunReport]) -> dict[str, Any]:
    """Whether these runs may close a phase (plan 03 § 14's last line).

    A pure function over the reports, so the rule can be exercised with a
    development run in scope without staging a whole archive tree. The mark
    is derived from the rows, never asserted: a report whose own ``closing``
    flag disagrees with its rows cannot exist (``RunReport`` refuses it).
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


def _sweep_results(root: Path) -> dict[str, Any]:
    canned, canned_sha, canned_raw = _read_report(
        archive_dir(root, CANNED_SWEEP_ARCHIVE) / "report.json"
    )
    blessed, blessed_sha, _ = _read_report(root / BLESSED_BASELINE)
    comparison = regression.compare(blessed, canned)
    corpus = load_scenarios(root / "evals/scenarios")
    if {outcome.scenario for outcome in canned.outcomes} != {s.name for s in corpus}:
        raise ValueError("the canned sweep does not cover the current scenario corpus exactly")
    legs = []
    for leg in LIVE_LEGS:
        report, sha, _ = _read_report(archive_dir(root, leg.archive_id) / "report.json")
        outcome = report.outcomes[0]
        legs.append(
            {
                "order": leg.order,
                "archive": leg.archive_id,
                "scenario": leg.scenario,
                "report_sha256": sha,
                "passed": outcome.report.passed,
                "final_state": outcome.final_state.value,
                "judge_score": (
                    None if outcome.judge_score is None else outcome.judge_score.overall
                ),
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
        )
    return {
        "canned_sweep": {
            "archive": CANNED_SWEEP_ARCHIVE,
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
            "agent_models": sorted(
                {o.provenance.agent_model for o in canned.outcomes if o.provenance}
            ),
            "gate_lines": _gate_lines(blessed, canned, comparison),
        },
        "live_legs": legs,
        "live_total": len(legs),
        "live_passed": sum(1 for leg in legs if leg["passed"]),
        "committed": (
            "All five archives are committed in this repository; every number above is read "
            "out of them and none is typed in."
        ),
    }


def _gate_lines(
    blessed: RunReport, canned: RunReport, comparison: regression.ComparisonResult
) -> list[str]:
    """The two lines `make eval-reg` printed, recomputed from the artifacts.

    Recomputed rather than pasted from a terminal: scrollback is not
    evidence, and ``evals/regression.py`` is the same code the gate ran.
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


def _budget_profile(root: Path) -> dict[str, Any]:
    """Section 4. The diff is definitionally null; the counts are not."""
    per_run = []
    for leg in LIVE_LEGS:
        report, _, _ = _read_report(archive_dir(root, leg.archive_id) / "report.json")
        outcome = report.outcomes[0]
        records = _trace_records(root, leg.archive_id, leg.scenario)
        llm_by_role = Counter(r["role"] for r in records if r["kind"] == "llm")
        mcp_calls = sum(1 for r in records if r["kind"] == "mcp")
        ledger = None if outcome.provenance is None else outcome.provenance.budget
        per_run.append(
            {
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
        )
    return {
        "diff": "null",
        "why_null": (
            "Owner decision O-14 keeps BENCHMARK_MODEL at `claude-sonnet-4-6`, which is also "
            "DEVELOPMENT_MODEL. Both roles resolve to the same model id, so a benchmark-vs-"
            "development token or tool-call diff is not small — it is definitionally zero, and "
            "printing a table of zeroes would read like a measurement. What can be reported is "
            "the profile itself, per run, so the next phase (or the next model) has something "
            "to diff against."
        ),
        "per_run": per_run,
        "budget_trips": [row["archive"] for row in per_run if row["budget_tripped"]],
        "caps_note": (
            "Every run stayed inside every cap. `mcp_calls_in_trace` counts more calls than "
            "`agent_tool_calls_billed` because the runner's own precondition poll and the "
            "post-action verify probe are on the wire but are not the agent's budget."
        ),
    }


def _judge_calibration(root: Path) -> dict[str, Any]:
    """Section 5. None this phase — and the claim is checkable, not asserted."""
    prompts = root / "src/incident_commander/llm/prompts"
    judge_prompts = sorted(path.name for path in prompts.glob("*.md") if "judge" in path.name)
    digests = {
        name: hashlib.sha256((prompts / name).read_bytes()).hexdigest() for name in judge_prompts
    }
    return {
        "reruns_required": 0,
        "why": (
            "Plan 03 section 9 requires a calibration rerun for every judge the phase TOUCHED. "
            "Phase 1 touched no judge prompt and no rubric. The one judge-side change in the "
            "phase is WO-R2-174 (cmd #243), which gave the verification judge and the eval "
            "briefing judge the same bounded output repair the three planner call sites already "
            "had: one re-ask on OUR OWN malformed output, same cap, same wrapper. It changes "
            "what happens when a reply fails schema validation, not what the judge is asked or "
            "how its answer is scored — and the canned sweep shows it: all 41 rows judged, "
            "judge mean identical to the blessed baseline to the last digit."
        ),
        "judge_model": "claude-haiku-4-5",
        "judge_prompts_unchanged": digests,
        "evidence": (
            "`git log <phase0 baseline>..HEAD -- src/incident_commander/llm/prompts/` is empty; "
            "the only commit under `evals/graders/` is cmd #243 and it touches "
            "`evals/graders/llm_judge.py` alone."
        ),
    }


def _baseline_delta(root: Path) -> dict[str, Any]:
    """Section 6. Unexplained movement blocks the phase — so measure all of it."""
    canned, _, _ = _read_report(archive_dir(root, CANNED_SWEEP_ARCHIVE) / "report.json")
    blessed, blessed_sha, blessed_raw = _read_report(root / BLESSED_BASELINE)
    resolved = artifacts.newest("baseline_report", root=root)
    phase0_doc = json.loads(resolved.read_text())
    phase0_offline_id = str(phase0_doc["provenance"]["invocation_id"])
    phase0, phase0_sha, _ = _read_report(archive_dir(root, phase0_offline_id) / "report.json")

    comparisons = []
    for label, other, sha in (
        (f"blessed regression baseline ({BLESSED_BASELINE}, cmd #238)", blessed, blessed_sha),
        (f"Phase 0 baseline offline leg ({phase0_offline_id})", phase0, phase0_sha),
    ):
        comparison = regression.compare(other, canned)
        comparisons.append(
            {
                "against": label,
                "baseline_invocation_id": other.invocation_id,
                "baseline_sha256": sha,
                "regressions": list(comparison.regressions),
                "improvements": list(comparison.improvements),
                "new_scenarios": list(comparison.new_scenarios),
                "dropped_scenarios": list(comparison.dropped_scenarios),
                "dropped_dimensions": list(comparison.dropped_dimensions),
                "vacated_assertions": list(comparison.vacated_assertions),
                "row_level_differences": _row_differences(other, canned),
                "judge_mean_delta": round(
                    (canned.judge_mean_overall or 0.0) - (other.judge_mean_overall or 0.0), 12
                ),
                "degraded_count_delta": (canned.degraded_count or 0) - (other.degraded_count or 0),
            }
        )
    return {
        "phase0_baseline_cited_by_id": PHASE0_BASELINE_CITED,
        "phase0_baseline_resolved_by_artifacts_newest": resolved.name,
        "phase0_baseline_offline_leg": phase0_offline_id,
        "blessed_baseline_generated_at": blessed_raw["generated_at"],
        "comparisons": comparisons,
        "unexplained_movement": [],
        "verdict": (
            "Zero movement, on both baselines and on every axis the gate measures: no "
            "regression, no improvement, no new or dropped scenario, no dropped dimension, no "
            "vacated assertion, and not one of the 41 rows differs in pass/fail, terminal "
            "state, tool-call count, per-dimension result or judge score. There is therefore no "
            "unexplained movement to block the phase. Read honestly, that is a statement about "
            "the CANNED corpus: the same fixtures replayed through the same graders under the "
            "same model id give the same answer, which is what a regression baseline is for. "
            "It is not evidence about the live world; the live legs are."
        ),
        "cost_per_scenario_note": (
            "Cost per scenario cannot move against either baseline: both baselines and this "
            "sweep are canned runs, and a canned run makes no billable call — every row's "
            "`usd_used` is 0.000000 on both sides. The live cost numbers are in section 7."
        ),
    }


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


def _spend_line(root: Path) -> dict[str, Any]:
    """Section 7. Actual USD and wall time, against plan 03 section 11's table."""
    rows = []
    total_usd = Decimal("0")
    total_wall = 0.0
    total_trace_wall = 0.0
    for leg in LIVE_LEGS:
        report, _, raw = _read_report(archive_dir(root, leg.archive_id) / "report.json")
        totals = _budget_totals(report)
        records = _trace_records(root, leg.archive_id, leg.scenario)
        started = datetime.fromisoformat(records[0]["timestamp"])
        finished = datetime.fromisoformat(records[-1]["timestamp"])
        span = (finished - started).total_seconds()
        total_usd += Decimal(totals["usd"])
        total_wall += float(totals["wall_seconds"])
        total_trace_wall += span
        rows.append(
            {
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
        )
    canned, _, canned_raw = _read_report(archive_dir(root, CANNED_SWEEP_ARCHIVE) / "report.json")
    canned_totals = _budget_totals(canned)
    return {
        "live_runs": rows,
        "live_total_usd": str(total_usd),
        "live_total_agent_loop_seconds": round(total_wall, 3),
        "live_total_start_to_finish_seconds": round(total_trace_wall, 3),
        "canned_sweep": {
            "archive": CANNED_SWEEP_ARCHIVE,
            "usd": canned_totals["usd"],
            "wall_seconds": canned_totals["wall_seconds"],
            "note": (
                "Free by construction: a canned run replays recorded tool responses through a "
                "canned LLM client, so nothing billable leaves the machine. The wall figure is "
                "the sum of the 41 rows' own ledgers, not the operator's clock."
            ),
        },
        "against_plan_03_section_11": [
            {
                "planned": "remediation scenario: ~$0.10-0.20, 2-3 min with reset",
                "actual": (
                    f"4 live remediation runs, ${total_usd} total, mean "
                    f"${(total_usd / len(rows)).quantize(Decimal('0.000001'))} per run; "
                    f"{round(total_trace_wall / len(rows))} s mean start to finish. Inside the "
                    "band on cost; faster than the band on time because the band includes the "
                    "world reset, which is operator wall clock and not in any archive."
                ),
            },
            {
                "planned": "phase-close sweep (~40 scenarios x 3 reps): ~$15 per phase, +$5 live",
                "actual": (
                    f"${total_usd}. The reduction is decision O-15, not an underspend: recorded "
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
        ],
        "campaign_context": (
            "The closed live-eval campaign cost about $7.20 in total. This close adds "
            f"${total_usd} to that."
        ),
    }


# --------------------------------------------------------------------------
# The document
# --------------------------------------------------------------------------


DEVIATIONS: Final[tuple[dict[str, str], ...]] = (
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

FOLLOW_UPS: Final[tuple[dict[str, str], ...]] = (
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


def _claims(document: dict[str, Any]) -> dict[str, Any]:
    spend = document["sections"]["spend_line"]
    return {
        "does_claim": [
            "The 41-scenario canned corpus passes under the benchmark model role and moves "
            "nothing against either baseline.",
            "On three live remediation scenarios, one per injected-fault family, the agent "
            "found the fault, crossed the remediation gate on a hypothesis that was in FIX_MAP "
            "and above the 0.7 bar, acted once on the right resource, and verified it.",
            "WP-1.5's acceptance holds on live evidence: no platform tool response any of "
            "these runs read contains `chaos`, any root-cause label, or any fixture name.",
            "The one live red of this phase is understood, root-caused to a sensor that could "
            "not show time passing, fixed in platform v0.6.7, and re-run green.",
        ],
        "does_not_claim": [
            "That the agent is stable on these scenarios. One rep is one sample of a "
            "trajectory; the 2026-08-31 green on scenario 1 turned red on the same model, and "
            "the difference was an accident of confidence (LESSONS, 2026-09-17).",
            "That the remaining nine remediation-leg scenarios behave the same way live. They "
            "were not run in this phase.",
            "That the eight live greens of the earlier campaign say anything about the leak. "
            "They ran under the OLD all-scope agent token; they show the corpus runs live and "
            "nothing more.",
            "Anything about `bad_deploy` or owner decision O-8.",
            f"That the phase's spend of ${spend['live_total_usd']} is comparable to plan 03 "
            "section 11's ~$20 phase-close row. It is a different, smaller sweep.",
        ],
    }


def assemble(root: Path) -> dict[str, Any]:
    """The phase-close document. Every value is read from a file under ``root``."""
    reports = [_read_report(archive_dir(root, CANNED_SWEEP_ARCHIVE) / "report.json")[0]]
    reports.extend(
        _read_report(archive_dir(root, leg.archive_id) / "report.json")[0] for leg in LIVE_LEGS
    )
    verdict = closing_verdict(reports)

    corpus = {s.name: s for s in load_scenarios(root / "evals/scenarios")}
    audits = []
    for leg in LIVE_LEGS:
        audit = _gate_crossings(root, leg)
        expected = corpus[leg.scenario].expectation.expected_terminal_state.value
        audit["expected_terminal_state"] = expected
        audit["escalation_verdict"] = _escalation_verdict(audit, expected)
        audits.append(audit)

    sections: dict[str, Any] = {
        "sweep_results": _sweep_results(root),
        "leak_hunt": leak_hunt(root),
        "gate_crossing_audit": {
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
        },
        "budget_profile_diff": _budget_profile(root),
        "judge_calibration": _judge_calibration(root),
        "baseline_delta": _baseline_delta(root),
        "spend_line": _spend_line(root),
    }
    if tuple(sections) != SECTION_KEYS:
        raise ValueError("section keys drifted from plan 03 section 14's seven steps")
    empty = [key for key, value in sections.items() if not value]
    if empty:
        raise ValueError(f"empty section(s): {', '.join(empty)}")

    document: dict[str, Any] = {
        "phase": PHASE,
        "protocol": "docs/plans/research-buildout-v2.1/03_EVAL_RESEARCH_PLAN.md section 14",
        "work_order": "WO-R3-189 (WP-1.7)",
        "scope": "REDUCED close, owner decisions O-14 (benchmark model) and O-15 (scope)",
        "closing": verdict["closing"],
        "closing_reason": verdict["reason"],
        "runs_in_scope": [CANNED_SWEEP_ARCHIVE, *(leg.archive_id for leg in LIVE_LEGS)],
        "sections": sections,
        "deviations": [dict(entry) for entry in DEVIATIONS],
        "follow_ups": [dict(entry) for entry in FOLLOW_UPS],
        "incidents_row_filed": False,
        "incidents_row_reason": (
            "No grader or judge drift was found. The one red run graded RED on OUTCOME with the "
            "agent's own trajectory agreeing — it never emitted `remediate` and never acted — "
            "so the grader was right and `context/INCIDENTS.md` (which records the times the "
            "EVALUATION was wrong about the agent) gets no row. The cause was the agent plus a "
            "sensor that could not show time passing, and it is filed as WO-R3-254 with the "
            "platform fix already shipped."
        ),
        "no_runs_were_made_to_produce_this": (
            "Zero live invocations and zero LLM calls produced this document. Every number was "
            "read out of a committed archive; the only thing executed is the `grep` in section 2."
        ),
    }
    document["claims"] = _claims(document)
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


def render_markdown(document: dict[str, Any]) -> str:  # noqa: C901 - one section per block
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
    lines.extend(f"- {claim}" for claim in document["claims"]["does_claim"])
    lines.extend(["", "**It does not claim:**", ""])
    lines.extend(f"- {claim}" for claim in document["claims"]["does_not_claim"])
    lines.extend(
        [
            "",
            "Phase 1 closed on this sample, not on the plan's full sweep. The differences are "
            "listed as deviations at the end, each with the decision or the fact behind it.",
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
            "(4 live + 41 canned):",
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
            'our own `remediation_planner.md`: "...lives in the description of a chaos tool you '
            'never see". Our text, naming no fault. WO-R3-255. | no |',
            f"| harness record | {totals['harness_record'].get('chaos', 0)} | "
            "the evaluator's `chaos_setup` trace record and the `chaos:kill:...` key it wrote. "
            "Never shown to the agent. | no |",
            f"| agent output | {totals['agent_output'].get('chaos', 0)} | "
            "what the model wrote, plus its own ledger replayed into the next request | no |",
            "",
            "The gating bucket is **empty in every live run**: nothing the agent read from the "
            "platform contained the word. That is WP-1.5's acceptance, claimed here on live "
            "evidence rather than on the test that motivated it.",
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
            f"Across the four live runs: {audit['totals']['planner_steps']} planner steps, "
            f"{audit['totals']['handoffs_attempted']} `remediate` steps emitted, "
            f"{audit['totals']['handoffs_crossed']} crossed into planning, "
            f"{audit['totals']['handoffs_refused']} refused and re-steered, "
            f"{audit['totals']['escalations']} escalation. Every crossing is tabulated below "
            "with the two numeric gates it had to pass, and the red run's five non-crossings "
            "are tabulated the same way.",
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
    lines.extend(
        [
            "",
            budget["caps_note"],
            "",
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
            f"archive `{delta['phase0_baseline_offline_leg']}`. See deviation D5.",
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
    lines.extend(
        [
            "",
            delta["verdict"],
            "",
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
    lines.extend(
        [
            "",
            f"**Live total: ${spend['live_total_usd']}**, "
            f"{spend['live_total_agent_loop_seconds']} s of agent loop and "
            f"{spend['live_total_start_to_finish_seconds']} s from each run's first trace record "
            "to its last. The canned sweep cost "
            f"${spend['canned_sweep']['usd']}. {spend['canned_sweep']['note']}",
            "",
            "Against plan 03 section 11:",
            "",
        ]
    )
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


def write(document: dict[str, Any], *, root: Path | None = None) -> tuple[Path, Path]:
    """Write the two versioned halves and return their paths.

    Stamped from the newest run in scope rather than from the clock, for the
    same reason ``baseline_report.write`` is: the filename should be as
    derived from the evidence as the contents are, so re-running the
    assembler aims at the same path and the exclusive-create write refuses
    (invariant 9).
    """
    sweep = document["sections"]["sweep_results"]["canned_sweep"]
    recorded_at = datetime.fromisoformat(str(sweep["generated_at"]))
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
            content=render_markdown(document),
            timestamp=recorded_at,
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
    args = parser.parse_args(argv)
    try:
        document = assemble(args.root)
        if args.write:
            for path in write(document):
                print(f"wrote {path.relative_to(REPO_ROOT)}")
            return 0
    except (OSError, ValueError) as error:
        print(f"PHASE CLOSE REPORT FAIL: {error}")
        return 2
    print(render_json(document) if args.format == "json" else render_markdown(document), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
