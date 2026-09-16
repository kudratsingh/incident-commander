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


# The id every model ROLE resolves to unless an operator points it somewhere
# else. One constant rather than three copies of a literal: the three settings
# below are the same claim about which model this repo is pinned to, and three
# copies of a pinned id is three places for it to drift. Verify a change
# against docs.claude.com (CLAUDE.md) AND add the new id's row to
# ``MODEL_PRICING`` — the validator at the bottom of this file refuses startup
# otherwise.
_DEFAULT_MODEL_ID: Final[str] = "claude-sonnet-4-6"


class ModelRole(StrEnum):
    """Which of the two model roles a run is being made under (plan 02 § 9).

    The roles are not two more model dimensions: a run resolves ``AGENT_MODEL``
    from exactly one of them, and the role travels with the run's provenance so
    a reported number can name the role that produced it.

    * ``DEVELOPMENT`` — harness work, schema work, plumbing, grader logic. A
      report containing one of these cannot close a phase (03 § 14), which is
      the whole reason the role is recorded rather than inferred.
    * ``BENCHMARK`` — every reported number; the phase-close protocol.

    ``DEVELOPMENT`` is the default everywhere, deliberately: the expensive
    mistake is a development run that is mistaken for a benchmark one, so the
    role that claims less is the one you get without asking.
    """

    DEVELOPMENT = "development"
    BENCHMARK = "benchmark"


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


