"""Pinned per-model token prices for the USD budget meter (ADR 0015).

Prices are configuration pinned per ADR 0011; verify against
docs.claude.com when AGENT_MODEL/JUDGE_MODEL change — the same rule
CLAUDE.md already applies to the model id strings themselves.

The map is a committed constant, never a runtime lookup: offline eval
runs must not need network, and a run's reported cost must be
reproducible from the checkout alone.

Rates are USD per million tokens, verified against docs.claude.com
2026-08 for the 5-minute ephemeral cache TTL that ``llm/client.py``
applies to the system prompt — cache write is 1.25x input, cache read
is 0.1x input.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

from pydantic import BaseModel

from incident_commander.llm.client import LLMResult

_LOG: Final = logging.getLogger(__name__)

_TOKENS_PER_MILLION: Final[Decimal] = Decimal(1_000_000)
# Sub-cent resolution: a single planner call on a cached system prompt can
# cost well under $0.001, and truncating those to cents would make the meter
# read zero for an entire investigation.
_USD_QUANTUM: Final[Decimal] = Decimal("0.000001")


@dataclass(frozen=True, slots=True)
class ModelPricing:
    """USD per million tokens for one model id, by token class."""

    input_usd_per_mtok: Decimal
    output_usd_per_mtok: Decimal
    cache_write_usd_per_mtok: Decimal
    cache_read_usd_per_mtok: Decimal

    @property
    def rate_total(self) -> Decimal:
        """Sum of the four rates. Orders rows for the unknown-model fallback."""
        return (
            self.input_usd_per_mtok
            + self.output_usd_per_mtok
            + self.cache_write_usd_per_mtok
            + self.cache_read_usd_per_mtok
        )


MODEL_PRICING: Final[dict[str, ModelPricing]] = {
    "claude-sonnet-4-6": ModelPricing(
        input_usd_per_mtok=Decimal("3.00"),
        output_usd_per_mtok=Decimal("15.00"),
        cache_write_usd_per_mtok=Decimal("3.75"),
        cache_read_usd_per_mtok=Decimal("0.30"),
    ),
    "claude-haiku-4-5": ModelPricing(
        input_usd_per_mtok=Decimal("1.00"),
        output_usd_per_mtok=Decimal("5.00"),
        cache_write_usd_per_mtok=Decimal("1.25"),
        cache_read_usd_per_mtok=Decimal("0.10"),
    ),
}

# Ties broken by model id so the fallback row is deterministic across runs.
_FALLBACK_MODEL: Final[str] = max(
    MODEL_PRICING, key=lambda name: (MODEL_PRICING[name].rate_total, name)
)

_warned_models: set[str] = set()


def pricing_for(model: str) -> ModelPricing:
    """Price row for ``model``, falling back to the most expensive row.

    An unpinned model id is an operator error (a changed ``AGENT_MODEL``
    without a matching price row), but raising here would abort a live
    incident run over an accounting gap. Charging the most expensive
    known rate keeps the USD ceiling conservative — the meter can
    over-report, never silently under-report — and warns once per id.
    """
    row = MODEL_PRICING.get(model)
    if row is not None:
        return row
    if model not in _warned_models:
        _warned_models.add(model)
        _LOG.warning(
            "no pinned price row for model %r; billing at the most expensive "
            "row (%r). Add it to MODEL_PRICING (see ADR 0015).",
            model,
            _FALLBACK_MODEL,
        )
    return MODEL_PRICING[_FALLBACK_MODEL]


def cost_of[T: BaseModel](model: str, result: LLMResult[T]) -> Decimal:
    """USD cost of one LLM call, quantized to microdollars.

    Decimal end to end: ``BudgetLedger.usd_used`` is a ``Decimal`` and a
    float intermediate would leak binary-rounding noise into every
    briefing that renders it.
    """
    row = pricing_for(model)
    raw = (
        result.input_tokens * row.input_usd_per_mtok
        + result.output_tokens * row.output_usd_per_mtok
        + result.cache_creation_tokens * row.cache_write_usd_per_mtok
        + result.cache_read_tokens * row.cache_read_usd_per_mtok
    ) / _TOKENS_PER_MILLION
    return raw.quantize(_USD_QUANTUM, rounding=ROUND_HALF_UP)
