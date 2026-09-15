"""Scenario schema. A scenario is a triggering alert plus a scored expectation.

Scenarios drive the eval runner: the runner starts a run from the alert, drives
the state machine to a terminal state, and calls the grader with the scenario's
``expectation``. Canned tool responses let the runner exercise the agent offline
against a fake platform — one response per tool name is enough for Phase 0's
one-probe shape; more elaborate matching lands with multi-probe scenarios.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
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
from incident_commander.tools.mcp_client import ToolResult
from incident_commander.tools.policies import Tier, tier_of
from incident_commander.tools.registry import TOOL_REGISTRY

_SNAPSHOT_PATH: Final = (
    Path(__file__).resolve().parents[2] / "contracts" / "platform-tools.snapshot.json"
)
# Since v0.4.9 the platform stamps every chaos tool's description with its
# blast radius (platform app/mcp/chaos.py:79). Selecting on that prefix is
# the same structural filter tests/unit/test_registry.py uses to exclude
# chaos tools from TOOL_REGISTRY — hand-lists of chaos tools have drifted
# three times in this repo's history, so nothing here is hand-listed.
_CHAOS_PREFIX: Final = "[chaos:"
# Chaos tools the platform registers but the commander deliberately does not
# use. ``seed_dlq_messages`` is deferred, flag-off platform work that stays
# out of this repo entirely — not in TOOL_REGISTRY, not in a ``chaos_setup``,
# not in a scenario. The v0.5.0 rebless first pulled it into the snapshot;
# excluding it here by construction means that rebless could not, and no
# future one can, silently widen the closed set. (The v0.6.0 rebless took
# the snapshot from 27 to 29 tools without touching this set — the
# mechanism working, not a coincidence.)
_DEFERRED_CHAOS_TOOLS: Final = frozenset({"seed_dlq_messages"})


def _chaos_schemas_from_snapshot(payload: object) -> dict[str, dict[str, Any]]:
    """Chaos tools in a parsed ``tools/list`` snapshot: name → ``inputSchema``.

    Selected by the same structural ``[chaos:`` description prefix used for
    the name closure, minus deferrals. A chaos tool that somehow ships
    without an ``inputSchema`` maps to an empty schema, which admits any
    arguments — the same "nothing to check against" posture the per-property
    type walk takes below, rather than a spurious load-time failure.
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

    Derived from the committed contract snapshot, read lazily and cached:
    ``evals`` is imported at unit-test collection time and this must not
    cost a file read per scenario. The snapshot is always present in a
    checkout; a missing one is a broken checkout, and the resulting
    ``FileNotFoundError`` says so more usefully than a silent empty set.
    """
    return MappingProxyType(_chaos_schemas_from_snapshot(json.loads(_SNAPSHOT_PATH.read_text())))


def chaos_tool_names() -> frozenset[str]:
    """The closed set of chaos hook names a scenario may declare."""
    return frozenset(chaos_tool_schemas())


def json_types_for(prop: object) -> frozenset[str]:
    """JSON ``type`` names a schema property admits (direct or via ``anyOf``)."""
    if not isinstance(prop, dict):
        return frozenset()
    if "type" in prop:
        return frozenset({str(prop["type"])})
    return frozenset(
        str(branch["type"])
        for branch in prop.get("anyOf", [])
        if isinstance(branch, dict) and "type" in branch
    )


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

    Empty list means the invocation is well formed as far as the committed
    contract can tell. Checks unknown argument names, missing required ones,
    and primitive value types — a name-and-required-only check would miss a
    ``ttl_seconds`` integer→string flip, which is the exact shape of the S-18
    probe in ``tests/unit/test_chaos_schema_alignment.py``.
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
        admitted = json_types_for(properties[argument])
        if not value_compatible(value, admitted):
            errors.append(
                f"{name}.{argument}={value!r} ({type(value).__name__}) is not "
                f"compatible with the snapshot's JSON type(s) {sorted(admitted)}"
            )
    return errors


class ChaosHook(BaseModel):
    """Declarative chaos-hook invocation the runner fires before a live run.

    Moves the "which chaos hook seeds this scenario" mapping from operator
    memory (or the sibling ``make chaos-*`` targets) into the scenario file
    itself. Only invoked when the run is live (``use_live_mcp`` is true AND
    ``PLATFORM_MCP_URL`` is a real endpoint); canned runs ignore it entirely
    since the canned tool responses already encode the broken state.

    ``name`` is a CLOSED SET, not a free string. Chaos seeding runs under
    ``settings.platform_token`` — the full write+chaos principal — and
    ``ChaosClient.call`` forwards the name verbatim as a ``tools/call``, so
    an unconstrained name lets a scenario YAML execute any platform tool,
    Tier-1 writes included, under that principal (S-03).

    ``arguments`` is closed the same way, against the same snapshot entry's
    ``inputSchema``. The name closure alone left half the invocation
    unchecked: a typo'd argument name or a flipped value type validated
    happily at load time and surfaced only as a live ``ChaosInvocationError``
    during seeding — mid-campaign, after the platform had already been
    touched under the write principal and after run startup was paid for.
    Both halves now fail in the same place, at scenario load (G1-07).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, description="Platform hook name, e.g. `inject_latency`.")
    arguments: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _name_is_a_registered_chaos_tool(cls, value: str) -> str:
        allowed = chaos_tool_names()
        if value not in allowed:
            raise ValueError(
                f"{value!r} is not a chaos tool. A chaos hook runs under the full "
                f"write+chaos principal, so its name is a closed set: "
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


class ChaosPlan(BaseModel):
    """The whole fault a scenario manufactures, and how it is put back.

    One ``chaos_setup`` hook was enough while every scenario's world was one
    fault. Multi-fault (Level 5) and cascading (Level 6) worlds need more
    than one, in a DECLARED order — the second hook of a cascade is only
    meaningful after the first has landed — and every one of them has to say
    how the world is restored. A plan is that declaration, in the scenario
    file, where the fault it describes already lives (plan 01 § 4).

    Three fields, and each is a different question:

    * ``setup`` — the hooks that manufacture the world, fired in the order
      written, under the chaos principal. A failure here means the
      BENCHMARK WORLD IS INVALID, not that the agent did anything: the
      runner abandons the scenario ungraded rather than scoring a run
      against a premise nobody established.
    * ``teardown`` — the compensators, fired in a ``finally`` so a crashed
      or killed run still puts the world back. A teardown failure is a
      different event from an agent failure: the run's grade may be
      perfectly valid while the SHARED environment is now contaminated, so
      it is reported separately and blocks further live runs until
      ``make eval-reset``.
    * ``settle_seconds`` — how long the fault needs to become observable
      before preconditions are allowed to look. Chaos is not instantaneous
      (a killed consumer's lag climbs over the platform's metrics
      interval), and this is the plan-level wait that precedes the
      per-probe ``attempts``/``delay_seconds`` polling, not a replacement
      for it.

    Teardown is not mandatory, and that is deliberate: the accepted model is
    **compensators where practical, plus a bounded TTL on the hook, plus the
    authoritative ``make eval-reset``** (plan 05 § A). Most shipped hooks
    already take ``ttl_seconds``, and several have no compensating tool on
    the platform at all — ``kill_consumer`` has no ``revive_consumer``. A
    schema that demanded a teardown tuple would therefore be satisfied by
    fiction. What the schema CAN guarantee is that a declared teardown is a
    real, validated chaos invocation, and that it runs.

    Every member — setup and teardown alike — is a ``ChaosHook``, so both go
    through the same closed-name and snapshot-argument validation. There is
    no second, weaker validator for the teardown half.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    setup: tuple[ChaosHook, ...] = ()
    teardown: tuple[ChaosHook, ...] = ()
    # Bounded so a typo cannot park a paid live run for an hour. The ceiling
    # is generous next to ``PreconditionProbe.delay_seconds`` (60s per
    # attempt) because this is ONE wait for the whole plan rather than a
    # per-attempt one, and a cascade's second-order effect can take minutes
    # of platform loop ticks to appear.
    settle_seconds: float = Field(default=0.0, ge=0.0, le=300.0)

    @property
    def seeds_chaos(self) -> bool:
        """Whether running this plan touches the platform's chaos surface."""
        return bool(self.setup)

    @property
    def hook_names(self) -> tuple[str, ...]:
        """Every hook this plan fires, setup then teardown, in declared order."""
        return tuple(hook.name for hook in self.setup + self.teardown)


class PreconditionField(FieldComparator):
    """One assertion about the world, before the agent is allowed to start.

    ``path`` reads the probe's parsed response, descending into lists at
    ``[]`` — ``total``, or ``items[].remediation_hint``. An assertion holds
    when ANY observed value satisfies it, which is the only useful reading
    for a fixture pack whose row order is not guaranteed.

    ``where`` narrows those values to ONE row first, exactly as it does on
    ``EvidenceFieldExpectation``, and it is here because the any-row reading
    above is cross-satisfiable in the world these preconditions describe. A
    five-row dead-letter queue asserted as ``items[].id equals <chaos row>``
    plus ``items[].remediation_hint is_null true`` is satisfied by two
    DIFFERENT rows — the chaos row being present, and some other row being
    unclassified — so a premise that reads like "the injected fault landed
    unclassified" was in fact two weaker premises side by side. With a
    selector it is one claim about one row:

    ``path: items[].remediation_hint``, ``where: {field: id, equals: <row>}``,
    ``is_null: true``.

    A selector matching no row fails the assertion closed, and the failure
    text says the row was never seen rather than that its field was wrong —
    the same two-diagnoses split the grader side makes, and the one that
    matters most here, because "the chaos hook did not fire" and "it fired
    and wrote the wrong thing" send a reader to different places.
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

    One copy, because two probes now hold the same rule for the same reason
    and a second copy is the drift ``docs/architecture-principles.md`` § 2
    names. ``subject`` is the caller's own noun so the error still reads as a
    sentence about the thing that failed.
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

    Read tools only, enforced below. A precondition establishes what is
    already the case; a probe that changed anything would be manufacturing
    the state it claims to be verifying, and the run would prove nothing.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)
    expect: tuple[PreconditionField, ...] = Field(min_length=1)
    # Chaos effects are not instantaneous — a killed consumer's lag climbs
    # over the platform's 60s metrics interval — so a scenario whose fault
    # takes time to become observable declares how long to wait for it.
    # Defaults to one look, because most preconditions are about seeded
    # state that is either there or is not.
    attempts: int = Field(default=1, ge=1, le=30)
    delay_seconds: float = Field(default=0.0, ge=0.0, le=60.0)

    @field_validator("tool")
    @classmethod
    def _tool_is_read_only(cls, value: str) -> str:
        return _read_only_registered_tool(value, "A precondition")


class GroundTruth(BaseModel):
    """What was actually wrong with the world — the evaluator's copy, never the agent's.

    A scenario manufactures a fault and then grades what the agent concluded
    about it. Until now the only record of the fault was the chaos plan that
    seeded it plus prose in ``description``, so "did the agent name the right
    root cause?" could only be answered by a human reading a trajectory. This
    is that answer written down once, in the enum the agent itself classifies
    into, so the root-cause grader (WP-2.2) reads a label rather than parsing
    English.

    **Evaluator-only, structurally.** Nothing here may reach the agent. That
    is not enforced by remembering to leave it out of a dump — it is enforced
    by ``Scenario.agent_visible``, which builds the agent's whole input from
    an allow-list that this field is not on. See
    ``tests/unit/test_ground_truth_never_leaks.py``.

    **No action fields.** ``acceptable_actions`` / ``forbidden_actions`` are
    deliberately absent: ``ScenarioExpectation.expected_action_tools`` and
    ``forbidden_action_tools`` already carry that fact and are cross-checked
    against ``FIX_MAP`` by ``tests/unit/test_policies.py``. Two sources of
    truth for one fact is how ``FIX_MAP`` drifted for weeks
    (``agent/investigation.py``'s own comment records it), so the second one
    is not created here (plan 01 § 5, divergence C5).

    ``incident_count`` is how many distinct incidents the world holds, which
    is a fact about the *world* and not about ``root_causes``: a cascade is
    one incident with several causes, and two unrelated faults seeded
    together are two incidents. The one pairing that is not a judgement call
    is the level-0 control — nothing is wrong, so the count is zero and the
    only admissible label is ``no_fault`` — and that one is enforced below
    rather than left to a scenario author to get right.
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

        Both directions are refused because both are silently wrong rather
        than loudly wrong. ``root_causes: [no_fault, outbox_stall]`` grades an
        agent correct for saying either "healthy" or "the outbox stalled",
        which are opposite answers; and ``incident_count: 1`` with
        ``root_causes: [no_fault]`` is a world that claims an incident nobody
        can name — the level-0 control's whole point is that the count is
        zero (plan 02 § 5, ``HypothesisCategory.NO_FAULT``).
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

    Evaluator-only, like ``GroundTruth``: a probe listed here is the read a
    correct investigation *would* make, and handing the agent that list would
    be handing it the answer. It exists so a strategy comparison can say
    which run found the distinguishing evidence and which one guessed, rather
    than only which one landed on the right label.

    ``argument_pattern`` maps an argument name to a regular expression the
    agent's own value for that argument must match. A pattern rather than a
    literal because the discriminating fact is usually the *shape* of the
    call — ``group_id`` matching ``^worker-.*``, ``status`` matching
    ``^(replay_safe|human_required)$`` — and an argument the probe does not
    constrain is simply absent from the mapping. Patterns are compiled at
    load, so a broken one is a scenario-load error rather than a grader
    crash halfway through a suite.

    ``tool`` is held to the same rule as ``PreconditionProbe.tool``: a
    registered read tool. A discriminating probe that wrote would change the
    world it is supposed to distinguish.
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

        The tool name must be equal and every constrained argument must be
        present and match its pattern. An argument the pattern does not name
        is unconstrained, and a constrained argument the agent omitted is a
        miss rather than a vacuous pass — the grader side of "did the run
        take the read that tells the two causes apart?".
        """
        if tool != self.tool:
            return False
        for argument, pattern in self.argument_pattern.items():
            if argument not in arguments:
                return False
            if re.search(pattern, str(arguments[argument])) is None:
                return False
        return True


class AgentVisibleScenario(BaseModel):
    """Everything about a scenario that reaches the agent under test. The whole list.

    This is the trust boundary in plan 00 § 3.1, written as a type. The
    runner used to reach into a ``Scenario`` for each thing it handed the
    agent — the alert here, the tool-call cap there, the canned platform
    responses somewhere else — which meant the set of agent-visible fields
    was whatever those call sites happened to read, discoverable only by
    grepping. A field added to ``Scenario`` for the evaluator was safe only
    for as long as nobody wrote a fifth call site.

    An allow-list projection inverts that. A new field on ``Scenario`` is
    invisible to the agent by construction: it is not on this model, and this
    model is ``extra="forbid"``, so making it visible takes a deliberate edit
    here and a reviewer who sees it. The alternative shape — dump the
    scenario and delete a hand-listed set of keys — fails in the direction
    that matters, because the failure is an omission and omissions are
    silent (``docs/architecture-principles.md`` § 3; LESSONS records the same
    shape going stale twice).

    ``max_tool_calls`` is the one number lifted out of the graded
    ``expectation``, and it belongs here: the agent is *told* its budget
    (ADR 0019), and a ceiling is a constraint on the run rather than a fact
    about the fault.

    ``canned_llm_responses`` is deliberately NOT here. Those are the model's
    own replies, scripted — an output of the agent, not an observation it
    reads — so they are on the evaluator side of this boundary even though
    the harness feeds them into the same run.
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
    alert: AlertPayload
    expectation: ScenarioExpectation
    # One response per tool (served on every call), or a list consumed in
    # order with the last repeating — for scenarios whose canned platform
    # state changes mid-run (e.g. get_dag_state paused false→true across
    # a pause_dag action; v0.4.9 enforced-pause semantics).
    canned_tool_responses: dict[str, ToolResult | tuple[ToolResult, ...]] = Field(
        default_factory=dict
    )
    canned_llm_responses: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    # When True the runner ignores ``canned_tool_responses`` and builds a real
    # ``MCPClient`` against ``settings.platform_mcp_url``. Scenarios using this
    # flag are skipped by ``make eval`` when the URL is still the offline
    # placeholder — ``make eval-live`` (or an env with a real URL) runs them.
    use_live_mcp: bool = False
    # When True the runner ignores ``canned_llm_responses`` and builds a real
    # ``LLMClient`` against ``settings.anthropic_api_key`` for the planner,
    # briefing writer, and judge. Scenarios using this flag are skipped when
    # the API key is the offline placeholder. Non-deterministic — regression
    # gate does not apply.
    use_live_llm: bool = False
    # Optional chaos hook to fire before the run in live mode. Puts scenario
    # setup in the scenario file instead of in operator memory (see the
    # live-eval noise-source lessons doc, "shared mutable environment").
    #
    # LEGACY, and deliberately kept: all 40 shipped scenarios spell their
    # single fault this way, and ``Scenario`` is ``extra="forbid"``, so
    # renaming the field would be a 40-YAML migration for no behavioural
    # gain. It is normalized into a one-hook ``ChaosPlan`` by ``chaos``
    # below, which is what the runner and every gate read.
    chaos_setup: ChaosHook | None = None
    # The composable form: many setup hooks in a declared order, their
    # teardown, and a settle wait. Additive rather than a rename for the
    # reason above. Declare ONE of ``chaos_setup`` / ``chaos_plan``; the
    # validator refuses both, because two spellings of the same world would
    # otherwise silently compose into a third.
    chaos_plan: ChaosPlan | None = None
    # What must be true of the world before the agent is allowed to start.
    # Live-only: canned runs serve the broken state by construction, so
    # there is nothing to establish. An unmet precondition abandons the run
    # BEFORE any model call, and reports that the fault was never
    # manufactured rather than grading the agent on a false premise.
    expected_precondition: tuple[PreconditionProbe, ...] = ()
    # Why this scenario is held out of the read-only smoke pass despite
    # being eligible for it (WO-R2-123, following WO-R2-41/#151). The value
    # IS the reason; ``None`` means "in the pass".
    #
    # Membership of the smoke pass is otherwise DERIVED — see
    # ``smoke_eligible`` below — so the only thing a human still declares is
    # a deliberate hold-back, and they declare it on the scenario itself.
    # It used to be two comma-separated pattern lists in the Makefile
    # (``SMOKE_ONLY`` / ``SMOKE_EXCLUDE``) which could rot in three ways the
    # scenario-local field cannot: a renamed scenario left a pattern
    # matching nothing, a new eligible scenario nobody listed never ran, and
    # an exclusion could name a scenario that no longer existed. A field
    # travels with the rename, applies to the new scenario the moment it
    # lands, and cannot name a scenario that is not there.
    #
    # Non-blank and substantive by construction: an exclusion with no reason
    # is the un-recorded gap this mechanism exists to prevent, and "skip" is
    # not a reason. Write what would have to be true to lift it.
    smoke_exclusion: str | None = Field(default=None, min_length=20)
    # What was actually wrong, for the evaluator only. Optional: the 41
    # scenarios that predate it carry none, and a scenario without one is
    # simply not root-cause-graded — WP-2.2 reports that coverage rather
    # than back-filling a guess. See ``GroundTruth`` for why no action
    # fields live here.
    ground_truth: GroundTruth | None = None
    # The reads that tell this fault apart from the ones it looks like.
    # Evaluator-only for the same reason: it is the answer key to the
    # investigation, not a hint the agent is entitled to.
    discriminating_probes: tuple[DiscriminatingProbe, ...] = ()

    # Every field above is on exactly one side of the trust boundary in plan
    # 00 § 3.1, and the split is declared here rather than inferred from
    # whichever call sites happen to read what. The pair is checked against
    # ``model_fields`` at import time (below the class), so a field added
    # without a side cannot be imported, let alone shipped: a new field is
    # never agent-visible by accident, and never evaluator-only by accident
    # either. ``AGENT_VISIBLE_FIELDS`` is the list ``agent_visible`` builds
    # from; anything else is the evaluator's.
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

        The runner builds the agent's run from this and nothing else. It is
        an allow-list, so the interesting property is what it does with a
        field it has never heard of: nothing. ``ground_truth`` cannot leak by
        somebody forgetting to exclude it, because there is no exclusion list
        to forget — the projection names what goes in.

        ``max_tool_calls`` is lifted out of ``expectation`` deliberately; see
        ``AgentVisibleScenario`` for why that one number crosses and the rest
        of the graded expectation does not.
        """
        return AgentVisibleScenario(
            alert=self.alert.model_dump(),
            max_tool_calls=self.expectation.max_tool_calls,
            canned_tool_responses=dict(self.canned_tool_responses),
        )

    @property
    def root_cause_graded(self) -> bool:
        """Whether this scenario can be scored on the root cause it declares.

        The coverage number WP-2.2's report is built from: a scenario with no
        ``ground_truth`` is not graded on diagnosis, and saying so is better
        than a silent pass (a dimension nobody can fail is a dimension that
        measures nothing).
        """
        return self.ground_truth is not None

    @property
    def chaos(self) -> ChaosPlan:
        """This scenario's fault plan, whichever spelling declared it.

        The ONE reader of ``chaos_setup`` that anything downstream should
        use. A legacy single hook normalizes to ``ChaosPlan(setup=(hook,))``
        — same hook, same arguments, no teardown and no settle, which is
        exactly what the runner did for it before plans existed — and a
        scenario declaring neither normalizes to the empty plan rather than
        to ``None``, so a caller never has to spell the "no chaos" case
        twice.

        Computed rather than stored because ``Scenario`` is frozen and
        because a stored copy is a second source of truth: a scenario whose
        ``chaos_setup`` and cached plan could disagree is precisely the
        drift the ``extra="forbid"`` + validator pair exists to prevent.
        """
        if self.chaos_plan is not None:
            return self.chaos_plan
        if self.chaos_setup is not None:
            return ChaosPlan(setup=(self.chaos_setup,))
        return ChaosPlan()

    @property
    def seeds_chaos(self) -> bool:
        """Whether this scenario touches the platform's chaos surface at all.

        The predicate every gate keyed off ``chaos_setup is not None`` should
        key off instead: the smoke refusal (ADR 0018, exit 6), the
        one-mutating-scenario refusal (ADR 0020, exit 7), and the
        ``chaos:invoke`` principal guard. A two-hook plan is chaos-seeding
        for all three of them, and ``chaos_setup`` is ``None`` on such a
        scenario — so reading the legacy field directly is how a plan would
        quietly stop being counted.
        """
        return self.chaos.seeds_chaos

    @model_validator(mode="after")
    def _one_spelling_of_the_fault(self) -> Scenario:
        """Refuse both spellings at once, and refuse a plan that seeds nothing.

        Both-at-once has no defensible reading: firing ``chaos_setup`` and
        then the plan's setup composes a world neither declaration
        describes, and firing only one silently ignores the other.

        A ``chaos_plan`` with an empty ``setup`` is the second refusal. It is
        reachable two ways and both are mistakes: a teardown-only plan
        compensates a fault nobody seeded, and an all-empty plan reads as
        "this scenario seeds chaos" to a human while ``seeds_chaos`` reads
        False — which would put a scenario the author believed was gated out
        of the smoke pass straight back into it.
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

        The runner's own two selection refusals, not a second opinion about
        what "read-only" means:

        * ``seeds_chaos`` — ``--smoke`` refuses the whole run (exit 6, S-03,
          ADR 0018) if any selected scenario seeds chaos, because chaos
          seeding fires under the full write+chaos ``PLATFORM_TOKEN`` and
          that is precisely the claim the read-only stage exists to disprove.
          Read through ``seeds_chaos``, not ``chaos_setup``: a two-hook
          ``chaos_plan`` leaves the legacy field ``None``, and a smoke pass
          that admitted one would seed two faults under the very principal
          the stage exists to prove cannot write.
        * ``expected_action_tools`` — a graded Tier-1 write, which the
          read-scoped smoke token 403s by design. Such a scenario is
          guaranteed red here and belongs to the remediation stage under the
          full token. ``dlq_human_required_escalates`` is held out by this
          half rather than by hand.

        Note this is NOT ``not canned_only``: the smoke stage deliberately
        mixes canned harness-sanity rows (``noise_*``, ``tool_*``,
        ``planner_stops_immediately``) with live reads, and its report is
        read that way. Seeding no chaos and writing nothing is the line.
        """
        return not self.seeds_chaos and not self.expectation.expected_action_tools

    @property
    def in_smoke_pass(self) -> bool:
        """Eligible, and not deliberately held back. The derivation itself."""
        return self.smoke_eligible and self.smoke_exclusion is None

    @model_validator(mode="after")
    def _smoke_exclusion_is_not_redundant(self) -> Scenario:
        """An exclusion the predicate already covers implies a live decision.

        If the scenario declares ``chaos_setup`` or ``expected_action_tools``
        the runner refuses it from a smoke selection anyway, so a hand-written
        hold-back records a choice nobody still has to make — and would go on
        implying one after the reason stopped being true. This was
        ``test_smoke_exclude_entries_are_real_and_still_needed``; it is a
        load-time refusal now, so the redundant entry cannot be committed.
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

        Canned-only is a statement about the world, not the env: the live
        platform cannot manufacture (or expose) the fault this scenario
        grades, so a "live" run would grade the agent against a premise
        that does not exist. The runner refuses such a selection under
        ``--live`` (exit 8) instead of silently serving the canned
        fixtures inside a live report. Each canned-only scenario's YAML
        carries the reason and the platform change that unblocks it,
        right above the ``use_live_*`` flags.
        """
        return not (self.use_live_mcp or self.use_live_llm)


def _classify_every_scenario_field() -> None:
    """Refuse, at import, a ``Scenario`` field nobody put on a side of the boundary.

    This is a module-level check rather than a test because the failure it
    guards is the quiet one. A new evaluator-only field is safe today — the
    projection ignores it — and stays safe only while nobody widens the
    projection; a new agent-visible field that nobody declared is invisible
    to every leak test that walks the declared sets. Either way the mistake
    is an omission, and an omission does not announce itself in a diff. Made
    at import, it announces itself the first time anything loads a scenario.

    Raised as ``RuntimeError`` rather than ``assert`` so ``python -O`` cannot
    turn the guard off.
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
