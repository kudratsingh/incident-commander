from typing import Any

import pytest
from pydantic import ValidationError

from evals.graders.deterministic import ScenarioExpectation
from evals.scenarios.schema import (
    ChaosHook,
    Scenario,
    _chaos_names_from_snapshot,
    chaos_tool_names,
)
from incident_commander.agent.state import IncidentState
from incident_commander.api.schemas import AlertPayload


class TestScenario:
    def test_minimal_scenario_validates(self) -> None:
        scenario = Scenario(
            name="s",
            alert=AlertPayload(source="billing"),
            expectation=ScenarioExpectation(
                name="s", expected_terminal_state=IncidentState.ESCALATED
            ),
        )
        assert scenario.name == "s"
        assert scenario.tags == ()
        assert scenario.description == ""

    def test_empty_name_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Scenario(
                name="",
                alert=AlertPayload(source="billing"),
                expectation=ScenarioExpectation(
                    name="s", expected_terminal_state=IncidentState.ESCALATED
                ),
            )

    def test_extra_fields_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Scenario.model_validate(
                {
                    "name": "s",
                    "alert": {"source": "billing"},
                    "expectation": {
                        "name": "s",
                        "expected_terminal_state": "escalated",
                    },
                    "unknown_key": "boom",
                }
            )

    def test_frozen_mutation_rejected(self) -> None:
        scenario = Scenario(
            name="s",
            alert=AlertPayload(source="billing"),
            expectation=ScenarioExpectation(
                name="s", expected_terminal_state=IncidentState.ESCALATED
            ),
        )
        with pytest.raises(ValidationError):
            scenario.name = "changed"

    def test_chaos_setup_defaults_to_none(self) -> None:
        scenario = Scenario(
            name="s",
            alert=AlertPayload(source="billing"),
            expectation=ScenarioExpectation(
                name="s", expected_terminal_state=IncidentState.ESCALATED
            ),
        )
        assert scenario.chaos_setup is None

    def test_chaos_setup_loads_from_yaml_shape(self) -> None:
        scenario = Scenario.model_validate(
            {
                "name": "s",
                "alert": {"source": "billing"},
                "expectation": {
                    "name": "s",
                    "expected_terminal_state": "resolved",
                },
                "chaos_setup": {
                    "name": "inject_latency",
                    "arguments": {"consumer_group": "wd", "latency_ms": 2000},
                },
            }
        )
        assert scenario.chaos_setup is not None
        assert scenario.chaos_setup.name == "inject_latency"
        assert scenario.chaos_setup.arguments["latency_ms"] == 2000


class TestScenarioExpectation:
    def test_singular_expected_action_tool_is_rejected(self) -> None:
        """A-16: the stale documented name is not an alias — it fails the load.

        ``ScenarioExpectation`` is ``extra="forbid"``, so a scenario author who
        copies the singular ``expected_action_tool`` out of a doc gets a load
        failure rather than a silently ungraded ACTION dimension. The correct
        field is ``expected_action_tools``, a list of equivalent Tier-1 tools.
        """
        with pytest.raises(ValidationError, match="expected_action_tool"):
            ScenarioExpectation.model_validate(
                {
                    "name": "s",
                    "expected_terminal_state": "resolved",
                    "expected_action_tool": "restart_consumer_group",
                }
            )

    def test_plural_expected_action_tools_is_the_supported_name(self) -> None:
        """The counterpart to the rejection: the plural loads as a tuple."""
        expectation = ScenarioExpectation.model_validate(
            {
                "name": "s",
                "expected_terminal_state": "resolved",
                "expected_action_tools": ["replay_dlq_by_ids", "replay_dlq_by_category"],
            }
        )
        assert expectation.expected_action_tools == (
            "replay_dlq_by_ids",
            "replay_dlq_by_category",
        )


class TestChaosHook:
    def test_empty_name_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ChaosHook(name="")

    def test_extra_fields_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ChaosHook.model_validate({"name": "kill_consumer", "unexpected": 1})


