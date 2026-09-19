from __future__ import annotations

from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock

import anthropic
import httpx
import pytest
from pydantic import BaseModel

from incident_commander.llm.client import LLMClient, LLMError, preflight_auth
from incident_commander.llm.fakes import CannedLLMClient


class _SampleOutput(BaseModel):
    label: str
    confidence: float


def _tool_use_message(payload: dict[str, Any], **usage: int) -> MagicMock:
    """Fake the Anthropic ``Message`` shape returned by ``messages.create``."""
    block = MagicMock()
    block.type = "tool_use"
    block.name = "record_output"
    block.input = payload

    response = MagicMock()
    response.content = [block]
    response.stop_reason = "tool_use"
    response.usage.input_tokens = usage.get("input_tokens", 100)
    response.usage.output_tokens = usage.get("output_tokens", 50)
    response.usage.cache_creation_input_tokens = usage.get("cache_creation_input_tokens", 0)
    response.usage.cache_read_input_tokens = usage.get("cache_read_input_tokens", 0)
    return response


def _client(mock_sdk: MagicMock, max_attempts: int = 3) -> LLMClient:
    return LLMClient(
        api_key="test",
        max_attempts=max_attempts,
        retry_base_delay=0.0,
        sleep=lambda _s: None,
        client=mock_sdk,
    )


class TestCall:
    def test_returns_parsed_output(self) -> None:
        sdk = MagicMock()
        sdk.messages.create.return_value = _tool_use_message({"label": "ok", "confidence": 0.9})
        result = _client(sdk).call(
            system_prompt="sys",
            user_message="hi",
            output_model=_SampleOutput,
            model="claude-sonnet-4-6",
        )
        assert result.output == _SampleOutput(label="ok", confidence=0.9)
        assert result.input_tokens == 100
        assert result.output_tokens == 50

    def test_records_cache_usage_when_present(self) -> None:
        sdk = MagicMock()
        sdk.messages.create.return_value = _tool_use_message(
            {"label": "ok", "confidence": 0.5},
            cache_creation_input_tokens=200,
            cache_read_input_tokens=1000,
        )
        result = _client(sdk).call(
            system_prompt="sys",
            user_message="hi",
            output_model=_SampleOutput,
            model="claude-sonnet-4-6",
        )
        assert result.cache_creation_tokens == 200
        assert result.cache_read_tokens == 1000

    def test_sends_prompt_caching_control(self) -> None:
        sdk = MagicMock()
        sdk.messages.create.return_value = _tool_use_message({"label": "ok", "confidence": 0.5})
        _client(sdk).call(
            system_prompt="the-system",
            user_message="hi",
            output_model=_SampleOutput,
            model="claude-sonnet-4-6",
        )
        body = sdk.messages.create.call_args.kwargs
        assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
        assert body["system"][0]["text"] == "the-system"

    def test_forces_structured_tool_call(self) -> None:
        sdk = MagicMock()
        sdk.messages.create.return_value = _tool_use_message({"label": "ok", "confidence": 0.5})
        _client(sdk).call(
            system_prompt="s",
            user_message="u",
            output_model=_SampleOutput,
            model="claude-sonnet-4-6",
        )
        body = sdk.messages.create.call_args.kwargs
        assert body["tool_choice"] == {"type": "tool", "name": "record_output"}
        assert body["tools"][0]["name"] == "record_output"
        assert body["tools"][0]["input_schema"] == _SampleOutput.model_json_schema()

    def test_missing_tool_use_raises_llm_error(self) -> None:
        sdk = MagicMock()
        text_block = MagicMock()
        text_block.type = "text"
        response = MagicMock()
        response.content = [text_block]
        response.stop_reason = "end_turn"
        response.usage.input_tokens = 1
        response.usage.output_tokens = 1
        response.usage.cache_creation_input_tokens = 0
        response.usage.cache_read_input_tokens = 0
        sdk.messages.create.return_value = response
        with pytest.raises(LLMError, match="no record_output"):
            _client(sdk).call(
                system_prompt="s",
                user_message="u",
                output_model=_SampleOutput,
                model="claude-sonnet-4-6",
            )

    def test_schema_violation_wrapped_as_llm_error(self) -> None:
        # ADR 0007: only the domain exception crosses the boundary.
        sdk = MagicMock()
        sdk.messages.create.return_value = _tool_use_message({"label": "ok"})
        with pytest.raises(LLMError, match="output failed schema validation for _SampleOutput"):
            _client(sdk).call(
                system_prompt="s",
                user_message="u",
                output_model=_SampleOutput,
                model="claude-sonnet-4-6",
            )


