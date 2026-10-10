"""Platform v0.6.24 (plat #243, platform ADR 0041, INC-008): the agent's half of two new readings.

`get_consumer_lag` gains `last_poll_at` / `last_poll_age_seconds` (when the group's consumer last
asked Kafka for work), and `get_control_loops` is a new read tool (each background loop's pause
state and last pass). These tests pin the policy decision (a read, like `get_consumer_lag`) and
that both readings parse into the typed models instead of being dropped by `extra="ignore"`.

RED BEFORE: on `main` at the v0.6.23 pin, `get_control_loops` is in no tier, not in the registry
and not a probe the planner may name, and `GetConsumerLagOutput` has no poll fields, so every
test below fails there.
"""

from __future__ import annotations

import json
import typing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import pytest
from pydantic import ValidationError

from evals import recorded_client
from incident_commander.agent.hypothesis import ProbeAction, ReadToolName
from incident_commander.tools.mcp_client import ToolResult
from incident_commander.tools.policies import (
    RESOURCE_ARG_FIELDS,
    UUID_RESOURCE_FIELDS,
    Tier,
    is_cached_read,
    tier_of,
    tools_at_or_below,
)
from incident_commander.tools.registry import (
    TOOL_REGISTRY,
    ControlLoopName,
    GetConsumerLagOutput,
    GetControlLoopsOutput,
)

_SNAPSHOT: Final[Path] = (
    Path(__file__).resolve().parents[2] / "contracts" / "platform-tools.snapshot.json"
)

#: `get_consumer_lag(dependency-resolver)` as the v0.6.24 demo stack answered it on 2026-10-10,
#: under the read-only token (`recent_samples` trimmed to one entry).
_RESOLVER_LIVE: Final[dict[str, Any]] = {
    "consumer_group": "dependency-resolver",
    "lag": 0,
    "lag_known": True,
    "source": "live",
    "cache_key": "kafka:consumer_lag:dependency-resolver",
    "measured_at": "2026-10-10T14:54:37.452048Z",
    "age_seconds": 1,
    "recent_samples": [{"lag": 0, "measured_at": "2026-10-10T14:54:37.452048Z"}],
    "last_poll_at": "2026-10-10T14:54:37.441963Z",
    "last_poll_age_seconds": 1,
}

#: The same stack's answer for one of the seven recorded-constant groups.
_STATIC_LIVE: Final[dict[str, Any]] = {
    "consumer_group": "billing-consumer",
    "lag": 15000,
    "lag_known": True,
    "source": "static",
    "cache_key": "kafka:consumer_lag:billing-consumer",
    "measured_at": None,
    "age_seconds": None,
    "recent_samples": [],
    "last_poll_at": None,
    "last_poll_age_seconds": None,
}


def _loop(name: str, **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "name": name,
        "paused": False,
        "paused_expires_in_seconds": None,
        "tick_interval_seconds": 1.0,
        "last_run_at": "2026-10-10T14:54:36.900000Z",
        "last_run_age_seconds": 0.6,
    }
    row.update(overrides)
    return row


def _loops_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "measured_at": "2026-10-10T14:54:37.500000Z",
        "loops": [_loop(member.value) for member in ControlLoopName],
        "total": len(ControlLoopName),
        "unknown_reason": None,
    }
    payload.update(overrides)
    return payload


class TestControlLoopsIsARead:
    """`get_control_loops` sits where `get_consumer_lag` sits: the read tier, every state."""

    def test_it_is_a_read_tool(self) -> None:
        assert tier_of("get_control_loops") is Tier.READ
        assert tier_of("get_control_loops") is tier_of("get_consumer_lag")

    def test_the_investigation_planner_may_propose_it(self) -> None:
        assert "get_control_loops" in tools_at_or_below(Tier.READ)
        assert "get_control_loops" in typing.get_args(ReadToolName)
        probe = ProbeAction(tool_name="get_control_loops", arguments={})
        assert probe.tool_name == "get_control_loops"

    def test_it_is_not_an_action(self) -> None:
        actions = tools_at_or_below(Tier.TIER_1) - tools_at_or_below(Tier.READ)
        assert "get_control_loops" not in actions

    def test_it_takes_no_arguments_and_names_no_resource(self) -> None:
        assert RESOURCE_ARG_FIELDS["get_control_loops"] == frozenset()
        assert UUID_RESOURCE_FIELDS["get_control_loops"] == frozenset()
        input_model = TOOL_REGISTRY["get_control_loops"].input_model
        assert input_model.model_validate({}).model_dump() == {}
        with pytest.raises(ValidationError):
            input_model.model_validate({"loop_name": "metrics"})

    def test_it_is_answered_at_call_time_not_from_a_cache(self) -> None:
        # The platform reads each pause key and pass record when asked; nothing here is a
        # cached measurement that a second read inside a window could contradict.
        assert not is_cached_read("get_control_loops")

    def test_the_platform_grants_it_under_the_read_scope(self) -> None:
        entry = _snapshot_tool("get_control_loops")
        assert entry["required_scope"] == "telemetry:read"
        assert not entry["description"].startswith(("[chaos:", "[commander:"))


