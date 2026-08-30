"""Runtime configuration loaded from environment (see .env.example)."""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from typing import Final

from pydantic import AnyHttpUrl, Field, PostgresDsn, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Peak connections one investigation run holds AT ONCE (ADR 0022): the lease
# connection, pinned for the whole run by ``incident_lease`` (ADR 0016), plus
# at most one transient checkout for a checkpoint load or write.
# ``PostgresCheckpointer.write`` does its version read and its INSERT in ONE
# transaction on ONE connection, so the transient half is 1 and this total
# is 2. Raising either of those makes this number wrong and the ceiling below
# unsafe, so the two move together.
_CONNECTIONS_PER_RUN: Final[int] = 2

# How long the platform can keep serving a stale metric after the world
# changed. ``get_consumer_lag`` reads a cache the platform recomputes on a
# 60s interval (docs/eval-methodology.md, ADR 0006 "Context"), so any
# polling window that must observe a *change* has to outlast this number —
# a shorter window can only ever look at the pre-change value.
PLATFORM_METRICS_INTERVAL_SECONDS: Final[float] = 60.0


def polling_window_seconds(attempts: int, delay_seconds: float) -> float:
    """Wall-clock span covered by a bounded polling loop, in seconds.

    Both polling loops in this codebase — ADR 0006's verify window
    (``agent/remediation.py::make_llm_verify``) and the eval precondition
    probe (``evals/runner.py::_assert_preconditions``) — are written as::

        for attempt in range(attempts):
            if attempt:
                sleep(delay_seconds)
            ...probe...

    so the delay falls BETWEEN attempts: ``attempts`` probes are separated
    by ``attempts - 1`` sleeps. The window is therefore
    ``(attempts - 1) * delay_seconds``, not ``attempts * delay_seconds`` —
    the last probe fires at the end of the window, not one delay past it.

    The distinction is not cosmetic: ``attempts * delay`` overstates the
    real wait by one delay, so a guard using it green-lights a window that
    does not actually outlast the staleness it was sized for (WO-R2-88).
    A single attempt is not polling at all and returns 0.0.
    """
    return max(attempts - 1, 0) * delay_seconds


