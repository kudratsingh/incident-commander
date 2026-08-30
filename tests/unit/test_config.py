from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from incident_commander.config import Settings, get_settings, settings_env_var_names

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Every env var Settings can read, walked from the model itself. Env vars
# outrank dotenv, so any of these leaking from the developer's shell would
# silently override the file under test — which is why this is derived and
# not typed out. The hand-kept version of this tuple had drifted from the
# model it claimed to describe (WO-R2-87).
_ENV_VARS = settings_env_var_names()

# The same surface, written down once, as a tripwire rather than as the
# source: a new Settings field fails the assertion in TestEnvIsolation until
# somebody adds it here, and adding it here is the prompt to ask whether the
# new knob is also in .env.example and the operator docs. Nothing reads this
# to build an isolation list — that is what the derived tuple above is for.
_DOCUMENTED_ENV_VARS = frozenset(
    {
        "ACTION_TOOL_TIMEOUT_SECONDS",
        "AGENT_ENABLED",
        "AGENT_MAX_CONCURRENT_RUNS",
        "AGENT_MODEL",
        "ANTHROPIC_API_KEY",
        "BUDGET_MAX_SECONDS",
        "BUDGET_MAX_TOKENS",
        "BUDGET_MAX_TOOL_CALLS",
        "BUDGET_MAX_USD",
        "DATABASE_URL",
        "DB_INGEST_RESERVED_CONNECTIONS",
        "DB_MAX_OVERFLOW",
        "DB_POOL_SIZE",
        "DB_POOL_TIMEOUT_SECONDS",
        "HEALTH_PROBE_TIMEOUT_SECONDS",
        "INVESTIGATE_REPROBE_ATTEMPTS",
        "INVESTIGATE_REPROBE_DELAY_SECONDS",
        "JUDGE_MODEL",
        "PLATFORM_AGENT_PRINCIPAL_ID",
        "PLATFORM_MCP_URL",
        "PLATFORM_REST_URL",
        "PLATFORM_SMOKE_PRINCIPAL_ID",
        "PLATFORM_SMOKE_TOKEN",
        "PLATFORM_TOKEN",
        "PLATFORM_WEBHOOK_SECRET",
        "VERIFY_PROBE_ATTEMPTS",
        "VERIFY_PROBE_DELAY_SECONDS",
        "WEBHOOK_MAX_BODY_BYTES",
        "WEBHOOK_MAX_SKEW_SECONDS",
    }
)


