"""WP-5.3 — ``best_of_n_sampled``: N draws, one emitted step, N accruals.

The order's acceptance list, one class each:

* ``TestNIndependentCallsAllAccrue`` — N calls are made and **all N reach the
  ledger**, asserted against the ledger so an under-accrual is a failure. The
  hard half is the failure path: when draw k raises, the k−1 that returned are
  charged too (ADR 0045), because the loop's own ``accrue_llm_error`` runs
  against the state this strategy never got to return.
* ``TestTheUnionDeduplicates`` — two samples that agree produce one candidate.
* ``TestTheTemperatureIsApplied`` — asserted **on the client call**, not on a
  setting, and the request body is checked for the field itself.
* ``TestOtherRolesSendNoTemperature`` — the other half of that: adding the
  parameter moved no other call's request bytes.
* ``TestTheEmittedStepIsOneSamples`` — never a blend (ADR 0045).
* ``TestTheArmIsConfigGated`` — it does not run in the development loop by
  default: ``baseline`` is still the configured strategy and N is still 1.
* ``TestTheCostMultiplierIsDeclared`` — the declaration reaches this arm's
  ledger. Measuring it against the real token cost needs a run and is DEFERRED;
  the tolerance is stated here so the deferred check has a bar to meet.
* ``TestTheSamplingRejectionTripwire`` — no priced model rejects sampling, so
  the day one is priced the suite says so instead of a paid sweep discovering it.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any, Final
from uuid import uuid4

import pytest
from pydantic import BaseModel, ValidationError

from incident_commander.agent.accounting import RunAccounting
from incident_commander.agent.factory import start_run
from incident_commander.agent.hypothesis import InvestigationStep
from incident_commander.agent.investigation import make_llm_investigate
from incident_commander.agent.planner_context import EVIDENCE_ID_PREFIX
from incident_commander.agent.state import IncidentState, RunState
from incident_commander.agent.strategies.best_of_n_sampled import (
    DEFAULT_SAMPLE_TEMPERATURE,
    BestOfNSampledStrategy,
    SampledPlannerFailed,
)
from incident_commander.agent.strategies.knobs import StrategyKnobs
from incident_commander.agent.strategies.names import StrategyName
from incident_commander.agent.strategies.protocol import InvestigationStrategy, StrategyContext
from incident_commander.agent.strategies.records import StepRecord
from incident_commander.agent.strategies.registry import STRATEGIES
from incident_commander.config import Settings
from incident_commander.llm.client import (
    SAMPLING_REJECTED_MODELS,
    LLMClient,
    LLMClientProtocol,
    LLMError,
    LLMOutputError,
    LLMResult,
    LLMUsage,
)
from incident_commander.llm.fakes import CannedLLMClient, CannedUsage
from incident_commander.llm.pricing import MODEL_PRICING
from incident_commander.tools.mcp_client import ToolResult

_MODEL: Final[str] = "claude-sonnet-4-6"


# --------------------------------------------------------------------------
# Fakes. Local, so this file can fail on its own.


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


def _lag_response() -> ToolResult:
    return ToolResult(
        content=[
            {
                "type": "text",
                "text": json.dumps(
                    {"consumer_group": "billing", "lag": 42, "lag_known": True, "source": "static"}
                ),
            }
        ]
    )


class _RaisesOnCall:
    """Returns canned payloads until ``fail_on``, then raises.

    The fixture the failure-path accrual is proved with: the calls before the
    failure are real, billed calls, so the ledger has something to lose.
    """

    def __init__(
        self,
        payloads: list[dict[str, Any]],
        *,
        fail_on: int,
        usage: CannedUsage,
        error: Exception | None = None,
    ) -> None:
        self._payloads = payloads
        self._fail_on = fail_on
        self._usage = usage
        self._error = error or LLMError(
            "transport died",
            usage=LLMUsage(input_tokens=7, output_tokens=3),
        )
        self.made = 0

    def call[T: BaseModel](
        self,
        system_prompt: str,
        user_message: str,
        output_model: type[T],
        model: str,
        max_tokens: int = 4096,
        *,
        repair_of: str | None = None,
        temperature: float | None = None,
    ) -> LLMResult[T]:
        self.made += 1
        if self.made == self._fail_on:
            raise self._error
        payload = self._payloads[(self.made - 1) % len(self._payloads)]
        return LLMResult(
            output=output_model.model_validate(payload),
            input_tokens=self._usage.input_tokens,
            output_tokens=self._usage.output_tokens,
            cache_creation_tokens=self._usage.cache_creation_tokens,
            cache_read_tokens=self._usage.cache_read_tokens,
            stop_reason="canned",
        )


class _RecordingAnthropic:
    """Captures the request body the real client builds, and answers it."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.requests: list[dict[str, Any]] = []
        self.messages = self
        self._payload = payload

    def create(self, **body: Any) -> Any:
        self.requests.append(body)

        class _Usage:
            input_tokens = 5
            output_tokens = 2
            cache_creation_input_tokens = 0
            cache_read_input_tokens = 0

        class _Block:
            type = "tool_use"
            name = "record_output"

            def __init__(self, payload: dict[str, Any]) -> None:
                self.input = payload

        class _Message:
            stop_reason = "tool_use"

            def __init__(self, payload: dict[str, Any]) -> None:
                self.content = [_Block(payload)]
                self.usage = _Usage()

            def model_dump(self, **_: Any) -> dict[str, Any]:
                return {}

        return _Message(self._payload)


