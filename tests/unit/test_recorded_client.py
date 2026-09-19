"""`RecordedMCPClient` — the replay half of recorded mode (WP-3.2, WO-R3-197).

Four properties, each of whose failures looks like a working benchmark: the key is the
WIRED call through the recorder's own key function (F2, ADR 0043 § 1); a miss is answered,
counted and visible, never an empty healthy-looking result; a Tier-1 call is refused by
``policies.tier_of`` and RAISES; and the clocks re-base by a registry-derived list.
"""

from __future__ import annotations

import ast
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, get_args, get_origin

import pytest
from pydantic import BaseModel

from evals import recorded_client, recorder
from evals.fakes import CannedMCPClient
from evals.recorded_client import (
    NOT_RECORDED,
    RecordedMCPClient,
    ReplayMiss,
    ReplayRefused,
)
from evals.scenarios.loader import load_scenarios
from evals.scenarios.schema import Scenario
from incident_commander.agent.investigation import make_llm_investigate
from incident_commander.agent.state import IncidentState, RunState
from incident_commander.llm.fakes import CannedLLMClient
from incident_commander.tools.mcp_client import MCPError, ToolResult
from incident_commander.tools.policies import Tier, tier_of
from incident_commander.tools.registry import TOOL_REGISTRY
from incident_commander.tools.wire import wire_arguments

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
_SCENARIOS_DIR: Final[Path] = _REPO_ROOT / "evals" / "scenarios"
_RECORDED_WORLDS: Final[Path] = _REPO_ROOT / "evals" / "recorded_worlds"

#: The moment every synthetic recording below was "taken".
_RECORDED_AT: Final[datetime] = datetime(2026, 9, 17, 12, 0, 0, tzinfo=UTC)
#: The moment it is replayed at — eleven days and a bit later, which is the
#: point: a recording replayed the same afternoon hides every clock bug.
_REPLAY_AT: Final[datetime] = datetime(2026, 9, 28, 9, 30, 0, tzinfo=UTC)


# --------------------------------------------------------------------------
# Building a recording by hand
# --------------------------------------------------------------------------


def _provenance(recorded_at: datetime = _RECORDED_AT) -> recorder.RecordedProvenance:
    return recorder.RecordedProvenance(
        recorded_at=recorded_at.isoformat(),
        invocation_id="aaaabbbbcccc",
        commander_head="abc1234",
        platform_mcp_url="http://platform.invalid:8001/mcp",
        read_principal="PLATFORM_SMOKE_TOKEN (read-scoped)",
    )


def _recorded_call(
    tool: str,
    raw_args: Mapping[str, Any],
    payload: Mapping[str, Any],
    *,
    at: datetime = _RECORDED_AT,
    is_error: bool = False,
) -> recorder.RecordedCall:
    """One call recorded the way the recorder records it — WIRED, keyed by ``call_key``."""
    wired = wire_arguments(TOOL_REGISTRY[tool], raw_args)
    result = ToolResult(
        content=[{"type": "text", "text": json.dumps(payload, separators=(",", ":"))}],
        is_error=is_error,
    )
    return recorder.RecordedCall(
        tool=tool,
        arguments=wired,
        key=recorder.call_key(tool, wired),
        result=result.model_dump(mode="json"),
        started_at=at.isoformat(),
        completed_at=at.isoformat(),
        duration_ms=7,
    )


def _world(
    *calls: recorder.RecordedCall,
    scenario: str = "dlq_backlog",
    recorded_at: datetime = _RECORDED_AT,
) -> recorder.RecordedWorld:
    return recorder.RecordedWorld(
        schema_version=recorder.SCHEMA_VERSION,
        scenario=scenario,
        world=recorder.RecordedWorldLabel(
            label="a synthetic world, assembled in a unit test",
            live_mcp=True,
            chaos_seeded=True,
        ),
        calls=tuple(calls),
        provenance=_provenance(recorded_at),
    )


def _payload_of(result: ToolResult) -> dict[str, Any]:
    """The first JSON object in a result's text blocks."""
    for block in result.content:
        text = block.get("text")
        if block.get("type") == "text" and isinstance(text, str):
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
    raise AssertionError("no JSON object in the result")


