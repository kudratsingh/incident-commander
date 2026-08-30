"""The human trace renderer must honor the append-only tracer (WO-C4-03).

``JsonlTracer`` stopped truncating on 2026-08-07 (study/findings.md F-002):
a re-run APPENDS a fresh ``invocation_id``-stamped block instead of erasing
the previous attempt's billed records. ``scripts/format_traces.py`` was never
updated for that, so it

* rendered a mixed-vintage file as one trajectory, attributing the OLDEST
  attempt's pass/fail and timestamps to the whole history and inflating step
  counts across invocations (A-06), and
* read ``r["output"]`` unconditionally, which ``KeyError``s on the
  ``parse_failed=True`` records ``LLMClient`` writes for billed-but-
  unparseable responses (A-05) — and the crash escaped the un-guarded
  per-file loop in ``main``, so one bad scenario left the whole invocation
  with zero human reports after the paid runner had already exited.

Every fixture here is synthetic JSONL under ``tmp_path``. No runner executes
and nothing is written under ``evals/`` (ADR 0011 freeze; study/findings.md
F-003).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from scripts.estimate_cost import PRE_INVOCATION_ID
from scripts.format_traces import BOUNDARY_KINDS, STEP_FORMATTERS, format_trace, main

_MODEL = "claude-sonnet-4-6"


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    return path


def _stamp(record: dict[str, Any], invocation: str, when: str) -> dict[str, Any]:
    """Add the three fields ``JsonlTracer.write`` stamps on every record."""
    return {
        **record,
        "timestamp": when,
        "invocation_id": invocation,
        "invocation_started_at": when,
    }


def _scenario_start(
    invocation: str, when: str, scenario: str = "redis_saturation"
) -> dict[str, Any]:
    return _stamp(
        {
            "kind": "scenario_start",
            "scenario": scenario,
            "live_mcp": True,
            "live_llm": True,
            "model": _MODEL,
            "judge_model": "claude-haiku-4-5",
        },
        invocation,
        when,
    )


def _scenario_end(
    invocation: str,
    when: str,
    *,
    passed: bool,
    final_state: str,
    scenario: str = "redis_saturation",
) -> dict[str, Any]:
    return _stamp(
        {
            "kind": "scenario_end",
            "scenario": scenario,
            "final_state": final_state,
            "tool_calls_used": 3,
            "passed": passed,
            "failure_class": "none",
        },
        invocation,
        when,
    )


def _llm(
    invocation: str, when: str, *, hypothesis: str = "redis memory pressure"
) -> dict[str, Any]:
    """A normal, parsed planner call."""
    return _stamp(
        {
            "kind": "llm",
            "role": "planner",
            "request": {
                "model": _MODEL,
                "system": [{"type": "text", "text": "You are an SRE."}],
                "messages": [{"role": "user", "content": "Investigate the alert."}],
            },
            "response": {
                "stop_reason": "tool_use",
                "usage": {
                    "input_tokens": 1200,
                    "output_tokens": 300,
                    "cache_read_input_tokens": 900,
                    "cache_creation_input_tokens": 0,
                },
                "content": [{"type": "tool_use", "name": "record_output", "input": {}}],
            },
            "output_model": "InvestigationStep",
            "duration_seconds": 2.5,
            "output": {
                "hypotheses": [
                    {"name": hypothesis, "confidence": 0.8, "reasoning": "evictions climbing"}
                ],
                "next_action": {"kind": "stop", "reason": "enough evidence"},
            },
        },
        invocation,
        when,
    )


def _llm_parse_failed(invocation: str, when: str) -> dict[str, Any]:
    """Shaped exactly like ``dict(trace, parse_failed=True)`` in llm/client.py.

    The billed response is there; there is no ``output`` key, and the cache
    usage fields come back JSON-null the way the SDK serializes them when the
    call never reached the cache.
    """
    return _stamp(
        {
            "kind": "llm",
            "role": "planner",
            "request": {
                "model": _MODEL,
                "system": [{"type": "text", "text": "You are an SRE."}],
                "messages": [{"role": "user", "content": "Investigate the alert."}],
            },
            "response": {
                "stop_reason": "max_tokens",
                "usage": {
                    "input_tokens": 1200,
                    "output_tokens": 4096,
                    "cache_read_input_tokens": None,
                    "cache_creation_input_tokens": None,
                },
                "content": [{"type": "text", "text": "I will start by checking the"}],
            },
            "output_model": "InvestigationStep",
            "duration_seconds": 31.2,
            "parse_failed": True,
        },
        invocation,
        when,
    )


def _llm_steps(text: str) -> list[str]:
    return re.findall(r"^STEP \d+ — LLM CALL.*$", text, flags=re.MULTILINE)


# --- A-05: parse_failed records ------------------------------------------


def test_parse_failed_record_renders(tmp_path: Path) -> None:
    """A billed-but-unparseable call renders as a PARSE FAILED step, not a crash."""
    path = _write_jsonl(
        tmp_path / "redis_saturation.jsonl",
        [
            _scenario_start("inv1", "2026-08-01T10:00:00+00:00"),
            _llm_parse_failed("inv1", "2026-08-01T10:00:05+00:00"),
            _scenario_end("inv1", "2026-08-01T10:01:00+00:00", passed=False, final_state="failed"),
        ],
    )

    text = format_trace(path)

    assert "PARSE FAILED" in text
    # The raw content blocks are the whole reason the client traces before
    # parsing — they must reach the human report.
    assert "I will start by checking the" in text
    # Null cache usage must not blow up the token line either.
    assert "input=1200" in text
    assert "output=4096" in text


def test_parse_failed_step_reports_the_billing_facts(tmp_path: Path) -> None:
    """Model, stop reason and prompts still render for the failed parse."""
    path = _write_jsonl(
        tmp_path / "redis_saturation.jsonl",
        [_llm_parse_failed("inv1", "2026-08-01T10:00:05+00:00")],
    )

    text = format_trace(path)

    assert f"Model: {_MODEL}" in text
    assert "Stop reason: max_tokens" in text
    assert "You are an SRE." in text
    assert "Investigate the alert." in text


# --- A-06: per-invocation rendering ---------------------------------------


def test_mixed_vintage_file_renders_newest_invocation(tmp_path: Path) -> None:
    """Header/footer come from the LAST invocation in file order, not the first."""
    path = _write_jsonl(
        tmp_path / "redis_saturation.jsonl",
        [
            # Invocation A: one call, failed.
            _scenario_start("aaaaaaaaaaaa", "2026-08-01T10:00:00+00:00"),
            _llm("aaaaaaaaaaaa", "2026-08-01T10:00:05+00:00"),
            _scenario_end(
                "aaaaaaaaaaaa", "2026-08-01T10:01:00+00:00", passed=False, final_state="escalated"
            ),
            # Invocation B: two calls, passed.
            _scenario_start("bbbbbbbbbbbb", "2026-08-02T09:00:00+00:00"),
            _llm("bbbbbbbbbbbb", "2026-08-02T09:00:05+00:00"),
            _llm("bbbbbbbbbbbb", "2026-08-02T09:00:20+00:00"),
            _scenario_end(
                "bbbbbbbbbbbb", "2026-08-02T09:02:00+00:00", passed=True, final_state="resolved"
            ),
        ],
    )

    text = format_trace(path)

    assert "Passed:        True" in text
    assert "Passed:        False" not in text
    assert "Final state:   resolved" in text
    assert "Final state:   escalated" not in text
    assert "Started:       2026-08-02 09:00:00" in text
    assert "Finished:      2026-08-02 09:02:00" in text
    # Exactly B's two calls are numbered — not all three in the file.
    assert len(_llm_steps(text)) == 2
    assert "LLM calls:     2" in text
    # A is not erased from the report: it is indexed as a prior attempt.
    assert "PRIOR INVOCATIONS" in text
    assert "aaaaaaaaaaaa" in text


def test_prior_invocation_index_names_a_killed_attempt(tmp_path: Path) -> None:
    """An earlier group with no scenario_end is reported as killed mid-run."""
    path = _write_jsonl(
        tmp_path / "redis_saturation.jsonl",
        [
            _scenario_start("aaaaaaaaaaaa", "2026-08-01T10:00:00+00:00"),
            _llm("aaaaaaaaaaaa", "2026-08-01T10:00:05+00:00"),
            # No scenario_end: Ctrl-C.
            _scenario_start("bbbbbbbbbbbb", "2026-08-02T09:00:00+00:00"),
            _llm("bbbbbbbbbbbb", "2026-08-02T09:00:05+00:00"),
            _scenario_end(
                "bbbbbbbbbbbb", "2026-08-02T09:02:00+00:00", passed=True, final_state="resolved"
            ),
        ],
    )

    text = format_trace(path)

    index = text.split("PRIOR INVOCATIONS", 1)[1].split("INCIDENT TRAJECTORY", 1)[0]
    assert "aaaaaaaaaaaa" in index
    assert "killed mid-run" in index


def test_single_invocation_file_has_no_prior_index(tmp_path: Path) -> None:
    path = _write_jsonl(
        tmp_path / "redis_saturation.jsonl",
        [
            _scenario_start("only", "2026-08-01T10:00:00+00:00"),
            _llm("only", "2026-08-01T10:00:05+00:00"),
            _scenario_end("only", "2026-08-01T10:01:00+00:00", passed=True, final_state="resolved"),
        ],
    )

    assert "PRIOR INVOCATIONS" not in format_trace(path)


def test_render_all_shows_every_invocation(tmp_path: Path) -> None:
    path = _write_jsonl(
        tmp_path / "redis_saturation.jsonl",
        [
            _scenario_start("aaaaaaaaaaaa", "2026-08-01T10:00:00+00:00"),
            _llm("aaaaaaaaaaaa", "2026-08-01T10:00:05+00:00", hypothesis="oldest guess"),
            _scenario_end(
                "aaaaaaaaaaaa", "2026-08-01T10:01:00+00:00", passed=False, final_state="escalated"
            ),
            _scenario_start("bbbbbbbbbbbb", "2026-08-02T09:00:00+00:00"),
            _llm("bbbbbbbbbbbb", "2026-08-02T09:00:05+00:00", hypothesis="newest guess"),
            _scenario_end(
                "bbbbbbbbbbbb", "2026-08-02T09:02:00+00:00", passed=True, final_state="resolved"
            ),
        ],
    )

    text = format_trace(path, render_all=True)

    assert "oldest guess" in text
    assert "newest guess" in text
    # Step numbering restarts per invocation rather than running 1..2.
    assert [int(n) for n in re.findall(r"^STEP (\d+) — LLM CALL", text, flags=re.MULTILINE)] == [
        1,
        1,
    ]


def test_invocation_flag_selects_an_older_group(tmp_path: Path) -> None:
    path = _write_jsonl(
        tmp_path / "redis_saturation.jsonl",
        [
            _scenario_start("aaaaaaaaaaaa", "2026-08-01T10:00:00+00:00"),
            _llm("aaaaaaaaaaaa", "2026-08-01T10:00:05+00:00", hypothesis="oldest guess"),
            _scenario_end(
                "aaaaaaaaaaaa", "2026-08-01T10:01:00+00:00", passed=False, final_state="escalated"
            ),
            _scenario_start("bbbbbbbbbbbb", "2026-08-02T09:00:00+00:00"),
            _llm("bbbbbbbbbbbb", "2026-08-02T09:00:05+00:00", hypothesis="newest guess"),
            _scenario_end(
                "bbbbbbbbbbbb", "2026-08-02T09:02:00+00:00", passed=True, final_state="resolved"
            ),
        ],
    )

    text = format_trace(path, invocation="aaaaaaaaaaaa")

    assert "oldest guess" in text
    assert "newest guess" not in text
    assert "Passed:        False" in text


def test_records_without_invocation_id_group_under_the_shared_literal(tmp_path: Path) -> None:
    """Pre-invocation-id vintage records keep estimate_cost's group key."""
    path = _write_jsonl(
        tmp_path / "redis_saturation.jsonl",
        [
            {
                "kind": "scenario_start",
                "scenario": "redis_saturation",
                "timestamp": "2026-07-01T00:00:00+00:00",
            },
            {
                "kind": "llm",
                "role": "planner",
                "timestamp": "2026-07-01T00:00:01+00:00",
                "duration_seconds": 1.0,
                "request": {
                    "model": _MODEL,
                    "system": [{"text": "s"}],
                    "messages": [{"role": "user", "content": "u"}],
                },
                "response": {},
                "output_model": "?",
            },
            _scenario_start("bbbbbbbbbbbb", "2026-08-02T09:00:00+00:00"),
            _llm("bbbbbbbbbbbb", "2026-08-02T09:00:05+00:00"),
            _scenario_end(
                "bbbbbbbbbbbb", "2026-08-02T09:02:00+00:00", passed=True, final_state="resolved"
            ),
        ],
    )

    text = format_trace(path)

    assert PRE_INVOCATION_ID in text
    assert len(_llm_steps(text)) == 1


