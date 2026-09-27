"""A scenario's own ``volatile:`` list, honoured by both drift walks (WO-R3-309).

``_VOLATILE`` is keyed by tool, so one scenario's moving counter was another's authored
premise: ``get_slo_status.objectives.total`` is a count of ``make traffic`` in the
``api_latency`` worlds and the cascade's graded premise in its own.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

import pytest
from pydantic import ValidationError

from evals import recorder, world_drift
from evals.fixture_drift import _VOLATILE, CannedCall, canned_calls, compare
from evals.scenarios.loader import load_scenarios
from evals.scenarios.schema import Scenario, VolatileField
from incident_commander.tools.mcp_client import ToolResult

_SCENARIOS_DIR: Final[Path] = Path(__file__).resolve().parents[2] / "evals" / "scenarios"
_API_LATENCY: Final[tuple[str, ...]] = (
    "api_latency_db_query",
    "api_latency_downstream",
    "api_latency_healthy_control",
    "api_latency_redis",
)


@pytest.fixture(scope="module")
def corpus() -> dict[str, Scenario]:
    return {scenario.name: scenario for scenario in load_scenarios(_SCENARIOS_DIR)}


def _slo_call(corpus: dict[str, Scenario], name: str) -> Any:
    return next(call for call in canned_calls([corpus[name]]) if call.tool == "get_slo_status")


def _quiet(payload: Mapping[str, Any]) -> dict[str, Any]:
    """The same reading on a stack with no traffic in its window."""
    quiet = copy.deepcopy(dict(payload))
    for objective in quiet["objectives"]:
        objective["total"] = 0
    return quiet


class TestTheSchema:
    def test_every_api_latency_world_declares_its_two_moving_counters(
        self, corpus: dict[str, Scenario]
    ) -> None:
        for name in _API_LATENCY:
            assert corpus[name].volatile_paths("get_slo_status") == {"objectives.total"}, name
            assert corpus[name].volatile_paths("get_outbox_status") == {"unpublished_count"}, name

    def test_the_cascade_keeps_its_counters_as_premise(self, corpus: dict[str, Scenario]) -> None:
        assert corpus["cascading_redis_starves_backpressure"].volatile == ()

    def test_the_per_tool_map_keeps_only_clocks_for_these_tools(self) -> None:
        assert _VOLATILE["get_slo_status"] == {"measured_at"}
        assert "unpublished_count" not in _VOLATILE["get_outbox_status"]

    @pytest.mark.parametrize(
        ("entry", "fragment"),
        [
            ({"tool": "get_slo_status", "path": "objectives[].total"}, "list markers"),
            ({"tool": "pause_dag", "path": "paused"}, "may only"),
            ({"tool": "get_slo_statu", "path": "objectives.total"}, "not a registered tool"),
        ],
    )
    def test_a_malformed_entry_is_refused(self, entry: dict[str, str], fragment: str) -> None:
        with pytest.raises(ValidationError, match=fragment):
            VolatileField(**entry, why="a reason long enough to be read as one")

    def test_a_reason_is_required(self) -> None:
        with pytest.raises(ValidationError):
            VolatileField(tool="get_slo_status", path="objectives.total", why="")

    def test_a_duplicate_entry_is_refused(self, corpus: dict[str, Scenario]) -> None:
        entry = {"tool": "get_slo_status", "path": "objectives.total", "why": "x" * 30}
        payload = corpus["api_latency_redis"].model_dump(mode="json")
        payload["volatile"] = [entry, entry]
        with pytest.raises(ValidationError, match="twice"):
            Scenario.model_validate(payload)


class TestTheFixtureWalk:
    def test_a_declared_counter_is_compared_by_type_only(self, corpus: dict[str, Scenario]) -> None:
        call = _slo_call(corpus, "api_latency_redis")
        assert "objectives.total" in call.volatile
        drifts = compare(call, _quiet(call.payload))
        assert [d.path for d in drifts] == []

    def test_the_same_path_is_still_compared_in_a_world_that_did_not_declare_it(
        self, corpus: dict[str, Scenario]
    ) -> None:
        call = _slo_call(corpus, "cascading_redis_starves_backpressure")
        drifts = compare(call, _quiet(call.payload))
        assert "objectives[].total[]" in {d.path for d in drifts}

    def test_a_type_change_on_a_declared_scalar_still_drifts(self) -> None:
        call = CannedCall(
            scenario="s",
            tool="get_outbox_status",
            arguments={},
            payload=_OUTBOX,
            volatile=frozenset({"unpublished_count"}),
        )
        assert compare(call, dict(_OUTBOX, unpublished_count=1)) == []
        assert [d.kind for d in compare(call, dict(_OUTBOX, unpublished_count="1"))] == ["type"]


def _recorded(tool: str, payload: Mapping[str, Any], scenario: str) -> recorder.RecordedCall:
    result = ToolResult(content=[{"type": "text", "text": json.dumps(payload)}])
    return recorder.RecordedCall(
        tool=tool,
        arguments={},
        key=recorder.call_key(tool, {}),
        result=result.model_dump(mode="json"),
        started_at="2026-09-19T09:00:00+00:00",
        completed_at="2026-09-19T09:00:00+00:00",
        duration_ms=3,
    )


def _world(scenario: str, *calls: recorder.RecordedCall) -> recorder.RecordedWorld:
    return recorder.RecordedWorld(
        schema_version=recorder.SCHEMA_VERSION,
        scenario=scenario,
        world=recorder.RecordedWorldLabel(label="synthetic", live_mcp=True, chaos_seeded=True),
        calls=calls,
        provenance=recorder.RecordedProvenance(
            recorded_at="2026-09-19T09:00:00+00:00",
            invocation_id="aaaabbbbcccc",
            commander_head="abc1234",
            platform_mcp_url="http://platform.invalid:8001/mcp",
            read_principal="PLATFORM_SMOKE_TOKEN (read-scoped)",
        ),
    )


_OUTBOX: Final[dict[str, Any]] = {"unpublished_count": 0, "relay_heartbeat_known": True}


class TestTheWorldWalk:
    def _drifts(
        self, corpus: dict[str, Scenario], tool: str, recorded: Any, live: Any, *, declared: bool
    ) -> set[str]:
        name = "api_latency_redis"
        world = _world(name, _recorded(tool, recorded, name))
        drifts = world_drift.drift_between(
            world, [_recorded(tool, live, name)], scenario=corpus[name] if declared else None
        )
        return {d.path for d in drifts}

    def test_the_traffic_count_is_not_drift_in_an_api_latency_world(
        self, corpus: dict[str, Scenario]
    ) -> None:
        recorded = _slo_call(corpus, "api_latency_redis").payload
        live = _quiet(recorded)
        assert self._drifts(corpus, "get_slo_status", recorded, live, declared=True) == set()
        assert "objectives[].total[]" in self._drifts(
            corpus, "get_slo_status", recorded, live, declared=False
        )

    def test_the_outbox_flicker_is_not_drift_in_an_api_latency_world(
        self, corpus: dict[str, Scenario]
    ) -> None:
        live = dict(_OUTBOX, unpublished_count=1)
        assert self._drifts(corpus, "get_outbox_status", _OUTBOX, live, declared=True) == set()
        assert self._drifts(corpus, "get_outbox_status", _OUTBOX, live, declared=False) == {
            "unpublished_count"
        }
