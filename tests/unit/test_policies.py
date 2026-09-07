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
from incident_commander.tools import policies
from incident_commander.tools.policies import (
    RESOURCE_ARG_FIELDS,
    PolicyCoverageError,
    Tier,
    ensure_covered,
    tier_of,
    tools_at_or_below,
)
from incident_commander.tools.registry import TOOL_REGISTRY

# A tool that lands in the registry with no tier decision taken. Named for
# what the old fall-through made it: `tier_of` returned Tier.READ, so the
# investigation planner was free to call it and nothing failed.
_UNCLASSIFIED = "delete_all_the_things"

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
