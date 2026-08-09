"""Sync JSON-RPC transport for the platform's MCP endpoint.

Deliberately transport-only: no tool-schema validation (that lives with the typed
registry), no budget accounting (that lives with the transition that calls tools).
Retry policy retries transient failures — network errors, 5xx, and 429 — never
other 4xx. Every failure path raises ``MCPError`` so transitions escalate with
a graded reason instead of crashing the scenario with a raw httpx exception.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable, Mapping
from itertools import count
from typing import Any, Final, Protocol

import httpx
from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from incident_commander.config import Settings

_DEFAULT_TIMEOUT_SECONDS: Final[float] = 30.0
_DEFAULT_MAX_ATTEMPTS: Final[int] = 3
_DEFAULT_RETRY_BASE_DELAY: Final[float] = 1.0


class MCPError(RuntimeError):
    """A JSON-RPC error returned by the MCP server."""

    def __init__(self, code: int, message: str, data: object | None = None) -> None:
        super().__init__(f"MCP error {code}: {message}")
        self.code = code
        self.data = data


class ToolResult(BaseModel):
    """Result of a ``tools/call`` invocation. Content blocks are untrusted data."""

    model_config = ConfigDict(extra="allow", frozen=True)

    content: list[dict[str, Any]] = []
    # The MCP spec (and the platform: protocol.py:139) spells the error flag
    # camelCase ``isError`` on the wire; canned fixtures and trajectories use
    # snake ``is_error``. Both must land on the field — without the alias a
    # spec-conformant {"isError": true} fell into __pydantic_extra__ and every
    # escalate-on-error guard stayed dead against the wire (C-02). No
    # serialization_alias on purpose: dumps keep emitting ``is_error`` so
    # tracer JSONL and canned-fixture round-trips stay byte-stable.
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
        started = time.monotonic()
        args_dict = dict(arguments)
        try:
            result = self._call(
                "tools/call",
                {"name": name, "arguments": args_dict},
                timeout_seconds=timeout_seconds,
            )
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
        tool_result = ToolResult.model_validate(result)
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
        body = {
            "jsonrpc": "2.0",
            "id": next(self._ids),
            "method": method,
            "params": dict(params),
        }
        # Per-request timeout override — used for Tier-1 action tools which
        # can legitimately take longer than the client's read-default. When
        # None, httpx falls back to the client-level timeout.
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
                        delay = max(delay, float(retry_after))
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
                err = payload["error"]
                raise MCPError(
                    int(err.get("code", -32603)),
                    str(err.get("message", "unknown error")),
                    err.get("data"),
                )
            result = payload.get("result", {})
            return result if isinstance(result, dict) else {}
        raise RuntimeError("unreachable: retry loop exited without response")


def make_client(
    settings: Settings,
    tracer: Callable[[dict[str, Any]], None] | None = None,
    token: str | None = None,
) -> MCPClient:
    """Build a client from Settings — the app-code entry point.

    ``token`` overrides ``settings.platform_token`` for callers that must
    run under a different principal (the eval runner's read-scoped smoke
    mode). Explicit beats ambient: the previous approach threaded the
    smoke token through make's environment and lost it to `-include .env`.

    "Not provided" and "provided as empty" are different requests and get
    different answers. Omitting ``token`` (or passing ``None``) selects the
    documented ambient default, ``settings.platform_token``. Passing an
    empty or blank string is a configuration error and raises: the old
    ``token or ...`` treated it as ambient, so a caller who *meant* to run
    restricted silently got the FULL write-scoped principal instead of a
    failure (S-04). A broken credential never resolves upward.
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