# --- Robustness of the reader --------------------------------------------


def test_unparseable_lines_are_skipped_and_counted(tmp_path: Path) -> None:
    """A truncated final line from a killed run must not sink the render."""
    path = tmp_path / "redis_saturation.jsonl"
    path.write_text(
        json.dumps(_scenario_start("inv1", "2026-08-01T10:00:00+00:00"))
        + "\n"
        + json.dumps(_llm("inv1", "2026-08-01T10:00:05+00:00"))
        + "\n"
        + '{"kind": "llm", "role": "plan'  # killed mid-write
    )

    text = format_trace(path)

    assert "Unparseable lines skipped: 1" in text
    assert len(_llm_steps(text)) == 1


def test_chaos_setup_record_renders_as_a_labeled_step(tmp_path: Path) -> None:
    path = _write_jsonl(
        tmp_path / "redis_saturation.jsonl",
        [
            _scenario_start("inv1", "2026-08-01T10:00:00+00:00"),
            _stamp(
                {
                    "kind": "chaos_setup",
                    "scenario": "redis_saturation",
                    "hook": "saturate_redis",
                    "arguments": {"target_pct": 95},
                    "result": {"ok": True},
                },
                "inv1",
                "2026-08-01T10:00:01+00:00",
            ),
        ],
    )

    text = format_trace(path)

    assert "CHAOS SETUP" in text
    assert "saturate_redis" in text
    assert "unknown kind" not in text


