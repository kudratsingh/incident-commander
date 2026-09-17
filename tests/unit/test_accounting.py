"""Billed LLM work reaches the ledger on every path, happy or not — and is
broken down by role, by step and by token class (WP-2.3).

ADR 0015's rule is one-directional: the meter may over-report but never
under-report, because ``BUDGET_MAX_USD`` is what bounds an unattended paid
run. The first half of this file pins the two halves of that rule the client
can express — the attempts that were billed and discarded, and the calls that
were billed and then raised.

The second half is the breakdown WP-2.3 adds on top, and its load-bearing
property is that it *adds up*: a per-role split that does not reconcile with
``BudgetLedger`` is worse than no split, because every cost column in every
strategy comparison is computed from it and a dropped leg reports one arm as
cheaper than it was.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final
from uuid import uuid4

import pytest
from pydantic import BaseModel, SecretStr

from evals.graders.deterministic import (
    DimensionResult,
    GradeDimension,
    GradeReport,
    ScenarioExpectation,
)
from evals.runner import (
    RoleAccounting,
    RunAccountingRecord,
    RunReport,
    ScenarioCrash,
    ScenarioOutcome,
    _crashed_result,
    _print_summary,
    run_scenario,
)
from evals.scenarios.schema import Scenario
from incident_commander.agent.accounting import (
    LLMCallAccounting,
    RunAccounting,
    accrue_llm_error,
    accrue_llm_usage,
    token_volume,
)
from incident_commander.agent.hypothesis import InvestigationStep
from incident_commander.agent.investigation import make_llm_investigate
from incident_commander.agent.state import BudgetLedger, IncidentState, RunState
from incident_commander.api.schemas import AlertPayload
from incident_commander.config import Settings
from incident_commander.llm.client import (
    LLMClientProtocol,
    LLMError,
    LLMOutputError,
    LLMResult,
    LLMUsage,
)
from incident_commander.llm.fakes import CannedLLMClient, CannedUsage
from incident_commander.llm.pricing import MODEL_PRICING, class_ceiling, cost_of
from incident_commander.tools.mcp_client import ToolResult

_MODEL = "claude-sonnet-4-6"
# 15.00 USD / Mtok output — the class the discarded estimate is billed at.
_OUTPUT_RATE = Decimal("15.00") / Decimal(1_000_000)


class _Out(BaseModel):
    pass


def _ledger() -> BudgetLedger:
    return BudgetLedger(
        max_tool_calls=25,
        max_tokens=1_000_000,
        max_wall_seconds=1800,
        max_usd=Decimal("10"),
    )


class TestDiscardedAttemptsAreCharged:
    def test_a_retried_attempt_adds_volume_and_dollars(self) -> None:
        one = accrue_llm_usage(_ledger(), LLMUsage(input_tokens=100, output_tokens=50), _MODEL)
        two = accrue_llm_usage(
            _ledger(),
            LLMUsage(
                input_tokens=100,
                output_tokens=50,
                discarded_attempts=2,
                discarded_max_tokens=1000,
            ),
            _MODEL,
        )
        assert two.tokens_used - one.tokens_used == 2000
        assert two.usd_used - one.usd_used == (2000 * _OUTPUT_RATE).quantize(Decimal("0.000001"))

    def test_a_result_is_usable_as_a_usage_record(self) -> None:
        """``LLMResult`` is an ``LLMUsage``; the call sites keep working."""
        result = LLMResult(
            output=_Out(),
            stop_reason="tool_use",
            input_tokens=10,
            output_tokens=20,
            discarded_attempts=1,
            discarded_max_tokens=100,
        )
        assert accrue_llm_usage(_ledger(), result, _MODEL).tokens_used == 130

    def test_the_estimate_is_never_below_the_real_output_bill(self) -> None:
        """max_tokens bounds what one attempt could have generated."""
        capped = LLMUsage(discarded_attempts=1, discarded_max_tokens=4096)
        real_worst_case = LLMUsage(output_tokens=4096)
        charged = accrue_llm_usage(_ledger(), capped, _MODEL)
        worst = accrue_llm_usage(_ledger(), real_worst_case, _MODEL)
        assert charged.usd_used >= worst.usd_used


class TestFailedCallsAreCharged:
    def test_an_llm_error_with_usage_reaches_the_ledger(self) -> None:
        err = LLMError("truncated", usage=LLMUsage(input_tokens=100, output_tokens=4096))
        after = accrue_llm_error(_ledger(), err, _MODEL)
        assert after.tokens_used == 4196
        assert after.usd_used > Decimal("0")

    def test_an_llm_error_without_usage_leaves_the_ledger_alone(self) -> None:
        """The canned client raises usage-free; charging a guess would be a lie."""
        before = _ledger()
        assert accrue_llm_error(before, LLMError("no more canned responses"), _MODEL) == before

    def test_a_non_llm_error_leaves_the_ledger_alone(self) -> None:
        before = _ledger()
        assert accrue_llm_error(before, ValueError("bad payload"), _MODEL) == before


# --- WP-2.3: per-role cost, latency and context accounting -----------------


class _StepCountingClock:
    """A monotonic clock that advances a fixed amount per reading pair.

    Real elapsed time in a unit test is either zero or flaky; this makes the
    duration a fact the test states rather than one it hopes for.
    """

    def __init__(self, step_seconds: float = 0.25) -> None:
        self._step = step_seconds
        self._now = 0.0
        self._readings = 0

    def __call__(self) -> float:
        self._readings += 1
        if self._readings % 2 == 0:
            self._now += self._step
        return self._now


class _FailsThenSucceeds:
    """Bills, raises ``LLMOutputError``, then bills again and parses.

    The ADR-0035 repair path as the accounting has to see it: two billed
    calls for one logical planner step. ``CannedLLMClient`` cannot stand in —
    it raises a bare ``ValidationError`` carrying no usage, so the leg that
    was billed and thrown away would be free, which is the exact hole this
    test exists to keep shut.
    """

    def __init__(self, output: dict[str, Any], *, failed_usage: LLMUsage, usage: LLMUsage) -> None:
        self._output = output
        self._failed_usage = failed_usage
        self._usage = usage
        self.calls = 0

    def call[T: BaseModel](
        self,
        system_prompt: str,
        user_message: str,
        output_model: type[T],
        model: str,
        max_tokens: int = 4096,
        *,
        repair_of: str | None = None,
    ) -> LLMResult[T]:
        self.calls += 1
        if self.calls == 1:
            raise LLMOutputError("hypotheses: field required", usage=self._failed_usage)
        return self._usage.with_output(
            output_model.model_validate(self._output), stop_reason="fake"
        )


class _FakeLagMCPClient:
    """The one read the planner below asks for, answered the same way twice."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def call_tool(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        timeout_seconds: float | None = None,
    ) -> ToolResult:
        self.calls.append(name)
        return ToolResult(
            content=[
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "consumer_group": "billing",
                            "lag": 42,
                            "lag_known": True,
                            "source": "static",
                            "cache_key": "kafka:consumer_lag:worker-dispatcher",
                        }
                    ),
                }
            ]
        )


