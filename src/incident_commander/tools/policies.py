"""Tier policy: which tools the agent may call in which state.

``READ`` is safe any time. ``TIER_1`` mutates with a bounded, reversible blast
radius the remediation planner may execute directly. ``TIER_2`` needs propose →
approve → execute against a platform approval object, and is not populated yet.
This is the agent-side first filter — the platform decides (CLAUDE.md invariant 2).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final, NamedTuple

from incident_commander.tools.registry import TOOL_REGISTRY


class Tier(StrEnum):
    """Blast-radius classification for one tool, least to most privileged."""

    READ = "read"
    TIER_1 = "tier_1"
    TIER_2 = "tier_2"


class PolicyCoverageError(RuntimeError):
    """A registered tool has no tier decision, or has more than one.

    Not a ``KeyError``: an unclassified tool is a missing safety decision.
    """


# Explicit map — every tool the registry knows about is classified in exactly
# one of the three sets below. A tool added to the registry but not here
# fails ``ensure_covered`` AND ``tier_of``.
#
# The read set is written out, not inferred as "everything else": the default
# answer to "what may this tool do" is "refuse to say" (ADR 0003).
_READ_TOOLS: Final[frozenset[str]] = frozenset(
    {
        "get_cache_key_info",
        "get_circuit_breakers",
        "get_consumer_lag",
        "get_dag_state",
        "get_deploy_history",
        "get_incident",
        "get_outbox_status",
        "get_postgres_health",
        "get_redis_health",
        "get_slo_status",
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


class Resolution(StrEnum):
    """Whether a Tier-1 action can END an incident, or only hold it still.

    Independent of tier (how much damage it can do). Tier-1 used to imply
    resolution, so a stabilizer could carry a run to RESOLVED.
    """

    RESOLVES = "resolves"
    """A verified success removes the incident's cause. The run may RESOLVE."""

    STABILIZES = "stabilizes"
    """A verified success holds the incident still and leaves its cause in
    place. The run executes the action, verifies it landed, and then
    ESCALATES — never RESOLVES. Not a lesser action: buying a human time to
    decide is a legitimate and sometimes the only safe move. It is simply
    not an answer, and a system that reports it as one is reporting a fix
    that fixed nothing."""


class ResolutionPolicy(NamedTuple):
    """One tool's resolution class plus the written reason for it.

    ``remediation.py`` quotes the rationale verbatim to a human — say what is still wrong.
    """

    resolution: Resolution
    rationale: str


