"""Typed schemas for every platform MCP tool the agent uses.

Mirrors the platform's Pydantic models tool-for-tool; drift is caught by
``contracts/platform-tools.snapshot.json`` + the contract diff test. Tier-1
write actions are registered here but gated by tier policy in ``policies.py``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Literal
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
    # No min_length: the platform accepts ANY group string, and the snapshot has none either.
    consumer_group: str = Field(default="worker-dispatcher")


class LagSample(BaseModel):
    """One past measurement of a group's lag (v0.6.7, plat #204)."""

    model_config = ConfigDict(extra="ignore", frozen=True)
    lag: int
    measured_at: datetime


class GetConsumerLagOutput(BaseModel):
    # `lag` null is "could not determine", never zero — check `lag_known` first. Only
    # `source: live` is refreshed (~60s); empty `recent_samples` is absent history (v0.6.7).
    model_config = ConfigDict(extra="ignore", frozen=True)
    consumer_group: str
    lag: int | None
    lag_known: bool
    source: Literal["live", "static", "unrecognized"]
    cache_key: str
    measured_at: datetime | None = None
    age_seconds: int | None = None
    recent_samples: list[LagSample] = Field(default_factory=list)


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
    # v0.4.9: the pause flag became observable here (pause_dag was a shipped no-op).
    model_config = ConfigDict(extra="ignore", frozen=True)
    # v0.5.0 moved the title, not the wire name `seed_id`; the snapshot test pins titles.
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
    # v0.2.1+: where entries came from (deploy_markers table vs env fallback).
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
#
# `pools` is a SAMPLE, not a live reading (v0.6.14, plat #227, platform ADR 0033): each
# process rewrites its entry under a 60 s TTL, so a few seconds of `reported_age_s` is
# normal and a process that stopped publishing LEAVES the list — absent is not healthy.