_PROBE_STEP: Final[dict[str, Any]] = {
    "hypotheses": [
        {
            "category": "consumer_saturation",
            "name": "consumer_saturation",
            "confidence": 0.55,
            "reasoning": "Paging severity on the billing consumer.",
        }
    ],
    "next_action": {
        "kind": "probe",
        "tool_name": "get_consumer_lag",
        "arguments": {"consumer_group": "billing"},
    },
}

_STOP_STEP: Final[dict[str, Any]] = {
    "hypotheses": [
        {
            "category": "consumer_saturation",
            "name": "consumer_saturation",
            "confidence": 0.9,
            "reasoning": "Lag reading confirms saturation.",
        }
    ],
    "next_action": {"kind": "stop", "reason": "confidence sufficient"},
}


def _investigating(budget: BudgetLedger, now: datetime) -> RunState:
    return RunState(
        incident_id=uuid4(),
        state=IncidentState.INVESTIGATING,
        alert={"source": "kafka", "severity": "high", "group": "billing"},
        budget=budget,
        created_at=now,
        updated_at=now,
    )


def _run_investigation(
    accounting: RunAccounting,
    client: LLMClientProtocol,
    budget: BudgetLedger,
    now: datetime,
) -> RunState:
    """Drive the real investigation loop over a metered client."""
    transition = make_llm_investigate(
        _FakeLagMCPClient(),
        accounting.meter(client, "investigation_planner"),
        model=_MODEL,
        record_step=accounting.step_sink(),
    )
    return transition(_investigating(budget, now), now)


