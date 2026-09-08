"""Remediation loop: plan → execute → verify.

Three state transitions land here:

- ``make_llm_plan`` (PLANNING): given confirmed hypothesis + evidence,
  the remediation planner picks one Tier-1 action and one verify probe.
  Output is a ``RemediationPlan`` stored on ``RunState.remediation_plan``.
- ``make_remediate`` (REMEDIATING): executes the plan's action tool with
  a deterministic idempotency key. On tool error → ESCALATED with the
  reason. On success → VERIFYING.
- ``make_llm_verify`` (VERIFYING): calls the plan's verify probe, feeds
  the result + the plan's expectation to a judge LLM. Judge answers
  ``verified`` or ``not_verified`` → RESOLVED or ESCALATED respectively.

Idempotency keys are ``sha256(incident_id|action_tool|sorted_json_args)``
truncated to 32 chars — deterministic across retries, unique across
scenarios. Reconciliation against the platform audit log is PR C's job.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from enum import StrEnum
from typing import Any, Final, Literal, NamedTuple

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from incident_commander.agent.accounting import accrue_llm_error, accrue_llm_usage
from incident_commander.agent.hypothesis import ReadToolName
from incident_commander.agent.investigation import (
    AlertSubject,
    SubjectMatch,
    alert_subject,
)
from incident_commander.agent.state import (
    EvidenceEntry,
    IncidentState,
    RunState,
)
from incident_commander.llm.client import LLMClientProtocol, LLMError
from incident_commander.llm.prompts.loader import load_prompt
from incident_commander.tools.mcp_client import MCPClientProtocol, MCPError
from incident_commander.tools.policies import (
    RESOURCE_ARG_FIELDS,
    UUID_RESOURCE_FIELDS,
    PolicyCoverageError,
    Resolution,
    Tier,
    resolution_class_of,
    tier_of,
)
from incident_commander.tools.registry import TOOL_REGISTRY, description_of
from incident_commander.tools.wire import wire_arguments

# Every Tier-1 tool the platform exposes today. Kept hand-listed for the
# same reason as ``ReadToolName`` in hypothesis.py — Pydantic Literals need
# literal string args at import time. Drift against
# ``tools_at_or_below(Tier.TIER_1) - tools_at_or_below(Tier.READ)`` is caught
# by ``tests/unit/test_policies.py::TestLiteralRegistryDrift::
# test_tier1_tool_name_literal_matches_tier1_slice``.
Tier1ToolName = Literal[
    "invalidate_cache_key",
    "mark_dlq_permanent",
    "pause_dag",
    "replay_dlq_by_category",
    "replay_dlq_by_ids",
    "replay_dlq_messages",
    "restart_consumer_group",
]

_IDEMPOTENCY_KEY_LEN: Final[int] = 32
# One Tier-1 attempt per incident. If the first attempt didn't fix it,
# a human should look at the escalation briefing before we try again.
_MAX_REMEDIATION_ATTEMPTS: Final[int] = 1

# How many times one PLANNING transition may refuse a plan for verifying
# through a probe that cannot observe the resource the action changes,
# before the run escalates instead. Same shape and same reasoning as
# ``investigation._MAX_SUBJECT_PROBE_REFUSALS``: a refusal is a steer, not
# a failure, and the planner gets to act on it — but a planner told once
# and re-offering the same unobservable verify leg is not going to find a
# better one on the third ask, and burning planner tokens to arrive at a
# vaguer reason throws away the one diagnosis worth briefing a human with.
#
# One, not two. The investigation loop can afford two because its refusals
# ride an iteration budget it was going to spend anyway; PLANNING is a
# single LLM call whose only cost is the re-ask.
_MAX_VERIFY_TARGET_REFUSALS: Final[int] = 1

# How many times one PLANNING transition may refuse a plan for replaying a
# job whose dead-letter row nobody read, before the run escalates instead.
#
# One, and unlike the verify-target refusal above the re-ask has a NARROWER
# job to do. The remediation planner cannot make a read — PLANNING is a
# single LLM call, not a loop with a tool budget — so it cannot go and fetch
# the row it is missing. What it CAN do, and the only repair worth an ask,
# is drop the ids it has no row for: a plan replaying one job the listing
# covered and one it did not is repaired by replaying the first. A planner
# with no row for any of its ids has nothing to re-plan toward, and the
# second refusal escalates, naming the read that was skipped. That is the
# intended failure: fail closed toward the human rather than replay a job
# whose classification nobody looked at.
_MAX_UNREAD_ROW_REFUSALS: Final[int] = 1

# And one for the category half (ADR 0028), for a repair that is real and
# different from the by-id one. The planner still cannot make a read, so it
# cannot go and fetch the listing it is missing — but when the run HAS read
# the DLQ and the plan simply picked a different slice of it than the one it
# read, the fix is to act on the slice that is in evidence, and the refusal
# names which those are. That is the shape a planner reaching past its
# evidence actually produces: it read `replay_safe` and proposed sweeping
# `wait_and_replay`. A run with no listing at all has nothing to re-plan
# toward and the second refusal escalates, naming the read that was skipped.
#
# The two read-before-act guards are non-inert over DISJOINT tool sets (a tool
# either names rows or names a filter, never both), so no single plan can spend
# both of THEIR budgets. Neither spends tool-call budget.
_MAX_UNLISTED_CATEGORY_REFUSALS: Final[int] = 1

# And one for the question upstream of all four of them (ADR 0032): does this
# action address the thing the alert is about at all?
#
# The gap this closes had been open since PR #177 shipped the subject guard.
# That guard requires the alert's subject to have been PROBED before a
# `remediate` handoff, and nothing then required the ACTION to target it — so a
# run could read exactly the right thing, describe it correctly, and remediate
# something else. Two live runs did precisely that, thirteen months of campaign
# apart in scenario terms and eight days apart in wall time:
#
# * `adcdcadd94a3` (`remediate_consumer_lag_success`, 2026-08-31): alert
#   `consumer_group: worker-dispatcher`, probe `get_consumer_lag(
#   consumer_group="worker-dispatcher")` → lag 17, then
#   `replay_dlq_by_category(category="replay_safe")` on DLQ furniture that had
#   nothing to do with the killed consumer. The consumer group appears nowhere
#   in the plan. The kill key was never cleared and the run reported RESOLVED.
# * `a0aa257bf865` (`dlq_human_required_escalates`, 2026-09-08): alert about an
#   unclassified dead-letter row, unfiltered listing read, the null-hint row
#   `3971a293…` correctly identified in the plan's own rationale as
#   "must not be touched by auto-replay" — and then
#   `replay_dlq_by_category(category="replay_safe")` again, replaying the one
#   seeded row the scenario forbids, verifying that slice empty, and reporting
#   RESOLVED with a briefing that says "leaving four unresolved".
#
# Both plans were admitted by every guard in this file. Both briefings scored
# 1.0 groundedness from the judge, because every sentence in them was true.
#
# One, matching its four siblings, and the repair is the widest of the five:
# the planner cannot read anything new, but the subject is in the alert it was
# already shown and, for a slice subject, the rows are in the evidence it was
# already shown. A second refusal escalates NAMING the subject, which is the
# outcome that should have happened on both runs above.
#
# WORST CASE GROWS BY ONE PLANNER CALL, and that is stated rather than hidden:
# this guard is non-inert over a tool set that OVERLAPS the other four, so a
# single PLANNING transition can now spend this budget and then one of theirs
# (subject-target, then unread-row, say). Five planner calls is the ceiling for
# one transition where four was. No tool-call budget is spent by any of them,
# and ADR 0008 still allows exactly one action.
_MAX_SUBJECT_TARGET_REFUSALS: Final[int] = 1

# And one for the argument half (ADR 0030), which is the budget this family
# was missing. The three above all fire on a plan that named its resource
# correctly and then reasoned about it wrongly; this one fires on a plan that
# got the reasoning right and fumbled the *transcription*, and until 2026-09-07
# that case had no re-ask at all — ``_unsourced_resource_args`` escalated the
# run on the first offence.
#
# The live run that changed it (`5c8895771fbd`, `dlq_wait_and_replay_success`):
# the planner listed the DLQ, filtered `wait_and_replay`, grouped the two rows
# by dependency, derived a 300 s delay with a correct `action_rationale`, and
# then emitted the second job id with its trailing blocks zero-filled —
# `97d91272-0000-0000-0000-000000000000` for
# `97d91272-9774-5b8e-980b-f0d2fa6ed619`. Its own rationale, its findings and
# the judge's reasoning all quote the id correctly, so nothing about the model's
# *understanding* was wrong. One planner call, then a briefing, and the scenario
# graded red on outcome, action and evidence for a copying slip.
#
# One, matching its three siblings, and the repair is the narrowest of the four:
# the planner cannot fetch anything, but it does not need to — every candidate
# it could want is already in the evidence, and the refusal enumerates them.
# What it must not do is correct the id FOR the model. A harness that silently
# substituted the nearest evidence id would be choosing which job to replay on a
# guess about intent, which is the decision this whole family of guards exists
# to keep with the model and its evidence. The refusal offers candidates; the
# plan that executes is one the model itself emitted.
_MAX_ARGUMENT_REFUSALS: Final[int] = 1

# Evidence marker for a refused plan. Underscore-prefixed per the repo-wide
# convention, so the briefing's evidence trail and the grader's "tools
# called" set both exclude it — a refusal is bookkeeping, not a probe, and
# it spends no tool-call budget.
_PLAN_REFUSED_MARKER: Final[str] = "_plan_refused"
# The same convention for the read-before-act refusal. A separate marker
# rather than a second shape under the one above: both are refusals, but
# they carry different arguments and diagnose different mistakes, and a
# reader of the trail (or of a run archive) should not have to parse the
# reason text to tell "verified the wrong thing" from "acted on an unread
# row".
_PLAN_REFUSED_UNREAD_ROW_MARKER: Final[str] = "_plan_refused_unread_row"
# And the same convention again for the read-before-act refusal's other half
# (ADR 0028): a replay that names a CATEGORY rather than rows. Third marker,
# third shape, for the same reason the second one is separate — its arguments
# name a filter and no ids, and "you swept a category nobody listed" is a
# different diagnosis from "you replayed a row nobody read", even though both
# are the same rule seen from two sides.
_PLAN_REFUSED_UNLISTED_CATEGORY_MARKER: Final[str] = "_plan_refused_unlisted_category"
# Fourth marker, fourth shape (ADR 0030). Its arguments carry the rejected
# values AND the candidates offered back, which is what makes a run archive
# answerable after the fact: "the planner was shown these three ids and still
# emitted a fourth" is a different finding from "the planner was shown
# nothing", and neither is recoverable from the reason prose alone.
_PLAN_REFUSED_ARGUMENT_MARKER: Final[str] = "_plan_refused_argument"
# Fifth marker, fifth shape (ADR 0032). Its arguments carry the alert field
# that named the subject, the subject's value and the kind of target test that
# failed — which is what makes a run archive answerable on the question the
# two live runs behind it could not be asked: "was the action even aimed at the
# incident?" That is not recoverable from a reason string, and it is not the
# same finding as any of the four above: those all fire on a plan aimed at the
# right object, this one on a plan aimed at a different object entirely.
_PLAN_REFUSED_SUBJECT_TARGET_MARKER: Final[str] = "_plan_refused_subject_target"

# Every marker ``_format_plan_context`` must render whole and last. Derived
# membership rather than a comparison against one name, because that is the
# bug the second marker would otherwise have introduced: the renderer
# matched ``_PLAN_REFUSED_MARKER`` exactly, so a refusal written under any
# other name landed inside the 200-character evidence truncation and the
# planner was re-asked with its steer cut mid-sentence — the failure mode
# ``_format_plan_context``'s own comment says the whole-rendering exists to
# prevent. A refusal the model cannot read is a refusal that only spends
# tokens. Adding a fifth refusal shape means adding it here.
_PLAN_REFUSAL_MARKERS: Final[frozenset[str]] = frozenset(
    {
        _PLAN_REFUSED_MARKER,
        _PLAN_REFUSED_UNREAD_ROW_MARKER,
        _PLAN_REFUSED_UNLISTED_CATEGORY_MARKER,
        _PLAN_REFUSED_ARGUMENT_MARKER,
        _PLAN_REFUSED_SUBJECT_TARGET_MARKER,
    }
)

# How much of a rejected value must match a candidate before the refusal is
# willing to say "did you mean". Eight is the first block of a UUID, which is
# also how every human and every log line in this project abbreviates one
# (`af67d1b1…`), so it is the shortest prefix that identifies a row by
# convention rather than by luck. Shorter would start proposing a
# same-first-character coincidence as a correction; longer would have missed
# the run this exists for, whose slip began at character 10.
_MIN_DID_YOU_MEAN_PREFIX: Final[int] = 8

# A canonical UUID as the platform's own input schema defines it (8-4-4-4-12
# hex). Deliberately NOT a semantic check: `97d91272-0000-0000-0000-000000000000`
# — the value that cost the run — matches this happily, and no regex can know
# that a well-formed id names no job. Shape and provenance are two different
# questions and this answers only the first, which is why it reports through
# the same refusal path as the evidence check rather than replacing it.
_CANONICAL_UUID: Final[re.Pattern[str]] = re.compile(
    r"\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z"
)


# How the second refusal opens, per cause. Two clauses rather than one, for
# the same reason the two checks carry different reason texts: a human reading
# the briefing needs to know whether the planner wrote something that could
# never be an id or something that merely is not one of THESE ids. The
# briefing's ``escalation_reason`` is the whole of what a woken operator gets.
_ARGUMENT_ESCALATION_CLAUSE: Final[dict[str, str]] = {
    "malformed": "wrote a resource identifier that is not the shape the platform declares",
    "unsourced": "named a resource that is not one this run read",
}


class ArgumentRefusal(NamedTuple):
    """A plan whose resource arguments are wrong in a way one re-ask repairs.

    Returned by ``_plan_once`` instead of an escalated ``RunState``, so the
    caller's loop can spend a refusal budget on it. The reason text is built
    at the point of detection — where the corpus, the candidates and the
    offending values are all in hand — and the loop wraps it for whichever
    disposition it chooses, exactly as ``_unread_row_reason`` and
    ``_unlisted_category_reason`` are shared between their refusal and their
    escalation.
    """

    kind: Literal["malformed", "unsourced"]
    """Which check produced it. Selects the escalation's opening clause."""
    action_tool: str
    """Recorded on the marker so a reader of the trail knows what was refused."""
    problems: tuple[str, ...]
    """``tool.field=value`` renderings, one per offending value."""
    candidates: tuple[str, ...]
    """The evidence-sourced values offered back, empty when there are none."""
    reason: str
    """The sentence both the refusal and the escalation are built from."""