# --------------------------------------------------------------------------
# Payload builders.


def _step(
    *,
    name: str = "lag",
    category: str = "consumer_saturation",
    confidence: float = 0.9,
    action: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "hypotheses": [
            {
                "category": category,
                "name": name,
                "confidence": confidence,
                "reasoning": f"{name} is the most likely cause",
            }
        ],
        "next_action": action or {"kind": "stop", "reason": "confidence sufficient"},
    }


def _probe_action(tool: str = "get_consumer_lag") -> dict[str, Any]:
    return {"kind": "probe", "tool_name": tool, "arguments": {"consumer_group": "billing"}}


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
        model=_MODEL,
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


def _arm(*, n: int, temperature: float | None = None) -> BestOfNSampledStrategy:
    return BestOfNSampledStrategy(StrategyKnobs(n=n, sample_temperature=temperature))


# --------------------------------------------------------------------------


class TestNIndependentCallsAllAccrue:
    """N calls, N accruals — asserted against the ledger, both paths."""

    @pytest.mark.parametrize("n", [1, 2, 4, 8])
    def test_n_calls_are_made(self, n: int, run_state: RunState, now: datetime) -> None:
        llm = CannedLLMClient([_step() for _ in range(n)])
        _arm(n=n).plan_next_step(_investigating(run_state), now, _context(llm))
        assert len(llm.calls) == n

    @pytest.mark.parametrize("n", [1, 2, 4, 8])
    def test_every_call_reaches_the_ledger(
        self, n: int, run_state: RunState, now: datetime
    ) -> None:
        """One call's tokens × N, not one call's tokens.

        The under-report this asserts against is the whole reason the order calls
        this the arm where ADR 0015's guarantee is easiest to break.
        """
        usage = CannedUsage(input_tokens=100, output_tokens=40)
        llm = CannedLLMClient([_step() for _ in range(n)], usage=usage)
        state = _investigating(run_state)
        updated, _, _ = _arm(n=n).plan_next_step(state, now, _context(llm))
        assert updated.budget.tokens_used == n * 140
        assert updated.budget.usd_used > state.budget.usd_used

    def test_a_failure_on_the_last_draw_still_charges_the_earlier_ones(
        self, run_state: RunState, now: datetime
    ) -> None:
        """ADR 0045's failure path, through the real loop.

        Four draws, the fourth raises. The loop catches it, charges
        ``accrue_llm_error`` against the state it held BEFORE the call — so the
        three that returned are only charged if the exception carried them.
        """
        llm = _RaisesOnCall(
            [_step()], fail_on=4, usage=CannedUsage(input_tokens=100, output_tokens=40)
        )
        result = make_llm_investigate(
            _FakeMCPClient(lambda _n, _a: _lag_response()),
            llm,
            model=_MODEL,
            strategy=_arm(n=4),
        )(_investigating(run_state), now)

        assert result.state is IncidentState.ESCALATED
        assert "sampled planner call 4 of 4 failed" in result.evidence[-1].result_summary
        # 3 completed draws at 140 tokens each, plus the failing call's own 10.
        assert result.budget.tokens_used == 3 * 140 + 10

    def test_the_exception_is_an_llm_error_the_loop_already_catches(self) -> None:
        # No new except arm in the loop, and no strategy deciding on its own
        # that a failure is survivable.
        assert issubclass(SampledPlannerFailed, LLMError)

    def test_a_repair_inside_one_draw_is_recorded_and_charged_what_it_billed(
        self, run_state: RunState, now: datetime
    ) -> None:
        """ADR 0035's re-ask is one more call, on this arm as on ``baseline``.

        Three calls for two samples: draw 1's payload failed validation and was
        re-asked once. The ledger moves by 2 x 140 rather than 3 x 140, and that
        is correct rather than a gap: ``CannedLLMClient`` raises from
        ``model_validate`` before it builds an ``LLMResult``, so the rejected leg
        reports no usage at all — the same case ``MeteredLLMClient`` documents,
        where charging a guess would be an over-report invented rather than
        measured. A real client attaches the billed usage to the exception and
        ``accrue_structured_call`` walks ``call.failures`` and charges it.
        """
        usage = CannedUsage(input_tokens=100, output_tokens=40)
        llm = CannedLLMClient([{"hypotheses": []}, _step(), _step()], usage=usage)
        updated, _, record = _arm(n=2).plan_next_step(_investigating(run_state), now, _context(llm))
        assert len(llm.calls) == 3
        assert updated.budget.tokens_used == 2 * 140
        assert record.generation_rejections == ("sample_1_repaired",)

    def test_a_failure_with_no_usage_anywhere_charges_nothing_invented(
        self, run_state: RunState, now: datetime
    ) -> None:
        """A transport failure that reports no usage is charged nothing invented.

        A transport ``LLMError`` rather than a ``ValueError``, deliberately: a
        ``ValueError`` is *repairable* (ADR 0035), so it would buy a re-ask and
        succeed rather than reach the loop. This is the unrepairable path.
        """
        llm = _RaisesOnCall(
            [_step()],
            fail_on=1,
            usage=CannedUsage(),
            error=LLMError("transport died before it billed anything"),
        )
        with pytest.raises(SampledPlannerFailed) as caught:
            _arm(n=2).plan_next_step(_investigating(run_state), now, _context(llm))
        assert caught.value.usage is None

    def test_one_call_record_per_sample_and_one_ledger_delta(
        self, run_state: RunState, now: datetime
    ) -> None:
        usage = CannedUsage(input_tokens=100, output_tokens=40)
        llm = CannedLLMClient([_step(), _step(), _step(), _step()], usage=usage)
        _, _, record = _arm(n=4).plan_next_step(_investigating(run_state), now, _context(llm))
        assert len(record.llm_calls) == 4
        # The delta is a per-step quantity: on the first record and zero after,
        # so summing the column gives the step's charge and not N times it.
        assert record.llm_calls[0].tokens_used == 4 * 140
        assert [call.tokens_used for call in record.llm_calls[1:]] == [0, 0, 0]
        assert all(call.input_tokens == 100 for call in record.llm_calls)


