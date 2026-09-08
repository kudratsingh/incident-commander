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
from typing import Final, NamedTuple

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


class Resolution(StrEnum):
    """Whether a Tier-1 action can END an incident, or only hold it still.

    Tier says how much damage an action can do. This says what a *successful*
    one is worth. They are independent questions and conflating them is what
    produced the bug this class exists to close: every Tier-1 tool was
    implicitly a resolution, so any verified action could carry a run to
    RESOLVED — including one whose entire effect is to stop the clock.
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

    The rationale is not decoration. It is the sentence a human reads in the
    escalation briefing when a stabilizer fires (``remediation.py`` quotes it
    verbatim), so it has to say what is still wrong and what will happen if
    nobody acts.
    """

    resolution: Resolution
    rationale: str


# Single source of truth for "can this action end an incident?".
#
# TOTAL over the Tier-1 slice, deliberately, and for the same reason
# ``VERIFY_PROBE_FOR_ACTION`` is: an absent entry is a safety decision
# nobody took. ``resolution_class_of`` raises rather than defaulting, and
# ``tests/unit/test_policies.py::TestResolutionClass`` fails on any Tier-1
# tool with no entry — so a tool shipped tomorrow cannot inherit "of course
# it resolves" by silence.
#
# What this closes. Until 2026-09-07 the remediation loop had exactly one
# RESOLVED transition and one condition on it: the verification judge said
# `verified`. For `pause_dag` that condition is trivially satisfiable — the
# platform's own tool description says a successful pause "reads as
# paused=true with children still in `waiting`", so a judge handed
# "paused=true, children waiting" against an expectation of "children stop
# advancing" answers `verified`, correctly, and the run reported RESOLVED on
# a chain that was exactly as stuck as before and would be stuck again the
# moment the 10-minute TTL lapsed. The judge was right; the question was
# wrong. No amount of judge prompting fixes that, because the judge is being
# asked whether the action worked, and it did.
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
    # STABILIZES since 2026-09-08 (WO-R2-140, decided by the user). ADR 0026
    # shipped this entry as RESOLVES with the disagreement recorded here
    # rather than settled, because flipping it would have turned a green
    # scenario red and that scenario was queued for a paid live run. The
    # decision is taken now, and it went the way the platform's own words
    # point: **fence, then escalate.**
    #
    # Three readings, all agreeing, none of which were in doubt — what was
    # missing was the decision, not the evidence:
    #
    # * The platform's tool description: the mark "Doesn't change
    #   job.status — the entry stays in DLQ, just won't be auto-replayed."
    #   The handler backs that up: it flips `remediation_hint` to
    #   `human_required` and writes an audit row, and touches nothing else
    #   (platform `mcp/tools/actions/mark_dlq_permanent.py`).
    # * The remediation planner prompt routes `human_required` as
    #   "`mark_dlq_permanent` … then `stop` (escalate)".
    # * The scenario exercising it is named `dlq_human_required_escalates`
    #   and its own description says "correct action is mark_dlq_permanent
    #   per job + escalate".
    #
    # So the fence is the `pause_dag` shape in a different dress: a verified
    # success that holds the incident still. The poisoned row cannot re-fail
    # a replay it is now excluded from — that is real and worth doing first,
    # because a later bulk replay by another operator (or by this agent on a
    # later incident) would otherwise re-run it — and the job is still dead,
    # its work still undone, its source data still wrong. Nothing about the
    # cause moved. A human fixes the CSV, the producer, or the schema; the
    # mark only makes sure nobody re-runs the poison in the meantime.
    #
    # It differs from a pause in one way worth stating: it does NOT
    # self-expire, and it does not block the real fix. That makes it a
    # *better* stabilizer than a pause and not a resolution — "permanent"
    # names the durability of the fence, never the end of the incident.
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

    Mirrors ``tier_of``'s posture exactly. Three closed failure modes:

    * not in ``TOOL_REGISTRY`` → ``KeyError``;
    * in the registry but not Tier-1 → ``PolicyCoverageError``, because the
      question is meaningless for a read tool and answering it anyway would
      let a caller ask it of one and act on the answer;
    * Tier-1 with no entry → ``PolicyCoverageError``. This is the case that
      must not default: "of course a successful action resolves the
      incident" is precisely the assumption ``pause_dag`` disproved.
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


def _derive_uuid_resource_fields() -> dict[str, frozenset[str]]:
    """Which ``RESOURCE_ARG_FIELDS`` entries the platform types as a UUID.

    DERIVED, not declared, and that is the whole point (architecture
    principle #2). The platform's own input schema already carries the
    answer — ``replay_dlq_by_ids.job_ids`` is ``list[UUID]`` and its JSON
    schema says ``items.format == "uuid"``, ``pause_dag.root_job_id`` says
    ``format == "uuid"`` — and ``tests/unit/test_registry_matches_snapshot.py``
    holds every input model to exact equality with
    ``contracts/platform-tools.snapshot.json``. A hand-written second copy
    of that fact would be a list to keep in sync with a contract that moves
    on the platform's schedule, and the first time it drifted the guard
    reading it would either demand a UUID of a field that is not one or stop
    demanding one of a field that is.

    The distinction it buys is real and narrow: ``get_trace.trace_id`` and
    ``invalidate_cache_key.key`` are resource names with no canonical form —
    the platform accepts any string of the right length — so nothing may be
    asserted about their shape. Six fields across five tools are UUIDs and
    can be checked before the call is ever wired.

    Both shapes are read because a resource field is either scalar
    (``job_id``) or a list of them (``job_ids``); ``anyOf`` is walked so an
    optional UUID field added tomorrow is picked up rather than silently
    dropped.
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


# Resource-naming fields whose values must be canonical UUIDs, per tool.
# Total over ``TOOL_REGISTRY`` (empty frozenset where no resource field is a
# UUID) for the same reason its three sibling maps in ``remediation.py`` are:
# an empty entry is a *declared* "nothing to check here", so a tool shipped
# tomorrow cannot inherit "of course any string is a valid id" by silence.
UUID_RESOURCE_FIELDS: Final[dict[str, frozenset[str]]] = _derive_uuid_resource_fields()


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
