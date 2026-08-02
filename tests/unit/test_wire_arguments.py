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

from uuid import UUID

import pytest

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
