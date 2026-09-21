"""``search`` — a bounded shallow walk over evidence-gathering decisions (plan 02 § 14, WP-12.1).

Depth ≤ 2, branch ≤ 3, both structural. A branch is a different READ, taken through the loop's
own prober so no branch can act, and the ceilings are the run's own, shared by every branch and
the chosen path. Recorded mode only: ``ctx.branch_prober`` is ``None`` elsewhere (ADR 0060).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from types import MappingProxyType
from typing import Any, Final

from incident_commander.agent.accounting import accrue_structured_call
from incident_commander.agent.candidates import DiagnosisCandidate
from incident_commander.agent.hypothesis import InvestigationStep, NextAction, ProbeAction
from incident_commander.agent.search import (
    DUPLICATE_BRANCH_PROBE,
    SEARCH_IS_RECORDED_MODE_ONLY,
    BranchAllowance,
    NodeCost,
    SearchNode,
    SearchWalk,
    cost_between,
    evidence_snapshot_ref,
    new_node_id,
    room_for_a_branch,
    score_node,
)
from incident_commander.agent.selection import (
    SELECTOR_ROLE,
    SelectionDecision,
    SelectionResult,
    select_candidate,
)
from incident_commander.agent.state import BudgetLedger, RunState
from incident_commander.agent.strategies.candidate_selector import (
    chosen_candidate,
    step_for_selection,
)
from incident_commander.agent.strategies.generation import (
    CandidateGeneration,
    CandidateGenerator,
    candidate_record_of,
)
from incident_commander.agent.strategies.knobs import StrategyKnobs
from incident_commander.agent.strategies.names import StrategyName
from incident_commander.agent.strategies.protocol import StrategyContext
from incident_commander.agent.strategies.records import (
    BranchRecord,
    LLMCallRecord,
    SearchRecord,
    SelectorRecord,
    StepRecord,
)
from incident_commander.llm.client import LLMError, LLMUsage
from incident_commander.llm.repair import RepairedCall, sum_usage, usage_of

#: Named so a test asserts the guard's own marker rather than that something raised (F-007).
NO_BRANCH_PROBER: Final[str] = "no branch prober is on the strategy context"
NO_SELECTOR_CLIENT: Final[str] = "no selector client is on the strategy context"

#: Which call of the walk failed, for ``SearchFailed``'s message.
GENERATION_STAGE: Final[str] = "generation"
SELECTOR_STAGE: Final[str] = "selector"


class SearchFailed(LLMError):
    """A call inside the walk failed, and everything the step billed comes with it.

    An ``LLMError`` so the loop's existing ``except`` arm escalates as for ``baseline``;
    ``usage`` sums every billed leg of the whole walk (ADR 0045).
    """

    def __init__(self, stage: str, cause: BaseException, usage: LLMUsage | None) -> None:
        super().__init__(
            f"search {stage} call failed: {cause}",
            usage=usage,
            record_id=getattr(cause, "record_id", None),
        )
        self.stage = stage
        self.cause = cause


@dataclass(frozen=True, slots=True, kw_only=True)
class _Path:
    """One node of the walk, with the live objects the walk goes on from: ``node`` is the record,
    the rest is what the next level needs — evidence, set, decision, the step it would emit."""

    node: SearchNode
    run_state: RunState
    candidates: tuple[DiagnosisCandidate, ...]
    selection: SelectionResult
    step: InvestigationStep
    #: What a ``select`` at this node acts on: the action its generation proposed.
    committed_action: NextAction
    #: This node's OWN ledger delta; ``node.cost`` is the whole path's.
    own_cost: NodeCost
    generation_call_id: str
    selector_call_id: str
    #: The candidate whose proposed read opened this branch. ``""`` on the root.
    candidate_id: str = ""


class SearchStrategy:
    """A bounded best-first walk per planner step; the chosen path hands off as usual."""

    name: str = StrategyName.SEARCH.value

    def __init__(self, knobs: StrategyKnobs | None = None) -> None:
        """Build the arm, refusing a depth or branch above the structural maximum — at
        construction, so a walk nobody could run costs nothing."""
        from incident_commander.agent.strategies.registry import STRATEGIES

        resolved = knobs if knobs is not None else StrategyKnobs()
        # Built and dropped: it raises ``SearchCapExceeded`` above the maximums, and each step
        # gets its own walk.
        SearchWalk(depth_allowed=resolved.search_depth, branch_allowed=resolved.search_branch)
        self._depth: Final[int] = resolved.search_depth
        self._branch: Final[int] = resolved.search_branch
        # N is the branch factor: one candidate's proposed read is one branch.
        built = STRATEGIES.create(
            resolved.selector_generator, replace(resolved, n=resolved.search_branch)
        )
        if not isinstance(built, CandidateGenerator):
            raise ValueError(
                f"strategy {resolved.selector_generator!r} cannot supply a candidate "
                "set: search branches on the reads a candidate set proposes, and "
                "`baseline` proposes one, so there is nothing to branch between."
            )
        self._generator: Final[CandidateGenerator] = built
        self.config: Mapping[str, Any] = MappingProxyType(
            {
                **dict(built.config),
                "generator": built.name,
                "selector": SELECTOR_ROLE,
                "depth": resolved.search_depth,
                "branch": resolved.search_branch,
                # How the bounds are held, so no row has to trust the two numbers above.
                "cap": "structural",
                # Both ceilings are the run's own, shared by every branch.
                "caps_shared_across_branches": True,
                "mode": "recorded",
            }
        )

    @property
    def generator(self) -> CandidateGenerator:
        """The arm that supplies each node's candidate set."""
        return self._generator

    def plan_next_step(
        self, run_state: RunState, at: datetime, ctx: StrategyContext
    ) -> tuple[RunState, InvestigationStep, StepRecord]:
        """Walk, then hand the loop the chosen path's step.

        Refuses before any call when the context carries no prober: degrading to a walk that
        cannot read would report a search number for a strategy that never searched.
        """
        if ctx.branch_prober is None:
            raise ValueError(f"{NO_BRANCH_PROBER}. {SEARCH_IS_RECORDED_MODE_ONLY}")
        if ctx.selector_llm_client is None:
            raise ValueError(
                f"{NO_SELECTOR_CLIENT}. A node's score starts from the selector's own "
                "confidence (plan 02 § 257), and the selector is its own metered role "
                "(agent/selection.SELECTOR_ROLE); borrowing the planner's client would "
                "report the walk's tokens under the planner's role."
            )
        walk = SearchWalk(depth_allowed=self._depth, branch_allowed=self._branch)
        return _Walk(ctx=ctx, at=at, walk=walk, strategy=self).run(run_state)