def _clear_settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset every variable Settings reads. The autouse fixture's whole body,
    extracted so the isolation itself can be tested rather than assumed."""
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_settings_env(monkeypatch)


class TestEnvIsolation:
    """The isolation fixture must cover the settings surface, not a copy of it.

    ``_ENV_VARS`` was a hand-kept list documented as "every env var Settings
    can read", and it had drifted: the principal ids and the whole ADR-0022
    pool group were missing, so a developer with any of them exported ran
    these tests against their own environment while the file under test said
    otherwise. Deriving the list removes the drift; these two tests are what
    keep the derivation honest.
    """

    def test_the_fixture_clears_every_variable_settings_reads(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The developer's shell, reproduced: every variable set to a poison
        # value, then the fixture's own isolation applied on top of it.
        for name in settings_env_var_names():
            monkeypatch.setenv(name, "9999")
        _clear_settings_env(monkeypatch)
        survivors = sorted(name for name in settings_env_var_names() if name in os.environ)
        assert survivors == [], (
            f"these Settings variables survive the isolation fixture: {survivors}. "
            "Whatever value the developer has exported for them is what the tests "
            "below actually read."
        )

    def test_the_settings_env_surface_is_the_documented_one(self) -> None:
        # The derived list is the thing the fixtures use; this is the tripwire
        # that a NEW setting was noticed. Adding a field to Settings fails
        # here until its variable is added below, which is the moment to ask
        # whether it also needs a line in .env.example and the docs.
        assert set(settings_env_var_names()) == _DOCUMENTED_ENV_VARS


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

    @pytest.mark.parametrize("zero", [Decimal("0"), Decimal("0.00"), "0"])
    def test_zero_budget_usd_rejected(self, valid_kwargs: dict[str, Any], zero: Any) -> None:
        # BUDGET_MAX_USD=0 was the one budget dimension that accepted zero,
        # and is_exhausted compares with >=, so every run was born exhausted:
        # it terminates on its first check having done nothing, and the
        # result reads exactly like a budget policy working correctly.
        valid_kwargs["budget_max_usd"] = zero
        with pytest.raises(ValidationError):
            _settings(**valid_kwargs)

    def test_a_sub_dollar_budget_is_still_allowed(self, valid_kwargs: dict[str, Any]) -> None:
        # The bound is gt=0, not ge=1 like the integer dimensions: a cheap
        # scenario capped at fifty cents is a legitimate operator choice, and
        # unlike zero it is not exhausted before it starts.
        valid_kwargs["budget_max_usd"] = Decimal("0.50")
        assert _settings(**valid_kwargs).budget_max_usd == Decimal("0.50")

    def test_agent_enabled_defaults_true(self, valid_kwargs: dict[str, Any]) -> None:
        # The kill switch (docs/safety-model.md#kill-switch) must be ON by
        # default — finding B-03: the documented env var had no field at all.
        settings = _settings(**valid_kwargs)
        assert settings.agent_enabled is True

    def test_agent_enabled_env_false_parses(
        self,
        valid_kwargs: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("AGENT_ENABLED", "false")
        settings = _settings(**valid_kwargs)
        assert settings.agent_enabled is False

    def test_blank_judge_model_rejected(self, valid_kwargs: dict[str, Any]) -> None:
        # C-09: judge_model="" used to be silently accepted and only failed
        # as an API 400 at the first judge call mid-run. min_length=1 guards
        # the direct-construction path that env_ignore_empty cannot reach.
        valid_kwargs["judge_model"] = ""
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


class TestEnvExampleTemplate:
    """`cp .env.example .env` + fill secrets is the documented onboarding
    path — the template must be copy-safe (C-09) and must ship the
    live-only mitigation knobs (S-10)."""

    def test_env_example_with_secrets_filled_parses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The C-09 repro inverted into a regression test. Env vars outrank
        # dotenv, so setenv stands in for "fill in the secrets"; everything
        # else comes from the template verbatim.
        for name, value in {
            "ANTHROPIC_API_KEY": "sk-ant-test",
            "JUDGE_MODEL": "claude-haiku-4-5",
            "PLATFORM_TOKEN": "sa_test",
            "PLATFORM_WEBHOOK_SECRET": "hmac-secret",
            "DATABASE_URL": "postgresql://commander:commander@localhost:5432/commander",
        }.items():
            monkeypatch.setenv(name, value)
        settings = Settings(_env_file=str(_REPO_ROOT / ".env.example"))  # type: ignore[call-arg]
        # Optional entries left out (or blank) fall back to config.py
        # defaults instead of failing int/Decimal parsing on "".
        assert settings.budget_max_tokens == 500_000
        assert settings.budget_max_seconds == 1_800
        assert settings.budget_max_usd == Decimal("5.00")
        # The shipped live-recommended probe-knob values flow through (S-10).
        assert settings.verify_probe_attempts == 6
        assert settings.investigate_reprobe_attempts == 1

    def test_every_settings_field_documented_in_env_example(self) -> None:
        # Every Settings field must at least appear in the template
        # (commented-out counts) so the next live-only knob cannot be
        # forgotten the way VERIFY_PROBE_ATTEMPTS was (S-10).
        text = (_REPO_ROOT / ".env.example").read_text(encoding="utf-8")
        for name in Settings.model_fields:
            assert name.upper() in text, f"{name.upper()} missing from .env.example"


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