class TestRetries:
    def _connection_error(self) -> anthropic.APIConnectionError:
        return anthropic.APIConnectionError(request=MagicMock())

    def _status_error(self, code: int) -> anthropic.APIStatusError:
        response = MagicMock()
        response.status_code = code
        return anthropic.APIStatusError(message="fail", response=response, body=None)

    def test_retries_on_connection_error_then_succeeds(self) -> None:
        sdk = MagicMock()
        sdk.messages.create.side_effect = [
            self._connection_error(),
            _tool_use_message({"label": "ok", "confidence": 1.0}),
        ]
        result = _client(sdk).call(
            system_prompt="s",
            user_message="u",
            output_model=_SampleOutput,
            model="claude-sonnet-4-6",
        )
        assert result.output.label == "ok"

    def test_retries_on_5xx_then_succeeds(self) -> None:
        sdk = MagicMock()
        sdk.messages.create.side_effect = [
            self._status_error(503),
            _tool_use_message({"label": "ok", "confidence": 1.0}),
        ]
        result = _client(sdk).call(
            system_prompt="s",
            user_message="u",
            output_model=_SampleOutput,
            model="claude-sonnet-4-6",
        )
        assert result.output.label == "ok"

    def test_does_not_retry_on_4xx(self) -> None:
        sdk = MagicMock()
        sdk.messages.create.side_effect = self._status_error(400)
        with pytest.raises(LLMError, match="LLM API error 400"):
            _client(sdk).call(
                system_prompt="s",
                user_message="u",
                output_model=_SampleOutput,
                model="claude-sonnet-4-6",
            )
        assert sdk.messages.create.call_count == 1

    def test_retries_on_429_then_succeeds(self) -> None:
        sdk = MagicMock()
        sdk.messages.create.side_effect = [
            self._status_error(429),
            _tool_use_message({"label": "ok", "confidence": 1.0}),
        ]
        result = _client(sdk).call(
            system_prompt="s",
            user_message="u",
            output_model=_SampleOutput,
            model="claude-sonnet-4-6",
        )
        assert result.output.label == "ok"
        assert sdk.messages.create.call_count == 2

    def test_gives_up_after_max_attempts_on_persistent_5xx(self) -> None:
        sdk = MagicMock()
        sdk.messages.create.side_effect = self._status_error(500)
        with pytest.raises(LLMError, match="transport failure after 2 attempts"):
            _client(sdk, max_attempts=2).call(
                system_prompt="s",
                user_message="u",
                output_model=_SampleOutput,
                model="claude-sonnet-4-6",
            )
        assert sdk.messages.create.call_count == 2

    def _rate_limited_then_ok(self, retry_after: str) -> MagicMock:
        # A real httpx.Response so the retry-after header round-trips through
        # the same parsing path a live 429 would take.
        response = httpx.Response(
            429,
            headers={"retry-after": retry_after},
            request=httpx.Request("POST", "https://api.anthropic.test/v1/messages"),
        )
        err = anthropic.APIStatusError(message="rate limited", response=response, body=None)
        sdk = MagicMock()
        sdk.messages.create.side_effect = [
            err,
            _tool_use_message({"label": "ok", "confidence": 1.0}),
        ]
        return sdk

    def _call_recording_delays(self, sdk: MagicMock) -> list[float]:
        delays: list[float] = []
        client = LLMClient(
            api_key="test",
            max_attempts=3,
            retry_base_delay=0.0,
            sleep=delays.append,
            client=sdk,
        )
        result = client.call(
            system_prompt="s",
            user_message="u",
            output_model=_SampleOutput,
            model="claude-sonnet-4-6",
        )
        assert result.output.label == "ok"
        return delays

    def test_honored_retry_after_is_capped(self) -> None:
        # An hour-long server-suggested pause must not stall the sync state
        # machine: the honored Retry-After is capped at 60s (C-07).
        sdk = self._rate_limited_then_ok(retry_after="3600")
        assert self._call_recording_delays(sdk) == [60.0]

    def test_retry_after_below_cap_still_honored(self) -> None:
        sdk = self._rate_limited_then_ok(retry_after="7")
        assert self._call_recording_delays(sdk) == [7.0]


