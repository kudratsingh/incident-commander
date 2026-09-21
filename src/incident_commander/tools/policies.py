"""Tier policy: which tools the agent may call in which state.

``READ`` changes nothing and is safe at any time; ``TIER_1`` changes something, but only within a
small and reversible radius; ``TIER_2`` needs a human to approve it first and holds no tools yet.
This is the agent's own first filter — the platform decides again on every call (invariant 2).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final, NamedTuple

from incident_commander.tools.registry import TOOL_REGISTRY


class Tier(StrEnum):
    """How much one tool could change, listed from the least privileged to the most."""

    READ = "read"
    TIER_1 = "tier_1"
    TIER_2 = "tier_2"


class PolicyCoverageError(RuntimeError):
    """A registered tool has no tier decision, or has more than one.

    Its own type rather than a ``KeyError`` because it means a safety decision was never taken,
    which is a different problem from asking about a tool that does not exist.
    """


# Every tool in the registry belongs to exactly one of the three sets below. One that belongs to
# none makes both ``tier_of`` and ``ensure_covered`` raise, rather than being treated as a read;
# that is why even the harmless read tools are listed by hand instead of inferred (ADR 0003).
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
        # The v0.4.0 dead-letter-queue tools. Safe to repeat (the platform dedupes them) and no
        # more privileged than the three above, so they sit in the same tier.
        "replay_dlq_by_ids",
        "replay_dlq_by_category",
        "mark_dlq_permanent",
    }
)

_TIER_2_TOOLS: Final[frozenset[str]] = frozenset()


class Resolution(StrEnum):
    """Whether a Tier-1 action can END an incident, or only hold it still.

    A separate question from the tier, which says how much damage an action could do. Being
    Tier-1 used to imply the action resolved things, which let an action that merely bought time
    carry a run all the way to RESOLVED — a fix reported for an incident still broken.
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

    ``remediation.py`` quotes ``rationale`` word for word to the human it escalates to, so each
    one is written as an explanation of what is still wrong after the action succeeds.
    """

    resolution: Resolution
    rationale: str


# The one place "can this action end an incident?" is answered. Every Tier-1 tool needs an entry:
# a missing one makes ``resolution_class_of`` raise instead of guessing, and
# ``tests/unit/test_policies.py`` fails if a Tier-1 tool is ever added without one.
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
    # Changed from RESOLVES to STABILIZES by WO-R2-140, amending ADR 0026: fencing the job stops a
    # later bulk replay from re-running a poisoned payload, but the job is still dead and a human
    # still has to fix the data. The scenario that grades this is `dlq_human_required_escalates`.
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
    """Say whether a verified success of this Tier-1 action ends the incident or only holds it.

    An unknown tool raises ``KeyError``. A tool that is not Tier-1, or a Tier-1 tool with no entry
    in ``RESOLUTION_CLASS``, raises ``PolicyCoverageError``: there is deliberately no default,
    because "of course it resolves" is how a run reports a fix that fixed nothing.
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


# Read tools the platform answers from a cache, and how many seconds old that answer may be. A
# reading this old can predate the fault entirely, so when one contradicts a live hypothesis the
# investigation is allowed to read it again before believing it (ADR 0009).
CACHED_READ_FRESHNESS_SECONDS: Final[dict[str, int]] = {
    "get_consumer_lag": 60,
}


def is_cached_read(tool_name: str) -> bool:
    """True when the tool's response is served from a declared staleness window."""
    return tool_name in CACHED_READ_FRESHNESS_SECONDS


# For each tool, the argument fields whose value NAMES a particular thing on the platform (a cache
# key, a job id) rather than filtering a list. A plan may fill these only by copying a value word
# for word from the alert or the evidence ledger, never by inventing or retyping one (ADR 0009).
# Every tool in the registry needs an entry; `tests/unit/test_policies.py` fails if one is missing.
RESOURCE_ARG_FIELDS: Final[dict[str, frozenset[str]]] = {
    # `key` names one cache entry, so the same copy-it-exactly rule applies as to the tool that
    # deletes that entry — a mistyped key reads a different entry and proves nothing.
    "get_cache_key_info": frozenset({"key"}),
    # Takes no arguments. Written as an empty set rather than left out, so the table is complete
    # and a missing entry always means "nobody decided" (ADR 0003).
    "get_circuit_breakers": frozenset(),
    "get_consumer_lag": frozenset({"consumer_group"}),
    "get_dag_state": frozenset({"job_id"}),
    "get_deploy_history": frozenset(),
    "get_incident": frozenset({"id"}),
    # Takes no arguments either, so nothing it is given can name a resource.
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
    """Which of the resource-naming fields above the platform declares to be UUIDs.

    Worked out from each tool's own input schema rather than listed by hand, so this cannot drift
    from the schemas (architecture-principles rule 2). Optional and list-valued fields are walked
    through their ``anyOf`` and ``items`` branches, so a ``list[UUID]`` counts as UUID-typed.
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


# Per tool, the resource-naming fields whose values have to be well-formed UUIDs. Every tool in the
# registry appears, so an empty entry says "checked, nothing to validate here" rather than nothing.
UUID_RESOURCE_FIELDS: Final[dict[str, frozenset[str]]] = _derive_uuid_resource_fields()


def tier_of(tool_name: str) -> Tier:
    """Say which tier one tool is in. Anything unclassified raises, and nothing defaults.

    A tool that is not in ``TOOL_REGISTRY`` raises ``KeyError``; one that is in the registry but in
    no tier set raises ``PolicyCoverageError``. That case used to return ``Tier.READ`` silently,
    which let an unreviewed action tool through the investigation gate as if it were harmless.
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
    """Check that the three tier sets cover ``TOOL_REGISTRY`` exactly, once each. Called by tests.

    It catches drift in both directions — a newly registered tool nobody classified, and a tier
    entry left behind by a retired tool — plus a tool claimed by two tiers at once, which is what
    would make "exactly one tier per tool" (ADR 0003) an assumption instead of a fact.
    """
    registered = set(TOOL_REGISTRY)
    classified = _READ_TOOLS | _TIER_1_TOOLS | _TIER_2_TOOLS
    problems: list[str] = []
    # 1. A tool the agent can call that nobody has assigned a tier: the safety decision is missing.
    if unclassified := sorted(registered - classified):
        problems.append(
            f"in TOOL_REGISTRY with no tier: {', '.join(unclassified)} — "
            "classify each in _READ_TOOLS, _TIER_1_TOOLS or _TIER_2_TOOLS"
        )
    # 2. A tier entry for a tool that no longer exists: harmless today, misleading tomorrow.
    if stale := sorted(classified - registered):
        problems.append(
            f"assigned a tier but not in TOOL_REGISTRY: {', '.join(stale)} — drop the stale entry"
        )
    # 3. A tool listed in two tiers: ``tier_of`` would answer with whichever set it checks first,
    #    so the same tool would be read-only or an action depending on the order of the code above.
    overlaps = sorted(
        (_READ_TOOLS & _TIER_1_TOOLS)
        | (_READ_TOOLS & _TIER_2_TOOLS)
        | (_TIER_1_TOOLS & _TIER_2_TOOLS)
    )
    if overlaps:
        problems.append(f"classified in more than one tier: {', '.join(overlaps)}")
    # 4. Report every problem found in one message, so one fix-and-rerun cycle clears them all.
    if problems:
        raise PolicyCoverageError(
            "tier policy does not cover the registry — " + "; ".join(problems)
        )
