"""``DiagnosisCandidate`` and ``EvidenceRef`` — the candidate schema (plan 02 § 11.3).

Four rules are validators, not prompt prose: an ``EvidenceRef`` resolves in
``RunState.evidence``; ``(category, name)`` and ``candidate_id`` are unique; ``candidates[0]``
is the top-confidence entry; ADR-0035's decode covers every nested field.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Annotated, Any, Final
from uuid import UUID

from pydantic import AfterValidator, ConfigDict, Field, field_validator

from incident_commander.agent.hypothesis import HypothesisCategory, ProbeAction
from incident_commander.agent.state import EvidenceEntry
from incident_commander.llm.structured import StructuredOutput

#: The evidence ids a candidate set may cite, for one planner call. ``None`` is a
#: refusal, not a licence; a ``ContextVar`` so concurrent runs cannot cross.
_LEDGER: ContextVar[frozenset[UUID] | None] = ContextVar(
    "incident_commander_evidence_ledger", default=None
)

#: Named so a test asserts the guard's marker, not that something raised (F-007).
NO_LEDGER_BOUND: Final[str] = "no evidence ledger is bound"
UNKNOWN_EVIDENCE_ID: Final[str] = "names no entry in the run's evidence ledger"
DUPLICATE_CANDIDATE: Final[str] = "duplicate candidate"
DUPLICATE_CANDIDATE_ID: Final[str] = "duplicate candidate_id"


@contextmanager
def grounded_in(evidence: Iterable[EvidenceEntry]) -> Iterator[None]:
    """Bind the ledger a candidate set is resolved against. Re-entrant: no stale ledger."""
    token = _LEDGER.set(frozenset(entry.evidence_id for entry in evidence))
    try:
        yield
    finally:
        _LEDGER.reset(token)


def _bound_ledger(subject: str) -> frozenset[UUID]:
    """The bound ledger, or a refusal naming what could not be resolved."""
    ledger = _LEDGER.get()
    if ledger is None:
        raise ValueError(
            f"{subject} cannot be validated: {NO_LEDGER_BOUND}. Validate "
            "inside `grounded_in(run_state.evidence)` — a reference with "
            "nothing to resolve it against is not a grounded reference."
        )
    return ledger


# No class docstring on any model below: pydantic copies one into the JSON schema
# ``description`` shown to the model. Field descriptions are the deliberate opposite.


class EvidenceRef(StructuredOutput):
    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_id: UUID = Field(
        description=(
            "The evidence_id of one entry in the evidence ledger you were "
            "shown. Cite only ids that appear there."
        )
    )

    @field_validator("evidence_id", mode="after")
    @classmethod
    def _resolves_to_a_ledger_entry(cls, value: UUID) -> UUID:
        """Grounding, as a validator (plan 02 § 11.3).

        On ``EvidenceRef`` rather than the fields, so every field of this type is covered once.
        """
        ledger = _bound_ledger(f"evidence_id {value}")
        if value not in ledger:
            raise ValueError(
                f"evidence_id {value} {UNKNOWN_EVIDENCE_ID} "
                f"({len(ledger)} entries). Cite an id from the ledger you "
                "were shown, or cite nothing."
            )
        return value


class DiagnosisCandidate(StructuredOutput):
    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate_id: str = Field(
        min_length=1,
        description=(
            "A short identifier for this candidate, unique within the set. "
            "It is how the candidate is referred to afterwards."
        ),
    )
    category: HypothesisCategory = Field(
        description=(
            "Structural category. Drives remediation routing via FIX_MAP. "
            "Only values in HypothesisCategory are accepted."
        )
    )
    name: str = Field(
        min_length=1,
        description=(
            "Descriptive short label for the briefing (e.g. "
            "'worker-dispatcher lag 15k sustained 5min'). Free-form."
        ),
    )
    confidence: float = Field(ge=0.0, le=1.0)
    # The only two defaulted fields: omitted and empty say the same thing, so
    # repairing the absence would spend a billed call for nothing.
    evidence_for: tuple[EvidenceRef, ...] = Field(
        default=(),
        description="Evidence ids that support this candidate. May be empty.",
    )
    evidence_against: tuple[EvidenceRef, ...] = Field(
        default=(),
        description="Evidence ids that argue against this candidate. May be empty.",
    )
    # Required even when null: plan 02 § 11.3 builds the step from the top
    # candidate's probe, so silence must be stated, not inferred.
    next_probe: ProbeAction | None = Field(
        description=(
            "The read tool that would best discriminate this candidate next, "
            "or null when no further probe would."
        )
    )


def _one_candidate_each(
    value: tuple[DiagnosisCandidate, ...],
) -> tuple[DiagnosisCandidate, ...]:
    """Reject duplicates, then normalise the ranking (plan 02 § 11.1, § 11.3).

    Also the only check an *uncited* set reaches, so a forgotten ``grounded_in`` cannot pass.
    Duplicates compare ``(category, name)`` exactly — normalising would hide WP-5.2's
    duplicate rate.
    """
    _bound_ledger("a candidate set")
    seen_ids: set[str] = set()
    seen_labels: set[tuple[HypothesisCategory, str]] = set()
    for candidate in value:
        if candidate.candidate_id in seen_ids:
            raise ValueError(
                f"{DUPLICATE_CANDIDATE_ID} {candidate.candidate_id!r}: the id "
                "is how one candidate is named afterwards, so it has to name "
                "one candidate."
            )
        seen_ids.add(candidate.candidate_id)
        label = (candidate.category, candidate.name)
        if label in seen_labels:
            raise ValueError(
                f"{DUPLICATE_CANDIDATE} (category={candidate.category.value!r}, "
                f"name={candidate.name!r}): one diagnosis stated twice is one "
                "diagnosis. Give each candidate a distinct category or name."
            )
        seen_labels.add(label)
    # Stable, so equal confidences keep the order the model stated them in.
    return tuple(sorted(value, key=lambda candidate: candidate.confidence, reverse=True))


#: The set-level rules in the model's words — one string, so the two types agree.
_SET_DESCRIPTION: Final[str] = (
    "List them most likely first; ordering is normalized after validation — "
    "entries are re-sorted by confidence descending (stable: equal-confidence "
    "entries keep their listed order), so index 0 is always the top candidate. "
    "Each candidate must have a distinct candidate_id and a distinct "
    "(category, name)."
)

#: The candidate set as a TYPE, not a field: ``candidates: CandidateTuple``
#: inherits all three set-level rules (architecture-principles rule 2).
CandidateTuple = Annotated[
    tuple[DiagnosisCandidate, ...],
    Field(
        min_length=1,
        description=_SET_DESCRIPTION,
    ),
    AfterValidator(_one_candidate_each),
]


def exact_candidate_tuple(n: int) -> Any:
    """``CandidateTuple`` bounded to exactly ``n`` entries — the WP-5.2 schema.

    The length constraint must precede ``AfterValidator`` or pydantic emits ``minLength`` /
    ``maxLength`` on an array, which reads as no bound at all. Returns ``Any`` because an
    ``Annotated[...]`` built from a run-time ``n`` is not a static type.
    """
    if n < 1:
        raise ValueError(f"a candidate set holds at least one candidate; got n={n}")
    return Annotated[
        tuple[DiagnosisCandidate, ...],
        Field(
            min_length=n,
            max_length=n,
            description=(
                f"Exactly {n} candidate diagnoses — not fewer and not more. A short "
                f"or long list is rejected rather than trimmed or padded. "
                f"{_SET_DESCRIPTION}"
            ),
        ),
        AfterValidator(_one_candidate_each),
    ]


class CandidateSet(StructuredOutput):
    model_config = ConfigDict(frozen=True, extra="forbid")

    candidates: CandidateTuple

    @property
    def top(self) -> DiagnosisCandidate:
        """The highest-confidence candidate — index 0 after validation."""
        return self.candidates[0]
