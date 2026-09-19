"""WP-12.1 — bounded shallow search (plan 02 § 14, ADR 0060).

The claims, each falsifiable on its own:

* ``TestTheBoundsAreStructural`` — a fake that wants depth 3 or branch 4 gets neither, and the
  bounds are objects rather than the shape of a loop or a line in a prompt.
* ``TestTheCapsAreSharedAtTheLedger`` — branches and the chosen path spend ONE ledger, never
  more than ``baseline``'s ceiling, and a branch cannot spend the call the chosen path needs.
* ``TestNoBranchActsOnTheWorld`` — a non-read inside a branch is refused, by tier, with the
  reason recorded; the schema cannot even name one.
* ``TestSearchIsRecordedModeOnly`` — live and canned are refused with a message, before a call.
* ``TestTheHandoffIsBaselineShaped`` — the chosen path hands the loop what ``baseline`` hands
  it, and every gate still runs on it.
* ``TestTheWalkIsOnTheRecord`` / ``TestTheScoreIsPlanTwoFiftySeven`` — every branch, its cost
  and the four score terms reach the ``StepRecord``, the accounting and the report.
"""

from __future__ import annotations

import ast
import json
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

import pytest
from pydantic import ValidationError

from incident_commander.agent.accounting import RunAccounting
from incident_commander.agent.hypothesis import InvestigationStep, ProbeAction
from incident_commander.agent.investigation import (
    BRANCH_PROBE_NOT_A_READ,
    make_branch_prober,
    make_llm_investigate,
)
from incident_commander.agent.search import (
    BRANCH_ABOVE_MAXIMUM,
    BRANCH_CAP_SPENT,
    DEPTH_ABOVE_MAXIMUM,
    DEPTH_CAP_SPENT,
    DUPLICATE_BRANCH_PROBE,
    MAX_BRANCH_FACTOR,
    MAX_SEARCH_DEPTH,
    SAFETY_RISK_WEIGHT,
    SEARCH_IS_RECORDED_MODE_ONLY,
    TOKEN_COST_WEIGHT,
    TOOL_CEILING_RESERVED,
    TOOL_COST_WEIGHT,
    NodeCost,
    SearchCapExceeded,
    SearchWalk,
    evidence_snapshot_ref,
    room_for_a_branch,
    score_node,
)
from incident_commander.agent.state import BudgetLedger, IncidentState, RunState
from incident_commander.agent.strategies.baseline import BaselineStrategy
from incident_commander.agent.strategies.names import StrategyName
from incident_commander.agent.strategies.protocol import StrategyContext
from incident_commander.agent.strategies.records import StepRecord
from incident_commander.agent.strategies.registry import STRATEGIES
from incident_commander.agent.strategies.search import (
    NO_BRANCH_PROBER,
    NO_SELECTOR_CLIENT,
    SearchFailed,
    SearchStrategy,
)
from incident_commander.config import Settings
from incident_commander.llm.client import LLMError, LLMResult
from incident_commander.llm.fakes import CannedLLMClient, CannedUsage
from incident_commander.tools.mcp_client import MCPError, ToolResult

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
_STRATEGY_MODULE: Final[Path] = (
    _REPO_ROOT / "src" / "incident_commander" / "agent" / "strategies" / "search.py"
)

#: Three reads that differ only in their arguments — one tool, three evidence-gathering
#: decisions (INC-002: the filter is part of what a branch IS).
_GROUPS: Final[tuple[str, ...]] = ("billing", "worker-dispatcher", "reports")


# --------------------------------------------------------------------------
# Fakes. Local copies on purpose: this file has to be able to fail on its own.


class _FakeMCPClient:
    def __init__(self, handler: Callable[[str, Mapping[str, Any]], ToolResult]) -> None:
        self._handler = handler
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call_tool(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        timeout_seconds: float | None = None,
    ) -> ToolResult:
        self.calls.append((name, dict(arguments)))
        return self._handler(name, arguments)