@dataclass(slots=True)
class _Walk:
    """One planner step's walk: the shared ledger, the nodes, and the bill.

    Mutable and per-step, so the ledger has ONE carrier: every branch's cost lands in ``ledger``
    before the next branch is considered, which is what a shared cap means.
    """

    ctx: StrategyContext
    at: datetime
    walk: SearchWalk
    strategy: SearchStrategy
    #: The one ledger every call and every read of this step is charged to.
    ledger: BudgetLedger | None = None
    #: The largest single call this step has paid for — the measured token reserve.
    token_reserve: int = 0
    calls: tuple[LLMCallRecord, ...] = ()
    billed: tuple[LLMUsage | None, ...] = ()
    nodes: tuple[BranchRecord, ...] = ()
    branches_taken: int = 0
    branches_refused: int = 0
    pruned_by_ledger: int = 0
    planner_input_tokens: int = 0
    planner_context_chars: int = 0

    def run(self, run_state: RunState) -> tuple[RunState, InvestigationStep, StepRecord]:
        """Root, then one level per bound, then the chosen path's handoff."""
        self.ledger = run_state.budget
        root = self._root(run_state)
        best = root
        frontier = root
        for level in range(self.walk.depth_allowed):
            self.walk.descend()
            children = self._branch_out(frontier)
            if not children:
                break
            frontier = _best_of(children)
            best = _best_of((best, frontier))
            if level + 1 < self.walk.depth_allowed:
                regrown = self._regrow(frontier)
                if regrown is None:
                    break
                frontier = regrown
        return self._handoff(run_state, best)

    # --- the walk ---------------------------------------------------------

    def _root(self, run_state: RunState) -> _Path:
        """Depth 0: the generator's set, scored as the path that gathers nothing more."""
        before = self._budget()
        generation = self._generate(run_state)
        return self._scored(
            run_state=generation.run_state,
            parent_id="",
            depth=0,
            probe_taken=None,
            candidates=generation.candidates,
            committed_action=generation.proposed_step.next_action,
            generation_call_id=_generation_call_id(generation),
            cost_from=before,
            path_cost=NodeCost(),
        )

    def _branch_out(self, parent: _Path) -> tuple[_Path, ...]:
        """Every branch this node can afford: one read each, scored and recorded.

        Stops on the branch allowance, a duplicate read, or the shared ledger — each with its
        own recorded reason, so a short walk is never read as a full one.
        """
        allowance = self.walk.branches()
        children: list[_Path] = []
        taken: set[str] = set()
        for candidate in parent.candidates:
            if allowance.exhausted:
                break
            probe = candidate.next_probe
            if probe is None:
                continue
            fingerprint = _probe_fingerprint(probe)
            if fingerprint in taken:
                self._refused(parent, probe, candidate, DUPLICATE_BRANCH_PROBE)
                continue
            reason = room_for_a_branch(self._budget(), token_reserve=self.token_reserve)
            if reason is not None:
                self._refused(parent, probe, candidate, reason)
                self.pruned_by_ledger += 1
                break
            taken.add(fingerprint)
            child = self._branch(parent, candidate, probe, allowance)
            if child is not None:
                children.append(child)
        return tuple(children)

    def _branch(
        self,
        parent: _Path,
        candidate: DiagnosisCandidate,
        probe: ProbeAction,
        allowance: BranchAllowance,
    ) -> _Path | None:
        """One branch: take the allowance, read the world, score what the reading says."""
        allowance.spend()
        prober = self.ctx.branch_prober
        if prober is None:  # pragma: no cover - refused before the walk starts
            raise ValueError(f"{NO_BRANCH_PROBER}. {SEARCH_IS_RECORDED_MODE_ONLY}")
        before = self._budget()
        outcome = prober(_with_ledger(parent.run_state, before), probe)
        # The ledger moves forward whatever the outcome; whether a refusal cost anything is
        # the ledger's business, not this walk's.
        self.ledger = outcome.run_state.budget
        if outcome.refused is not None:
            self._refused(parent, probe, candidate, outcome.refused)
            return None
        self.branches_taken += 1
        return self._scored(
            run_state=outcome.run_state,
            parent_id=parent.node.node_id,
            depth=parent.node.depth + 1,
            probe_taken=probe,
            candidates=parent.candidates,
            committed_action=parent.committed_action,
            generation_call_id=parent.generation_call_id,
            cost_from=before,
            path_cost=parent.node.cost,
            candidate_id=candidate.candidate_id,
        )

    def _regrow(self, path: _Path) -> _Path | None:
        """One generation over a branch's new evidence, so the next level has fresh reads.

        ``None`` when the shared ledger cannot afford another branch anyway: a set of proposals
        nothing could be spent on is a billed call for nothing.
        """
        if room_for_a_branch(self._budget(), token_reserve=self.token_reserve) is not None:
            self.pruned_by_ledger += 1
            return None
        generation = self._generate(_with_ledger(path.run_state, self._budget()))
        return replace(
            path,
            run_state=generation.run_state,
            candidates=generation.candidates,
            committed_action=generation.proposed_step.next_action,
            generation_call_id=_generation_call_id(generation),
        )

    # --- calls ------------------------------------------------------------

    def _generate(self, run_state: RunState) -> CandidateGeneration:
        """One generation call, charged to the shared ledger."""
        before = self._budget()
        try:
            generation = self.strategy.generator.generate(
                _with_ledger(run_state, before), self.at, self.ctx
            )
        except Exception as err:
            raise SearchFailed(
                GENERATION_STAGE, err, sum_usage(*self.billed, usage_of(err))
            ) from err
        self.billed = (*self.billed, generation.billed_usage)
        self.ledger = generation.run_state.budget
        self._note_reserve(before, generation.run_state.budget)
        self.calls = (*self.calls, *generation.record.llm_calls)
        self.planner_input_tokens += generation.record.planner_input_tokens or 0
        self.planner_context_chars += generation.record.planner_context_chars or 0
        return generation

    def _select(
        self, run_state: RunState, candidates: Sequence[DiagnosisCandidate]
    ) -> tuple[SelectionResult, RunState, str]:
        """One selector call over this node's set, charged to the shared ledger."""
        client = self.ctx.selector_llm_client
        if client is None:  # pragma: no cover - refused before the walk starts
            raise ValueError(NO_SELECTOR_CLIENT)
        before = _with_ledger(run_state, self._budget())
        try:
            call = select_candidate(
                client, run_state=before, candidates=candidates, model=self.ctx.model
            )
        except Exception as err:
            raise SearchFailed(SELECTOR_STAGE, err, sum_usage(*self.billed, usage_of(err))) from err
        self.billed = (*self.billed, _billed(call))
        after = before.model_copy(
            update={
                "budget": accrue_structured_call(before.budget, call, self.ctx.model),
                "updated_at": self.at,
            }
        )
        self.ledger = after.budget
        self._note_reserve(before.budget, after.budget)
        self.calls = (
            *self.calls,
            LLMCallRecord(
                role=SELECTOR_ROLE,
                model=self.ctx.model,
                tokens_used=after.budget.tokens_used - before.budget.tokens_used,
                usd_used=after.budget.usd_used - before.budget.usd_used,
                input_tokens=call.result.input_tokens,
                output_tokens=call.result.output_tokens,
                cache_read_tokens=call.result.cache_read_tokens,
                cache_creation_tokens=call.result.cache_creation_tokens,
                call_id=call.result.record_id,
                elapsed_ms=call.result.elapsed_ms,
            ),
        )
        return call.result.output, after, call.result.record_id

    # --- nodes ------------------------------------------------------------

    def _scored(
        self,
        *,
        run_state: RunState,
        parent_id: str,
        depth: int,
        probe_taken: ProbeAction | None,
        candidates: tuple[DiagnosisCandidate, ...],
        committed_action: NextAction,
        generation_call_id: str,
        cost_from: BudgetLedger,
        path_cost: NodeCost,
        candidate_id: str = "",
    ) -> _Path:
        """Score one node: one selector call, then plan 02 § 257's four terms.

        ``cost_from`` is the ledger before this node's first charge, so a branch's own cost
        includes its read; the score reads the PATH's cost, which is what paths differ by.
        """
        selection, after, selector_call_id = self._select(run_state, candidates)
        chosen = chosen_candidate(selection, candidates)
        step = step_for_selection(selection, candidates, committed_action=committed_action)
        own = cost_between(cost_from, after.budget)
        accumulated = path_cost.plus(own)
        node = SearchNode(
            node_id=new_node_id(),
            parent_id=parent_id,
            depth=depth,
            evidence_snapshot_ref=evidence_snapshot_ref(after.evidence),
            # The node's ranking IS the step its path would hand the loop.
            hypotheses=step.hypotheses,
            proposed_probe=None if chosen is None else chosen.next_probe,
            probe_taken=probe_taken,
            score=score_node(
                selector_confidence=_confidence(selection),
                uncertainty=selection.uncertainty,
                commits=selection.decision is SelectionDecision.SELECT,
                cost=accumulated,
                ceiling=after.budget,
            ),
            cost=accumulated,
        )
        path = _Path(
            node=node,
            run_state=after,
            candidates=candidates,
            selection=selection,
            step=step,
            committed_action=committed_action,
            own_cost=own,
            generation_call_id=generation_call_id,
            selector_call_id=selector_call_id,
            candidate_id=candidate_id,
        )
        self.nodes = (*self.nodes, _branch_record(path))
        return path

    def _refused(
        self,
        parent: _Path,
        probe: ProbeAction,
        candidate: DiagnosisCandidate,
        reason: str,
    ) -> None:
        """Record a branch that never became a node, with the reason it did not."""
        self.branches_refused += 1
        self.nodes = (
            *self.nodes,
            BranchRecord(
                branch_id=new_node_id(),
                parent_id=parent.node.node_id,
                depth=parent.node.depth + 1,
                evidence_snapshot_ref=parent.node.evidence_snapshot_ref,
                candidate_id=candidate.candidate_id,
                probe=probe.tool_name,
                probe_arguments=dict(probe.arguments),
                score=0.0,
                selector_confidence=0.0,
                tool_cost=0.0,
                token_cost=0.0,
                safety_risk=0.0,
                refused=reason,
            ),
        )

    # --- the handoff ------------------------------------------------------

    def _handoff(
        self, before: RunState, best: _Path
    ) -> tuple[RunState, InvestigationStep, StepRecord]:
        """The chosen path, as the loop takes any other step.

        The state carries the chosen path's evidence and the WHOLE walk's ledger: a branch not
        taken leaves its cost behind, and its reading with it.
        """
        updated = best.run_state.model_copy(
            update={
                "budget": self._budget(),
                "hypotheses": best.step.hypotheses,
                "updated_at": self.at,
            }
        )
        record = self._record(before, updated, best)
        if self.ctx.record_step is not None:
            self.ctx.record_step(record)
        return updated, best.step, record

    def _record(self, before: RunState, after: RunState, best: _Path) -> StepRecord:
        """One record per step, carrying every branch and what each one cost."""
        chosen_id = best.node.node_id
        nodes = tuple(replace(node, chosen=node.branch_id == chosen_id) for node in self.nodes)
        return StepRecord(
            run_id=str(before.incident_id),
            iteration=self.ctx.iteration,
            strategy=self.strategy.name,
            model=self.ctx.model,
            # The CHOSEN path's set, so pass@k means what it means elsewhere; every other node
            # is under ``search``.
            candidate_set=tuple(
                candidate_record_of(candidate, generation_call_id=best.generation_call_id)
                for candidate in best.candidates
            ),
            selector=SelectorRecord(
                selected_candidate_id=best.selection.selected_candidate_id,
                scores=dict(best.selection.scores),
                uncertainty=best.selection.uncertainty,
                decision=best.selection.decision.value,
                call_id=best.selector_call_id,
            ),
            search=SearchRecord(
                depth_allowed=self.walk.depth_allowed,
                depth_used=self.walk.depth_spent,
                branch_allowed=self.walk.branch_allowed,
                branches_taken=self.branches_taken,
                branches_refused=self.branches_refused,
                pruned_by_ledger=self.pruned_by_ledger,
                nodes=nodes,
                chosen_branch_id=chosen_id,
                tool_calls_used=after.budget.tool_calls_used - before.budget.tool_calls_used,
                tokens_used=after.budget.tokens_used - before.budget.tokens_used,
            ),
            emitted_step=best.step,
            hypothesis_state_before=before.hypotheses,
            hypothesis_state_after=best.step.hypotheses,
            llm_calls=self.calls,
            planner_input_tokens=self.planner_input_tokens,
            planner_context_chars=self.planner_context_chars,
        )

    # --- the ledger -------------------------------------------------------

    def _budget(self) -> BudgetLedger:
        """The shared ledger. One carrier, so no branch spends a copy of it."""
        if self.ledger is None:  # pragma: no cover - set before the walk starts
            raise ValueError("the walk has no ledger")
        return self.ledger

    def _note_reserve(self, before: BudgetLedger, after: BudgetLedger) -> None:
        """Remember the largest single call, which is what the reserve holds back."""
        self.token_reserve = max(self.token_reserve, after.tokens_used - before.tokens_used)