# --- A-05 blast radius: one bad file must not abort the loop --------------


def test_one_corrupt_file_does_not_abort_the_rest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """main() renders every other scenario and still exits 0."""
    trace_dir = tmp_path / "traces"
    out_dir = tmp_path / "human"
    _write_jsonl(
        trace_dir / "good_scenario.jsonl",
        [
            _scenario_start("inv1", "2026-08-01T10:00:00+00:00", scenario="good_scenario"),
            _llm("inv1", "2026-08-01T10:00:05+00:00"),
            _scenario_end(
                "inv1",
                "2026-08-01T10:01:00+00:00",
                passed=True,
                final_state="resolved",
                scenario="good_scenario",
            ),
        ],
    )
    # A record with no "kind" is genuine schema drift, not a truncated line:
    # the renderer must fail loudly for that file and keep going.
    _write_jsonl(trace_dir / "bad_scenario.jsonl", [{"role": "planner", "no": "kind"}])

    code = main(["--trace-dir", str(trace_dir), "--out-dir", str(out_dir)])

    assert code == 0
    assert (out_dir / "good_scenario.txt").exists()
    assert not (out_dir / "bad_scenario.txt").exists()
    printed = capsys.readouterr().out
    assert "ERROR bad_scenario.jsonl" in printed
    assert "1 file(s) failed to render" in printed


