"""Scenario schema. A scenario is a triggering alert plus a scored expectation.

Scenarios drive the eval runner: the runner starts a run from the alert, drives
the state machine to a terminal state, and calls the grader with the scenario's
``expectation``. Canned tool responses let the runner exercise the agent offline
against a fake platform — one response per tool name is enough for Phase 0's
one-probe shape; more elaborate matching lands with multi-probe scenarios.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from evals.graders.deterministic import FieldComparator, ScenarioExpectation
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
# not in a scenario. The v0.5.0 rebless made it the snapshot's 27th tool;
# excluding it here by construction means that rebless could not, and no
# future one can, silently widen the closed set.
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
                f"{value!r} is not a chaos tool. chaos_setup runs under the full "
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
                f"chaos_setup {self.name!r} arguments disagree with "
                f"contracts/platform-tools.snapshot.json: {'; '.join(errors)}. "
                "Fix the invocation, or — if the platform moved — bump the pinned "
                "digest in demo/compose.yml and re-bless with `make snapshot` "
                "(docs/runbook.md). Never hand-edit the snapshot."
            )
        return self


class PreconditionField(FieldComparator):
    """One assertion about the world, before the agent is allowed to start.

    ``path`` reads the probe's parsed response, descending into lists at
    ``[]`` — ``total``, or ``items[].remediation_hint``. An assertion holds
    when ANY observed value satisfies it, which is the only useful reading
    for a fixture pack whose row order is not guaranteed.
    """

    path: str = Field(min_length=1)


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
        if value not in TOOL_REGISTRY:
            raise ValueError(
                f"{value!r} is not a registered tool. Preconditions probe the platform "
                f"through the same typed registry the agent uses: {sorted(TOOL_REGISTRY)}."
            )
        if tier_of(value) is not Tier.READ:
            raise ValueError(
                f"{value!r} is tier {tier_of(value).value}, and a precondition may only "
                "read. A probe that mutates would manufacture the state it claims to "
                "verify, and the run would prove nothing."
            )
        return value


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
    chaos_setup: ChaosHook | None = None
    # What must be true of the world before the agent is allowed to start.
    # Live-only: canned runs serve the broken state by construction, so
    # there is nothing to establish. An unmet precondition abandons the run
    # BEFORE any model call, and reports that the fault was never
    # manufactured rather than grading the agent on a false premise.
    expected_precondition: tuple[PreconditionProbe, ...] = ()

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
