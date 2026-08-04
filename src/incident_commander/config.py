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
    )

    # Anthropic
    anthropic_api_key: SecretStr

    # Models. Verify strings against docs.claude.com before changing defaults.
    agent_model: str = "claude-sonnet-4-6"
    judge_model: str

    # Platform. MCP and REST are separate URLs per platform ADR-0006.
    platform_mcp_url: AnyHttpUrl
    platform_rest_url: AnyHttpUrl
    platform_token: SecretStr
    platform_webhook_secret: SecretStr

    # Agent-owned Postgres.
    database_url: PostgresDsn

    # Per-incident hard budgets (CLAUDE.md invariant 7).
    budget_max_tool_calls: int = Field(default=25, ge=1)
    budget_max_tokens: int = Field(default=500_000, ge=1)
    budget_max_seconds: int = Field(default=1_800, ge=1)
    budget_max_usd: Decimal = Field(default=Decimal("5.00"), ge=Decimal("0"))

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
