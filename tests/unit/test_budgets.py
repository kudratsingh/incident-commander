"""Per-strategy budget policy (WP-2.4): the multipliers, and where they apply.

Plan 02 § 8: a multiplier is declared in configuration and applied when the ledger is
seeded. Two deliberate things — the tool-call budget is never multiplied (probing is what
the strategies compete on), and BUDGET is reported beside correctness, never folded in.
The numbers are the repo's defaults; the paid protocol's live only in ``.env`` (D7).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from evals.graders.deterministic import (
    GradeDimension,
    ScenarioExpectation,
    grade,
)
from evals.runner import ExecutionMode, ScenarioOutcome, build_provenance
from incident_commander.agent.factory import start_run
from incident_commander.agent.state import BudgetLedger, IncidentState, RunState
from incident_commander.agent.strategies.names import StrategyName
from incident_commander.config import ModelRole, Settings

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Every multiplier a strategy can declare, by its ``Settings`` name. The source scan
# below reads this tuple.
_MULTIPLIER_FIELDS = ("token_budget_multiplier", "usd_budget_multiplier")

# The two derived budgets: multipliers are READ in config, these APPLIED at the seed.
_SEEDED_PROPERTIES = ("seeded_max_tokens", "seeded_max_usd")


def _settings(**overrides: Any) -> Settings:
    """A Settings with the four budgets seeded explicitly.

    ``_env_file=None`` disables dotenv, and the budgets are passed, not defaulted.
    """
    defaults: dict[str, Any] = {
        "anthropic_api_key": SecretStr("sk-ant-test"),
        "judge_model": "claude-haiku-4-5",
        "platform_mcp_url": "https://mcp.local",
        "platform_rest_url": "https://api.local",
        "platform_token": SecretStr("svc"),
        "platform_webhook_secret": SecretStr("hmac"),
        "database_url": "postgresql://u:p@localhost:5432/db",
        "budget_max_tool_calls": 25,
        "budget_max_tokens": 500_000,
        "budget_max_seconds": 1_800,
        "budget_max_usd": Decimal("5.00"),
    }
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)  # type: ignore[call-arg]


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 9, 17, 12, 0, tzinfo=UTC)


class TestTheMultiplierIsAppliedAtTheLedgerSeed:
    """``start_run`` is the one place a budget is scaled (04:95, factory.py)."""

    def test_token_multiplier_scales_the_seeded_token_budget(self, now: datetime) -> None:
        settings = _settings(budget_max_tokens=500_000, token_budget_multiplier=Decimal("4"))
        run = start_run({"source": "s"}, settings, now)
        assert run.budget.max_tokens == 2_000_000

    def test_usd_multiplier_scales_the_seeded_usd_budget(self, now: datetime) -> None:
        settings = _settings(budget_max_usd=Decimal("5.00"), usd_budget_multiplier=Decimal("1.5"))
        run = start_run({"source": "s"}, settings, now)
        assert run.budget.max_usd == Decimal("7.50")

    def test_a_multiplier_below_one_shrinks_the_budget(self, now: datetime) -> None:
        settings = _settings(
            budget_max_tokens=500_000,
            budget_max_usd=Decimal("5.00"),
            token_budget_multiplier=Decimal("0.5"),
            usd_budget_multiplier=Decimal("0.5"),
        )
        run = start_run({"source": "s"}, settings, now)
        assert run.budget.max_tokens == 250_000
        assert run.budget.max_usd == Decimal("2.50")

    def test_a_fractional_token_budget_floors_to_whole_tokens(self, now: datetime) -> None:
        """No fraction of a token can be spent, so none is granted.

        Rounding up would hide a budget of nothing behind a budget of one.
        """
        settings = _settings(budget_max_tokens=1_001, token_budget_multiplier=Decimal("0.5"))
        run = start_run({"source": "s"}, settings, now)
        assert run.budget.max_tokens == 500

    def test_the_usd_budget_carries_no_binary_rounding_noise(self, now: datetime) -> None:
        """Decimal times Decimal, never a float intermediate (ADR 0015)."""
        settings = _settings(budget_max_usd=Decimal("0.10"), usd_budget_multiplier=Decimal("3"))
        run = start_run({"source": "s"}, settings, now)
        assert run.budget.max_usd == Decimal("0.30")


class TestTheToolCallCeilingIsNeverMultiplied:
    """02 § 8: probing the world is what the strategies compete on."""

    @pytest.mark.parametrize(
        "multiplier", [Decimal("0.5"), Decimal("1"), Decimal("2"), Decimal("8")]
    )
    def test_the_fleet_default_survives_every_multiplier(
        self, multiplier: Decimal, now: datetime
    ) -> None:
        settings = _settings(
            budget_max_tool_calls=25,
            token_budget_multiplier=multiplier,
            usd_budget_multiplier=multiplier,
        )
        run = start_run({"source": "s"}, settings, now)
        assert run.budget.max_tool_calls == 25

    @pytest.mark.parametrize(
        "multiplier", [Decimal("0.5"), Decimal("1"), Decimal("2"), Decimal("8")]
    )
    def test_the_adr_0019_override_survives_every_multiplier(
        self, multiplier: Decimal, now: datetime
    ) -> None:
        """A scenario's declared cap is the runtime ceiling, unscaled.

        It is also the number the run is graded against (ADR 0019).
        """
        settings = _settings(
            budget_max_tool_calls=25,
            token_budget_multiplier=multiplier,
            usd_budget_multiplier=multiplier,
        )
        run = start_run({"source": "s"}, settings, now, max_tool_calls=13)
        assert run.budget.max_tool_calls == 13

    @pytest.mark.parametrize(
        "multiplier", [Decimal("0.5"), Decimal("1"), Decimal("2"), Decimal("8")]
    )
    def test_the_wall_clock_ceiling_survives_every_multiplier(
        self, multiplier: Decimal, now: datetime
    ) -> None:
        """No wall-clock multiplier is declared (02 § 8 names three knobs).

        Raising ``BUDGET_MAX_SECONDS`` is a visible act for the whole invocation.
        """
        settings = _settings(
            budget_max_seconds=1_800,
            token_budget_multiplier=multiplier,
            usd_budget_multiplier=multiplier,
        )
        run = start_run({"source": "s"}, settings, now)
        assert run.budget.max_wall_seconds == 1_800


class TestBaselineIsBitForBitUnchanged:
    """The control group must not move because this packet landed."""

    def test_the_defaults_are_one(self) -> None:
        settings = _settings()
        assert settings.inference_strategy is StrategyName.BASELINE
        assert settings.token_budget_multiplier == Decimal("1")
        assert settings.usd_budget_multiplier == Decimal("1")
        assert settings.max_iterations_override is None

    def test_the_seeded_baseline_ledger_is_the_one_this_repo_already_seeded(
        self, now: datetime
    ) -> None:
        """Identical to the pre-WP-2.4 seed, field for field.

        Written out rather than compared against another ``start_run`` call.
        """
        settings = _settings()
        run = start_run({"source": "s"}, settings, now)
        assert run.budget == BudgetLedger(
            max_tool_calls=settings.budget_max_tool_calls,
            max_tokens=settings.budget_max_tokens,
            max_wall_seconds=settings.budget_max_seconds,
            max_usd=settings.budget_max_usd,
        )

    def test_the_seeded_baseline_ledger_is_unchanged_under_the_adr_0019_override(
        self, now: datetime
    ) -> None:
        settings = _settings()
        run = start_run({"source": "s"}, settings, now, max_tool_calls=13)
        assert run.budget == BudgetLedger(
            max_tool_calls=13,
            max_tokens=settings.budget_max_tokens,
            max_wall_seconds=settings.budget_max_seconds,
            max_usd=settings.budget_max_usd,
        )


class TestNoOtherCallSiteScalesABudget:
    """One seam, pinned by reading the source.

    A second site that scaled a budget would not fail any behavioural test —
    both would be "correct" — and every reported cost number would silently be
    a different quantity from the one the strategy declared.
    """

    @staticmethod
    def _python_sources() -> list[Path]:
        roots = (_REPO_ROOT / "src", _REPO_ROOT / "evals", _REPO_ROOT / "scripts")
        return sorted(path for root in roots for path in root.rglob("*.py"))

    def test_the_multipliers_are_read_only_in_config(self) -> None:
        allowed = {_REPO_ROOT / "src" / "incident_commander" / "config.py"}
        offenders = {
            path
            for path in self._python_sources()
            if path not in allowed
            and any(field in path.read_text(encoding="utf-8") for field in _MULTIPLIER_FIELDS)
        }
        assert offenders == set(), (
            "a multiplier is read outside config.py; the scaled budgets are "
            f"{', '.join(_SEEDED_PROPERTIES)} and the seed is the only consumer"
        )

    def test_the_scaled_budgets_are_applied_only_where_the_ledger_is_seeded(self) -> None:
        allowed = {
            _REPO_ROOT / "src" / "incident_commander" / "config.py",
            _REPO_ROOT / "src" / "incident_commander" / "agent" / "factory.py",
        }
        offenders = {
            path
            for path in self._python_sources()
            if path not in allowed
            and any(name in path.read_text(encoding="utf-8") for name in _SEEDED_PROPERTIES)
        }
        assert offenders == set(), (
            "a scaled budget is applied outside start_run's ledger seed "
            "(plan 04:95); one seam, or every cost number means two things"
        )


class TestADegenerateMultiplierIsRefused:
    """factory.py:94's trap, one dimension wider.

    ``start_run`` already ignores a ``max_tool_calls`` of 0 (a zero ledger is born
    exhausted). Tokens, dollars and wall seconds have no such guard, so a multiplier
    seeding one at zero is indistinguishable from a ceiling working as designed.
    """

    def test_a_zero_token_multiplier_is_refused_naming_the_dimension(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            _settings(token_budget_multiplier=Decimal("0"))
        message = str(excinfo.value)
        assert "TOKEN_BUDGET_MULTIPLIER" in message
        assert "BUDGET_MAX_TOKENS" in message

    def test_a_zero_usd_multiplier_is_refused_naming_the_dimension(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            _settings(usd_budget_multiplier=Decimal("0"))
        message = str(excinfo.value)
        assert "USD_BUDGET_MULTIPLIER" in message
        assert "BUDGET_MAX_USD" in message

    def test_a_multiplier_that_rounds_the_token_budget_to_zero_is_refused(self) -> None:
        """Sub-minimal, not just zero: 100 tokens at 0.001 is no tokens."""
        with pytest.raises(ValidationError) as excinfo:
            _settings(budget_max_tokens=100, token_budget_multiplier=Decimal("0.001"))
        assert "TOKEN_BUDGET_MULTIPLIER" in str(excinfo.value)

    def test_both_dimensions_are_named_in_one_refusal(self) -> None:
        """Fixing them one restart at a time is the shape that wastes an afternoon."""
        with pytest.raises(ValidationError) as excinfo:
            _settings(
                token_budget_multiplier=Decimal("0"),
                usd_budget_multiplier=Decimal("0"),
            )
        message = str(excinfo.value)
        assert "TOKEN_BUDGET_MULTIPLIER" in message
        assert "USD_BUDGET_MULTIPLIER" in message

    @pytest.mark.parametrize("field", _MULTIPLIER_FIELDS)
    def test_a_negative_multiplier_is_refused(self, field: str) -> None:
        with pytest.raises(ValidationError) as excinfo:
            _settings(**{field: Decimal("-1")})
        assert field in str(excinfo.value)

    def test_a_zero_iteration_override_is_refused(self) -> None:
        """Same family: a loop allowed no iterations investigates nothing."""
        with pytest.raises(ValidationError):
            _settings(max_iterations_override=0)

    def test_a_multiplier_the_budget_survives_is_accepted(self) -> None:
        """The refusal is of a budget of nothing, not of a small budget."""
        settings = _settings(budget_max_tokens=100, token_budget_multiplier=Decimal("0.01"))
        assert settings.seeded_max_tokens == 1


class TestAnOverBaselineBudgetRunIsAFrontierPoint:
    """02 § 8: BUDGET is reported beside correctness, never folded into it."""

    @staticmethod
    def _resolved(run: RunState, tokens_used: int, usd_used: Decimal, calls: int) -> RunState:
        return run.model_copy(
            update={
                "state": IncidentState.RESOLVED,
                "budget": run.budget.model_copy(
                    update={
                        "tokens_used": tokens_used,
                        "usd_used": usd_used,
                        "tool_calls_used": calls,
                    }
                ),
            }
        )

    def test_a_correct_run_over_the_baseline_token_budget_is_not_a_failure(
        self, now: datetime
    ) -> None:
        baseline_tokens = 200_000
        settings = _settings(
            budget_max_tokens=baseline_tokens,
            token_budget_multiplier=Decimal("4"),
            budget_max_usd=Decimal("1.00"),
            usd_budget_multiplier=Decimal("4"),
        )
        run = self._resolved(
            start_run({"source": "s"}, settings, now, max_tool_calls=13),
            tokens_used=650_000,
            usd_used=Decimal("2.40"),
            calls=9,
        )
        # Over the baseline on both scaled meters, inside its own ledger.
        assert run.budget.tokens_used > baseline_tokens
        assert run.budget.usd_used > Decimal("1.00")
        assert not run.budget.is_exhausted

        report = grade(
            run,
            ScenarioExpectation(
                name="frontier",
                expected_terminal_state=IncidentState.RESOLVED,
                max_tool_calls=13,
            ),
        )
        budget_dimension = next(
            d for d in report.dimensions if d.dimension is GradeDimension.BUDGET
        )
        assert budget_dimension.passed is True
        assert report.passed is True

    def test_the_report_row_carries_the_spend_beside_the_grade(self, now: datetime) -> None:
        """The frontier needs the cost, and the row is where a reader finds it."""
        settings = _settings(
            budget_max_tokens=200_000,
            token_budget_multiplier=Decimal("4"),
            budget_max_usd=Decimal("1.00"),
            usd_budget_multiplier=Decimal("4"),
        )
        run = self._resolved(
            start_run({"source": "s"}, settings, now, max_tool_calls=13),
            tokens_used=650_000,
            usd_used=Decimal("2.40"),
            calls=9,
        )
        report = grade(
            run,
            ScenarioExpectation(
                name="frontier",
                expected_terminal_state=IncidentState.RESOLVED,
                max_tool_calls=13,
            ),
        )
        outcome = ScenarioOutcome(
            scenario="frontier",
            final_state=run.state,
            tool_calls_used=run.budget.tool_calls_used,
            report=report,
            failure_class="passed",
            provenance=build_provenance(
                "frontier",
                settings,
                model_role=ModelRole.BENCHMARK,
                invocation_id="wp24",
                execution_mode=ExecutionMode.CANNED,
                budget=run.budget,
                recorded_at=now,
            ),
        )

        assert outcome.report.passed is True
        assert outcome.provenance is not None
        ledger = outcome.provenance.budget
        # Both halves of the frontier point: what it spent, and the ceiling
        # the strategy was granted to spend it under.
        assert ledger.tokens_used == 650_000
        assert ledger.max_tokens == 800_000
        assert ledger.usd_used == Decimal("2.40")
        assert ledger.max_usd == Decimal("4.00")
        assert outcome.provenance.strategy == StrategyName.BASELINE.value
