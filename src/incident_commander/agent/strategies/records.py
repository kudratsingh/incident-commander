"""``StepRecord`` — the trace-side record of one planner step (plan 02 § 7).

Produced here, consumed by WP-2.1. Three properties are load-bearing:

* **It is trace data, never state.** ``RunState`` is a frozen ``schema_version
  = 3`` checkpoint holding the latest hypothesis ranking and nothing else
  (divergence C7); research data goes to the trace store. Writing a candidate
  set into the checkpoint would change the schema every future strategy
  touches, and none of it is needed to resume a run.
* **Every strategy emits one per planner step**, so a run's records are
  comparable across strategies. ``baseline`` emits a one-candidate set — it
  considers exactly one diagnosis, because the planner returns one ranking and
  there is no enumeration behind it. That is the shape of the control group,
  not a placeholder.
* **No hidden chain-of-thought is stored** (plan 02 § 7). The structured output
  the schema already asks for, and the short ``reasoning`` field on each
  hypothesis, only.

Two fields the plan's schema names are ``None`` on this seam rather than
invented: ``elapsed_ms`` (the LLM client does not time calls) and
``planner_input_tokens`` (the context size is not returned by
``_plan_next_step``, whose signature plan 02 § 4 and divergence A1 pin
verbatim). They are declared so WP-2.1 fills a named field instead of adding
one, and ``None`` says "not measured here" where a zero would read as a
measurement.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any

from incident_commander.agent.hypothesis import Hypothesis, HypothesisCategory, InvestigationStep


def _new_id() -> str:
    """A 12-hex id, the same width and mint as ``evals.tracing``'s record ids."""
    return uuid.uuid4().hex[:12]


@dataclass(frozen=True, slots=True, kw_only=True)
class CandidateRecord:
    """One candidate diagnosis a strategy considered, kept or discarded.

    ``baseline`` produces exactly one of these per step. A best-of-N strategy
    produces N, and the difference between "the candidate that was emitted" and
    "the candidates that were available" is the measurement Phases 5 and 6
    exist to make — which is why the whole set is recorded, not just the pick.
    """

    candidate_id: str = field(default_factory=_new_id)
    category: HypothesisCategory
    name: str
    confidence: float
    #: Evidence-entry references for and against. Empty for ``baseline``: the
    #: planner does not cite evidence per hypothesis today, and an empty tuple
    #: is the honest reading of "it did not say".
    evidence_for: tuple[str, ...] = ()
    evidence_against: tuple[str, ...] = ()
    #: The read tool this candidate would probe next, when the emitted step is
    #: a probe; ``None`` when the step remediates or stops.
    proposed_probe: str | None = None
    #: Trace-record id of the LLM call that generated this candidate, or ``""``
    #: when the seam cannot name it (see ``LLMCallRecord.call_id``).
    generation_call_id: str = ""

    def as_record(self) -> dict[str, Any]:
        """JSON-safe dict. ``category`` is a ``StrEnum`` and needs no coercion."""
        return asdict(self)


@dataclass(frozen=True, slots=True, kw_only=True)
class SelectorRecord:
    """The ``candidate_selector`` role's decision over a candidate set.

    ``None`` on every ``baseline`` record, and on every best-of-N-only record:
    with one candidate there is nothing to select between. The role arrives in
    Phase 6 (plan 02 § 3); the field exists now so the record shape does not
    change under a reader when it does.
    """

    selected_candidate_id: str
    scores: dict[str, float] = field(default_factory=dict)
    uncertainty: float | None = None
    #: ``select`` | ``probe_more`` | ``escalate`` — a Literal once the role has
    #: a prompt and a schema to constrain it (architecture principle 1).
    decision: str
    call_id: str = ""

    def as_record(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True, kw_only=True)
