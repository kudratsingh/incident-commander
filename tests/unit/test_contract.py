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
        assert set(result["tools"][0].keys()) == {"name", "description", "inputSchema"}

    def test_empty_tools_list(self) -> None:
        assert normalize({"tools": []}) == {"tools": []}
        assert normalize({}) == {"tools": []}


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
