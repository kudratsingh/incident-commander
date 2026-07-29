"""Unit tests for evals.tracing — JSONL trace file writer + tracer hooks."""

from __future__ import annotations

import json
from pathlib import Path

from evals.tracing import JsonlTracer, tracer_for


class TestJsonlTracer:
    def test_write_appends_line_and_stamps_timestamp(self, tmp_path: Path) -> None:
        tracer = JsonlTracer(path=tmp_path / "run.jsonl")
        tracer.write({"kind": "llm", "role": "planner"})
        lines = (tmp_path / "run.jsonl").read_text().strip().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["kind"] == "llm"
        assert record["role"] == "planner"
        assert "timestamp" in record  # auto-populated

    def test_write_respects_explicit_timestamp(self, tmp_path: Path) -> None:
        tracer = JsonlTracer(path=tmp_path / "run.jsonl")
        tracer.write({"kind": "mcp", "timestamp": "2026-01-01T00:00:00+00:00"})
        record = json.loads((tmp_path / "run.jsonl").read_text().strip())
        assert record["timestamp"] == "2026-01-01T00:00:00+00:00"

    def test_multiple_writes_produce_one_line_each(self, tmp_path: Path) -> None:
        tracer = JsonlTracer(path=tmp_path / "run.jsonl")
        tracer.write({"kind": "a"})
        tracer.write({"kind": "b"})
        lines = (tmp_path / "run.jsonl").read_text().strip().splitlines()
        assert [json.loads(line)["kind"] for line in lines] == ["a", "b"]

    def test_creates_parent_directory(self, tmp_path: Path) -> None:
        target = tmp_path / "nested" / "traces" / "s.jsonl"
        JsonlTracer(path=target)
        assert target.parent.exists()

    def test_truncates_on_construction(self, tmp_path: Path) -> None:
        # Simulates a re-run of the same scenario — the file should not
        # accumulate stale lines from the prior run.
        target = tmp_path / "run.jsonl"
        target.write_text('{"kind": "stale"}\n')
        JsonlTracer(path=target)
        assert target.read_text() == ""


class TestLlmHook:
    def test_hook_writes_llm_kind_with_role(self, tmp_path: Path) -> None:
        tracer = JsonlTracer(path=tmp_path / "run.jsonl")
        hook = tracer.llm_hook("investigation_planner")
        hook({"request": {"model": "x"}, "output": {"y": 1}})
        record = json.loads((tmp_path / "run.jsonl").read_text().strip())
        assert record["kind"] == "llm"
        assert record["role"] == "investigation_planner"
        assert record["request"] == {"model": "x"}
        assert record["output"] == {"y": 1}


class TestMcpHook:
    def test_success_hook_writes_mcp_kind(self, tmp_path: Path) -> None:
        tracer = JsonlTracer(path=tmp_path / "run.jsonl")
        hook = tracer.mcp_hook()
        hook({"tool_name": "get_consumer_lag", "arguments": {}, "result": {"lag": 0}})
        record = json.loads((tmp_path / "run.jsonl").read_text().strip())
        assert record["kind"] == "mcp"
        assert record["tool_name"] == "get_consumer_lag"

    def test_error_hook_writes_mcp_error_kind(self, tmp_path: Path) -> None:
        tracer = JsonlTracer(path=tmp_path / "run.jsonl")
        hook = tracer.mcp_hook()
        hook({"tool_name": "get_consumer_lag", "arguments": {}, "error": "boom"})
        record = json.loads((tmp_path / "run.jsonl").read_text().strip())
        assert record["kind"] == "mcp_error"
        assert record["error"] == "boom"


class TestTracerFor:
    def test_names_file_after_scenario(self, tmp_path: Path) -> None:
        tracer = tracer_for("consumer_lag_high", tmp_path)
        assert tracer.path == tmp_path / "consumer_lag_high.jsonl"
        assert tracer.path.exists()
