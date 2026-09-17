"""``DiagnosisCandidate`` and ``EvidenceRef`` — the candidate schema (plan 02 § 11.3).

Every best-of-N and selector packet in Phases 5 and 6 builds on this one
schema, so the properties it enforces are the properties those measurements
can assume. There are four, and each is a validator rather than a sentence in
a prompt:

1. **A reference resolves.** ``EvidenceRef`` carries one ``evidence_id`` from
   the run's ledger (``RunState.evidence``). A ref that names no entry there
   fails validation and the message names the id. Grounding is structural, not
   prompted — a candidate that cites evidence which does not exist must fail
   on the way in, not be caught by a reviewer reading a trajectory later.
2. **A set has no duplicates.** Two candidates with the same
   ``(category, name)`` are one candidate stated twice, and a best-of-N number
   computed over them would count it twice. Two candidates with the same
   ``candidate_id`` are worse: the selector's ``scores`` mapping (plan 02 § 12)
   is keyed by that id, so a repeated id makes ``selected_candidate_id``
   unresolvable.
3. **Ranking is normalised here.** ``candidates[0]`` is the highest-confidence
   candidate after validation, for any order the model emitted, with ties
   keeping the model's stated order. Same rule, the same way, and for the same
   reason as ``InvestigationStep._rank_by_confidence``: "the top candidate" is
   read by index downstream, and prose in a prompt is not what should make
   that true.
4. **A parse failure is a harness event.** All three models inherit
   ``StructuredOutput``, so the ADR-0035 decode of a nested container that
   arrived as a JSON string covers every nested field here — including fields
   that do not exist yet.

Why the ledger arrives through a context variable, rather than as an argument
-----------------------------------------------------------------------------

The grounding check needs two things at once: the candidate payload, and the
ledger to resolve it against. The order this packet implements left the choice
open — "a Pydantic validation-context carrying the ledger, or a boundary check
at the strategy seam" — and the deciding constraint is where the failure has
to be *raised* for ADR 0035 to cover it.

``llm/repair.py::call_with_output_repair`` wraps exactly one expression:
``llm_client.call(...)``. A failure raised inside that call is repairable — one
re-ask carrying the validation error, then escalate. A failure raised *after*
the call returns is not: it is outside the ``try``, so a post-validation
boundary check at the strategy seam would have no repair at all unless
``repair.py`` grew a hook for it. And ``LLMClient._parse`` validates with a
bare ``output_model.model_validate(block.input)`` — no context argument — so
threading a validation context down to it would change
``LLMClientProtocol.call``, every fake, and every one of the five existing
``record_output`` call sites, to carry a parameter only this schema uses.

A context variable bound at the seam is the one option that puts the check
inside the call, as a validator, with no change to the client, the protocol,
the fakes or the repair loop: ``with grounded_in(run_state.evidence):`` around
the planner call, and an ungrounded candidate set becomes an ordinary
``LLMOutputError`` that buys one re-ask and then escalates, exactly like the
stringified ``next_action`` that produced ADR 0035.

It is fail-closed, which is the only reason a global is tolerable here. With
no ledger bound, validation **refuses** — a set with no citations at all fails
too, on ``CandidateTuple``'s own check. Forgetting to bind is therefore a loud
failure on every path rather than grounding silently switched off, which is
what a permissive default would have made it.

What this module deliberately does not do
-----------------------------------------

* **No N of its own.** Plan 02 § 11.1 fixes the set size to the configured N
  at the schema boundary (``min_length=N``, ``max_length=N``). N is
  configuration and the strategy that reads it is WP-5.2, so this module offers
  the bound as a factory — ``exact_candidate_tuple(n)`` — and never a default.
  WP-5.2 found that the expression this docstring originally recommended
  (``Annotated[CandidateTuple, Field(min_length=n, max_length=n)]``) enforces
  the bound and advertises it wrongly; the factory's own docstring has the
  detail.
* **No planner call, no prompt, no strategy.** WP-5.2 built those. The note
  this list used to carry — that the planner context showed no ``evidence_id``,
  so a planner asked to cite one had never seen one — is closed: the rendering
  moved to ``agent/planner_context.py`` and takes a ``show_evidence_ids`` flag,
  off for ``baseline`` and on for the arms whose schema cites them
  (ADR 0044).
* **No ``reasoning`` field.** Plan 02 § 11.3's schema has none, and § 7 is
  explicit that no hidden chain-of-thought is stored. A candidate justifies
  itself with the evidence it cites.
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

#: The evidence ids a candidate set may cite, for the duration of one planner
#: call. ``None`` means nothing is bound, which is a refusal and not a licence
#: — see the module docstring. A ``ContextVar`` rather than a module global so
#: concurrent runs in one process (``asyncio``, threads) cannot read each
#: other's ledger.
_LEDGER: ContextVar[frozenset[UUID] | None] = ContextVar(
    "incident_commander_evidence_ledger", default=None
)

#: Named so the refusal reads the same wherever it is raised from, and so a
#: test asserts the guard's own marker rather than the fact that something
#: raised (F-007).
NO_LEDGER_BOUND: Final[str] = "no evidence ledger is bound"
UNKNOWN_EVIDENCE_ID: Final[str] = "names no entry in the run's evidence ledger"
DUPLICATE_CANDIDATE: Final[str] = "duplicate candidate"
DUPLICATE_CANDIDATE_ID: Final[str] = "duplicate candidate_id"


@contextmanager
def grounded_in(evidence: Iterable[EvidenceEntry]) -> Iterator[None]:
    """Bind the ledger a candidate set is resolved against.

    Wrap the planner call at the strategy seam with
    ``grounded_in(run_state.evidence)``. Re-entrant: the previous binding is
    restored on exit, so a nested call cannot leave a stale ledger behind.
    """
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


# No class docstring on any model below. Pydantic puts a class docstring into
# the model's JSON schema as its ``description``, and these schemas are shown
# to the model through ``record_output``; the prose belongs in the module
# docstring, where it reaches a reader and not a prompt. Field descriptions are
# the opposite case and are deliberate: they are how the schema tells the model
# what to put in the field.


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

        On ``EvidenceRef`` itself rather than on ``DiagnosisCandidate``'s two
        reference fields: the rule belongs where the class of field is defined,
        so ``evidence_against`` is covered by the same line as ``evidence_for``
        and so is every field of this type that does not exist yet. That is the
        lesson ``hypothesis.py`` records about the one-field ``json.loads``
        coercion that covered one field of one model for six weeks.
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
    # The only two defaulted fields here. An omitted citation list and an empty
    # one say the same thing — this candidate cited nothing — so turning the
    # absence into a harness repair would spend a billed call on a distinction
    # with no meaning. ``next_probe`` below is required for the opposite reason.
    evidence_for: tuple[EvidenceRef, ...] = Field(
        default=(),
        description="Evidence ids that support this candidate. May be empty.",
    )
    evidence_against: tuple[EvidenceRef, ...] = Field(
        default=(),
        description="Evidence ids that argue against this candidate. May be empty.",
    )
    # Required, including when the answer is null: "there is nothing left to
    # probe for this candidate" is a claim about the investigation, and plan
    # 02 § 11.3 builds the emitted step from the top candidate's probe — so
    # silence has to be stated rather than inferred from a missing key.
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

    Also the one place that catches an *uncited* set validated with no ledger
    bound: with no references to resolve, ``EvidenceRef`` never runs, and
    without this a forgotten ``grounded_in`` would let an ungrounded set
    through whenever the model happened to cite nothing.

    Duplicates are compared on the pair exactly as stated. ``name`` is
    free-form operator-facing text, and case-folding or collapsing whitespace
    would make two labels a reader can tell apart collide — while the
    duplicate *rate* WP-5.2 reports is a measurement of what the model
    produced, not of what a normaliser could hide.
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


#: What the set-level rules say, in the words the model reads. One string, so
#: the unbounded type and ``exact_candidate_tuple``'s bounded one cannot
#: describe the same rules differently.
_SET_DESCRIPTION: Final[str] = (
    "List them most likely first; ordering is normalized after validation — "
    "entries are re-sorted by confidence descending (stable: equal-confidence "
    "entries keep their listed order), so index 0 is always the top candidate. "
    "Each candidate must have a distinct candidate_id and a distinct "
    "(category, name)."
)

#: The candidate set, as a type rather than as one container's field. A model
#: that declares ``candidates: CandidateTuple`` inherits all three set-level
#: rules; WP-5.2's exact-N schema is ``exact_candidate_tuple(n)`` below and
#: keeps them. Carrying the rules on the type is what stops the next model that
#: needs a candidate set from re-declaring a bare tuple and quietly losing
#: them (architecture-principles rule 2).
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

    Composed here rather than at the call site, and **not** as
    ``Annotated[CandidateTuple, Field(min_length=n, max_length=n)]``, which is
    what this module's docstring said WP-5.2 would write. That expression
    enforces the bound at run time — ``tests/unit/test_candidates.py::
    TestTheExactNBoundWpFiveTwoWillNeed`` proves it does — and then **advertises
    it wrongly**: the extra ``Field`` lands after ``AfterValidator`` in the
    annotation chain, so pydantic emits ``minLength`` / ``maxLength`` on an
    array instead of ``minItems`` / ``maxItems``. Those two keywords mean
    nothing for a JSON-Schema array, so every reader of the schema — including
    the model being asked for exactly N candidates — sees only the inherited
    ``minItems: 1``. The bound would have been enforced against a model that was
    never told about it, turning a configuration into a repair loop.

    Ordering the metadata so the length constraint precedes the validator gives
    the same run-time behaviour and the right schema.
    ``tests/unit/test_best_of_n.py::TestTheExactNSchema`` asserts both halves,
    because the half that was missing is the half no exception reports.

    Returns ``Any`` because the value is an annotation object rather than a
    type: ``Annotated[...]`` built from a run-time ``n`` is not expressible as a
    static return type, and the alternative (a ``TypeAlias`` per N) is the
    hand-written-model-per-N this function exists to avoid.
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
        """The highest-confidence candidate — index 0 after validation.

        A property rather than a caller-side ``[0]`` so "the top candidate" has
        one spelling, and so the guarantee the validator provides is read
        through something that names it.
        """
        return self.candidates[0]