class TestTheUnionDeduplicates:
    """Two samples that agree produce one candidate, not two."""

    def test_agreeing_samples_collapse(self, run_state: RunState, now: datetime) -> None:
        llm = CannedLLMClient([_step(name="lag"), _step(name="lag"), _step(name="lag")])
        _, _, record = _arm(n=3).plan_next_step(_investigating(run_state), now, _context(llm))
        assert len(record.candidate_set) == 1
        assert record.candidate_set[0].name == "lag"

    def test_disagreeing_samples_are_all_kept(self, run_state: RunState, now: datetime) -> None:
        llm = CannedLLMClient(
            [
                _step(name="lag", confidence=0.9),
                _step(name="cache", category="stale_cache", confidence=0.4),
                _step(name="db", category="db_query_latency", confidence=0.6),
            ]
        )
        _, _, record = _arm(n=3).plan_next_step(_investigating(run_state), now, _context(llm))
        # Confidence-descending, like every other arm's record.
        assert [c.name for c in record.candidate_set] == ["lag", "db", "cache"]

    def test_the_key_is_category_and_name_together(
        self, run_state: RunState, now: datetime
    ) -> None:
        # Same name under two categories is two diagnoses, not one.
        llm = CannedLLMClient(
            [
                _step(name="stuck", category="consumer_saturation", confidence=0.8),
                _step(name="stuck", category="resolver_stall", confidence=0.5),
            ]
        )
        _, _, record = _arm(n=2).plan_next_step(_investigating(run_state), now, _context(llm))
        assert len(record.candidate_set) == 2

    def test_a_differently_cased_name_is_not_folded_together(
        self, run_state: RunState, now: datetime
    ) -> None:
        """The rule ``agent/candidates.py`` applies inside one set, applied across draws.

        ``name`` is operator-facing free text. Folding case here would make the
        duplicate rate a measurement of the normaliser.
        """
        llm = CannedLLMClient([_step(name="Lag"), _step(name="lag")])
        _, _, record = _arm(n=2).plan_next_step(_investigating(run_state), now, _context(llm))
        assert len(record.candidate_set) == 2

    def test_the_first_occurrence_keeps_the_record(
        self, run_state: RunState, now: datetime
    ) -> None:
        # A later agreeing sample is agreement, not a second candidate — so the
        # recorded confidence is the first draw's.
        llm = CannedLLMClient(
            [_step(name="lag", confidence=0.9), _step(name="lag", confidence=0.3)]
        )
        _, _, record = _arm(n=2).plan_next_step(_investigating(run_state), now, _context(llm))
        assert record.candidate_set[0].confidence == pytest.approx(0.9)
        assert record.candidate_set[0].candidate_id == "sample-1"

    def test_branch_count_follows_the_union(self, run_state: RunState, now: datetime) -> None:
        """WP-2.3's ``branch_count`` is candidates beyond the emitted one.

        Four draws that produced two distinct diagnoses branch once, not three
        times — which is the honest reading and the reason the union is recorded
        rather than the raw draws.
        """
        accounting = RunAccounting()
        llm = CannedLLMClient(
            [
                _step(name="lag", confidence=0.9),
                _step(name="lag", confidence=0.9),
                _step(name="cache", category="stale_cache", confidence=0.2),
                _step(name="lag", confidence=0.9),
            ]
        )
        make_llm_investigate(
            _FakeMCPClient(lambda _n, _a: _lag_response()),
            llm,
            model=_MODEL,
            strategy=_arm(n=4),
            record_step=accounting.step_sink(),
        )(_investigating(run_state), now)
        assert accounting.branch_count == 1
        assert accounting.selector_calls == 0