class LLMCallRecord:
    """What one LLM call inside a planner step billed.

    ``call_id`` is the call's trace-record id where the seam can name it, and
    ``""`` where it cannot: ``_plan_next_step`` returns ``(RunState,
    InvestigationStep)`` — the signature plan 02 § 4 writes verbatim and
    divergence A1 confirms — so the ``RepairedCall`` carrying ``record_id`` and
    the four token counters never leaves it. Rather than widen that contract
    (WP-0.2 replaces the call, it does not redesign it), ``baseline`` records
    the budget ledger's own delta for the step, which is the number ADR 0015
    holds the run to and includes a repair's second call and any billed-then-
    discarded attempt. WP-2.1 can widen the return and fill the split in.
    """

    role: str
    model: str
    #: Total token volume charged to the ledger by this step (ADR 0015's
    #: definition: input + output + cache creation + cache read + discarded).
    tokens_used: int
    usd_used: Decimal
    call_id: str = ""
    #: Wall time of the call. ``None`` here: nothing in ``llm/client.py`` times
    #: a call today, and a fabricated duration is worse than an absent one.
    elapsed_ms: int | None = None

    def as_record(self) -> dict[str, Any]:
        """JSON-safe dict. ``usd_used`` is stringified — ``Decimal`` is not JSON,
        and a float would quietly change the number the ledger recorded."""
        return {**asdict(self), "usd_used": str(self.usd_used)}


@dataclass(frozen=True, slots=True, kw_only=True)
class StepRecord:
    """One planner step, as the trace store sees it (plan 02 § 7).

    ``run_id`` is ``RunState.incident_id``: one live run per incident is an
    invariant of the lease (ADR 0002), so the incident's id *is* the run's
    identity in this codebase. Plan 02 § 7 calls the field ``run_id`` and the
    name is kept, with this sentence rather than a second id nothing mints.
    """

    step_id: str = field(default_factory=_new_id)
    run_id: str
    iteration: int
    strategy: str
    model: str
    candidate_set: tuple[CandidateRecord, ...]
    selector: SelectorRecord | None = None
    #: The ``InvestigationStep`` handed back to the loop — the one thing in
    #: this record that has consequences for the run.
    emitted_step: InvestigationStep
    hypothesis_state_before: tuple[Hypothesis, ...] = ()
    hypothesis_state_after: tuple[Hypothesis, ...] = ()
    llm_calls: tuple[LLMCallRecord, ...] = ()
    #: Context size fed to the planner this step. ``None`` at this seam — see
    #: the module docstring.
    planner_input_tokens: int | None = None

    def as_trace_record(self) -> dict[str, Any]:
        """JSON-safe dict, ready for a tracer.

        The tracer's ``kind`` is deliberately not set here: ``TraceKind`` is a
        closed enum whose every member must have a human-report formatter
        (``tests/unit/test_format_traces.py::TestEveryKindRenders``), so the
        ``step`` kind and its renderer land together in WP-2.1 — the packet
        that reads these records — not here, where nothing would render them.
        """
        return {
            "step_id": self.step_id,
            "run_id": self.run_id,
            "iteration": self.iteration,
            "strategy": self.strategy,
            "model": self.model,
            "candidate_set": [candidate.as_record() for candidate in self.candidate_set],
            "selector": None if self.selector is None else self.selector.as_record(),
            "emitted_step": self.emitted_step.model_dump(mode="json"),
            "hypothesis_state_before": [
                hypothesis.model_dump(mode="json") for hypothesis in self.hypothesis_state_before
            ],
            "hypothesis_state_after": [
                hypothesis.model_dump(mode="json") for hypothesis in self.hypothesis_state_after
            ],
            "llm_calls": [call.as_record() for call in self.llm_calls],
            "planner_input_tokens": self.planner_input_tokens,
        }


#: Where a strategy writes the records it produces. ``None`` at the call site
#: means "nobody is recording", which is the state of every offline run today:
#: ``evals/runner.py`` builds a tracer only when ``EVAL_TRACE_DIR`` is set, and
#: the ``make eval`` target behind ``make eval-reg`` does not set it
#: (divergence D1). Keeping the sink optional is what lets this packet produce
#: the record without changing a single byte of the canned suite's output.
StepSink = Callable[["StepRecord"], None]
