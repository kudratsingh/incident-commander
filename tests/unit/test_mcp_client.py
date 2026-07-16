from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from incident_commander.tools.mcp_client import MCPClient, MCPError

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

        with _client(handler) as client, pytest.raises(httpx.HTTPStatusError):
            client.list_tools()
        assert len(attempts) == 1

    def test_gives_up_after_max_attempts_on_persistent_5xx(self) -> None:
        attempts: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(len(attempts))
            return httpx.Response(500, text="server error")

        with _client(handler, max_attempts=2) as client, pytest.raises(httpx.HTTPStatusError):
            client.list_tools()
        assert len(attempts) == 2

    def test_gives_up_after_max_attempts_on_persistent_network_error(self) -> None:
        attempts: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(len(attempts))
            raise httpx.ConnectError("boom", request=request)

        with _client(handler, max_attempts=2) as client, pytest.raises(httpx.ConnectError):
            client.list_tools()
        assert len(attempts) == 2


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
