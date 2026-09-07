"""Tier-policy classification tests.

The policy module is the agent-side first filter deciding what the
investigation planner vs the remediation planner may propose.
Wave 3 PR F on the platform side will add Tier-2 approval objects;
until then ``_TIER_2_TOOLS`` is empty and ``TIER_2`` classification
is a schema hook, not a live path.
"""

from __future__ import annotations

import typing
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from evals.scenarios.loader import load_scenarios
from evals.scenarios.schema import Scenario
from incident_commander.agent.hypothesis import HypothesisCategory, ReadToolName
from incident_commander.agent.investigation import FIX_MAP, HINT_ROUTED_CATEGORIES
from incident_commander.agent.remediation import (
    RemediationPlan,
    Tier1ToolName,
    _absent_resource_args,
)
from incident_commander.agent.state import IncidentState
from incident_commander.llm.prompts.loader import load_prompt
from incident_commander.tools import policies
from incident_commander.tools.policies import (
    RESOLUTION_CLASS,
    RESOURCE_ARG_FIELDS,
    PolicyCoverageError,
    Resolution,
    Tier,
    ensure_covered,
    resolution_class_of,
    stabilize_only_tools,
    tier_of,
    tools_at_or_below,
)
from incident_commander.tools.registry import TOOL_REGISTRY

# A tool that lands in the registry with no tier decision taken. Named for
# what the old fall-through made it: `tier_of` returned Tier.READ, so the
# investigation planner was free to call it and nothing failed.
_UNCLASSIFIED = "delete_all_the_things"

_SCENARIO_DIR = Path(__file__).resolve().parents[2] / "evals" / "scenarios"


def _row_model(output_model: type[BaseModel], rows_field: str) -> type[BaseModel] | None:
    """The model of one row of a list-valued output field, if it has one.

    Walks the annotation the way the grader's ``_nested_models`` does, but
    from one named field rather than the whole tree: this asks "what shape
    are ``list_dlq_messages.items``' elements", so a map claiming those rows
    carry ``remediation_hint`` can be checked against the platform's own
    contract instead of against a recording.
    """
    annotation = output_model.model_fields[rows_field].annotation
    for arg in typing.get_args(annotation):
        if isinstance(arg, type) and issubclass(arg, BaseModel):
            return arg
    return None


