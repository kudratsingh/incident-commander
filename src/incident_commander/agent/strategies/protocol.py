"""The seam itself: ``InvestigationStrategy`` and ``StrategyContext`` (plan 02 § 4).

A strategy replaces ``_plan_next_step`` and returns a proposal and a record; it never calls a
tool, reads a tier, consults ``FIX_MAP`` or decides an action is allowed, and
``tests/unit/test_strategies.py::TestStrategiesHoldNoExecutionPolicy`` refuses any reference
that would move a piece of that policy in here.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from pydantic import BaseModel

from incident_commander.agent.hypothesis import InvestigationStep, ProbeAction, without_probe
from incident_commander.agent.state import RunState
from incident_commander.agent.strategies.records import StepRecord, StepSink
from incident_commander.llm.client import LLMClientProtocol


@dataclass(frozen=True, slots=True, kw_only=True)
class BranchProbeOutcome:
    """What a ``search`` branch's read did to the run: evidence and ledger, or a refusal.

    ``run_state`` comes back accrued — one tool call, one evidence entry — so every branch's
    cost lands in the SHARED ledger before the next branch starts (WP-12.1). A refusal returns
    the state unchanged and names its reason; the walk records and prunes, never escalates.
    """

    run_state: RunState
    refused: str | None = None


#: The loop's own read-only prober, handed to a strategy that explores (WP-12.1). It is NOT a
#: client: the tier re-check, the wire serialization, the MCP call and the ledger accrual are
#: all behind it in ``investigation.py``, which is where execution policy stays (ADR 0036).
#: ``None`` on the context means nobody may branch — every mode but RECORDED.
BranchProber = Callable[[RunState, ProbeAction], BranchProbeOutcome]


@dataclass(frozen=True, slots=True, kw_only=True)
class StrategyContext:
    """What a strategy is handed per planner step, with nothing that can act on the world.

    Rebuilt each iteration because ``iteration`` is part of it: the loop iterates for reasons a
    strategy never sees (an ADR-0009 re-probe, an ADR-0032 subject refusal).
    """

    llm_client: LLMClientProtocol
    model: str
    #: 0-based index of the investigation loop's current iteration.
    iteration: int
    #: The strategy's own inference settings block (plan 02 § 4). Empty for ``baseline``, and
    #: stamped into the run's provenance as ``strategy_config``.
    config: Mapping[str, Any] = field(default_factory=dict)
    #: Where this step's ``StepRecord`` goes. ``None`` means nobody is
    #: recording — see ``records.StepSink``.
    record_step: StepSink | None = None
    #: The client the ``candidate_selector`` role calls through (WP-6.2). Separate because the
    #: accounting splits on ROLE; the selector strategy refuses rather than sharing this one.
    selector_llm_client: LLMClientProtocol | None = None
    #: The client the ``reflection_critic`` role calls through (WP-9.1), for the same reason:
    #: "added tokens" is the number reflection is judged on, so the critique's cost is metered
    #: apart from the planner's. ``reflection`` refuses rather than borrowing ``llm_client``.
    critic_llm_client: LLMClientProtocol | None = None
    #: How a ``search`` branch gathers evidence (WP-12.1): the loop's own read-only prober,
    #: wired ONLY in recorded mode. ``None`` is the refusal — ``search`` stops rather than
    #: degrading into a strategy that explores without reading (ADR 0060).
    branch_prober: BranchProber | None = None
    #: Whether a probe is on offer this step (ADR 0074). ``False`` when the loop has withdrawn
    #: it — the ranking has settled on an actionable answer and the alerted resource's own
    #: newest reading is fresh and shows the fault — and every strategy must then make its
    #: planner call with the narrowed schema. A flag the LOOP sets rather than a decision a
    #: strategy makes: the conditions are the remediate gate's own inputs, which is policy and
    #: stays in ``investigation.py`` (ADR 0036).
    offer_probe: bool = True

    def step_model[T: BaseModel](self, model: type[T]) -> type[T]:
        """The step schema THIS call is made with: ``model``, or ``model`` minus ``probe``.

        Every strategy's planner call goes through this rather than naming
        ``InvestigationStep`` directly, so a narrowing reaches the arm that generates N
        candidates and the one that revises its own step as surely as it reaches ``baseline``.
        The argument is the model the arm would have used, because two arms do not use the same
        one (``CandidateStep`` carries the candidate set).
        """
        return model if self.offer_probe else without_probe(model)


class InvestigationStrategy(Protocol):
    """One inference strategy: the planner call, and only the planner call.

    ``plan_next_step`` returns the usual pair plus the ``StepRecord`` for the trace. Its
    ``RunState`` carries the accrued budget: a strategy that skipped it makes every budget
    number a lower bound (ADR 0015).
    """

    #: Registry key and the value stamped into the run's provenance. Matches a
    #: ``StrategyName`` member, since ``INFERENCE_STRATEGY`` is typed as that enum.
    name: str

    #: The knobs this strategy ran with, as stamped into ``strategy_config``. Beyond plan
    #: 02 § 4, required by the run provenance record WP-0.3 landed (cmd #223).
    config: Mapping[str, Any]

    def plan_next_step(
        self, run_state: RunState, at: datetime, ctx: StrategyContext
    ) -> tuple[RunState, InvestigationStep, StepRecord]: ...
