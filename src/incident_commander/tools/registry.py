"""Typed schemas for every platform MCP tool the agent uses.

Schemas are hand-written but mirror the platform's Pydantic models tool-for-tool
(source: ``incident-platform/backend/app/mcp/tools/*.py``). Drift is caught by
``contracts/platform-tools.snapshot.json`` + the contract diff test.

Read tools (Wave 1 + Wave 2) are unrestricted. Tier-1 write actions
(``pause_dag``, ``invalidate_cache_key``, ``replay_dlq_messages``,
``restart_consumer_group``) are registered here for the remediation loop
(Phase 6) but tier policy in ``policies.py`` gates when the agent may
invoke them — the investigation planner cannot propose Tier-1 actions.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
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
    # No min_length: the platform accepts ANY group string and returns
    # lag:null for unknown ones (platform consumer_lag.py — "Any group
    # name is accepted"). The snapshot inputSchema has no minLength, and
    # the input-side contract test holds this model to exact schema
    # equality with it.
    consumer_group: str = Field(default="worker-dispatcher")


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
    # v0.4.9: the pause flag became observable here (platform fix after the
    # 2026-08-03 campaign surfaced pause_dag as a shipped no-op — see
    # docs/lessons/live-campaign-2026-08-03.md exhibit 2). Without these
    # fields the output model would strip `paused` before the verify judge
    # ever saw it.
    model_config = ConfigDict(extra="ignore", frozen=True)
    # v0.5.0 renamed the platform-side field, so its derived JSON-Schema title
    # became "Root Job Id". The wire name `seed_id` is unchanged; only the
    # title moved, and the output-leg snapshot test compares titles strictly.
    seed_id: str = Field(title="Root Job Id")
    nodes: list[DagNode]
    edges: list[DagEdge]
    paused: bool
    paused_expires_in_seconds: int | None = None
    paused_by: str | None = None


# --- get_deploy_history --------------------------------------------------


class GetDeployHistoryInput(BaseModel):
    model_config = _EMPTY_CONFIG
    environment: str | None = None
    limit: int = Field(default=20, ge=1, le=100)


class DeployEntry(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    version: str
    revision: str | None = None
    image_tag: str | None = None
    deployed_at: datetime
    environment: str
    notes: str | None = None


class GetDeployHistoryOutput(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    total: int
    entries: list[DeployEntry]
    # v0.2.1+: platform advertises where the entries came from
    # ("deploy_markers" table vs env-based fallback). Required + non-
    # nullable per v0.4.8 outputSchema.
    source: str


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
    # v0.4.0+: filter to entries in one remediation category.
    # Values: replay_safe, wait_and_replay, human_required.
    # Omit for all categories (including uncategorized).
    remediation_hint: str | None = None
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
    # v0.4.0+: platform's category classification for the remediation
    # planner. When null, the agent falls back to LLM classification from
    # ``error_message`` + ``triage``. Values: replay_safe, wait_and_replay,
    # human_required. Drives the routing in the remediation planner prompt.
    remediation_hint: str | None = None
    extra: dict[str, Any] | None = None


class ListDlqMessagesOutput(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    total: int
    items: list[DlqEntry]


# --- Tier-1 write actions (remediation) ---------------------------------
#
# The platform enforces authz + idempotency + audit for every call to these.
# Agent-side, tier policy (``policies.py``) bars the investigation planner
# from proposing them; only the remediation planner may.


class RestartConsumerGroupInput(BaseModel):
    model_config = _EMPTY_CONFIG
    consumer_group: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=8, max_length=255)


class RestartConsumerGroupOutput(BaseModel):
    # v0.4.5+: latency_key_cleared makes the tool the single compensating
    # action for both `kill_consumer` and `inject_latency`. v0.4.9 dropped
    # the raw kill_key/latency_key strings from the response (internal
    # Redis key names, not operator signal); the booleans remain.
    model_config = ConfigDict(extra="ignore", frozen=True)
    consumer_group: str
    kill_key_cleared: bool
    latency_key_cleared: bool
    accepted: bool


class PauseDagInput(BaseModel):
    model_config = _EMPTY_CONFIG
    root_job_id: UUID
    ttl_seconds: int = Field(default=600, ge=1, le=3600)
    idempotency_key: str = Field(min_length=8, max_length=255)


class PauseDagOutput(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    root_job_id: str
    pause_key: str
    ttl_seconds: int
    accepted: bool


class ReplayDlqMessagesInput(BaseModel):
    model_config = _EMPTY_CONFIG
    job_type: str | None = None
    limit: int = Field(default=25, ge=1, le=200)
    idempotency_key: str = Field(min_length=8, max_length=255)


class ReplayedJob(BaseModel):
    """Per-job entry inside the legacy ``replay_dlq_messages.jobs[]``.

    Just ``id`` + ``type``, no per-job outcome. Kept for back-compat
    with the older tool; ``replay_dlq_by_ids`` (v0.4.0+) uses the
    richer ``ReplayResult`` below.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)
    id: str
    type: str