class TestOneDefinitionOfTokenVolume:
    """The ledger's volume and the accounting record's are the same arithmetic.

    Two copies of "input + output + cache creation + cache read + discarded"
    is how a breakdown silently stops adding up to the total it breaks down —
    and the breakdown is the thing every strategy comparison is built on.
    """

    def test_the_ledger_delta_is_the_token_volume(self) -> None:
        usage = LLMUsage(
            input_tokens=100,
            output_tokens=50,
            cache_creation_tokens=2_000,
            cache_read_tokens=8_000,
            discarded_attempts=1,
            discarded_max_tokens=4_096,
        )
        after = accrue_llm_usage(_ledger(), usage, _MODEL)
        assert after.tokens_used == token_volume(usage)

    def test_the_record_carries_that_same_volume(self) -> None:
        usage = LLMUsage(input_tokens=100, output_tokens=50, cache_read_tokens=8_000)
        record = LLMCallAccounting.of(
            role="investigation_planner", model=_MODEL, usage=usage, elapsed_ms=7
        )
        assert record.tokens_used == token_volume(usage)


class TestOneCallIsPricedAndTimed:
    def test_the_four_counters_are_kept_apart(self) -> None:
        """Cache creation is billed at 1.25x input and cache read at 0.1x, so
        a breakdown that folds them into 'input' misprices every run."""
        record = LLMCallAccounting.of(
            role="investigation_planner",
            model=_MODEL,
            usage=LLMUsage(
                input_tokens=100,
                output_tokens=50,
                cache_creation_tokens=2_000,
                cache_read_tokens=8_000,
            ),
            elapsed_ms=0,
        )
        assert record.input_tokens == 100
        assert record.output_tokens == 50
        assert record.cache_creation_tokens == 2_000
        assert record.cache_read_tokens == 8_000

    def test_the_cost_is_the_pricing_module_s_answer(self) -> None:
        """Never re-derived here: ``cost_of`` is the one priced seam."""
        usage = LLMUsage(input_tokens=1_000, output_tokens=500, cache_read_tokens=9_000)
        record = LLMCallAccounting.of(
            role="briefing_writer", model=_MODEL, usage=usage, elapsed_ms=0
        )
        assert record.usd_used == cost_of(_MODEL, usage)

    def test_an_unpriced_model_bills_at_the_class_ceiling_not_zero(self) -> None:
        """An unpinned model id is an operator error, not a free call."""
        usage = LLMUsage(input_tokens=1_000, output_tokens=1_000)
        record = LLMCallAccounting.of(
            role="investigation_planner", model="claude-unpinned-9", usage=usage, elapsed_ms=0
        )
        ceiling = class_ceiling(MODEL_PRICING)
        expected = (
            1_000 * ceiling.input_usd_per_mtok + 1_000 * ceiling.output_usd_per_mtok
        ) / Decimal(1_000_000)
        assert record.usd_used > Decimal("0")
        assert record.usd_used == expected.quantize(Decimal("0.000001"))

    def test_the_call_is_timed(self) -> None:
        accounting = RunAccounting()
        metered = accounting.meter(
            CannedLLMClient([_STOP_STEP]),
            "investigation_planner",
            clock=_StepCountingClock(0.25),
        )
        metered.call(
            system_prompt="s",
            user_message="u",
            output_model=InvestigationStep,
            model=_MODEL,
        )
        assert accounting.calls[0].elapsed_ms == 250

    def test_a_billed_call_that_raises_is_recorded_and_still_raises(self) -> None:
        accounting = RunAccounting()

        class _Boom:
            def call[T: BaseModel](
                self,
                system_prompt: str,
                user_message: str,
                output_model: type[T],
                model: str,
                max_tokens: int = 4096,
                *,
                repair_of: str | None = None,
            ) -> LLMResult[T]:
                raise LLMError("truncated", usage=LLMUsage(input_tokens=90, output_tokens=4_096))

        metered = accounting.meter(_Boom(), "investigation_planner")
        with pytest.raises(LLMError):
            metered.call(
                system_prompt="s",
                user_message="u",
                output_model=InvestigationStep,
                model=_MODEL,
            )
        assert len(accounting.calls) == 1
        assert accounting.calls[0].failed is True
        assert accounting.calls[0].tokens_used == 4_186

    def test_a_failure_carrying_no_usage_records_nothing(self) -> None:
        """The canned client raises usage-free; a guessed charge would be a lie."""
        accounting = RunAccounting()
        metered = accounting.meter(CannedLLMClient([]), "investigation_planner")
        with pytest.raises(LLMError):
            metered.call(
                system_prompt="s",
                user_message="u",
                output_model=InvestigationStep,
                model=_MODEL,
            )
        assert accounting.calls == []


