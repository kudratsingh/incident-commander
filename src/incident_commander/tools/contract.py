"""Contract snapshot comparison.

The committed snapshot at ``contracts/platform-tools.snapshot.json`` captures
BOTH sides of the tool contract as of the pinned image:

- ``inputSchema``  — from the platform's live ``tools/list`` response.
- ``outputSchema`` — from the agent's local registry (each ``ToolSpec``'s
  ``output_model.model_json_schema()``). MCP doesn't advertise output
  shapes, so the agent's Pydantic models ARE the contract on this side.

Compared against a fresh fetch, we surface three deltas:

- ``added``   — tool present live but not in the committed snapshot
- ``removed`` — tool present in the committed snapshot but not live
- ``changed`` — tool present in both, but description / inputSchema /
                outputSchema differs

v0.4.4's silent output drift is the reason ``outputSchema`` was added.
Regenerate the snapshot with ``make snapshot`` whenever either side moves.

The functions here are pure — the caller passes the platform response
AND a name->output-schema lookup, so this module has no dependency on
``tools/registry.py`` and stays unit-testable without a running platform.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ContractDiff:
    """Per-name deltas between two ``tools/list`` snapshots."""

    added: tuple[str, ...]
    removed: tuple[str, ...]
    changed: tuple[str, ...]

    @property
    def is_empty(self) -> bool:
        return not (self.added or self.removed or self.changed)


def normalize(
    snapshot: dict[str, Any],
    output_schemas: Mapping[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return a stable, order-independent representation.

    Tools are sorted alphabetically by name. Fields kept are ``name``
    + ``description`` + ``inputSchema`` (from the snapshot's tool
    descriptor) + ``outputSchema`` (from ``output_schemas``, keyed by
    tool name; ``{}`` for tools we don't have a local model for, like
    operator-only chaos hooks).
    """
    schemas = output_schemas or {}
    tools = snapshot.get("tools") or []
    normalized: list[dict[str, Any]] = []
    for tool in sorted(tools, key=lambda t: t["name"]):
        normalized.append(_tool_view(tool, schemas.get(tool["name"], {})))
    return {"tools": normalized}


def compare(committed: dict[str, Any], live: dict[str, Any]) -> ContractDiff:
    """Compute the delta from ``committed`` to ``live``.

    Both inputs should already be ``normalize``d — ``compare`` does not
    re-derive ``outputSchema``. That keeps the diff honest: if the
    caller forgot to include current output schemas in ``live``, the
    diff surfaces every tool as changed, which is the correct signal.
    """
    committed_by_name = _index(committed)
    live_by_name = _index(live)

    added = tuple(sorted(set(live_by_name) - set(committed_by_name)))
    removed = tuple(sorted(set(committed_by_name) - set(live_by_name)))
    changed = tuple(
        sorted(
            name
            for name in set(committed_by_name) & set(live_by_name)
            if committed_by_name[name] != live_by_name[name]
        )
    )
    return ContractDiff(added=added, removed=removed, changed=changed)


def _index(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {t["name"]: dict(t) for t in snapshot.get("tools") or []}


def _tool_view(tool: dict[str, Any], output_schema: dict[str, Any]) -> dict[str, Any]:
    """Just the fields we snapshot — no volatile server-side metadata."""
    return {
        "name": tool["name"],
        "description": tool.get("description", ""),
        "inputSchema": tool.get("inputSchema") or {},
        "outputSchema": output_schema,
    }
