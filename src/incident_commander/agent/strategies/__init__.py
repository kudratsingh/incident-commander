"""Inference strategies for the investigation loop (plan 02 § 4, WP-0.2).

A strategy replaces **exactly one** call: the single planner LLM call the
investigation loop makes, ``agent/investigation.py::_plan_next_step``. Nothing
else moves. The subject-probe refusal, the ``FIX_MAP`` gate, the 0.7 remediate
threshold, the ADR-0009 freshness re-probe, ``max_iterations``, hint routing
and ``_execute_probe``'s tier re-check all stay in ``investigation.py`` and are
shared by every strategy, because they are the execution policy that gates
real actions — and a strategy is a way of *proposing*, never of acting
(``docs/ADR/0036-the-planner-call-is-the-only-strategy-seam.md``).

``baseline`` is the only strategy today and it is the current behaviour: its
``plan_next_step`` calls the existing ``_plan_next_step`` body verbatim. That
is what makes it an honest control group — the canned suite comes out
byte-identical, which is the proof that nothing moved.

**Nothing is re-exported here on purpose.** ``config.Settings`` validates
``INFERENCE_STRATEGY`` against :mod:`.names`, and importing a submodule
imports its package first, so a re-export of :mod:`.baseline` (which imports
``agent.investigation`` → ``tools.mcp_client`` → ``config``) would make that
validation a circular import. Import from the submodule you need:

* :mod:`.names` — ``StrategyName``, the closed set of configurable values.
* :mod:`.records` — ``StepRecord`` and the ``StepSink`` it is written to.
* :mod:`.protocol` — ``InvestigationStrategy`` and ``StrategyContext``.
* :mod:`.baseline` — ``BaselineStrategy``.
* :mod:`.registry` — ``STRATEGIES``, ``default_strategy()``.
"""