_DLQ_PAYLOAD: Final[dict[str, Any]] = {
    "total": 1,
    "items": [
        {
            "id": "11111111-1111-5111-8111-111111111111",
            "type": "email_send",
            "remediation_hint": "replay_safe",
            "error_message": "ConnectionRefusedError: smtp upstream",
            "retry_count": 3,
            "created_at": "2026-09-17T11:40:00Z",
            "updated_at": "2026-09-17T11:55:00Z",
            "dead_lettered_at": "2026-09-17T11:58:00Z",
            "fenced_at": None,
        }
    ],
}

_LAG_PAYLOAD: Final[dict[str, Any]] = {
    "consumer_group": "worker-dispatcher",
    "lag": 29,
    "lag_known": True,
    "source": "live",
    "cache_key": "kafka:consumer_lag:worker-dispatcher",
    "measured_at": "2026-09-17T11:59:48Z",
    "age_seconds": 12,
    "recent_samples": [
        {"lag": 29, "measured_at": "2026-09-17T11:59:48Z"},
        {"lag": 29, "measured_at": "2026-09-17T11:58:48Z"},
    ],
}

_REDIS_PAYLOAD: Final[dict[str, Any]] = {
    "ok": True,
    "ping_latency_ms": 0.225,
    "connected_clients": 12,
    "used_memory_bytes": 1798912,
    "used_memory_human": "1.72M",
    "keyspace_hits": 12259,
    "keyspace_misses": 1622361,
    "error": None,
}


@pytest.fixture(scope="module")
def scenarios() -> dict[str, Scenario]:
    return {s.name: s for s in load_scenarios(_SCENARIOS_DIR)}


def _committed() -> list[Path]:
    return sorted(p for p in _RECORDED_WORLDS.rglob("*.json") if not p.name.endswith(".truth.json"))


# --------------------------------------------------------------------------


class TestTheKeyIsTheWiredCall:
    """Divergence F2 on the lookup side — the property the whole mode rests on.

    RED BEFORE: replace ``replay_key``'s ``wire_arguments`` with ``dict(arguments)`` and both
    tests fail — ``list_dlq_messages({})`` hashes to something no recording holds, so the
    client answers ``not_recorded`` to every read.
    """

    def test_an_omitted_optional_is_answered_because_the_lookup_wires_first(self) -> None:
        client = RecordedMCPClient(
            _world(_recorded_call("list_dlq_messages", {}, _DLQ_PAYLOAD)),
            replay_clock=_RECORDED_AT,
        )
        result = client.call_tool("list_dlq_messages", {})
        assert result.is_error is False
        assert _payload_of(result)["total"] == 1
        assert client.misses == []
        assert client.answered == 1

    def test_the_agents_own_wired_call_is_answered(self) -> None:
        """What the agent actually sends: every optional default-filled."""
        client = RecordedMCPClient(
            _world(_recorded_call("list_dlq_messages", {}, _DLQ_PAYLOAD)),
            replay_clock=_RECORDED_AT,
        )
        wired = wire_arguments(TOOL_REGISTRY["list_dlq_messages"], {})
        assert wired == {"job_type": None, "remediation_hint": None, "limit": 50, "offset": 0}
        result = client.call_tool("list_dlq_messages", wired)
        assert result.is_error is False
        assert client.misses == []

    def test_the_raw_key_cannot_find_the_answer(self) -> None:
        """The red-before, stated as a fact about the two keys.

        A lookup keyed on what the planner wrote computes a different hash.
        """
        world = _world(_recorded_call("list_dlq_messages", {}, _DLQ_PAYLOAD))
        raw_key = recorder.call_key("list_dlq_messages", {})
        assert raw_key not in world.keys
        assert recorded_client.replay_key("list_dlq_messages", {}) in world.keys

    def test_the_key_function_is_the_recorders_own(self) -> None:
        """One function computes the key on both sides (ADR 0043 § 1).

        Read off the syntax tree: the replay module IMPORTS it and defines no second one.
        """
        tree = ast.parse((_REPO_ROOT / "evals" / "recorded_client.py").read_text())
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module == "evals.recorder"
            for alias in node.names
        }
        assert "call_key" in imported
        defined = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
        assert "call_key" not in defined
        assert "arguments_hash" not in defined

    def test_the_client_answers_what_the_recording_answers(self) -> None:
        """Every committed call, through both paths, at the recording's own clock."""
        for path in _committed():
            world = recorder.load_recording(path)
            client = RecordedMCPClient(
                world, replay_clock=datetime.fromisoformat(world.provenance.recorded_at)
            )
            for call in world.calls:
                through_client = client.call_tool(call.tool, call.arguments)
                through_world = world.answer(call.tool, call.arguments)
                assert through_world is not None, f"{path.name}: {call.tool}"
                assert through_client.model_dump(mode="json") == through_world.model_dump(
                    mode="json"
                ), f"{path.name}: {call.tool}"
            assert client.misses == [], path.name

    def test_an_unwireable_tool_argument_is_a_miss_not_a_crash(self) -> None:
        client = RecordedMCPClient(
            _world(
                _recorded_call(
                    "get_dag_state",
                    {"job_id": "11111111-1111-5111-8111-111111111111"},
                    {"seed_id": "x", "nodes": [], "edges": [], "paused": False},
                )
            ),
            replay_clock=_RECORDED_AT,
        )
        result = client.call_tool("get_dag_state", {"job_id": "not-a-uuid"})
        assert result.is_error is True
        assert _payload_of(result)["error"] == NOT_RECORDED
        assert len(client.misses) == 1
        assert "do not validate" in client.misses[0].detail


