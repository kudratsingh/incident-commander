"""Typed schemas for every platform MCP tool the agent uses.

Mirrors the platform's Pydantic models tool-for-tool; drift is caught against
``contracts/platform-tools.snapshot.json``. Tier-1 writes are gated by ``policies.py``.
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
    # Deliberately no minimum length: the platform accepts any string as a group name, and this
    # model must accept exactly what the platform does, not a stricter version of it.
    consumer_group: str = Field(default="worker-dispatcher")


class LagSample(BaseModel):
    """One past measurement of a group's lag (v0.6.7, plat #204)."""

    model_config = ConfigDict(extra="ignore", frozen=True)
    lag: int
    measured_at: datetime


class GetConsumerLagOutput(BaseModel):
    # A null `lag` means the platform could not work the lag out, NOT that the lag is zero, so read
    # `lag_known` before the number. Only `source: live` is a fresh reading (recomputed about every
    # 60 seconds); an empty `recent_samples` means no history was kept, not a flat history.
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
    # The `paused` fields arrived in platform v0.4.9. Before that a pause could not be observed at
    # all, so the tool that pauses a chain was shipped as something nothing could verify.
    model_config = ConfigDict(extra="ignore", frozen=True)
    # Platform v0.5.0 renamed only this field's human-readable title; the name on the wire is still
    # `seed_id`. The title is mirrored because the snapshot test compares titles as well as types.
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
    # Where the entries came from: the platform's own `deploy_markers` table, or the fallback it
    # reads from the environment when that table is empty. The two are not equally trustworthy.
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
# The `pools` list below holds readings each process published about ITSELF, not measurements taken
# when the call arrived (platform ADR 0033). Entries expire after 60 seconds, so a few seconds of
# `reported_age_s` is normal and a process that stopped publishing drops out: absent is not healthy.