class PoolGaugeReading(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    # Declaration order IS the snapshot's `required` order; the mirror test checks.
    # `api_worker` (REST + background workers) or `mcp`. A plain string platform-side, so
    # a Literal here would reject a process a later release adds.
    process: str
    size: int
    # 0 is a measurement (idle). Read against `size` + `max_overflow`, never on its own.
    checked_out: int
    overflow: int
    max_overflow: int | None = None
    # Callers in THAT process that gave up waiting in the 60 s before the reading; 0 is real.
    wait_timeouts_1m: int
    # That process's own clock, not the answering one's — a second either way is skew.
    written_at: datetime
    reported_age_s: float


class PostgresHealthOutput(BaseModel):
    # Every field is mirrored in the snapshot's declaration order, because `extra="ignore"`
    # drops an unmirrored one in silence (`test_registry_matches_snapshot.py` pins it).
    # `null` is unknown and `0` is a measurement throughout; `*_unknown_reason` says which.
    model_config = ConfigDict(extra="ignore", frozen=True)
    ok: bool
    ping_latency_ms: float | None = None
    active_connections: int | None = None
    dialect: str
    error: str | None = None
    # WHOSE POOL: only the process that ANSWERED the call (MCP). A pool exhausted in the
    # api service reads healthy in these five fields — see `pools` below (ADR 0030).
    pool_size: int | None = None
    # Includes the connection this very call holds, so an otherwise idle process
    # reads 1, never 0. Only meaningful against `pool_size` + `pool_max_overflow`.
    pool_checked_out: int | None = None
    pool_overflow: int | None = None
    pool_max_overflow: int | None = None
    pool_wait_timeouts_1m: int | None = None
    pool_stats_unknown_reason: str | None = None
    # Read from the database server, so these cover every connection to it from
    # every process — the pool fields' limit does not apply. The server's clock.
    longest_active_query_ms: float | None = None
    active_queries_over_slow_threshold: int | None = None
    # Required (no default): the yardstick above is never absent and cannot be changed.
    slow_query_threshold_ms: float
    # ALWAYS null in this release (O-28); `query_stats_unknown_reason` says which of three
    # cases. Mirrored anyway — the live equivalents are the two query fields above.
    p95_query_ms_1m: float | None = None
    slow_query_count_1m: int | None = None
    query_stats_unknown_reason: str | None = None
    # v0.6.14: where a pool held in ANOTHER process shows up — the flat `pool_*`
    # fields above cannot show it. Not a page: no cap, nothing truncated.
    pools: tuple[PoolGaugeReading, ...] = ()
    # Null exactly when at least one process reported, so an empty `pools` with this SET
    # means nothing could be read about any pool — which is not "no pool is busy".
    pool_gauges_unknown_reason: str | None = None


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


# --- get_outbox_status (read) --------------------------------------------
#
# A READ tool under `telemetry:read` (v0.6.9, plat #211) over the transactional outbox: rows
# committed and waiting to be PUBLISHED, NOT consumer lag. Null is "nothing to report", never
# zero; `unpublished_past_attempt_limit` counts rows INSIDE `unpublished_count`.


class GetOutboxStatusOutput(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    # Declaration order IS the snapshot's `required` order; the mirror test checks.
    measured_at: datetime
    unpublished_count: int
    oldest_unpublished_at: datetime | None = None
    oldest_unpublished_age_s: float | None = None
    newest_unpublished_at: datetime | None = None
    newest_unpublished_age_s: float | None = None
    unpublished_past_attempt_limit: int
    last_publish_at: datetime | None = None
    seconds_since_last_publish: float | None = None
    # The worker process's own clock, unlike everything above — the age carries skew.
    relay_last_tick_at: datetime | None = None
    relay_heartbeat_age_s: float | None = None
    relay_heartbeat_known: bool
    relay_heartbeat_unknown_reason: str | None = None
    relay_tick_interval_s: float


# --- get_slo_status (read) -----------------------------------------------
#
# A READ tool under `telemetry:read`, no arguments, no paging (v0.6.11, platform ADR 0030).
# Two readings that are easy to get backwards: `total: 0` makes `current_success_rate: 1.0`
# an absence of evidence, not health; `burn_rate: null` means UNBOUNDED, not unknown.


class SloObjective(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    # Declaration order IS the snapshot's `required` order; the mirror test checks.
    id: str
    name: str
    # The objective's own prose, not a field about this model; an ordinary field.
    description: str
    target: float
    window_hours: int
    # Read this first: the denominator.
    total: int
    failed: int
    current_success_rate: float
    budget_remaining_pct: float
    burn_rate: float | None = None
    healthy: bool
    fast_burn: bool


class GetSloStatusOutput(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    measured_at: datetime
    objectives: list[SloObjective]
    total: int
    fast_burn_threshold: float


# --- get_circuit_breakers (read) -----------------------------------------
#
# A READ tool under `telemetry:read`, no arguments (v0.6.11, plat #218, platform ADR 0030).
# State lives in Redis under `breaker:state:<name>`, so its timestamps are the OWNING
# process's clock. `reported_age_s` is not a heartbeat; empty + `unknown_reason` ≠ none open.


class CircuitBreakerReading(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    # Declaration order IS the snapshot's `required` order; the mirror test checks.
    name: str
    # `closed` / `open` / `half_open`. A plain string on the platform side, so a
    # Literal here would reject a state a later release adds.
    state: str
    failure_count: int
    failure_threshold: int
    recovery_timeout_s: float
    last_state_change_at: datetime | None = None
    seconds_since_state_change: float | None = None
    last_failure_at: datetime | None = None
    # A CLASS — `timeout`, `connection` or `other` — never the error text.
    last_failure_reason_class: str | None = None
    recorded_at: datetime
    reported_age_s: float


class GetCircuitBreakersOutput(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    measured_at: datetime
    breakers: list[CircuitBreakerReading]
    total: int
    unknown_reason: str | None = None


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
    # v0.6.0: capped — when `truncated`, conclude nothing from absence. Null is not zero.
    model_config = ConfigDict(extra="ignore", frozen=True)
    trace_id: str
    jobs: list[TracedJob]
    audit_events: list[TracedAuditRow]
    truncated: bool
    total_jobs: int
    total_audit_events: int | None = None


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
    # v0.6.0 (plat #185): closed enum — a typo is now an error, not "nothing happened".
    principal_type: Literal["user", "service_account"] | None = None
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
    # v0.4.0+: replay_safe | wait_and_replay | human_required; omit for all.
    remediation_hint: str | None = None
    limit: int = Field(default=50, ge=1, le=200)
    # v0.6.0 (plat #180, R2-53): paging — compare against `total` to know if more remain.
    offset: int = Field(default=0, ge=0)


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
    # v0.4.0+: platform category — replay_safe, wait_and_replay or
    # human_required — routes the planner. Null → agent classifies instead.
    remediation_hint: str | None = None
    # v0.6.0 (plat #180, R2-53): terminal time, and the list's order — not `created_at`.
    dead_lettered_at: datetime | None = None
    # v0.6.2 (WO-R2-158): a THIRD clock beside `created_at` and `dead_lettered_at`. Verify a
    # fence on `fenced_at`, never on `remediation_hint`. Null until fenced; a replay clears it.
    fenced_at: datetime | None = None
    # `"{principal_type}:{principal_id}"`, per-stack — never pin it; assert `fenced_at`.
    fenced_by: str | None = None
    extra: dict[str, Any] | None = None


class ListDlqMessagesOutput(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    total: int
    items: list[DlqEntry]


# --- Tier-1 write actions (remediation) ---------------------------------
#
# Platform enforces authz + idempotency + audit; ``policies.py`` bars the investigation planner.


class RestartConsumerGroupInput(BaseModel):
    model_config = _EMPTY_CONFIG
    consumer_group: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=8, max_length=255)


class RestartConsumerGroupOutput(BaseModel):
    # v0.4.5+: the single compensating action for both `kill_consumer` and `inject_latency`.
    model_config = ConfigDict(extra="ignore", frozen=True)
    consumer_group: str
    kill_key_cleared: bool
    latency_key_cleared: bool
    # v0.6.0 (plat #166): `accepted` is true even for a typo; this is not.
    group_recognized: bool
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
    # v0.6.0 (plat #172, R2-22): the bulk path FENCES `human_required`; True opts back in.
    include_human_required: bool = False
    idempotency_key: str = Field(min_length=8, max_length=255)


class ReplayedJob(BaseModel):
    """One legacy ``replay_dlq_messages.jobs[]`` entry: id + type, no outcome."""

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
    # v0.6.0 (plat #172, R2-22): what the human_required fence held back.
    skipped_human_required: int = 0
    skipped_jobs: list[ReplayedJob] = []


class ReplayResult(BaseModel):
    """Per-job outcome inside ``replay_dlq_by_ids.results[]`` (v0.4.4+).

    ``scheduled`` + ``execute_at`` are set by ``delay_seconds``.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)
    id: str
    ok: bool
    error: str | None = None
    scheduled: bool = False
    execute_at: float | None = None


# --- get_cache_key_info (read) -------------------------------------------
#
# A READ tool, not Tier-1 (v0.6.0, plat #146/#182, R2-54): the SHAPE of an entry (existence,
# TTL, type, size), never the value. Same prefixes `invalidate_cache_key` deletes.


class GetCacheKeyInfoInput(BaseModel):
    model_config = _EMPTY_CONFIG
    key: str = Field(min_length=1, max_length=512)


class GetCacheKeyInfoOutput(BaseModel):
    # v0.6.8 (WO-R3-267): `records_referenced` is what the entry names, `records_found` what
    # the tenant holds now. Equal counts do NOT say the copied fields are current; null ≠ 0.
    model_config = ConfigDict(extra="ignore", frozen=True)
    key: str
    exists: bool
    # Null for all three when absent — check `exists`; no expiry also reads null.
    type: str | None = None
    ttl_seconds: int | None = None
    size: int | None = None
    records_referenced: int | None = None
    records_found: int | None = None


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
# Category-aware replacements for the coarse ``replay_dlq_messages``: ``replay_dlq_by_ids``
# (targeted, cap 50), ``replay_dlq_by_category`` (bulk, replay_safe / wait_and_replay only),
# ``mark_dlq_permanent`` (human_required). ``delay_seconds`` defers the enqueue, platform-side.


class ReplayDlqByIdsInput(BaseModel):
    model_config = _EMPTY_CONFIG
    job_ids: list[UUID] = Field(min_length=1, max_length=50)
    idempotency_key: str = Field(min_length=8, max_length=255)
    # v0.4.1+: defers each replay (cap 1 hour) for ``wait_and_replay``; omit for immediate.
    delay_seconds: int | None = Field(default=None, ge=1, le=3600)


class ReplayDlqByIdsOutput(BaseModel):
    """v0.4.4 output: ``replayed`` + ``scheduled`` + ``failed`` sum to ``requested``.

    ``results[]`` says which ids were rejected.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)
    requested: int
    replayed: int
    # Platform advertises `scheduled` with default=0; `replayed` stays required.
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
    """v0.4.4 output shape.

    ``matched`` (not requested) is what the filter hit; ``replayed`` +
    ``scheduled`` + ``failed`` sum to it. ``execute_at`` is one bulk epoch.
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
    """Output shape from v0.4.4 platform, extended by v0.6.2.

    ``remediation_hint`` is always ``'human_required'`` after the call.
    ``already_marked`` does NOT mean a no-op — see the v0.6.2 correction below.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)
    job_id: str
    # Nullable but required (no default) per v0.4.8 outputSchema.
    previous_hint: str | None
    remediation_hint: str
    # v0.6.2 (WO-R2-158) CORRECTS THIS FIELD: every mark writes the hint, `fenced_at`,
    # `fenced_by` and an audit row, so this says "you were not the first", never "no-op".
    already_marked: bool
    # v0.6.2: when THIS call fenced the row. Required, so absence fails parsing rather than
    # reading as "not fenced". Verify a fence here, never on `remediation_hint`.
    fenced_at: datetime


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

#: Description prefixes for tools this registry deliberately does NOT mirror — a structural
#: filter, not a hand-list. ``[chaos:`` are the lab's hooks (``evals/chaos_hooks.py`` fires
#: them); ``[commander:`` is the agent's own telemetry, which the planner may not propose.
EXCLUDED_DESCRIPTION_PREFIXES: Final[tuple[str, ...]] = ("[chaos:", "[commander:")


def mirrored_in_registry(description: str) -> bool:
    """Whether a snapshot tool carrying this description belongs in ``TOOL_REGISTRY``.

    The one predicate the coverage pin reads, so no test keeps its own copy of the list.
    """
    return not description.startswith(EXCLUDED_DESCRIPTION_PREFIXES)


def _load_snapshot_descriptions(path: Path = _SNAPSHOT_PATH) -> dict[str, str]:
    """Tool descriptions, mirrored verbatim from the committed contract snapshot.

    Load-bearing — the remediation planner authors its verify expectation from
    them — so a missing or unreadable snapshot raises at import, never ``{}``. A
    packaged deployment must ship the snapshot as package data.
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
    "get_outbox_status": ToolSpec("get_outbox_status", _EmptyInput, GetOutboxStatusOutput),
    "get_circuit_breakers": ToolSpec("get_circuit_breakers", _EmptyInput, GetCircuitBreakersOutput),
    "get_slo_status": ToolSpec("get_slo_status", _EmptyInput, GetSloStatusOutput),
    "get_postgres_health": ToolSpec("get_postgres_health", _EmptyInput, PostgresHealthOutput),
    "get_redis_health": ToolSpec("get_redis_health", _EmptyInput, RedisHealthOutput),
    "get_trace": ToolSpec("get_trace", GetTraceInput, GetTraceOutput),
    "get_cache_key_info": ToolSpec(
        "get_cache_key_info", GetCacheKeyInfoInput, GetCacheKeyInfoOutput
    ),
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
    # v0.4.0 DLQ tools — the remediation planner routes on ``DlqEntry.remediation_hint``.
    "replay_dlq_by_ids": ToolSpec("replay_dlq_by_ids", ReplayDlqByIdsInput, ReplayDlqByIdsOutput),
    "replay_dlq_by_category": ToolSpec(
        "replay_dlq_by_category", ReplayDlqByCategoryInput, ReplayDlqByCategoryOutput
    ),
    "mark_dlq_permanent": ToolSpec(
        "mark_dlq_permanent", MarkDlqPermanentInput, MarkDlqPermanentOutput
    ),
}
