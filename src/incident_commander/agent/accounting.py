"""Charge one LLM call to the incident budget ledger (ADR 0015).

Lives here rather than in ``agent/state.py`` because ``state.py``
deliberately stays free of ``llm`` imports: the checkpoint schema must
not depend on the model client.
"""

from __future__ import annotations

from incident_commander.agent.state import BudgetLedger
from incident_commander.llm.client import LLMError, LLMUsage
from incident_commander.llm.pricing import cost_of


def accrue_llm_usage(budget: BudgetLedger, usage: LLMUsage, model: str) -> BudgetLedger:
    """Return ``budget`` with this call's tokens and dollars added.

    ``tokens_used`` is total token *volume* — input + output +
    cache_creation + cache_read. ``client.py`` caches the system prompt,
    so on a live run most input volume arrives on the cache counters;
    summing only input+output metered the un-cached remainder and
    under-enforced ``BUDGET_MAX_TOKENS`` exactly when caching worked
    well (C-06). ``usd_used`` carries the cost weighting, so the volume
    meter stays a volume meter (ADR 0015).

    ``discarded_output_tokens`` joins the volume for the same reason it
    joins the dollars: a retried attempt spent real capacity, and a
    ceiling that cannot see it is not a ceiling.
    """
    return budget.model_copy(
        update={
            "tokens_used": (
                budget.tokens_used
                + usage.input_tokens
                + usage.output_tokens
                + usage.cache_creation_tokens
                + usage.cache_read_tokens
                + usage.discarded_output_tokens
            ),
            "usd_used": budget.usd_used + cost_of(model, usage),
        }
    )


def accrue_llm_error(budget: BudgetLedger, err: Exception, model: str) -> BudgetLedger:
    """Charge whatever a *failed* LLM call already billed. No-op if unknown.

    The transitions turn an ``LLMError`` into a graded escalation rather
    than a crash, which is right — but it made the failure free. A
    truncated response and three retried 5xx are both billed work that
    ended in an ``except`` arm, and neither reached ``tokens_used`` or
    ``usd_used``. Callers pass the exception they caught; anything that is
    not an ``LLMError`` carrying usage (a ``ValueError`` from the canned
    client, a pydantic ``ValidationError``) leaves the ledger untouched.
    """
    if not isinstance(err, LLMError) or err.usage is None:
        return budget
    return accrue_llm_usage(budget, err.usage, model)
