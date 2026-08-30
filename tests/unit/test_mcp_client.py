from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from pydantic import AnyHttpUrl, PostgresDsn, SecretStr

from incident_commander.config import Settings
from incident_commander.tools.mcp_client import MCPClient, MCPError, ToolResult, make_client

_BASE_URL = "https://mcp.local"


def _client(
    handler: Callable[[httpx.Request], httpx.Response],
    max_attempts: int = 3,
) -> MCPClient:
    return MCPClient(
        base_url=_BASE_URL,
        token="svc-token",
        max_attempts=max_attempts,
        retry_base_delay=0.0,
        transport=httpx.MockTransport(handler),
        sleep=lambda _s: None,
    )


def _rpc_ok(result: dict[str, Any], *, request_id: int = 1) -> httpx.Response:
    return httpx.Response(
        200,
        json={"jsonrpc": "2.0", "id": request_id, "result": result},
    )


def _rpc_error(code: int, message: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={"jsonrpc": "2.0", "id": 1, "error": {"code": code, "message": message}},
    )


class TestListTools:
    def test_returns_tools_from_result(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "POST"
            assert request.url == httpx.URL(_BASE_URL)
            body = json.loads(request.content)
            assert body["method"] == "tools/list"
            return _rpc_ok({"tools": [{"name": "get_consumer_lag"}]})

        with _client(handler) as client:
            tools = client.list_tools()
        assert tools == [{"name": "get_consumer_lag"}]

    def test_returns_empty_list_when_result_missing_tools(self) -> None:
        with _client(lambda _r: _rpc_ok({})) as client:
            assert client.list_tools() == []

    def test_authorization_header_sent(self) -> None:
        captured: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["auth"] = request.headers["authorization"]
            return _rpc_ok({"tools": []})

        with _client(handler) as client:
            client.list_tools()
        assert captured["auth"] == "Bearer svc-token"


class TestCallTool:
    def test_returns_tool_result_content(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            assert body["method"] == "tools/call"
            assert body["params"] == {"name": "get_consumer_lag", "arguments": {"group": "billing"}}
            return _rpc_ok({"content": [{"type": "text", "text": "lag=42"}], "isError": False})

        with _client(handler) as client:
            result = client.call_tool("get_consumer_lag", {"group": "billing"})
        assert result.content == [{"type": "text", "text": "lag=42"}]
        assert result.is_error is False

    def test_json_rpc_error_raises_mcp_error(self) -> None:
        with (
            _client(lambda _r: _rpc_error(-32602, "invalid params")) as client,
            pytest.raises(MCPError) as exc,
        ):
            client.call_tool("get_consumer_lag", {})
        assert exc.value.code == -32602
        assert "invalid params" in str(exc.value)

    def test_per_call_timeout_overrides_client_default(self) -> None:
        captured: list[httpx.Timeout | float | None] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request.extensions.get("timeout"))
            return _rpc_ok({"content": [{"type": "text", "text": "ok"}], "isError": False})

        with _client(handler) as client:
            client.call_tool("restart_consumer_group", {}, timeout_seconds=90.0)
            client.call_tool("get_consumer_lag", {})
        assert captured[0] == {"connect": 90.0, "read": 90.0, "write": 90.0, "pool": 90.0}
        assert captured[1] == {"connect": 30.0, "read": 30.0, "write": 30.0, "pool": 30.0}


class TestRetries:
    def test_retries_on_network_error_then_succeeds(self) -> None:
        attempts: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(len(attempts))
            if len(attempts) < 3:
                raise httpx.ConnectError("boom", request=request)
            return _rpc_ok({"tools": []})

        with _client(handler) as client:
            client.list_tools()
        assert len(attempts) == 3

    def test_retries_on_5xx_then_succeeds(self) -> None:
        attempts: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(len(attempts))
            if len(attempts) == 1:
                return httpx.Response(503, text="service unavailable")
            return _rpc_ok({"tools": []})

        with _client(handler) as client:
            client.list_tools()
        assert len(attempts) == 2

    def test_does_not_retry_on_4xx(self) -> None:
        attempts: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(len(attempts))
            return httpx.Response(401, text="unauthorized")

        with _client(handler) as client, pytest.raises(MCPError, match="HTTP 401"):
            client.list_tools()
        assert len(attempts) == 1

    def test_gives_up_after_max_attempts_on_persistent_5xx(self) -> None:
        attempts: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(len(attempts))
            return httpx.Response(500, text="server error")

        with _client(handler, max_attempts=2) as client, pytest.raises(MCPError, match="HTTP 500"):
            client.list_tools()
        assert len(attempts) == 2

    def test_retries_on_429_then_succeeds(self) -> None:
        attempts: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(len(attempts))
            if len(attempts) == 1:
                return httpx.Response(429, text="rate limited", headers={"retry-after": "0"})
            return _rpc_ok({"tools": []})

        with _client(handler) as client:
            client.list_tools()
        assert len(attempts) == 2

    def test_gives_up_after_max_attempts_on_persistent_network_error(self) -> None:
        attempts: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(len(attempts))
            raise httpx.ConnectError("boom", request=request)

        with (
            _client(handler, max_attempts=2) as client,
            pytest.raises(MCPError, match="transport error"),
        ):
            client.list_tools()
        assert len(attempts) == 2


class TestRetryAfterCeiling:
    """A server-supplied ``Retry-After`` is a hint, not an instruction.

    ``LLMClient`` caps it at 60s (``llm/client.py``'s
    ``_MAX_RETRY_AFTER_SECONDS``, applied at :218) precisely so one
    hostile-or-buggy header cannot park a run for a day. This client
    honoured the value unbounded, so a ``Retry-After: 86400`` on a single
    429 outlasted every wall-clock budget the incident had (invariant 7:
    budgets are hard limits, and a sleep the budget cannot see is not
    bounded by it).
    """

    @staticmethod
    def _recording_client(
        handler: Callable[[httpx.Request], httpx.Response],
        delays: list[float],
    ) -> MCPClient:
        return MCPClient(
            base_url=_BASE_URL,
            token="svc-token",
            max_attempts=3,
            retry_base_delay=1.0,
            transport=httpx.MockTransport(handler),
            sleep=delays.append,
        )

    @staticmethod
    def _retry_after_once(value: str) -> Callable[[httpx.Request], httpx.Response]:
        attempts: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(len(attempts))
            if len(attempts) == 1:
                return httpx.Response(429, text="slow down", headers={"retry-after": value})
            return _rpc_ok({"tools": []})

        return handler

    def test_absurd_retry_after_is_capped_at_sixty_seconds(self) -> None:
        delays: list[float] = []
        with self._recording_client(self._retry_after_once("86400"), delays) as client:
            client.list_tools()
        assert delays == [60.0]

    def test_retry_after_below_the_cap_is_still_honoured(self) -> None:
        delays: list[float] = []
        with self._recording_client(self._retry_after_once("7.5"), delays) as client:
            client.list_tools()
        assert delays == [7.5]

    def test_retry_after_never_shortens_the_backoff(self) -> None:
        delays: list[float] = []
        with self._recording_client(self._retry_after_once("0"), delays) as client:
            client.list_tools()
        assert delays == [1.0]


class TestMalformedResultEnvelope:
    """Every failure path raises ``MCPError`` — including envelope validation.

    ``ToolResult.model_validate`` used to sit *outside* ``call_tool``'s
    error-wrapping path, so a 200 carrying a result the envelope cannot
    parse raised a raw ``ValidationError`` out of the module. Every
    transition catches ``MCPError`` and nothing else
    (``investigation.py`` :89 and :334, ``remediation.py`` :532 and :688),
    so that one exception walked straight past the escalate-with-reason
    rail and terminated the incident FAILED with no briefing — the
    fail-open promise (invariant 5) inverted by a typo on the server.
    """

    def test_non_list_content_raises_mcp_error(self) -> None:
        with (
            _client(lambda _r: _rpc_ok({"content": "not-a-list"})) as client,
            pytest.raises(MCPError, match="malformed tools/call result"),
        ):
            client.call_tool("get_consumer_lag", {})

    def test_non_object_content_blocks_raise_mcp_error(self) -> None:
        with _client(lambda _r: _rpc_ok({"content": [1, 2, 3]})) as client, pytest.raises(MCPError):
            client.call_tool("get_consumer_lag", {})

    def test_non_boolean_is_error_flag_raises_mcp_error(self) -> None:
        with _client(lambda _r: _rpc_ok({"isError": "perhaps"})) as client, pytest.raises(MCPError):
            client.call_tool("get_consumer_lag", {})

    def test_malformed_envelope_is_traced_like_any_other_failure(self) -> None:
        traced: list[dict[str, Any]] = []
        client = MCPClient(
            base_url=_BASE_URL,
            token="svc-token",
            transport=httpx.MockTransport(lambda _r: _rpc_ok({"content": "not-a-list"})),
            sleep=lambda _s: None,
            tracer=traced.append,
        )
        with client, pytest.raises(MCPError):
            client.call_tool("get_consumer_lag", {"consumer_group": "billing"})
        assert len(traced) == 1
        assert traced[0]["tool_name"] == "get_consumer_lag"
        assert traced[0]["error"].startswith("MCPError:")
        assert "result" not in traced[0]

    def test_well_formed_envelope_is_unaffected(self) -> None:
        with _client(lambda _r: _rpc_ok({"content": [{"type": "text", "text": "ok"}]})) as client:
            result = client.call_tool("get_consumer_lag", {})
        assert result.content == [{"type": "text", "text": "ok"}]


class TestNonConformantErrorMember:
    """``payload["error"]`` is server-controlled and need not be a mapping.

    The branch called ``.get`` on it and ``int()`` on whatever came back,
    so a bare-string error member raised ``AttributeError`` and a
    non-numeric code raised ``ValueError`` — both escaping the module's
    documented ``MCPError``-only contract in exactly the way the malformed
    envelope did.
    """

    @staticmethod
    def _with_error_member(member: Any) -> Callable[[httpx.Request], httpx.Response]:
        return lambda _r: httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "error": member})

    def test_string_error_member_raises_mcp_error(self) -> None:
        with (
            _client(self._with_error_member("boom")) as client,
            pytest.raises(MCPError, match="non-object JSON-RPC error member"),
        ):
            client.call_tool("get_consumer_lag", {})

    def test_list_error_member_raises_mcp_error(self) -> None:
        with _client(self._with_error_member([1, 2])) as client, pytest.raises(MCPError):
            client.call_tool("get_consumer_lag", {})

    def test_null_error_member_raises_mcp_error(self) -> None:
        with _client(self._with_error_member(None)) as client, pytest.raises(MCPError):
            client.call_tool("get_consumer_lag", {})

    def test_non_numeric_code_falls_back_without_losing_the_raw_value(self) -> None:
        member = {"code": "not-a-number", "message": "boom"}
        with _client(self._with_error_member(member)) as client, pytest.raises(MCPError) as excinfo:
            client.call_tool("get_consumer_lag", {})
        assert excinfo.value.code == -32603
        assert "not-a-number" in str(excinfo.value)

    def test_float_code_is_coerced(self) -> None:
        member = {"code": -32601.0, "message": "no such tool"}
        with _client(self._with_error_member(member)) as client, pytest.raises(MCPError) as excinfo:
            client.call_tool("get_consumer_lag", {})
        assert excinfo.value.code == -32601

    def test_conformant_error_member_still_carries_code_message_and_data(self) -> None:
        member = {"code": -32602, "message": "bad params", "data": {"field": "topic"}}
        with _client(self._with_error_member(member)) as client, pytest.raises(MCPError) as excinfo:
            client.call_tool("get_consumer_lag", {})
        assert excinfo.value.code == -32602
        assert excinfo.value.data == {"field": "topic"}
        assert "bad params" in str(excinfo.value)


