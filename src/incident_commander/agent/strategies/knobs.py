"""``StrategyKnobs`` — the inference block a strategy is built with (WP-5.2).

Configuration is wired at the edge: ``evals/runner.py`` reads ``Settings``, builds one of these
and hands it to ``STRATEGIES.create``, so nothing under ``agent/`` calls ``get_settings()``.
Imports nothing from this package, so the edge can reach it without dragging a strategy in.
Defaults are the control group's, so a missing knobs block is never a silent change of arm.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True, kw_only=True)
class StrategyKnobs:
    """N and the sampling temperature: the knobs a strategy is built with.

    The budget multipliers are deliberately NOT here — they are read once in ``config.py``,
    where ``agent/factory.py::start_run`` seeds the ledger, and
    ``tests/unit/test_budgets.py::TestNoOtherCallSiteScalesABudget`` refuses a second reader.
    """

    #: Candidate diagnoses generated per planner step (plan 02 § 11, N ∈ {1, 2, 4, 8}).
    #: 1 is the control group's shape.
    n: int = 1
    #: Sampling temperature for the arms that draw independent samples (plan 02 § 11.2).
    #: ``None`` means "do not send one" — the provider's default, as every call here has.
    sample_temperature: float | None = None
    #: Which generator a ``candidate_selector`` decides over (plan 02 § 12, WP-6.2). A plain
    #: ``str`` — this module imports nothing from its own package — resolved by the registry.
    selector_generator: str = "best_of_n_enumerated"
    #: How far and how wide a ``search`` walk may go (plan 02 § 14, WP-12.1). These are
    #: REQUESTS: ``agent/search.py`` holds the structural maximums (2 and 3) and refuses
    #: anything above them at construction, which is why the defaults here are those numbers
    #: written as literals — this module imports nothing, and
    #: ``tests/unit/test_search.py::TestTheBoundsAreStructural`` pins the two spellings equal.
    search_depth: int = 2
    search_branch: int = 3
    #: The ``adaptive`` ladder's escalation thresholds (plan 02 § 15, WP-13.1, WP-13.2), one
    #: optional override each. ``None`` means the default DECLARED in
    #: ``strategies/policy.py`` — the only value with a benchmark split behind it (ADR 0061) —
    #: so this module still declares no number of its own. Spelled out one field per
    #: threshold rather than taken as a mapping, for ``UncertaintyThresholds.resolve``'s
    #: reason: a mapping lets a typo become a knob that is silently ignored.
    uncertainty_top1_confidence_floor: float | None = None
    uncertainty_top1_top2_margin_floor: float | None = None
    uncertainty_selector_uncertainty_ceiling: float | None = None
    uncertainty_candidate_disagreement_ceiling: float | None = None
    uncertainty_confidence_floor_after_probes: float | None = None
    uncertainty_contradictory_evidence_count: int | None = None
    uncertainty_failed_attempt_count: int | None = None
    uncertainty_probe_count_before_confidence_check: int | None = None
