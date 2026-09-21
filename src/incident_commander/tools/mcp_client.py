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
# Longest pause this client takes between attempts even when the platform's Retry-After header
# asks for more: the header is a hint, and a long one would stall the incident run behind it.
_MAX_RETRY_AFTER_SECONDS: Final[float] = 60.0
_JSON_RPC_INTERNAL_ERROR: Final[int] = -32603
_JSON_RPC_INVALID_PARAMS: Final[int] = -32602

# How the evaluator marks a call as its own, so the platform's audit log does not credit the agent
# with the lab's reads (platform ADR 0038). It sits BESIDE ``arguments``, so no prompt ever sees it.
LAB_PROBE_PARAM: Final[str] = "_lab_probe"
LAB_PRINCIPAL_HEADER: Final[str] = "X-Lab-Principal"
#: Longest reason the platform accepts with that marker. It refuses a longer one outright rather
#: than shortening it, so the check below happens here instead of costing a round trip.
LAB_PROBE_REASON_MAX_CHARS: Final[int] = 200
#: The string in ``data.error_code`` when the platform refuses to honour that marker. The numeric
#: code is -32602, which a bad-argument refusal also uses, so only this string tells them apart.
LAB_PROBE_REFUSED_ERROR_CODE: Final[str] = "lab_probe_refused"


class MCPError(RuntimeError):
    """A JSON-RPC error returned by the MCP server."""

    def __init__(self, code: int, message: str, data: object | None = None) -> None:
        super().__init__(f"MCP error {code}: {message}")
        self.code = code
        self.data = data


class LabProbeRefused(MCPError):
    """The platform refused to label this call as the lab's own, and did not run it.

    Its own type because the refusal arrives as ``-32602``, the code a bad-argument refusal also
    uses: the guards that check who made a call read that code as "fine, carry on" (finding F4).
    """

    @property
    def reason_code(self) -> str:
        """Which part of the labelling rule the request broke, in the platform's own words."""
        data = self.data
        if isinstance(data, Mapping):
            return str(data.get("reason_code", "unknown"))
        return "unknown"


