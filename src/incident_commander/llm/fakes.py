"""Structural ``LLMClientProtocol`` fakes for tests and offline eval runs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from incident_commander.llm.client import LLMError, LLMResult


@dataclass(frozen=True, slots=True)
class CannedUsage:
    """Per-call token counts a ``CannedLLMClient`` reports.

    Defaults are all-zero so every existing canned scenario stays
    byte-identical. Tests that exercise budget accrual — cache-token
    volume, the USD meter — pass a non-zero instance; a real response's
    input volume lands mostly on the cache counters (``client.py`` caches
    the system prompt), so zero-usage fakes cannot reach that path.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0


class CannedLLMClient:
    """Plays back a fixed sequence of output payloads.

    Each ``call`` pops the next payload, validates it against the caller's
    ``output_model``, and wraps it in an ``LLMResult`` reporting ``usage``
    (zero token counts by default).
    Runs out → ``LLMError``. Records each call for post-run introspection.
    """

    def __init__(self, outputs: list[dict[str, Any]], usage: CannedUsage | None = None) -> None:
        self._outputs = list(outputs)
        self._usage = usage or CannedUsage()
        self._index = 0
        self.calls: list[tuple[str, str]] = []

    @property
    def has_remaining(self) -> bool:
        return self._index < len(self._outputs)

    def call[T: BaseModel](
        self,
        system_prompt: str,
        user_message: str,
        output_model: type[T],
        model: str,
        max_tokens: int = 4096,
    ) -> LLMResult[T]:
        self.calls.append((system_prompt, user_message))
        if self._index >= len(self._outputs):
            raise LLMError("no more canned responses")
        payload = self._outputs[self._index]
        self._index += 1
        return LLMResult(
            output=output_model.model_validate(payload),
            input_tokens=self._usage.input_tokens,
            output_tokens=self._usage.output_tokens,
            cache_creation_tokens=self._usage.cache_creation_tokens,
            cache_read_tokens=self._usage.cache_read_tokens,
            stop_reason="canned",
        )
