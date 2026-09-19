"""`make world-record` — the recorded-world recorder (WP-3.1, WO-R3-196).

A recording is REPLAYED to measured runs for weeks. Five things are proved here: keys are
the WIRED arguments (divergence F2 — a raw-argument key misses every read), the whole
``ToolResult`` survives (F3), a recording is deterministic bar timestamps, the answer key
is out of reach (ADR 0038/0040), and the write is exclusive-create (invariant 9).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import pytest

from evals import artifacts, dossier, recorder
from evals.graders.root_cause import label_describes_this_world
from evals.scenarios.loader import load_scenarios
from evals.scenarios.schema import Scenario, chaos_tool_names
from incident_commander.tools.mcp_client import MCPError, ToolResult
from incident_commander.tools.policies import Tier, tier_of
from incident_commander.tools.registry import TOOL_REGISTRY
from incident_commander.tools.wire import wire_arguments

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
_SCENARIOS_DIR: Final[Path] = _REPO_ROOT / "evals" / "scenarios"

_T1: Final[datetime] = datetime(2026, 9, 17, 12, 0, 0, tzinfo=UTC)
_T2: Final[datetime] = datetime(2026, 9, 17, 13, 30, 0, tzinfo=UTC)
_INV1: Final[str] = "aaaabbbbcccc"
_INV2: Final[str] = "ddddeeeeffff"

#: The rem-4 contradiction: a `replay_safe` hint over a permanent data bug.
_INCOHERENT_ROW: Final[dict[str, Any]] = {
    "id": "11111111-1111-5111-8111-111111111111",
    "type": "csv_upload",
    "remediation_hint": "replay_safe",
    "error_message": "SchemaValidationError: payload missing required field 'user_id'",
    "retry_count": 3,
    "created_at": "2026-09-17T10:00:00Z",
}


class FakeClient:
    """Structural ``MCPClientProtocol`` fake, keyed by tool name.

    Records the arguments it was called with, the only way to assert the WIRED form went
    over the wire. Not ``CannedMCPClient``: a per-tool queue cannot express that.
    """

    def __init__(
        self,
        payloads: Mapping[str, Any] | None = None,
        errors: Sequence[str] = (),
        *,
        is_error: Sequence[str] = (),
        default: Any = None,
    ) -> None:
        self._payloads = dict(payloads or {})
        self._errors = set(errors)
        self._is_error = set(is_error)
        self._default = default
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call_tool(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        timeout_seconds: float | None = None,
    ) -> ToolResult:
        self.calls.append((name, dict(arguments)))
        if name in self._errors:
            raise MCPError(-32603, f"fake failure for {name}")
        payload = self._payloads.get(name, self._default)
        if payload is None:
            raise MCPError(-32601, f"no fake payload for {name}")
        return ToolResult(
            content=[{"type": "text", "text": json.dumps(payload)}],
            is_error=name in self._is_error,
        )


@pytest.fixture(scope="module")
def scenarios() -> dict[str, Scenario]:
    return {s.name: s for s in load_scenarios(_SCENARIOS_DIR)}


def _provenance() -> recorder.RecordedProvenance:
    return recorder.RecordedProvenance(
        recorded_at=_T1.isoformat(),
        invocation_id=_INV1,
        commander_head="abc1234",
        platform_mcp_url="http://platform.invalid:8001/mcp",
        read_principal="PLATFORM_SMOKE_TOKEN (read-scoped)",
    )


def _record(
    scenario: Scenario,
    client: FakeClient,
    *,
    invocation_id: str = _INV1,
    at: datetime = _T1,
) -> recorder.RecordedWorld:
    """The recorder's own pipeline, minus the seeding and the reset.

    Assembled from public functions: driving ``main`` would need a fake ``eval-reset``.
    """
    derived, notes = recorder.recording_probes(scenario)
    probes, wire_notes = recorder.wire_probes(derived)
    calls, readings, failures = recorder.record_calls(client, probes, now=lambda: at)
    return recorder.build_world(
        scenario=scenario,
        label=recorder.world_label(scenario, chaos_seeded=bool(scenario.chaos.setup)),
        calls=calls,
        failures=failures,
        notes=[*notes, *wire_notes],
        findings=recorder.findings_of(scenario, readings, ()),
        provenance=recorder.RecordedProvenance(
            recorded_at=at.isoformat(),
            invocation_id=invocation_id,
            commander_head="abc1234",
            platform_mcp_url="http://platform.invalid:8001/mcp",
            read_principal="PLATFORM_SMOKE_TOKEN (read-scoped)",
        ),
    )


class TestKeysAreTheWiredArguments:
    """Divergence F2, the one that decides whether a recording works at all.

    RED BEFORE: keying on the raw ``probe.args`` carries ``{}`` where the agent sends four.
    """

    def test_an_omitted_optional_is_in_the_key_with_the_default_the_platform_fills(
        self,
    ) -> None:
        """``list_dlq_messages({})`` is recorded as the four-field call it becomes."""
        probes, notes = recorder.wire_probes([dossier._probe("list_dlq_messages", {}, "test")])
        assert notes == []
        assert len(probes) == 1
        assert probes[0].args == {
            "job_type": None,
            "remediation_hint": None,
            "limit": 50,
            "offset": 0,
        }

    def test_the_call_made_is_the_wired_call_not_the_raw_one(self) -> None:
        """The wire is what matters: a recording of a call nobody makes is empty.

        Wiring happens BEFORE the call.
        """
        client = FakeClient({"list_dlq_messages": {"total": 0, "items": []}})
        probes, _notes = recorder.wire_probes([dossier._probe("list_dlq_messages", {}, "test")])
        recorder.record_calls(client, probes, now=lambda: _T1)
        assert client.calls == [
            (
                "list_dlq_messages",
                {"job_type": None, "remediation_hint": None, "limit": 50, "offset": 0},
            )
        ]

    def test_the_key_is_what_the_agents_own_call_hashes_to(self) -> None:
        """One key function, computed from the wire form, used by both sides.

        ``answer`` wires whatever it is handed, so a raw-argument lookup finds it (F2).
        """
        client = FakeClient({"list_dlq_messages": {"total": 1, "items": [_INCOHERENT_ROW]}})
        probes, _notes = recorder.wire_probes(
            [dossier._probe("list_dlq_messages", {"remediation_hint": "replay_safe"}, "test")]
        )
        calls, _readings, _failures = recorder.record_calls(client, probes, now=lambda: _T1)
        world = recorder.build_world(
            scenario=_only_scenario_stub(),
            label=recorder.RecordedWorldLabel(label="test", live_mcp=True, chaos_seeded=True),
            calls=calls,
            failures=(),
            notes=(),
            findings=(),
            provenance=_provenance(),
        )
        agent_side = world.answer("list_dlq_messages", {"remediation_hint": "replay_safe"})
        assert agent_side is not None
        assert json.loads(agent_side.content[0]["text"])["total"] == 1

    def test_two_probes_that_wire_to_one_call_are_recorded_once(self) -> None:
        """``{}`` and ``{"limit": 50}`` are the same request after wiring.

        The merge runs again on the WIRED form, or two entries collide.
        """
        probes, _notes = recorder.wire_probes(
            [
                dossier._probe("list_dlq_messages", {}, "a"),
                dossier._probe("list_dlq_messages", {"limit": 50}, "b"),
            ]
        )
        assert len(probes) == 1
        assert probes[0].origins == ("a", "b")

    def test_a_probe_that_cannot_be_wired_is_a_note_not_a_crash(self) -> None:
        """The agent's client would be refused the same way, so it is a scenario fact."""
        probes, notes = recorder.wire_probes(
            [dossier._probe("get_consumer_lag", {"consumer_group": None}, "test")]
        )
        assert probes == []
        assert len(notes) == 1
        assert "does not validate" in notes[0]
        assert "GetConsumerLagInput" in notes[0]

    def test_an_unregistered_tool_is_a_note_not_a_crash(self) -> None:
        probes, notes = recorder.wire_probes([dossier._probe("no_such_tool", {}, "test")])
        assert probes == []
        assert "not in the registry" in notes[0]


