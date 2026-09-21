"""``StepRecord`` — the trace-side record of one planner step (plan 02 § 7).

**Trace data, never state:** ``RunState`` is the checkpoint, research data goes to the trace
store. Every strategy emits one per planner step, so records compare across arms. No hidden
chain-of-thought is stored, and ``None`` on a counter means "not measured", never zero.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any

from incident_commander.agent.hypothesis import Hypothesis, HypothesisCategory, InvestigationStep
from incident_commander.agent.incidents import IncidentSlots
from incident_commander.llm.client import LLMUsage


def _new_id() -> str:
    """A 12-hex id, the same width and mint as ``evals.tracing``'s record ids."""
    return uuid.uuid4().hex[:12]


@dataclass(frozen=True, slots=True, kw_only=True)
class PlannerCall:
    """What one planner call reported, carried out of ``_plan_next_step``.

    The counters describe the call that PARSED; a billed re-ask before it (ADR 0035) shows up
    in ``LLMCallRecord.tokens_used`` and as its own ``llm`` trace record.
    """

    #: Id of the trace record for the call whose output parsed, or empty where the client
    #: writes no trace — every offline run, and any live run with no trace directory set.
    record_id: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    #: How many characters of prompt this step's first ask sent. Counted here rather than
    #: reported by the provider, so it is a real number even on an offline run.
    context_chars: int = 0
    #: How long the call whose output parsed took, as the client measured it; a rejected
    #: re-ask before it is timed as its own call. ``None`` where the client does not time.
    elapsed_ms: int | None = None
    #: Everything this step's planner call was billed, re-asked attempts included, so an arm
    #: whose LATER call fails can still report what this one cost.
    billed_usage: LLMUsage | None = None

    @property
    def context_tokens(self) -> int:
        """Provider-reported size of the context the model was fed — all three input-side
        counters, since the system prompt is cached and the first alone shrinks."""
        return self.input_tokens + self.cache_read_tokens + self.cache_creation_tokens


@dataclass(frozen=True, slots=True, kw_only=True)
class CandidateRecord:
    """One candidate diagnosis a strategy considered — the whole set is recorded, not the pick."""

    candidate_id: str = field(default_factory=_new_id)
    category: HypothesisCategory
    name: str
    confidence: float
    #: Which evidence entries this candidate cites for and against itself. Empty for
    #: ``baseline``, whose planner is never asked to cite any, so it said nothing either way.
    evidence_for: tuple[str, ...] = ()
    evidence_against: tuple[str, ...] = ()
    #: The read this candidate would make next, where the step is a probe; ``None`` where the
    #: step remediates or stops instead.
    proposed_probe: str | None = None
    #: Id of the trace record for the model call that produced this candidate, or empty where
    #: the client writes no trace.
    generation_call_id: str = ""

    def as_record(self) -> dict[str, Any]:
        """JSON-safe dict. ``category`` is a ``StrEnum`` and needs no coercion."""
        return asdict(self)


@dataclass(frozen=True, slots=True, kw_only=True)
class SelectorRecord:
    """The ``candidate_selector`` role's decision over a candidate set. ``None`` on every
    ``baseline`` and best-of-N-only record: nothing to select between."""

    #: ``None`` where the selector asked for another read or escalated instead of choosing: it
    #: names a candidate only when it commits to one, and empty would read as a real id.
    selected_candidate_id: str | None
    scores: dict[str, float] = field(default_factory=dict)
    uncertainty: float | None = None
    #: What the selector decided: ``select``, ``probe_more`` or ``escalate``. Held as a plain
    #: string, because importing the enum here would make this module import the selector.
    decision: str
    call_id: str = ""

    def as_record(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True, kw_only=True)
