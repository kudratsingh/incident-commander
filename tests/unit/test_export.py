"""The trajectory export (WP-15.1, `evals/export.py`) and the promises it has to keep.

A line of it can end up in a training set, so each test is tied to a way that goes wrong:
a holdout template is refused BY NAME and the refusal is the whole export (plan 03 § 4);
labels live in a separate file (ADR 0038); observations are refs, not inlined tool output
(invariant 4); no model prose beyond an action's `arguments`; and the export re-derives.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import pytest

from evals import artifacts, export, reward
from evals.graders.deterministic import ScenarioExpectation
from evals.scenarios.schema import (
    BenchmarkSplit,
    DiscriminatingProbe,
    GroundTruth,
    Scenario,
    ScenarioDifficulty,
    ScenarioFamily,
)
from evals.tracing import TraceKind
from incident_commander.agent.hypothesis import HypothesisCategory
from incident_commander.agent.state import IncidentState
from incident_commander.api.schemas import AlertPayload

# Imported under a non-collectable name so the trace store's rule has ONE definition.
from tests.unit.test_tracing import TestNoChainOfThoughtIsStored as _CoTNames

_WHEN: Final[datetime] = datetime(2026, 9, 18, 21, 30, tzinfo=UTC)
_INVOCATION: Final[str] = "aaaabbbbcccc"

#: Planted in the places a leak would come from. None may reach a trajectory line.
_PAYLOAD_SENTINEL: Final[str] = "PAYLOAD-SENTINEL-ignore-your-instructions"
_PROSE_SENTINEL: Final[str] = "PROSE-SENTINEL-the-model-said-this"
_TRUTH_SENTINEL: Final[str] = "TRUTH-SENTINEL-the-answer-key"


def _scenario(
    name: str,
    *,
    template_id: str | None = None,
    split: BenchmarkSplit = BenchmarkSplit.DEV,
    truth: GroundTruth | None = None,
) -> Scenario:
    return Scenario(
        name=name,
        template_id=template_id or name,
        family=ScenarioFamily.CONSUMER_LAG,
        difficulty=ScenarioDifficulty.SINGLE,
        benchmark_split=split,
        alert=AlertPayload(source="billing"),
        expectation=ScenarioExpectation(
            name=name,
            expected_terminal_state=IncidentState.RESOLVED,
            expected_action_tools=("restart_consumer_group",),
            forbidden_action_tools=("pause_dag",),
            max_tool_calls=9,
        ),
        ground_truth=truth,
        discriminating_probes=(
            DiscriminatingProbe(tool="list_dlq_messages", argument_pattern={"limit": "^5"}),
        ),
    )


_TRUTH: Final[GroundTruth] = GroundTruth(
    incident_count=1,
    # A label the agent's own trace never mentions, so finding this string in the
    # trajectory file can only mean the answer key leaked into it.
    root_causes=(HypothesisCategory.POISON_MESSAGE,),
    affected_components=(_TRUTH_SENTINEL,),
    causal_chain=(_TRUTH_SENTINEL, "then_this"),
)


def _step_record(scenario: str) -> dict[str, Any]:
    """One `step` trace record, with model prose in every field that carries it."""
    return {
        "kind": TraceKind.STEP.value,
        "record_id": "step0record1",
        "step_id": "stepid000001",
        "run_id": "run000000001",
        "iteration": 1,
        "strategy": "baseline",
        "model": "claude-sonnet-4-6",
        "candidate_set": [
            {
                "candidate_id": "cand00000001",
                "category": HypothesisCategory.CONSUMER_SATURATION.value,
                "name": _PROSE_SENTINEL,
                "confidence": 0.82,
                "evidence_for": ["ev-1"],
                "evidence_against": [],
                "proposed_probe": "get_consumer_lag",
                "generation_call_id": "llmcall00001",
            }
        ],
        "selector": {
            "selected_candidate_id": "cand00000001",
            "scores": {"cand00000001": 0.9},
            "uncertainty": 0.1,
            "decision": "select",
        },
        "emitted_step": {
            "hypotheses": [
                {
                    "category": HypothesisCategory.CONSUMER_SATURATION.value,
                    "name": _PROSE_SENTINEL,
                    "confidence": 0.82,
                    "reasoning": _PROSE_SENTINEL,
                }
            ],
            "next_action": {
                "kind": "probe",
                "tool_name": "get_consumer_lag",
                "arguments": {"consumer_group": "analytics-consumer"},
            },
        },
        "hypothesis_state_before": [],
        "hypothesis_state_after": [
            {
                "category": HypothesisCategory.CONSUMER_SATURATION.value,
                "name": _PROSE_SENTINEL,
                "confidence": 0.82,
                "reasoning": _PROSE_SENTINEL,
            }
        ],
        "llm_calls": [
            {
                "role": "investigation_planner",
                "model": "claude-sonnet-4-6",
                "tokens_used": 1200,
                "usd_used": "0.004000",
                "input_tokens": 900,
                "output_tokens": 300,
                "cache_read_tokens": 0,
                "cache_creation_tokens": 0,
                "call_id": "llmcall00001",
                "elapsed_ms": 1400,
            }
        ],
        "planner_input_tokens": 900,
        "planner_context_chars": 4200,
        "generation_rejections": [],
        "scenario": scenario,
    }


def _records(scenario: str, *, invocation: str, complete: bool = True) -> list[dict[str, Any]]:
    """One whole attempt at one scenario, in the order the tracer writes it."""
    common = {"invocation_id": invocation, "invocation_started_at": "2026-09-18T21:00:00+00:00"}
    records: list[dict[str, Any]] = [
        {
            "kind": TraceKind.SCENARIO_START.value,
            "record_id": "startrecord1",
            "scenario": scenario,
            "live_mcp": True,
            "live_llm": True,
            "model": "claude-sonnet-4-6",
            "model_role": "benchmark",
            "judge_model": "claude-haiku-4-5",
        },
        {
            "kind": TraceKind.LLM.value,
            "record_id": "llmcall00001",
            # The full request and response live here, and nothing of them may travel.
            "role": "investigation_planner",
            "request": {"system": _PROSE_SENTINEL, "messages": [{"text": _PAYLOAD_SENTINEL}]},
            "response": {"content": [{"text": _PROSE_SENTINEL}]},
        },
        _step_record(scenario),
        {
            "kind": TraceKind.MCP.value,
            "record_id": "mcprecord001",
            "tool_name": "get_consumer_lag",
            "arguments": {"consumer_group": "analytics-consumer"},
            "result": {
                "content": [
                    {"type": "text", "text": f'{{"lag":50000,"note":"{_PAYLOAD_SENTINEL}"}}'}
                ],
                "is_error": False,
            },
            "duration_seconds": 0.031,
        },
        {
            "kind": TraceKind.MCP_ERROR.value,
            "record_id": "mcprecord002",
            "tool_name": "list_dlq_messages",
            "arguments": {"limit": 50},
            "error": f"upstream said {_PAYLOAD_SENTINEL}",
            "duration_seconds": 0.02,
        },
    ]
    if complete:
        records.append(
            {
                "kind": TraceKind.SCENARIO_END.value,
                "record_id": "endrecord001",
                "scenario": scenario,
                "final_state": IncidentState.RESOLVED.value,
                "passed": True,
                "tool_calls_used": 2,
                "failure_class": "passed",
            }
        )
    return [{**common, **record} for record in records]


def _write_trace(directory: Path, scenario: str, *, invocations: tuple[str, ...]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{scenario}.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        for invocation in invocations:
            for record in _records(scenario, invocation=invocation):
                handle.write(json.dumps(record) + "\n")
    return path


@pytest.fixture
def traces(tmp_path: Path) -> list[Path]:
    """One dev scenario's trace file, holding two attempts (invariant 9: both stay)."""
    return [
        _write_trace(tmp_path / "traces", "lag_dev", invocations=("inv000000001", "inv000000002"))
    ]


