"""Contract snapshot comparison.

The committed snapshot at ``contracts/platform-tools.snapshot.json`` captures
the platform's ``tools/list`` response as of the pinned image. Compared
against a fresh fetch, we surface three deltas:

- ``added``   — tool present live but not in the committed snapshot
- ``removed`` — tool present in the committed snapshot but not live
- ``changed`` — tool present in both, but description or inputSchema differs

The functions here are pure so they're unit-testable without hitting a
running platform. The integration test wires them up against a live MCP.
"""

from __future__ import annotations

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


def normalize(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Return a stable, order-independent representation.

    Tools are sorted alphabetically by name. Only the fields we snapshot
    (``name``, ``description``, ``inputSchema``) are kept.
    """
    tools = snapshot.get("tools") or []
    normalized: list[dict[str, Any]] = []
    for tool in sorted(tools, key=lambda t: t["name"]):
        normalized.append(_tool_view(tool))
    return {"tools": normalized}


def compare(committed: dict[str, Any], live: dict[str, Any]) -> ContractDiff:
    """Compute the delta from ``committed`` to ``live``."""
    committed_by_name = {t["name"]: _tool_view(t) for t in committed.get("tools") or []}
    live_by_name = {t["name"]: _tool_view(t) for t in live.get("tools") or []}

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


def _tool_view(tool: dict[str, Any]) -> dict[str, Any]:
    """Just the fields we snapshot — no volatile server-side metadata."""
    return {
        "name": tool["name"],
        "description": tool.get("description", ""),
        "inputSchema": tool.get("inputSchema") or {},
    }
