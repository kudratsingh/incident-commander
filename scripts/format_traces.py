#!/usr/bin/env python3
"""Render eval trace JSONL files as human-readable text per scenario.

Each ``evals/traces/<scenario>.jsonl`` produces one
``evals/reports/human/<scenario>.txt`` where every LLM call, MCP tool call,
and scenario boundary is a numbered, labeled step. Written for eyeball
inspection of a full incident trajectory — the JSONL stays canonical.

Usage:
    uv run python scripts/format_traces.py               # all scenarios
    uv run python scripts/format_traces.py <scenario>    # one scenario
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
_TRACE_DIR = _REPO_ROOT / "evals" / "traces"
_OUT_DIR = _REPO_ROOT / "evals" / "reports" / "human"


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


def _fmt_llm(step: int, r: dict[str, Any]) -> str:
    lines: list[str] = []
    role = r["role"]
    lines.append(_rule("="))
    lines.append(
        f"STEP {step} — LLM CALL ({role}) @ {_fmt_ts(r['timestamp'])} "
        f"({r['duration_seconds']:.2f}s)"
    )
    lines.append(_rule("="))
    lines.append("")

    usage = r["response"]["usage"]
    lines.append(f"Model: {r['request']['model']}")
    lines.append(
        f"Tokens: input={usage['input_tokens']} output={usage['output_tokens']} "
        f"cache_hit={usage['cache_read_input_tokens']} "
        f"cache_write={usage['cache_creation_input_tokens']}"
    )
    lines.append(f"Stop reason: {r['response']['stop_reason']}")
    lines.append("")

    lines.append("--- SYSTEM PROMPT ---")
    system_text = r["request"]["system"][0]["text"]
    lines.append(system_text.strip())
    lines.append("")

    lines.append("--- USER MESSAGE ---")
    lines.append(r["request"]["messages"][0]["content"].strip())
    lines.append("")

    output = r["output"]
    output_model = r.get("output_model", "?")
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


def _fmt_header(records: list[dict[str, Any]]) -> str:
    starts = [r for r in records if r["kind"] == "scenario_start"]
    ends = [r for r in records if r["kind"] == "scenario_end"]
    llm_calls = sum(1 for r in records if r["kind"] == "llm")
    mcp_calls = sum(1 for r in records if r["kind"] in {"mcp", "mcp_error"})

    start = starts[0] if starts else {}
    end = ends[0] if ends else {}

    scenario = start.get("scenario") or end.get("scenario", "?")
    lines = [
        _rule("#"),
        f"INCIDENT TRAJECTORY: {scenario}",
        _rule("#"),
        "",
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


def format_trace(path: Path) -> str:
    records = [json.loads(line) for line in path.read_text().strip().split("\n") if line]
    parts: list[str] = [_fmt_header(records)]

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
        else:
            parts.append(f"STEP {step} — unknown kind={kind}: {json.dumps(r)}\n")

    parts.append(_fmt_footer(records))
    return "\n".join(parts)


def main() -> int:
    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    if len(sys.argv) > 1:
        names = sys.argv[1:]
        paths = [_TRACE_DIR / f"{n}.jsonl" for n in names]
    else:
        paths = sorted(_TRACE_DIR.glob("*.jsonl"))

    written = 0
    for path in paths:
        if not path.exists() or path.stat().st_size == 0:
            print(f"skip {path.name} (missing or empty)")
            continue
        out = _OUT_DIR / f"{path.stem}.txt"
        out.write_text(format_trace(path))
        print(f"wrote {out.relative_to(_REPO_ROOT)}")
        written += 1
    print(f"\n{written} trajectory files under {_OUT_DIR.relative_to(_REPO_ROOT)}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
