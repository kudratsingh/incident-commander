"""Chaos seeding failures must arrive as ChaosInvocationError, named.

``evals/runner.py`` wraps the seeding call in ``except ChaosInvocationError``
and re-raises with the scenario and hook name attached. Anything the client
lets through unwrapped skips that handler entirely and lands in
``_crashed_result`` as a bare "transport" crash — which sends the reader to
the network when the truth is that one specific fault could not be seeded.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from evals.chaos_hooks import ChaosClient, ChaosInvocationError


class TestEveryFailureNamesTheHook:
    """A seeding failure must say which fault failed to seed.

    The runner catches ChaosInvocationError. Anything else escapes it and
    lands as a bare "transport" crash — losing the hook name, which is the
    one detail that says which fault did not get made.
    """

    @staticmethod
    def _client(handler: Any) -> ChaosClient:
        client = ChaosClient("http://platform.test/mcp", "tok")
        client._client = httpx.Client(transport=httpx.MockTransport(handler))
        return client

    def test_http_error_status_is_wrapped_and_names_the_hook(self) -> None:
        client = self._client(lambda _r: httpx.Response(502, text="<html>bad gateway</html>"))
        with pytest.raises(ChaosInvocationError, match="kill_consumer.*502"):
            client.call("kill_consumer", {"consumer_group": "wd"})

    def test_a_non_json_body_is_wrapped(self) -> None:
        client = self._client(lambda _r: httpx.Response(200, text="not json at all"))
        with pytest.raises(ChaosInvocationError, match="kill_consumer.*not JSON"):
            client.call("kill_consumer", {"consumer_group": "wd"})

    def test_a_connection_failure_is_wrapped(self) -> None:
        def _boom(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        client = self._client(_boom)
        with pytest.raises(ChaosInvocationError, match="kill_consumer.*transport failure"):
            client.call("kill_consumer", {"consumer_group": "wd"})

    def test_unparseable_result_content_is_wrapped(self) -> None:
        payload = {"result": {"content": [{"type": "text", "text": "{not json"}]}}
        client = self._client(lambda _r: httpx.Response(200, json=payload))
        with pytest.raises(ChaosInvocationError, match="kill_consumer.*not JSON"):
            client.call("kill_consumer", {"consumer_group": "wd"})

    def test_an_mcp_error_still_names_the_hook(self) -> None:
        payload = {"error": {"code": -32002, "message": "missing required scope"}}
        client = self._client(lambda _r: httpx.Response(200, json=payload))
        with pytest.raises(ChaosInvocationError, match="kill_consumer.*missing required scope"):
            client.call("kill_consumer", {"consumer_group": "wd"})

    def test_a_good_response_still_returns_the_parsed_result(self) -> None:
        payload = {"result": {"content": [{"type": "text", "text": '{"accepted": true}'}]}}
        client = self._client(lambda _r: httpx.Response(200, json=payload))
        assert client.call("kill_consumer", {"consumer_group": "wd"}) == {"accepted": True}
