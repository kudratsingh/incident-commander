"""Unit tests for evals.tracing — JSONL trace file writer + tracer hooks."""

from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path
from typing import Any, Final

import pytest
from pydantic import SecretStr

from evals.graders.deterministic import ScenarioExpectation
from evals.runner import run_scenario
from evals.scenarios.schema import Scenario
from evals.tracing import JsonlTracer, TraceKind, tracer_for
from incident_commander.agent.state import IncidentState
from incident_commander.agent.strategies.records import (
    CandidateRecord,
    LLMCallRecord,
    SelectorRecord,
    StepRecord,
)
from incident_commander.api.schemas import AlertPayload
from incident_commander.config import Settings
from incident_commander.tools.mcp_client import ToolResult


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
        # INVERTED 2026-08-07: this asserted the file was cleared on construction, which pinned
        # behaviour that deleted the previous attempt's records. Attempts separate by invocation_id.
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

    Until 2026-08-07 ``__post_init__`` truncated the file (F-002).
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


class TestRecordIdentity:
    """Every record is addressable, so one can name another (ADR 0035).

    ``invocation_id`` groups an attempt but cannot say which record a re-ask repairs.
    """

    def test_every_record_gets_a_record_id(self, tmp_path: Path) -> None:
        tracer = JsonlTracer(path=tmp_path / "t.jsonl")
        tracer.write({"kind": "llm"})
        tracer.write({"kind": "llm"})
        ids = [
            json.loads(line)["record_id"]
            for line in (tmp_path / "t.jsonl").read_text().splitlines()
        ]
        assert all(ids)
        assert len(set(ids)) == 2

    def test_a_caller_supplied_record_id_is_not_overwritten(self, tmp_path: Path) -> None:
        """``LLMClient`` mints its own so it can hand it back to the caller."""
        tracer = JsonlTracer(path=tmp_path / "t.jsonl")
        tracer.write({"kind": "llm", "record_id": "mine"})
        assert json.loads((tmp_path / "t.jsonl").read_text())["record_id"] == "mine"


# ---------------------------------------------------------------------------
# WP-2.1 — StepRecords reach the append-only store, one per planner step, offline
# too, and carry no hidden chain-of-thought.


def _test_settings(**overrides: Any) -> Settings:
    """Offline placeholders: every URL and key here is the canned sentinel."""
    defaults: dict[str, Any] = {
        "anthropic_api_key": SecretStr("eval"),
        "judge_model": "claude-haiku-4-5",
        "platform_mcp_url": "https://eval.local",
        "platform_rest_url": "https://eval.local",
        "platform_token": SecretStr("eval"),
        "platform_chaos_token": SecretStr("eval-chaos"),
        "platform_webhook_secret": SecretStr("eval"),
        "database_url": "postgresql://eval:eval@localhost:5432/eval",
    }
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)  # type: ignore[call-arg]


def _two_iteration_scenario() -> Scenario:
    """A canned scenario whose planner runs exactly twice: probe, then stop.

    Once per run passes any one-step test.
    """
    return Scenario(
        name="step_record_two_iterations",
        alert=AlertPayload(source="platform.kafka", severity="high", group="billing"),
        expectation=ScenarioExpectation(
            name="step_record_two_iterations",
            expected_terminal_state=IncidentState.ESCALATED,
            expected_evidence_contains=("billing",),
            max_tool_calls=5,
        ),
        canned_tool_responses={
            "get_consumer_lag": ToolResult(
                content=[
                    {
                        "type": "text",
                        "text": (
                            '{"consumer_group":"billing","lag":42,"lag_known":true,'
                            '"source":"static",'
                            '"cache_key":"kafka:consumer_lag:worker-dispatcher"}'
                        ),
                    }
                ],
            )
        },
        canned_llm_responses={
            "investigation_planner": [
                {
                    "hypotheses": [
                        {
                            "category": "consumer_saturation",
                            "name": "consumer_saturation",
                            "confidence": 0.55,
                            "reasoning": "Paging severity on the billing consumer.",
                        }
                    ],
                    "next_action": {
                        "kind": "probe",
                        "tool_name": "get_consumer_lag",
                        "arguments": {"consumer_group": "billing"},
                    },
                },
                {
                    "hypotheses": [
                        {
                            "category": "consumer_saturation",
                            "name": "consumer_saturation",
                            "confidence": 0.85,
                            "reasoning": "Lag reading confirms saturation.",
                        }
                    ],
                    "next_action": {"kind": "stop", "reason": "confidence sufficient"},
                },
            ],
            "briefing_writer": [
                {
                    "findings": "billing consumer lag observed at 42 messages",
                    "recommendation": "verify the billing consumer pod",
                }
            ],
        },
    )


