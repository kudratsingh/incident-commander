"""End-to-end crash-recovery test for the Phase 6 remediation loop.

Simulates the failure mode CLAUDE.md invariant 6 requires the agent to
handle: agent executes the Tier-1 action, writes a checkpoint, then
crashes before advancing to VERIFYING. On restart the checkpoint is
loaded and REMEDIATING re-fires. Under ADR 0008 the agent does NOT
short-circuit here — it re-invokes with the SAME idempotency_key, and
the platform's idempotency store (ADR 0010 on the platform side) is
what makes the re-send safe. See
``tests/integration/test_idempotency_contract.py`` for the live proof
that the platform actually dedupes.

Uses ``PostgresCheckpointer`` so the round-trip proves the schema
handles v3 fields (``remediation_attempts``, ``remediation_plan``).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import Engine

from incident_commander.agent.remediation import make_remediate
from incident_commander.agent.state import (
    BudgetLedger,
    IncidentState,
    RunState,
)
from incident_commander.persistence.postgres import PostgresCheckpointer
from incident_commander.tools.mcp_client import ToolResult


class _CountingMCP:
    def __init__(self, response: ToolResult) -> None:
        self._response = response
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call_tool(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        timeout_seconds: float | None = None,
    ) -> ToolResult:
        self.calls.append((name, dict(arguments)))
        return self._response


def _now() -> datetime:
    return datetime(2026, 7, 30, 12, 0, tzinfo=UTC)


def _plan() -> dict[str, Any]:
    return {
        "target_hypothesis": "consumer_saturation",
        "action_tool": "restart_consumer_group",
        "action_arguments": {"consumer_group": "worker-dispatcher"},
        "verify_tool": "get_consumer_lag",
        "verify_arguments": {"consumer_group": "worker-dispatcher"},
        "verify_expectation": "lag drops to near-zero",
    }


def _pre_remediation_state() -> RunState:
    return RunState(
        incident_id=UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
        state=IncidentState.REMEDIATING,
        alert={"source": "platform.kafka", "severity": "high"},
        budget=BudgetLedger(
            max_tool_calls=25,
            max_tokens=200_000,
            max_wall_seconds=600,
            max_usd=Decimal("1.00"),
        ),
        remediation_plan=_plan(),
        remediation_attempts=0,
        created_at=_now(),
        updated_at=_now(),
    )


def _successful_action() -> ToolResult:
    return ToolResult(
        content=[
            {
                "type": "text",
                "text": json.dumps(
                    {
                        "consumer_group": "worker-dispatcher",
                        "kill_key_cleared": True,
                        "kill_key": "chaos:kill:worker-dispatcher",
                        "accepted": True,
                    }
                ),
            }
        ]
    )


class TestCrashRecoveryRoundTrip:
    """Full cycle: execute → checkpoint → crash → resume → re-send with same idem key.

    Under ADR 0008 there is no client-side reconciliation branch. The
    invariant is stronger, not weaker: on crash-resume the agent re-
    invokes with a *deterministic* ``build_idempotency_key(incident,
    tool, args)``, and the platform's idempotency store returns the
    cached response. The agent doesn't need to know whether the first
    invocation landed — the wire contract makes both cases equivalent.
    """

    def test_resume_reissues_same_idempotency_key(self, clean_engine: Engine) -> None:
        checkpointer = PostgresCheckpointer(clean_engine)
        mcp = _CountingMCP(_successful_action())
        remediate: Callable[[RunState, datetime], RunState] = make_remediate(mcp)

        # 1) Pre-crash: execute REMEDIATING, checkpoint the result.
        pre = _pre_remediation_state()
        checkpointer.write(pre)
        post = remediate(pre, _now())
        checkpointer.write(post)

        assert post.state is IncidentState.VERIFYING
        assert post.remediation_attempts == 1
        assert len(mcp.calls) == 1
        first_call_args = mcp.calls[0][1]
        first_idem_key = first_call_args["idempotency_key"]

        # 2) Simulate the worst-case crash: the action landed on the
        #    platform, but the post-transition checkpoint didn't. On
        #    restart the state loaded from Postgres is still the
        #    pre-execute snapshot.
        checkpointer.write(pre)
        resumed = checkpointer.load(pre.incident_id)
        assert resumed is not None
        assert resumed.state is IncidentState.REMEDIATING

        # 3) Re-fire REMEDIATING. The agent invokes MCP AGAIN — no
        #    client-side reconciliation short-circuit exists. The
        #    critical assertion is that the second call carries the
        #    SAME idempotency_key as the first, which is what makes
        #    the platform's dedup safe.
        after_recover = remediate(resumed, _now())
        assert after_recover.state is IncidentState.VERIFYING
        assert len(mcp.calls) == 2, (
            "REMEDIATING must re-invoke on resume; safety is on the "
            "platform side (ADR 0010) via the idempotency_key match, "
            "not on the agent side via a reconciliation short-circuit."
        )
        assert mcp.calls[1][1]["idempotency_key"] == first_idem_key, (
            "Second call must carry the same idempotency_key as the first — "
            "otherwise the platform would treat it as a fresh execute and "
            "the effect would double. build_idempotency_key is deterministic "
            "in (incident, tool, args); if this fails, that determinism broke."
        )

    def test_schema_v3_fields_round_trip_through_postgres(self, clean_engine: Engine) -> None:
        # Explicit round-trip check for the two v3 fields the earlier
        # remediation-loop tests exercise only in memory.
        checkpointer = PostgresCheckpointer(clean_engine)
        state = _pre_remediation_state().model_copy(update={"remediation_attempts": 1})
        checkpointer.write(state)
        loaded = checkpointer.load(state.incident_id)
        assert loaded is not None
        assert loaded.remediation_plan == _plan()
        assert loaded.remediation_attempts == 1
        assert loaded.schema_version == 3
