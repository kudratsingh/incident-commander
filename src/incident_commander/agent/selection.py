"""``SelectionResult``, its context and the ``candidate_selector`` call (plan 02 § 12, WP-6.1).

Rules are validators: ``scores`` covers the set exactly, ``selected_candidate_id`` is set iff
the decision is ``select``, ``selecting_among`` binds the set. No temperature (decision O-24).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from enum import StrEnum
from typing import Final

from pydantic import ConfigDict, Field, field_validator, model_validator

from incident_commander.agent.briefing import render_trail, trail_of
from incident_commander.agent.candidates import DiagnosisCandidate, EvidenceRef
from incident_commander.agent.state import RunState
from incident_commander.llm.client import LLMClientProtocol
from incident_commander.llm.prompts.loader import load_prompt
from incident_commander.llm.repair import RepairedCall, call_with_output_repair
from incident_commander.llm.structured import StructuredOutput

#: The prompt file this role is asked with. One spelling, shared by the strategy, the snapshot
#: tests and the loader.
SELECTOR_PROMPT: Final[str] = "candidate_selector"

#: The role the selector's calls are accounted and traced under, so its cost can be read apart
#: from the planner's.
SELECTOR_ROLE: Final[str] = "candidate_selector"

#: The candidate ids a selection is allowed to name during one selector call. ``None`` refuses
#: every id rather than allowing any; a context variable, so two runs cannot cross.
_CANDIDATES: ContextVar[tuple[str, ...] | None] = ContextVar(
    "incident_commander_selector_candidates", default=None
)

#: The exact wording each validation failure uses, named so a test can assert the reason rather
#: than merely that something raised.
NO_CANDIDATES_BOUND: Final[str] = "no candidate set is bound"
UNKNOWN_CANDIDATE_ID: Final[str] = "names no candidate in the set you were shown"
UNSCORED_CANDIDATE: Final[str] = "was not scored"
SELECTION_WITHOUT_SELECT: Final[str] = "selected_candidate_id is set on a decision that is not"
SELECT_WITHOUT_SELECTION: Final[str] = "decision is 'select' and no candidate is selected"


@contextmanager
def selecting_among(candidates: Iterable[DiagnosisCandidate]) -> Iterator[None]:
    """Bind the candidate set a ``SelectionResult`` is resolved against. Re-entrant."""
    token = _CANDIDATES.set(tuple(candidate.candidate_id for candidate in candidates))
    try:
        yield
    finally:
        _CANDIDATES.reset(token)


def _bound_candidates(subject: str) -> tuple[str, ...]:
    """The bound candidate set, or a refusal naming what could not be resolved."""
    bound = _CANDIDATES.get()
    if bound is None:
        raise ValueError(
            f"{subject} cannot be validated: {NO_CANDIDATES_BOUND}. Validate "
            "inside `selecting_among(candidates)` — a selection with nothing to "
            "resolve it against is not a selection."
        )
    return bound


class SelectionDecision(StrEnum):
    # No class docstring: Pydantic copies one into the JSON schema the model itself reads, so a
    # note meant for developers would become an instruction to the model.
    SELECT = "select"
    PROBE_MORE = "probe_more"
    ESCALATE = "escalate"


class SelectionResult(StructuredOutput):
    # No class docstring here either, for the same reason: it would reach the model's schema.
    model_config = ConfigDict(frozen=True, extra="forbid")

    decision: SelectionDecision = Field(
        description=(
            "'select' to commit to one candidate as the diagnosis, 'probe_more' "
            "to gather more evidence before committing, 'escalate' to hand the "
            "incident to a human."
        )
    )
    selected_candidate_id: str | None = Field(
        description=(
            "The candidate_id you are committing to. Required when decision is "
            "'select', and null for every other decision."
        )
    )
    scores: dict[str, float] = Field(
        description=(
            "One entry per candidate you were shown, keyed by its candidate_id: "
            "how well the evidence supports it, from 0.0 to 1.0. Score every "
            "candidate, including the ones you are rejecting."
        )
    )
    uncertainty: float = Field(
        ge=0.0,
        le=1.0,
        description=(
            "How unsure you are of this decision, from 0.0 (certain) to 1.0 "
            "(no better than a guess)."
        ),
    )
    reasoning: str = Field(
        min_length=1,
        description=(
            "Two sentences at most: the evidence that decided it, naming the "
            "probe you read it from. Plain prose, no lists or markdown."
        ),
    )

    @field_validator("scores", mode="after")
    @classmethod
    def _scores_cover_the_set_exactly(cls, value: dict[str, float]) -> dict[str, float]:
        """Every scored id is a candidate, and every candidate is scored.

        ``scores`` is the only ranking, so a missing entry is one ``probe_more`` cannot
        point at (plan 02 § 12).
        """
        known = _bound_candidates("scores")
        for candidate_id in value:
            if candidate_id not in known:
                raise ValueError(
                    f"scores key {candidate_id!r} {UNKNOWN_CANDIDATE_ID} "
                    f"({len(known)} candidates). Score the candidates you were "
                    "shown, by the candidate_id each one carries."
                )
        for candidate_id in known:
            if candidate_id not in value:
                raise ValueError(
                    f"candidate {candidate_id!r} {UNSCORED_CANDIDATE}. Every "
                    "candidate in the set needs a score, including the ones you "
                    "reject — an unscored candidate is one you did not consider."
                )
        for candidate_id, score in value.items():
            if not 0.0 <= score <= 1.0:
                raise ValueError(
                    f"score {score} for candidate {candidate_id!r} is outside "
                    "0.0-1.0. Scores are on that scale; a number off it cannot "
                    "be compared with another run's."
                )
        return value

    @model_validator(mode="after")
    def _a_selection_is_stated_exactly_when_there_is_one(self) -> SelectionResult:
        """``selected_candidate_id`` is ``None`` iff the decision is not ``select``.

        An id on ``escalate`` is a diagnosis nobody acts on; a ``select`` with none forces
        an invented fallback.
        """
        if self.decision is SelectionDecision.SELECT:
            if self.selected_candidate_id is None:
                raise ValueError(
                    f"{SELECT_WITHOUT_SELECTION}. Name the candidate_id you are "
                    "committing to, or choose 'probe_more' or 'escalate'."
                )
            known = _bound_candidates("selected_candidate_id")
            if self.selected_candidate_id not in known:
                raise ValueError(
                    f"selected_candidate_id {self.selected_candidate_id!r} "
                    f"{UNKNOWN_CANDIDATE_ID} ({len(known)} candidates). Select "
                    "one of the candidates you were shown."
                )
        elif self.selected_candidate_id is not None:
            raise ValueError(
                f"{SELECTION_WITHOUT_SELECT} 'select' "
                f"(decision={self.decision.value!r}, "
                f"selected_candidate_id={self.selected_candidate_id!r}). Leave it "
                "null unless you are committing to that candidate."
            )
        return self

    @property
    def chosen_candidate_id(self) -> str | None:
        """The candidate this decision points at: ``select`` names it, ``probe_more`` takes the
        highest-scored (ties go to the first in ``scores``), ``escalate`` points at nothing."""
        if self.decision is SelectionDecision.SELECT:
            return self.selected_candidate_id
        if self.decision is SelectionDecision.PROBE_MORE and self.scores:
            return max(self.scores, key=lambda candidate_id: self.scores[candidate_id])
        return None


#: The headings of the selector's context block, named once each so the prompt and the tests
#: cannot spell them differently.
CANDIDATES_HEADING: Final[str] = "Candidate diagnoses:"
ALERT_PREFIX: Final[str] = "Alert: "


def format_selection_context(run_state: RunState, candidates: Sequence[DiagnosisCandidate]) -> str:
    """What the ``candidate_selector`` is shown: the alert, the trail, the set.

    The trail renders arguments first (INC-002). Nothing evaluator-side is reachable —
    ``ground_truth`` and ``discriminating_probes`` are not arguments at all (ADR 0038).
    """
    lines = [
        f"{ALERT_PREFIX}{_alert_line(run_state)}",
        "",
        *render_trail(trail_of(run_state.evidence)),
        "",
        CANDIDATES_HEADING,
    ]
    for candidate in candidates:
        lines.append(
            f"  - candidate_id={candidate.candidate_id} "
            f"category={candidate.category.value} name={candidate.name} "
            f"stated_confidence={candidate.confidence}"
        )
        lines.append(f"      evidence_for: {_ids(candidate.evidence_for)}")
        lines.append(f"      evidence_against: {_ids(candidate.evidence_against)}")
        lines.append(f"      next_probe: {_probe(candidate)}")
    return "\n".join(lines)


def _alert_line(run_state: RunState) -> str:
    """The alert, sorted, so two renders of one run are the same string."""
    return json.dumps(dict(run_state.alert), sort_keys=True)


def _ids(refs: Sequence[EvidenceRef]) -> str:
    """A candidate's citations, or the word ``none``, so silence is visible."""
    return ", ".join(str(ref.evidence_id) for ref in refs) or "none"


