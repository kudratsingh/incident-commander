"""The run's own reasoning, observed the moment the loop accepts it (ADR 0075).

``ToolCallLog`` (``agent/run_reporting.py``) reports a tool call when the client seam sees it
return. This module is its twin for the thing no client seam can see: the RANKING a planner
call produced, and the verify judge's verdict. Both are LLM calls, so they exist nowhere but
the run's own state — and before ADR 0075 they reached a watching operator only at the next
state transition. In the owner's fourth take that meant three planner rankings decided over 22
seconds arriving as one burst at the end, and a console that showed the agent thinking nothing
for the whole investigation.

Three properties, in the order they matter:

1. **It is observed where it is ACCEPTED, not where it is produced.** The loop is the one
   place every strategy's proposal passes through (ADR 0036: strategies propose, the loop
   decides), so that is the single write point for a planner ranking — one line, and it covers
   ``baseline``, ``reflection``'s revision, both ``best_of_n`` arms, ``candidate_selector``,
   ``search`` and ``adaptive`` without any of them learning that telemetry exists.
2. **The stamp is the observation's own moment**, read from this log's clock when the
   observation is made — the same rule ``ToolCallLog`` follows and the same rule ADR 0075
   applies to a transition. The loop's ``at`` is the iteration's START, which is precisely the
   lie this work order is about.
3. **Nothing here can fail a run.** ``observe`` swallows everything its sink raises, a sink
   that cannot send is a counted drop, and ``None`` in place of a log means nobody is
   watching. A dropped observation still reaches the console on the next transition report,
   which carries the ranking as it always has.

Why its own module rather than beside ``ToolCallLog``: the producers are
``agent/investigation.py`` and ``agent/remediation.py``, and ``agent/run_reporting.py`` imports
both of them transitively (through ``agent/briefing.py``). A shared type has to sit below all
three, and this is that place — it imports one model and nothing else.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, NamedTuple

from incident_commander.agent.hypothesis import Hypothesis

_LOG: Final = logging.getLogger(__name__)

#: The tool name each kind of thinking is filed under. They are LLM ROLES, not platform
#: tools — a ``report``-kind step is the platform's own word for "this row is the agent
#: telling you something, not a call it made" — so nothing here is in ``TOOL_REGISTRY`` and
#: none of them can reach the planner's page.
PLANNER_TOOL: Final = "investigation_planner"
REFLECTION_TOOL: Final = "reflection"
VERIFY_JUDGE_TOOL: Final = "verify_judge"

#: All three, for anything that has to recognise a thinking row without listing them again.
THINKING_TOOLS: Final[frozenset[str]] = frozenset(
    {PLANNER_TOOL, REFLECTION_TOOL, VERIFY_JUDGE_TOOL}
)

#: How many ranked entries one thinking row carries. Five, because this is the ranking a
#: person reads at a glance while a run is happening; the whole ranking travels on the
#: report's own ``hypotheses`` field beside it.
RANKING_ENTRIES: Final = 5

#: How much of the model's own reason travels. The platform's cap on a report excerpt, named
#: here so the truncation happens once, where the sentence is built.
MAX_REASON_CHARS: Final = 280


def _now() -> datetime:
    """The wall clock — this log's default stamp for an observation."""
    return datetime.now(UTC)


class ThinkingAction(NamedTuple):
    """The move the thinking chose, in the two fields a console row shows.

    ``kind`` is the planner's own ``next_action.kind`` (``probe``, ``remediate`` or ``stop``)
    and ``tool`` the tool a probe named, ``None`` for the two that name none.
    """

    kind: str
    tool: str | None = None


@dataclass(frozen=True, slots=True)
class ObservedThinking:
    """One accepted piece of the run's reasoning, as the loop accepted it.

    ``hypotheses`` is the ranking THIS call produced, carried on the observation rather than
    read back off the run state: a report that goes out mid-transition would otherwise carry
    the ranking as of the last transition, which is the stale number the owner's take showed.
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
        """The top entries, in the three fields a ranking card draws.

        Without the reasoning: the whole ranking WITH reasoning travels on the report's
        ``hypotheses`` field, and repeating it inside the step's arguments would send the same
        paragraphs twice per planner call.
        """
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

        ``top consumer_saturation 0.85 → probe get_consumer_lag: one more fresh reading of
        the alerted subject``. Built here rather than in the reporter so the wording is the
        same for every producer and testable without a platform.
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

    One subscriber, because one reporter reports one run — the same shape as
    ``ToolCallLog.subscribe``, and for the same reason: the producer must not have to know
    whether anybody is listening.
    """

    def __init__(self, *, clock: Callable[[], datetime] = _now) -> None:
        self.clock = clock
        self._sink: Callable[[ObservedThinking], bool] | None = None
        #: How many observations were made, and how many nobody could send. Counts, not
        #: rails: they change nothing about the run and exist so "the console showed no
        #: thinking" has an answer other than "the frontend is broken".
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
        """Record one verify poll's verdict, stamped now.

        The ranking travels unchanged — a verdict does not re-rank anything — so the row says
        what the run still believes beside what the judge just said about the action.
        """
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

        It is called from inside a transition, between an LLM call and the loop's decision
        about it, so an exception here would surface as a failed investigation step in the run
        this is describing. A sink that answers ``False`` — no state to stamp a report with
        yet, or a platform that does not declare the step field — is a counted drop: the
        ranking still reaches the console on the next transition report, which has carried it
        since WO-R3-329.
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
