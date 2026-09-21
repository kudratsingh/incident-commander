"""Contract snapshot comparison.

``contracts/platform-tools.snapshot.json`` holds the platform's ``tools/list`` for the pinned
image; ``compare`` reports ``added``/``removed``/``changed`` (WO-R2-130). See ``make snapshot``.
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

    Sorted by name, keeping only the six fields ``_tool_view`` reads.
    """
    tools = snapshot.get("tools") or []
    normalized: list[dict[str, Any]] = []
    for tool in sorted(tools, key=lambda t: t["name"]):
        normalized.append(_tool_view(tool))
    return {"tools": normalized}


def compare(committed: dict[str, Any], live: dict[str, Any]) -> ContractDiff:
    """Compute the delta from ``committed`` to ``live``.

    Both inputs should already be ``normalize``d.
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


def _tool_view(tool: dict[str, Any]) -> dict[str, Any]:
    """Just the fields we snapshot — no volatile server-side metadata.

    ``required_scope``/``is_idempotent`` use ``.get`` with a default, not ``or``:
    ``None`` and ``False`` are REAL answers, and ``or`` would erase them.
    """
    return {
        "name": tool["name"],
        "description": tool.get("description", ""),
        "inputSchema": tool.get("inputSchema") or {},
        "outputSchema": tool.get("outputSchema") or {},
        "required_scope": tool.get("required_scope"),
        "is_idempotent": tool.get("is_idempotent", False),
    }
