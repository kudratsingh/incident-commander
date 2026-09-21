"""``StrategyKnobs`` — the inference block a strategy is built with (WP-5.2).

Wired at the edge: ``evals/runner.py`` reads ``Settings`` and hands one of these to
``STRATEGIES.create``, so nothing under ``agent/`` calls ``get_settings()``. Defaults are the
control group's, so a missing knobs block is never a silent change of arm.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True, kw_only=True)
class StrategyKnobs:
    """N and the sampling temperature: the knobs a strategy is built with.

    The budget multipliers are deliberately NOT here — read once in ``config.py``, where
    ``start_run`` seeds the ledger; ``TestNoOtherCallSiteScalesABudget`` refuses a second reader.
    """

    #: How many competing diagnoses a step asks the planner for. One is the control group's
    #: shape; the arms that compare candidates use 2, 4 or 8.
    n: int = 1
    #: The sampling temperature for arms that take several independent samples. ``None`` means
    #: send no temperature at all, leaving the provider's default, as every other call does.
    sample_temperature: float | None = None
    #: Which generating arm a selector chooses between the candidates of. A plain string, because
    #: this module imports nothing from its own package; the registry turns it into an object.
    selector_generator: str = "best_of_n_enumerated"
    #: How deep and how wide a search walk may go. These are requests only: the real maximums
    #: live in ``agent/search.py``, which refuses anything larger, and these match them.
    search_depth: int = 2
    search_branch: int = 3
    #: One optional override per uncertainty threshold the ladder compares against; ``None`` takes
    #: the declared default. A field each, not a mapping, so a misspelling cannot pass unnoticed.
    uncertainty_top1_confidence_floor: float | None = None
    uncertainty_top1_top2_margin_floor: float | None = None
    uncertainty_selector_uncertainty_ceiling: float | None = None
    uncertainty_candidate_disagreement_ceiling: float | None = None
    uncertainty_confidence_floor_after_probes: float | None = None
    uncertainty_contradictory_evidence_count: int | None = None
    uncertainty_failed_attempt_count: int | None = None
    uncertainty_probe_count_before_confidence_check: int | None = None
