"""``StepRecord`` — the trace-side record of one planner step (plan 02 § 7).

Produced here, consumed by WP-2.1. **It is trace data, never state:** ``RunState`` is a frozen
``schema_version = 3`` checkpoint (divergence C7) and research data goes to the trace store.
**Every strategy emits one per planner step**, so a run's records are comparable across
strategies — ``baseline``'s one-candidate set is the control group's shape, not a placeholder.
**No hidden chain-of-thought is stored.** ``elapsed_ms`` is carried since WO-R3-260; ``None``
still means "not measured", never a zero a reader would take for a sub-millisecond call.
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

    Not a trace record: it is the measurement a strategy needs to *fill* one. The counters
    describe the call that PARSED; a billed re-ask before it (ADR 0035) is in the ledger delta
    on ``LLMCallRecord.tokens_used`` and in the trace as its own ``llm`` record.
    """

    #: Trace-record id of the call that parsed, or ``""`` when the client is
    #: untraced (every canned run, and any live run without ``EVAL_TRACE_DIR``).
    record_id: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    #: Characters of system prompt + user message in the FIRST ask of this step. Measured
    #: locally, so it is a real number on a canned run, where every counter above is zero.
    context_chars: int = 0
    #: Wall time of the call that PARSED, as the client measured it: a repair's rejected leg
    #: is its own logical call. ``None`` when the client does not time itself.
    elapsed_ms: int | None = None

    @property
    def context_tokens(self) -> int:
        """Provider-reported size of the context the model was fed.

        All three input-side counters: the system prompt is cached, so the first alone shrinks.
        """
        return self.input_tokens + self.cache_read_tokens + self.cache_creation_tokens


@dataclass(frozen=True, slots=True, kw_only=True)
class CandidateRecord:
    """One candidate diagnosis a strategy considered, kept or discarded.

    The whole set is recorded, not just the pick (Phases 5 and 6).
    """

    candidate_id: str = field(default_factory=_new_id)
    category: HypothesisCategory
    name: str
    confidence: float
    #: Evidence-entry references for and against. Empty for ``baseline``, whose planner does
    #: not cite evidence per hypothesis — the honest reading of "it did not say".
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

    ``None`` on every ``baseline`` and best-of-N-only record: nothing to select between.
    """

    #: ``None`` on ``probe_more`` and ``escalate``: ``SelectionResult`` states a selection only
    #: when it commits to one (ADR 0048), and ``""`` would read as a candidate with an empty id.
    selected_candidate_id: str | None
    scores: dict[str, float] = field(default_factory=dict)
    uncertainty: float | None = None
    #: ``select`` | ``probe_more`` | ``escalate`` — ``SelectionDecision``'s value as a ``str``,
    #: not the enum: this module imports nothing but ``hypothesis``.
    decision: str
    call_id: str = ""

    def as_record(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True, kw_only=True)
class LLMCallRecord:
    """What one LLM call inside a planner step billed.

    Two numbers, neither substitutable: ``tokens_used`` / ``usd_used`` are the ledger's own
    delta across the step (ADR 0015), including a repair's second call; the four counters are
    what the call that PARSED reported, the split WP-2.3 needs. ``call_id`` is its trace id.
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
    #: Wall time of the call that parsed (``LLMResult.elapsed_ms``); it belongs with the four
    #: counters, not the ledger delta. ``None`` means not measured; a zero would read as fast.
    elapsed_ms: int | None = None

    def as_record(self) -> dict[str, Any]:
        """JSON-safe dict. ``usd_used`` is stringified — ``Decimal`` is not JSON,
        and a float would quietly change the number the ledger recorded."""
        return {**asdict(self), "usd_used": str(self.usd_used)}


@dataclass(frozen=True, slots=True, kw_only=True)
class StepRecord:
    """One planner step, as the trace store sees it (plan 02 § 7).

    ``run_id`` is ``RunState.incident_id``: one live run per incident (ADR 0002).
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
    #: Context size fed to the planner this step (``PlannerCall.context_tokens``). ``None`` when
    #: no call was measured; a canned run reports 0, the true number it was charged.
    planner_input_tokens: int | None = None
    #: The same context measured locally, in characters. Beyond plan 02 § 7, for divergence D1:
    #: on the offline suite's fake client every token count is honestly zero.
    planner_context_chars: int | None = None
    #: Validation classes of the billed calls rejected before the accepted one (WP-5.2,
    #: ``best_of_n_enumerated.rejection_class``); feeds ``evals/candidate_metrics.py``.
    generation_rejections: tuple[str, ...] = ()

    def as_trace_record(self) -> dict[str, Any]:
        """JSON-safe dict, ready for a tracer.

        ``kind`` is not set here: ``TraceKind`` is closed
        (``tests/unit/test_format_traces.py::TestEveryKindRenders``) and stamping it is
        ``evals/runner.py``'s job.
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
            "generation_rejections": list(self.generation_rejections),
        }


#: Where a strategy writes its records. ``None`` means nobody is recording, the state of every
#: offline run: ``evals/runner.py`` needs ``EVAL_TRACE_DIR`` (divergence D1).
StepSink = Callable[["StepRecord"], None]
