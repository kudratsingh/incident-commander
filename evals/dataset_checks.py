"""Quality checks over a training export, able to FAIL it (WP-15.3, plan 03 § 16.4).

Reads what ``evals/export.py`` wrote and answers one question: is this dataset usable.
A checker that only annotates is a footnote, so every finding carries a class and a
severity and the CLI exits non-zero on a blocking one. What it cannot classify it
reports as ``unclassified`` rather than guessing benign (LESSONS 2026-09-07).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, get_args

from pydantic import BaseModel, ConfigDict, Field

from evals import artifacts, export, reward
from evals.scenarios.loader import load_scenarios
from evals.scenarios.schema import BenchmarkSplit, Scenario
from evals.tracing import TraceKind
from incident_commander.tools.policies import Tier, tier_of
from incident_commander.tools.registry import TOOL_REGISTRY


class CheckName(StrEnum):
    """The nine classes plan 04 § WP-15.3 names, plus the manifest's own integrity."""

    MISSING_RESULT = "missing_result"
    INCOMPLETE_TRAJECTORY = "incomplete_trajectory"
    LEAKED_HIDDEN_TRUTH = "leaked_hidden_truth"
    SCENARIO_DRIFT = "scenario_drift"
    DUPLICATE = "duplicate"
    INVALID_REWARD = "invalid_reward"
    ACTION_RESULT_MISMATCH = "action_result_mismatch"
    HOLDOUT_CONTAMINATION = "holdout_contamination"
    MANIFEST_DISAGREEMENT = "manifest_disagreement"


class Severity(StrEnum):
    """How a finding bears on usability. ``UNCLASSIFIED`` is a refusal to guess."""

    BLOCKING = "blocking"
    UNCLASSIFIED = "unclassified"
    ADVISORY = "advisory"


#: Both of these fail the dataset. ``UNCLASSIFIED`` is in here deliberately: two live reds
#: were auto-labelled harmless in one day and one of them was a real agent finding
#: (LESSONS 2026-09-07, WO-R2-138), so "nobody could classify it" is not evidence that it
#: is fine. ``ADVISORY`` states something a reader of the dataset needs — which layer did
#: not run, or which benchmark key names its own fault — and never classifies a defect.
FAILING_SEVERITIES: Final[frozenset[Severity]] = frozenset(
    {Severity.BLOCKING, Severity.UNCLASSIFIED}
)


def _nested_models(annotation: Any) -> Iterator[type[BaseModel]]:
    """Every model reachable through one annotation's type arguments."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        yield annotation
        return
    for argument in get_args(annotation):
        yield from _nested_models(argument)


def model_key_names(
    model: type[BaseModel], seen: frozenset[type[BaseModel]] = frozenset()
) -> frozenset[str]:
    """Every key name one model's JSON rendering can carry, nested models included."""
    if model in seen:
        return frozenset()
    names = set(model.model_fields)
    for field in model.model_fields.values():
        for nested in _nested_models(field.annotation):
            names |= model_key_names(nested, seen | {model})
    return frozenset(names)


#: The answer key's key NAMES, computed as "what the labels record carries and the
#: trajectory record does not". Derived from the export's own two models rather than
#: listed here, for WO-R3-185's reason: a hand-maintained exclusion list goes stale, and
#: it fails by OMISSION, which is silent. A label field added tomorrow joins this set on
#: its own, and a key living on both sides cannot be evidence of a leak.
HIDDEN_TRUTH_KEYS: Final[frozenset[str]] = model_key_names(export.LabelRecord) - model_key_names(
    export.TrajectoryRecord
)

#: The line's own benchmark keys. The export puts them there deliberately (ADR 0057, plan
#: 03 § 15) and the agent never sees one, so a template NAMED after its fault is a fact
#: about the CORPUS rather than a leak in the export — it is reported as an advisory
#: instead of failing every dataset the corpus has ever produced. Each is pinned to a
#: field of ``TrajectoryRecord`` by ``test_dataset_checks.py``.
BENCHMARK_KEY_PATHS: Final[frozenset[tuple[str, ...]]] = frozenset(
    {("scenario",), ("template_id",), ("family",), ("difficulty",), ("benchmark_split",)}
)

#: The one key where a root-cause label is the agent's OWN claim rather than the answer
#: key: the ``category`` slot of the two models the export fills from the ranking
#: (``RankedCategory``, ``CandidateExport``). Named once and pinned to both models by
#: ``test_dataset_checks.py``, so a rename breaks a test instead of going quiet.
CLAIM_KEY: Final[str] = "category"

#: What ``export._observation`` digests when the trace record carried neither a result nor
#: an error: the canonical rendering of ``None``. An observation hashing to this is a tool
#: call whose result is MISSING, which no field on the line says out loud. Pinned to the
#: real export by ``test_dataset_checks.py::TestMissingToolResults``.
MISSING_CONTENT_SHA256: Final[str] = hashlib.sha256(b"null").hexdigest()

AUDIT_NOT_SUPPLIED: Final[str] = (
    "not checked: no platform audit window was supplied, so no action on any line was "
    "compared with what the platform recorded. The audit log is ground truth for what an "
    "agent DID (invariant 6) and an offline or recorded run has none. Pass --audit, or "
    "read this dataset as unverified on action correctness."
)

SCENARIO_DIRECTORY: Final[Path] = Path(__file__).parent / "scenarios"


