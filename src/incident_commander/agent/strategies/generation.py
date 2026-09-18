"""``CandidateGeneration`` — what a generator hands a selector (WP-6.2).

Plan 02 § 12 makes the arm ``(generator, N, selector)``, so the seam is one method,
``generate``, and each arm's ``plan_next_step`` is expressed in terms of it — one generation
path per arm. A ``StepRecord``'s ``CandidateRecord`` could not serve: it carries a probe's
*tool name* and not its arguments. The generator's whole record travels out and the selector
**rebuilds** it (``dataclasses.replace``), so ``planner_context_chars`` has one definition;
``generate`` never writes it, so a step produces exactly one record.
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

    ``run_state`` is accrued, so a selector cannot make generation cheaper (ADR 0015).
    """

    run_state: RunState
    #: The set, ranked by confidence at the schema boundary, so index 0 is the
    #: top candidate for every generator.
    candidates: tuple[DiagnosisCandidate, ...]
    #: The step this generator would have emitted on its own. The selector replaces it, and a
    #: selector that fails leaves it as what the run does — the generator arm's own behaviour.
    proposed_step: InvestigationStep
    #: The generator's own ``StepRecord``, with ``selector=None``.
    record: StepRecord
    #: Everything the generation billed, ADR-0035 repairs included: a selector can fail AFTER
    #: this was paid for, and ``accrue_llm_error`` charges pre-``plan_next_step`` (ADR 0045).
    billed_usage: LLMUsage | None = None


@runtime_checkable
class CandidateGenerator(Protocol):
    """A strategy that can be asked for its candidate set.

    Both best-of-N arms satisfy it; ``baseline`` does not, because a selector over its one
    candidate is a billed call with one possible answer. ``runtime_checkable`` so the check
    sees only METHOD NAMES — enough, since ``baseline`` has no ``generate`` at all.
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

    Shared by both arms, so the projection is written once. ``proposed_probe`` is the tool name,
    not the probe: ``evals/candidate_metrics.py`` groups on labels.
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

    ``best_of_n_sampled._billed_so_far``'s arithmetic, once. ``sum_usage`` treats a
    ``None`` member as nothing, so no usage gives ``None``, not a zero.
    """
    billed: list[LLMUsage | None] = []
    for call in calls:
        billed.extend(usage_of(err) for err in call.failures)
        billed.append(call.result)
    return sum_usage(*billed)
