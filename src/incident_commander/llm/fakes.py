"""Structural ``LLMClientProtocol`` fakes for tests and offline eval runs."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from incident_commander.llm.client import LLMError, LLMResult


class CannedLLMClient:
    """Plays back a fixed sequence of output payloads.

    Each ``call`` pops the next payload, validates it against the caller's
    ``output_model``, and wraps it in an ``LLMResult`` with zero token counts.
    Runs out → ``LLMError``. Records each call for post-run introspection.
    """

    def __init__(self, outputs: list[dict[str, Any]]) -> None:
        self._outputs = list(outputs)
        self._index = 0
        self.calls: list[tuple[str, str]] = []

    def call[T: BaseModel](
        self,
        system_prompt: str,
        user_message: str,
        output_model: type[T],
        model: str,
        max_tokens: int = 2048,
    ) -> LLMResult[T]:
        self.calls.append((system_prompt, user_message))
        if self._index >= len(self._outputs):
            raise LLMError("no more canned responses")
        payload = self._outputs[self._index]
        self._index += 1
        return LLMResult(
            output=output_model.model_validate(payload),
            input_tokens=0,
            output_tokens=0,
            cache_creation_tokens=0,
            cache_read_tokens=0,
            stop_reason="canned",
        )