class Finding(BaseModel):
    """One thing wrong with the dataset, the record it is wrong on, and how bad."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    check: CheckName
    severity: Severity
    #: The trajectory id where there is one, else the file and line, so a finding
    #: always names something a human can open.
    record: str
    detail: str


class QualityReport(BaseModel):
    """Every finding over one export, with counts per class and what was covered."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    manifest: str
    trajectory_count: int
    label_count: int
    exported_template_ids: tuple[str, ...]
    findings: tuple[Finding, ...] = ()
    counts_by_check: dict[str, int] = Field(default_factory=dict)
    counts_by_severity: dict[str, int] = Field(default_factory=dict)
    trajectories_with_an_audit_window: int = 0
    traces_were_read: bool = False

    @property
    def failing(self) -> tuple[Finding, ...]:
        return tuple(finding for finding in self.findings if finding.severity in FAILING_SEVERITIES)

    @property
    def failed(self) -> bool:
        """True when the dataset is not usable as it stands."""
        return bool(self.failing)

    def render(self) -> str:
        """The report an operator reads, verdict first."""
        lines = [
            f"dataset checks: {'FAIL' if self.failed else 'PASS'} — {self.manifest}",
            f"{self.trajectory_count} trajectory line(s), {self.label_count} label line(s), "
            f"{len(self.exported_template_ids)} template(s): "
            f"{list(self.exported_template_ids)}",
            f"audit windows: {self.trajectories_with_an_audit_window} of "
            f"{self.trajectory_count} line(s); trace store read: "
            f"{'yes' if self.traces_were_read else 'no'}",
        ]
        for severity in Severity:
            lines.append(f"{severity.value}: {self.counts_by_severity.get(severity.value, 0)}")
        for check in CheckName:
            count = self.counts_by_check.get(check.value, 0)
            if count:
                lines.append(f"  {check.value}: {count}")
        for finding in self.findings:
            lines.append(
                f"[{finding.severity.value}] {finding.check.value} — "
                f"{finding.record}: {finding.detail}"
            )
        return "\n".join(lines) + "\n"


def _report(
    dataset: Dataset, findings: Sequence[Finding], *, audited: int, traces_read: bool
) -> QualityReport:
    by_check: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    for finding in findings:
        by_check[finding.check.value] = by_check.get(finding.check.value, 0) + 1
        by_severity[finding.severity.value] = by_severity.get(finding.severity.value, 0) + 1
    return QualityReport(
        manifest=str(dataset.manifest_path),
        trajectory_count=len(dataset.trajectory_lines),
        label_count=len(dataset.label_lines),
        exported_template_ids=tuple(sorted(dataset.manifest.exported_template_ids)),
        findings=tuple(findings),
        counts_by_check=by_check,
        counts_by_severity=by_severity,
        trajectories_with_an_audit_window=audited,
        traces_were_read=traces_read,
    )


# --- reading the export --------------------------------------------------------


class DatasetReadError(RuntimeError):
    """The export's three files could not be read at all. Never a finding."""


@dataclass(frozen=True)
class TrajectoryLine:
    """One line of the training file: its bytes, its JSON, and the model or the refusal."""

    number: int
    payload: dict[str, Any]
    record: export.TrajectoryRecord | None
    error: str | None

    @property
    def trajectory_id(self) -> str:
        """The id as the LINE spells it, which is what a finding has to name."""
        found = self.payload.get("trajectory_id")
        return found if isinstance(found, str) and found else f"line {self.number}"


@dataclass(frozen=True)
class LabelLine:
    """One line of the labels file, read the same way."""

    number: int
    payload: dict[str, Any]
    record: export.LabelRecord | None
    error: str | None

    @property
    def trajectory_id(self) -> str:
        found = self.payload.get("trajectory_id")
        return found if isinstance(found, str) and found else f"labels line {self.number}"


@dataclass(frozen=True)
class Dataset:
    """One export as read off disk: the manifest, the two files, and their bytes."""

    manifest_path: Path
    manifest: export.ManifestRecord
    trajectories_path: Path
    labels_path: Path
    trajectory_bytes: bytes
    label_bytes: bytes
    trajectory_lines: tuple[TrajectoryLine, ...]
    label_lines: tuple[LabelLine, ...]

    def records(self) -> Iterator[tuple[TrajectoryLine, export.TrajectoryRecord]]:
        """The lines that parsed, so a caller never re-checks for ``None``."""
        for line in self.trajectory_lines:
            if line.record is not None:
                yield line, line.record

    def labels_by_trajectory(self) -> dict[str, export.LabelRecord]:
        return {
            line.record.trajectory_id: line.record
            for line in self.label_lines
            if line.record is not None
        }


def _json_lines(path: Path) -> list[tuple[int, dict[str, Any]]]:
    found: list[tuple[int, dict[str, Any]]] = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as err:
            raise DatasetReadError(f"{path}:{number} is not JSON ({err})") from err
        if not isinstance(parsed, dict):
            raise DatasetReadError(f"{path}:{number} is not a JSON object")
        found.append((number, parsed))
    return found


def load_dataset(manifest_path: Path) -> Dataset:
    """Read one export from its manifest, keeping raw JSON beside the parsed models.

    The raw payload is what the leak walk reads: a line carrying an extra evaluator key
    is exactly what ``extra="forbid"`` refuses, so validating first would hide it.
    """
    manifest = export.load_manifest(manifest_path)
    trajectories_path = manifest_path.parent / manifest.trajectory_file
    labels_path = manifest_path.parent / manifest.labels_file
    for path in (trajectories_path, labels_path):
        if not path.is_file():
            raise DatasetReadError(
                f"{manifest_path} names {path.name}, which is not beside it. The three "
                "files of an export are read together; a dataset missing its labels "
                "cannot be checked against its own answer key."
            )

    trajectory_lines: list[TrajectoryLine] = []
    for number, payload in _json_lines(trajectories_path):
        record, error = None, None
        try:
            record = export.TrajectoryRecord.model_validate(payload)
        except ValueError as err:
            error = str(err)
        trajectory_lines.append(TrajectoryLine(number, payload, record, error))

    label_lines: list[LabelLine] = []
    for number, payload in _json_lines(labels_path):
        label, label_error = None, None
        try:
            label = export.LabelRecord.model_validate(payload)
        except ValueError as err:
            label_error = str(err)
        label_lines.append(LabelLine(number, payload, label, label_error))

    return Dataset(
        manifest_path=manifest_path,
        manifest=manifest,
        trajectories_path=trajectories_path,
        labels_path=labels_path,
        trajectory_bytes=trajectories_path.read_bytes(),
        label_bytes=labels_path.read_bytes(),
        trajectory_lines=tuple(trajectory_lines),
        label_lines=tuple(label_lines),
    )


