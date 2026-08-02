"""Contract snapshot comparison.

The committed snapshot at ``contracts/platform-tools.snapshot.json`` captures
the platform's ``tools/list`` response as of the pinned image:

- ``inputSchema``  — advertised by the platform.
- ``outputSchema`` — also advertised by the platform since v0.4.8 (PR #88).
  The agent's local ``TOOL_REGISTRY`` output models must match; a separate
  unit test (``tests/unit/test_registry_matches_snapshot.py``) enforces
  that direction so registry drift can't diverge from platform truth.

Compared against a fresh fetch, we surface three deltas:

- ``added``   — tool present live but not in the committed snapshot
- ``removed`` — tool present in the committed snapshot but not live
- ``changed`` — tool present in both, but description / inputSchema /
                outputSchema differs

v0.4.4's silent output drift was the reason ``outputSchema`` was added
to the snapshot. Regenerate with ``make snapshot`` whenever the pinned
platform image bumps.

The functions here are pure — the caller passes the platform response
and we read every field off the wire, so this module has no dependency
on ``tools/registry.py`` and stays unit-testable without a running
platform.
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

    Tools are sorted alphabetically by name. Fields kept are ``name``,
    ``description``, ``inputSchema``, and ``outputSchema``. Every one
    is read directly off the platform's ``tools/list`` response.
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
    """Just the fields we snapshot — no volatile server-side metadata."""
    return {
        "name": tool["name"],
        "description": tool.get("description", ""),
        "inputSchema": tool.get("inputSchema") or {},
        "outputSchema": tool.get("outputSchema") or {},
    }
