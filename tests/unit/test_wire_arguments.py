"""Golden snapshots for ``wire_arguments`` — the exact bytes the agent sends.

The platform hashes the JSON body of ``tools/call`` for idempotency. A
serialization tweak (``exclude_none``, ``by_alias``, aliasing, default
handling) that goes unnoticed here would silently break retry dedup:
same key + drifted bytes = 409 on a legitimate crash-recovery re-send.

These tests pin the exact wire format. When one fails, that's the
signal to stop, look at the platform's arguments-hash spec
(ADR 0010 on the platform side), and confirm the change is intentional
before reblessing the snapshot.

Focus on the two easily-drifting cases:

- **defaulted field**: Pydantic fills it; is it in the wire output?
- **nullable field**: user omits it; does it wire as ``null`` or is it
  absent?

``replay_dlq_by_ids`` is the canonical fixture because ``delay_seconds``
is both defaulted (``None``) and typed as ``int | None`` — exactly the
combination that surfaces ``exclude_none`` / ``exclude_defaults``
regressions.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any
from uuid import UUID

import pytest

from incident_commander.agent.hypothesis import ProbeAction
from incident_commander.agent.investigation import _execute_probe, make_investigate
from incident_commander.agent.state import IncidentState, RunState
from incident_commander.tools.mcp_client import ToolResult
from incident_commander.tools.registry import TOOL_REGISTRY
from incident_commander.tools.wire import wire_arguments


class TestReplayDlqByIds:
    """Golden shapes for the tool with a nullable-defaulted field."""

    _SPEC = TOOL_REGISTRY["replay_dlq_by_ids"]
    _JOB_ID = UUID("11111111-1111-1111-1111-000000000001")

    def test_delay_seconds_defaults_to_null_in_wire(self) -> None:
        # Omit delay_seconds. Pydantic fills default=None. Wire MUST include
        # it as null — the platform's arguments hash covers the raw wire
        # bytes with Pydantic-filled defaults, so dropping the field would
        # produce a different hash than the platform expects.
        out = wire_arguments(
            self._SPEC,
            {
                "job_ids": [self._JOB_ID],
                "idempotency_key": "01234567890abcdef",
            },
        )
        assert out == {
            "job_ids": ["11111111-1111-1111-1111-000000000001"],
            "idempotency_key": "01234567890abcdef",
            "delay_seconds": None,
        }

    def test_delay_seconds_explicit_int_survives(self) -> None:
        out = wire_arguments(
            self._SPEC,
            {
                "job_ids": [self._JOB_ID],
                "idempotency_key": "01234567890abcdef",
                "delay_seconds": 300,
            },
        )
        assert out["delay_seconds"] == 300

    def test_uuid_serializes_to_string_in_wire(self) -> None:
        # mode="json" is what makes UUIDs strings. If someone drops mode
        # (or switches to python mode), the wire body carries UUID objects
        # → different bytes → hash mismatch.
        out = wire_arguments(
            self._SPEC,
            {
                "job_ids": [self._JOB_ID],
                "idempotency_key": "01234567890abcdef",
            },
        )
        assert out["job_ids"] == ["11111111-1111-1111-1111-000000000001"]
        assert isinstance(out["job_ids"][0], str)


class TestRestartConsumerGroup:
    """No optional fields — everything is required. Simplest golden."""

    _SPEC = TOOL_REGISTRY["restart_consumer_group"]

    def test_exact_wire_shape(self) -> None:
        out = wire_arguments(
            self._SPEC,
            {
                "consumer_group": "worker-dispatcher",
                "idempotency_key": "01234567890abcdef",
            },
        )
        assert out == {
            "consumer_group": "worker-dispatcher",
            "idempotency_key": "01234567890abcdef",
        }


class TestValidationBubblesUp:
    """Wire mode is a validation boundary; garbage in → ValidationError."""

    def test_missing_required_field_raises(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            wire_arguments(
                TOOL_REGISTRY["replay_dlq_by_ids"],
                {"idempotency_key": "01234567890abcdef"},  # no job_ids
            )


class _RecordingMCP:
    """Captures the exact argument dict handed to the transport."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call_tool(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        timeout_seconds: float | None = None,
    ) -> ToolResult:
        self.calls.append((name, dict(arguments)))
        return ToolResult(content=[{"type": "text", "text": json.dumps(self._payload)}])


class TestEveryCallPathRoutesThroughWireArguments:
    """One serialization, asserted at each call site (WO-R2-15 finding 3).

    This module's docstring calls ``wire_arguments`` the canonical
    producer of the bytes the platform hashes, and ``wire.py``'s own
    docstring forbids re-implementing it. The investigation legs used to
    do exactly that — ``spec.input_model.model_validate(...).model_dump()``
    inline, and the opening probe's copy omitted ``mode="json"`` — so the
    rule was stated in two places and enforced in neither.

    These tests compare what the MCP client actually received against
    ``wire_arguments`` output for the same inputs. A future inline
    re-implementation that drifts (drops a default, skips ``mode="json"``,
    adds ``exclude_none``) fails here rather than at a 409 on a
    crash-recovery re-send.
    """

    def test_opening_probe_matches_wire_arguments_when_alert_names_no_group(
        self, run_state: RunState, now: datetime
    ) -> None:
        # This leg default-fills: a read-only probe with no group named is
        # allowed to fall back to the registry default (unlike the
        # remediation legs — ADR 0022). The bytes must still be canonical.
        mcp = _RecordingMCP({"consumer_group": "worker-dispatcher", "lag": 5, "cache_key": "k"})
        run = run_state.model_copy(
            update={
                "state": IncidentState.INVESTIGATING,
                "alert": {"source": "platform.kafka", "severity": "high"},
            }
        )

        make_investigate(mcp)(run, now)

        name, sent = mcp.calls[0]
        assert sent == wire_arguments(TOOL_REGISTRY[name], {})

    def test_opening_probe_matches_wire_arguments_with_named_group(
        self, run_state: RunState, now: datetime
    ) -> None:
        mcp = _RecordingMCP({"consumer_group": "billing", "lag": 5, "cache_key": "k"})
        run = run_state.model_copy(
            update={
                "state": IncidentState.INVESTIGATING,
                "alert": {
                    "source": "platform.kafka",
                    "severity": "high",
                    "consumer_group": "billing",
                },
            }
        )

        make_investigate(mcp)(run, now)

        name, sent = mcp.calls[0]
        assert sent == wire_arguments(TOOL_REGISTRY[name], {"consumer_group": "billing"})

    def test_planner_probe_matches_wire_arguments_including_uuid_coercion(
        self, run_state: RunState, now: datetime
    ) -> None:
        # get_dag_state.job_id is a UUID field, and mode="json" is what
        # turns it into the string httpx can encode. An inline
        # `.model_dump()` without a mode would reintroduce that bug here.
        job_id = "33333333-3333-3333-3333-333333333333"
        mcp = _RecordingMCP({"seed_id": job_id, "nodes": [], "edges": [], "paused": True})
        action = ProbeAction(tool_name="get_dag_state", arguments={"job_id": job_id})

        _execute_probe(
            run_state.model_copy(update={"state": IncidentState.INVESTIGATING}),
            now,
            mcp,
            action,
        )

        name, sent = mcp.calls[0]
        assert sent == wire_arguments(TOOL_REGISTRY[name], action.arguments)
        assert sent["job_id"] == job_id  # a str, not a UUID object
