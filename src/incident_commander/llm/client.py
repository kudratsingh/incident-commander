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
import httpx
from anthropic.types import Message
from pydantic import BaseModel

_STRUCTURED_TOOL_NAME: Final[str] = "record_output"

# Explicit SDK bounds (C-07): the outer loop below owns retry policy, so the
# SDK's own retries are off — SDK defaults (max_retries=2, 600s read timeout)
# would otherwise layer under the 3-attempt loop for up to 9 HTTP requests
# x 600s per logical call. The 120s read bound leaves room for a slow
# structured generation at max_tokens=4096 (60s is too tight for that);
# worst case per logical call is 3 attempts x ~120s ~= 6 min, well inside
# budget_max_seconds=1800. The bound matters because wall_seconds_used is
# never accrued today (B-01), so nothing else can interrupt a stalled call.
_SDK_MAX_RETRIES: Final[int] = 0
_SDK_TIMEOUT: Final[httpx.Timeout] = httpx.Timeout(120.0, connect=5.0)
# Preflight is one models.list call — it should fail on a dead network in
# seconds, not minutes.
_PREFLIGHT_TIMEOUT: Final[httpx.Timeout] = httpx.Timeout(30.0, connect=5.0)
# Cap the honored Retry-After: an hour-long server-suggested pause would
# silently stall the sync state machine.
_MAX_RETRY_AFTER_SECONDS: Final[float] = 60.0


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
        max_tokens: int = 4096,
    ) -> LLMResult[T]: ...


class LLMClient:
    """Real Anthropic client. Tests use ``CannedLLMClient`` from ``llm.fakes``.

    Pass ``tracer`` to capture per-call payloads for offline inspection —
    used by the eval runner to write JSONL trace files under
    ``EVAL_TRACE_DIR``. The tracer receives a dict with ``request``,
    ``response`` (raw Anthropic Message), ``output`` (parsed dict), and
    ``duration_seconds`` after every successful call.
    """

    def __init__(
        self,
        api_key: str,
        max_attempts: int = 3,
        retry_base_delay: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
        client: anthropic.Anthropic | None = None,
        tracer: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._client = client or anthropic.Anthropic(
            api_key=api_key,
            timeout=_SDK_TIMEOUT,
            max_retries=_SDK_MAX_RETRIES,
        )
        self._max_attempts = max_attempts
        self._retry_base_delay = retry_base_delay
        self._sleep = sleep
        self._tracer = tracer

    def call[T: BaseModel](
        self,
        system_prompt: str,
        user_message: str,
        output_model: type[T],
        model: str,
        max_tokens: int = 4096,
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
        retry_after: float | None = None
        for attempt in range(self._max_attempts):
            started = time.monotonic()
            try:
                response = self._client.messages.create(**request_body)
            except anthropic.APIConnectionError as err:
                last_exc = err
                retry_after = None
            except anthropic.APIStatusError as err:
                if err.status_code != 429 and err.status_code < 500:
                    # Client-side error (bad request, auth). Retrying can't
                    # help; wrap so the transition escalates-with-reason
                    # instead of the scenario crashing on a raw SDK error.
                    raise LLMError(f"LLM API error {err.status_code}: {err}") from err
                # 429 + 5xx are transient: retry with backoff, honoring a
                # numeric Retry-After when the platform sends one.
                last_exc = err
                retry_after = _retry_after_seconds(err)
            else:
                # Trace BEFORE parsing: the API call is already billed at
                # this point, and a response we fail to parse (no
                # record_output block, max_tokens truncation) is exactly
                # the call a cost or debugging audit most wants to see.
                # Tracing after the parse meant those spent money and left
                # no record (study/findings.md F-002).
                trace: dict[str, Any] | None = None
                if self._tracer is not None:
                    trace = {
                        "request": request_body,
                        "response": response.model_dump(mode="json"),
                        "output_model": output_model.__name__,
                        "duration_seconds": time.monotonic() - started,
                    }
                try:
                    result = self._parse(response, output_model)
                except LLMError:
                    if trace is not None:
                        self._tracer(dict(trace, parse_failed=True))  # type: ignore[misc]
                    raise
                if trace is not None:
                    self._tracer(  # type: ignore[misc]
                        dict(trace, output=result.output.model_dump(mode="json"))
                    )
                return result
            if attempt < self._max_attempts - 1:
                delay = self._retry_base_delay * (2**attempt)
                if retry_after is not None:
                    delay = max(delay, min(retry_after, _MAX_RETRY_AFTER_SECONDS))
                self._sleep(delay)
        assert last_exc is not None
        raise LLMError(
            f"LLM transport failure after {self._max_attempts} attempts: "
            f"{type(last_exc).__name__}: {last_exc}"
        ) from last_exc

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


def _retry_after_seconds(err: anthropic.APIStatusError) -> float | None:
    """Numeric Retry-After off a 429/5xx response, when present and parseable."""
    try:
        value = err.response.headers.get("retry-after")
        return float(value) if value is not None else None
    except Exception:
        return None


def preflight_auth(api_key: str) -> None:
    """One cheap authenticated call; raises ``LLMError`` if the key is bad.

    The 2026-08-03 campaign discovered an expired key as 24 identical
    per-scenario crash rows across a whole smoke pass. models.list is
    free, instant, and fails with the same 401 the first real call
    would — so the runner can turn that failure mode into one labeled
    line before anything runs.
    """
    client = anthropic.Anthropic(
        api_key=api_key,
        timeout=_PREFLIGHT_TIMEOUT,
        max_retries=_SDK_MAX_RETRIES,
    )
    try:
        client.models.list(limit=1)
    except anthropic.APIStatusError as err:
        raise LLMError(f"auth preflight failed: HTTP {err.status_code}: {err}") from err
    except anthropic.APIConnectionError as err:
        raise LLMError(f"auth preflight failed: connection error: {err}") from err
