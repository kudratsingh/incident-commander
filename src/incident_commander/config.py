"""Runtime configuration loaded from environment (see .env.example)."""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache

from pydantic import AnyHttpUrl, Field, PostgresDsn, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


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


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings for application code. Tests should construct ``Settings`` directly."""
    return Settings()  # type: ignore[call-arg]