def test_main_renders_a_partial_archive_trace_slice(tmp_path: Path) -> None:
    """A run directory with NO report.json is still renderable evidence (ADR 0017).

    ``report.json`` absent means the run was killed mid-suite; the per-scenario
    trace slices under it cost real money and are first-class evidence, so the
    renderer must never require the completion marker.
    """
    archive = tmp_path / "runs" / "deadbeefcafe"
    slice_dir = archive / "traces"
    _write_jsonl(
        slice_dir / "redis_saturation.jsonl",
        [
            _scenario_start("deadbeefcafe", "2026-08-01T10:00:00+00:00"),
            _llm_parse_failed("deadbeefcafe", "2026-08-01T10:00:05+00:00"),
            # Killed before scenario_end: the marker file was never written.
        ],
    )
    out_dir = tmp_path / "human"

    code = main(["--trace-dir", str(slice_dir), "--out-dir", str(out_dir)])

    assert code == 0
    assert not (archive / "report.json").exists()
    rendered = (out_dir / "redis_saturation.txt").read_text()
    assert "PARSE FAILED" in rendered
    assert "SCENARIO DID NOT REACH scenario_end" in rendered


def test_main_never_writes_outside_the_requested_out_dir(tmp_path: Path) -> None:
    """--trace-dir/--out-dir are honored so tests never touch the real evals/ tree."""
    trace_dir = tmp_path / "traces"
    out_dir = tmp_path / "human"
    _write_jsonl(
        trace_dir / "solo.jsonl",
        [_scenario_start("inv1", "2026-08-01T10:00:00+00:00", scenario="solo")],
    )

    assert main(["--trace-dir", str(trace_dir), "--out-dir", str(out_dir)]) == 0
    assert sorted(p.name for p in out_dir.iterdir()) == ["solo.txt"]