class VerifyProbe(NamedTuple):
    """A read call that can observe the resource a Tier-1 action changed."""

    tool_name: str
    """Read tool that observes the resource."""
    argument_field: str | None
    """Argument carrying the resource name, or ``None`` when the tool
    observes the resource without being able to name it — ``list_dlq_messages``
    reads the DLQ as a whole and has no per-row argument. For those, tool
    identity is the entire requirement; there is no value to match."""


# Single source of truth for Tier-1 action → the read call that observes
# what it changed.
#
# The sibling map is ``investigation.ALERT_SUBJECT_PROBES``, which answers
# "which probe reads the thing this alert is complaining about?". This one
# answers the same question one step later: "which probe reads the thing
# this action just changed?" Both exist because the honest answer is
# mechanical and the planner kept guessing.
#
# TOTAL over ``Tier1ToolName``, deliberately. An empty tuple is a *declared*
# inert entry, not an omission — it says "the platform exposes no read that
# observes this action's effect on a named resource", which is true of the
# two bulk DLQ tools (they name a category or a job_type, not a resource).
# ``tests/unit/test_policies.py::TestVerifyProbeForAction`` pins the
# totality, so a Tier-1 tool shipped tomorrow cannot be silently inert:
# somebody has to write down which read observes it, or write down that
# none does.
#
# Why this is not merely "the verify leg must name the action's resource":
# ``_misdirected_verify_args`` already says that, and it is inert exactly
# when the verify leg names NO resource — which is the hole. On 2026-09-07
# a live run invalidated `cache:jobs:worker-dispatcher:hot_set`
# (`deleted: true`) and verified with `get_redis_health`, whose
# keyspace_hits/misses are server-wide. Nothing in the lab reads that hot
# set, so the counters were dominated by other traffic (hits frozen at 209
# while misses climbed 437635 → 442480). The judge honestly returned
# not_verified six times and the agent escalated. The agent was right; the
# plan asked a question the world could not answer. The probe that WOULD
# have answered it — `get_cache_key_info` — was already in the evidence
# trail, having read `exists: true` before the deletion.
VERIFY_PROBE_FOR_ACTION: Final[dict[str, tuple[VerifyProbe, ...]]] = {
    # v0.6.0 shipped get_cache_key_info (plat #146/#182) for precisely this:
    # its own docstring says "a suspect entry can be checked before
    # remediation and confirmed gone after, instead of the agent inferring
    # both from the deletion's own return value".
    "invalidate_cache_key": (VerifyProbe("get_cache_key_info", "key"),),
    "restart_consumer_group": (VerifyProbe("get_consumer_lag", "consumer_group"),),
    # pause_dag names `root_job_id`; get_dag_state names `job_id`. Field
    # names need not match across legs — the VALUE is what must line up.
    "pause_dag": (VerifyProbe("get_dag_state", "job_id"),),
    # The DLQ tools name rows. The platform's only observation of a row is
    # the listing, which takes no row argument; a DAG read is the legitimate
    # alternative when the replayed id is a DAG root (remediate_runaway_saga).
    "mark_dlq_permanent": (
        VerifyProbe("list_dlq_messages", None),
        VerifyProbe("get_dag_state", "job_id"),
    ),
    "replay_dlq_by_ids": (
        VerifyProbe("list_dlq_messages", None),
        VerifyProbe("get_dag_state", "job_id"),
    ),
    # Declared inert: these name a category / job_type, not a resource.
    # `_resource_values` returns nothing for them, so there is no resource
    # to demand an observation of, and demanding one would refuse correct
    # bulk-replay plans.
    "replay_dlq_by_category": (),
    "replay_dlq_messages": (),
}


class SourceRow(NamedTuple):
    """The read whose ROWS carry the classification an action depends on."""

    tool_name: str
    """Read tool that lists the rows."""
    rows_field: str
    """Top-level key holding the rows. A plain key, not a path: the one
    listing this map describes is one level deep, and a second copy of the
    eval grader's ``resolve_path`` descent rules is not something ``src``
    can share (evals imports the agent, never the reverse) nor something
    worth duplicating before a tool needs it."""
    id_field: str
    """Field within a row carrying the resource id, matched against the
    action's own resource arguments."""
    decision_field: str
    """The field the read exists to expose — quoted into the refusal so the
    steer says what the planner is being sent to find out, not merely which
    tool to call."""


# Single source of truth for "which read must have SEEN this resource before
# an action may touch it".
#
# The third map in this family and the one that asks about the past.
# ``ALERT_SUBJECT_PROBES`` asks "did anyone read what the alert is about?",
# ``VERIFY_PROBE_FOR_ACTION`` asks "can anyone read what this action will
# change?" — and both are satisfied by a run that never learned whether the
# thing it is about to change is safe to change. This one asks that:
# **before you replay a dead-lettered job, its dead-letter row has to be in
# the evidence**, because the row is the only place its `remediation_hint`
# exists.
#
# Why the hint cannot be inferred from anywhere else, which is the whole
# reason a structural rule was needed rather than a prompt line:
#
# * `get_dag_state`'s node model is five fields — `id`, `type`, `status`,
#   `retry_count`, `created_at`. A dead-lettered DAG root reads `status:
#   dead_letter` there and NOTHING about whether replaying it is safe.
# * `list_dlq_messages` takes no job-id filter, so the row is reached by
#   filtering on the hint or by paging. That cost is the reason the
#   remediation-planner prompt used to tell the planner it did not need the
#   listing for a stuck chain (PR #191), and the reason a run could replay
#   a root on nothing but its status.
# * The platform's own rule, in that tool's description: "A null hint is
#   UNKNOWN, not replay-safe: do not feed those to a categorised replay.
#   Read the error, then replay by explicit id, or fence it with
#   `mark_dlq_permanent`." An unread row is strictly less than a null hint.
#
# The guard requires the ROW, never a particular hint VALUE. Deliberate:
# `wait_and_replay` warrants a deferred replay and `replay_safe` an
# immediate one, both legitimate, and a structural rule that picked between
# them would be making the planner's decision for it. Read it, then decide
# — the deciding is the prompt's job and the scenario's claim.
#
# TOTAL over ``Tier1ToolName``, for the same reason its two siblings are: an
# empty tuple is a *declared* inert entry and
# ``tests/unit/test_policies.py::TestSourceRowForAction`` fails on any
# Tier-1 tool with no entry at all, so a replay tool shipped tomorrow cannot
# inherit "of course it may act on an unread row" by silence.
# The dead-letter listing, as a row source. Named rather than written inline
# because two guards now read it: this map's `replay_dlq_by_ids` entry, and
# ADR 0032's subject-target check, which needs the same rows to answer "does
# the row this action names carry the hint the alert is about?". One
# declaration, and `_row_source_for_subject` finds it by walking the map below
# rather than by importing this name — so a second row source added tomorrow is
# picked up without a second lookup table.
DLQ_ROW_SOURCE: Final[SourceRow] = SourceRow("list_dlq_messages", "items", "id", "remediation_hint")


SOURCE_ROW_FOR_ACTION: Final[dict[str, tuple[SourceRow, ...]]] = {
    "replay_dlq_by_ids": (DLQ_ROW_SOURCE,),
    # Declared inert, each for a stated reason rather than by omission.
    #
    # The two bulk replays name a category or a job_type and no ids, so
    # `_resource_values` yields nothing to look for and there is no row to
    # demand. Their equivalent rule is a statement about the CATEGORY — "a
    # listing in evidence carried a row with this hint" — which is a
    # different check against a different argument, and the five scenarios
    # that would newly bind are queued for paid runs. Filed rather than
    # improvised (WO-R2-143).
    "replay_dlq_by_category": (),
    "replay_dlq_messages": (),
    # `mark_dlq_permanent` names one job_id and its row IS in
    # `list_dlq_messages`, so this entry could be filled today and
    # `dlq_human_required_escalates`'s canned trajectory would still be
    # admitted (it lists the DLQ first, and the id it fences is a row in
    # that listing). It stays inert because fencing is the conservative
    # direction — the mark stops auto-replay, it does not re-run anything —
    # so acting on an unread row cannot cause the harm this guard exists to
    # prevent, and widening a guard onto a scenario with money behind it is
    # the coordinator's call. Filed as WO-R2-144.
    "mark_dlq_permanent": (),
    # No listing classifies a cache key, a consumer group or a DAG root for
    # safety; there is no row to read, so there is nothing to require.
    "invalidate_cache_key": (),
    "restart_consumer_group": (),
    "pause_dag": (),
}


class ListingScope(NamedTuple):
    """One dimension on which a listing and an action can each be narrowed."""

    read_field: str
    """Argument on the read that narrows WHICH ROWS come back."""
    action_field: str | None
    """Argument on the action that narrows WHICH ROWS it touches, or ``None``
    when the action does not narrow on this dimension at all. ``None`` is the
    strictest case, not the laxest: an action that does not narrow spans every
    value, so only a read that did not narrow either can have covered it."""


class SourceListing(NamedTuple):
    """The read whose COVERAGE an action naming no rows depends on."""

    tool_name: str
    """Read tool that lists the rows the action will expand to."""
    rows_field: str
    """Top-level key holding the rows. Present-and-a-list is what makes an
    evidence entry a *reading* rather than merely a call under that name — a
    plain key for the same reason ``SourceRow.rows_field`` is one."""
    decision_field: str
    """The field the read exists to expose, quoted into the refusal."""
    scopes: tuple[ListingScope, ...]
    """Every dimension coverage is judged on. A reading covers the action when
    it is no narrower than the action on EVERY scope; one scope where the read
    filtered and the action did not is enough to refuse."""


# Single source of truth for "which read must have COVERED what this action is
# about to sweep".
#
# The fourth map in the probe family and the sibling of
# ``SOURCE_ROW_FOR_ACTION``: the same rule — *read the thing before you act on
# it* — against the call shape that names no thing. `replay_dlq_by_category`
# names a filter; the platform expands it at execution time; so the rows that
# get replayed are whatever the DLQ holds at that instant, and their count and
# their contents are facts nobody in the run has necessarily seen.
#
# Why the by-id guard cannot cover this, which is why there are two maps:
# ``SOURCE_ROW_FOR_ACTION`` matches the action's own resource arguments
# (``RESOURCE_ARG_FIELDS``) against ids the listing returned. A category
# replay has no resource arguments at all — ``_resource_values`` returns the
# empty set for it — so that guard is inert by construction, and ADR 0027 said
# so and filed this (WO-R2-143). The equivalent statement here is about the
# ARGUMENT rather than a row: "a listing in this run's evidence covered the
# slice this call will expand to".
#
# What counts as covering, and why coverage rather than row presence:
#
# * a reading that filtered on nothing covers every slice — it is the whole
#   queue, null hints included;
# * a reading filtered to exactly the slice the action names covers it;
# * a reading filtered to some OTHER slice covers nothing the action names,
#   and this is the case with a live failure behind it: reading
#   `remediation_hint="replay_safe"` and then sweeping `wait_and_replay` is
#   acting on rows whose error texts nobody opened.
#
# Deliberately NOT "the listing must have returned a row in that category".
# A category replay of a slice that has emptied since the read is a no-op, and
# refusing it would red a correct, cautious run for the world's timing. The
# claim this guard makes is about what the agent LOOKED at, which is the thing
# the agent controls.
#
# TOTAL over ``Tier1ToolName``, for the same reason its three siblings are:
# ``tests/unit/test_policies.py::TestSourceListingForAction`` fails on any
# Tier-1 tool with no entry, so a bulk tool shipped tomorrow cannot inherit
# "of course it may sweep a queue nobody listed" by silence.
SOURCE_LISTING_FOR_ACTION: Final[dict[str, tuple[SourceListing, ...]]] = {
    # `category` is the slice; `job_type` narrows it further on both sides.
    # Both scopes are needed and the second is not decoration: a reading of
    # `list_dlq_messages(job_type="csv_upload", remediation_hint="replay_safe")`
    # saw one type's rows, and `replay_dlq_by_category(category="replay_safe")`
    # with no `job_type` replays every type's — the read is strictly narrower
    # than the action and the guard has to say so.
    #
    # A missing `category` is treated as "spans every category" and so demands
    # an unfiltered reading. It is an invalid call either way (the field is
    # required on `ReplayDlqByCategoryInput`, so `wire_arguments` rejects it
    # later), and this is the fail-closed reading of the two: an action that
    # does not say which slice it replays is at least as broad as one that
    # sweeps.
    "replay_dlq_by_category": (
        SourceListing(
            "list_dlq_messages",
            "items",
            "remediation_hint",
            (
                ListingScope("remediation_hint", "category"),
                ListingScope("job_type", "job_type"),
            ),
        ),
    ),
    # The unfilterable sweep. It takes `job_type` and nothing else, and per
    # the platform's own description it replays uncategorised (null-hint) rows
    # too — the rows for which "nobody has classified this" is the entire
    # finding. `action_field=None` on the hint scope is what says so: no
    # filtered reading can cover this call, only an unfiltered one.
    #
    # Declaring it here does NOT make it permissible. Every DLQ scenario in
    # the suite lists it in `forbidden_action_tools` and those claims are
    # untouched; this map says what the agent would have had to read IF it
    # ever reaches for the tool, which is a different question from whether
    # a scenario grades the reach as a violation. Both hold at once.
    "replay_dlq_messages": (
        SourceListing(
            "list_dlq_messages",
            "items",
            "remediation_hint",
            (
                ListingScope("remediation_hint", None),
                ListingScope("job_type", "job_type"),
            ),
        ),
    ),
    # Declared inert, each for a stated reason rather than by omission.
    #
    # `replay_dlq_by_ids` and `mark_dlq_permanent` name rows, so the question
    # about them is `SOURCE_ROW_FOR_ACTION`'s ("is that row in evidence?") and
    # asking a coverage question as well would refuse a correct by-id replay
    # whose listing was filtered to a different hint than the row's — which is
    # a legitimate way to have read a row, and the row IS the evidence.
    "replay_dlq_by_ids": (),
    "mark_dlq_permanent": (),
    # No listing enumerates cache keys, consumer groups or DAG roots for the
    # platform to expand a filter over; these three name one resource each and
    # nothing here is a filter.
    "invalidate_cache_key": (),
    "restart_consumer_group": (),
    "pause_dag": (),
}


