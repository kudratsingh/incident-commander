"""Trajectory export for a later training stage (plan 03 § 16.4, WP-15.1).

One JSONL line per trajectory: observation REFS rather than tool output (invariant 4),
structured decisions, actions and results. Evaluator labels go to a separate
``.labels.jsonl`` no training path loads. It REFUSES a holdout template by name, records
every ``template_id`` it emitted, and only ever READS the append-only trace store.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

from evals import artifacts
from evals.scenarios.loader import load_scenarios
from evals.scenarios.schema import BenchmarkSplit, Scenario
from evals.tracing import TraceKind

#: The three artifact families this module writes, in ``evals/artifacts.py::KINDS``.
TRAJECTORY_KIND: Final[str] = "training_export"
LABELS_KIND: Final[str] = "training_export_labels"
MANIFEST_KIND: Final[str] = "training_export_manifest"

#: Bumped when a field is added, removed or re-meant. On the line, so a mixed
#: directory of exports is still readable one line at a time.
SCHEMA_VERSION: Final[int] = 1

SCENARIO_DIRECTORY: Final[Path] = Path(__file__).parent / "scenarios"
DEFAULT_TRACE_DIRECTORY: Final[Path] = Path(__file__).parent / "traces"
_TRACE_DIR_ENV: Final[str] = "EVAL_TRACE_DIR"

#: What plan 03 § 16.4 and plan 02 § 7 ask a run-level record to carry that this export
#: cannot fill from the trace store, and why. In the MANIFEST rather than as a null field
#: on every line: a gap explained once is auditable, and a null repeated 500 times reads
#: as a value. Filling one of these is a follow-up order, not a guess made here.
ABSENT_FIELDS: Final[dict[str, str]] = {
    "execution_mode": (
        "the trace store's scenario_start record does not say which mode produced the "
        "run, and a recorded run is indistinguishable from a live one in it. live_mcp "
        "and live_llm are what it does record and are on the line; deriving "
        "'live' from them would label every recorded run wrongly."
    ),
    "recorded_world_id": (
        "same record, same gap: which recording a replay answered from is not traced. A "
        "recorded run's world is identified in its report row, not here."
    ),
    "reward_components": (
        "reward v0 is WP-15.2's packet. Computing components here would be a second "
        "definition of the reward beside that one, which is F-011's shape - the harness "
        "rewarding something other than what the spec says."
    ),
    "root_cause_grade": (
        "a grade is an evaluator label, so it is in the labels file, never in the training data."
    ),
}


class ExportError(RuntimeError):
    """Something about the export's promise does not hold. Never a warning."""


class HoldoutExportError(ExportError):
    """A held-out template reached the export. Refused by name (plan 03 § 4)."""


class TrainingContaminationError(ExportError):
    """A template this export emitted is about to be scored as if it were unseen."""


class ObservationRef(BaseModel):
    """Where one observation lives in the trace store, and what it was - never its text."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    observation_id: str
    trace_file: str
    kind: str
    tool_name: str
    ok: bool
    content_sha256: str
    content_bytes: int


class ActionResult(BaseModel):
    """What one action returned, as a reference to the observation it produced."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    observation_ref: str
    ok: bool
    duration_seconds: float | None = None


class ActionRecord(BaseModel):
    """One tool call the agent made, bound to its own result by construction."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sequence: int
    tool_name: str
    arguments: dict[str, Any]
    result: ActionResult


class RankedCategory(BaseModel):
    """One entry of a hypothesis ranking, label and confidence only."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    category: str
    confidence: float


class CandidateExport(BaseModel):
    """One candidate diagnosis a strategy considered, without its free-text label."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate_id: str
    category: str
    confidence: float
    evidence_for: tuple[str, ...] = ()
    evidence_against: tuple[str, ...] = ()
    proposed_probe: str | None = None
    generation_call_id: str = ""


class SelectorExport(BaseModel):
    """The candidate selector's decision over one candidate set (ADR 0048)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    decision: str
    selected_candidate_id: str | None = None
    uncertainty: float | None = None
    scores: dict[str, float] = Field(default_factory=dict)


