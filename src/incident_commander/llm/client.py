"""Anthropic-backed LLM client with typed structured outputs.

Structured outputs use the tool-use pattern: we advertise a single tool whose
input schema is the caller's Pydantic model, and force the model to call it.
That gives us JSON-schema-validated output for free — the SDK rejects any
tool-call payload that violates the schema.

Prompt caching is applied to the system prompt so repeat calls with the same
system content pay the cheap cache-read rate. Reported usage separates
cache_creation from cache_read so we can graph hit-rate over time.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final, Protocol

import anthropic
from anthropic.types import Message
from pydantic import BaseModel

_STRUCTURED_TOOL_NAME: Final[str] = "record_output"


class LLMError(RuntimeError):
    """The response could not be parsed into the caller's output model."""


@dataclass(frozen=True)
class LLMResult[T: BaseModel]:
    """Parsed output + usage accounting from a single LLM call."""

    output: T
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    stop_reason: str


class LLMClientProtocol(Protocol):
    """Structural type for anything the agent can use as an LLM."""

    def call[T: BaseModel](
        self,
        system_prompt: str,
        user_message: str,
        output_model: type[T],
        model: str,
        max_tokens: int = 2048,
    ) -> LLMResult[T]: ...


class LLMClient:
    """Real Anthropic client. Tests use ``CannedLLMClient`` from ``llm.fakes``."""

    def __init__(
        self,
        api_key: str,
        max_attempts: int = 3,
        retry_base_delay: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
        client: anthropic.Anthropic | None = None,
    ) -> None:
        self._client = client or anthropic.Anthropic(api_key=api_key)
        self._max_attempts = max_attempts
        self._retry_base_delay = retry_base_delay
        self._sleep = sleep

    def call[T: BaseModel](
        self,
        system_prompt: str,
        user_message: str,
        output_model: type[T],
        model: str,
        max_tokens: int = 2048,
    ) -> LLMResult[T]:
        request_body: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": [
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": [{"role": "user", "content": user_message}],
            "tools": [
                {
                    "name": _STRUCTURED_TOOL_NAME,
                    "description": "Record the structured output for this call.",
                    "input_schema": output_model.model_json_schema(),
                }
            ],
            "tool_choice": {"type": "tool", "name": _STRUCTURED_TOOL_NAME},
        }
        last_exc: Exception | None = None
        for attempt in range(self._max_attempts):
            try:
                response = self._client.messages.create(**request_body)
                return self._parse(response, output_model)
            except anthropic.APIConnectionError as err:
                last_exc = err
            except anthropic.APIStatusError as err:
                if err.status_code < 500:
                    raise
                last_exc = err
            if attempt < self._max_attempts - 1:
                self._sleep(self._retry_base_delay * (2**attempt))
        assert last_exc is not None
        raise last_exc

    def _parse[T: BaseModel](self, response: Message, output_model: type[T]) -> LLMResult[T]:
        for block in response.content:
            if block.type == "tool_use" and block.name == _STRUCTURED_TOOL_NAME:
                usage = response.usage
                return LLMResult(
                    output=output_model.model_validate(block.input),
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    cache_creation_tokens=usage.cache_creation_input_tokens or 0,
                    cache_read_tokens=usage.cache_read_input_tokens or 0,
                    stop_reason=response.stop_reason or "unknown",
                )
        raise LLMError(
            f"no {_STRUCTURED_TOOL_NAME} tool_use in response; stop_reason={response.stop_reason}"
        )