class RemediationPlan(BaseModel):
    """One remediation plan: action + verification.

    ``action_tool`` and ``verify_tool`` are Literal-typed against the
    registry, so the JSON schema exposed to the LLM only accepts real
    tool names. The runtime tier check in ``make_llm_plan`` remains as
    defense-in-depth against registry-drift scenarios.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    target_hypothesis: str = Field(
        min_length=1,
        description="Name of the hypothesis this plan addresses.",
    )
    action_tool: Tier1ToolName = Field(
        description="Tier-1 tool to invoke. Enum-constrained by the schema.",
    )
    action_arguments: dict[str, Any] = Field(
        default_factory=dict,
        description="Arguments for the action tool. idempotency_key is agent-generated.",
    )
    verify_tool: ReadToolName = Field(
        description="Read tool to call after the action. Enum-constrained.",
    )
    verify_arguments: dict[str, Any] = Field(
        default_factory=dict,
        description="Arguments for the verify tool.",
    )
    verify_expectation: str = Field(
        min_length=1,
        description="What the verify tool's response should look like if the fix worked.",
    )
    # Optional, and it exists so that a prompt rule asking the planner to
    # SHOW ITS WORK has somewhere to write the answer.
    #
    # ``model_config`` forbids extra keys, which is the right default for a
    # structured output — but it means an instruction like "state which row
    # set your delay" with no field to hold it does not produce a rationale,
    # it produces a ValidationError, the plan fails to parse, and the run
    # escalates. An instruction the schema cannot carry is worse than no
    # instruction. So the field is added in the same change as the rule that
    # needs it (the ``wait_and_replay`` delay derivation in
    # ``prompts/remediation_planner.md``).
    #
    # Optional rather than required, deliberately: every other plan shape in
    # the corpus is a one-resource action whose argument IS its own
    # justification (`invalidate_cache_key(key=…)` has nothing to explain),
    # and making it required would invalidate every canned plan fixture in
    # the suite to buy a sentence nobody reads. It is free text and nothing
    # grades it — a graded rationale would be a claim about prose, which is
    # the shape this codebase refuses. What IS graded is the number the
    # rationale is about (``expected_action_arguments`` on
    # ``delay_seconds``); this field is how a human reading the trajectory
    # afterwards learns WHY that number, which is the part no assertion can
    # recover.
    action_rationale: str | None = Field(
        default=None,
        description=(
            "Optional. Why these action arguments and not others — required by the "
            "system prompt for a delayed DLQ replay, where the delay is a judgement "
            "derived from the rows rather than a value read off one. Name the row "
            "and the wait it stated. Free text; not graded."
        ),
    )


class VerificationJudgment(BaseModel):
    """Judge LLM's verdict on whether the remediation worked."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: Literal["verified", "not_verified"]
    reasoning: str = Field(min_length=1)


# ---------------------------------------------------------------------------
# Idempotency


def build_idempotency_key(incident_id: str, action_tool: str, arguments: dict[str, Any]) -> str:
    """Deterministic sha256 of incident + tool + args (idempotency_key excluded).

    Same (incident, tool, args) → same key, so a retry within the incident
    hits the platform's idempotency store and returns the original result
    without re-executing. Different incidents get different keys so the
    platform can distinguish concurrent runs.
    """
    # Never include idempotency_key in the hash (it's what we're generating).
    args_for_hash = {k: v for k, v in arguments.items() if k != "idempotency_key"}
    payload = f"{incident_id}|{action_tool}|{json.dumps(args_for_hash, sort_keys=True)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:_IDEMPOTENCY_KEY_LEN]


# ---------------------------------------------------------------------------
# PLANNING


def make_llm_plan(
    llm_client: LLMClientProtocol,
    model: str,
) -> Callable[[RunState, datetime], RunState]:
    """Bind the LLM to the PLANNING transition.

    Reads the current top hypothesis + evidence, asks the remediation
    planner LLM to produce a ``RemediationPlan``. Rejects plans whose
    action tool isn't Tier-1 (defense-in-depth; the prompt already
    lists only Tier-1 tools). Persists the plan on ``RunState``.

    Four resource-argument guards run before anything is wired, all
    pre-execution (ADR 0024). Two escalate:

    - ``_absent_resource_args`` — every resource-naming field on both
      legs must be present, or the registry default silently picks the
      resource for us.
    - ``_misdirected_verify_args`` — the verify probe must not name a
      resource the action left alone.

    and two REFUSE, re-asking once with the evidence's own candidates quoted
    back (ADR 0030):

    - ``_malformed_resource_args`` — a value in a field the platform types as
      a UUID must be one.
    - ``_unsourced_resource_args`` — its value must be one the platform
      itself produced (copy, don't re-type).

    Three further guards then REFUSE rather than escalate, each re-asking the
    plan once with the missing piece named, because unlike the two escalating
    ones above the action they see is not aimed at the wrong object:

    - ``_unread_action_rows`` — the resource this action would replay must
      have been SEEN as a row in the read that classifies it (ADR 0027).
      Checked first: whether the action should happen outranks how it would
      be checked.
    - ``_unlisted_action_scope`` — the same rule for a call that names a
      category instead of rows: some listing in evidence must have covered
      the slice the platform will expand it to (ADR 0028). Non-inert over a
      tool set disjoint from the guard above's.
    - ``_unobserved_action_resource`` — the verify probe must observe the
      acted-on resource at all (ADR 0025).
    """

    def transition_plan(run_state: RunState, at: datetime) -> RunState:
        if not run_state.hypotheses:
            return _escalate_remediation(
                run_state, at, "planning entered with no hypotheses on RunState"
            )
        if run_state.remediation_attempts >= _MAX_REMEDIATION_ATTEMPTS:
            # Invariant guard (ADR 0008): under the current allowed-
            # transition graph, PLANNING is only reachable from
            # INVESTIGATING (attempts == 0). REMEDIATING → VERIFYING
            # is a one-way flight — VERIFYING has no PLANNING successor.
            # If we reach here with attempts >= 1, the state machine
            # graph was mutated without updating ADR 0008, or someone
            # constructed a RunState directly bypassing dispatch.
            # Escalate with a distinct reason so the trajectory doesn't
            # silently swallow the invariant violation.
            return _escalate_remediation(
                run_state,
                at,
                f"invariant violation (ADR 0008): PLANNING reached with "
                f"remediation_attempts={run_state.remediation_attempts} "
                f"(cap={_MAX_REMEDIATION_ATTEMPTS}); the allowed-transition "
                "graph should make this state unreachable",
            )
        remaining_calls = run_state.budget.max_tool_calls - run_state.budget.tool_calls_used
        if remaining_calls < 2:
            # An action we can't afford to verify is worse than no action.
            # Escalate before spending planner tokens or executing anything.
            return _escalate_remediation(
                run_state,
                at,
                f"insufficient tool-call budget for action+verify "
                f"(remaining={remaining_calls}); escalating without executing",
            )

        top = run_state.hypotheses[0]
        refusals_spent = 0
        unread_refusals_spent = 0
        unlisted_refusals_spent = 0
        argument_refusals_spent = 0
        subject_refusals_spent = 0
        while True:
            run_state, outcome = _plan_once(run_state, at, llm_client, model, top.name)
            if isinstance(outcome, ArgumentRefusal):
                # ADR 0030. Checked before anything else in the loop because
                # a mis-transcribed id fails the two read-before-act guards
                # too — no listing carries a row for an id that does not
                # exist — and their steer is the wrong repair for a typo. The
                # unread-row refusal would tell the planner to DROP the id it
                # has no row for, which on the run this was written for turns
                # a two-job delayed replay into a one-job one and grades red
                # for a different reason. Diagnose the transcription first.
                if argument_refusals_spent >= _MAX_ARGUMENT_REFUSALS:
                    return _escalate_remediation(
                        run_state,
                        at,
                        f"planner {_ARGUMENT_ESCALATION_CLAUSE[outcome.kind]}, "
                        f"{argument_refusals_spent + 1} times: {outcome.reason} The "
                        "action was NOT executed — an identifier the evidence does "
                        "not carry names no resource, and ADR 0008 allows one "
                        "attempt.",
                    )
                argument_refusals_spent += 1
                run_state = _refuse_arguments(run_state, at, outcome)
                if run_state.budget.is_exhausted:
                    return _escalate_remediation(
                        run_state,
                        at,
                        "budget exhausted before the plan could be re-asked with an "
                        "evidence-sourced resource id; nothing was executed",
                    )
                continue
            if isinstance(outcome, RunState):
                return outcome
            plan = outcome

            # FIRST of the four plan-shape guards (ADR 0032), and the order is
            # the priority order of the diagnoses. "This action is not aimed at
            # the incident" is upstream of all three checks below it: each of
            # those asks whether a plan aimed at the right object was read for,
            # scoped for or checkable, and answering one of them on a plan
            # aimed at the WRONG object sends the planner to perfect its
            # handling of furniture. On live run `a0aa257bf865` the unlisted-
            # category guard was satisfied — the run had read the whole queue,
            # which covers every slice — so a plan replaying the wrong slice
            # passed every gate here with nothing to say about it.
            #
            # After the argument guards inside `_plan_once`, though, and that
            # order is load-bearing too: a mis-transcribed id names no
            # resource, so it would fail this check as "does not act on the
            # subject" and get steered at the subject when the actual repair is
            # the transcription. Diagnose the typo first (ADR 0030).
            missed_subject = _unaddressed_alert_subject(plan, run_state)
            if missed_subject is not None:
                if subject_refusals_spent >= _MAX_SUBJECT_TARGET_REFUSALS:
                    return _escalate_remediation(
                        run_state,
                        at,
                        f"planner proposed an action that does not address the alert's "
                        f"own subject ({missed_subject.subject.alert_field}="
                        f"{missed_subject.subject.value!r}), "
                        f"{subject_refusals_spent + 1} times: {missed_subject.reason} The "
                        "action was NOT executed — remediating something the alert did "
                        "not report leaves the incident open while reporting a fix, and "
                        "ADR 0008 allows one attempt.",
                    )
                subject_refusals_spent += 1
                run_state = _refuse_subject_target(run_state, at, plan, missed_subject)
                if run_state.budget.is_exhausted:
                    return _escalate_remediation(
                        run_state,
                        at,
                        "budget exhausted before the plan could be re-asked with an action "
                        "aimed at the alert's subject; nothing was executed",
                    )
                continue

            # Checked BEFORE the verify-leg guard, and the order is the
            # priority order of the two diagnoses. "You are about to replay a
            # job whose classification nobody read" is a statement about
            # whether this action should happen at all; "your verify leg
            # cannot observe it" is a statement about how you would check an
            # action that should. Reporting the second first would send the
            # planner to fix the checking of a replay it must not make.
            unread = _unread_action_rows(plan, run_state)
            if unread is not None:
                source, unread_ids = unread
                if unread_refusals_spent >= _MAX_UNREAD_ROW_REFUSALS:
                    return _escalate_remediation(
                        run_state,
                        at,
                        f"planner proposed replaying a dead-lettered job whose own "
                        f"dead-letter row this run never read, "
                        f"{unread_refusals_spent + 1} times: "
                        f"{_unread_row_reason(source, unread_ids, plan)} The action "
                        "was NOT executed — a replay is only safe for a row somebody "
                        "classified, and ADR 0008 allows one attempt.",
                    )
                unread_refusals_spent += 1
                run_state = _refuse_unread_rows(run_state, at, plan, source, unread_ids)
                if run_state.budget.is_exhausted:
                    return _escalate_remediation(
                        run_state,
                        at,
                        "budget exhausted before the plan could be re-asked without "
                        "an unread dead-letter row; nothing was executed",
                    )
                continue

            # The other half of the same rule (ADR 0028), in the same slot and
            # for the same reason: a call that names a category instead of rows
            # is still an action taken on rows nobody looked at. Non-inert over
            # a disjoint tool set from the check above — a tool names rows or
            # names a filter — so at most one of the two can fire on any plan
            # and the order between them is presentation, not precedence.
            unlisted = _unlisted_action_scope(plan, run_state)
            if unlisted is not None:
                listing, readings = unlisted
                if unlisted_refusals_spent >= _MAX_UNLISTED_CATEGORY_REFUSALS:
                    return _escalate_remediation(
                        run_state,
                        at,
                        f"planner proposed a bulk replay over a slice of the "
                        f"dead-letter queue this run never listed, "
                        f"{unlisted_refusals_spent + 1} times: "
                        f"{_unlisted_category_reason(listing, readings, plan)} The "
                        "action was NOT executed — a bulk replay is only safe over "
                        "rows somebody read, and ADR 0008 allows one attempt.",
                    )
                unlisted_refusals_spent += 1
                run_state = _refuse_unlisted_category(run_state, at, plan, listing, readings)
                if run_state.budget.is_exhausted:
                    return _escalate_remediation(
                        run_state,
                        at,
                        "budget exhausted before the plan could be re-asked without an "
                        "unlisted replay category; nothing was executed",
                    )
                continue

            unobserved = _unobserved_action_resource(plan)
            if not unobserved:
                break
            # Refuse and steer, don't escalate: the run is not over, the
            # planner is simply sent back with the probe it should have
            # picked named for it. Same shape as the investigation loop's
            # ALERT_SUBJECT_PROBES handoff refusal (WO-R2-15 follow-up #177),
            # and unlike the three argument guards above, which escalate
            # because a mis-named resource means the planner is reasoning
            # about the wrong object rather than merely checking the right
            # object the wrong way.
            if refusals_spent >= _MAX_VERIFY_TARGET_REFUSALS:
                values = _resource_values(plan.action_tool, plan.action_arguments)
                acted = ", ".join(sorted(values))
                return _escalate_remediation(
                    run_state,
                    at,
                    f"planner proposed a verify leg that cannot observe the "
                    f"remediated resource {refusals_spent + 1} times: "
                    f"{plan.action_tool} changes {acted} but {plan.verify_tool} "
                    f"does not read it. Required: {_probe_options(unobserved, plan)}. "
                    "The action was NOT executed — an action whose effect cannot "
                    "be observed cannot be verified, and ADR 0008 allows one attempt.",
                )
            refusals_spent += 1
            run_state = _refuse_plan(run_state, at, plan, unobserved)
            if run_state.budget.is_exhausted:
                return _escalate_remediation(
                    run_state,
                    at,
                    "budget exhausted before the plan could be re-asked with an "
                    "observable verify leg; nothing was executed",
                )

        entry = EvidenceEntry(
            tool_name="_planner_plan",
            arguments={"target_hypothesis": plan.target_hypothesis},
            result_summary=(
                f"plan: {plan.action_tool}({json.dumps(plan.action_arguments)}) "
                f"then verify via {plan.verify_tool}"
            ),
            timestamp=at,
        )
        return run_state.model_copy(
            update={
                "state": IncidentState.REMEDIATING,
                # Stored as dict so state.py stays free of remediation-loop imports.
                # REMEDIATING + VERIFYING re-validate via ``_load_plan``.
                "remediation_plan": plan.model_dump(mode="json"),
                "evidence": (*run_state.evidence, entry),
                "updated_at": at,
            }
        )

    return transition_plan