class _AlwaysBranches:
    """A generator that proposes three fresh reads on every call, forever.

    The red-before fake: nothing it ever answers says "stop exploring", so a depth or branch
    bound that lived in a prompt, or in the absence of a loop, would let it walk as far as the
    budget allowed. Paired with ``_AlwaysProbesMore`` below.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def call[T: Any](
        self,
        system_prompt: str,
        user_message: str,
        output_model: type[T],
        model: str,
        max_tokens: int = 4096,
        *,
        repair_of: str | None = None,
        temperature: float | None = None,
    ) -> Any:
        self.calls.append((system_prompt, user_message))
        # A new group per call, so no branch is ever a duplicate of an earlier one and the
        # walk is never stopped by anything but its own bounds.
        turn = len(self.calls)
        return LLMResult(
            output=output_model.model_validate(
                _candidates_payload(
                    probes=[f"{group}-{turn}" for group in _GROUPS],
                )
            ),
            stop_reason="canned",
        )


class _AlwaysProbesMore:
    """A selector that never commits: every call says ``probe_more``, forever."""

    def __init__(self, *, uncertainty: float = 0.4) -> None:
        self.calls: list[tuple[str, str]] = []
        self._uncertainty = uncertainty

    def call[T: Any](
        self,
        system_prompt: str,
        user_message: str,
        output_model: type[T],
        model: str,
        max_tokens: int = 4096,
        *,
        repair_of: str | None = None,
        temperature: float | None = None,
    ) -> Any:
        self.calls.append((system_prompt, user_message))
        return LLMResult(
            output=output_model.model_validate(_selection_payload(uncertainty=self._uncertainty)),
            stop_reason="canned",
        )


def _lag_response(group: str = "billing", lag: int = 42) -> ToolResult:
    return ToolResult(
        content=[
            {
                "type": "text",
                "text": json.dumps(
                    {
                        "consumer_group": group,
                        "lag": lag,
                        "lag_known": True,
                        "source": "static",
                        "cache_key": f"kafka:consumer_lag:{group}",
                    }
                ),
            }
        ]
    )


def _probe(group: str) -> dict[str, Any]:
    return {
        "kind": "probe",
        "tool_name": "get_consumer_lag",
        "arguments": {"consumer_group": group},
    }


def _candidates_payload(
    *,
    probes: Sequence[str | None] = _GROUPS,
    confidences: Sequence[float] = (0.8, 0.6, 0.4),
    action: dict[str, Any] | None = None,
    categories: Sequence[str] = ("consumer_saturation", "poison_message", "stale_cache"),
) -> dict[str, Any]:
    """One ``best_of_n_enumerated`` answer: three candidates, each proposing one read."""
    return {
        "candidates": [
            {
                "candidate_id": f"c{index}",
                "category": categories[index],
                "name": f"candidate {index}",
                "confidence": confidences[index],
                "next_probe": None if group is None else _probe(group),
            }
            for index, group in enumerate(probes)
        ],
        "next_action": action or _probe(_GROUPS[0]),
    }


def _selection_payload(
    *,
    decision: str = "probe_more",
    selected: str | None = None,
    scores: Mapping[str, float] | None = None,
    uncertainty: float = 0.3,
) -> dict[str, Any]:
    return {
        "decision": decision,
        "selected_candidate_id": selected,
        "scores": dict(scores or {"c0": 0.6, "c1": 0.4, "c2": 0.2}),
        "uncertainty": uncertainty,
        "reasoning": "The lag reading separates the leaders.",
    }


def _selector_queue(
    *,
    root: float = 0.3,
    branches: Sequence[float] = (0.9, 0.8, 0.7),
    usage: CannedUsage | None = None,
) -> CannedLLMClient:
    """A selector that grows more confident once a read has happened.

    The root scores low and each branch scores higher, which is what makes a BRANCH the
    chosen path: with an identical answer either side of a read, the read bought nothing and
    the score prefers the cheaper root — which is the arithmetic working, not a fixture bug.
    """
    payloads = [_selection_payload(scores={"c0": root, "c1": 0.1, "c2": 0.05})]
    payloads += [
        _selection_payload(scores={"c0": score, "c1": 0.1, "c2": 0.05}) for score in branches
    ]
    # Spare answers for a second level's branches.
    payloads += [_selection_payload(scores={"c0": 0.2, "c1": 0.1, "c2": 0.05}) for _ in range(6)]
    return CannedLLMClient(payloads, usage=usage)


def _investigating(run_state: RunState) -> RunState:
    return run_state.model_copy(
        update={
            "state": IncidentState.INVESTIGATING,
            "alert": {"source": "kafka", "severity": "high", "group": "billing"},
        }
    )


def _context(
    planner: Any,
    selector: Any,
    *,
    mcp: _FakeMCPClient | None = None,
    prober: Any = None,
    iteration: int = 0,
    sink: list[StepRecord] | None = None,
) -> StrategyContext:
    """A context with a prober, which is what recorded mode gives a strategy."""
    if prober is None and mcp is not None:
        prober = make_branch_prober(mcp)
    return StrategyContext(
        llm_client=planner,
        model="claude-sonnet-4-6",
        iteration=iteration,
        record_step=None if sink is None else sink.append,
        selector_llm_client=selector,
        branch_prober=prober,
    )


def _strategy(**knobs: Any) -> SearchStrategy:
    from incident_commander.agent.strategies.knobs import StrategyKnobs

    return SearchStrategy(StrategyKnobs(**knobs))


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "anthropic_api_key": "sk-ant-test",
        "judge_model": "claude-haiku-4-5",
        "platform_mcp_url": "https://mcp.platform.local",
        "platform_rest_url": "https://api.platform.local",
        "platform_token": "svc-token",
        "platform_webhook_secret": "hmac-secret",
        "database_url": "postgresql://commander:commander@localhost:5432/commander",
    }
    return Settings(_env_file=None, **{**base, **overrides})  # type: ignore[call-arg]


def _ledger(**overrides: Any) -> BudgetLedger:
    base: dict[str, Any] = {
        "max_tool_calls": 25,
        "max_tokens": 200_000,
        "max_wall_seconds": 1_800,
        "max_usd": Decimal("5.00"),
    }
    return BudgetLedger(**{**base, **overrides})


# --------------------------------------------------------------------------


class TestTheBoundsAreStructural:
    """Depth ≤ 2 and branch ≤ 3, held in code. Four independent proofs.

    A fake that wants more is the first: ``_AlwaysBranches`` proposes three new reads every
    call and ``_AlwaysProbesMore`` never commits, so nothing in any model answer ever ends
    the walk.
    """

    def test_a_walk_cannot_be_built_above_the_maximum(self) -> None:
        with pytest.raises(SearchCapExceeded) as depth:
            SearchWalk(depth_allowed=MAX_SEARCH_DEPTH + 1)
        assert DEPTH_ABOVE_MAXIMUM in str(depth.value)
        with pytest.raises(SearchCapExceeded) as branch:
            SearchWalk(branch_allowed=MAX_BRANCH_FACTOR + 1)
        assert BRANCH_ABOVE_MAXIMUM in str(branch.value)

    def test_the_arm_refuses_a_configured_depth_three_or_branch_four(self) -> None:
        # Refused at CONSTRUCTION, before a run spends anything, and refused rather than
        # clamped: a walk that ran at 3 while its row said 4 would be a reported bound the
        # run never had.
        with pytest.raises(SearchCapExceeded):
            _strategy(search_depth=3)
        with pytest.raises(SearchCapExceeded):
            _strategy(search_branch=4)

    def test_a_second_level_or_a_fourth_branch_raises(self) -> None:
        walk = SearchWalk(depth_allowed=1, branch_allowed=1)
        walk.descend()
        with pytest.raises(SearchCapExceeded) as depth:
            walk.descend()
        assert DEPTH_CAP_SPENT in str(depth.value)
        allowance = walk.branches()
        allowance.spend()
        with pytest.raises(SearchCapExceeded) as branch:
            allowance.spend()
        assert BRANCH_CAP_SPENT in str(branch.value)

    def test_a_fake_that_wants_more_gets_depth_two_and_branch_three(
        self, run_state: RunState, now: datetime
    ) -> None:
        mcp = _FakeMCPClient(lambda name, args: _lag_response(str(args["consumer_group"])))
        planner, selector = _AlwaysBranches(), _AlwaysProbesMore()
        sink: list[StepRecord] = []
        _, _, record = _strategy().plan_next_step(
            _investigating(run_state), now, _context(planner, selector, mcp=mcp, sink=sink)
        )

        assert record.search is not None
        assert record.search.depth_used == MAX_SEARCH_DEPTH
        assert record.search.branch_allowed == MAX_BRANCH_FACTOR
        # Two levels, three branches each: six reads, and not one more however many the
        # generator proposed.
        assert record.search.branches_taken == MAX_SEARCH_DEPTH * MAX_BRANCH_FACTOR
        assert len(mcp.calls) == MAX_SEARCH_DEPTH * MAX_BRANCH_FACTOR
        assert max(node.depth for node in record.search.nodes) == MAX_SEARCH_DEPTH

    def test_the_default_knobs_are_the_structural_maximums(self) -> None:
        from incident_commander.agent.strategies.knobs import StrategyKnobs

        # knobs.py imports nothing, so the numbers are literals there. These two spellings
        # are the reason this assertion exists.
        assert StrategyKnobs().search_depth == MAX_SEARCH_DEPTH
        assert StrategyKnobs().search_branch == MAX_BRANCH_FACTOR
        assert Settings.model_fields["search_depth"].default == MAX_SEARCH_DEPTH
        assert Settings.model_fields["search_branch"].default == MAX_BRANCH_FACTOR

    def test_config_refuses_a_value_above_the_maximum(self) -> None:
        with pytest.raises(ValidationError):
            _settings(search_depth=3)
        with pytest.raises(ValidationError):
            _settings(search_branch=4)

    def test_the_module_walks_levels_in_one_bounded_loop(self) -> None:
        # Anti-drift, in the spirit of reflection's "no loop over the critic": the only
        # iteration over LEVELS is a `for` over `range(depth_allowed)`, and every level is
        # taken through `walk.descend()`, which raises past the bound.
        tree = ast.parse(_STRATEGY_MODULE.read_text(encoding="utf-8"))
        whiles = [node for node in ast.walk(tree) if isinstance(node, ast.While)]
        assert whiles == [], (
            "search.py grew a `while` loop. The walk's depth is a bound, so it is a `for` "
            "over the bound plus a token that raises — a `while` is how a bounded walk "
            "becomes an unbounded one."
        )
        descends = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and node.attr == "descend"
        ]
        assert len(descends) == 1, f"one call site takes a level; found {len(descends)}"


class TestTheCapsAreSharedAtTheLedger:
    """One ledger for the whole walk, and it is ``baseline``'s ceiling (plan 02 § 8)."""

    def test_branches_and_the_chosen_path_never_exceed_the_ceiling(
        self, run_state: RunState, now: datetime
    ) -> None:
        # Four tool calls for the whole run. A walk that minted its own budget, or counted
        # in the strategy instead of at the ledger, would read six.
        narrow = run_state.model_copy(update={"budget": _ledger(max_tool_calls=4)})
        mcp = _FakeMCPClient(lambda name, args: _lag_response(str(args["consumer_group"])))
        updated, _, record = _strategy().plan_next_step(
            _investigating(narrow),
            now,
            _context(_AlwaysBranches(), _AlwaysProbesMore(), mcp=mcp),
        )

        assert record.search is not None
        assert updated.budget.tool_calls_used == len(mcp.calls)
        assert updated.budget.tool_calls_used <= narrow.budget.max_tool_calls
        # And the reserve held: a call is left for the chosen path's own probe, which is the
        # difference between "the ledger was respected" and "the ledger was emptied".
        assert updated.budget.tool_calls_used < narrow.budget.max_tool_calls
        # And the walk says the ledger stopped it, rather than looking like a walk that ran
        # out of ideas: "search was capped" and "search explored less" are different findings.
        assert record.search.pruned_by_ledger >= 1

    def test_a_branch_the_ledger_cannot_afford_is_recorded_with_its_reason(
        self, run_state: RunState, now: datetime
    ) -> None:
        # Three tool calls: two branches fit beside the reserved one, the third does not and
        # is recorded as refused by the ceiling rather than silently skipped.
        narrow = run_state.model_copy(update={"budget": _ledger(max_tool_calls=3)})
        mcp = _FakeMCPClient(lambda name, args: _lag_response(str(args["consumer_group"])))
        planner = CannedLLMClient([_candidates_payload()])
        selector = CannedLLMClient([_selection_payload() for _ in range(6)])
        _, _, record = _strategy(search_depth=1).plan_next_step(
            _investigating(narrow), now, _context(planner, selector, mcp=mcp)
        )

        assert record.search is not None
        assert record.search.branches_taken == 2
        assert any(node.refused == TOOL_CEILING_RESERVED for node in record.search.nodes)

    def test_every_branch_spends_the_same_ledger(self, run_state: RunState, now: datetime) -> None:
        # Three branches read, one path is chosen: the returned state carries ONE reading in
        # its evidence and THREE tool calls in its ledger. A per-branch copy of the budget
        # would have returned one.
        mcp = _FakeMCPClient(lambda name, args: _lag_response(str(args["consumer_group"])))
        planner = CannedLLMClient([_candidates_payload()])
        updated, _, record = _strategy(search_depth=1).plan_next_step(
            _investigating(run_state), now, _context(planner, _selector_queue(), mcp=mcp)
        )

        assert len(mcp.calls) == MAX_BRANCH_FACTOR
        assert updated.budget.tool_calls_used == MAX_BRANCH_FACTOR
        assert len(updated.evidence) == 1
        assert record.search is not None
        assert record.search.tool_calls_used == MAX_BRANCH_FACTOR

    def test_the_reserve_is_measured_not_assumed(self) -> None:
        # The tool reserve is one call, and it is checked against the ledger's own ceiling.
        assert room_for_a_branch(_ledger(max_tool_calls=2), token_reserve=0) is None
        assert (
            room_for_a_branch(_ledger(max_tool_calls=2, tool_calls_used=1), token_reserve=0)
            == TOOL_CEILING_RESERVED
        )
        # A token reserve the ledger cannot cover stops the branch too, and the reserve the
        # walk passes is the largest call it has already PAID for.
        assert (
            room_for_a_branch(_ledger(max_tokens=100, tokens_used=60), token_reserve=50) is not None
        )

    def test_a_walk_on_an_exhausted_ledger_takes_no_branch(
        self, run_state: RunState, now: datetime
    ) -> None:
        spent = run_state.model_copy(update={"budget": _ledger(max_tool_calls=1)})
        mcp = _FakeMCPClient(lambda name, args: _lag_response(str(args["consumer_group"])))
        _, step, record = _strategy().plan_next_step(
            _investigating(spent), now, _context(_AlwaysBranches(), _AlwaysProbesMore(), mcp=mcp)
        )

        assert mcp.calls == []
        assert record.search is not None
        assert record.search.branches_taken == 0
        # Still a step: the root is a path, so a walk that could afford nothing is the
        # control group's answer rather than a crash.
        assert isinstance(step, InvestigationStep)


