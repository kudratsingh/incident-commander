"""Structural ``LLMClientProtocol`` fakes for tests and offline eval runs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from incident_commander.llm.client import LLMError, LLMResult


@dataclass(frozen=True, slots=True)
class CannedUsage:
    """Per-call token counts a ``CannedLLMClient`` reports.

    All-zero by default so existing canned scenarios stay byte-identical; budget
    accrual tests pass a non-zero instance.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0


class CannedLLMClient:
    """Plays back a fixed sequence of output payloads.

    Each ``call`` pops and validates the next payload; runs out → ``LLMError``.
    """

    def __init__(self, outputs: list[dict[str, Any]], usage: CannedUsage | None = None) -> None:
        self._outputs = list(outputs)
        self._usage = usage or CannedUsage()
        self._index = 0
        self.calls: list[tuple[str, str]] = []
        self.repair_of: list[str | None] = []
        #: The ``temperature`` each call was made with; ``None`` is "none sent".
        #: Recorded so a test can assert it was APPLIED (WP-5.3).
        self.temperatures: list[float | None] = []

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
        *,
        repair_of: str | None = None,
        temperature: float | None = None,
    ) -> LLMResult[T]:
        # ``repair_of`` and ``temperature`` have no canned equivalent; both are
        # recorded so a test can assert the call carried them (ADR 0035, WP-5.3).
        self.calls.append((system_prompt, user_message))
        self.repair_of.append(repair_of)
        self.temperatures.append(temperature)
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