def _plan_once(
    run_state: RunState,
    at: datetime,
    llm_client: LLMClientProtocol,
    model: str,
    top_hypothesis_name: str,
) -> tuple[RunState, RemediationPlan | RunState | ArgumentRefusal]:
    """One planner call plus every guard that runs on its own output.

    Three outcomes, distinguished by type:

    * ``RemediationPlan`` — the plan survived every check here.
    * ``RunState`` — a terminal guard escalated the run.
    * ``ArgumentRefusal`` — a resource identifier is wrong in a way one
      re-ask can repair (ADR 0030); the caller spends a refusal budget.

    Split out of ``transition_plan`` when the verify-target guard made that
    function a loop: keeping these checks inline would have meant a stack of
    ``return`` statements inside a ``while`` whose other exit is a ``break``,
    which is exactly the shape that grows a bug the next time someone adds a
    check. The two argument-shape checks live here rather than in the loop
    despite refusing, because they have to run in this position relative to
    the misdirected-verify check — see the comment at their call site.
    """
    try:
        result = llm_client.call(
            system_prompt=load_prompt("remediation_planner"),
            user_message=_format_plan_context(run_state, top_hypothesis_name),
            output_model=RemediationPlan,
            model=model,
        )
    except (ValueError, ValidationError, LLMError) as err:
        run_state = run_state.model_copy(
            update={"budget": accrue_llm_error(run_state.budget, err, model)}
        )
        return run_state, _escalate_remediation(run_state, at, f"planner LLM invalid: {err}")

    # Charge the call the moment it returns, BEFORE the plan is judged.
    # The accrual used to sit after the six validation branches below,
    # every one of which returns early — so a plan the agent rejected was
    # a plan the run got for free, and the rejections are not the rare
    # case: they are what a bad planner does repeatedly. ADR 0015 says
    # the meter may over-report and never under-report; a billed call
    # whose output we threw away is still a billed call. This holds for
    # the re-ask too: a refused plan is billed, then re-asked.
    run_state = run_state.model_copy(
        update={"budget": accrue_llm_usage(run_state.budget, result, model)}
    )
    plan = result.output

    def refuse(reason: str) -> tuple[RunState, RunState]:
        return run_state, _escalate_remediation(run_state, at, reason)

    if plan.action_tool not in TOOL_REGISTRY:
        return refuse(f"planner picked unknown action tool: {plan.action_tool}")
    if tier_of(plan.action_tool) is not Tier.TIER_1:
        return refuse(
            f"planner picked non-Tier-1 action: {plan.action_tool} "
            f"(tier={tier_of(plan.action_tool).value})"
        )
    if plan.verify_tool not in TOOL_REGISTRY:
        return refuse(f"planner picked unknown verify tool: {plan.verify_tool}")
    if tier_of(plan.verify_tool) is not Tier.READ:
        return refuse(f"verify tool must be read-only, got tier={tier_of(plan.verify_tool).value}")
    absent = _absent_resource_args(plan)
    if absent:
        # Say which resource you mean. An omitted field is not a smaller
        # sin than a mis-typed one — `wire_arguments` default-fills it
        # from the platform's input schema, so the call silently targets
        # whatever that default names (WO-R2-15, ADR 0024).
        return refuse(
            "plan rejected before execution: resource argument(s) not "
            f"named by the plan: {', '.join(absent)}. An omitted resource "
            "argument is filled from the platform's input-schema default "
            "at wire time, so the call would target that default's "
            "resource instead of this incident's."
        )
    # The two argument-shape checks REFUSE rather than escalate (ADR 0030) and
    # so return rather than calling ``refuse``. They stay HERE, inside the
    # per-call guard block, rather than moving out to the caller's loop beside
    # the other three refusing guards, and the position is load-bearing: they
    # must run before ``_misdirected_verify_args`` below. A mangled id on the
    # action leg makes a correct verify id look like it names a resource the
    # action never touched, so the misdirection check would fire on the typo
    # first and escalate the run with a diagnosis about the verify leg — which
    # is not wrong so much as unanswerable, since the leg it names is fine.
    malformed = _malformed_resource_args(plan)
    if malformed:
        return run_state, _argument_refusal(plan, run_state, "malformed", malformed)
    unsourced = _unsourced_resource_args(plan, _evidence_value_corpus(run_state))
    if unsourced:
        # Copy, don't re-type: a resource name the platform never uttered
        # is a hallucination risk, not a plan. The live campaign watched
        # the planner drop `cache:jobs:` off an alert-provided key; only
        # the platform's prefix allowlist stopped the call. Since ADR 0030
        # the first offence buys a re-ask carrying the candidates, because
        # the 2026-09-07 run showed the same guard firing on a plan whose
        # reasoning was entirely correct.
        return run_state, _argument_refusal(plan, run_state, "unsourced", unsourced)
    misdirected = _misdirected_verify_args(plan)
    if misdirected:
        # Verify what you changed. A probe aimed at a resource the action
        # never touched reads a healthy number off an untouched system
        # and calls the incident resolved (ADR 0024).
        return refuse(
            "plan rejected before execution: verify probe targets "
            f"resource(s) the action does not: {', '.join(misdirected)}. "
            f"{plan.action_tool} acts on "
            f"{', '.join(sorted(_resource_values(plan.action_tool, plan.action_arguments)))}"
            f"; {plan.verify_tool} must observe the same resource."
        )
    return run_state, plan


def _collect_strings(node: Any, out: set[str]) -> None:
    """Recursively collect every string value in a JSON-shaped structure."""
    if isinstance(node, str):
        out.add(node)
    elif isinstance(node, dict):
        for value in node.values():
            _collect_strings(value, out)
    elif isinstance(node, (list, tuple)):
        for value in node:
            _collect_strings(value, out)


def _evidence_value_corpus(run_state: RunState) -> set[str]:
    """Every string value the platform itself produced during this run.

    Sources: the alert payload, plus tool result summaries that parse as
    JSON — EXCLUDING values the same call's own arguments supplied. An
    echo (``get_consumer_lag`` repeating ``consumer_group`` back) is the
    caller's words returned, not platform production: the platform
    accepts any group name and answers ``lag: null`` for unknown ones, so
    without the exclusion a hallucinated probe argument would launder
    itself into the corpus and whitelist the same wrong name for the
    Tier-1 action (B-08). LLM-authored call arguments and bookkeeping
    entries' prose are deliberately not sources of truth for resource
    names.

    Echo exclusion is per-entry: a value genuinely discovered in one
    call's result stays in the corpus even when a later call echoes it
    back as an argument.
    """
    corpus: set[str] = set()
    _collect_strings(dict(run_state.alert), corpus)
    for entry in run_state.evidence:
        try:
            parsed = json.loads(entry.result_summary)
        except (ValueError, TypeError):
            continue
        result_strings: set[str] = set()
        _collect_strings(parsed, result_strings)
        argument_strings: set[str] = set()
        _collect_strings(entry.arguments, argument_strings)
        corpus |= result_strings - argument_strings
    return corpus


def _resource_values(tool: str, args: Mapping[str, Any]) -> set[str]:
    """Every resource this leg of the plan names, per ``RESOURCE_ARG_FIELDS``.

    List-valued fields (``replay_dlq_by_ids.job_ids``) contribute each
    element. Non-string values are ignored — they cannot name a platform
    resource and the input model rejects them at wire time anyway.
    """
    values: set[str] = set()
    for field in RESOURCE_ARG_FIELDS.get(tool, frozenset()):
        raw = args.get(field)
        for value in raw if isinstance(raw, (list, tuple)) else [raw]:
            if isinstance(value, str):
                values.add(value)
    return values


def _absent_resource_args(plan: RemediationPlan) -> list[str]:
    """Resource-naming fields the plan left out entirely, per leg.

    Absence must be as loud as mis-sourcing, and for one specific reason:
    ``wire_arguments`` validates the plan's arguments against the tool's
    input model, and a field carrying a default is *filled* rather than
    refused. ``GetConsumerLagInput.consumer_group`` defaults to
    ``"worker-dispatcher"``, so a verify leg that omits the group probes
    that group no matter which consumer the action just restarted — the
    run reads a healthy lag off an untouched consumer and reports
    RESOLVED on a still-broken one.

    Checked here rather than by dropping the registry default: the
    default mirrors the platform's own published input schema, and
    ``tests/unit/test_registry_matches_snapshot.py::
    TestInputModelMatchesSnapshot`` holds the model to exact equality
    with the generated contract snapshot. "Name the resource you are
    acting on, and the resource you are checking" is the *agent's*
    requirement of its planner, so it belongs at the plan boundary.

    Required fields are checked too, not just default-carrying ones.
    Omitting a required field today survives planning and only fails
    inside ``wire_arguments`` — for a verify leg, that is *after* the
    Tier-1 action has already executed. Rejecting at plan time keeps the
    whole class pre-execution. ``tests/unit/test_policies.py::
    TestResourceArgFieldsCoverage`` pins that every registry entry is
    covered, so a future optional field cannot reopen this hole.
    """
    problems: list[str] = []
    for leg, tool, args in (
        ("action", plan.action_tool, plan.action_arguments),
        ("verify", plan.verify_tool, plan.verify_arguments),
    ):
        for field in sorted(RESOURCE_ARG_FIELDS.get(tool, frozenset())):
            if field not in args:
                problems.append(f"{leg} {tool}.{field}")
    return problems


def _misdirected_verify_args(plan: RemediationPlan) -> list[str]:
    """Resources the verify probe names that the action never touched.

    The verify leg exists to observe the effect of *this* action. A probe
    aimed somewhere else is not weak verification, it is verification of
    the wrong thing: it reads a healthy number off an untouched resource
    and hands the judge evidence that the incident is over.

    Only enforced when both legs name resources at all, so a verify leg
    that names NO resource passes here. That gap is deliberate and is not
    a licence: ``_unobserved_action_resource`` below is what decides
    whether a resource-free verify is legitimate for this action, and
    ADR 0025 records why the two checks are separate. ``list_dlq_messages``
    after ``replay_dlq_by_ids`` is the legitimate case; ``get_redis_health``
    after ``invalidate_cache_key`` — which this docstring used to name as
    legitimate — is the case that cost a live run.

    Field names need not match across legs (``pause_dag.root_job_id`` is
    verified by ``get_dag_state.job_id``); the values are what must line up.
    """
    action_values = _resource_values(plan.action_tool, plan.action_arguments)
    verify_values = _resource_values(plan.verify_tool, plan.verify_arguments)
    if not action_values or not verify_values:
        return []
    return sorted(verify_values - action_values)


def _unobserved_action_resource(plan: RemediationPlan) -> tuple[VerifyProbe, ...]:
    """The probes that WOULD observe this action, when the plan picked none.

    Empty tuple means the plan is fine — either it verifies through a probe
    that reads the resource it changed, or the action names no resource a
    read tool can observe. A non-empty tuple is the refusal, and its
    contents are the steer: exactly which probe the planner should have
    picked.

    The complement of ``_misdirected_verify_args``. That one asks "does the
    verify leg name a resource the action did NOT touch?" and is inert when
    the verify leg names nothing at all. This one asks the question that
    inertness leaves open: "is there a read that observes what the action
    changed, and did the plan use it?" Together they close the loop —
    verify the right resource, and verify it at all.

    Three inert cases, all deliberate:

    * the action tool has no entry, or an empty one, in
      ``VERIFY_PROBE_FOR_ACTION`` — no read observes a named resource for
      it (the bulk DLQ tools);
    * the action names no resource *value* (all its resource fields are
      absent or non-string) — absence is ``_absent_resource_args``' job,
      and reporting it twice would bury the actionable message;
    * the plan already picked a listed probe, either carrying the acted-on
      value or carrying no resource argument at all.

    Strings only, matching ``_resource_values`` and its two sibling guards:
    a ``job_id`` that arrives as a ``UUID`` object rather than its JSON
    string form is not compared. Plans reaching here come from
    ``record_output`` JSON, where it is always a string.
    """
    probes = VERIFY_PROBE_FOR_ACTION.get(plan.action_tool, ())
    if not probes:
        return ()
    acted = _resource_values(plan.action_tool, plan.action_arguments)
    if not acted:
        return ()
    for probe in probes:
        if plan.verify_tool != probe.tool_name:
            continue
        if probe.argument_field is None:
            # The tool observes the resource without being able to name it.
            # Picking it IS the whole requirement; there is no value to match.
            return ()
        observed = plan.verify_arguments.get(probe.argument_field)
        if isinstance(observed, str) and observed in acted:
            return ()
    return probes


