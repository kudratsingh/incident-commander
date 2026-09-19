"""WP-0.2 — the ``baseline`` strategy seam (plan 02 § 4, ADR 0036).

Acceptance in one line: delete the indirection and the canned report is unchanged. The
unit half (``make eval-reg`` is the other): ``baseline`` returns the old call's result,
ledger included; the seam holds no execution policy (an import scan); the registry
refuses what it does not know; one ``StepRecord`` per iteration, one candidate in it.
"""

from __future__ import annotations

import ast
import json
from collections.abc import Callable, Mapping
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Final
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from incident_commander.agent.hypothesis import HypothesisCategory
from incident_commander.agent.investigation import _plan_next_step, make_llm_investigate
from incident_commander.agent.state import IncidentState, RunState
from incident_commander.agent.strategies.baseline import BaselineStrategy
from incident_commander.agent.strategies.names import StrategyName
from incident_commander.agent.strategies.protocol import InvestigationStrategy, StrategyContext
from incident_commander.agent.strategies.records import StepRecord
from incident_commander.agent.strategies.registry import (
    STRATEGIES,
    StrategyRegistry,
    UnknownStrategyError,
    default_strategy,
)
from incident_commander.config import Settings
from incident_commander.llm.client import LLMClient, LLMClientProtocol
from incident_commander.llm.fakes import CannedLLMClient, CannedUsage
from incident_commander.tools.mcp_client import ToolResult

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
_STRATEGIES_DIR: Final[Path] = _REPO_ROOT / "src" / "incident_commander" / "agent" / "strategies"
_INVESTIGATION: Final[Path] = (
    _REPO_ROOT / "src" / "incident_commander" / "agent" / "investigation.py"
)
_POLICIES: Final[Path] = _REPO_ROOT / "src" / "incident_commander" / "tools" / "policies.py"


# --------------------------------------------------------------------------
# Local fakes, so this file can fail on its own.


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
                        "cache_key": "kafka:consumer_lag:worker-dispatcher",
                    }
                ),
            }
        ]
    )


def _probe_payload(confidence: float = 0.55) -> dict[str, Any]:
    return {
        "hypotheses": [
            {
                "category": "consumer_saturation",
                "name": "consumer_saturation",
                "confidence": confidence,
                "reasoning": "Alert severity suggests saturation.",
            }
        ],
        "next_action": {
            "kind": "probe",
            "tool_name": "get_consumer_lag",
            "arguments": {"consumer_group": "billing"},
        },
    }


def _stop_payload(confidence: float = 0.9) -> dict[str, Any]:
    return {
        "hypotheses": [
            {
                "category": "consumer_saturation",
                "name": "consumer_saturation",
                "confidence": confidence,
                "reasoning": "Lag reading confirms saturation.",
            }
        ],
        "next_action": {"kind": "stop", "reason": "confidence sufficient for handoff"},
    }


def _investigating(run_state: RunState) -> RunState:
    return run_state.model_copy(
        update={
            "state": IncidentState.INVESTIGATING,
            "alert": {"source": "kafka", "severity": "high", "group": "billing"},
        }
    )


def _context(
    llm: LLMClientProtocol,
    *,
    iteration: int = 0,
    sink: list[StepRecord] | None = None,
) -> StrategyContext:
    return StrategyContext(
        llm_client=llm,
        model="m",
        iteration=iteration,
        record_step=None if sink is None else sink.append,
    )


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


# --------------------------------------------------------------------------