class TestNoBranchActsOnTheWorld:
    """A branch gathers evidence. It cannot act, and the refusal names why."""

    def test_the_schema_cannot_name_a_non_read(self) -> None:
        # The first half is the type: ``ProbeAction.tool_name`` is a ``ReadToolName``
        # literal, so a candidate proposing a Tier-1 tool does not validate at all.
        with pytest.raises(ValidationError):
            ProbeAction.model_validate(
                {"kind": "probe", "tool_name": "restart_consumer_group", "arguments": {}}
            )

    def test_a_tier_one_call_inside_a_branch_is_refused_by_its_tier(self) -> None:
        # The second half is the runtime guard (B-06): a tool reclassified READ → TIER_1
        # after the literal was hand-listed reaches the prober, and the prober refuses it
        # WITHOUT calling the client. ``model_construct`` is how a validated schema is
        # bypassed to test the guard behind it.
        mcp = _FakeMCPClient(lambda name, args: _lag_response())
        prober = make_branch_prober(mcp)
        action = ProbeAction.model_construct(
            kind="probe", tool_name="restart_consumer_group", arguments={}
        )
        state = RunState(
            incident_id=__import__("uuid").uuid4(),
            state=IncidentState.INVESTIGATING,
            alert={},
            budget=_ledger(),
            created_at=datetime(2026, 7, 15, 20, 0),
            updated_at=datetime(2026, 7, 15, 20, 0),
        )
        outcome = prober(state, action)

        assert outcome.refused is not None
        assert BRANCH_PROBE_NOT_A_READ in outcome.refused
        assert "tier_1" in outcome.refused
        assert mcp.calls == [], "a refused branch must not reach the platform at all"
        assert outcome.run_state is state, "a refusal costs nothing, ledger included"

    def test_a_refused_branch_is_recorded_and_the_walk_continues(
        self, run_state: RunState, now: datetime
    ) -> None:
        # A read the recording cannot answer (``is_error``) is the recorded-world case: the
        # branch is pruned with its reason, and the rest of the walk still runs.
        def handler(name: str, arguments: Mapping[str, Any]) -> ToolResult:
            if arguments.get("consumer_group") == _GROUPS[0]:
                return ToolResult(content=[{"type": "text", "text": "{}"}], is_error=True)
            return _lag_response(str(arguments["consumer_group"]))

        mcp = _FakeMCPClient(handler)
        planner = CannedLLMClient([_candidates_payload()])
        selector = CannedLLMClient([_selection_payload() for _ in range(5)])
        _, _, record = _strategy(search_depth=1).plan_next_step(
            _investigating(run_state), now, _context(planner, selector, mcp=mcp)
        )

        assert record.search is not None
        assert record.search.branches_refused == 1
        assert record.search.branches_taken == MAX_BRANCH_FACTOR - 1
        refused = [node for node in record.search.nodes if node.refused is not None]
        assert refused[0].probe == "get_consumer_lag"
        assert refused[0].probe_arguments == {"consumer_group": _GROUPS[0]}

    def test_a_transport_failure_in_a_branch_does_not_escalate_the_run(self) -> None:
        def handler(name: str, arguments: Mapping[str, Any]) -> ToolResult:
            raise MCPError(-32000, "connection reset")

        prober = make_branch_prober(_FakeMCPClient(handler))
        state = RunState(
            incident_id=__import__("uuid").uuid4(),
            state=IncidentState.INVESTIGATING,
            alert={},
            budget=_ledger(),
            created_at=datetime(2026, 7, 15, 20, 0),
            updated_at=datetime(2026, 7, 15, 20, 0),
        )
        outcome = prober(state, ProbeAction(tool_name="get_consumer_lag", arguments={}))

        assert outcome.refused is not None
        assert outcome.run_state.state is IncidentState.INVESTIGATING

    def test_the_second_branch_on_one_read_is_not_a_branch(
        self, run_state: RunState, now: datetime
    ) -> None:
        # Two candidates naming the same read are one evidence-gathering decision. The
        # second is recorded as a duplicate rather than paying a second tool call.
        mcp = _FakeMCPClient(lambda name, args: _lag_response(str(args["consumer_group"])))
        planner = CannedLLMClient([_candidates_payload(probes=(_GROUPS[0], _GROUPS[0], None))])
        selector = CannedLLMClient([_selection_payload() for _ in range(5)])
        _, _, record = _strategy(search_depth=1).plan_next_step(
            _investigating(run_state), now, _context(planner, selector, mcp=mcp)
        )

        assert len(mcp.calls) == 1
        assert record.search is not None
        assert any(node.refused == DUPLICATE_BRANCH_PROBE for node in record.search.nodes)