def _corpus(*scenarios: Scenario) -> tuple[Scenario, ...]:
    return scenarios


def _build(traces: list[Path], corpus: tuple[Scenario, ...], **kwargs: Any) -> export.Export:
    return export.build_export(
        traces=traces,
        corpus=corpus,
        timestamp=kwargs.pop("timestamp", _WHEN),
        invocation_id=kwargs.pop("invocation_id", _INVOCATION),
        **kwargs,
    )


def _keys(value: Any) -> Iterator[str]:
    """Every key anywhere in a JSON value, however deep."""
    if isinstance(value, Mapping):
        for key, child in value.items():
            yield str(key)
            yield from _keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _keys(child)


class TestHoldoutIsRefused:
    def test_a_held_out_scenario_is_refused_by_name(self, traces: list[Path]) -> None:
        corpus = _corpus(_scenario("lag_dev", split=BenchmarkSplit.HOLDOUT, truth=_TRUTH))
        with pytest.raises(export.HoldoutExportError) as raised:
            _build(traces, corpus)
        assert "lag_dev" in str(raised.value)
        assert "template lag_dev" in str(raised.value)

    def test_the_refusal_is_a_hard_error_and_not_a_skip(self, tmp_path: Path) -> None:
        """One held-out template refuses the WHOLE export, dev trajectories included."""
        trace_dir = tmp_path / "traces"
        traces = [
            _write_trace(trace_dir, "lag_dev", invocations=("inv000000001",)),
            _write_trace(trace_dir, "lag_secret", invocations=("inv000000003",)),
        ]
        corpus = _corpus(
            _scenario("lag_dev", truth=_TRUTH),
            _scenario("lag_secret", split=BenchmarkSplit.HOLDOUT),
        )
        with pytest.raises(export.HoldoutExportError) as raised:
            _build(traces, corpus)
        assert "lag_secret" in str(raised.value)
        assert "lag_dev" not in str(raised.value)
        assert list((tmp_path / "exports").glob("*")) == []

    def test_a_dev_instance_of_a_held_out_template_is_refused_too(self, traces: list[Path]) -> None:
        """Every instance of a held-out template is held out (plan 03 § 4, 06 D7)."""
        corpus = _corpus(
            _scenario("lag_dev", template_id="shared_lag", truth=_TRUTH),
            _scenario("lag_other", template_id="shared_lag", split=BenchmarkSplit.HOLDOUT),
        )
        with pytest.raises(export.HoldoutExportError, match="shared_lag"):
            _build(traces, corpus)

    def test_a_scenario_the_corpus_does_not_hold_is_refused(self, traces: list[Path]) -> None:
        """No split, no proof it is not held out — so it is refused, never assumed."""
        with pytest.raises(export.ExportError, match="lag_dev"):
            _build(traces, _corpus(_scenario("something_else")))

    def test_nothing_is_held_out_in_the_shipped_corpus_so_the_gate_is_unused(self) -> None:
        """Today's corpus is all `dev`; this gate is for the day it is not."""
        assert export.holdout_template_ids(()) == frozenset()