def _records(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _steps(path: Path) -> list[dict[str, Any]]:
    return [r for r in _records(path) if r["kind"] == TraceKind.STEP]


class TestStepRecordsReachTheTraceStore:
    """The offline landing place for per-step research data (divergence D1).

    Only the live targets export ``EVAL_TRACE_DIR``, so a ``StepRecord`` was built on
    every run and read on none.
    """

    def _run(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        monkeypatch.setenv("EVAL_TRACE_DIR", str(tmp_path))
        scenario = _two_iteration_scenario()
        run_scenario(scenario, _test_settings())
        return tmp_path / f"{scenario.name}.jsonl"

    def test_one_record_per_planner_iteration(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        steps = _steps(self._run(tmp_path, monkeypatch))
        assert [step["iteration"] for step in steps] == [0, 1], (
            "expected one step record per planner iteration, numbered by the loop"
        )

    def test_the_baseline_candidate_set_holds_exactly_one_candidate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The control group's shape on a canned run: one call, one ranking, no alternatives.
        steps = _steps(self._run(tmp_path, monkeypatch))
        assert [len(step["candidate_set"]) for step in steps] == [1, 1]
        assert {step["strategy"] for step in steps} == {"baseline"}
        assert [step["selector"] for step in steps] == [None, None], (
            "baseline selects between nothing; a selector record here would be invented"
        )

    def test_the_record_names_the_step_the_loop_was_handed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        probe, stop = _steps(self._run(tmp_path, monkeypatch))
        assert probe["emitted_step"]["next_action"]["tool_name"] == "get_consumer_lag"
        assert probe["candidate_set"][0]["proposed_probe"] == "get_consumer_lag"
        assert stop["emitted_step"]["next_action"]["kind"] == "stop"
        # The ranking either side of the step, so a reader can see what the
        # probe did to the model's mind rather than only where it ended up.
        assert probe["hypothesis_state_before"] == []
        assert stop["hypothesis_state_before"] == probe["hypothesis_state_after"]

    def test_the_records_join_to_the_run_that_produced_them(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("EVAL_TRACE_DIR", str(tmp_path))
        scenario = _two_iteration_scenario()
        result = run_scenario(scenario, _test_settings(), invocation_id="inv0000000001")
        steps = _steps(tmp_path / f"{scenario.name}.jsonl")

        assert {step["run_id"] for step in steps} == {result.trajectory.incident_id}
        assert {step["invocation_id"] for step in steps} == {"inv0000000001"}
        assert all(step["record_id"] for step in steps), "every record is addressable"
        assert len({step["step_id"] for step in steps}) == 2

    def test_the_context_size_is_measured_and_moves_with_the_context(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Anti-vacuity for ``planner_input_tokens`` and its offline twin.

        The second step's context carries the first step's evidence, so a real measurement is
        larger the second time. ``planner_input_tokens`` is honestly 0 offline.
        """
        first, second = _steps(self._run(tmp_path, monkeypatch))

        assert first["planner_context_chars"] > 0
        assert second["planner_context_chars"] > first["planner_context_chars"], (
            "the second planner step saw the first probe's evidence and the "
            "measured context did not grow"
        )
        assert first["planner_input_tokens"] == 0, (
            "a canned client bills nothing; a non-zero token count here is invented"
        )

    def test_nothing_is_written_when_no_trace_dir_is_set(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The opt-in stays opt-in: `make eval-reg` sets no EVAL_TRACE_DIR and
        # must stay byte-identical, records built and nobody recording.
        monkeypatch.delenv("EVAL_TRACE_DIR", raising=False)
        scenario = _two_iteration_scenario()
        run_scenario(scenario, _test_settings())
        assert list(tmp_path.iterdir()) == []

    def test_the_checkpoint_is_still_schema_version_3(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Research data goes to the trace store, never to ``RunState``.

        ``RunState`` is a frozen checkpoint (divergence C7).
        """
        monkeypatch.setenv("EVAL_TRACE_DIR", str(tmp_path))
        scenario = _two_iteration_scenario()
        result = run_scenario(scenario, _test_settings())

        assert _steps(tmp_path / f"{scenario.name}.jsonl"), "no step records were written"
        assert {c.schema_version for c in result.trajectory.checkpoints} == {3}
        dumped = json.dumps([c.model_dump(mode="json") for c in result.trajectory.checkpoints])
        for leaked in ("candidate_set", "step_id", "planner_input_tokens", "emitted_step"):
            assert leaked not in dumped, f"{leaked} reached the checkpoint"

    def test_a_second_invocation_appends_its_steps(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Invariant 9 for the new kind (F-002 is what it is for).

        A tracer that truncated would compare only the last run.
        """
        monkeypatch.setenv("EVAL_TRACE_DIR", str(tmp_path))
        scenario = _two_iteration_scenario()
        run_scenario(scenario, _test_settings(), invocation_id="inv0000000001")
        run_scenario(scenario, _test_settings(), invocation_id="inv0000000002")

        steps = _steps(tmp_path / f"{scenario.name}.jsonl")
        assert [step["invocation_id"] for step in steps] == [
            "inv0000000001",
            "inv0000000001",
            "inv0000000002",
            "inv0000000002",
        ]
        assert [step["iteration"] for step in steps] == [0, 1, 0, 1]


class TestNoChainOfThoughtIsStored:
    """Plan 02 § 7: structured outputs and the short ``reasoning`` fields the schema asks
    for — nothing else.

    A hidden chain-of-thought is unreviewed model text stored under the evaluator's name,
    so the ban is structural.
    """

    #: Names a hidden-reasoning field once separators are stripped, so the three spellings
    #: are one entry. ``reasoning`` is NOT here: plan 02 § 7 permits it.
    _BANNED_SUBSTRINGS: Final[tuple[str, ...]] = (
        "chainofthought",
        "thinking",
        "thought",
        "scratchpad",
        "monologue",
        "deliberation",
        "rawoutput",
        "rawresponse",
    )
    #: Too short to match as a substring without firing on innocent words, so
    #: these must be a whole word of the name.
    _BANNED_TOKENS: Final[tuple[str, ...]] = ("cot",)

    def _offending(self, name: str) -> list[str]:
        tokens = [part for part in re.split(r"[^a-z0-9]+", name.lower()) if part]
        flat = "".join(tokens)
        return sorted(
            {banned for banned in self._BANNED_TOKENS if banned in tokens}
            | {banned for banned in self._BANNED_SUBSTRINGS if banned in flat}
        )

    def _field_names(self, record_type: type) -> list[str]:
        return [field.name for field in dataclasses.fields(record_type)]

    @pytest.mark.parametrize(
        "record_type",
        [StepRecord, CandidateRecord, SelectorRecord, LLMCallRecord],
        ids=lambda record_type: record_type.__name__,
    )
    def test_no_field_is_named_like_a_chain_of_thought(self, record_type: type) -> None:
        offenders = {
            name: self._offending(name)
            for name in self._field_names(record_type)
            if self._offending(name)
        }
        assert offenders == {}, (
            f"{record_type.__name__} carries {sorted(offenders)}. Plan 02 § 7: no hidden "
            "chain-of-thought is stored — structured outputs and the short 'reasoning' "
            "fields the schema already asks for, only."
        )

    def test_the_written_record_carries_no_such_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The other half: field names are the schema, this is what actually
        # lands on disk, nested keys included.
        monkeypatch.setenv("EVAL_TRACE_DIR", str(tmp_path))
        scenario = _two_iteration_scenario()
        run_scenario(scenario, _test_settings())

        def keys(node: Any) -> list[str]:
            if isinstance(node, dict):
                return [str(k) for k in node] + [k for v in node.values() for k in keys(v)]
            if isinstance(node, list):
                return [k for item in node for k in keys(item)]
            return []

        for step in _steps(tmp_path / f"{scenario.name}.jsonl"):
            offenders = sorted({k for k in keys(step) if self._offending(k)})
            assert offenders == [], f"step record wrote {offenders}"

    def test_the_ban_is_not_vacuous(self) -> None:
        # If the matcher stops matching, every test above passes for the
        # wrong reason. Red-before, permanently.
        assert self._offending("chain_of_thought")
        assert self._offending("chainOfThought")
        assert self._offending("raw_thought")
        assert self._offending("planner_scratchpad")
        assert self._offending("raw_output")
        assert self._offending("cot")
        # ...and the permitted neighbours stay permitted.
        assert not self._offending("reasoning")
        assert not self._offending("planner_context_chars")
        assert not self._offending("hypothesis_state_before")
        assert not self._offending("context")


class TestTheStepKindIsARealTraceKind:
    def test_step_is_written_through_the_enumeration(self) -> None:
        # ``TraceKind`` membership is what makes the renderer's coverage test fire.
        assert TraceKind.STEP.value == "step"

    def test_the_tracer_stamps_a_step_record_like_any_other(self, tmp_path: Path) -> None:
        tracer = tracer_for("scenario_x", tmp_path)
        record = StepRecord(
            run_id="incident-1",
            iteration=0,
            strategy="baseline",
            model="m",
            candidate_set=(),
            emitted_step=_investigation_step(),
        )
        tracer.write({"kind": TraceKind.STEP, **record.as_trace_record()})

        written = _records(tmp_path / "scenario_x.jsonl")[0]
        assert written["kind"] == "step"
        assert written["invocation_id"] == tracer.invocation_id
        assert written["record_id"] and written["timestamp"]
        assert written["step_id"] == record.step_id


def _investigation_step() -> Any:
    from incident_commander.agent.hypothesis import InvestigationStep

    return InvestigationStep.model_validate(
        {
            "hypotheses": [
                {
                    "category": "consumer_saturation",
                    "name": "consumer_saturation",
                    "confidence": 0.9,
                    "reasoning": "Lag reading confirms saturation.",
                }
            ],
            "next_action": {"kind": "stop", "reason": "confidence sufficient"},
        }
    )