class TestBaselineIsTheExistingCall:
    """``baseline`` is the control group: the same call, the same result.

    Not "equivalent behaviour" — the same values, or the campaign numbers stop
    describing the loop.
    """

    def test_it_returns_what_plan_next_step_returns(
        self, run_state: RunState, now: datetime
    ) -> None:
        usage = CannedUsage(input_tokens=11, output_tokens=7, cache_read_tokens=3)
        state = _investigating(run_state)

        direct_state, direct_step, direct_call = _plan_next_step(
            state, now, CannedLLMClient([_stop_payload()], usage=usage), "m"
        )
        strategy_llm = CannedLLMClient([_stop_payload()], usage=usage)
        seam_state, seam_step, record = BaselineStrategy().plan_next_step(
            state, now, _context(strategy_llm)
        )

        assert seam_step == direct_step
        assert seam_state == direct_state
        # Skipping ``accrue_structured_call`` makes every budget number a lower bound.
        assert seam_state.budget.tokens_used == direct_state.budget.tokens_used == 21
        assert seam_state.hypotheses == direct_state.hypotheses
        assert record.llm_calls[0].tokens_used == 21
        # The measurements the record carries are the call's own report, not
        # a second reading taken beside it (WP-2.1).
        assert record.llm_calls[0].input_tokens == direct_call.input_tokens == 11
        assert record.llm_calls[0].output_tokens == direct_call.output_tokens == 7
        assert record.llm_calls[0].cache_read_tokens == direct_call.cache_read_tokens == 3
        assert record.planner_input_tokens == direct_call.context_tokens == 14

    def test_the_planner_prompt_is_the_same_one(self, run_state: RunState, now: datetime) -> None:
        # Canned responses are keyed by prompt name, so an own prompt changes the queue.
        direct_llm = CannedLLMClient([_stop_payload()])
        _plan_next_step(_investigating(run_state), now, direct_llm, "m")
        seam_llm = CannedLLMClient([_stop_payload()])
        BaselineStrategy().plan_next_step(_investigating(run_state), now, _context(seam_llm))

        assert seam_llm.calls == direct_llm.calls

    def test_the_output_repair_wrapper_still_fires(
        self, run_state: RunState, now: datetime
    ) -> None:
        # ADR 0035: the repair lives inside ``_plan_next_step``, and both calls are billed.
        broken = {**_stop_payload(), "next_action": {"kind": "stop"}}  # no reason
        llm = CannedLLMClient([broken, _stop_payload()], usage=CannedUsage(output_tokens=5))
        state, step, record = BaselineStrategy().plan_next_step(
            _investigating(run_state), now, _context(llm)
        )

        assert len(llm.calls) == 2, "the repair re-ask did not happen"
        assert step.next_action.kind == "stop"
        # Read off the ledger, so the two cannot disagree (the canned rejection carries no usage).
        assert state.budget.tokens_used == 5
        assert record.llm_calls[0].tokens_used == state.budget.tokens_used

    def test_a_planner_failure_propagates_untouched(
        self, run_state: RunState, now: datetime
    ) -> None:
        # The loop's own except arm charges the failed call and escalates.
        llm = CannedLLMClient([])
        with pytest.raises(Exception, match="no more canned responses"):
            BaselineStrategy().plan_next_step(_investigating(run_state), now, _context(llm))

    def test_the_loop_run_through_the_seam_is_unchanged(
        self, run_state: RunState, now: datetime
    ) -> None:
        # End to end through ``make_llm_investigate``: probe, then stop, then
        # escalate with the ranking — the shape every canned scenario replays.
        mcp = _FakeMCPClient(lambda _n, _a: _lag_response())
        llm = CannedLLMClient([_probe_payload(), _stop_payload()])
        result = make_llm_investigate(mcp, llm, model="m")(_investigating(run_state), now)

        assert result.state is IncidentState.ESCALATED
        assert result.hypotheses[0].confidence == 0.9
        assert result.budget.tool_calls_used == 1
        assert mcp.calls == [("get_consumer_lag", {"consumer_group": "billing"})]