class TestTheManifestRecordsWhatWasExported:
    def test_it_names_every_template_and_scenario_it_emitted(self, traces: list[Path]) -> None:
        built = _build(traces, _corpus(_scenario("lag_dev", truth=_TRUTH)))
        assert built.manifest.exported_template_ids == ("lag_dev",)
        assert built.manifest.exported_scenarios == ("lag_dev",)
        assert built.manifest.splits_present == ("dev",)
        assert built.manifest.trajectory_count == 2
        assert built.manifest.complete_count == 2

    def test_it_digests_its_own_data_and_its_sources(self, traces: list[Path]) -> None:
        built = _build(traces, _corpus(_scenario("lag_dev", truth=_TRUTH)))
        assert (
            built.manifest.trajectories_sha256
            == hashlib.sha256(built.trajectories_jsonl.encode()).hexdigest()
        )
        assert (
            built.manifest.labels_sha256 == hashlib.sha256(built.labels_jsonl.encode()).hexdigest()
        )
        assert [source.sha256 for source in built.manifest.source_traces] == [
            hashlib.sha256(traces[0].read_bytes()).hexdigest()
        ]

    def test_it_says_which_asked_for_fields_are_not_in_the_data(self, traces: list[Path]) -> None:
        """A gap is explained once in the manifest, never as a null on every line."""
        built = _build(traces, _corpus(_scenario("lag_dev", truth=_TRUTH)))
        assert set(built.manifest.absent_fields) == {
            "execution_mode",
            "recorded_world_id",
            "root_cause_grade",
        }
        assert all(len(reason) > 40 for reason in built.manifest.absent_fields.values())

    def test_reward_components_are_no_longer_absent(self, traces: list[Path]) -> None:
        """WP-15.2 filled the gap WP-15.1 left: the reward is defined once, in one module."""
        built = _build(traces, _corpus(_scenario("lag_dev", truth=_TRUTH)))
        assert "reward_components" not in built.manifest.absent_fields
        assert built.manifest.reward["module"] == "evals/reward.py"
        assert built.manifest.reward["spec"] == "docs/reward-spec.md"
        assert built.trajectories[0].reward is not None

    def test_without_an_audit_window_every_reward_is_withheld_with_its_reason(
        self, traces: list[Path]
    ) -> None:
        """Invariant 6: no audit log, no action credit — and the manifest says so once."""
        built = _build(traces, _corpus(_scenario("lag_dev", truth=_TRUTH)))
        assert built.manifest.reward["graded"] == 0
        assert built.manifest.reward["withheld"] == built.manifest.trajectory_count
        assert built.manifest.reward["withheld_reasons"] == [reward.WITHHELD_NO_AUDIT]
        training = built.trajectories[0].reward
        assert training is not None and training.total is None and not training.graded

    def test_an_audit_window_scores_the_line(self, traces: list[Path]) -> None:
        """The reward comes from `evals/reward.py`; the export never recomputes it."""
        built = _build(
            traces,
            _corpus(_scenario("lag_dev", truth=_TRUTH)),
            audit_windows={
                "lag_dev:inv000000001": reward.AuditWindow(
                    calls=(
                        reward.AuditedCall(
                            tool_name="restart_consumer_group",
                            outcome="success",
                            at=_WHEN,
                        ),
                    )
                )
            },
        )
        training = built.trajectories[0].reward
        assert training is not None
        assert training.graded
        assert training.components["action"] == 1.0
        assert set(training.components) == {
            "root_cause",
            "action",
            "budget",
            "process",
            "judge",
        }
        assert sum(training.weights.values()) == pytest.approx(1.0)
        assert built.manifest.reward["graded"] == 1
        # The SECOND attempt got no window, so it is withheld — both reasons are named.
        assert built.manifest.reward["withheld"] == 1

    def test_the_reward_s_reasons_are_in_the_labels_file_only(self, traces: list[Path]) -> None:
        """A withheld reason is a statement about the SCENARIO, so it travels with labels."""
        built = _build(traces, _corpus(_scenario("lag_dev", truth=_TRUTH)))
        detail = built.labels[0].reward_detail
        assert detail is not None
        assert detail.withheld_reason == reward.WITHHELD_NO_AUDIT
        assert reward.WITHHELD_NO_AUDIT not in built.trajectories_jsonl
        first = json.loads(built.trajectories_jsonl.splitlines()[0])
        assert "escalation_because" not in set(_keys(first))

    def test_it_names_the_two_files_it_describes(self, traces: list[Path], tmp_path: Path) -> None:
        built = _build(traces, _corpus(_scenario("lag_dev", truth=_TRUTH)))
        paths = export.write_export(
            built, timestamp=_WHEN, invocation_id=_INVOCATION, root=tmp_path
        )
        assert paths.trajectories.name == built.manifest.trajectory_file
        assert paths.labels.name == built.manifest.labels_file
        assert export.load_manifest(paths.manifest) == built.manifest


