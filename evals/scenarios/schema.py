"""Scenario schema. A scenario is a triggering alert plus a scored expectation.

The runner starts a run from the alert, drives the state machine to a terminal state and
calls the grader with the scenario's ``expectation``. Canned tool responses exercise the
agent offline against a fake platform, one response (or ordered list) per tool name.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Any, ClassVar, Final

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from evals.graders.deterministic import (
    FieldComparator,
    RowSelector,
    ScenarioExpectation,
    where_path_errors,
)
from incident_commander.agent.hypothesis import HypothesisCategory
from incident_commander.api.schemas import AlertPayload
from incident_commander.config import polling_window_seconds
from incident_commander.tools.mcp_client import ToolResult
from incident_commander.tools.policies import Tier, tier_of
from incident_commander.tools.registry import TOOL_REGISTRY

_SNAPSHOT_PATH: Final = (
    Path(__file__).resolve().parents[2] / "contracts" / "platform-tools.snapshot.json"
)
# The platform stamps every chaos tool's description with its blast radius, and selecting
# on that prefix is the structural filter ``test_registry.py`` already uses — hand-lists
# of chaos tools have drifted three times here, so nothing is hand-listed.
_CHAOS_PREFIX: Final = "[chaos:"
# Chaos tools the platform registers and the commander deliberately does not use.
# Excluding them HERE means no rebless can silently widen the closed set — v0.6.0 took
# the snapshot from 27 tools to 29 without touching this, which is the mechanism working.
_DEFERRED_CHAOS_TOOLS: Final = frozenset({"seed_dlq_messages"})


def _chaos_schemas_from_snapshot(payload: object) -> dict[str, dict[str, Any]]:
    """Chaos tools in a parsed ``tools/list`` snapshot: name → ``inputSchema``.

    The same structural ``[chaos:`` prefix as the name closure, minus deferrals. A tool
    with no ``inputSchema`` maps to an empty one, which admits anything — the walk's own
    "nothing to check against" posture rather than a spurious load failure.
    """
    if not isinstance(payload, dict):
        return {}
    tools = payload.get("tools", [])
    if not isinstance(tools, list):
        return {}
    schemas: dict[str, dict[str, Any]] = {}
    for tool in tools:
        if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
            continue
        if not str(tool.get("description", "")).startswith(_CHAOS_PREFIX):
            continue
        name = str(tool["name"])
        if name in _DEFERRED_CHAOS_TOOLS:
            continue
        schema = tool.get("inputSchema")
        schemas[name] = schema if isinstance(schema, dict) else {}
    return schemas


def _chaos_names_from_snapshot(payload: object) -> frozenset[str]:
    """Chaos tool names in a parsed ``tools/list`` snapshot, minus deferrals."""
    return frozenset(_chaos_schemas_from_snapshot(payload))


@lru_cache(maxsize=1)
def chaos_tool_schemas() -> Mapping[str, dict[str, Any]]:
    """The declarable chaos hooks and their ``inputSchema``s.

    From the committed contract snapshot, read lazily and cached, because ``evals`` is
    imported at collection time. A missing snapshot is a broken checkout, and the
    ``FileNotFoundError`` says so more usefully than a silent empty set.
    """
    return MappingProxyType(_chaos_schemas_from_snapshot(json.loads(_SNAPSHOT_PATH.read_text())))


def chaos_tool_names() -> frozenset[str]:
    """The closed set of chaos hook names a scenario may declare."""
    return frozenset(chaos_tool_schemas())


def resolve_schema_ref(prop: object, schema: Mapping[str, Any]) -> object:
    """One property with its local ``$ref`` followed into the schema's ``$defs``.

    A closed enum reaches the snapshot as a ``$ref`` with its ``type`` and ``enum`` one
    level down, so a checker reading the property alone sees a schema declaring nothing:
    without the hop, ``chaos_argument_errors`` accepted both ``loop_name=1`` and
    ``loop_name="not_a_loop"`` (platform v0.6.9's ``pause_control_loop``). Local refs
    only, one hop; the property's own keys win, and anything unresolvable comes back
    unchanged.
    """
    if not isinstance(prop, dict):
        return prop
    branches = prop.get("anyOf")
    if isinstance(branches, list):
        merged = dict(prop)
        merged["anyOf"] = [resolve_schema_ref(branch, schema) for branch in branches]
        prop = merged
        if "$ref" not in prop:
            return prop
    ref = prop.get("$ref")
    if not isinstance(ref, str) or not ref.startswith("#/$defs/"):
        return prop
    definitions = schema.get("$defs")
    if not isinstance(definitions, dict):
        return prop
    target = definitions.get(ref.removeprefix("#/$defs/"))
    if not isinstance(target, dict):
        return prop
    return {**target, **{k: v for k, v in prop.items() if k != "$ref"}}


def json_types_for(prop: object) -> frozenset[str]:
    """JSON ``type`` names a schema property admits (direct or via ``anyOf``).

    Resolve a ``$ref`` first: a reference declares no type, and the empty set returned
    for it reads as "nothing to check" at every caller.
    """
    if not isinstance(prop, dict):
        return frozenset()
    if "type" in prop:
        return frozenset({str(prop["type"])})
    return frozenset(
        str(branch["type"])
        for branch in prop.get("anyOf", [])
        if isinstance(branch, dict) and "type" in branch
    )


def enum_values_for(prop: object) -> tuple[Any, ...] | None:
    """The closed set of values a resolved property admits, or ``None``.

    ``None`` means not closed, which is also the answer for an unresolved ``$ref``. An
    ``anyOf`` branch with no ``enum`` returns ``None`` rather than the union of the
    others, since the union is a NARROWER claim than the schema makes and rejecting a
    legal value is worse than passing an illegal one. A plain null branch is the exception.
    """
    if not isinstance(prop, dict):
        return None
    if isinstance(prop.get("enum"), list):
        return tuple(prop["enum"])
    branches = prop.get("anyOf")
    if not isinstance(branches, list) or not branches:
        return None
    values: list[Any] = []
    for branch in branches:
        if not isinstance(branch, dict):
            return None
        if isinstance(branch.get("enum"), list):
            values.extend(branch["enum"])
        elif branch.get("type") == "null":
            values.append(None)
        else:
            return None
    return tuple(values) or None


# bool must precede int: bool is a subclass of int, and JSON "integer"
# does not admit true/false on the platform side.
_PY_TO_JSON: Final[tuple[tuple[type[object], frozenset[str]], ...]] = (
    (bool, frozenset({"boolean"})),
    (int, frozenset({"integer", "number"})),
    (float, frozenset({"number"})),
    (str, frozenset({"string"})),
    (dict, frozenset({"object"})),
    (list, frozenset({"array"})),
)


def value_compatible(value: object, admitted: frozenset[str]) -> bool:
    """Is this Python argument value serializable into one of the JSON types?"""
    if not admitted:
        return True  # property declares no type — nothing to check against
    if value is None:
        return "null" in admitted
    for py_type, json_types in _PY_TO_JSON:
        if isinstance(value, py_type):
            return bool(json_types & admitted)
    return True  # non-primitive value — out of scope for this check


def chaos_argument_errors(name: str, arguments: Mapping[str, Any]) -> list[str]:
    """Ways one chaos invocation disagrees with the snapshot's ``inputSchema``.

    Unknown names, missing required ones, primitive types and closed-set membership: a
    name-and-required-only check would miss a ``ttl_seconds`` integer→string flip, the
    S-18 probe's shape. Each property is resolved through ``resolve_schema_ref`` first.
    """
    schema = chaos_tool_schemas().get(name)
    if schema is None:
        return []  # unknown hook — the name validator already rejected it
    raw_properties = schema.get("properties")
    properties: dict[str, Any] = raw_properties if isinstance(raw_properties, dict) else {}
    if not properties:
        return []  # schema declares no properties — nothing to check against
    errors: list[str] = []
    unknown = sorted(set(arguments) - set(properties))
    if unknown:
        errors.append(
            f"unknown argument(s) {unknown} — {name} accepts {sorted(properties)}. "
            "The platform declares additionalProperties=false, so live seeding "
            "would fail on this."
        )
    raw_required = schema.get("required")
    required = raw_required if isinstance(raw_required, list) else []
    missing = sorted({str(field) for field in required} - set(arguments))
    if missing:
        errors.append(f"missing required argument(s) {missing} for {name}")
    for argument, value in sorted(arguments.items()):
        if argument not in properties:
            continue  # already reported as unknown
        resolved = resolve_schema_ref(properties[argument], schema)
        admitted = json_types_for(resolved)
        if not value_compatible(value, admitted):
            errors.append(
                f"{name}.{argument}={value!r} ({type(value).__name__}) is not "
                f"compatible with the snapshot's JSON type(s) {sorted(admitted)}"
            )
        admitted_values = enum_values_for(resolved)
        if admitted_values is not None and value not in admitted_values:
            errors.append(
                f"{name}.{argument}={value!r} is not one of the snapshot's "
                f"closed set {list(admitted_values)}"
            )
    return errors


#: The hook argument a TTL derivation resolves into. One spelling, because the
#: platform names it the same way on every hook that has one.
TTL_ARGUMENT: Final[str] = "ttl_seconds"


def _ttl_bound_errors(name: str, resolved: int) -> list[str]:
    """Ways a resolved TTL falls outside the snapshot's own bounds for that hook.

    ``chaos_argument_errors`` checks names, types and closed sets — not ``minimum`` and
    ``maximum``, which no hand-written argument had ever needed. A DERIVED one does: the
    knobs decide the number, the platform caps ``ttl_seconds`` at 3600, and a refusal
    mid-seeding would arrive after the world had been touched.
    """
    schema = chaos_tool_schemas().get(name) or {}
    raw = schema.get("properties")
    if not isinstance(raw, dict) or TTL_ARGUMENT not in raw:
        return []
    resolved_property = resolve_schema_ref(raw[TTL_ARGUMENT], schema)
    if not isinstance(resolved_property, Mapping):
        return []
    errors = []
    minimum = resolved_property.get("minimum")
    maximum = resolved_property.get("maximum")
    if isinstance(minimum, int | float) and resolved < minimum:
        errors.append(
            f"{name}.{TTL_ARGUMENT}={resolved} is outside the snapshot's minimum of {minimum}"
        )
    if isinstance(maximum, int | float) and resolved > maximum:
        errors.append(
            f"{name}.{TTL_ARGUMENT}={resolved} is outside the snapshot's maximum of {maximum}"
        )
    return errors


class TtlFromWindows(BaseModel):
    """A self-recovering fault's TTL, as a multiple of the agent's own timing knobs.

    WP-14.1. A bare ``ttl_seconds: 45`` reads as a fact and is really a bet on
    ``INVESTIGATE_REPROBE_DELAY_SECONDS`` still being 75 — change the knob and the
    template silently becomes a different experiment (the fault outlives the run, or
    expires before the agent's first probe). So the TTL is DERIVED: what the
    precondition spends proving the fault is there, plus a declared multiple of the
    window each knob pair gives the investigation and the verify poll.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Multiple of the ADR 0009 investigation re-probe window. 0.6 puts the expiry
    #: inside the investigation (the fault is gone before the agent can act); 1.0 or
    #: more carries it past the action.
    investigation_multiple: float = Field(default=0.0, ge=0.0, le=10.0)
    #: Multiple of the ADR 0006 verify polling window, added on top.
    verify_multiple: float = Field(default=0.0, ge=0.0, le=10.0)
    #: The smallest TTL that still leaves the fault observable at run start, in the
    #: units the world imposes (a metric's refresh interval, a heartbeat's staleness
    #: bar). Load-bearing at the OFFLINE knob defaults, where both windows are 0.
    floor_seconds: float = Field(default=30.0, ge=1.0, le=3600.0)
    #: Why this floor, in the scenario author's own words. Required, because a floor
    #: is the one number in the derivation that is not derived.
    floor_reason: str = Field(min_length=20)

    def seconds(
        self,
        *,
        precondition_window: float,
        investigation_window: float,
        verify_window: float,
    ) -> int:
        """The TTL to seed, in whole seconds — the platform types it as an integer.

        Rounded UP: a half-second lost to truncation is a half-second of fault the
        precondition may not get to see.
        """
        derived = (
            precondition_window
            + investigation_window * self.investigation_multiple
            + verify_window * self.verify_multiple
        )
        return int(math.ceil(max(derived, self.floor_seconds)))


class ChaosHook(BaseModel):
    """Declarative chaos-hook invocation the runner fires before a live run.

    Puts "which hook seeds this scenario" in the scenario file rather than operator
    memory. Live runs only; canned tool responses already encode the broken state.
    ``name`` is a CLOSED SET, because ``ChaosClient.call`` forwards it verbatim as a
    ``tools/call`` and a free string would let a YAML execute any tool the chaos
    principal can reach (S-03). ``arguments`` is closed against the same snapshot entry,
    so a typo'd name or flipped type fails at LOAD rather than mid-campaign (G1-07).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, description="Platform hook name, e.g. `inject_latency`.")
    arguments: dict[str, Any] = Field(default_factory=dict)
    #: WP-14.1: derive ``ttl_seconds`` from the agent's timing knobs instead of writing
    #: one. Set only by a temporal template; ``None`` leaves ``arguments`` as declared.
    ttl_from_windows: TtlFromWindows | None = None

    @field_validator("name")
    @classmethod
    def _name_is_a_registered_chaos_tool(cls, value: str) -> str:
        allowed = chaos_tool_names()
        if value not in allowed:
            raise ValueError(
                f"{value!r} is not a chaos tool. A chaos hook runs under the "
                f"evaluator's chaos principal, so its name is a closed set: "
                f"{', '.join(sorted(allowed))}. To add a hook, land it on the platform, "
                f"bump the pinned digest in demo/compose.yml, and re-bless the snapshot "
                f"with `make snapshot` (docs/runbook.md) — this list is derived from "
                f"contracts/platform-tools.snapshot.json, never hand-edited."
            )
        return value

    @model_validator(mode="after")
    def _arguments_match_the_snapshot_input_schema(self) -> ChaosHook:
        errors = chaos_argument_errors(self.name, self.arguments)
        if errors:
            raise ValueError(
                f"chaos hook {self.name!r} arguments disagree with "
                f"contracts/platform-tools.snapshot.json: {'; '.join(errors)}. "
                "Fix the invocation, or — if the platform moved — bump the pinned "
                "digest in demo/compose.yml and re-bless with `make snapshot` "
                "(docs/runbook.md). Never hand-edit the snapshot."
            )
        return self

    @model_validator(mode="after")
    def _a_derived_ttl_replaces_a_written_one(self) -> ChaosHook:
        """A derivation is the hook's only TTL, and only where the hook has one.

        Two spellings of one fact is how ``FIX_MAP`` drifted (divergence C5): a written
        ``ttl_seconds`` beside a derivation would seed one value and document another.
        And a derivation on a hook whose schema has no TTL is fiction — the argument
        would be rejected live, after the world was touched.
        """
        if self.ttl_from_windows is None:
            return self
        if TTL_ARGUMENT in self.arguments:
            raise ValueError(
                f"chaos hook {self.name!r} declares both ttl_from_windows and a "
                f"written {TTL_ARGUMENT}. The derivation resolves into that argument, "
                "so keeping both leaves the seeded value and the documented one free "
                "to disagree. Drop the written one."
            )
        schema = chaos_tool_schemas().get(self.name) or {}
        properties = schema.get("properties")
        if not isinstance(properties, dict) or TTL_ARGUMENT not in properties:
            raise ValueError(
                f"chaos hook {self.name!r} declares ttl_from_windows, but the snapshot "
                f"gives it no {TTL_ARGUMENT} argument — the fault it seeds does not "
                "recover on a clock, so a TTL derivation describes something the "
                "platform will not do. Pick a hook that carries a TTL."
            )
        return self

    def seeded_arguments(
        self,
        *,
        precondition_window: float,
        investigation_window: float,
        verify_window: float,
    ) -> dict[str, Any]:
        """The arguments to send, with a derived TTL resolved into them.

        Validated against the snapshot on the way out, because the resolved integer is
        the one argument value no load-time check has seen.
        """
        if self.ttl_from_windows is None:
            return dict(self.arguments)
        resolved = self.ttl_from_windows.seconds(
            precondition_window=precondition_window,
            investigation_window=investigation_window,
            verify_window=verify_window,
        )
        arguments = {**self.arguments, TTL_ARGUMENT: resolved}
        errors = chaos_argument_errors(self.name, arguments)
        errors += _ttl_bound_errors(self.name, resolved)
        if errors:
            raise ValueError(
                f"chaos hook {self.name!r} resolved {TTL_ARGUMENT}={resolved} from its "
                f"ttl_from_windows derivation and the snapshot refuses it: "
                f"{'; '.join(errors)}. The knobs this run carries put the derivation "
                "outside what the platform accepts."
            )
        return arguments


class ChaosPlan(BaseModel):
    """The whole fault a scenario manufactures, and how it is put back.

    Multi-fault and cascading worlds need more than one hook in a DECLARED order, and
    every one has to say how the world is restored (plan 01 § 4). ``setup`` fires in
    order under the chaos principal, and a failure means the BENCHMARK WORLD IS INVALID —
    the scenario is abandoned ungraded rather than scored against a premise nobody
    established. ``teardown`` fires in a ``finally``, and its failure is a different event
    from an agent failure: the grade may stand while the SHARED world is contaminated, so
    it blocks further live runs until ``make eval-reset``. ``settle_seconds`` is the
    plan-level wait before preconditions look, not a replacement for their polling.

    Teardown is NOT mandatory, deliberately: the accepted model is compensators where
    practical plus a bounded TTL plus the authoritative reset (plan 05 § A) — several
    hooks have no compensator at all, and a schema demanding one would be satisfied by
    fiction. Both halves are ``ChaosHook``, so neither gets a weaker validator.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    setup: tuple[ChaosHook, ...] = ()
    teardown: tuple[ChaosHook, ...] = ()
    # Bounded so a typo cannot park a paid run for an hour; generous next to
    # ``PreconditionProbe.delay_seconds`` because this is ONE wait for the whole plan,
    # and a cascade's second-order effect can take minutes of loop ticks.
    settle_seconds: float = Field(default=0.0, ge=0.0, le=300.0)

    @property
    def seeds_chaos(self) -> bool:
        """Whether running this plan touches the platform's chaos surface."""
        return bool(self.setup)

    @property
    def hook_names(self) -> tuple[str, ...]:
        """Every hook this plan fires, setup then teardown, in declared order."""
        return tuple(hook.name for hook in self.setup + self.teardown)

    @property
    def self_recovering(self) -> bool:
        """Whether any setup hook's fault is timed to expire during the run (WP-14.1).

        Reads the DERIVATION, not a written ``ttl_seconds``: every TTL hook has one, and
        most are set long enough to outlast the run, which is not a temporal experiment.
        """
        return any(hook.ttl_from_windows is not None for hook in self.setup)


class PreconditionField(FieldComparator):
    """One assertion about the world, before the agent is allowed to start.

    ``path`` reads the probe's parsed response, descending into lists at ``[]``, and
    holds when ANY observed value satisfies it — the only useful reading where row order
    is not guaranteed. ``where`` narrows to ONE row first, as on
    ``EvidenceFieldExpectation``, because the any-row reading is cross-satisfiable: two
    assertions over a five-row queue can be met by two DIFFERENT rows, so "the injected
    fault landed unclassified" was really two weaker premises side by side. A selector
    matching no row fails closed and says the ROW was never seen, because "the hook did
    not fire" and "it wrote the wrong thing" send a reader to different places.
    """

    path: str = Field(min_length=1)
    where: RowSelector | None = None

    @model_validator(mode="after")
    def _where_needs_rows_to_select_from(self) -> PreconditionField:
        """Validated against the grader's own rule, never a second copy."""
        error = where_path_errors(self.path, self.where)
        if error is not None:
            raise ValueError(error)
        return self


def _read_only_registered_tool(value: str, subject: str) -> str:
    """A tool name that is in the registry and reads. Shared by both probe kinds.

    One copy, because two probes hold the same rule for the same reason.
    ``subject`` is the caller's noun, so the error still reads as a sentence.
    """
    if value not in TOOL_REGISTRY:
        raise ValueError(
            f"{value!r} is not a registered tool. {subject} probes the platform "
            f"through the same typed registry the agent uses: {sorted(TOOL_REGISTRY)}."
        )
    if tier_of(value) is not Tier.READ:
        raise ValueError(
            f"{value!r} is tier {tier_of(value).value}, and {subject.lower()} may only "
            "read. A probe that mutates would manufacture the state it claims to "
            "verify, and the run would prove nothing."
        )
    return value


class PreconditionProbe(BaseModel):
    """One read the runner performs to confirm the scenario's premise is true.

    Read tools only, enforced below: a probe that changed anything would manufacture the
    state it claims to verify.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)
    expect: tuple[PreconditionField, ...] = Field(min_length=1)
    # Chaos is not instantaneous — a killed consumer's lag climbs over the platform's 60s
    # metrics interval — so a slow fault declares how long to wait. One look by default,
    # since most preconditions are about seeded state that is there or is not.
    attempts: int = Field(default=1, ge=1, le=30)
    delay_seconds: float = Field(default=0.0, ge=0.0, le=60.0)

    @field_validator("tool")
    @classmethod
    def _tool_is_read_only(cls, value: str) -> str:
        return _read_only_registered_tool(value, "A precondition")


class GroundTruth(BaseModel):
    """What was actually wrong with the world — the evaluator's copy, never the agent's.

    The fault written down once, in the enum the agent itself classifies into, so the
    root-cause grader (WP-2.2) reads a label instead of a human reading a trajectory.
    Evaluator-only STRUCTURALLY: ``Scenario.agent_visible`` is an allow-list this field
    is not on (``tests/unit/test_ground_truth_never_leaks.py``). No action fields, because
    ``expected_action_tools`` already carries that fact and is cross-checked against
    ``FIX_MAP`` — two sources of truth is how ``FIX_MAP`` drifted for weeks (plan 01 § 5,
    divergence C5). ``incident_count`` is a fact about the WORLD, not about
    ``root_causes``: a cascade is one incident with several causes. The level-0 control is
    the one pairing that is not a judgement call, and it is enforced below.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    incident_count: int = Field(ge=0)
    root_causes: tuple[HypothesisCategory, ...] = Field(min_length=1)
    affected_components: tuple[str, ...] = ()
    causal_chain: tuple[str, ...] = ()

    @field_validator("root_causes")
    @classmethod
    def _root_causes_are_distinct(
        cls, value: tuple[HypothesisCategory, ...]
    ) -> tuple[HypothesisCategory, ...]:
        """A label twice is a typo, and it would double-count in coverage reports."""
        seen = sorted({c.value for c in value})
        if len(seen) != len(value):
            raise ValueError(
                f"root_causes repeats a label: {[c.value for c in value]}. Each root "
                f"cause is named once; the distinct set is {seen}."
            )
        return value

    @field_validator("affected_components", "causal_chain")
    @classmethod
    def _entries_are_non_blank(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not entry.strip() for entry in value):
            raise ValueError(
                "affected_components and causal_chain name things; a blank entry "
                "names nothing and would read as a component called ''."
            )
        return value

    @model_validator(mode="after")
    def _no_fault_is_the_whole_answer(self) -> GroundTruth:
        """``no_fault`` means nothing is wrong, so it cannot sit beside a fault.

        Both directions are refused because both are SILENTLY wrong: ``[no_fault,
        outbox_stall]`` grades an agent correct for two opposite answers, and
        ``incident_count: 1`` with ``[no_fault]`` claims an incident nobody can name.
        """
        has_no_fault = HypothesisCategory.NO_FAULT in self.root_causes
        if has_no_fault and len(self.root_causes) > 1:
            raise ValueError(
                f"root_causes pairs no_fault with "
                f"{[c.value for c in self.root_causes if c is not HypothesisCategory.NO_FAULT]}. "
                "no_fault is the answer 'nothing is wrong'; it is the whole answer or "
                "it is not the answer."
            )
        if has_no_fault and self.incident_count != 0:
            raise ValueError(
                f"root_causes is [no_fault] but incident_count is {self.incident_count}. "
                "A world with nothing wrong holds no incidents — set incident_count: 0."
            )
        if not has_no_fault and self.incident_count == 0:
            raise ValueError(
                f"incident_count is 0 but root_causes names "
                f"{[c.value for c in self.root_causes]}. A world with no incidents has "
                "no root cause to name; use root_causes: [no_fault] for the control."
            )
        return self


class DiscriminatingProbe(BaseModel):
    """One read that separates this scenario's true cause from its neighbours.

    Evaluator-only, like ``GroundTruth``: this is the read a correct investigation WOULD
    make, so handing the agent the list would hand it the answer. It exists so a strategy
    comparison can say which run found the distinguishing evidence and which guessed.
    ``argument_pattern`` maps an argument to a regex, because the discriminating fact is
    usually the SHAPE of the call; unconstrained arguments are simply absent, and patterns
    compile at load so a broken one is a load error. ``tool`` must be a registered READ
    tool — a probe that wrote would change the world it distinguishes.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool: str = Field(min_length=1)
    argument_pattern: dict[str, str] = Field(default_factory=dict)

    @field_validator("tool")
    @classmethod
    def _tool_is_read_only(cls, value: str) -> str:
        return _read_only_registered_tool(value, "A discriminating probe")

    @field_validator("argument_pattern")
    @classmethod
    def _patterns_compile(cls, value: dict[str, str]) -> dict[str, str]:
        for argument, pattern in value.items():
            try:
                re.compile(pattern)
            except re.error as err:
                raise ValueError(
                    f"argument_pattern[{argument!r}] is not a valid regular "
                    f"expression: {pattern!r} ({err}). The pattern is matched against "
                    "the agent's own argument value, so a broken one would fail in the "
                    "grader rather than here."
                ) from err
        return value

    def matches(self, tool: str, arguments: Mapping[str, Any]) -> bool:
        """Whether a call the agent actually made is this probe.

        Equal tool name, and every constrained argument present and matching. An omitted
        constrained argument is a MISS rather than a vacuous pass.
        """
        if tool != self.tool:
            return False
        for argument, pattern in self.argument_pattern.items():
            if argument not in arguments:
                return False
            if re.search(pattern, str(arguments[argument])) is None:
                return False
        return True


class ScenarioFamily(StrEnum):
    """The observable symptom a scenario's world presents, shared across root causes.

    Plan 03 § 2's benchmark unit, and the first grouping key every report slices on — a
    closed enum rather than a free string, because a report grouped on typo-adjacent
    strings reports two families where there is one and a passing suite says nothing.
    The members are what the corpus honestly IS (promoted from WO-R3-179's provisional
    substring rule). Deliberately absent: ``api_latency``, which no scenario manufactures
    yet — a member lands in the same change as the scenarios that fill it, never before.
    """

    CACHE_REDIS = "cache_redis"
    CONSUMER_LAG = "consumer_lag"
    DEPLOY = "deploy"
    DLQ = "dlq"
    # The harness under test rather than a world: the planner's own control
    # path (stop on iteration 1) with no fault to diagnose.
    HARNESS_CONTROL = "harness_control"
    INCIDENTS = "incidents"
    # Plan 01 § 7.1's Family B, "accepted but not executing": one symptom, four answers,
    # one alert (evals/scenarios/README-jobs-not-progressing.md holds the matrix).
    JOBS_NOT_PROGRESSING = "jobs_not_progressing"
    NOISE_CONTROL = "noise_control"
    POSTGRES = "postgres"
    # Plan 00 § 113's capability level 7 (WO-R3-236, WP-14.1): the symptom is a fault
    # that is there and then is not, on its own clock. Its own family because what the
    # world PRESENTS is the recovery, not the fault underneath it.
    TEMPORAL_RECOVERY = "temporal_recovery"
    TOOL_FAULT = "tool_fault"
    TRACES = "traces"
    WORKFLOW = "workflow"
    # Plan 01 § 7.2's Family C, "the child never ran" (WO-R3-214, WP-7.2). Distinct from
    # ``workflow``, which groups the chain scenarios written before families existed
    # (evals/scenarios/README-workflow-stuck.md holds the matrix).
    WORKFLOW_STUCK = "workflow_stuck"


class ScenarioDifficulty(StrEnum):
    """How hard the diagnosis is, on plan 03 § 3's closed vocabulary.

    Closed BY THE PLAN, not by this repo: 03:22 names exactly these nine, so widening it
    is a plan change. ``control`` is the level-0 rung, and the one member a reader must
    not guess at: counted as ``single`` it inflates every "solved a real fault" number.
    """

    CONTROL = "control"
    SINGLE = "single"
    AMBIGUOUS = "ambiguous"
    MULTI_HOP = "multi_hop"
    NOISY = "noisy"
    MULTI_FAULT = "multi_fault"
    CASCADING = "cascading"
    TEMPORAL = "temporal"
    TRADEOFF = "tradeoff"


class BenchmarkSplit(StrEnum):
    """Which pool a template belongs to (plan 03 § 4).

    ``dev`` is visible and tunable, ``validation`` is for version comparisons, and
    ``holdout`` is NEVER tuned against. The split is a property of the TEMPLATE, enforced
    at load by ``loader.load_scenarios``: an instance-level holdout lets a later SFT stage
    memorise the template through its siblings (plan 06 D7).
    """

    DEV = "dev"
    VALIDATION = "validation"
    HOLDOUT = "holdout"


class AgentVisibleScenario(BaseModel):
    """Everything about a scenario that reaches the agent under test. The whole list.

    Plan 00 § 3.1's trust boundary written as a type. The runner used to read whatever it
    needed straight off a ``Scenario``, so the agent-visible set was whatever those call
    sites happened to touch. An allow-list inverts that: a new field is invisible by
    construction, and this model is ``extra="forbid"``, so making it visible takes an edit
    here and a reviewer. The alternative — dump and delete a hand-listed set — fails by
    OMISSION, which is silent. ``max_tool_calls`` crosses because the agent is TOLD its
    budget (ADR 0019); ``canned_llm_responses`` does not, because scripted replies are an
    output of the agent rather than an observation it reads.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    alert: dict[str, Any]
    max_tool_calls: int | None = None
    canned_tool_responses: dict[str, ToolResult | tuple[ToolResult, ...]] = Field(
        default_factory=dict
    )


class Scenario(BaseModel):
    """One eval scenario. Loaded from YAML, validated at load time."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    description: str = ""
    tags: tuple[str, ...] = ()
    # Stable across every instance of one template (plan 03 § 2). ``name`` identifies the
    # INSTANCE and keys the archive, the report, the baseline and the drift ledger, so it
    # cannot double as the template key. Defaults to the name via
    # ``_template_id_defaults_to_name``, not a property, so the inventory, the report and
    # the loader's split check compare ONE concrete value.
    template_id: str = ""
    # Which instance of the template this is. 0 for every legacy scenario, none of which
    # is generated; instance generation (WP-3.x) is what makes it move.
    seed: int = Field(default=0, ge=0)
    # The observable symptom, and the first key every report groups on. Optional on the
    # MODEL and mandatory in the CORPUS: required here, every inline test fixture would
    # carry a family it has no opinion about, so
    # ``test_scenario_metadata.py::TestEveryScenarioIsClassified`` walks the directory
    # instead and ``evals/inventory.py`` flags an undeclared row ``provisional``.
    family: ScenarioFamily | None = None
    # How hard the diagnosis is, on plan 03 § 3's closed nine. Optional and mandatory
    # for ``family``'s reasons, by the same test.
    difficulty: ScenarioDifficulty | None = None
    # Which pool this scenario's TEMPLATE belongs to. ``dev`` everywhere and nothing in
    # ``holdout`` yet: a holdout is a promise never to tune against a template, which is
    # the user's decision (``TestNothingIsHeldOutWithoutADecision``).
    benchmark_split: BenchmarkSplit = BenchmarkSplit.DEV
    alert: AlertPayload
    expectation: ScenarioExpectation
    # One response per tool, or a list consumed in order with the last repeating — for a
    # canned world whose state changes mid-run (get_dag_state paused false→true).
    canned_tool_responses: dict[str, ToolResult | tuple[ToolResult, ...]] = Field(
        default_factory=dict
    )
    canned_llm_responses: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    # When True the runner ignores ``canned_tool_responses`` and builds a real
    # ``MCPClient``. Such scenarios are skipped while the URL is the offline placeholder.
    use_live_mcp: bool = False
    # When True the runner ignores ``canned_llm_responses`` and builds real clients for
    # every role. Skipped under a placeholder key, and non-deterministic, so the
    # regression gate does not apply.
    use_live_llm: bool = False
    # Optional chaos hook fired before a live run. LEGACY and deliberately kept: every
    # shipped scenario spells its single fault this way and ``Scenario`` is
    # ``extra="forbid"``, so a rename is a 40-YAML migration for no gain. ``chaos`` below
    # normalizes it into a one-hook plan, which is what the runner and the gates read.
    chaos_setup: ChaosHook | None = None
    # The composable form: many hooks in a declared order, their teardown, and a settle
    # wait. Declare ONE of the two — the validator refuses both, because two spellings of
    # one world would compose into a third.
    chaos_plan: ChaosPlan | None = None
    # What must be true of the world before the agent starts. Live-only: a canned run
    # serves the broken state by construction. An unmet precondition abandons the run
    # BEFORE any model call rather than grading the agent on a false premise.
    expected_precondition: tuple[PreconditionProbe, ...] = ()
    # Why an eligible scenario is held out of the smoke pass (WO-R2-123); the value IS
    # the reason, and ``None`` means "in the pass". Membership is otherwise DERIVED
    # (``smoke_eligible``), so the only thing a human declares is a hold-back, on the
    # scenario itself: the Makefile pattern lists this replaced could rot three ways that
    # a scenario-local field cannot. Substantive by construction — write what would have
    # to be true to lift it, because "skip" is not a reason.
    smoke_exclusion: str | None = Field(default=None, min_length=20)
    # What was actually wrong, for the evaluator only. Optional: a scenario without one
    # is simply not root-cause-graded, and WP-2.2 reports that coverage rather than
    # back-filling a guess. ``GroundTruth`` says why no action fields live here.
    ground_truth: GroundTruth | None = None
    # The reads that tell this fault apart from the ones it looks like. Evaluator-only:
    # the answer key to the investigation, not a hint the agent is entitled to.
    discriminating_probes: tuple[DiscriminatingProbe, ...] = ()

    # Every field above is on exactly one side of plan 00 § 3.1's trust boundary,
    # DECLARED here rather than inferred from whichever call sites read what. Checked
    # against ``model_fields`` at import, so a field with no side cannot be imported —
    # never agent-visible by accident, and never evaluator-only by accident either.
    AGENT_VISIBLE_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            # The triggering alert — the agent's whole starting brief.
            "alert",
            # The canned platform's responses ARE the world the agent reads
            # in an offline run, so they are agent-visible by definition.
            "canned_tool_responses",
        }
    )
    EVALUATOR_ONLY_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "name",
            "description",
            "tags",
            # Benchmark bookkeeping (WP-1.4), evaluator-only without exception:
            # `difficulty` and `family` would narrow the agent's search for free, and
            # `benchmark_split` would tell it which runs are being scored.
            "template_id",
            "seed",
            "family",
            "difficulty",
            "benchmark_split",
            "expectation",
            "canned_llm_responses",
            "use_live_mcp",
            "use_live_llm",
            "chaos_setup",
            "chaos_plan",
            "expected_precondition",
            "smoke_exclusion",
            "ground_truth",
            "discriminating_probes",
        }
    )

    def agent_visible(self) -> AgentVisibleScenario:
        """This scenario projected onto everything the agent is allowed to see.

        The runner builds the agent's run from this and nothing else. An ALLOW-LIST, so
        what it does with a field it has never heard of is nothing: ``ground_truth``
        cannot leak by somebody forgetting an exclusion, because there is no exclusion
        list to forget.
        """
        return AgentVisibleScenario(
            alert=self.alert.model_dump(),
            max_tool_calls=self.expectation.max_tool_calls,
            canned_tool_responses=dict(self.canned_tool_responses),
        )

    @property
    def root_cause_graded(self) -> bool:
        """Whether this scenario can be scored on the root cause it declares.

        WP-2.2's coverage number: a scenario with no ``ground_truth`` is not graded on
        diagnosis, and a dimension nobody can fail measures nothing.
        """
        return self.ground_truth is not None

    @property
    def chaos(self) -> ChaosPlan:
        """This scenario's fault plan, whichever spelling declared it.

        The ONE reader of ``chaos_setup`` anything downstream should use. A legacy hook
        normalizes to a one-hook plan and a scenario declaring neither to the EMPTY plan,
        so no caller spells "no chaos" twice. Computed, not stored: a cached copy could
        disagree with ``chaos_setup``, which is the drift the validators prevent.
        """
        if self.chaos_plan is not None:
            return self.chaos_plan
        if self.chaos_setup is not None:
            return ChaosPlan(setup=(self.chaos_setup,))
        return ChaosPlan()

    @property
    def seeds_chaos(self) -> bool:
        """Whether this scenario touches the platform's chaos surface at all.

        What the smoke refusal (ADR 0018), the one-mutating-scenario refusal (ADR 0020)
        and the ``chaos:invoke`` guard should all key off: ``chaos_setup`` is ``None`` on
        a plan-declaring scenario, so reading it directly stops counting a plan.
        """
        return self.chaos.seeds_chaos

    @property
    def is_temporal(self) -> bool:
        """Whether this scenario's fault is timed to recover during the run (WP-14.1)."""
        return self.chaos.self_recovering

    @property
    def precondition_window_seconds(self) -> float:
        """Wall-clock the preconditions may spend before the agent's first call.

        Summed across probes, because they run in sequence, and with
        ``polling_window_seconds``' own arithmetic per probe: the delay falls BETWEEN
        attempts, so a one-look probe waits nothing. Part of a derived TTL, since this is
        fault-time the run has already spent by the time the agent starts.
        """
        return sum(
            polling_window_seconds(probe.attempts, probe.delay_seconds)
            for probe in self.expected_precondition
        )

    @property
    def recorded_refusal(self) -> str | None:
        """Why a recorded run of this scenario would not be a run of it — or ``None``.

        WP-14.1, and the one refusal this repo makes about a MODE rather than a world: a
        recording answers the call that was made at the clock it is replayed at (ADR
        0046), so a fault whose whole content is when it expires replays as a fault that
        never expires. The row would carry a temporal label over a static world.
        """
        if not self.is_temporal:
            return None
        return (
            f"scenario {self.name!r} is a temporal template: its fault is timed to "
            "recover on its own during the run, and that clock IS the experiment. A "
            "recording replays each call's stored answer (ADR 0046), so the expiry "
            "never happens and the false-attribution grade has no timeline to compare "
            "against — the row would claim a temporal measurement from a static world. "
            "Run it live (WP-14.1), one scenario per invocation, or not at all."
        )

    @model_validator(mode="after")
    def _a_timed_fault_asserts_it_is_present_at_run_start(self) -> Scenario:
        """A temporal template proves its fault is THERE, not that it was seeded.

        A time-windowed fixture drifts by the clock rather than by the data (LESSONS
        2026-09-07: ``failed_traces_scan``'s seeded traces age out of their own probe
        window), and a TTL fault is a time-windowed fixture BY DESIGN. Without a
        precondition the two failure modes are indistinguishable from the grade: a fault
        so short the agent never saw it reads as an agent that missed it, which measures
        the harness. So the precondition is structural here, not a scenario author's
        habit.
        """
        if self.is_temporal and not self.expected_precondition:
            raise ValueError(
                f"scenario {self.name!r} seeds a fault with a ttl_from_windows "
                "derivation and declares no expected_precondition. A timed fault must "
                "be asserted PRESENT at the moment the run starts — seeding it is not "
                "evidence it is still there, and a fault that expired before the "
                "agent's first probe grades the agent for missing something that had "
                "already gone. Add a precondition that reads the fault itself."
            )
        return self

    @model_validator(mode="before")
    @classmethod
    def _template_id_defaults_to_name(cls, payload: Any) -> Any:
        """A scenario that declares no ``template_id`` is its own template.

        Filled before field validation rather than exposed as a ``template_id or name``
        property: the split check, the inventory row and the report's grouping key all
        compare this value, and a computed fallback beside a stored field is two spellings
        of one identity. Non-mapping payloads are left to pydantic's own message.
        """
        if isinstance(payload, Mapping) and not payload.get("template_id"):
            name = payload.get("name")
            if isinstance(name, str) and name:
                return {**payload, "template_id": name}
        return payload

    @model_validator(mode="after")
    def _template_id_is_not_empty(self) -> Scenario:
        """Belt to ``_template_id_defaults_to_name``'s braces.

        The only route to an empty template id is a construction that bypassed the
        before-validator, and it is worth one branch: ``""`` compared to ``""`` across
        unrelated scenarios either refuses a healthy corpus as "one template in two
        splits" or groups the whole corpus as one template in every report.
        """
        if not self.template_id:
            raise ValueError(
                f"scenario {self.name!r} has an empty template_id. It defaults to the "
                "scenario name; set it explicitly only to declare that this scenario "
                "is one instance of a template that has others."
            )
        return self

    @model_validator(mode="after")
    def _one_spelling_of_the_fault(self) -> Scenario:
        """Refuse both spellings at once, and refuse a plan that seeds nothing.

        Both-at-once has no defensible reading: firing both composes a world neither
        declaration describes, and firing one silently ignores the other. An empty
        ``setup`` reads as "this scenario seeds chaos" to a human while ``seeds_chaos``
        is False, which puts a scenario the author gated out back into the smoke pass.
        """
        if self.chaos_setup is not None and self.chaos_plan is not None:
            raise ValueError(
                f"scenario {self.name!r} declares both chaos_setup and chaos_plan. "
                "They are two spellings of the same fault: keep the legacy single "
                "hook, or move it into chaos_plan.setup — never both."
            )
        if self.chaos_plan is not None and not self.chaos_plan.setup:
            raise ValueError(
                f"scenario {self.name!r} declares a chaos_plan with no setup hooks. "
                "A plan manufactures a fault; teardown compensates one. Drop the "
                "plan, or give it the hook(s) that seed the world it describes."
            )
        return self

    @property
    def smoke_eligible(self) -> bool:
        """Whether the read-only smoke stage can run and grade this honestly.

        The runner's own two refusals, not a second opinion about "read-only":
        ``seeds_chaos``, because seeding mutates the shared world the stage exists to
        prove it did not touch (ADR 0018, S-03) — and read through ``seeds_chaos``, since
        a plan leaves ``chaos_setup`` ``None``; and ``expected_action_tools``, a graded
        Tier-1 write the read-scoped token 403s by design. NOT ``not canned_only``: the
        stage deliberately mixes canned harness-sanity rows with live reads.
        """
        return not self.seeds_chaos and not self.expectation.expected_action_tools

    @property
    def in_smoke_pass(self) -> bool:
        """Eligible, and not deliberately held back. The derivation itself."""
        return self.smoke_eligible and self.smoke_exclusion is None

    @model_validator(mode="after")
    def _smoke_exclusion_is_not_redundant(self) -> Scenario:
        """An exclusion the predicate already covers implies a live decision.

        The runner refuses such a scenario from a smoke selection anyway, so the
        hold-back records a choice nobody still has to make and goes on implying one.
        A load-time refusal now, so the redundant entry cannot be committed.
        """
        if self.smoke_exclusion is not None and not self.smoke_eligible:
            raise ValueError(
                f"scenario {self.name!r} sets smoke_exclusion, but it seeds "
                "chaos or declares expected_action_tools, so the runner already "
                "refuses it from a smoke selection. The hand-written exclusion is "
                "redundant — drop it and let the predicate speak."
            )
        return self

    @property
    def canned_only(self) -> bool:
        """True when the scenario declares no live leg at all.

        A statement about the WORLD, not the env: the platform cannot manufacture this
        fault, so a "live" run would grade the agent against a premise that does not
        exist. The runner refuses the selection (exit 8) rather than serving canned
        fixtures inside a live report, and each such YAML carries what would unblock it.
        """
        return not (self.use_live_mcp or self.use_live_llm)


def _classify_every_scenario_field() -> None:
    """Refuse, at import, a ``Scenario`` field nobody put on a side of the boundary.

    A module-level check rather than a test, because the failure is the quiet one: an
    undeclared evaluator-only field is safe only while nobody widens the projection, and
    an undeclared agent-visible one is invisible to every leak test. Either mistake is an
    OMISSION, which does not announce itself in a diff. ``RuntimeError``, not ``assert``,
    so ``python -O`` cannot turn it off.
    """
    declared = Scenario.AGENT_VISIBLE_FIELDS | Scenario.EVALUATOR_ONLY_FIELDS
    actual = set(Scenario.model_fields)
    unclassified = sorted(actual - declared)
    invented = sorted(declared - actual)
    both = sorted(Scenario.AGENT_VISIBLE_FIELDS & Scenario.EVALUATOR_ONLY_FIELDS)
    problems = []
    if unclassified:
        problems.append(
            f"Scenario field(s) {unclassified} are on neither side of the trust "
            "boundary. Add each to AGENT_VISIBLE_FIELDS (and to AgentVisibleScenario, "
            "and to the projection) or to EVALUATOR_ONLY_FIELDS."
        )
    if invented:
        problems.append(
            f"AGENT_VISIBLE_FIELDS/EVALUATOR_ONLY_FIELDS name {invented}, which "
            "Scenario does not define — a renamed or deleted field left a stale entry."
        )
    if both:
        problems.append(
            f"{both} are declared both agent-visible and evaluator-only. A field is "
            "on one side of the boundary."
        )
    if problems:
        raise RuntimeError(" ".join(problems))


_classify_every_scenario_field()