class TestTheWholeToolResultSurvives:
    """Divergence F3: a replay client answers with a ``ToolResult``, not a paraphrase."""

    def test_content_blocks_and_is_error_are_both_kept(self) -> None:
        client = FakeClient(
            {"list_dlq_messages": {"total": 0, "items": []}}, is_error=["list_dlq_messages"]
        )
        probes, _notes = recorder.wire_probes([dossier._probe("list_dlq_messages", {}, "test")])
        calls, readings, failures = recorder.record_calls(client, probes, now=lambda: _T1)
        assert failures == []
        assert calls[0].result["is_error"] is True
        assert calls[0].result["content"] == [{"type": "text", "text": '{"total": 0, "items": []}'}]
        # …and the Reading, which is what the lints read, still says what it said.
        assert readings[0].error == "the tool reported is_error=True"

    def test_read_is_unchanged_and_read_result_is_the_same_loop(self) -> None:
        """``read`` must keep behaving exactly as it did — the dossier depends on it."""
        client = FakeClient({"list_dlq_messages": {"total": 0, "items": []}})
        probe = dossier._probe("list_dlq_messages", {}, "test")
        plain = dossier.read(client, probe)
        reading, result = dossier.read_result(client, probe)
        assert plain == reading
        assert result is not None
        assert result.content == [{"type": "text", "text": '{"total": 0, "items": []}'}]

    def test_a_call_that_never_reached_the_platform_is_a_failure_not_a_call(self) -> None:
        """No answer exists, so there is nothing to replay — and it is still reported."""
        client = FakeClient(errors=["list_dlq_messages"])
        probes, _notes = recorder.wire_probes([dossier._probe("list_dlq_messages", {}, "test")])
        calls, _readings, failures = recorder.record_calls(client, probes, now=lambda: _T1)
        assert calls == []
        assert len(failures) == 1
        assert failures[0].tool == "list_dlq_messages"
        assert "MCPError" in failures[0].detail