class TestToolResultWireShape:
    """The platform wire emits camelCase ``isError`` (MCP spec — platform
    protocol.py:139); canned fixtures use snake ``is_error``. Both spellings
    must land on the field, never in ``__pydantic_extra__``, or every
    escalate-on-``is_error`` guard is dead against the wire (C-02)."""

    def test_wire_iserror_true_maps_onto_is_error(self) -> None:
        result = ToolResult.model_validate(
            {"content": [{"type": "text", "text": "boom"}], "isError": True}
        )
        assert result.is_error is True
        assert "isError" not in (result.__pydantic_extra__ or {})

    def test_snake_case_fixture_spelling_still_maps(self) -> None:
        result = ToolResult.model_validate({"content": [], "is_error": True})
        assert result.is_error is True

    def test_direct_construction_still_works(self) -> None:
        assert ToolResult(is_error=True).is_error is True
        assert ToolResult().is_error is False

    def test_model_dump_still_emits_snake_case_field_name(self) -> None:
        # Trajectories, tracer JSONL, and canned fixtures all spell the
        # field ``is_error`` — dumps must keep that spelling and must not
        # leak a stray ``isError`` extra alongside it.
        dumped = ToolResult.model_validate({"content": [], "isError": True}).model_dump(mode="json")
        assert dumped["is_error"] is True
        assert "isError" not in dumped

    def test_call_tool_maps_wire_iserror_true_end_to_end(self) -> None:
        def handler(_r: httpx.Request) -> httpx.Response:
            return _rpc_ok({"content": [{"type": "text", "text": "boom"}], "isError": True})

        with _client(handler) as client:
            result = client.call_tool("restart_consumer_group", {})
        assert result.is_error is True