class ChaosTokenNotConfigured(RuntimeError):
    """A seed/reset/chaos path was reached with ``PLATFORM_CHAOS_TOKEN`` unset.

    Its own type, not a bare ``RuntimeError``, because every caller wants the
    same behaviour — refuse before touching the world, and say the one thing
    the operator has to do — and because the runner's crash rail buckets
    exceptions by type. The message is deliberately one line: it is read at
    the moment a paid run has just refused to start.
    """


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
    agent_model: str = _DEFAULT_MODEL_ID
    # The two model ROLES (plan 02 § 9, WP-0.3). ``AGENT_MODEL`` above stays
    # the id a run actually bills; these two say which id each role resolves
    # it to, and the eval runner's ``--model-role`` picks between them. They
    # are separate settings rather than one AGENT_MODEL an operator edits
    # per run because the point is to tell the two apart *in the artifact*:
    # every reported number has to name the model that produced it, and a
    # development run must never be mistaken for a closing one.
    #
    # Both default to the id this repo is pinned to, so adding the roles
    # changes what no run resolves to. Pointing either at another id is a
    # configuration change that needs that id's row in MODEL_PRICING — the
    # validator below refuses startup without one, for both of these exactly
    # as it already does for AGENT_MODEL and JUDGE_MODEL.
    development_model: str = _DEFAULT_MODEL_ID
    benchmark_model: str = _DEFAULT_MODEL_ID
    # Which inference strategy the investigation loop's one planner call runs
    # (plan 02 § 4, WP-0.2). Env var INFERENCE_STRATEGY — bare, like every
    # other name here: SettingsConfigDict sets no env_prefix.
    #
    # Typed as the enum, not as ``str``, and that is the whole guard: pydantic
    # refuses an unknown value at construction and names the permitted ones in
    # the error, so ``INFERENCE_STRATEGY=basline`` cannot start a run that
    # silently reports a ``baseline`` number (architecture principle 1 — a
    # value from a fixed set is a StrEnum, not a validated string). Blank means
    # unset, not empty (``env_ignore_empty``), so it falls back to the default.
    #
    # The default is the control group and is pinned by a test on both sides of
    # the seam (``tests/unit/test_strategies.py``): ``baseline`` is the current
    # behaviour, the loop the campaign's eight green live runs were made with,
    # and the strategy every later one is measured against.
    inference_strategy: StrategyName = StrategyName.BASELINE

    # Required with no default (pinned separately for eval stability, per
    # CLAUDE.md). min_length guards direct construction — Settings(
    # judge_model="") — which env_ignore_empty cannot reach; an empty judge
    # id otherwise only failed as an API 400 at the first judge call mid-run.
    judge_model: str = Field(min_length=1)

    # Platform. MCP and REST are separate URLs per platform ADR-0006.
    platform_mcp_url: AnyHttpUrl
    platform_rest_url: AnyHttpUrl
    # The AGENT's own principal: telemetry:read + incidents:read +
    # actions:execute, and — since platform v0.6.5 — deliberately NOT
    # chaos:invoke. The platform withholds the `chaos.%` audit stream from
    # principals that cannot fire chaos, so an agent token carrying
    # chaos:invoke can read which fault was injected seconds before its own
    # alert (platform ADR 0012, owner decision O-4). Minted as
    # `PLATFORM_TOKEN` by `make bootstrap-token`.
    platform_token: SecretStr
    # Read-scoped twin used by `make eval-smoke` (telemetry:read +
    # incidents:read only). Selected by the runner's --smoke flag rather
    # than shell plumbing: passing it through make was silently defeated
    # by `-include .env` (PR #62 vs #69), so every "read-scoped" smoke run
    # up to 2026-08-07 actually held write scope. Config beats inheritance.
    platform_smoke_token: SecretStr | None = None
    # The EVALUATOR's principal: telemetry:read + incidents:read +
    # chaos:invoke, minted as `PLATFORM_CHAOS_TOKEN` alongside the two
    # above. Every path that seeds, verifies or tears down a fault world
    # runs under it; the agent under test never sees it.
    #
    # Optional at load time and required at point of use, on purpose. The
    # agent process, the API, `make test`, every canned eval and the whole
    # offline suite need no chaos principal at all, and making this required
    # would refuse to construct Settings for all of them. What must not
    # happen is a seeding path silently falling back to
    # ``platform_token``: since v0.6.5 that principal cannot seed, so the
    # fallback would surface as a mid-run -32002 with the archive already
    # open (the S-04 shape). ``require_chaos_token`` below is the one
    # accessor, and it refuses rather than degrades.
    platform_chaos_token: SecretStr | None = None
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
    # Largest alert body the ingress will read (WO-R2-86). The HMAC covers the
    # body, so authentication cannot happen until the body is in memory —
    # which means an unauthenticated caller decides how much memory this
    # process spends unless something ahead of the route says no. 1 MiB is two
    # orders of magnitude above the largest alert the platform emits and small
    # enough that a flood of them is a bandwidth problem, not an OOM.
    webhook_max_body_bytes: int = Field(default=1_048_576, ge=1)
    # How long /health waits for its datastore probe before calling the agent
    # degraded. Deliberately far below DB_POOL_TIMEOUT_SECONDS: an exhausted
    # pool makes the probe wait the full checkout timeout, and a health check
    # that waits as long as the fault it reports is one more thing that has
    # stopped answering.
    health_probe_timeout_seconds: float = Field(default=2.0, gt=0)

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
    # gt=0, not ge=1 like the integer dimensions above: a run capped at fifty
    # cents is a legitimate operator choice, but a run capped at zero is not a
    # policy — ``is_exhausted`` compares with ``>=``, so BUDGET_MAX_USD=0 made
    # every run born exhausted. It terminated on its first budget check having
    # investigated nothing, and the outcome was indistinguishable from a
    # budget ceiling working exactly as designed.
    budget_max_usd: Decimal = Field(default=Decimal("5.00"), gt=Decimal("0"))

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
        return polling_window_seconds(self.verify_probe_attempts, self.verify_probe_delay_seconds)

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

    def require_chaos_token(self) -> str:
        """The evaluator's ``chaos:invoke`` token, or refuse in one line.

        The only way a seed/reset/chaos path reads
        ``PLATFORM_CHAOS_TOKEN``. Blank counts as unset (``env_ignore_empty``
        already makes ``PLATFORM_CHAOS_TOKEN=`` a ``None``, and a
        whitespace-only value is checked here so a hand-edited ``.env``
        cannot slip one through) — and neither falls back to
        ``platform_token``, which since platform v0.6.5 does not carry the
        scope at all. A fallback would turn a missing credential into a
        refusal fired mid-run, under the archive, which is exactly the
        failure S-04 named on the smoke token.
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

        The one place the mapping lives. A run resolves ``AGENT_MODEL`` from
        its role here and then stamps BOTH the role and the resolved id into
        its provenance, so the artifact answers "which model, under which
        role?" without the reader having to know this table.
        """
        return {
            ModelRole.DEVELOPMENT: self.development_model,
            ModelRole.BENCHMARK: self.benchmark_model,
        }[role]

    @model_validator(mode="after")
    def _configured_models_are_priced(self) -> Settings:
        """Refuse at startup any model id with no row in the price table.

        Without this the failure is silent and expensive rather than loud:
        ``pricing_for`` falls back to ``class_ceiling``, the per-token-class
        maximum of every registered row, so an unpriced model bills at the
        dearest rate on record. Every budget the meter guards — the USD
        ceiling, the per-run cap, the numbers in the briefing — is then
        computed from a price nobody chose, and the only trace is one
        ``WARNING`` on first use.

        That fallback stays exactly as it is; it is the right behaviour for
        the case it exists for, which is a *live run* discovering an
        accounting gap mid-incident (ADR 0015 — do not abort an incident over
        billing arithmetic). This check runs earlier, where the tradeoff is
        different: nothing is in flight at construction time, so the honest
        answer to "which model am I about to bill?" is to demand one rather
        than to guess high.

        The remedy is four numbers in ``MODEL_PRICING``, in this repo,
        verified against docs.claude.com — the same rule CLAUDE.md already
        applies to the model id strings themselves.
        """
        unpriced = {
            name.upper(): value
            for name, value in (
                ("agent_model", self.agent_model),
                ("judge_model", self.judge_model),
                # The two role settings are checked here and not only where
                # they are resolved: ``--model-role benchmark`` resolves
                # BENCHMARK_MODEL on the paid path, and an unpriced id
                # discovered there would be discovered mid-run, billing at
                # the per-class ceiling with one log line (the accounting
                # hole ADR 0015 exists to close). Four model settings, one
                # refusal, before anything is in flight.
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


def settings_env_var_names() -> tuple[str, ...]:
    """Every environment variable ``Settings`` can read, sorted.

    Derived from the model rather than written down, because it had been
    written down three times and all three copies had drifted: the two test
    isolation fixtures each claimed to list "every env var Settings can read"
    and each missed a different pair, so the variables they missed were
    silently inherited from whoever ran the tests. A hand-kept list of a
    generated thing is a list that is wrong at some point after it is written.

    Names are upper-cased because pydantic-settings resolves env lookups
    case-insensitively and upper case is the spelling everything else uses;
    ``env_prefix`` is honoured so the derivation stays correct if one is ever
    set. A field carrying an explicit string alias reports that alias.
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