def _best_of(paths: Sequence[_Path]) -> _Path:
    """The highest-scoring path; a tie keeps the earlier one (the shallower, cheaper node)."""
    return max(paths, key=lambda path: path.node.score.total)


def _confidence(selection: SelectionResult) -> float:
    """The selector's own score for the candidate it points at, or 0.0 when it points at none.

    ``uncertainty`` is NOT folded in — that is the ``safety_risk`` term, and a confidence built
    from two numbers could not say which one decided a path.
    """
    chosen_id = selection.chosen_candidate_id
    if chosen_id is None:
        return 0.0
    return selection.scores.get(chosen_id, 0.0)


def _probe_fingerprint(probe: ProbeAction) -> str:
    """``tool(arguments)``, sorted — one evidence-gathering decision's identity (INC-002)."""
    return f"{probe.tool_name}({json.dumps(probe.arguments, sort_keys=True)})"


def _branch_record(path: _Path) -> BranchRecord:
    """One scored node, as the trace holds it: its own cost, its score's four terms."""
    node = path.node
    probe = node.probe_taken
    proposed = node.proposed_probe
    return BranchRecord(
        branch_id=node.node_id,
        parent_id=node.parent_id,
        depth=node.depth,
        evidence_snapshot_ref=node.evidence_snapshot_ref,
        candidate_id=path.candidate_id,
        probe=None if probe is None else probe.tool_name,
        probe_arguments={} if probe is None else dict(probe.arguments),
        proposed_probe=None if proposed is None else proposed.tool_name,
        score=node.score.total,
        selector_confidence=node.score.selector_confidence,
        tool_cost=node.score.tool_cost,
        token_cost=node.score.token_cost,
        safety_risk=node.score.safety_risk,
        tool_calls_used=path.own_cost.tool_calls,
        tokens_used=path.own_cost.tokens,
        usd_used=path.own_cost.usd,
    )


def _generation_call_id(generation: CandidateGeneration) -> str:
    """The generation's own call id, off the record it already built."""
    candidates = generation.record.candidate_set
    return candidates[0].generation_call_id if candidates else ""


def _with_ledger(state: RunState, budget: BudgetLedger) -> RunState:
    """One state, rebased on the shared ledger — the only way a branch starts."""
    return state.model_copy(update={"budget": budget})


def _billed(call: RepairedCall[Any]) -> LLMUsage | None:
    """Everything one call billed: the leg that parsed, and each repair leg before it."""
    return sum_usage(*(usage_of(err) for err in call.failures), call.result)