class TestSearchIsRecordedModeOnly:
    """Live and canned are refused with a message, not degraded into (06 C13)."""

    def test_a_context_without_a_prober_is_refused_before_any_call(
        self, run_state: RunState, now: datetime
    ) -> None:
        planner, selector = _AlwaysBranches(), _AlwaysProbesMore()
        with pytest.raises(ValueError) as err:
            _strategy().plan_next_step(
                _investigating(run_state), now, _context(planner, selector, prober=None)
            )

        assert NO_BRANCH_PROBER in str(err.value)
        assert SEARCH_IS_RECORDED_MODE_ONLY in str(err.value)
        assert planner.calls == [], "the refusal must cost nothing"
        assert selector.calls == []

    def test_the_message_says_what_to_do_instead(self) -> None:
        # Plain language, and it names the one command that makes a recorded world.
        assert "RECORDED mode only" in SEARCH_IS_RECORDED_MODE_ONLY
        assert "make world-record" in SEARCH_IS_RECORDED_MODE_ONLY
        assert "moved" in SEARCH_IS_RECORDED_MODE_ONLY

    def test_the_runner_refuses_live_and_canned_and_allows_recorded(self) -> None:
        from evals.runner import search_mode_refusal

        search = _settings(inference_strategy=StrategyName.SEARCH)
        assert search_mode_refusal(search, recorded=False) == SEARCH_IS_RECORDED_MODE_ONLY
        assert search_mode_refusal(search, recorded=True) is None
        # No other arm is affected, in either mode.
        assert search_mode_refusal(_settings(), recorded=False) is None

    def test_a_selector_client_is_required_too(self, run_state: RunState, now: datetime) -> None:
        mcp = _FakeMCPClient(lambda name, args: _lag_response())
        with pytest.raises(ValueError) as err:
            _strategy().plan_next_step(
                _investigating(run_state),
                now,
                StrategyContext(
                    llm_client=_AlwaysBranches(),
                    model="m",
                    iteration=0,
                    branch_prober=make_branch_prober(mcp),
                ),
            )
        assert NO_SELECTOR_CLIENT in str(err.value)


