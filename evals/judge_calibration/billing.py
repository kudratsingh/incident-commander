"""What a calibration was billed: the API's own usage blocks, summed, priced (WO-R3-384, O-62).

The owner's rule (O-62): "record the billed figure, not the estimate". Every call a calibration
makes passes through ``agent.accounting.MeteredLLMClient``, the same meter a run's judge goes
through, so each call's input, output, cache-write and cache-read tokens arrive exactly as the
API reported them, a failed-but-billed call included (ADR 0015). This module adds them up per leg
and per model and prices them with ``llm/pricing.cost_of``, the one price map the budget meter
uses: a cache read is charged at the cache-read rate and a cache write at the cache-write rate.

"Billed" here means: the usage the API returned, times the pinned price map. It is not the
Anthropic console's invoice (that is the authority, and it can differ by rounding or by a price
change the map has not caught up with), but it is no longer a token estimate made beforehand.
A report written before this module has no ``usage`` block and reads ``usd_used: null``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal
from typing import Any, Final

from incident_commander.agent.accounting import LLMCallAccounting
from incident_commander.llm.pricing import CACHE_MINIMUM_TOKENS

#: The leg names a calibration's calls are metered under, in the order the legs run.
LABEL_LEG: Final[str] = "labels"
TRAP_LEG: Final[str] = "traps"

#: What "billed" means, in the report, so a reader does not take it for the invoice.
BILLED_MEANS: Final[str] = (
    "the API's own usage block for every call this calibration made (input, output, cache-write "
    "and cache-read tokens; a billed call that then failed included), priced by "
    "llm/pricing.MODEL_PRICING — cache reads at the cache-read rate, cache writes at the "
    "cache-write rate. Not an estimate made before the run; the Anthropic console stays the "
    "authority on the invoice (owner O-62, WO-R3-384)"
)


def _usd(value: Decimal) -> float:
    """Dollars as a JSON number, to the microdollar the price map quantizes to."""
    return float(value)


def _totals(calls: Sequence[LLMCallAccounting]) -> dict[str, Any]:
    input_tokens = sum(call.input_tokens for call in calls)
    cache_read = sum(call.cache_read_tokens for call in calls)
    cache_write = sum(call.cache_creation_tokens for call in calls)
    prompt_tokens = input_tokens + cache_read + cache_write
    return {
        "calls": len(calls),
        "failed_calls": sum(call.failed for call in calls),
        "input_tokens": input_tokens,
        "output_tokens": sum(call.output_tokens for call in calls),
        "cache_write_tokens": cache_write,
        "cache_read_tokens": cache_read,
        "discarded_output_tokens": sum(call.discarded_output_tokens for call in calls),
        # The share of every prompt token that was served from the cache.
        "cache_read_share": None if prompt_tokens == 0 else round(cache_read / prompt_tokens, 4),
        "usd_used": _usd(sum((call.usd_used for call in calls), Decimal("0"))),
    }


def usage_block(calls: Iterable[LLMCallAccounting], *, question_cached: bool) -> dict[str, Any]:
    """Every call of one calibration, summed: in total, per leg and per model.

    ``question_cached`` says whether the live client marked the question as well as the rubric
    (``harness.live_client``, ADR 0084), so a cache-read share can be read against the request
    shape that produced it.
    """
    recorded = list(calls)
    legs: dict[str, list[LLMCallAccounting]] = {}
    models: dict[str, list[LLMCallAccounting]] = {}
    for call in recorded:
        legs.setdefault(call.role, []).append(call)
        models.setdefault(call.model, []).append(call)
    return {
        **_totals(recorded),
        "by_leg": {leg: _totals(rows) for leg, rows in legs.items()},
        "by_model": {
            model: {**_totals(rows), "cache_minimum_tokens": CACHE_MINIMUM_TOKENS.get(model)}
            for model, rows in models.items()
        },
        "cache_markers": {"rubric": True, "question": question_cached},
        "means": BILLED_MEANS,
    }


def billed_line(usage: Mapping[str, Any] | None) -> str:
    """``billed $X.XX (N calls, cache-read P%)``; a report without usage says so."""
    if usage is None:
        return "billed n/a (report predates WO-R3-384: no usage recorded)"
    share = usage["cache_read_share"]
    cached = "n/a" if share is None else f"{share * 100:.0f}%"
    return f"billed ${usage['usd_used']:.2f} ({usage['calls']} calls, cache-read {cached})"


def billed_of(report: Mapping[str, Any]) -> dict[str, Any]:
    """The billed figure of any calibration report JSON, old or new.

    A report written before WO-R3-384 carries no ``usage`` and reads ``usd_used: None`` — never
    ``0``, which would claim the run was free.
    """
    usage = report.get("usage")
    return {
        "report_id": report.get("report_id"),
        "usd_used": report.get("usd_used") if usage is not None else None,
        "usage": usage,
    }