class TestClientBounds:
    """C-07: the default SDK client must not layer its own retries and 600s
    read timeout underneath the outer 3-attempt loop."""

    def test_default_client_disables_sdk_retries_and_bounds_timeout(self) -> None:
        # Constructing anthropic.Anthropic performs no network I/O.
        client = LLMClient(api_key="sk-test")
        assert client._client.max_retries == 0
        timeout = client._client.timeout
        assert isinstance(timeout, httpx.Timeout)
        assert timeout.read == 120.0
        assert timeout.connect == 5.0

    def test_injected_client_is_used_unchanged(self) -> None:
        sdk = MagicMock()
        assert _client(sdk)._client is sdk

    def test_preflight_client_disables_sdk_retries_and_bounds_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}
        fake_client = MagicMock()

        def ctor(**kwargs: Any) -> MagicMock:
            captured.update(kwargs)
            return fake_client

        monkeypatch.setattr(anthropic, "Anthropic", ctor)
        preflight_auth("sk-test")
        assert captured["api_key"] == "sk-test"
        assert captured["max_retries"] == 0
        timeout = captured["timeout"]
        assert isinstance(timeout, httpx.Timeout)
        assert timeout.read == 30.0
        assert timeout.connect == 5.0
        fake_client.models.list.assert_called_once_with(limit=1)


class TestTracer:
    def test_tracer_receives_request_response_output(self) -> None:
        sdk = MagicMock()
        response = _tool_use_message({"label": "ok", "confidence": 0.9})
        response.model_dump.return_value = {"content": [{"type": "tool_use"}]}
        sdk.messages.create.return_value = response
        captured: list[dict[str, Any]] = []
        client = LLMClient(
            api_key="test",
            max_attempts=1,
            retry_base_delay=0.0,
            sleep=lambda _s: None,
            client=sdk,
            tracer=captured.append,
        )
        client.call(
            system_prompt="sys",
            user_message="hi",
            output_model=_SampleOutput,
            model="claude-sonnet-4-6",
        )
        assert len(captured) == 1
        record = captured[0]
        assert record["request"]["model"] == "claude-sonnet-4-6"
        assert record["request"]["messages"] == [{"role": "user", "content": "hi"}]
        assert record["response"] == {"content": [{"type": "tool_use"}]}
        assert record["output"] == {"label": "ok", "confidence": 0.9}
        assert record["output_model"] == "_SampleOutput"
        assert record["duration_seconds"] >= 0

    def test_tracer_records_parse_failed_on_schema_violation(self) -> None:
        # The call is already billed when validation fails, so the trace must survive it
        # (F-002).
        sdk = MagicMock()
        response = _tool_use_message({"label": "ok"})
        response.model_dump.return_value = {"content": [{"type": "tool_use"}]}
        sdk.messages.create.return_value = response
        captured: list[dict[str, Any]] = []
        client = LLMClient(
            api_key="test",
            max_attempts=1,
            retry_base_delay=0.0,
            sleep=lambda _s: None,
            client=sdk,
            tracer=captured.append,
        )
        with pytest.raises(LLMError):
            client.call(
                system_prompt="s",
                user_message="u",
                output_model=_SampleOutput,
                model="claude-sonnet-4-6",
            )
        assert len(captured) == 1
        assert captured[0]["parse_failed"] is True
        assert captured[0]["output_model"] == "_SampleOutput"

    def test_a_failed_call_is_traced_as_llm_error(self) -> None:
        """This assertion used to read `captured == []`, and that was the bug.

        `tracing.py` has listed `llm_error` since it was written and nothing emitted one, so a
        transport failure left no record of a call the provider may have billed (F-002's shape).
        """
        sdk = MagicMock()
        sdk.messages.create.side_effect = anthropic.APIConnectionError(request=MagicMock())
        captured: list[dict[str, Any]] = []
        client = LLMClient(
            api_key="test",
            max_attempts=1,
            retry_base_delay=0.0,
            sleep=lambda _s: None,
            client=sdk,
            tracer=captured.append,
        )
        with pytest.raises(LLMError):
            client.call(
                system_prompt="s",
                user_message="u",
                output_model=_SampleOutput,
                model="claude-sonnet-4-6",
            )
        assert len(captured) == 1
        assert "APIConnectionError" in captured[0]["error"]
        assert captured[0]["terminal"] is True


