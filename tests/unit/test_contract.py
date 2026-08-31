from copy import deepcopy
from typing import Any

import pytest

from incident_commander.tools.contract import ContractDiff, compare, normalize


@pytest.fixture
def snapshot() -> dict[str, Any]:
    return {
        "tools": [
            {
                "name": "get_consumer_lag",
                "description": "Read lag",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "consumer_group": {
                            "type": "string",
                            "default": "worker-dispatcher",
                        }
                    },
                },
            },
            {
                "name": "list_dlq_messages",
                "description": "List DLQ",
                "inputSchema": {"type": "object", "properties": {}},
            },
        ]
    }


class TestNormalize:
    def test_sorts_tools_by_name(self) -> None:
        raw = {
            "tools": [
                {"name": "b", "description": "", "inputSchema": {}},
                {"name": "a", "description": "", "inputSchema": {}},
            ]
        }
        result = normalize(raw)
        assert [t["name"] for t in result["tools"]] == ["a", "b"]

    def test_strips_extra_top_level_fields(self) -> None:
        raw = {
            "tools": [{"name": "a", "description": "d", "inputSchema": {}}],
            "server_time": "irrelevant",
            "cursor": "irrelevant",
        }
        result = normalize(raw)
        assert set(result.keys()) == {"tools"}

    def test_strips_extra_per_tool_fields(self) -> None:
        raw = {
            "tools": [
                {
                    "name": "a",
                    "description": "d",
                    "inputSchema": {},
                    "annotations": "should be dropped",
                }
            ]
        }
        result = normalize(raw)
        # `annotations` is dropped; the six snapshotted fields are kept.
        # required_scope/is_idempotent joined this set at wave-10
        # (WO-R2-130) — see TestScopeAndIdempotencyAreVisibleToTheDiff.
        assert set(result["tools"][0].keys()) == {
            "name",
            "description",
            "inputSchema",
            "outputSchema",
            "required_scope",
            "is_idempotent",
        }

    def test_empty_tools_list(self) -> None:
        assert normalize({"tools": []}) == {"tools": []}
        assert normalize({}) == {"tools": []}

    def test_output_schema_read_from_wire(self) -> None:
        # v0.4.8+ platform emits outputSchema in tools/list directly.
        raw = {
            "tools": [
                {
                    "name": "a",
                    "description": "d",
                    "inputSchema": {},
                    "outputSchema": {"type": "object", "properties": {"ok": {"type": "boolean"}}},
                }
            ]
        }
        result = normalize(raw)
        assert result["tools"][0]["outputSchema"] == {
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
        }

    def test_missing_output_schema_defaults_to_empty(self) -> None:
        # Older platforms or chaos-only tools may omit outputSchema.
        raw = {"tools": [{"name": "a", "description": "d", "inputSchema": {}}]}
        result = normalize(raw)
        assert result["tools"][0]["outputSchema"] == {}


class TestCompare:
    def test_identical_snapshots_no_diff(self, snapshot: dict[str, Any]) -> None:
        diff = compare(snapshot, deepcopy(snapshot))
        assert diff == ContractDiff(added=(), removed=(), changed=())
        assert diff.is_empty

    def test_added_tool(self, snapshot: dict[str, Any]) -> None:
        live = deepcopy(snapshot)
        live["tools"].append({"name": "new_tool", "description": "d", "inputSchema": {}})
        diff = compare(snapshot, live)
        assert diff.added == ("new_tool",)
        assert diff.removed == ()
        assert diff.changed == ()
        assert not diff.is_empty

    def test_removed_tool(self, snapshot: dict[str, Any]) -> None:
        live = deepcopy(snapshot)
        live["tools"].pop(0)  # remove get_consumer_lag
        diff = compare(snapshot, live)
        assert diff.removed == ("get_consumer_lag",)
        assert diff.added == ()

    def test_description_change_flagged(self, snapshot: dict[str, Any]) -> None:
        live = deepcopy(snapshot)
        live["tools"][0]["description"] = "Read lag (v2)"
        diff = compare(snapshot, live)
        assert diff.changed == ("get_consumer_lag",)

    def test_input_schema_change_flagged(self, snapshot: dict[str, Any]) -> None:
        live = deepcopy(snapshot)
        live["tools"][0]["inputSchema"]["properties"]["consumer_group"]["default"] = "event-log"
        diff = compare(snapshot, live)
        assert diff.changed == ("get_consumer_lag",)

    def test_output_schema_change_flagged(self) -> None:
        # v0.4.4's real drift was outputs — this is the assertion that would
        # have caught it. Same platform tool description + inputSchema, but a
        # response field changed → surfaces as a `changed` delta.
        committed = normalize(
            {
                "tools": [
                    {
                        "name": "get_consumer_lag",
                        "description": "d",
                        "inputSchema": {},
                        "outputSchema": {"properties": {"lag": {"type": "integer"}}},
                    }
                ]
            }
        )
        live = normalize(
            {
                "tools": [
                    {
                        "name": "get_consumer_lag",
                        "description": "d",
                        "inputSchema": {},
                        "outputSchema": {
                            "properties": {
                                "lag": {"type": "integer"},
                                "new_field": {"type": "string"},
                            }
                        },
                    }
                ]
            }
        )
        diff = compare(committed, live)
        assert diff.changed == ("get_consumer_lag",)

    def test_new_required_field_flagged(self, snapshot: dict[str, Any]) -> None:
        live = deepcopy(snapshot)
        live["tools"][0]["inputSchema"]["required"] = ["consumer_group"]
        diff = compare(snapshot, live)
        assert diff.changed == ("get_consumer_lag",)

    def test_multiple_deltas_reported_together(self, snapshot: dict[str, Any]) -> None:
        live = deepcopy(snapshot)
        live["tools"][0]["description"] = "changed"
        live["tools"].pop(1)  # remove list_dlq_messages
        live["tools"].append({"name": "new_thing", "description": "", "inputSchema": {}})
        diff = compare(snapshot, live)
        assert diff.added == ("new_thing",)
        assert diff.removed == ("list_dlq_messages",)
        assert diff.changed == ("get_consumer_lag",)

    def test_deltas_sorted(self, snapshot: dict[str, Any]) -> None:
        live = deepcopy(snapshot)
        live["tools"].append({"name": "zeta", "description": "", "inputSchema": {}})
        live["tools"].append({"name": "alpha", "description": "", "inputSchema": {}})
        diff = compare(snapshot, live)
        assert diff.added == ("alpha", "zeta")

    def test_mutation_of_committed_snapshot_detected(self) -> None:
        """The exit criterion: a mutated schema fails the check.

        We load the shipped snapshot, mutate a field, and confirm ``compare``
        flags it as changed. Wired against the real committed file so this
        breaks if the snapshot moves and its schema stops looking the way we
        expect.
        """
        import json
        from pathlib import Path

        committed = json.loads(
            (
                Path(__file__).resolve().parents[2] / "contracts" / "platform-tools.snapshot.json"
            ).read_text()
        )
        mutated = deepcopy(committed)
        # Change any tool's description; the "mutated schema" would be a real
        # source of drift in production.
        mutated["tools"][0]["description"] = "mutated for the test"
        diff = compare(committed, mutated)
        assert not diff.is_empty
        assert mutated["tools"][0]["name"] in diff.changed


