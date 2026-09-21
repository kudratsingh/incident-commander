"""Runtime configuration loaded from environment (see .env.example)."""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from functools import lru_cache
from typing import Final

from pydantic import AnyHttpUrl, Field, PostgresDsn, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from incident_commander.agent.strategies.names import StrategyName
from incident_commander.llm.pricing import MODEL_PRICING

# Peak connections one run holds AT ONCE (ADR 0022): the lease connection pinned for the
# whole run (ADR 0016) plus one transient checkout for a checkpoint.
_CONNECTIONS_PER_RUN: Final[int] = 2

# Staleness bound on platform metrics: ``get_consumer_lag`` reads a 60s cache
# (ADR 0006), so a window that must observe a change has to outlast this.
PLATFORM_METRICS_INTERVAL_SECONDS: Final[float] = 60.0

# Tier-1 attempts one incident may make: the action plus one retry with reinvestigation
# (ADR 0056, superseding ADR 0008). The ONE place the number lives.
DEFAULT_MAX_REMEDIATION_ATTEMPTS: Final[int] = 2

# Ceiling on that knob: raising it would buy autonomy no eval measures, because the
# scenarios that justify a third attempt do not exist yet (ADR 0056).
_MAX_REMEDIATION_ATTEMPTS_CEILING: Final[int] = 3


# The id every model ROLE resolves to unless overridden — one constant so the pin cannot
# drift. A change needs docs.claude.com and a ``MODEL_PRICING`` row, or startup refuses.
_DEFAULT_MODEL_ID: Final[str] = "claude-sonnet-4-6"


class ModelRole(StrEnum):
    """Which model role a run is made under (plan 02 § 9): DEVELOPMENT or BENCHMARK.

    A run resolves ``AGENT_MODEL`` from exactly one, and the role travels with the run's
    provenance. ``DEVELOPMENT`` is the default and cannot close a phase (03 § 14).
    """

    DEVELOPMENT = "development"
    BENCHMARK = "benchmark"


def polling_window_seconds(attempts: int, delay_seconds: float) -> float:
    """Wall-clock span of a bounded polling loop: ``(attempts - 1) * delay_seconds``.

    The delay falls BETWEEN attempts, so ``attempts * delay`` overstates the wait by one
    delay (WO-R2-88). A single attempt is not polling and returns 0.0.
    """
    return max(attempts - 1, 0) * delay_seconds


class ChaosTokenNotConfigured(RuntimeError):
    """A seed/reset/chaos path was reached with ``PLATFORM_CHAOS_TOKEN`` unset.

    Its own type so callers refuse before touching the world and the runner's
    crash rail can bucket it.
    """


class SmokeTokenNotConfigured(RuntimeError):
    """A read-only observation path was reached with ``PLATFORM_SMOKE_TOKEN`` unset.

    Its own type: the demo runner's every read is made under this principal (ADR 0074, F3),
    because a fallback to the agent's token would log the runner's reads as the AGENT's.
    """