class TestUnexpectedApiErrorsAreWrapped:
    """Every anthropic error must leave this client as an LLMError.

    An uncaught SDK exception takes out an already-graded run at the briefing step,
    recorded as a crash.
    """

    @staticmethod
    def _validation_error() -> anthropic.APIResponseValidationError:
        # The live one: a 200 whose body the SDK cannot validate, so neither error class.
        return anthropic.APIResponseValidationError(
            response=httpx.Response(200, request=httpx.Request("POST", "http://x")),
            body=None,
        )

    def _call(self, sdk: MagicMock) -> None:
        _client(sdk).call(
            system_prompt="s",
            user_message="u",
            output_model=_SampleOutput,
            model="claude-test",
        )

    def test_a_response_validation_error_becomes_an_llm_error(self) -> None:
        sdk = MagicMock()
        sdk.messages.create.side_effect = self._validation_error()
        with pytest.raises(LLMError, match="APIResponseValidationError"):
            self._call(sdk)

    def test_the_original_is_kept_as_the_cause(self) -> None:
        err = self._validation_error()
        sdk = MagicMock()
        sdk.messages.create.side_effect = err
        with pytest.raises(LLMError) as excinfo:
            self._call(sdk)
        assert excinfo.value.__cause__ is err

    def test_a_wrapped_error_is_not_retried(self) -> None:
        # It is not transient: retrying burns money for the same answer.
        sdk = MagicMock()
        sdk.messages.create.side_effect = self._validation_error()
        with pytest.raises(LLMError):
            self._call(sdk)
        assert sdk.messages.create.call_count == 1

    def test_a_transient_status_error_is_still_retried(self) -> None:
        # The catch-all must not swallow the retry paths above it.
        sdk = MagicMock()
        sdk.messages.create.side_effect = anthropic.APIStatusError(
            "rate limited",
            response=httpx.Response(429, request=httpx.Request("POST", "http://x")),
            body=None,
        )
        with pytest.raises(LLMError, match="transport failure after"):
            self._call(sdk)
        assert sdk.messages.create.call_count == 3


