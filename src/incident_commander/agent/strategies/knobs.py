"""``StrategyKnobs`` — the inference block a strategy is built with.

Configuration is wired at the edge in this repo: nothing under ``agent/``
calls ``get_settings()``, and the investigation loop already takes its model,
its budgets and its re-probe knobs as parameters. ``baseline`` needed none of
this, so ``StrategyRegistry`` built strategies from zero-argument factories.
WP-5.2 is the first strategy with a knob — N — and the question is where N
enters.

It enters here: ``evals/runner.py`` reads ``Settings``, builds one of these,
and hands it to ``STRATEGIES.create``. The alternative — a strategy that reads
``Settings`` itself — would put configuration inside the thing whose whole
value is being comparable across configurations, and would make a strategy
untestable without an environment.

Deliberately importing nothing from this package, for the same reason
``names.py`` imports nothing: it is reachable from the edge without dragging a
strategy (and so the investigation loop, and so the MCP client) in behind it.

Defaults are the control group's. ``StrategyKnobs()`` is what ``baseline``
runs on and what every strategy falls back to when a caller builds one without
configuration, so a missing knobs block is never a silent change of arm.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True, kw_only=True)
class StrategyKnobs:
    """N and the sampling temperature: the knobs a strategy is built with.

    The budget multipliers are deliberately NOT here. They are read in
    ``config.py`` and applied once, where ``agent/factory.py::start_run`` seeds
    the ledger, and ``tests/unit/test_budgets.py::TestNoOtherCallSiteScalesABudget``
    refuses a second reader anywhere under ``src/``, ``evals/`` or ``scripts/``.
    An earlier draft of WP-5.2 carried the token ratio through here so the arm
    could stamp it into ``strategy_config``; that would have been a second
    reader of a number whose whole guarantee is that it has one, and the guard
    caught it. A BUDGET result for an N-arm is read beside ``strategy_config.n``
    and the seeded ledger the provenance record already carries (decision C4).
    """

    #: How many candidate diagnoses the strategy generates per planner step
    #: (plan 02 § 11, N ∈ {1, 2, 4, 8}). 1 is the control group's shape: one
    #: diagnosis considered, none enumerated behind it.
    n: int = 1
    #: Sampling temperature for the strategies that draw several independent
    #: samples (plan 02 § 11.2). ``None`` means "do not send one", which is
    #: what every call in this repo has always done — the provider's default.
    sample_temperature: float | None = None
