"""Charge one LLM call to the incident budget ledger (ADR 0015).

Lives here rather than in ``agent/state.py`` because ``state.py``
deliberately stays free of ``llm`` imports: the checkpoint schema must
not depend on the model client.
"""

from __future__ import annotations

from pydantic import BaseModel

from incident_commander.agent.state import BudgetLedger
from incident_commander.llm.client import LLMResult
from incident_commander.llm.pricing import cost_of


def accrue_llm_usage[T: BaseModel](
    budget: BudgetLedger, result: LLMResult[T], model: str
) -> BudgetLedger:
    """Return ``budget`` with this call's tokens and dollars added.

    ``tokens_used`` is total token *volume* — input + output +
    cache_creation + cache_read. ``client.py`` caches the system prompt,
    so on a live run most input volume arrives on the cache counters;
    summing only input+output metered the un-cached remainder and
    under-enforced ``BUDGET_MAX_TOKENS`` exactly when caching worked
    well (C-06). ``usd_used`` carries the cost weighting, so the volume
    meter stays a volume meter (ADR 0015).
    """
    return budget.model_copy(
        update={
            "tokens_used": (
                budget.tokens_used
                + result.input_tokens
                + result.output_tokens
                + result.cache_creation_tokens
                + result.cache_read_tokens
            ),
            "usd_used": budget.usd_used + cost_of(model, result),
        }
    )
