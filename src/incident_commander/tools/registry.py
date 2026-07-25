"""Typed schemas for the platform's MCP read tools (Wave 1 + Wave 2).

Schemas are hand-written but mirror the platform's Pydantic models tool-for-tool
(source: ``incident-platform/backend/app/mcp/tools/*.py``). Drift is caught by
``contracts/platform-tools.snapshot.json`` + the contract diff test.

This file covers **read** tools only. Tier-1 write actions (``pause_dag``,
``invalidate_cache_key``, ``replay_dlq_messages``, ``restart_consumer_group``)
are on the platform's surface but land in Phase 6 alongside the approvals flow.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

# --- Common empty input --------------------------------------------------

_EMPTY_CONFIG = ConfigDict(extra="forbid", frozen=True)


class _EmptyInput(BaseModel):
    """No arguments. Matches the platform's ``_EmptyIn`` for zero-arg tools."""

    model_config = _EMPTY_CONFIG


# --- get_consumer_lag ----------------------------------------------------


class GetConsumerLagInput(BaseModel):
    model_config = _EMPTY_CONFIG
    consumer_group: str = Field(default="worker-dispatcher", min_length=1)


class GetConsumerLagOutput(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    consumer_group: str
    lag: int | None
    cache_key: str


# --- get_dag_state -------------------------------------------------------


class GetDagStateInput(BaseModel):
    model_config = _EMPTY_CONFIG
    job_id: UUID


class DagNode(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    id: str
    type: str
    status: str
    retry_count: int
    created_at: datetime


class DagEdge(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    from_id: str
    to_id: str


class GetDagStateOutput(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    seed_id: str
    nodes: list[DagNode]
    edges: list[DagEdge]


# --- get_deploy_history --------------------------------------------------


class DeployEntry(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    version: str
    revision: str | None = None
    image_tag: str | None = None
    started_at: datetime
    env: str
    notes: str = ""


class GetDeployHistoryOutput(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    total: int
    entries: list[DeployEntry]


# --- get_incident + list_incidents --------------------------------------


class GetIncidentInput(BaseModel):
    model_config = _EMPTY_CONFIG
    id: UUID


class GetIncidentOutput(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    id: str
    severity: str
    source: str
    title: str
    description: str | None = None
    fired_at: datetime
    resolved_at: datetime | None = None
    is_active: bool
    extra_data: dict[str, Any] | None = None
    request_id: str | None = None


class ListIncidentsInput(BaseModel):
    model_config = _EMPTY_CONFIG
    include_resolved: bool = False
    severity: str | None = None
    source: str | None = None
    limit: int = Field(default=50, ge=1, le=200)


class IncidentSummary(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    id: str
    severity: str
    source: str
    title: str
    description: str | None = None
    fired_at: datetime
    resolved_at: datetime | None = None
    is_active: bool


class ListIncidentsOutput(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    total: int
    incidents: list[IncidentSummary]


# --- get_postgres_health / get_redis_health -----------------------------


class PostgresHealthOutput(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    ok: bool
    ping_latency_ms: float | None = None
    active_connections: int | None = None
    dialect: str
    error: str | None = None


class RedisHealthOutput(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    ok: bool
    ping_latency_ms: float | None = None
    connected_clients: int | None = None
    used_memory_bytes: int | None = None
    used_memory_human: str | None = None
    keyspace_hits: int | None = None
    keyspace_misses: int | None = None
    error: str | None = None


# --- get_trace + search_traces ------------------------------------------


class GetTraceInput(BaseModel):
    model_config = _EMPTY_CONFIG
    trace_id: str = Field(min_length=1, max_length=255)
    include_audit: bool = True


class TracedJob(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    id: str
    type: str
    status: str
    user_id: str | None = None
    retry_count: int
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime | None = None


class TracedAuditRow(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    action: str
    resource_type: str | None = None
    resource_id: str | None = None
    principal_type: str
    created_at: datetime
    extra_data: dict[str, Any] | None = None


class GetTraceOutput(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    trace_id: str
    jobs: list[TracedJob]
    audit_events: list[TracedAuditRow]


class SearchTracesInput(BaseModel):
    model_config = _EMPTY_CONFIG
    status: str | None = None
    job_type: str | None = None
    since_hours: int | None = Field(default=None, ge=1, le=168)
    limit: int = Field(default=50, ge=1, le=200)


class TraceMatch(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    trace_id: str
    job_id: str
    job_type: str
    status: str
    created_at: datetime


class SearchTracesOutput(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    matches: list[TraceMatch]


# --- list_active_alerts --------------------------------------------------


class ListActiveAlertsInput(BaseModel):
    model_config = _EMPTY_CONFIG
    severity: str | None = None
    limit: int = Field(default=50, ge=1, le=200)


class AlertSummary(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    id: str
    severity: str
    source: str
    title: str
    description: str | None = None
    fired_at: datetime
    extra_data: dict[str, Any] | None = None


class ListActiveAlertsOutput(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    total: int
    alerts: list[AlertSummary]


# --- list_audit_events --------------------------------------------------


class ListAuditEventsInput(BaseModel):
    model_config = _EMPTY_CONFIG
    action: str | None = None
    action_prefix: str | None = None
    principal_type: str | None = None
    limit: int = Field(default=50, ge=1, le=200)


class AuditEventEntry(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    id: str
    action: str
    principal_type: str
    principal_id: str | None = None
    resource_type: str | None = None
    resource_id: str | None = None
    request_id: str | None = None
    created_at: datetime
    extra_data: dict[str, Any] | None = None


class ListAuditEventsOutput(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    total: int
    events: list[AuditEventEntry]


# --- list_dlq_messages ---------------------------------------------------


class ListDlqMessagesInput(BaseModel):
    model_config = _EMPTY_CONFIG
    job_type: str | None = None
    limit: int = Field(default=50, ge=1, le=200)


class DlqTriageSummary(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    root_cause_category: str | None = None
    summary: str
    suggested_fix: str | None = None
    is_retryable: bool | None = None
    confidence: float | None = None


class DlqEntry(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    id: str
    type: str
    error_message: str | None = None
    retry_count: int
    created_at: datetime
    updated_at: datetime | None = None
    trace_id: str | None = None
    triage: DlqTriageSummary | None = None
    extra: dict[str, Any] | None = None


class ListDlqMessagesOutput(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    total: int
    items: list[DlqEntry]


# --- Registry ------------------------------------------------------------


@dataclass(frozen=True)
class ToolSpec:
    """One entry in the tool registry."""

    name: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]


TOOL_REGISTRY: Final[dict[str, ToolSpec]] = {
    "get_consumer_lag": ToolSpec("get_consumer_lag", GetConsumerLagInput, GetConsumerLagOutput),
    "get_dag_state": ToolSpec("get_dag_state", GetDagStateInput, GetDagStateOutput),
    "get_deploy_history": ToolSpec("get_deploy_history", _EmptyInput, GetDeployHistoryOutput),
    "get_incident": ToolSpec("get_incident", GetIncidentInput, GetIncidentOutput),
    "get_postgres_health": ToolSpec("get_postgres_health", _EmptyInput, PostgresHealthOutput),
    "get_redis_health": ToolSpec("get_redis_health", _EmptyInput, RedisHealthOutput),
    "get_trace": ToolSpec("get_trace", GetTraceInput, GetTraceOutput),
    "list_active_alerts": ToolSpec(
        "list_active_alerts", ListActiveAlertsInput, ListActiveAlertsOutput
    ),
    "list_audit_events": ToolSpec("list_audit_events", ListAuditEventsInput, ListAuditEventsOutput),
    "list_dlq_messages": ToolSpec("list_dlq_messages", ListDlqMessagesInput, ListDlqMessagesOutput),
    "list_incidents": ToolSpec("list_incidents", ListIncidentsInput, ListIncidentsOutput),
    "search_traces": ToolSpec("search_traces", SearchTracesInput, SearchTracesOutput),
}
