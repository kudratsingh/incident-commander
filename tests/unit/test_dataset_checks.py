"""Dataset quality checks (WP-15.3, `evals/dataset_checks.py`): every class, red then green.

Each class gets a fixture that TRIGGERS it and the clean export that does not, because a
check nobody has watched fail is a check nobody knows the shape of. The clean fixture is
the REAL `evals/export.py` output over a real trace file — built from the trace fixture
`test_export.py` already owns, so the two cannot drift apart.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import pytest

from evals import artifacts, dataset_checks, export, reward
from evals.scenarios.schema import BenchmarkSplit, Scenario
from incident_commander.agent.hypothesis import HypothesisCategory
from incident_commander.tools.registry import TOOL_REGISTRY

# The answer key's key list as WO-R3-185 pins it, and the trace fixture the export's own
# tests use. Imported rather than copied: a second copy of either is a second thing to
# keep true, and the leak check exists because hand-maintained lists go stale.
from tests.unit.test_export import _PROSE_SENTINEL, _TRUTH, _TRUTH_SENTINEL
from tests.unit.test_export import _records as _export_records
from tests.unit.test_export import _scenario as _export_scenario
from tests.unit.test_ground_truth_never_leaks import _LEAK_KEYS

_WHEN: Final[datetime] = datetime(2026, 9, 19, 9, 0, tzinfo=UTC)
_INVOCATION: Final[str] = "ddddeeeeffff"
_SCENARIO: Final[str] = "lag_dev"
_ATTEMPT: Final[str] = "inv000000001"
_TRAJECTORY_ID: Final[str] = f"{_SCENARIO}:{_ATTEMPT}"
_TIER_1: Final[str] = "restart_consumer_group"


def _corpus(*, split: BenchmarkSplit = BenchmarkSplit.DEV, **kwargs: Any) -> tuple[Scenario, ...]:
    return (_export_scenario(_SCENARIO, split=split, truth=_TRUTH, **kwargs),)


def _tier_1_record() -> dict[str, Any]:
    """One traced Tier-1 call, so a line has an action the audit log can disagree with."""
    return {
        "kind": "mcp",
        "record_id": "mcprecord003",
        "tool_name": _TIER_1,
        "arguments": {"consumer_group": "analytics-consumer"},
        "result": {"content": [{"type": "text", "text": '{"restarted":true}'}], "is_error": False},
        "duration_seconds": 0.5,
    }


def _write_trace(
    directory: Path,
    *,
    scenario: str = _SCENARIO,
    complete: bool = True,
    extra: Sequence[Mapping[str, Any]] = (),
    invocations: Sequence[str] = (_ATTEMPT,),
) -> Path:
    """One trace file, optionally with extra records and optionally missing its end."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{scenario}.jsonl"
    common = {"invocation_started_at": "2026-09-18T21:00:00+00:00"}
    with path.open("a", encoding="utf-8") as handle:
        for invocation in invocations:
            records = _export_records(scenario, invocation=invocation, complete=complete)
            tail = records.pop() if complete else None
            records.extend({**common, **record, "invocation_id": invocation} for record in extra)
            if tail is not None:
                records.append(tail)
            for record in records:
                handle.write(json.dumps(record) + "\n")
    return path


def _derived(
    base: export.ManifestRecord,
    trajectories: Sequence[Mapping[str, Any]],
    labels: Sequence[Mapping[str, Any]],
) -> export.ManifestRecord:
    """A manifest that honestly describes the payloads passed, so one check fires at a time."""
    parsed: list[export.TrajectoryRecord] = []
    for payload in trajectories:
        try:
            parsed.append(export.TrajectoryRecord.model_validate(payload))
        except ValueError:
            continue
    trajectory_text = _jsonl(trajectories)
    label_text = _jsonl(labels)
    return base.model_copy(
        update={
            "trajectory_count": len(trajectories),
            "complete_count": sum(1 for record in parsed if record.outcome.complete),
            "exported_template_ids": tuple(sorted({record.template_id for record in parsed})),
            "exported_scenarios": tuple(sorted({record.scenario for record in parsed})),
            "splits_present": tuple(sorted({record.benchmark_split for record in parsed})),
            "trajectories_sha256": hashlib.sha256(trajectory_text.encode()).hexdigest(),
            "trajectories_bytes": len(trajectory_text.encode()),
            "labels_sha256": hashlib.sha256(label_text.encode()).hexdigest(),
            "labels_bytes": len(label_text.encode()),
        }
    )


def _jsonl(payloads: Iterable[Mapping[str, Any]]) -> str:
    return "".join(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n" for payload in payloads
    )


@pytest.fixture
def cli_corpus(monkeypatch: pytest.MonkeyPatch) -> None:
    """The CLI loads the shipped corpus; point it at the fixture's one instead."""
    monkeypatch.setattr(dataset_checks, "load_scenarios", lambda _directory: _corpus())


@pytest.fixture
def clean(tmp_path: Path) -> Path:
    """The manifest of a real export of one complete attempt. The baseline for every red."""
    return _export_to(tmp_path)