class TestAMissIsCountedAndVisible:
    """A recording that cannot answer must never look like a world that says "nothing"."""

    def _client(self) -> RecordedMCPClient:
        return RecordedMCPClient(
            _world(_recorded_call("list_dlq_messages", {}, _DLQ_PAYLOAD)),
            replay_clock=_RECORDED_AT,
        )

    def test_a_probe_for_an_unrecorded_argument_is_an_error_not_an_empty_answer(self) -> None:
        """RED BEFORE: a client that returned ``ToolResult()`` here passes "it answered"."""
        client = self._client()
        result = client.call_tool("list_dlq_messages", {"remediation_hint": "replay_safe"})
        assert result.is_error is True
        payload = _payload_of(result)
        assert payload["error"] == NOT_RECORDED
        assert "total" not in payload and "items" not in payload

    def test_the_miss_is_counted_and_carries_the_key_it_looked_up(self) -> None:
        client = self._client()
        client.call_tool("list_dlq_messages", {"remediation_hint": "replay_safe"})
        assert len(client.misses) == 1
        miss = client.misses[0]
        assert isinstance(miss, ReplayMiss)
        assert miss.tool == "list_dlq_messages"
        assert miss.reason == NOT_RECORDED
        assert miss.key.startswith("list_dlq_messages ")
        assert miss.key not in client.world.keys

    def test_a_run_with_a_miss_is_degraded_and_the_summary_says_so(self) -> None:
        client = self._client()
        client.call_tool("list_dlq_messages", {})
        client.call_tool("get_redis_health", {})
        assert client.degraded is True
        summary = client.summary()
        assert summary["answered"] == 1
        assert summary["misses"] == 1
        assert summary["degraded"] is True
        assert summary["miss_details"] == [
            {
                "tool": "get_redis_health",
                "reason": NOT_RECORDED,
                "key": summary["miss_details"][0]["key"],
            }
        ]
        assert summary["world_fingerprint"] == recorder.world_fingerprint(client.world)

    def test_a_clean_replay_is_not_degraded(self) -> None:
        client = self._client()
        client.call_tool("list_dlq_messages", {})
        assert client.degraded is False
        assert client.summary()["misses"] == 0

    def test_the_miss_text_names_no_scenario_and_no_lab_vocabulary(self) -> None:
        """The one part of a replay the agent reads must not hand it the answer."""
        client = RecordedMCPClient(
            _world(
                _recorded_call("list_dlq_messages", {}, _DLQ_PAYLOAD),
                scenario="remediate_consumer_lag_success",
            ),
            replay_clock=_RECORDED_AT,
        )
        result = client.call_tool("get_redis_health", {})
        blob = json.dumps(result.model_dump(mode="json")).lower()
        assert "remediate_consumer_lag_success" not in blob
        for term in sorted(recorder.lab_vocabulary_terms()):
            assert term not in blob, term
        # The scenario IS named in the evaluator-side miss record, which is
        # where an operator needs it.
        assert "remediate_consumer_lag_success" in client.misses[0].detail


