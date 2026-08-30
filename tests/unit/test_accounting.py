"""Billed LLM work reaches the ledger on every path, happy or not.

ADR 0015's rule is one-directional: the meter may over-report but never
under-report, because ``BUDGET_MAX_USD`` is what bounds an unattended paid
run. These tests pin the two halves of that rule the client can express —
the attempts that were billed and discarded, and the calls that were billed
and then raised.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel

from incident_commander.agent.accounting import accrue_llm_error, accrue_llm_usage
from incident_commander.agent.state import BudgetLedger
from incident_commander.llm.client import LLMError, LLMResult, LLMUsage

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
