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
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

from incident_commander.llm.client import LLMUsage

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

    NOT a registered row. Selecting the priciest *registered* row — by the
    sum of its four rates, which is how this used to work — is not an upper
    bound: a table can hold a row that is cheaper in total yet dearer in a
    single class, and an unpinned model billed at the sum-winner's rates is
    then metered below its real price in that class. That breaks the one
    guarantee this module and ADR 0015 both state outright, and it breaks it
    silently, which is the part that matters for an unattended paid run.

    Taking the maximum per class instead makes the guarantee true by
    construction for any table anyone writes later, rather than true by
    coincidence for the two rows that happen to be registered today.
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

    An unpinned model id is an operator error (a changed ``AGENT_MODEL``
    without a matching price row), but raising here would abort a live
    incident run over an accounting gap. Charging the per-class maximum
    keeps the USD ceiling conservative — the meter can over-report, never
    silently under-report — and warns once per id.

    Derived from ``MODEL_PRICING`` at call time rather than pinned at import:
    the bound is then a fact about the table as it actually is, and a test
    can prove the property against a table this module does not ship. The
    cost is eight Decimal comparisons on a path that is meant to be rare.
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

    Takes ``LLMUsage`` rather than ``LLMResult`` so the billed paths that
    never produce a parsed output — a truncated response, an exhausted
    retry loop — are priced by the same arithmetic as the happy path.
    ``LLMResult`` is an ``LLMUsage``, so callers holding one still fit.

    ``discarded_output_tokens`` is billed at the output rate: it is a
    conservative stand-in for attempts that generated and were thrown
    away, and the output rate is the dearest of the four classes, so the
    stand-in cannot under-bill them (ADR 0015).

    Decimal end to end: ``BudgetLedger.usd_used`` is a ``Decimal`` and a
    float intermediate would leak binary-rounding noise into every
    briefing that renders it.
    """
    row = pricing_for(model)
    raw = (
        usage.input_tokens * row.input_usd_per_mtok
        + (usage.output_tokens + usage.discarded_output_tokens) * row.output_usd_per_mtok
        + usage.cache_creation_tokens * row.cache_write_usd_per_mtok
        + usage.cache_read_tokens * row.cache_read_usd_per_mtok
    ) / _TOKENS_PER_MILLION
    return raw.quantize(_USD_QUANTUM, rounding=ROUND_HALF_UP)
