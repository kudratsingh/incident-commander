"""The run's own reasoning, observed the moment the loop accepts it (ADR 0075).

``ToolCallLog``'s twin for what no client seam can see: the ranking a planner call produced and
the verify judge's verdict. Observed where the loop ACCEPTS it, so one write point covers every
strategy (ADR 0036); stamped with this log's clock, not the iteration's start; never fails a run.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, NamedTuple

from incident_commander.agent.hypothesis import Hypothesis

_LOG: Final = logging.getLogger(__name__)

#: The tool name each kind of thinking is filed under. LLM ROLES, not platform tools: none of
#: them is in ``TOOL_REGISTRY``, so none can reach the planner's page.
PLANNER_TOOL: Final = "investigation_planner"
REFLECTION_TOOL: Final = "reflection"
VERIFY_JUDGE_TOOL: Final = "verify_judge"

#: All three, for anything that has to recognise a thinking row without listing them again.
THINKING_TOOLS: Final[frozenset[str]] = frozenset(
    {PLANNER_TOOL, REFLECTION_TOOL, VERIFY_JUDGE_TOOL}
)

#: How many ranked entries one thinking row carries — what a person reads at a glance. The
#: whole ranking travels on the report's own ``hypotheses`` field beside it.
RANKING_ENTRIES: Final = 5

#: The platform's cap on a report excerpt, named here so the truncation happens once.
MAX_REASON_CHARS: Final = 280


def _now() -> datetime:
    """The wall clock — this log's default stamp for an observation."""
    return datetime.now(UTC)


class ThinkingAction(NamedTuple):
    """The move the thinking chose: ``kind`` is the planner's own ``next_action.kind``
    (``probe``/``remediate``/``stop``), ``tool`` the probe's tool or ``None``."""

    kind: str
    tool: str | None = None


@dataclass(frozen=True, slots=True)
class ObservedThinking:
    """One accepted piece of the run's reasoning, as the loop accepted it.

    ``hypotheses`` is the ranking THIS call produced, carried here rather than read off the
    run state — a mid-transition report would otherwise carry the last transition's ranking.
    """

    #: One of ``THINKING_TOOLS``.
    tool: str
    #: The ranking this call produced, best first (``InvestigationStep`` normalizes it).
    hypotheses: tuple[Hypothesis, ...]
    #: What it decided to do next, or ``None`` where the thinking chose no move — a verify
    #: verdict judges what already happened.
    next_action: ThinkingAction | None
    #: The model's own reason, truncated at ``MAX_REASON_CHARS``.
    reason: str | None
    at: datetime
    #: What to say in place of a move, for thinking that made none: ``verify 2/4 verified``.
    #: ``None`` everywhere else, where ``next_action`` is the headline.
    headline: str | None = None

    def ranking(self) -> list[dict[str, Any]]:
        """The top entries, in the three fields a ranking card draws — without the reasoning,
        which the report's own ``hypotheses`` field already carries in full."""
        return [
            {
                "name": entry.name,
                "category": entry.category.value,
                "confidence": entry.confidence,
            }
            for entry in self.hypotheses[:RANKING_ENTRIES]
        ]

    def action(self) -> dict[str, Any] | None:
        """``next_action`` as the console reads it, or ``None`` when there was none."""
        if self.next_action is None:
            return None
        return {"kind": self.next_action.kind, "tool": self.next_action.tool}

    def sentence(self) -> str:
        """One readable sentence — the row an operator sees without clicking anything.

        ``top consumer_saturation 0.85 → probe get_consumer_lag: <reason>``. Built here, not in
        the reporter, so every producer words it the same and it is testable without a platform.
        """
        top = self.hypotheses[0] if self.hypotheses else None
        lead = (
            f"top {top.category.value} {top.confidence:.2f}"
            if top is not None
            else "no ranking yet"
        )
        move = self.headline if self.headline is not None else self._move()
        tail = f": {self.reason}" if self.reason else ""
        return f"{lead} → {move}{tail}"

    def _move(self) -> str:
        if self.next_action is None:
            return "no next move named"
        if self.next_action.tool:
            return f"{self.next_action.kind} {self.next_action.tool}"
        return self.next_action.kind


def _reason(text: str | None) -> str | None:
    """A model's reason, cut to what a report may carry. ``None`` for nothing."""
    if text is None:
        return None
    collapsed = " ".join(text.split())
    if not collapsed:
        return None
    if len(collapsed) <= MAX_REASON_CHARS:
        return collapsed
    return f"{collapsed[: MAX_REASON_CHARS - 1]}…"


class PlannerLog:
    """Where the loop writes the reasoning it just accepted, and who is watching it.

    One subscriber, like ``ToolCallLog.subscribe``: the producer must not have to know whether
    anybody is listening.
    """

    def __init__(self, *, clock: Callable[[], datetime] = _now) -> None:
        self.clock = clock
        self._sink: Callable[[ObservedThinking], bool] | None = None
        #: Observations made, and observations nobody could send. Counts, not rails: they exist
        #: so "the console showed no thinking" has an answer other than a broken frontend.
        self.observed = 0
        self.dropped = 0

    def subscribe(self, sink: Callable[[ObservedThinking], bool]) -> None:
        """Report each observation as it is made. The sink must answer, never raise."""
        self._sink = sink

    def ranking(
        self,
        *,
        tool: str,
        hypotheses: tuple[Hypothesis, ...],
        next_action: ThinkingAction | None,
        reason: str | None,
    ) -> None:
        """Record one accepted planner ranking, stamped now."""
        self.observe(
            ObservedThinking(
                tool=tool,
                hypotheses=tuple(hypotheses),
                next_action=next_action,
                reason=_reason(reason),
                at=self.clock(),
            )
        )

    def verdict(
        self,
        *,
        hypotheses: tuple[Hypothesis, ...],
        verdict: str,
        reasoning: str | None,
        attempt: int,
        of: int,
    ) -> None:
        """Record one verify poll's verdict, stamped now. The ranking travels unchanged — a
        verdict does not re-rank anything — so the row keeps what the run still believes."""
        self.observe(
            ObservedThinking(
                tool=VERIFY_JUDGE_TOOL,
                hypotheses=tuple(hypotheses),
                next_action=None,
                reason=_reason(reasoning),
                at=self.clock(),
                headline=f"verify {attempt}/{of} {verdict}",
            )
        )

    def observe(self, thinking: ObservedThinking) -> None:
        """Hand one observation to the subscriber. Never raises.

        Called inside a transition, so an exception here would surface as a failed
        investigation step in the run it is describing. A sink answering ``False`` is a
        counted drop; the ranking still reaches the console on the next transition report.
        """
        self.observed += 1
        sent = False
        if self._sink is not None:
            try:
                sent = self._sink(thinking)
            except Exception as err:  # noqa: BLE001 - telemetry may never fail a run
                _LOG.warning("agent-run reporting: dropped one observed ranking: %s", err)
        if not sent:
            self.dropped += 1