class TestTier1IsRefusedByItsTier:
    """Refused by ``tier_of`` over the whole registry, not by a list somebody types."""

    def _client(self) -> RecordedMCPClient:
        return RecordedMCPClient(
            _world(_recorded_call("list_dlq_messages", {}, _DLQ_PAYLOAD)),
            replay_clock=_RECORDED_AT,
        )

    def test_every_non_read_tool_in_the_registry_is_refused(self) -> None:
        above_read = sorted(name for name in TOOL_REGISTRY if tier_of(name) is not Tier.READ)
        assert above_read, "the registry has no tool above read — this test would prove nothing"
        for name in above_read:
            client = self._client()
            with pytest.raises(ReplayRefused) as raised:
                client.call_tool(name, {})
            assert tier_of(name).value in str(raised.value)
            assert client.refusals == [(name, tier_of(name).value)]

    def test_the_refusal_is_not_an_mcp_error_so_no_escalate_rail_absorbs_it(self) -> None:
        """An ``MCPError`` here would be filed as an incident the agent handled."""
        client = self._client()
        with pytest.raises(ReplayRefused) as raised:
            client.call_tool("restart_consumer_group", {})
        assert not isinstance(raised.value, MCPError)

    def test_a_read_tool_is_not_refused(self) -> None:
        client = self._client()
        client.call_tool("list_dlq_messages", {})
        assert client.refusals == []

    def test_a_tool_outside_the_registry_is_refused_rather_than_answered(self) -> None:
        client = self._client()
        with pytest.raises(ReplayRefused, match="TOOL_REGISTRY"):
            client.call_tool("drop_everything", {})
        assert client.refusals == [("drop_everything", "unclassifiable")]

    def test_no_recorded_call_is_above_read_so_a_refusal_can_never_hide_one(self) -> None:
        for path in _committed():
            for call in recorder.load_recording(path).calls:
                assert tier_of(call.tool) is Tier.READ, f"{path.name}: {call.tool}"


