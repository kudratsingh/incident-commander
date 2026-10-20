"""`make replay-coverage`: would an archived run replay with no misses? (WO-R3-387)

Three things are proved here: the call list read back from an archived trace holds what the run
asked for (the evidence AND the emitted step, because a missed read never reaches the evidence)
apart from what the planner only proposed; a lookup uses the replay client's own key, so a call
the recording holds under its wired defaults is a hit; and the recorder's ``PLANNER_PROPOSED_READS``
is exactly what the pilot's archives say, not a hand-typed list.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import pytest

from evals import recorder, replay_coverage
from evals.scenarios.loader import load_scenarios
from evals.scenarios.schema import ScenarioFamily
from incident_commander.tools.mcp_client import ToolResult
from incident_commander.tools.registry import TOOL_REGISTRY
from incident_commander.tools.wire import wire_arguments

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
_AT: Final[datetime] = datetime(2026, 10, 20, 12, 0, 0, tzinfo=UTC)
_ARCHIVE: Final[str] = "aaaabbbbcccc"
_SCENARIO: Final[str] = "api_latency_db_query"


def _recorded_call(tool: str, raw_args: Mapping[str, Any]) -> recorder.RecordedCall:
    wired = wire_arguments(TOOL_REGISTRY[tool], raw_args)
    result = ToolResult(content=[{"type": "text", "text": "{}"}], is_error=False)
    return recorder.RecordedCall(
        tool=tool,
        arguments=wired,
        key=recorder.call_key(tool, wired),
        result=result.model_dump(mode="json"),
        started_at=_AT.isoformat(),
        completed_at=_AT.isoformat(),
        duration_ms=3,
    )


def _world(*calls: recorder.RecordedCall) -> recorder.RecordedWorld:
    return recorder.RecordedWorld(
        schema_version=recorder.SCHEMA_VERSION,
        scenario=_SCENARIO,
        world=recorder.RecordedWorldLabel(
            label="a synthetic world, assembled in a unit test", live_mcp=True, chaos_seeded=True
        ),
        calls=calls,
        provenance=recorder.RecordedProvenance(
            recorded_at=_AT.isoformat(),
            invocation_id="ddddeeeeffff",
            commander_head="abc1234",
            platform_mcp_url="http://platform.invalid:8001/mcp",
            read_principal="PLATFORM_SMOKE_TOKEN (read-scoped)",
        ),
    )


def _probe(tool: str, **arguments: Any) -> dict[str, Any]:
    return {"kind": "probe", "tool_name": tool, "arguments": arguments}


def _write_archive(root: Path, *, mode: str = "recorded") -> None:
    """A canned archive in the pilot's shape: one planner call, one step, one trajectory."""
    run = root / "evals" / "runs" / _ARCHIVE
    (run / "traces").mkdir(parents=True)
    (run / "trajectories").mkdir()
    planner_output = {
        "candidates": [
            {
                "candidate_id": "C1",
                "next_probe": _probe("get_consumer_lag", consumer_group="worker-dispatcher"),
            },
            {"candidate_id": "C2", "next_probe": _probe("get_postgres_health")},
            {"candidate_id": "C3", "next_probe": None},
            # An action is not a read: a replay refuses it by tier, so it is never a miss.
            {
                "candidate_id": "C4",
                "next_probe": {"kind": "action", "tool_name": "replay_dlq_messages"},
            },
        ],
        "next_action": _probe("get_slo_status"),
    }
    records = [
        {"kind": "scenario_start", "scenario": _SCENARIO, "execution_mode": mode},
        {"kind": "llm", "role": "investigation_planner", "output": planner_output},
        # A planner call that failed to parse carries no output; it must be skipped, not crash.
        {"kind": "llm", "role": "investigation_planner", "parse_failed": True},
        {
            "kind": "step",
            "candidate_set": [{"candidate_id": "C1", "proposed_probe": "get_consumer_lag"}],
            "emitted_step": json.dumps(
                {"next_action": _probe("get_consumer_lag", consumer_group="worker-dispatcher")}
            ),
        },
        {"kind": "scenario_end", "scenario": _SCENARIO},
    ]
    (run / "traces" / f"{_SCENARIO}.jsonl").write_text(
        "\n".join(json.dumps(record) for record in records) + "\n"
    )
    trajectory = {
        "scenario": _SCENARIO,
        "checkpoints": [
            {
                "evidence": [
                    {"tool_name": "_triage", "arguments": {"severity": "critical"}},
                    {"tool_name": "get_circuit_breakers", "arguments": {}},
                ]
            }
        ],
    }
    (run / "trajectories" / f"{_SCENARIO}.json").write_text(json.dumps(trajectory))


class TestTheArchivedCallList:
    def test_asked_is_the_evidence_plus_the_emitted_step_and_the_rest_is_proposed(
        self, tmp_path: Path
    ) -> None:
        _write_archive(tmp_path)
        calls = {
            call.label: call.asked
            for call in replay_coverage.archived_calls(tmp_path, _ARCHIVE, _SCENARIO)
        }
        assert calls == {
            "get_consumer_lag(consumer_group='worker-dispatcher')": True,
            "get_circuit_breakers()": True,
            "get_postgres_health()": False,
            "get_slo_status()": False,
        }

    def test_only_recorded_mode_archives_are_listed(self, tmp_path: Path) -> None:
        _write_archive(tmp_path, mode="live")
        assert replay_coverage.recorded_archives(tmp_path, _SCENARIO) == []

    def test_a_missing_trace_is_an_empty_list(self, tmp_path: Path) -> None:
        assert replay_coverage.archived_calls(tmp_path, _ARCHIVE, _SCENARIO) == ()


