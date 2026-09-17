"""``CandidateGeneration`` — what a generator hands a selector (WP-6.2).

Plan 02 § 12 says the selector "runs over enumerated or sampled sets (config)",
so the arm is ``(generator, N, selector)`` and the selector has to be able to ask
a generator for its candidate set. Neither best-of-N arm could be asked: both
produced their set inside ``plan_next_step`` and handed back an
``InvestigationStep`` plus a ``StepRecord``, and a ``StepRecord``'s
``CandidateRecord`` is a lossy projection — it carries a probe's *tool name* and
not its arguments, so a selector reconstructing candidates from one could emit
``get_consumer_lag()`` with no consumer group and call it the selected
candidate's next probe.

So the seam is one method, ``generate``, and each arm's ``plan_next_step`` is
now expressed in terms of it. That keeps exactly one generation path per arm —
the alternative was the selector re-implementing the planner call it needs the
candidates from, which would make an arm comparison a comparison of two copies
of one call.

Why a whole ``StepRecord`` on the way out
-----------------------------------------

``CandidateGeneration`` carries the generator's own finished record, and the
selector **rebuilds** it (``dataclasses.replace``) with the selector block, the
selector's own ``LLMCallRecord`` appended and the emitted step replaced. The
alternative — carrying the seven measurements a record needs and assembling it
in two places — is how ``planner_context_chars`` would come to mean one thing
for a generator-only run and another for a selected one, and those two numbers
are the ones an arm comparison is read off.

The record the generator builds is never *written* by ``generate``: the sink is
called by ``plan_next_step`` (a generator running alone) or by the selector
strategy (a generator running under one), so a step produces exactly one record
and there is no path on which a reader sees both the pre-selection record and
the post-selection one for the same step.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from incident_commander.agent.candidates import DiagnosisCandidate
from incident_commander.agent.hypothesis import InvestigationStep
from incident_commander.agent.state import RunState
from incident_commander.agent.strategies.protocol import StrategyContext
from incident_commander.agent.strategies.records import CandidateRecord, StepRecord
from incident_commander.llm.client import LLMUsage
from incident_commander.llm.repair import RepairedCall, sum_usage, usage_of


@dataclass(frozen=True, slots=True, kw_only=True)
class CandidateGeneration:
    """One generator's answer for one planner step, before anything selects.

    ``run_state`` is the accrued state — the generator charged its own calls
    through ``accrue_structured_call`` exactly as it does when it runs alone, so
    a selector composing over it cannot make the generation cheaper by being
    there (ADR 0015).
    """

    run_state: RunState
    #: The set, ranked by confidence at the schema boundary, so index 0 is the
    #: top candidate for every generator.
    candidates: tuple[DiagnosisCandidate, ...]
    #: The step this generator would have emitted on its own — the top
    #: candidate's step for ``best_of_n_enumerated``, the best sample's for
    #: ``best_of_n_sampled``. The selector replaces it, and a selector that
    #: fails leaves it as what the run does, which is the generator arm's own
    #: behaviour rather than a crash.
    proposed_step: InvestigationStep
    #: The generator's own ``StepRecord``, with ``selector=None``.
    record: StepRecord
    #: Everything the generation billed: every call that returned, and every
    #: ADR-0035 repair leg inside them. Carried out because a selector composed
    #: over this arm can fail AFTER the generation was paid for, and the loop's
    #: ``except`` arm charges ``accrue_llm_error`` against the state it held
    #: BEFORE ``plan_next_step`` — so a selector that raised without handing this
    #: back would charge the generation to nobody. That is ADR 0045's trap one
    #: layer up, and ``candidate_selector`` closes it the same way: re-raise
    #: carrying the summed usage.
    #:
    #: ``None`` only when nothing anywhere reported a usage — a fake client with
    #: no counters — where charging a guess would be an over-report invented
    #: rather than measured.
    billed_usage: LLMUsage | None = None


@runtime_checkable
class CandidateGenerator(Protocol):
    """A strategy that can be asked for its candidate set.

    Both best-of-N arms satisfy it. ``baseline`` deliberately does not: it has
    no candidate set to select between — it considers one diagnosis — and a
    selector over a one-candidate set is a billed call whose only possible
    answer is the candidate it was given.

    ``runtime_checkable`` so ``CandidateSelectorStrategy`` can refuse a
    configured generator that cannot generate, with the refusal reading as a type
    question rather than as a ``hasattr``. The check is structural and therefore
    only sees the METHOD NAMES, not their signatures — which is enough here,
    because the alternative it has to reject is ``baseline``, and ``baseline``
    has no ``generate`` at all.
    """

    name: str
    config: Mapping[str, Any]

    def generate(
        self, run_state: RunState, at: datetime, ctx: StrategyContext
    ) -> CandidateGeneration: ...

    def plan_next_step(
        self, run_state: RunState, at: datetime, ctx: StrategyContext
    ) -> tuple[RunState, InvestigationStep, StepRecord]: ...


def candidate_record_of(
    candidate: DiagnosisCandidate, *, generation_call_id: str
) -> CandidateRecord:
    """One candidate, as the trace record holds it.

    Shared by both arms so the projection is written once. ``proposed_probe`` is
    the probe's tool name and not the probe: the record is research data read by
    ``evals/candidate_metrics.py``, which groups on labels, and the arguments are
    on the emitted step when the probe was emitted. Anything that needs the whole
    probe — the selector, on ``probe_more`` — reads the ``DiagnosisCandidate``
    off ``CandidateGeneration`` instead, which is why that field exists.
    """
    return CandidateRecord(
        candidate_id=candidate.candidate_id,
        category=candidate.category,
        name=candidate.name,
        confidence=candidate.confidence,
        evidence_for=tuple(str(ref.evidence_id) for ref in candidate.evidence_for),
        evidence_against=tuple(str(ref.evidence_id) for ref in candidate.evidence_against),
        proposed_probe=(
            candidate.next_probe.tool_name if candidate.next_probe is not None else None
        ),
        generation_call_id=generation_call_id,
    )


def billed_usage_of(calls: Sequence[RepairedCall[Any]]) -> LLMUsage | None:
    """Everything a generation billed: each call that parsed, and each repair leg.

    The same arithmetic ``best_of_n_sampled._billed_so_far`` does for its own
    failure path, written once here because the selector needs it for a
    generation that SUCCEEDED and was then followed by a selector call that
    failed. ``sum_usage`` treats a ``None`` member as nothing rather than as
    zero, so a fake client that reports no usage produces ``None`` and not a
    fabricated zero.
    """
    billed: list[LLMUsage | None] = []
    for call in calls:
        billed.extend(usage_of(err) for err in call.failures)
        billed.append(call.result)
    return sum_usage(*billed)