class LLMCallExport(BaseModel):
    """What one planner call billed, and the trace id of the call itself."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: str
    model: str
    tokens_used: int
    usd_used: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    call_id: str = ""
    elapsed_ms: int | None = None


class DecisionRecord(BaseModel):
    """One planner step as a decision: what was considered, what was chosen."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    step_id: str
    iteration: int
    strategy: str
    model: str
    candidates: tuple[CandidateExport, ...] = ()
    selector: SelectorExport | None = None
    action_kind: str
    probe_tool: str | None = None
    probe_arguments: dict[str, Any] = Field(default_factory=dict)
    ranking_before: tuple[RankedCategory, ...] = ()
    ranking_after: tuple[RankedCategory, ...] = ()
    planner_input_tokens: int | None = None
    planner_context_chars: int | None = None
    llm_calls: tuple[LLMCallExport, ...] = ()
    generation_rejections: tuple[str, ...] = ()


class OutcomeRecord(BaseModel):
    """How the run ended, as the run itself reported it - not as it was graded."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    final_state: str | None = None
    tool_calls_used: int | None = None
    decision_count: int
    action_count: int
    complete: bool


class TrajectoryRecord(BaseModel):
    """One trajectory: the training-side line, with no label and no tool output."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = SCHEMA_VERSION
    record_kind: str = "trajectory"
    trajectory_id: str
    scenario: str
    template_id: str
    seed: int
    family: str | None = None
    difficulty: str | None = None
    benchmark_split: str
    invocation_id: str
    invocation_started_at: str | None = None
    agent_model: str | None = None
    model_role: str | None = None
    live_mcp: bool | None = None
    live_llm: bool | None = None
    strategy: str | None = None
    decisions: tuple[DecisionRecord, ...] = ()
    actions: tuple[ActionRecord, ...] = ()
    observations: tuple[ObservationRef, ...] = ()
    outcome: OutcomeRecord


class GroundTruthLabel(BaseModel):
    """The evaluator's answer key for one trajectory's world (ADR 0038, ADR 0040)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    incident_count: int
    root_causes: tuple[str, ...]
    affected_components: tuple[str, ...] = ()
    causal_chain: tuple[str, ...] = ()


class ProbeLabel(BaseModel):
    """One read that tells this world from its siblings, as (tool, argument pattern)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool: str
    argument_pattern: dict[str, str] = Field(default_factory=dict)


class ExpectationLabel(BaseModel):
    """What the scenario expects of a correct run, in labels rather than prose."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    expected_terminal_state: str
    expected_action_tools: tuple[str, ...] = ()
    forbidden_action_tools: tuple[str, ...] = ()
    max_tool_calls: int | None = None


class LabelRecord(BaseModel):
    """The evaluator's labels for one trajectory. Separate file, never the training set."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = SCHEMA_VERSION
    record_kind: str = "labels"
    trajectory_id: str
    scenario: str
    template_id: str
    benchmark_split: str
    ground_truth: GroundTruthLabel | None = None
    ground_truth_absent_reason: str | None = None
    discriminating_probes: tuple[ProbeLabel, ...] = ()
    expectation: ExpectationLabel
    passed: bool | None = None
    failure_class: str | None = None


class SourceTrace(BaseModel):
    """One trace file this export read, and its digest before and after reading."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    sha256: str
    bytes: int


class ManifestRecord(BaseModel):
    """What was exported, which templates it covers, and what is not in it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = SCHEMA_VERSION
    record_kind: str = "manifest"
    generated_at: str
    invocation_id: str
    trajectory_file: str
    labels_file: str
    trajectory_count: int
    complete_count: int
    #: The promise the report enforces: a policy is never scored on one of these
    #: (plan 03 § 4's third enforcement point, ``refuse_templates_seen_in_training``).
    exported_template_ids: tuple[str, ...]
    exported_scenarios: tuple[str, ...]
    splits_present: tuple[str, ...]
    holdout_template_ids_in_corpus: tuple[str, ...]
    source_traces: tuple[SourceTrace, ...]
    trajectories_sha256: str
    trajectories_bytes: int
    labels_sha256: str
    labels_bytes: int
    labels_are_evaluator_only: bool = True
    absent_fields: dict[str, str] = Field(default_factory=dict)