class TestBaselineEmitsOneCandidatePerStep:
    def test_one_record_per_planner_iteration(self, run_state: RunState, now: datetime) -> None:
        sink: list[StepRecord] = []
        mcp = _FakeMCPClient(lambda _n, _a: _lag_response())
        llm = CannedLLMClient([_probe_payload(), _stop_payload()])
        make_llm_investigate(mcp, llm, model="m", record_step=sink.append)(
            _investigating(run_state), now
        )

        assert len(sink) == 2, "one StepRecord per planner call, no more and no fewer"
        assert [record.iteration for record in sink] == [0, 1]
        assert {record.strategy for record in sink} == {"baseline"}
        assert {record.run_id for record in sink} == {str(run_state.incident_id)}

    def test_every_record_holds_exactly_one_candidate(
        self, run_state: RunState, now: datetime
    ) -> None:
        sink: list[StepRecord] = []
        mcp = _FakeMCPClient(lambda _n, _a: _lag_response())
        llm = CannedLLMClient([_probe_payload(), _stop_payload()])
        make_llm_investigate(mcp, llm, model="m", record_step=sink.append)(
            _investigating(run_state), now
        )

        for record in sink:
            assert len(record.candidate_set) == 1, (
                "baseline considers exactly one diagnosis per step — one call, "
                "one ranking, no alternatives generated"
            )
            assert record.candidate_set[0].category is HypothesisCategory.CONSUMER_SATURATION
            # Plan 02 § 7: the selector is null for baseline. With one
            # candidate there is nothing to select between.
            assert record.selector is None

        first, second = sink
        assert first.candidate_set[0].proposed_probe == "get_consumer_lag"
        assert second.candidate_set[0].proposed_probe is None, "a stop step probes nothing"

    def test_the_record_carries_the_ranking_either_side_of_the_call(
        self, run_state: RunState, now: datetime
    ) -> None:
        sink: list[StepRecord] = []
        mcp = _FakeMCPClient(lambda _n, _a: _lag_response())
        llm = CannedLLMClient([_probe_payload(), _stop_payload()])
        make_llm_investigate(mcp, llm, model="m", record_step=sink.append)(
            _investigating(run_state), now
        )

        first, second = sink
        assert first.hypothesis_state_before == ()
        assert first.hypothesis_state_after[0].confidence == 0.55
        assert second.hypothesis_state_before == first.hypothesis_state_after
        assert second.hypothesis_state_after[0].confidence == 0.9
        assert second.emitted_step.next_action.kind == "stop"

    def test_a_record_is_produced_even_when_nobody_is_recording(
        self, run_state: RunState, now: datetime
    ) -> None:
        # The tracer is opt-in (``EVAL_TRACE_DIR``) and the offline suite does not set it.
        _state, _step, record = BaselineStrategy().plan_next_step(
            _investigating(run_state), now, _context(CannedLLMClient([_stop_payload()]))
        )
        assert record.candidate_set[0].name == "consumer_saturation"

    def test_the_record_serializes_for_a_tracer(self, run_state: RunState, now: datetime) -> None:
        # Invariant 9's artifacts are JSONL. A record that cannot be written
        # is a record WP-2.1 would have to redesign.
        _state, _step, record = BaselineStrategy().plan_next_step(
            _investigating(run_state), now, _context(CannedLLMClient([_stop_payload()]))
        )
        written = json.loads(json.dumps(record.as_trace_record()))

        assert written["strategy"] == "baseline"
        assert written["emitted_step"]["next_action"]["kind"] == "stop"
        assert len(written["candidate_set"]) == 1
        assert written["selector"] is None
        # ``kind`` is WP-2.1's to add, with the human-report formatter
        # ``TraceKind`` membership requires in the same PR.
        assert "kind" not in written

    def test_the_record_never_reaches_run_state(self, run_state: RunState, now: datetime) -> None:
        # Divergence C7: RunState is a frozen schema_version=3 checkpoint, extra="forbid".
        state, _step, _record = BaselineStrategy().plan_next_step(
            _investigating(run_state), now, _context(CannedLLMClient([_stop_payload()]))
        )
        assert state.schema_version == 3
        assert "candidate" not in state.model_dump(mode="json")


class TestTheStepRecordCarriesTheCallsOwnDuration:
    """WO-R3-260: ``StepRecord.llm_calls[].elapsed_ms``, which was always null.

    The client times its logical calls now, so the strategy carries the number through and
    nothing more: a stopwatch around ``plan_next_step`` would also time the accrual.
    """

    def _live_shaped_client(self, payload: dict[str, Any], *, took_seconds: float) -> LLMClient:
        """A real ``LLMClient`` over a stubbed SDK — the live path, offline.

        Not a ``CannedLLMClient``: this measurement only exists on the client that makes real
        calls.
        """
        block = MagicMock()
        block.type = "tool_use"
        block.name = "record_output"
        block.input = payload
        response = MagicMock()
        response.content = [block]
        response.stop_reason = "tool_use"
        response.usage.input_tokens = 11
        response.usage.output_tokens = 7
        response.usage.cache_creation_input_tokens = 0
        response.usage.cache_read_input_tokens = 3
        sdk = MagicMock()
        sdk.messages.create.return_value = response
        readings = [0.0, took_seconds]
        return LLMClient(
            api_key="test",
            client=sdk,
            clock=lambda: readings.pop(0) if len(readings) > 1 else readings[0],
        )

    def test_a_live_shaped_call_lands_on_the_record(
        self, run_state: RunState, now: datetime
    ) -> None:
        llm = self._live_shaped_client(_stop_payload(), took_seconds=2.75)
        _state, _step, record = BaselineStrategy().plan_next_step(
            _investigating(run_state), now, _context(llm)
        )
        assert record.llm_calls[0].elapsed_ms == 2_750

    def test_it_survives_serialisation_as_a_number(
        self, run_state: RunState, now: datetime
    ) -> None:
        # The record's only consumer is a JSONL trace file. A field filled in
        # memory and dropped on the way out is the same null one layer later.
        llm = self._live_shaped_client(_stop_payload(), took_seconds=0.42)
        _state, _step, record = BaselineStrategy().plan_next_step(
            _investigating(run_state), now, _context(llm)
        )
        written = json.loads(json.dumps(record.as_trace_record()))
        assert written["llm_calls"][0]["elapsed_ms"] == 420

    def test_a_canned_run_still_says_not_measured(self, run_state: RunState, now: datetime) -> None:
        """``None``, not ``0``. The canned client does not time itself, and a
        zero would read as a sub-millisecond model call in every offline
        record the suite has ever written."""
        _state, _step, record = BaselineStrategy().plan_next_step(
            _investigating(run_state), now, _context(CannedLLMClient([_stop_payload()]))
        )
        assert record.llm_calls[0].elapsed_ms is None


