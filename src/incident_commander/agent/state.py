"""Typed run state for the incident state machine (ADR-0002)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Self
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from incident_commander.agent.hypothesis import Hypothesis


class IncidentState(StrEnum):
    """States in the incident-run state machine. New states require an ADR update."""

    TRIAGE = "triage"
    INVESTIGATING = "investigating"
    PLANNING = "planning"
    AWAITING_APPROVAL = "awaiting_approval"
    REMEDIATING = "remediating"
    VERIFYING = "verifying"
    RESOLVED = "resolved"
    ESCALATED = "escalated"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL_STATES


_TERMINAL_STATES: frozenset[IncidentState] = frozenset(
    {IncidentState.RESOLVED, IncidentState.ESCALATED, IncidentState.FAILED}
)


class BudgetLedger(BaseModel):
    """Per-incident hard budgets. Exhausting any dimension forces escalation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_tool_calls: int = Field(ge=0)
    tool_calls_used: int = Field(default=0, ge=0)
    max_tokens: int = Field(ge=0)
    tokens_used: int = Field(default=0, ge=0)
    max_wall_seconds: int = Field(ge=0)
    wall_seconds_used: float = Field(default=0.0, ge=0.0)
    max_usd: Decimal = Field(ge=0)
    usd_used: Decimal = Field(default=Decimal("0"), ge=0)

    @property
    def is_exhausted(self) -> bool:
        return (
            self.tool_calls_used >= self.max_tool_calls
            or self.tokens_used >= self.max_tokens
            or self.wall_seconds_used >= self.max_wall_seconds
            or self.usd_used >= self.max_usd
        )


class EvidenceEntry(BaseModel):
    """One tool-call outcome recorded in the evidence ledger.

    ``result_summary`` is a compact string; raw tool output is untrusted data
    (CLAUDE.md invariant 4) and lives in the trajectory store, not here.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_id: UUID = Field(default_factory=uuid4)
    tool_name: str
    arguments: dict[str, object]
    result_summary: str
    timestamp: datetime
    hypothesis_ref: str | None = None


class RunState(BaseModel):
    """Durable representation of one incident run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = Field(default=2, ge=1)
    incident_id: UUID
    state: IncidentState
    alert: dict[str, object]
    budget: BudgetLedger
    evidence: tuple[EvidenceEntry, ...] = ()
    hypotheses: tuple[Hypothesis, ...] = ()
    pending_approval_id: str | None = None
    created_at: datetime
    updated_at: datetime

    def with_state(self, next_state: IncidentState, at: datetime) -> Self:
        return self.model_copy(update={"state": next_state, "updated_at": at})
