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

One field the plan's schema names is still ``None`` here: ``elapsed_ms``, the
wall time of a call, because nothing in ``llm/client.py`` times one and a
fabricated duration is worse than an absent one. ``None`` says "not measured"
where a zero would read as a measurement.

WP-2.1 filled the rest. ``_plan_next_step`` now returns a ``PlannerCall``
beside the state and the step, so the four token counters, the call's own
trace-record id and the size of the context the planner was handed are
measured where they are visible instead of left at zero.
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
class PlannerCall:
    """What one planner call reported, carried out of ``_plan_next_step``.

    Not a trace record: it is the measurement a strategy needs to *fill* one.
    Everything here is visible only inside the function that makes the call —
    the four token counters live on ``LLMResult``, the trace-record id is
    minted by the client, and the rendered prompt is a local — so before
    WP-2.1 widened that return, the record's ``planner_input_tokens``,
    ``call_id`` and ``generation_call_id`` had no honest value and sat at
    ``None`` / ``""``.

    The counters describe the call that PARSED. A billed re-ask before it
    (ADR 0035) is in the ledger delta on ``LLMCallRecord.tokens_used`` and in
    the trace as its own ``llm`` record naming what it repaired — so a
    repaired step's record shows both what the accepted call was fed and what
    the whole step cost, and neither number pretends to be the other.
    """

    #: Trace-record id of the call that parsed, or ``""`` when the client is
    #: untraced (every canned run, and any live run without ``EVAL_TRACE_DIR``).
    record_id: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    #: Characters of system prompt + user message in the FIRST ask of this
    #: step — the context the step was planned from. Measured locally, so it
    #: is a real number on a canned run, where the fake client bills nothing
    #: and every provider-reported counter above is honestly zero.
    context_chars: int = 0

    @property
    def context_tokens(self) -> int:
        """Provider-reported size of the context the model was fed.

        The sum of the three input-side counters, not ``input_tokens`` alone:
        ``llm/client.py`` caches the system prompt, so most of a real call's
        context arrives on ``cache_read_tokens`` and reading only the first
        counter would report a shrinking context as the cache warms.
        """
        return self.input_tokens + self.cache_read_tokens + self.cache_creation_tokens


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

    Two different numbers, both wanted, neither substitutable for the other:

    * ``tokens_used`` / ``usd_used`` are the budget ledger's own delta across
      the step — what ADR 0015 holds the run to. They include a repair's
      second call and any billed-then-discarded attempt, so they are the
      honest cost of the step.
    * the four counters are what the call that PARSED reported. They are the
      split a context/cost comparison across strategies needs (WP-2.3), and
      they cannot see a discarded attempt, which is exactly why they do not
      replace the ledger delta.

    ``call_id`` is the call's trace-record id, so a reader can put this step
    beside the ``llm`` record holding its full request and response in the
    same JSONL. ``""`` when the client is untraced.
    """

    role: str
    model: str
    #: Total token volume charged to the ledger by this step (ADR 0015's
    #: definition: input + output + cache creation + cache read + discarded).
    tokens_used: int
    usd_used: Decimal
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
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
    #: Context size fed to the planner this step, as the provider counted it
    #: (``PlannerCall.context_tokens``). ``None`` when no call was measured.
    #: A canned run reports 0: the fake client bills nothing, and 0 is the
    #: true number of tokens it charged for the context it was handed.
    planner_input_tokens: int | None = None
    #: The same context measured locally, in characters of prompt text. An
    #: extension beyond plan 02 § 7's schema, and the reason for it is
    #: divergence D1: the offline suite runs on a fake client, so every
    #: provider-reported token count there is honestly zero and a record with
    #: only ``planner_input_tokens`` says nothing about how much context an
    #: offline run's planner actually saw. Characters are not tokens and are
    #: never reported as if they were — they are a measurement the canned
    #: suite can make.
    planner_context_chars: int | None = None

    def as_trace_record(self) -> dict[str, Any]:
        """JSON-safe dict, ready for a tracer.

        The tracer's ``kind`` is deliberately not set here. ``TraceKind`` is a
        closed enum whose every member must have a human-report formatter
        (``tests/unit/test_format_traces.py::TestEveryKindRenders``), and
        stamping the kind is the writer's job: ``evals/runner.py`` wraps this
        dict as ``{"kind": TraceKind.STEP, **record}`` on its way to the
        tracer. A record that named its own kind would let a strategy write a
        kind the enumeration has never heard of.
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
            "planner_context_chars": self.planner_context_chars,
        }


#: Where a strategy writes the records it produces. ``None`` at the call site
#: means "nobody is recording", which is the state of every offline run today:
#: ``evals/runner.py`` builds a tracer only when ``EVAL_TRACE_DIR`` is set, and
#: the ``make eval`` target behind ``make eval-reg`` does not set it
#: (divergence D1). Keeping the sink optional is what lets this packet produce
#: the record without changing a single byte of the canned suite's output.
StepSink = Callable[["StepRecord"], None]