def _categories_in(scenario: Scenario) -> set[HypothesisCategory]:
    """Every hypothesis category the scenario's canned planner emits.

    Read off ``canned_llm_responses`` rather than off the expectation,
    because the expectation records the tool and the terminal state but
    never the category — and the category is what ``FIX_MAP`` is keyed on.
    Live scenarios carry canned responses too (they are the offline
    fallback), so the corpus is fully covered.
    """
    found: set[HypothesisCategory] = set()
    turns: list[dict[str, Any]] = scenario.canned_llm_responses.get("investigation_planner", [])
    for turn in turns:
        for hypothesis in turn.get("hypotheses", []) or []:
            raw = hypothesis.get("category")
            try:
                found.add(HypothesisCategory(raw))
            except ValueError:
                continue
    return found


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

    def test_an_unclassified_registry_tool_raises_instead_of_reading_as_read(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The fail-closed guarantee the docstring and ADR 0003 both claim.

        ``tier_of`` used to fall through to ``Tier.READ`` for anything not
        explicitly listed, so a tool added to the registry without a policy
        decision became a read tool — callable by the investigation planner,
        with no check anywhere raising. Silence is the wrong answer to "what
        may this tool do"; the only safe answer is to refuse to classify it.
        """
        monkeypatch.setitem(TOOL_REGISTRY, _UNCLASSIFIED, TOOL_REGISTRY["get_incident"])
        with pytest.raises(PolicyCoverageError, match=_UNCLASSIFIED):
            tier_of(_UNCLASSIFIED)

    def test_the_tier_sets_partition_the_registry(self) -> None:
        # One source of truth, three disjoint sets covering it exactly. Any
        # other shape is what let the fall-through hide.
        union = policies._READ_TOOLS | policies._TIER_1_TOOLS | policies._TIER_2_TOOLS
        assert union == set(TOOL_REGISTRY)
        assert not (policies._READ_TOOLS & policies._TIER_1_TOOLS)
        assert not (policies._READ_TOOLS & policies._TIER_2_TOOLS)
        assert not (policies._TIER_1_TOOLS & policies._TIER_2_TOOLS)


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
    """The check has to be able to fail, or it is not a check.

    ``ensure_covered`` iterated the registry's own keys and called
    ``tier_of`` on each — and ``tier_of`` answered ``Tier.READ`` for
    everything unlisted, so no registry contents could ever make this raise.
    The comment above ``_TIER_1_TOOLS``, the docstring, ADR 0003 and this
    test all described a guarantee nothing implemented.
    """

    def test_every_registered_tool_has_a_tier(self) -> None:
        # If someone adds a tool to the registry without touching policies,
        # this fails — that's the whole point.
        ensure_covered()

    def test_a_registry_tool_with_no_tier_entry_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(TOOL_REGISTRY, _UNCLASSIFIED, TOOL_REGISTRY["get_incident"])
        with pytest.raises(PolicyCoverageError, match=_UNCLASSIFIED):
            ensure_covered()

    def test_a_tier_entry_naming_no_registry_tool_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The other direction of the same drift: a tool retired from the
        # registry leaves a tier entry behind, and the mapping now claims a
        # decision about something that does not exist.
        monkeypatch.setattr(policies, "_TIER_2_TOOLS", frozenset({"retired_tool"}))
        with pytest.raises(PolicyCoverageError, match="retired_tool"):
            ensure_covered()

    def test_a_tool_classified_twice_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Two tiers for one tool is not a coverage gap but it is the same
        # defect: the mapping stops being a single answer. tier_of would
        # silently prefer the more privileged set and the reader of
        # _READ_TOOLS would be wrong.
        monkeypatch.setattr(policies, "_TIER_2_TOOLS", frozenset({"get_consumer_lag"}))
        with pytest.raises(PolicyCoverageError, match="get_consumer_lag"):
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

    def test_uuid_resource_fields_is_total_and_a_subset(self) -> None:
        # ADR 0030. Total over the registry for the same reason
        # RESOURCE_ARG_FIELDS is: an empty entry is a declared "no field here
        # has a canonical shape", so a replay tool shipped tomorrow cannot
        # inherit "any string is a valid id" by silence. Subset, because a
        # field can only be shape-checked if it is a resource field at all.
        from incident_commander.tools.policies import (
            RESOURCE_ARG_FIELDS,
            UUID_RESOURCE_FIELDS,
        )
        from incident_commander.tools.registry import TOOL_REGISTRY

        assert set(UUID_RESOURCE_FIELDS) == set(TOOL_REGISTRY)
        for tool, fields in UUID_RESOURCE_FIELDS.items():
            assert fields <= RESOURCE_ARG_FIELDS[tool], tool

    def test_the_uuid_map_is_derived_from_the_platform_contract(self) -> None:
        """Anti-vacuity, and the reason this map is derived rather than typed.

        Two ways for the derivation to be silently useless: it finds nothing
        (a schema-shape assumption broke, so no plan is ever shape-checked and
        `_malformed_resource_args` is dead code), or it finds everything (the
        check fires on cache keys and trace ids, which have no canonical form,
        and refuses every legitimate `invalidate_cache_key` plan). Both read as
        green without an assertion on the contents, so name them.
        """
        from incident_commander.tools.policies import UUID_RESOURCE_FIELDS

        assert UUID_RESOURCE_FIELDS["replay_dlq_by_ids"] == frozenset({"job_ids"})
        assert UUID_RESOURCE_FIELDS["pause_dag"] == frozenset({"root_job_id"})
        assert UUID_RESOURCE_FIELDS["mark_dlq_permanent"] == frozenset({"job_id"})
        assert UUID_RESOURCE_FIELDS["get_dag_state"] == frozenset({"job_id"})
        # Resource names with no canonical form. `get_trace.trace_id` is
        # declared `maxLength: 255` and nothing else in the platform's own
        # schema, and a cache key is free text — asserting a shape on either
        # would be the agent inventing a contract the platform does not have.
        assert UUID_RESOURCE_FIELDS["get_trace"] == frozenset()
        assert UUID_RESOURCE_FIELDS["invalidate_cache_key"] == frozenset()
        assert UUID_RESOURCE_FIELDS["restart_consumer_group"] == frozenset()

    def test_plan_scaffold_tools_name_no_resources(self) -> None:
        # The parametrized test below is only meaningful if the filler
        # leg contributes no findings of its own.
        assert not RESOURCE_ARG_FIELDS[_FILLER_ACTION_TOOL]
        assert not RESOURCE_ARG_FIELDS[_FILLER_VERIFY_TOOL]

    @pytest.mark.parametrize(("tool", "field"), _RESOURCE_ARG_ENTRIES)
    def test_omitting_any_resource_field_is_a_planning_violation(
        self, tool: str, field: str
    ) -> None:
        """WO-R2-15 / ADR 0024: absence is as loud as mis-sourcing.

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


class TestAlertSubjectProbes:
    """``ALERT_SUBJECT_PROBES`` is the alert-side half of ``RESOURCE_ARG_FIELDS``.

    The guard in ``investigation.py`` turns an alert into "the exact probe
    call that would read this incident's subject". That claim is only true
    while the map's tool/argument halves still exist on the platform's own
    tool contract — a renamed argument or a retiered tool would leave the
    guard demanding a call nobody can make, which fails the handoff of every
    alert in that family. These are the cross-checks that make the map's
    docstring enforceable rather than aspirational (architecture-principles
    rule 2: one mapping, read from both sides, with a test asserting sync).
    """

    def test_every_probe_tool_is_a_registered_read_tool(self) -> None:
        from incident_commander.agent.investigation import ALERT_SUBJECT_PROBES

        for alert_field, (tool, _arg) in ALERT_SUBJECT_PROBES.items():
            assert tool in TOOL_REGISTRY, f"{alert_field} maps to unknown tool {tool}"
            assert tier_of(tool) is Tier.READ, (
                f"{alert_field} maps to {tool}, which is tier "
                f"{tier_of(tool).value}. The subject probe must be a read: the "
                "guard asks the planner to CALL it before remediating."
            )

    def test_every_probe_argument_exists_on_its_input_model(self) -> None:
        from incident_commander.agent.investigation import ALERT_SUBJECT_PROBES

        for alert_field, (tool, arg) in ALERT_SUBJECT_PROBES.items():
            assert arg in TOOL_REGISTRY[tool].input_model.model_fields, (
                f"{alert_field} maps to {tool}.{arg}, which is not an argument "
                f"of {tool}. The refusal reason tells the planner to call it."
            )

    def test_every_probe_argument_is_a_declared_resource_field(self) -> None:
        """The two maps must agree on what counts as naming a resource.

        ``RESOURCE_ARG_FIELDS`` already decided which arguments NAME a
        platform resource as opposed to filtering or counting. A subject
        probe pointed at a non-resource argument (``limit``, ``since_hours``)
        would be asking the planner to prove something about a filter.
        """
        from incident_commander.agent.investigation import ALERT_SUBJECT_PROBES

        for alert_field, (tool, arg) in ALERT_SUBJECT_PROBES.items():
            assert arg in RESOURCE_ARG_FIELDS[tool], (
                f"{alert_field} maps to {tool}.{arg}, which RESOURCE_ARG_FIELDS "
                f"does not classify as resource-naming ({sorted(RESOURCE_ARG_FIELDS[tool])})."
            )

    def test_the_alert_fields_are_drawn_from_the_known_alert_vocabulary(self) -> None:
        """No invented alert keys.

        ``tests/unit/test_scenario_alert_premise.py`` pins the vocabulary the
        corpus actually uses; a subject field outside it is a guess about a
        payload shape nothing produces, and it would sit inert forever.
        """
        from incident_commander.agent.investigation import ALERT_SUBJECT_PROBES

        known = {
            "fingerprint",
            "group",
            "consumer_group",
            "service",
            "summary",
            "queue",
            "job_id",
            "job_type",
            "cache_key",
            "trace_id",
        }
        assert set(ALERT_SUBJECT_PROBES) <= known, (
            f"unknown alert field(s): {sorted(set(ALERT_SUBJECT_PROBES) - known)}. "
            "Add them to _NON_WEBHOOK_ALERT_FIELDS in test_scenario_alert_premise.py "
            "first — an alert key no scenario carries cannot be exercised."
        )


class TestVerifyProbeForAction:
    """``VERIFY_PROBE_FOR_ACTION`` is the action-side sibling of ``ALERT_SUBJECT_PROBES``.

    One asks "which probe reads what this alert is about?", the other
    "which probe reads what this action just changed?". Same cross-checks
    apply for the same reason (architecture-principles rule 2): a map
    naming a tool or argument the platform does not expose would have the
    guard demanding a call nobody can make, and the failure would land on
    every plan in that family.

    The totality test is the one that matters most here — see its docstring.
    """

    def test_every_tier_1_tool_has_a_declared_entry(self) -> None:
        """TOTAL over Tier-1, so a new action tool cannot be silently inert.

        This guard is inert when the action tool has no entry. That is the
        correct behaviour for the bulk DLQ tools, which name a category
        rather than a resource — and it is a silent hole for any Tier-1
        tool someone adds later and forgets. Requiring an explicit entry,
        even an explicitly EMPTY one, converts that hole into a failing
        test with a message saying what decision is missing.

        This is the same shape as ``TestResourceArgFieldsCoverage``, and it
        exists for the same reason ADR 0024 gave: "the narrowness is a
        trap, not a comfort".
        """
        from incident_commander.agent.remediation import VERIFY_PROBE_FOR_ACTION

        tier_1 = tools_at_or_below(Tier.TIER_1) - tools_at_or_below(Tier.READ)
        missing = sorted(tier_1 - set(VERIFY_PROBE_FOR_ACTION))
        stale = sorted(set(VERIFY_PROBE_FOR_ACTION) - tier_1)
        assert not missing and not stale, (
            f"VERIFY_PROBE_FOR_ACTION does not cover the Tier-1 slice.\n"
            f"  Tier-1 tools with no entry: {missing}\n"
            f"  entries that are not Tier-1: {stale}\n"
            f"For each missing tool, decide which READ tool observes the "
            f"resource it changes and add it — or add an empty tuple to say "
            f"that no read observes it, which is what the bulk DLQ tools do. "
            f"An absent entry makes the verify-target guard silently inert "
            f"for that tool (ADR 0025)."
        )

    def test_every_probe_tool_is_a_registered_read_tool(self) -> None:
        from incident_commander.agent.remediation import VERIFY_PROBE_FOR_ACTION

        for action, probes in VERIFY_PROBE_FOR_ACTION.items():
            for probe in probes:
                assert probe.tool_name in TOOL_REGISTRY, (
                    f"{action} maps to unknown tool {probe.tool_name}"
                )
                assert tier_of(probe.tool_name) is Tier.READ, (
                    f"{action} maps to {probe.tool_name}, which is tier "
                    f"{tier_of(probe.tool_name).value}. A verify probe must be "
                    "a read — the plan's verify leg is tier-checked separately, "
                    "so a non-read here would be unreachable advice."
                )

    def test_every_probe_argument_is_a_declared_resource_field(self) -> None:
        """A named argument must be one ``RESOURCE_ARG_FIELDS`` calls a resource.

        The guard compares the probe's argument VALUE against the values
        the action's resource fields carry. Pointing it at a filter
        (``limit``, ``remediation_hint``) would compare a resource name to
        a filter value and refuse every correct plan.
        """
        from incident_commander.agent.remediation import VERIFY_PROBE_FOR_ACTION

        for action, probes in VERIFY_PROBE_FOR_ACTION.items():
            for probe in probes:
                if probe.argument_field is None:
                    continue
                assert probe.argument_field in RESOURCE_ARG_FIELDS[probe.tool_name], (
                    f"{action} maps to {probe.tool_name}.{probe.argument_field}, "
                    f"which RESOURCE_ARG_FIELDS does not classify as "
                    f"resource-naming ({sorted(RESOURCE_ARG_FIELDS[probe.tool_name])})."
                )

    def test_argument_free_probes_really_take_no_resource_argument(self) -> None:
        """``argument_field=None`` is a claim about the tool, not a shortcut.

        It says "this read observes the resource but cannot name it", which
        is why picking the tool is the whole requirement. If the tool DOES
        have a resource argument, that licence would let a plan verify the
        wrong row while looking compliant.
        """
        from incident_commander.agent.remediation import VERIFY_PROBE_FOR_ACTION

        for action, probes in VERIFY_PROBE_FOR_ACTION.items():
            for probe in probes:
                if probe.argument_field is not None:
                    continue
                assert not RESOURCE_ARG_FIELDS[probe.tool_name], (
                    f"{action} maps to {probe.tool_name} with no argument, but "
                    f"{probe.tool_name} DOES take resource arguments "
                    f"({sorted(RESOURCE_ARG_FIELDS[probe.tool_name])}). Name one, "
                    "or the guard cannot tell which resource was observed."
                )

    def test_an_action_with_resource_fields_has_at_least_one_probe(self) -> None:
        """Empty entries are only honest for actions that name no resource.

        ``replay_dlq_by_category`` and ``replay_dlq_messages`` earn their
        empty tuples by having no resource-naming argument at all. An action
        that DOES name a resource and maps to nothing would be declaring
        that the platform cannot observe its own effect — possible in
        principle, but it should be argued in an ADR rather than typed in
        as an empty tuple.
        """
        from incident_commander.agent.remediation import VERIFY_PROBE_FOR_ACTION

        for action, probes in VERIFY_PROBE_FOR_ACTION.items():
            if probes:
                continue
            assert not RESOURCE_ARG_FIELDS[action], (
                f"{action} is declared inert (empty tuple) but names resources "
                f"({sorted(RESOURCE_ARG_FIELDS[action])}). Either a read observes "
                "that resource — map it — or explain in an ADR why none can."
            )


class TestSourceRowForAction:
    """``SOURCE_ROW_FOR_ACTION`` is the third map in the probe family.

    ``ALERT_SUBJECT_PROBES`` asks "did anyone read what this alert is
    about?"; ``VERIFY_PROBE_FOR_ACTION`` asks "can anyone read what this
    action will change?". Both are satisfied by a run that never established
    whether the thing it is about to change is safe to change, and this one
    asks that: before a dead-lettered job is replayed, its dead-letter row
    has to be in the evidence, because the row is the only place its
    ``remediation_hint`` exists.

    Same cross-checks as its siblings and for the same reason
    (architecture-principles rule 2): a map naming a tool, a rows field or
    an id field the platform does not emit would refuse every plan in that
    family, with the failure landing on correct agents.
    """

    def test_every_tier_1_tool_has_a_declared_entry(self) -> None:
        """TOTAL over Tier-1: inertness is declared, never inherited.

        The guard is inert for a tool with no entry, which is right for the
        five actions no listing classifies and a silent hole for a replay
        tool someone adds next year. An explicitly empty tuple says a human
        decided; an absent key says nobody looked.
        """
        from incident_commander.agent.remediation import SOURCE_ROW_FOR_ACTION

        tier_1 = tools_at_or_below(Tier.TIER_1) - tools_at_or_below(Tier.READ)
        missing = sorted(tier_1 - set(SOURCE_ROW_FOR_ACTION))
        stale = sorted(set(SOURCE_ROW_FOR_ACTION) - tier_1)
        assert not missing and not stale, (
            f"SOURCE_ROW_FOR_ACTION does not cover the Tier-1 slice.\n"
            f"  Tier-1 tools with no entry: {missing}\n"
            f"  entries that are not Tier-1: {stale}\n"
            f"For each missing tool, decide whether a read classifies the "
            f"resources it acts on — map it — or add an empty tuple to say "
            f"none does. An absent entry makes the read-before-act guard "
            f"silently inert for that tool (ADR 0027)."
        )

    def test_every_source_is_a_registered_read_tool(self) -> None:
        from incident_commander.agent.remediation import SOURCE_ROW_FOR_ACTION

        for action, sources in SOURCE_ROW_FOR_ACTION.items():
            for source in sources:
                assert source.tool_name in TOOL_REGISTRY, (
                    f"{action} maps to unknown tool {source.tool_name}"
                )
                assert tier_of(source.tool_name) is Tier.READ, (
                    f"{action} maps to {source.tool_name}, which is tier "
                    f"{tier_of(source.tool_name).value}. Establishing that an "
                    "action is safe must not itself mutate anything."
                )

    def test_every_declared_field_is_one_the_platform_emits(self) -> None:
        """The rows path, the id field and the decision field must exist.

        Checked against the tool's own output model rather than against a
        recording: a typo in ``rows_field`` makes ``_rows_read_for`` find no
        rows in any listing, so the guard refuses every replay while looking
        exactly like an agent that never read the DLQ. That failure is
        indistinguishable from the thing the guard exists to catch, which is
        the worst possible shape for a typo to take.
        """
        from incident_commander.agent.remediation import SOURCE_ROW_FOR_ACTION

        for action, sources in SOURCE_ROW_FOR_ACTION.items():
            for source in sources:
                output = TOOL_REGISTRY[source.tool_name].output_model
                assert source.rows_field in output.model_fields, (
                    f"{action} reads rows from {source.tool_name}.{source.rows_field}, "
                    f"which that tool does not return ({sorted(output.model_fields)})."
                )
                row_model = _row_model(output, source.rows_field)
                assert row_model is not None, (
                    f"{source.tool_name}.{source.rows_field} does not hold typed rows, "
                    "so there is no row model to look an id up in."
                )
                for field in (source.id_field, source.decision_field):
                    assert field in row_model.model_fields, (
                        f"{action} expects {source.tool_name} rows to carry "
                        f"{field!r}; the row model has "
                        f"{sorted(row_model.model_fields)}."
                    )

    def test_the_acting_tool_names_the_resources_the_rows_identify(self) -> None:
        """A non-empty entry is only meaningful for an action that names ids.

        The guard compares the action's own resource arguments against the
        ids it found in rows. An action that names no resource yields nothing
        to compare, so a source mapped to it would be inert while reading as
        enforcement — the same trap ``VERIFY_PROBE_FOR_ACTION``'s empty-entry
        test closes from the other direction.
        """
        from incident_commander.agent.remediation import SOURCE_ROW_FOR_ACTION

        for action, sources in SOURCE_ROW_FOR_ACTION.items():
            if not sources:
                continue
            assert RESOURCE_ARG_FIELDS[action], (
                f"{action} declares a source row but names no resource arguments, "
                "so the guard has nothing to look up and would never fire."
            )


class TestSourceListingForAction:
    """``SOURCE_LISTING_FOR_ACTION`` is the fourth map in the probe family.

    Same rule as ``SOURCE_ROW_FOR_ACTION`` — read the thing before you act
    on it — against the call shape that names no thing. A category replay
    hands the platform a filter and lets it choose the rows at execution
    time, so ``RESOURCE_ARG_FIELDS`` is empty for it, the by-id guard is
    inert by construction, and until ADR 0028 a bulk replay by a run that
    had listed nothing at all was admitted (WO-R2-143).

    Same cross-checks as its siblings and for the same reason
    (architecture-principles rule 2): a map naming a tool, a rows field or a
    filter argument the platform does not have would refuse every plan in
    that family, with the failure landing on correct agents.
    """

    def test_every_tier_1_tool_has_a_declared_entry(self) -> None:
        """TOTAL over Tier-1: inertness is declared, never inherited.

        The trap this closes is the one the map itself was born from. A
        bulk tool with no entry is silently exempt, and "silently exempt"
        is precisely what ``replay_dlq_by_category`` was for the whole life
        of ADR 0027.
        """
        from incident_commander.agent.remediation import SOURCE_LISTING_FOR_ACTION

        tier_1 = tools_at_or_below(Tier.TIER_1) - tools_at_or_below(Tier.READ)
        missing = sorted(tier_1 - set(SOURCE_LISTING_FOR_ACTION))
        stale = sorted(set(SOURCE_LISTING_FOR_ACTION) - tier_1)
        assert not missing and not stale, (
            f"SOURCE_LISTING_FOR_ACTION does not cover the Tier-1 slice.\n"
            f"  Tier-1 tools with no entry: {missing}\n"
            f"  entries that are not Tier-1: {stale}\n"
            f"For each missing tool, decide whether it expands a FILTER over "
            f"rows the agent should have listed — map it — or add an empty "
            f"tuple to say it names its rows outright. An absent entry makes "
            f"the coverage guard silently inert for that tool (ADR 0028)."
        )

    def test_every_source_is_a_registered_read_tool(self) -> None:
        from incident_commander.agent.remediation import SOURCE_LISTING_FOR_ACTION

        for action, sources in SOURCE_LISTING_FOR_ACTION.items():
            for source in sources:
                assert source.tool_name in TOOL_REGISTRY, (
                    f"{action} maps to unknown tool {source.tool_name}"
                )
                assert tier_of(source.tool_name) is Tier.READ, (
                    f"{action} maps to {source.tool_name}, which is tier "
                    f"{tier_of(source.tool_name).value}. Establishing that an "
                    "action is safe must not itself mutate anything."
                )

    def test_every_declared_field_is_one_the_platform_emits(self) -> None:
        """Rows field, decision field, and BOTH ends of every scope.

        The scope fields are the ones worth checking and the reason is the
        asymmetry: ``read_field`` is an argument on the listing's INPUT
        model, ``action_field`` an argument on the action's. A typo in
        either silently changes the guard's meaning rather than breaking it
        — a misspelled ``read_field`` reads as "this listing never narrowed"
        and admits everything; a misspelled ``action_field`` reads as "this
        action narrows on nothing" and refuses every filtered read. Both
        failures look like agent behaviour.
        """
        from incident_commander.agent.remediation import SOURCE_LISTING_FOR_ACTION

        for action, sources in SOURCE_LISTING_FOR_ACTION.items():
            action_input = TOOL_REGISTRY[action].input_model
            for source in sources:
                spec = TOOL_REGISTRY[source.tool_name]
                output = spec.output_model
                assert source.rows_field in output.model_fields, (
                    f"{action} reads rows from {source.tool_name}.{source.rows_field}, "
                    f"which that tool does not return ({sorted(output.model_fields)})."
                )
                row_model = _row_model(output, source.rows_field)
                assert row_model is not None, (
                    f"{source.tool_name}.{source.rows_field} does not hold typed rows."
                )
                assert source.decision_field in row_model.model_fields, (
                    f"{action} expects {source.tool_name} rows to carry "
                    f"{source.decision_field!r}; the row model has "
                    f"{sorted(row_model.model_fields)}."
                )
                assert source.scopes, (
                    f"{action} declares a source listing with no scopes, so "
                    "coverage would be satisfied by any reading at all."
                )
                for scope in source.scopes:
                    assert scope.read_field in spec.input_model.model_fields, (
                        f"{action} narrows {source.tool_name} on "
                        f"{scope.read_field!r}, which is not an argument it takes "
                        f"({sorted(spec.input_model.model_fields)})."
                    )
                    if scope.action_field is None:
                        continue
                    assert scope.action_field in action_input.model_fields, (
                        f"{action} is said to narrow on {scope.action_field!r}, "
                        f"which is not one of its arguments "
                        f"({sorted(action_input.model_fields)})."
                    )

    def test_the_acting_tool_names_no_resources_of_its_own(self) -> None:
        """A non-empty entry is only meaningful for an action that names NO ids.

        The exact mirror of ``TestSourceRowForAction``'s last case, and
        together the two say the family is a partition rather than an
        overlap: an action either names its rows (and the by-id guard asks
        whether they were read) or names a filter (and this one asks whether
        the slice was listed). A tool in both maps would be asked to satisfy
        two rules for one act, and the stricter one would refuse correct
        plans nobody could diagnose.
        """
        from incident_commander.agent.remediation import (
            SOURCE_LISTING_FOR_ACTION,
            SOURCE_ROW_FOR_ACTION,
        )

        for action, sources in SOURCE_LISTING_FOR_ACTION.items():
            if not sources:
                continue
            assert not RESOURCE_ARG_FIELDS[action], (
                f"{action} declares a source listing but names resource arguments "
                f"({sorted(RESOURCE_ARG_FIELDS[action])}), so the by-id guard "
                "already covers it and this one would double-charge the same act."
            )
            assert not SOURCE_ROW_FOR_ACTION[action], (
                f"{action} is non-inert in BOTH read-before-act maps. The two are "
                "meant to partition the Tier-1 slice: rows or a filter, never both."
            )


class TestResolutionClass:
    """``RESOLUTION_CLASS`` answers "can a successful call END the incident?".

    Tier answers a different question — how much damage the call can do —
    and until 2026-09-07 nothing answered this one at all, so every Tier-1
    tool was implicitly a resolution. ``pause_dag`` is the counter-example
    that class exists for: it halts promotion of waiting children, self-
    cleans on a 10-minute TTL, and changes nothing about the node that
    stopped the chain. A pause that works reads back exactly as the
    platform's own description says it should — ``paused=true`` with the
    children still ``waiting`` — so a verification judge holding an
    expectation of "children stop advancing" answers ``verified``, and the
    run reported RESOLVED on a chain nobody had fixed.

    Same coverage shape as ``TestVerifyProbeForAction`` above, for the same
    reason (architecture-principles rule 2): the map is TOTAL over the
    Tier-1 slice, because an absent entry is a safety decision nobody took
    and the default it would inherit — "of course it resolves" — is the
    exact assumption ``pause_dag`` disproved.
    """

    def test_every_tier_1_tool_is_classified(self) -> None:
        tier_1 = tools_at_or_below(Tier.TIER_1) - tools_at_or_below(Tier.READ)
        missing = sorted(tier_1 - set(RESOLUTION_CLASS))
        stale = sorted(set(RESOLUTION_CLASS) - tier_1)
        assert not missing and not stale, (
            f"RESOLUTION_CLASS does not cover the Tier-1 slice.\n"
            f"  Tier-1 tools with no entry: {missing}\n"
            f"  entries that are not Tier-1: {stale}\n"
            f"For each missing tool, decide whether a verified success "
            f"REMOVES the incident's cause (Resolution.RESOLVES) or only "
            f"holds it still (Resolution.STABILIZES), and write the reason "
            f"beside it. There is no safe default: a stabilizer that "
            f"inherits 'resolves' by silence reports a fix that fixed "
            f"nothing."
        )

    def test_every_entry_carries_a_written_reason(self) -> None:
        """The rationale is load-bearing, not a comment.

        ``remediation._stabilized_reason`` quotes it verbatim into the
        escalation reason, which ``briefing.py`` reads into
        ``EscalationBriefing.escalation_reason`` — so for a stabilizer it
        is literally the text an on-call reads at 3am. A placeholder here
        ships as a placeholder there.
        """
        for name, policy in sorted(RESOLUTION_CLASS.items()):
            assert len(policy.rationale.strip()) >= 40, (
                f"{name} has a {len(policy.rationale.strip())}-character "
                f"rationale. Say what a successful call does and does not "
                f"achieve — for a stabilizer this string is what the human "
                f"reads in the briefing."
            )

    def test_pause_dag_is_stabilize_only(self) -> None:
        assert resolution_class_of("pause_dag").resolution is Resolution.STABILIZES
        assert "pause_dag" in stabilize_only_tools()

    def test_the_stabilize_only_set_is_not_empty(self) -> None:
        """Anti-vacuity canary.

        Every assertion about stabilize-only behaviour elsewhere in the
        suite is written against a specific tool, but the *class* going
        empty — someone reclassifying the one member — would leave the
        enforcement branch in ``transition_verify`` unreachable and every
        remaining test green. If the set is ever legitimately emptied,
        delete the branch and this test together, deliberately.
        """
        assert stabilize_only_tools()

    def test_a_read_tool_has_no_resolution_class(self) -> None:
        with pytest.raises(PolicyCoverageError, match="not Tier-1"):
            resolution_class_of("get_dag_state")

    def test_an_unknown_tool_raises_key_error(self) -> None:
        with pytest.raises(KeyError):
            resolution_class_of(_UNCLASSIFIED)

    def test_an_unclassified_tier_1_tool_raises_rather_than_defaulting(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The whole point: silence must not mean "resolves".

        Simulated by removing an entry rather than by adding a tool,
        because ``Tier1ToolName`` is a Literal frozen at import time and a
        fake tool would fail earlier for the wrong reason.
        """
        patched = dict(RESOLUTION_CLASS)
        del patched["pause_dag"]
        monkeypatch.setattr(policies, "RESOLUTION_CLASS", patched)

        with pytest.raises(PolicyCoverageError, match="no entry in RESOLUTION_CLASS"):
            policies.resolution_class_of("pause_dag")


class TestFixMapMatchesTheSuite:
    """The tool a category is steered toward may not be one its scenarios forbid.

    ``FIX_MAP`` is the single source of truth for hypothesis-category →
    Tier-1 routing (architecture-principles rule 2), and the remediation
    planner prompt is written *from* it. But only ``FIX_MAP``'s KEYS are
    read at runtime (``top.category not in FIX_MAP`` gates the handoff), so
    a stale VALUE breaks nothing any existing test could see — and offline
    eval cannot see it either, because offline runs replay canned planner
    output and never load the prompt at all.

    That blind spot ran for the whole life of PR #173. That PR redesigned
    ``remediate_runaway_saga_success`` around replaying the dead-lettered
    chain root and put ``pause_dag`` into the scenario's
    ``forbidden_action_tools`` — because the platform refuses to replay a
    job inside a paused DAG, so pausing does not merely fail to fix the
    chain, it breaks the fix. It did not touch ``FIX_MAP``, which still
    said ``RUNAWAY_SAGA: "pause_dag"``, or the prompt, which still said
    ``runaway_saga / stuck_dag → pause_dag``. The suite steered the live
    agent at the one tool that scenario forbids, and 38/38 stayed green.

    This is the check that closes it, and it is deliberately a statement
    about the CORPUS rather than about one scenario: any future scenario
    that forbids its own category's steered fix fails here.

    Scoped to scenarios that expect ``resolved``. Escalate-only scenarios
    forbid every Tier-1 tool on purpose — "when the correct behaviour is to
    touch nothing, the forbidden set is every tool that can touch
    something" — so including them would make the check fire on every
    category that has a fix at all, which is no check.
    """

    @staticmethod
    def _resolving_scenarios() -> list[Scenario]:
        return [
            s
            for s in load_scenarios(_SCENARIO_DIR)
            if s.expectation.expected_terminal_state is IncidentState.RESOLVED
        ]

    def test_the_corpus_has_resolving_scenarios_to_check(self) -> None:
        """Anti-vacuity canary: an empty selection would report nothing, green."""
        assert self._resolving_scenarios()

    def test_no_scenario_forbids_the_fix_its_category_is_steered_to(self) -> None:
        problems: list[str] = []
        for scenario in self._resolving_scenarios():
            forbidden = set(scenario.expectation.forbidden_action_tools)
            if not forbidden:
                continue
            for category in _categories_in(scenario):
                if category in HINT_ROUTED_CATEGORIES:
                    # The map's value names the common case only; the
                    # actual tool comes from the row's `remediation_hint`.
                    # `dlq_human_required_escalates` forbids
                    # `replay_dlq_by_ids` and is right to.
                    continue
                steered = FIX_MAP.get(category)
                if steered is not None and steered in forbidden:
                    problems.append(
                        f"{scenario.name}: hypothesis category {category.value!r} "
                        f"is steered to {steered!r} by FIX_MAP, and "
                        f"{steered!r} is in that scenario's "
                        f"forbidden_action_tools"
                    )
        assert not problems, (
            "FIX_MAP steers the agent at a tool the scenario grades as a "
            "safety violation:\n  " + "\n  ".join(problems) + "\n"
            "Either the routing is stale (fix FIX_MAP *and* the "
            "remediation-planner prompt, which is written from it) or the "
            "scenario forbids the wrong tool. Both halves move together — "
            "the runtime reads only FIX_MAP's keys, so a stale value is "
            "invisible except through the prompt the live agent obeys."
        )

    def test_hint_routed_categories_are_categories_with_a_fix(self) -> None:
        """The exemption set may only name categories the map actually routes.

        A category exempted here but absent from ``FIX_MAP`` escalates
        regardless, so the entry would describe a routing that does not
        exist — and would quietly widen the exemption if that category
        later gained a fix.
        """
        stale = sorted(c.value for c in HINT_ROUTED_CATEGORIES - set(FIX_MAP))
        assert not stale, (
            f"HINT_ROUTED_CATEGORIES names {stale}, which FIX_MAP does not "
            f"route. A category with no fix auto-escalates; there is no "
            f"tool selection to exempt."
        )

    def test_every_steered_tool_is_a_tier_1_action(self) -> None:
        """A value that is not Tier-1 could never be planned at all."""
        for category, tool in sorted(FIX_MAP.items()):
            assert tool in TOOL_REGISTRY, f"{category.value} → unknown tool {tool!r}"
            assert tier_of(tool) is Tier.TIER_1, (
                f"{category.value} → {tool!r}, which is tier "
                f"{tier_of(tool).value}. The remediation planner may only "
                f"propose Tier-1 actions, so this routing is unreachable."
            )

    def test_every_steered_tool_is_named_in_the_planner_prompt(self) -> None:
        """Rule 2's "prompt describes the mapping *from* the code", checked.

        Weak on its own — both halves were stale together in the case
        above, and this test would have passed throughout — which is why it
        sits beside the corpus check rather than instead of it. It still
        catches the other direction: a ``FIX_MAP`` value changed with no
        prompt edit.
        """
        prompt = load_prompt("remediation_planner")
        for category, tool in sorted(FIX_MAP.items()):
            assert tool in prompt, (
                f"FIX_MAP routes {category.value} to {tool!r}, which the "
                f"remediation planner prompt never names. The prompt is "
                f"written from this map; a tool the map steers to and the "
                f"prompt omits is a routing the live agent never sees."
            )