def _export_to(
    tmp_path: Path,
    *,
    complete: bool = True,
    extra: Sequence[Mapping[str, Any]] = (),
    invocations: Sequence[str] = (_ATTEMPT,),
    corpus: Sequence[Scenario] | None = None,
) -> Path:
    """Build and write one real export under ``tmp_path``, returning its manifest path."""
    trace = _write_trace(
        tmp_path / "traces", complete=complete, extra=extra, invocations=invocations
    )
    built = export.build_export(
        traces=[trace],
        corpus=corpus if corpus is not None else _corpus(),
        timestamp=_WHEN,
        invocation_id=_INVOCATION,
    )
    return _write(tmp_path / "exports", built)


def _write(directory: Path, built: export.Export) -> Path:
    """Write one export's three files as `write_export` would, but under a plain directory."""
    directory.mkdir(parents=True, exist_ok=True)
    trajectory_payloads = [record.model_dump(mode="json") for record in built.trajectories]
    label_payloads = [record.model_dump(mode="json") for record in built.labels]
    described = _derived(built.manifest, trajectory_payloads, label_payloads)
    (directory / described.trajectory_file).write_text(_jsonl(trajectory_payloads), "utf-8")
    (directory / described.labels_file).write_text(_jsonl(label_payloads), "utf-8")
    path = directory / artifacts.version_name(
        export.MANIFEST_KIND, timestamp=_WHEN, invocation_id=_INVOCATION
    )
    path.write_text(
        json.dumps(described.model_dump(mode="json"), indent=2, sort_keys=True), "utf-8"
    )
    return path


def _mutate(
    manifest_path: Path,
    *,
    trajectory: Any = None,
    labels: Any = None,
    manifest: Any = None,
) -> Path:
    """Rewrite one written export through payload-level edits, digests recomputed."""
    dataset = dataset_checks.load_dataset(manifest_path)
    trajectory_payloads = [copy.deepcopy(line.payload) for line in dataset.trajectory_lines]
    label_payloads = [copy.deepcopy(line.payload) for line in dataset.label_lines]
    if trajectory is not None:
        trajectory_payloads = trajectory(trajectory_payloads)
    if labels is not None:
        label_payloads = labels(label_payloads)
    described = _derived(dataset.manifest, trajectory_payloads, label_payloads)
    if manifest is not None:
        described = manifest(described)
    (manifest_path.parent / described.trajectory_file).write_text(
        _jsonl(trajectory_payloads), "utf-8"
    )
    (manifest_path.parent / described.labels_file).write_text(_jsonl(label_payloads), "utf-8")
    manifest_path.write_text(
        json.dumps(described.model_dump(mode="json"), indent=2, sort_keys=True), "utf-8"
    )
    return manifest_path


def _window(*tools: str, complete: bool = True) -> reward.AuditWindow:
    """One platform audit window: the Tier-1 calls it recorded as succeeding."""
    return reward.AuditWindow(
        calls=tuple(
            reward.AuditedCall(
                tool_name=tool,
                arguments={},
                outcome=reward.OUTCOME_SUCCESS,
                at=datetime(2026, 9, 18, 21, 5, index, tzinfo=UTC),
                principal_id="agent",
            )
            for index, tool in enumerate(tools)
        ),
        complete=complete,
    )


def _check(
    manifest_path: Path,
    *,
    corpus: Sequence[Scenario] | None = None,
    traces: Sequence[Path] | None = None,
    audit_windows: Mapping[str, reward.AuditWindow] | None = None,
) -> dataset_checks.QualityReport:
    return dataset_checks.check_dataset(
        dataset_checks.load_dataset(manifest_path),
        corpus=corpus if corpus is not None else _corpus(),
        traces=traces,
        audit_windows=audit_windows,
    )


def _of(
    report: dataset_checks.QualityReport, check: dataset_checks.CheckName
) -> list[dataset_checks.Finding]:
    return [finding for finding in report.findings if finding.check is check]


def _clean_report(manifest_path: Path, tmp_path: Path) -> dataset_checks.QualityReport:
    """The clean export checked with every layer ON, so nothing is clean by omission."""
    return _check(
        manifest_path,
        traces=[tmp_path / "traces" / f"{_SCENARIO}.jsonl"],
        audit_windows={_TRAJECTORY_ID: _window()},
    )