class TestTheClockIsRebased:
    """The recording is the world as it was, NOW.

    RED BEFORE: drop ``rebase_result`` and ``measured_at`` comes back eleven days
    behind the replay clock.
    """

    def _lag_client(self, replay_clock: datetime = _REPLAY_AT) -> RecordedMCPClient:
        return RecordedMCPClient(
            _world(_recorded_call("get_consumer_lag", {}, _LAG_PAYLOAD)),
            replay_clock=replay_clock,
        )

    def test_an_absolute_clock_moves_to_the_replay_timeline(self) -> None:
        payload = _payload_of(self._lag_client().call_tool("get_consumer_lag", {}))
        measured_at = datetime.fromisoformat(payload["measured_at"])
        assert _REPLAY_AT - measured_at == timedelta(seconds=12)

    def test_a_relative_duration_is_held_because_the_replay_clock_is_the_reading(self) -> None:
        payload = _payload_of(self._lag_client().call_tool("get_consumer_lag", {}))
        assert payload["age_seconds"] == 12

    def test_the_two_halves_agree_at_the_replay_clock(self) -> None:
        """The property the whole re-base exists for: the reading is coherent NOW."""
        payload = _payload_of(self._lag_client().call_tool("get_consumer_lag", {}))
        recomputed = (_REPLAY_AT - datetime.fromisoformat(payload["measured_at"])).total_seconds()
        assert int(recomputed) == payload["age_seconds"]

    def test_a_nested_clock_inside_a_list_moves_too(self) -> None:
        payload = _payload_of(self._lag_client().call_tool("get_consumer_lag", {}))
        samples = [datetime.fromisoformat(s["measured_at"]) for s in payload["recent_samples"]]
        assert _REPLAY_AT - samples[0] == timedelta(seconds=12)
        # The 60-second gap between two samples is a relationship inside the
        # world and a rigid shift keeps it exactly.
        assert samples[0] - samples[1] == timedelta(seconds=60)

    def test_every_clock_in_the_document_moves_by_one_offset(self) -> None:
        client = RecordedMCPClient(
            _world(
                _recorded_call("list_dlq_messages", {}, _DLQ_PAYLOAD),
                _recorded_call("get_consumer_lag", {}, _LAG_PAYLOAD),
            ),
            replay_clock=_REPLAY_AT,
        )
        row = _payload_of(client.call_tool("list_dlq_messages", {}))["items"][0]
        lag = _payload_of(client.call_tool("get_consumer_lag", {}))
        offset = timedelta(seconds=int(client.summary()["replay_offset_seconds"]))
        assert (
            datetime.fromisoformat(row["created_at"])
            == datetime.fromisoformat("2026-09-17T11:40:00Z") + offset
        )
        assert (
            datetime.fromisoformat(row["dead_lettered_at"])
            == datetime.fromisoformat("2026-09-17T11:58:00Z") + offset
        )
        assert (
            datetime.fromisoformat(lag["measured_at"])
            == datetime.fromisoformat("2026-09-17T11:59:48Z") + offset
        )

    def test_a_null_clock_stays_null(self) -> None:
        row = _payload_of(self._dlq().call_tool("list_dlq_messages", {}))["items"][0]
        assert row["fenced_at"] is None

    def _dlq(self) -> RecordedMCPClient:
        return RecordedMCPClient(
            _world(_recorded_call("list_dlq_messages", {}, _DLQ_PAYLOAD)),
            replay_clock=_REPLAY_AT,
        )

    def test_the_z_suffix_survives(self) -> None:
        payload = _payload_of(self._dlq().call_tool("list_dlq_messages", {}))
        assert payload["items"][0]["created_at"].endswith("Z")

    def test_replaying_at_the_recording_moment_changes_nothing(self) -> None:
        """Zero offset returns the recorded bytes, so a recording is its own replay."""
        call = _recorded_call("list_dlq_messages", {}, _DLQ_PAYLOAD)
        client = RecordedMCPClient(_world(call), replay_clock=_RECORDED_AT)
        assert client.summary()["replay_offset_seconds"] == 0
        assert client.call_tool("list_dlq_messages", {}).model_dump(mode="json") == call.result

    def test_a_result_with_no_declared_clock_is_byte_identical(self) -> None:
        call = _recorded_call("get_redis_health", {}, _REDIS_PAYLOAD)
        client = RecordedMCPClient(_world(call), replay_clock=_REPLAY_AT)
        assert client.summary()["replay_offset_seconds"] != 0
        assert client.call_tool("get_redis_health", {}).model_dump(mode="json") == call.result

    def test_the_two_tables_are_disjoint_and_name_only_read_tools(self) -> None:
        shifted = recorded_client.SHIFTED_CLOCK_FIELDS
        held = recorded_client.HELD_DURATION_FIELDS
        for table in (shifted, held):
            for tool in table:
                assert tool in TOOL_REGISTRY, tool
                assert tier_of(tool) is Tier.READ, tool
        for tool in set(shifted) & set(held):
            assert not shifted[tool] & held[tool], tool

    def test_the_written_down_list_covers_every_time_field_the_registry_declares(self) -> None:
        """Derived here, written down there — so a new field fails this test.

        The module states which fields move and why; this states that none was left out.
        """
        expected_clocks: dict[str, set[str]] = {}
        expected_durations: dict[str, set[str]] = {}
        for name in sorted(TOOL_REGISTRY):
            if tier_of(name) is not Tier.READ:
                continue
            clocks, durations = _time_fields(TOOL_REGISTRY[name].output_model)
            if clocks:
                expected_clocks[name] = clocks
            if durations:
                expected_durations[name] = durations
        assert {t: set(v) for t, v in recorded_client.SHIFTED_CLOCK_FIELDS.items()} == (
            expected_clocks
        )
        assert {t: set(v) for t, v in recorded_client.HELD_DURATION_FIELDS.items()} == (
            expected_durations
        )


