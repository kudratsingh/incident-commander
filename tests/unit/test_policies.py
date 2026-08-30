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
from incident_commander.agent.remediation import (
    RemediationPlan,
    Tier1ToolName,
    _absent_resource_args,
)
from incident_commander.tools.policies import (
    RESOURCE_ARG_FIELDS,
    Tier,
    ensure_covered,
    tier_of,
    tools_at_or_below,
)
from incident_commander.tools.registry import TOOL_REGISTRY

# Every (tool, resource-naming field) pair the policy map declares. Driven
# off RESOURCE_ARG_FIELDS rather than hand-listed so a newly classified
# field is covered the moment it is added.
_RESOURCE_ARG_ENTRIES = sorted(
    (tool, field) for tool, fields in RESOURCE_ARG_FIELDS.items() for field in fields
)

# Resource-free stand-ins, one per leg, so the leg under test is the only
# source of findings. Asserted to be resource-free by
# ``test_plan_scaffold_tools_name_no_resources``.
_FILLER_ACTION_TOOL = "replay_dlq_by_category"
_FILLER_VERIFY_TOOL = "list_dlq_messages"


def _plan_omitting(tool: str, field: str) -> dict[str, object]:
    """A plan placing ``tool`` on its tier's leg with ``field`` left out.

    Tier decides the leg: ``RemediationPlan.action_tool`` is Literal-typed
    to Tier-1 names and ``verify_tool`` to read names, so a tool can only
    be exercised on the leg its tier allows. Any other resource fields on
    the same tool are filled, so the omission under test is the only one.
    """
    present = {f: f"placeholder-{f}" for f in RESOURCE_ARG_FIELDS[tool] if f != field}
    on_action = tier_of(tool) is Tier.TIER_1
    return {
        "target_hypothesis": "h",
        "action_tool": tool if on_action else _FILLER_ACTION_TOOL,
        "action_arguments": present if on_action else {},
        "verify_tool": _FILLER_VERIFY_TOOL if on_action else tool,
        "verify_arguments": {} if on_action else present,
        "verify_expectation": "e",
    }


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

    def test_plan_scaffold_tools_name_no_resources(self) -> None:
        # The parametrized test below is only meaningful if the filler
        # leg contributes no findings of its own.
        assert not RESOURCE_ARG_FIELDS[_FILLER_ACTION_TOOL]
        assert not RESOURCE_ARG_FIELDS[_FILLER_VERIFY_TOOL]

    @pytest.mark.parametrize(("tool", "field"), _RESOURCE_ARG_ENTRIES)
    def test_omitting_any_resource_field_is_a_planning_violation(
        self, tool: str, field: str
    ) -> None:
        """WO-R2-15 / ADR 0022: absence is as loud as mis-sourcing.

        The registry hole was narrow — ``get_consumer_lag.consumer_group``
        was the one resource-naming field with a default, so omitting it
        got silently default-filled by ``wire_arguments`` instead of
        refused. This test does not care which fields carry defaults: it
        walks every ``RESOURCE_ARG_FIELDS`` entry and asserts the plan
        layer refuses a leg that leaves it out. A future field that turns
        optional cannot reopen the hole without failing here.
        """
        plan = RemediationPlan.model_validate(_plan_omitting(tool, field))
        problems = _absent_resource_args(plan)

        assert any(p.endswith(f"{tool}.{field}") for p in problems), (
            f"{tool}.{field} is classified as resource-naming in "
            f"RESOURCE_ARG_FIELDS, but a plan that omits it is not "
            f"rejected: _absent_resource_args returned {problems}. An "
            f"unnamed resource argument is default-filled or fails at "
            f"wire time — after the Tier-1 action has run."
        )

    def test_default_carrying_resource_fields_are_the_known_inventory(self) -> None:
        """Pins WHICH resource fields the platform lets us omit.

        Every entry in this set is a field where a plan's silence becomes
        a concrete resource name chosen by the platform's input schema
        rather than by the incident — the WO-R2-15 shape exactly. The
        fix lives at the plan layer precisely because these defaults are
        legitimate: they mirror the platform's published schema, which
        ``test_registry_matches_snapshot.py`` holds to exact equality.

        If this set grows, that is not automatically a bug — but the new
        field must be deliberate, and the parametrized test above must
        cover it. Do not "fix" a failure here by deleting the registry
        default; that breaks the contract snapshot test instead.
        """
        from incident_commander.tools.policies import RESOURCE_ARG_FIELDS
        from incident_commander.tools.registry import TOOL_REGISTRY

        optional = {
            (tool, field)
            for tool, fields in RESOURCE_ARG_FIELDS.items()
            for field in fields
            if not TOOL_REGISTRY[tool].input_model.model_fields[field].is_required()
        }
        assert optional == {("get_consumer_lag", "consumer_group")}