class TestTheHandoffIsBaselineShaped:
    """The chosen path hands the loop what ``baseline`` hands it, and the gates still run."""

    def test_the_emitted_step_has_baselines_shape(self, run_state: RunState, now: datetime) -> None:
        mcp = _FakeMCPClient(lambda name, args: _lag_response(str(args["consumer_group"])))
        planner = CannedLLMClient([_candidates_payload()])
        selector = CannedLLMClient([_selection_payload() for _ in range(5)])
        state, step, _ = _strategy(search_depth=1).plan_next_step(
            _investigating(run_state), now, _context(planner, selector, mcp=mcp)
        )
        control = BaselineStrategy().plan_next_step(
            _investigating(run_state),
            now,
            StrategyContext(
                llm_client=CannedLLMClient([_baseline_payload()]), model="m", iteration=0
            ),
        )

        assert isinstance(step, InvestigationStep)
        assert step.model_dump(mode="json").keys() == control[1].model_dump(mode="json").keys()
        # The state is the same TYPE and the same schema: no field appears because a walk ran.
        assert set(state.model_dump().keys()) == set(control[0].model_dump().keys())
        assert state.schema_version == control[0].schema_version

    def test_a_remediate_below_the_threshold_still_escalates(
        self, run_state: RunState, now: datetime
    ) -> None:
        # The gates live in investigation.py and run on the emitted step whatever produced
        # it: a walk that ended on `select` with 0.5 confidence escalates, as baseline's
        # would. Selection is not authorization, and neither is exploring (plan 02 § 18).
        mcp = _FakeMCPClient(lambda name, args: _lag_response(str(args["consumer_group"])))
        planner = CannedLLMClient(
            [
                _candidates_payload(
                    confidences=(0.5, 0.4, 0.3),
                    action={"kind": "remediate", "reason": "fix it"},
                )
            ]
        )
        selector = CannedLLMClient(
            [_selection_payload(decision="select", selected="c0") for _ in range(6)]
        )
        result = make_llm_investigate(
            mcp,
            planner,
            model="m",
            strategy=_strategy(search_depth=1),
            selector_llm_client=selector,
            branch_prober=make_branch_prober(mcp),
        )(_investigating(run_state), now)

        assert result.state is IncidentState.ESCALATED
        assert "below threshold" in result.evidence[-1].result_summary

    def test_the_loop_passes_a_prober_only_when_it_is_given_one(
        self, run_state: RunState, now: datetime
    ) -> None:
        # ``make_llm_investigate``'s default is ``None``: every existing caller keeps the
        # behaviour it had, and no arm gains a way to read the world on its own.
        seen: list[StrategyContext] = []

        class _Recorder:
            name = "recorder"
            config: Mapping[str, Any] = {}

            def plan_next_step(
                self, state: RunState, at: datetime, ctx: StrategyContext
            ) -> tuple[RunState, InvestigationStep, StepRecord]:
                seen.append(ctx)
                raise LLMError("stop here")

        mcp = _FakeMCPClient(lambda name, args: _lag_response())
        make_llm_investigate(
            mcp,
            CannedLLMClient([]),
            model="m",
            strategy=_Recorder(),
        )(_investigating(run_state), now)

        assert seen[0].branch_prober is None