class RevisionRecord:
    """The ``reflection`` pass over one step: the step first proposed, and the critique of it.

    Both steps are here — ``initial_step`` and the record's ``emitted_step`` — because a pass
    that kept only its output could not be measured for HARM.
    """

    #: The step the first planner call proposed, before any critique — the same thing
    #: ``baseline`` would have handed the loop from that turn.
    initial_step: InvestigationStep
    #: What the critic decided: ``keep`` or ``revise``. A plain string, for the same reason
    #: the selector's decision above is one.
    verdict: str
    #: Every problem the critique named, each prefixed with the kind of problem it is.
    findings: tuple[str, ...] = ()
    #: The evidence entries the critique said the proposed step contradicts.
    contradicted_evidence_ids: tuple[str, ...] = ()
    #: Whether a second planner call actually ran. Today that is the same as the verdict being
    #: ``revise``, but it is recorded separately so any later gate between the two is visible.
    revised: bool = False
    #: How many revision passes were taken, and the limit they were taken against. Both written
    #: down, so reading an old record needs no knowledge of that release's constant.
    passes_used: int = 0
    passes_allowed: int = 1
    critic_call_id: str = ""
    revision_call_id: str = ""

    def as_record(self) -> dict[str, Any]:
        """JSON-safe dict. ``initial_step`` is a Pydantic model and needs dumping."""
        return {
            "initial_step": self.initial_step.model_dump(mode="json"),
            "verdict": self.verdict,
            "findings": list(self.findings),
            "contradicted_evidence_ids": list(self.contradicted_evidence_ids),
            "revised": self.revised,
            "passes_used": self.passes_used,
            "passes_allowed": self.passes_allowed,
            "critic_call_id": self.critic_call_id,
            "revision_call_id": self.revision_call_id,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class BranchRecord:
    """One node of a ``search`` walk, as the trace holds it (plan 02 § 14).

    ``probe`` carries its ARGUMENTS as well as its tool name: filtered and unfiltered reads
    share a name (INC-002), and "which read" is what a branch IS.
    """

    branch_id: str
    #: Empty on the root node, which is the step the generator proposed before any branch ran.
    parent_id: str = ""
    depth: int
    evidence_snapshot_ref: str
    #: Which candidate diagnosis proposed the read that opened this branch, so a reader can tie
    #: the branch to the diagnosis that wanted it. Empty on the root.
    candidate_id: str = ""
    #: The read that was actually made to reach this node; ``None`` on the root, where none was.
    probe: str | None = None
    probe_arguments: dict[str, Any] = field(default_factory=dict)
    #: The read this node would make next; ``None`` where its path would stop or remediate.
    proposed_probe: str | None = None
    #: This node's score and each of the four terms it is made of, so a reader can see which
    #: term decided the walk rather than only that one number beat another.
    score: float
    selector_confidence: float
    tool_cost: float
    token_cost: float
    safety_risk: float
    #: What this one node cost, which is the per-branch figure a report reads.
    tool_calls_used: int = 0
    tokens_used: int = 0
    usd_used: Decimal = Decimal("0")
    #: True on exactly one node per step: the path the run actually took.
    chosen: bool = False
    #: Why this branch never became a scored node — the read was not allowed, the recording had
    #: no answer for it, or the shared budget was spent. ``None`` on every node that ran.
    refused: str | None = None

    def as_record(self) -> dict[str, Any]:
        """JSON-safe dict. ``usd_used`` is stringified, as ``LLMCallRecord``'s is."""
        return {**asdict(self), "usd_used": str(self.usd_used)}


@dataclass(frozen=True, slots=True, kw_only=True)
class SearchRecord:
    """One ``search`` step's whole walk: every branch, and the bounds it ran under.

    The bounds are written, not implied. ``pruned_by_ledger`` is what makes a short walk
    readable as "the shared budget stopped it" rather than as a defect.
    """

    depth_allowed: int
    depth_used: int
    branch_allowed: int
    branches_taken: int
    branches_refused: int
    pruned_by_ledger: int = 0
    nodes: tuple[BranchRecord, ...] = ()
    chosen_branch_id: str = ""
    #: What the whole walk spent, branches and chosen path together, which is the number the
    #: claim about one shared ceiling is checked against.
    tool_calls_used: int = 0
    tokens_used: int = 0

    def as_record(self) -> dict[str, Any]:
        return {
            "depth_allowed": self.depth_allowed,
            "depth_used": self.depth_used,
            "branch_allowed": self.branch_allowed,
            "branches_taken": self.branches_taken,
            "branches_refused": self.branches_refused,
            "pruned_by_ledger": self.pruned_by_ledger,
            "nodes": [node.as_record() for node in self.nodes],
            "chosen_branch_id": self.chosen_branch_id,
            "tool_calls_used": self.tool_calls_used,
            "tokens_used": self.tokens_used,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class RungRecord:
    """One rung of the ``adaptive`` ladder: why it was entered, what it cost, what it left.

    ``entered_because`` is the rung below's fired signals, so a reader follows the TRANSITIONS
    rather than inferring them from which rungs are present (WP-13.2).
    """

    #: Which rung this is, as a plain string, for the same reason the selector's decision is one.
    rung: str
    #: This rung's position in the ladder, counting from zero.
    index: int
    #: Which uncertainty signals sent the step up to this rung. Empty on the first rung, which
    #: every step enters: the point of the ladder is that it starts at the control group.
    entered_because: tuple[str, ...] = ()
    #: Which signals were still firing after this rung ran, and which nothing here could measure.
    fired: tuple[str, ...] = ()
    unmeasured: tuple[str, ...] = ()
    #: Each firing signal written out in words, with the number that made it fire.
    reasons: tuple[str, ...] = ()
    #: How many model calls this rung made. Zero on the ``escalate`` rung, which makes none.
    llm_calls: int = 0
    #: What this rung alone cost, so "what did the ladder add" is read off the record rather
    #: than worked out from the rungs around it.
    tokens_used: int = 0
    usd_used: Decimal = Decimal("0")
    tool_calls_used: int = 0
    #: Exactly one rung per step produced the step the loop ran; every rung before it climbed.
    climbed: bool = False
    emitted: bool = False

    def as_record(self) -> dict[str, Any]:
        """JSON-safe dict. ``usd_used`` is stringified, as ``LLMCallRecord``'s is."""
        return {**asdict(self), "usd_used": str(self.usd_used)}


@dataclass(frozen=True, slots=True, kw_only=True)
class LadderRecord:
    """The ``adaptive`` climb over one step (plan 02 § 15). ``None`` on every other strategy.

    ``terminated_on`` is what the Pareto report groups by; ``extra_llm_calls`` is the cheapness
    claim as a number, 0 on a step that stayed on the baseline rung.
    """

    #: The rungs this step could have climbed, in order, with the last one resolved: ``search``
    #: where this run may read the world in a branch, ``escalate`` where it may not.
    ladder: tuple[str, ...]
    terminated_on: str
    rungs_used: int
    #: How many model calls this step made beyond the first rung's one, which is the arm's
    #: claim that an easy step stays cheap, stated as a number that can be checked.
    extra_llm_calls: int
    #: Whether the search rung could be reached at all on this run, so a run that was unable
    #: to climb to it is never read as one that chose not to.
    search_available: bool
    #: Signals still firing on the last rung that no amount of extra thinking could clear —
    #: today only the failed-remediation one, which is about the run rather than this step.
    unclearable: tuple[str, ...] = ()
    #: The value of every threshold this climb actually compared against; which data split each
    #: one came from is stored with the run's settings.
    thresholds: dict[str, float] = field(default_factory=dict)
    rungs: tuple[RungRecord, ...] = ()

    def as_record(self) -> dict[str, Any]:
        return {
            "ladder": list(self.ladder),
            "terminated_on": self.terminated_on,
            "rungs_used": self.rungs_used,
            "extra_llm_calls": self.extra_llm_calls,
            "search_available": self.search_available,
            "unclearable": list(self.unclearable),
            "thresholds": dict(self.thresholds),
            "rungs": [rung.as_record() for rung in self.rungs],
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class LLMCallRecord:
    """What one LLM call inside a planner step billed.

    Two numbers, neither substitutable: ``tokens_used`` / ``usd_used`` are the ledger's delta
    across the step, repairs included (ADR 0015); the counters are what the call that PARSED
    reported.
    """

    role: str
    model: str
    #: Every token this step charged to the run's budget: input, output, cache writes, cache
    #: reads, and the output of any attempt that was billed and then thrown away.
    tokens_used: int
    usd_used: Decimal
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    call_id: str = ""
    #: How long the call whose output parsed took. ``None`` means nobody measured it; a zero
    #: here would be read as a call that returned instantly.
    elapsed_ms: int | None = None

    def as_record(self) -> dict[str, Any]:
        """JSON-safe dict. ``usd_used`` is stringified: a float would quietly change the
        number the ledger recorded."""
        return {**asdict(self), "usd_used": str(self.usd_used)}


@dataclass(frozen=True, slots=True, kw_only=True)
class StepRecord:
    """One planner step, as the trace store sees it (plan 02 § 7). ``run_id`` is
    ``RunState.incident_id``: one live run per incident (ADR 0002)."""

    step_id: str = field(default_factory=_new_id)
    run_id: str
    iteration: int
    strategy: str
    model: str
    candidate_set: tuple[CandidateRecord, ...]
    selector: SelectorRecord | None = None
    #: The critique-and-revise pass over this step, or ``None`` where no arm ran one.
    revision: RevisionRecord | None = None
    #: The exploration walk over this step, or ``None`` where no arm ran one.
    search: SearchRecord | None = None
    #: The ladder climb over this step, or ``None`` where no arm ran one.
    ladder: LadderRecord | None = None
    #: The step actually handed back to the loop: the one thing in this record that changes
    #: what the run does next.
    emitted_step: InvestigationStep
    hypothesis_state_before: tuple[Hypothesis, ...] = ()
    hypothesis_state_after: tuple[Hypothesis, ...] = ()
    #: The causes this step named, sorted into slots by the loop, which is where the confidence
    #: bar lives. ``None`` means nobody worked them out, never that there were none.
    incidents: IncidentSlots | None = None
    llm_calls: tuple[LLMCallRecord, ...] = ()
    #: How many tokens of context the planner was fed this step, as the provider counted them.
    #: ``None`` where nothing was measured; an offline run reports 0, which is what it was billed.
    planner_input_tokens: int | None = None
    #: The same context measured here instead, in characters, so there is still a real number on
    #: an offline run, where the fake client honestly reports zero tokens.
    planner_context_chars: int | None = None
    #: What was wrong with each billed call that was rejected before the accepted one; the
    #: candidate metrics are computed from this.
    generation_rejections: tuple[str, ...] = ()

    def as_trace_record(self) -> dict[str, Any]:
        """JSON-safe dict, ready for a tracer. ``kind`` is not set here: ``TraceKind`` is
        closed, and stamping it is ``evals/runner.py``'s job."""
        return {
            "step_id": self.step_id,
            "run_id": self.run_id,
            "iteration": self.iteration,
            "strategy": self.strategy,
            "model": self.model,
            "candidate_set": [candidate.as_record() for candidate in self.candidate_set],
            "selector": None if self.selector is None else self.selector.as_record(),
            "revision": None if self.revision is None else self.revision.as_record(),
            "search": None if self.search is None else self.search.as_record(),
            "ladder": None if self.ladder is None else self.ladder.as_record(),
            "emitted_step": self.emitted_step.model_dump(mode="json"),
            "hypothesis_state_before": [
                hypothesis.model_dump(mode="json") for hypothesis in self.hypothesis_state_before
            ],
            "hypothesis_state_after": [
                hypothesis.model_dump(mode="json") for hypothesis in self.hypothesis_state_after
            ],
            "incidents": None if self.incidents is None else self.incidents.model_dump(mode="json"),
            "llm_calls": [call.as_record() for call in self.llm_calls],
            "planner_input_tokens": self.planner_input_tokens,
            "planner_context_chars": self.planner_context_chars,
            "generation_rejections": list(self.generation_rejections),
        }


#: Where a strategy sends its research records. ``None`` means nothing is recording, which is the
#: case for every run without a trace directory set.
StepSink = Callable[["StepRecord"], None]
