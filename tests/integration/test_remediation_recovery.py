"""End-to-end crash-recovery test for the Phase 6 remediation loop.

Simulates the failure mode CLAUDE.md invariant 6 requires the agent to
handle: agent executes the Tier-1 action, writes a checkpoint, then
crashes before verifying. On restart the checkpoint is loaded and the
REMEDIATING transition must reconcile — recognize the action already
ran and skip re-execution — rather than double-invoking the platform.

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
    EvidenceEntry,
    IncidentState,
    RunState,
)
from incident_commander.persistence.postgres import PostgresCheckpointer
from incident_commander.tools.mcp_client import ToolResult


class _CountingMCP:
    def __init__(self, response: ToolResult) -> None:
        self._response = response
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call_tool(self, name: str, arguments: Mapping[str, Any]) -> ToolResult:
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
    """Full cycle: execute → checkpoint → crash → resume → reconcile."""

    def test_reload_after_execution_reconciles_without_re_invocation(
        self, clean_engine: Engine
    ) -> None:
        checkpointer = PostgresCheckpointer(clean_engine)
        mcp = _CountingMCP(_successful_action())
        remediate: Callable[[RunState, datetime], RunState] = make_remediate(mcp)

        # 1) Pre-crash: execute REMEDIATING, checkpoint the result. This
        #    is where the agent would successfully call the platform and
        #    persist the outcome to Postgres.
        pre = _pre_remediation_state()
        checkpointer.write(pre)
        post = remediate(pre, _now())
        checkpointer.write(post)

        # Sanity: state advanced to VERIFYING, attempts=1, MCP called once.
        assert post.state is IncidentState.VERIFYING
        assert post.remediation_attempts == 1
        assert len(mcp.calls) == 1

        # 2) Simulate crash + restart: agent process dies, comes back
        #    with only what's in Postgres. Load and inspect.
        reloaded = checkpointer.load(pre.incident_id)
        assert reloaded == post

        # 3) Suppose the crash happened between the platform call and
        #    the post-write (worst case) — the loaded state would be
        #    ``pre`` with the evidence-bearing checkpoint missing.
        #    Overwrite Postgres to represent that timeline: state is
        #    still REMEDIATING but evidence records the tool call
        #    (because we wrote the "action succeeded" checkpoint first
        #    in a real system, before flipping state).
        mid_crash = pre.model_copy(
            update={
                "state": IncidentState.REMEDIATING,
                "evidence": (
                    EvidenceEntry(
                        tool_name="restart_consumer_group",
                        arguments={
                            "consumer_group": "worker-dispatcher",
                            "idempotency_key": "abc123",
                        },
                        result_summary='{"consumer_group":"worker-dispatcher","accepted":true}',
                        timestamp=_now(),
                    ),
                ),
                "remediation_attempts": 1,
            }
        )
        checkpointer.write(mid_crash)

        # 4) Resume: REMEDIATING transition re-fires. Reconciliation
        #    must recognize the action already ran and NOT call MCP
        #    again — otherwise the platform sees a duplicate audit
        #    entry (safe due to idempotency, but noise we can avoid).
        resumed = checkpointer.load(pre.incident_id)
        assert resumed is not None
        after_recover = remediate(resumed, _now())

        assert after_recover.state is IncidentState.VERIFYING
        # Critical assertion: MCP was NOT called a second time.
        assert len(mcp.calls) == 1
        # Reconciliation note is on evidence for the trajectory.
        assert any(e.tool_name == "_remediate_reconciled" for e in after_recover.evidence)
        # attempts stays at 1 — no new attempt was made.
        assert after_recover.remediation_attempts == 1

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