class TestTheReportRefusesAScoredTrainedTemplate:
    def _written(self, traces: list[Path], tmp_path: Path) -> export.ExportPaths:
        built = _build(traces, _corpus(_scenario("lag_dev", truth=_TRUTH)))
        return export.write_export(built, timestamp=_WHEN, invocation_id=_INVOCATION, root=tmp_path)

    def test_a_template_in_the_export_cannot_be_scored(
        self, traces: list[Path], tmp_path: Path
    ) -> None:
        written = self._written(traces, tmp_path)
        with pytest.raises(export.TrainingContaminationError) as raised:
            export.refuse_templates_seen_in_training(["lag_dev"], manifests=[written.manifest])
        assert "lag_dev" in str(raised.value)

    def test_the_newest_manifest_is_the_default(self, traces: list[Path], tmp_path: Path) -> None:
        self._written(traces, tmp_path)
        with pytest.raises(export.TrainingContaminationError):
            export.refuse_templates_seen_in_training(["lag_dev"], root=tmp_path)

    def test_a_template_no_export_covers_scores_normally(
        self, traces: list[Path], tmp_path: Path
    ) -> None:
        self._written(traces, tmp_path)
        export.refuse_templates_seen_in_training(["never_exported"], root=tmp_path)

    def test_no_export_at_all_is_a_pass_not_an_error(self, tmp_path: Path) -> None:
        export.refuse_templates_seen_in_training(["lag_dev"], root=tmp_path)