def _probe(candidate: DiagnosisCandidate) -> str:
    """The probe this candidate would run next, with its arguments — filtered and unfiltered
    reads share one tool name, so the arguments are the difference (INC-002)."""
    if candidate.next_probe is None:
        return "none"
    return (
        f"{candidate.next_probe.tool_name}"
        f"({json.dumps(candidate.next_probe.arguments, sort_keys=True)})"
    )


def select_candidate(
    llm_client: LLMClientProtocol,
    *,
    run_state: RunState,
    candidates: Sequence[DiagnosisCandidate],
    model: str,
) -> RepairedCall[SelectionResult]:
    """Ask the ``candidate_selector`` which candidate the run should act on.

    ``selecting_among`` wraps the call so validators run inside the repair loop: a bad id is
    then an ordinary output failure, re-asked once (ADR 0035) with both legs charged (ADR 0015).
    """
    if not candidates:
        raise ValueError(
            "the candidate_selector was given an empty candidate set; there is "
            "nothing to select between. A generator that produced no candidates "
            "is a failure of generation, not a selection."
        )
    with selecting_among(candidates):
        return call_with_output_repair(
            llm_client,
            system_prompt=load_prompt(SELECTOR_PROMPT),
            user_message=format_selection_context(run_state, candidates),
            output_model=SelectionResult,
            model=model,
        )