class Settings(BaseSettings):
    """Immutable application settings. Constructed once at startup."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
        # A blank entry (VAR=) means "unset", not "": optional fields fall back
        # to their defaults and required ones raise "Field required" (C-09).
        env_ignore_empty=True,
    )

    # Anthropic
    anthropic_api_key: SecretStr

    # Models. Verify strings against docs.claude.com before changing defaults.
    agent_model: str = _DEFAULT_MODEL_ID
    # The two model ROLES (plan 02 § 9, WP-0.3): which id each resolves ``AGENT_MODEL`` to.
    # Pointing either elsewhere needs that id's row in MODEL_PRICING.
    development_model: str = _DEFAULT_MODEL_ID
    benchmark_model: str = _DEFAULT_MODEL_ID
    # Which inference strategy the planner call runs (plan 02 § 4). Env INFERENCE_STRATEGY,
    # typed as the enum so a typo is refused rather than reported as a baseline number.
    inference_strategy: StrategyName = StrategyName.BASELINE
    # Candidate diagnoses a best-of-N strategy generates per planner step (WP-5.2). Env
    # BEST_OF_N; default 1 is the control group's shape and le=8 is a spend guard.
    best_of_n: int = Field(default=1, ge=1, le=8)
    # Sampling temperature for ``best_of_n_sampled``'s N planner calls (WP-5.3). Env
    # SAMPLE_TEMPERATURE; at 0.0 a sampled arm is N identical calls (SAMPLING_REJECTED_MODELS).
    sample_temperature: float = Field(default=1.0, ge=0.0, le=1.0)
    # Which generator supplies the set a ``candidate_selector`` arm decides over (WP-6.2).
    # Env SELECTOR_GENERATOR; ``baseline`` is refused — one diagnosis is not a set.
    selector_generator: StrategyName = StrategyName.BEST_OF_N_ENUMERATED
    # How far and how wide a `search` walk goes (WP-12.1). Env SEARCH_DEPTH and SEARCH_BRANCH,
    # both requests BELOW the structural maximum in agent/search.py (depth 2, branch 3,
    # ADR 0060). No budget knob for search: a branch that could mint one explores for free.
    search_depth: int = Field(default=2, ge=1, le=2)
    search_branch: int = Field(default=3, ge=1, le=3)
    # `reflection` (WP-9.1) has NO field here on purpose: its one revision pass per step is
    # the safety property (ADR 0055), and test_reflection.py refuses a knob with that name.

    # --- The selected strategy's budget policy (plan 02 § 8, WP-2.4) -------
    # A strategy that samples N candidates spends ~N× the control group's tokens, and against
    # the fleet ceilings it would escalate mid-run and read as the STRATEGY failing. A RATIO,
    # applied where ``agent/factory.py::start_run`` seeds the ledger. Tool calls are NOT scaled.
    token_budget_multiplier: Decimal = Field(default=Decimal("1"), ge=Decimal("0"))
    usd_budget_multiplier: Decimal = Field(default=Decimal("1"), ge=Decimal("0"))
    # Investigation-loop iterations the strategy may take, overriding
    # ``agent/investigation.py``'s default of 5. Unset means that default.
    # ``ge=1``: a loop allowed none escalates having investigated nothing.
    max_iterations_override: int | None = Field(default=None, ge=1)

    # --- The adaptive ladder's escalation thresholds (plan 02 § 15, WP-13.1) -----
    # All UNSET on purpose: each DEFAULT, and the benchmark split behind it, is declared once
    # in ``agent/strategies/policy.py``'s table (ADR 0061), so ``None`` means "the declared
    # default". The 0.7 remediate bar gets no knob at all — it is a reported operating point.
    uncertainty_top1_confidence_floor: float | None = Field(default=None, ge=0.0, le=1.0)
    uncertainty_top1_top2_margin_floor: float | None = Field(default=None, ge=0.0, le=1.0)
    uncertainty_selector_uncertainty_ceiling: float | None = Field(default=None, ge=0.0, le=1.0)
    uncertainty_candidate_disagreement_ceiling: float | None = Field(default=None, ge=0.0, le=1.0)
    uncertainty_confidence_floor_after_probes: float | None = Field(default=None, ge=0.0, le=1.0)
    # The three that count rather than score. ``ge=1``: a count of zero fires on every step
    # (the comparison is "at or above"), which is not a threshold but an always-escalate.
    uncertainty_contradictory_evidence_count: int | None = Field(default=None, ge=1)
    uncertainty_failed_attempt_count: int | None = Field(default=None, ge=1)
    uncertainty_probe_count_before_confidence_check: int | None = Field(default=None, ge=1)

    # Required, no default (pinned for eval stability); min_length guards direct construction.
    judge_model: str = Field(min_length=1)

    # Platform. MCP and REST are separate URLs per platform ADR-0006.
    platform_mcp_url: AnyHttpUrl
    platform_rest_url: AnyHttpUrl
    # The AGENT's principal: telemetry:read + incidents:read + actions:execute, NOT
    # chaos:invoke — that would let it read which fault caused its own alert (platform ADR 0012).
    platform_token: SecretStr
    # Read-scoped twin for `make eval-smoke`, selected by the runner's --smoke flag rather than
    # shell plumbing: `-include .env` silently defeated that and every run held write scope.
    platform_smoke_token: SecretStr | None = None
    # The EVALUATOR's principal (`chaos:invoke`): every path that seeds, verifies or tears
    # down a fault world. Optional at load, required by ``require_chaos_token`` (S-04).
    platform_chaos_token: SecretStr | None = None
    # Principal ids of the two service accounts (not secrets), scoping the post-stage audit
    # guard (A-13). BOTH must be set — F-001's rows are the AGENT's. Unset means unfiltered.
    platform_agent_principal_id: str | None = None
    platform_smoke_principal_id: str | None = None
    platform_webhook_secret: SecretStr
    # Webhook replay guard (ADR 0014): reject a delivery whose X-Alert-Timestamp is further
    # from local time than this, and bound the identical-redelivery suppression window.
    webhook_max_skew_seconds: int = Field(default=300, ge=1)
    # Largest alert body ingress will read (WO-R2-86). The HMAC covers the body,
    # so an unauthenticated caller would otherwise decide this process's memory.
    webhook_max_body_bytes: int = Field(default=1_048_576, ge=1)
    # How long /health waits for its datastore probe. Far below DB_POOL_TIMEOUT_SECONDS: a
    # check that waits as long as the fault it reports has itself stopped answering.
    health_probe_timeout_seconds: float = Field(default=2.0, gt=0)

    # Agent-owned Postgres.
    database_url: PostgresDsn

    # --- Connection pool and run admission (ADR 0022) ---------------------
    # SQLAlchemy's defaults are unsafe here: a run PINS one connection for its whole life
    # (ADR 0016) and asks the same pool for a second per checkpoint — hold-and-wait deadlock.
    db_pool_size: int = Field(default=10, ge=1)
    db_max_overflow: int = Field(default=10, ge=0)
    # Far below SQLAlchemy's 30s default: this wait is spent in a threadpool
    # worker on the ingress path, unable to investigate or refuse the alert.
    db_pool_timeout_seconds: float = Field(default=10.0, gt=0)
    # Held back from the run bound for non-run work: ingress identity derivation, its
    # checkpoint write, and the crash rail. Short-lived, so headroom is the right instrument.
    db_ingest_reserved_connections: int = Field(default=4, ge=0)
    # Optional LOWER bound on concurrent runs; unset means whatever the pool
    # safely serves. The validator refuses a value above that ceiling.
    agent_max_concurrent_runs: int | None = Field(default=None, ge=1)

    # Per-incident hard budgets (CLAUDE.md invariant 7).
    budget_max_tool_calls: int = Field(default=25, ge=1)
    budget_max_tokens: int = Field(default=500_000, ge=1)
    budget_max_seconds: int = Field(default=1_800, ge=1)
    # gt=0, not ge=1: a fifty-cent cap is a choice, zero is not — ``is_exhausted``
    # compares with ``>=``, so BUDGET_MAX_USD=0 made every run born exhausted.
    budget_max_usd: Decimal = Field(default=Decimal("5.00"), gt=Decimal("0"))

    # Kill switch, AGENT_ENABLED (docs/safety-model.md): false keeps ingress recording alerts
    # but spawns no runs. Read once at process start, so a change needs a restart.
    agent_enabled: bool = True

    # Live-eval verification polling: attempts=1 is a single probe, and live runs set
    # VERIFY_PROBE_ATTEMPTS>1 to re-read a cached metric over a window.
    verify_probe_attempts: int = Field(default=1, ge=1, le=10)
    verify_probe_delay_seconds: float = Field(default=15.0, ge=0.0)

    # Investigation-side twin of the verify window (ADR 0009): re-read a declared-cached tool
    # before accepting that it killed a fixable hypothesis. Default 0 keeps canned runs identical.
    investigate_reprobe_attempts: int = Field(default=0, ge=0, le=3)
    investigate_reprobe_delay_seconds: float = Field(default=20.0, ge=0.0)

    # Tier-1 attempts one incident may make (ADR 0056); the only place the number is decided.
    max_remediation_attempts: int = Field(
        default=DEFAULT_MAX_REMEDIATION_ATTEMPTS, ge=1, le=_MAX_REMEDIATION_ATTEMPTS_CEILING
    )

    # Tier-1 action tools do real work and can outlast a read, so they get their
    # own knob rather than escalating a slow success as a transport error.
    action_tool_timeout_seconds: float = Field(default=60.0, ge=1.0)

    # AGENT_RUN_REPORTING (ADR 0068): report each transition so an operator console can follow
    # the run. Default FALSE is the decision — a graded eval must not make extra platform
    # calls; `make demo-live` is the only caller that turns it on. Fail-open either way.
    agent_run_reporting: bool = False

    @property
    def verify_polling_window_seconds(self) -> float:
        """Wall-clock span of the ADR 0006 verify window: 0.0 at the defaults,
        100.0 at the live knobs. ``test_polling_window.py`` pins both.
        """
        return polling_window_seconds(self.verify_probe_attempts, self.verify_probe_delay_seconds)

    @property
    def investigation_reprobe_window_seconds(self) -> float:
        """Wall-clock the ADR 0009 re-probes add: 0.0 at the defaults, 75.0 live.

        ``attempts * delay``, NOT ``polling_window_seconds``: a re-probe is an EXTRA probe with
        its own sleep, so one costs a whole delay where a verify attempt costs none (WP-14.1).
        """
        return self.investigate_reprobe_attempts * self.investigate_reprobe_delay_seconds

    @property
    def db_pool_capacity(self) -> int:
        """Total connections the pool will ever hand out at once."""
        return self.db_pool_size + self.db_max_overflow

    @property
    def max_concurrent_runs(self) -> int:
        """Runs that may hold a lease at once, derived from pool capacity.

        ``AGENT_MAX_CONCURRENT_RUNS`` may lower it, never raise it.
        """
        ceiling = (
            self.db_pool_capacity - self.db_ingest_reserved_connections
        ) // _CONNECTIONS_PER_RUN
        if self.agent_max_concurrent_runs is None:
            return ceiling
        return min(self.agent_max_concurrent_runs, ceiling)

    @property
    def seeded_max_tokens(self) -> int:
        """The token ceiling one run is seeded with, multiplier applied.

        Floored, not rounded: rounding up would hide a budget of nothing.
        """
        return int(Decimal(self.budget_max_tokens) * self.token_budget_multiplier)

    @property
    def seeded_max_usd(self) -> Decimal:
        """The dollar ceiling one run is seeded with, multiplier applied.

        Decimal throughout, never a float intermediate (ADR 0015).
        """
        return self.budget_max_usd * self.usd_budget_multiplier

    def require_chaos_token(self) -> str:
        """The evaluator's ``chaos:invoke`` token, or refuse in one line.

        The only reader of ``PLATFORM_CHAOS_TOKEN``; blank or whitespace counts as
        unset. Never falls back to ``platform_token``, which lacks the scope (S-04).
        """
        token = self.platform_chaos_token
        if token is None or not token.get_secret_value().strip():
            raise ChaosTokenNotConfigured(
                "PLATFORM_CHAOS_TOKEN is not set in .env, and seeding, resetting or "
                "verifying a fault world needs the chaos principal (the agent's "
                "PLATFORM_TOKEN no longer carries chaos:invoke): run "
                "`make bootstrap-token` and paste both printed lines."
            )
        return token.get_secret_value()

    def require_smoke_token(self) -> str:
        """The read-only principal's token, or refuse in one line.

        ``require_chaos_token``'s twin: blank counts as unset, and there is no fall back to
        ``platform_token`` — a read under the agent's token is logged as the AGENT's (F3).
        """
        token = self.platform_smoke_token
        if token is None or not token.get_secret_value().strip():
            raise SmokeTokenNotConfigured(
                "PLATFORM_SMOKE_TOKEN is not set in .env, and this path reads the world "
                "under the read-only principal on purpose — it will not fall back to the "
                "agent's PLATFORM_TOKEN, because the agent's own audit rows are what the "
                "demo page is showing. Run `make bootstrap-token` and paste the printed line."
            )
        return token.get_secret_value()

    def model_for_role(self, role: ModelRole) -> str:
        """The model id a run of ``role`` bills (plan 02 § 9).

        The one place the mapping lives; the run stamps role and id into its provenance.
        """
        return {
            ModelRole.DEVELOPMENT: self.development_model,
            ModelRole.BENCHMARK: self.benchmark_model,
        }[role]

    @model_validator(mode="after")
    def _configured_models_are_priced(self) -> Settings:
        """Refuse at startup any model id with no row in ``MODEL_PRICING``.

        Otherwise ``pricing_for`` falls back to ``class_ceiling`` and bills at the dearest
        rate on record. That fallback is right mid-run (ADR 0015), not at construction.
        """
        unpriced = {
            name.upper(): value
            for name, value in (
                ("agent_model", self.agent_model),
                ("judge_model", self.judge_model),
                # Checked here too: ``--model-role benchmark`` resolves BENCHMARK_MODEL on
                # the paid path, where an unpriced id would be found mid-run (ADR 0015).
                ("development_model", self.development_model),
                ("benchmark_model", self.benchmark_model),
            )
            if value not in MODEL_PRICING
        }
        if unpriced:
            # Both in one message: one restart per fix wastes an operator's afternoon.
            named = ", ".join(f"{var}={value!r}" for var, value in sorted(unpriced.items()))
            known = ", ".join(sorted(MODEL_PRICING))
            raise ValueError(
                f"{named} has no row in MODEL_PRICING, so its cost would be billed at the "
                f"per-class maximum of every priced model rather than its own rate. "
                f"Priced ids: {known}. Add the model to MODEL_PRICING "
                "(src/incident_commander/llm/pricing.py), with rates verified against "
                "docs.claude.com, or configure one of the ids above."
            )
        return self

    @model_validator(mode="after")
    def _run_bound_fits_the_pool(self) -> Settings:
        """Refuse at startup any pool that cannot serve a single run.

        The alternative is a 10-second stall under load. Better not to boot.
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

    @model_validator(mode="after")
    def _multipliers_seed_a_budget_that_can_be_spent(self) -> Settings:
        """Refuse a strategy multiplier that seeds a budget of nothing.

        ``BudgetLedger.is_exhausted`` is ``used >= max``, so a zero ledger is born exhausted
        and the run escalates before TRIAGE, reading like a working ceiling.
        """
        degenerate: list[str] = []
        if self.seeded_max_tokens < 1:
            degenerate.append(
                f"TOKEN_BUDGET_MULTIPLIER={self.token_budget_multiplier} applied to "
                f"BUDGET_MAX_TOKENS={self.budget_max_tokens} seeds a token budget of "
                f"{self.seeded_max_tokens}"
            )
        if self.seeded_max_usd <= 0:
            degenerate.append(
                f"USD_BUDGET_MULTIPLIER={self.usd_budget_multiplier} applied to "
                f"BUDGET_MAX_USD={self.budget_max_usd} seeds a dollar budget of "
                f"{self.seeded_max_usd}"
            )
        if degenerate:
            # Both in one message: one restart per fix wastes an afternoon.
            raise ValueError(
                f"{'; '.join(degenerate)}. A ledger seeded at zero is born exhausted "
                "(BudgetLedger.is_exhausted compares used >= max), so the run would "
                "escalate before TRIAGE having investigated nothing — which reads in "
                "the report exactly like a budget ceiling working as intended. Raise "
                "the multiplier, or raise the budget it multiplies."
            )
        return self


def settings_env_var_names() -> tuple[str, ...]:
    """Every environment variable ``Settings`` can read, sorted.

    Derived from the model, never written down: a hand-kept copy drifts. Names are
    upper-cased; ``env_prefix`` and explicit string aliases are honoured.
    """
    prefix = str(Settings.model_config.get("env_prefix", ""))
    names: list[str] = []
    for name, field in Settings.model_fields.items():
        alias = field.validation_alias if isinstance(field.validation_alias, str) else field.alias
        names.append(alias if isinstance(alias, str) else f"{prefix}{name}".upper())
    return tuple(sorted(names))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings for application code. Tests should construct ``Settings`` directly."""
    return Settings()  # type: ignore[call-arg]