class ReplayDlqMessagesOutput(BaseModel):
    """Output for the legacy ``replay_dlq_messages`` tool (pre-v0.4.0)."""

    model_config = ConfigDict(extra="ignore", frozen=True)
    requested: int
    replayed: int
    failed: int
    jobs: list[ReplayedJob]


class ReplayResult(BaseModel):
    """Per-job outcome inside ``replay_dlq_by_ids.results[]`` (v0.4.4+).

    ``ok`` distinguishes "platform accepted the replay" from "platform
    rejected it" (e.g. job wasn't in a replayable state). ``scheduled``
    + ``execute_at`` are set when the caller passed ``delay_seconds``.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)
    id: str
    ok: bool
    error: str | None = None
    scheduled: bool = False
    execute_at: float | None = None


class InvalidateCacheKeyInput(BaseModel):
    model_config = _EMPTY_CONFIG
    key: str = Field(min_length=1, max_length=512)
    idempotency_key: str = Field(min_length=8, max_length=255)


class InvalidateCacheKeyOutput(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    key: str
    deleted: bool


# --- v0.4.0 DLQ categorization tools ------------------------------------
#
# Replaces the coarse ``replay_dlq_messages`` for category-aware runs:
# - ``replay_dlq_by_ids`` — targeted, cap 50 per call
# - ``replay_dlq_by_category`` — bulk, filtered to replay_safe / wait_and_replay
# - ``mark_dlq_permanent`` — for entries the agent classifies as human_required
#
# ``delay_seconds`` on both replay tools is agent-side optional (defaults None,
# meaning immediate). When set, the platform schedules the enqueue via its
# scheduler — used for ``wait_and_replay`` category where the external
# dependency needs recovery time. Platform-owned scheduling (per ADR: the
# platform owns durable state, not the agent).


class ReplayDlqByIdsInput(BaseModel):
    model_config = _EMPTY_CONFIG
    job_ids: list[UUID] = Field(min_length=1, max_length=50)
    idempotency_key: str = Field(min_length=8, max_length=255)
    # v0.4.1+: platform defers each replay by ``delay_seconds`` when set.
    # Cap of 1 hour keeps scheduled work from lingering past the incident's
    # natural lifecycle. Omit for immediate replay. Used for
    # ``wait_and_replay`` category — external dependency needs recovery time.
    delay_seconds: int | None = Field(default=None, ge=1, le=3600)


class ReplayDlqByIdsOutput(BaseModel):
    """Output shape from v0.4.4 platform.

    ``requested`` == input job_ids count. ``replayed`` + ``scheduled`` +
    ``failed`` sum to ``requested``. Per-job outcome lives in
    ``results[]`` — that's where an operator finds which specific ids
    were rejected and why.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)
    requested: int
    replayed: int
    # Platform advertises scheduled with default=0 (delay_seconds omitted →
    # nothing scheduled, that's the modal case). replayed stays required.
    scheduled: int = 0
    failed: int
    results: list[ReplayResult]


class ReplayDlqByCategoryInput(BaseModel):
    model_config = _EMPTY_CONFIG
    category: str = Field(
        description="Must be 'replay_safe' or 'wait_and_replay'. "
        "Platform refuses 'human_required' with error code dlq_category_refused."
    )
    job_type: str | None = None
    max_replays: int = Field(default=20, ge=1, le=100)
    idempotency_key: str = Field(min_length=8, max_length=255)
    # v0.4.1+: same delay semantics as replay_dlq_by_ids.
    delay_seconds: int | None = Field(default=None, ge=1, le=3600)


class ReplayDlqByCategoryOutput(BaseModel):
    """Output shape from v0.4.4 platform.

    ``matched`` (not requested) — how many DLQ rows the category filter
    hit. ``replayed`` + ``scheduled`` + ``failed`` sum to ``matched``.
    ``job_ids[]`` lists the specific ids that were processed.
    ``execute_at`` is set when the caller passed ``delay_seconds``
    (single epoch for the bulk operation; per-job in the by-ids tool).
    """

    model_config = ConfigDict(extra="ignore", frozen=True)
    category: str
    matched: int
    replayed: int = 0
    scheduled: int = 0
    failed: int
    job_ids: list[str]
    execute_at: float | None = None


