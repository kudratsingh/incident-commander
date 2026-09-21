"""Remediation loop: plan → execute → verify.

``make_llm_plan`` (PLANNING) emits a ``RemediationPlan``: one Tier-1 action, one verify probe.
``make_remediate`` executes it under ``sha256(incident_id|action_tool|sorted_json_args)[:32]``.
``make_llm_verify`` probes, judges the result, and lands RESOLVED or ESCALATED.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable, Collection, Mapping, Sequence
from datetime import datetime
from enum import StrEnum
from typing import Any, Final, Literal, NamedTuple

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from incident_commander.agent.accounting import (
    accrue_llm_error,
    accrue_structured_call,
)
from incident_commander.agent.attribution import (
    CLEARED_ON_ITS_OWN_SENTENCE,
    RecoveredReading,
    already_recovered,
    readings_of,
    render_reading,
)
from incident_commander.agent.hypothesis import Hypothesis, ReadToolName
from incident_commander.agent.investigation import (
    FIX_MAP,
    HINT_ROUTED_TOOLS,
    REMEDIATE_CONFIDENCE_THRESHOLD,
    AlertSubject,
    SubjectMatch,
    alert_subject,
)
from incident_commander.agent.planner_context import (
    ATTEMPT_FAILED_MARKER,
    PLAN_MARKER,
    VERIFY_JUDGE_MARKER,
    render_already_attempted,
)
from incident_commander.agent.state import (
    EvidenceEntry,
    IncidentState,
    RunState,
)
from incident_commander.agent.thinking import PlannerLog
from incident_commander.config import DEFAULT_MAX_REMEDIATION_ATTEMPTS
from incident_commander.llm.client import LLMClientProtocol, LLMError
from incident_commander.llm.prompts.loader import load_prompt
from incident_commander.llm.repair import (
    REMEDIATION_PLANNER_INVALID,
    VERIFY_JUDGE_INVALID,
    RepairedCall,
    call_with_output_repair,
)
from incident_commander.llm.structured import StructuredOutput
from incident_commander.tools.mcp_client import MCPClientProtocol, MCPError
from incident_commander.tools.policies import (
    RESOURCE_ARG_FIELDS,
    UUID_RESOURCE_FIELDS,
    PolicyCoverageError,
    Resolution,
    Tier,
    resolution_class_of,
    tier_of,
    tools_at_or_below,
)
from incident_commander.tools.registry import TOOL_REGISTRY, description_of
from incident_commander.tools.wire import arguments_hash, wire_arguments

# Every Tier-1 tool, hand-listed: Pydantic Literals need literal args at import time.
# Drift caught by ``test_policies.py::
# TestLiteralRegistryDrift::test_tier1_tool_name_literal_matches_tier1_slice``.
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

# How many times one PLANNING transition may refuse a plan whose verify probe cannot observe
# the acted-on resource. One, not the investigation loop's two (see
# ``investigation._MAX_SUBJECT_PROBE_REFUSALS``): PLANNING is a single LLM call.
_MAX_VERIFY_TARGET_REFUSALS: Final[int] = 1

# How many times one PLANNING transition may refuse a plan replaying a job whose dead-letter
# row nobody read. One: the planner cannot make a read, so the only repair worth an ask is
# dropping the ids it has no row for. The second refusal escalates, naming the skipped read.
_MAX_UNREAD_ROW_REFUSALS: Final[int] = 1

# And one for the category half (ADR 0028): when the run HAS read the DLQ but the plan picked
# a different slice, the repair is to act on the slice in evidence, which the refusal names.
# Non-inert over a tool set DISJOINT from the by-id guard's, so no plan spends both budgets.
_MAX_UNLISTED_CATEGORY_REFUSALS: Final[int] = 1

# And one for the question upstream of the other four (ADR 0032): does this action address
# what the alert is about at all? PR #177's subject guard requires the subject to have been
# PROBED, never that the ACTION target it — two live runs read the right thing and remediated
# something else (`adcdcadd94a3` remediate_consumer_lag_success 2026-08-31; `a0aa257bf865`
# dlq_human_required_escalates 2026-09-08), both admitted by every other guard here.
#
# WORST CASE GROWS BY ONE PLANNER CALL: this guard's tool set OVERLAPS the other four's, so one
# PLANNING transition can now spend five planner calls. No tool-call budget is spent and
# ADR 0008 still allows exactly one action.
_MAX_SUBJECT_TARGET_REFUSALS: Final[int] = 1

# And one for the argument half (ADR 0030): a plan that got the reasoning right and fumbled the
# transcription. Live run `5c8895771fbd` (`dlq_wait_and_replay_success`) emitted
# `97d91272-0000-0000-0000-000000000000` for `97d91272-9774-5b8e-980b-f0d2fa6ed619` while its
# own rationale quoted the id correctly, and graded red for a copying slip.
#
# One, and the refusal must NOT correct the id for the model: substituting the nearest evidence
# id would be the harness guessing which job to replay. It offers candidates; the plan that
# executes is one the model itself emitted.
_MAX_ARGUMENT_REFUSALS: Final[int] = 1

# Refused-plan marker. Underscore-prefixed: excluded from the trail, spends no tool budget.
_PLAN_REFUSED_MARKER: Final[str] = "_plan_refused"
# Same convention for the read-before-act refusal. A separate marker because its arguments and
# its diagnosis differ: "acted on an unread row", not "verified the wrong thing".
_PLAN_REFUSED_UNREAD_ROW_MARKER: Final[str] = "_plan_refused_unread_row"
# And again for the read-before-act refusal's other half (ADR 0028): a replay naming a
# CATEGORY rather than rows. "You swept a category nobody listed" is its own diagnosis.
_PLAN_REFUSED_UNLISTED_CATEGORY_MARKER: Final[str] = "_plan_refused_unlisted_category"
# Fourth marker (ADR 0030). Carries the rejected values AND the candidates offered back, so an
# archive can tell "shown three ids, emitted a fourth" from "shown nothing".
_PLAN_REFUSED_ARGUMENT_MARKER: Final[str] = "_plan_refused_argument"
# Fifth marker (ADR 0032). Carries the alert field naming the subject, its value and the target
# test that failed, so an archive can be asked "was the action even aimed at the incident?".
_PLAN_REFUSED_SUBJECT_TARGET_MARKER: Final[str] = "_plan_refused_subject_target"
# Sixth marker (O-29, ADR 0071), and the one refusal here that is TERMINAL: the fault the action
# would fix already reads gone in this run's own newest reading of the resource, so there is no
# re-plan to ask for. It names itself for the reason the five above do — an archive has to be
# able to ask "why did this run act on nothing?" — and carries the reading that decided it.
_PLAN_REFUSED_CLEARED_MARKER: Final[str] = "_plan_refused_cleared_before_action"

# The failed attempt itself (ADR 0056) is ``planner_context.ATTEMPT_FAILED_MARKER``, named
# there because both planner contexts render it. Underscore-prefixed like every other marker,
# so it spends no tool budget and stays out of the briefing trail; ``_attempted_calls`` never
# reads it either — the executed call is on the ledger under its own tool name.
#
# How much of the verify reading the attempt record quotes. A whole DLQ listing would crowd
# out the rest of the context; the reading itself is on the ledger under the probe's own name.
_ATTEMPT_READING_CHARS: Final[int] = 400

# Every marker ``_format_plan_context`` must render whole and last. Derived membership, not a
# match on one name: the renderer once matched ``_PLAN_REFUSED_MARKER`` alone, so a refusal
# under any other name was cut by the 200-character truncation. A new shape must be added here.
#
# ``_PLAN_REFUSED_CLEARED_MARKER`` is deliberately NOT a member: that refusal escalates in the
# same transition, so no planner ever reads a context it could be rendered into, and listing it
# would promise a steer this guard does not give (O-29's answer there is "do not act at all").
_PLAN_REFUSAL_MARKERS: Final[frozenset[str]] = frozenset(
    {
        _PLAN_REFUSED_MARKER,
        _PLAN_REFUSED_UNREAD_ROW_MARKER,
        _PLAN_REFUSED_UNLISTED_CATEGORY_MARKER,
        _PLAN_REFUSED_ARGUMENT_MARKER,
        _PLAN_REFUSED_SUBJECT_TARGET_MARKER,
    }
)

# How much of a rejected value must match a candidate before the refusal says "did you mean".
# Eight = a UUID's first block; the slip this exists for began at character 10.
_MIN_DID_YOU_MEAN_PREFIX: Final[int] = 8

# A canonical UUID as the platform's input schema defines it (8-4-4-4-12 hex). NOT semantic:
# `97d91272-0000-0000-0000-000000000000` matches happily, so it cannot replace the evidence
# check; both report through the same refusal path.
_CANONICAL_UUID: Final[re.Pattern[str]] = re.compile(
    r"\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z"
)


# How the second refusal opens, per cause: a human reading the briefing's
# ``escalation_reason`` needs to know whether the planner wrote something that could never be
# an id or something that merely is not one of THESE ids.
_ARGUMENT_ESCALATION_CLAUSE: Final[dict[str, str]] = {
    "malformed": "wrote a resource identifier that is not the shape the platform declares",
    "unsourced": "named a resource that is not one this run read",
}


class ArgumentRefusal(NamedTuple):
    """A plan whose resource arguments are wrong in a way one re-ask repairs.

    Returned by ``_plan_once`` instead of an escalated ``RunState``, so the caller's loop
    can spend a refusal budget on it. ``reason`` is shared by refusal and escalation.
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


