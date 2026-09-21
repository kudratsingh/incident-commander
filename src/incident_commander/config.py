"""Every setting the agent reads from its environment, and the checks that refuse a bad one.

``.env.example`` is the annotated list for an operator; this file is the authority. The validators
at the bottom refuse to start on a configuration that would otherwise fail mid-run: an unpriced
model id, a pool too small for a single run, or a budget multiplier that leaves nothing to spend.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from functools import lru_cache
from typing import Final

from pydantic import AnyHttpUrl, Field, PostgresDsn, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from incident_commander.agent.strategies.names import StrategyName
from incident_commander.llm.pricing import MODEL_PRICING

# The most connections one run needs at the same moment (ADR 0022): the one it keeps for its whole
# life to hold its lease (ADR 0016), plus one it borrows briefly to write a checkpoint.
_CONNECTIONS_PER_RUN: Final[int] = 2

# The oldest a platform metric can be: ``get_consumer_lag`` is served from a cache refreshed about
# this often (ADR 0006), so any wait that has to SEE a change happen must be longer than this.
PLATFORM_METRICS_INTERVAL_SECONDS: Final[float] = 60.0

# How many actions one incident may attempt by default: the first action, plus one retry that had to
# investigate again before earning it (ADR 0056, replacing ADR 0008's single attempt).
DEFAULT_MAX_REMEDIATION_ATTEMPTS: Final[int] = 2

# The highest the setting below may be configured to. Allowing more would let the agent act more
# often than any eval scenario measures, and no scenario needs a third attempt yet (ADR 0056).
_MAX_REMEDIATION_ATTEMPTS_CEILING: Final[int] = 3


# The model every role below falls back to. Held as one constant so the three defaults cannot drift
# apart. Changing it means checking the id against docs.claude.com and adding its rates to
# ``MODEL_PRICING`` first, or the validator at the bottom of this file refuses to start.
_DEFAULT_MODEL_ID: Final[str] = "claude-sonnet-4-6"


class ModelRole(StrEnum):
    """Which of the two model pins a run was made under: the everyday one, or the measured one.

    A run takes its model from exactly one of them and records which, so a number can always be
    traced to the model that produced it. ``DEVELOPMENT`` is the default, and a result measured
    under it may not be used to close a phase — only a ``BENCHMARK`` run counts for that.
    """

    DEVELOPMENT = "development"
    BENCHMARK = "benchmark"


def polling_window_seconds(attempts: int, delay_seconds: float) -> float:
    """How long a polling loop of ``attempts`` probes actually takes: ``(attempts - 1) * delay``.

    The delay sits BETWEEN attempts, so multiplying by the number of attempts overstates the wait by
    one whole delay. One attempt is not polling at all and takes no time, so it returns 0.0.
    """
    return max(attempts - 1, 0) * delay_seconds


class ChaosTokenNotConfigured(RuntimeError):
    """Something tried to seed, verify or clean up a fault world with no evaluator token set.

    Its own type so the caller can refuse before touching anything, and so the eval runner can
    report "you forgot to configure the token" separately from a real failure.
    """


class SmokeTokenNotConfigured(RuntimeError):
    """A read that must be made as the evaluator was reached with no read-only token set.

    Its own type because the demo runner takes every reading under that account on purpose: falling
    back to the agent's token would write the runner's reads into the audit log as the AGENT's, and
    the agent's own rows are exactly what the demo is showing (finding F3).
    """


class Settings(BaseSettings):
    """Immutable application settings. Constructed once at startup."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
        # A blank line in .env (`VAR=`) is read as "not set" rather than as an empty string, so an
        # optional field falls back to its default and a required one says "Field required" instead
        # of accepting emptiness — an empty token used to pass validation and fail at the API.
        env_ignore_empty=True,
    )

    # Anthropic
    anthropic_api_key: SecretStr

    # The model the agent itself runs on. Check any new id against docs.claude.com before setting.
    agent_model: str = _DEFAULT_MODEL_ID
    # The model each of the two roles above uses. Pointing either at a different id needs a price
    # row for it in MODEL_PRICING first, or the validator at the bottom refuses to start.
    development_model: str = _DEFAULT_MODEL_ID
    benchmark_model: str = _DEFAULT_MODEL_ID
    # Which way of making the planner's decision a run uses. Env INFERENCE_STRATEGY, typed as the
    # enum so a misspelled name is refused instead of quietly reported as the baseline's number.
    inference_strategy: StrategyName = StrategyName.BASELINE
    # How many candidate diagnoses the strategies that generate several ask for per step. Env
    # BEST_OF_N. One is the control group's behaviour, and the upper limit of 8 is a spend guard.
    best_of_n: int = Field(default=1, ge=1, le=8)
    # The temperature the sampled strategy's repeated planner calls use. Env SAMPLE_TEMPERATURE. At
    # 0.0 those calls all come back the same, which makes that strategy an expensive copy of the
    # control group; some model ids reject the field entirely, see ``SAMPLING_REJECTED_MODELS``.
    sample_temperature: float = Field(default=1.0, ge=0.0, le=1.0)
    # When a run picks between candidate diagnoses, this says which strategy produces the candidates
    # it picks from. Env SELECTOR_GENERATOR. ``baseline`` is refused: it produces one, not a set.
    selector_generator: StrategyName = StrategyName.BEST_OF_N_ENUMERATED
    # How deep and how wide the `search` strategy may explore. Env SEARCH_DEPTH and SEARCH_BRANCH.
    # Both are requests that cannot exceed the limits built into agent/search.py (ADR 0060), and
    # search gets no budget of its own — a branch allowed to raise its own budget explores for free.
    search_depth: int = Field(default=2, ge=1, le=2)
    search_branch: int = Field(default=3, ge=1, le=3)
    # The `reflection` strategy deliberately has NO setting here: doing exactly one revision pass
    # per step is what makes it safe (ADR 0055), so test_reflection.py fails if one ever appears.

    # --- Budget allowances for a strategy that costs more than the control group -------
    # A strategy that asks the model for several candidates spends several times the tokens, so
    # under the standard ceilings it runs out mid-incident and escalates — which reads as the
    # STRATEGY failing. These MULTIPLY the ceilings in ``start_run``; tool calls are never scaled.
    token_budget_multiplier: Decimal = Field(default=Decimal("1"), ge=Decimal("0"))
    usd_budget_multiplier: Decimal = Field(default=Decimal("1"), ge=Decimal("0"))
    # How many times round the investigation loop a run may go, replacing the default of 5 in
    # ``agent/investigation.py``. Unset means that default. At least 1, because a run allowed zero
    # iterations escalates having investigated nothing, which looks like a failed investigation.
    max_iterations_override: int | None = Field(default=None, ge=1)

    # --- When the adaptive strategy gives up and hands the incident to a human -----
    # All unset on purpose. Each threshold's real default lives in one table in
    # ``agent/strategies/policy.py`` (ADR 0061), so ``None`` here means "use the declared default"
    # and there is one place to read them. The 0.7 bar for acting at all gets no setting at all.
    uncertainty_top1_confidence_floor: float | None = Field(default=None, ge=0.0, le=1.0)
    uncertainty_top1_top2_margin_floor: float | None = Field(default=None, ge=0.0, le=1.0)
    uncertainty_selector_uncertainty_ceiling: float | None = Field(default=None, ge=0.0, le=1.0)
    uncertainty_candidate_disagreement_ceiling: float | None = Field(default=None, ge=0.0, le=1.0)
    uncertainty_confidence_floor_after_probes: float | None = Field(default=None, ge=0.0, le=1.0)
    # These three count things instead of scoring them. At least 1, because the comparison is "at or
    # above": a threshold of zero is met on every single step, so it escalates every run at once.
    uncertainty_contradictory_evidence_count: int | None = Field(default=None, ge=1)
    uncertainty_failed_attempt_count: int | None = Field(default=None, ge=1)
    uncertainty_probe_count_before_confidence_check: int | None = Field(default=None, ge=1)

    # The model that grades eval runs. Required and given no default on purpose: a judge that
    # silently followed the agent's model would move every soft score when that model changed.
    judge_model: str = Field(min_length=1)

    # How the agent reaches the platform. The MCP endpoint and the REST endpoint are configured
    # separately because the platform runs MCP as its own process (platform ADR 0006).
    platform_mcp_url: AnyHttpUrl
    platform_rest_url: AnyHttpUrl
    # The AGENT's own account: it may read telemetry and incidents and execute actions, and it may
    # NOT inject faults — that permission would also let it read the audit trail of the fault that
    # caused its own alert, which is the answer it is being tested on (platform ADR 0012).
    platform_token: SecretStr
    # The same account with reads only, used by `make eval-smoke`. The runner picks it with its
    # --smoke flag rather than by swapping environment variables, because a Makefile include of
    # .env silently overrode that and every "read-only" run actually held write permission.
    platform_smoke_token: SecretStr | None = None
    # The EVALUATOR's account, the only one allowed to inject faults. Every path that seeds, checks
    # or clears a fault world uses it. Optional here and required by ``require_chaos_token``, which
    # refuses rather than falling back to the agent's token (finding S-04).
    platform_chaos_token: SecretStr | None = None
    # The account IDs (not secrets) of the two service accounts above. The audit check that runs
    # after a stage uses them to tell the agent's own rows apart from the evaluator's; with either
    # one missing it cannot, and checks every row instead — which is how F-001 went unnoticed.
    platform_agent_principal_id: str | None = None
    platform_smoke_principal_id: str | None = None
    platform_webhook_secret: SecretStr
    # How far an alert's own timestamp may be from our clock before ingress refuses it, and how long
    # a delivery is remembered so a repeat of it can be recognised (ADR 0014).
    webhook_max_skew_seconds: int = Field(default=300, ge=1)
    # The largest alert body ingress will read. The signature covers the body, so it can only be
    # checked once the whole body is in memory — without a cap, an unauthenticated caller would be
    # the one deciding how much memory this process uses.
    webhook_max_body_bytes: int = Field(default=1_048_576, ge=1)
    # How long /health waits for the run store to answer. Deliberately far below the pool's own
    # timeout: a health check that waits as long as the fault it is meant to report has itself
    # stopped answering, and whatever polls it concludes the process is simply hung.
    health_probe_timeout_seconds: float = Field(default=2.0, gt=0)

    # The agent's own database. It never points at the platform's: the agent reaches the platform
    # only through the endpoints above (invariant 1).
    database_url: PostgresDsn

    # --- Connection pool, and how many runs it can safely serve (ADR 0022) ---------------------
    # The library's defaults are unsafe here: a run holds one connection for its whole life to keep
    # its lease (ADR 0016) and asks the same pool for a second one per checkpoint, so with enough
    # runs at once every one of them waits for a connection another one is holding.
    db_pool_size: int = Field(default=10, ge=1)
    db_max_overflow: int = Field(default=10, ge=0)
    # How long a caller waits for a free connection, far below the library's 30-second default:
    # this wait is spent in a worker thread on the ingress path, where it can neither investigate
    # the alert nor refuse it, so failing fast is better than waiting quietly.
    db_pool_timeout_seconds: float = Field(default=10.0, gt=0)
    # Connections kept back from runs for the short-lived work around them: working out an alert's
    # incident id, writing the first snapshot, and recording a crash. They are held briefly, so
    # reserving spare capacity fits them better than giving each its own pool.
    db_ingest_reserved_connections: int = Field(default=4, ge=0)
    # An optional LOWER limit on how many runs may be in flight. Unset means "as many as the pool
    # can serve", and a value above that is refused at startup rather than deadlocking later.
    agent_max_concurrent_runs: int | None = Field(default=None, ge=1)

    # Hard ceilings per incident (invariant 7). Reaching one escalates the incident to a human with
    # a briefing; none of them is ever raised mid-run.
    budget_max_tool_calls: int = Field(default=25, ge=1)
    budget_max_tokens: int = Field(default=500_000, ge=1)
    budget_max_seconds: int = Field(default=1_800, ge=1)
    # Must be above zero rather than at least one dollar: a fifty-cent cap is a real choice, and a
    # cap of zero is not — the ledger calls itself exhausted at "used >= max", so BUDGET_MAX_USD=0
    # made every run exhausted before it started and escalate having done nothing.
    budget_max_usd: Decimal = Field(default=Decimal("5.00"), gt=Decimal("0"))

    # The kill switch (docs/safety-model.md). False keeps ingress accepting and recording alerts and
    # starts no investigations. It is read once at start-up, so changing it needs a restart.
    agent_enabled: bool = True

    # How many times a run re-reads a metric to confirm a remediation worked, and how long it waits
    # between reads. One attempt means a single reading; live runs raise it because the metric they
    # are watching is served from a cache that may not have caught up yet.
    verify_probe_attempts: int = Field(default=1, ge=1, le=10)
    verify_probe_delay_seconds: float = Field(default=15.0, ge=0.0)

    # The same idea during an investigation (ADR 0009): before believing a cached reading that has
    # just ruled out a fixable explanation, read it again in case the reading predates the fault.
    # The default of zero keeps offline runs identical, since they replay fixed readings anyway.
    investigate_reprobe_attempts: int = Field(default=0, ge=0, le=3)
    investigate_reprobe_delay_seconds: float = Field(default=20.0, ge=0.0)

    # How many actions one incident may attempt: the action, plus a retry that had to investigate
    # again first (ADR 0056). This field and its default above are the only place the number lives.
    max_remediation_attempts: int = Field(
        default=DEFAULT_MAX_REMEDIATION_ATTEMPTS, ge=1, le=_MAX_REMEDIATION_ATTEMPTS_CEILING
    )

    # Action tools do real work and can take longer than a read, so they get their own timeout.
    # Sharing the read timeout made a slow SUCCESS look like a transport failure and escalate.
    action_tool_timeout_seconds: float = Field(default=60.0, ge=1.0)

    # Whether the agent reports each state change to the platform so an operator console can follow
    # a run (ADR 0068). False by default and that IS the decision: a graded eval must not make calls
    # the grading does not expect. `make demo-live` turns it on; a failed report never stops a run.
    agent_run_reporting: bool = False

    @property
    def verify_polling_window_seconds(self) -> float:
        """How long the verification re-reads take in total: 0.0 at the defaults, 100.0 live.

        Both numbers are pinned by ``tests/unit/test_polling_window.py``, because this window has
        to outlast the 60-second cache the metric is served from to observe a change at all.
        """
        return polling_window_seconds(self.verify_probe_attempts, self.verify_probe_delay_seconds)

    @property
    def investigation_reprobe_window_seconds(self) -> float:
        """How much time the investigation re-reads add: 0.0 at the defaults, 75.0 live.

        Attempts times delay, and NOT ``polling_window_seconds``: a re-read is an extra reading that
        waits first, so the first one already costs a whole delay, where the first verification
        reading costs none. Using the other formula here would understate the wait by one delay.
        """
        return self.investigate_reprobe_attempts * self.investigate_reprobe_delay_seconds

    @property
    def db_pool_capacity(self) -> int:
        """Total connections the pool will ever hand out at once."""
        return self.db_pool_size + self.db_max_overflow

    @property
    def max_concurrent_runs(self) -> int:
        """How many runs may be in flight at once, worked out from what the pool can serve.

        ``AGENT_MAX_CONCURRENT_RUNS`` can only lower this: a configured value above what the pool
        supports is refused at startup, because it would deadlock rather than run faster.
        """
        ceiling = (
            self.db_pool_capacity - self.db_ingest_reserved_connections
        ) // _CONNECTIONS_PER_RUN
        if self.agent_max_concurrent_runs is None:
            return ceiling
        return min(self.agent_max_concurrent_runs, ceiling)

    @property
    def seeded_max_tokens(self) -> int:
        """The token ceiling a run actually starts with, once the multiplier is applied.

        Rounded DOWN, so a multiplier small enough to leave no budget shows up as zero here and is
        refused by the validator below, rather than being rounded up into a budget of one token.
        """
        return int(Decimal(self.budget_max_tokens) * self.token_budget_multiplier)

    @property
    def seeded_max_usd(self) -> Decimal:
        """The dollar ceiling a run actually starts with, once the multiplier is applied.

        Exact decimals from end to end, never a floating-point step in between, so a cost meter
        cannot drift a fraction of a cent away from what was configured (ADR 0015).
        """
        return self.budget_max_usd * self.usd_budget_multiplier

    def require_chaos_token(self) -> str:
        """The evaluator's fault-injection token, or a refusal that says how to fix it.

        The one place ``PLATFORM_CHAOS_TOKEN`` is read. Blank or whitespace counts as not set, and
        it never falls back to the agent's token, which does not carry the permission anyway — the
        fallback is what made a seeding failure look like the platform refusing (finding S-04).
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
        """The read-only account's token, or a refusal that says how to fix it.

        The same rules as ``require_chaos_token``: blank counts as not set, and no falling back to
        the agent's token, because a read made with the agent's token is recorded in the audit log
        as the agent's own — and those rows are what the demo is showing (finding F3).
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
        """The model id a run of this role bills its tokens to.

        The one place the two roles map to model ids. A run records both the role and the id it
        resolved to, so a result can always be traced back to the model that produced it.
        """
        return {
            ModelRole.DEVELOPMENT: self.development_model,
            ModelRole.BENCHMARK: self.benchmark_model,
        }[role]

    @model_validator(mode="after")
    def _configured_models_are_priced(self) -> Settings:
        """Refuse to start if any configured model id has no price row.

        Without this the cost meter falls back to the most expensive rate on record for every call
        (ADR 0015). That fallback is the right thing mid-run, where aborting would waste the work
        already paid for, and the wrong thing here, where the operator can simply fix the config.
        """
        unpriced = {
            name.upper(): value
            for name, value in (
                ("agent_model", self.agent_model),
                ("judge_model", self.judge_model),
                # The role models are checked too, because `--model-role benchmark` picks
                # BENCHMARK_MODEL on the paid path, where an unpriced id would otherwise only be
                # discovered once the run was already spending money at the fallback rate.
                ("development_model", self.development_model),
                ("benchmark_model", self.benchmark_model),
            )
            if value not in MODEL_PRICING
        }
        if unpriced:
            # Every unpriced id in one message: reporting them one at a time would cost the operator
            # a restart per fix, and the message says exactly which file to add the rates to.
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
        """Refuse to start on a pool that cannot serve even one run from end to end.

        The alternative is a process that boots, accepts alerts, and then stalls for the pool
        timeout on every one of them. Failing at startup says what is wrong; stalling does not.
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
        """Refuse a budget multiplier that leaves a run with nothing to spend.

        A ledger counts itself exhausted at "used >= max", so a ceiling of zero is already exhausted
        before the first step: the run escalates having investigated nothing, and the report reads
        exactly like a budget ceiling doing its job.
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
            # Both problems in one message, for the same reason as the pricing check above.
            raise ValueError(
                f"{'; '.join(degenerate)}. A ledger seeded at zero is born exhausted "
                "(BudgetLedger.is_exhausted compares used >= max), so the run would "
                "escalate before TRIAGE having investigated nothing — which reads in "
                "the report exactly like a budget ceiling working as intended. Raise "
                "the multiplier, or raise the budget it multiplies."
            )
        return self


def settings_env_var_names() -> tuple[str, ...]:
    """Every environment variable ``Settings`` can read, sorted by name.

    Worked out from the model itself rather than listed anywhere, because a hand-kept list drifts
    the first time a field is added. Names come back upper-cased, and a field with an explicit
    alias is reported under that alias, which is the name an operator actually sets.
    """
    prefix = str(Settings.model_config.get("env_prefix", ""))
    names: list[str] = []
    for name, field in Settings.model_fields.items():
        alias = field.validation_alias if isinstance(field.validation_alias, str) else field.alias
        names.append(alias if isinstance(alias, str) else f"{prefix}{name}".upper())
    return tuple(sorted(names))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """The one ``Settings`` instance application code shares, built on first use.

    Cached, so the environment and .env are read once per process. Tests build ``Settings``
    directly instead, because a cached instance would leak between them.
    """
    return Settings()  # type: ignore[call-arg]
