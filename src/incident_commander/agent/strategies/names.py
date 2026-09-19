"""The configurable strategy names — a closed set, on purpose.

Imports nothing, so ``config.Settings`` can type ``INFERENCE_STRATEGY`` as this enum without
dragging a strategy in.
"""

from __future__ import annotations

from enum import StrEnum


class StrategyName(StrEnum):
    """Every value ``INFERENCE_STRATEGY`` accepts.

    ``baseline`` is the control group (plan 04 working rule 5). A member lands only *with* its
    implementation and its registry entry, never in advance. ``StrEnum``, so the value
    serializes as a plain string into the run's provenance record.
    """

    BASELINE = "baseline"
    #: WP-5.2, plan 02 § 11.1: one planner call asking for N distinct diagnoses. Measures
    #: enumeration, not the literature's pass@k (decision D4).
    BEST_OF_N_ENUMERATED = "best_of_n_enumerated"
    #: WP-5.3, plan 02 § 11.2: N independent planner calls at a configured temperature, the set
    #: being the union of their top hypotheses. N× planner cost, and the pass@k half of D4.
    BEST_OF_N_SAMPLED = "best_of_n_sampled"
    #: WP-6.2, plan 02 § 12: a generator arm plus a ``candidate_selector`` call over its set.
    #: The arm is ``(generator, N, selector)``, so ``strategy_config`` stamps all three.
    CANDIDATE_SELECTOR = "candidate_selector"
    #: WP-9.1, plan 02 § 13: ``baseline``'s call, one critique of it, at most one revision.
    #: One pass per step, capped in code rather than in the critic's instructions.
    REFLECTION = "reflection"
    #: WP-12.1, plan 02 § 14: a bounded walk over evidence-gathering decisions — depth ≤ 2,
    #: branch ≤ 3, one shared ledger, RECORDED mode only (a live world would move under it).
    SEARCH = "search"
    #: WP-13.2, plan 02 § 15: the ladder. ``baseline`` on an easy step, and a rung above it for
    #: each of WP-13.1's escalation signals that fired on the rung below (ADR 0064).
    ADAPTIVE = "adaptive"