def _error_from_member(member: object) -> MCPError:
    """Build an ``MCPError`` from a server-supplied JSON-RPC ``error`` member.

    The member comes from outside this process, so nothing about its shape can be assumed. A code
    that is not a number is put into the message rather than dropped, so no detail is lost.
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
    """Check the lab's reason and credential, and return the header the credential travels in.

    Both or neither: one without the other is a mistake in the calling code, and it raises here
    rather than spending a round trip to be told the same thing by the platform.
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
    # The platform spells it ``isError``, our fixtures ``is_error``. Without the alias a real
    # ``{"isError": true}`` lands in the extras and every escalate-on-error guard sees False (C-02).
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

        ``lab_probe`` makes the platform record the call as ``lab.probe`` rather than
        ``agent.tool_invoked``, and needs ``lab_principal_token`` with it. The agent's own path
        cannot reach either argument, because ``MCPClientProtocol`` does not declare them.
        """
        # 1. Build the ``tools/call`` parameters. The arguments are copied into a plain dict so the
        #    trace below records exactly what was sent, whatever kind of mapping the caller passed.
        started = time.monotonic()
        args_dict = dict(arguments)
        params: dict[str, Any] = {"name": name, "arguments": args_dict}
        extra_headers: dict[str, str] | None = None
        # 2. When the evaluator is making this call rather than the agent, check the label pair and
        #    send the lab's own credential, which is what makes the platform log it as the lab's.
        if lab_probe is not None or lab_principal_token is not None:
            extra_headers = _lab_probe_envelope(lab_probe, lab_principal_token)
            params[LAB_PROBE_PARAM] = lab_probe
        # 3. Make the round trip and read the result envelope the platform sent back.
        try:
            result = self._call(
                "tools/call",
                params,
                timeout_seconds=timeout_seconds,
                extra_headers=extra_headers,
            )
            # Converted to ``MCPError`` inside this wrapper on purpose: the state transitions catch
            # ``MCPError`` and nothing else, so a raw Pydantic error would escape every rail that
            # turns a tool failure into an escalation with a reason.
            try:
                tool_result = ToolResult.model_validate(result)
            except ValidationError as exc:
                raise MCPError(-32700, f"malformed tools/call result envelope: {exc}") from exc
        # 4. The call failed somewhere above: record it in the trace with the error text, then let
        #    the exception through unchanged so the caller decides what the run does about it.
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
        # 5. The call succeeded: record it with its result, then hand the result back. Content
        #    blocks inside it are untrusted data, so nothing here reads or acts on them.
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
        # 1. Build the JSON-RPC envelope. The id comes from this client's own counter, so a reply
        #    can be matched to the request that asked for it.
        body = {
            "jsonrpc": "2.0",
            "id": next(self._ids),
            "method": method,
            "params": dict(params),
        }
        # 2. Assemble this request's headers and timeout. The headers are COPIED, because a lab
        #    credential belongs to one call; the timeout lets a slow action outlast a read's limit.
        headers = self._headers if extra_headers is None else {**self._headers, **extra_headers}
        post_kwargs: dict[str, Any] = {"json": body, "headers": headers}
        if timeout_seconds is not None:
            post_kwargs["timeout"] = timeout_seconds
        # 3. Send the same body up to ``max_attempts`` times.
        for attempt in range(self._max_attempts):
            try:
                response = self._client.post(self._base_url, **post_kwargs)
            # 4. The request never got an answer (connection refused, DNS, timeout). Back off and
            #    resend, unless this was the last attempt, in which case report the transport error.
            except httpx.RequestError as exc:
                if attempt == self._max_attempts - 1:
                    raise MCPError(
                        -32000,
                        f"transport error after {self._max_attempts} attempts: "
                        f"{type(exc).__name__}: {exc}",
                    ) from exc
                self._sleep(self._retry_base_delay * (2**attempt))
                continue
            # 5. The platform is rate limiting us or is briefly broken, and an attempt is left:
            #    wait the doubling backoff, or the server's own Retry-After when it asks for longer.
            status = response.status_code
            if (status >= 500 or status == 429) and attempt < self._max_attempts - 1:
                delay = self._retry_base_delay * (2**attempt)
                retry_after = response.headers.get("retry-after")
                if retry_after is not None:
                    with contextlib.suppress(ValueError):
                        # Capped at 60 seconds, as in ``LLMClient``: a header saying
                        # `Retry-After: 86400` would outlast the run's whole time budget.
                        delay = max(delay, min(float(retry_after), _MAX_RETRY_AFTER_SECONDS))
                self._sleep(delay)
                continue
            # 6. Any other error status, including a 5xx on the last attempt. The first 200
            #    characters of the body go in the message; the rest could be a wall of HTML.
            if status >= 400:
                raise MCPError(-32000, f"HTTP {status} from MCP endpoint: {response.text[:200]}")
            # 7. A 2xx, so read the body. Anything that is not a JSON object is a broken server,
            #    not a tool failure, and is reported as a parse error rather than trusted.
            try:
                payload = response.json()
            except ValueError as exc:
                raise MCPError(-32700, f"non-JSON response body: {exc}") from exc
            if not isinstance(payload, dict):
                raise MCPError(-32700, f"non-object JSON response: {type(payload).__name__}")
            # 8. The platform refused the call and said why: raise it as ``MCPError``, or as
            #    ``LabProbeRefused`` when the refusal is specifically about the lab label.
            if "error" in payload:
                raise _error_from_member(payload["error"])
            # 9. Success. A ``result`` that is not an object reads as an empty one, so a caller
            #    never has to type-check what it got back from here.
            result = payload.get("result", {})
            return result if isinstance(result, dict) else {}
        raise RuntimeError("unreachable: retry loop exited without response")


def make_client(
    settings: Settings,
    tracer: Callable[[dict[str, Any]], None] | None = None,
    token: str | None = None,
) -> MCPClient:
    """Build a client from Settings — the entry point application code should use.

    ``token`` replaces ``settings.platform_token`` when the caller is a different principal, such
    as the read-only smoke account. An empty or blank one RAISES instead of quietly falling back
    to the agent's full-privilege token, which is how a read-only run gained write scope (S-04).
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

    It satisfies ``MCPClientProtocol``, so a shared read walk (``evals/world_audit.py``) labels
    every call without threading the credential through five signatures. Validated at init.
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

    def close(self) -> None:
        """Close the transport underneath, when it has one to close.

        So a caller that only holds the wrapper can still shut the connection down; a fake
        transport in a test need not have the method.
        """
        closer = getattr(self._inner, "close", None)
        if callable(closer):
            closer()

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
