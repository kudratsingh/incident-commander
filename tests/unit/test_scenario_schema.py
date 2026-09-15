from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from evals.graders.deterministic import ScenarioExpectation
from evals.scenarios.loader import load_scenarios
from evals.scenarios.schema import (
    ChaosHook,
    ChaosPlan,
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


class TestChaosPlan:
    """WP-1.1: many hooks, in order, with their teardown and a settle wait.

    The one-hook world is what all 40 shipped scenarios describe. These
    assert the composable form validates the same way — same closed name
    set, same snapshot argument check, on BOTH halves of the plan — because
    a teardown validated more loosely than a setup is a second, weaker door
    into the same write+chaos principal.
    """

    def test_one_hook_plan_validates(self) -> None:
        plan = ChaosPlan(setup=(ChaosHook(name="saturate_redis"),))
        assert plan.hook_names == ("saturate_redis",)
        assert plan.seeds_chaos is True
        assert plan.teardown == ()
        assert plan.settle_seconds == 0.0

    def test_two_hook_plan_validates_and_keeps_declared_order(self) -> None:
        plan = ChaosPlan(
            setup=(
                ChaosHook(
                    name="kill_consumer",
                    arguments={"consumer_group": "worker-dispatcher", "ttl_seconds": 300},
                ),
                ChaosHook(name="create_stale_cache", arguments={"key": "kafka:lag"}),
            ),
            teardown=(ChaosHook(name="saturate_redis"),),
            settle_seconds=5.0,
        )
        # Order is the declaration, not a set: a cascade's second fault is
        # only the fault it claims to be if the first one landed already.
        assert plan.hook_names == ("kill_consumer", "create_stale_cache", "saturate_redis")
        assert [hook.name for hook in plan.setup] == ["kill_consumer", "create_stale_cache"]

    def test_unknown_hook_name_in_setup_is_rejected_by_name(self) -> None:
        with pytest.raises(ValidationError, match="restart_consumer_group"):
            ChaosPlan(setup=(ChaosHook(name="restart_consumer_group"),))

    def test_unknown_hook_name_in_teardown_is_rejected_by_name(self) -> None:
        # The teardown half goes through the SAME validator. A closed set on
        # setup alone would leave the compensator free to name any tool.
        with pytest.raises(ValidationError, match="list_dlq_messages"):
            ChaosPlan(
                setup=(ChaosHook(name="saturate_redis"),),
                teardown=(ChaosHook(name="list_dlq_messages"),),
            )

    def test_wrong_argument_type_in_teardown_names_the_hook(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            ChaosPlan(
                setup=(ChaosHook(name="saturate_redis"),),
                teardown=(
                    ChaosHook(
                        name="kill_consumer",
                        arguments={"consumer_group": "worker-dispatcher", "ttl_seconds": "300"},
                    ),
                ),
            )
        message = str(excinfo.value)
        assert "kill_consumer.ttl_seconds" in message
        assert "not compatible" in message

    def test_settle_seconds_is_bounded(self) -> None:
        with pytest.raises(ValidationError):
            ChaosPlan(setup=(ChaosHook(name="saturate_redis"),), settle_seconds=-1.0)
        with pytest.raises(ValidationError):
            ChaosPlan(setup=(ChaosHook(name="saturate_redis"),), settle_seconds=301.0)

    def test_plan_is_frozen_and_forbids_extras(self) -> None:
        with pytest.raises(ValidationError):
            ChaosPlan(setup=(ChaosHook(name="saturate_redis"),), settle=1.0)  # type: ignore[call-arg]


class TestScenarioChaosNormalization:
    """A legacy ``chaos_setup`` and a one-hook plan are the same world."""

    def _scenario(self, **extra: Any) -> Scenario:
        return Scenario(
            name="s",
            alert=AlertPayload(source="billing"),
            expectation=ScenarioExpectation(
                name="s", expected_terminal_state=IncidentState.ESCALATED
            ),
            **extra,
        )

    def test_no_chaos_normalizes_to_the_empty_plan(self) -> None:
        # Empty plan, not None: a caller never has to spell "no chaos" twice.
        scenario = self._scenario()
        assert scenario.chaos == ChaosPlan()
        assert scenario.seeds_chaos is False

    def test_legacy_chaos_setup_normalizes_to_a_one_hook_plan(self) -> None:
        hook = ChaosHook(
            name="inject_latency",
            arguments={"consumer_group": "worker-dispatcher", "latency_ms": 2000},
        )
        scenario = self._scenario(chaos_setup=hook)
        assert scenario.chaos == ChaosPlan(setup=(hook,))
        assert scenario.seeds_chaos is True
        # No teardown and no settle — exactly what the runner did for a
        # legacy hook before plans existed.
        assert scenario.chaos.teardown == ()
        assert scenario.chaos.settle_seconds == 0.0

    def test_chaos_plan_is_returned_as_declared(self) -> None:
        plan = ChaosPlan(
            setup=(ChaosHook(name="saturate_redis"), ChaosHook(name="bad_deploy")),
            settle_seconds=2.0,
        )
        scenario = self._scenario(chaos_plan=plan)
        assert scenario.chaos is plan
        assert scenario.seeds_chaos is True
        assert scenario.chaos_setup is None

    def test_declaring_both_spellings_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="both chaos_setup and chaos_plan"):
            self._scenario(
                chaos_setup=ChaosHook(name="saturate_redis"),
                chaos_plan=ChaosPlan(setup=(ChaosHook(name="bad_deploy"),)),
            )

    def test_a_plan_with_no_setup_is_rejected(self) -> None:
        # Teardown-only compensates a fault nobody seeded, and an all-empty
        # plan reads as "seeds chaos" to a human while seeds_chaos is False.
        with pytest.raises(ValidationError, match="no setup hooks"):
            self._scenario(chaos_plan=ChaosPlan(teardown=(ChaosHook(name="saturate_redis"),)))

    def test_a_multi_hook_plan_is_never_smoke_eligible(self) -> None:
        # The predicate reads seeds_chaos, not chaos_setup — the legacy field
        # is None here, and a smoke pass admitting this would seed two faults
        # under the principal the stage exists to prove cannot write.
        scenario = self._scenario(
            chaos_plan=ChaosPlan(
                setup=(ChaosHook(name="saturate_redis"), ChaosHook(name="bad_deploy"))
            )
        )
        assert scenario.smoke_eligible is False
        assert scenario.in_smoke_pass is False

    def test_chaos_plan_loads_from_yaml_shape(self) -> None:
        scenario = Scenario.model_validate(
            {
                "name": "s",
                "alert": {"source": "billing"},
                "expectation": {"name": "s", "expected_terminal_state": "resolved"},
                "chaos_plan": {
                    "setup": [
                        {
                            "name": "kill_consumer",
                            "arguments": {"consumer_group": "worker-dispatcher"},
                        },
                        {"name": "saturate_redis", "arguments": {"num_keys": 10}},
                    ],
                    "teardown": [{"name": "bad_deploy", "arguments": {"label": "restore"}}],
                    "settle_seconds": 3.5,
                },
            }
        )
        assert scenario.chaos.hook_names == ("kill_consumer", "saturate_redis", "bad_deploy")
        assert scenario.chaos.settle_seconds == 3.5


class TestShippedScenariosRoundTripToPlans:
    """Every shipped YAML normalizes to the plan the runner will execute.

    The migration claim, stated over the whole corpus rather than over a
    sample: no YAML changed, and no scenario's seeding changed either — the
    plan derived from each one fires exactly the hook the legacy field named,
    with exactly its arguments.
    """

    def test_every_scenario_normalizes_without_loss(self) -> None:
        scenarios = load_scenarios(_SCENARIOS_DIR)
        assert len(scenarios) >= 40
        for scenario in scenarios:
            plan = scenario.chaos
            if scenario.chaos_setup is None:
                assert plan == ChaosPlan(), scenario.name
                assert scenario.seeds_chaos is False, scenario.name
                continue
            assert plan.setup == (scenario.chaos_setup,), scenario.name
            assert plan.teardown == (), scenario.name
            assert plan.settle_seconds == 0.0, scenario.name
            assert scenario.seeds_chaos is True, scenario.name

    def test_no_shipped_scenario_declares_a_plan_yet(self) -> None:
        # The migration is additive and nothing has moved: this is the
        # statement that the round-trip above covers the WHOLE corpus rather
        # than the legacy half of a half-migrated one.
        assert [s.name for s in load_scenarios(_SCENARIOS_DIR) if s.chaos_plan is not None] == []

    def test_seeds_chaos_agrees_with_the_legacy_field_everywhere(self) -> None:
        # The gates moved from `chaos_setup is not None` to `seeds_chaos`.
        # Over today's corpus the two must be the same predicate, or the
        # smoke derivation and the ADR 0020 selection just changed silently.
        for scenario in load_scenarios(_SCENARIOS_DIR):
            assert scenario.seeds_chaos is (scenario.chaos_setup is not None), scenario.name
