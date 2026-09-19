"""The planner's page, pinned — the half of the prompt nobody authored.

``format_tool_block`` assembles the tool listing from ``TOOL_REGISTRY``, the tier map and
the platform-authored descriptions in the snapshot, so it moves when the PINNED PLATFORM
IMAGE moves, which no prompt hash could see: v0.6.9 grew it ~4,700 characters with the
suite green. Both halves are here — the block is read-tier only, and it is pinned by hash.
"""

from __future__ import annotations

import hashlib
import json
from typing import Final

from incident_commander.agent.planner_context import format_tool_block
from incident_commander.tools.policies import Tier, tools_at_or_below
from incident_commander.tools.registry import TOOL_REGISTRY, description_of

#: The read surface the agent is shown, spelled out. Hand-listed on purpose: a tool
#: joining or leaving it is worth a reviewer's eye. Held equal to the tier map.
_EXPECTED_READ_TOOLS: Final[frozenset[str]] = frozenset(
    {
        "get_cache_key_info",
        "get_consumer_lag",
        "get_dag_state",
        "get_deploy_history",
        "get_incident",
        # Joined on the platform v0.6.9 re-pin (WO-R3-201): how the
        # transactional outbox is draining. The 14th read tool.
        "get_outbox_status",
        "get_postgres_health",
        "get_redis_health",
        "get_trace",
        "list_active_alerts",
        "list_audit_events",
        "list_dlq_messages",
        "list_incidents",
        "search_traces",
    }
)

#: sha256 of ``format_tool_block()``. Moved by the v0.6.9 re-pin: 13 read tools → 14.
_EXPECTED_TOOL_BLOCK_HASH: Final[str] = (
    "3132f829b129ad0d07fea7e4bcb406cfaa10f17b3c3bf56f08f4d56c9227b0c2"
)


class TestTheBlockIsReadTierOnly:
    """What ``planner_context``'s docstring promises: a strategy importing the
    renderer can learn which read probes exist, and nothing more."""

    def test_it_lists_exactly_the_read_tier(self) -> None:
        listed = {
            line.strip().removeprefix("- ").split(":", 1)[0]
            for line in format_tool_block().splitlines()
            if line.startswith("  - ")
        }
        assert listed == tools_at_or_below(Tier.READ)

    def test_the_expected_set_matches_the_tier_map(self) -> None:
        assert tools_at_or_below(Tier.READ) == _EXPECTED_READ_TOOLS, (
            "the read surface moved. Update _EXPECTED_READ_TOOLS in this file "
            "and say in the PR which tool joined or left what the agent sees."
        )

    def test_no_tier_1_tool_is_listed(self) -> None:
        block = format_tool_block()
        writes = tools_at_or_below(Tier.TIER_1) - tools_at_or_below(Tier.READ)
        assert writes
        for name in writes:
            assert f"  - {name}:" not in block


class TestTheBlockIsPinned:
    def test_the_hash_matches(self) -> None:
        actual = hashlib.sha256(format_tool_block().encode()).hexdigest()
        assert actual == _EXPECTED_TOOL_BLOCK_HASH, (
            "the planner's tool listing changed — the agent's prompt moved.\n"
            "This is expected on a platform re-pin (a new read tool, or a tool "
            "description the platform rewrote) and on a tier reclassification. "
            "It is NOT expected otherwise.\n"
            f"Update _EXPECTED_TOOL_BLOCK_HASH in this file to: {actual}\n"
            "and say in the PR body what moved. Never hand-edit "
            "contracts/platform-tools.snapshot.json to make this pass."
        )

    def test_every_listed_tool_carries_its_platform_description(self) -> None:
        # The descriptions are the interface: an empty one means a stale snapshot.
        block = format_tool_block()
        for name in tools_at_or_below(Tier.READ):
            description = description_of(name)
            assert description
            assert description.splitlines()[0] in block, name

    def test_every_listed_tool_carries_its_input_schema(self) -> None:
        block = format_tool_block()
        for name in tools_at_or_below(Tier.READ):
            schema = TOOL_REGISTRY[name].input_model.model_json_schema()
            assert f"input_schema={json.dumps(schema, sort_keys=True)}" in block, name


class TestTheOutboxReadingIsOnThePage:
    """v0.6.9's read tool, from the planner's side rather than the registry's."""

    def test_it_is_listed_with_no_arguments(self) -> None:
        block = format_tool_block()
        assert "  - get_outbox_status: " in block
        # `_EmptyInput`: the platform's own input schema has no properties, so
        # the planner is shown a probe it cannot get wrong.
        schema = TOOL_REGISTRY["get_outbox_status"].input_model.model_json_schema()
        assert schema["properties"] == {}
        assert schema["additionalProperties"] is False

    def test_the_description_says_which_clock_and_that_nothing_is_capped(self) -> None:
        # The platform's four normative description rules reached what the agent reads.
        description = description_of("get_outbox_status")
        for phrase in (
            "FRESHNESS AND WHICH CLOCK",
            "NO PAGING, NOTHING CAPPED",
            "UNKNOWN IS NULL, NEVER 0",
            "It takes no arguments.",
        ):
            assert phrase in description, phrase
        # Verbatim on the page, only re-indented: the renderer puts four
        # spaces in front of every continuation line and changes nothing else.
        assert description.replace("\n", "\n    ") in format_tool_block()

    def test_it_says_the_outbox_queue_is_not_consumer_lag(self) -> None:
        # Without it an outbox stall and a consumer stall look the same.
        description = description_of("get_outbox_status")
        assert "not the same queue as consumer lag" in description

    def test_no_lab_vocabulary_reaches_the_page(self) -> None:
        # ADR 0012: `pause_control_loop` must leave no trace in the read tool's prose.
        block = format_tool_block().lower()
        for term in ("chaos", "pause_control_loop", "inject", "seeded"):
            assert term not in block, term
