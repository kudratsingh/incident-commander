"""Pinned per-model token prices for the USD budget meter (ADR 0015).

Pinned per ADR 0011: verify against docs.claude.com when AGENT_MODEL, JUDGE_MODEL,
DEVELOPMENT_MODEL or BENCHMARK_MODEL change, and add the four rates in the same change —
``config.py::_configured_models_are_priced`` refuses an unpriced id at startup. A committed
constant, not a runtime lookup. USD per million tokens; cache write 1.25x, cache read 0.1x.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:  # pragma: no cover - typing only
    # Type-only: a runtime import would drag the Anthropic SDK into every
    # module that needs a price row (WO-R2-118).
    from incident_commander.llm.client import LLMUsage

_LOG: Final = logging.getLogger(__name__)

_TOKENS_PER_MILLION: Final[Decimal] = Decimal(1_000_000)
# Sub-cent resolution: cents would make the meter read zero for a
# whole investigation.
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
    """
    row = pricing_for(model)
    raw = (
        usage.input_tokens * row.input_usd_per_mtok
        + (usage.output_tokens + usage.discarded_output_tokens) * row.output_usd_per_mtok
        + usage.cache_creation_tokens * row.cache_write_usd_per_mtok
        + usage.cache_read_tokens * row.cache_read_usd_per_mtok
    ) / _TOKENS_PER_MILLION
    return raw.quantize(_USD_QUANTUM, rounding=ROUND_HALF_UP)
