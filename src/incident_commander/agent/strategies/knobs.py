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