class TestTheTemperatureIsApplied:
    """Asserted on the client call and in the request body, never assumed."""

    def test_every_draw_carries_the_configured_temperature(
        self, run_state: RunState, now: datetime
    ) -> None:
        llm = CannedLLMClient([_step(), _step(), _step()])
        _arm(n=3, temperature=0.8).plan_next_step(_investigating(run_state), now, _context(llm))
        assert llm.temperatures == [0.8, 0.8, 0.8]

    def test_the_repair_re_ask_is_made_at_the_same_temperature(
        self, run_state: RunState, now: datetime
    ) -> None:
        """A re-ask at a different temperature would be a different draw."""
        llm = CannedLLMClient([{"hypotheses": []}, _step()])
        _arm(n=1, temperature=0.3).plan_next_step(_investigating(run_state), now, _context(llm))
        assert llm.temperatures == [0.3, 0.3]

    def test_the_request_body_carries_the_field(self) -> None:
        """Through the REAL client, because that is where the field is rendered."""
        anthropic = _RecordingAnthropic(_step())
        client = LLMClient(api_key="k", client=anthropic)  # type: ignore[arg-type]
        client.call(
            system_prompt="s",
            user_message="u",
            output_model=InvestigationStep,
            model=_MODEL,
            temperature=0.7,
        )
        assert anthropic.requests[0]["temperature"] == 0.7
        # And forced tool use is untouched: the schema still constrains the
        # payload, temperature only varies what goes in it.
        assert anthropic.requests[0]["tool_choice"] == {"type": "tool", "name": "record_output"}

    def test_the_default_is_the_plans_default(self) -> None:
        assert DEFAULT_SAMPLE_TEMPERATURE == 1.0
        assert _arm(n=2).temperature == 1.0
        assert _settings().sample_temperature == 1.0

    def test_the_setting_is_bounded_to_the_api_range(self) -> None:
        for out_of_range in (-0.1, 1.1):
            with pytest.raises(ValidationError):
                _settings(sample_temperature=out_of_range)
        assert _settings(sample_temperature=0.0).sample_temperature == 0.0

    def test_the_bare_env_var_sets_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SAMPLE_TEMPERATURE", "0.5")
        assert _settings().sample_temperature == 0.5

    def test_the_runner_carries_it_into_the_knobs(self) -> None:
        from evals.runner import strategy_knobs

        knobs = strategy_knobs(_settings(best_of_n=4, sample_temperature=0.6))
        assert (knobs.n, knobs.sample_temperature) == (4, 0.6)


