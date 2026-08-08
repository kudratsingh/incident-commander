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

    def test_does_not_truncate_on_construction(self, tmp_path: Path) -> None:
        # INVERTED 2026-08-07. This test previously asserted the file was
        # cleared on construction "so re-runs don't accumulate stale
        # lines" — the assertion was the bug, not the guard: it pinned
        # behavior that deleted the previous attempt's records. Run 001's
        # killed attempt was erased by its own re-run. Attempts are now
        # separated by invocation_id, not by deletion.
        target = tmp_path / "run.jsonl"
        target.write_text('{"kind": "prior_attempt"}\n')
        JsonlTracer(path=target)
        assert target.read_text() == '{"kind": "prior_attempt"}\n'


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
        # The file is created by the first write, not by construction —
        # construction no longer touches an existing file's contents.
        assert tracer.path.parent.exists()
        tracer.write({"kind": "llm"})
        assert tracer.path.exists()


class TestNoTruncationAcrossInvocations:
    """Regression: the tracer must never delete a prior attempt's records.

    Until 2026-08-07 ``JsonlTracer.__post_init__`` truncated the file "so
    re-runs don't concatenate". Run 001's killed first attempt was erased
    in full by its own re-run; the loss surfaced only as a ~1.9x gap
    between trace-derived and billed cost (study/findings.md F-002).
    """

    def _records(self, path: Path) -> list[dict[str, object]]:
        return [json.loads(line) for line in path.read_text().splitlines()]

    def test_second_invocation_preserves_the_first(self, tmp_path: Path) -> None:
        first = tracer_for("scenario_x", tmp_path)
        first.write({"kind": "llm", "attempt": 1})
        second = tracer_for("scenario_x", tmp_path)
        second.write({"kind": "llm", "attempt": 2})

        records = self._records(tmp_path / "scenario_x.jsonl")
        assert [r["attempt"] for r in records] == [1, 2], (
            "re-running a scenario deleted the earlier attempt's records"
        )

    def test_invocations_are_separable(self, tmp_path: Path) -> None:
        first = tracer_for("scenario_x", tmp_path)
        first.write({"kind": "llm"})
        first.write({"kind": "mcp"})
        second = tracer_for("scenario_x", tmp_path)
        second.write({"kind": "llm"})

        records = self._records(tmp_path / "scenario_x.jsonl")
        ids = [r["invocation_id"] for r in records]
        assert ids[0] == ids[1] != ids[2], "records must group by invocation"
        assert all(r["invocation_started_at"] for r in records)

    def test_invocation_id_does_not_overwrite_caller_supplied_value(self, tmp_path: Path) -> None:
        tracer = tracer_for("scenario_x", tmp_path)
        tracer.write({"kind": "llm", "invocation_id": "explicit"})
        assert self._records(tmp_path / "scenario_x.jsonl")[0]["invocation_id"] == "explicit"
