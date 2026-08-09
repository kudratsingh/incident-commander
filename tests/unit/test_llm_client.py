from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import anthropic
import httpx
import pytest
from pydantic import BaseModel, ValidationError

from incident_commander.llm.client import LLMClient, LLMError, preflight_auth


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

    def test_schema_violation_bubbles_as_validation_error(self) -> None:
        sdk = MagicMock()
        sdk.messages.create.return_value = _tool_use_message({"label": "ok"})
        with pytest.raises(ValidationError):
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

    def test_tracer_not_called_on_error(self) -> None:
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
        assert captured == []