class TestLabelsAreSeparatelyControlled:
    def test_the_answer_key_is_in_the_labels_file_only(self, traces: list[Path]) -> None:
        built = _build(traces, _corpus(_scenario("lag_dev", truth=_TRUTH)))
        assert _TRUTH_SENTINEL in built.labels_jsonl
        assert _TRUTH_SENTINEL not in built.trajectories_jsonl
        assert HypothesisCategory.POISON_MESSAGE.value not in built.trajectories_jsonl

    def test_no_trajectory_line_carries_a_ground_truth_key(self, traces: list[Path]) -> None:
        built = _build(traces, _corpus(_scenario("lag_dev", truth=_TRUTH)))
        for raw in built.trajectories_jsonl.splitlines():
            keys = set(_keys(json.loads(raw)))
            assert {"ground_truth", "root_causes", "causal_chain", "passed"} & keys == set()

    def test_every_label_line_joins_to_a_trajectory_line(self, traces: list[Path]) -> None:
        built = _build(traces, _corpus(_scenario("lag_dev", truth=_TRUTH)))
        assert [record.trajectory_id for record in built.labels] == [
            record.trajectory_id for record in built.trajectories
        ]
        assert [record.passed for record in built.labels] == [True, True]

    def test_a_scenario_with_no_ground_truth_says_so(self, traces: list[Path]) -> None:
        built = _build(traces, _corpus(_scenario("lag_dev")))
        assert built.labels[0].ground_truth is None
        assert "not root-cause graded" in str(built.labels[0].ground_truth_absent_reason)

    def test_the_three_families_never_resolve_to_each_other(
        self, traces: list[Path], tmp_path: Path
    ) -> None:
        """`newest("training_export")` must not answer with the labels beside it."""
        built = _build(traces, _corpus(_scenario("lag_dev", truth=_TRUTH)))
        written = export.write_export(
            built, timestamp=_WHEN, invocation_id=_INVOCATION, root=tmp_path
        )
        assert artifacts.newest(export.TRAJECTORY_KIND, root=tmp_path) == written.trajectories
        assert artifacts.newest(export.LABELS_KIND, root=tmp_path) == written.labels
        assert artifacts.newest(export.MANIFEST_KIND, root=tmp_path) == written.manifest
        for kind in (export.TRAJECTORY_KIND, export.LABELS_KIND, export.MANIFEST_KIND):
            assert len(artifacts.versions(kind, root=tmp_path)) == 1


class TestObservationsAreRefs:
    def test_no_raw_tool_output_is_inlined(self, traces: list[Path]) -> None:
        """Invariant 4: a DLQ payload's text must not reach a training set."""
        built = _build(traces, _corpus(_scenario("lag_dev", truth=_TRUTH)))
        assert _PAYLOAD_SENTINEL in traces[0].read_text()
        assert _PAYLOAD_SENTINEL not in built.trajectories_jsonl
        assert _PAYLOAD_SENTINEL not in built.labels_jsonl
        assert _PAYLOAD_SENTINEL not in built.manifest_json

    def test_a_ref_says_where_the_observation_is_and_what_it_hashes_to(
        self, traces: list[Path]
    ) -> None:
        built = _build(traces, _corpus(_scenario("lag_dev", truth=_TRUTH)))
        refs = built.trajectories[0].observations
        assert [ref.observation_id for ref in refs] == ["mcprecord001", "mcprecord002"]
        assert {ref.trace_file for ref in refs} == {"lag_dev.jsonl"}
        assert [ref.ok for ref in refs] == [True, False]
        source = [
            json.loads(line)
            for line in traces[0].read_text().splitlines()
            if json.loads(line)["record_id"] == "mcprecord001"
        ][0]
        canonical = json.dumps(source["result"], sort_keys=True, separators=(",", ":")).encode()
        assert refs[0].content_sha256 == hashlib.sha256(canonical).hexdigest()
        assert refs[0].content_bytes == len(canonical)

    def test_every_action_names_the_observation_it_produced(self, traces: list[Path]) -> None:
        built = _build(traces, _corpus(_scenario("lag_dev", truth=_TRUTH)))
        trajectory = built.trajectories[0]
        assert [action.result.observation_ref for action in trajectory.actions] == [
            ref.observation_id for ref in trajectory.observations
        ]
        assert [action.tool_name for action in trajectory.actions] == [
            "get_consumer_lag",
            "list_dlq_messages",
        ]
        assert trajectory.actions[0].arguments == {"consumer_group": "analytics-consumer"}

    def test_a_failed_call_carries_no_error_text(self, traces: list[Path]) -> None:
        """An error string is platform output too (invariant 4) — ok=False, and a digest."""
        built = _build(traces, _corpus(_scenario("lag_dev", truth=_TRUTH)))
        failed = built.trajectories[0].observations[1]
        assert failed.kind == TraceKind.MCP_ERROR.value
        assert failed.ok is False
        assert "upstream said" not in built.trajectories_jsonl


