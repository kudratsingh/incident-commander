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

    One member today, and that is the point twice over:

    * It is the control group. ``baseline`` is the current behaviour with the
      planner call moved behind a seam and nothing else changed, so a report
      that names ``baseline`` names the loop the eight green live runs were
      made with (plan 04 working rule 5).
    * A value nothing can produce is a value a reader has to guess the meaning
      of. Plan 02 § 4 lists seven names for Phases 5, 6, 9, 12 and 13; each
      lands as a member here *with* its implementation and its registry entry,
      never in advance — the same reasoning ``evals.runner.ExecutionMode`` uses
      for withholding ``recorded`` until WP-3.3 can produce one.

    ``StrEnum``, so a settings field typed as this serializes as the plain
    string into the run's provenance record and a stamped value read back from
    JSON compares equal to the member.
    """

    BASELINE = "baseline"