class TestOtherRolesSendNoTemperature:
    """The other half of the change: nobody else's request bytes moved."""

    def test_the_field_is_absent_when_none_is_asked_for(self) -> None:
        anthropic = _RecordingAnthropic(_step())
        client = LLMClient(api_key="k", client=anthropic)  # type: ignore[arg-type]
        client.call(
            system_prompt="s", user_message="u", output_model=InvestigationStep, model=_MODEL
        )
        assert "temperature" not in anthropic.requests[0]

    def test_baseline_sends_none(self, run_state: RunState, now: datetime) -> None:
        llm = CannedLLMClient([_step()])
        STRATEGIES.create(StrategyName.BASELINE).plan_next_step(
            _investigating(run_state), now, _context(llm)
        )
        assert llm.temperatures == [None]

    def test_the_enumerated_arm_sends_none_either(self, run_state: RunState, now: datetime) -> None:
        from incident_commander.agent.strategies.best_of_n_enumerated import (
            BestOfNEnumeratedStrategy,
        )

        entry_id = uuid4()
        state = _investigating(run_state)
        llm = CannedLLMClient(
            [
                {
                    "candidates": [
                        {
                            "candidate_id": "c1",
                            "category": "consumer_saturation",
                            "name": "lag",
                            "confidence": 0.9,
                            "evidence_for": [],
                            "evidence_against": [],
                            "next_probe": None,
                        }
                    ],
                    "next_action": {"kind": "stop", "reason": "done"},
                }
            ]
        )
        BestOfNEnumeratedStrategy(StrategyKnobs(n=1)).plan_next_step(state, now, _context(llm))
        assert llm.temperatures == [None]
        assert entry_id  # the ledger is empty here; the candidate cites nothing