class TestJsonRpcIds:
    def test_request_ids_increment(self) -> None:
        seen_ids: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            seen_ids.append(body["id"])
            return _rpc_ok({"tools": []}, request_id=body["id"])

        with _client(handler) as client:
            client.list_tools()
            client.list_tools()
            client.list_tools()
        assert seen_ids == [1, 2, 3]


class TestTracer:
    def test_tracer_receives_tool_name_arguments_and_result(self) -> None:
        captured: list[dict[str, Any]] = []

        def handler(_r: httpx.Request) -> httpx.Response:
            return _rpc_ok({"content": [{"type": "text", "text": "ok"}], "isError": False})

        client = MCPClient(
            base_url=_BASE_URL,
            token="svc-token",
            max_attempts=1,
            retry_base_delay=0.0,
            transport=httpx.MockTransport(handler),
            sleep=lambda _s: None,
            tracer=captured.append,
        )
        with client:
            client.call_tool("get_consumer_lag", {"group": "billing"})
        assert len(captured) == 1
        record = captured[0]
        assert record["tool_name"] == "get_consumer_lag"
        assert record["arguments"] == {"group": "billing"}
        assert record["result"]["content"] == [{"type": "text", "text": "ok"}]
        assert record["duration_seconds"] >= 0

    def test_tracer_captures_error(self) -> None:
        captured: list[dict[str, Any]] = []

        client = MCPClient(
            base_url=_BASE_URL,
            token="svc-token",
            max_attempts=1,
            retry_base_delay=0.0,
            transport=httpx.MockTransport(lambda _r: _rpc_error(-32602, "bad args")),
            sleep=lambda _s: None,
            tracer=captured.append,
        )
        with client, pytest.raises(MCPError):
            client.call_tool("get_consumer_lag", {})
        assert len(captured) == 1
        assert captured[0]["error"].startswith("MCPError")


