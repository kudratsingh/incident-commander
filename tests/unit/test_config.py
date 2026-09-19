from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from incident_commander.config import (
    ChaosTokenNotConfigured,
    ModelRole,
    Settings,
    get_settings,
    settings_env_var_names,
)
from incident_commander.llm.pricing import MODEL_PRICING

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Every env var Settings can read, walked from the model itself: env vars outrank
# dotenv, so a leak from the developer's shell would override the file under test.
_ENV_VARS = settings_env_var_names()

# The same surface written down once, as a tripwire: a new Settings field fails
# TestEnvIsolation until it is added here, which prompts the .env.example question.
_DOCUMENTED_ENV_VARS = frozenset(
    {
        "ACTION_TOOL_TIMEOUT_SECONDS",
        "AGENT_ENABLED",
        "AGENT_MAX_CONCURRENT_RUNS",
        "AGENT_MODEL",
        "ANTHROPIC_API_KEY",
        "BENCHMARK_MODEL",
        "BUDGET_MAX_SECONDS",
        "BUDGET_MAX_TOKENS",
        "BUDGET_MAX_TOOL_CALLS",
        "BUDGET_MAX_USD",
        "DATABASE_URL",
        "DB_INGEST_RESERVED_CONNECTIONS",
        "DB_MAX_OVERFLOW",
        "DB_POOL_SIZE",
        "DB_POOL_TIMEOUT_SECONDS",
        "DEVELOPMENT_MODEL",
        "HEALTH_PROBE_TIMEOUT_SECONDS",
        "INFERENCE_STRATEGY",
        "BEST_OF_N",
        "SAMPLE_TEMPERATURE",
        "SELECTOR_GENERATOR",
        "INVESTIGATE_REPROBE_ATTEMPTS",
        "INVESTIGATE_REPROBE_DELAY_SECONDS",
        "JUDGE_MODEL",
        "MAX_ITERATIONS_OVERRIDE",
        "PLATFORM_AGENT_PRINCIPAL_ID",
        "PLATFORM_CHAOS_TOKEN",
        "PLATFORM_MCP_URL",
        "PLATFORM_REST_URL",
        "PLATFORM_SMOKE_PRINCIPAL_ID",
        "PLATFORM_SMOKE_TOKEN",
        "PLATFORM_TOKEN",
        "PLATFORM_WEBHOOK_SECRET",
        "TOKEN_BUDGET_MULTIPLIER",
        "USD_BUDGET_MULTIPLIER",
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

    ``_ENV_VARS`` was hand-kept and had drifted — the principal ids and the ADR-0022 pool
    group were missing, so an exported one ran against a real environment.
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
        # The tripwire that a NEW setting was noticed: adding a field to Settings fails here
        # until its variable is added below.
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
        # BUDGET_MAX_USD=0 was the one dimension that accepted zero, and is_exhausted
        # compares with >=, so a run was born exhausted.
        valid_kwargs["budget_max_usd"] = zero
        with pytest.raises(ValidationError):
            _settings(**valid_kwargs)

    def test_a_sub_dollar_budget_is_still_allowed(self, valid_kwargs: dict[str, Any]) -> None:
        # The bound is gt=0, not ge=1: fifty cents is a legitimate operator choice.
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
        # C-09: judge_model="" used to fail only as an API 400 mid-run; min_length=1 guards it.
        valid_kwargs["judge_model"] = ""
        with pytest.raises(ValidationError):
            _settings(**valid_kwargs)

    def test_unpriced_judge_model_refused_at_startup(self, valid_kwargs: dict[str, Any]) -> None:
        # WO-R2-118: an unpriced id billed at the per-class maximum with nothing to say why.
        valid_kwargs["judge_model"] = "claude-not-a-real-model"
        with pytest.raises(ValidationError) as err:
            _settings(**valid_kwargs)
        message = str(err.value)
        assert "JUDGE_MODEL" in message
        assert "claude-not-a-real-model" in message
        # The refusal names the remedy and the ids that would work.
        assert "MODEL_PRICING" in message
        assert "claude-haiku-4-5" in message

    def test_unpriced_agent_model_refused_at_startup(self, valid_kwargs: dict[str, Any]) -> None:
        valid_kwargs["agent_model"] = "claude-not-a-real-model"
        with pytest.raises(ValidationError) as err:
            _settings(**valid_kwargs)
        assert "AGENT_MODEL" in str(err.value)

    def test_both_unpriced_models_named_in_one_refusal(self, valid_kwargs: dict[str, Any]) -> None:
        # One boot, one list: fixing them one restart at a time is the shape
        # of refusal that wastes an operator's afternoon.
        valid_kwargs["agent_model"] = "bad-agent"
        valid_kwargs["judge_model"] = "bad-judge"
        with pytest.raises(ValidationError) as err:
            _settings(**valid_kwargs)
        message = str(err.value)
        assert "bad-agent" in message
        assert "bad-judge" in message

    def test_priced_models_accepted(self, valid_kwargs: dict[str, Any]) -> None:
        valid_kwargs["agent_model"] = "claude-sonnet-4-6"
        valid_kwargs["judge_model"] = "claude-haiku-4-5"
        settings = _settings(**valid_kwargs)
        assert settings.agent_model == "claude-sonnet-4-6"

    def test_the_default_agent_model_is_priced(self, valid_kwargs: dict[str, Any]) -> None:
        # The default must not be the thing that trips the validator.
        valid_kwargs.pop("agent_model", None)
        assert _settings(**valid_kwargs).agent_model in MODEL_PRICING


class TestModelRoles:
    """The two model roles (WP-0.3, plan 02 section 9).

    A role points at a model id, which makes the price guard load-bearing on both: an
    unpriced ``BENCHMARK_MODEL`` bills at the per-class ceiling with one log line.
    """

    def test_unpriced_development_model_refused_at_startup(
        self, valid_kwargs: dict[str, Any]
    ) -> None:
        # Red before the validator tuple was extended: the unpriced id reached the meter (D5).
        valid_kwargs["development_model"] = "not-a-priced-model"
        with pytest.raises(ValidationError) as err:
            _settings(**valid_kwargs)
        message = str(err.value)
        assert "DEVELOPMENT_MODEL" in message
        assert "not-a-priced-model" in message
        assert "MODEL_PRICING" in message

    def test_unpriced_benchmark_model_refused_at_startup(
        self, valid_kwargs: dict[str, Any]
    ) -> None:
        valid_kwargs["benchmark_model"] = "not-a-priced-model"
        with pytest.raises(ValidationError) as err:
            _settings(**valid_kwargs)
        message = str(err.value)
        assert "BENCHMARK_MODEL" in message
        assert "MODEL_PRICING" in message

    def test_all_four_unpriced_models_named_in_one_refusal(
        self, valid_kwargs: dict[str, Any]
    ) -> None:
        # One boot, one list — now over four settings, not two.
        valid_kwargs.update(
            agent_model="bad-agent",
            judge_model="bad-judge",
            development_model="bad-development",
            benchmark_model="bad-benchmark",
        )
        with pytest.raises(ValidationError) as err:
            _settings(**valid_kwargs)
        message = str(err.value)
        for bad in ("bad-agent", "bad-judge", "bad-development", "bad-benchmark"):
            assert bad in message

    def test_both_role_defaults_are_priced(self, valid_kwargs: dict[str, Any]) -> None:
        # Same guarantee as the agent-model default: a fresh checkout boots.
        settings = _settings(**valid_kwargs)
        assert settings.development_model in MODEL_PRICING
        assert settings.benchmark_model in MODEL_PRICING

    def test_each_role_resolves_its_own_setting(self, valid_kwargs: dict[str, Any]) -> None:
        valid_kwargs.update(
            development_model="claude-haiku-4-5", benchmark_model="claude-sonnet-4-6"
        )
        settings = _settings(**valid_kwargs)
        assert settings.model_for_role(ModelRole.DEVELOPMENT) == "claude-haiku-4-5"
        assert settings.model_for_role(ModelRole.BENCHMARK) == "claude-sonnet-4-6"

    def test_every_role_has_a_resolvable_model(self, valid_kwargs: dict[str, Any]) -> None:
        # Total over the enum: a role added without a setting to resolve it
        # would be a run that cannot say which model it billed.
        settings = _settings(**valid_kwargs)
        for role in ModelRole:
            assert settings.model_for_role(role) in MODEL_PRICING

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
        # The C-09 repro inverted: setenv stands in for filling the secrets.
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
        # Every Settings field must appear in the template, as VERIFY_PROBE_ATTEMPTS did not.
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


class TestTheChaosPrincipalIsSeparateAndRequiredAtUse:
    """`PLATFORM_CHAOS_TOKEN`: optional to load, mandatory at the point of use.

    v0.6.5 split one four-scope principal into two (O-4), because the platform withholds
    `chaos.%` audit rows from principals without it. An absent value never
    blocks the offline world, and seeding never degrades to the agent's token.
    """

    def test_it_is_optional_at_load(self, valid_kwargs: dict[str, Any]) -> None:
        # The agent process, the API and the whole canned suite have no chaos
        # principal and must still construct.
        settings = _settings(**valid_kwargs)
        assert settings.platform_chaos_token is None

    def test_it_is_a_secret_and_never_reprs(self, valid_kwargs: dict[str, Any]) -> None:
        settings = _settings(**valid_kwargs, platform_chaos_token="sa_chaos")
        assert isinstance(settings.platform_chaos_token, SecretStr)
        assert settings.require_chaos_token() == "sa_chaos"
        assert "sa_chaos" not in repr(settings)

    @pytest.mark.parametrize("unset", [None, "", "   "])
    def test_unset_or_blank_refuses_rather_than_falling_back(
        self, valid_kwargs: dict[str, Any], unset: str | None
    ) -> None:
        # Blank is UNSET: platform_token cannot seed anything since v0.6.5.
        overrides = {} if unset is None else {"platform_chaos_token": unset}
        settings = _settings(**valid_kwargs, **overrides)
        with pytest.raises(ChaosTokenNotConfigured) as exc:
            settings.require_chaos_token()
        message = str(exc.value)
        assert "PLATFORM_CHAOS_TOKEN" in message
        assert "make bootstrap-token" in message
        assert settings.platform_token.get_secret_value() not in message

    def test_the_refusal_is_one_line(self, valid_kwargs: dict[str, Any]) -> None:
        # Read at the moment a run has just refused to start: the operator
        # needs the variable and the command, on one line, not a traceback.
        settings = _settings(**valid_kwargs)
        with pytest.raises(ChaosTokenNotConfigured) as exc:
            settings.require_chaos_token()
        assert "\n" not in str(exc.value)