class TestConsumerLagPollFieldsParse:
    """The two new fields reach the typed model rather than being thrown away."""

    def test_a_live_group_carries_its_poll_time(self) -> None:
        reading = GetConsumerLagOutput.model_validate(_RESOLVER_LIVE)
        assert reading.source == "live"
        assert reading.last_poll_at == datetime(2026, 10, 10, 14, 54, 37, 441963, tzinfo=UTC)
        assert reading.last_poll_age_seconds == 1

    def test_a_static_group_reads_null_which_is_unknown(self) -> None:
        reading = GetConsumerLagOutput.model_validate(_STATIC_LIVE)
        assert reading.last_poll_at is None
        assert reading.last_poll_age_seconds is None

    def test_a_reading_from_before_v0_6_24_still_parses(self) -> None:
        # Every recording and canned fixture made before this pin lacks both fields; they are
        # optional on the platform side too, so an absent field is null, never a parse failure.
        older = {k: v for k, v in _RESOLVER_LIVE.items() if not k.startswith("last_poll")}
        reading = GetConsumerLagOutput.model_validate(older)
        assert reading.last_poll_at is None
        assert reading.last_poll_age_seconds is None

    def test_a_stopped_consumer_reads_as_platform_adr_0041_describes(self) -> None:
        # Lag flat, known and fresh; the poll time frozen and its age far above `age_seconds`.
        stopped = {
            **_RESOLVER_LIVE,
            "last_poll_at": "2026-10-10T14:50:00.000000Z",
            "last_poll_age_seconds": 277,
        }
        reading = GetConsumerLagOutput.model_validate(stopped)
        assert reading.lag == 0 and reading.lag_known and reading.age_seconds == 1
        assert reading.last_poll_age_seconds == 277


class TestControlLoopsParse:
    def test_a_full_reading_parses_every_loop(self) -> None:
        reading = GetControlLoopsOutput.model_validate(_loops_payload())
        assert reading.total == 11
        assert [loop.name for loop in reading.loops] == list(ControlLoopName)
        assert all(loop.paused is False for loop in reading.loops)

    def test_a_paused_loop_carries_its_expiry(self) -> None:
        loops = [
            _loop("resume_unblocked_waiting", paused=True, paused_expires_in_seconds=420),
        ]
        reading = GetControlLoopsOutput.model_validate(_loops_payload(loops=loops, total=1))
        assert reading.loops[0].name is ControlLoopName.RESUME_UNBLOCKED_WAITING
        assert reading.loops[0].paused is True
        assert reading.loops[0].paused_expires_in_seconds == 420

    def test_an_unreadable_store_is_unknown_not_healthy(self) -> None:
        loops = [
            _loop(member.value, paused=None, last_run_at=None, last_run_age_seconds=None)
            for member in ControlLoopName
        ]
        reading = GetControlLoopsOutput.model_validate(
            _loops_payload(loops=loops, unknown_reason="redis unavailable")
        )
        assert reading.unknown_reason == "redis unavailable"
        assert all(loop.paused is None for loop in reading.loops)

    def test_paused_is_required_even_when_null(self) -> None:
        row = _loop("metrics")
        del row["paused"]
        with pytest.raises(ValidationError):
            GetControlLoopsOutput.model_validate(_loops_payload(loops=[row], total=1))

    def test_the_loop_names_are_the_platforms_closed_set(self) -> None:
        snapshot_enum = _snapshot_tool("get_control_loops")["outputSchema"]["$defs"][
            "ControlLoopName"
        ]["enum"]
        assert [member.value for member in ControlLoopName] == snapshot_enum
        with pytest.raises(ValidationError):
            GetControlLoopsOutput.model_validate(
                _loops_payload(loops=[_loop("not_a_loop")], total=1)
            )


class TestTheNewClocksReplayCoherently:
    """A recorded world on v0.6.24 shifts the new clocks and holds the new durations."""

    def test_the_poll_time_moves_and_its_age_is_held(self) -> None:
        delta = timedelta(days=1)
        moved = _payload(
            recorded_client.rebase_result("get_consumer_lag", _result(_RESOLVER_LIVE), delta)
        )
        assert (
            datetime.fromisoformat(moved["last_poll_at"])
            == datetime.fromisoformat(_RESOLVER_LIVE["last_poll_at"]) + delta
        )
        assert moved["last_poll_age_seconds"] == _RESOLVER_LIVE["last_poll_age_seconds"]

    def test_each_loops_last_run_moves_and_its_age_is_held(self) -> None:
        delta = timedelta(hours=3)
        original = _loops_payload()
        moved = _payload(
            recorded_client.rebase_result("get_control_loops", _result(original), delta)
        )
        assert (
            datetime.fromisoformat(moved["measured_at"])
            == datetime.fromisoformat(original["measured_at"]) + delta
        )
        for before, after in zip(original["loops"], moved["loops"], strict=True):
            assert (
                datetime.fromisoformat(after["last_run_at"])
                == datetime.fromisoformat(before["last_run_at"]) + delta
            )
            assert after["last_run_age_seconds"] == before["last_run_age_seconds"]
            assert after["tick_interval_seconds"] == before["tick_interval_seconds"]


def _snapshot_tool(name: str) -> dict[str, Any]:
    tools = json.loads(_SNAPSHOT.read_text())["tools"]
    return next(tool for tool in tools if tool["name"] == name)


def _result(payload: dict[str, Any]) -> ToolResult:
    return ToolResult(content=[{"type": "text", "text": json.dumps(payload)}])


def _payload(result: ToolResult) -> dict[str, Any]:
    text = result.content[0]["text"]
    assert isinstance(text, str)
    parsed = json.loads(text)
    assert isinstance(parsed, dict)
    return parsed