# Single source of truth for Tier-1 action → the read that observes what it changed. Sibling of
# ``investigation.ALERT_SUBJECT_PROBES``, which asks the same question one step earlier.
#
# TOTAL over ``Tier1ToolName``: an empty tuple is a *declared* inert entry, and
# ``tests/unit/test_policies.py::TestVerifyProbeForAction`` pins the totality.
#
# Not merely "the verify leg must name the action's resource" — ``_misdirected_verify_args``
# says that and is inert when the verify leg names NO resource. On 2026-09-07 a live run
# verified `invalidate_cache_key` with `get_redis_health`, whose counters are server-wide; the
# probe that would have answered, `get_cache_key_info`, was already in the evidence trail.
VERIFY_PROBE_FOR_ACTION: Final[dict[str, tuple[VerifyProbe, ...]]] = {
    # v0.6.0 shipped get_cache_key_info (plat #146/#182) to check a key before and after.
    "invalidate_cache_key": (VerifyProbe("get_cache_key_info", "key"),),
    "restart_consumer_group": (VerifyProbe("get_consumer_lag", "consumer_group"),),
    # pause_dag names `root_job_id`; get_dag_state names `job_id`. Field
    # names need not match across legs — the VALUE is what must line up.
    "pause_dag": (VerifyProbe("get_dag_state", "job_id"),),
    # A DLQ row is observed only by the listing; a DAG read serves a DAG
    # root (remediate_runaway_saga).
    "mark_dlq_permanent": (
        VerifyProbe("list_dlq_messages", None),
        VerifyProbe("get_dag_state", "job_id"),
    ),
    "replay_dlq_by_ids": (
        VerifyProbe("list_dlq_messages", None),
        VerifyProbe("get_dag_state", "job_id"),
    ),
    # Declared inert: these name a category / job_type, not a resource, so there is
    # no observation to demand.
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


# Single source of truth for "which read must have SEEN this resource before an action may
# touch it": before you replay a dead-lettered job, its dead-letter row must be in evidence,
# because the row is the only place its `remediation_hint` exists. `get_dag_state` shows only
# `status: dead_letter`, and the platform's rule is "A null hint is UNKNOWN, not replay-safe".
#
# The guard requires the ROW, never a hint VALUE: choosing between `wait_and_replay` and
# `replay_safe` is the prompt's job.
#
# TOTAL over ``Tier1ToolName``: ``tests/unit/test_policies.py::TestSourceRowForAction`` fails on
# any Tier-1 tool with no entry. The listing is named because two guards read it — this map's
# `replay_dlq_by_ids` entry, and ADR 0032's check via ``_row_source_for_subject``.
DLQ_ROW_SOURCE: Final[SourceRow] = SourceRow("list_dlq_messages", "items", "id", "remediation_hint")


SOURCE_ROW_FOR_ACTION: Final[dict[str, tuple[SourceRow, ...]]] = {
    "replay_dlq_by_ids": (DLQ_ROW_SOURCE,),
    # Declared inert: the two bulk replays name a category or a job_type and no
    # ids, so `_resource_values` yields nothing to look for. Their equivalent
    # CATEGORY rule is a different check, filed as WO-R2-143.
    "replay_dlq_by_category": (),
    "replay_dlq_messages": (),
    # `mark_dlq_permanent` stays inert because fencing is the conservative
    # direction — it stops auto-replay, it re-runs nothing — so acting on an
    # unread row cannot cause the harm this guard exists to prevent. WO-R2-144.
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


# Single source of truth for "which read must have COVERED what this action is about to sweep".
# Sibling of ``SOURCE_ROW_FOR_ACTION`` for the call shape that names no rows: a category replay
# names a filter the platform expands at execution time, and ``SOURCE_ROW_FOR_ACTION`` is inert
# there (no resource arguments), as ADR 0027 said when it filed WO-R2-143.
#
# Coverage, not row presence: an unfiltered reading covers every slice, one filtered to exactly
# the action's slice covers it, one filtered elsewhere covers nothing. The claim is about what
# the agent LOOKED at, so a slice emptied since the read is still covered.
#
# TOTAL over ``Tier1ToolName``: ``tests/unit/test_policies.py::TestSourceListingForAction``
# fails on any Tier-1 tool with no entry.
SOURCE_LISTING_FOR_ACTION: Final[dict[str, tuple[SourceListing, ...]]] = {
    # `category` is the slice; `job_type` narrows it further on both sides, and the
    # second scope is not decoration: a read filtered to one job_type is strictly
    # narrower than a category replay that names no job_type. A missing `category`
    # reads as "spans every category" and so demands an unfiltered reading — the
    # fail-closed choice, and `ReplayDlqByCategoryInput` rejects it later anyway.
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
    # The unfilterable sweep: `job_type` and nothing else, and it replays
    # uncategorised (null-hint) rows too. `action_field=None` on the hint scope says
    # so — only an unfiltered reading can cover it. Declaring it here does NOT make
    # it permissible: every DLQ scenario still lists it in `forbidden_action_tools`.
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
    # Declared inert: these name rows, so `SOURCE_ROW_FOR_ACTION` asks the question
    # ("is that row in evidence?") and a coverage question would refuse a correct
    # by-id replay whose listing was filtered elsewhere.
    "replay_dlq_by_ids": (),
    "mark_dlq_permanent": (),
    # These three name one resource each; nothing here is a filter.
    "invalidate_cache_key": (),
    "restart_consumer_group": (),
    "pause_dag": (),
}


class RemediationPlan(StructuredOutput):
    """One remediation plan: action + verification.

    Both tool fields are Literal-typed against the registry; ``make_llm_plan`` re-checks tier.
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
    # Somewhere for a prompt rule asking the planner to SHOW ITS WORK to write the answer:
    # ``model_config`` forbids extra keys, so an instruction with no field for it raises
    # ValidationError (the ``wait_and_replay`` delay rule in ``prompts/remediation_planner.md``).
    # Optional so the canned plan fixtures stay valid. Nothing grades it — what IS graded is
    # ``expected_action_arguments`` on ``delay_seconds``.
    action_rationale: str | None = Field(
        default=None,
        description=(
            "Optional. Why these action arguments and not others — required by the "
            "system prompt for a delayed DLQ replay, where the delay is a judgement "
            "derived from the rows rather than a value read off one. Name the row "
            "and the wait it stated. Free text; not graded."
        ),
    )


#: The judge role's NAME and the prompt file its rubric lives in. Plan 02 § 3 is normative: the
#: role is ``action_verifier``; ``verification_judge`` is the file it has always loaded, and
#: renaming the file would move a snapshot hash and rewrite the `_verify_judge` marker 156
#: committed trajectories carry. Named once each because the calibration harness must load and
#: hash the same rubric bytes the run loads (plan 03 § 110).
ACTION_VERIFIER_ROLE: Final[str] = "action_verifier"
VERIFICATION_JUDGE_PROMPT: Final[str] = "verification_judge"


class VerificationJudgment(StructuredOutput):
    """Judge LLM's verdict on whether the remediation worked."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: Literal["verified", "not_verified"]
    reasoning: str = Field(min_length=1)


# ---------------------------------------------------------------------------
# Idempotency


def build_idempotency_key(incident_id: str, action_tool: str, arguments: dict[str, Any]) -> str:
    """Deterministic sha256 of incident + tool + args (idempotency_key excluded).

    Same (incident, tool, args) → same key, so a retry is deduped platform-side.
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
    max_attempts: int = DEFAULT_MAX_REMEDIATION_ATTEMPTS,
) -> Callable[[RunState, datetime], RunState]:
    """Bind the LLM to the PLANNING transition: hypothesis + evidence → a ``RemediationPlan``.

    Non-Tier-1 actions are rejected, then every pre-execution guard runs (ADR 0024): three
    escalate, five refuse and re-ask once (ADR 0030, 0032, 0027, 0028, 0025), and one refuses
    under its own name and escalates in the same breath, because its finding is that no action
    is left to take (ADR 0071). ``max_attempts`` is ``MAX_REMEDIATION_ATTEMPTS`` (ADR 0056).
    """

    def transition_plan(run_state: RunState, at: datetime) -> RunState:
        if not run_state.hypotheses:
            return _escalate_remediation(
                run_state, at, "planning entered with no hypotheses on RunState"
            )
        if run_state.remediation_attempts >= max_attempts:
            # A real limit since ADR 0056, not the unreachability assertion ADR 0008 made:
            # VERIFYING may now hand back to INVESTIGATING, so PLANNING can be entered with
            # attempts already spent. VERIFYING declines the edge at the cap, so this fires
            # on a run whose graph or RunState was built some other way — and it is the
            # backstop either way, which is why the number is the same one.
            return _escalate_remediation(
                run_state,
                at,
                f"remediation attempt cap reached (ADR 0056): PLANNING entered with "
                f"remediation_attempts={run_state.remediation_attempts} of "
                f"max_attempts={max_attempts}. No further Tier-1 action is planned; "
                "this incident needs a human.",
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
                # ADR 0030, checked before anything else in the loop: a mis-transcribed
                # id fails the read-before-act guards too, and their steer ("drop the id
                # you have no row for") is the wrong repair for a typo.
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

            # ADR 0056's structural half, and it escalates rather than re-asking: an
            # identical call is not a plan that needs correcting, it is the first attempt
            # again with worse justification (ADR 0008's own words). Compared on the WIRED
            # form, because ``wire_arguments`` default-fills every omitted optional — two
            # plans one `delay_seconds` apart on paper are one call on the wire.
            # No ``attempted_tool`` on the marker: this call was never sent, so recording it
            # as an attempt would have the SAFETY dimension grade a write that never
            # happened. The reason names the tool in words instead.
            repeated = _repeated_attempt(plan, run_state)
            if repeated is not None:
                return _escalate_remediation(run_state, at, repeated)

            # FIRST of the plan-shape guards (ADR 0032), and the order is the priority
            # order of the diagnoses: "not aimed at the incident" is upstream of asking
            # whether a correctly-aimed plan was read for, scoped for or checkable. On
            # live run `a0aa257bf865` every other gate here passed. After the argument
            # guards inside `_plan_once`, though: a mis-transcribed id names no resource,
            # so diagnose the typo first (ADR 0030).
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

            # SECOND, and the only guard here that never re-asks (O-29, ADR 0071): is the
            # fault this action would fix still there? After the subject guard, because on a
            # plan aimed at the wrong resource this question would be asked of furniture; and
            # before the three read-before-act guards, because "there is nothing left to do"
            # outranks every question about how well the doing was prepared. The owner's
            # answer when the resource already reads healthy is not a better plan, it is no
            # plan: report that it cleared on its own and escalate, cause unknown.
            cleared = _cleared_before_action(plan, run_state)
            if cleared is not None:
                return _refuse_cleared_before_action(run_state, at, plan, cleared)

            # Before the verify-leg guard, by priority: "should this action happen at
            # all" outranks "how would you check it". The other order would send the
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

            # The other half of the same rule (ADR 0028): a call naming a category is
            # still an action on rows nobody read. Non-inert over a disjoint tool set,
            # so at most one of the two fires on any plan.
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
            # Refuse and steer, don't escalate: the planner is sent back with the probe
            # it should have picked named for it. Same shape as the investigation loop's
            # ALERT_SUBJECT_PROBES handoff refusal (WO-R2-15 follow-up #177).
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
            tool_name=PLAN_MARKER,
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

    Three outcomes by type: ``RemediationPlan`` (survived), ``RunState`` (a guard escalated),
    ``ArgumentRefusal`` (one re-ask repairs a resource id, ADR 0030). The two argument checks
    stay here rather than in the caller's loop: they must run before ``_misdirected_verify_args``.
    """
    try:
        call = call_with_output_repair(
            llm_client,
            system_prompt=load_prompt("remediation_planner"),
            user_message=_format_plan_context(run_state, top_hypothesis_name),
            output_model=RemediationPlan,
            model=model,
        )
    except (ValueError, ValidationError, LLMError) as err:
        run_state = run_state.model_copy(
            update={"budget": accrue_llm_error(run_state.budget, err, model)}
        )
        return run_state, _escalate_remediation(
            run_state, at, f"{REMEDIATION_PLANNER_INVALID}: {err}"
        )

    # Charge the call the moment it returns, BEFORE the plan is judged: a rejected plan is
    # still a billed call, and ADR 0015 says the meter may over-report and never under-report.
    # Since ADR 0035 the same holds for a re-asked leg whose output did not parse.
    run_state = run_state.model_copy(
        update={"budget": accrue_structured_call(run_state.budget, call, model)}
    )
    plan = call.result.output

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
        # An omitted field is default-filled from the platform's input schema (WO-R2-15, ADR 0024).
        return refuse(
            "plan rejected before execution: resource argument(s) not "
            f"named by the plan: {', '.join(absent)}. An omitted resource "
            "argument is filled from the platform's input-schema default "
            "at wire time, so the call would target that default's "
            "resource instead of this incident's."
        )
    # These two REFUSE rather than escalate (ADR 0030), so they return rather than calling
    # ``refuse``. Position is load-bearing: they must run before ``_misdirected_verify_args``,
    # because a mangled action id makes a correct verify id look misdirected.
    malformed = _malformed_resource_args(plan)
    if malformed:
        return run_state, _argument_refusal(plan, run_state, "malformed", malformed)
    unsourced = _unsourced_resource_args(plan, _evidence_value_corpus(run_state))
    if unsourced:
        # Copy, don't re-type: a resource name the platform never uttered is a
        # hallucination risk. Since ADR 0030 the first offence buys a re-ask
        # carrying the candidates.
        return run_state, _argument_refusal(plan, run_state, "unsourced", unsourced)
    misdirected = _misdirected_verify_args(plan)
    if misdirected:
        # Verify what you changed: an untouched system reads healthy (ADR 0024).
        return refuse(
            "plan rejected before execution: verify probe targets "
            f"resource(s) the action does not: {', '.join(misdirected)}. "
            f"{plan.action_tool} acts on "
            f"{', '.join(sorted(_resource_values(plan.action_tool, plan.action_arguments)))}"
            f"; {plan.verify_tool} must observe the same resource."
        )
    return run_state, plan


def _tier_1_tools() -> frozenset[str]:
    """The Tier-1 slice, derived from the policy map rather than from the Literal above."""
    return tools_at_or_below(Tier.TIER_1) - tools_at_or_below(Tier.READ)


def _wired_call_id(tool: str, arguments: Mapping[str, Any]) -> str | None:
    """``tool`` plus the hash of its WIRED arguments, or ``None`` when they do not validate.

    The comparison ADR 0056 refuses a repeat on. ``idempotency_key`` is dropped from both
    sides: it is a deterministic function of (incident, tool, args), so keeping it would
    compare the same thing twice, and a call that differs in nothing else mints the same key
    and would be replayed from the platform's store (LESSONS 2026-09-07).
    """
    spec = TOOL_REGISTRY.get(tool)
    if spec is None:
        return None
    try:
        wired = wire_arguments(spec, {**arguments, "idempotency_key": "0" * _IDEMPOTENCY_KEY_LEN})
    except ValidationError:
        return None
    wired.pop("idempotency_key", None)
    return f"{tool}:{arguments_hash(wired)}"


def _attempted_calls(run_state: RunState) -> frozenset[str]:
    """Every Tier-1 call this run has already EXECUTED, as ``_wired_call_id`` values.

    Read off the ledger rather than from a new state field: ``make_remediate`` records each
    executed action under its own tool name with the exact wired arguments it sent, so the
    ledger already answers "what has this run done" (invariant 6's shape, one layer in).
    """
    tier_1 = _tier_1_tools()
    return frozenset(
        call_id
        for entry in run_state.evidence
        if entry.tool_name in tier_1
        and (call_id := _wired_call_id(entry.tool_name, entry.arguments)) is not None
    )


def _repeated_attempt(plan: RemediationPlan, run_state: RunState) -> str | None:
    """The escalation reason for a plan repeating a call this run already made, or ``None``."""
    candidate = _wired_call_id(plan.action_tool, plan.action_arguments)
    if candidate is None or candidate not in _attempted_calls(run_state):
        return None
    return (
        f"identical second attempt refused (ADR 0056): "
        f"{plan.action_tool}({json.dumps(plan.action_arguments, sort_keys=True)}) is the same "
        f"call this incident already executed — the same tool with the same arguments once "
        f"every omitted optional is filled in at wire time. The action was NOT executed a "
        f"second time. A retry earns its attempt by targeting a different hypothesis or a "
        f"different resource; re-sending the first attempt would re-use its idempotency key "
        f"and read back as a success that changed nothing. Escalating with the attempt "
        f"already on the ledger."
    )


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

    The alert payload plus tool result summaries that parse as JSON, EXCLUDING values the same
    call's own arguments supplied — an echo would launder a hallucinated probe argument into the
    corpus and whitelist it for the Tier-1 action (B-08). Exclusion is per-entry.
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

    List fields contribute each element; non-strings are ignored.
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

    ``wire_arguments`` *fills* a field carrying a default rather than refusing it —
    ``GetConsumerLagInput.consumer_group`` defaults to ``"worker-dispatcher"``, so a verify leg
    omitting the group probes that group whatever the action restarted. Required fields count
    too, keeping the class pre-execution; coverage pinned by
    ``tests/unit/test_policies.py::TestResourceArgFieldsCoverage``.
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

    Only enforced when both legs name resources, so a verify leg naming NO resource passes here
    — ``_unobserved_action_resource`` decides whether that is legitimate, and ADR 0025 records
    why the two are separate (``get_redis_health`` after ``invalidate_cache_key`` cost a live
    run). Values must line up, not field names (``pause_dag.root_job_id`` ↔ ``get_dag_state``).
    """
    action_values = _resource_values(plan.action_tool, plan.action_arguments)
    verify_values = _resource_values(plan.verify_tool, plan.verify_arguments)
    if not action_values or not verify_values:
        return []
    return sorted(verify_values - action_values)


def _unobserved_action_resource(plan: RemediationPlan) -> tuple[VerifyProbe, ...]:
    """The probes that WOULD observe this action, when the plan picked none.

    Empty tuple means the plan is fine; a non-empty tuple is the refusal, and its contents are
    the steer. The complement of ``_misdirected_verify_args``, which is inert when the verify
    leg names nothing. Three inert cases: no (or an empty) ``VERIFY_PROBE_FOR_ACTION`` entry;
    the action names no resource value (``_absent_resource_args``' report to make); the plan
    already picked a listed probe. Strings only, as in ``_resource_values``.
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

    Narrower than ``_evidence_value_corpus`` deliberately: that corpus holds every string the
    platform uttered, including an id echoed by the alert or read off ``get_dag_state``, which
    is what lets ``_unsourced_resource_args`` pass a job whose row nobody opened. Only the
    listing's own rows count, and only the row's identifying field. Non-string ids are skipped.
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

    ``None`` means the plan is fine. Inert when ``SOURCE_ROW_FOR_ACTION``'s entry is empty, when
    the action names no resource value (``_absent_resource_args``' report), or when every id is
    already a row in evidence. Only the FIRST declared source is reported, but an id counts as
    read if ANY declared source carried it.
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

    A missing key, a JSON ``null`` (what ``wire_arguments`` writes for an unset optional
    filter), a non-string and a whitespace-only string all read as *not narrowed*, on the
    read side and the action side alike.
    """
    if field is None:
        return None
    value = arguments.get(field)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _is_reading(entry: EvidenceEntry, source: SourceListing) -> bool:
    """Whether one evidence entry is a real reading of ``source``, not just its name.

    The rows field must be present and a list, so a non-listing entry recorded under a read
    tool's name cannot count as coverage.
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

    Set containment scope by scope: on every declared scope the read must either not narrow,
    or narrow to exactly the value the action names.
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

    Deduplicated: an ADR-0009 re-probe or ADR-0006 verify poll is one fact, not three.
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

    ``None`` means the plan is fine: inert when ``SOURCE_LISTING_FOR_ACTION``'s entry is empty
    (``_unread_action_rows`` asks the right question there) or when some reading covers every
    scope the action narrows on. ANY covering source admits the plan; the FIRST is reported.
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

    Refuses rather than escalates, like ``_refuse_plan``: the readings already in evidence are
    named, so the planner can re-plan onto the slice it looked at. Underscore-prefixed marker,
    so the trail and the grader's called-tools set skip it and it spends no tool-call budget.
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

    DERIVED from the subject's own probe (``_subject_kind``), never declared twice: the three
    shapes ``investigation.ALERT_SUBJECT_PROBES`` can produce (ADR 0031's resource/slice line).
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

    Returned by ``_unaddressed_alert_subject``; ``reason`` is shared by refusal and escalation.
    """

    subject: AlertSubject
    """The alert's subject, quoted back to the planner and onto the marker."""
    kind: SubjectKind
    """Which target test failed. Recorded so an archive can be asked which."""
    reason: str
    """The sentence both the refusal and the escalation are built from."""


def _subject_kind(subject: AlertSubject) -> SubjectKind:
    """Which target test this subject demands — derived from its probe.

    ``UNFILTERED`` is decisive on its own. Otherwise ``RESOURCE_ARG_FIELDS`` answers whether the
    probe's argument NAMES a resource — the same map ``_resource_values`` reads on the act side.
    """
    if subject.match is SubjectMatch.UNFILTERED:
        return SubjectKind.UNCLASSIFIED
    if subject.argument_field in RESOURCE_ARG_FIELDS.get(subject.tool_name, frozenset()):
        return SubjectKind.RESOURCE
    return SubjectKind.CATEGORY


def _row_source_for_subject(subject: AlertSubject) -> SourceRow | None:
    """The declared row source whose rows carry this subject's decision field.

    Walks ``SOURCE_ROW_FOR_ACTION``'s values rather than a second table; today that is
    ``DLQ_ROW_SOURCE``, and ``tests/unit/test_remediation.py`` pins that it resolves. ``None``
    leaves the guard inert rather than inventing a probe nobody declared.
    """
    for sources in SOURCE_ROW_FOR_ACTION.values():
        for source in sources:
            if (
                source.tool_name == subject.tool_name
                and source.decision_field == subject.argument_field
            ):
                return source
    return None


def _row_decisions_in_evidence(
    evidence: Sequence[EvidenceEntry], source: SourceRow
) -> dict[str, str | None]:
    """Every row id these evidence entries carry, mapped to the decision field's value.

    Sibling of ``_rows_read_for``, which only answers "was this row read at all". ``None`` is a
    VALUE, not a lookup miss: a missing key means never read, a key mapped to ``None`` means
    read and unclassified. A later reading overwrites an earlier one — after a fence the hint
    changes, and the newest reading is the current classification.
    """
    decisions: dict[str, str | None] = {}
    for entry in evidence:
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


class GraphView(NamedTuple):
    """The read whose response is a GRAPH the alerted resource roots (ADR 0070).

    Sibling of ``SourceRow``, which describes a listing of independent rows. The
    difference that earns a second shape: this read's response says which resource it is
    a view OF, so a node id in it is a claim about the alerted object rather than about
    the world at large.
    """

    tool_name: str
    """Read tool that returns the graph. Keyed by the SUBJECT's probe tool in
    ``GRAPH_VIEW_FOR_SUBJECT``, so the admission is inert for every subject whose probe
    returns something else."""
    subject_field: str
    """Top-level field echoing the resource the view is rooted at — compared against the
    alert's subject, which is what scopes a node id to THIS graph."""
    nodes_field: str
    """Top-level key holding the nodes. A plain key, for ``SourceRow.rows_field``'s
    reason."""
    id_field: str
    """Field within a node carrying its resource id."""


# Which subjects have a graph view, and what it is called. Keyed on the subject's own
# probe tool (``investigation.ALERT_SUBJECT_PROBES``), so a consumer-group or DLQ-slice
# subject is untouched by this admission however many chain readings a run holds — which
# is live run `adcdcadd94a3`'s shape and the thing ADR 0032 exists to refuse.
#
# Field names are the pinned contract's, checked against it by
# ``tests/unit/test_remediation.py::TestAChainNodeActionIsAdmittedByTheChainsOwnReading``:
# a renamed response field would otherwise make the admission silently inert rather than
# loud, and an inert admission reads exactly like the refusal it replaced.
GRAPH_VIEW_FOR_SUBJECT: Final[dict[str, GraphView]] = {
    "get_dag_state": GraphView("get_dag_state", "seed_id", "nodes", "id"),
}


def _graph_nodes_in_evidence(
    evidence: Sequence[EvidenceEntry], subject: AlertSubject
) -> frozenset[str]:
    """Every node id this run has read in a graph view rooted at ``subject``.

    Empty when the subject has no graph view, when the run holds no such reading, or when
    every reading it holds is of a DIFFERENT graph — three distinct inert cases that all
    mean the same thing here, which is "no licence". The ``subject_field`` comparison is
    what makes the third one true: this suite seeds three stuck chains, so a node id read
    in one of them is not evidence about another.
    """
    view = GRAPH_VIEW_FOR_SUBJECT.get(subject.tool_name)
    if view is None:
        return frozenset()
    nodes: set[str] = set()
    for entry in evidence:
        if entry.tool_name != view.tool_name:
            continue
        try:
            parsed = json.loads(entry.result_summary)
        except (ValueError, TypeError):
            continue
        if not isinstance(parsed, Mapping):
            continue
        rooted_at = parsed.get(view.subject_field)
        if not isinstance(rooted_at, str) or rooted_at.strip() != subject.value:
            continue
        rows = parsed.get(view.nodes_field)
        if not isinstance(rows, (list, tuple)):
            continue
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            node_id = row.get(view.id_field)
            if isinstance(node_id, str) and node_id.strip():
                nodes.add(node_id.strip())
    return frozenset(nodes)


def _subject_action_field(plan: RemediationPlan, subject: AlertSubject) -> str | None:
    """The action argument that narrows on the subject's own dimension, if any.

    Derived from ``SOURCE_LISTING_FOR_ACTION``'s ``ListingScope`` pairs — the projection
    ADR 0031 used to admit a slice as a subject — so the read side and the act side of one
    dimension cannot drift apart.
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

    ``None`` means the plan is fine: inert when the alert names no subject (an alert storm,
    `db_latency_high`, a whole-queue DLQ depth alert — ``investigation.alert_subject``'s own
    inert set), when no declared listing exposes a slice subject's decision field
    (``_row_source_for_subject``), or when the plan targets the subject. NOT inert: an action
    naming no resource at all under a RESOURCE subject — the shape of live run `adcdcadd94a3`.
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
        # ADR 0070: a node of the graph the subject ROOTS is the subject's own incident.
        # Grounded in a reading THIS RUN HOLDS whose own `seed_id` is the subject, so an
        # invented id, a node of another chain and a batch that reaches outside the graph
        # are all still refused — and `off_graph` says which, by name, in the steer.
        nodes = _graph_nodes_in_evidence(run_state.evidence, subject)
        off_graph = sorted(acted - nodes)
        if acted and not off_graph:
            return None
        names = (
            f"it acts on {', '.join(sorted(acted))}"
            if acted
            else "it names no resource of its own at all"
        )
        graph_route = (
            ""
            if not GRAPH_VIEW_FOR_SUBJECT.get(subject.tool_name)
            else (
                f" An action may instead name a node of {subject.value!r}'s own "
                f"{subject.tool_name} reading — the alerted chain, and only that chain: "
                + (
                    f"this run has read {', '.join(sorted(nodes))}"
                    if nodes
                    else f"this run holds no {subject.tool_name} reading of "
                    f"{subject.value!r} at all, so read it first"
                )
                + (
                    f", and {', '.join(off_graph)} "
                    f"{'is' if len(off_graph) == 1 else 'are'} not among them"
                    if off_graph and nodes
                    else ""
                )
                + "."
            )
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
            f"resource argument is {subject.value!r}.{graph_route} If no Tier-1 action "
            f"can address it, say so — this run escalates naming the subject, which is "
            f"the honest outcome and a better one than remediating something nobody "
            f"reported.",
        )

    source = _row_source_for_subject(subject)
    if source is None:
        return None
    decisions = _row_decisions_in_evidence(run_state.evidence, source)
    wanted: str | None = None if kind is SubjectKind.UNCLASSIFIED else subject.value
    in_slice = sorted(row for row, hint in decisions.items() if hint == wanted)
    slice_names = ", ".join(in_slice) if in_slice else None

    if kind is SubjectKind.UNCLASSIFIED:
        # No `category=` route exists: `remediation_hint=null` means "every
        # category", so unclassified rows are reachable by explicit id only.
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


class ClearedMiss(NamedTuple):
    """A plan whose own resource already reads recovered (O-29, ADR 0071).

    Returned by ``_cleared_before_action``; ``reason`` is what the refusal records and what
    the escalation carries to the briefing, so the on-call reads one sentence, not two.
    """

    resource: str
    """The resource the action would have touched."""
    reading: RecoveredReading
    """The declared predicate that read it recovered — named so the refusal is checkable."""
    rendered: str
    """That reading rendered as the call it was."""
    reason: str
    """The sentence the refusal and the escalation are both built from."""


def _cleared_before_action(plan: RemediationPlan, run_state: RunState) -> ClearedMiss | None:
    """The fault this plan would fix, already gone in the run's newest reading of it.

    ``None`` means the plan may proceed. Inert wherever the question cannot be answered from a
    reading: no declared ``RECOVERED_READING`` for the resource's probe (every entry and its
    reason are in ``agent/attribution.py``), no reading of that resource in this run, or a
    reading that shows the fault present. NOT inert when the newest reading reads recovered —
    that is O-29's "if already healthy, do not act", and it is the one case where the action
    provably cannot cause the recovery the run would go on to report.
    """
    found = already_recovered(run_state.evidence, plan.action_tool, plan.action_arguments)
    if found is None:
        return None
    resource, reading = found
    seen = readings_of(run_state.evidence, reading, resource)
    rendered = render_reading(reading, resource, seen[-1][1])
    return ClearedMiss(
        resource,
        reading,
        rendered,
        # Opens with a banner for the same reason ADR 0026's `STABILIZED, NOT RESOLVED` does:
        # this lands on `EscalationBriefing.escalation_reason` and the first words are what an
        # on-call reads. The owner's sentence follows it VERBATIM — a test pins that.
        f"NO ACTION TAKEN: {CLEARED_ON_ITS_OWN_SENTENCE}. {plan.action_tool} would act on "
        f"{resource}, and this "
        f"run's own newest reading of it — {rendered} — already shows the fault gone: "
        f"{reading.why}. An action cannot cause a recovery that has already happened, so no "
        f"Tier-1 action is taken and this run escalates. The cause is UNKNOWN and may recur: "
        f"nothing this run read says why {resource} recovered, only that it did.",
    )


def _refuse_cleared_before_action(
    run_state: RunState, at: datetime, plan: RemediationPlan, miss: ClearedMiss
) -> RunState:
    """Record the refusal under its own name, then escalate with the same sentence.

    Two entries, in that order, because they answer different questions and the escalation
    marker must stay last (``agent/briefing.py::_terminal_marker`` reads it as the reason).
    Underscore-prefixed, so no tool-call budget is spent and the trail is unchanged.
    """
    entry = EvidenceEntry(
        tool_name=_PLAN_REFUSED_CLEARED_MARKER,
        arguments={
            "action_tool": plan.action_tool,
            "action_arguments": plan.action_arguments,
            "resource": miss.resource,
            "probe_tool": miss.reading.tool_name,
            "reading": miss.rendered,
        },
        result_summary=f"plan refused before execution: {miss.reason}",
        timestamp=at,
    )
    run_state = run_state.model_copy(
        update={"evidence": (*run_state.evidence, entry), "updated_at": at}
    )
    return _escalate_remediation(run_state, at, miss.reason)


def _refuse_subject_target(
    run_state: RunState, at: datetime, plan: RemediationPlan, miss: SubjectMiss
) -> RunState:
    """Refuse a plan aimed at something other than the alert's subject.

    Refuses rather than escalates, like its four siblings and ``investigation._refuse_handoff``:
    the repair needs no new read, since the subject is in the alert and the rows are in the
    evidence the planner was already shown. Underscore-prefixed marker: no tool-call budget.
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

    Values come from the action leg, so the steer is a concrete call; with several resources,
    the first in sorted order — an example, not an enumeration.
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

    Deliberately NOT a terminal transition: the action is correct and only the evidence for it
    is missing, so naming the probe is usually enough. Same shape as
    ``investigation._refuse_handoff``. The marker is underscore-prefixed so the trail and the
    grader's called-tools set skip it, and it spends no tool-call budget.
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

    Refuses rather than escalates, like ``_refuse_plan`` and
    ``investigation._refuse_handoff``: a batch replay carrying one listed id and one unlisted
    one is repaired by dropping the second. Underscore-prefixed marker: it spends no tool-call
    budget, only the re-ask's planner tokens, which ``_plan_once`` charges.
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

    Fields per tool come from ``policies.RESOURCE_ARG_FIELDS``. Compared exactly, never by
    substring: `worker-dispatcher:hot_set` IS a substring of the true key.
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

    The cheap half of ADR 0030, run BEFORE the evidence check: "not the shape of a job id"
    points at the characters, where "never produced that string" leaves a truncated id looking
    invented. Only ``policies.UUID_RESOURCE_FIELDS`` is checked, every offender is reported, and
    a zero-filled block is still 8-4-4-4-12 hex — so shape and provenance are a pair.
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

    Reuses ``SOURCE_ROW_FOR_ACTION`` rather than ``_evidence_value_corpus``, which holds every
    string the platform uttered and would make the offer a wall of noise. Empty for an inert
    entry and for a run that never listed anything; the caller makes no offer then.
    """
    seen: set[str] = set()
    for source in SOURCE_ROW_FOR_ACTION.get(tool, ()):
        seen |= _rows_read_for(run_state, source)
    return tuple(sorted(seen))


def _did_you_mean(value: str, candidates: Sequence[str]) -> str | None:
    """The one candidate a rejected value was probably a slip of, or ``None``.

    Prefix-based, because the slips this exists for (zero-filled tail, truncation, doubled
    block) keep the opening characters. Single-match-only, because offering two is a wrong
    claim: if the prefix cannot separate them, go and look. NEVER a correction — the value is
    quoted into a refusal the planner must act on itself.
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

    The ids this run READ in full, then a per-value "did you mean" wherever one candidate is an
    unambiguous near-match — enumeration alone is a haystack.
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

    Refuses rather than escalates (ADR 0030), against this module's older reading that a
    mis-named resource means the planner reasoned about the wrong object: live run
    ``5c8895771fbd`` quoted the right id in its rationale, briefing and judge reasoning, and
    only the arguments dict was wrong. Underscore-prefixed marker: no tool-call budget is spent.
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
    """What the remediation planner is shown.

    The alert, the ranked hypotheses, the evidence trail, the tools it may
    pick from, and any refusal of the plan it last proposed.
    """
    hypotheses_dump = json.dumps([h.model_dump() for h in run_state.hypotheses], indent=2)
    # Refusals are pulled OUT of the evidence dump and rendered whole at the end: evidence
    # lines are truncated to 200 characters, which would cut a refusal mid-sentence, and a
    # refusal is an instruction about the planner's own last output, so last position is best.
    refusals = [e for e in run_state.evidence if e.tool_name in _PLAN_REFUSAL_MARKERS]
    # An attempt record is pulled out for the same reason and rendered by the same function
    # the investigation planner's context uses (ADR 0056).
    attempted_dump = render_already_attempted(run_state.evidence)
    evidence_dump = "\n".join(
        f"  - [{e.tool_name}] {e.result_summary[:200]}"
        for e in run_state.evidence
        if e.tool_name not in _PLAN_REFUSAL_MARKERS and e.tool_name != ATTEMPT_FAILED_MARKER
    )
    refusal_dump = (
        "\nYour previous plan was REFUSED. Correct it:\n"
        + "\n".join(f"  - {e.result_summary}" for e in refusals)
        + "\n"
        if refusals
        else ""
    )
    # Verbatim platform-contract descriptions: the planner authors the action AND
    # the verify expectation from them.
    tier_1_tools = sorted(name for name in TOOL_REGISTRY if tier_of(name) is Tier.TIER_1)
    tier_1_dump = "\n".join(_tool_context_block(name) for name in tier_1_tools)
    read_tools = sorted(name for name in TOOL_REGISTRY if tier_of(name) is Tier.READ)
    read_dump = "\n".join(_tool_context_block(name) for name in read_tools)
    # The alert is verbatim: resource arguments must be copied from platform values, not prose.
    return (
        f"Incident: {run_state.incident_id}\n"
        f"Alert: {json.dumps(dict(run_state.alert), sort_keys=True)}\n"
        f"Target hypothesis: {top_hypothesis_name}\n\n"
        f"All ranked hypotheses:\n{hypotheses_dump}\n\n"
        f"Evidence collected during investigation:\n{evidence_dump}\n\n"
        f"Tier-1 remediation tools (pick exactly one):\n{tier_1_dump}\n\n"
        f"Read tools (pick one for verification):\n{read_dump}\n"
        f"{f'\n{attempted_dump}\n' if attempted_dump else ''}"
        f"{refusal_dump}"
    )


# ---------------------------------------------------------------------------
# REMEDIATING


def make_remediate(
    mcp_client: MCPClientProtocol,
    action_timeout_seconds: float | None = None,
) -> Callable[[RunState, datetime], RunState]:
    """Bind the MCP client to the REMEDIATING transition.

    Executes the plan's action tool under an agent-generated idempotency key: success →
    VERIFYING, any failure → ESCALATED. ``action_timeout_seconds`` overrides the client
    read-default for this call only.
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

        # Crash-recovery contract (ADR 0008): re-entering REMEDIATING re-invokes with the
        # SAME idempotency_key and the platform's store replays the cached response
        # (platform ADR 0010), proven by tests/integration/test_idempotency_contract.py.
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
            # is_error=False, so the Tier-1 action EXECUTED and only our parse of its
            # response failed. Charged here, unlike the MCPError and is_error=True
            # branches above, which are the platform saying it did NOT act.
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


class RetryDeclined(NamedTuple):
    """Why a finished attempt may NOT reinvestigate (ADR 0056)."""

    kind: Literal["cap", "no_alternative", "budget"]
    """Which precondition failed. Recorded so an archive can be asked which."""
    reason: str
    """The escalation reason, used only by a run that had already retried once."""


def _alternative_targets(run_state: RunState, plan: RemediationPlan) -> tuple[str, ...]:
    """Ranked hypotheses other than the attempted one that a Tier-1 action could address."""
    return tuple(
        hypothesis.name
        for hypothesis in run_state.hypotheses
        if hypothesis.name != plan.target_hypothesis and hypothesis.category in FIX_MAP
    )


def _retry_declined(
    run_state: RunState, plan: RemediationPlan, *, max_attempts: int, verdict: str
) -> RetryDeclined | None:
    """Whether this attempt may hand back to INVESTIGATING, and why not (ADR 0056).

    Three deterministic preconditions, checked in this order: the cap, the budget, and
    somewhere else to go. The third is what keeps the retry from being ADR 0008's rejected
    shape — with no other ranked hypothesis carrying a Tier-1 fix, the only plan a
    reinvestigation could reach is the one ``_repeated_attempt`` refuses, so the outcome is
    already known and spending a planner call to arrive at it buys nothing.
    """
    attempts = run_state.remediation_attempts
    if attempts >= max_attempts:
        return RetryDeclined(
            "cap",
            f"no fix converged after {attempts} Tier-1 attempts (cap "
            f"MAX_REMEDIATION_ATTEMPTS={max_attempts}, ADR 0056). The last attempt "
            f"({plan.action_tool}) ended {verdict}; every attempt this run made is on the "
            "ledger with its verify reading. No further action will be taken autonomously — "
            "the agent's model of this incident is wrong and a human needs the briefing.",
        )
    remaining_calls = run_state.budget.max_tool_calls - run_state.budget.tool_calls_used
    if run_state.budget.is_exhausted or remaining_calls < 2:
        return RetryDeclined(
            "budget",
            f"budget cannot fund a second attempt (remaining tool calls={remaining_calls}); "
            f"the {verdict} verdict on {plan.action_tool} stands and the run escalates. "
            "Budgets are not reset between attempts (ADR 0056).",
        )
    if not _alternative_targets(run_state, plan):
        return RetryDeclined(
            "no_alternative",
            f"no second attempt is available: every ranked hypothesis other than "
            f"{plan.target_hypothesis!r} has no Tier-1 fix, so reinvestigation could only "
            f"re-propose the {plan.action_tool} call already on the ledger (ADR 0056).",
        )
    return None


def _attempt_failed_entry(
    run_state: RunState,
    at: datetime,
    plan: RemediationPlan,
    *,
    max_attempts: int,
    verdict: str,
    reading: str,
    why: str,
) -> EvidenceEntry:
    """The structured record of one failed attempt, rendered under "Already attempted"."""
    trimmed = (
        reading
        if len(reading) <= _ATTEMPT_READING_CHARS
        else f"{reading[:_ATTEMPT_READING_CHARS]}…"
    )
    return EvidenceEntry(
        tool_name=ATTEMPT_FAILED_MARKER,
        # Deliberately NOT ``attempted_tool``: that key means "a call the platform refused"
        # to ``evals/graders/deterministic.py``, and this call executed and is already on
        # the ledger under its own name. A second spelling would double-count it.
        arguments={
            "attempt": run_state.remediation_attempts,
            "of": max_attempts,
            # Which cause this attempt aimed at, so ADR 0059's resolve gate can tell a
            # cause the run has ADDRESSED from one it is still only naming.
            "target_hypothesis": plan.target_hypothesis,
            "action_tool": plan.action_tool,
            "action_arguments": dict(plan.action_arguments),
            "verify_tool": plan.verify_tool,
            "verify_arguments": dict(plan.verify_arguments),
            "verdict": verdict,
        },
        result_summary=(
            f"attempt {run_state.remediation_attempts} of {max_attempts}: "
            f"{plan.action_tool}({json.dumps(plan.action_arguments, sort_keys=True)}) executed, "
            f"then {plan.verify_tool}({json.dumps(plan.verify_arguments, sort_keys=True)}) read "
            f"{trimmed} — verdict {verdict}. {why} Do not propose this call again; target a "
            "different hypothesis or a different resource, or stop."
        ),
        timestamp=at,
    )


def _reinvestigate(run_state: RunState, at: datetime, entry: EvidenceEntry) -> RunState:
    """Hand back to INVESTIGATING with the failed attempt on the ledger (ADR 0056).

    ``hypotheses`` carry over so the planner RE-RANKS given the failure instead of starting
    over; ``budget`` and ``remediation_attempts`` are untouched, so the second attempt spends
    from the first's ledger. The spent plan is dropped: the next PLANNING writes its own, and
    a stale plan on the state is the re-plan-against-stale-evidence shape ADR 0008 refused.
    """
    return run_state.model_copy(
        update={
            "state": IncidentState.INVESTIGATING,
            "remediation_plan": None,
            "evidence": (*run_state.evidence, entry),
            "updated_at": at,
        }
    )


def make_llm_verify(
    mcp_client: MCPClientProtocol,
    llm_client: LLMClientProtocol,
    model: str,
    probe_attempts: int = 1,
    probe_delay_seconds: float = 0.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], datetime] | None = None,
    max_attempts: int = DEFAULT_MAX_REMEDIATION_ATTEMPTS,
    planner_log: PlannerLog | None = None,
) -> Callable[[RunState, datetime], RunState]:
    """Bind clients + model to the VERIFYING transition.

    Probe, then judge the result against the plan's expectation: ``verified`` → RESOLVED,
    ``not_verified`` → re-probe after ``probe_delay_seconds`` until ``probe_attempts`` is spent.
    An attempt that did not end the incident — ``not_verified``, a verified stabilizer
    (ADR 0026) or a verified action that left the alerted condition standing — then either
    hands back to INVESTIGATING for one more attempt or escalates, per ``_retry_declined``
    (ADR 0056, superseding ADR 0008). Polling exists because live probes are eventually
    consistent; defaults (attempts=1, delay=0) keep the single-probe behaviour canned runs
    rely on. Attempt 1 always runs (ADR 0006, why loop.py exempts VERIFYING from the
    loop-level short-circuit); later ones need budget. ``clock`` stamps each poll for real,
    ``None`` with ``at``.

    ``planner_log`` is where each poll's verdict is written the moment the judge returns
    (ADR 0075). A verify leg can poll for minutes, and until ADR 0075 every verdict reached a
    watching operator only when the leg ended, so a run that polled four times looked stuck.
    ``None`` means nobody is watching, and changes nothing about the run.
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

        # Seeded with the transition's `at` so the clock=None path is unchanged (#59).
        at_attempt = at
        # The last poll's reading and the judge's words, for the attempt record ADR 0056
        # writes when this attempt does not end the incident. Seeded so the no-poll case
        # (unreachable while probe_attempts >= 1) still types.
        last_reading = ""
        last_reasoning = ""
        for attempt in range(probe_attempts):
            if attempt > 0:
                if run_state.budget.is_exhausted:
                    # ADR 0006 blesses "one extra probe over budget", not
                    # `verify_probe_attempts` of them. Gate BEFORE the sleep.
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
                judge_call = judge_verification(
                    llm_client,
                    plan=plan,
                    probe_summary=probe_summary,
                    action_summary=_action_result_of(run_state, plan),
                    model=model,
                )
            except (ValueError, ValidationError, LLMError) as err:
                # A billed judge call that then failed is spend, not a free escalation.
                # ``OutputRepairExhausted`` is an ``LLMError`` carrying BOTH legs' totals.
                run_state = run_state.model_copy(
                    update={"budget": accrue_llm_error(run_state.budget, err, model)}
                )
                return _escalate_remediation(
                    run_state, at_attempt, f"{VERIFY_JUDGE_INVALID}: {err}"
                )

            judgment = judge_call.result.output
            last_reading = probe_summary
            last_reasoning = judgment.reasoning
            # {attempt, of} on the evidence arguments is what lets a reader tell poll
            # #2/4 from #4/4 (issue #59); ordinals stay authoritative for canned runs.
            ordinal = {"attempt": attempt + 1, "of": probe_attempts}
            probe_entry = EvidenceEntry(
                tool_name=plan.verify_tool,
                arguments={**arguments, **ordinal},
                result_summary=probe_summary,
                timestamp=at_attempt,
            )
            judge_entry = EvidenceEntry(
                tool_name=VERIFY_JUDGE_MARKER,
                arguments={"expectation": plan.verify_expectation, **ordinal},
                result_summary=f"{judgment.verdict}: {judgment.reasoning}",
                timestamp=at_attempt,
            )
            # Dollars and cache counters accrue per poll attempt, not once per VERIFYING
            # entry (ADR 0015). ``accrue_structured_call``, because a repaired judgment
            # is two billed calls.
            new_budget = accrue_structured_call(run_state.budget, judge_call, model).model_copy(
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
            # ADR 0075: the verdict goes out now, not when this leg ends. A verify leg can poll
            # for minutes against the platform's own 60-second lag clock, and a page that shows
            # nothing for those minutes reads as a hung run. The ranking travels unchanged — a
            # verdict judges the action, it does not re-rank the causes.
            if planner_log is not None:
                planner_log.verdict(
                    hypotheses=tuple(run_state.hypotheses),
                    verdict=judgment.verdict,
                    reasoning=judgment.reasoning,
                    attempt=attempt + 1,
                    of=probe_attempts,
                )
            if judgment.verdict == "verified":
                # "Did the action work?" is not "is the incident over?": a STABILIZE-ONLY
                # pause reads verified and leaves the chain stuck. Consulted here rather
                # than at plan time because a stabilizer is a legitimate plan.
                try:
                    policy = resolution_class_of(plan.action_tool)
                except PolicyCoverageError as err:
                    # Fail closed: an unclassified Tier-1 tool is a missing safety decision.
                    return _escalate_remediation(run_state, at_attempt, str(err))
                if policy.resolution is Resolution.STABILIZES:
                    stabilized = _stabilized_reason(plan, policy.rationale)
                    # ADR 0026's outcome is unchanged when no retry is available, and
                    # unchanged in the end when one is: a stabilizer never resolves. What
                    # ADR 0056 adds is that a stabilized incident may reinvestigate once
                    # before handing off, carrying the sentence below onto the ledger.
                    declined = _retry_declined(
                        run_state, plan, max_attempts=max_attempts, verdict="verified_stabilizer"
                    )
                    if declined is None:
                        return _reinvestigate(
                            run_state,
                            at_attempt,
                            _attempt_failed_entry(
                                run_state,
                                at_attempt,
                                plan,
                                max_attempts=max_attempts,
                                verdict="verified_stabilizer",
                                reading=last_reading,
                                why=stabilized,
                            ),
                        )
                    return _escalate_remediation(
                        run_state,
                        at_attempt,
                        stabilized,
                        # `make_remediate` already charged the action, so no
                        # `executed=True` here — it would bill twice. This carries it
                        # onto the briefing's `attempted_action` field.
                        attempted_tool=plan.action_tool,
                        attempted_arguments=plan.action_arguments,
                    )
                # One question left, about the INCIDENT not the action: is the alerted
                # condition cleared? Yes by construction with a subject (ADR 0032); with
                # none, the queue's rows answer (WO-R2-164). Then the same question about
                # the OTHER faults this run named, which no alert can answer (ADR 0059).
                condition = _uncleared_alert_condition(plan, run_state) or (
                    _unaddressed_second_cause(plan, run_state)
                )
                if condition is not None:
                    # Same shape as the stabilizer above: the action worked and the incident
                    # is not over, so it is an attempt that may earn one reinvestigation.
                    declined = _retry_declined(
                        run_state, plan, max_attempts=max_attempts, verdict="verified_unresolved"
                    )
                    if declined is None:
                        return _reinvestigate(
                            run_state,
                            at_attempt,
                            _attempt_failed_entry(
                                run_state,
                                at_attempt,
                                plan,
                                max_attempts=max_attempts,
                                verdict="verified_unresolved",
                                reading=last_reading,
                                why=condition.reason,
                            ),
                        )
                    return _escalate_remediation(
                        run_state,
                        at_attempt,
                        condition.reason,
                        # As in the stabilizer branch: already charged, and
                        # `attempted_action` stops a repeat being recommended.
                        attempted_tool=plan.action_tool,
                        attempted_arguments=plan.action_arguments,
                    )
                return run_state.with_state(IncidentState.RESOLVED, at_attempt)

        # Every poll spent on ``not_verified``. One more attempt, or hand off (ADR 0056).
        declined = _retry_declined(
            run_state, plan, max_attempts=max_attempts, verdict="not_verified"
        )
        if declined is None:
            return _reinvestigate(
                run_state,
                at_attempt,
                _attempt_failed_entry(
                    run_state,
                    at_attempt,
                    plan,
                    max_attempts=max_attempts,
                    verdict="not_verified",
                    reading=last_reading,
                    why=last_reasoning,
                ),
            )
        # A run that has NOT yet retried escalates exactly as it did under ADR 0008: the
        # judge's `not_verified` verdict is the last entry and so the briefing's reason, and
        # every canned run predating the edge is byte-identical. Only a run that already
        # spent a retry adds a reason of its own, because "we tried twice" is new information.
        if run_state.remediation_attempts > 1:
            return _escalate_remediation(
                run_state,
                at_attempt,
                declined.reason,
                attempted_tool=plan.action_tool,
                attempted_arguments=plan.action_arguments,
            )
        return run_state.with_state(IncidentState.ESCALATED, at_attempt)

    return transition_verify


def _tool_context_block(name: str) -> str:
    """One tool's entry in a planner prompt: its platform description and input schema."""
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


def judge_verification(
    llm_client: LLMClientProtocol,
    *,
    plan: RemediationPlan,
    probe_summary: str,
    action_summary: str | None,
    model: str,
) -> RepairedCall[VerificationJudgment]:
    """Ask the ``action_verifier`` whether the executed action worked.

    The role is ``action_verifier`` (plan 02 § 3); ``verification_judge`` is the prompt file it
    loads. "Verifier" is never unprefixed here —
    ``tests/unit/test_judge_calibration.py::TestTheRoleWordIsAlwaysPrefixed`` sweeps for it.
    One bounded re-ask, cap 1 (ADR 0035, widened by WO-R2-174). A function because WP-6.3's
    calibration must use the same prompt, renderer, schema and repair the run does; a second
    copy in ``evals/`` is the drift that produced INC-002. No temperature and no parameter for
    one (O-24, ADR 0048, ``llm/client.SAMPLING_REJECTED_MODELS``); ADR 0052 measures stability.
    """
    return call_with_output_repair(
        llm_client,
        system_prompt=load_prompt(VERIFICATION_JUDGE_PROMPT),
        user_message=format_verify_context(plan, probe_summary, action_summary),
        output_model=VerificationJudgment,
        model=model,
    )


def format_verify_context(
    plan: RemediationPlan, probe_summary: str, action_summary: str | None = None
) -> str:
    """What the judge is shown.

    Public since WP-6.3 so the calibration asks in exactly the bytes a run would.
    ``action_summary`` is the executed action's own response: without it a delayed replay is
    unjudgeable — ``wait_and_replay`` reports ``scheduled``/``execute_at`` there and the platform
    holds the timer, so the DLQ cannot shrink inside the verify window.
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
    """A tool result read through its output model and dumped back to compact JSON."""
    for block in content:
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            payload = json.loads(block["text"])
            return output_model.model_validate(payload).model_dump_json()
    raise ValueError("no text content block in tool result")


def _stabilized_reason(plan: RemediationPlan, rationale: str) -> str:
    """The escalation reason for a verified STABILIZE-ONLY action.

    ``_escalate_remediation`` puts it on the marker's ``result_summary``, which
    ``agent/briefing.py`` reads into ``EscalationBriefing.escalation_reason``, so it must carry
    three things without reading as a failure: the action worked, the incident is not over, and
    which resource still needs a decision. The resource is named from the plan's own arguments.
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


# ---------------------------------------------------------------------------
# ADR 0031, amended: a partial action on a subject-less alert does not resolve


def _dead_letter_actions() -> frozenset[str]:
    """Every Tier-1 action that acts on dead-letter rows — derived, not declared.

    The union of three maps that already answer it (architecture-principles rule 2):
    ``SOURCE_ROW_FOR_ACTION`` (must have READ a ``DLQ_ROW_SOURCE`` row),
    ``SOURCE_LISTING_FOR_ACTION`` (a listing must have COVERED the slice) and
    ``investigation.HINT_ROUTED_TOOLS`` (where `mark_dlq_permanent` joins, inert in both maps).
    ``tests/unit/test_remediation.py`` pins the derived set so it cannot empty or grow silently.
    """
    tools = {
        tool
        for tool, rows in SOURCE_ROW_FOR_ACTION.items()
        if any(source.tool_name == DLQ_ROW_SOURCE.tool_name for source in rows)
    }
    tools |= {
        tool
        for tool, listings in SOURCE_LISTING_FOR_ACTION.items()
        if any(source.tool_name == DLQ_ROW_SOURCE.tool_name for source in listings)
    }
    tools |= {tool for routed in HINT_ROUTED_TOOLS.values() for tool in routed}
    return frozenset(tools)


DEAD_LETTER_ACTIONS: Final[frozenset[str]] = _dead_letter_actions()


def _dlq_listing_scopes() -> frozenset[str]:
    """Every argument that narrows WHICH ROWS a dead-letter listing returns.

    Derived from the ``ListingScope`` pairs ADR 0028's coverage check reads. `limit` and
    `offset` are not among them — paging is answered by ``_whole_queue_readings``' ``total``.
    """
    return frozenset(
        scope.read_field
        for listings in SOURCE_LISTING_FOR_ACTION.values()
        for source in listings
        if source.tool_name == DLQ_ROW_SOURCE.tool_name
        for scope in source.scopes
    )


DLQ_LISTING_SCOPES: Final[frozenset[str]] = _dlq_listing_scopes()


class UnaddressedRow(NamedTuple):
    """One dead-letter row the alerted condition still holds after the action."""

    job_id: str
    """The row's own id, verbatim from the listing — the briefing names it."""
    hint: str | None
    """Its ``remediation_hint`` as the listing carried it. ``None`` is a value,
    not a lookup miss: the platform returned the row with nothing classified."""


class ConditionMiss(NamedTuple):
    """A verified action that leaves the alerted condition standing.

    ``SubjectMiss`` one state later: that one refuses at PLANNING, this one escalates at the
    RESOLVED transition, because by then the action has executed and been verified.
    """

    unaddressed: tuple[UnaddressedRow, ...]
    """The rows still dead-lettered and untouched by this run. Empty when the
    miss is that no reading in evidence shows what the queue held at all."""
    reason: str
    """The escalation reason, which is the whole human-facing product here —
    ``_escalate_remediation`` puts it on the marker's ``result_summary`` and
    ``agent/briefing.py`` reads it into ``EscalationBriefing.
    escalation_reason``."""


def _stabilized_earlier(run_state: RunState) -> str | None:
    """An earlier attempt in this run that STABILIZED rather than resolved (ADR 0026).

    Read off the attempt records ADR 0056 writes, so the fact travels with the ledger
    rather than with a flag; the returned string is that record's own summary. ADR 0026's
    rule is unconditional — a stabilizer is never a resolution — and ADR 0056 put the
    sentence on the ledger without stopping a LATER attempt resolving over it.
    ``not_verified`` and ``verified_unresolved`` are deliberately not here: the first
    changed nothing (``retry_second_hypothesis_succeeds`` resolves on its second attempt
    and still does) and the second's own condition is re-asked of the current plan.
    """
    for entry in run_state.evidence:
        if entry.tool_name != ATTEMPT_FAILED_MARKER:
            continue
        if str(entry.arguments.get("verdict", "")) == "verified_stabilizer":
            return entry.result_summary
    return None


def _attempted_targets(run_state: RunState, plan: RemediationPlan) -> frozenset[str]:
    """Every hypothesis this run has already aimed a Tier-1 action at."""
    earlier = {
        str(entry.arguments["target_hypothesis"])
        for entry in run_state.evidence
        if entry.tool_name == ATTEMPT_FAILED_MARKER and "target_hypothesis" in entry.arguments
    }
    return frozenset(earlier | {plan.target_hypothesis})


def _is_attempted(hypothesis: Hypothesis, attempted: Collection[str]) -> bool:
    """Whether a ranked hypothesis is one an attempt in this run aimed at.

    Name OR category, because ``RemediationPlan.target_hypothesis`` is a free string and the
    corpus spells it both ways (``jobs_not_progressing_dispatcher_stall`` names the category,
    the retry scenarios name the hypothesis). Reading only one spelling would call an
    already-remediated cause unaddressed and escalate a correct single-fault run.
    """
    return hypothesis.name in attempted or hypothesis.category.value in attempted


def _standing_causes(run_state: RunState, plan: RemediationPlan) -> tuple[Hypothesis, ...]:
    """Causes this run still ranks at the bar it acts on and has NOT acted on (ADR 0059).

    The run's OWN ranking, at the loop's own threshold: below the bar is a cause the agent
    is considering, not one it is asserting, so hedging costs nothing while claiming a
    second cause costs precision if it is wrong. A cause an attempt already targeted is
    addressed, however the ranking still scores it — otherwise a run that fixed both faults
    could never say so.
    """
    attempted = _attempted_targets(run_state, plan)
    return tuple(
        hypothesis
        for hypothesis in run_state.hypotheses
        if not _is_attempted(hypothesis, attempted)
        and hypothesis.confidence >= REMEDIATE_CONFIDENCE_THRESHOLD
    )


def _unaddressed_second_cause(plan: RemediationPlan, run_state: RunState) -> ConditionMiss | None:
    """A verified action that leaves another cause of this incident standing (ADR 0059).

    ``_uncleared_alert_condition``'s question asked of the run's own diagnosis instead of the
    dead-letter queue, so it is live whatever the alert names: a second, independent fault is
    not something an alert can name. Two ways to be unfinished — a stabilizer earlier in this
    run, and a cause the run asserts and has not acted on.
    """
    if (stabilized := _stabilized_earlier(run_state)) is not None:
        return ConditionMiss(
            (),
            f"STABILIZED, NOT RESOLVED. {plan.action_tool} executed successfully and "
            f"{plan.verify_tool} confirmed it landed, and an earlier attempt in this same "
            f"incident only stabilized what it addressed: {stabilized} A stabilizer is never "
            "a resolution (ADR 0026), so this incident ends with a human however well the "
            "later action worked.",
        )
    standing = _standing_causes(run_state, plan)
    if not standing:
        return None
    named = "; ".join(
        f"{hypothesis.name} ({hypothesis.category.value}, confidence {hypothesis.confidence:.2f})"
        for hypothesis in standing
    )
    return ConditionMiss(
        (),
        f"STABILIZED, NOT RESOLVED. {plan.action_tool} executed successfully and "
        f"{plan.verify_tool} confirmed it landed — this is an escalation by design, not a "
        f"failed remediation. This run still ranks {len(standing)} other cause(s) of this "
        f"incident at or above the bar it acts on ({REMEDIATE_CONFIDENCE_THRESHOLD}) and has "
        f"acted on none of them: {named}. One fault fixed is not the incident fixed, and "
        "RESOLVED here would tell the on-call that a fault this run itself named is gone.",
    )


def _evidence_before(
    evidence: Sequence[EvidenceEntry], tool_name: str
) -> tuple[EvidenceEntry, ...] | None:
    """The evidence prefix recorded before ``tool_name`` was first called.

    ``make_remediate`` writes the action's own entry under ``plan.action_tool``, so this is the
    run as it stood when the action was chosen. Slicing there matters: the verify probe re-reads
    the same listing afterwards, so a post-action page answers "what did the action do?", not
    "what was the alert about?". ``None`` (no entry under that name) means the action cannot be
    located and callers treat it as inert; a real run cannot produce it.
    """
    prefix: list[EvidenceEntry] = []
    for entry in evidence:
        if entry.tool_name == tool_name:
            return tuple(prefix)
        prefix.append(entry)
    return None


def _whole_queue_readings(
    evidence: Sequence[EvidenceEntry], source: SourceRow
) -> tuple[EvidenceEntry, ...]:
    """The readings that saw the WHOLE dead-letter queue, not a slice or a page.

    Two conditions: **narrowed on nothing** — every declared ``ListingScope.read_field`` absent
    by ``_scope_value``'s four-way collapse — and **the page is the queue**, ``total`` equal to
    the rows returned (``total: 40`` with four rows saw page one of ten). No usable ``total`` is
    accepted: this is a contradiction test, not an invention.
    """
    readings: list[EvidenceEntry] = []
    for entry in evidence:
        if entry.tool_name != source.tool_name:
            continue
        if any(_scope_value(entry.arguments, field) is not None for field in DLQ_LISTING_SCOPES):
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
        total = parsed.get("total")
        if isinstance(total, int) and not isinstance(total, bool) and total != len(rows):
            continue
        readings.append(entry)
    return tuple(readings)


def _rows_addressed_by(
    plan: RemediationPlan, decisions: Mapping[str, str | None]
) -> frozenset[str]:
    """The rows this run's one action ADDRESSED — replayed, scheduled, or fenced.

    Narrow and provable: a row is addressed when the action NAMED it, **by id** (among its
    ``RESOURCE_ARG_FIELDS`` values) or **by slice** (narrowed to the value the listing classified
    it on; ``ListingScope.action_field is None`` spans every value). Un-provable is unaddressed —
    with another declared scope also narrowed, no map says which rows survive the intersection,
    so the slice route guesses nothing. It does NOT claim the row is fixed (``RESOLUTION_CLASS``).
    """
    addressed = {
        value
        for value in _resource_values(plan.action_tool, plan.action_arguments)
        if value in decisions
    }
    for source in SOURCE_LISTING_FOR_ACTION.get(plan.action_tool, ()):
        if source.tool_name != DLQ_ROW_SOURCE.tool_name:
            continue
        hint_scope = next(
            (s for s in source.scopes if s.read_field == DLQ_ROW_SOURCE.decision_field), None
        )
        if hint_scope is None:
            continue
        if any(
            _scope_value(plan.action_arguments, scope.action_field) is not None
            for scope in source.scopes
            if scope is not hint_scope
        ):
            continue
        if hint_scope.action_field is None:
            addressed |= set(decisions)
            continue
        wanted = _scope_value(plan.action_arguments, hint_scope.action_field)
        if wanted is None:
            continue
        addressed |= {row for row, hint in decisions.items() if hint == wanted}
    return frozenset(addressed)


# What each remaining row still needs, in words a human can act on. The TOOL half is derived
# from ``HINT_ROUTED_TOOLS``; this is the phrase half, keyed on the same slice words (``None``
# → ``unclassified``) so ``tests/unit/test_remediation.py`` pins the key sets equal.
_ROW_DISPOSITION: Final[dict[str, str]] = {
    "replay_safe": "an immediate replay",
    "wait_and_replay": "a delayed replay, once the dependency its error names answers",
    "human_required": "a human decision, behind a fence so no later bulk replay re-runs it",
    "unclassified": (
        "a human decision — nobody has classified it, so no routing here can either; "
        "read its error first"
    ),
}


def _row_needs(row: UnaddressedRow) -> str:
    """One remaining row, rendered as "id (hint) needs <disposition> (<tools>)"."""
    slice_word = row.hint if row.hint is not None else "unclassified"
    disposition = _ROW_DISPOSITION.get(
        slice_word, "a decision this agent has no routing for — hand it over as it stands"
    )
    tools = HINT_ROUTED_TOOLS.get(slice_word, frozenset())
    routed = f" ({', '.join(sorted(tools))})" if tools else ""
    shown = row.hint if row.hint is not None else "no hint"
    return f"{row.job_id} [{shown}] needs {disposition}{routed}"


def _uncleared_condition_reason(
    plan: RemediationPlan, miss_rows: tuple[UnaddressedRow, ...], read_count: int
) -> str:
    """The escalation reason for a verified action that left the condition standing.

    Opens with ``STABILIZED, NOT RESOLVED``, the string ADR 0026 minted for a stabilize-only
    TOOL: this is a stabilize-only OUTCOME, the same message to the same reader.
    """
    if not miss_rows:
        return (
            f"STABILIZED, NOT RESOLVED. {plan.action_tool} executed successfully and "
            f"{plan.verify_tool} confirmed it landed — this is an escalation by design, "
            f"not a failed remediation. The alert named no subject, so the incident is "
            f"the dead-letter queue itself, and no reading in this run's evidence shows "
            f"what that queue held before the action: every listing was narrowed to a "
            f"slice or was a partial page. One action cannot be shown to have cleared a "
            f"queue nobody read whole. Handing this to a human with the action named is "
            f"the honest end; RESOLVED would claim a queue is clear on evidence that "
            f"never covered it."
        )
    listed = "; ".join(_row_needs(row) for row in miss_rows)
    return (
        f"STABILIZED, NOT RESOLVED. {plan.action_tool} executed successfully and "
        f"{plan.verify_tool} confirmed it landed — this is an escalation by design, not "
        f"a failed remediation. The alert named no subject, so the incident is the "
        f"dead-letter queue this run read, and one Tier-1 action (ADR 0008) addressed "
        f"{read_count - len(miss_rows)} of its {read_count} rows. "
        f"{len(miss_rows)} row(s) are still dead-lettered with nothing recorded against "
        f"them by this run: {listed}. Each needs an action this run had no second call "
        f"for, so a human decides them. The partial fix is real and it is not an "
        f"outcome: RESOLVED here would tell the on-call the queue is clear while these "
        f"rows sit in it."
    )


def _uncleared_alert_condition(plan: RemediationPlan, run_state: RunState) -> ConditionMiss | None:
    """The alerted condition this verified action did not clear (WO-R2-164).

    ``None`` means RESOLVED is admissible. Inert when the alert names a subject (ADR 0032 has
    already refused any plan aimed elsewhere, and a second question would escalate every
    correctly-scoped run, against ADR 0031); when the action is not a dead-letter action (the
    link is the agent's own plan; nothing parses alert free text, which ADR 0031 refused); when
    the whole queue was read and every row addressed; when the action is not locatable
    (``_evidence_before``); and when no listing precedes it (ADR 0027/0028 own that). NOT inert:
    filtered or partial pages, per ``_whole_queue_readings``.
    """
    if alert_subject(run_state.alert) is not None:
        return None
    if plan.action_tool not in DEAD_LETTER_ACTIONS:
        return None
    before = _evidence_before(run_state.evidence, plan.action_tool)
    if before is None:
        return None
    if not any(entry.tool_name == DLQ_ROW_SOURCE.tool_name for entry in before):
        return None
    readings = _whole_queue_readings(before, DLQ_ROW_SOURCE)
    if not readings:
        return ConditionMiss((), _uncleared_condition_reason(plan, (), 0))
    decisions = _row_decisions_in_evidence(readings, DLQ_ROW_SOURCE)
    addressed = _rows_addressed_by(plan, decisions)
    miss_rows = tuple(
        UnaddressedRow(row_id, decisions[row_id])
        for row_id in sorted(decisions)
        if row_id not in addressed
    )
    if not miss_rows:
        return None
    return ConditionMiss(miss_rows, _uncleared_condition_reason(plan, miss_rows, len(decisions)))


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

    ``attempted_tool`` / ``attempted_arguments`` record a Tier-1 call that was MADE and refused;
    without them the entry is ``_remediation_escalate``, which the SAFETY dimension does not
    match, and ``docs/eval-methodology.md`` promises the attempt graded red even when the
    platform blocks it. ``executed=True`` (only the unparseable-response branch) charges one
    tool call and one ``remediation_attempts``, which ADR 0008 reads for the single-attempt
    invariant. The reason lands on ``result_summary``, read into
    ``EscalationBriefing.escalation_reason`` by ``agent/briefing.py``.
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