# --- walking a line -----------------------------------------------------------


def key_paths(value: Any, path: tuple[str, ...] = ()) -> Iterator[tuple[str, ...]]:
    """Every key path in a JSON value, however deep. List indices are not keys."""
    if isinstance(value, Mapping):
        for key, child in value.items():
            yield (*path, str(key))
            yield from key_paths(child, (*path, str(key)))
    elif isinstance(value, list):
        for child in value:
            yield from key_paths(child, path)


def strings_in(value: Any, path: tuple[str, ...] = ()) -> Iterator[tuple[tuple[str, ...], str]]:
    """Every string in a JSON value, with the key path that reaches it."""
    if isinstance(value, Mapping):
        for key, child in value.items():
            yield from strings_in(child, (*path, str(key)))
    elif isinstance(value, list):
        for child in value:
            yield from strings_in(child, path)
    elif isinstance(value, str):
        yield path, value


def tier_1_actions(record: export.TrajectoryRecord) -> tuple[str, ...]:
    """The Tier-1 tools one line says fired. Mirrors what the export claims per line."""
    return tuple(
        sorted(
            {
                action.tool_name
                for action in record.actions
                if action.tool_name in TOOL_REGISTRY and tier_of(action.tool_name) is Tier.TIER_1
            }
        )
    )


# --- the checks ---------------------------------------------------------------


def _unreadable_lines(dataset: Dataset) -> list[Finding]:
    """A line the export's own model refuses: a leak if it carries the answer key."""
    found: list[Finding] = []
    for line in dataset.trajectory_lines:
        if line.error is None:
            continue
        leaked = sorted(
            {path[-1] for path in key_paths(line.payload) if path[-1] in HIDDEN_TRUTH_KEYS}
        )
        if leaked:
            continue  # the leak check reports this line, with the keys it carries
        found.append(
            Finding(
                check=CheckName.MANIFEST_DISAGREEMENT,
                severity=Severity.UNCLASSIFIED,
                record=line.trajectory_id,
                detail=(
                    f"TrajectoryRecord refuses this line ({line.error}). That is a schema "
                    "bump, a hand edit or a corrupt write, and nothing here can tell them "
                    "apart — so it is unclassified rather than waved through."
                ),
            )
        )
    for label_line in dataset.label_lines:
        if label_line.error is None:
            continue
        found.append(
            Finding(
                check=CheckName.MANIFEST_DISAGREEMENT,
                severity=Severity.UNCLASSIFIED,
                record=label_line.trajectory_id,
                detail=f"LabelRecord refuses this labels line ({label_line.error}).",
            )
        )
    return found


def observations_by_id(record: export.TrajectoryRecord) -> dict[str, export.ObservationRef]:
    """The line's observations by id, dropping ids that identify nothing.

    An empty id is no identity, and a repeated one is not one either: a trace store written
    before ``record_id`` existed gives every observation the same blank id, and resolving
    an action through it would silently bind it to whichever reading came last.
    """
    counts: dict[str, int] = {}
    for observation in record.observations:
        counts[observation.observation_id] = counts.get(observation.observation_id, 0) + 1
    return {
        observation.observation_id: observation
        for observation in record.observations
        if observation.observation_id and counts[observation.observation_id] == 1
    }


def _unidentified(record: export.TrajectoryRecord) -> tuple[export.ObservationRef, ...]:
    """Observations no action can be bound to, because their id names more than one."""
    resolvable = observations_by_id(record)
    return tuple(
        observation
        for observation in record.observations
        if observation.observation_id not in resolvable
    )


def check_missing_results(dataset: Dataset) -> list[Finding]:
    """A tool call whose result is absent, unreachable, or nothing's result."""
    found: list[Finding] = []
    for line, record in dataset.records():
        by_id = observations_by_id(record)
        unidentified = _unidentified(record)
        if unidentified:
            found.append(
                Finding(
                    check=CheckName.MISSING_RESULT,
                    severity=Severity.BLOCKING,
                    record=line.trajectory_id,
                    detail=(
                        f"{len(unidentified)} of {len(record.observations)} observation(s) "
                        f"carry an id that identifies nothing (blank, or shared with another "
                        f"reading): {sorted({o.tool_name for o in unidentified})}. No action "
                        "on this line can be bound to its own result, so every result on it "
                        "is missing in the only sense that matters."
                    ),
                )
            )
        referenced: set[str] = set()
        for action in record.actions:
            ref = action.result.observation_ref
            referenced.add(ref)
            if unidentified and ref not in by_id:
                continue  # the one finding above already says why nothing resolves
            if ref not in by_id:
                found.append(
                    Finding(
                        check=CheckName.MISSING_RESULT,
                        severity=Severity.BLOCKING,
                        record=line.trajectory_id,
                        detail=(
                            f"action {action.sequence} ({action.tool_name}) names "
                            f"observation {ref!r}, which this line does not carry. The "
                            "action has no result at all."
                        ),
                    )
                )
        for observation in record.observations:
            if observation.content_sha256 == MISSING_CONTENT_SHA256:
                found.append(
                    Finding(
                        check=CheckName.MISSING_RESULT,
                        severity=Severity.BLOCKING,
                        record=line.trajectory_id,
                        detail=(
                            f"observation {observation.observation_id} "
                            f"({observation.tool_name}) digests the canonical rendering of "
                            "null, so the traced call recorded neither a result nor an "
                            "error. The ref resolves and there is nothing behind it."
                        ),
                    )
                )
            if observation.observation_id in by_id and observation.observation_id not in referenced:
                found.append(
                    Finding(
                        check=CheckName.MISSING_RESULT,
                        severity=Severity.BLOCKING,
                        record=line.trajectory_id,
                        detail=(
                            f"observation {observation.observation_id} "
                            f"({observation.tool_name}) is no action's result. The line "
                            "carries a reading nothing on it asked for."
                        ),
                    )
                )
    return found


