from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import anthropic
import pytest
from pydantic import BaseModel, ValidationError

from incident_commander.llm.client import LLMClient, LLMError


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
        with pytest.raises(anthropic.APIStatusError):
            _client(sdk).call(
                system_prompt="s",
                user_message="u",
                output_model=_SampleOutput,
                model="claude-sonnet-4-6",
            )
        assert sdk.messages.create.call_count == 1

    def test_gives_up_after_max_attempts_on_persistent_5xx(self) -> None:
        sdk = MagicMock()
        sdk.messages.create.side_effect = self._status_error(500)
        with pytest.raises(anthropic.APIStatusError):
            _client(sdk, max_attempts=2).call(
                system_prompt="s",
                user_message="u",
                output_model=_SampleOutput,
                model="claude-sonnet-4-6",
            )
        assert sdk.messages.create.call_count == 2