# Single source of truth for "can this action end an incident?".
#
# TOTAL over the Tier-1 slice, like ``VERIFY_PROBE_FOR_ACTION``: an absent entry
# is a safety decision nobody took, so ``resolution_class_of`` raises and
# ``tests/unit/test_policies.py::TestResolutionClass`` fails on a Tier-1 tool
# with no entry.
#
# What this closes (2026-09-07): the one RESOLVED transition asked only whether
# the judge said `verified`, which `pause_dag` satisfies trivially — the judge
# was right, the question was wrong.
RESOLUTION_CLASS: Final[dict[str, ResolutionPolicy]] = {
    "pause_dag": ResolutionPolicy(
        Resolution.STABILIZES,
        "pause_dag halts promotion of WAITING children under the root and "
        "self-cleans on its TTL (default 10 minutes), after which the held "
        "children promote again. It changes nothing about the node that "
        "stopped the chain, so when the pause lapses the chain is as stuck "
        "as it was. It also BLOCKS the real fix while it holds: the platform "
        "refuses to replay any job inside a paused DAG "
        "(`find_blocking_pause`, backend/app/utils/dag_pause.py). A pause "
        "buys a human time to decide; it is never the decision.",
    ),
    "restart_consumer_group": ResolutionPolicy(
        Resolution.RESOLVES,
        "clears the kill/latency flags on one consumer group; the group "
        "resumes and its lag drains. The fault is gone, not held.",
    ),
    "invalidate_cache_key": ResolutionPolicy(
        Resolution.RESOLVES,
        "deletes the stale entry. Absence is the fixed state, not a pause on the way to one.",
    ),
    "replay_dlq_by_ids": ResolutionPolicy(
        Resolution.RESOLVES,
        "re-submits the named dead-lettered jobs. An immediate replay puts "
        "the work back on the normal path — and for a dead-lettered DAG "
        "root it is the platform's own un-stick: the replayed root completes "
        "and the resolver promotes the descendants that were waiting.",
    ),
    "replay_dlq_by_category": ResolutionPolicy(
        Resolution.RESOLVES,
        "re-submits every job the platform classified into one category. "
        "Same effect as a by-id replay, chosen by filter instead of by name.",
    ),
    "replay_dlq_messages": ResolutionPolicy(
        Resolution.RESOLVES,
        "legacy bulk re-submit. Kept resolving for parity with the two "
        "targeted replay tools it predates.",
    ),
    # STABILIZES since 2026-09-08 (WO-R2-140, decided by the user); ADR 0026
    # shipped it as RESOLVES with the disagreement recorded rather than
    # settled. The platform's own words point one way — fence, then escalate:
    # the mark "doesn't change job.status — the entry stays in DLQ, just won't
    # be auto-replayed", the planner prompt routes `human_required` to
    # "`mark_dlq_permanent` … then `stop`", and the scenario is named
    # `dlq_human_required_escalates`.
    #
    # So it is the `pause_dag` shape in a different dress: a verified success
    # that holds the incident still. The fence stops a later bulk replay from
    # re-running the poison; the job is still dead and a human fixes the CSV,
    # the producer or the schema. Unlike a pause it does not self-expire and
    # does not block the real fix — a better stabilizer, not a resolution.
    "mark_dlq_permanent": ResolutionPolicy(
        Resolution.STABILIZES,
        "fences one dead-lettered job out of auto-replay — it sets "
        "`remediation_hint=human_required` and writes the operator's reason "
        "to the audit trail, and the platform's own description says it "
        '"doesn\'t change job.status — the entry stays in DLQ". So the job '
        "is still dead and its work is still undone: the fence stops a "
        "later bulk replay from re-running a poisoned payload, and it fixes "
        "nothing about why the payload failed. The bad data, the producer "
        "bug or the schema behind it is untouched and a human still has to "
        "act. Unlike a pause the fence does not expire, so nothing is on a "
        "clock — but 'permanent' describes the fence, not the incident.",
    ),
}


def resolution_class_of(tool_name: str) -> ResolutionPolicy:
    """Classify one Tier-1 action's worth. Unclassified raises, never defaults.

    Not in ``TOOL_REGISTRY`` → ``KeyError``; not Tier-1 or no entry →
    ``PolicyCoverageError``, because "of course it resolves" must not default.
    """
    if tool_name not in TOOL_REGISTRY:
        raise KeyError(f"unknown tool: {tool_name}")
    if tier_of(tool_name) is not Tier.TIER_1:
        raise PolicyCoverageError(
            f"{tool_name!r} is tier {tier_of(tool_name).value}, not Tier-1. "
            "A resolution class describes what a remediation ACTION is "
            "worth; asking it of a read tool has no answer."
        )
    policy = RESOLUTION_CLASS.get(tool_name)
    if policy is None:
        raise PolicyCoverageError(
            f"{tool_name!r} is a Tier-1 action with no entry in "
            "RESOLUTION_CLASS in policies.py. Decide whether a verified "
            "success of this tool REMOVES the incident's cause "
            "(Resolution.RESOLVES) or only holds it still "
            "(Resolution.STABILIZES), and write the reason down beside it. "
            "There is no default: a stabilizer that inherits 'resolves' by "
            "silence reports a fix that fixed nothing."
        )
    return policy


def stabilize_only_tools() -> frozenset[str]:
    """Every Tier-1 tool whose verified success still escalates."""
    return frozenset(
        name
        for name, policy in RESOLUTION_CLASS.items()
        if policy.resolution is Resolution.STABILIZES
    )