class TestDeterminism:
    """Two recordings of one canned world are the same recording."""

    def test_the_fingerprint_ignores_only_the_clock(self, scenarios: dict[str, Scenario]) -> None:
        scenario = scenarios["remediate_dlq_backlog_success"]
        first = _record(scenario, _dlq_client(), invocation_id=_INV1, at=_T1)
        second = _record(scenario, _dlq_client(), invocation_id=_INV2, at=_T2)
        assert recorder.world_fingerprint(first) == recorder.world_fingerprint(second)

    def test_the_documents_differ_only_in_the_volatile_fields(
        self, scenarios: dict[str, Scenario]
    ) -> None:
        """Byte-identical apart from timestamps, stated as a diff over the JSON.

        Checked as well as the fingerprint, which could be wrong too.
        """
        scenario = scenarios["remediate_dlq_backlog_success"]
        first = json.loads(
            _record(scenario, _dlq_client(), invocation_id=_INV1, at=_T1).model_dump_json()
        )
        second = json.loads(
            _record(scenario, _dlq_client(), invocation_id=_INV2, at=_T2).model_dump_json()
        )
        assert first.pop("provenance") != second.pop("provenance")
        for left, right in zip(first["calls"], second["calls"], strict=True):
            moved = {key for key in left if left[key] != right[key]}
            assert moved <= recorder.VOLATILE_CALL_FIELDS, moved
            for key in recorder.VOLATILE_CALL_FIELDS:
                left.pop(key)
                right.pop(key)
        assert first == second

    def test_the_calls_are_ordered_by_key_not_by_derivation_order(
        self, scenarios: dict[str, Scenario]
    ) -> None:
        """So a change in how probes are derived does not read as a changed world."""
        world = _record(scenarios["remediate_dlq_backlog_success"], _dlq_client())
        assert list(world.keys) == sorted(world.keys)


