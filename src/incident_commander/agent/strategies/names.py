"""The configurable strategy names — a closed set, on purpose.

Deliberately importing nothing: ``config.Settings`` types
``INFERENCE_STRATEGY`` as this enum, so this module has to be reachable from
``config`` without dragging in the strategies themselves (``baseline`` imports
``agent.investigation``, which imports ``tools.mcp_client``, which imports
``config``).
"""

from __future__ import annotations

from enum import StrEnum


class StrategyName(StrEnum):
    """Every value ``INFERENCE_STRATEGY`` accepts.

    ``baseline`` is the control group: the current behaviour with the planner
    call moved behind a seam and nothing else changed, so a report that names
    ``baseline`` names the loop the eight green live runs were made with (plan
    04 working rule 5).

    A value nothing can produce is a value a reader has to guess the meaning of.
    Plan 02 § 4 lists seven names for Phases 5, 6, 9, 12 and 13; each lands as a
    member here *with* its implementation and its registry entry, never in
    advance — the same reasoning ``evals.runner.ExecutionMode`` uses for
    withholding ``recorded`` until WP-3.3 can produce one. Three members today:
    the control group and Phase 5's two best-of-N arms.

    ``StrEnum``, so a settings field typed as this serializes as the plain
    string into the run's provenance record and a stamped value read back from
    JSON compares equal to the member.
    """

    BASELINE = "baseline"
    #: WP-5.2, plan 02 § 11.1: one planner call asking for N distinct candidate
    #: diagnoses. Input unchanged, output ~N×; it measures enumeration, which is
    #: a different thing from the literature's pass@k (decision D4).
    BEST_OF_N_ENUMERATED = "best_of_n_enumerated"
    #: WP-5.3, plan 02 § 11.2: N independent planner calls at a configured
    #: temperature, the candidate set being the union of their top hypotheses.
    #: N× planner cost, and the literature's pass@k — the other half of D4.
    BEST_OF_N_SAMPLED = "best_of_n_sampled"