def _settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        anthropic_api_key=SecretStr("sk-ant-test-not-a-real-key"),
        judge_model="claude-sonnet-4-5",
        platform_mcp_url=AnyHttpUrl(_BASE_URL),
        platform_rest_url=AnyHttpUrl(_BASE_URL),
        platform_token=SecretStr("sa_full_scope"),
        platform_webhook_secret=SecretStr("whsec_test"),
        database_url=PostgresDsn("postgresql://eval:eval@localhost:5432/eval"),
    )


def _bearer(client: MCPClient) -> str:
    return client._headers["Authorization"]


class TestMakeClientPrincipalSelection:
    """S-04: an explicit empty token is a config error, not a request for root.

    ``token or settings.platform_token`` conflated "no token given" with
    "an empty token was given" and answered both with the FULL write-scoped
    principal. "Not provided" is a documented default; "provided as empty"
    is a broken config, and a broken config must not resolve upward.
    """

    def test_explicit_empty_token_raises(self) -> None:
        with pytest.raises(ValueError, match="refusing to fall back"):
            make_client(_settings(), token="")

    def test_explicit_empty_token_does_not_build_a_privileged_client(self) -> None:
        # The assertion that catches fallback-to-privileged: at HEAD this
        # returned a client whose Authorization header carried the full
        # platform token.
        try:
            client = make_client(_settings(), token="")
        except ValueError:
            return
        try:
            raise AssertionError(f"make_client returned a client bearing {_bearer(client)!r}")
        finally:
            client.close()

    def test_absent_token_still_selects_the_ambient_platform_token(self) -> None:
        with make_client(_settings()) as client:
            assert _bearer(client) == "Bearer sa_full_scope"
        with make_client(_settings(), token=None) as client:
            assert _bearer(client) == "Bearer sa_full_scope"

    def test_explicit_token_beats_the_ambient_one(self) -> None:
        with make_client(_settings(), token="sa_x") as client:
            assert _bearer(client) == "Bearer sa_x"

    def test_whitespace_only_token_is_not_a_token(self) -> None:
        with pytest.raises(ValueError, match="refusing to fall back"):
            make_client(_settings(), token="   ")