class TestThePerRoleSumEqualsTheLedger:
    """The reconciliation, so the two can never disagree silently.

    ADR 0015 holds an unattended run to ``BudgetLedger``. A per-role
    breakdown that does not add up to it is worse than no breakdown: every
    cost column in every strategy comparison is computed from the split, and
    a split that quietly drops a leg reports one arm as cheaper than it was.
    """

    def test_a_clean_run_reconciles(self, budget: BudgetLedger, now: datetime) -> None:
        accounting = RunAccounting()
        client = CannedLLMClient(
            [_PROBE_STEP, _STOP_STEP],
            usage=CannedUsage(input_tokens=1_200, output_tokens=300, cache_read_tokens=4_000),
        )
        final = _run_investigation(accounting, client, budget, now)
        assert accounting.tokens_used > 0
        assert accounting.reconciles_with(final.budget)

    def test_a_repaired_call_is_counted_once_and_its_billed_tokens_are_included(
        self, budget: BudgetLedger, now: datetime
    ) -> None:
        """ADR 0035: the thrown-away leg is billed work and reaches both sides."""
        accounting = RunAccounting()
        client = _FailsThenSucceeds(
            _STOP_STEP,
            failed_usage=LLMUsage(input_tokens=1_100, output_tokens=64),
            usage=LLMUsage(input_tokens=1_200, output_tokens=300),
        )
        final = _run_investigation(accounting, client, budget, now)
        assert client.calls == 2
        assert [call.failed for call in accounting.calls] == [True, False]
        # 1164 from the leg that was thrown away, 1500 from the one that parsed.
        assert accounting.tokens_used == 2_664
        assert accounting.reconciles_with(final.budget)

    def test_an_evaluator_side_role_is_reported_but_not_reconciled(self) -> None:
        """The briefing judge is the evaluator's spend, never the agent's.

        It is billed, so it is recorded; it never touches ``BudgetLedger``,
        so folding it into the reconciliation would make every run look
        un-reconciled. The row says which side it is on.
        """
        accounting = RunAccounting()
        metered = accounting.meter(
            CannedLLMClient([_STOP_STEP], usage=CannedUsage(input_tokens=500)),
            "briefing_judge",
            charged_to_ledger=False,
        )
        metered.call(
            system_prompt="s",
            user_message="u",
            output_model=InvestigationStep,
            model=_MODEL,
        )
        assert accounting.tokens_used == 500
        assert accounting.charged_tokens_used == 0
        assert [role.charged_to_ledger for role in accounting.roles] == [False]


class TestContextSizeIsMeasuredPerStep:
    def test_the_planner_context_is_non_zero_and_moves_with_the_context(
        self, budget: BudgetLedger, now: datetime
    ) -> None:
        accounting = RunAccounting()
        client = CannedLLMClient(
            [_PROBE_STEP, _STOP_STEP],
            usage=CannedUsage(input_tokens=1_200, cache_read_tokens=4_000),
        )
        _run_investigation(accounting, client, budget, now)
        assert len(accounting.steps) == 2
        # The provider's own count of what it was fed — input + cache read +
        # cache creation, because the system prompt is cached.
        assert accounting.planner_input_tokens == (5_200, 5_200)
        # The same context measured locally. Step two was handed step one's
        # probe result on top of everything step one saw.
        first, second = accounting.planner_context_chars
        assert first > 0
        assert second > first

    def test_a_canned_run_reports_an_honest_zero_for_the_provider_count(
        self, budget: BudgetLedger, now: datetime
    ) -> None:
        """The fake client bills nothing, and 0 is the true number it charged.

        Which is why the character measurement exists beside it (divergence
        D1): the offline suite would otherwise have nothing to say about how
        much context its planner saw.
        """
        accounting = RunAccounting()
        _run_investigation(accounting, CannedLLMClient([_STOP_STEP]), budget, now)
        assert accounting.planner_input_tokens == (0,)
        assert accounting.planner_context_chars[0] > 0


