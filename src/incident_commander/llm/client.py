"""Anthropic-backed LLM client with typed structured outputs.

Structured output is one forced tool whose schema is the caller's Pydantic model.
The system prompt is cache-controlled; usage splits cache_creation from
cache_read.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final, Protocol

import anthropic
import httpx
from anthropic.types import Message
from pydantic import BaseModel, ValidationError

_STRUCTURED_TOOL_NAME: Final[str] = "record_output"

# Explicit SDK bounds (C-07): the outer loop owns retry policy, so SDK retries are off.
# The 120s read bound is what makes a stalled call reach the wall meter (ADR 0015).
_SDK_MAX_RETRIES: Final[int] = 0
_SDK_TIMEOUT: Final[httpx.Timeout] = httpx.Timeout(120.0, connect=5.0)
# Preflight is one models.list call — it should fail on a dead network in
# seconds, not minutes.
_PREFLIGHT_TIMEOUT: Final[httpx.Timeout] = httpx.Timeout(30.0, connect=5.0)
# Cap the honored Retry-After: an hour-long server-suggested pause would
# silently stall the sync state machine.
_MAX_RETRY_AFTER_SECONDS: Final[float] = 60.0


#: Model ids that **reject** ``temperature`` with a 400, so the sampled inference
#: strategy (WP-5.3) would 400 under a newer pin. DECLARED, not a refusal: it is
#: the tripwire in ``tests/unit/test_best_of_n_sampled.py`` — no id in
#: ``MODEL_PRICING`` may appear here.
SAMPLING_REJECTED_MODELS: Final[frozenset[str]] = frozenset(
    {
        "claude-fable-5",
        "claude-fable-5-1",
        "claude-mythos-5",
        "claude-mythos-5-1",
        "claude-opus-5",
        "claude-opus-4-8",
        "claude-opus-4-7",
        "claude-sonnet-5",
    }
)


def elapsed_ms_of(seconds: float) -> int:
    """Whole milliseconds, never negative. ``0`` is a measurement, not a gap.

    One definition for its two callers (here and ``agent/accounting.py``).
    """
    return max(round(seconds * 1000), 0)


@dataclass(frozen=True, kw_only=True)
class LLMUsage:
    """What one logical ``call`` billed — including work that never came back.

    ADR 0015: the meter may over-report, never under-report. ``discarded_attempts``
    counts billed attempts whose response was thrown away; they carry no usage
    block, so they are charged ``max_tokens`` at the output rate.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    discarded_attempts: int = 0
    discarded_max_tokens: int = 0

    @property
    def discarded_output_tokens(self) -> int:
        """Conservative token charge for the billed-then-discarded attempts."""
        return self.discarded_attempts * self.discarded_max_tokens

    def with_output[T: BaseModel](
        self, output: T, stop_reason: str, record_id: str = "", elapsed_ms: int | None = None
    ) -> LLMResult[T]:
        """Promote a usage record to a full result once parsing has succeeded."""
        return LLMResult(
            output=output,
            stop_reason=stop_reason,
            record_id=record_id,
            elapsed_ms=elapsed_ms,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cache_creation_tokens=self.cache_creation_tokens,
            cache_read_tokens=self.cache_read_tokens,
            discarded_attempts=self.discarded_attempts,
            discarded_max_tokens=self.discarded_max_tokens,
        )


