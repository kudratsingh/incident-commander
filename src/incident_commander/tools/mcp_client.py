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
_JSON_RPC_INVALID_PARAMS: Final[int] = -32602

# The lab's label on a call it makes with the agent's token (platform ADR 0038). It sits
# BESIDE ``arguments`` in ``params``, so it reaches no tool's input model and no prompt.
LAB_PROBE_PARAM: Final[str] = "_lab_probe"
LAB_PRINCIPAL_HEADER: Final[str] = "X-Lab-Principal"
#: The platform refuses an over-long reason rather than truncating it.
LAB_PROBE_REASON_MAX_CHARS: Final[int] = 200
#: ``data.error_code`` on the refusal. The CODE is -32602, which is also what an
#: argument-validation refusal carries, so the distinguishing mark is this string.
LAB_PROBE_REFUSED_ERROR_CODE: Final[str] = "lab_probe_refused"


class MCPError(RuntimeError):
    """A JSON-RPC error returned by the MCP server."""

    def __init__(self, code: int, message: str, data: object | None = None) -> None:
        super().__init__(f"MCP error {code}: {message}")
        self.code = code
        self.data = data


class LabProbeRefused(MCPError):
    """The platform refused to label this call as the lab's own, and did not run it.

    Its own type because the refusal arrives as ``-32602``, the same code an
    argument-validation refusal carries, which the principal guards read as a pass (F4).
    """

    @property
    def reason_code(self) -> str:
        """Which half of the rule the request failed, per the platform's closed set."""
        data = self.data
        if isinstance(data, Mapping):
            return str(data.get("reason_code", "unknown"))
        return "unknown"


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
    data = member.get("data")
    refused = (
        code == _JSON_RPC_INVALID_PARAMS
        and isinstance(data, Mapping)
        and data.get("error_code") == LAB_PROBE_REFUSED_ERROR_CODE
    )
    return (LabProbeRefused if refused else MCPError)(code, message, data)


def _lab_probe_envelope(reason: str | None, principal_token: str | None) -> dict[str, str]:
    """Validate the label pair and return the header it travels with.

    Both or neither: either alone is a request bug, and raises here rather than spending
    a round trip to be told so.
    """
    if reason is None or principal_token is None:
        raise ValueError(
            "lab probe: pass both lab_probe and lab_principal_token or neither — "
            f"got reason={'set' if reason is not None else 'None'}, "
            f"credential={'set' if principal_token is not None else 'None'}. "
            "The platform honours the label only with the lab's own credential."
        )
    if not 1 <= len(reason) <= LAB_PROBE_REASON_MAX_CHARS:
        raise ValueError(
            f"lab probe: the reason is {len(reason)} characters; the platform takes "
            f"1 to {LAB_PROBE_REASON_MAX_CHARS} and REFUSES anything longer rather "
            "than truncating it."
        )
    if not principal_token.strip():
        raise ValueError(
            "lab probe: the lab credential is empty — refusing to send an "
            "unlabelled call instead, which is how the row goes back to being the "
            "agent's."
        )
    return {LAB_PRINCIPAL_HEADER: f"Bearer {principal_token}"}


class ToolResult(BaseModel):
    """Result of a ``tools/call`` invocation. Content blocks are untrusted data."""

    model_config = ConfigDict(extra="allow", frozen=True)

    content: list[dict[str, Any]] = []
    # The wire spells the flag ``isError``, fixtures ``is_error``; without the alias
    # {"isError": true} falls into extras and every escalate-on-error guard is dead (C-02).
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
        lab_probe: str | None = None,
        lab_principal_token: str | None = None,
    ) -> ToolResult:
        """Invoke one platform tool and return its result; the call is traced either way.

        ``lab_probe`` makes the platform write ``lab.probe`` instead of ``agent.tool_invoked``,
        and is honoured only with ``lab_principal_token``. The agent's own path passes
        neither — ``MCPClientProtocol`` has no such parameter.
        """
        started = time.monotonic()
        args_dict = dict(arguments)
        params: dict[str, Any] = {"name": name, "arguments": args_dict}
        extra_headers: dict[str, str] | None = None
        if lab_probe is not None or lab_principal_token is not None:
            extra_headers = _lab_probe_envelope(lab_probe, lab_principal_token)
            params[LAB_PROBE_PARAM] = lab_probe
        try:
            result = self._call(
                "tools/call",
                params,
                timeout_seconds=timeout_seconds,
                extra_headers=extra_headers,
            )
            # Inside the wrapper on purpose: transitions catch MCPError and nothing else, so
            # a raw ValidationError would walk past every escalate-with-reason rail.
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
        extra_headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """One JSON-RPC round trip, retrying transient failures, returning the result object."""
        body = {
            "jsonrpc": "2.0",
            "id": next(self._ids),
            "method": method,
            "params": dict(params),
        }
        # A per-request copy: the lab credential belongs to one call, not to the client.
        headers = self._headers if extra_headers is None else {**self._headers, **extra_headers}
        # Per-request timeout override for Tier-1 action tools; None = client default.
        post_kwargs: dict[str, Any] = {"json": body, "headers": headers}
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
                        # Capped like LLMClient's: `Retry-After: 86400` would
                        # outlast every wall-clock budget (invariant 7).
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
    """Structural type for anything a transition can call to invoke a tool.

    NO lab-probe parameter, deliberately: the agent's own path has no way to label a call
    as the lab's. ``LabProbeCapableClient`` below offers that, for the evaluator only.
    """

    def call_tool(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        timeout_seconds: float | None = None,
    ) -> ToolResult: ...


class LabProbeCapableClient(Protocol):
    """Structural type for a transport that CAN label a call as the lab's own."""

    def call_tool(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        timeout_seconds: float | None = None,
        lab_probe: str | None = None,
        lab_principal_token: str | None = None,
    ) -> ToolResult: ...


class LabProbeClient:
    """A client whose every call is labelled as the lab's, with one reason.

    It satisfies ``MCPClientProtocol``, so a shared read walk (``evals/world_audit.py``'s
    probe set) labels every call without threading the credential through five signatures.
    The pair is validated at construction, not on the first read.
    """

    def __init__(self, inner: LabProbeCapableClient, *, reason: str, principal_token: str) -> None:
        _lab_probe_envelope(reason, principal_token)
        self._inner = inner
        self._reason = reason
        self._principal_token = principal_token

    @property
    def reason(self) -> str:
        """The label every call from this client carries."""
        return self._reason

    def call_tool(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        timeout_seconds: float | None = None,
    ) -> ToolResult:
        return self._inner.call_tool(
            name,
            arguments,
            timeout_seconds=timeout_seconds,
            lab_probe=self._reason,
            lab_principal_token=self._principal_token,
        )
