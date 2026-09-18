"""Sync JSON-RPC transport for the platform's MCP endpoint.

Transport only: no schema validation, no budget accounting. Retries network
errors, 5xx and 429, never other 4xx; every failure raises ``MCPError``.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable, Mapping
from itertools import count
from typing import Any, Final, Protocol

import httpx
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError

from incident_commander.config import Settings

_DEFAULT_TIMEOUT_SECONDS: Final[float] = 30.0
_DEFAULT_MAX_ATTEMPTS: Final[int] = 3
_DEFAULT_RETRY_BASE_DELAY: Final[float] = 1.0
# Ceiling on a server-supplied Retry-After: a hint, not an instruction.
_MAX_RETRY_AFTER_SECONDS: Final[float] = 60.0
_JSON_RPC_INTERNAL_ERROR: Final[int] = -32603


class MCPError(RuntimeError):
    """A JSON-RPC error returned by the MCP server."""

    def __init__(self, code: int, message: str, data: object | None = None) -> None:
        super().__init__(f"MCP error {code}: {message}")
        self.code = code
        self.data = data


def _error_from_member(member: object) -> MCPError:
    """Build an ``MCPError`` from a server-supplied JSON-RPC ``error`` member.

    The member crosses the trust boundary, so nothing about its shape is
    guaranteed; an uncoercible code is folded into the message, never dropped.
    """
    if not isinstance(member, Mapping):
        return MCPError(
            _JSON_RPC_INTERNAL_ERROR,
            f"non-object JSON-RPC error member ({type(member).__name__}): {member!r:.200}",
        )
    raw_code = member.get("code", _JSON_RPC_INTERNAL_ERROR)
    try:
        code = int(raw_code)
    except (TypeError, ValueError):
        code = _JSON_RPC_INTERNAL_ERROR
    message = str(member.get("message", "unknown error"))
    if code != raw_code:
        message = f"{message} (uncoercible error code {raw_code!r:.100})"
    return MCPError(code, message, member.get("data"))


class ToolResult(BaseModel):
    """Result of a ``tools/call`` invocation. Content blocks are untrusted data."""

    model_config = ConfigDict(extra="allow", frozen=True)

    content: list[dict[str, Any]] = []
    # The wire spells the flag ``isError``, fixtures ``is_error``; without the
    # alias {"isError": true} fell into extras and every escalate-on-error
    # guard stayed dead (C-02). No serialization_alias: dumps keep ``is_error``.
    is_error: bool = Field(default=False, validation_alias=AliasChoices("isError", "is_error"))


class MCPClient:
    """Thin sync JSON-RPC client for one MCP endpoint URL."""

    def __init__(
        self,
        base_url: str,
        token: str,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
        retry_base_delay: float = _DEFAULT_RETRY_BASE_DELAY,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        tracer: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        self._client = httpx.Client(
            timeout=timeout_seconds,
            transport=transport,
        )
        self._max_attempts = max_attempts
        self._retry_base_delay = retry_base_delay
        self._sleep = sleep
        self._ids = count(1)
        self._tracer = tracer

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> MCPClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def list_tools(self) -> list[dict[str, Any]]:
        """Every tool the platform advertises; an unexpected shape reads as none at all."""
        result = self._call("tools/list", {})
        tools = result.get("tools", [])
        return list(tools) if isinstance(tools, list) else []

    def call_tool(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        timeout_seconds: float | None = None,
    ) -> ToolResult:
        """Invoke one platform tool and return its result; the call is traced either way."""
        started = time.monotonic()
        args_dict = dict(arguments)
        try:
            result = self._call(
                "tools/call",
                {"name": name, "arguments": args_dict},
                timeout_seconds=timeout_seconds,
            )
            # Inside the wrapper on purpose: transitions catch MCPError and
            # nothing else, so a raw ValidationError would walk past every
            # escalate-with-reason rail and end the incident FAILED.
            try:
                tool_result = ToolResult.model_validate(result)
            except ValidationError as exc:
                raise MCPError(-32700, f"malformed tools/call result envelope: {exc}") from exc
        except Exception as exc:
            if self._tracer is not None:
                self._tracer(
                    {
                        "tool_name": name,
                        "arguments": args_dict,
                        "error": f"{type(exc).__name__}: {exc}",
                        "duration_seconds": time.monotonic() - started,
                    }
                )
            raise
        if self._tracer is not None:
            self._tracer(
                {
                    "tool_name": name,
                    "arguments": args_dict,
                    "result": tool_result.model_dump(mode="json"),
                    "duration_seconds": time.monotonic() - started,
                }
            )
        return tool_result

    def _call(
        self,
        method: str,
        params: Mapping[str, Any],
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        """One JSON-RPC round trip, retrying transient failures, returning the result object."""
        body = {
            "jsonrpc": "2.0",
            "id": next(self._ids),
            "method": method,
            "params": dict(params),
        }
        # Per-request timeout override for Tier-1 action tools; None = client default.
        post_kwargs: dict[str, Any] = {"json": body, "headers": self._headers}
        if timeout_seconds is not None:
            post_kwargs["timeout"] = timeout_seconds
        for attempt in range(self._max_attempts):
            try:
                response = self._client.post(self._base_url, **post_kwargs)
            except httpx.RequestError as exc:
                if attempt == self._max_attempts - 1:
                    raise MCPError(
                        -32000,
                        f"transport error after {self._max_attempts} attempts: "
                        f"{type(exc).__name__}: {exc}",
                    ) from exc
                self._sleep(self._retry_base_delay * (2**attempt))
                continue
            status = response.status_code
            if (status >= 500 or status == 429) and attempt < self._max_attempts - 1:
                delay = self._retry_base_delay * (2**attempt)
                retry_after = response.headers.get("retry-after")
                if retry_after is not None:
                    with contextlib.suppress(ValueError):
                        # Capped like LLMClient's: a server-controlled
                        # `Retry-After: 86400` outlasts every wall-clock
                        # budget (invariant 7).
                        delay = max(delay, min(float(retry_after), _MAX_RETRY_AFTER_SECONDS))
                self._sleep(delay)
                continue
            if status >= 400:
                raise MCPError(-32000, f"HTTP {status} from MCP endpoint: {response.text[:200]}")
            try:
                payload = response.json()
            except ValueError as exc:
                raise MCPError(-32700, f"non-JSON response body: {exc}") from exc
            if not isinstance(payload, dict):
                raise MCPError(-32700, f"non-object JSON response: {type(payload).__name__}")
            if "error" in payload:
                raise _error_from_member(payload["error"])
            result = payload.get("result", {})
            return result if isinstance(result, dict) else {}
        raise RuntimeError("unreachable: retry loop exited without response")


def make_client(
    settings: Settings,
    tracer: Callable[[dict[str, Any]], None] | None = None,
    token: str | None = None,
) -> MCPClient:
    """Build a client from Settings — the app-code entry point.

    ``token`` overrides ``settings.platform_token`` for a different principal;
    empty or blank RAISES rather than falling back to the full principal (S-04).
    """
    if token is not None and not token.strip():
        raise ValueError(
            "make_client: explicit token is empty — refusing to fall back to the "
            "full platform principal. Pass None to select settings.platform_token "
            "deliberately, or fix the empty credential."
        )
    return MCPClient(
        base_url=str(settings.platform_mcp_url),
        token=token if token is not None else settings.platform_token.get_secret_value(),
        tracer=tracer,
    )


class MCPClientProtocol(Protocol):
    """Structural type for anything a transition can call to invoke a tool."""

    def call_tool(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        timeout_seconds: float | None = None,
    ) -> ToolResult: ...