class TestTheAnswerKeyIsOutOfReach:
    """ADR 0038's wall, applied to a recording; ADR 0040 for which world it is about."""

    def test_the_recording_has_no_field_that_could_hold_an_answer_key(
        self, scenarios: dict[str, Scenario]
    ) -> None:
        """Checked on the SHAPE, not by grepping for a label.

        A grep is wrong here: ``remediate_dlq_backlog_success`` seeds ``poison_message`` and so
        is its label. What must hold is structural: no field an answer key could sit in.
        """
        scenario = scenarios["remediate_dlq_backlog_success"]
        assert scenario.ground_truth is not None, "this test needs a labelled scenario"
        document = json.loads(_record(scenario, _dlq_client()).model_dump_json())
        assert set(document) == set(recorder.RecordedWorld.model_fields)
        assert "ground_truth" not in document
        assert "root_causes" not in document

    def test_no_label_reaches_the_only_part_a_replayed_agent_can_see(
        self, scenarios: dict[str, Scenario]
    ) -> None:
        """The agent's whole view of a recording is ``calls[].result`` (via ``answer``).

        There a substring check is right (ADR 0012).
        """
        scenario = scenarios["remediate_dlq_backlog_success"]
        assert scenario.ground_truth is not None
        world = _record(scenario, _dlq_client())
        visible = json.dumps([call.result for call in world.calls])
        for label in scenario.ground_truth.root_causes:
            assert label.value not in visible
        assert "chaos" not in visible

    def test_a_recording_carrying_a_ground_truth_key_refuses_to_load(self, tmp_path: Path) -> None:
        """``extra="forbid"`` is the wall, not a habit of whoever wrote the file."""
        world = _minimal_world()
        smuggled = json.loads(world.model_dump_json())
        smuggled["ground_truth"] = {"root_causes": ["poison_message"]}
        path = tmp_path / "smuggled.json"
        path.write_text(json.dumps(smuggled))
        with pytest.raises(ValueError, match="ground_truth"):
            recorder.load_recording(path)

    def test_the_replay_loader_opens_the_recording_and_nothing_else(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The property, checked by watching every file the loader touches.

        ``load_recording`` takes one path and has no parameter that could reach the sibling.
        """
        recording = artifacts.write_versioned(
            "recorded_world",
            "a_scenario",
            content=_minimal_world().model_dump_json(),
            timestamp=_T1,
            invocation_id=_INV1,
            root=tmp_path,
        )
        truth = artifacts.write_versioned(
            "recorded_world_truth",
            "a_scenario",
            content=json.dumps({"ground_truth": {"root_causes": ["poison_message"]}}),
            timestamp=_T1,
            invocation_id=_INV1,
            root=tmp_path,
        )
        assert truth.parent == recording.parent, "the answer key is a SIBLING, by design"

        touched: list[str] = []
        real_read_text = Path.read_text
        real_open = Path.open

        def spy_read_text(self: Path, *args: Any, **kwargs: Any) -> str:
            touched.append(str(self))
            return real_read_text(self, *args, **kwargs)

        def spy_open(self: Path, *args: Any, **kwargs: Any) -> Any:
            touched.append(str(self))
            return real_open(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", spy_read_text)
        monkeypatch.setattr(Path, "open", spy_open)
        loaded = recorder.load_recording(recording)

        assert loaded.scenario == "a_scenario"
        # A set, because ``read_text`` opens the same path it was given: what is
        # under test is WHICH files were touched, not how many times.
        assert set(touched) == {str(recording)}
        assert str(truth) not in touched

    def test_answer_hands_back_a_tool_result_and_nothing_else(self) -> None:
        """The whole replay surface: the agent sees a ``ToolResult``, never the document."""
        world = _minimal_world()
        answered = world.answer("list_dlq_messages", {})
        assert isinstance(answered, ToolResult)
        assert world.answer("list_dlq_messages", {"remediation_hint": "replay_safe"}) is None
        assert world.answer("get_dag_state", {"job_id": "unrecorded"}) is None
        assert world.answer("not_a_tool", {}) is None

    def test_the_truth_sibling_says_which_world_its_label_is_about(
        self, scenarios: dict[str, Scenario]
    ) -> None:
        """ADR 0040 / INC-003, stated at record time rather than found at grade time."""
        scenario = scenarios["remediate_dlq_backlog_success"]
        seeded = recorder.world_label(scenario, chaos_seeded=True)
        unseeded = recorder.world_label(scenario, chaos_seeded=False)
        provenance = _provenance()
        assert recorder.ground_truth_document(scenario, seeded, provenance, recording="r.json")[
            "applies"
        ]
        document = recorder.ground_truth_document(
            scenario, unseeded, provenance, recording="r.json"
        )
        assert document["applies"] is False
        assert document["ground_truth"] is not None
        assert "ADR 0040" in unseeded.label

    def test_an_unlabelled_scenario_records_the_reason_rather_than_a_bare_null(
        self, scenarios: dict[str, Scenario]
    ) -> None:
        unlabelled = [s for s in scenarios.values() if s.ground_truth is None]
        if not unlabelled:  # pragma: no cover - the corpus is only partly labelled today
            pytest.skip("every scenario in the corpus declares a ground truth")
        scenario = unlabelled[0]
        document = recorder.ground_truth_document(
            scenario,
            recorder.world_label(scenario, chaos_seeded=bool(scenario.chaos.setup)),
            _provenance(),
            recording="r.json",
        )
        assert document["ground_truth"] is None
        assert scenario.name in str(document["absent_reason"])


class TestARecordingIsEvidence:
    """Invariant 9 / divergence D2: registered, versioned, exclusive-create."""

    def test_both_kinds_are_registered(self) -> None:
        assert "recorded_world" in artifacts.KINDS
        assert "recorded_world_truth" in artifacts.KINDS

    def test_a_second_recording_of_the_same_scenario_raises(self, tmp_path: Path) -> None:
        for kind in ("recorded_world", "recorded_world_truth"):
            artifacts.write_versioned(
                kind, "a_scenario", content="{}", timestamp=_T1, invocation_id=_INV1, root=tmp_path
            )
            with pytest.raises(FileExistsError):
                artifacts.write_versioned(
                    kind,
                    "a_scenario",
                    content="{}",
                    timestamp=_T1,
                    invocation_id=_INV1,
                    root=tmp_path,
                )

    def test_the_two_families_do_not_resolve_each_other(self, tmp_path: Path) -> None:
        """The suffix keeps them disjoint, so ``newest`` of one never returns the other."""
        recording = artifacts.write_versioned(
            "recorded_world",
            "a_scenario",
            content="{}",
            timestamp=_T1,
            invocation_id=_INV1,
            root=tmp_path,
        )
        truth = artifacts.write_versioned(
            "recorded_world_truth",
            "a_scenario",
            content="{}",
            timestamp=_T2,
            invocation_id=_INV2,
            root=tmp_path,
        )
        assert artifacts.newest("recorded_world", "a_scenario", root=tmp_path) == recording
        assert artifacts.newest("recorded_world_truth", "a_scenario", root=tmp_path) == truth
        assert artifacts.versions("recorded_world", "a_scenario", root=tmp_path) == [recording]

    def test_a_newer_recording_wins_and_the_older_one_stays(self, tmp_path: Path) -> None:
        first = artifacts.write_versioned(
            "recorded_world",
            "a_scenario",
            content="{}",
            timestamp=_T1,
            invocation_id=_INV1,
            root=tmp_path,
        )
        second = artifacts.write_versioned(
            "recorded_world",
            "a_scenario",
            content="{}",
            timestamp=_T2,
            invocation_id=_INV2,
            root=tmp_path,
        )
        assert artifacts.newest("recorded_world", "a_scenario", root=tmp_path) == second
        assert artifacts.versions("recorded_world", "a_scenario", root=tmp_path) == [first, second]

    def test_recordings_live_outside_the_reports_tree(self) -> None:
        """A recording is an INPUT to a run, not a document; the reports tree is the other thing."""
        assert artifacts.KINDS["recorded_world"].parts == ("evals", "recorded_worlds")
        assert artifacts.KINDS["recorded_world_truth"].parts == ("evals", "recorded_worlds")


class TestTheCoherenceLintsRunOnTheRecording:
    """Plan 02 §187: a recorded world that contradicts itself is a fixture defect."""

    def test_a_deliberately_incoherent_row_is_reported_as_a_finding(
        self, scenarios: dict[str, Scenario]
    ) -> None:
        scenario = scenarios["remediate_dlq_backlog_success"]
        world = _record(scenario, _dlq_client(rows=[_INCOHERENT_ROW]))
        flagged = [f for f in world.findings if f.kind == dossier.INCOHERENT_KIND]
        assert flagged, [f.kind for f in world.findings]
        assert _INCOHERENT_ROW["id"] in flagged[0].subject
        assert "efdc3b2a9864" in flagged[0].detail

    def test_a_coherent_world_reports_no_contradiction(
        self, scenarios: dict[str, Scenario]
    ) -> None:
        world = _record(scenarios["remediate_dlq_backlog_success"], _dlq_client())
        assert [f for f in world.findings if f.kind == dossier.INCOHERENT_KIND] == []

    def test_the_findings_travel_inside_the_document(self, scenarios: dict[str, Scenario]) -> None:
        """A benchmark built on the recording can be told the world is defective."""
        world = _record(
            scenarios["remediate_dlq_backlog_success"], _dlq_client(rows=[_INCOHERENT_ROW])
        )
        reloaded = recorder.RecordedWorld.model_validate_json(world.model_dump_json())
        assert reloaded.findings == world.findings


class TestThePremiseIsEstablishedBeforeAnythingIsRecorded:
    """04:110 — record after setup, settle and preconditions PASS.

    A recording of a world whose fault never landed is replayed to every run
    built on it, so "not met" has to stop the recording rather than annotate it.
    """

    def test_a_precondition_that_lands_late_is_polled_for(
        self, scenarios: dict[str, Scenario]
    ) -> None:
        """The consumer-lag case: ``lag >= 20`` behind a 60-second gauge cadence.

        Single-shot would refuse every lag scenario.
        """
        scenario = scenarios["remediate_consumer_lag_success"]
        probe = scenario.expected_precondition[0]
        assert probe.attempts > 1, "this test needs a polled precondition"

        climbing = [
            {"lag": 0, "lag_known": True, "consumer_group": "worker-dispatcher"},
            {"lag": 4, "lag_known": True, "consumer_group": "worker-dispatcher"},
            {"lag": 40, "lag_known": True, "consumer_group": "worker-dispatcher"},
        ]

        class Climbing(FakeClient):
            def call_tool(
                self,
                name: str,
                arguments: Mapping[str, Any],
                *,
                timeout_seconds: float | None = None,
            ) -> ToolResult:
                self.calls.append((name, dict(arguments)))
                reading = climbing[min(len(self.calls) - 1, len(climbing) - 1)]
                return ToolResult(content=[{"type": "text", "text": json.dumps(reading)}])

        client = Climbing()
        slept: list[float] = []
        established = recorder.establish_preconditions(client, scenario, sleep=slept.append)
        assert [entry.met for entry in established] == [True]
        assert len(client.calls) == 3
        assert slept == [probe.delay_seconds, probe.delay_seconds]

    def test_a_premise_that_never_lands_stays_unmet_after_the_whole_window(
        self, scenarios: dict[str, Scenario]
    ) -> None:
        scenario = scenarios["remediate_consumer_lag_success"]
        probe = scenario.expected_precondition[0]
        client = FakeClient(
            {
                "get_consumer_lag": {
                    "lag": 0,
                    "lag_known": True,
                    "consumer_group": "worker-dispatcher",
                }
            }
        )
        slept: list[float] = []
        established = recorder.establish_preconditions(client, scenario, sleep=slept.append)
        assert [entry.met for entry in established] == [False]
        assert len(client.calls) == probe.attempts
        assert len(slept) == probe.attempts - 1

    def test_a_scenario_with_no_precondition_polls_nothing(
        self, scenarios: dict[str, Scenario]
    ) -> None:
        bare = [s for s in scenarios.values() if not s.expected_precondition]
        assert bare, "the corpus has always held scenarios that assert no premise"
        client = FakeClient()
        assert recorder.establish_preconditions(client, bare[0]) == []
        assert client.calls == []


class TestTheCallSet:
    """What gets recorded, and what cannot get recorded."""

    def test_no_recorded_call_is_ever_a_write(self, scenarios: dict[str, Scenario]) -> None:
        """Tier is checked by construction, not by a rule somebody follows."""
        for scenario in scenarios.values():
            derived, _notes = recorder.recording_probes(scenario)
            probes, _wire_notes = recorder.wire_probes(derived)
            for probe in probes:
                assert probe.tool in TOOL_REGISTRY, probe.tool
                assert tier_of(probe.tool) is Tier.READ, f"{scenario.name}: {probe.label}"

    def test_the_set_is_a_superset_of_the_dossiers(self, scenarios: dict[str, Scenario]) -> None:
        """A recording answers every read the dossier reads, and more.

        Compared after wiring on both sides, because that is the only form in
        which the two are comparable at all (F2).
        """
        for name in ("remediate_dlq_backlog_success", "remediate_consumer_lag_success"):
            scenario = scenarios[name]
            expected, _notes = dossier.derive_probes(scenario)
            expected_keys = {
                recorder.call_key(p.tool, p.args) for p in recorder.wire_probes(expected)[0]
            }
            derived, _n = recorder.recording_probes(scenario)
            recorded = recorder.wire_probes(derived)[0]
            recorded_keys = {recorder.call_key(p.tool, p.args) for p in recorded}
            assert expected_keys and expected_keys <= recorded_keys, name

    def test_the_scenarios_own_precondition_calls_are_recorded(
        self, scenarios: dict[str, Scenario]
    ) -> None:
        """The scenario's own declared argument values — the filtered forms (04:110).

        The derivation reads UNFILTERED.
        """
        scenario = scenarios["remediate_dlq_backlog_success"]
        assert scenario.expected_precondition, "this test needs a scenario with preconditions"
        derived, _notes = recorder.recording_probes(scenario)
        probes, _wire_notes = recorder.wire_probes(derived)
        recorded = {recorder.call_key(p.tool, p.args) for p in probes}
        for probe in scenario.expected_precondition:
            spec = TOOL_REGISTRY[probe.tool]
            wanted = recorder.call_key(probe.tool, wire_arguments(spec, dict(probe.arguments)))
            assert wanted in recorded, probe.tool

    def test_the_sweep_adds_the_unfiltered_form_of_every_read_tool_it_can_fill(
        self, scenarios: dict[str, Scenario]
    ) -> None:
        """Wider than the expected set, because an unrecorded call is a miss in a measured run."""
        scenario = scenarios["remediate_dlq_backlog_success"]
        sweep, _notes = recorder.sweep_probes(scenario)
        tools = {probe.tool for probe in sweep}
        assert {"list_dlq_messages", "list_active_alerts", "get_deploy_history"} <= tools
        for probe in sweep:
            assert tier_of(probe.tool) is Tier.READ

    def test_a_tool_whose_argument_the_scenario_never_names_is_a_note(
        self, scenarios: dict[str, Scenario]
    ) -> None:
        """Never a guessed resource id: a probe aimed at a made-up resource is not evidence."""
        _sweep, notes = recorder.sweep_probes(scenarios["consumer_lag_high"])
        assert notes, "a scenario that names few resources must say what went unrecorded"
        assert "not_recorded" in notes[0]

    def test_the_derivations_notes_survive_into_the_document(
        self, scenarios: dict[str, Scenario]
    ) -> None:
        """A probe that could not be derived is a finding about the scenario.

        Notes are not warnings to swallow — ``derive_probes``' own docstring
        says so, and a recording that dropped them would look complete.
        """
        world = _record(scenarios["consumer_lag_high"], _lag_client())
        assert world.notes


class TestTheDossierPathIsUnchanged:
    """``make world-dossier`` must render exactly what it rendered before this packet."""

    def test_the_same_scenario_derives_the_same_probes(
        self, scenarios: dict[str, Scenario]
    ) -> None:
        """``derive_probes`` is imported, not forked: same probes, same origins, same order."""
        for scenario in scenarios.values():
            probes, notes = dossier.derive_probes(scenario)
            again, again_notes = dossier.derive_probes(scenario)
            assert [(p.tool, p.arguments, p.origins) for p in probes] == [
                (p.tool, p.arguments, p.origins) for p in again
            ]
            assert notes == again_notes

    def test_a_dossier_of_a_canned_world_is_byte_identical_across_the_change(
        self, scenarios: dict[str, Scenario]
    ) -> None:
        """The rendered document, pinned against its own re-render.

        ``render`` is pure over its inputs, so this catches a recorder change that reached
        ``derive_probes``, ``read`` or a lint.
        """
        scenario = scenarios["remediate_dlq_backlog_success"]
        client = _dlq_client()
        probes, notes = dossier.derive_probes(scenario)
        readings = [dossier.read(client, probe) for probe in probes]
        dlq_findings, dlq_rows = dossier.lint_dlq_coherence(readings)
        target_findings, target_rows = dossier.lint_action_targets(scenario, readings, ())
        furniture_findings, furniture_rows = dossier.lint_forbidden_furniture(scenario, readings)
        rendered = [
            dossier.render(
                scenario=scenario,
                generated_at=_T1,
                invocation_id=_INV1,
                settings_url="http://platform.invalid:8001/mcp",
                head="abc1234",
                seeding="seeded",
                preconditions=(),
                readings=readings,
                notes=notes,
                dlq_findings=dlq_findings,
                dlq_rows=dlq_rows,
                target_findings=target_findings,
                target_rows=target_rows,
                furniture_findings=furniture_findings,
                furniture_rows=furniture_rows,
                reset_code=0,
                reset_output="ok",
                baseline=(),
            )
            for _ in range(2)
        ]
        assert rendered[0] == rendered[1]
        assert "## 4. Every read the agent is expected to make" in rendered[0]


def _paths_read_while_loading(path: Path) -> set[str]:
    """Every file ``load_recording`` touches, watched from outside it.

    A free function, not a closure: a nested spy would capture the loop's accumulator (B023).
    """
    touched: set[str] = set()
    real_read_text = Path.read_text

    def spy(self: Path, *args: Any, **kwargs: Any) -> str:
        touched.add(str(self))
        return real_read_text(self, *args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(Path, "read_text", spy)
        recorder.load_recording(path)
    return touched


class TestNothingAgentVisibleNamesTheLab:
    """The hazard the recorder introduces, and nothing else in the repo has.

    A live agent's `PLATFORM_TOKEN` lacks `chaos:invoke` so it cannot read the `chaos.`
    audit stream (ADR 0012); the recorder reads under `PLATFORM_SMOKE_TOKEN`, and what
    it sees reaches a replayed agent.
    """

    def test_the_term_set_is_derived_from_the_contract_not_typed(self) -> None:
        terms = recorder.lab_vocabulary_terms()
        assert "chaos" in terms
        assert chaos_tool_names() <= terms
        # `unknown` is an ordinary English word and would cry on innocent
        # output; the whole-corpus leak hunt adjudicates labels by name instead.
        assert "unknown" not in terms

    def test_a_term_in_a_recorded_result_is_a_finding(self) -> None:
        wired = wire_arguments(TOOL_REGISTRY["list_audit_events"], {})
        leaky = recorder.RecordedCall(
            tool="list_audit_events",
            arguments=wired,
            key=recorder.call_key("list_audit_events", wired),
            result={
                "content": [
                    {"type": "text", "text": '{"events": [{"action": "chaos.kill_consumer"}]}'}
                ],
                "is_error": False,
            },
            started_at=_T1.isoformat(),
            completed_at=_T1.isoformat(),
            duration_ms=1,
        )
        findings = recorder.lint_agent_visible_vocabulary([leaky])
        assert [f.kind for f in findings] == [recorder.LAB_VOCABULARY_KIND]
        assert "list_audit_events" in findings[0].subject
        assert "ADR 0012" in findings[0].detail

    def test_a_clean_result_is_silent(self) -> None:
        assert recorder.lint_agent_visible_vocabulary(_minimal_world().calls) == []

    def test_the_world_label_may_name_its_hooks_and_is_not_scanned(
        self, scenarios: dict[str, Scenario]
    ) -> None:
        """ADR 0040 needs the label to say which hooks built the world.

        It is evaluator-side and unreachable through `answer()`.
        """
        label = recorder.world_label(scenarios["remediate_consumer_lag_success"], chaos_seeded=True)
        assert "kill_consumer" in label.label
        assert label.chaos_hooks == ("kill_consumer",)
        world = _record(scenarios["remediate_consumer_lag_success"], _lag_client())
        assert [f for f in world.findings if f.kind == recorder.LAB_VOCABULARY_KIND] == []


class TestTheCommittedRecordings:
    """Every recording this repo carries, checked as the fixture it now is.

    Committed, not gitignored: WP-3.2's replay client and ``--mode recorded`` cannot run
    in a fresh clone without them, so one that stops loading is a defect in the repo.
    """

    @staticmethod
    def _recordings() -> list[Path]:
        folder = _REPO_ROOT / "evals" / "recorded_worlds"
        return sorted(p for p in folder.rglob("*.json") if not p.name.endswith(".truth.json"))

    def test_there_is_at_least_one(self) -> None:
        assert self._recordings(), (
            "evals/recorded_worlds/ carries no recording — WP-3.2 and WP-3.3 need at "
            "least one committed world to run against in a fresh clone"
        )

    def test_every_one_loads_through_the_loader(self) -> None:
        for path in self._recordings():
            world = recorder.load_recording(path)
            assert world.schema_version == recorder.SCHEMA_VERSION, path.name
            assert world.calls, path.name
            assert world.scenario == path.parent.name, path.name
            # A hand-edited key that stopped matching its arguments answers nothing.
            for call in world.calls:
                assert call.key == recorder.call_key(call.tool, call.arguments), path.name

    def test_every_recorded_call_is_answerable_by_its_own_arguments(self) -> None:
        """The round trip that is the whole point: what was recorded is what replays."""
        for path in self._recordings():
            world = recorder.load_recording(path)
            for call in world.calls:
                answered = world.answer(call.tool, call.arguments)
                assert answered is not None, f"{path.name}: {call.tool}"
                assert answered.model_dump(mode="json") == call.result, f"{path.name}: {call.tool}"

    def test_no_recording_carries_an_answer_key_and_every_sibling_does(self) -> None:
        for path in self._recordings():
            document = json.loads(path.read_text())
            assert "ground_truth" not in document, path.name
            assert set(document) == set(recorder.RecordedWorld.model_fields), path.name
            sibling = path.with_name(path.name.replace(".json", ".truth.json"))
            assert sibling.exists(), f"{path.name} has no ground-truth sibling"
            truth = json.loads(sibling.read_text())
            assert truth["scenario"] == document["scenario"]
            assert truth["recording"] == path.name
            # The rule, not a restatement of it: whether the label applies is
            # `label_describes_this_world`'s answer for that world.
            assert truth["applies"] == label_describes_this_world(
                live_mcp=document["world"]["live_mcp"],
                chaos_seeded=document["world"]["chaos_seeded"],
            ), sibling.name

    def test_the_answer_key_is_unreachable_from_the_recording(self) -> None:
        """Loading a committed recording opens that file and no other."""
        for path in self._recordings():
            touched = _paths_read_while_loading(path)
            sibling = path.with_name(path.name.replace(".json", ".truth.json"))
            assert touched == {str(path)}, path.name
            assert str(sibling) not in touched, path.name

    def test_nothing_a_replayed_agent_can_see_names_the_lab(self) -> None:
        """Run the vocabulary lint over what is committed, not only over a fake."""
        for path in self._recordings():
            world = recorder.load_recording(path)
            assert recorder.lint_agent_visible_vocabulary(world.calls) == [], path.name

    def test_no_recorded_call_is_a_write(self) -> None:
        for path in self._recordings():
            for call in recorder.load_recording(path).calls:
                assert tier_of(call.tool) is Tier.READ, f"{path.name}: {call.tool}"

    def test_no_committed_recording_holds_a_credential(self) -> None:
        """A recording is read from a live platform, so this is not hypothetical.

        Principal UUIDs are NOT credentials.
        """
        secrets = re.compile(r"sk-ant-|sa_[A-Za-z0-9_]{10,}|Bearer\s+\S|ANTHROPIC_API_KEY")
        folder = _REPO_ROOT / "evals" / "recorded_worlds"
        for path in sorted(folder.rglob("*.json")):
            found = secrets.findall(path.read_text())
            assert not found, f"{path.name} carries a credential-shaped string"


class TestSelectionGuard:
    """A recording seeds chaos into the shared world, so the ONLY guard is the same one."""

    def test_no_only_refuses_before_touching_settings(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert recorder.main([]) == recorder.EXIT_SELECTION
        assert "nothing was seeded" in capsys.readouterr().out

    def test_two_scenarios_refuse(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert recorder.main(["--only", "a,b"]) == recorder.EXIT_SELECTION
        assert "nothing was seeded" in capsys.readouterr().out

    def test_a_substring_refuses_and_says_what_it_meant(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert recorder.main(["--only", "remediate_dlq"]) == recorder.EXIT_SELECTION
        out = capsys.readouterr().out
        assert "is not a scenario name" in out
        assert "ONLY=remediate_dlq_backlog_success" in out
        assert "RECORD FAIL" in out
        assert "world-dossier" not in out


# --------------------------------------------------------------------------
# Fixtures for the fake worlds above
# --------------------------------------------------------------------------


def _dlq_client(rows: Sequence[Mapping[str, Any]] | None = None) -> FakeClient:
    """A canned world coherent enough that only what a test plants is flagged."""
    listed = (
        list(rows)
        if rows is not None
        else [
            {
                "id": "22222222-2222-5222-8222-222222222222",
                "type": "bulk_api_sync",
                "remediation_hint": "replay_safe",
                "error_message": "UpstreamTimeout: partner-api timed out after 30s",
                "retry_count": 3,
                "created_at": "2026-09-17T10:00:00Z",
            }
        ]
    )
    return FakeClient(
        {
            "list_dlq_messages": {
                "total": len(listed),
                "items": listed,
                "listing_complete": True,
            },
            "list_active_alerts": {"total": 0, "alerts": []},
            "get_deploy_history": {"deploys": []},
            "get_postgres_health": {"ok": True},
            "get_redis_health": {"ok": True},
            "list_audit_events": {"events": []},
            "list_incidents": {"incidents": []},
            "search_traces": {"traces": []},
        },
        default={"ok": True},
    )


def _lag_client() -> FakeClient:
    return FakeClient(
        {
            "get_consumer_lag": {
                "lag": 0,
                "lag_known": True,
                "consumer_group": "worker-dispatcher",
            },
            "list_dlq_messages": {"total": 0, "items": [], "listing_complete": True},
            "list_active_alerts": {"total": 0, "alerts": []},
        },
        default={"ok": True},
    )


def _minimal_world() -> recorder.RecordedWorld:
    """One recorded call, assembled by hand — no scenario, no client, no derivation."""
    wired = wire_arguments(TOOL_REGISTRY["list_dlq_messages"], {})
    return recorder.RecordedWorld(
        schema_version=recorder.SCHEMA_VERSION,
        scenario="a_scenario",
        world=recorder.RecordedWorldLabel(label="a test world", live_mcp=True, chaos_seeded=True),
        calls=(
            recorder.RecordedCall(
                tool="list_dlq_messages",
                arguments=wired,
                key=recorder.call_key("list_dlq_messages", wired),
                result={
                    "content": [{"type": "text", "text": '{"total": 0, "items": []}'}],
                    "is_error": False,
                },
                started_at=_T1.isoformat(),
                completed_at=_T1.isoformat(),
                duration_ms=1,
            ),
        ),
        provenance=_provenance(),
    )


def _only_scenario_stub() -> Scenario:
    """The first scenario in the corpus, used where the test is about a call and not a scenario."""
    return next(iter(load_scenarios(_SCENARIOS_DIR)))