class LLMError(RuntimeError):
    """The response could not be parsed into the caller's output model.

    ``usage`` carries what the call already billed, so callers can charge it with
    ``accounting.accrue_llm_error`` rather than lose the spend.
    """

    def __init__(
        self,
        message: str,
        *,
        usage: LLMUsage | None = None,
        record_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.usage = usage
        #: Trace-record id of the failed call; a repair re-ask carries it as
        #: ``repair_of`` (ADR 0035).
        self.record_id = record_id


class LLMOutputError(LLMError):
    """The call RETURNED and was billed; its payload did not fit the model.

    Split from ``LLMError``: a transport failure is the client's business, while
    an output-shape failure gets one bounded re-ask (ADR 0035). Still a subclass,
    so nothing that escalated stops escalating.
    """


@dataclass(frozen=True, kw_only=True)
class LLMResult[T: BaseModel](LLMUsage):
    """Parsed output + usage accounting from a single LLM call."""

    output: T
    stop_reason: str
    #: Trace-record id this call was written under, or "" when untraced.
    record_id: str = ""
    #: Wall time of the whole logical call, retries and backoff sleeps included.
    #: ``None`` means **not measured** and only a fake can report it;
    #: ``LLMClient`` always fills it.
    elapsed_ms: int | None = None


class LLMClientProtocol(Protocol):
    """Structural type for anything the agent can use as an LLM.

    ``repair_of`` is trace correlation only (ADR 0035) and changes nothing sent to
    the model. ``temperature`` (WP-5.3) defaults to ``None``, which sends no
    temperature field at all — see ``SAMPLING_REJECTED_MODELS``.
    """

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
    ) -> LLMResult[T]: ...