class TestTheCleanExportIsClean:
    """One clean fixture, asserted per class. A red below means that class fired here too."""

    @pytest.mark.parametrize("check", list(dataset_checks.CheckName), ids=lambda c: c.value)
    def test_no_class_fires_on_a_real_export(
        self, clean: Path, tmp_path: Path, check: dataset_checks.CheckName
    ) -> None:
        report = _clean_report(clean, tmp_path)
        assert _of(report, check) == [], (
            f"{check.value} fired on the export `evals/export.py` actually produces: "
            f"{[finding.detail for finding in _of(report, check)]}"
        )

    def test_the_verdict_is_a_pass_and_the_exit_code_is_zero(
        self, clean: Path, tmp_path: Path, cli_corpus: None
    ) -> None:
        report = _clean_report(clean, tmp_path)
        assert not report.failed
        assert report.failing == ()
        arguments = [
            "--manifest",
            str(clean),
            "--audit",
            str(_audit_file(tmp_path)),
            "--trace-dir",
            str(tmp_path / "traces"),
        ]
        assert dataset_checks._main(arguments) == 0

    def test_the_report_names_the_counts_and_the_template_set(
        self, clean: Path, tmp_path: Path
    ) -> None:
        """What the order asks the report to carry: counts per class, and the templates."""
        report = _clean_report(clean, tmp_path)
        assert report.exported_template_ids == (_SCENARIO,)
        assert set(report.counts_by_severity) <= {s.value for s in dataset_checks.Severity}
        assert set(report.counts_by_check) <= {c.value for c in dataset_checks.CheckName}
        rendered = report.render()
        assert "PASS" in rendered
        assert _SCENARIO in rendered
        assert "blocking: 0" in rendered
        assert "unclassified: 0" in rendered


def _audit_file(tmp_path: Path) -> Path:
    path = tmp_path / "audit.json"
    path.write_text(
        json.dumps({_TRAJECTORY_ID: _window().model_dump(mode="json")}, default=str), "utf-8"
    )
    return path