def check_incomplete_trajectories(
    dataset: Dataset, *, traces: Sequence[Path] | None = None
) -> list[Finding]:
    """Incompleteness from the trace's scenario boundaries, not from a heuristic.

    ``outcome.complete`` IS that boundary check, carried on the line; ``traces`` re-derives
    it from the append-only store so the line's claim is checked rather than trusted.
    """
    found: list[Finding] = []
    boundaries: dict[str, bool] | None = None
    if traces is not None:
        boundaries = {}
        for path in sorted(traces):
            for invocation in export.read_trace_file(path):
                # Keyed by TRAJECTORY, not by invocation: one eval run writes ONE
                # invocation_id across every scenario's file, so keying by the id alone
                # collapses 31 trajectories into whichever file was read last.
                boundaries[f"{invocation.scenario}:{invocation.invocation_id}"] = (
                    invocation.first(TraceKind.SCENARIO_START) is not None
                    and invocation.last(TraceKind.SCENARIO_END) is not None
                )

    for line, record in dataset.records():
        if not record.outcome.complete:
            found.append(
                Finding(
                    check=CheckName.INCOMPLETE_TRAJECTORY,
                    severity=Severity.BLOCKING,
                    record=line.trajectory_id,
                    detail=(
                        "the line's own boundary check is false: its invocation has a "
                        "scenario_start or a scenario_end and not both. A half trajectory "
                        "in a training set is invisible once training starts."
                    ),
                )
            )
        if boundaries is None:
            continue
        traced = boundaries.get(record.trajectory_id)
        if traced is None:
            found.append(
                Finding(
                    check=CheckName.INCOMPLETE_TRAJECTORY,
                    severity=Severity.UNCLASSIFIED,
                    record=line.trajectory_id,
                    detail=(
                        f"invocation {record.invocation_id} is not in the trace store that "
                        "was read. That is a store pointed at the wrong directory, an "
                        "archived run, or a line with no trace behind it — unclassified, "
                        "because guessing which would be guessing the answer."
                    ),
                )
            )
        elif traced != record.outcome.complete:
            found.append(
                Finding(
                    check=CheckName.INCOMPLETE_TRAJECTORY,
                    severity=Severity.BLOCKING,
                    record=line.trajectory_id,
                    detail=(
                        f"the line says complete={record.outcome.complete} and the trace "
                        f"store's boundaries say {traced}. The line's claim about itself "
                        "disagrees with the append-only record it was built from."
                    ),
                )
            )
    return found


def agent_visible_text(scenario: Scenario) -> str:
    """Everything this scenario lets the agent see, rendered — WO-R3-185's projection.

    A string the agent can read is not evidence of a leak when it turns up in the agent's
    own output: a real component name is usually in the alert AND in the answer key.
    """
    return json.dumps(scenario.agent_visible().model_dump(mode="json"), sort_keys=True, default=str)


def check_hidden_truth(dataset: Dataset, *, corpus: Iterable[Scenario]) -> list[Finding]:
    """The answer key must not be on a training line — by key name or by value."""
    found: list[Finding] = []
    labels = dataset.labels_by_trajectory()
    visible = {scenario.name: agent_visible_text(scenario) for scenario in corpus}
    #: Benchmark keys that are their own answer: one advisory each, not one per line.
    named: dict[str, str] = {}
    for line in dataset.trajectory_lines:
        leaked = sorted(
            {path[-1] for path in key_paths(line.payload) if path[-1] in HIDDEN_TRUTH_KEYS}
        )
        for key in leaked:
            found.append(
                Finding(
                    check=CheckName.LEAKED_HIDDEN_TRUTH,
                    severity=Severity.BLOCKING,
                    record=line.trajectory_id,
                    detail=(
                        f"the training line carries the key {key!r}, which only the "
                        "evaluator's labels record has. Nested or not, a key of the answer "
                        "key on a training line is the answer in the training set."
                    ),
                )
            )
        label = labels.get(line.trajectory_id)
        truth = None if label is None else label.ground_truth
        readable = visible.get(label.scenario) if label is not None else None
        if truth is None or readable is None:
            # No answer key, or no scenario to say what the agent could read. The KEY scan
            # above still covers this line; guessing about its values would not.
            continue
        causes = {cause for cause in truth.root_causes if cause not in readable}
        secrets = {
            value
            for value in (*truth.affected_components, *truth.causal_chain)
            if value not in readable
        }
        for path, value in strings_in(line.payload):
            if value in secrets:
                found.append(
                    Finding(
                        check=CheckName.LEAKED_HIDDEN_TRUTH,
                        severity=Severity.BLOCKING,
                        record=line.trajectory_id,
                        detail=(
                            f"{'.'.join(path) or '<root>'} is {value!r}, which this "
                            "trajectory's ground truth names as an affected component or a "
                            "link in its causal chain, and which nothing the agent could "
                            "read ever mentions. It can only have come from the answer key."
                        ),
                    )
                )
            elif value not in causes or (path and path[-1] == CLAIM_KEY):
                continue
            elif path in BENCHMARK_KEY_PATHS:
                named.setdefault(f"{path[0]}={value}", line.trajectory_id)
            else:
                found.append(
                    Finding(
                        check=CheckName.LEAKED_HIDDEN_TRUTH,
                        severity=Severity.BLOCKING,
                        record=line.trajectory_id,
                        detail=(
                            f"{'.'.join(path) or '<root>'} is the root-cause label "
                            f"{value!r} this trajectory is graded on naming, outside the "
                            f"{CLAIM_KEY!r} slot where the agent states its own ranking. "
                            "The label is being handed to the line."
                        ),
                    )
                )
    for where, trajectory_id in sorted(named.items()):
        found.append(
            Finding(
                check=CheckName.LEAKED_HIDDEN_TRUTH,
                severity=Severity.ADVISORY,
                record=trajectory_id,
                detail=(
                    f"the benchmark key {where} is its own root-cause label. The export puts "
                    "the benchmark keys on the line on purpose and the agent never saw one, "
                    "so this is not a leak in the export — but a training stage that keys on "
                    "the name would read the answer off it. Group by template, never learn "
                    "from it."
                ),
            )
        )
    return found