class TestBaselineRecordsTheStrategyDimensionsAsZero:
    """Zero, never absent — the control group's row has to be comparable.

    A best-of-N strategy's report will carry a selector-call count and a
    branch count. If ``baseline`` omitted them, every comparison would have
    to decide what a missing key means, and the cheapest wrong answer
    ("treat it as zero") is indistinguishable from the right one until a
    strategy that genuinely records nothing arrives.
    """

    def test_baseline_records_both_as_zero(self, budget: BudgetLedger, now: datetime) -> None:
        accounting = RunAccounting()
        _run_investigation(accounting, CannedLLMClient([_PROBE_STEP, _STOP_STEP]), budget, now)
        assert accounting.selector_calls == 0
        assert accounting.branch_count == 0
        assert len(accounting.steps) == 2


def _eval_settings(**overrides: Any) -> Settings:
    defaults: dict[str, Any] = {
        "anthropic_api_key": SecretStr("eval"),
        "judge_model": "claude-haiku-4-5",
        "platform_mcp_url": "https://eval.local",
        "platform_rest_url": "https://eval.local",
        "platform_token": SecretStr("eval"),
        "platform_chaos_token": SecretStr("eval-chaos"),
        "platform_webhook_secret": SecretStr("eval"),
        "database_url": "postgresql://eval:eval@localhost:5432/eval",
    }
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)  # type: ignore[call-arg]


def _accounted_scenario(name: str = "accounting_probe") -> Scenario:
    """Two planner steps, one probe, one briefing — every role that can run."""
    return Scenario(
        name=name,
        alert=AlertPayload(source="platform.kafka", severity="high", group="billing"),
        expectation=ScenarioExpectation(
            name=name,
            expected_terminal_state=IncidentState.ESCALATED,
            expected_evidence_contains=("billing",),
            max_tool_calls=5,
        ),
        canned_tool_responses={
            "get_consumer_lag": ToolResult(
                content=[
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "consumer_group": "billing",
                                "lag": 42,
                                "lag_known": True,
                                "source": "static",
                                "cache_key": "kafka:consumer_lag:worker-dispatcher",
                            }
                        ),
                    }
                ],
            )
        },
        canned_llm_responses={
            "investigation_planner": [_PROBE_STEP, _STOP_STEP],
            "briefing_writer": [
                {
                    "findings": "billing consumer lag observed at 42 messages",
                    "recommendation": "verify the billing consumer pod",
                }
            ],
        },
    )


class TestTheRunReportCarriesTheAccounting:
    """Divergence D3, the half WP-0.3 left: a report with no cost in it.

    WP-0.3 put the run's own ledger on the row, which answered "what did this
    run spend in total?". It could not answer "on which role?", "over how many
    planner steps?", or "how much context did each step carry?" — and those
    are the columns the accuracy/cost frontier in Phases 6, 9, 12 and 13 is
    drawn from. A number that is only in prose is not derivable from the
    artifacts (divergence D4, the ledger that stopped).
    """

    def _outcome(self) -> ScenarioOutcome:
        return run_scenario(_accounted_scenario(), _eval_settings()).outcome

    def test_every_outcome_carries_a_record(self) -> None:
        assert self._outcome().accounting is not None

    def test_the_roles_that_ran_are_named(self) -> None:
        accounting = self._outcome().accounting
        assert accounting is not None
        assert [role.role for role in accounting.by_role] == [
            "investigation_planner",
            "briefing_writer",
        ]
        assert [role.calls for role in accounting.by_role] == [2, 1]

    def test_the_evaluator_s_own_spend_is_marked_as_such(self) -> None:
        accounting = self._outcome().accounting
        assert accounting is not None
        charged = {role.role: role.charged_to_ledger for role in accounting.by_role}
        assert charged == {"investigation_planner": True, "briefing_writer": False}

    def test_the_tool_calls_and_wall_time_come_from_the_ledger(self) -> None:
        outcome = self._outcome()
        accounting = outcome.accounting
        assert accounting is not None
        assert accounting.tool_calls == outcome.tool_calls_used == 1
        assert accounting.wall_seconds >= 0.0
        assert accounting.reconciled is True

    def test_the_planner_context_is_recorded_per_step_and_in_total(self) -> None:
        accounting = self._outcome().accounting
        assert accounting is not None
        assert accounting.planner_steps == 2
        assert len(accounting.planner_context_chars) == 2
        assert accounting.planner_context_chars_total == sum(accounting.planner_context_chars)
        assert accounting.planner_input_tokens_total == sum(accounting.planner_input_tokens)

    def test_the_strategy_dimensions_are_written_not_omitted(self) -> None:
        """A key present and zero, so a later strategy's row is comparable."""
        accounting = self._outcome().accounting
        assert accounting is not None
        written = json.loads(accounting.model_dump_json())
        assert written["selector_calls"] == 0
        assert written["branch_count"] == 0

    def test_a_crash_that_measured_nothing_says_so_rather_than_reporting_zero(
        self,
    ) -> None:
        """A bare exception carries no measurement; an invented zero would read
        as "this run was free"."""
        result = _crashed_result(
            _accounted_scenario(),
            RuntimeError("platform unreachable"),
            settings=_eval_settings(),
        )
        assert result.outcome.accounting is None

    def test_a_crashed_row_still_reports_what_it_spent(self) -> None:
        """Invariant 9 one layer down: the evidence a crash produced is evidence."""
        accounting = RunAccounting()
        accounting.record_call(
            LLMCallAccounting.of(
                role="investigation_planner",
                model=_MODEL,
                usage=LLMUsage(input_tokens=1_000, output_tokens=200),
                elapsed_ms=120,
            )
        )
        # The checkpoint the run had written when it died — the ledger the
        # record is reconciled against. 1000 input + 200 output on Sonnet is
        # 1200 tokens of volume and $0.006.
        checkpoint = _investigating(
            _ledger().model_copy(
                update={"tokens_used": 1_200, "usd_used": Decimal("0.006"), "tool_calls_used": 3}
            ),
            datetime(2026, 9, 17, tzinfo=UTC),
        )
        crash = ScenarioCrash(RuntimeError("platform unreachable"), (checkpoint,))
        crash.accounting = accounting
        record = _crashed_result(
            _accounted_scenario(), crash, settings=_eval_settings()
        ).outcome.accounting
        assert record is not None
        assert record.tokens_used == 1_200
        assert record.tool_calls == 3
        assert record.reconciled is True
        assert [role.role for role in record.by_role] == ["investigation_planner"]