class PoolGaugeReading(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    # Field ORDER matters: `test_registry_matches_snapshot.py` compares it to the snapshot's.
    # Which process published this reading: `api_worker` (the REST service and its background
    # workers) or `mcp`. A plain string, so a later release can add one without breaking parsing.
    process: str
    size: int
    # How many connections that process had checked out. Zero is a real reading (the pool is idle),
    # and the number only means something next to `size` and `max_overflow`.
    checked_out: int
    overflow: int
    max_overflow: int | None = None
    # Callers inside THAT process that gave up waiting for a connection in the minute before the
    # reading. Zero is a real answer, and a rising number is the pool running out.
    wait_timeouts_1m: int
    # Stamped by the process that published the reading, not the one answering this call, so the
    # two clocks can differ by a second or so.
    written_at: datetime
    reported_age_s: float


class PostgresHealthOutput(BaseModel):
    # Every field the platform sends is listed here, in its order: `extra="ignore"` would drop a
    # forgotten one without a word, so the agent would never see it (the snapshot test catches it).
    # Throughout, `null` means unknown and `0` is a real zero; `*_unknown_reason` says which.
    model_config = ConfigDict(extra="ignore", frozen=True)
    ok: bool
    ping_latency_ms: float | None = None
    active_connections: int | None = None
    dialect: str
    error: str | None = None
    # These five fields describe the pool of the ONE process that answered this call, the MCP
    # server. A pool exhausted in the REST service reads perfectly healthy here, which is what the
    # `pools` list further down exists to show instead (platform ADR 0030).
    pool_size: int | None = None
    # Counts the connection this very call is holding, so an otherwise idle process reports 1 and
    # never 0. Only meaningful read against `pool_size` and `pool_max_overflow`.
    pool_checked_out: int | None = None
    pool_overflow: int | None = None
    pool_max_overflow: int | None = None
    pool_wait_timeouts_1m: int | None = None
    pool_stats_unknown_reason: str | None = None
    # Asked of the database server itself, so unlike the pool fields these two cover queries from
    # every process at once. The timestamps behind them are the database server's clock.
    longest_active_query_ms: float | None = None
    active_queries_over_slow_threshold: int | None = None
    # The number of milliseconds the field above counts as "slow". Required, never absent, and not
    # something a caller can change — it is there so the count can be interpreted at all.
    slow_query_threshold_ms: float
    # These two are ALWAYS null in the pinned platform release; `query_stats_unknown_reason` says
    # why. Mirrored anyway so the shape matches, and the two fields above are the live substitute.
    p95_query_ms_1m: float | None = None
    slow_query_count_1m: int | None = None
    query_stats_unknown_reason: str | None = None
    # One entry per process that published a pool reading — the only place a pool held in a process
    # OTHER than the one answering shows up. The whole list is always sent; nothing is paged.
    pools: tuple[PoolGaugeReading, ...] = ()
    # Set only when no process reported anything, so an empty `pools` with a reason here means
    # nothing could be learned about any pool — which is not the same as "no pool is busy".
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
# A read-only tool over the outbox table: work the platform has committed and still has to PUBLISH.
# That is not consumer lag, which is work already published and not yet consumed. Null means
# "nothing to report", not zero, and `unpublished_past_attempt_limit` counts rows that are ALSO in
# `unpublished_count`, so adding the two together double-counts them.


class GetOutboxStatusOutput(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    # Field ORDER matters: `test_registry_matches_snapshot.py` compares it to the snapshot's.
    measured_at: datetime
    unpublished_count: int
    oldest_unpublished_at: datetime | None = None
    oldest_unpublished_age_s: float | None = None
    newest_unpublished_at: datetime | None = None
    newest_unpublished_age_s: float | None = None
    unpublished_past_attempt_limit: int
    last_publish_at: datetime | None = None
    seconds_since_last_publish: float | None = None
    # Stamped by the relay worker itself, unlike every field above, so the age derived from it
    # carries the difference between that worker's clock and the answering process's.
    relay_last_tick_at: datetime | None = None
    relay_heartbeat_age_s: float | None = None
    relay_heartbeat_known: bool
    relay_heartbeat_unknown_reason: str | None = None
    relay_tick_interval_s: float


# --- get_slo_status (read) -----------------------------------------------
#
# A read-only tool, no arguments, whole answer in one response (platform ADR 0030). Two readings
# are easy to get backwards: with `total: 0` nothing was measured, so `current_success_rate: 1.0`
# says nothing at all; and `burn_rate: null` means the budget is burning without bound, not unknown.


class SloObjective(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    # Field ORDER matters: `test_registry_matches_snapshot.py` compares it to the snapshot's.
    id: str
    name: str
    # The objective's own wording, sent by the platform. It is ordinary data, not documentation
    # about this model, which is why it is a field rather than part of the docstring.
    description: str
    target: float
    window_hours: int
    # How many requests the success rate below was computed over. Read it first: at zero, every
    # rate and percentage in this model is arithmetic over nothing.
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
# A read-only tool, no arguments (platform ADR 0030). Each breaker's state is kept in Redis under
# `breaker:state:<name>`, stamped by whichever process owns it, so `reported_age_s` is the age of
# that entry and not a sign of life. An empty list with `unknown_reason` set means nothing could be
# read about any breaker — never that no breaker is open.


class CircuitBreakerReading(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    # Field ORDER matters: `test_registry_matches_snapshot.py` compares it to the snapshot's.
    name: str
    # One of `closed`, `open` or `half_open`. Typed as a plain string, as on the platform side, so
    # a state added by a later release parses instead of failing the whole reading.
    state: str
    failure_count: int
    failure_threshold: int
    recovery_timeout_s: float
    last_state_change_at: datetime | None = None
    seconds_since_state_change: float | None = None
    last_failure_at: datetime | None = None
    # A category of failure — `timeout`, `connection` or `other` — never the error message itself,
    # so nothing an upstream service wrote can reach a prompt through this field.
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
    # The platform caps how much of a trace it returns, so when `truncated` is true a job or an
    # audit row missing from these lists may simply be past the cap. A null count is not a zero.
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
    # Only these two values, and the platform rejects anything else. Before that a misspelled
    # principal type came back as an empty list, which reads exactly like "nothing happened".
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
    # Filters to one platform category: `replay_safe`, `wait_and_replay` or `human_required`.
    # Leave it out to list every dead-lettered job whatever its category.
    remediation_hint: str | None = None
    limit: int = Field(default=50, ge=1, le=200)
    # Where in the list to start. The answer's `total` counts every matching row, so compare it
    # against offset plus the rows returned to know whether more are waiting.
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
    # The platform's own verdict on the job — `replay_safe`, `wait_and_replay` or
    # `human_required` — which is what the planner routes on. Null means the platform had no
    # verdict, so the agent has to decide from the error message itself.
    remediation_hint: str | None = None
    # When the job gave up and landed in the queue, which is also the order this list comes in.
    # Not the same as `created_at`, which is when the work was first submitted.
    dead_lettered_at: datetime | None = None
    # When this job was fenced out of automatic replay — a third timestamp beside the two above.
    # This is the field that proves a fence happened, never `remediation_hint`, which a job can
    # carry for other reasons. Null until fenced, and a later replay clears it again.
    fenced_at: datetime | None = None
    # Who fenced it, as `"{principal_type}:{principal_id}"`. The id differs between stacks, so no
    # test should assert on this value; assert on `fenced_at` instead.
    fenced_by: str | None = None
    extra: dict[str, Any] | None = None


class ListDlqMessagesOutput(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    total: int
    items: list[DlqEntry]


# --- Tier-1 write actions (remediation) ---------------------------------
#
# The platform is what actually checks permission, de-duplicates a repeated call and writes the
# audit row. On this side, ``policies.py`` keeps these tools out of the investigation planner's
# hands entirely, so only the remediation path can ever propose one.


class RestartConsumerGroupInput(BaseModel):
    model_config = _EMPTY_CONFIG
    consumer_group: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=8, max_length=255)


class RestartConsumerGroupOutput(BaseModel):
    # This one action undoes both faults the lab can inject into a consumer group: a killed
    # consumer and an injected latency. The two `*_cleared` flags say which one it actually undid.
    model_config = ConfigDict(extra="ignore", frozen=True)
    consumer_group: str
    kill_key_cleared: bool
    latency_key_cleared: bool
    # Whether the platform knows this consumer group at all. Read it: `accepted` below is true even
    # for a misspelled group name, so `accepted` alone cannot tell a restart from a no-op.
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
    # The bulk replay skips jobs the platform marked `human_required`; setting this to true replays
    # them anyway, which means re-running payloads a human was supposed to look at first.
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
    # How many jobs the `human_required` fence held back, and which ones. A non-zero count means
    # work is still undone even though the call succeeded.
    skipped_human_required: int = 0
    skipped_jobs: list[ReplayedJob] = []


class ReplayResult(BaseModel):
    """Per-job outcome inside ``replay_dlq_by_ids.results[]``.

    ``scheduled`` and ``execute_at`` are filled only when the call asked for a delay: the job was
    accepted but has not run yet, so an immediate replay and a deferred one look different here.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)
    id: str
    ok: bool
    error: str | None = None
    scheduled: bool = False
    execute_at: float | None = None


# --- get_cache_key_info (read) -------------------------------------------
#
# A read-only tool, not an action: it reports what a cache entry LOOKS like — whether it exists,
# its remaining time to live, its type and size — and never the value stored in it. It accepts the
# same key prefixes that `invalidate_cache_key` below is allowed to delete.


class GetCacheKeyInfoInput(BaseModel):
    model_config = _EMPTY_CONFIG
    key: str = Field(min_length=1, max_length=512)


class GetCacheKeyInfoOutput(BaseModel):
    # `records_referenced` is how many records the cached entry claims to cover, `records_found` how
    # many of them the tenant holds right now. Equal counts do NOT mean the cached copies are
    # up to date — only that nothing was added or removed. Null is unknown, not zero.
    model_config = ConfigDict(extra="ignore", frozen=True)
    key: str
    exists: bool
    # All three are null when there is no entry, so read `exists` first. `ttl_seconds` is also null
    # for an entry that exists with no expiry set, which is not the same as an absent entry.
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
# Three tools that replaced the all-or-nothing ``replay_dlq_messages`` with ones that respect the
# platform's own verdict on a job: ``replay_dlq_by_ids`` replays up to 50 named jobs,
# ``replay_dlq_by_category`` replays a whole category and refuses `human_required`, and
# ``mark_dlq_permanent`` fences such a job. ``delay_seconds`` makes the PLATFORM hold a replay back.


class ReplayDlqByIdsInput(BaseModel):
    model_config = _EMPTY_CONFIG
    job_ids: list[UUID] = Field(min_length=1, max_length=50)
    idempotency_key: str = Field(min_length=8, max_length=255)
    # Holds each replay back by this many seconds, up to an hour — the point of the
    # `wait_and_replay` category, where the job failed on something temporary. Omit to replay now.
    delay_seconds: int | None = Field(default=None, ge=1, le=3600)


class ReplayDlqByIdsOutput(BaseModel):
    """``replayed`` plus ``scheduled`` plus ``failed`` add up to ``requested``.

    So a call that reports no failures can still have replayed nothing yet. ``results[]`` is the
    per-job detail, including which ids the platform rejected and why.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)
    requested: int
    replayed: int
    # Defaulted because the platform declares a default for it and an older release omitted it
    # entirely; `replayed` above is always sent, so it stays required.
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
    # Holds every replay back by this many seconds, exactly as in ``replay_dlq_by_ids`` above.
    delay_seconds: int | None = Field(default=None, ge=1, le=3600)


class ReplayDlqByCategoryOutput(BaseModel):
    """What the platform reports after replaying one whole category.

    ``matched`` is how many jobs the filter hit, and ``replayed`` + ``scheduled`` + ``failed`` add
    up to it; there is no ``requested``, because the caller named a category, not a list of jobs.
    ``execute_at`` is one time for the whole batch, not one per job.
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
    """What the platform reports after fencing one dead-lettered job out of automatic replay.

    ``remediation_hint`` is always ``'human_required'`` once the call succeeds. ``already_marked``
    does NOT mean the call did nothing — see the field's own note below.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)
    job_id: str
    # The category the job carried before this call. It may be null (the platform had no verdict),
    # but the field itself is always sent, which is why it has no default here.
    previous_hint: str | None
    remediation_hint: str
    # True when the job was already fenced before this call — NOT "nothing happened". Every call
    # rewrites the hint, `fenced_at`, `fenced_by` and an audit row, so this flag says only that
    # someone got there first; reading it as a no-op is what WO-R2-158 had to correct.
    already_marked: bool
    # When THIS call fenced the row. Always sent, so a missing value fails parsing instead of
    # quietly reading as "not fenced". This is the field that proves a fence, not the hint above.
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

#: Tools whose platform description starts with one of these are deliberately NOT mirrored here, so
#: the planner can never propose one. ``[chaos:`` marks the lab's fault injectors, which only
#: ``evals/chaos_hooks.py`` fires; ``[commander:`` marks the agent's own reporting endpoints.
EXCLUDED_DESCRIPTION_PREFIXES: Final[tuple[str, ...]] = ("[chaos:", "[commander:")


def mirrored_in_registry(description: str) -> bool:
    """Whether a snapshot tool carrying this description belongs in ``TOOL_REGISTRY``.

    The single place that question is answered, so the test that checks the registry covers the
    snapshot cannot drift by keeping its own copy of the exclusion list.
    """
    return not description.startswith(EXCLUDED_DESCRIPTION_PREFIXES)


def _load_snapshot_descriptions(path: Path = _SNAPSHOT_PATH) -> dict[str, str]:
    """Tool descriptions, copied word for word from the committed contract snapshot.

    The remediation planner writes its verification expectations out of these, so an empty set
    would silently produce worse plans: a missing or unreadable snapshot raises at import instead.
    Anything that installs this package has to ship the snapshot file with it.
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
    # Read tools: they change nothing, so the investigation planner may propose any of them.
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
    # Actions that change the platform. Only the remediation planner may propose one, and
    # ``policies.py`` is what keeps them out of the investigation planner's tool list.
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
    # The category-aware dead-letter tools. Which of them a plan uses is decided by the platform's
    # own verdict on the job, ``DlqEntry.remediation_hint``, not by the agent's reading of it.
    "replay_dlq_by_ids": ToolSpec("replay_dlq_by_ids", ReplayDlqByIdsInput, ReplayDlqByIdsOutput),
    "replay_dlq_by_category": ToolSpec(
        "replay_dlq_by_category", ReplayDlqByCategoryInput, ReplayDlqByCategoryOutput
    ),
    "mark_dlq_permanent": ToolSpec(
        "mark_dlq_permanent", MarkDlqPermanentInput, MarkDlqPermanentOutput
    ),
}