def _snapshot_payload(*tools: tuple[str, str]) -> dict[str, Any]:
    """A synthetic tools/list payload: (name, description) pairs."""
    return {"tools": [{"name": name, "description": desc} for name, desc in tools]}


class TestChaosHookClosedSet:
    """S-03: a chaos hook name is a closed set, not an arbitrary string.

    ``chaos_setup`` is fired by the runner under ``settings.platform_token``
    — the FULL write+chaos principal — and ``ChaosClient.call`` forwards the
    name verbatim as a ``tools/call``. An unconstrained ``str`` therefore
    lets any scenario YAML execute any platform tool under that principal.
    """

    def test_tier1_write_tool_name_rejected(self) -> None:
        # The exact S-03 attack: a Tier-1 write crossing the chaos boundary.
        with pytest.raises(ValidationError, match="not a chaos tool"):
            ChaosHook(name="replay_dlq_by_category")

    def test_unknown_tool_name_rejected(self) -> None:
        with pytest.raises(ValidationError, match="not a chaos tool"):
            ChaosHook(name="definitely_not_a_tool")

    def test_read_tool_name_rejected(self) -> None:
        # Even a harmless read is out: chaos_setup is for seeding chaos.
        with pytest.raises(ValidationError, match="not a chaos tool"):
            ChaosHook(name="get_consumer_lag")

    @pytest.mark.parametrize("name", sorted(chaos_tool_names()))
    def test_every_allowed_chaos_tool_validates(self, name: str) -> None:
        assert ChaosHook(name=name).name == name

    def test_allowed_set_is_the_snapshots_chaos_tools(self) -> None:
        # The seven the pinned v0.4.9 platform registers. Pinned as a subset,
        # not an equality, so a later pin bump that adds a chaos tool widens
        # the set without a test edit — the exclusions below are the closure.
        assert {
            "bad_deploy",
            "create_bad_data_job",
            "create_stale_cache",
            "inject_latency",
            "kill_consumer",
            "poison_message",
            "saturate_redis",
        } <= chaos_tool_names()

    def test_no_non_chaos_snapshot_tool_leaks_in(self) -> None:
        for name in ("get_consumer_lag", "replay_dlq_messages", "pause_dag", "list_incidents"):
            assert name not in chaos_tool_names()

    def test_seed_dlq_messages_excluded_even_when_the_snapshot_carries_it(self) -> None:
        # Cross-repo rule: seed_dlq_messages stays out of the commander —
        # not in TOOL_REGISTRY, not in ChaosHook usage, not in scenarios. It
        # is deferred, flag-off platform work, and the post-campaign rebless
        # will put it INTO the snapshot. The exclusion is by construction so
        # that rebless cannot silently widen this closed set.
        allowed = _chaos_names_from_snapshot(
            _snapshot_payload(
                ("kill_consumer", "[chaos: single_consumer] shut one down"),
                ("seed_dlq_messages", "[chaos: environment_wide] seed N rows"),
            )
        )
        assert allowed == frozenset({"kill_consumer"})

    def test_a_future_chaos_tool_joins_the_set(self) -> None:
        # A 27th tool must not break the derivation: anything the platform
        # registers as chaos (and does not defer) is legal by construction.
        allowed = _chaos_names_from_snapshot(
            _snapshot_payload(
                ("kill_consumer", "[chaos: single_consumer] shut one down"),
                ("freeze_clock", "[chaos: environment_wide] stop time"),
                ("get_consumer_lag", "Read the last-emitted Kafka consumer lag"),
            )
        )
        assert allowed == frozenset({"kill_consumer", "freeze_clock"})

    def test_error_message_names_the_allowed_set(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            ChaosHook(name="replay_dlq_by_category")
        message = str(excinfo.value)
        assert "kill_consumer" in message
        assert "make snapshot" in message

    def test_shipped_scenario_chaos_names_are_all_members(self) -> None:
        # Regression against the audit's hand-list, which omitted
        # create_stale_cache and misspelled create_bad_data_job: adopting it
        # would have broken a shipped scenario at load time.
        for name in ("inject_latency", "create_stale_cache", "create_bad_data_job"):
            assert ChaosHook(name=name).name == name
