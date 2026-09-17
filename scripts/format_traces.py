#!/usr/bin/env python3
"""Render eval trace JSONL files as human-readable text per scenario.

Each ``evals/traces/<scenario>.jsonl`` produces one
``evals/reports/human/<scenario>.<YYYYMMDDTHHMMSSZ>.<render_id>.txt`` where
every LLM call, MCP tool call, planner step and scenario boundary is a
numbered, labeled step. Written for eyeball inspection of a full incident
trajectory — the JSONL stays canonical.

Reports are VERSIONED and never overwritten (CLAUDE.md invariant 9). Each
run of this script is one render session with its own stamp and id, so a
second render lands beside the first instead of replacing it, and the whole
session's files sort together. The id names the RENDER, not the traced
invocation: which invocation was rendered is stated inside the file, and
``--all`` has no single one. Resolve the newest report for a scenario with
``evals.artifacts.newest("human", scenario)`` — never by mtime.

The tracer is APPEND-ONLY (``evals/tracing.py``, study/findings.md F-002):
a re-run of a scenario adds a fresh block of records stamped with a new
``invocation_id`` instead of erasing the earlier attempt. So one file is a
history of attempts, not one trajectory. This renderer therefore partitions
records by ``invocation_id`` in file order and renders the NEWEST attempt by
default, indexing the earlier ones above it. Rendering them as one run
attributed the oldest attempt's pass/fail and timestamps to the whole history
and inflated step counts (A-06).

An archived slice under ``evals/runs/<invocation_id>/traces/`` works the same
way — pass it with ``--trace-dir``. A run directory with no ``report.json``
was killed mid-suite (ADR 0017); its slices are still first-class evidence and
render normally.

**A run adds one file, not thirty-nine (WO-R3-257).** This script is chained
after the runner in ``make eval-live`` and ``make eval-smoke``, and it used to
re-render EVERY trace file it could find on every invocation. A live run of
one scenario therefore wrote 39 human reports: one of the run that had just
happened, and 38 fresh copies of trajectories nobody had re-run. That is how
``evals/reports/human/`` reached 765 files for 56 distinct runs — 93% repeats,
each one a byte-for-byte re-render of an already-rendered invocation, and each
one permanent, because invariant 9 forbids deleting any of them.

So a bare invocation is now INCREMENTAL. For each trace file it compares the
newest invocation in the trace against the invocations the scenario's newest
existing render already covers, and renders only where the two disagree. That
is both halves of the rule in one test: the scenario that just ran has an
invocation no render covers, and so does a scenario whose last render was
somehow missed or lost. Everything else is skipped and says so.

Naming scenarios, ``--invocation``, ``--all`` and ``--force`` are explicit
requests and always render — an operator asking for a render gets one.

Usage:
    uv run python scripts/format_traces.py                    # what is not yet rendered
    uv run python scripts/format_traces.py <scenario> ...     # some scenarios, always
    uv run python scripts/format_traces.py --force            # re-render everything
    uv run python scripts/format_traces.py --all              # every invocation
    uv run python scripts/format_traces.py --invocation <id>  # one invocation
    uv run python scripts/format_traces.py --trace-dir DIR --out-dir DIR
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

_REPO_ROOT = Path(__file__).resolve().parents[1]
# ``python scripts/format_traces.py`` puts scripts/ on sys.path[0], not the
# repo root, and ``evals`` is outside the installed package (src layout), so
# ``import evals`` would fail before line one runs. The Makefile recipes
# carry ``PYTHONPATH=.`` for this (the repo convention — see `fixture-drift`,
# and the lint in tests/unit/test_make_script_import_path.py). This bootstrap
# is the OTHER half: the Usage block above documents running this script
# directly, and a human who does that has no PYTHONPATH set.
#
# ``evals.artifacts`` is deliberately stdlib-only, so importing it keeps this
# script's "renders an archived slice from any checkout with only the stdlib"
# property intact. The naming and newest-wins rules must NOT be duplicated
# here: one resolver, one definition of "current" (three tools disagreeing
# about that is the same failure shape as PRE_INVOCATION_ID below).
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from evals import artifacts  # noqa: E402

_TRACE_DIR = _REPO_ROOT / "evals" / "traces"
_OUT_DIR = _REPO_ROOT / "evals" / "reports" / "human"

# Group key for records written before the tracer stamped invocation_id.
# Must stay byte-identical to scripts/estimate_cost.PRE_INVOCATION_ID and to
# the missing-key convention in evals/runner.py::_archive_trace_slice — three
# tools disagreeing about what "no invocation id" means would split or merge
# attempts differently and quietly contradict each other.
PRE_INVOCATION_ID: Final[str] = "pre-invocation-id"

# Mirror of ``incident_commander.llm.repair.MAX_OUTPUT_REPAIRS``, kept as a
# literal so this script stays importable with only the stdlib (see the
# comment above the ``evals.artifacts`` import). The copy is not allowed to
# drift: ``tests/unit/test_format_traces.py`` asserts the two are equal.
_MAX_OUTPUT_REPAIRS: Final[int] = 1


def _fmt_ts(ts: str) -> str:
    return ts.replace("T", " ").split("+")[0].split(".")[0]


def _rule(char: str = "=") -> str:
    return char * 78


def _fmt_hypothesis(h: dict[str, Any], idx: int) -> str:
    return f"    {idx}. {h['name']} (confidence {h['confidence']})\n       {h['reasoning']}"


def _fmt_next_action(action: dict[str, Any]) -> str:
    kind = action.get("kind", "?")
    if kind == "probe":
        args = json.dumps(action.get("arguments", {}))
        return f"PROBE {action['tool_name']}({args})"
    if kind == "stop":
        return f"STOP — {action.get('reason', '(no reason)')}"
    return f"{kind}({action})"


def _token_count(usage: dict[str, Any], key: str) -> str:
    """Render one usage field. Cache fields are Optional and arrive as null."""
    value = usage.get(key)
    return "?" if value is None else str(value)


def _fmt_llm(step: int, r: dict[str, Any]) -> str:
    lines: list[str] = []
    role = r["role"]
    # llm/client.py traces BEFORE parsing, so a billed response we could not
    # parse (max_tokens truncation, no record_output block) is written with
    # parse_failed=True and NO "output" key. Those are the records tracing
    # before the parse exists to preserve — render them, never crash (A-05).
    parse_failed = bool(r.get("parse_failed"))
    label = f"LLM CALL ({role})"
    # ADR 0035: a call carrying `repair_of` is the one bounded re-ask of the
    # call whose output did not parse. Labeling it says the pair is ONE
    # logical step — without it a reader counts two planner calls and reads a
    # loop that never happened.
    repair_of = r.get("repair_of")
    if repair_of:
        label += f" — REPAIR (1 of {_MAX_OUTPUT_REPAIRS})"
    if parse_failed:
        label += " — PARSE FAILED (billed, no parsed output)"
    lines.append(_rule("="))
    lines.append(
        f"STEP {step} — {label} @ {_fmt_ts(r['timestamp'])} ({r['duration_seconds']:.2f}s)"
    )
    lines.append(_rule("="))
    lines.append("")

    response = r.get("response") or {}
    usage = response.get("usage") or {}
    lines.append(f"Model: {r['request']['model']}")
    lines.append(
        f"Tokens: input={_token_count(usage, 'input_tokens')} "
        f"output={_token_count(usage, 'output_tokens')} "
        f"cache_hit={_token_count(usage, 'cache_read_input_tokens')} "
        f"cache_write={_token_count(usage, 'cache_creation_input_tokens')}"
    )
    lines.append(f"Stop reason: {response.get('stop_reason', '?')}")
    if repair_of:
        lines.append(f"Repair of trace record: {repair_of}")
    lines.append("")

    lines.append("--- SYSTEM PROMPT ---")
    system_text = r["request"]["system"][0]["text"]
    lines.append(system_text.strip())
    lines.append("")

    lines.append("--- USER MESSAGE ---")
    lines.append(r["request"]["messages"][0]["content"].strip())
    lines.append("")

    output = r.get("output")
    output_model = r.get("output_model", "?")
    if output is None:
        lines.append(f"--- RAW RESPONSE CONTENT (no parsed {output_model}) ---")
        lines.append(json.dumps(response.get("content", []), indent=2))
        lines.append("")
        return "\n".join(lines)

    lines.append(f"--- OUTPUT ({output_model}) ---")
    if output_model == "InvestigationStep":
        lines.append("Hypotheses (ranked):")
        for i, h in enumerate(output.get("hypotheses", []), start=1):
            lines.append(_fmt_hypothesis(h, i))
        lines.append("")
        lines.append(f"Next action: {_fmt_next_action(output.get('next_action', {}))}")
    elif output_model == "BriefingContent":
        lines.append("Findings:")
        lines.append(f"    {output.get('findings', '(none)')}")
        lines.append("")
        lines.append("Recommendation:")
        lines.append(f"    {output.get('recommendation', '(none)')}")
    elif output_model == "JudgeScore":
        lines.append(f"Groundedness:   {output.get('groundedness')}")
        lines.append(f"Actionability:  {output.get('actionability')}")
        lines.append("")
        lines.append("Reasoning:")
        lines.append(f"    {output.get('reasoning', '(none)')}")
    else:
        lines.append(json.dumps(output, indent=2))
    lines.append("")
    return "\n".join(lines)


def _fmt_mcp(step: int, r: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append(_rule("="))
    lines.append(
        f"STEP {step} — MCP TOOL CALL @ {_fmt_ts(r['timestamp'])} ({r['duration_seconds']:.3f}s)"
    )
    lines.append(_rule("="))
    lines.append("")
    lines.append(f"Tool: {r['tool_name']}")
    lines.append(f"Arguments: {json.dumps(r['arguments'])}")
    lines.append("")
    lines.append("--- RESULT ---")
    result = r.get("result", {})
    is_error = result.get("is_error", False)
    lines.append(f"is_error: {is_error}")
    content = result.get("content", [])
    for block in content:
        text = block.get("text", "")
        try:
            # Pretty-print the JSON tool payload when possible.
            pretty = json.dumps(json.loads(text), indent=2)
            lines.append(pretty)
        except (json.JSONDecodeError, TypeError):
            lines.append(text)
    lines.append("")
    return "\n".join(lines)


def _fmt_mcp_error(step: int, r: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append(_rule("="))
    lines.append(
        f"STEP {step} — MCP TOOL ERROR @ {_fmt_ts(r['timestamp'])} ({r['duration_seconds']:.3f}s)"
    )
    lines.append(_rule("="))
    lines.append("")
    lines.append(f"Tool: {r['tool_name']}")
    lines.append(f"Arguments: {json.dumps(r['arguments'])}")
    lines.append(f"Error: {r['error']}")
    lines.append("")
    return "\n".join(lines)


def _fmt_llm_error(step: int, r: dict[str, Any]) -> str:
    """A billed-or-attempted LLM call that never returned (``evals/tracing.py``).

    These are the records the tracer was extended to capture: an exhausted
    429, a dropped connection, a client-side refusal. They used to render as
    ``unknown kind`` — a raw JSON dump of the whole request — which put the
    full system prompt on one line in the middle of the trajectory and told
    the reader nothing about what failed. What matters here is the error, the
    attempt ordinal, and whether the retry loop gave up.
    """
    role = r.get("role", "?")
    attempt = r.get("attempt")
    terminal = bool(r.get("terminal"))
    outcome = "gave up" if terminal else "retried"
    ordinal = "?" if attempt is None else str(int(attempt) + 1)
    duration = r.get("duration_seconds")
    took = "" if duration is None else f" ({float(duration):.2f}s)"
    lines = [
        _rule("="),
        f"STEP {step} — LLM CALL FAILED ({role}) @ {_fmt_ts(r.get('timestamp', ''))}{took}",
        _rule("="),
        "",
        f"Model: {r.get('request', {}).get('model', '?')}",
        f"Attempt: {ordinal} — {outcome}",
        f"Expected output: {r.get('output_model', '?')}",
        "",
        "--- ERROR ---",
        str(r.get("error", "(no error recorded)")),
        "",
    ]
    return "\n".join(lines)


def _precondition_verdict(r: dict[str, Any]) -> str:
    """MET / NOT MET / UNVERIFIABLE — the distinction the record exists for.

    ``evals/runner.py`` raises two different failures here and the difference
    is the whole point of the pair: NOT MET says the fault was never
    manufactured (look at seeding), UNVERIFIABLE says the platform never
    answered the deciding attempt (look at the platform). A report that
    collapsed them would send the reader to the wrong half of the system.
    """
    if r.get("met"):
        return "MET"
    return "NOT MET" if r.get("answered") else "UNVERIFIABLE — no readable answer"


def _fmt_precondition(step: int, r: dict[str, Any]) -> str:
    """The world-state check that decides whether the premise ever existed."""
    lines = [
        _rule("="),
        f"STEP {step} — PRECONDITION {_precondition_verdict(r)} "
        f"@ {_fmt_ts(r.get('timestamp', ''))}",
        _rule("="),
        "",
        f"Tool: {r.get('tool', '?')}",
        f"Arguments: {json.dumps(r.get('arguments', {}), default=str)}",
        f"Answered: {r.get('answered')}  (any attempt answered: {r.get('ever_answered')})",
    ]
    failures = r.get("failures") or []
    if failures:
        lines.append("")
        lines.append("--- UNSATISFIED ---")
        lines.extend(f"  - {failure}" for failure in failures)
    lines.append("")
    return "\n".join(lines)


def _fmt_candidate(c: dict[str, Any], idx: int) -> str:
    probe = c.get("proposed_probe")
    tail = f" → probe {probe}" if probe else ""
    return (
        f"    {idx}. {c.get('name', '?')} "
        f"[{c.get('category', '?')}] confidence {c.get('confidence', '?')}{tail}"
    )


def _fmt_selector(selector: dict[str, Any] | None) -> list[str]:
    """The ``candidate_selector``'s decision, or the honest absence of one.

    ``null`` is the baseline's value and says something: one candidate was
    generated, so nothing was selected between. Rendering it as a blank would
    make the control group look like a strategy whose selector did not run.
    """
    if not selector:
        return ["Selector:      none (single-candidate step)"]
    scores = selector.get("scores") or {}
    return [
        f"Selector:      {selector.get('decision', '?')} → "
        f"{selector.get('selected_candidate_id', '?')}",
        f"  uncertainty: {selector.get('uncertainty')}",
        f"  scores:      {json.dumps(scores)}",
    ]


def _fmt_step_record(step: int, r: dict[str, Any]) -> str:
    """One planner step as the strategy recorded it (``StepRecord``, 02 § 7).

    This is the research record: the candidates the strategy considered, the
    one step it handed the loop, the ranking either side of it, and what the
    call was fed and billed. It sits beside the ``llm`` record of the same
    call — ``call_id`` joins them — and a reader comparing two strategies is
    reading this, not the prose.
    """
    candidates = r.get("candidate_set") or []
    calls = r.get("llm_calls") or []
    before = r.get("hypothesis_state_before") or []
    after = r.get("hypothesis_state_after") or []
    lines = [
        _rule("="),
        f"STEP {step} — PLANNER STEP (iteration {r.get('iteration', '?')}, "
        f"strategy {r.get('strategy', '?')}) @ {_fmt_ts(r.get('timestamp', ''))}",
        _rule("="),
        "",
        f"Model:         {r.get('model', '?')}",
        f"Run:           {r.get('run_id', '?')}  step_id={r.get('step_id', '?')}",
        f"Context:       planner_input_tokens={r.get('planner_input_tokens')} "
        f"chars={r.get('planner_context_chars')}",
    ]
    for call in calls:
        lines.append(
            f"LLM call:      {call.get('role', '?')} "
            f"tokens_used={call.get('tokens_used')} usd={call.get('usd_used')} "
            f"in={call.get('input_tokens')} out={call.get('output_tokens')} "
            f"cache_read={call.get('cache_read_tokens')} "
            f"cache_write={call.get('cache_creation_tokens')} "
            f"call_id={call.get('call_id') or '(untraced)'}"
        )
    lines.extend(_fmt_selector(r.get("selector")))
    lines.append("")
    lines.append(f"--- CANDIDATE SET ({len(candidates)}) ---")
    for i, candidate in enumerate(candidates, start=1):
        lines.append(_fmt_candidate(candidate, i))
    lines.append("")
    lines.append(f"Ranking: {len(before)} hypotheses before → {len(after)} after")
    emitted = r.get("emitted_step") or {}
    lines.append(f"Emitted: {_fmt_next_action(emitted.get('next_action', {}))}")
    lines.append("")
    return "\n".join(lines)


def _fmt_chaos_setup(step: int, r: dict[str, Any]) -> str:
    """Compact step for the seeding hook a live scenario fires before the agent runs."""
    lines = [
        _rule("="),
        f"STEP {step} — CHAOS SETUP @ {_fmt_ts(r['timestamp'])}",
        _rule("="),
        "",
        f"Hook: {r.get('hook', '?')}",
        f"Arguments: {json.dumps(r.get('arguments', {}), default=str)}",
        f"Result: {json.dumps(r.get('result', {}), default=str)}",
        "",
    ]
    return "\n".join(lines)


# The renderer's half of the tracer's ``TraceKind`` enumeration
# (``evals/tracing.py``). Spelled as plain strings rather than imported: this
# file is run as ``python scripts/format_traces.py`` from Makefile targets
# that do not put the repo root on sys.path, and it must render an archived
# slice from any checkout with only the stdlib. So the coupling is enforced
# instead of assumed — ``tests/unit/test_format_traces.py::TestEveryKindRenders``
# fails if ``TraceKind`` grows a member with no formatter here, which is what
# stops the next new kind from landing in the report as ``unknown kind``.
BOUNDARY_KINDS: Final[frozenset[str]] = frozenset({"scenario_start", "scenario_end"})
STEP_FORMATTERS: Final[dict[str, Callable[[int, dict[str, Any]], str]]] = {
    "llm": _fmt_llm,
    "llm_error": _fmt_llm_error,
    "mcp": _fmt_mcp,
    "mcp_error": _fmt_mcp_error,
    "precondition": _fmt_precondition,
    "chaos_setup": _fmt_chaos_setup,
    "step": _fmt_step_record,
}
# Billed-but-failed calls count as calls. A header that counted `mcp_error`
# as a tool call but dropped `llm_error` from the LLM total reported the
# run's LLM spend as lower than it was — the trace is the record of what a
# live run actually spent, and a record that omits failures is a lower bound
# presented as a total.
LLM_KINDS: Final[frozenset[str]] = frozenset({"llm", "llm_error"})
TOOL_KINDS: Final[frozenset[str]] = frozenset({"mcp", "mcp_error"})


def _count(records: list[dict[str, Any]], kinds: frozenset[str]) -> int:
    return sum(1 for r in records if r["kind"] in kinds)


def _with_failures(total: int, failed: int) -> str:
    """``7`` or ``7 (1 failed)`` — never a total that hides the failures."""
    return f"{total}" if not failed else f"{total} ({failed} failed)"


def _group_by_invocation(records: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Partition records into per-invocation groups, in first-seen file order.

    File order is the chronology: the tracer is append-only and
    single-writer, so the last group is the newest attempt. Sorting by
    ``invocation_started_at`` would be wrong — pre-invocation-id records do
    not carry it, and clock skew is not worth reasoning about.
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in records:
        key = str(r.get("invocation_id") or PRE_INVOCATION_ID)
        groups.setdefault(key, []).append(r)
    return groups


def _group_outcome(records: list[dict[str, Any]]) -> str:
    ends = [r for r in records if r["kind"] == "scenario_end"]
    if not ends:
        return "no scenario_end — killed mid-run"
    end = ends[0]
    if "error" in end:
        return f"ERRORED: {end['error']}"
    return f"passed={end.get('passed')} final_state={end.get('final_state')}"


def _fmt_prior_index(groups: dict[str, list[dict[str, Any]]], keys: Sequence[str]) -> str:
    """Index the earlier attempts this file also holds — never hide them."""
    lines = [
        _rule("#"),
        f"PRIOR INVOCATIONS IN THIS FILE ({len(keys)}) — append-only tracer, not rendered below",
        _rule("#"),
        "",
    ]
    for key in keys:
        records = groups[key]
        started = str(records[0].get("invocation_started_at") or "?")
        lines.append(
            f"  {key}  started={_fmt_ts(started)}  records={len(records)}  "
            f"{_group_outcome(records)}"
        )
    lines.append("")
    lines.append("  Render one with: --invocation <id>, or all of them with --all")
    lines.append("")
    return "\n".join(lines)


def _fmt_header(
    records: list[dict[str, Any]], *, invocation_id: str, position: str, skipped_lines: int
) -> str:
    starts = [r for r in records if r["kind"] == "scenario_start"]
    ends = [r for r in records if r["kind"] == "scenario_end"]
    llm_calls = _count(records, LLM_KINDS)
    llm_failed = _count(records, frozenset({"llm_error"}))
    mcp_calls = _count(records, TOOL_KINDS)
    mcp_failed = _count(records, frozenset({"mcp_error"}))

    # Correct now that the group is one invocation: exactly one
    # scenario_start, and at most one scenario_end.
    start = starts[0] if starts else {}
    end = ends[0] if ends else {}

    scenario = start.get("scenario") or end.get("scenario", "?")
    lines = [
        _rule("#"),
        f"INCIDENT TRAJECTORY: {scenario}",
        _rule("#"),
        "",
        f"Invocation:    {invocation_id} ({position})",
        f"Started:       {_fmt_ts(start.get('timestamp', ''))}",
        f"Finished:      {_fmt_ts(end.get('timestamp', ''))}",
        f"Live MCP:      {start.get('live_mcp')}",
        f"Live LLM:      {start.get('live_llm')}",
        f"Planner model: {start.get('model')}",
        f"Judge model:   {start.get('judge_model')}",
        "",
        f"LLM calls:     {_with_failures(llm_calls, llm_failed)}",
        f"Tool calls:    {_with_failures(mcp_calls, mcp_failed)}",
    ]
    if "final_state" in end:
        lines.append(f"Final state:   {end['final_state']}")
    if "tool_calls_used" in end:
        lines.append(f"Tool budget:   {end['tool_calls_used']} used")
    if "passed" in end:
        lines.append(f"Passed:        {end['passed']}")
    if "error" in end:
        lines.append(f"ERRORED:       {end['error']}")
    if skipped_lines:
        lines.append(f"Unparseable lines skipped: {skipped_lines}")
    lines.append("")
    return "\n".join(lines)


def _fmt_footer(records: list[dict[str, Any]]) -> str:
    ends = [r for r in records if r["kind"] == "scenario_end"]
    if not ends:
        return _rule("#") + "\nSCENARIO DID NOT REACH scenario_end (crashed mid-trace)\n"
    end = ends[0]
    lines = [
        _rule("#"),
        f"SCENARIO END @ {_fmt_ts(end.get('timestamp', ''))}",
        _rule("#"),
    ]
    for k, v in end.items():
        if k in {"kind", "timestamp"}:
            continue
        lines.append(f"  {k}: {v}")
    lines.append("")
    return "\n".join(lines)


def _read_records(path: Path) -> tuple[list[dict[str, Any]], int]:
    """Parse the JSONL, skipping and counting unreadable lines.

    A killed run leaves a truncated final line; a half-written record must
    not cost the operator the whole report. Same convention as
    ``evals/runner.py::_archive_trace_slice``. Records that DO parse are
    handed on unfiltered — a record missing ``kind`` is schema drift and
    still fails loudly, per file, in ``main``.
    """
    records: list[dict[str, Any]] = []
    skipped = 0
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            skipped += 1
            continue
        if not isinstance(record, dict):
            skipped += 1
            continue
        records.append(record)
    return records, skipped


def _fmt_steps(records: list[dict[str, Any]]) -> list[str]:
    parts: list[str] = []
    step = 0
    for r in records:
        kind = r["kind"]
        if kind in BOUNDARY_KINDS:
            continue
        step += 1
        formatter = STEP_FORMATTERS.get(kind)
        if formatter is None:
            # Reached only by a trace from a NEWER harness than this
            # checkout. Dump rather than drop — an unrenderable record is
            # still evidence — but the coverage test above means no kind
            # this harness writes can land here.
            parts.append(f"STEP {step} — unknown kind={kind}: {json.dumps(r)}\n")
            continue
        parts.append(formatter(step, r))
    return parts


def format_trace(path: Path, *, invocation: str | None = None, render_all: bool = False) -> str:
    """Render one scenario's trace file.

    Renders the newest invocation by default. ``invocation`` picks one group
    by id; ``render_all`` renders every group in file order.
    """
    records, skipped = _read_records(path)
    groups = _group_by_invocation(records)
    if not groups:
        return "\n".join(
            [
                _fmt_header([], invocation_id="?", position="0 of 0", skipped_lines=skipped),
                _fmt_footer([]),
            ]
        )

    keys = list(groups)
    if render_all:
        selected = keys
    elif invocation is not None:
        if invocation not in groups:
            raise KeyError(
                f"no records for invocation {invocation!r} in {path.name} "
                f"(present: {', '.join(keys)})"
            )
        selected = [invocation]
    else:
        selected = [keys[-1]]

    parts: list[str] = []
    for n, key in enumerate(selected):
        if n:
            parts.append(_rule("~"))
        index = keys.index(key)
        if not render_all and index > 0:
            parts.append(_fmt_prior_index(groups, keys[:index]))
        group = groups[key]
        parts.append(
            _fmt_header(
                group,
                invocation_id=key,
                position=f"{index + 1} of {len(keys)}",
                # The skip count belongs to the file, so report it once, on
                # whichever group is rendered first.
                skipped_lines=skipped if n == 0 else 0,
            )
        )
        parts.extend(_fmt_steps(group))
        parts.append(_fmt_footer(group))
    return "\n".join(parts)


def render_to(
    path: Path,
    out_dir: Path,
    *,
    timestamp: datetime,
    render_id: str,
    invocation: str | None = None,
    render_all: bool = False,
) -> Path:
    """Render one trace file to a versioned report and return the path written.

    Exclusive-create, via ``artifacts.write_versioned``: a report that
    already exists at this path was produced by this same render session for
    this same scenario, which cannot happen twice in one pass — so it raises
    rather than replacing a rendered report.
    """
    rendered = format_trace(path, invocation=invocation, render_all=render_all)
    return artifacts.write_versioned(
        "human",
        path.stem,
        content=rendered,
        timestamp=timestamp,
        invocation_id=render_id,
        directory=out_dir,
    )


#: The header field ``_fmt_header`` writes for every invocation it renders.
#: Reading it back is how an existing report answers "which runs are in you?"
#: — the same question `evidence/build_human_index.py` asks of the same line
#: in the hub. The alternative, putting the traced invocation in the
#: FILENAME, was rejected: the id in a report's name is the RENDER's
#: (``--all`` has no single traced one), and changing that would re-point
#: every existing file's meaning.
_INVOCATION_FIELD: Final[str] = "Invocation:"


def rendered_invocations(path: Path) -> set[str]:
    """The invocation ids an existing human report renders.

    Every rendered group writes one ``Invocation:`` header, so a plain scan
    of the file answers this for a one-group render and an ``--all`` render
    alike. Unreadable file → the empty set, which reads as "covers nothing"
    and re-renders. Failing towards a second render is the safe direction:
    the cost is one file, and the alternative is a run with no readable
    trajectory.
    """
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return set()
    found: set[str] = set()
    for line in text.splitlines():
        if not line.startswith(_INVOCATION_FIELD):
            continue
        value = line[len(_INVOCATION_FIELD) :].split()
        if value:
            found.add(value[0])
    return found


def newest_invocation(path: Path) -> str | None:
    """The invocation id of the last attempt in a trace file, or ``None``.

    The tracer is append-only and single-writer, so file order is the
    chronology and the LAST record belongs to the newest attempt — the same
    fact ``_group_by_invocation`` relies on to pick what to render. Read
    from the end so a 39-scenario sweep does not parse every record of every
    file to decide it has nothing to do; a killed run's truncated final line
    is skipped exactly as ``_read_records`` skips it.
    """
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            return str(record.get("invocation_id") or PRE_INVOCATION_ID)
    return None


def needs_render(path: Path, out_dir: Path) -> bool:
    """Whether this trace file holds an attempt no existing report covers."""
    latest = newest_invocation(path)
    if latest is None:
        # Nothing parseable in the file. main() still skips it as empty; say
        # "yes" here so the two never disagree about an unreadable trace.
        return True
    existing = artifacts.newest_or_none("human", path.stem, directory=out_dir)
    if existing is None:
        return True
    return latest not in rendered_invocations(existing)


def _display(path: Path) -> str:
    try:
        return str(path.relative_to(_REPO_ROOT))
    except ValueError:
        return str(path)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, add_help=True)
    parser.add_argument(
        "scenarios",
        nargs="*",
        help="scenario names to render; renders every trace file when omitted",
    )
    parser.add_argument(
        "--trace-dir",
        type=Path,
        default=_TRACE_DIR,
        help="directory of <scenario>.jsonl trace files "
        "(also accepts evals/runs/<invocation_id>/traces)",
    )
    parser.add_argument("--out-dir", type=Path, default=_OUT_DIR, help="where to write the .txt")
    parser.add_argument(
        "--invocation", default=None, help="render this invocation_id instead of the newest"
    )
    parser.add_argument(
        "--all",
        dest="render_all",
        action="store_true",
        help="render every invocation in the file, oldest first",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="re-render every scenario, including ones whose newest attempt is already rendered",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    trace_dir: Path = args.trace_dir
    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.scenarios:
        paths = [trace_dir / f"{name}.jsonl" for name in args.scenarios]
    else:
        paths = sorted(trace_dir.glob("*.jsonl"))

    # An explicit request always renders; a bare sweep renders only what is
    # not already covered (see the module docstring). Naming scenarios counts
    # as explicit, which is what lets the runner or an operator force one
    # scenario's report without arguing with the skip rule.
    explicit = bool(args.scenarios or args.force or args.render_all or args.invocation)

    # One render session, one stamp and one id, shared by every scenario in
    # this pass: the session's reports sort together and are distinguishable
    # from every earlier session's without reading a single file.
    session_at = datetime.now(UTC)
    render_id = uuid.uuid4().hex[:12]

    written = 0
    failed = 0
    skipped = 0
    for path in paths:
        if not path.exists() or path.stat().st_size == 0:
            print(f"skip {path.name} (missing or empty)")
            continue
        if not explicit and not needs_render(path, out_dir):
            skipped += 1
            continue
        try:
            out = render_to(
                path,
                out_dir,
                timestamp=session_at,
                render_id=render_id,
                invocation=args.invocation,
                render_all=args.render_all,
            )
        except Exception as exc:
            # One malformed scenario must not cost every other scenario its
            # report: before this guard, a single parse_failed record aborted
            # the whole loop and the invocation ended with zero reports (A-05).
            failed += 1
            print(f"ERROR {path.name}: {type(exc).__name__}: {exc}")
            continue
        print(f"wrote {_display(out)}")
        written += 1

    summary = f"\n{written} trajectory files under {_display(out_dir)}/<scenario>/"
    if skipped:
        summary += (
            f" ({skipped} scenario(s) skipped — newest attempt already rendered; "
            "--force re-renders them)"
        )
    if failed:
        summary += f" ({failed} file(s) failed to render — see the ERROR lines above)"
    print(summary)
    # Always 0, even with failures. This script is chained AFTER the paid
    # runner in `make eval-live` and `make eval-smoke`; the JSONL is the
    # canonical record and a derived-render failure must never repaint a
    # successful, already-billed run as a red make target.
    return 0


if __name__ == "__main__":
    sys.exit(main())
