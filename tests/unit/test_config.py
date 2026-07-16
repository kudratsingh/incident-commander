from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from incident_commander.config import Settings, get_settings

_ENV_VARS = (
    "ANTHROPIC_API_KEY",
    "AGENT_MODEL",
    "JUDGE_MODEL",
    "PLATFORM_MCP_URL",
    "PLATFORM_REST_URL",
    "PLATFORM_TOKEN",
    "PLATFORM_WEBHOOK_SECRET",
    "DATABASE_URL",
    "BUDGET_MAX_TOOL_CALLS",
    "BUDGET_MAX_TOKENS",
    "BUDGET_MAX_SECONDS",
    "BUDGET_MAX_USD",
)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def _settings(**overrides: Any) -> Settings:
    """Test-only constructor: bypasses any local .env file and applies overrides."""
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


@pytest.fixture
def valid_kwargs() -> dict[str, Any]:
    return {
        "anthropic_api_key": "sk-ant-test",
        "judge_model": "claude-haiku-4-5",
        "platform_mcp_url": "https://mcp.platform.local",
        "platform_rest_url": "https://api.platform.local",
        "platform_token": "svc-token",
        "platform_webhook_secret": "hmac-secret",
        "database_url": "postgresql://commander:commander@localhost:5432/commander",
    }


class TestSettings:
    def test_constructs_with_valid_kwargs(self, valid_kwargs: dict[str, Any]) -> None:
        settings = _settings(**valid_kwargs)
        assert settings.agent_model == "claude-sonnet-4-6"
        assert settings.judge_model == "claude-haiku-4-5"

    def test_defaults_applied(self, valid_kwargs: dict[str, Any]) -> None:
        settings = _settings(**valid_kwargs)
        assert settings.budget_max_tool_calls == 25
        assert settings.budget_max_tokens == 500_000
        assert settings.budget_max_seconds == 1_800
        assert settings.budget_max_usd == Decimal("5.00")

    @pytest.mark.parametrize(
        "missing",
        [
            "anthropic_api_key",
            "judge_model",
            "platform_mcp_url",
            "platform_rest_url",
            "platform_token",
            "platform_webhook_secret",
            "database_url",
        ],
    )
    def test_missing_required_field_rejected(
        self, valid_kwargs: dict[str, Any], missing: str
    ) -> None:
        del valid_kwargs[missing]
        with pytest.raises(ValidationError) as exc:
            _settings(**valid_kwargs)
        assert missing in str(exc.value)

    def test_secret_str_wraps_secrets(self, valid_kwargs: dict[str, Any]) -> None:
        settings = _settings(**valid_kwargs)
        assert isinstance(settings.anthropic_api_key, SecretStr)
        assert isinstance(settings.platform_token, SecretStr)
        assert isinstance(settings.platform_webhook_secret, SecretStr)
        assert settings.platform_token.get_secret_value() == "svc-token"
        assert "svc-token" not in repr(settings)

    def test_invalid_url_rejected(self, valid_kwargs: dict[str, Any]) -> None:
        valid_kwargs["platform_mcp_url"] = "not-a-url"
        with pytest.raises(ValidationError):
            _settings(**valid_kwargs)

    def test_non_postgres_database_url_rejected(self, valid_kwargs: dict[str, Any]) -> None:
        valid_kwargs["database_url"] = "mysql://user:pass@localhost/db"
        with pytest.raises(ValidationError):
            _settings(**valid_kwargs)

    def test_zero_budget_tool_calls_rejected(self, valid_kwargs: dict[str, Any]) -> None:
        valid_kwargs["budget_max_tool_calls"] = 0
        with pytest.raises(ValidationError):
            _settings(**valid_kwargs)

    def test_frozen_direct_mutation_rejected(self, valid_kwargs: dict[str, Any]) -> None:
        settings = _settings(**valid_kwargs)
        with pytest.raises(ValidationError):
            settings.agent_model = "something-else"

    def test_reads_from_environment(
        self,
        valid_kwargs: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        for key, value in valid_kwargs.items():
            monkeypatch.setenv(key.upper(), str(value))
        monkeypatch.setenv("BUDGET_MAX_TOOL_CALLS", "42")
        settings = _settings()
        assert settings.budget_max_tool_calls == 42
        assert settings.judge_model == "claude-haiku-4-5"


class TestGetSettings:
    def test_caches(
        self,
        valid_kwargs: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        for key, value in valid_kwargs.items():
            monkeypatch.setenv(key.upper(), str(value))
        get_settings.cache_clear()
        first = get_settings()
        second = get_settings()
        assert first is second
        get_settings.cache_clear()