def _is_duration_name(name: str) -> bool:
    """Does this field name say "a number of seconds"?

    Three spellings, all the platform's: ``_seconds``, ``_s`` (v0.6.9's outbox reading) and
    ``seconds_since_last_publish``. ``_seconds`` alone missed five real durations.
    """
    return name.endswith(("_seconds", "_s")) or name.startswith("seconds_")


def _time_fields(model: type[BaseModel], prefix: str = "") -> tuple[set[str], set[str]]:
    """Every datetime path and every duration-named number path in one output model.

    A local walk, not an import: the hand-written table is the subject.
    """
    clocks: set[str] = set()
    durations: set[str] = set()
    for name, field in model.model_fields.items():
        path = f"{prefix}{name}"
        for annotation in _unwrap(field.annotation):
            if annotation is datetime:
                clocks.add(path)
            elif isinstance(annotation, type) and issubclass(annotation, BaseModel):
                nested_clocks, nested_durations = _time_fields(annotation, f"{path}.")
                clocks |= nested_clocks
                durations |= nested_durations
            elif annotation in (int, float) and _is_duration_name(name):
                durations.add(path)
    return clocks, durations


def _unwrap(annotation: Any) -> list[Any]:
    """Every concrete type inside an annotation — ``list[X] | None`` → ``[X, None]``."""
    found: list[Any] = []
    stack = [annotation]
    while stack:
        current = stack.pop()
        if get_origin(current) is None:
            found.append(current)
        else:
            stack.extend(get_args(current))
    return found


class TestItRefusesToConstructWithoutARecording:
    """A client with nothing to replay would answer every call ``not_recorded``."""

    def test_an_empty_recording_refuses(self) -> None:
        with pytest.raises(ValueError, match="nothing to replay"):
            RecordedMCPClient(_world())

    def test_no_recording_at_all_refuses(self) -> None:
        with pytest.raises(TypeError, match="requires a loaded RecordedWorld"):
            RecordedMCPClient(None)  # type: ignore[arg-type]

    def test_a_recording_whose_key_was_edited_refuses(self) -> None:
        """A stored key that no longer matches its arguments would answer the wrong call."""
        call = _recorded_call("list_dlq_messages", {}, _DLQ_PAYLOAD)
        tampered = call.model_copy(update={"key": "list_dlq_messages deadbeef"})
        with pytest.raises(ValueError, match="has been edited"):
            RecordedMCPClient(_world(tampered), replay_clock=_RECORDED_AT)

    def test_it_loads_one_path_and_has_no_other_entry_point(self) -> None:
        path = _committed()[0]
        client = RecordedMCPClient.from_path(path, replay_clock=_RECORDED_AT)
        assert client.world.scenario == path.parent.name

    def test_nothing_in_this_module_could_reach_a_real_platform(self) -> None:
        """The structural half of "never falls back to a real client" (ADR 0013).

        Read off the syntax tree, so prose may name ``make_client``.
        """
        tree = ast.parse((_REPO_ROOT / "evals" / "recorded_client.py").read_text())
        modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                modules.add(node.module or "")
        assert "httpx" not in modules
        assert not any(module.startswith("incident_commander.config") for module in modules)
        named = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {
            n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
        }
        for forbidden in ("make_client", "MCPClient", "Settings", "get_secret_value"):
            assert forbidden not in named, forbidden
        # The transport module contributes exactly one name: the answer type.
        transport = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            and node.module == "incident_commander.tools.mcp_client"
        ]
        assert [alias.name for node in transport for alias in node.names] == ["ToolResult"]


