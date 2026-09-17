"""The seam itself: ``InvestigationStrategy`` and ``StrategyContext``.

Written as plan 02 § 4 writes it. One method, one call replaced:

.. code-block:: python

    class InvestigationStrategy(Protocol):
        name: str
        def plan_next_step(
            self, run_state: RunState, at: datetime, ctx: StrategyContext
        ) -> tuple[RunState, InvestigationStep, StepRecord]: ...

What is *not* here is the point of the packet. A strategy returns a proposal
and a record; it never calls a tool, never reads a tier, never consults
``FIX_MAP``, and never decides that an action is allowed. The same execution
policy gates every real action for every strategy (plan 02 § 2), and
``tests/unit/test_strategies.py::TestStrategiesHoldNoExecutionPolicy`` refuses
any import or reference that would move a piece of it in here.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from incident_commander.agent.hypothesis import InvestigationStep
from incident_commander.agent.state import RunState
from incident_commander.agent.strategies.records import StepRecord, StepSink
from incident_commander.llm.client import LLMClientProtocol


@dataclass(frozen=True, slots=True, kw_only=True)
class StrategyContext:
    """What a strategy is handed per planner step: the model, its settings, the
    sink for its records — and nothing that can act on the world.

    Rebuilt each iteration rather than once per run, because ``iteration`` is
    part of it. A strategy cannot count its own steps honestly: the loop
    iterates for reasons the strategy never sees (an ADR-0009 freshness
    re-probe and an ADR-0032 subject refusal both continue without a planner
    call), so the loop is the only thing that knows which iteration this is.
    The object is a frozen dataclass of five references; building five of them
    per run is not a cost worth designing around.
    """

    llm_client: LLMClientProtocol
    model: str
    #: 0-based index of the investigation loop's current iteration.
    iteration: int
    #: The strategy's own inference settings block — N, temperature, caps for
    #: the strategies that have them (plan 02 § 4). Empty for ``baseline``,
    #: which has nothing to configure, and stamped into the run's provenance as
    #: ``strategy_config`` so a reported number names the knobs behind it.
    config: Mapping[str, Any] = field(default_factory=dict)
    #: Where this step's ``StepRecord`` goes. ``None`` means nobody is
    #: recording — see ``records.StepSink``.
    record_step: StepSink | None = None
    #: The client the ``candidate_selector`` role calls through (WP-6.2).
    #: Separate from ``llm_client`` because the ROLE is what the accounting
    #: splits on: the two clients are metered as ``investigation_planner`` and
    #: ``candidate_selector``, and a selector sharing the planner's wrapper
    #: would fold selection's cost into generation's and make "what did
    #: selection cost" unanswerable — which is the number the arm is compared
    #: on. ``None`` for every strategy that makes no selector call, which is
    #: every strategy but one; the selector strategy refuses rather than
    #: silently falling back to the planner's client, because a fallback would
    #: report the selector's tokens under the planner's role.
    selector_llm_client: LLMClientProtocol | None = None


class InvestigationStrategy(Protocol):
    """One inference strategy: the planner call, and only the planner call.

    ``plan_next_step`` returns the same ``(RunState, InvestigationStep)`` pair
    ``_plan_next_step`` has always returned, plus the ``StepRecord`` for the
    trace. The ``RunState`` it returns carries the accrued budget and the new
    hypothesis ranking; a strategy that skipped the accrual would make every
    budget number in every report a lower bound (ADR 0015), which is why
    ``baseline`` calls the existing body rather than re-implementing it and why
    a new strategy's first test is that its ledger moved.
    """

    #: Registry key and the value stamped into the run's provenance. Matches a
    #: ``StrategyName`` member: ``INFERENCE_STRATEGY`` is typed as that enum, so
    #: a name nothing can be configured to is a strategy nothing can select.
    name: str

    #: The knobs this strategy ran with, as stamped into ``strategy_config``.
    #: An extension beyond plan 02 § 4's two members, required by the run
    #: provenance record WP-0.3 landed (cmd #223): the field exists there and
    #: the strategy is the only thing that can honestly fill it.
    config: Mapping[str, Any]

    def plan_next_step(
        self, run_state: RunState, at: datetime, ctx: StrategyContext
    ) -> tuple[RunState, InvestigationStep, StepRecord]: ...