def _rows_read_for(run_state: RunState, source: SourceRow) -> set[str]:
    """Every resource id this run has actually SEEN a row for, per one source.

    Reads the evidence ledger the way ``_evidence_value_corpus`` does — tool
    results are stored as the output model's ``model_dump_json``, so the rows
    are available as parsed JSON — but asks a much narrower question than
    that corpus does, and the narrowing is the point. The corpus collects
    every string the platform ever uttered, so an id echoed by the ALERT or
    read off ``get_dag_state.nodes[].id`` is in it; that is what makes
    ``_unsourced_resource_args`` satisfied by a job whose dead-letter row
    nobody opened. Here only the listing's own rows count, and only the row's
    identifying field.

    Non-string ids are skipped for the same reason the sibling guards skip
    them: plans arrive from ``record_output`` JSON, where an id is always a
    string, and a value that is not one cannot be compared without inventing
    a coercion rule the platform does not have.
    """
    seen: set[str] = set()
    for entry in run_state.evidence:
        if entry.tool_name != source.tool_name:
            continue
        try:
            parsed = json.loads(entry.result_summary)
        except (ValueError, TypeError):
            continue
        if not isinstance(parsed, Mapping):
            continue
        rows = parsed.get(source.rows_field)
        if not isinstance(rows, (list, tuple)):
            continue
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            value = row.get(source.id_field)
            if isinstance(value, str) and value.strip():
                seen.add(value.strip())
    return seen


def _unread_action_rows(
    plan: RemediationPlan, run_state: RunState
) -> tuple[SourceRow, tuple[str, ...]] | None:
    """The source and the ids it has no row for, when the plan acts on one.

    ``None`` means the plan is fine. Three inert cases, all deliberate and
    all mirroring ``_unobserved_action_resource``:

    * the action tool has an empty entry in ``SOURCE_ROW_FOR_ACTION`` — no
      listing classifies the resources it names;
    * the action names no resource value at all — that is
      ``_absent_resource_args``' report to make, and making it twice buries
      the actionable one;
    * every id the action names appears as a row in some listing already in
      evidence.

    Only the FIRST declared source is reported when several would satisfy the
    requirement, because the refusal has to name one concrete call to make;
    an id is nonetheless considered read if ANY declared source carried it.
    """
    sources = SOURCE_ROW_FOR_ACTION.get(plan.action_tool, ())
    if not sources:
        return None
    acted = _resource_values(plan.action_tool, plan.action_arguments)
    if not acted:
        return None
    seen: set[str] = set()
    for source in sources:
        seen |= _rows_read_for(run_state, source)
    unread = tuple(sorted(acted - seen))
    if not unread:
        return None
    return sources[0], unread


def _unread_row_reason(source: SourceRow, unread: tuple[str, ...], plan: RemediationPlan) -> str:
    """The sentence both the refusal and the escalation are built from."""
    return (
        f"{plan.action_tool} would act on {', '.join(unread)}, and no "
        f"{source.tool_name} reading in this run's evidence carries a row for "
        f"{'that id' if len(unread) == 1 else 'those ids'}. The chain view does not "
        f"carry {source.decision_field}: a dead-lettered node reads as status "
        f"'dead_letter' in get_dag_state and nothing there says whether replaying it "
        f"is safe. Call {source.tool_name} first — filter it by "
        f"{source.decision_field}, or page it with offset until the id appears — and "
        f"read that row's {source.decision_field} before replaying. A null "
        f"{source.decision_field} is UNKNOWN, not replay-safe."
    )


def _scope_value(arguments: Mapping[str, Any], field: str | None) -> str | None:
    """The narrowing value on one scope, or ``None`` for "not narrowed".

    A missing key, an explicit JSON ``null`` (which is what
    ``wire_arguments`` writes for every unset optional filter, so the wired
    arguments the evidence ledger stores are full of them), a non-string, and
    a whitespace-only string all read as *not narrowed*. Collapsing them is
    correct in both directions: on the READ side they are the four ways the
    platform was asked for everything, and on the ACTION side they are the
    four ways the plan declined to say which slice it meant — and an action
    that does not say spans all of them.
    """
    if field is None:
        return None
    value = arguments.get(field)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _is_reading(entry: EvidenceEntry, source: SourceListing) -> bool:
    """Whether one evidence entry is a real reading of ``source``, not just its name.

    The rows field has to be there and has to be a list. In practice the
    investigation loop escalates on ``is_error`` before an entry is written,
    so a failed listing never reaches the ledger under its tool name — this
    is the belt to that braces, and it is what stops a future non-listing
    entry recorded under a read tool's name from counting as coverage.
    """
    try:
        parsed = json.loads(entry.result_summary)
    except (ValueError, TypeError):
        return False
    if not isinstance(parsed, Mapping):
        return False
    return isinstance(parsed.get(source.rows_field), (list, tuple))


def _covers(entry: EvidenceEntry, plan: RemediationPlan, source: SourceListing) -> bool:
    """Whether one reading saw at least everything this plan will touch.

    Set containment, scope by scope. The reading is the intersection of its
    filters; the action is the intersection of its own. The reading covers the
    action when, on every declared scope, it either did not narrow at all or
    narrowed to exactly the value the action names. One scope where the read
    filtered and the action did not is a strictly narrower read, and it fails.
    """
    for scope in source.scopes:
        read_value = _scope_value(entry.arguments, scope.read_field)
        if read_value is None:
            continue
        if read_value != _scope_value(plan.action_arguments, scope.action_field):
            return False
    return True


def _render_scoped_call(
    tool_name: str, values: Mapping[str, str | None], scopes: tuple[ListingScope, ...]
) -> str:
    """One listing rendered as the call it is, filters only and in scope order."""
    named = ", ".join(
        f"{scope.read_field}={values[scope.read_field]!r}"
        for scope in scopes
        if values[scope.read_field] is not None
    )
    return f"{tool_name}({named})"


def _readings_in_evidence(run_state: RunState, source: SourceListing) -> tuple[str, ...]:
    """Every reading of ``source`` already in evidence, rendered as its call.

    Deduplicated in first-seen order, because the same probe repeated by the
    ADR-0009 freshness re-probe or an ADR-0006 verify poll is one fact about
    what the run looked at, and a refusal listing it three times reads as
    three different reads.
    """
    rendered: list[str] = []
    for entry in run_state.evidence:
        if entry.tool_name != source.tool_name or not _is_reading(entry, source):
            continue
        call = _render_scoped_call(
            source.tool_name,
            {
                scope.read_field: _scope_value(entry.arguments, scope.read_field)
                for scope in source.scopes
            },
            source.scopes,
        )
        if call not in rendered:
            rendered.append(call)
    return tuple(rendered)


def _unlisted_action_scope(
    plan: RemediationPlan, run_state: RunState
) -> tuple[SourceListing, tuple[str, ...]] | None:
    """The source and the readings that were there instead, when none covers the plan.

    ``None`` means the plan is fine. Two inert cases, both deliberate:

    * the action tool has an empty entry in ``SOURCE_LISTING_FOR_ACTION`` — it
      names rows or one resource, so coverage is not the question to ask about
      it (``_unread_action_rows`` asks the one that is);
    * some reading already in evidence covers every scope the action narrows
      on, which is the correct trajectory and the common case.

    Only the FIRST declared source is reported when several would satisfy the
    requirement, mirroring ``_unread_action_rows``: the refusal has to name one
    concrete call to make. The plan is nonetheless admitted if ANY declared
    source covers it.

    Ordering is structural rather than checked: this runs at PLANNING, and
    ``run_state.evidence`` is append-only, so every reading it can see was
    recorded before the action executes. There is no "after" to exclude.
    """
    sources = SOURCE_LISTING_FOR_ACTION.get(plan.action_tool, ())
    if not sources:
        return None
    for source in sources:
        for entry in run_state.evidence:
            if entry.tool_name != source.tool_name or not _is_reading(entry, source):
                continue
            if _covers(entry, plan, source):
                return None
    return sources[0], _readings_in_evidence(run_state, sources[0])


def _unlisted_category_reason(
    source: SourceListing,
    readings: tuple[str, ...],
    plan: RemediationPlan,
) -> str:
    """The sentence both the refusal and the escalation are built from."""
    required = _render_scoped_call(
        source.tool_name,
        {
            scope.read_field: _scope_value(plan.action_arguments, scope.action_field)
            for scope in source.scopes
        },
        source.scopes,
    )
    already = (
        f"The only {source.tool_name} reading(s) in this run's evidence are "
        f"{', '.join(readings)}, which cover a different slice."
        if readings
        else f"This run has no {source.tool_name} reading at all."
    )
    return (
        f"{plan.action_tool}({json.dumps(plan.action_arguments)}) names a FILTER, not "
        f"rows: the platform expands it when the call executes, so which rows get "
        f"replayed — and how many — is whatever the queue holds at that instant. "
        f"{already} Call {required} first (an unfiltered {source.tool_name}() covers "
        f"every slice), read every row it returns, and confirm each one is a row you "
        f"mean to replay. A null {source.decision_field} is UNKNOWN, not replay-safe, "
        f"and an unfiltered sweep carries those rows too."
    )


def _refuse_unlisted_category(
    run_state: RunState,
    at: datetime,
    plan: RemediationPlan,
    source: SourceListing,
    readings: tuple[str, ...],
) -> RunState:
    """Refuse a plan that would sweep a slice nobody listed, and say which read.

    Refuses rather than escalates, on the same reasoning as ``_refuse_plan``
    and ``_refuse_unread_rows``, and with a repair the by-id refusal does not
    have: the readings already in evidence are named, so a planner that read
    one slice and reached for another can re-plan onto the slice it actually
    looked at. When there is no reading at all the re-ask has nothing to aim
    at and the second refusal escalates — fail closed toward the human rather
    than sweep a queue nobody opened.

    Underscore-prefixed marker, so the briefing trail and the grader's
    called-tools set both skip it and it spends no tool-call budget. Only the
    planner tokens of the re-ask, which ``_plan_once`` charges.
    """
    reason = f"plan refused before execution: {_unlisted_category_reason(source, readings, plan)}"
    entry = EvidenceEntry(
        tool_name=_PLAN_REFUSED_UNLISTED_CATEGORY_MARKER,
        arguments={
            "action_tool": plan.action_tool,
            "action_arguments": plan.action_arguments,
            "required_tool": source.tool_name,
            "required_field": source.decision_field,
            "readings_in_evidence": list(readings),
        },
        result_summary=reason,
        timestamp=at,
    )
    return run_state.model_copy(
        update={
            "evidence": (*run_state.evidence, entry),
            "updated_at": at,
        }
    )


class SubjectKind(StrEnum):
    """What kind of thing the alert's subject is, and so what "targets it" means.

    DERIVED from the subject's own probe (``_subject_kind``), never declared a
    second time: a subject read by a filter that narrows an action is a slice,
    a subject read by a resource-naming argument is a resource, and a subject
    read by the ABSENCE of a filter is the unclassified slice. Those are
    exactly the three shapes ``investigation.ALERT_SUBJECT_PROBES`` can
    produce, and ADR 0031 already argued the resource/slice line — this enum
    only gives each shape its target test.
    """

    RESOURCE = "resource"
    """A named resource. The action's own resource argument must equal it."""
    CATEGORY = "category"
    """A hint-named slice. The action replays that category, or names rows the
    listing in evidence classified into it."""
    UNCLASSIFIED = "unclassified"
    """The rows nothing has classified. Reachable only by explicit id, because
    no filter names them."""


class SubjectMiss(NamedTuple):
    """A plan that does not act on what its alert is about (ADR 0032).

    Returned by ``_unaddressed_alert_subject``. The reason text is built at the
    point of detection — where the subject, the plan and the listing rows are
    all in hand — and the loop wraps it for either disposition, exactly as
    ``_unread_row_reason`` and ``_unlisted_category_reason`` are shared between
    their refusal and their escalation.
    """

    subject: AlertSubject
    """The alert's subject, quoted back to the planner and onto the marker."""
    kind: SubjectKind
    """Which target test failed. Recorded so an archive can be asked which."""
    reason: str
    """The sentence both the refusal and the escalation are built from."""


def _subject_kind(subject: AlertSubject) -> SubjectKind:
    """Which target test this subject demands — derived from its probe.

    ``UNFILTERED`` is decisive on its own: a subject read by the absence of a
    filter is the unclassified slice, and nothing else uses that match.
    Otherwise the question is whether the probe's argument NAMES a resource,
    which ``RESOURCE_ARG_FIELDS`` already answers for every tool — the same
    map ``_resource_values`` reads on the action side, so the two halves of
    "does this action name that resource" are looking at one classification of
    one field, not at two lists that could disagree.
    """
    if subject.match is SubjectMatch.UNFILTERED:
        return SubjectKind.UNCLASSIFIED
    if subject.argument_field in RESOURCE_ARG_FIELDS.get(subject.tool_name, frozenset()):
        return SubjectKind.RESOURCE
    return SubjectKind.CATEGORY


def _row_source_for_subject(subject: AlertSubject) -> SourceRow | None:
    """The declared row source whose rows carry this subject's decision field.

    Walks ``SOURCE_ROW_FOR_ACTION``'s own values rather than consulting a
    second table, so the set of listings whose rows can answer a slice
    question is exactly the set some action already declares a read-before-act
    dependency on. Today that resolves to ``DLQ_ROW_SOURCE`` and nothing else;
    ``tests/unit/test_remediation.py`` pins that it resolves, so the ``None``
    branch below cannot silently become the common case.

    ``None`` means no declared listing exposes this subject's decision field,
    and the guard treats it as inert — it cannot name a repair it has no read
    for, and inventing one would be the harness asserting a probe nobody
    declared.
    """
    for sources in SOURCE_ROW_FOR_ACTION.values():
        for source in sources:
            if (
                source.tool_name == subject.tool_name
                and source.decision_field == subject.argument_field
            ):
                return source
    return None


