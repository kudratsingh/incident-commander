"""Check Tier-1 canned fixtures against the tool's output model, offline.

The live drift check cannot look at these. Probing ``replay_dlq_by_category``
to see what it returns would replay the DLQ, and probing ``pause_dag`` would
pause a DAG, so ``fixture_probe.read_tier_calls`` excludes every Tier-1
fixture by construction. That exclusion is right, and it left the nine
Tier-1 recordings in this repo checked by nothing at all — the one class of
fixture that can invent a field, a type, or a whole response and never be
contradicted.

This is the half of the question that needs no platform: the committed
``contracts/platform-tools.snapshot.json`` carries every tool's
``outputSchema``, so a fixture's KEY SET and TYPES can be compared against
the model the tool actually returns without executing anything. Three
findings, mirroring the live check's vocabulary:

``undeclared_field``
    The fixture carries a key the output model does not have. The platform
    cannot emit it, so any expectation reading it grades the fixture.

``missing_required_field``
    The model declares the field required and the fixture omits it. The
    offline run then serves the agent a response shape no live call
    produces, and the gap only shows up on the paid live run.

``type``
    The fixture's value is of a type the field is not declared to hold.

What this deliberately does NOT check is VALUES, and the two defects that
motivated it were both values: a ``pause_key`` naming a Redis namespace the
platform has never used, and an ``already_marked`` flag contradicting the
``previous_hint`` returned beside it. A JSON Schema of plain strings and
booleans cannot express either. Those needed someone to read the platform's
code, and that remains the only way to catch their class — this check
closes the shape hole and names the value hole rather than pretending to
cover it.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from evals.fixture_drift import CannedCall
from incident_commander.tools.policies import Tier, tier_of
from incident_commander.tools.registry import TOOL_REGISTRY

SNAPSHOT_PATH: Final[Path] = (
    Path(__file__).resolve().parents[1] / "contracts" / "platform-tools.snapshot.json"
)

UNDECLARED_FIELD: Final = "undeclared_field"
MISSING_REQUIRED_FIELD: Final = "missing_required_field"
TYPE: Final = "type"


@dataclass(frozen=True)
class ShapeDefect:
    """One way a canned payload disagrees with its tool's output model."""

    scenario: str
    tool: str
    path: str
    kind: str
    detail: str

    def describe(self) -> str:
        return f"{self.scenario}:{self.tool} {self.path} [{self.kind}] {self.detail}"


def load_output_schemas(path: Path | None = None) -> dict[str, Mapping[str, Any]]:
    """Tool name → ``outputSchema`` from the committed platform snapshot."""
    payload = json.loads((path or SNAPSHOT_PATH).read_text())
    return {
        tool["name"]: tool["outputSchema"]
        for tool in payload.get("tools", [])
        if isinstance(tool, Mapping) and "outputSchema" in tool
    }


def write_tier_calls(calls: Iterable[CannedCall]) -> tuple[CannedCall, ...]:
    """The complement of ``fixture_probe.read_tier_calls``: what nothing probes.

    Unregistered names are excluded here for the same reason they are
    there — ``tier_of`` refuses to classify them, and reporting a typo is
    ``fixture_probe.unregistered_calls``' job, not this one's.
    """
    return tuple(
        call for call in calls if call.tool in TOOL_REGISTRY and tier_of(call.tool) is not Tier.READ
    )


def check_calls(
    calls: Iterable[CannedCall], schemas: Mapping[str, Mapping[str, Any]] | None = None
) -> tuple[ShapeDefect, ...]:
    """Every shape disagreement in the given calls. Makes no network call."""
    known = schemas if schemas is not None else load_output_schemas()
    defects: list[ShapeDefect] = []
    for call in calls:
        schema = known.get(call.tool)
        if schema is None:
            # A registered tool the snapshot does not describe is a snapshot
            # staleness problem, and test_registry_matches_snapshot owns it.
            continue
        defects.extend(check_call(call, schema))
    return tuple(defects)