class TestTheWalkIsOnTheRecord:
    """Every branch, its cost and its score reach the record, the accounting and the row."""

    def test_the_record_carries_every_branch_and_its_own_cost(
        self, run_state: RunState, now: datetime
    ) -> None:
        mcp = _FakeMCPClient(lambda name, args: _lag_response(str(args["consumer_group"])))
        planner = CannedLLMClient([_candidates_payload()], usage=CannedUsage(input_tokens=100))
        selector = CannedLLMClient(
            [_selection_payload() for _ in range(5)], usage=CannedUsage(input_tokens=10)
        )
        sink: list[StepRecord] = []
        _, _, record = _strategy(search_depth=1).plan_next_step(
            _investigating(run_state), now, _context(planner, selector, mcp=mcp, sink=sink)
        )

        assert sink == [record], "one record per step, written to the sink"
        assert record.search is not None
        assert record.search.branches_taken == MAX_BRANCH_FACTOR
        assert record.search.depth_allowed == 1
        assert [node.chosen for node in record.search.nodes].count(True) == 1
        # Per-branch cost: each branch paid for one read and one selector call.
        branches = [node for node in record.search.nodes if node.depth == 1]
        assert all(node.tool_calls_used == 1 for node in branches)
        assert all(node.tokens_used > 0 for node in branches)
        # The root read nothing, and says so rather than omitting the field.
        root = next(node for node in record.search.nodes if node.depth == 0)
        assert root.probe is None and root.tool_calls_used == 0

    def test_the_record_serialises_for_a_tracer(self, run_state: RunState, now: datetime) -> None:
        mcp = _FakeMCPClient(lambda name, args: _lag_response(str(args["consumer_group"])))
        planner = CannedLLMClient([_candidates_payload()])
        selector = CannedLLMClient([_selection_payload() for _ in range(5)])
        _, _, record = _strategy(search_depth=1).plan_next_step(
            _investigating(run_state), now, _context(planner, selector, mcp=mcp)
        )
        written = json.loads(json.dumps(record.as_trace_record()))

        assert written["strategy"] == StrategyName.SEARCH.value
        assert written["search"]["branches_taken"] == MAX_BRANCH_FACTOR
        assert len(written["search"]["nodes"]) == len(record.search.nodes)  # type: ignore[union-attr]
        assert isinstance(written["search"]["nodes"][0]["usd_used"], str)

    def test_every_other_arm_writes_a_null_search_block(
        self, run_state: RunState, now: datetime
    ) -> None:
        _, _, record = BaselineStrategy().plan_next_step(
            _investigating(run_state),
            now,
            StrategyContext(
                llm_client=CannedLLMClient([_baseline_payload()]), model="m", iteration=0
            ),
        )
        assert record.search is None
        assert record.as_trace_record()["search"] is None

    def test_the_accounting_counts_branches_and_their_reads(
        self, run_state: RunState, now: datetime
    ) -> None:
        from evals.runner import build_accounting

        mcp = _FakeMCPClient(lambda name, args: _lag_response(str(args["consumer_group"])))
        accounting = RunAccounting()
        # The sink the runner wires is the accounting's own, composed with the tracer's.
        updated, _, record = _strategy(search_depth=1).plan_next_step(
            _investigating(run_state),
            now,
            StrategyContext(
                llm_client=CannedLLMClient([_candidates_payload()]),
                model="m",
                iteration=0,
                selector_llm_client=_selector_queue(),
                branch_prober=make_branch_prober(mcp),
                record_step=accounting.step_sink(),
            ),
        )

        assert record.search is not None
        assert accounting.search_branches == MAX_BRANCH_FACTOR
        assert accounting.search_branch_tool_calls == MAX_BRANCH_FACTOR
        row = build_accounting(accounting, updated.budget)
        assert row.search_branches == MAX_BRANCH_FACTOR
        assert row.search_branch_tool_calls == MAX_BRANCH_FACTOR
        # The run's own ceiling is what those reads were spent from, so the two numbers are
        # comparable in one row.
        assert row.tool_calls == MAX_BRANCH_FACTOR

    def test_a_baseline_row_still_reports_zero(self) -> None:
        from evals.runner import build_accounting

        row = build_accounting(RunAccounting(), _ledger())
        assert row.search_branches == 0
        assert row.search_branch_tool_calls == 0


