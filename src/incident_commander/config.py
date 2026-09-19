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

# Peak connections one run holds AT ONCE (ADR 0022): the lease connection pinned
# for the whole run (ADR 0016) plus one transient checkout for a checkpoint.
# Raising either makes the ceiling below unsafe, so the two move together.
_CONNECTIONS_PER_RUN: Final[int] = 2

# Staleness bound on platform metrics: ``get_consumer_lag`` reads a 60s cache
# (ADR 0006), so a window that must observe a change has to outlast this.
PLATFORM_METRICS_INTERVAL_SECONDS: Final[float] = 60.0

# Tier-1 attempts one incident may make: the initial action plus one retry with
# reinvestigation (ADR 0056, which supersedes ADR 0008's single attempt). The ONE
# place the number lives — ``MAX_REMEDIATION_ATTEMPTS`` defaults to it and the
# PLANNING and VERIFYING transitions take it as their own default, so a run
# nobody configured and a run reading Settings agree.
DEFAULT_MAX_REMEDIATION_ATTEMPTS: Final[int] = 2

# Ceiling on that knob. Not a safety limit in itself — the scenarios that would
# justify a third attempt do not exist yet, and raising it without them would
# buy autonomy no eval measures (ADR 0056 § consequences).
_MAX_REMEDIATION_ATTEMPTS_CEILING: Final[int] = 3


# The id every model ROLE resolves to unless an operator overrides it — one
# constant so the pin cannot drift. A change needs docs.claude.com and a
# ``MODEL_PRICING`` row, or the validator below refuses startup.
_DEFAULT_MODEL_ID: Final[str] = "claude-sonnet-4-6"


class ModelRole(StrEnum):
    """Which model role a run is made under (plan 02 § 9): DEVELOPMENT or BENCHMARK.

    A run resolves ``AGENT_MODEL`` from exactly one, and the role travels with the
    run's provenance. ``DEVELOPMENT`` is the default and cannot close a phase
    (03 § 14), so the role that claims less is the one you get without asking.
    """

    DEVELOPMENT = "development"
    BENCHMARK = "benchmark"


def polling_window_seconds(attempts: int, delay_seconds: float) -> float:
    """Wall-clock span of a bounded polling loop: ``(attempts - 1) * delay_seconds``.

    The delay falls BETWEEN attempts, so ``attempts * delay`` overstates the wait by
    one delay and green-lights a window that does not outlast the staleness
    (WO-R2-88). A single attempt is not polling and returns 0.0.
    """
    return max(attempts - 1, 0) * delay_seconds


