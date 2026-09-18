#!/usr/bin/env python3
"""Render eval trace JSONL files as human-readable text per scenario.

Each ``evals/traces/<scenario>.jsonl`` (or an archived ``--trace-dir`` slice) becomes a
versioned report under ``evals/reports/human/`` — never overwritten (invariant 9), named for
the RENDER, not the traced run; resolve the newest with ``artifacts.newest``. The tracer is
append-only (F-002), so one file holds every attempt: the newest renders, the earlier ones
are indexed above it (A-06). A bare invocation renders only what no report covers
(WO-R3-257); explicit requests always render.

Usage:
    uv run python scripts/format_traces.py                    # what is not yet rendered
    uv run python scripts/format_traces.py <scenario> ...     # some scenarios, always
    uv run python scripts/format_traces.py --force            # re-render everything
    uv run python scripts/format_traces.py --all              # every invocation
    uv run python scripts/format_traces.py --invocation <id>  # one invocation
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
# scripts/ lands on sys.path[0], not the repo root, so ``import evals`` fails when a human
# follows Usage (lint: tests/unit/test_make_script_import_path.py). ``evals.artifacts`` is
# stdlib-only and keeps the only copy of the naming rules.
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from evals import artifacts  # noqa: E402

_TRACE_DIR = _REPO_ROOT / "evals" / "traces"
_OUT_DIR = _REPO_ROOT / "evals" / "reports" / "human"

# Group key for records predating the invocation_id stamp. Must stay byte-identical to
# scripts/estimate_cost.PRE_INVOCATION_ID and evals/runner.py::_archive_trace_slice.
PRE_INVOCATION_ID: Final[str] = "pre-invocation-id"

# Literal mirror of ``llm.repair.MAX_OUTPUT_REPAIRS`` (this script stays stdlib-only);
# ``tests/unit/test_format_traces.py`` asserts the two are equal.
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
    # llm/client.py traces BEFORE parsing, so a billed-but-unparseable response arrives
    # with parse_failed=True and no "output" key. Render it, never crash (A-05).
    parse_failed = bool(r.get("parse_failed"))
    label = f"LLM CALL ({role})"
    # ADR 0035: `repair_of` marks the one bounded re-ask. Labelled so the pair reads as
    # ONE logical step, not as two planner calls in a loop that never happened.
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

    The error, the attempt ordinal, and whether the retry loop gave up.
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

    NOT MET means the fault was never seeded; UNVERIFIABLE means the platform never answered.
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

    ``null`` is the baseline's value — one candidate — and is rendered, not blank.
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

    ``call_id`` joins it to the ``llm`` record of the same call.
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
    # Billed candidate sets the schema refused before the accepted one (WP-5.2), rendered
    # only when there were any.
    if rejections := r.get("generation_rejections") or []:
        lines.append(f"Rejected:      {len(rejections)} billed set(s): {', '.join(rejections)}")
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


# The renderer's half of ``evals/tracing.py``'s ``TraceKind``, as plain strings to stay
# stdlib-only; ``test_format_traces.py::TestEveryKindRenders`` fails on a missing formatter.
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
# Billed-but-failed calls count as calls: a total that omits failures is a lower bound
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

    That order is the chronology (append-only, single-writer): the last group is newest.
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

    A killed run's truncated final line must not cost the whole report (as in
    ``evals/runner.py::_archive_trace_slice``).
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
            # Only a trace from a NEWER harness lands here (the coverage test bars the
            # rest). Dump rather than drop: an unrenderable record is still evidence.
            parts.append(f"STEP {step} — unknown kind={kind}: {json.dumps(r)}\n")
            continue
        parts.append(formatter(step, r))
    return parts


def format_trace(path: Path, *, invocation: str | None = None, render_all: bool = False) -> str:
    """Render one scenario's trace file — the newest invocation by default.

    ``invocation`` picks one group by id; ``render_all`` takes every group in order.
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

    Exclusive-create: raises rather than replacing a rendered report (invariant 9).
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


#: The header field ``_fmt_header`` writes per invocation, and the line
#: `evidence/build_human_index.py` reads — the filename's own id is the RENDER's.
_INVOCATION_FIELD: Final[str] = "Invocation:"


def rendered_invocations(path: Path) -> set[str]:
    """The invocation ids an existing human report renders.

    One ``Invocation:`` header per rendered group. An unreadable file gives the empty set,
    which re-renders — the safe direction.
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

    File order is the chronology, so read from the end: a sweep parses no more than it must.
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
        # Nothing parseable. main() skips it as empty anyway; say "yes" here so the two
        # never disagree about an unreadable trace.
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

    # An explicit request always renders; a bare sweep renders only what is not covered.
    # Naming scenarios counts as explicit, so one report can be forced.
    explicit = bool(args.scenarios or args.force or args.render_all or args.invocation)

    # One render session, one stamp and one id for every scenario in this pass, so its
    # reports sort together.
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
            # One malformed scenario must not cost every other scenario its report: a
            # single parse_failed record once aborted the loop at zero reports (A-05).
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
    # Always 0, even with failures: chained AFTER the paid runner in `make eval-live`,
    # so a derived-render failure must never repaint an already-billed run as red.
    return 0


if __name__ == "__main__":
    sys.exit(main())