class TestFailuresAreTraced:
    """A billed call that did not return must leave a record.

    `llm_error` was in tracing.py's kind set from the start and nothing wrote one, so an
    exhausted 429 left a gap.
    """

    @staticmethod
    def _traced(sdk: MagicMock) -> list[dict[str, Any]]:
        traces: list[dict[str, Any]] = []
        client = LLMClient(
            api_key="test",
            max_attempts=3,
            retry_base_delay=0.0,
            sleep=lambda _s: None,
            client=sdk,
            tracer=traces.append,
        )
        with pytest.raises(LLMError):
            client.call(
                system_prompt="s",
                user_message="u",
                output_model=_SampleOutput,
                model="claude-test",
            )
        return traces

    def test_every_retry_of_a_429_is_recorded(self) -> None:
        sdk = MagicMock()
        sdk.messages.create.side_effect = anthropic.APIStatusError(
            "rate limited",
            response=httpx.Response(429, request=httpx.Request("POST", "http://x")),
            body=None,
        )
        traces = self._traced(sdk)
        # Three attempts, three records — not one summary at the end. Each
        # attempt is a separate request the provider may have billed.
        assert len(traces) == 3
        assert all("error" in t for t in traces)
        assert [t["attempt"] for t in traces] == [0, 1, 2]
        assert [t["terminal"] for t in traces] == [False, False, True]

    def test_a_connection_failure_is_recorded(self) -> None:
        sdk = MagicMock()
        sdk.messages.create.side_effect = anthropic.APIConnectionError(
            request=httpx.Request("POST", "http://x")
        )
        traces = self._traced(sdk)
        assert len(traces) == 3
        assert "APIConnectionError" in traces[0]["error"]

    def test_a_non_retryable_status_is_recorded_once(self) -> None:
        sdk = MagicMock()
        sdk.messages.create.side_effect = anthropic.APIStatusError(
            "bad request",
            response=httpx.Response(400, request=httpx.Request("POST", "http://x")),
            body=None,
        )
        traces = self._traced(sdk)
        assert len(traces) == 1
        assert traces[0]["terminal"] is True

    def test_the_catch_all_arm_is_recorded(self) -> None:
        sdk = MagicMock()
        sdk.messages.create.side_effect = anthropic.APIResponseValidationError(
            response=httpx.Response(200, request=httpx.Request("POST", "http://x")),
            body=None,
        )
        traces = self._traced(sdk)
        assert len(traces) == 1
        assert "APIResponseValidationError" in traces[0]["error"]

    def test_the_request_body_is_kept_so_the_call_can_be_costed(self) -> None:
        sdk = MagicMock()
        sdk.messages.create.side_effect = anthropic.APIConnectionError(
            request=httpx.Request("POST", "http://x")
        )
        traces = self._traced(sdk)
        assert traces[0]["request"]["model"] == "claude-test"
        assert "duration_seconds" in traces[0]

    def test_a_successful_call_still_traces_as_a_success(self) -> None:
        sdk = MagicMock()
        sdk.messages.create.return_value = _tool_use_message({"label": "x", "confidence": 0.5})
        traces: list[dict[str, Any]] = []
        LLMClient(
            api_key="test",
            retry_base_delay=0.0,
            sleep=lambda _s: None,
            client=sdk,
            tracer=traces.append,
        ).call(system_prompt="s", user_message="u", output_model=_SampleOutput, model="claude-test")
        assert len(traces) == 1
        assert "error" not in traces[0]


