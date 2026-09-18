import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from evals.graders.deterministic import ScenarioExpectation
from evals.scenarios.loader import load_scenarios
from evals.scenarios.schema import (
    ChaosHook,
    ChaosPlan,
    DiscriminatingProbe,
    GroundTruth,
    Scenario,
    _chaos_names_from_snapshot,
    _SNAPSHOT_PATH,
    chaos_argument_errors,
    chaos_tool_names,
    chaos_tool_schemas,
    enum_values_for,
    json_types_for,
    resolve_schema_ref,
)
from incident_commander.agent.hypothesis import HypothesisCategory
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

    Each property is resolved through ``resolve_schema_ref`` first, and a
    closed set is sampled from its own members rather than from the type
    table — ``pause_control_loop.loop_name`` (v0.6.9) is a ``$ref``'d enum,
    so ``"x"`` is the right JSON type and still not a legal value.
    """
    schema = chaos_tool_schemas()[hook]
    properties = schema.get("properties") or {}
    arguments: dict[str, Any] = {}
    for field in schema.get("required") or []:
        resolved = resolve_schema_ref(properties.get(field), schema)
        members = enum_values_for(resolved)
        if members:
            arguments[str(field)] = members[0]
            continue
        admitted = json_types_for(resolved)
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


class TestPauseControlLoopIsDeclarable:
    """v0.6.9's 11th chaos hook, proven declarable without shipping a scenario.

    WP-4.3 is what adds the outbox family; this packet only re-pins. But the
    closed set is derived from the snapshot at load time, so "a scenario
    could declare it" is a property of THIS commit and testable here — and
    the interesting half is the argument, not the name: ``loop_name`` is the
    first chaos argument the platform expresses as a ``$ref`` into
    ``$defs``, and before ``resolve_schema_ref`` the checker read a property
    that declared nothing and so admitted everything.

    No scenario YAML is added. ``test_shipped_scenario_chaos_names_are_all_members``
    above is the other direction and stays as it is.
    """

    def test_the_hook_is_in_the_closed_set(self) -> None:
        assert "pause_control_loop" in chaos_tool_names()

    def test_a_scenario_could_declare_it(self) -> None:
        hook = ChaosHook(
            name="pause_control_loop",
            arguments={"loop_name": "outbox_relay", "ttl_seconds": 120},
        )
        assert hook.name == "pause_control_loop"
        assert hook.arguments["loop_name"] == "outbox_relay"

    def test_ttl_is_optional_and_the_loop_name_is_not(self) -> None:
        assert ChaosHook(name="pause_control_loop", arguments={"loop_name": "metrics"})
        with pytest.raises(ValidationError, match="missing required argument"):
            ChaosHook(name="pause_control_loop", arguments={"ttl_seconds": 60})

    def test_a_loop_name_outside_the_enum_is_rejected(self) -> None:
        # Before the `$ref` hop this validated, and the scenario failed at
        # live seeding time with a platform 400 — after the run had started.
        with pytest.raises(ValidationError, match="closed set"):
            ChaosHook(name="pause_control_loop", arguments={"loop_name": "not_a_loop"})

    def test_a_loop_name_of_the_wrong_type_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="compatible with the snapshot"):
            ChaosHook(name="pause_control_loop", arguments={"loop_name": 7})

    def test_every_loop_the_platform_offers_is_declarable(self) -> None:
        schema = chaos_tool_schemas()["pause_control_loop"]
        properties = schema["properties"]
        members = enum_values_for(resolve_schema_ref(properties["loop_name"], schema))
        assert members is not None
        # The platform ships eleven background loops (plat #210). Asserted as
        # a count plus two anchors rather than a copied list: the list belongs
        # to the snapshot, and a twelfth loop is a platform release, not a
        # test edit.
        assert len(members) == 11
        assert {"outbox_relay", "slo_evaluation"} <= set(members)
        for loop in members:
            assert ChaosHook(name="pause_control_loop", arguments={"loop_name": loop})

    def test_the_hook_never_reaches_the_agents_typed_registry(self) -> None:
        # A chaos tool is seeded by the evaluator under `chaos:invoke`; the
        # agent's own principal cannot call it and its name must not appear
        # in the registry the planner picks from (ADR 0012).
        from incident_commander.tools.registry import TOOL_REGISTRY

        assert "pause_control_loop" not in TOOL_REGISTRY


class TestPauseDagChaosIsDeclarable:
    """v0.6.10's 12th chaos hook, proven declarable without shipping a scenario.

    WP-7.2 is what adds the `workflow_stuck` family; this packet only
    re-pins. But the closed set is derived from the snapshot at load time, so
    "a scenario could declare it" is a property of THIS commit and testable
    here — the same shape as ``TestPauseControlLoopIsDeclarable`` above.

    The hook writes the same ``dag:paused:<root>`` flag the Tier-1 action
    ``pause_dag`` writes, so ``get_dag_state`` reads ``paused: true`` exactly
    as after an operator pause (platform ADR 0029). Nothing here is
    agent-facing: the pause the agent can see is indistinguishable from an
    operator's, and the hook that set it is not in the agent's registry.

    No scenario YAML is added.
    """

    def test_the_hook_is_in_the_closed_set(self) -> None:
        assert "pause_dag_chaos" in chaos_tool_names()

    def test_a_scenario_could_declare_it(self) -> None:
        hook = ChaosHook(
            name="pause_dag_chaos",
            arguments=_minimal_arguments("pause_dag_chaos"),
        )
        assert hook.name == "pause_dag_chaos"

    def test_the_hook_is_marked_chaos_and_scoped_to_the_evaluator(self) -> None:
        # Selected into the closed set by the structural `[chaos:` prefix, not
        # by a hand-list — and the scope is what keeps the agent out.
        entry = next(
            t
            for t in json.loads(_SNAPSHOT_PATH.read_text())["tools"]
            if t["name"] == "pause_dag_chaos"
        )
        assert entry["description"].startswith("[chaos: environment_wide]")
        assert entry["required_scope"] == "chaos:invoke"

    def test_the_hook_never_reaches_the_agents_typed_registry(self) -> None:
        # ADR 0012: the lab's own name must not appear in the registry the
        # planner picks from. `pause_dag` (the Tier-1 action) still does.
        from incident_commander.tools.registry import TOOL_REGISTRY

        assert "pause_dag_chaos" not in TOOL_REGISTRY
        assert "pause_dag" in TOOL_REGISTRY


class TestStrandedChainIsDeclarable:
    """v0.6.10's ``create_stuck_dag`` gains three optional inputs (plat #214).

    ``root_status: completed`` + ``child_age_seconds`` + ``failed_step`` are
    what make the `workflow_stuck` family's three-way contrast manufacturable
    (WO-R3-274): a chain whose root COMPLETED and whose descendants are still
    `waiting`, backdated, with no dead-letter row at all. Proven declarable
    here; the family itself is WP-7.2's.

    The other half of the proof is that the defaults did not move. ADR 0043
    keys a recorded world by the wired arguments of the hooks that made it, so
    a new optional argument that changed a default — or that a scenario had to
    start passing — would move every committed recording's key. The two
    shipped `create_stuck_dag` scenarios pass three arguments each, and they
    still validate byte-identically.
    """

    def test_the_three_new_arguments_are_declarable_together(self) -> None:
        hook = ChaosHook(
            name="create_stuck_dag",
            arguments={
                "chain_name": "resolver-stall-eval",
                "waiting_steps": 3,
                "root_status": "completed",
                "child_age_seconds": 2820,
                "failed_step": 2,
            },
        )
        assert hook.arguments["root_status"] == "completed"
        assert hook.arguments["child_age_seconds"] == 2820
        assert hook.arguments["failed_step"] == 2

    def test_root_status_is_a_closed_set_holding_both_worlds(self) -> None:
        schema = chaos_tool_schemas()["create_stuck_dag"]
        members = enum_values_for(resolve_schema_ref(schema["properties"]["root_status"], schema))
        assert members is not None
        assert {"dead_letter", "completed"} <= set(members)

    def test_a_root_status_outside_the_closed_set_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="closed set"):
            ChaosHook(name="create_stuck_dag", arguments={"root_status": "waiting"})

    def test_the_backdate_and_the_failed_step_are_integer_typed(self) -> None:
        schema = chaos_tool_schemas()["create_stuck_dag"]
        for argument in ("child_age_seconds", "failed_step"):
            admitted = json_types_for(resolve_schema_ref(schema["properties"][argument], schema))
            assert "integer" in admitted, argument
            with pytest.raises(ValidationError, match="compatible with the snapshot"):
                ChaosHook(name="create_stuck_dag", arguments={argument: "large"})

    def test_all_three_are_optional_so_no_wired_argument_has_to_move(self) -> None:
        required = chaos_tool_schemas()["create_stuck_dag"].get("required") or []
        assert not ({"root_status", "child_age_seconds", "failed_step"} & set(required))
        # The two shipped scenarios' wired arguments, verbatim from their YAML.
        # If a new input were required, these would stop validating and every
        # recording keyed on them (ADR 0043) would need a new key.
        for chain in ("runaway-saga-eval", "saga-stuck-eval"):
            assert ChaosHook(
                name="create_stuck_dag",
                arguments={
                    "chain_name": chain,
                    "waiting_steps": 2,
                    "remediation_hint": "replay_safe",
                },
            )

    def test_the_stranded_chains_refusal_is_already_ledgered(self) -> None:
        # A repeat call with a different shape under the same chain_name is
        # refused `stuck_chain_name_in_use` — the code the hook already
        # raised for a drifted chain, so no new refusal joins the ledger.
        from evals.chaos_hooks import _REFUSAL_MEANINGS

        assert "stuck_chain_name_in_use" in _REFUSAL_MEANINGS


class TestSchemaRefResolution:
    """``$ref`` is followed exactly one hop, and only into local ``$defs``."""

    def test_a_ref_is_merged_with_the_property_keeping_its_own_keys(self) -> None:
        schema: dict[str, Any] = {
            "$defs": {"Loop": {"enum": ["a", "b"], "type": "string", "description": "shared"}},
            "properties": {"loop": {"$ref": "#/$defs/Loop", "description": "per-use"}},
        }
        resolved = resolve_schema_ref(schema["properties"]["loop"], schema)
        assert resolved == {
            "enum": ["a", "b"],
            "type": "string",
            "description": "per-use",
        }

    def test_an_unresolvable_ref_comes_back_unchanged(self) -> None:
        for prop, schema in (
            ({"$ref": "#/$defs/Missing"}, {"$defs": {}}),
            ({"$ref": "#/$defs/Loop"}, {}),
            ({"$ref": "https://example.test/Loop"}, {"$defs": {"Loop": {"type": "string"}}}),
        ):
            assert resolve_schema_ref(prop, schema) == prop

    def test_a_property_with_no_ref_is_untouched(self) -> None:
        prop = {"type": "integer", "maximum": 3600}
        assert resolve_schema_ref(prop, {}) == prop

    def test_a_ref_inside_anyof_is_resolved(self) -> None:
        schema = {"$defs": {"Loop": {"enum": ["a"], "type": "string"}}}
        prop = {"anyOf": [{"$ref": "#/$defs/Loop"}, {"type": "null"}]}
        resolved = resolve_schema_ref(prop, schema)
        assert json_types_for(resolved) == frozenset({"string", "null"})
        assert enum_values_for(resolved) == ("a", None)

    def test_an_unclosed_branch_makes_the_whole_property_unclosed(self) -> None:
        # The union of the enum branches would be a NARROWER claim than the
        # schema makes, and a check that rejects a legal value is the worse
        # failure — so nothing is asserted at all.
        assert enum_values_for({"anyOf": [{"enum": ["a"]}, {"type": "string"}]}) is None

    def test_a_property_with_no_enum_is_unclosed(self) -> None:
        assert enum_values_for({"type": "string"}) is None
        assert enum_values_for(None) is None


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


class TestGroundTruth:
    """The evaluator's record of what was actually wrong (WP-1.3, plan 01 § 5).

    Loader-level validation only here; that it never reaches the agent is
    ``tests/unit/test_ground_truth_never_leaks.py``, which is the half of
    this packet that carries the weight.
    """

    def test_a_scenario_ships_without_one(self) -> None:
        """Optional, and 41 scenarios predate it. Absence is not an error."""
        scenario = Scenario(
            name="s",
            alert=AlertPayload(source="billing"),
            expectation=ScenarioExpectation(
                name="s", expected_terminal_state=IncidentState.ESCALATED
            ),
        )
        assert scenario.ground_truth is None
        assert scenario.discriminating_probes == ()
        assert scenario.root_cause_graded is False

    def test_it_loads_from_the_yaml_shape_the_plan_documents(self) -> None:
        scenario = Scenario.model_validate(
            {
                "name": "s",
                "alert": {"source": "billing"},
                "expectation": {"name": "s", "expected_terminal_state": "escalated"},
                "ground_truth": {
                    "incident_count": 1,
                    "root_causes": ["outbox_stall"],
                    "affected_components": ["outbox_relay", "worker_dispatcher"],
                    "causal_chain": ["outbox_stall", "jobs_pending", "no_execution"],
                },
                "discriminating_probes": [
                    {"tool": "list_dlq_messages", "argument_pattern": {"category": "^wait.*"}}
                ],
            }
        )
        assert scenario.root_cause_graded is True
        assert scenario.ground_truth is not None
        assert scenario.ground_truth.root_causes == (HypothesisCategory.OUTBOX_STALL,)
        assert scenario.ground_truth.affected_components == ("outbox_relay", "worker_dispatcher")
        assert scenario.ground_truth.causal_chain[0] == "outbox_stall"
        assert scenario.discriminating_probes[0].tool == "list_dlq_messages"

    def test_an_unknown_root_cause_label_is_rejected(self) -> None:
        """The label is the enum the agent classifies into, or it is nothing.

        A free-string root cause could never be compared with a
        ``Hypothesis.category``, so the root-cause grader would be matching
        prose. ``StrEnum`` makes the typo a load error instead.
        """
        with pytest.raises(ValidationError, match="root_causes"):
            GroundTruth.model_validate({"incident_count": 1, "root_causes": ["outbox_stalled"]})

    def test_every_taxonomy_label_is_admissible(self) -> None:
        """No category is unreachable — including the WP-1.6 additions."""
        for category in HypothesisCategory:
            count = 0 if category is HypothesisCategory.NO_FAULT else 1
            assert GroundTruth(incident_count=count, root_causes=(category,)).root_causes == (
                category,
            )

    def test_an_empty_root_cause_list_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            GroundTruth(incident_count=1, root_causes=())

    def test_a_repeated_root_cause_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="repeats a label"):
            GroundTruth(
                incident_count=2,
                root_causes=(
                    HypothesisCategory.OUTBOX_STALL,
                    HypothesisCategory.OUTBOX_STALL,
                ),
            )

    def test_no_fault_cannot_sit_beside_a_fault(self) -> None:
        with pytest.raises(ValidationError, match="whole answer"):
            GroundTruth(
                incident_count=1,
                root_causes=(HypothesisCategory.NO_FAULT, HypothesisCategory.OUTBOX_STALL),
            )

    def test_no_fault_means_no_incidents(self) -> None:
        with pytest.raises(ValidationError, match="incident_count: 0"):
            GroundTruth(incident_count=1, root_causes=(HypothesisCategory.NO_FAULT,))

    def test_a_named_fault_means_at_least_one_incident(self) -> None:
        with pytest.raises(ValidationError, match="no root cause to name"):
            GroundTruth(incident_count=0, root_causes=(HypothesisCategory.OUTBOX_STALL,))

    def test_a_blank_component_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="names nothing"):
            GroundTruth(
                incident_count=1,
                root_causes=(HypothesisCategory.OUTBOX_STALL,),
                affected_components=("outbox_relay", "  "),
            )

    def test_no_action_fields_exist_on_it(self) -> None:
        """Divergence C5 / plan 01 § 5, pinned rather than remembered.

        ``expected_action_tools`` and ``forbidden_action_tools`` live on
        ``ScenarioExpectation`` and are cross-checked against ``FIX_MAP`` by
        ``test_policies.py``. A second copy here is how ``FIX_MAP`` drifted
        for weeks; this test is what stops the next author adding one for
        convenience.
        """
        action_shaped = sorted(field for field in GroundTruth.model_fields if "action" in field)
        assert action_shaped == [], (
            f"GroundTruth gained {action_shaped}. Admissible and forbidden actions are "
            "ScenarioExpectation's, cross-checked against FIX_MAP — two sources of "
            "truth for one fact is the drift plan 01 § 5 forbids."
        )

    def test_it_is_frozen_and_forbids_extras(self) -> None:
        truth = GroundTruth(incident_count=1, root_causes=(HypothesisCategory.OUTBOX_STALL,))
        with pytest.raises(ValidationError):
            truth.incident_count = 2
        with pytest.raises(ValidationError):
            GroundTruth.model_validate(
                {"incident_count": 1, "root_causes": ["outbox_stall"], "acceptable_actions": []}
            )


class TestDiscriminatingProbe:
    def test_a_write_tool_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="may only read"):
            DiscriminatingProbe(tool="replay_dlq_by_ids")

    def test_an_unregistered_tool_is_rejected(self) -> None:
        # Was `get_outbox_status`, written when it was the outbox family's
        # planned-but-absent read tool. Platform v0.6.9 shipped it, so the
        # case needed a name no release can take from it.
        with pytest.raises(ValidationError, match="not a registered tool"):
            DiscriminatingProbe(tool="not_a_platform_tool")

    def test_a_broken_pattern_is_a_load_error(self) -> None:
        with pytest.raises(ValidationError, match="not a valid regular expression"):
            DiscriminatingProbe(tool="list_dlq_messages", argument_pattern={"category": "["})

    def test_matches_requires_the_tool_and_every_constrained_argument(self) -> None:
        probe = DiscriminatingProbe(
            tool="list_dlq_messages", argument_pattern={"category": "^wait_"}
        )
        assert probe.matches("list_dlq_messages", {"category": "wait_dependency", "limit": 5})
        assert not probe.matches("list_dlq_messages", {"category": "replay_safe"})
        assert not probe.matches("list_dlq_messages", {"limit": 5})
        assert not probe.matches("get_consumer_lag", {"category": "wait_dependency"})

    def test_an_unconstrained_argument_is_ignored(self) -> None:
        probe = DiscriminatingProbe(tool="list_dlq_messages")
        assert probe.matches("list_dlq_messages", {"anything": "at all"})


class TestTheGraderSideCanReadTheAnswerKey:
    """WP-2.2's reader, proven reachable from where the grader is called.

    ``grade()`` takes only ``RunState`` and ``ScenarioExpectation`` today, so
    the root-cause dimension cannot be a field on the expectation without
    duplicating it (divergence C4 — WP-2.2 decides whether that is a new
    signature or a second grader). What this packet owes that decision is a
    record the evaluator can reach from the ``Scenario`` the runner already
    holds, and a coverage predicate to report against. Both are asserted
    here against the real corpus so "the grader can read it" is a fact rather
    than an intention.
    """

    def test_the_answer_key_is_reachable_from_a_loaded_scenario(self) -> None:
        loaded = Scenario.model_validate(
            {
                "name": "s",
                "alert": {"source": "billing"},
                "expectation": {"name": "s", "expected_terminal_state": "escalated"},
                "ground_truth": {"incident_count": 1, "root_causes": ["stale_cache"]},
            }
        )
        diagnosis = HypothesisCategory.STALE_CACHE
        assert loaded.ground_truth is not None
        assert diagnosis in loaded.ground_truth.root_causes

    def test_coverage_is_reportable_over_the_whole_corpus(self) -> None:
        corpus = load_scenarios(_SCENARIOS_DIR)
        graded = [s.name for s in corpus if s.root_cause_graded]
        # 36 of 45: 32 of 41 since WO-R3-261, which wrote a decision for every
        # scenario from the world it manufactures, plus WO-R3-202's four
        # `jobs_not_progressing` worlds, every one of them labelled. The other
        # nine are recorded abstentions rather than omissions — the tool-failure
        # tests, the harness control and the noise controls, none of which
        # produces a diagnosis to grade. WHICH scenarios, and why each one, is
        # pinned by ``tests/unit/test_ground_truth_corpus.py``; what this asserts
        # is only that the predicate the report is built from can still be
        # computed over the whole corpus and is no longer vacuous.
        assert len(graded) == 36
        assert len(corpus) >= 45


class TestTheAgentVisibleProjection:
    def test_it_carries_the_alert_the_runner_used_to_read_directly(self) -> None:
        scenario = Scenario(
            name="s",
            alert=AlertPayload(source="billing"),
            expectation=ScenarioExpectation(
                name="s", expected_terminal_state=IncidentState.ESCALATED, max_tool_calls=7
            ),
        )
        visible = scenario.agent_visible()
        assert visible.alert == scenario.alert.model_dump()
        assert visible.max_tool_calls == 7

    def test_every_shipped_scenario_projects_to_the_same_alert(self) -> None:
        """The runner change is behaviour-preserving, over the whole corpus."""
        for scenario in load_scenarios(_SCENARIOS_DIR):
            visible = scenario.agent_visible()
            assert visible.alert == scenario.alert.model_dump(), scenario.name
            assert visible.max_tool_calls == scenario.expectation.max_tool_calls, scenario.name
            assert visible.canned_tool_responses == scenario.canned_tool_responses, scenario.name