def check_call(call: CannedCall, schema: Mapping[str, Any]) -> list[ShapeDefect]:
    """One canned payload against one ``outputSchema``."""
    defects: list[ShapeDefect] = []
    defs = schema.get("$defs") or {}

    def record(path: str, kind: str, detail: str) -> None:
        defects.append(
            ShapeDefect(
                scenario=call.scenario,
                tool=call.tool,
                path=path or "<root>",
                kind=kind,
                detail=detail,
            )
        )

    def walk_object(payload: Mapping[str, Any], node: Mapping[str, Any], path: str) -> None:
        properties = node.get("properties")
        if not isinstance(properties, Mapping):
            return  # an unconstrained object claims nothing to disagree with
        for key in sorted(set(payload) - set(properties)):
            record(
                _join(path, key),
                UNDECLARED_FIELD,
                f"the output model declares no field {key!r}",
            )
        for key in sorted(set(node.get("required") or []) - set(payload)):
            record(
                _join(path, key),
                MISSING_REQUIRED_FIELD,
                "the output model declares this field required",
            )
        for key in sorted(set(payload) & set(properties)):
            walk_value(payload[key], properties[key], _join(path, key))

    def walk_value(value: Any, node: Mapping[str, Any], path: str) -> None:
        node = _resolve(node, defs)
        declared = _declared_types(node)
        if declared and not (_schema_types(value) & declared):
            record(
                path,
                TYPE,
                f"declared {'/'.join(sorted(declared))}, fixture carries "
                f"{_short(value)} ({'/'.join(sorted(_schema_types(value))) or 'unknown'})",
            )
            return
        if isinstance(value, Mapping):
            walk_object(value, node, path)
            return
        if isinstance(value, list):
            items = _resolve(node.get("items") or {}, defs)
            for element in value:
                walk_value(element, items, f"{path}[]")

    walk_object(dict(call.payload), schema, "")
    return defects


def _resolve(node: Mapping[str, Any], defs: Mapping[str, Any]) -> Mapping[str, Any]:
    """Follow one ``$ref`` into the schema's own ``$defs``.

    One hop, not a general resolver: the platform's output models nest one
    level (``replay_dlq_by_ids`` returns a list of ``ReplayResult``), and a
    ``$ref`` this cannot follow leaves an unconstrained node, which reports
    nothing rather than reporting a guess.
    """
    ref = node.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/$defs/"):
        target = defs.get(ref.removeprefix("#/$defs/"))
        if isinstance(target, Mapping):
            return target
    return node


def _declared_types(node: Mapping[str, Any]) -> set[str]:
    """The JSON Schema type names this node allows. Empty means unconstrained."""
    declared = node.get("type")
    if isinstance(declared, str):
        return {declared}
    if isinstance(declared, list):
        return {t for t in declared if isinstance(t, str)}
    union = node.get("anyOf") or node.get("oneOf")
    if isinstance(union, Sequence):
        types: set[str] = set()
        for member in union:
            if isinstance(member, Mapping):
                member_types = _declared_types(member)
                if not member_types:
                    return set()  # one unconstrained branch constrains nothing
                types |= member_types
        return types
    return set()


def _schema_types(value: Any) -> set[str]:
    """Every JSON Schema type name that would accept this value."""
    if value is None:
        return {"null"}
    if isinstance(value, bool):
        return {"boolean"}  # NOT number: JSON Schema says a bool is not an int
    if isinstance(value, int):
        return {"integer", "number"}
    if isinstance(value, float):
        return {"integer", "number"} if value.is_integer() else {"number"}
    if isinstance(value, str):
        return {"string"}
    if isinstance(value, Mapping):
        return {"object"}
    if isinstance(value, Sequence):
        return {"array"}
    return set()


def _join(path: str, key: str) -> str:
    return f"{path}.{key}" if path else key


def _short(value: Any, limit: int = 40) -> str:
    text = json.dumps(value, default=str)
    return text if len(text) <= limit else text[: limit - 1] + "…"