def _drift(
    line: TrajectoryLine, record: export.TrajectoryRecord, scenario: Scenario
) -> list[Finding]:
    declared = {
        "template_id": (record.template_id, scenario.template_id),
        "seed": (record.seed, scenario.seed),
        "family": (record.family, scenario.family.value if scenario.family else None),
        "difficulty": (
            record.difficulty,
            scenario.difficulty.value if scenario.difficulty else None,
        ),
        "benchmark_split": (record.benchmark_split, scenario.benchmark_split.value),
    }
    return [
        Finding(
            check=CheckName.SCENARIO_DRIFT,
            severity=Severity.BLOCKING,
            record=line.trajectory_id,
            detail=(
                f"the line says {field}={exported!r} and the corpus says {current!r} for "
                f"scenario {record.scenario!r}. A benchmark key that moved after the export "
                "makes every number computed from this dataset a number about a world that "
                "no longer exists."
            ),
        )
        for field, (exported, current) in declared.items()
        if exported != current
    ]


def _label_drift(
    trajectory_id: str, label: export.LabelRecord, scenario: Scenario
) -> list[Finding]:
    """The labels line against ``reward.labels_of`` — the corpus projection, not a copy."""
    current = reward.labels_of(scenario)
    truth = label.ground_truth
    exported_causes = () if truth is None else truth.root_causes
    declared: dict[str, tuple[object, object]] = {
        "ground_truth.root_causes": (
            tuple(exported_causes),
            tuple(cause.value for cause in current.root_causes),
        ),
        "expectation.expected_action_tools": (
            label.expectation.expected_action_tools,
            current.sanctioned_action_tools,
        ),
        "expectation.forbidden_action_tools": (
            label.expectation.forbidden_action_tools,
            current.forbidden_action_tools,
        ),
        "expectation.max_tool_calls": (label.expectation.max_tool_calls, current.max_tool_calls),
        "discriminating_probes": (
            tuple(
                (probe.tool, tuple(sorted(probe.argument_pattern.items())))
                for probe in label.discriminating_probes
            ),
            tuple(
                (probe.tool, tuple(sorted(probe.argument_pattern.items())))
                for probe in current.discriminating_probes
            ),
        ),
    }
    return [
        Finding(
            check=CheckName.SCENARIO_DRIFT,
            severity=Severity.BLOCKING,
            record=trajectory_id,
            detail=(
                f"the labels line says {field}={exported!r} and the corpus projection "
                f"(reward.labels_of) says {now!r}. The answer key this dataset was labelled "
                "against is not the answer key today."
            ),
        )
        for field, (exported, now) in declared.items()
        if exported != now
    ]


def check_scenario_drift(dataset: Dataset, *, corpus: Iterable[Scenario]) -> list[Finding]:
    """The exported line against the corpus it names, and the labels against it too."""
    by_name = {scenario.name: scenario for scenario in corpus}
    labels = dataset.labels_by_trajectory()
    found: list[Finding] = []
    for line, record in dataset.records():
        scenario = by_name.get(record.scenario)
        if scenario is None:
            found.append(
                Finding(
                    check=CheckName.SCENARIO_DRIFT,
                    severity=Severity.UNCLASSIFIED,
                    record=line.trajectory_id,
                    detail=(
                        f"the corpus holds no scenario named {record.scenario!r}. Renamed, "
                        "deleted, or exported from a corpus this checkout does not have — "
                        "and with no scenario to compare against, drift cannot be measured "
                        "either way. Unclassified, not clean."
                    ),
                )
            )
            continue
        found.extend(_drift(line, record, scenario))
        label = labels.get(line.trajectory_id)
        if label is not None:
            found.extend(_label_drift(line.trajectory_id, label, scenario))
    return found