class TestTheSummaryPrintsTheBill:
    """The console line and the artifact are one value, not two (A-01).

    A cost that only exists inside a JSON file is a cost nobody reads on the
    run that produced it, and the campaign's own ledger stopping (divergence
    D4) is what that looks like a month later.
    """

    def _report(self, *, reconciled: bool = True) -> RunReport:
        accounting = RunAccountingRecord(
            by_role=(
                RoleAccounting(
                    role="investigation_planner",
                    charged_to_ledger=True,
                    calls=2,
                    input_tokens=2_400,
                    output_tokens=600,
                    cache_creation_tokens=0,
                    cache_read_tokens=8_000,
                    discarded_output_tokens=0,
                    tokens_used=11_000,
                    usd_used=Decimal("0.018600"),
                    elapsed_ms=2_400,
                ),
            ),
            llm_calls=2,
            tool_calls=3,
            tokens_used=11_000,
            usd_used=Decimal("0.018600"),
            charged_tokens_used=11_000,
            charged_usd_used=Decimal("0.018600"),
            ledger_tokens_used=11_000,
            ledger_usd_used=Decimal("0.018600"),
            reconciled=reconciled,
        )
        outcome = ScenarioOutcome(
            scenario="accounting_probe",
            final_state=IncidentState.ESCALATED,
            tool_calls_used=3,
            report=GradeReport(
                scenario="accounting_probe",
                passed=True,
                dimensions=(
                    DimensionResult(dimension=GradeDimension.OUTCOME, passed=True, detail="ok"),
                ),
            ),
            accounting=accounting,
        )
        return RunReport(
            generated_at=datetime(2026, 9, 17, tzinfo=UTC),
            total=1,
            passed=1,
            failed=0,
            outcomes=(outcome,),
        )

    def test_the_bill_is_printed(self, capsys: pytest.CaptureFixture[str]) -> None:
        _print_summary(self._report())
        out = capsys.readouterr().out
        assert "cost: $0.018600, 11000 tokens, 2 LLM calls, 3 tool calls" in out
        assert "UNRECONCILED" not in out

    def test_a_split_that_does_not_add_up_is_never_silent(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _print_summary(self._report(reconciled=False))
        out = capsys.readouterr().out
        assert "COST UNRECONCILED" in out
        assert "accounting_probe" in out

    def test_a_canned_suite_prints_no_cost_line(self) -> None:
        """Every fake bills nothing; a "$0.000000" line would be noise."""
        outcome = run_scenario(_accounted_scenario(), _eval_settings()).outcome
        assert outcome.accounting is not None
        assert outcome.accounting.usd_used == Decimal("0")
