"""Pinned per-model token prices for the USD budget meter (ADR 0015, ADR 0011).

USD per million tokens. Add four rates, verified against docs.claude.com, for any new
AGENT_MODEL / JUDGE_MODEL / DEVELOPMENT_MODEL / BENCHMARK_MODEL; startup refuses an unpriced id.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:  # pragma: no cover - typing only
    # Imported for type checking only. At runtime this line would pull in the Anthropic SDK,
    # which nothing in the cost meter needs and offline tooling should not have to install.
    from incident_commander.llm.client import LLMUsage

_LOG: Final = logging.getLogger(__name__)

_TOKENS_PER_MILLION: Final[Decimal] = Decimal(1_000_000)
# Costs are rounded to millionths of a dollar, not to cents: a whole cheap investigation can come
# to less than one cent, and rounding to cents would report it as free.
_USD_QUANTUM: Final[Decimal] = Decimal("0.000001")


@dataclass(frozen=True, slots=True)
class ModelPricing:
    """USD per million tokens for one model id, by token class."""

    input_usd_per_mtok: Decimal
    output_usd_per_mtok: Decimal
    cache_write_usd_per_mtok: Decimal
    cache_read_usd_per_mtok: Decimal


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

#: The shortest prefix each priced model will cache, in tokens (docs.claude.com prompt caching,
#: checked 2026-10-12). A marked prefix shorter than this is not an error: the call is billed as
#: uncached input and the usage block shows no cache tokens. Haiku 4.5's 4,096 is above the
#: briefing judge's ~3,300-token rubric, which is why ADR 0084 also marks the question.
CACHE_MINIMUM_TOKENS: Final[dict[str, int]] = {
    "claude-sonnet-4-6": 1024,
    "claude-haiku-4-5": 4096,
}

_warned_models: set[str] = set()


def class_ceiling(table: Mapping[str, ModelPricing]) -> ModelPricing:
    """A synthetic row that is at least as expensive as every row, per class.

    NOT a registered row: the priciest *registered* row is not an upper bound, so the
    per-class maximum makes ADR 0015's never-under-report guarantee true by construction.
    """
    rows = tuple(table.values())
    if not rows:
        raise ValueError("no registered price rows; cannot bound an unknown model")
    return ModelPricing(
        input_usd_per_mtok=max(row.input_usd_per_mtok for row in rows),
        output_usd_per_mtok=max(row.output_usd_per_mtok for row in rows),
        cache_write_usd_per_mtok=max(row.cache_write_usd_per_mtok for row in rows),
        cache_read_usd_per_mtok=max(row.cache_read_usd_per_mtok for row in rows),
    )


def pricing_for(model: str) -> ModelPricing:
    """Price row for ``model``, falling back to the per-class ceiling.

    An unpinned id (a changed ``AGENT_MODEL`` with no price row) is an operator error, but
    raising would abort a live run, so it is charged the per-class maximum and warned once.
    """
    row = MODEL_PRICING.get(model)
    if row is not None:
        return row
    if model not in _warned_models:
        _warned_models.add(model)
        _LOG.warning(
            "no pinned price row for model %r; billing at the per-class maximum "
            "of every registered row. Add it to MODEL_PRICING (see ADR 0015).",
            model,
        )
    return class_ceiling(MODEL_PRICING)


def cost_of(model: str, usage: LLMUsage) -> Decimal:
    """USD cost of one logical LLM call, quantized to microdollars.

    Takes ``LLMUsage``, not ``LLMResult``, so billed paths with no parsed output price the
    same way; ``discarded_output_tokens`` bills at the output rate, never under (ADR 0015).
    A call that billed no token of any class costs nothing at any price, so it is not looked up:
    a scripted fake under a made-up model id is not an unpriced model (WO-R3-384).
    """
    if not (
        usage.input_tokens
        or usage.output_tokens
        or usage.discarded_output_tokens
        or usage.cache_creation_tokens
        or usage.cache_read_tokens
    ):
        return Decimal("0").quantize(_USD_QUANTUM)
    row = pricing_for(model)
    raw = (
        usage.input_tokens * row.input_usd_per_mtok
        + (usage.output_tokens + usage.discarded_output_tokens) * row.output_usd_per_mtok
        + usage.cache_creation_tokens * row.cache_write_usd_per_mtok
        + usage.cache_read_tokens * row.cache_read_usd_per_mtok
    ) / _TOKENS_PER_MILLION
    return raw.quantize(_USD_QUANTUM, rounding=ROUND_HALF_UP)