class TestARecordedTrajectoryMatchesTheCannedOne:
    """The acceptance from the order: a replay reproduces a canned-equivalent run.

    ``dlq_backlog`` has both forms and no action leg. The two worlds hold DIFFERENT VALUES —
    that is the point of a recording — so what must match is the SHAPE: same probe, same
    wired arguments, same order, same terminal state, same tool-call count, no miss.
    """

    def _run(self, scenario: Scenario, client: Any, run_state: RunState, now: datetime) -> RunState:
        planner = CannedLLMClient(list(scenario.canned_llm_responses["investigation_planner"]))
        transition = make_llm_investigate(client, planner, model="canned-model")
        start = run_state.model_copy(
            update={"state": IncidentState.INVESTIGATING, "alert": dict(scenario.alert)}
        )
        return transition(start, now)

    def test_the_replay_reproduces_the_canned_trajectory(
        self, scenarios: dict[str, Scenario], run_state: RunState, now: datetime
    ) -> None:
        scenario = scenarios["dlq_backlog"]
        canned = CannedMCPClient(scenario.canned_tool_responses)
        recorded = RecordedMCPClient.from_path(_newest_recording("dlq_backlog"), replay_clock=now)

        canned_run = self._run(scenario, canned, run_state, now)
        recorded_run = self._run(scenario, recorded, run_state, now)

        assert canned.calls == recorded.calls
        assert [c[0] for c in recorded.calls] == ["list_dlq_messages"]
        assert recorded_run.state is canned_run.state
        assert len(recorded_run.evidence) == len(canned_run.evidence)
        assert [e.tool_name for e in recorded_run.evidence] == [
            e.tool_name for e in canned_run.evidence
        ]
        assert [e.arguments for e in recorded_run.evidence] == [
            e.arguments for e in canned_run.evidence
        ]
        assert recorded_run.budget.tool_calls_used == canned_run.budget.tool_calls_used
        assert recorded.misses == []
        assert recorded.refusals == []

    def test_the_replayed_evidence_is_the_platforms_own_answer(
        self, scenarios: dict[str, Scenario], run_state: RunState, now: datetime
    ) -> None:
        """Equivalent in shape, not in content — and the content is the live world's."""
        scenario = scenarios["dlq_backlog"]
        recorded = RecordedMCPClient.from_path(_newest_recording("dlq_backlog"), replay_clock=now)
        run = self._run(scenario, recorded, run_state, now)
        assert run.evidence
        assert recorded.answered == 1


def _newest_recording(scenario: str) -> Path:
    found = [p for p in _committed() if p.parent.name == scenario]
    assert found, f"no committed recording for {scenario}"
    return found[-1]


class TestTheCommittedRecordingsAllReplay:
    """Every recording this repo carries, exercised through the client that replays it.

    ``test_recorder.py`` holds them to loading and answering; this is the client a run is
    handed, at a replay clock that is not the recording's.
    """

    def test_every_recording_serves_every_call_it_holds(self) -> None:
        for path in _committed():
            world = recorder.load_recording(path)
            client = RecordedMCPClient(world, replay_clock=_REPLAY_AT)
            for call in world.calls:
                result = client.call_tool(call.tool, call.arguments)
                assert result.is_error is False, f"{path.name}: {call.tool}"
            assert client.misses == [], path.name
            assert client.degraded is False, path.name
            assert client.answered == len(world.calls), path.name

    def test_every_rebased_result_still_parses_as_the_tools_output(self) -> None:
        """A re-base that produced an unparseable payload would escalate every run."""
        for path in _committed():
            world = recorder.load_recording(path)
            client = RecordedMCPClient(world, replay_clock=_REPLAY_AT)
            for call in world.calls:
                result = client.call_tool(call.tool, call.arguments)
                model = TOOL_REGISTRY[call.tool].output_model
                model.model_validate(_payload_of(result))

    def test_the_rebase_moved_something_somewhere(self) -> None:
        """Otherwise every assertion above would hold against a no-op re-base."""
        moved = 0
        for path in _committed():
            world = recorder.load_recording(path)
            client = RecordedMCPClient(world, replay_clock=_REPLAY_AT)
            for call in world.calls:
                if client.call_tool(call.tool, call.arguments).model_dump(mode="json") != (
                    call.result
                ):
                    moved += 1
        assert moved, "no committed recording carries a clock — the re-base is untested here"

    def test_nothing_a_replayed_agent_sees_names_the_lab_after_rebasing(self) -> None:
        """The recorder lints the recording; this lints what the client actually serves."""
        terms = sorted(recorder.lab_vocabulary_terms())
        for path in _committed():
            world = recorder.load_recording(path)
            client = RecordedMCPClient(world, replay_clock=_REPLAY_AT)
            for call in world.calls:
                blob = json.dumps(
                    client.call_tool(call.tool, call.arguments).model_dump(mode="json")
                ).lower()
                for term in terms:
                    assert term not in blob, f"{path.name}: {call.tool}: {term}"