# Read tools served from a cache, with the declared staleness window in seconds:
# a reading inside its window may predate the fault — a 60s-cached lag of 0 once
# killed a correct consumer_saturation hypothesis (ADR 0009). The loop uses this
# to decide when a contradicting probe deserves a fresh re-read.
CACHED_READ_FRESHNESS_SECONDS: Final[dict[str, int]] = {
    "get_consumer_lag": 60,
}


def is_cached_read(tool_name: str) -> bool:
    """True when the tool's response is served from a declared staleness window."""
    return tool_name in CACHED_READ_FRESHNESS_SECONDS


# Per-tool argument fields whose values NAME a platform resource (a cache key, a
# job id) rather than filter. A remediation plan may fill these only from values
# copied verbatim out of the alert or the evidence ledger — the planner once
# re-typed a key minus its `cache:jobs:` prefix (ADR 0009). Total over
# TOOL_REGISTRY, and `tests/unit/test_policies.py` fails on an unclassified tool.
RESOURCE_ARG_FIELDS: Final[dict[str, frozenset[str]]] = {
    # `key` NAMES a resource — same copy-don't-re-type rule as the write tool.
    "get_cache_key_info": frozenset({"key"}),
    # No arguments — declared empty rather than omitted (ADR 0003), the same
    # record `get_outbox_status` below is: the question was asked.
    "get_circuit_breakers": frozenset(),
    "get_consumer_lag": frozenset({"consumer_group"}),
    "get_dag_state": frozenset({"job_id"}),
    "get_deploy_history": frozenset(),
    "get_incident": frozenset({"id"}),
    # No arguments, so nothing can name a resource. Declared empty, not
    # omitted (ADR 0003).
    "get_outbox_status": frozenset(),
    "get_postgres_health": frozenset(),
    "get_redis_health": frozenset(),
    "get_slo_status": frozenset(),
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


def _derive_uuid_resource_fields() -> dict[str, frozenset[str]]:
    """Which ``RESOURCE_ARG_FIELDS`` entries the platform types as a UUID.

    DERIVED from the input schemas, not declared (architecture principle #2), and
    ``anyOf``/``items`` are walked so a list or optional UUID field is picked up.
    """

    def _is_uuid(schema: object) -> bool:
        if not isinstance(schema, dict):
            return False
        if schema.get("format") == "uuid":
            return True
        if _is_uuid(schema.get("items")):
            return True
        return any(_is_uuid(branch) for branch in schema.get("anyOf", ()))

    derived: dict[str, frozenset[str]] = {}
    for name, fields in RESOURCE_ARG_FIELDS.items():
        properties = TOOL_REGISTRY[name].input_model.model_json_schema().get("properties", {})
        derived[name] = frozenset(f for f in fields if _is_uuid(properties.get(f)))
    return derived


# Resource-naming fields whose values must be canonical UUIDs, per tool. Total
# over ``TOOL_REGISTRY``, where an empty entry is a *declared* "nothing to
# check" rather than silence.
UUID_RESOURCE_FIELDS: Final[dict[str, frozenset[str]]] = _derive_uuid_resource_fields()


def tier_of(tool_name: str) -> Tier:
    """Classify one tool. Anything unclassified raises, never defaults.

    Not in ``TOOL_REGISTRY`` → ``KeyError``; in it but in no tier set →
    ``PolicyCoverageError``, which used to be a silent ``Tier.READ``.
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
    """Every tool at or below a tier: ``READ`` investigates, ``TIER_1`` remediates."""
    order = {Tier.READ: 0, Tier.TIER_1: 1, Tier.TIER_2: 2}
    cutoff = order[max_tier]
    return frozenset(name for name in TOOL_REGISTRY if order[tier_of(name)] <= cutoff)


def ensure_covered() -> None:
    """Assert the tier sets partition ``TOOL_REGISTRY`` exactly. From tests.

    Catches drift both ways — a new tool with no decision, a tier entry left by
    a retired tool — and a tool claimed by two tiers (ADR 0003's guarantee).
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