@dataclass(frozen=True)
class Export:
    """A rendered export: three byte strings and the records behind them."""

    trajectories: tuple[TrajectoryRecord, ...]
    labels: tuple[LabelRecord, ...]
    manifest: ManifestRecord
    trajectories_jsonl: str
    labels_jsonl: str
    manifest_json: str


@dataclass(frozen=True)
class ExportPaths:
    """Where one written export landed."""

    trajectories: Path
    labels: Path
    manifest: Path


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical(value: Any) -> bytes:
    """A stable byte rendering of one JSON value, for digesting only."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()


def _line(model: BaseModel) -> str:
    """One JSONL line: sorted keys, no spaces, so the same records are the same bytes."""
    return json.dumps(model.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class TraceInvocation:
    """The records of ONE attempt at one scenario, in the order they were written."""

    scenario: str
    invocation_id: str
    trace_file: str
    records: tuple[dict[str, Any], ...]

    def of_kind(self, kind: TraceKind) -> list[dict[str, Any]]:
        return [record for record in self.records if record.get("kind") == kind.value]

    def first(self, kind: TraceKind) -> dict[str, Any] | None:
        found = self.of_kind(kind)
        return found[0] if found else None

    def last(self, kind: TraceKind) -> dict[str, Any] | None:
        found = self.of_kind(kind)
        return found[-1] if found else None


def read_trace_file(path: Path) -> list[TraceInvocation]:
    """Split one trace file into its invocations, oldest first (invariant 9: read only)."""
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for number, raw in enumerate(handle, start=1):
            if not raw.strip():
                continue
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as err:
                raise ExportError(f"{path}:{number} is not JSON ({err})") from err
            if not isinstance(parsed, dict):
                raise ExportError(f"{path}:{number} is not a trace record object")
            records.append(parsed)

    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        invocation_id = record.get("invocation_id")
        if not isinstance(invocation_id, str) or not invocation_id:
            raise ExportError(
                f"{path} holds a record with no invocation_id. Attempts are separated by "
                "that id (invariant 9); without it two runs cannot be told apart."
            )
        grouped.setdefault(invocation_id, []).append(record)

    found: list[TraceInvocation] = []
    for invocation_id, group in grouped.items():
        named = {
            record["scenario"]
            for record in group
            if isinstance(record.get("scenario"), str) and record["scenario"]
        }
        if len(named) > 1:
            raise ExportError(
                f"{path} invocation {invocation_id} names more than one scenario "
                f"({sorted(named)}). One invocation is one attempt at one scenario."
            )
        scenario = named.pop() if named else path.name.split(".", 1)[0]
        found.append(
            TraceInvocation(
                scenario=scenario,
                invocation_id=invocation_id,
                trace_file=path.name,
                records=tuple(group),
            )
        )
    return found


def _ranking(entries: Any) -> tuple[RankedCategory, ...]:
    """A hypothesis ranking projected onto label and confidence. Free text is dropped."""
    if not isinstance(entries, list):
        return ()
    ranked: list[RankedCategory] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        category, confidence = entry.get("category"), entry.get("confidence")
        if isinstance(category, str) and isinstance(confidence, int | float):
            ranked.append(RankedCategory(category=category, confidence=float(confidence)))
    return tuple(ranked)


def _candidates(entries: Any) -> tuple[CandidateExport, ...]:
    """Every candidate the step considered. ``name`` is model prose and does not travel."""
    if not isinstance(entries, list):
        return ()
    found: list[CandidateExport] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        found.append(
            CandidateExport(
                candidate_id=str(entry.get("candidate_id", "")),
                category=str(entry.get("category", "")),
                confidence=float(entry.get("confidence", 0.0)),
                evidence_for=tuple(str(ref) for ref in entry.get("evidence_for", ())),
                evidence_against=tuple(str(ref) for ref in entry.get("evidence_against", ())),
                proposed_probe=entry.get("proposed_probe"),
                generation_call_id=str(entry.get("generation_call_id", "")),
            )
        )
    return tuple(found)


def _selector(payload: Any) -> SelectorExport | None:
    if not isinstance(payload, dict):
        return None
    scores = payload.get("scores")
    return SelectorExport(
        decision=str(payload.get("decision", "")),
        selected_candidate_id=payload.get("selected_candidate_id"),
        uncertainty=payload.get("uncertainty"),
        scores={str(key): float(value) for key, value in scores.items()}
        if isinstance(scores, dict)
        else {},
    )


def _llm_calls(entries: Any) -> tuple[LLMCallExport, ...]:
    if not isinstance(entries, list):
        return ()
    found: list[LLMCallExport] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        found.append(
            LLMCallExport(
                role=str(entry.get("role", "")),
                model=str(entry.get("model", "")),
                tokens_used=int(entry.get("tokens_used", 0)),
                usd_used=str(entry.get("usd_used", "0")),
                input_tokens=int(entry.get("input_tokens", 0)),
                output_tokens=int(entry.get("output_tokens", 0)),
                cache_read_tokens=int(entry.get("cache_read_tokens", 0)),
                cache_creation_tokens=int(entry.get("cache_creation_tokens", 0)),
                call_id=str(entry.get("call_id", "")),
                elapsed_ms=entry.get("elapsed_ms"),
            )
        )
    return tuple(found)


def _decision(record: Mapping[str, Any]) -> DecisionRecord:
    """One ``step`` trace record as a decision. ``reason`` and ``name`` stay behind."""
    emitted = record.get("emitted_step")
    emitted = emitted if isinstance(emitted, dict) else {}
    action = emitted.get("next_action")
    action = action if isinstance(action, dict) else {}
    arguments = action.get("arguments")
    return DecisionRecord(
        step_id=str(record.get("step_id", "")),
        iteration=int(record.get("iteration", 0)),
        strategy=str(record.get("strategy", "")),
        model=str(record.get("model", "")),
        candidates=_candidates(record.get("candidate_set")),
        selector=_selector(record.get("selector")),
        action_kind=str(action.get("kind", "")),
        probe_tool=action.get("tool_name"),
        probe_arguments=dict(arguments) if isinstance(arguments, dict) else {},
        ranking_before=_ranking(record.get("hypothesis_state_before")),
        ranking_after=_ranking(record.get("hypothesis_state_after")),
        planner_input_tokens=record.get("planner_input_tokens"),
        planner_context_chars=record.get("planner_context_chars"),
        llm_calls=_llm_calls(record.get("llm_calls")),
        generation_rejections=tuple(
            str(entry) for entry in record.get("generation_rejections", ())
        ),
    )


def _observation(record: Mapping[str, Any], *, trace_file: str) -> ObservationRef:
    """One tool call's result as a REF: where it is and what it hashes to, not what it said.

    The content is untrusted platform output (invariant 4) and inlining it would ship
    whatever a DLQ payload said into a training set. It stays in the append-only trace
    store, reachable by ``(trace_file, observation_id)`` and checkable by the digest.
    """
    ok = record.get("kind") == TraceKind.MCP.value
    content = record.get("result") if ok else record.get("error")
    payload = _canonical(content)
    return ObservationRef(
        observation_id=str(record.get("record_id", "")),
        trace_file=trace_file,
        kind=str(record.get("kind", "")),
        tool_name=str(record.get("tool_name", "")),
        ok=ok,
        content_sha256=_digest(payload),
        content_bytes=len(payload),
    )


def _actions(
    invocation: TraceInvocation,
) -> tuple[tuple[ActionRecord, ...], tuple[ObservationRef, ...]]:
    """The tool calls of one attempt, in order, each bound to the observation it produced."""
    calls = [
        record
        for record in invocation.records
        if record.get("kind") in {TraceKind.MCP.value, TraceKind.MCP_ERROR.value}
    ]
    actions: list[ActionRecord] = []
    observations: list[ObservationRef] = []
    for sequence, record in enumerate(calls):
        ref = _observation(record, trace_file=invocation.trace_file)
        observations.append(ref)
        arguments = record.get("arguments")
        actions.append(
            ActionRecord(
                sequence=sequence,
                tool_name=ref.tool_name,
                arguments=dict(arguments) if isinstance(arguments, dict) else {},
                result=ActionResult(
                    observation_ref=ref.observation_id,
                    ok=ref.ok,
                    duration_seconds=record.get("duration_seconds"),
                ),
            )
        )
    return tuple(actions), tuple(observations)


def _trajectory(invocation: TraceInvocation, scenario: Scenario) -> TrajectoryRecord:
    start = invocation.first(TraceKind.SCENARIO_START)
    end = invocation.last(TraceKind.SCENARIO_END)
    decisions = tuple(_decision(record) for record in invocation.of_kind(TraceKind.STEP))
    actions, observations = _actions(invocation)
    started_at = invocation.records[0].get("invocation_started_at")
    return TrajectoryRecord(
        trajectory_id=f"{invocation.scenario}:{invocation.invocation_id}",
        scenario=invocation.scenario,
        template_id=scenario.template_id,
        seed=scenario.seed,
        family=scenario.family.value if scenario.family else None,
        difficulty=scenario.difficulty.value if scenario.difficulty else None,
        benchmark_split=scenario.benchmark_split.value,
        invocation_id=invocation.invocation_id,
        invocation_started_at=str(started_at) if started_at is not None else None,
        agent_model=start.get("model") if start else None,
        model_role=start.get("model_role") if start else None,
        live_mcp=start.get("live_mcp") if start else None,
        live_llm=start.get("live_llm") if start else None,
        strategy=decisions[0].strategy if decisions else None,
        decisions=decisions,
        actions=actions,
        observations=observations,
        outcome=OutcomeRecord(
            final_state=end.get("final_state") if end else None,
            tool_calls_used=end.get("tool_calls_used") if end else None,
            decision_count=len(decisions),
            action_count=len(actions),
            complete=start is not None and end is not None,
        ),
    )


def _labels(invocation: TraceInvocation, scenario: Scenario) -> LabelRecord:
    end = invocation.last(TraceKind.SCENARIO_END)
    truth = scenario.ground_truth
    expectation = scenario.expectation
    return LabelRecord(
        trajectory_id=f"{invocation.scenario}:{invocation.invocation_id}",
        scenario=invocation.scenario,
        template_id=scenario.template_id,
        benchmark_split=scenario.benchmark_split.value,
        ground_truth=None
        if truth is None
        else GroundTruthLabel(
            incident_count=truth.incident_count,
            root_causes=tuple(cause.value for cause in truth.root_causes),
            affected_components=truth.affected_components,
            causal_chain=truth.causal_chain,
        ),
        ground_truth_absent_reason=None
        if truth is not None
        else "this scenario declares none, so it is not root-cause graded",
        discriminating_probes=tuple(
            ProbeLabel(tool=probe.tool, argument_pattern=dict(probe.argument_pattern))
            for probe in scenario.discriminating_probes
        ),
        expectation=ExpectationLabel(
            expected_terminal_state=expectation.expected_terminal_state.value,
            expected_action_tools=expectation.expected_action_tools,
            forbidden_action_tools=expectation.forbidden_action_tools,
            max_tool_calls=expectation.max_tool_calls,
        ),
        passed=end.get("passed") if end else None,
        failure_class=end.get("failure_class") if end else None,
    )


def holdout_template_ids(corpus: Iterable[Scenario]) -> frozenset[str]:
    """Every ``template_id`` the corpus holds out. The set the export refuses to emit."""
    return frozenset(
        scenario.template_id
        for scenario in corpus
        if scenario.benchmark_split is BenchmarkSplit.HOLDOUT
    )


def _refuse_holdout(pairs: Sequence[tuple[str, Scenario]], held_out: frozenset[str]) -> None:
    """Refuse the whole export when any trajectory's template is held out.

    A hard error naming each one, not a skip: a filter would emit the rest and leave the
    holdout promise (plan 03 § 4) resting on nobody noticing a warning.
    """
    offending = sorted(
        {
            (scenario.name, scenario.template_id)
            for _, scenario in pairs
            if scenario.benchmark_split is BenchmarkSplit.HOLDOUT
            or scenario.template_id in held_out
        }
    )
    if not offending:
        return
    named = ", ".join(f"{name} (template {template})" for name, template in offending)
    raise HoldoutExportError(
        f"refusing to export {len(offending)} held-out scenario(s): {named}. A holdout "
        "template is never trained on, and every instance of it is held out (plan 03 § 4, "
        "plan 06 D7). Nothing was written. Drop these traces from the input, or move the "
        "template out of the holdout split deliberately."
    )


def build_export(
    *,
    traces: Sequence[Path],
    corpus: Iterable[Scenario],
    timestamp: datetime,
    invocation_id: str,
) -> Export:
    """Render an export from trace files and the corpus. Writes nothing, mutates nothing.

    A pure function of its inputs: the same traces under the same stamp and id render the
    same bytes, and the trajectory and label lines carry no clock of their own at all.
    """
    by_name = {scenario.name: scenario for scenario in corpus}
    held_out = holdout_template_ids(by_name.values())

    invocations: list[TraceInvocation] = []
    sources: list[SourceTrace] = []
    for path in sorted(traces):
        payload = path.read_bytes()
        sources.append(SourceTrace(path=path.name, sha256=_digest(payload), bytes=len(payload)))
        invocations.extend(read_trace_file(path))

    unknown = sorted({found.scenario for found in invocations if found.scenario not in by_name})
    if unknown:
        raise ExportError(
            f"the traces name scenarios the corpus does not hold: {unknown}. Their split "
            "is unknown, so the export cannot show they are not held out; it refuses "
            "rather than assume."
        )

    ordered = sorted(invocations, key=lambda found: (found.scenario, found.invocation_id))
    pairs = [(found.invocation_id, by_name[found.scenario]) for found in ordered]
    _refuse_holdout(pairs, held_out)

    trajectories = tuple(_trajectory(found, by_name[found.scenario]) for found in ordered)
    labels = tuple(_labels(found, by_name[found.scenario]) for found in ordered)
    trajectories_jsonl = "".join(f"{_line(record)}\n" for record in trajectories)
    labels_jsonl = "".join(f"{_line(record)}\n" for record in labels)

    manifest = ManifestRecord(
        generated_at=artifacts.stamp(timestamp),
        invocation_id=invocation_id,
        trajectory_file=artifacts.version_name(
            TRAJECTORY_KIND, timestamp=timestamp, invocation_id=invocation_id
        ),
        labels_file=artifacts.version_name(
            LABELS_KIND, timestamp=timestamp, invocation_id=invocation_id
        ),
        trajectory_count=len(trajectories),
        complete_count=sum(1 for record in trajectories if record.outcome.complete),
        exported_template_ids=tuple(sorted({record.template_id for record in trajectories})),
        exported_scenarios=tuple(sorted({record.scenario for record in trajectories})),
        splits_present=tuple(sorted({record.benchmark_split for record in trajectories})),
        holdout_template_ids_in_corpus=tuple(sorted(held_out)),
        source_traces=tuple(sources),
        trajectories_sha256=_digest(trajectories_jsonl.encode()),
        trajectories_bytes=len(trajectories_jsonl.encode()),
        labels_sha256=_digest(labels_jsonl.encode()),
        labels_bytes=len(labels_jsonl.encode()),
        absent_fields=dict(ABSENT_FIELDS),
    )
    return Export(
        trajectories=trajectories,
        labels=labels,
        manifest=manifest,
        trajectories_jsonl=trajectories_jsonl,
        labels_jsonl=labels_jsonl,
        manifest_json=json.dumps(manifest.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
    )


def write_export(
    export: Export,
    *,
    timestamp: datetime,
    invocation_id: str,
    directory: Path | None = None,
    root: Path | None = None,
) -> ExportPaths:
    """Persist the three files, exclusive-create, never overwriting a prior export."""
    written = {
        kind: artifacts.write_versioned(
            kind,
            content=content,
            timestamp=timestamp,
            invocation_id=invocation_id,
            directory=directory,
            root=root,
        )
        for kind, content in (
            (TRAJECTORY_KIND, export.trajectories_jsonl),
            (LABELS_KIND, export.labels_jsonl),
            (MANIFEST_KIND, export.manifest_json),
        )
    }
    return ExportPaths(
        trajectories=written[TRAJECTORY_KIND],
        labels=written[LABELS_KIND],
        manifest=written[MANIFEST_KIND],
    )


def load_manifest(path: Path) -> ManifestRecord:
    """Read one export manifest. The labels file is NOT read - that takes asking for it."""
    return ManifestRecord.model_validate_json(path.read_text(encoding="utf-8"))


def refuse_templates_seen_in_training(
    template_ids: Iterable[str],
    *,
    manifests: Sequence[Path] | None = None,
    root: Path | None = None,
    subject: str = "a policy",
) -> None:
    """Plan 03 § 4's third gate: a scored template must never have been exported.

    ``manifests`` defaults to the newest export's; nothing exported yet is a pass, not an
    error. Raises rather than filtering, for the reason the export itself raises.
    """
    if manifests is None:
        newest = artifacts.newest_or_none(MANIFEST_KIND, root=root)
        manifests = [newest] if newest is not None else []
    exported: set[str] = set()
    for path in manifests:
        exported |= set(load_manifest(path).exported_template_ids)
    overlap = sorted(set(template_ids) & exported)
    if overlap:
        raise TrainingContaminationError(
            f"refusing to score {subject} on {len(overlap)} template(s) the training "
            f"export already emitted: {overlap}. A number measured on a template the "
            "policy trained on measures memorisation (plan 06 D7). Score it on a "
            "template no export covers, or say plainly that this is a training-set score."
        )


def _trace_directory() -> Path:
    override = os.environ.get(_TRACE_DIR_ENV)
    return Path(override) if override else DEFAULT_TRACE_DIRECTORY


def _main(argv: list[str] | None = None) -> int:
    """``python -m evals.export --write`` - render, then persist on request."""
    parser = argparse.ArgumentParser(prog="python -m evals.export", description=__doc__)
    parser.add_argument(
        "--trace-dir",
        type=Path,
        default=None,
        help=f"directory of <scenario>.jsonl trace files (default: ${_TRACE_DIR_ENV} "
        "or evals/traces)",
    )
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="SCENARIO",
        help="export one scenario's traces; repeatable. Default: every trace file",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="persist the export (default: report what it would contain and stop)",
    )
    args = parser.parse_args(argv)

    trace_dir = args.trace_dir if args.trace_dir is not None else _trace_directory()
    if not trace_dir.is_dir():
        print(f"no trace directory at {trace_dir}", file=sys.stderr)
        return 1
    wanted: list[str] = list(args.only)
    traces = sorted(
        path
        for path in trace_dir.glob("*.jsonl")
        if not wanted or path.name.split(".", 1)[0] in wanted
    )
    if not traces:
        print(f"no trace files under {trace_dir}", file=sys.stderr)
        return 1

    timestamp = datetime.now(UTC)
    invocation_id = uuid.uuid4().hex[:12]
    try:
        export = build_export(
            traces=traces,
            corpus=load_scenarios(SCENARIO_DIRECTORY),
            timestamp=timestamp,
            invocation_id=invocation_id,
        )
    except ExportError as err:
        print(str(err), file=sys.stderr)
        return 1

    manifest = export.manifest
    print(
        f"{manifest.trajectory_count} trajectories "
        f"({manifest.complete_count} complete) from {len(traces)} trace file(s); "
        f"{len(manifest.exported_template_ids)} template(s), "
        f"splits {list(manifest.splits_present)}"
    )
    if not args.write:
        print("nothing written (pass --write to persist)")
        return 0
    paths = write_export(export, timestamp=timestamp, invocation_id=invocation_id)
    print(f"trajectories: {paths.trajectories}")
    print(f"labels (evaluator-only): {paths.labels}")
    print(f"manifest: {paths.manifest}")
    return 0


if __name__ == "__main__":  # pragma: no cover - thin CLI wrapper
    raise SystemExit(_main())