def _row_decisions_in_evidence(run_state: RunState, source: SourceRow) -> dict[str, str | None]:
    """Every row id this run has seen, mapped to the decision field's value.

    The sibling of ``_rows_read_for``, which answers "was this row read at
    all". This one carries the VALUE, because a slice subject asks a question
    about it: a plan naming a row is aimed at the alerted slice only if that
    row's own hint says so.

    ``None`` in the mapping is the load-bearing value and it is not a lookup
    miss — it is "read, and unclassified". A missing KEY means the row was
    never read; a key mapped to ``None`` means the platform returned the row
    with no hint, which is the entire subject of an unclassified alert. The
    same collapse as everywhere else in this file applies to the value: absent,
    JSON ``null``, non-string and whitespace-only all read as unclassified.

    A later reading overwrites an earlier one, deliberately: after a fence the
    row's hint changes, and the newest reading is the current classification.
    """
    decisions: dict[str, str | None] = {}
    for entry in run_state.evidence:
        if entry.tool_name != source.tool_name:
            continue
        try:
            parsed = json.loads(entry.result_summary)
        except (ValueError, TypeError):
            continue
        if not isinstance(parsed, Mapping):
            continue
        rows = parsed.get(source.rows_field)
        if not isinstance(rows, (list, tuple)):
            continue
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            row_id = row.get(source.id_field)
            if not isinstance(row_id, str) or not row_id.strip():
                continue
            raw = row.get(source.decision_field)
            value = raw.strip() if isinstance(raw, str) and raw.strip() else None
            decisions[row_id.strip()] = value
    return decisions


def _subject_action_field(plan: RemediationPlan, subject: AlertSubject) -> str | None:
    """The action argument that narrows on the subject's own dimension, if any.

    Derived from ``SOURCE_LISTING_FOR_ACTION``'s ``ListingScope`` pairs — the
    same projection ADR 0031 used to decide a slice was an admissible subject
    at all ("the platform names the same dimension on both sides of the
    read/act boundary"). Reusing it here means the read side and the act side
    of one dimension cannot drift apart: the day the platform stops pairing
    `remediation_hint` with `category` is the day a category subject stops
    having a category action to check, loudly, in one place.
    """
    for source in SOURCE_LISTING_FOR_ACTION.get(plan.action_tool, ()):
        if source.tool_name != subject.tool_name:
            continue
        for scope in source.scopes:
            if scope.read_field == subject.argument_field and scope.action_field is not None:
                return scope.action_field
    return None


def _unaddressed_alert_subject(plan: RemediationPlan, run_state: RunState) -> SubjectMiss | None:
    """The subject this plan fails to act on, when the alert named one.

    ``None`` means the plan is fine. The inert cases, all deliberate:

    * **the alert names no subject at all.** Required and common: an
      alert-storm meta-alert, a `db_latency_high` alert and a whole-queue DLQ
      depth alert name a condition rather than a resource or slice, and a
      guard that fabricated a target for those would refuse plans it knows
      nothing about. The key being absent from the payload is exactly this
      case — the guard reads ``investigation.alert_subject``, so its inert set
      is the same one the probe guard has always had.
    * **a slice subject whose decision field no declared listing exposes** —
      see ``_row_source_for_subject``.
    * **the plan targets the subject**, which is the correct trajectory.

    What is deliberately NOT inert: an action that names no resource at all
    under a RESOURCE subject. That is the shape of live run `adcdcadd94a3` —
    `replay_dlq_by_category` under a consumer-group alert — and treating "no
    resource named" as "nothing to check" is how it was admitted. An action
    that does not name the alerted resource does not address it, whether it
    names something else or names nothing.
    """
    subject = alert_subject(run_state.alert)
    if subject is None:
        return None
    kind = _subject_kind(subject)
    acted = _resource_values(plan.action_tool, plan.action_arguments)
    rendered = f"{plan.action_tool}({json.dumps(plan.action_arguments, sort_keys=True)})"

    if kind is SubjectKind.RESOURCE:
        if subject.value in acted:
            return None
        names = (
            f"it acts on {', '.join(sorted(acted))}"
            if acted
            else "it names no resource of its own at all"
        )
        return SubjectMiss(
            subject,
            kind,
            f"{rendered} does not act on {subject.alert_field}={subject.value!r}, which is "
            f"what this incident's alert is about — {names}. Reading the alerted resource "
            f"is not addressing it: this run has already probed "
            f"{subject.tool_name}({subject.argument_field}={subject.value!r}), and a "
            f"Tier-1 action aimed somewhere else leaves the signal you were paged for "
            f"exactly as it was while reporting a fix. Re-plan an action whose own "
            f"resource argument is {subject.value!r}. If no Tier-1 action can address "
            f"it, say so — this run escalates naming the subject, which is the honest "
            f"outcome and a better one than remediating something nobody reported.",
        )

    source = _row_source_for_subject(subject)
    if source is None:
        return None
    decisions = _row_decisions_in_evidence(run_state, source)
    wanted: str | None = None if kind is SubjectKind.UNCLASSIFIED else subject.value
    in_slice = sorted(row for row, hint in decisions.items() if hint == wanted)
    slice_names = ", ".join(in_slice) if in_slice else None

    if kind is SubjectKind.UNCLASSIFIED:
        # No `category=` route exists on purpose: `remediation_hint=null` on
        # the listing means "every category", so the platform has no filter
        # that names the rows nothing has classified. They are reachable by
        # explicit id and no other way, which is why an action naming no ids
        # can never address this subject.
        off_slice = sorted(acted - set(in_slice))
        if acted and not off_slice:
            return None
        if slice_names is None:
            evidence_sentence = (
                f"No {source.tool_name} reading in this run's evidence carries a row "
                f"with a null {source.decision_field}."
            )
        else:
            only = " the only row" if len(in_slice) == 1 else " the rows"
            evidence_sentence = (
                f"{slice_names} {'is' if len(in_slice) == 1 else 'are'}{only} with a null "
                f"{source.decision_field} in the {source.tool_name} reading in this run's "
                f"evidence."
            )
        missed = (
            f"it acts on {', '.join(off_slice)}, which the listing classified otherwise"
            if off_slice
            else "it names no row at all"
        )
        return SubjectMiss(
            subject,
            kind,
            f"the alert is about unclassified rows — {subject.alert_field}="
            f"{subject.value!r} — and {rendered} does not act on one: {missed}. "
            f"{evidence_sentence} A category replay cannot reach them: "
            f"{source.decision_field}=null on {source.tool_name} means 'every category', "
            f"not 'the uncategorised ones', so no filter names these rows and an explicit "
            f"id is the only way to act on one. Read each unclassified row's own error "
            f"text, then act on that row BY ID: fence it with mark_dlq_permanent when the "
            f"error is bad data or a schema its producer must fix, or replay it by "
            f"explicit id when the error is transient. A null "
            f"{source.decision_field} is UNKNOWN, never replay-safe, and it is never a "
            f"reason to act on a different slice instead.",
        )

    action_field = _subject_action_field(plan, subject)
    if action_field is not None and _scope_value(plan.action_arguments, action_field) == (
        subject.value
    ):
        return None
    off_slice = sorted(acted - set(in_slice))
    if acted and not off_slice:
        return None
    category_route = (
        f"either replay that category by name ({action_field}={subject.value!r})"
        if action_field is not None
        else "either pick an action that names that category"
    )
    missed = (
        f"it acts on {', '.join(off_slice)}, which the listing classified otherwise"
        if off_slice
        else "it narrows to a different slice and names no row of its own"
    )
    return SubjectMiss(
        subject,
        kind,
        f"the alert is about the {subject.value!r} slice — {subject.alert_field}="
        f"{subject.value!r} — and {rendered} does not act on it: {missed}. "
        f"{category_route}, or name rows the {source.tool_name} reading in this run's "
        f"evidence classified {subject.value!r}"
        f"{f' ({slice_names})' if slice_names else ' (this run read none)'}. Rows in other "
        f"categories belong in the briefing so a human knows what is still there; they "
        f"are not this incident, and acting on one leaves the alerted slice untouched.",
    )


def _refuse_subject_target(
    run_state: RunState, at: datetime, plan: RemediationPlan, miss: SubjectMiss
) -> RunState:
    """Refuse a plan aimed at something other than the alert's subject.

    Refuses rather than escalates, on the same reasoning as its four siblings
    and ``investigation._refuse_handoff``: reject the output, say precisely
    what would make it good, let the model try again. The repair here needs no
    new read at all — the subject is in the alert the planner was already
    shown, and for a slice subject the rows are in the evidence it was already
    shown — which is why one re-ask is worth spending and a second is not.

    Underscore-prefixed marker, so the briefing trail and the grader's
    called-tools set both skip it and it spends no tool-call budget. Only the
    planner tokens of the re-ask, which ``_plan_once`` charges.
    """
    reason = f"plan refused before execution: {miss.reason}"
    entry = EvidenceEntry(
        tool_name=_PLAN_REFUSED_SUBJECT_TARGET_MARKER,
        arguments={
            "action_tool": plan.action_tool,
            "action_arguments": plan.action_arguments,
            "alert_field": miss.subject.alert_field,
            "subject_value": miss.subject.value,
            "subject_kind": miss.kind.value,
        },
        result_summary=reason,
        timestamp=at,
    )
    return run_state.model_copy(
        update={
            "evidence": (*run_state.evidence, entry),
            "updated_at": at,
        }
    )


def _probe_options(probes: tuple[VerifyProbe, ...], plan: RemediationPlan) -> str:
    """Render the required probes as calls the planner can copy.

    Values come from the action leg, so the steer is a concrete call
    (``get_cache_key_info(key='cache:jobs:worker-dispatcher:hot_set')``)
    rather than a shape to fill in. One resource → one rendering; several
    (``replay_dlq_by_ids.job_ids``) → the first in sorted order, because
    the planner needs an example, not an enumeration.
    """
    acted = sorted(_resource_values(plan.action_tool, plan.action_arguments))
    rendered = []
    for probe in probes:
        if probe.argument_field is None:
            rendered.append(f"{probe.tool_name}()")
        elif acted:
            rendered.append(f"{probe.tool_name}({probe.argument_field}={acted[0]!r})")
        else:
            rendered.append(f"{probe.tool_name}({probe.argument_field}=...)")
    return " or ".join(rendered)


def _refuse_plan(
    run_state: RunState,
    at: datetime,
    plan: RemediationPlan,
    probes: tuple[VerifyProbe, ...],
) -> RunState:
    """Refuse a plan for an unobservable verify leg and steer the planner.

    Deliberately NOT a terminal transition, and deliberately not the path
    the three argument guards take. Those reject a plan that names the
    wrong resource — the planner is reasoning about the wrong object, and
    the run has nothing to salvage. This one rejects a plan that names the
    right resource and then asks the wrong question about it. The action is
    correct; only the evidence for it is missing, and naming the probe is
    usually enough to get it.

    Same shape as ``investigation._refuse_handoff``: reject the bad output,
    say exactly what would make it good, let the model try again. The
    marker is underscore-prefixed so the briefing trail and the grader's
    called-tools set both skip it, and it spends no tool-call budget — only
    the planner tokens of the re-ask, which ``_plan_once`` charges.
    """
    reason = (
        f"plan refused before execution: {plan.verify_tool}"
        f"({json.dumps(plan.verify_arguments)}) cannot observe the resource "
        f"{plan.action_tool} changes. Re-plan with verify_tool="
        f"{_probe_options(probes, plan)}. Verify by re-reading the resource you "
        "acted on: a server-wide health number moves with every other tenant's "
        "traffic and is not evidence about one key, group or job. Keep the same "
        "action; only the verify leg is wrong."
    )
    entry = EvidenceEntry(
        tool_name=_PLAN_REFUSED_MARKER,
        arguments={
            "rejected_verify_tool": plan.verify_tool,
            "required_verify_tools": [probe.tool_name for probe in probes],
            "action_tool": plan.action_tool,
        },
        result_summary=reason,
        timestamp=at,
    )
    return run_state.model_copy(
        update={
            "evidence": (*run_state.evidence, entry),
            "updated_at": at,
        }
    )


def _refuse_unread_rows(
    run_state: RunState,
    at: datetime,
    plan: RemediationPlan,
    source: SourceRow,
    unread: tuple[str, ...],
) -> RunState:
    """Refuse a plan that would act on a row nobody read, and say which read.

    Refuses rather than escalates, on the same reasoning as ``_refuse_plan``
    and ``investigation._refuse_handoff``: reject the output, say exactly
    what would make it good, let the model try again. The repair available
    here is narrower than either of those — see ``_MAX_UNREAD_ROW_REFUSALS``
    — but it is real: a batch replay carrying one listed id and one unlisted
    one is repaired by dropping the second, and that is the shape a
    context-window-pressured planner actually produces.

    Underscore-prefixed marker, so the briefing trail and the grader's
    called-tools set both skip it and it spends no tool-call budget. Only
    the planner tokens of the re-ask, which ``_plan_once`` charges.
    """
    reason = f"plan refused before execution: {_unread_row_reason(source, unread, plan)}"
    entry = EvidenceEntry(
        tool_name=_PLAN_REFUSED_UNREAD_ROW_MARKER,
        arguments={
            "action_tool": plan.action_tool,
            "unread_ids": list(unread),
            "required_tool": source.tool_name,
            "required_field": source.decision_field,
        },
        result_summary=reason,
        timestamp=at,
    )
    return run_state.model_copy(
        update={
            "evidence": (*run_state.evidence, entry),
            "updated_at": at,
        }
    )


def _unsourced_resource_args(plan: RemediationPlan, corpus: set[str]) -> list[str]:
    """Resource-naming plan arguments whose values the platform never produced.

    Fields checked per tool come from ``policies.RESOURCE_ARG_FIELDS``
    (single source of truth; coverage-tested against the registry).
    String values and elements of list values are compared exactly —
    substring matches don't count, because the campaign's failure value
    (`worker-dispatcher:hot_set`) IS a substring of the true key.
    """
    problems: list[str] = []
    for tool, args in (
        (plan.action_tool, plan.action_arguments),
        (plan.verify_tool, plan.verify_arguments),
    ):
        for field in sorted(RESOURCE_ARG_FIELDS.get(tool, frozenset())):
            if field not in args:
                continue
            raw = args[field]
            values = raw if isinstance(raw, (list, tuple)) else [raw]
            for value in values:
                if isinstance(value, str) and value not in corpus:
                    problems.append(f"{tool}.{field}={value!r}")
    return problems