class TestTheScoreIsPlanTwoFiftySeven:
    """``selector_confidence − tool_cost − token_cost − safety_risk``, and nothing else."""

    def test_the_total_is_the_four_terms(self) -> None:
        score = score_node(
            selector_confidence=0.8,
            uncertainty=0.5,
            commits=True,
            cost=NodeCost(tool_calls=5, tokens=1_000),
            ceiling=_ledger(max_tool_calls=10, max_tokens=10_000),
        )
        assert score.selector_confidence == 0.8
        assert score.tool_cost == pytest.approx(TOOL_COST_WEIGHT * 0.5)
        assert score.token_cost == pytest.approx(TOKEN_COST_WEIGHT * 0.1)
        assert score.safety_risk == pytest.approx(SAFETY_RISK_WEIGHT * 0.5)
        assert score.total == pytest.approx(
            score.selector_confidence - score.tool_cost - score.token_cost - score.safety_risk
        )

    def test_risk_is_charged_only_against_a_node_that_would_commit(self) -> None:
        gathering = score_node(
            selector_confidence=0.5,
            uncertainty=1.0,
            commits=False,
            cost=NodeCost(),
            ceiling=_ledger(),
        )
        committing = score_node(
            selector_confidence=0.5,
            uncertainty=1.0,
            commits=True,
            cost=NodeCost(),
            ceiling=_ledger(),
        )
        assert gathering.safety_risk == 0.0
        assert committing.safety_risk == SAFETY_RISK_WEIGHT
        assert gathering.total > committing.total

    def test_the_cheaper_of_two_equal_paths_wins(self) -> None:
        ceiling = _ledger(max_tool_calls=10, max_tokens=10_000)
        cheap = score_node(
            selector_confidence=0.7,
            uncertainty=0.2,
            commits=False,
            cost=NodeCost(tool_calls=1, tokens=100),
            ceiling=ceiling,
        )
        dear = score_node(
            selector_confidence=0.7,
            uncertainty=0.2,
            commits=False,
            cost=NodeCost(tool_calls=6, tokens=6_000),
            ceiling=ceiling,
        )
        assert cheap.total > dear.total

    def test_a_node_the_selector_points_at_nothing_scores_no_confidence(self) -> None:
        # ``escalate`` names no candidate, so there is no confidence to start from — 0.0,
        # not the top candidate's number, which nobody chose.
        score = score_node(
            selector_confidence=0.0,
            uncertainty=0.9,
            commits=False,
            cost=NodeCost(),
            ceiling=_ledger(),
        )
        assert score.total == 0.0

    def test_a_snapshot_ref_is_the_ledgers_ids_in_order(
        self, run_state: RunState, now: datetime
    ) -> None:
        from incident_commander.agent.state import EvidenceEntry

        first = EvidenceEntry(
            tool_name="get_consumer_lag", arguments={}, result_summary="a", timestamp=now
        )
        second = EvidenceEntry(
            tool_name="get_redis_health", arguments={}, result_summary="b", timestamp=now
        )
        assert evidence_snapshot_ref(()) != evidence_snapshot_ref((first,))
        assert evidence_snapshot_ref((first, second)) != evidence_snapshot_ref((second, first))
        assert len(evidence_snapshot_ref((first,))) == 12