class ChaosTokenNotConfigured(RuntimeError):
    """A seed/reset/chaos path was reached with ``PLATFORM_CHAOS_TOKEN`` unset.

    Its own type so callers refuse before touching the world and the runner's
    crash rail can bucket it.
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
    # The two model ROLES (plan 02 § 9, WP-0.3): which id each role resolves
    # ``AGENT_MODEL`` to, so a reported number can name the role behind it.
    # Pointing either elsewhere needs that id's row in MODEL_PRICING.
    development_model: str = _DEFAULT_MODEL_ID
    benchmark_model: str = _DEFAULT_MODEL_ID
    # Which inference strategy the investigation loop's planner call runs
    # (plan 02 § 4, WP-0.2). Env var INFERENCE_STRATEGY. Typed as the enum so
    # pydantic refuses a typo rather than silently reporting a baseline number;
    # the default is the control group, pinned by tests/unit/test_strategies.py.
    inference_strategy: StrategyName = StrategyName.BASELINE
    # How many candidate diagnoses a best-of-N strategy generates per planner
    # step (plan 02 § 11, WP-5.2). Env var BEST_OF_N. Default 1 is the control
    # group's shape; le=8 is a spend guard, and raising it means re-pricing the
    # cost table (03 § 11, decision C12). Unread by ``baseline``.
    best_of_n: int = Field(default=1, ge=1, le=8)
    # Sampling temperature for ``best_of_n_sampled``'s N planner calls (plan 02
    # § 11.2, WP-5.3). Env var SAMPLE_TEMPERATURE. Sent only when a caller
    # passes one, so no other arm's request bytes move; 1.0 because at 0.0 a
    # sampled arm is N identical calls. See llm/client.SAMPLING_REJECTED_MODELS.
    sample_temperature: float = Field(default=1.0, ge=0.0, le=1.0)
    # Which generator supplies the set a ``candidate_selector`` arm decides over
    # (plan 02 § 12, WP-6.2). Env var SELECTOR_GENERATOR. Typed as the enum;
    # ``baseline`` is refused because one diagnosis is not a set to select from.
    # Default is the enumerated arm, the cheaper generator. Unread by other arms.
    selector_generator: StrategyName = StrategyName.BEST_OF_N_ENUMERATED
    # How far and how wide a `search` walk goes (plan 02 § 14, WP-12.1). Env vars
    # SEARCH_DEPTH and SEARCH_BRANCH. Both are requests BELOW a structural maximum held
    # in agent/search.py (depth 2, branch 3, ADR 0060): the `le=` bounds here refuse a
    # typo early, and the strategy refuses again at construction, so a walk is never
    # quietly clamped to less than the number a report row would print. The experiment
    # matrix varies branch 2 vs 3 (plan 03 § 8), which is what these are for.
    #
    # There is deliberately NO budget knob for search. Its token and tool-call ceilings
    # are the run's own, shared by every branch and the chosen path — tool calls are never
    # multiplied (plan 02 § 8), and that shared ceiling IS the experiment: a branch that
    # could mint its own budget would make exploring free.
    search_depth: int = Field(default=2, ge=1, le=2)
    search_branch: int = Field(default=3, ge=1, le=3)
    # `reflection` (WP-9.1) has NO field here on purpose: its one revision pass per
    # step is the safety property, held by a token in agent/reflection.py (ADR 0055),
    # and a bound an operator could raise would not be one. The arm stamps `passes`
    # and `cap` into `strategy_config` instead, so a report row carries what it ran
    # under. tests/unit/test_reflection.py refuses a knob with that name here.

    # --- The selected strategy's budget policy (plan 02 § 8, WP-2.4) -------
    # A strategy that samples N candidates spends ~N× the control group's tokens
    # and dollars; metered against the fleet ceilings it would escalate mid-run
    # and the report would read that as the STRATEGY failing. So a strategy
    # declares what it may spend relative to the control group, applied once
    # where ``agent/factory.py::start_run`` seeds the ledger.
    #
    # Deliberately NOT scaled: tool calls (what the strategies compete on, and
    # the scenario's grading cap, ADR 0019), wall seconds (raise
    # BUDGET_MAX_SECONDS instead), and the control group (both default to 1 —
    # tests/unit/test_budgets.py::TestBaselineIsBitForBitUnchanged). A ratio,
    # so it holds whichever ceilings the invocation was handed (divergence D7).
    token_budget_multiplier: Decimal = Field(default=Decimal("1"), ge=Decimal("0"))
    usd_budget_multiplier: Decimal = Field(default=Decimal("1"), ge=Decimal("0"))
    # Investigation-loop iterations the strategy may take, overriding
    # ``agent/investigation.py``'s default of 5. Unset means that default.
    # ``ge=1``: a loop allowed none escalates having investigated nothing.
    max_iterations_override: int | None = Field(default=None, ge=1)

    # --- The adaptive ladder's escalation thresholds (plan 02 § 15, WP-13.1) -----
    # One override per threshold from plan 02 § 15's signal list, all UNSET here on purpose:
    # each DEFAULT — and the benchmark split that default was set on — is declared once, in
    # ``agent/strategies/policy.py``'s table, which refuses a default derived from the holdout
    # (ADR 0061). A number here would be a second declaration with no split behind it, so
    # ``None`` means "the declared default" and an operator's value is reported as having no
    # split at all. Unread by every arm shipped today; ``adaptive`` (WP-13.2) is the reader.
    #
    # The 0.7 remediate bar is deliberately NOT in this block and gets no knob: it is a
    # reported operating point re-examined per model in the phase-close protocol (plan
    # 02 § 16), and a knob would move every arm's numbers at once from an environment.
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

    # Required, no default (pinned for eval stability). min_length guards direct
    # construction, which env_ignore_empty cannot reach.
    judge_model: str = Field(min_length=1)

    # Platform. MCP and REST are separate URLs per platform ADR-0006.
    platform_mcp_url: AnyHttpUrl
    platform_rest_url: AnyHttpUrl
    # The AGENT's principal: telemetry:read + incidents:read + actions:execute,
    # NOT chaos:invoke — that scope would let it read which fault caused its own
    # alert (platform ADR 0012, O-4). Minted as `PLATFORM_TOKEN` by
    # `make bootstrap-token`.
    platform_token: SecretStr
    # Read-scoped twin for `make eval-smoke`. Selected by the runner's --smoke
    # flag, not shell plumbing: `-include .env` silently defeated that, so every
    # "read-scoped" run up to 2026-08-07 held write scope (PR #62 vs #69).
    platform_smoke_token: SecretStr | None = None
    # The EVALUATOR's principal (chaos:invoke), minted as `PLATFORM_CHAOS_TOKEN`.
    # Every path that seeds, verifies or tears down a fault world runs under it.
    # Optional at load, required at point of use: most paths need no chaos
    # principal, and ``require_chaos_token`` below refuses rather than falling
    # back to ``platform_token``, which since v0.6.5 cannot seed (the S-04 shape).
    platform_chaos_token: SecretStr | None = None
    # Principal ids of the two service accounts, printed by `make bootstrap-token`.
    # Not secrets. They scope the post-stage audit guard to self-owned principals
    # (A-13). BOTH must be set: F-001 writes its rows under the AGENT principal,
    # so filtering to the smoke id alone would blind the guard to its own reason
    # for existing. Unset means unfiltered.
    platform_agent_principal_id: str | None = None
    platform_smoke_principal_id: str | None = None
    platform_webhook_secret: SecretStr
    # Webhook replay guard (ADR 0014): reject deliveries whose X-Alert-Timestamp
    # deviates from local time by more than this, and bound the window in which
    # an identical redelivery is suppressed.
    webhook_max_skew_seconds: int = Field(default=300, ge=1)
    # Largest alert body ingress will read (WO-R2-86). The HMAC covers the body,
    # so an unauthenticated caller would otherwise decide this process's memory.
    webhook_max_body_bytes: int = Field(default=1_048_576, ge=1)
    # How long /health waits for its datastore probe. Far below
    # DB_POOL_TIMEOUT_SECONDS: a check that waits as long as the fault it reports
    # has itself stopped answering.
    health_probe_timeout_seconds: float = Field(default=2.0, gt=0)

    # Agent-owned Postgres.
    database_url: PostgresDsn

    # --- Connection pool and run admission (ADR 0022) ---------------------
    # SQLAlchemy's default pool is unsafe here: a run PINS one connection for its
    # whole life (ADR 0016) and asks the same pool for a second on every
    # checkpoint write, so enough concurrent runs deadlock on hold-and-wait. Size
    # the pool explicitly and bound live runs to what it can serve.
    db_pool_size: int = Field(default=10, ge=1)
    db_max_overflow: int = Field(default=10, ge=0)
    # Far below SQLAlchemy's 30s default: this wait is spent in a threadpool
    # worker on the ingress path, unable to investigate or refuse the alert.
    db_pool_timeout_seconds: float = Field(default=10.0, gt=0)
    # Held back from the run bound for non-run work: ingress identity derivation,
    # its checkpoint write, and the crash rail. Short-lived, so they cause
    # contention, never deadlock — headroom is the right instrument.
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

    # Kill switch, AGENT_ENABLED (docs/safety-model.md, docs/runbook.md): false
    # keeps ingress recording alerts but spawns no runs. Read once at process
    # start, so changing it needs a restart.
    agent_enabled: bool = True

    # Live-eval verification polling. attempts=1 is the legacy single probe;
    # live runs set VERIFY_PROBE_ATTEMPTS>1 to re-read a cached metric (the
    # 60s consumer lag) over a window instead of ~1s after the action.
    verify_probe_attempts: int = Field(default=1, ge=1, le=10)
    verify_probe_delay_seconds: float = Field(default=15.0, ge=0.0)

    # Investigation-side twin of the verify window (ADR 0009): re-read a
    # declared-cached tool before accepting that it killed a fixable hypothesis.
    # Default 0 keeps canned runs byte-identical; the runner wires it for live.
    investigate_reprobe_attempts: int = Field(default=0, ge=0, le=3)
    investigate_reprobe_delay_seconds: float = Field(default=20.0, ge=0.0)

    # Tier-1 attempts one incident may make (ADR 0056, supersedes ADR 0008). The
    # transitions take their default from the constant above this class, so this
    # field is the only place the number is decided.
    max_remediation_attempts: int = Field(
        default=DEFAULT_MAX_REMEDIATION_ATTEMPTS, ge=1, le=_MAX_REMEDIATION_ATTEMPTS_CEILING
    )

    # Tier-1 action tools do real work and can outlast a read, so they get their
    # own knob rather than escalating a slow success as a transport error.
    action_tool_timeout_seconds: float = Field(default=60.0, ge=1.0)

    @property
    def verify_polling_window_seconds(self) -> float:
        """Wall-clock span of the ADR 0006 verify window: 0.0 at the defaults,
        100.0 at the live knobs. ``test_polling_window.py`` pins both.
        """
        return polling_window_seconds(self.verify_probe_attempts, self.verify_probe_delay_seconds)

    @property
    def investigation_reprobe_window_seconds(self) -> float:
        """Wall-clock the ADR 0009 re-probes add: 0.0 at the defaults, 75.0 live.

        ``attempts * delay``, NOT ``polling_window_seconds``: a verify attempt is a probe
        with the delay between attempts, while a re-probe is an EXTRA probe each preceded
        by its own sleep (``investigation.py``'s ``sleep`` then probe). One attempt there
        costs a whole delay; one verify attempt costs none. WP-14.1's TTL derivation is
        the first reader, and reading it off the wrong formula would put a temporal
        fault's expiry a full delay from where the template meant it.
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

    def model_for_role(self, role: ModelRole) -> str:
        """The model id a run of ``role`` bills (plan 02 § 9).

        The one place the mapping lives; the run stamps role and id into its
        provenance.
        """
        return {
            ModelRole.DEVELOPMENT: self.development_model,
            ModelRole.BENCHMARK: self.benchmark_model,
        }[role]

    @model_validator(mode="after")
    def _configured_models_are_priced(self) -> Settings:
        """Refuse at startup any model id with no row in ``MODEL_PRICING``.

        Otherwise ``pricing_for`` falls back to ``class_ceiling`` and the model
        bills at the dearest rate on record with one ``WARNING``. That fallback is
        right mid-run (ADR 0015); at construction, demanding a price is honest.
        """
        unpriced = {
            name.upper(): value
            for name, value in (
                ("agent_model", self.agent_model),
                ("judge_model", self.judge_model),
                # Checked here too: ``--model-role benchmark`` resolves
                # BENCHMARK_MODEL on the paid path, where an unpriced id would
                # be found mid-run (the hole ADR 0015 closes).
                ("development_model", self.development_model),
                ("benchmark_model", self.benchmark_model),
            )
            if value not in MODEL_PRICING
        }
        if unpriced:
            # Both in one message: fixing them one restart at a time is the
            # shape of refusal that wastes an operator's afternoon.
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

        ``BudgetLedger.is_exhausted`` is ``used >= max``, so a zero ledger is born
        exhausted — the run escalates before TRIAGE, reading like a working ceiling.
        A multiplier reaches zero directly or by rounding.
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

    Derived from the model, not written down: three hand-kept copies had each
    drifted, so the vars they missed were inherited from the shell. Names are
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