class TestScopeAndIdempotencyAreVisibleToTheDiff:
    """`required_scope` and `is_idempotent` are snapshotted (WO-R2-130).

    Both are platform extensions (plat #168 / WO-R2-32) advertised on every
    `tools/list` entry, and both were invisible to this module until
    wave-10: `_tool_view` kept only name/description/inputSchema/
    outputSchema, so re-scoping a tool or dropping its idempotency changed
    nothing the contract diff could see. The platform added them *for* this
    diff — its own `ToolInfo` docstring says they "mirror `ToolDefinition`'s
    attribute names, which is what the commander's `_tool_view` reads on
    the other side" — so a snapshot that dropped them made the contract
    test unable to catch the drift it exists to catch.

    `is_idempotent` is the one that bites. It is what makes a Tier-1
    recovery re-invoke return the cached response verbatim; if it is
    silently dropped the retry actually re-runs, returns a different
    payload, and verification reads that as a spurious escalation — a
    failure that surfaces far from its cause.

    Both are snake_case on the wire, unlike inputSchema/outputSchema: the
    camelCase convention belongs to the MCP spec's own fields.
    """

    @staticmethod
    def _one(**overrides: Any) -> dict[str, Any]:
        tool: dict[str, Any] = {
            "name": "replay_dlq_by_ids",
            "description": "Replay by id",
            "inputSchema": {"type": "object", "properties": {}},
            "outputSchema": {"type": "object", "properties": {}},
            "required_scope": "actions:execute",
            "is_idempotent": True,
        }
        tool.update(overrides)
        return {"tools": [tool]}

    def test_both_fields_are_read_off_the_wire(self) -> None:
        result = normalize(self._one())
        assert result["tools"][0]["required_scope"] == "actions:execute"
        assert result["tools"][0]["is_idempotent"] is True
        assert set(result["tools"][0].keys()) == {
            "name",
            "description",
            "inputSchema",
            "outputSchema",
            "required_scope",
            "is_idempotent",
        }

    def test_a_rescoped_tool_is_named_by_the_diff(self) -> None:
        """The work order's own red/green: mutate a scope, diff names it."""
        committed = normalize(self._one())
        live = normalize(self._one(required_scope="telemetry:read"))
        assert compare(committed, live).changed == ("replay_dlq_by_ids",)

    def test_a_dropped_idempotency_is_named_by_the_diff(self) -> None:
        committed = normalize(self._one())
        live = normalize(self._one(is_idempotent=False))
        assert compare(committed, live).changed == ("replay_dlq_by_ids",)

    def test_defaults_match_the_platforms_own(self) -> None:
        """A tool that advertises neither reads as unscoped, non-idempotent.

        The platform defaults `required_scope` to None (the few tools that
        need no scope) and `is_idempotent` to False, so the snapshot has to
        agree or every such entry would diff on the first rebless.
        """
        raw = {"tools": [{"name": "a", "description": "d", "inputSchema": {}}]}
        result = normalize(raw)
        assert result["tools"][0]["required_scope"] is None
        assert result["tools"][0]["is_idempotent"] is False

    def test_a_null_scope_is_preserved_not_coerced(self) -> None:
        """`None` is a real value here, not a missing one.

        Coercing it to "" would make an unscoped tool and a tool whose
        scope was dropped look identical to the diff.
        """
        result = normalize(self._one(required_scope=None))
        assert result["tools"][0]["required_scope"] is None
