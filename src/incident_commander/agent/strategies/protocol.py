"""The seam itself: ``InvestigationStrategy`` and ``StrategyContext`` (plan 02 § 4).

A strategy returns a proposal and a record; it never calls a tool, reads a tier or consults
``FIX_MAP``, and ``TestStrategiesHoldNoExecutionPolicy`` refuses a reference that would.
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

    ``run_state`` comes back accrued, so every branch's cost lands in the SHARED ledger before
    the next one starts (WP-12.1). A refusal returns the state unchanged and names its reason.
    """

    run_state: RunState
    refused: str | None = None


#: A read-only prober the loop hands to a strategy that explores, so the tier check, the call
#: itself and the charging all stay in the loop. ``None`` means this run may not branch at all.
BranchProber = Callable[[RunState, ProbeAction], BranchProbeOutcome]


@dataclass(frozen=True, slots=True, kw_only=True)
class StrategyContext:
    """What a strategy is handed per planner step, with nothing that can act on the world.

    Rebuilt each iteration because ``iteration`` is part of it: the loop iterates for reasons a
    strategy never sees (an ADR-0009 re-probe, an ADR-0032 subject refusal).
    """

    llm_client: LLMClientProtocol
    model: str
    #: Which pass of the investigation loop this is, counting from zero.
    iteration: int
    #: The strategy's own settings, empty for ``baseline``, stored with the run so a later reader
    #: knows exactly how it was configured.
    config: Mapping[str, Any] = field(default_factory=dict)
    #: Where this step's research record is sent. ``None`` means nothing is recording, which is
    #: the case for every run without a trace directory.
    record_step: StepSink | None = None
    #: A separate client for the selector's calls, because cost is accounted per role. An arm
    #: that needs one refuses rather than borrow the planner's and misreport its tokens.
    selector_llm_client: LLMClientProtocol | None = None
    #: A separate client for the critic's calls, for the same reason: how many tokens reflection
    #: adds is the number it is judged on, so it refuses rather than borrow the planner's.
    critic_llm_client: LLMClientProtocol | None = None
    #: How a search branch reads the world, provided only on a replayed run. ``None`` is a
    #: refusal: the search arm stops rather than explore a world it cannot read (ADR 0060).
    branch_prober: BranchProber | None = None
    #: Whether this step may propose another read at all. The loop decides it, because the
    #: conditions are the same ones its remediate gate uses, and that is the loop's policy.
    offer_probe: bool = True

    def step_model[T: BaseModel](self, model: type[T]) -> type[T]:
        """The step schema THIS call is made with: ``model``, or ``model`` minus ``probe``.

        Every strategy's planner call goes through this, so a narrowing reaches every arm.
        ``model`` is passed in because arms use different ones (``CandidateStep`` and friends).
        """
        return model if self.offer_probe else without_probe(model)


class InvestigationStrategy(Protocol):
    """One inference strategy: the planner call, and only the planner call.

    The returned ``RunState`` carries the accrued budget — a strategy that skipped it makes
    every budget number a lower bound (ADR 0015).
    """

    #: The name this strategy is registered and recorded under. It must match a ``StrategyName``
    #: member, because the environment variable that selects a strategy is typed as that enum.
    name: str

    #: The settings this strategy ran with, stored with the run as ``strategy_config``.
    config: Mapping[str, Any]

    def plan_next_step(
        self, run_state: RunState, at: datetime, ctx: StrategyContext
    ) -> tuple[RunState, InvestigationStep, StepRecord]: ...
