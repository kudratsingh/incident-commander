"""Tier-policy classification tests.

The policy module is the agent-side first filter deciding what the
investigation planner vs the remediation planner may propose.
Wave 3 PR F on the platform side will add Tier-2 approval objects;
until then ``_TIER_2_TOOLS`` is empty and ``TIER_2`` classification
is a schema hook, not a live path.
"""

from __future__ import annotations

import typing

import pytest

from incident_commander.agent.hypothesis import ReadToolName
from incident_commander.agent.remediation import Tier1ToolName
from incident_commander.tools.policies import (
    Tier,
    ensure_covered,
    tier_of,
    tools_at_or_below,
)
from incident_commander.tools.registry import TOOL_REGISTRY


class TestTierOf:
    @pytest.mark.parametrize(
        "tool_name",
        [
            "restart_consumer_group",
            "pause_dag",
            "replay_dlq_messages",
            "invalidate_cache_key",
        ],
    )
    def test_write_actions_are_tier_1(self, tool_name: str) -> None:
        assert tier_of(tool_name) is Tier.TIER_1

    @pytest.mark.parametrize(
        "tool_name",
        [
            "get_consumer_lag",
            "list_dlq_messages",
            "get_redis_health",
            "search_traces",
            "list_incidents",
        ],
    )
    def test_read_tools_are_read(self, tool_name: str) -> None:
        assert tier_of(tool_name) is Tier.READ

    def test_unknown_tool_raises(self) -> None:
        with pytest.raises(KeyError, match="unknown tool"):
            tier_of("not_a_real_tool")


class TestToolsAtOrBelow:
    def test_read_returns_only_read_tools(self) -> None:
        read_only = tools_at_or_below(Tier.READ)
        assert "get_consumer_lag" in read_only
        assert "restart_consumer_group" not in read_only
        # Every returned tool must classify as READ.
        for name in read_only:
            assert tier_of(name) is Tier.READ

    def test_tier_1_returns_read_plus_tier_1(self) -> None:
        allowed = tools_at_or_below(Tier.TIER_1)
        assert "get_consumer_lag" in allowed  # read still allowed
        assert "restart_consumer_group" in allowed
        assert "invalidate_cache_key" in allowed

    def test_tier_2_returns_everything_currently_registered(self) -> None:
        assert tools_at_or_below(Tier.TIER_2) == frozenset(TOOL_REGISTRY)


class TestEnsureCovered:
    def test_every_registered_tool_has_a_tier(self) -> None:
        # If someone adds a tool to the registry without touching policies,
        # this fails — that's the whole point.
        ensure_covered()


class TestLiteralRegistryDrift:
    """The hand-listed Literals must track the tier map (B-06).

    ``ReadToolName`` (hypothesis.py) and ``Tier1ToolName`` (remediation.py)
    are the schema half of the LLM-boundary guard: Pydantic needs literal
    strings at import time, so they cannot be generated from the registry.
    These are the drift tripwires both files' comments promise — they fail
    the day a tool is added, removed, or reclassified in ``policies.py``
    without regenerating the Literal.
    """

    def test_read_tool_name_literal_matches_read_tier(self) -> None:
        assert set(typing.get_args(ReadToolName)) == tools_at_or_below(Tier.READ)

    def test_tier1_tool_name_literal_matches_tier1_slice(self) -> None:
        assert set(typing.get_args(Tier1ToolName)) == tools_at_or_below(
            Tier.TIER_1
        ) - tools_at_or_below(Tier.READ)


class TestResourceArgFieldsCoverage:
    def test_every_registry_tool_is_classified(self) -> None:
        # A new tool must classify its resource-naming fields (possibly
        # empty) or the evidence-sourcing validator silently skips it.
        from incident_commander.tools.policies import RESOURCE_ARG_FIELDS
        from incident_commander.tools.registry import TOOL_REGISTRY

        assert set(RESOURCE_ARG_FIELDS) == set(TOOL_REGISTRY)

    def test_classified_fields_exist_on_input_models(self) -> None:
        from incident_commander.tools.policies import RESOURCE_ARG_FIELDS
        from incident_commander.tools.registry import TOOL_REGISTRY

        for tool, fields in RESOURCE_ARG_FIELDS.items():
            model_fields = set(TOOL_REGISTRY[tool].input_model.model_fields)
            missing = fields - model_fields
            assert not missing, f"{tool}: {missing} not on input model"