class TestTheRegistryRefusesWhatItDoesNotKnow:
    def test_baseline_resolves_to_the_baseline_strategy(self) -> None:
        strategy = STRATEGIES.create("baseline")
        assert isinstance(strategy, BaselineStrategy)
        assert strategy.name == "baseline"
        assert dict(strategy.config) == {}

    def test_an_unknown_name_raises_and_names_what_it_knows(self) -> None:
        with pytest.raises(UnknownStrategyError) as caught:
            STRATEGIES.create("basline")
        message = str(caught.value)
        assert "basline" in message
        for known in STRATEGIES.names:
            assert known in message, "the refusal must list the names the operator can use"

    def test_a_member_is_the_same_key_as_its_value(self) -> None:
        assert STRATEGIES.create(StrategyName.BASELINE).name == "baseline"

    def test_the_registry_and_the_configurable_names_are_the_same_set(self) -> None:
        # Architecture principle 2: a member with no entry fails at run time.
        assert set(STRATEGIES.names) == {member.value for member in StrategyName}

    def test_a_factory_under_the_wrong_key_is_refused(self) -> None:
        # The one way this indirection could lie about what produced a number:
        # a name in the provenance record that is not the strategy that ran.
        mislabelled = StrategyRegistry({"best_of_n": BaselineStrategy})
        with pytest.raises(UnknownStrategyError):
            mislabelled.create("best_of_n")

    def test_each_create_returns_its_own_object(self) -> None:
        assert STRATEGIES.create("baseline") is not STRATEGIES.create("baseline")


class TestTheDefaultIsBaseline:
    """A config typo cannot silently change the control group — either side."""

    def test_the_configured_default_is_baseline(self) -> None:
        assert _settings().inference_strategy is StrategyName.BASELINE

    def test_the_wired_default_is_the_configured_default(self) -> None:
        # ``make_llm_investigate``'s default and ``INFERENCE_STRATEGY``'s
        # default are two decisions in two files; this is what keeps them one.
        assert default_strategy().name == _settings().inference_strategy.value == "baseline"

    def test_an_unknown_configured_strategy_is_refused_at_construction(self) -> None:
        # A name from plan 02 § 4 with no implementation yet is the right stand-in — the
        # placeholder was ``best_of_n_sampled``, then ``reflection`` (WP-5.3, WP-9.1).
        with pytest.raises(ValidationError) as caught:
            _settings(inference_strategy="search")
        message = str(caught.value)
        assert "baseline" in message, (
            "the refusal must name the permitted values; an operator who typed "
            "the wrong one is the person who needs the list"
        )
        for member in StrategyName:
            assert member.value in message

    def test_a_blank_value_falls_back_to_the_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # ``env_ignore_empty``: INFERENCE_STRATEGY= means unset, not "".
        monkeypatch.setenv("INFERENCE_STRATEGY", "")
        assert _settings().inference_strategy is StrategyName.BASELINE

    def test_the_bare_env_var_selects_the_strategy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # No env_prefix on SettingsConfigDict, so the name is bare.
        monkeypatch.setenv("INFERENCE_STRATEGY", "baseline")
        assert _settings().inference_strategy is StrategyName.BASELINE

    def test_make_llm_investigate_runs_baseline_by_default(
        self, run_state: RunState, now: datetime
    ) -> None:
        sink: list[StepRecord] = []
        llm = CannedLLMClient([_stop_payload()])
        make_llm_investigate(_FakeMCPClient(lambda _n, _a: _lag_response()), llm, model="m")(
            _investigating(run_state), now
        )
        # And the same loop with an explicit strategy behaves identically.
        second = CannedLLMClient([_stop_payload()])
        make_llm_investigate(
            _FakeMCPClient(lambda _n, _a: _lag_response()),
            second,
            model="m",
            strategy=STRATEGIES.create("baseline"),
            record_step=sink.append,
        )(_investigating(run_state), now)
        assert [record.strategy for record in sink] == ["baseline"]