class TestExitSemantics:
    def test_unclassified_fails_the_dataset_like_blocking_does(self) -> None:
        """The whole point of the severity: 'nobody could classify it' is not 'it is fine'."""
        assert dataset_checks.Severity.UNCLASSIFIED in dataset_checks.FAILING_SEVERITIES
        assert dataset_checks.Severity.BLOCKING in dataset_checks.FAILING_SEVERITIES
        assert dataset_checks.Severity.ADVISORY not in dataset_checks.FAILING_SEVERITIES

    def test_the_cli_exits_non_zero_and_names_the_record(
        self, clean: Path, cli_corpus: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _mutate(clean, trajectory=lambda lines: [*lines, copy.deepcopy(lines[0])])
        assert dataset_checks._main(["--manifest", str(clean)]) == 1
        printed = capsys.readouterr().out
        assert "FAIL" in printed
        assert _TRAJECTORY_ID in printed
        assert dataset_checks.CheckName.DUPLICATE.value in printed

    def test_an_advisory_alone_does_not_fail_the_dataset(self, clean: Path) -> None:
        """No audit window is a coverage statement, not a defect — and it is SAID."""
        report = _check(clean)
        assert not report.failed
        advisory = [
            finding
            for finding in report.findings
            if finding.severity is dataset_checks.Severity.ADVISORY
        ]
        assert [finding.detail for finding in advisory] == [dataset_checks.AUDIT_NOT_SUPPLIED]

    def test_json_output_round_trips(
        self, clean: Path, cli_corpus: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert dataset_checks._main(["--manifest", str(clean), "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["exported_template_ids"] == [_SCENARIO]


class TestMissingToolResults:
    def test_an_observation_that_digests_null_is_a_missing_result(self, clean: Path) -> None:
        def blank(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
            lines[0]["observations"][0]["content_sha256"] = dataset_checks.MISSING_CONTENT_SHA256
            lines[0]["observations"][0]["content_bytes"] = 4
            return lines

        _mutate(clean, trajectory=blank)
        found = _of(_check(clean), dataset_checks.CheckName.MISSING_RESULT)
        assert [finding.severity for finding in found] == [dataset_checks.Severity.BLOCKING]
        assert found[0].record == _TRAJECTORY_ID
        assert "neither a result nor an error" in found[0].detail

    def test_the_null_digest_is_the_one_the_real_export_writes(self, tmp_path: Path) -> None:
        """The pin: a traced call with no result and no error must hash to that constant."""
        empty = {
            "kind": "mcp",
            "record_id": "mcprecord009",
            "tool_name": "get_consumer_lag",
            "arguments": {},
        }
        manifest_path = _export_to(tmp_path, extra=[empty])
        digests = {
            observation["content_sha256"]
            for line in dataset_checks.load_dataset(manifest_path).trajectory_lines
            for observation in line.payload["observations"]
        }
        assert dataset_checks.MISSING_CONTENT_SHA256 in digests
        found = _of(_check(manifest_path), dataset_checks.CheckName.MISSING_RESULT)
        assert found and all(f.severity is dataset_checks.Severity.BLOCKING for f in found)

    def test_observations_that_share_one_id_are_reported_once_not_per_action(
        self, clean: Path
    ) -> None:
        """A trace store written before ``record_id`` gives every reading the same blank id.

        Resolving through it bound each action to whichever reading came last, which read as
        283 "the pair is not one call" findings on the shipped store. It is ONE fact.
        """

        def blank_ids(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
            for observation in lines[0]["observations"]:
                observation["observation_id"] = ""
            for action in lines[0]["actions"]:
                action["result"]["observation_ref"] = ""
            return lines

        _mutate(clean, trajectory=blank_ids)
        report = _check(clean)
        missing = _of(report, dataset_checks.CheckName.MISSING_RESULT)
        assert [finding.severity for finding in missing] == [dataset_checks.Severity.BLOCKING]
        assert "identifies nothing" in missing[0].detail
        cascade = [
            finding
            for finding in _of(report, dataset_checks.CheckName.ACTION_RESULT_MISMATCH)
            if finding.severity is not dataset_checks.Severity.ADVISORY
        ]
        assert cascade == []

    def test_an_action_whose_result_ref_resolves_to_nothing(self, clean: Path) -> None:
        def dangle(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
            lines[0]["actions"][0]["result"]["observation_ref"] = "notanobserv"
            return lines

        _mutate(clean, trajectory=dangle)
        found = _of(_check(clean), dataset_checks.CheckName.MISSING_RESULT)
        assert any("has no result at all" in finding.detail for finding in found)
        assert any("no action's result" in finding.detail for finding in found)


class TestIncompleteTrajectories:
    def test_a_start_with_no_end_is_incomplete(self, tmp_path: Path) -> None:
        """Structural, from the trace's own boundaries — not from the run's length."""
        manifest_path = _export_to(tmp_path, complete=False)
        found = _of(_check(manifest_path), dataset_checks.CheckName.INCOMPLETE_TRAJECTORY)
        assert [finding.severity for finding in found] == [dataset_checks.Severity.BLOCKING]
        assert "scenario_start or a scenario_end and not both" in found[0].detail

    def test_a_line_that_claims_completeness_the_trace_store_denies(self, tmp_path: Path) -> None:
        manifest_path = _export_to(tmp_path, complete=False)

        def lie(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
            lines[0]["outcome"]["complete"] = True
            return lines

        _mutate(manifest_path, trajectory=lie)
        found = _of(
            _check(manifest_path, traces=[tmp_path / "traces" / f"{_SCENARIO}.jsonl"]),
            dataset_checks.CheckName.INCOMPLETE_TRAJECTORY,
        )
        assert [finding.severity for finding in found] == [dataset_checks.Severity.BLOCKING]
        assert "disagrees with the append-only record" in found[0].detail

    def test_one_invocation_id_across_two_scenarios_is_two_trajectories(
        self, tmp_path: Path
    ) -> None:
        """One eval run writes ONE invocation_id into every scenario's file.

        Keyed by the id alone, the incomplete file would mark the complete one incomplete
        too — which is what it did on the shipped trace store: 34 false blocking findings.
        """
        traces = tmp_path / "traces"
        _write_trace(traces, scenario=_SCENARIO, complete=True)
        _write_trace(traces, scenario="lag_other", complete=False)
        corpus = (*_corpus(), _export_scenario("lag_other", truth=_TRUTH))
        built = export.build_export(
            traces=sorted(traces.glob("*.jsonl")),
            corpus=corpus,
            timestamp=_WHEN,
            invocation_id=_INVOCATION,
        )
        manifest_path = _write(tmp_path / "exports", built)
        found = _of(
            _check(manifest_path, corpus=corpus, traces=sorted(traces.glob("*.jsonl"))),
            dataset_checks.CheckName.INCOMPLETE_TRAJECTORY,
        )
        assert [finding.record for finding in found] == [f"lag_other:{_ATTEMPT}"]

    def test_a_line_with_no_trace_behind_it_is_unclassified(
        self, clean: Path, tmp_path: Path
    ) -> None:
        empty = tmp_path / "elsewhere"
        empty.mkdir()
        found = _of(
            _check(clean, traces=list(empty.glob("*.jsonl"))),
            dataset_checks.CheckName.INCOMPLETE_TRAJECTORY,
        )
        assert [finding.severity for finding in found] == [dataset_checks.Severity.UNCLASSIFIED]
        assert "unclassified" in found[0].detail


class TestLeakedHiddenTruth:
    """WO-R3-185's boundary, asked of the artifact that leaves the harness."""

    def test_the_key_set_is_derived_and_covers_the_pinned_list(self) -> None:
        """Reuse, not a second exclusion list: the set is computed from the two models."""
        assert set(_LEAK_KEYS) <= dataset_checks.HIDDEN_TRUTH_KEYS
        label_side = dataset_checks.model_key_names(export.LabelRecord)
        training_side = dataset_checks.model_key_names(export.TrajectoryRecord)
        assert label_side - training_side == dataset_checks.HIDDEN_TRUTH_KEYS
        assert "ground_truth" in dataset_checks.HIDDEN_TRUTH_KEYS
        assert "template_id" not in dataset_checks.HIDDEN_TRUTH_KEYS

    def test_no_tool_argument_name_can_be_mistaken_for_the_answer_key(self) -> None:
        """A false positive would be a key the agent chooses, so check the whole surface."""
        arguments = {
            name for spec in TOOL_REGISTRY.values() for name in spec.input_model.model_fields
        }
        assert arguments & dataset_checks.HIDDEN_TRUTH_KEYS == set()

    def test_a_nested_ground_truth_key_is_found(self, clean: Path) -> None:
        """The realistic shape: the answer key inside a free-form arguments dict."""

        def bury(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
            lines[0]["actions"][0]["arguments"]["note"] = {
                "context": {"ground_truth": {"root_causes": ["poison_message"]}}
            }
            return lines

        _mutate(clean, trajectory=bury)
        found = _of(_check(clean), dataset_checks.CheckName.LEAKED_HIDDEN_TRUTH)
        keys = {finding.detail.split("'")[1] for finding in found}
        assert "ground_truth" in keys
        assert "root_causes" in keys
        assert all(finding.severity is dataset_checks.Severity.BLOCKING for finding in found)
        assert all(finding.record == _TRAJECTORY_ID for finding in found)

    def test_a_ground_truth_value_is_found_even_with_no_key_to_name_it(self, clean: Path) -> None:
        """A component name from the answer key, carried as a plain string."""

        def plant(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
            lines[0]["decisions"][0]["probe_arguments"]["consumer_group"] = _TRUTH_SENTINEL
            return lines

        _mutate(clean, trajectory=plant)
        found = _of(_check(clean), dataset_checks.CheckName.LEAKED_HIDDEN_TRUTH)
        assert found and all(_TRUTH_SENTINEL in finding.detail for finding in found)

    def test_a_root_cause_label_outside_the_claim_slot_is_a_leak(self, clean: Path) -> None:
        def plant(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
            lines[0]["decisions"][0]["probe_arguments"]["hint"] = (
                HypothesisCategory.POISON_MESSAGE.value
            )
            return lines

        _mutate(clean, trajectory=plant)
        found = _of(_check(clean), dataset_checks.CheckName.LEAKED_HIDDEN_TRUTH)
        assert [finding.severity for finding in found] == [dataset_checks.Severity.BLOCKING]
        assert dataset_checks.CLAIM_KEY in found[0].detail

    def test_a_component_the_alert_already_names_is_not_a_leak(self, clean: Path) -> None:
        """The scoping that keeps the check usable: `agent_visible` decides, not a guess.

        A real component name is usually in the alert AND in `affected_components`, and the
        agent naming it is the agent doing its job. Unscoped, this fired 191 times on the
        shipped trace store — a checker that cries wolf that often is worse than none.
        """
        shared = _export_scenario(_SCENARIO).ground_truth
        assert shared is None  # the fixture scenario ships no truth of its own
        readable = _export_scenario(_SCENARIO, truth=_TRUTH).model_copy(
            update={"ground_truth": _TRUTH.model_copy(update={"affected_components": ("billing",)})}
        )

        def plant(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
            lines[0]["decisions"][0]["probe_arguments"]["source"] = "billing"
            return lines

        def relabel(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
            lines[0]["ground_truth"]["affected_components"] = ["billing"]
            return lines

        _mutate(clean, trajectory=plant, labels=relabel)
        assert "billing" in dataset_checks.agent_visible_text(readable)
        assert (
            _of(_check(clean, corpus=(readable,)), dataset_checks.CheckName.LEAKED_HIDDEN_TRUTH)
            == []
        )

    def test_the_agents_own_ranking_is_not_a_leak(self, clean: Path) -> None:
        """The other half: a category the agent RANKED is its claim, and stays."""

        def rank(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
            lines[0]["decisions"][0]["ranking_after"] = [
                {"category": HypothesisCategory.POISON_MESSAGE.value, "confidence": 0.9}
            ]
            return lines

        _mutate(clean, trajectory=rank)
        assert _of(_check(clean), dataset_checks.CheckName.LEAKED_HIDDEN_TRUTH) == []

    def test_a_benchmark_key_that_names_its_own_fault_is_an_advisory(self, clean: Path) -> None:
        """A template named after its root cause: the corpus's fact, not the export's leak.

        `redis_saturation` is that shape in the shipped corpus. Blocking it would fail every
        dataset that corpus can produce; hiding it would let a training stage key on the name.
        """
        named = _export_scenario("poison_message", truth=_TRUTH)

        def rename(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
            for line in lines:
                line["scenario"] = "poison_message"
                line["template_id"] = "poison_message"
            return lines

        def relabel(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
            for line in lines:
                line["scenario"] = "poison_message"
                line["template_id"] = "poison_message"
            return lines

        _mutate(clean, trajectory=rename, labels=relabel)
        report = _check(clean, corpus=(named,))
        found = _of(report, dataset_checks.CheckName.LEAKED_HIDDEN_TRUTH)
        assert found and all(f.severity is dataset_checks.Severity.ADVISORY for f in found)
        assert any("is its own root-cause label" in finding.detail for finding in found)
        assert not report.failed

    def test_every_benchmark_key_path_is_a_field_of_the_line(self) -> None:
        """The scoped paths are real fields, so the scoping cannot silently cover nothing."""
        for path in dataset_checks.BENCHMARK_KEY_PATHS:
            assert len(path) == 1
            assert path[0] in export.TrajectoryRecord.model_fields

    def test_the_claim_slot_is_a_field_of_both_ranking_models(self) -> None:
        """CLAIM_KEY is one name; pin it to the models the export fills from the ranking."""
        assert dataset_checks.CLAIM_KEY in export.RankedCategory.model_fields
        assert dataset_checks.CLAIM_KEY in export.CandidateExport.model_fields

    def test_a_line_the_model_refuses_while_carrying_the_answer_key_is_a_leak(
        self, clean: Path
    ) -> None:
        """`extra='forbid'` means a top-level leak refuses the line; it is still a LEAK."""

        def top_level(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
            lines[0]["ground_truth"] = {"root_causes": ["poison_message"]}
            return lines

        _mutate(clean, trajectory=top_level)
        report = _check(clean)
        leaks = _of(report, dataset_checks.CheckName.LEAKED_HIDDEN_TRUTH)
        assert leaks and all(f.severity is dataset_checks.Severity.BLOCKING for f in leaks)


class TestScenarioDrift:
    def test_a_benchmark_key_that_moved_is_drift(self, clean: Path) -> None:
        def move(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
            lines[0]["seed"] = 7
            return lines

        _mutate(clean, trajectory=move)
        found = _of(_check(clean), dataset_checks.CheckName.SCENARIO_DRIFT)
        assert [finding.severity for finding in found] == [dataset_checks.Severity.BLOCKING]
        assert "seed=7" in found[0].detail

    def test_a_label_the_corpus_no_longer_agrees_with_is_drift(self, clean: Path) -> None:
        """The labels line is checked against `reward.labels_of`, the corpus projection."""
        moved = _export_scenario(_SCENARIO, truth=_TRUTH).model_copy(
            update={
                "expectation": _export_scenario(_SCENARIO).expectation.model_copy(
                    update={"max_tool_calls": 3}
                )
            }
        )
        found = _of(_check(clean, corpus=(moved,)), dataset_checks.CheckName.SCENARIO_DRIFT)
        assert found and all(f.severity is dataset_checks.Severity.BLOCKING for f in found)
        assert any("max_tool_calls" in finding.detail for finding in found)

    def test_a_scenario_the_corpus_does_not_hold_is_unclassified(self, clean: Path) -> None:
        """Renamed, deleted or from another corpus — the checker will not pick one."""
        other = _export_scenario("something_else", truth=_TRUTH)
        found = _of(_check(clean, corpus=(other,)), dataset_checks.CheckName.SCENARIO_DRIFT)
        assert [finding.severity for finding in found] == [dataset_checks.Severity.UNCLASSIFIED]
        assert "Unclassified, not clean" in found[0].detail


class TestDuplicates:
    def test_the_same_trajectory_id_twice(self, clean: Path) -> None:
        _mutate(clean, trajectory=lambda lines: [*lines, copy.deepcopy(lines[0])])
        found = _of(_check(clean), dataset_checks.CheckName.DUPLICATE)
        assert any("same trajectory_id" in finding.detail for finding in found)
        assert all(f.severity is dataset_checks.Severity.BLOCKING for f in found)

    def test_one_run_under_two_ids(self, clean: Path) -> None:
        """Two ids for one run: the same calls, arguments and readings, different uuids."""

        def clone(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
            twin = copy.deepcopy(lines[0])
            twin["trajectory_id"] = f"{_SCENARIO}:inv000000002"
            twin["invocation_id"] = "inv000000002"
            return [*lines, twin]

        def pair(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
            twin = copy.deepcopy(lines[0])
            twin["trajectory_id"] = f"{_SCENARIO}:inv000000002"
            return [*lines, twin]

        _mutate(clean, trajectory=clone, labels=pair)
        found = _of(_check(clean), dataset_checks.CheckName.DUPLICATE)
        assert any("record the same run" in finding.detail for finding in found)
        assert all(f.severity is dataset_checks.Severity.BLOCKING for f in found)

    def test_a_second_run_of_the_same_scenario_that_differs_is_not_a_duplicate(
        self, clean: Path
    ) -> None:
        """The other half: two attempts that read different things are two trajectories."""

        def differ(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
            twin = copy.deepcopy(lines[0])
            twin["trajectory_id"] = f"{_SCENARIO}:inv000000002"
            twin["invocation_id"] = "inv000000002"
            twin["observations"][0]["content_sha256"] = "0" * 64
            return [*lines, twin]

        def pair(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
            twin = copy.deepcopy(lines[0])
            twin["trajectory_id"] = f"{_SCENARIO}:inv000000002"
            return [*lines, twin]

        _mutate(clean, trajectory=differ, labels=pair)
        assert _of(_check(clean), dataset_checks.CheckName.DUPLICATE) == []

    def test_per_attempt_ids_are_not_what_makes_two_runs_different(self, clean: Path) -> None:
        """A fresh uuid per observation must not hide a duplicate (the digest's whole point)."""

        def renumber(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
            twin = copy.deepcopy(lines[0])
            twin["trajectory_id"] = f"{_SCENARIO}:inv000000002"
            twin["invocation_id"] = "inv000000002"
            for index, observation in enumerate(twin["observations"]):
                observation["observation_id"] = f"freshuuid{index:03d}"
                twin["actions"][index]["result"]["observation_ref"] = observation["observation_id"]
            return [*lines, twin]

        def pair(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
            twin = copy.deepcopy(lines[0])
            twin["trajectory_id"] = f"{_SCENARIO}:inv000000002"
            return [*lines, twin]

        _mutate(clean, trajectory=renumber, labels=pair)
        found = _of(_check(clean), dataset_checks.CheckName.DUPLICATE)
        assert any("record the same run" in finding.detail for finding in found)


class TestInvalidRewards:
    @pytest.mark.parametrize(
        ("edit", "expected"),
        [
            ({"total": 1.4, "graded": True}, "outside [0, 1]"),
            ({"graded": True}, "never both and never neither"),
            ({"total": 0.5, "graded": True, "safety_violated": True}, "hard zero"),
            ({"components": {"not_a_component": 0.5}}, "is not a reward component"),
            ({"components": {"root_cause": 4.0}}, "outside [0, 1]"),
        ],
        ids=["out_of_range", "graded_without_a_number", "safety_paid", "unknown", "component"],
    )
    def test_a_reward_that_cannot_be_true_is_blocking(
        self, clean: Path, edit: dict[str, Any], expected: str
    ) -> None:
        def apply(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
            lines[0]["reward"].update(edit)
            return lines

        _mutate(clean, trajectory=apply)
        found = _of(_check(clean), dataset_checks.CheckName.INVALID_REWARD)
        assert found and all(f.severity is dataset_checks.Severity.BLOCKING for f in found)
        assert any(expected in finding.detail for finding in found)

    def test_a_weight_with_no_value_is_a_silent_zero(self, clean: Path) -> None:
        def apply(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
            lines[0]["reward"]["components"] = {"root_cause": None}
            lines[0]["reward"]["weights"] = {"root_cause": 1.0}
            return lines

        _mutate(clean, trajectory=apply)
        found = _of(_check(clean), dataset_checks.CheckName.INVALID_REWARD)
        assert any("silent zero" in finding.detail for finding in found)

    def test_weights_that_do_not_sum_to_one(self, clean: Path) -> None:
        def apply(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
            lines[0]["reward"]["components"] = {"root_cause": 1.0, "action": 1.0}
            lines[0]["reward"]["weights"] = {"root_cause": 0.3, "action": 0.3}
            return lines

        _mutate(clean, trajectory=apply)
        found = _of(_check(clean), dataset_checks.CheckName.INVALID_REWARD)
        assert any("no comparable scale" in finding.detail for finding in found)

    def test_another_spec_version_is_unclassified_not_passed(self, clean: Path) -> None:
        def apply(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
            lines[0]["reward"]["spec_version"] = reward.SPEC_VERSION + 99
            lines[0]["reward"]["total"] = 4.0
            return lines

        _mutate(clean, trajectory=apply)
        found = _of(_check(clean), dataset_checks.CheckName.INVALID_REWARD)
        assert [finding.severity for finding in found] == [dataset_checks.Severity.UNCLASSIFIED]
        assert "cannot be checked here" in found[0].detail


class TestActionResultMismatches:
    def test_an_action_the_audit_log_does_not_show(self, tmp_path: Path) -> None:
        manifest_path = _export_to(tmp_path, extra=[_tier_1_record()])
        found = _of(
            _check(manifest_path, audit_windows={_TRAJECTORY_ID: _window()}),
            dataset_checks.CheckName.ACTION_RESULT_MISMATCH,
        )
        assert [finding.severity for finding in found] == [dataset_checks.Severity.BLOCKING]
        assert _TIER_1 in found[0].detail
        assert "invariant 6" in found[0].detail
        assert found[0].record == _TRAJECTORY_ID

    def test_an_audited_action_the_line_does_not_carry(self, clean: Path) -> None:
        found = _of(
            _check(clean, audit_windows={_TRAJECTORY_ID: _window(_TIER_1)}),
            dataset_checks.CheckName.ACTION_RESULT_MISMATCH,
        )
        assert [finding.severity for finding in found] == [dataset_checks.Severity.BLOCKING]
        assert "missing an action the run actually took" in found[0].detail

    def test_an_action_bound_to_another_calls_result(self, clean: Path) -> None:
        def swap(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
            lines[0]["actions"][0]["tool_name"] = "list_dlq_messages"
            return lines

        _mutate(clean, trajectory=swap)
        found = _of(_check(clean), dataset_checks.CheckName.ACTION_RESULT_MISMATCH)
        assert any("not one call" in finding.detail for finding in found)

    def test_an_outcome_count_that_disagrees_with_the_line(self, clean: Path) -> None:
        def miscount(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
            lines[0]["outcome"]["action_count"] = 9
            return lines

        _mutate(clean, trajectory=miscount)
        found = _of(_check(clean), dataset_checks.CheckName.ACTION_RESULT_MISMATCH)
        assert any("action_count is 9" in finding.detail for finding in found)

    def test_a_missing_window_is_unclassified_when_windows_were_supplied(self, clean: Path) -> None:
        found = _of(
            _check(clean, audit_windows={"someone:else": _window()}),
            dataset_checks.CheckName.ACTION_RESULT_MISMATCH,
        )
        assert [finding.severity for finding in found] == [dataset_checks.Severity.UNCLASSIFIED]
        assert "Unknown is not clean" in found[0].detail

    def test_a_partial_window_is_unclassified(self, clean: Path) -> None:
        found = _of(
            _check(clean, audit_windows={_TRAJECTORY_ID: _window(complete=False)}),
            dataset_checks.CheckName.ACTION_RESULT_MISMATCH,
        )
        assert [finding.severity for finding in found] == [dataset_checks.Severity.UNCLASSIFIED]
        assert "not fully scanned" in found[0].detail


class TestHoldoutContamination:
    """Layer two. It has to be able to fail where the export's own refusal passed."""

    def test_a_held_out_template_in_an_existing_export_is_caught(self, clean: Path) -> None:
        held = _corpus(split=BenchmarkSplit.HOLDOUT)
        found = _of(_check(clean, corpus=held), dataset_checks.CheckName.HOLDOUT_CONTAMINATION)
        assert found and all(f.severity is dataset_checks.Severity.BLOCKING for f in found)
        assert any(_SCENARIO in finding.detail for finding in found)

    def test_it_never_asks_the_export_to_refuse(
        self, clean: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Independence, proved: break the export's refusal and the check still fires."""

        def exploded(*_: Any, **__: Any) -> None:
            raise AssertionError("the checker called the export's own refusal")

        monkeypatch.setattr(export, "_refuse_holdout", exploded)
        found = _of(
            _check(clean, corpus=_corpus(split=BenchmarkSplit.HOLDOUT)),
            dataset_checks.CheckName.HOLDOUT_CONTAMINATION,
        )
        assert found

    def test_the_export_itself_would_also_refuse_today(self, tmp_path: Path) -> None:
        """The other half of "two layers": layer one is real and it is a different path."""
        with pytest.raises(export.HoldoutExportError):
            _export_to(tmp_path, corpus=_corpus(split=BenchmarkSplit.HOLDOUT))

    def test_a_holdout_set_that_moved_under_the_manifest(self, clean: Path) -> None:
        def widen(manifest: export.ManifestRecord) -> export.ManifestRecord:
            return manifest.model_copy(update={"holdout_template_ids_in_corpus": ("some_other",)})

        _mutate(clean, manifest=widen)
        found = _of(_check(clean), dataset_checks.CheckName.HOLDOUT_CONTAMINATION)
        assert any("changed under it" in finding.detail for finding in found)


class TestManifestDisagreement:
    def test_a_file_edited_after_the_manifest_was_written(self, clean: Path) -> None:
        dataset = dataset_checks.load_dataset(clean)
        dataset.trajectories_path.write_text(
            _jsonl([{**dataset.trajectory_lines[0].payload, "strategy": "edited"}]), "utf-8"
        )
        found = _of(_check(clean), dataset_checks.CheckName.MANIFEST_DISAGREEMENT)
        assert any("has changed since it was written" in finding.detail for finding in found)

    def test_a_trajectory_with_no_labels_line(self, clean: Path) -> None:
        _mutate(clean, labels=lambda lines: [])
        found = _of(_check(clean), dataset_checks.CheckName.MANIFEST_DISAGREEMENT)
        assert any("no labels line" in finding.detail for finding in found)
        assert all(f.severity is dataset_checks.Severity.BLOCKING for f in found)

    def test_a_line_the_model_refuses_for_no_visible_reason_is_unclassified(
        self, clean: Path
    ) -> None:
        """Not a leak, not a known shape — so the checker says so instead of guessing."""

        def widen(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
            lines[0]["a_field_from_the_future"] = 1
            return lines

        _mutate(clean, trajectory=widen)
        found = _of(_check(clean), dataset_checks.CheckName.MANIFEST_DISAGREEMENT)
        unclassified = [
            finding for finding in found if finding.severity is dataset_checks.Severity.UNCLASSIFIED
        ]
        assert unclassified
        assert "unclassified rather than waved through" in unclassified[0].detail


class TestReadingTheExport:
    def test_a_manifest_with_no_files_beside_it_is_an_error_not_a_finding(
        self, clean: Path
    ) -> None:
        dataset_checks.load_dataset(clean).trajectories_path.unlink()
        with pytest.raises(dataset_checks.DatasetReadError, match="not beside it"):
            dataset_checks.load_dataset(clean)

    def test_the_cli_says_so_when_there_is_no_export(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert dataset_checks._main(["--manifest", str(tmp_path / "nothing.json")]) == 1
        assert capsys.readouterr().err != ""

    def test_tier_1_actions_agrees_with_the_exports_own_projection(self, clean: Path) -> None:
        """Reuse by pinning: two readings of "what Tier-1 did this line claim"."""
        for _, record in dataset_checks.load_dataset(clean).records():
            assert (
                dataset_checks.tier_1_actions(record)
                == export._claimed(record).claimed_action_tools
            )

    def test_the_prose_sentinel_never_reaches_a_finding(self, clean: Path, tmp_path: Path) -> None:
        """A quality report is read by humans and must not become the leak itself."""
        report = _clean_report(clean, tmp_path)
        assert _PROSE_SENTINEL not in report.render()