class MarkDlqPermanentInput(BaseModel):
    model_config = _EMPTY_CONFIG
    job_id: UUID
    reason: str = Field(
        min_length=8,
        max_length=1024,
        description="Full sentence explaining why the entry is not replayable. "
        "Written to the audit log for the human review path.",
    )
    idempotency_key: str = Field(min_length=8, max_length=255)


class MarkDlqPermanentOutput(BaseModel):
    """Output shape from v0.4.4 platform.

    ``previous_hint`` shows what the categorizer had classified this
    entry as before the mark. ``remediation_hint`` is always
    ``'human_required'`` after the call (that's the whole point).
    ``already_marked`` tells the operator whether this call was a
    no-op (repeat call with the same idempotency_key OR the entry was
    already ``human_required``).
    """

    model_config = ConfigDict(extra="ignore", frozen=True)
    job_id: str
    # Nullable but required (no default) per v0.4.8 outputSchema.
    previous_hint: str | None
    remediation_hint: str
    already_marked: bool


# --- Registry ------------------------------------------------------------


@dataclass(frozen=True)
class ToolSpec:
    """One entry in the tool registry."""

    name: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]


_SNAPSHOT_PATH: Final[Path] = (
    Path(__file__).resolve().parents[3] / "contracts" / "platform-tools.snapshot.json"
)


def _load_snapshot_descriptions(path: Path = _SNAPSHOT_PATH) -> dict[str, str]:
    """Tool descriptions, mirrored verbatim from the committed contract snapshot.

    The v0.4.9 descriptions are load-bearing eval infrastructure, not doc
    fluff: the remediation planner authors its verify expectation from
    them (delayed-replay semantics, lag freshness windows, the pause
    flag's observable effect). Loading from the snapshot at import makes
    the mirror true by construction — a `make snapshot` refresh IS the
    description update, and there is no second copy to drift.

    A missing or unreadable snapshot is a hard error, raised at import.
    The previous behavior — silently degrading to ``{}`` — meant any
    non-checkout install (e.g. a wheel, where ``parents[3]`` of this file
    is not the repo root) shipped '(no description)' to the planner for
    every tool: a silent quality collapse of the verify context that no
    deployed environment would ever test for. If a packaged deployment
    lands later, the snapshot must ship as package data (an ADR-level
    layout change) — not by reintroducing the silent fallback.
    """
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError) as err:
        raise RuntimeError(
            f"platform-tools snapshot missing or unreadable at {path}; the "
            "registry's tool descriptions are load-bearing (verify "
            "expectations are authored from them) — refusing to run with "
            "empty descriptions"
        ) from err
    return {t["name"]: t.get("description", "") for t in raw.get("tools", [])}


_TOOL_DESCRIPTIONS: Final[dict[str, str]] = _load_snapshot_descriptions()


def description_of(tool_name: str) -> str:
    """Platform-authored description for one tool ("" when unknown)."""
    return _TOOL_DESCRIPTIONS.get(tool_name, "")


TOOL_REGISTRY: Final[dict[str, ToolSpec]] = {
    # Read tools (Wave 1 + Wave 2) — investigation planner may propose freely.
    "get_consumer_lag": ToolSpec("get_consumer_lag", GetConsumerLagInput, GetConsumerLagOutput),
    "get_dag_state": ToolSpec("get_dag_state", GetDagStateInput, GetDagStateOutput),
    "get_deploy_history": ToolSpec(
        "get_deploy_history", GetDeployHistoryInput, GetDeployHistoryOutput
    ),
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
    # Tier-1 write actions — remediation planner only. Guarded by policies.py.
    "restart_consumer_group": ToolSpec(
        "restart_consumer_group", RestartConsumerGroupInput, RestartConsumerGroupOutput
    ),
    "pause_dag": ToolSpec("pause_dag", PauseDagInput, PauseDagOutput),
    "replay_dlq_messages": ToolSpec(
        "replay_dlq_messages", ReplayDlqMessagesInput, ReplayDlqMessagesOutput
    ),
    "invalidate_cache_key": ToolSpec(
        "invalidate_cache_key", InvalidateCacheKeyInput, InvalidateCacheKeyOutput
    ),
    # v0.4.0 DLQ categorization tools — remediation planner routes on
    # ``DlqEntry.remediation_hint`` between these.
    "replay_dlq_by_ids": ToolSpec("replay_dlq_by_ids", ReplayDlqByIdsInput, ReplayDlqByIdsOutput),
    "replay_dlq_by_category": ToolSpec(
        "replay_dlq_by_category", ReplayDlqByCategoryInput, ReplayDlqByCategoryOutput
    ),
    "mark_dlq_permanent": ToolSpec(
        "mark_dlq_permanent", MarkDlqPermanentInput, MarkDlqPermanentOutput
    ),
}
