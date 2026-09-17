"""``SelectionResult`` and the ``candidate_selector`` call (plan 02 § 12, WP-6.1).

The selector is the first genuinely new LLM role in the system. It is handed a
candidate set — several distinct diagnoses of one world, produced by either
best-of-N arm — and it says which one the run should act on, or that it wants
another probe, or that it wants a human. It is the agent side of the trust
boundary: it reads the alert, the evidence ledger and the candidates, and it
never reads a ground truth.

What this module is, and what WP-6.2 adds
-----------------------------------------

Here: the output schema with its validators, the context the selector is shown,
and the one function that makes the call. There is no strategy here — nothing in
this module decides a step, emits an action or touches the loop. WP-6.2's
``strategies/candidate_selector.py`` composes this over a generator arm and is
where a decision becomes an ``InvestigationStep``.

Four properties are enforced as validators rather than asked for in prose, for
the same reason ``agent/candidates.py`` gives: a rule the schema enforces is a
rule that cannot be half-followed.

1. **Every scored id is a real candidate, and every real candidate is scored.**
   ``scores`` is keyed by ``candidate_id`` and is the only thing that ranks the
   set, so an id that names no candidate makes ``selected_candidate_id``
   unresolvable and a candidate with no score is a candidate the selector
   silently declined to consider. Both fail, and the message names the id.
2. **A selection is stated exactly when there is one.** ``selected_candidate_id``
   is ``None`` if and only if the decision is not ``select`` — see "What
   ``probe_more`` points at" below, which is where the plan's own two sentences
   about this had to be reconciled.
3. **The scale is declared.** ``scores`` and ``uncertainty`` are both in
   ``[0, 1]``. WP-6.3 calibrates the selector's uncertainty against whether it
   was right, and a number on an undeclared scale cannot be calibrated — 0.8
   would mean one thing in a run that scored out of 1 and another in a run that
   scored out of 100, and nothing in the record would say which.
4. **The candidate set is bound, fail-closed.** Validation happens inside
   ``selecting_among(candidates)``; with nothing bound it **refuses**. Same
   mechanism and same reasoning as ``candidates.grounded_in``: the check needs
   the payload and the set at once, and raising *inside* ``llm_client.call`` is
   what makes an unresolvable id an ordinary output failure that ADR 0035
   repairs once and then escalates, rather than a crash outside the repair loop.

The context: one renderer, INC-002's rule from day one
------------------------------------------------------

The evidence the selector reads is rendered through ``briefing.render_trail`` —
the function cmd #221 produced for the briefing writer and the briefing judge —
so each probe appears as ``tool(arguments) -> result`` with its arguments first.
It is not a fourth renderer and it is not a copy.

INC-002 is why. The briefing judge was shown
``list_dlq_messages: {"total":0,"items":[]}`` with the
``remediation_hint='replay_safe'`` that scoped it stripped away, read it as "the
queue is empty", and scored an honest briefing 0.0 for groundedness. The root
cause was two halves: the rule went to one reader, and the record dropped the
arguments so the scope was unrecoverable from the judge's context. The selector
is a new reader of the same evidence, and it gets the same reading rule in the
same change — in its prompt, and structurally, by being rendered through the
same function. ``RunState.evidence`` has carried ``arguments`` all along, so
this is a renderer choice and not a schema change.

What ``probe_more`` points at
-----------------------------

Plan 02 says two things that cannot both be literally true. § 12's schema and
WO-R3-208's own acceptance say ``selected_candidate_id`` is ``None`` exactly
when the decision is not ``select``; 02:239 says "if decision == probe_more, the
selected candidate's next_probe is emitted". If ``probe_more`` carries no
selected id, there is no "selected candidate" to take a probe from.

Resolved in favour of the acceptance text, with the gap closed by derivation
rather than by widening the schema: ``select`` means "commit to this diagnosis",
so it is the only decision that names one, and on ``probe_more`` the candidate
whose ``next_probe`` is emitted is the **highest-scored** one. ``chosen_candidate_id``
below is the single spelling of "the candidate this decision points at", so
WP-6.2 reads one property instead of re-deriving the rule. The divergence is
reported rather than smoothed over; see this packet's PR body.

Temperature (decision O-24)
---------------------------

Plan 04:161 asks for "temperature 0". Nothing here sends a temperature, and
nothing here can be configured to. Anthropic's newest model families reject the
sampling parameters outright (``llm/client.SAMPLING_REJECTED_MODELS``), so a
selector that *required* ``temperature=0`` would 400 on the first selector call
under a newer pin — and the headline experiment of the whole buildout would be
un-runnable for a reason that has nothing to do with selection.

What the plan wanted from temperature 0 is a selector that does not wander, and
this schema buys that structurally instead: forced tool use against a fixed
schema, a closed decision set, a declared scale, and an id space bounded by the
candidate set. ``tests/unit/test_selector_schema.py`` asserts that the call
sends no temperature at all, which is the assertion this repo can keep under
every pin.
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

#: Prompt file this role is asked with. Named so the strategy, the snapshot
#: suite and the loader all spell it once.
SELECTOR_PROMPT: Final[str] = "candidate_selector"

#: Role label for the accounting split and the trace. Its own role, not the
#: planner's: the selector is a different call with a different prompt, and a
#: cost breakdown that folded it into ``investigation_planner`` could not
#: answer "what did selection cost" — which is the number WP-6.2's arm is
#: compared on. ``StepAccounting.selector_calls`` already counts the calls;
#: this is what charges them.
SELECTOR_ROLE: Final[str] = "candidate_selector"

#: The candidate ids one ``SelectionResult`` may refer to, for the duration of
#: one selector call. ``None`` means nothing is bound, which is a refusal and
#: not a licence. A ``ContextVar`` rather than a module global so concurrent
#: runs in one process cannot read each other's candidate set.
_CANDIDATES: ContextVar[tuple[str, ...] | None] = ContextVar(
    "incident_commander_selector_candidates", default=None
)

#: Named so a refusal reads the same wherever it is raised from, and so a test
#: asserts the guard's own marker rather than the fact that something raised
#: (F-007).
NO_CANDIDATES_BOUND: Final[str] = "no candidate set is bound"
UNKNOWN_CANDIDATE_ID: Final[str] = "names no candidate in the set you were shown"
UNSCORED_CANDIDATE: Final[str] = "was not scored"
SELECTION_WITHOUT_SELECT: Final[str] = "selected_candidate_id is set on a decision that is not"
SELECT_WITHOUT_SELECTION: Final[str] = "decision is 'select' and no candidate is selected"


@contextmanager
def selecting_among(candidates: Iterable[DiagnosisCandidate]) -> Iterator[None]:
    """Bind the candidate set a ``SelectionResult`` is resolved against.

    Wrap the selector call with ``selecting_among(candidate_set)``. Re-entrant:
    the previous binding is restored on exit, so a nested call cannot leave a
    stale set behind.
    """
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
    # No class docstring: this enum is a field type on ``SelectionResult``,
    # whose JSON schema is shown to the model on the ``record_output`` tool,
    # and pydantic copies an enum's class docstring into
    # ``$defs.<Enum>.description`` (LESSONS 2026-09-17, plat #210). The prose
    # belongs in the module docstring and in the prompt, where a reviewer reads
    # it. The three members are plan 02 § 12's ``Literal``, as an enum so the
    # strategy that branches on them has names to branch on rather than three
    # string literals repeated at three call sites.
    SELECT = "select"
    PROBE_MORE = "probe_more"
    ESCALATE = "escalate"


class SelectionResult(StructuredOutput):
    # No class docstring here either, same reason as above.
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

        The first half is plan 02 § 12's validator and the packet's red-before
        case. The second half is a deliberate strengthening: ``scores`` is the
        only ranking of the set, so a missing entry is a candidate the selector
        declined to consider without saying so — and after WP-6.2 it is also a
        candidate that cannot be the one ``probe_more`` points at. Both
        directions name the id, because the id is what the person (or the
        repair re-ask) needs.
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

        Both directions are failures worth catching. An id on ``escalate`` reads
        as a diagnosis the run then does not act on; a ``select`` with no id is
        a commitment to nothing, and the strategy that reads it would have to
        invent a fallback — which is how a decision the model did not make
        becomes an action the run takes.
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
        """The candidate this decision points at — one spelling, one rule.

        ``select`` points at the candidate it named. ``probe_more`` points at
        the highest-scored candidate, because it names none and a probe has to
        come from somewhere (see the module docstring). Ties go to the first
        such id in ``scores``, which is the order the model stated them in.
        ``escalate`` points at nothing: the run is over.
        """
        if self.decision is SelectionDecision.SELECT:
            return self.selected_candidate_id
        if self.decision is SelectionDecision.PROBE_MORE and self.scores:
            return max(self.scores, key=lambda candidate_id: self.scores[candidate_id])
        return None


#: Headings of the selector's context block, named so the prompt and the tests
#: spell them once each.
CANDIDATES_HEADING: Final[str] = "Candidate diagnoses:"
ALERT_PREFIX: Final[str] = "Alert: "


def format_selection_context(run_state: RunState, candidates: Sequence[DiagnosisCandidate]) -> str:
    """What the ``candidate_selector`` is shown: the alert, the trail, the set.

    The trail comes from ``briefing.render_trail``, so each probe reads
    ``tool(arguments) -> result`` with its arguments first — the INC-002 rule,
    applied through the same function the briefing writer and the briefing judge
    read, not a copy of it.

    Nothing evaluator-side is reachable from here. The three inputs are the
    alert (``AgentVisibleScenario.alert``), the run's own evidence ledger and
    the candidate set the model itself produced; ``Scenario.ground_truth`` and
    ``discriminating_probes`` are not on this function's arguments at all, which
    is the allow-list projection of ADR 0038 holding one layer further in.
    ``tests/unit/test_selector_schema.py`` asserts it over the whole corpus.
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
    """A candidate's citations, or ``none`` — the word, so silence is visible.

    Same choice ``best_of_n_enumerated.citation_reasoning`` makes: an empty
    list rendered as nothing at all reads as a rendering gap, and "this
    candidate cited nothing" is a fact the selector should weigh.
    """
    return ", ".join(str(ref.evidence_id) for ref in refs) or "none"


def _probe(candidate: DiagnosisCandidate) -> str:
    """The probe this candidate would run next, with its arguments.

    Arguments included for the same reason the trail carries them: a probe is
    identified by what it reads, and ``list_dlq_messages`` filtered and
    unfiltered are two different reads under one name (INC-002).
    """
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

    One call, through ``call_with_output_repair`` like every other structured
    call in this repo, so an unresolvable id or a missing score is an ordinary
    output failure: ADR 0035 re-asks once with the validation error, and ADR
    0015 charges both legs. The caller accrues — ``accounting.accrue_structured_call``
    — because a selector call the ledger did not see would make every cost
    number for this arm a lower bound.

    No temperature is sent, and there is no parameter to send one with; see the
    module docstring on decision O-24.

    ``selecting_among`` wraps the call rather than the parse afterwards, which
    is what puts the validators inside ``llm_client.call`` and so inside the
    repair loop (``agent/candidates.py`` explains the choice at length).
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