class TestTheEmittedStepIsOneSamples:
    """Never a blend (ADR 0045)."""

    def test_the_most_confident_samples_whole_step_is_emitted(
        self, run_state: RunState, now: datetime
    ) -> None:
        weak = _step(name="weak", confidence=0.2, action=_probe_action("get_redis_health"))
        strong = _step(name="strong", confidence=0.95, action=_probe_action("get_consumer_lag"))
        llm = CannedLLMClient([weak, strong])
        _, step, _ = _arm(n=2).plan_next_step(_investigating(run_state), now, _context(llm))
        assert step.hypotheses[0].name == "strong"
        # The ACTION came from the same sample as the ranking — that is the
        # decision ADR 0045 records. A blend would emit get_redis_health here.
        assert step.next_action.tool_name == "get_consumer_lag"  # type: ignore[union-attr]
        # And it is that sample's own reasoning, not a derived string.
        assert step.hypotheses[0].reasoning == "strong is the most likely cause"

    def test_a_tie_resolves_to_the_earlier_draw(self, run_state: RunState, now: datetime) -> None:
        first = _step(name="first", confidence=0.7, action=_probe_action("get_consumer_lag"))
        second = _step(name="second", confidence=0.7, action=_probe_action("get_redis_health"))
        llm = CannedLLMClient([first, second])
        _, step, _ = _arm(n=2).plan_next_step(_investigating(run_state), now, _context(llm))
        assert step.hypotheses[0].name == "first"

    def test_the_gates_still_run_on_the_emitted_step(
        self, run_state: RunState, now: datetime
    ) -> None:
        low = _step(confidence=0.5, action={"kind": "remediate", "reason": "fix it"})
        result = make_llm_investigate(
            _FakeMCPClient(lambda _n, _a: _lag_response()),
            CannedLLMClient([low, low]),
            model=_MODEL,
            strategy=_arm(n=2),
        )(_investigating(run_state), now)
        assert result.state is IncidentState.ESCALATED
        assert "below threshold" in result.evidence[-1].result_summary

    def test_an_unmapped_category_still_stops_at_the_shared_gate(
        self, run_state: RunState, now: datetime
    ) -> None:
        unmapped = _step(
            category="db_query_latency",
            confidence=0.95,
            action={"kind": "remediate", "reason": "fix it"},
        )
        result = make_llm_investigate(
            _FakeMCPClient(lambda _n, _a: _lag_response()),
            CannedLLMClient([unmapped, unmapped]),
            model=_MODEL,
            strategy=_arm(n=2),
        )(_investigating(run_state), now)
        assert result.state is IncidentState.ESCALATED
        assert "no Tier-1 fix" in result.evidence[-1].result_summary

    def test_the_context_is_rendered_once_and_sent_to_every_draw(
        self, run_state: RunState, now: datetime
    ) -> None:
        """What makes them samples of ONE distribution."""
        llm = CannedLLMClient([_step(), _step(), _step()])
        _arm(n=3).plan_next_step(_investigating(run_state), now, _context(llm))
        assert len({call for call in llm.calls}) == 1

    def test_this_arm_is_shown_no_evidence_ids(self, run_state: RunState, now: datetime) -> None:
        """Its schema cites none, so it has no reason to see them (ADR 0044)."""
        llm = CannedLLMClient([_step()])
        arm = _arm(n=1)
        arm.plan_next_step(_investigating(run_state), now, _context(llm))
        assert EVIDENCE_ID_PREFIX not in llm.calls[0][1]
        assert arm.config["evidence_ids_rendered"] is False


class TestTheArmIsConfigGated:
    """It does not run in the development loop by default."""

    def test_the_default_strategy_is_still_baseline(self) -> None:
        assert _settings().inference_strategy is StrategyName.BASELINE

    def test_selecting_it_takes_an_explicit_value(self) -> None:
        assert (
            _settings(inference_strategy="best_of_n_sampled").inference_strategy
            is StrategyName.BEST_OF_N_SAMPLED
        )

    def test_selecting_it_without_setting_n_draws_once(self) -> None:
        # Not eight. A caller who forgot the configuration gets one call.
        arm = STRATEGIES.create(StrategyName.BEST_OF_N_SAMPLED)
        assert isinstance(arm, BestOfNSampledStrategy)
        assert arm.n == 1

    def test_the_registry_builds_it_with_the_knobs(self) -> None:
        arm = STRATEGIES.create("best_of_n_sampled", StrategyKnobs(n=8, sample_temperature=0.9))
        assert dict(arm.config) == {
            "n": 8,
            "sample_temperature": "0.9",
            "evidence_ids_rendered": False,
        }

    def test_the_registry_and_the_configurable_names_are_still_one_set(self) -> None:
        assert set(STRATEGIES.names) == {member.value for member in StrategyName}

    def test_it_satisfies_the_protocol(self) -> None:
        strategy: InvestigationStrategy = BestOfNSampledStrategy(StrategyKnobs(n=2))
        assert strategy.name == "best_of_n_sampled"

    def test_the_stamp_cannot_be_written_through(self) -> None:
        with pytest.raises(TypeError):
            _arm(n=2).config["n"] = 8  # type: ignore[index]