class LLMClient:
    """Real Anthropic client. Tests use ``CannedLLMClient`` from ``llm.fakes``.

    ``tracer`` receives one dict per call, written as JSONL under ``EVAL_TRACE_DIR``.
    """

    def __init__(
        self,
        api_key: str,
        max_attempts: int = 3,
        retry_base_delay: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
        client: anthropic.Anthropic | None = None,
        tracer: Callable[[dict[str, Any]], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
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
        # Injectable like ``sleep``: a real monotonic duration cannot be
        # asserted on, so ``elapsed_ms`` could only be pinned to ">= 0".
        self._clock = clock

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
        """Make one structured-output call and return the parsed output with what it billed.

        One forced tool carries the output schema; transient failures are retried.
        ``elapsed_ms`` spans the whole retry loop, not the attempt that returned.
        """
        call_started = self._clock()
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
        # Added only when asked for, never as a default: the request body is what the
        # tracer writes, and newer models reject the field (``SAMPLING_REJECTED_MODELS``).
        if temperature is not None:
            request_body["temperature"] = temperature

        def _trace_error(err: Exception, attempt: int, *, terminal: bool) -> None:
            """Record a call that was billed (or attempted) and did not return.

            Without it an exhausted 429 or dropped connection left a silent gap
            exactly where billed work happened.
            """
            if self._tracer is None:
                return
            self._tracer(
                {
                    "request": request_body,
                    "error": f"{type(err).__name__}: {err}",
                    "attempt": attempt,
                    "terminal": terminal,
                    "output_model": output_model.__name__,
                    "duration_seconds": self._clock() - started,
                    **_repair_keys(record_id, repair_of),
                }
            )

        last_exc: Exception | None = None
        retry_after: float | None = None
        # One id per ATTEMPT: ``repair_of`` has to name the exact record
        # whose payload failed.
        record_id = ""
        for attempt in range(self._max_attempts):
            started = self._clock()
            record_id = uuid.uuid4().hex[:12]
            # `attempt` is also the count already billed and discarded; every
            # exit carries it so the whole logical call is charged.
            discarded = LLMUsage(discarded_attempts=attempt, discarded_max_tokens=max_tokens)
            try:
                response = self._client.messages.create(**request_body)
            except anthropic.APIConnectionError as err:
                _trace_error(err, attempt, terminal=attempt == self._max_attempts - 1)
                last_exc = err
                retry_after = None
            except anthropic.APIStatusError as err:
                if err.status_code != 429 and err.status_code < 500:
                    # Client-side (bad request, auth): retrying can't help, so wrap
                    # it. Charges `attempt`: a rejected request is known unbilled.
                    _trace_error(err, attempt, terminal=True)
                    raise LLMError(
                        f"LLM API error {err.status_code}: {err}",
                        usage=discarded,
                        record_id=record_id,
                    ) from err
                # 429 + 5xx are transient: retry with backoff, honoring a
                # numeric Retry-After when the platform sends one.
                _trace_error(err, attempt, terminal=attempt == self._max_attempts - 1)
                last_exc = err
                retry_after = _retry_after_seconds(err)
            except anthropic.APIError as err:
                _trace_error(err, attempt, terminal=True)
                # Load-bearing catch-all: an APIResponseValidationError (a 200 the SDK
                # could not validate) escapes every `except LLMError`, and it billed.
                raise LLMError(
                    f"LLM API error: {type(err).__name__}: {err}",
                    usage=LLMUsage(discarded_attempts=attempt + 1, discarded_max_tokens=max_tokens),
                    record_id=record_id,
                ) from err
            else:
                # Trace BEFORE parsing: the call is already billed, and tracing
                # after the parse left unparseable calls with no record (F-002).
                trace: dict[str, Any] | None = None
                if self._tracer is not None:
                    trace = {
                        "request": request_body,
                        "response": response.model_dump(mode="json"),
                        "output_model": output_model.__name__,
                        "duration_seconds": self._clock() - started,
                        **_repair_keys(record_id, repair_of),
                    }
                try:
                    result = self._parse(
                        response,
                        output_model,
                        _usage_of(response, discarded),
                        record_id,
                        elapsed_ms_of(self._clock() - call_started),
                    )
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
        # Every attempt was discarded, so every attempt is charged.
        raise LLMError(
            f"LLM transport failure after {self._max_attempts} attempts: "
            f"{type(last_exc).__name__}: {last_exc}",
            usage=LLMUsage(discarded_attempts=self._max_attempts, discarded_max_tokens=max_tokens),
            record_id=record_id,
        ) from last_exc

    def _parse[T: BaseModel](
        self,
        response: Message,
        output_model: type[T],
        usage: LLMUsage,
        record_id: str = "",
        elapsed_ms: int | None = None,
    ) -> LLMResult[T]:
        """Pull the forced tool-use block out of the response and validate it."""
        for block in response.content:
            if block.type == "tool_use" and block.name == _STRUCTURED_TOOL_NAME:
                try:
                    output = output_model.model_validate(block.input)
                except ValidationError as err:
                    # ADR 0007: only the domain exception crosses this boundary.
                    # A raw ValidationError also skipped the trace below (F-002).
                    raise LLMOutputError(
                        f"output failed schema validation for {output_model.__name__}: {err}",
                        usage=usage,
                        record_id=record_id,
                    ) from err
                return usage.with_output(
                    output, response.stop_reason or "unknown", record_id, elapsed_ms
                )
        # Billed and unreturned — the max_tokens-truncation case. Raising without
        # `usage` charged the client's most expensive failure the least.
        raise LLMOutputError(
            f"no {_STRUCTURED_TOOL_NAME} tool_use in response; stop_reason={response.stop_reason}",
            usage=usage,
            record_id=record_id,
        )


def _repair_keys(record_id: str, repair_of: str | None) -> dict[str, Any]:
    """Identity keys for one trace record: its own id, and what it repairs.

    ``repair_of`` is omitted rather than ``null`` on an ordinary call.
    """
    keys: dict[str, Any] = {"record_id": record_id}
    if repair_of is not None:
        keys["repair_of"] = repair_of
    return keys


def _usage_of(response: Message, discarded: LLMUsage) -> LLMUsage:
    """This response's own counters, plus whatever earlier attempts discarded."""
    usage = response.usage
    return LLMUsage(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_creation_tokens=usage.cache_creation_input_tokens or 0,
        cache_read_tokens=usage.cache_read_input_tokens or 0,
        discarded_attempts=discarded.discarded_attempts,
        discarded_max_tokens=discarded.discarded_max_tokens,
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

    An expired key once cost 24 identical crash rows before anything ran.
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
    except anthropic.APIError as err:
        # Catch-all, like `call`: an uncaught APIResponseValidationError crashed the
        # eval runner with a traceback instead of one labeled line.
        raise LLMError(f"auth preflight failed: {type(err).__name__}: {err}") from err
