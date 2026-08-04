"""Tier policy: which tools the agent may call in which state.

Every tool in ``TOOL_REGISTRY`` is classified into one of:

- ``READ``: safe to call any time. Investigation planner uses these to
  gather evidence.
- ``TIER_1``: state-mutating but the platform-side blast radius is
  bounded and reversible (idempotent restart, single-key cache flush,
  DLQ replay with TTL-scoped pause). Remediation planner may propose;
  agent executes directly, no human approval required.
- ``TIER_2``: state-mutating with wide blast radius or hard to reverse.
  Requires propose → approve (human) → execute against a platform
  approval object. **Not populated yet** — lands with Wave 3 PR F on
  the platform side.

This module is the *agent-side* first filter. The platform enforces the
final authorization decision (per invariant 2 in CLAUDE.md). Both layers
must agree before any Tier-1+ call succeeds.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from incident_commander.tools.registry import TOOL_REGISTRY


class Tier(StrEnum):
    """Blast-radius classification for one tool.

    Ordered from least to most privileged. Comparisons like
    ``tier > Tier.READ`` are used to guard proposal paths.
    """

    READ = "read"
    TIER_1 = "tier_1"
    TIER_2 = "tier_2"


# Explicit map — every tool the registry knows about is classified.
# Adding a tool to the registry without adding it here fails ``ensure_covered``.
_TIER_1_TOOLS: Final[frozenset[str]] = frozenset(
    {
        "restart_consumer_group",
        "pause_dag",
        "replay_dlq_messages",
        "invalidate_cache_key",
        # v0.4.0 DLQ categorization tools. All idempotent, all bounded by
        # platform-side scope check (actions:execute) + tier policy here.
        "replay_dlq_by_ids",
        "replay_dlq_by_category",
        "mark_dlq_permanent",
    }
)

_TIER_2_TOOLS: Final[frozenset[str]] = frozenset()

# Read tools whose responses come from a cache rather than a live read,
# with the platform-declared staleness window in seconds. A reading from
# one of these taken inside its window may predate the fault entirely —
# the 2026-08-03 live campaign watched a 60s-cached lag value of 0, read
# 10s after chaos injection, kill a correct consumer_saturation hypothesis
# at 0.75 confidence (ADR 0009). The investigation loop uses this map to
# decide when a contradicting probe deserves one fresh re-read before the
# hypothesis dies. Extend as the platform declares freshness on more tools;
# instant DB-backed reads do not belong here.
CACHED_READ_FRESHNESS_SECONDS: Final[dict[str, int]] = {
    "get_consumer_lag": 60,
}


def is_cached_read(tool_name: str) -> bool:
    """True when the tool's response is served from a declared staleness window."""
    return tool_name in CACHED_READ_FRESHNESS_SECONDS


def tier_of(tool_name: str) -> Tier:
    """Classify one tool. Unknown tools raise — callers should validate
    against ``TOOL_REGISTRY`` first."""
    if tool_name not in TOOL_REGISTRY:
        raise KeyError(f"unknown tool: {tool_name}")
    if tool_name in _TIER_2_TOOLS:
        return Tier.TIER_2
    if tool_name in _TIER_1_TOOLS:
        return Tier.TIER_1
    return Tier.READ


def tools_at_or_below(max_tier: Tier) -> frozenset[str]:
    """Every registered tool at or below the given tier.

    Investigation planner asks for ``READ`` — only read tools returned.
    Remediation planner asks for ``TIER_1`` — read + tier-1 returned.
    """
    order = {Tier.READ: 0, Tier.TIER_1: 1, Tier.TIER_2: 2}
    cutoff = order[max_tier]
    return frozenset(name for name in TOOL_REGISTRY if order[tier_of(name)] <= cutoff)


def ensure_covered() -> None:
    """Assert every registered tool has a tier assignment. Called from tests.

    Guards against silent drift where a new tool is added to
    ``TOOL_REGISTRY`` without a policy decision.
    """
    for name in TOOL_REGISTRY:
        _ = tier_of(name)  # raises if missing