class TestTheCostMultiplierIsDeclared:
    """Declared, not discovered (decision C12). Measuring it is DEFERRED."""

    def test_the_declared_multiplier_seeds_this_arms_ledger(self, now: datetime) -> None:
        settings = _settings(
            inference_strategy="best_of_n_sampled",
            best_of_n=8,
            token_budget_multiplier="2.5",
            usd_budget_multiplier="2.5",
        )
        # 03 section 11's blended figure for this arm.
        assert settings.seeded_max_tokens == int(
            Decimal(settings.budget_max_tokens) * Decimal("2.5")
        )
        run = start_run({"source": "kafka"}, settings, now)
        assert run.budget.max_tokens == settings.seeded_max_tokens

    def test_an_unfunded_arm_is_not_silently_rescued(self, now: datetime) -> None:
        """Multiplier 1 is legal and is a budget experiment, not a strategy one.

        Nothing here refuses it: the arm stamps ``n`` and the provenance record
        carries the seeded ledger, so a BUDGET result can be read for what it is
        (decision C4). The refusal would be the wrong guard — "does best-of-8
        fit in baseline's budget" is a question worth being able to ask.
        """
        settings = _settings(inference_strategy="best_of_n_sampled", best_of_n=8)
        assert settings.seeded_max_tokens == settings.budget_max_tokens
        assert start_run({"source": "kafka"}, settings, now).budget.max_tokens == (
            settings.budget_max_tokens
        )

    def test_the_tokens_the_measurement_would_compare_are_on_the_record(
        self, run_state: RunState, now: datetime
    ) -> None:
        """The deferred check needs a number; this is where it comes from.

        ``investigation_planner`` role tokens for this arm over the same
        scenarios as a ``baseline`` run, compared against the declared
        multiplier within ±20%.
        """
        accounting = RunAccounting()
        usage = CannedUsage(input_tokens=100, output_tokens=40)
        make_llm_investigate(
            _FakeMCPClient(lambda _n, _a: _lag_response()),
            accounting.meter(
                CannedLLMClient([_step(), _step(), _step(), _step()], usage=usage),
                "investigation_planner",
            ),
            model=_MODEL,
            strategy=_arm(n=4),
            record_step=accounting.step_sink(),
        )(_investigating(run_state), now)
        planner = next(role for role in accounting.roles if role.role == "investigation_planner")
        assert planner.calls == 4
        assert planner.tokens_used == 4 * 140


class TestTheSamplingRejectionTripwire:
    """No priced model rejects sampling — and the day one does, this says so."""

    def test_no_priced_model_is_on_the_rejection_list(self) -> None:
        overlap = sorted(set(MODEL_PRICING) & SAMPLING_REJECTED_MODELS)
        assert overlap == [], (
            f"{overlap} can be configured as AGENT_MODEL and reject the sampling "
            "parameters, so best_of_n_sampled would 400 on its first planner "
            "call. Either that arm needs a different mechanism on those models, "
            "or the id does not belong in MODEL_PRICING."
        )

    def test_the_list_is_not_empty(self) -> None:
        # Anti-vacuity: an empty list would make the check above pass for the
        # wrong reason.
        assert SAMPLING_REJECTED_MODELS

    def test_it_is_a_tripwire_and_not_a_refusal(self) -> None:
        """Nothing blocks a model that is not on the list.

        A wrong refusal stops a legitimate run; a wrong allow is a rejected
        request the provider does not bill, which the client already turns into
        one escalation.
        """
        anthropic = _RecordingAnthropic(_step())
        client = LLMClient(api_key="k", client=anthropic)  # type: ignore[arg-type]
        unknown = "claude-some-model-nobody-listed"
        client.call(
            system_prompt="s",
            user_message="u",
            output_model=InvestigationStep,
            model=unknown,
            temperature=0.5,
        )
        assert anthropic.requests[0]["model"] == unknown

    def test_an_output_failure_is_still_repairable_on_this_arm(self) -> None:
        # Sanity: the exception type the repair loop keys on is unchanged by the
        # temperature parameter.
        assert issubclass(LLMOutputError, LLMError)
