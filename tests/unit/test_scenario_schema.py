from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from evals.graders.deterministic import ScenarioExpectation
from evals.scenarios.loader import load_scenarios
from evals.scenarios.schema import (
    ChaosHook,
    Scenario,
    _chaos_names_from_snapshot,
    chaos_argument_errors,
    chaos_tool_names,
    chaos_tool_schemas,
    json_types_for,
)
from incident_commander.agent.state import IncidentState
from incident_commander.api.schemas import AlertPayload

_SCENARIOS_DIR = Path(__file__).resolve().parents[2] / "evals" / "scenarios"


def _shipped_scenarios_with_chaos() -> list[Scenario]:
    return [s for s in load_scenarios(_SCENARIOS_DIR) if s.chaos_setup is not None]


# A value of each JSON type, for synthesizing an argument the snapshot admits.
_SAMPLE_BY_JSON_TYPE: dict[str, Any] = {
    "string": "x",
    "integer": 1,
    "number": 1.0,
    "boolean": True,
    "object": {},
    "array": [],
    "null": None,
}


def _minimal_arguments(hook: str) -> dict[str, Any]:
    """The smallest argument set the snapshot accepts for ``hook``.

    Derived from the snapshot's ``required`` list and each property's declared
    JSON type, never hand-listed: a hook that gains a required argument on a
    future platform pin keeps working here, and one whose argument type flips
    fails in ``test_chaos_schema_alignment.py`` where that is the subject.
    """
    schema = chaos_tool_schemas()[hook]
    properties = schema.get("properties") or {}
    arguments: dict[str, Any] = {}
    for field in schema.get("required") or []:
        admitted = json_types_for(properties.get(field))
        # Prefer a non-null sample: `null` is last-resort for a property whose
        # only declared branch is null, and a required field is never that.
        for json_type in sorted(admitted - {"null"}) or sorted(admitted):
            arguments[str(field)] = _SAMPLE_BY_JSON_TYPE[json_type]
            break
    return arguments


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
        assert ChaosHook(name=name, arguments=_minimal_arguments(name)).name == name

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
            assert ChaosHook(name=name, arguments=_minimal_arguments(name)).name == name


class TestChaosHookArgumentClosure:
    """G1-07: the arguments are a closed set too, against the same snapshot entry.

    The name closure (#116) left half of every chaos invocation unchecked.
    ``ChaosClient.call`` forwards ``arguments`` verbatim to ``tools/call``
    under the full write+chaos principal, and every chaos ``inputSchema``
    declares ``additionalProperties: false`` — so a typo'd argument name, a
    missing required one, or a flipped value type is a guaranteed live
    ``ChaosInvocationError``. Before this closure all three validated happily
    at load time and surfaced only during seeding: mid-campaign, after the
    platform had been touched and after run startup was already paid for.
    """

    def test_missing_required_argument_rejected(self) -> None:
        # inject_latency requires consumer_group AND latency_ms.
        with pytest.raises(ValidationError, match="missing required argument"):
            ChaosHook(name="inject_latency")

    def test_missing_one_of_two_required_arguments_rejected(self) -> None:
        with pytest.raises(ValidationError, match=r"missing required argument.*latency_ms"):
            ChaosHook(name="inject_latency", arguments={"consumer_group": "worker-dispatcher"})

    def test_typo_in_an_argument_name_rejected(self) -> None:
        with pytest.raises(ValidationError, match=r"unknown argument.*consumer_grp"):
            ChaosHook(
                name="kill_consumer",
                arguments={"consumer_grp": "worker-dispatcher", "ttl_seconds": 300},
            )

    def test_flipped_argument_type_rejected(self) -> None:
        # The S-18 shape: ttl_seconds integer→string. Caught at load, not at
        # seeding — tests/unit/test_chaos_schema_alignment.py pins the same
        # flip for the shipped invocations.
        with pytest.raises(ValidationError, match="not compatible"):
            ChaosHook(
                name="kill_consumer",
                arguments={"consumer_group": "worker-dispatcher", "ttl_seconds": "300"},
            )

    def test_bool_is_not_accepted_for_an_integer_argument(self) -> None:
        # bool is a subclass of int in Python; JSON "integer" does not admit
        # true/false on the platform side.
        with pytest.raises(ValidationError, match="not compatible"):
            ChaosHook(
                name="kill_consumer",
                arguments={"consumer_group": "worker-dispatcher", "ttl_seconds": True},
            )

    def test_explicit_null_on_a_nullable_argument_stays_legal(self) -> None:
        # bad_deploy.note is anyOf[string, null]; the CLI sends None by default.
        hook = ChaosHook(name="bad_deploy", arguments={"note": None})
        assert hook.arguments["note"] is None

    def test_null_on_a_non_nullable_argument_rejected(self) -> None:
        with pytest.raises(ValidationError, match="not compatible"):
            ChaosHook(name="kill_consumer", arguments={"consumer_group": None})

    def test_all_optional_hook_accepts_no_arguments(self) -> None:
        # saturate_redis declares defaults for everything; an empty block is
        # a legal invocation and must stay one.
        assert ChaosHook(name="saturate_redis").arguments == {}

    def test_error_message_points_at_the_snapshot_not_a_hand_edit(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            ChaosHook(name="inject_latency")
        message = str(excinfo.value)
        assert "platform-tools.snapshot.json" in message
        assert "make snapshot" in message
        assert "Never hand-edit" in message

    def test_every_shipped_scenario_chaos_block_is_well_formed(self) -> None:
        # The shipped four load through the real loader elsewhere; this is the
        # direct statement that the new closure admits every one of them, so a
        # future author reading this file sees the closure is not vacuous.
        for scenario in _shipped_scenarios_with_chaos():
            assert scenario.chaos_setup is not None
            assert (
                chaos_argument_errors(scenario.chaos_setup.name, scenario.chaos_setup.arguments)
                == []
            )
