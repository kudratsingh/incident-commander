#!/usr/bin/env python3
"""Render eval trace JSONL files as human-readable text per scenario.

Each ``evals/traces/<scenario>.jsonl`` produces one
``evals/reports/human/<scenario>.txt`` where every LLM call, MCP tool call,
and scenario boundary is a numbered, labeled step. Written for eyeball
inspection of a full incident trajectory — the JSONL stays canonical.

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

Usage:
    uv run python scripts/format_traces.py                    # all scenarios
    uv run python scripts/format_traces.py <scenario> ...     # some scenarios
    uv run python scripts/format_traces.py --all              # every invocation
    uv run python scripts/format_traces.py --invocation <id>  # one invocation
    uv run python scripts/format_traces.py --trace-dir DIR --out-dir DIR
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final

_REPO_ROOT = Path(__file__).resolve().parents[1]
_TRACE_DIR = _REPO_ROOT / "evals" / "traces"
_OUT_DIR = _REPO_ROOT / "evals" / "reports" / "human"

# Group key for records written before the tracer stamped invocation_id.
# Must stay byte-identical to scripts/estimate_cost.PRE_INVOCATION_ID and to
# the missing-key convention in evals/runner.py::_archive_trace_slice — three
# tools disagreeing about what "no invocation id" means would split or merge
# attempts differently and quietly contradict each other.
PRE_INVOCATION_ID: Final[str] = "pre-invocation-id"


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
    llm_calls = sum(1 for r in records if r["kind"] == "llm")
    mcp_calls = sum(1 for r in records if r["kind"] in {"mcp", "mcp_error"})

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
        f"LLM calls:     {llm_calls}",
        f"Tool calls:    {mcp_calls}",
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
        if kind in {"scenario_start", "scenario_end"}:
            continue
        step += 1
        if kind == "llm":
            parts.append(_fmt_llm(step, r))
        elif kind == "mcp":
            parts.append(_fmt_mcp(step, r))
        elif kind == "mcp_error":
            parts.append(_fmt_mcp_error(step, r))
        elif kind == "chaos_setup":
            parts.append(_fmt_chaos_setup(step, r))
        else:
            parts.append(f"STEP {step} — unknown kind={kind}: {json.dumps(r)}\n")
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

    written = 0
    failed = 0
    for path in paths:
        if not path.exists() or path.stat().st_size == 0:
            print(f"skip {path.name} (missing or empty)")
            continue
        try:
            rendered = format_trace(path, invocation=args.invocation, render_all=args.render_all)
            out = out_dir / f"{path.stem}.txt"
            out.write_text(rendered)
        except Exception as exc:
            # One malformed scenario must not cost every other scenario its
            # report: before this guard, a single parse_failed record aborted
            # the whole loop and the invocation ended with zero reports (A-05).
            failed += 1
            print(f"ERROR {path.name}: {type(exc).__name__}: {exc}")
            continue
        print(f"wrote {_display(out)}")
        written += 1

    summary = f"\n{written} trajectory files under {_display(out_dir)}/"
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