class Settings(BaseSettings):
    """Immutable application settings. Constructed once at startup."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
        # A blank env/dotenv entry (VAR=) means "unset", not "empty string":
        # optional fields fall back to their defaults instead of failing
        # int/Decimal parsing on "", and required fields raise a clear
        # "Field required" at construction instead of silently accepting ""
        # (C-09 — `cp .env.example .env` + fill secrets must just work).
        env_ignore_empty=True,
    )

    # Anthropic
    anthropic_api_key: SecretStr

    # Models. Verify strings against docs.claude.com before changing defaults.
    agent_model: str = "claude-sonnet-4-6"
    # Required with no default (pinned separately for eval stability, per
    # CLAUDE.md). min_length guards direct construction — Settings(
    # judge_model="") — which env_ignore_empty cannot reach; an empty judge
    # id otherwise only failed as an API 400 at the first judge call mid-run.
    judge_model: str = Field(min_length=1)

    # Platform. MCP and REST are separate URLs per platform ADR-0006.
    platform_mcp_url: AnyHttpUrl
    platform_rest_url: AnyHttpUrl
    platform_token: SecretStr
    # Read-scoped twin used by `make eval-smoke` (telemetry:read +
    # incidents:read only). Selected by the runner's --smoke flag rather
    # than shell plumbing: passing it through make was silently defeated
    # by `-include .env` (PR #62 vs #69), so every "read-scoped" smoke run
    # up to 2026-08-07 actually held write scope. Config beats inheritance.
    platform_smoke_token: SecretStr | None = None
    # Principal ids of the two service accounts above, printed by
    # `make bootstrap-token`. They scope the post-stage audit guard to
    # self-owned principals so a neighbouring tenant's legitimate Tier-1
    # success on a shared platform is not our exit 5 (A-13). Not secrets —
    # plain ids, no scope, nothing to authenticate with. BOTH must be set
    # to take effect: the failure mode the guard exists for (F-001, the
    # "read-scoped" stage silently holding the full token) writes its
    # audit rows under the AGENT principal, so filtering to the smoke id
    # alone would make the guard blind to its own reason for existing.
    # Unset means unfiltered — any service account's success fails.
    platform_agent_principal_id: str | None = None
    platform_smoke_principal_id: str | None = None
    platform_webhook_secret: SecretStr
    # Webhook ingress replay guard (ADR 0014): reject deliveries whose
    # X-Alert-Timestamp (epoch ms, platform alerts.py) deviates from local
    # time by more than this many seconds. Also bounds the window in which
    # an identical redelivery is suppressed instead of spawning a run.
    webhook_max_skew_seconds: int = Field(default=300, ge=1)

    # Agent-owned Postgres.
    database_url: PostgresDsn

    # --- Connection pool and run admission (ADR 0022) ---------------------
    # SQLAlchemy's unconfigured default pool (5 + 10 overflow, 30s timeout) is
    # not a safe pool for this application, and the reason is the lease. A run
    # PINS one connection for its whole life (ADR 0016, up to
    # ``budget_max_seconds``) and then asks the SAME pool for a second one on
    # every checkpoint write. Fifteen concurrent runs therefore hold all
    # fifteen connections and every one of them blocks waiting for a
    # connection only another lease holder could release — textbook
    # hold-and-wait, and the crash rail that would record the failure needs a
    # connection too. The pool is sized explicitly here, and the number of
    # live runs is bounded to what that pool can actually serve.
    db_pool_size: int = Field(default=10, ge=1)
    db_max_overflow: int = Field(default=10, ge=0)
    # Deliberately far below SQLAlchemy's 30s default. This is the wait a
    # checkout endures before giving up; it is spent inside a threadpool
    # worker on the ingress path, and every second of it is a second the
    # alert is neither investigated nor refused.
    db_pool_timeout_seconds: float = Field(default=10.0, gt=0)
    # Connections held back from the run bound for work that is NOT a run:
    # ingress identity derivation and its checkpoint write, plus the crash
    # rail. Unlike a lease these are short-lived and always released, so they
    # cause contention, never deadlock — headroom is the right instrument.
    db_ingest_reserved_connections: int = Field(default=4, ge=0)
    # Optional lower bound on concurrent runs. Unset means "whatever the pool
    # safely serves" (``max_concurrent_runs`` below). It can only ever be set
    # LOWER than that ceiling — the validator refuses a value that would
    # reintroduce the deadlock, so the pool arithmetic is not something an
    # operator can accidentally opt out of.
    agent_max_concurrent_runs: int | None = Field(default=None, ge=1)

    # Per-incident hard budgets (CLAUDE.md invariant 7).
    budget_max_tool_calls: int = Field(default=25, ge=1)
    budget_max_tokens: int = Field(default=500_000, ge=1)
    budget_max_seconds: int = Field(default=1_800, ge=1)
    budget_max_usd: Decimal = Field(default=Decimal("5.00"), ge=Decimal("0"))

    # Operational kill switch, env var AGENT_ENABLED (docs/safety-model.md
    # #kill-switch and docs/runbook.md#kill-switch): false keeps the webhook
    # ingress accepting and recording alerts but spawns no investigation
    # runs — the state machine never advances. Read once at process start
    # (Settings is frozen, get_settings() is cached); changing it requires
    # an agent-process restart.
    agent_enabled: bool = True

    # Live-eval verification polling. attempts=1 keeps the legacy single-
    # probe behavior (canned/offline runs). Live runs should set
    # VERIFY_PROBE_ATTEMPTS>1 so eventually-consistent probes (e.g. the
    # 60s-cached consumer lag) are re-read over a window instead of judged
    # on one instant read taken ~1s after the action.
    verify_probe_attempts: int = Field(default=1, ge=1, le=10)
    verify_probe_delay_seconds: float = Field(default=15.0, ge=0.0)

    # Investigation-side twin of the verify window (ADR 0009): when a probe
    # of a declared-cached tool kills a fixable hypothesis at/above the
    # remediate threshold, re-read it fresh before accepting the
    # contradiction — at most this many times per tool per run. Default 0
    # keeps canned runs byte-identical (a re-probe would consume an extra
    # scripted planner response); the eval runner wires it for live runs.
    investigate_reprobe_attempts: int = Field(default=0, ge=0, le=3)
    investigate_reprobe_delay_seconds: float = Field(default=20.0, ge=0.0)

    # Tier-1 action tool calls (restart_consumer_group, replay_dlq_by_ids,
    # etc.) do real work on the platform side and can legitimately take
    # longer than a read. The MCPClient's 30s default is right for reads;
    # actions get their own knob so a slow-but-successful action doesn't
    # escalate as a transport error.
    action_tool_timeout_seconds: float = Field(default=60.0, ge=1.0)

    @property
    def verify_polling_window_seconds(self) -> float:
        """Wall-clock span of the ADR 0006 verify window at these settings.

        0.0 at the defaults (one probe is not a window); 100.0 at the
        live-recommended knobs (6 attempts, 20s → five sleeps). ADR 0006's
        2026-08-30 amendment quotes these two numbers and
        ``tests/unit/test_polling_window.py`` holds the ADR to them.
        """
        return polling_window_seconds(
            self.verify_probe_attempts, self.verify_probe_delay_seconds
        )

    @property
    def db_pool_capacity(self) -> int:
        """Total connections the pool will ever hand out at once."""
        return self.db_pool_size + self.db_max_overflow

    @property
    def max_concurrent_runs(self) -> int:
        """How many investigation runs may hold a lease simultaneously.

        Derived, not guessed: whatever is left of the pool after the ingest
        reservation, divided by the connections one run needs at its peak.
        ``AGENT_MAX_CONCURRENT_RUNS`` may lower it and — enforced below —
        never raise it.
        """
        ceiling = (
            self.db_pool_capacity - self.db_ingest_reserved_connections
        ) // _CONNECTIONS_PER_RUN
        if self.agent_max_concurrent_runs is None:
            return ceiling
        return min(self.agent_max_concurrent_runs, ceiling)

    @model_validator(mode="after")
    def _run_bound_fits_the_pool(self) -> Settings:
        """Refuse at startup any pool that cannot serve a single run.

        A misconfiguration here does not fail loudly on its own — it fails as
        a 10-second stall under load, which is the failure this whole
        arrangement exists to prevent. Better to never boot.
        """
        capacity = self.db_pool_capacity
        for_runs = capacity - self.db_ingest_reserved_connections
        if for_runs < _CONNECTIONS_PER_RUN:
            raise ValueError(
                f"pool capacity DB_POOL_SIZE+DB_MAX_OVERFLOW={capacity} minus "
                f"DB_INGEST_RESERVED_CONNECTIONS={self.db_ingest_reserved_connections} "
                f"leaves {for_runs} connections for runs, but one run needs "
                f"{_CONNECTIONS_PER_RUN} at its peak (pinned lease + checkpoint "
                "write). No run could finish. Raise the pool or lower the "
                "reservation."
            )
        requested = self.agent_max_concurrent_runs
        ceiling = for_runs // _CONNECTIONS_PER_RUN
        if requested is not None and requested > ceiling:
            raise ValueError(
                f"AGENT_MAX_CONCURRENT_RUNS={requested} exceeds the {ceiling} runs this "
                f"pool can serve without deadlocking ({capacity} capacity - "
                f"{self.db_ingest_reserved_connections} reserved, {_CONNECTIONS_PER_RUN} "
                "connections per run). Raise DB_POOL_SIZE/DB_MAX_OVERFLOW to lift the "
                "ceiling, or lower the bound."
            )
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings for application code. Tests should construct ``Settings`` directly."""
    return Settings()  # type: ignore[call-arg]