# --- Every kind the harness writes must render (WO-R2-85) -----------------


def _llm_error(
    invocation: str,
    when: str,
    *,
    attempt: int = 2,
    terminal: bool = True,
    role: str = "planner",
) -> dict[str, Any]:
    """What ``LLMClient._trace_error`` writes: a call that was billed and lost."""
    return _stamp(
        {
            "kind": "llm_error",
            "role": role,
            "request": {"model": _MODEL, "system": [{"text": "system"}]},
            "error": "APIStatusError: 429 rate_limit_error",
            "attempt": attempt,
            "terminal": terminal,
            "output_model": "InvestigationStep",
            "duration_seconds": 3.5,
        },
        invocation,
        when,
    )


def _precondition(
    invocation: str,
    when: str,
    *,
    met: bool = True,
    answered: bool = True,
    failures: list[str] | None = None,
) -> dict[str, Any]:
    """What ``runner._assert_preconditions`` writes after the deciding attempt."""
    return _stamp(
        {
            "kind": "precondition",
            "scenario": "redis_saturation",
            "tool": "get_consumer_lag",
            "arguments": {"consumer_group": "worker-dispatcher"},
            "met": met,
            "answered": answered,
            "ever_answered": True,
            "failures": failures or [],
        },
        invocation,
        when,
    )


class TestEveryKindRenders:
    """The renderer's formatter table must cover the tracer's enumeration.

    ``llm_error`` and ``precondition`` were written by the harness for as
    long as they have existed and rendered as ``STEP N — unknown kind=…``,
    a one-line JSON dump of the whole record — including, for an
    ``llm_error``, the entire system prompt. This class is why a new
    ``TraceKind`` member cannot repeat that: adding one fails here until it
    has a formatter.
    """

    def test_the_formatter_table_covers_every_tracer_kind(self) -> None:
        from evals.tracing import TraceKind

        covered = set(STEP_FORMATTERS) | set(BOUNDARY_KINDS)
        missing = sorted(str(kind) for kind in TraceKind if kind not in covered)
        assert missing == [], (
            f"these TraceKind members have no step formatter: {missing}. They will "
            "render as 'unknown kind' — a raw JSON dump — in every human report. "
            "Add a formatter to scripts/format_traces.py::STEP_FORMATTERS."
        )

    def test_the_table_names_no_kind_the_tracer_cannot_write(self) -> None:
        """The other direction: a formatter for a kind that no longer exists.

        Dead entries are how a table stops describing the harness — the
        renderer looks covered while the real kinds drift past it.
        """
        from evals.tracing import TraceKind

        known = {str(kind) for kind in TraceKind}
        stale = sorted((set(STEP_FORMATTERS) | set(BOUNDARY_KINDS)) - known)
        assert stale == [], (
            f"scripts/format_traces.py renders kinds the tracer cannot write: {stale}. "
            "Either they were removed from TraceKind or the table has a typo."
        )

    def test_trace_records_name_their_kind_through_the_enumeration(self) -> None:
        """No bare ``"kind": "..."`` literal in the two writers.

        The coverage test above is only as good as the enumeration's claim to
        be complete. A literal written straight into a record bypasses it —
        which is exactly how ``precondition`` came to exist without the
        renderer ever hearing about it.
        """
        repo_root = Path(__file__).resolve().parents[2]
        offenders = [
            f"{path.relative_to(repo_root)}:{number}"
            for path in (repo_root / "evals" / "tracing.py", repo_root / "evals" / "runner.py")
            for number, line in enumerate(path.read_text().splitlines(), start=1)
            if re.search(r'"kind":\s*"', line)
        ]
        assert offenders == [], (
            f"trace records with a hard-coded kind string: {offenders}. Use an "
            "evals.tracing.TraceKind member so the renderer's coverage test sees it."
        )


