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


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings for application code. Tests should construct ``Settings`` directly."""
    return Settings()  # type: ignore[call-arg]