class TestTheArmIsWiredIn:
    """Registered, configurable, stamped — and it stamps the bounds it ran under."""

    def test_the_registry_knows_search_and_builds_it(self) -> None:
        strategy = STRATEGIES.create(StrategyName.SEARCH.value)
        assert isinstance(strategy, SearchStrategy)
        assert strategy.name == StrategyName.SEARCH.value

    def test_the_config_block_stamps_the_bounds_and_the_shared_ceiling(self) -> None:
        config = _strategy().config
        assert config["depth"] == MAX_SEARCH_DEPTH
        assert config["branch"] == MAX_BRANCH_FACTOR
        assert config["cap"] == "structural"
        assert config["caps_shared_across_branches"] is True
        assert config["mode"] == "recorded"
        assert config["generator"] == StrategyName.BEST_OF_N_ENUMERATED.value
        # N is the branch factor: the generator asks for one candidate per branch.
        assert config["n"] == MAX_BRANCH_FACTOR

    def test_the_config_block_cannot_be_written_through(self) -> None:
        with pytest.raises(TypeError):
            _strategy().config["depth"] = 9  # type: ignore[index]

    def test_a_generator_that_cannot_generate_is_refused(self) -> None:
        from incident_commander.agent.strategies.knobs import StrategyKnobs

        with pytest.raises(ValueError) as err:
            SearchStrategy(StrategyKnobs(selector_generator=StrategyName.BASELINE.value))
        assert "nothing to branch between" in str(err.value)

    def test_the_settings_value_selects_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("INFERENCE_STRATEGY", "search")
        settings = _settings()
        assert settings.inference_strategy is StrategyName.SEARCH

    def test_a_failing_call_carries_the_whole_walks_bill(
        self, run_state: RunState, now: datetime
    ) -> None:
        # ADR 0045 one layer up: the selector fails after the generation was paid for, so
        # the error carries both legs and no billed call is charged to nobody.
        mcp = _FakeMCPClient(lambda name, args: _lag_response())
        planner = CannedLLMClient([_candidates_payload()], usage=CannedUsage(input_tokens=500))
        with pytest.raises(SearchFailed) as err:
            _strategy().plan_next_step(
                _investigating(run_state),
                now,
                _context(planner, CannedLLMClient([]), mcp=mcp),
            )
        assert err.value.usage is not None
        assert err.value.usage.input_tokens == 500


def _baseline_payload() -> dict[str, Any]:
    return {
        "hypotheses": [
            {
                "category": "consumer_saturation",
                "name": "consumer_saturation",
                "confidence": 0.55,
                "reasoning": "Alert severity suggests saturation.",
            }
        ],
        "next_action": _probe(_GROUPS[0]),
    }
