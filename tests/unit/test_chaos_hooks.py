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

    def test_a_non_object_error_member_is_wrapped(self) -> None:
        """A JSON-RPC ``error`` that is not an object must not raise AttributeError.

        ``.get`` on a string is an AttributeError, which sails straight past
        the runner's ``except ChaosInvocationError`` — the bare untyped crash
        this module's own header says it eliminated.
        """
        payload = {"error": "missing required scope"}
        client = self._client(lambda _r: httpx.Response(200, json=payload))
        with pytest.raises(ChaosInvocationError, match="kill_consumer.*missing required scope"):
            client.call("kill_consumer", {"consumer_group": "wd"})

    def test_a_non_object_content_block_is_wrapped(self) -> None:
        payload = {"result": {"content": ["just a string", {"type": "text", "text": "{}"}]}}
        client = self._client(lambda _r: httpx.Response(200, json=payload))
        assert client.call("kill_consumer", {"consumer_group": "wd"}) == {}

    def test_non_list_content_is_not_iterated_as_one(self) -> None:
        payload = {"result": {"content": {"type": "text", "text": "{}"}}}
        client = self._client(lambda _r: httpx.Response(200, json=payload))
        assert client.call("kill_consumer", {"consumer_group": "wd"}) == {}


class TestToolLevelFailureIsAFailedSeed:
    """A hook that fails at the tool level did not seed the fault.

    JSON-RPC success carries MCP tool failures in ``result.isError``, not in
    the JSON-RPC ``error`` member. Reading only the latter reports a failed
    hook to the runner as a successful seed — so the run grades the agent on
    a fault that was never manufactured. The agent's own ``MCPClient`` was
    hardened for exactly this in C-02; this brings the chaos client to parity.
    """

    @staticmethod
    def _client(handler: Any) -> ChaosClient:
        client = ChaosClient("http://platform.test/mcp", "tok")
        client._client = httpx.Client(transport=httpx.MockTransport(handler))
        return client

    def test_is_error_camel_case_raises_and_names_the_hook(self) -> None:
        """Today this returns the error body as a successful seed."""
        payload = {
            "result": {
                "isError": True,
                "content": [{"type": "text", "text": '{"error": "consumer group not found"}'}],
            }
        }
        client = self._client(lambda _r: httpx.Response(200, json=payload))
        with pytest.raises(ChaosInvocationError, match="kill_consumer.*consumer group not found"):
            client.call("kill_consumer", {"consumer_group": "wd"})

    def test_is_error_snake_case_also_raises(self) -> None:
        """Fixtures and trajectories spell it ``is_error`` (C-02)."""
        payload = {"result": {"is_error": True, "content": [{"type": "text", "text": '"nope"'}]}}
        client = self._client(lambda _r: httpx.Response(200, json=payload))
        with pytest.raises(ChaosInvocationError, match="kill_consumer.*nope"):
            client.call("kill_consumer", {"consumer_group": "wd"})

    def test_is_error_with_no_text_still_names_the_hook(self) -> None:
        payload = {"result": {"isError": True, "content": []}}
        client = self._client(lambda _r: httpx.Response(200, json=payload))
        with pytest.raises(ChaosInvocationError, match="kill_consumer"):
            client.call("kill_consumer", {"consumer_group": "wd"})

    def test_is_error_false_is_still_a_good_seed(self) -> None:
        payload = {
            "result": {"isError": False, "content": [{"type": "text", "text": '{"killed": 1}'}]}
        }
        client = self._client(lambda _r: httpx.Response(200, json=payload))
        assert client.call("kill_consumer", {"consumer_group": "wd"}) == {"killed": 1}


class TestARefusalIsNamedNotBucketedAsFlakiness:
    """A chaos refusal carries the platform's own code, not just ``-32011``.

    WO-R2-16's second half, and the reason plat #199 flagged it at the release:
    every tool-level refusal the platform raises arrives as JSON-RPC ``-32011``
    with the name in ``data.error_code``, so on the code alone a fixture-name
    collision and a Kafka outage read identically. The runner buckets a failed
    seed as ``shared-env`` and a human reads the message — and a message that
    looks like transport flakiness gets re-run, which is exactly wrong for
    ``*_fixture_name_in_use``: the previous run's row is still there, so the
    retry seeds the same refusal again. The fix is a reset, and the message now
    says so.
    """

    @staticmethod
    def _client(handler: Any) -> ChaosClient:
        client = ChaosClient("http://platform.test/mcp", "tok")
        client._client = httpx.Client(transport=httpx.MockTransport(handler))
        return client

    @staticmethod
    def _refusal(code: str) -> dict[str, Any]:
        return {
            "error": {
                "code": -32011,
                "message": f"fixture_name 'x' is already in use ({code})",
                "data": {"error_code": code},
            }
        }

    @pytest.mark.parametrize(
        "code",
        [
            "poison_fixture_name_in_use",
            "mislabeled_fixture_name_in_use",
            "bad_data_fixture_name_in_use",
            "stuck_chain_name_in_use",
        ],
    )
    def test_a_ledgered_refusal_names_itself_and_says_what_to_do(self, code: str) -> None:
        client = self._client(lambda _r: httpx.Response(200, json=self._refusal(code)))
        with pytest.raises(ChaosInvocationError) as err:
            client.call("poison_message", {"topic": "job.submitted", "payload": {}})
        message = str(err.value)
        assert "poison_message" in message  # still names the hook
        assert code in message  # the platform's own name for the refusal
        assert "reset the world, do not retry" in message  # what it means

    def test_an_unledgered_code_still_surfaces_by_name(self) -> None:
        """The table adds a sentence; the NAME comes off the wire.

        So a refusal code the platform ships tomorrow cannot go quiet while
        this dict is out of date — it loses the advice, never the identity.
        """
        client = self._client(lambda _r: httpx.Response(200, json=self._refusal("some_new_code")))
        with pytest.raises(ChaosInvocationError) as err:
            client.call("poison_message", {"topic": "job.submitted", "payload": {}})
        assert "some_new_code" in str(err.value)
        assert "reset the world" not in str(err.value)

    def test_an_error_with_no_data_block_is_unchanged(self) -> None:
        payload = {"error": {"code": -32010, "message": "unknown tool: nope"}}
        client = self._client(lambda _r: httpx.Response(200, json=payload))
        with pytest.raises(ChaosInvocationError, match="-32010: unknown tool"):
            client.call("nope", {})

    def test_a_non_object_error_member_is_still_wrapped(self) -> None:
        """The isinstance guard the hint helper must not undo."""
        client = self._client(lambda _r: httpx.Response(200, json={"error": "just a string"}))
        with pytest.raises(ChaosInvocationError, match="just a string"):
            client.call("poison_message", {"topic": "t", "payload": {}})