class TestTheProtocolIsSatisfied:
    def test_baseline_is_an_investigation_strategy(self) -> None:
        # Structural, checked by the type checker; asserted here so the
        # annotation is exercised by the suite too.
        strategy: InvestigationStrategy = BaselineStrategy()
        assert strategy.name == "baseline"

    def test_the_config_block_cannot_be_written_through(self) -> None:
        # ``config`` is stamped into provenance, so the default is immutable.
        strategy = BaselineStrategy()
        with pytest.raises(TypeError):
            strategy.config["n"] = 8  # type: ignore[index]


# --------------------------------------------------------------------------
# The contract: one call replaced, no policy moved.


def _strategy_modules() -> tuple[Path, ...]:
    modules = tuple(sorted(_STRATEGIES_DIR.glob("*.py")))
    assert modules, "no modules found under agent/strategies/ — this scan is vacuous"
    return modules


def _imported_modules(tree: ast.AST) -> dict[str, tuple[str, ...]]:
    """``module -> names imported from it`` for every import in ``tree``."""
    imports: dict[str, tuple[str, ...]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imports[node.module] = imports.get(node.module, ()) + tuple(
                alias.name for alias in node.names
            )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imports[alias.name] = imports.get(alias.name, ())
    return imports


def _referenced_names(tree: ast.AST) -> set[str]:
    """Every identifier the module's *code* uses.

    AST nodes, not text: naming a thing in prose is not depending on it.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.ImportFrom):
            names.update(alias.name for alias in node.names)
    return names


#: Modules a strategy may not import: the four ways a proposal could become an action.
_FORBIDDEN_MODULES: Final[frozenset[str]] = frozenset(
    {
        "incident_commander.tools.policies",
        "incident_commander.tools.registry",
        "incident_commander.tools.wire",
        "incident_commander.tools.mcp_client",
    }
)

#: Shared execution policy and gate logic, by name. Each is asserted to exist below, so
#: a rename cannot make this match nothing.
_FORBIDDEN_NAMES: Final[frozenset[str]] = frozenset(
    {
        "FIX_MAP",
        "HINT_ROUTED_CATEGORIES",
        "HINT_ROUTED_TOOLS",
        "CONTRADICTED_HINT_TOOLS",
        "ALERT_SUBJECT_PROBES",
        "TOOL_REGISTRY",
        "RESOURCE_ARG_FIELDS",
        "RESOLUTION_CLASS",
        "Tier",
        "tier_of",
        "tools_at_or_below",
        "is_cached_read",
        "wire_arguments",
        "alert_subject",
        "_alert_subject_probed",
        "_execute_probe",
        "_REMEDIATE_CONFIDENCE_THRESHOLD",
        "_MAX_SUBJECT_PROBE_REFUSALS",
    }
)


class TestStrategiesHoldNoExecutionPolicy:
    """The seam replaces one call. It does not carry the policy around it.

    Plan 02 § 2: the same execution policy gates every real action — a property of the
    import graph, so it is scanned.
    """

    @pytest.mark.parametrize("module", _strategy_modules(), ids=lambda path: path.name)
    def test_no_module_imports_the_tool_surface(self, module: Path) -> None:
        imports = _imported_modules(ast.parse(module.read_text(encoding="utf-8")))
        offenders = sorted(set(imports) & _FORBIDDEN_MODULES)
        assert offenders == [], (
            f"{module.name} imports {offenders}. A strategy proposes; it never "
            "reads a tier, wires an argument or calls a tool."
        )

    @pytest.mark.parametrize("module", _strategy_modules(), ids=lambda path: path.name)
    def test_no_module_references_a_gate(self, module: Path) -> None:
        referenced = _referenced_names(ast.parse(module.read_text(encoding="utf-8")))
        offenders = sorted(referenced & _FORBIDDEN_NAMES)
        assert offenders == [], (
            f"{module.name} references {offenders}. The FIX_MAP gate, the 0.7 "
            "threshold, the subject-probe guard and the tier re-check stay in "
            "investigation.py, shared by every strategy."
        )

    @pytest.mark.parametrize("module", _strategy_modules(), ids=lambda path: path.name)
    def test_the_only_thing_taken_from_the_loop_is_the_planner_call(self, module: Path) -> None:
        imports = _imported_modules(ast.parse(module.read_text(encoding="utf-8")))
        taken = imports.get("incident_commander.agent.investigation", ())
        assert set(taken) <= {"_plan_next_step"}, (
            f"{module.name} imports {sorted(taken)} from the investigation loop. "
            "WP-0.2 replaces exactly one call; anything else pulled across is a "
            "second seam nobody decided to open."
        )

    def test_the_names_this_scan_forbids_are_real(self) -> None:
        # Anti-vacuity. If a gate is renamed and this list is not, the scan
        # above starts passing for the wrong reason.
        haystack = _INVESTIGATION.read_text(encoding="utf-8") + _POLICIES.read_text(
            encoding="utf-8"
        )
        missing = sorted(name for name in _FORBIDDEN_NAMES if name not in haystack)
        assert missing == [], (
            f"{missing} appear in neither investigation.py nor policies.py. Either "
            "the gate was renamed (update this list) or it is gone (update the ADR)."
        )

    def test_the_gates_still_run_on_the_strategy_path(
        self, run_state: RunState, now: datetime
    ) -> None:
        # The FIX_MAP gate and the 0.7 threshold, exercised through the seam: a
        # remediate handoff below the threshold escalates, as it always has.
        low = {
            **_stop_payload(confidence=0.5),
            "next_action": {"kind": "remediate", "reason": "fix it"},
        }
        llm = CannedLLMClient([low])
        result = make_llm_investigate(
            _FakeMCPClient(lambda _n, _a: _lag_response()), llm, model="m"
        )(_investigating(run_state), now)

        assert result.state is IncidentState.ESCALATED
        assert "below threshold" in result.evidence[-1].result_summary

    def test_a_strategy_cannot_reach_a_tool_through_its_context(self) -> None:
        # The whole of a strategy's reach: models, its own settings, a sink. The selector and critic
        # clients are further LLM clients, not a widening; an MCP client or the run itself would be.
        fields = set(StrategyContext.__dataclass_fields__)
        assert fields == {
            "llm_client",
            "model",
            "iteration",
            "config",
            "record_step",
            "selector_llm_client",
            "critic_llm_client",
        }
        assert all("client" not in name or name.endswith("llm_client") for name in fields), (
            f"a non-LLM client reached StrategyContext: {sorted(fields)}. A "
            "strategy proposes; the loop acts (ADR 0036)."
        )


class TestTheRunRecordNamesTheStrategy:
    def test_provenance_stamps_the_strategy_that_ran(self) -> None:
        # WP-0.3's record (cmd #223) carried the placeholder ``"builtin"``.
        # This packet fills it from the object that made the planner calls.
        from evals.runner import ExecutionMode, build_provenance
        from incident_commander.agent.state import BudgetLedger
        from incident_commander.config import ModelRole

        provenance = build_provenance(
            "s",
            _settings(),
            model_role=ModelRole.DEVELOPMENT,
            invocation_id="inv000000001",
            execution_mode=ExecutionMode.CANNED,
            budget=BudgetLedger(
                max_tool_calls=1, max_tokens=1, max_wall_seconds=1, max_usd=Decimal("1")
            ),
            strategy=STRATEGIES.create("baseline"),
        )
        assert provenance.strategy == "baseline"
        assert provenance.strategy_config == {}

    def test_the_crash_path_stamps_the_configured_strategy(self) -> None:
        # A scenario can crash before a strategy is built. The configured one
        # is the only honest answer there, and "" would read as a value.
        from evals.runner import ExecutionMode, build_provenance
        from incident_commander.agent.state import BudgetLedger
        from incident_commander.config import ModelRole

        provenance = build_provenance(
            "s",
            _settings(),
            model_role=ModelRole.DEVELOPMENT,
            invocation_id="inv000000001",
            execution_mode=ExecutionMode.CANNED,
            budget=BudgetLedger(
                max_tool_calls=1, max_tokens=1, max_wall_seconds=1, max_usd=Decimal("1")
            ),
        )
        assert provenance.strategy == "baseline"