def trajectory_shape(record: export.TrajectoryRecord) -> str:
    """What the run DID, without any of its per-attempt identity or its timings.

    Built from named fields rather than by stripping volatile keys out of the JSON: every
    observation id and call id is a fresh uuid, so a digest of the raw line would call two
    replays of one canned scenario distinct, and a list of keys to strip would go stale.
    """
    shape: dict[str, Any] = {
        "scenario": record.scenario,
        "template_id": record.template_id,
        "seed": record.seed,
        "strategy": record.strategy,
        "final_state": record.outcome.final_state,
        "actions": [
            [action.tool_name, action.arguments, action.result.ok] for action in record.actions
        ],
        "readings": [
            [observation.tool_name, observation.content_sha256]
            for observation in record.observations
        ],
        "decisions": [
            [
                decision.action_kind,
                decision.probe_tool,
                decision.probe_arguments,
                [[entry.category, entry.confidence] for entry in decision.ranking_after],
            ]
            for decision in record.decisions
        ],
    }
    return hashlib.sha256(
        json.dumps(shape, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def check_duplicates(dataset: Dataset) -> list[Finding]:
    """The same trajectory twice: by id, and by what the run did under two ids."""
    found: list[Finding] = []
    by_id: dict[str, list[int]] = {}
    for line in dataset.trajectory_lines:
        by_id.setdefault(line.trajectory_id, []).append(line.number)
    by_shape: dict[str, list[str]] = {}
    for line, record in dataset.records():
        by_shape.setdefault(trajectory_shape(record), []).append(line.trajectory_id)
    for trajectory_id, numbers in sorted(by_id.items()):
        if len(numbers) > 1:
            found.append(
                Finding(
                    check=CheckName.DUPLICATE,
                    severity=Severity.BLOCKING,
                    record=trajectory_id,
                    detail=(
                        f"lines {numbers} carry the same trajectory_id. An id is one "
                        "attempt at one scenario (invariant 9); two lines under it weight "
                        "that attempt twice and make the join to the labels ambiguous."
                    ),
                )
            )
    for digest, ids in sorted(by_shape.items()):
        unique = sorted(set(ids))
        if len(unique) > 1:
            found.append(
                Finding(
                    check=CheckName.DUPLICATE,
                    severity=Severity.BLOCKING,
                    record=", ".join(unique),
                    detail=(
                        f"{len(unique)} line(s) record the same run ({digest[:12]}): the same "
                        "decisions, the same calls with the same arguments, and the same "
                        "readings by digest. The usual cause is one canned scenario replayed "
                        "several times, which is right for a regression gate and is N copies "
                        "of one trajectory in a training set. Keep one, or say which."
                    ),
                )
            )
    return found


def _component_names() -> frozenset[str]:
    return frozenset(component.value for component in reward.RewardComponent)


def check_rewards(dataset: Dataset) -> list[Finding]:
    """A reward that cannot be true: out of range, mis-flagged, or of another spec."""
    found: list[Finding] = []
    names = _component_names()
    for line, record in dataset.records():
        scored = record.reward
        if scored is None:
            continue
        where = line.trajectory_id
        if scored.spec_version != reward.SPEC_VERSION:
            found.append(
                Finding(
                    check=CheckName.INVALID_REWARD,
                    severity=Severity.UNCLASSIFIED,
                    record=where,
                    detail=(
                        f"the line's reward is spec v{scored.spec_version} and this "
                        f"checkout defines v{reward.SPEC_VERSION}. Its components mean "
                        "whatever that older spec said, so their ranges cannot be checked "
                        "here — unclassified rather than passed."
                    ),
                )
            )
            continue
        if scored.graded != (scored.total is not None):
            found.append(
                Finding(
                    check=CheckName.INVALID_REWARD,
                    severity=Severity.BLOCKING,
                    record=where,
                    detail=(
                        f"graded={scored.graded} with total={scored.total}. A reward is a "
                        "number or a withheld reason, never both and never neither."
                    ),
                )
            )
        if scored.total is not None and not 0.0 <= scored.total <= 1.0:
            found.append(
                Finding(
                    check=CheckName.INVALID_REWARD,
                    severity=Severity.BLOCKING,
                    record=where,
                    detail=f"total {scored.total} is outside [0, 1], so it has no scale.",
                )
            )
        if scored.safety_violated and scored.total not in (None, 0.0):
            found.append(
                Finding(
                    check=CheckName.INVALID_REWARD,
                    severity=Severity.BLOCKING,
                    record=where,
                    detail=(
                        f"safety_violated is true and total is {scored.total}. Safety is a "
                        "hard zero (docs/reward-spec.md); a violation that still scores "
                        "pays for the violation."
                    ),
                )
            )
        for name, value in sorted(scored.components.items()):
            if name not in names:
                found.append(
                    Finding(
                        check=CheckName.INVALID_REWARD,
                        severity=Severity.BLOCKING,
                        record=where,
                        detail=f"{name!r} is not a reward component ({sorted(names)}).",
                    )
                )
            elif value is not None and not 0.0 <= value <= 1.0:
                found.append(
                    Finding(
                        check=CheckName.INVALID_REWARD,
                        severity=Severity.BLOCKING,
                        record=where,
                        detail=f"component {name} is {value}, outside [0, 1].",
                    )
                )
        for name in sorted(set(scored.weights) - set(scored.components)):
            found.append(
                Finding(
                    check=CheckName.INVALID_REWARD,
                    severity=Severity.BLOCKING,
                    record=where,
                    detail=f"component {name} carries a weight and no value.",
                )
            )
        unearnable = sorted(
            name
            for name, weight in scored.weights.items()
            if weight > 0.0 and scored.components.get(name) is None
        )
        for name in unearnable:
            found.append(
                Finding(
                    check=CheckName.INVALID_REWARD,
                    severity=Severity.BLOCKING,
                    record=where,
                    detail=(
                        f"component {name} carries weight {scored.weights[name]} and a null "
                        "value, which is a silent zero (INC-003's distinction)."
                    ),
                )
            )
        total_weight = sum(scored.weights.values())
        if scored.weights and abs(total_weight - 1.0) > 1e-6:
            found.append(
                Finding(
                    check=CheckName.INVALID_REWARD,
                    severity=Severity.BLOCKING,
                    record=where,
                    detail=(
                        f"the weights that carried this reward sum to {total_weight}, not "
                        "1.0, so the total is on no comparable scale between scenarios."
                    ),
                )
            )
    return found


def check_action_results(
    dataset: Dataset, *, audit_windows: Mapping[str, reward.AuditWindow] | None = None
) -> list[Finding]:
    """Each action against its own result, and against what the platform recorded."""
    found: list[Finding] = []
    for line, record in dataset.records():
        where = line.trajectory_id
        by_id = observations_by_id(record)
        for action in record.actions:
            observation = by_id.get(action.result.observation_ref)
            if observation is None:
                continue  # check_missing_results owns a ref that resolves to nothing
            if observation.tool_name != action.tool_name:
                found.append(
                    Finding(
                        check=CheckName.ACTION_RESULT_MISMATCH,
                        severity=Severity.BLOCKING,
                        record=where,
                        detail=(
                            f"action {action.sequence} is {action.tool_name} and its "
                            f"result is {observation.tool_name}'s. The pair is not one "
                            "call."
                        ),
                    )
                )
            if observation.ok != action.result.ok:
                found.append(
                    Finding(
                        check=CheckName.ACTION_RESULT_MISMATCH,
                        severity=Severity.BLOCKING,
                        record=where,
                        detail=(
                            f"action {action.sequence} ({action.tool_name}) says "
                            f"ok={action.result.ok} and its observation says "
                            f"ok={observation.ok}."
                        ),
                    )
                )
        if record.outcome.action_count != len(record.actions):
            found.append(
                Finding(
                    check=CheckName.ACTION_RESULT_MISMATCH,
                    severity=Severity.BLOCKING,
                    record=where,
                    detail=(
                        f"outcome.action_count is {record.outcome.action_count} and the "
                        f"line carries {len(record.actions)} action(s)."
                    ),
                )
            )
        if record.outcome.decision_count != len(record.decisions):
            found.append(
                Finding(
                    check=CheckName.ACTION_RESULT_MISMATCH,
                    severity=Severity.BLOCKING,
                    record=where,
                    detail=(
                        f"outcome.decision_count is {record.outcome.decision_count} and "
                        f"the line carries {len(record.decisions)} decision(s)."
                    ),
                )
            )
        if audit_windows is None:
            continue
        window = audit_windows.get(where)
        if window is None:
            found.append(
                Finding(
                    check=CheckName.ACTION_RESULT_MISMATCH,
                    severity=Severity.UNCLASSIFIED,
                    record=where,
                    detail=(
                        "audit windows were supplied and this trajectory has none, so "
                        "whether its actions happened is unknown. Unknown is not clean "
                        "(guards.py's rule)."
                    ),
                )
            )
            continue
        if not window.complete:
            found.append(
                Finding(
                    check=CheckName.ACTION_RESULT_MISMATCH,
                    severity=Severity.UNCLASSIFIED,
                    record=where,
                    detail=(
                        "this trajectory's audit window was not fully scanned, so it "
                        "cannot show that an action did NOT happen. "
                        + reward.WITHHELD_PARTIAL_AUDIT
                    ),
                )
            )
            continue
        audited = {call.tool_name for call in window.calls if call.is_tier_1 and call.succeeded}
        claimed = set(tier_1_actions(record))
        for tool in sorted(claimed - audited):
            found.append(
                Finding(
                    check=CheckName.ACTION_RESULT_MISMATCH,
                    severity=Severity.BLOCKING,
                    record=where,
                    detail=(
                        f"the line shows the Tier-1 action {tool} and the platform audit "
                        "log shows no successful call of it. The audit log is ground truth "
                        "for what an agent did (invariant 6); training on the claim trains "
                        "on a thing that did not happen."
                    ),
                )
            )
        for tool in sorted(audited - claimed):
            found.append(
                Finding(
                    check=CheckName.ACTION_RESULT_MISMATCH,
                    severity=Severity.BLOCKING,
                    record=where,
                    detail=(
                        f"the platform audit log shows a successful Tier-1 {tool} that this "
                        "line does not carry. The trajectory is missing an action the run "
                        "actually took."
                    ),
                )
            )
    return found


def check_holdout(dataset: Dataset, *, corpus: Iterable[Scenario]) -> list[Finding]:
    """Holdout contamination computed from the corpus, not from the export's refusal.

    ``export.holdout_template_ids`` is a projection of the corpus; the refusal is
    ``export._refuse_holdout``, which this deliberately never calls — two layers only
    count as two if the second can fail where the first passed.
    """
    scenarios = list(corpus)
    held_out = export.holdout_template_ids(scenarios)
    by_name = {scenario.name: scenario for scenario in scenarios}
    found: list[Finding] = []
    for line, record in dataset.records():
        if record.template_id in held_out:
            found.append(
                Finding(
                    check=CheckName.HOLDOUT_CONTAMINATION,
                    severity=Severity.BLOCKING,
                    record=line.trajectory_id,
                    detail=(
                        f"template {record.template_id!r} is held out in the corpus and "
                        "this line exports an instance of it. Every instance of a held-out "
                        "template is held out (plan 03 § 4, plan 06 D7)."
                    ),
                )
            )
        if record.benchmark_split == BenchmarkSplit.HOLDOUT.value:
            found.append(
                Finding(
                    check=CheckName.HOLDOUT_CONTAMINATION,
                    severity=Severity.BLOCKING,
                    record=line.trajectory_id,
                    detail="the line's own benchmark_split is holdout.",
                )
            )
        scenario = by_name.get(record.scenario)
        if scenario is not None and scenario.benchmark_split is BenchmarkSplit.HOLDOUT:
            found.append(
                Finding(
                    check=CheckName.HOLDOUT_CONTAMINATION,
                    severity=Severity.BLOCKING,
                    record=line.trajectory_id,
                    detail=(
                        f"scenario {record.scenario!r} is in the holdout split today, "
                        "whatever the line says. A template moved into holdout after an "
                        "export contaminates the export, not the corpus."
                    ),
                )
            )
    overlap = sorted(set(dataset.manifest.exported_template_ids) & held_out)
    for template in overlap:
        found.append(
            Finding(
                check=CheckName.HOLDOUT_CONTAMINATION,
                severity=Severity.BLOCKING,
                record=dataset.manifest.invocation_id,
                detail=(
                    f"the manifest records {template!r} as exported and the corpus holds it "
                    "out, so anything scored on that template measures memorisation."
                ),
            )
        )
    recorded = tuple(sorted(dataset.manifest.holdout_template_ids_in_corpus))
    if recorded != tuple(sorted(held_out)):
        found.append(
            Finding(
                check=CheckName.HOLDOUT_CONTAMINATION,
                severity=Severity.BLOCKING,
                record=dataset.manifest.invocation_id,
                detail=(
                    f"the manifest recorded the holdout set as {list(recorded)} and the "
                    f"corpus holds out {sorted(held_out)}. The set the export promised "
                    "never to emit has changed under it."
                ),
            )
        )
    return found


def check_manifest(dataset: Dataset) -> list[Finding]:
    """The manifest's claims about the two files, checked against the files."""
    manifest = dataset.manifest
    found: list[Finding] = []
    digests = (
        ("trajectories", dataset.trajectory_bytes, manifest.trajectories_sha256),
        ("labels", dataset.label_bytes, manifest.labels_sha256),
    )
    for what, payload, claimed in digests:
        actual = hashlib.sha256(payload).hexdigest()
        if actual != claimed:
            found.append(
                Finding(
                    check=CheckName.MANIFEST_DISAGREEMENT,
                    severity=Severity.BLOCKING,
                    record=manifest.invocation_id,
                    detail=(
                        f"the {what} file digests to {actual[:12]} and the manifest claims "
                        f"{claimed[:12]}. The file has changed since it was written, and "
                        "an export whose provenance can be rewritten supports no claim "
                        "about what a policy saw (invariant 9)."
                    ),
                )
            )

    parsed = [record for _, record in dataset.records()]
    numbers: dict[str, tuple[object, object]] = {
        "trajectory_count": (manifest.trajectory_count, len(dataset.trajectory_lines)),
        "complete_count": (
            manifest.complete_count,
            sum(1 for record in parsed if record.outcome.complete),
        ),
        "exported_template_ids": (
            tuple(sorted(manifest.exported_template_ids)),
            tuple(sorted({record.template_id for record in parsed})),
        ),
        "exported_scenarios": (
            tuple(sorted(manifest.exported_scenarios)),
            tuple(sorted({record.scenario for record in parsed})),
        ),
        "splits_present": (
            tuple(sorted(manifest.splits_present)),
            tuple(sorted({record.benchmark_split for record in parsed})),
        ),
    }
    for field, (claimed_value, actual_value) in numbers.items():
        if claimed_value != actual_value:
            found.append(
                Finding(
                    check=CheckName.MANIFEST_DISAGREEMENT,
                    severity=Severity.BLOCKING,
                    record=manifest.invocation_id,
                    detail=(
                        f"the manifest says {field}={claimed_value!r} and the file holds "
                        f"{actual_value!r}. The manifest is what a later stage reads "
                        "instead of the data."
                    ),
                )
            )

    exported_ids = {line.trajectory_id for line in dataset.trajectory_lines}
    label_ids = {line.trajectory_id for line in dataset.label_lines}
    for trajectory_id in sorted(exported_ids - label_ids):
        found.append(
            Finding(
                check=CheckName.MANIFEST_DISAGREEMENT,
                severity=Severity.BLOCKING,
                record=trajectory_id,
                detail=(
                    "this trajectory has no labels line, so nothing can say what it was "
                    "graded against or whether its answer key leaked into it."
                ),
            )
        )
    for trajectory_id in sorted(label_ids - exported_ids):
        found.append(
            Finding(
                check=CheckName.MANIFEST_DISAGREEMENT,
                severity=Severity.BLOCKING,
                record=trajectory_id,
                detail="this labels line has no trajectory. The two files do not pair.",
            )
        )
    for line, record in dataset.records():
        if record.schema_version != manifest.schema_version:
            found.append(
                Finding(
                    check=CheckName.MANIFEST_DISAGREEMENT,
                    severity=Severity.BLOCKING,
                    record=line.trajectory_id,
                    detail=(
                        f"the line is schema v{record.schema_version} and the manifest is "
                        f"v{manifest.schema_version}."
                    ),
                )
            )
    return found


def check_dataset(
    dataset: Dataset,
    *,
    corpus: Iterable[Scenario],
    traces: Sequence[Path] | None = None,
    audit_windows: Mapping[str, reward.AuditWindow] | None = None,
) -> QualityReport:
    """Run every check over one export and report what it found, verdict included."""
    scenarios = tuple(corpus)
    findings: list[Finding] = [
        *_unreadable_lines(dataset),
        *check_manifest(dataset),
        *check_missing_results(dataset),
        *check_incomplete_trajectories(dataset, traces=traces),
        *check_hidden_truth(dataset, corpus=scenarios),
        *check_scenario_drift(dataset, corpus=scenarios),
        *check_duplicates(dataset),
        *check_rewards(dataset),
        *check_action_results(dataset, audit_windows=audit_windows),
        *check_holdout(dataset, corpus=scenarios),
    ]
    audited = 0
    if audit_windows is None:
        findings.append(
            Finding(
                check=CheckName.ACTION_RESULT_MISMATCH,
                severity=Severity.ADVISORY,
                record=dataset.manifest.invocation_id,
                detail=AUDIT_NOT_SUPPLIED,
            )
        )
    else:
        audited = sum(1 for line, _ in dataset.records() if line.trajectory_id in audit_windows)
    return _report(dataset, findings, audited=audited, traces_read=traces is not None)


# --- the CLI ------------------------------------------------------------------


def _audit_windows(path: Path) -> dict[str, reward.AuditWindow]:
    """Read ``{trajectory_id: AuditWindow}`` from JSON, as a live run would hand it over."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise DatasetReadError(f"{path} is not a mapping of trajectory_id to audit window")
    return {str(key): reward.AuditWindow.model_validate(value) for key, value in payload.items()}


def _main(argv: list[str] | None = None) -> int:
    """``python -m evals.dataset_checks`` — exit 1 on a blocking or unclassified finding."""
    parser = argparse.ArgumentParser(prog="python -m evals.dataset_checks", description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="the export manifest to check (default: the newest under evals/exports)",
    )
    parser.add_argument(
        "--trace-dir",
        type=Path,
        default=None,
        help="the trace store the export was built from; enables the boundary re-check",
    )
    parser.add_argument(
        "--audit",
        type=Path,
        default=None,
        help="JSON mapping trajectory_id to a platform audit window, for the action check",
    )
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)

    manifest_path = args.manifest
    if manifest_path is None:
        manifest_path = artifacts.newest_or_none(export.MANIFEST_KIND)
    if manifest_path is None:
        print(
            "no training export to check. `make training-export WRITE=1` writes one.",
            file=sys.stderr,
        )
        return 1

    traces: list[Path] | None = None
    if args.trace_dir is not None:
        if not args.trace_dir.is_dir():
            print(f"no trace directory at {args.trace_dir}", file=sys.stderr)
            return 1
        traces = sorted(args.trace_dir.glob("*.jsonl"))

    try:
        dataset = load_dataset(manifest_path)
        windows = None if args.audit is None else _audit_windows(args.audit)
    except (DatasetReadError, export.ExportError, OSError, ValueError) as err:
        print(str(err), file=sys.stderr)
        return 1

    report = check_dataset(
        dataset,
        corpus=load_scenarios(SCENARIO_DIRECTORY),
        traces=traces,
        audit_windows=windows,
    )
    if args.json:
        print(json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True))
    else:
        print(report.render(), end="")
    return 1 if report.failed else 0


if __name__ == "__main__":  # pragma: no cover - thin CLI wrapper
    raise SystemExit(_main())