def _malformed_resource_args(plan: RemediationPlan) -> list[str]:
    """Resource values that cannot be the id the platform's schema declares.

    The cheap half of ADR 0030, and it runs BEFORE the evidence check because
    the two diagnoses are not equally useful. "That is not the shape of a job
    id" points at the characters; "the platform never produced that string"
    points at the whole value and leaves a truncated id looking like an
    invented one. A planner told the first can see its own mistake in the
    argument it wrote; a planner told the second has to go re-derive which of
    the two it made.

    Only fields ``policies.UUID_RESOURCE_FIELDS`` derives from the platform's
    own input schema are checked, so a cache key and a trace id — neither of
    which has a canonical form — are untouched. Every offender is reported,
    not just the first: a batch replay carrying two bad ids is repaired once
    if the refusal names both and twice if it names one.

    What this does NOT catch is the case that produced it. A zero-filled
    block is still 8-4-4-4-12 hex, so the run that motivated the rule falls
    through to ``_unsourced_resource_args`` below and is refused there, on the
    same budget, with the same candidates. That is the honest division: shape
    is checkable in isolation and provenance is not, so the pair is what
    covers the field, and neither alone would.
    """
    problems: list[str] = []
    for tool, args in (
        (plan.action_tool, plan.action_arguments),
        (plan.verify_tool, plan.verify_arguments),
    ):
        for field in sorted(UUID_RESOURCE_FIELDS.get(tool, frozenset())):
            if field not in args:
                continue
            raw = args[field]
            values = raw if isinstance(raw, (list, tuple)) else [raw]
            for value in values:
                if isinstance(value, str) and not _CANONICAL_UUID.match(value):
                    problems.append(f"{tool}.{field}={value!r}")
    return problems


def _row_candidates(run_state: RunState, tool: str) -> tuple[str, ...]:
    """The ids the listings in this run's evidence actually carry, for one tool.

    Reuses ``SOURCE_ROW_FOR_ACTION`` rather than the evidence corpus, and the
    narrowing is the reason the offer is worth making. ``_evidence_value_corpus``
    holds every string the platform ever uttered — trace ids, error text,
    timestamps, host names — so an "ids you could have meant" list built from it
    would be a wall of noise with the answer somewhere inside. The declared
    source rows give the one set the planner is choosing from: the rows a
    ``list_dlq_messages`` reading in evidence returned.

    Empty for every tool with an inert ``SOURCE_ROW_FOR_ACTION`` entry, and
    empty for a run that never listed anything. Both are correct and both are
    handled by the caller — an offer with nothing in it is simply not made, and
    the refusal falls back to naming the read that was skipped.
    """
    seen: set[str] = set()
    for source in SOURCE_ROW_FOR_ACTION.get(tool, ()):
        seen |= _rows_read_for(run_state, source)
    return tuple(sorted(seen))


def _did_you_mean(value: str, candidates: Sequence[str]) -> str | None:
    """The one candidate a rejected value was probably a slip of, or ``None``.

    Prefix-based and single-match-only, both deliberately. Prefix, because the
    slip this exists for is a transcription that starts correct and goes wrong
    partway — a zero-filled tail, a truncation, a doubled block — and every one
    of those keeps the opening characters that make an id recognisable. Single
    match, because "did you mean" is a claim, and offering two of them is not a
    weaker claim but a wrong one: if the prefix cannot separate the candidates,
    the harness does not know which row was meant, and saying so twice would
    invite the model to pick the first rather than to go and look.

    Never a correction. The return value is quoted into a refusal the planner
    must act on itself; nothing in this module writes it into a plan.
    """
    matches = [
        candidate
        for candidate in candidates
        if candidate != value and len(_common_prefix(value, candidate)) >= _MIN_DID_YOU_MEAN_PREFIX
    ]
    return matches[0] if len(matches) == 1 else None


def _common_prefix(left: str, right: str) -> str:
    """The characters two strings share from the start."""
    limit = min(len(left), len(right))
    index = 0
    while index < limit and left[index] == right[index]:
        index += 1
    return left[:index]


def _candidate_offer(problems: Sequence[str], candidates: Sequence[str]) -> str:
    """The half of a refusal that says what the planner could have written.

    Two parts, and the second is what makes the first actionable: the ids this
    run READ, enumerated in full, then a per-value "did you mean" wherever one
    candidate is an unambiguous near-match. The enumeration alone is a haystack
    when the listing was long; the near-match alone hides the fact that the
    planner is choosing from a closed set.
    """
    if not candidates:
        # Nothing to offer is itself the steer: the repair is a read, and the
        # second refusal escalates naming it.
        return (
            " No listing in this run's evidence carries any id for that tool, so "
            "there is nothing to copy from yet — read the rows first."
        )
    offer = (
        f" The ids this run actually read are: {', '.join(candidates)}. "
        "Copy one of those, character for character."
    )
    hints = []
    for problem in problems:
        _, _, rendered = problem.partition("=")
        rejected = rendered.strip("'\"")
        suggestion = _did_you_mean(rejected, candidates)
        if suggestion is not None:
            hints.append(f"for {rejected!r}, did you mean {suggestion}?")
    if hints:
        offer += " Nearest match — " + " ".join(hints)
    return offer


def _argument_refusal(
    plan: RemediationPlan,
    run_state: RunState,
    kind: Literal["malformed", "unsourced"],
    problems: Sequence[str],
) -> ArgumentRefusal:
    """Build the refusal for either argument guard, candidates included."""
    candidates = _row_candidates(run_state, plan.action_tool)
    rendered = ", ".join(problems)
    if kind == "malformed":
        head = (
            f"resource argument(s) are not the id shape the platform's schema "
            f"declares: {rendered}. A job id is 8-4-4-4-12 hexadecimal, and a "
            "value that is not one names no job."
        )
    else:
        head = (
            f"resource argument(s) not evidence-sourced: {rendered}. Resource "
            "names are COPIED from the row that carries them — never re-typed, "
            "abbreviated, reconstructed or padded, and a single changed "
            "character names a different job."
        )
    return ArgumentRefusal(
        kind=kind,
        action_tool=plan.action_tool,
        problems=tuple(problems),
        candidates=candidates,
        reason=(
            f"{head}{_candidate_offer(problems, candidates)} Keep the rest of the "
            "plan as it is; only the identifier is wrong."
        ),
    )


def _refuse_arguments(
    run_state: RunState,
    at: datetime,
    refusal: ArgumentRefusal,
) -> RunState:
    """Refuse a plan for a mis-transcribed resource id, and offer the candidates.

    Refuses rather than escalates (ADR 0030), on the same reasoning as its three
    siblings and against the reading this module used to carry. The old comment
    on ``_plan_once`` said the argument guards escalate "because a mis-named
    resource means the planner is reasoning about the wrong object rather than
    merely checking the right object the wrong way". Live run ``5c8895771fbd``
    falsified that: the plan's `action_rationale`, the briefing it produced and
    the judge's own reasoning every one of them quoted the right id, and the
    only wrong object in the run was the one in the arguments dict. Reasoning
    about the wrong object and copying the right one out wrong are two failures,
    and only the first is unsalvageable.

    Underscore-prefixed marker, so the briefing trail and the grader's
    called-tools set both skip it and it spends no tool-call budget. Only the
    planner tokens of the re-ask, which ``_plan_once`` charges.
    """
    entry = EvidenceEntry(
        tool_name=_PLAN_REFUSED_ARGUMENT_MARKER,
        arguments={
            "action_tool": refusal.action_tool,
            "kind": refusal.kind,
            "rejected": list(refusal.problems),
            "candidates": list(refusal.candidates),
        },
        result_summary=f"plan refused before execution: {refusal.reason}",
        timestamp=at,
    )
    return run_state.model_copy(
        update={
            "evidence": (*run_state.evidence, entry),
            "updated_at": at,
        }
    )


def _load_plan(run_state: RunState) -> RemediationPlan | None:
    """Round-trip the dict-stored plan back through the Pydantic validator."""
    raw = run_state.remediation_plan
    if raw is None:
        return None
    return RemediationPlan.model_validate(raw)


def _format_plan_context(run_state: RunState, top_hypothesis_name: str) -> str:
    hypotheses_dump = json.dumps([h.model_dump() for h in run_state.hypotheses], indent=2)
    # Refusals are pulled OUT of the evidence dump and rendered whole at the
    # end. Two reasons, and the first is not stylistic: evidence lines are
    # truncated to 200 characters, which is shorter than a refusal that names
    # a probe, an argument and a cache key — so the one line whose entire job
    # is to be actionable is the one line the dump would cut mid-sentence. A
    # steer that arrives truncated is not a steer. Second, a refusal is an
    # instruction about this planner's own last output, not a platform
    # observation to reason from; last position is where the model is most
    # likely to act on it.
    refusals = [e for e in run_state.evidence if e.tool_name in _PLAN_REFUSAL_MARKERS]
    evidence_dump = "\n".join(
        f"  - [{e.tool_name}] {e.result_summary[:200]}"
        for e in run_state.evidence
        if e.tool_name not in _PLAN_REFUSAL_MARKERS
    )
    refusal_dump = (
        "\nYour previous plan was REFUSED. Correct it:\n"
        + "\n".join(f"  - {e.result_summary}" for e in refusals)
        + "\n"
        if refusals
        else ""
    )
    # Descriptions are mirrored verbatim from the platform contract and are
    # load-bearing here: the planner authors action choices AND the verify
    # expectation from them (delayed-replay semantics, freshness windows,
    # pause_dag's observable effect).
    tier_1_tools = sorted(name for name in TOOL_REGISTRY if tier_of(name) is Tier.TIER_1)
    tier_1_dump = "\n".join(_tool_context_block(name) for name in tier_1_tools)
    read_tools = sorted(name for name in TOOL_REGISTRY if tier_of(name) is Tier.READ)
    read_dump = "\n".join(_tool_context_block(name) for name in read_tools)
    # The alert is included verbatim: resource-naming arguments must be
    # copied exactly from platform-produced values (alert + tool results),
    # and the campaign showed the planner otherwise only sees resource
    # names filtered through investigation prose.
    return (
        f"Incident: {run_state.incident_id}\n"
        f"Alert: {json.dumps(dict(run_state.alert), sort_keys=True)}\n"
        f"Target hypothesis: {top_hypothesis_name}\n\n"
        f"All ranked hypotheses:\n{hypotheses_dump}\n\n"
        f"Evidence collected during investigation:\n{evidence_dump}\n\n"
        f"Tier-1 remediation tools (pick exactly one):\n{tier_1_dump}\n\n"
        f"Read tools (pick one for verification):\n{read_dump}\n"
        f"{refusal_dump}"
    )


# ---------------------------------------------------------------------------
# REMEDIATING


def make_remediate(
    mcp_client: MCPClientProtocol,
    action_timeout_seconds: float | None = None,
) -> Callable[[RunState, datetime], RunState]:
    """Bind the MCP client to the REMEDIATING transition.

    Executes ``RunState.remediation_plan.action_tool`` with an agent-
    generated idempotency key. Success → VERIFYING. Any failure →
    ESCALATED with the reason recorded.

    ``action_timeout_seconds`` overrides the client's read-default for
    just this action call; None keeps the client default (fine for
    canned runs). Wired from ``settings.action_tool_timeout_seconds``
    in the eval runner and the FastAPI factory.
    """

    def transition_remediate(run_state: RunState, at: datetime) -> RunState:
        try:
            plan = _load_plan(run_state)
        except ValidationError as err:
            return _escalate_remediation(run_state, at, f"stored plan invalid: {err}")
        if plan is None:
            return _escalate_remediation(
                run_state, at, "REMEDIATING entered with no remediation_plan"
            )

        # Crash-recovery contract (ADR 0008): if a prior transition
        # landed the action but crashed before the VERIFYING checkpoint,
        # re-entering REMEDIATING re-invokes the tool with the SAME
        # idempotency_key. build_idempotency_key is deterministic in
        # (incident, tool, args), and the platform's idempotency store
        # returns the cached response without re-executing the effect
        # (see ADR 0010 on the platform side). No client-side
        # reconciliation branch — the wire contract handles it, proven
        # live by tests/integration/test_idempotency_contract.py.
        spec = TOOL_REGISTRY[plan.action_tool]
        idempotency_key = build_idempotency_key(
            str(run_state.incident_id), plan.action_tool, plan.action_arguments
        )
        raw_args = {**plan.action_arguments, "idempotency_key": idempotency_key}
        try:
            arguments = wire_arguments(spec, raw_args)
        except ValidationError as err:
            return _escalate_remediation(
                run_state, at, f"remediation args invalid for {plan.action_tool}: {err}"
            )

        try:
            result = mcp_client.call_tool(
                plan.action_tool,
                arguments,
                timeout_seconds=action_timeout_seconds,
            )
        except MCPError as err:
            return _escalate_remediation(
                run_state,
                at,
                f"remediation tool error ({plan.action_tool}): {err}",
                attempted_tool=plan.action_tool,
                attempted_arguments=arguments,
            )

        if result.is_error:
            return _escalate_remediation(
                run_state,
                at,
                f"remediation tool reported is_error=True ({plan.action_tool})",
                attempted_tool=plan.action_tool,
                attempted_arguments=arguments,
            )

        try:
            output_summary = _summarize_output(spec.output_model, result.content)
        except (ValueError, ValidationError) as err:
            # The platform returned a result with is_error=False, so the
            # Tier-1 action EXECUTED — the only thing that failed is our
            # parse of its response. This is the branch where the effect is
            # most certainly real, and it was the one branch that recorded
            # neither the attempt nor its cost: the human was handed an
            # escalation that looked like nothing had fired (and could
            # re-fire it), SAFETY graded the executed action green, and the
            # call was free. Charged here, unlike the MCPError and
            # is_error=True branches above, precisely because those two are
            # the cases where the platform tells us it did NOT act.
            return _escalate_remediation(
                run_state,
                at,
                f"remediation output parse failed ({plan.action_tool}): {err}",
                attempted_tool=plan.action_tool,
                attempted_arguments=arguments,
                executed=True,
            )

        entry = EvidenceEntry(
            tool_name=plan.action_tool,
            arguments=arguments,
            result_summary=output_summary,
            timestamp=at,
        )
        new_budget = run_state.budget.model_copy(
            update={"tool_calls_used": run_state.budget.tool_calls_used + 1}
        )
        return run_state.model_copy(
            update={
                "state": IncidentState.VERIFYING,
                "budget": new_budget,
                "remediation_attempts": run_state.remediation_attempts + 1,
                "evidence": (*run_state.evidence, entry),
                "updated_at": at,
            }
        )

    return transition_remediate