class TestBilledWorkLeavesTheClient:
    """Every non-happy path must hand the caller what it billed (ADR 0015).

    A logical call can bill up to ``max_attempts`` times, and in full for an unparseable
    response; none of that reached the ledger, under-counting by up to 3x on retries.
    """

    def _connection_error(self) -> anthropic.APIConnectionError:
        return anthropic.APIConnectionError(request=MagicMock())

    def _status_error(self, code: int) -> anthropic.APIStatusError:
        response = MagicMock()
        response.status_code = code
        return anthropic.APIStatusError(message="fail", response=response, body=None)

    def _call(self, sdk: MagicMock, max_tokens: int = 4096) -> Any:
        return _client(sdk).call(
            system_prompt="s",
            user_message="u",
            output_model=_SampleOutput,
            model="claude-sonnet-4-6",
            max_tokens=max_tokens,
        )

    def test_two_failures_then_success_charges_three_attempts_not_one(self) -> None:
        sdk = MagicMock()
        sdk.messages.create.side_effect = [
            self._connection_error(),
            self._status_error(503),
            _tool_use_message({"label": "ok", "confidence": 1.0}),
        ]
        result = self._call(sdk, max_tokens=1000)
        assert sdk.messages.create.call_count == 3
        # The returned attempt's own counters, unchanged...
        assert result.output_tokens == 50
        # ...plus a conservative charge for the two that were billed and
        # thrown away. Before this, those two were free.
        assert result.discarded_attempts == 2
        assert result.discarded_output_tokens == 2000

    def test_a_first_attempt_success_charges_nothing_extra(self) -> None:
        sdk = MagicMock()
        sdk.messages.create.return_value = _tool_use_message({"label": "ok", "confidence": 1.0})
        result = self._call(sdk)
        assert result.discarded_attempts == 0
        assert result.discarded_output_tokens == 0

    def test_a_billed_but_unparseable_response_carries_its_usage(self) -> None:
        """max_tokens truncation: fully billed, nothing to parse."""
        response = _tool_use_message({"label": "ok", "confidence": 1.0}, output_tokens=4096)
        response.content = []
        response.stop_reason = "max_tokens"
        sdk = MagicMock()
        sdk.messages.create.return_value = response
        with pytest.raises(LLMError, match="no record_output tool_use") as caught:
            self._call(sdk)
        usage = caught.value.usage
        assert usage is not None
        assert usage.input_tokens == 100
        assert usage.output_tokens == 4096

    def test_a_schema_validation_failure_carries_its_usage(self) -> None:
        sdk = MagicMock()
        sdk.messages.create.return_value = _tool_use_message({"label": "ok"})
        with pytest.raises(LLMError, match="failed schema validation") as caught:
            self._call(sdk)
        assert caught.value.usage is not None
        assert caught.value.usage.output_tokens == 50

    def test_an_exhausted_retry_loop_charges_every_attempt(self) -> None:
        sdk = MagicMock()
        sdk.messages.create.side_effect = self._status_error(503)
        with pytest.raises(LLMError, match="transport failure after 3 attempts") as caught:
            self._call(sdk, max_tokens=1000)
        usage = caught.value.usage
        assert usage is not None
        assert usage.discarded_attempts == 3
        assert usage.discarded_output_tokens == 3000

    def test_a_client_error_charges_only_the_attempts_before_it(self) -> None:
        """A rejected request never reached the model, so it is not billed."""
        sdk = MagicMock()
        sdk.messages.create.side_effect = [self._status_error(503), self._status_error(400)]
        with pytest.raises(LLMError, match="LLM API error 400") as caught:
            self._call(sdk, max_tokens=1000)
        usage = caught.value.usage
        assert usage is not None
        assert usage.discarded_attempts == 1

    def test_an_unexpected_api_error_charges_the_attempt_it_billed(self) -> None:
        """APIResponseValidationError is a 200 the SDK could not validate."""
        sdk = MagicMock()
        sdk.messages.create.side_effect = anthropic.APIResponseValidationError(
            response=httpx.Response(200, request=httpx.Request("POST", "http://x")),
            body=None,
        )
        with pytest.raises(LLMError, match="APIResponseValidationError") as caught:
            self._call(sdk, max_tokens=1000)
        usage = caught.value.usage
        assert usage is not None
        assert usage.discarded_attempts == 1


class TestPreflightWrapsEverySdkError:
    """The point of preflight is one labeled line, never a traceback.

    ``evals/runner.py`` catches ``LLMError`` for exit 3; an SDK error that escapes skips
    that handler.
    """

    def test_an_unexpected_api_error_becomes_an_llm_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        err = anthropic.APIResponseValidationError(
            response=httpx.Response(200, request=httpx.Request("GET", "http://x")),
            body=None,
        )
        sdk = MagicMock()
        sdk.models.list.side_effect = err
        monkeypatch.setattr(anthropic, "Anthropic", lambda **_kw: sdk)
        with pytest.raises(LLMError, match="auth preflight failed.*APIResponseValidationError"):
            preflight_auth("sk-test")

    def test_a_status_error_still_reports_its_code(self, monkeypatch: pytest.MonkeyPatch) -> None:
        response = MagicMock()
        response.status_code = 401
        sdk = MagicMock()
        sdk.models.list.side_effect = anthropic.APIStatusError(
            message="bad key", response=response, body=None
        )
        monkeypatch.setattr(anthropic, "Anthropic", lambda **_kw: sdk)
        with pytest.raises(LLMError, match="auth preflight failed: HTTP 401"):
            preflight_auth("sk-test")