class TestNoModelProseAndNoChainOfThought:
    def test_no_key_anywhere_is_named_like_a_chain_of_thought(self, traces: list[Path]) -> None:
        built = _build(traces, _corpus(_scenario("lag_dev", truth=_TRUTH)))
        names = _CoTNames()
        documents = [json.loads(raw) for raw in built.trajectories_jsonl.splitlines()]
        documents += [json.loads(raw) for raw in built.labels_jsonl.splitlines()]
        documents.append(json.loads(built.manifest_json))
        for document in documents:
            offenders = {
                key: names._offending(key) for key in _keys(document) if names._offending(key)
            }
            assert offenders == {}, f"the export carries {sorted(offenders)}"

    def test_the_planner_s_prose_does_not_travel(self, traces: list[Path]) -> None:
        """Not even the schema's short `reasoning`: the trace store keeps it, this does not."""
        built = _build(traces, _corpus(_scenario("lag_dev", truth=_TRUTH)))
        assert _PROSE_SENTINEL in traces[0].read_text()
        assert _PROSE_SENTINEL not in built.trajectories_jsonl
        keys = set(_keys(json.loads(built.trajectories_jsonl.splitlines()[0])))
        assert {"reasoning", "name", "reason", "request", "response"} & keys == set()

    def test_the_trajectory_keys_are_exactly_these(self, traces: list[Path]) -> None:
        """A census, not a ban list: a field added to the line lands in this diff."""
        built = _build(traces, _corpus(_scenario("lag_dev", truth=_TRUTH)))
        line = json.loads(built.trajectories_jsonl.splitlines()[0])
        # Three keys are DATA rather than schema: two tool-argument names the agent chose,
        # and a candidate id the selector's score map is keyed by.
        data_keys = {"consumer_group", "limit", "cand00000001"}
        assert set(_keys(line)) - data_keys == {
            "action_count",
            "action_kind",
            "actions",
            "agent_model",
            "arguments",
            "benchmark_split",
            "cache_creation_tokens",
            "cache_read_tokens",
            "call_id",
            "candidate_id",
            "candidates",
            "category",
            "complete",
            "components",
            "confidence",
            "content_bytes",
            "content_sha256",
            "decision",
            "decision_count",
            "decisions",
            "duration_seconds",
            "elapsed_ms",
            "execution_mode",
            "evidence_against",
            "evidence_for",
            "family",
            "final_state",
            "difficulty",
            "generation_call_id",
            "generation_rejections",
            "graded",
            "input_tokens",
            "invocation_id",
            "invocation_started_at",
            "iteration",
            "kind",
            "live_llm",
            "live_mcp",
            "llm_calls",
            "model",
            "model_role",
            "observation_ref",
            "observation_id",
            "observations",
            "ok",
            "outcome",
            "output_tokens",
            "planner_context_chars",
            "planner_input_tokens",
            "probe_arguments",
            "probe_tool",
            "proposed_probe",
            "ranking_after",
            "ranking_before",
            "record_kind",
            "recorded_world_id",
            "result",
            "reward",
            "role",
            "safety_violated",
            "scenario",
            "schema_version",
            "scores",
            "seed",
            "selected_candidate_id",
            "selector",
            "sequence",
            "spec_version",
            "step_id",
            "strategy",
            "template_id",
            "tokens_used",
            "tool_calls_used",
            "tool_name",
            "total",
            "trace_file",
            "trajectory_id",
            "uncertainty",
            "usd_used",
            "weights",
        }

    def test_a_decision_still_says_what_was_decided(self, traces: list[Path]) -> None:
        """Dropping prose must not drop the decision: this is what training reads."""
        built = _build(traces, _corpus(_scenario("lag_dev", truth=_TRUTH)))
        decision = built.trajectories[0].decisions[0]
        assert (decision.action_kind, decision.probe_tool) == ("probe", "get_consumer_lag")
        assert decision.candidates[0].category == HypothesisCategory.CONSUMER_SATURATION.value
        assert decision.candidates[0].confidence == pytest.approx(0.82)
        assert decision.selector is not None and decision.selector.decision == "select"
        assert decision.ranking_after[0].category == HypothesisCategory.CONSUMER_SATURATION.value
        assert decision.llm_calls[0].usd_used == "0.004000"


