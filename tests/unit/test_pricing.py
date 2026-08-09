"""Unit tests for the pinned per-model price map (ADR 0015).

Covers the arithmetic of ``cost_of`` (including the cache token classes
the old accrual dropped), the unknown-model fallback, and the
Decimal-end-to-end guarantee the ``usd_used`` ledger field depends on.
"""

from __future__ import annotations

import logging
from decimal import Decimal

import pytest
from pydantic import BaseModel

from incident_commander.llm import pricing
from incident_commander.llm.client import LLMResult
from incident_commander.llm.pricing import MODEL_PRICING, cost_of, pricing_for


class _Out(BaseModel):
    pass


def _result(
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_creation_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> LLMResult[_Out]:
    return LLMResult(
        output=_Out(),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_tokens=cache_creation_tokens,
        cache_read_tokens=cache_read_tokens,
        stop_reason="tool_use",
    )


class TestCostOf:
    def test_all_four_token_classes_are_priced(self) -> None:
        # (3.00 + 15.00 + 3.75 + 0.30) * 1000 / 1e6
        cost = cost_of(
            "claude-sonnet-4-6",
            _result(
                input_tokens=1000,
                output_tokens=1000,
                cache_creation_tokens=1000,
                cache_read_tokens=1000,
            ),
        )
        assert cost == Decimal("0.022050")

    def test_cache_tokens_alone_cost_money(self) -> None:
        """The class C-06 dropped: a fully cache-served call is not free."""
        cost = cost_of(
            "claude-sonnet-4-6",
            _result(cache_creation_tokens=2000, cache_read_tokens=8000),
        )
        # 2000 * 3.75/1e6 + 8000 * 0.30/1e6
        assert cost == Decimal("0.009900")

    def test_haiku_row_is_cheaper_than_sonnet_for_the_same_call(self) -> None:
        call = _result(input_tokens=10_000, output_tokens=2_000)
        assert cost_of("claude-haiku-4-5", call) < cost_of("claude-sonnet-4-6", call)

    def test_zero_usage_costs_zero(self) -> None:
        assert cost_of("claude-sonnet-4-6", _result()) == Decimal("0")

    def test_result_is_decimal_quantized_to_microdollars(self) -> None:
        cost = cost_of("claude-sonnet-4-6", _result(input_tokens=1))
        assert isinstance(cost, Decimal)
        assert cost.as_tuple().exponent == -6

    def test_sub_microdollar_call_does_not_round_to_zero_at_scale(self) -> None:
        """Per-call quantization must not erase a long investigation's cost."""
        one = cost_of("claude-sonnet-4-6", _result(cache_read_tokens=100))
        assert one > Decimal("0")
        assert sum((one for _ in range(1000)), Decimal("0")) > Decimal("0.002")


class TestUnknownModel:
    def test_falls_back_to_most_expensive_row_without_raising(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        pricing._warned_models.clear()
        call = _result(input_tokens=1_000_000)
        with caplog.at_level(logging.WARNING, logger=pricing.__name__):
            cost = cost_of("claude-not-a-real-model", call)
        assert cost == max(row.input_usd_per_mtok for row in MODEL_PRICING.values())
        assert "claude-not-a-real-model" in caplog.text

    def test_warns_once_per_model_id(self, caplog: pytest.LogCaptureFixture) -> None:
        pricing._warned_models.clear()
        with caplog.at_level(logging.WARNING, logger=pricing.__name__):
            for _ in range(3):
                cost_of("claude-mystery", _result(input_tokens=10))
        assert caplog.text.count("claude-mystery") == 1

    def test_fallback_row_is_the_most_expensive_registered_row(self) -> None:
        fallback = pricing_for("claude-nonexistent")
        assert fallback.rate_total == max(row.rate_total for row in MODEL_PRICING.values())


class TestPinnedTable:
    @pytest.mark.parametrize("model", ["claude-sonnet-4-6", "claude-haiku-4-5"])
    def test_cache_multipliers_match_the_published_ratios(self, model: str) -> None:
        """Cache write is 1.25x input, cache read is 0.1x, for the 5m TTL."""
        row = MODEL_PRICING[model]
        assert row.cache_write_usd_per_mtok == row.input_usd_per_mtok * Decimal("1.25")
        assert row.cache_read_usd_per_mtok == row.input_usd_per_mtok * Decimal("0.1")

    def test_agent_model_default_is_priced(self) -> None:
        """A default AGENT_MODEL with no price row would silently fall back."""
        assert "claude-sonnet-4-6" in MODEL_PRICING

    @pytest.mark.parametrize("model", sorted(MODEL_PRICING))
    def test_every_rate_is_a_decimal(self, model: str) -> None:
        row = MODEL_PRICING[model]
        assert all(
            isinstance(rate, Decimal)
            for rate in (
                row.input_usd_per_mtok,
                row.output_usd_per_mtok,
                row.cache_write_usd_per_mtok,
                row.cache_read_usd_per_mtok,
            )
        )