class TestCountingMisses:
    def test_the_pilots_miss_is_counted_as_an_asked_miss(self, tmp_path: Path) -> None:
        _write_archive(tmp_path)
        narrow = _world(
            _recorded_call("get_circuit_breakers", {}),
            _recorded_call("get_postgres_health", {}),
            _recorded_call("get_slo_status", {}),
        )
        row = replay_coverage.coverage(
            narrow,
            replay_coverage.archived_calls(tmp_path, _ARCHIVE, _SCENARIO),
            archive=_ARCHIVE,
            recording="ddddeeeeffff",
        )
        assert [call.label for call in row.asked_misses] == [
            "get_consumer_lag(consumer_group='worker-dispatcher')"
        ]
        assert row.proposed_misses == ()

    def test_a_wider_recording_answers_every_call(self, tmp_path: Path) -> None:
        _write_archive(tmp_path)
        wide = _world(
            _recorded_call("get_circuit_breakers", {}),
            _recorded_call("get_postgres_health", {}),
            _recorded_call("get_slo_status", {}),
            _recorded_call("get_consumer_lag", {"consumer_group": "worker-dispatcher"}),
        )
        row = replay_coverage.coverage(
            wide,
            replay_coverage.archived_calls(tmp_path, _ARCHIVE, _SCENARIO),
            archive=_ARCHIVE,
            recording="ddddeeeeffff",
        )
        assert row.missed == ()
        assert len(row.asked) == 2

    def test_a_proposed_read_the_recording_lacks_is_reported_apart(self, tmp_path: Path) -> None:
        _write_archive(tmp_path)
        world = _world(
            _recorded_call("get_circuit_breakers", {}),
            _recorded_call("get_consumer_lag", {"consumer_group": "worker-dispatcher"}),
        )
        row = replay_coverage.coverage(
            world,
            replay_coverage.archived_calls(tmp_path, _ARCHIVE, _SCENARIO),
            archive=_ARCHIVE,
            recording="ddddeeeeffff",
        )
        assert row.asked_misses == ()
        assert sorted(call.label for call in row.proposed_misses) == [
            "get_postgres_health()",
            "get_slo_status()",
        ]

    def test_the_lookup_uses_the_wired_key(self) -> None:
        # `get_deploy_history` has defaults; the archive holds `{}` and the recording the wired
        # form. The replay client wires before it looks up, and so must this check.
        world = _world(_recorded_call("get_deploy_history", {}))
        call = replay_coverage.ArchivedCall("get_deploy_history", (), frozenset({"asked"}))
        row = replay_coverage.coverage(world, [call], archive=_ARCHIVE, recording="x")
        assert row.missed == ()


class TestThePlannerReadsAreDerivedFromThePilot:
    def test_the_recorder_constant_matches_the_archived_candidate_sets(self) -> None:
        corpus = load_scenarios(_REPO_ROOT / "evals" / "scenarios")
        for family, reads in recorder.PLANNER_PROPOSED_READS.items():
            scenarios = [s.name for s in corpus if s.family is ScenarioFamily(family)]
            derived = replay_coverage.proposed_reads(
                _REPO_ROOT, recorder.PLANNER_PROPOSED_SOURCES, scenarios
            )
            assert derived, f"no archived reads for {family}: the sources are missing"
            assert sorted(derived, key=repr) == sorted(reads, key=repr)

    def test_the_db_query_walk_records_the_read_the_pilot_missed(self) -> None:
        corpus = {s.name: s for s in load_scenarios(_REPO_ROOT / "evals" / "scenarios")}
        probes, _notes = recorder.recording_probes(corpus[_SCENARIO])
        wired, _wire_notes = recorder.wire_probes(probes)
        labels = {probe.label for probe in wired}
        assert "get_consumer_lag(consumer_group='worker-dispatcher')" in labels
        assert "get_consumer_lag(consumer_group='dependency-resolver')" in labels

    @pytest.mark.parametrize("family", ["dlq", "workflow_stuck", "jobs_not_progressing"])
    def test_other_families_walks_are_unchanged(self, family: str) -> None:
        corpus = load_scenarios(_REPO_ROOT / "evals" / "scenarios")
        for scenario in corpus:
            if scenario.family is not None and scenario.family.value == family:
                assert recorder.planner_probes(scenario) == []


class TestTheCommandLine:
    def test_the_pilot_archives_are_clean_against_the_pinned_recordings(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = replay_coverage.main(
            ["--plan", str(_REPO_ROOT / "evals" / "samples" / "oracle_gap_pilot.json")]
        )
        out = capsys.readouterr().out
        assert code == replay_coverage.EXIT_CLEAN, out
        assert "0 asked read(s) would miss" in out

    def test_the_old_db_query_recording_misses_the_pilots_read(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = replay_coverage.main(
            [
                "--plan",
                str(_REPO_ROOT / "evals" / "samples" / "oracle_gap_pilot.json"),
                "--world",
                _SCENARIO,
                "--recording",
                "301c99c4ae11",
            ]
        )
        out = capsys.readouterr().out
        assert code == replay_coverage.EXIT_MISSES
        assert "3 asked read(s) would miss" in out

    def test_recording_override_needs_one_world(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert replay_coverage.main(["--recording", "301c99c4ae11"]) == replay_coverage.EXIT_USAGE