def test_llm_error_renders_as_a_labeled_step(tmp_path: Path) -> None:
    path = _write_jsonl(
        tmp_path / "redis_saturation.jsonl",
        [
            _scenario_start("inv1", "2026-08-01T10:00:00+00:00"),
            _llm_error("inv1", "2026-08-01T10:00:05+00:00"),
        ],
    )

    text = format_trace(path)

    assert "unknown kind" not in text
    assert "LLM CALL FAILED (planner)" in text
    assert "429 rate_limit_error" in text
    # Attempt is zero-based on the record and one-based for the reader.
    assert "Attempt: 3 — gave up" in text


def test_a_retried_llm_error_says_so(tmp_path: Path) -> None:
    path = _write_jsonl(
        tmp_path / "redis_saturation.jsonl",
        [
            _scenario_start("inv1", "2026-08-01T10:00:00+00:00"),
            _llm_error("inv1", "2026-08-01T10:00:05+00:00", attempt=0, terminal=False),
        ],
    )

    assert "Attempt: 1 — retried" in format_trace(path)


def test_llm_errors_count_toward_the_llm_call_total(tmp_path: Path) -> None:
    """The billed-but-failed calls the tracer captures are spend, not gaps.

    ``mcp_error`` was already counted in the tool total; ``llm_error`` was
    counted nowhere, so a run that burned three 429-exhausted planner calls
    reported them as zero LLM calls.
    """
    path = _write_jsonl(
        tmp_path / "redis_saturation.jsonl",
        [
            _scenario_start("inv1", "2026-08-01T10:00:00+00:00"),
            _llm("inv1", "2026-08-01T10:00:01+00:00"),
            _llm_error("inv1", "2026-08-01T10:00:05+00:00"),
            _scenario_end(
                "inv1", "2026-08-01T10:00:09+00:00", passed=False, final_state="escalated"
            ),
        ],
    )

    text = format_trace(path)

    assert "LLM calls:     2 (1 failed)" in text


def test_precondition_renders_its_verdict(tmp_path: Path) -> None:
    path = _write_jsonl(
        tmp_path / "redis_saturation.jsonl",
        [
            _scenario_start("inv1", "2026-08-01T10:00:00+00:00"),
            _precondition("inv1", "2026-08-01T10:00:01+00:00"),
        ],
    )

    text = format_trace(path)

    assert "unknown kind" not in text
    assert "PRECONDITION MET" in text
    assert "get_consumer_lag" in text


def test_an_unverifiable_precondition_is_not_reported_as_not_met(tmp_path: Path) -> None:
    """The two failures send a reader to different halves of the system.

    NOT MET says the fault was never manufactured — look at seeding.
    UNVERIFIABLE says the platform never answered the deciding attempt —
    look at the platform. Collapsing them is the bug the record pair exists
    to prevent, so the report must not collapse them either.
    """
    path = _write_jsonl(
        tmp_path / "redis_saturation.jsonl",
        [
            _scenario_start("inv1", "2026-08-01T10:00:00+00:00"),
            _precondition(
                "inv1",
                "2026-08-01T10:00:01+00:00",
                met=False,
                answered=False,
                failures=["get_consumer_lag: probe failed: transport error"],
            ),
        ],
    )

    text = format_trace(path)

    assert "PRECONDITION UNVERIFIABLE" in text
    assert "PRECONDITION NOT MET" not in text
    assert "transport error" in text


def test_a_kind_from_a_newer_harness_is_dumped_not_dropped(tmp_path: Path) -> None:
    """An unrenderable record is still evidence — the fallback stays."""
    path = _write_jsonl(
        tmp_path / "redis_saturation.jsonl",
        [
            _scenario_start("inv1", "2026-08-01T10:00:00+00:00"),
            _stamp({"kind": "from_the_future", "why": "not yet invented"}, "inv1", "2026-08-01T10:00:01+00:00"),
        ],
    )

    text = format_trace(path)

    assert "unknown kind=from_the_future" in text
    assert "not yet invented" in text