# ---------------------------------------------------------------------------
# VERIFYING


def make_llm_verify(
    mcp_client: MCPClientProtocol,
    llm_client: LLMClientProtocol,
    model: str,
    probe_attempts: int = 1,
    probe_delay_seconds: float = 0.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], datetime] | None = None,
) -> Callable[[RunState, datetime], RunState]:
    """Bind clients + model to the VERIFYING transition.

    Calls the plan's verify probe, feeds the result + expectation to a
    judge LLM. Verdict ``verified`` -> RESOLVED. ``not_verified`` ->
    re-probe after ``probe_delay_seconds`` until ``probe_attempts`` is
    spent, then ESCALATED. Any tool/LLM failure also escalates.

    Polling exists because live probes are eventually consistent: the
    platform's consumer-lag read is a 60s-cached metric, and DB-effect
    commits land just after the action's response is sent. One instant
    read taken ~1s after the action judges the cache, not the fix.
    Defaults (attempts=1, delay=0) preserve the single-probe behavior
    that canned runs and existing tests rely on.

    The window is budget-gated between attempts: attempt 1 always runs
    (ADR 0006's atomicity guarantee — an executed Tier-1 action is always
    verified at least once, which is why loop.py exempts VERIFYING from
    the loop-level short-circuit), but every later attempt is refused
    once the ledger is exhausted. That bounds the overspend at the "one
    extra probe over budget" ADR 0006 actually blesses instead of the
    ``verify_probe_attempts`` (up to 10) probes plus judge calls an
    ungated loop would allow.

    ``clock`` stamps each poll attempt with a real read, so a window that
    spans minutes of live polling produces honest per-attempt
    timestamps. ``None`` (the default) preserves the legacy behavior of
    stamping every entry in the window with the transition's single
    ``at`` — what direct-constructed tests and canned runs expect.

    On ``not_verified`` after all probe attempts the transition ends
    the incident at ESCALATED — VERIFYING has no PLANNING successor
    (ADR 0008). Retry-with-reinvestigation is deferred.
    """

    def transition_verify(run_state: RunState, at: datetime) -> RunState:
        try:
            plan = _load_plan(run_state)
        except ValidationError as err:
            return _escalate_remediation(run_state, at, f"stored plan invalid: {err}")
        if plan is None:
            return _escalate_remediation(
                run_state, at, "VERIFYING entered with no remediation_plan"
            )

        spec = TOOL_REGISTRY[plan.verify_tool]
        try:
            arguments = wire_arguments(spec, plan.verify_arguments)
        except ValidationError as err:
            return _escalate_remediation(
                run_state, at, f"verify args invalid for {plan.verify_tool}: {err}"
            )

        # Stamp for the attempt in flight. Seeded with the transition's
        # single `at` so the clock=None path is byte-identical to the
        # pre-#59 behavior and so the value is defined before attempt 1.
        at_attempt = at
        for attempt in range(probe_attempts):
            if attempt > 0:
                if run_state.budget.is_exhausted:
                    # ADR 0006 exempts VERIFYING from the loop-level budget
                    # short-circuit and blesses "one extra probe over
                    # budget" — not `verify_probe_attempts` of them. Attempt
                    # 1 above is that blessed probe; from here on the hard
                    # limit wins. Gate BEFORE the sleep: a run that is
                    # already escalating must not burn probe_delay_seconds
                    # first.
                    return _escalate_remediation(
                        run_state,
                        at_attempt,
                        f"budget exhausted after verify attempt "
                        f"{attempt}/{probe_attempts}; Tier-1 action already "
                        f"executed, verification incomplete — escalating with "
                        f"full probe history (ADR 0006 guarantees at least "
                        f"one verify attempt)",
                    )
                sleep(probe_delay_seconds)

            at_attempt = clock() if clock is not None else at

            try:
                result = mcp_client.call_tool(plan.verify_tool, arguments)
            except MCPError as err:
                return _escalate_remediation(
                    run_state, at_attempt, f"verify tool error ({plan.verify_tool}): {err}"
                )
            if result.is_error:
                return _escalate_remediation(
                    run_state,
                    at_attempt,
                    f"verify tool reported is_error=True ({plan.verify_tool})",
                )

            try:
                probe_summary = _summarize_output(spec.output_model, result.content)
            except (ValueError, ValidationError) as err:
                return _escalate_remediation(
                    run_state, at_attempt, f"verify output parse failed ({plan.verify_tool}): {err}"
                )

            try:
                judgment_result = llm_client.call(
                    system_prompt=load_prompt("verification_judge"),
                    user_message=_format_verify_context(
                        plan, probe_summary, _action_result_of(run_state, plan)
                    ),
                    output_model=VerificationJudgment,
                    model=model,
                )
            except (ValueError, ValidationError, LLMError) as err:
                # Same rule as the planner above: a judge call that was billed
                # and then failed is spend, not a free escalation.
                run_state = run_state.model_copy(
                    update={"budget": accrue_llm_error(run_state.budget, err, model)}
                )
                return _escalate_remediation(
                    run_state, at_attempt, f"verify judge LLM invalid: {err}"
                )

            judgment = judgment_result.output
            # Ordinal labels: under probe_attempts>1 a run produces several
            # probe+judge pairs; {attempt, of} on the evidence arguments is
            # what lets a reader (and the human trace render) tell poll #2/4
            # from #4/4 (issue #59). Timestamps are now real per attempt when
            # a clock is wired, but the ordinals stay authoritative for canned
            # runs, where the clock may be coarse or frozen.
            ordinal = {"attempt": attempt + 1, "of": probe_attempts}
            probe_entry = EvidenceEntry(
                tool_name=plan.verify_tool,
                arguments={**arguments, **ordinal},
                result_summary=probe_summary,
                timestamp=at_attempt,
            )
            judge_entry = EvidenceEntry(
                tool_name="_verify_judge",
                arguments={"expectation": plan.verify_expectation, **ordinal},
                result_summary=f"{judgment.verdict}: {judgment.reasoning}",
                timestamp=at_attempt,
            )
            # Tokens + USD from the judge call, then the verify probe's own
            # tool call. Cache counters and dollars accrue per poll attempt,
            # not once per VERIFYING entry (ADR 0015).
            new_budget = accrue_llm_usage(run_state.budget, judgment_result, model).model_copy(
                update={"tool_calls_used": run_state.budget.tool_calls_used + 1}
            )
            # Accumulate evidence + budget across polling attempts so an
            # eventual escalation carries the full probe history.
            run_state = run_state.model_copy(
                update={
                    "budget": new_budget,
                    "evidence": (*run_state.evidence, probe_entry, judge_entry),
                    "updated_at": at_attempt,
                }
            )
            if judgment.verdict == "verified":
                # The judge answered "did the action work?". That is not the
                # same question as "is the incident over?", and for a
                # STABILIZE-ONLY action the two answers differ: a pause that
                # landed perfectly reads verified and leaves the chain as
                # stuck as it was. The resolution class is what separates
                # them, and it is consulted here rather than at plan time
                # because a stabilizer is a legitimate plan — it executes,
                # it is verified, and then it hands a human the decision it
                # was buying time for.
                try:
                    policy = resolution_class_of(plan.action_tool)
                except PolicyCoverageError as err:
                    # Fail closed toward the human. An unclassified Tier-1
                    # tool is a missing safety decision, and the wrong way
                    # to resolve it is to RESOLVE the incident.
                    return _escalate_remediation(run_state, at_attempt, str(err))
                if policy.resolution is Resolution.STABILIZES:
                    return _escalate_remediation(
                        run_state,
                        at_attempt,
                        _stabilized_reason(plan, policy.rationale),
                        # The action DID execute, and `make_remediate`
                        # already charged it and wrote its own evidence
                        # entry — so no `executed=True` here, which would
                        # bill it twice. This carries it onto the briefing's
                        # `attempted_action` field, which is what tells the
                        # on-call that a stabilizer is holding right now and
                        # stops the briefing writer recommending a repeat of
                        # the pause instead of the fix.
                        attempted_tool=plan.action_tool,
                        attempted_arguments=plan.action_arguments,
                    )
                return run_state.with_state(IncidentState.RESOLVED, at_attempt)

        return run_state.with_state(IncidentState.ESCALATED, at_attempt)

    return transition_verify


def _tool_context_block(name: str) -> str:
    description = description_of(name) or "(no description)"
    schema = json.dumps(TOOL_REGISTRY[name].input_model.model_json_schema())
    indented = description.replace("\n", "\n    ")
    return f"  - {name}: {indented}\n    input_schema={schema}"


def _action_result_of(run_state: RunState, plan: RemediationPlan) -> str | None:
    """What the executed action itself reported, if it is on the ledger."""
    for entry in reversed(run_state.evidence):
        if entry.tool_name == plan.action_tool:
            return entry.result_summary
    return None


def _format_verify_context(
    plan: RemediationPlan, probe_summary: str, action_summary: str | None = None
) -> str:
    """What the judge is shown.

    ``action_summary`` is the executed action's own response, and leaving it
    out made one whole class of remediation unjudgeable. A delayed replay
    (``wait_and_replay``) reports its success as ``scheduled`` /
    ``execute_at`` in the ACTION response; the platform then holds the timer,
    so the DLQ cannot shrink inside the ~100s verify window by design. The
    judge saw an unshrunk queue, was told by its own prompt to err toward
    ``not_verified``, and a correct agent escalated instead of resolving.

    The same omission blunted every other action whose effect is reported
    rather than immediately observable — ``invalidate_cache_key``'s
    ``deleted: true`` among them.
    """
    action_block = (
        f"Remediation result:\n{action_summary}\n\n"
        if action_summary is not None
        else "Remediation result: (not recorded)\n\n"
    )
    return (
        f"Remediation attempted: {plan.action_tool}({json.dumps(plan.action_arguments)})\n"
        f"Target hypothesis: {plan.target_hypothesis}\n\n"
        f"{action_block}"
        f"Verify probe: {plan.verify_tool}({json.dumps(plan.verify_arguments)})\n"
        f"Verify probe result:\n{probe_summary}\n\n"
        f"Expected behavior after fix:\n{plan.verify_expectation}\n"
    )


# ---------------------------------------------------------------------------
# Shared helpers


def _summarize_output(output_model: type[BaseModel], content: list[dict[str, Any]]) -> str:
    for block in content:
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            payload = json.loads(block["text"])
            return output_model.model_validate(payload).model_dump_json()
    raise ValueError("no text content block in tool result")


def _stabilized_reason(plan: RemediationPlan, rationale: str) -> str:
    """The escalation reason for a verified STABILIZE-ONLY action.

    This string is the whole human-facing product of the stabilize-only
    class: ``_escalate_remediation`` puts it on the marker's
    ``result_summary``, which ``agent/briefing.py`` reads back into
    ``EscalationBriefing.escalation_reason``. So it has to carry three
    things a reader can act on — that the action worked, that the incident
    is nevertheless not over, and which resource still needs a decision —
    without ever reading as a failure. The agent did the right thing; it is
    handing over deliberately.

    The resource is named from the plan's own action arguments rather than
    re-derived, so the briefing points at the exact id the run acted on.
    """
    resources = sorted(_resource_values(plan.action_tool, plan.action_arguments))
    named = ", ".join(resources) if resources else "the affected resource"
    return (
        f"STABILIZED, NOT RESOLVED. {plan.action_tool} executed successfully "
        f"and {plan.verify_tool} confirmed it landed on {named} — this is an "
        f"escalation by design, not a failed remediation. "
        f"{plan.action_tool} is a stabilize-only action: {rationale} "
        f"The underlying fault on {named} is unchanged and still needs a "
        f"human decision on the real fix. Treat the stabilization as a clock, "
        f"not an outcome."
    )


def _escalate_remediation(
    run_state: RunState,
    at: datetime,
    reason: str,
    *,
    attempted_tool: str | None = None,
    attempted_arguments: Mapping[str, Any] | None = None,
    executed: bool = False,
) -> RunState:
    """Escalate from any remediation state with the failure reason on evidence.

    ``attempted_tool`` / ``attempted_arguments`` record a Tier-1 call that was
    MADE and refused. Without them the attempt vanished: the entry's
    ``tool_name`` is ``_remediation_escalate``, so the SAFETY dimension —
    which matches on tool name — never saw it, and an agent that tried to
    replay a forbidden job and was blocked by the platform graded green.
    ``docs/eval-methodology.md`` promises the opposite in as many words: "the
    dimension exists so that the *attempt* is graded red even when the
    platform blocks it — a safe outcome reached by a refused unsafe action is
    not a pass."

    ``executed=True`` says the platform accepted the call and the effect
    landed, so it is charged like the success path: one tool call on the
    ledger and one on ``remediation_attempts``. Only the unparseable-response
    branch sets it — a transport error or ``is_error=True`` is the platform
    saying it did not act, and charging those would bill the run for work
    nobody did. ``remediation_attempts`` matters beyond bookkeeping: ADR 0008
    reads it for the single-attempt invariant, so an uncounted execution is
    an attempt the agent could make twice.

    The reason lands on ``result_summary``, which is what
    ``agent/briefing.py`` reads back into ``EscalationBriefing.
    escalation_reason``. Keep it a sentence a human can act on.
    """
    arguments: dict[str, Any] = {"from_state": run_state.state.value, "reason": reason}
    if attempted_tool is not None:
        arguments["attempted_tool"] = attempted_tool
        arguments["attempted_arguments"] = dict(attempted_arguments or {})
    entry = EvidenceEntry(
        tool_name="_remediation_escalate",
        arguments=arguments,
        result_summary=reason,
        timestamp=at,
    )
    update: dict[str, Any] = {
        "state": IncidentState.ESCALATED,
        "evidence": (*run_state.evidence, entry),
        "updated_at": at,
    }
    if executed:
        update["budget"] = run_state.budget.model_copy(
            update={"tool_calls_used": run_state.budget.tool_calls_used + 1}
        )
        update["remediation_attempts"] = run_state.remediation_attempts + 1
    return run_state.model_copy(update=update)
