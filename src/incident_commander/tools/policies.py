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


class PolicyCoverageError(RuntimeError):
    """A registered tool has no tier decision, or has more than one.

    Deliberately not a ``KeyError``: an unclassified tool is not a lookup
    miss the caller might reasonably paper over, it is a missing safety
    decision. Raised by ``tier_of`` at the point of classification and by
    ``ensure_covered`` over the whole registry.
    """


# Explicit map — every tool the registry knows about is classified, in
# exactly one of the three sets below. Adding a tool to the registry
# without adding it here fails ``ensure_covered`` AND ``tier_of``.
#
# The read set is written out rather than inferred as "everything else".
# It used to be inferred: ``tier_of`` returned ``Tier.READ`` for any name
# it did not recognise, which made the guarantee this comment claims a
# fiction — ``ensure_covered`` iterated the registry's own keys and could
# not fail, and a new tool landed as a read tool with no decision taken
# and nothing raised. The default answer to "what may this tool do" is
# now "refuse to say", which is the only safe one (ADR 0003).
_READ_TOOLS: Final[frozenset[str]] = frozenset(
    {
        "get_cache_key_info",
        "get_consumer_lag",
        "get_dag_state",
        "get_deploy_history",
        "get_incident",
        "get_postgres_health",
        "get_redis_health",
        "get_trace",
        "list_active_alerts",
        "list_audit_events",
        "list_dlq_messages",
        "list_incidents",
        "search_traces",
    }
)

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


# Per-tool argument fields whose values NAME a specific platform resource
# (a cache key, a job id, a consumer group, a trace id) — as opposed to
# filters, enums, counts, and free text. A remediation plan may only fill
# these fields with values copied verbatim from the alert or from tool
# results (the evidence ledger): the 2026-08-03 live campaign watched the
# remediation planner re-type an alert's cache key minus its `cache:jobs:`
# prefix, and only the platform's key-prefix allowlist stopped the call
# (ADR 0009's sibling fix; see `remediation._unsourced_resource_args`).
# Copy, don't re-type — enforced structurally, not by prompt prose.
#
# Every tool in TOOL_REGISTRY has an entry, empty when it takes no
# resource-naming args; `tests/unit/test_policies.py` fails if a new tool
# lands without classifying its fields here.
RESOURCE_ARG_FIELDS: Final[dict[str, frozenset[str]]] = {
    # `key` NAMES a resource: same copy-don't-re-type rule as
    # `invalidate_cache_key` below. The read tool is the more likely place
    # for a re-typed key to look harmless, since nothing is mutated.
    "get_cache_key_info": frozenset({"key"}),
    "get_consumer_lag": frozenset({"consumer_group"}),
    "get_dag_state": frozenset({"job_id"}),
    "get_deploy_history": frozenset(),
    "get_incident": frozenset({"id"}),
    "get_postgres_health": frozenset(),
    "get_redis_health": frozenset(),
    "get_trace": frozenset({"trace_id"}),
    "invalidate_cache_key": frozenset({"key"}),
    "list_active_alerts": frozenset(),
    "list_audit_events": frozenset(),
    "list_dlq_messages": frozenset(),
    "list_incidents": frozenset(),
    "mark_dlq_permanent": frozenset({"job_id"}),
    "pause_dag": frozenset({"root_job_id"}),
    "replay_dlq_by_category": frozenset(),
    "replay_dlq_by_ids": frozenset({"job_ids"}),
    "replay_dlq_messages": frozenset(),
    "restart_consumer_group": frozenset({"consumer_group"}),
    "search_traces": frozenset(),
}


def tier_of(tool_name: str) -> Tier:
    """Classify one tool. Anything unclassified raises, never defaults.

    Two failure modes, both closed:

    * Not in ``TOOL_REGISTRY`` → ``KeyError``. Callers should validate
      against the registry first.
    * In the registry but in none of the tier sets →
      ``PolicyCoverageError``. This is the case that used to return
      ``Tier.READ``, which handed an unclassified tool to the
      investigation planner as though someone had decided it was safe.
      Nobody had.
    """
    if tool_name not in TOOL_REGISTRY:
        raise KeyError(f"unknown tool: {tool_name}")
    if tool_name in _TIER_2_TOOLS:
        return Tier.TIER_2
    if tool_name in _TIER_1_TOOLS:
        return Tier.TIER_1
    if tool_name in _READ_TOOLS:
        return Tier.READ
    raise PolicyCoverageError(
        f"{tool_name!r} is in TOOL_REGISTRY but has no tier assignment in "
        "policies.py. Classify it in _READ_TOOLS, _TIER_1_TOOLS or "
        "_TIER_2_TOOLS — an unclassified tool is a policy decision nobody "
        "has taken, and it does not default to read."
    )


def tools_at_or_below(max_tier: Tier) -> frozenset[str]:
    """Every registered tool at or below the given tier.

    Investigation planner asks for ``READ`` — only read tools returned.
    Remediation planner asks for ``TIER_1`` — read + tier-1 returned.
    """
    order = {Tier.READ: 0, Tier.TIER_1: 1, Tier.TIER_2: 2}
    cutoff = order[max_tier]
    return frozenset(name for name in TOOL_REGISTRY if order[tier_of(name)] <= cutoff)


def ensure_covered() -> None:
    """Assert the tier sets partition ``TOOL_REGISTRY`` exactly. From tests.

    Guards against silent drift in both directions: a new tool added to
    the registry with no policy decision, and a tier entry left behind by
    a tool that has since been retired. Also rejects a tool claimed by two
    tiers — ``tier_of`` would quietly answer with the more privileged one
    and every reader of the other set would be wrong.

    This used to iterate the registry's own keys calling ``tier_of``,
    which could not fail while ``tier_of`` defaulted to ``Tier.READ``: the
    check, the comment above ``_READ_TOOLS`` and ADR 0003 all promised a
    coverage guarantee that no code enforced.
    """
    registered = set(TOOL_REGISTRY)
    classified = _READ_TOOLS | _TIER_1_TOOLS | _TIER_2_TOOLS
    problems: list[str] = []
    if unclassified := sorted(registered - classified):
        problems.append(
            f"in TOOL_REGISTRY with no tier: {', '.join(unclassified)} — "
            "classify each in _READ_TOOLS, _TIER_1_TOOLS or _TIER_2_TOOLS"
        )
    if stale := sorted(classified - registered):
        problems.append(
            f"assigned a tier but not in TOOL_REGISTRY: {', '.join(stale)} — drop the stale entry"
        )
    overlaps = sorted(
        (_READ_TOOLS & _TIER_1_TOOLS)
        | (_READ_TOOLS & _TIER_2_TOOLS)
        | (_TIER_1_TOOLS & _TIER_2_TOOLS)
    )
    if overlaps:
        problems.append(f"classified in more than one tier: {', '.join(overlaps)}")
    if problems:
        raise PolicyCoverageError(
            "tier policy does not cover the registry — " + "; ".join(problems)
        )