class TestReproducibleAndNonDestructive:
    def test_the_same_traces_render_the_same_bytes(self, traces: list[Path]) -> None:
        corpus = _corpus(_scenario("lag_dev", truth=_TRUTH))
        first, second = _build(traces, corpus), _build(traces, corpus)
        assert first.trajectories_jsonl == second.trajectories_jsonl
        assert first.labels_jsonl == second.labels_jsonl
        assert first.manifest_json == second.manifest_json

    def test_the_data_carries_no_clock_of_its_own(self, traces: list[Path]) -> None:
        """Only the manifest moves when the export is re-run under a different stamp."""
        corpus = _corpus(_scenario("lag_dev", truth=_TRUTH))
        first = _build(traces, corpus)
        later = _build(
            traces,
            corpus,
            timestamp=datetime(2027, 1, 1, tzinfo=UTC),
            invocation_id="ffffeeeedddd",
        )
        assert first.trajectories_jsonl == later.trajectories_jsonl
        assert first.labels_jsonl == later.labels_jsonl
        assert first.manifest_json != later.manifest_json

    def test_the_traces_are_untouched_by_an_export(
        self, traces: list[Path], tmp_path: Path
    ) -> None:
        """Invariant 9: the export READS evidence. F-002 is what consuming it looks like."""
        before = {
            path: (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_size)
            for path in traces
        }
        built = _build(traces, _corpus(_scenario("lag_dev", truth=_TRUTH)))
        export.write_export(built, timestamp=_WHEN, invocation_id=_INVOCATION, root=tmp_path)
        after = {
            path: (path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_size)
            for path in traces
        }
        assert before == after

    def test_a_second_write_at_the_same_version_raises(
        self, traces: list[Path], tmp_path: Path
    ) -> None:
        built = _build(traces, _corpus(_scenario("lag_dev", truth=_TRUTH)))
        export.write_export(built, timestamp=_WHEN, invocation_id=_INVOCATION, root=tmp_path)
        with pytest.raises(FileExistsError):
            export.write_export(built, timestamp=_WHEN, invocation_id=_INVOCATION, root=tmp_path)

    def test_both_attempts_in_one_trace_file_become_two_trajectories(
        self, traces: list[Path]
    ) -> None:
        """Attempts are separated by `invocation_id`, never collapsed (invariant 9)."""
        built = _build(traces, _corpus(_scenario("lag_dev", truth=_TRUTH)))
        assert [record.trajectory_id for record in built.trajectories] == [
            "lag_dev:inv000000001",
            "lag_dev:inv000000002",
        ]

    def test_an_attempt_with_no_end_record_is_marked_incomplete(self, tmp_path: Path) -> None:
        directory = tmp_path / "traces"
        directory.mkdir(parents=True)
        path = directory / "lag_dev.jsonl"
        with path.open("w", encoding="utf-8") as handle:
            for record in _records("lag_dev", invocation="inv000000009", complete=False):
                handle.write(json.dumps(record) + "\n")
        built = _build([path], _corpus(_scenario("lag_dev", truth=_TRUTH)))
        assert built.trajectories[0].outcome.complete is False
        assert built.manifest.complete_count == 0

    def test_a_record_without_an_invocation_id_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "lag_dev.jsonl"
        path.write_text(json.dumps({"kind": "mcp", "record_id": "x"}) + "\n", encoding="utf-8")
        with pytest.raises(export.ExportError, match="invocation_id"):
            export.read_trace_file(path)

    def test_a_malformed_line_names_its_own_line_number(self, tmp_path: Path) -> None:
        path = tmp_path / "lag_dev.jsonl"
        path.write_text("{not json}\n", encoding="utf-8")
        with pytest.raises(export.ExportError, match=re.escape(f"{path}:1")):
            export.read_trace_file(path)