class TestTheClientTimesItsOwnCalls:
    """WO-R3-260: ``LLMResult.elapsed_ms`` — the last plan-02 field left null.

    Nothing timed a call: the only duration was a per-attempt ``duration_seconds`` in a
    trace file the offline suite never writes. The measurement is of the LOGICAL call —
    every retry and backoff sleep — because that is the latency the loop waited out.
    """

    def _clock(self, *readings: float) -> Callable[[], float]:
        """A clock that returns each reading once, then holds the last.

        Holding rather than raising: the number of reads per call is an implementation detail.
        """
        ticks = list(readings)

        def read() -> float:
            return ticks.pop(0) if len(ticks) > 1 else ticks[0]

        return read

    def test_a_call_reports_how_long_it_took(self) -> None:
        sdk = MagicMock()
        sdk.messages.create.return_value = _tool_use_message({"label": "ok", "confidence": 0.9})
        client = LLMClient(api_key="test", client=sdk, clock=self._clock(10.0, 10.25, 11.5))
        result = client.call(
            system_prompt="s",
            user_message="u",
            output_model=_SampleOutput,
            model="claude-sonnet-4-6",
        )
        # 10.0 at entry, 11.5 when the parsed result is built: 1.5 s.
        assert result.elapsed_ms == 1500

    def test_the_retried_attempts_and_the_backoff_are_inside_the_number(self) -> None:
        response = MagicMock()
        response.status_code = 503
        response.headers = {}
        err = anthropic.APIStatusError(message="upstream", response=response, body=None)
        sdk = MagicMock()
        sdk.messages.create.side_effect = [
            err,
            _tool_use_message({"label": "ok", "confidence": 1.0}),
        ]
        client = LLMClient(
            api_key="test",
            max_attempts=3,
            retry_base_delay=0.0,
            sleep=lambda _s: None,
            client=sdk,
            # entry 0.0; first attempt's error trace at 4.0; second attempt
            # starts at 9.0 and the result is built at 12.0.
            clock=self._clock(0.0, 4.0, 9.0, 12.0),
        )
        result = client.call(
            system_prompt="s",
            user_message="u",
            output_model=_SampleOutput,
            model="claude-sonnet-4-6",
        )
        assert result.elapsed_ms == 12_000

    def test_a_sub_millisecond_call_reports_zero_not_none(self) -> None:
        """``0`` is a measurement; ``None`` is the absence of one. A client
        that times itself must never say "not measured"."""
        sdk = MagicMock()
        sdk.messages.create.return_value = _tool_use_message({"label": "ok", "confidence": 0.9})
        client = LLMClient(api_key="test", client=sdk, clock=self._clock(1.0, 1.0, 1.0))
        result = client.call(
            system_prompt="s",
            user_message="u",
            output_model=_SampleOutput,
            model="claude-sonnet-4-6",
        )
        assert result.elapsed_ms == 0

    def test_a_client_that_does_not_time_itself_says_not_measured(self) -> None:
        """The canned client reports ``None``, and that is the honest answer:
        a fake's duration is a property of the test machine, not of a call."""
        result = CannedLLMClient([{"label": "ok", "confidence": 0.5}]).call(
            system_prompt="s",
            user_message="u",
            output_model=_SampleOutput,
            model="claude-sonnet-4-6",
        )
        assert result.elapsed_ms is None
