"""Remediation loop: plan → execute → verify.

``make_llm_plan`` emits a ``RemediationPlan`` (one Tier-1 action, one verify probe),
``make_remediate`` executes it under a deterministic idempotency key, ``make_llm_verify`` judges.
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

# Every Tier-1 tool name, written out by hand because a Pydantic Literal needs literal values at
# import time. `TestLiteralRegistryDrift` fails when this list stops matching the tool registry.
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

# How often one PLANNING transition may refuse a plan because its verify read cannot observe the
# resource the action changes. One, not the investigation loop's two: PLANNING is a single call.
_MAX_VERIFY_TARGET_REFUSALS: Final[int] = 1

# The same allowance for a plan replaying a job whose dead-letter row nobody read. One, because
# the planner cannot read anything itself; the only repair is dropping the ids it has no row for.
_MAX_UNREAD_ROW_REFUSALS: Final[int] = 1

# One more for the category case (ADR 0028): the run did list the queue, but the plan sweeps a
# different slice than the one it read. The tools are disjoint, so no plan spends both allowances.
_MAX_UNLISTED_CATEGORY_REFUSALS: Final[int] = 1

# One more for the question upstream of the rest (ADR 0032): does the action touch what the alert
# is about at all? Two live runs read the right resource and then remediated a different one.
_MAX_SUBJECT_TARGET_REFUSALS: Final[int] = 1

# One more for a plan whose reasoning is right but whose ids are mistyped (ADR 0030). The refusal
# offers back the ids the run did read and asks again; correcting one here would be us guessing.
_MAX_ARGUMENT_REFUSALS: Final[int] = 1

# One ledger row name per refusal above. The leading underscore keeps them out of the tool trail
# and off the budget; separate names let a reader ask which question a plan failed.
_PLAN_REFUSED_MARKER: Final[str] = "_plan_refused"
_PLAN_REFUSED_UNREAD_ROW_MARKER: Final[str] = "_plan_refused_unread_row"
_PLAN_REFUSED_UNLISTED_CATEGORY_MARKER: Final[str] = "_plan_refused_unlisted_category"
_PLAN_REFUSED_ARGUMENT_MARKER: Final[str] = "_plan_refused_argument"
_PLAN_REFUSED_SUBJECT_TARGET_MARKER: Final[str] = "_plan_refused_subject_target"
# The one refusal that ends the run instead of asking again (ADR 0071's rule): this run's own
# newest reading already shows the fault gone, so there is no better plan to ask for.
_PLAN_REFUSED_CLEARED_MARKER: Final[str] = "_plan_refused_cleared_before_action"

# How many characters of the verify reading a failed-attempt record quotes back to the planner. A
# whole queue listing would crowd out its context, and the full reading is on the ledger anyway.
_ATTEMPT_READING_CHARS: Final[int] = 400

# The refusal rows ``_format_plan_context`` renders in full, and last, in the planner's context. A
# set, not one name: matching ``_PLAN_REFUSED_MARKER`` alone once truncated every other refusal.
_PLAN_REFUSAL_MARKERS: Final[frozenset[str]] = frozenset(
    {
        _PLAN_REFUSED_MARKER,
        _PLAN_REFUSED_UNREAD_ROW_MARKER,
        _PLAN_REFUSED_UNLISTED_CATEGORY_MARKER,
        _PLAN_REFUSED_ARGUMENT_MARKER,
        _PLAN_REFUSED_SUBJECT_TARGET_MARKER,
    }
)

# How many leading characters of a rejected id must match a real one before the refusal offers it
# as "did you mean". Eight is a UUID's first block; the mistyping this exists for began at 10.
_MIN_DID_YOU_MEAN_PREFIX: Final[int] = 8

# Matches a UUID in exactly the shape the platform's input schema declares. Shape only: an id of
# all zeroes passes, so this never replaces the check that the run really read that id.
_CANONICAL_UUID: Final[re.Pattern[str]] = re.compile(
    r"\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z"
)


# How the escalation sentence opens for each kind of bad argument, so a human reading it can tell
# "that could never be an id" from "that is not one of the ids this run read".
_ARGUMENT_ESCALATION_CLAUSE: Final[dict[str, str]] = {
    "malformed": "wrote a resource identifier that is not the shape the platform declares",
    "unsourced": "named a resource that is not one this run read",
}


class ArgumentRefusal(NamedTuple):
    """A plan whose resource arguments are wrong in a way one re-ask repairs.

    Returned by ``_plan_once`` instead of an escalated ``RunState``, so the caller's loop can
    spend a refusal budget on it. ``reason`` is shared by refusal and escalation.
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


# For each Tier-1 action, the reads that can observe what it changed. Every action above has an
# entry; an empty tuple states on purpose that no read can observe that one.
VERIFY_PROBE_FOR_ACTION: Final[dict[str, tuple[VerifyProbe, ...]]] = {
    "invalidate_cache_key": (VerifyProbe("get_cache_key_info", "key"),),
    "restart_consumer_group": (VerifyProbe("get_consumer_lag", "consumer_group"),),
    # `pause_dag` calls it `root_job_id` and `get_dag_state` calls it `job_id`, so the two
    # arguments must carry the same VALUE; the field names are allowed to differ.
    "pause_dag": (VerifyProbe("get_dag_state", "job_id"),),
    # One dead-letter row can only be observed by listing the queue; reading the chain works
    # where the row's job is that chain's root.
    "mark_dlq_permanent": (
        VerifyProbe("list_dlq_messages", None),
        VerifyProbe("get_dag_state", "job_id"),
    ),
    "replay_dlq_by_ids": (
        VerifyProbe("list_dlq_messages", None),
        VerifyProbe("get_dag_state", "job_id"),
    ),
    # No verify read demanded: these two name a category or a job type rather than one
    # resource, so there is nothing specific for a read to observe.
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


# The read whose rows must have been seen before an action may touch one: a dead-letter row is the
# only place its `remediation_hint` lives. Seeing the row is the requirement, not its value.
DLQ_ROW_SOURCE: Final[SourceRow] = SourceRow("list_dlq_messages", "items", "id", "remediation_hint")


SOURCE_ROW_FOR_ACTION: Final[dict[str, tuple[SourceRow, ...]]] = {
    "replay_dlq_by_ids": (DLQ_ROW_SOURCE,),
    # Nothing required: a bulk replay names a category or job type and no row ids, so there is
    # no id to look for. The rule that does cover them is SOURCE_LISTING_FOR_ACTION below.
    "replay_dlq_by_category": (),
    "replay_dlq_messages": (),
    # Nothing required: fencing a row stops it being replayed and re-runs nothing, so acting on
    # a row nobody read cannot cause the harm this guard exists to prevent.
    "mark_dlq_permanent": (),
    # Nothing required: no listing classifies a cache key, a consumer group or a chain root,
    # so there is no row to have seen first.
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


# For an action that names no rows, the read that must have covered everything it will sweep
# (ADR 0027's rule). The claim is what the run looked at, so a slice emptied since still counts.
SOURCE_LISTING_FOR_ACTION: Final[dict[str, tuple[SourceListing, ...]]] = {
    # `job_type` matters: a read narrowed to one job type saw less than a replay naming none.
    # An action with no `category` sweeps every category, so only an unfiltered read covers it.
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
    # The widest sweep: it narrows on `job_type` only and replays uncategorised rows too, which
    # is what `action_field=None` says. Declared here, yet forbidden in every DLQ scenario.
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
    # Nothing required here: these name rows, so the map above asks the question instead. Asking
    # about coverage as well would refuse a correct by-id replay read off a filtered listing.
    "replay_dlq_by_ids": (),
    "mark_dlq_permanent": (),
    # These three each name a single resource, so there is no filter whose coverage to judge.
    "invalidate_cache_key": (),
    "restart_consumer_group": (),
    "pause_dag": (),
}


class RemediationPlan(StructuredOutput):
    """One remediation plan: action + verification. Both tool fields are Literal-typed against
    the registry, and ``make_llm_plan`` re-checks tier."""

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
    # A field for the planner to show its working in. The model rejects unknown fields, so a
    # prompt that asks for a rationale needs one declared here. Optional, so old fixtures fit.
    action_rationale: str | None = Field(
        default=None,
        description=(
            "Optional. Why these action arguments and not others — required by the "
            "system prompt for a delayed DLQ replay, where the delay is a judgement "
            "derived from the rows rather than a value read off one. Name the row "
            "and the wait it stated. Free text; not graded."
        ),
    )


#: The verification judge's role name, and the prompt file holding its rubric. The two spellings
#: differ on purpose: renaming the file would move a snapshot hash and rewrite committed records.
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
    """Deterministic sha256 of incident + tool + args, so a retry with the same (incident, tool,
    args) produces the same key and the platform dedupes it."""
    # Never include idempotency_key in the hash: it is what we are generating.
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
    escalate, five refuse and re-ask once, and one does both because no action is left (ADR 0071).
    """

    def transition_plan(run_state: RunState, at: datetime) -> RunState:
        # 1. Escalate before spending a planner token if there is no ranking to plan from.
        if not run_state.hypotheses:
            return _escalate_remediation(
                run_state, at, "planning entered with no hypotheses on RunState"
            )
        # 2. Escalate if every remediation attempt is already used. VERIFYING may hand the run
        #    back here (ADR 0056's rule), so PLANNING can be entered with attempts spent.
        if run_state.remediation_attempts >= max_attempts:
            return _escalate_remediation(
                run_state,
                at,
                f"remediation attempt cap reached (ADR 0056): PLANNING entered with "
                f"remediation_attempts={run_state.remediation_attempts} of "
                f"max_attempts={max_attempts}. No further Tier-1 action is planned; "
                "this incident needs a human.",
            )
        # 3. Escalate unless two tool calls remain, one for the action and one to verify it: an
        #    action nobody can afford to check on is worse than no action at all.
        remaining_calls = run_state.budget.max_tool_calls - run_state.budget.tool_calls_used
        if remaining_calls < 2:
            return _escalate_remediation(
                run_state,
                at,
                f"insufficient tool-call budget for action+verify "
                f"(remaining={remaining_calls}); escalating without executing",
            )

        # 4. Give each guard below its own refusal allowance, then ask for a plan and check it,
        #    again and again, until one plan survives every guard or a guard escalates.
        top = run_state.hypotheses[0]
        refusals_spent = 0
        unread_refusals_spent = 0
        unlisted_refusals_spent = 0
        argument_refusals_spent = 0
        subject_refusals_spent = 0
        while True:
            run_state, outcome = _plan_once(run_state, at, llm_client, model, top.name)
            # 5. A mistyped resource id (ADR 0030) is handled first: it also trips the guards
            #    below, whose advice — go and read the row — is the wrong fix for a typo.
            if isinstance(outcome, ArgumentRefusal):
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
            # 6. A guard inside `_plan_once` already ended the run: return its state unchanged.
            if isinstance(outcome, RunState):
                return outcome
            plan = outcome

            # 7. Escalate on a plan identical to one already attempted (ADR 0056's rule): the
            #    same call cannot earn a second try. Compared as the arguments go on the wire.
            repeated = _repeated_attempt(plan, run_state)
            if repeated is not None:
                return _escalate_remediation(run_state, at, repeated)

            # 8. Refuse a plan that acts on something other than what the alert is about
            #    (ADR 0032's rule). First of the plan-shape guards, because it matters most.
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

            # 9. End the run where this run's newest reading already shows the fault gone
            #    (ADR 0071's rule) — the one guard that never re-asks: no plan is the answer.
            cleared = _cleared_before_action(plan, run_state)
            if cleared is not None:
                return _refuse_cleared_before_action(run_state, at, plan, cleared)

            # 10. Refuse a plan replaying a dead-letter row nobody read (ADR 0027's rule).
            #     Before the verify guard: whether to act outranks how the result is checked.
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

            # 11. The same refusal for a bulk replay over a slice this run never listed
            #     (ADR 0028's rule). Different tools, so only one of these two can fire.
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

            # 12. Refuse a plan whose verify read cannot observe the resource the action changes
            #     (ADR 0025's rule), naming the reads it could use. Pass here and the plan stands.
            unobserved = _unobserved_action_resource(plan)
            if not unobserved:
                break
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

        # 13. Record the surviving plan on the ledger, store it on the run, and move the run to
        #     REMEDIATING, which is where the action is actually executed.
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
                # Stored as a plain dict so state.py need not import this module; REMEDIATING
                # and VERIFYING validate it back into a plan through ``_load_plan``.
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

    Three outcomes by type: the plan survived, a guard escalated, or one re-ask repairs a resource
    id (ADR 0030). The argument checks stay here, before ``_misdirected_verify_args``.
    """
    # 1. Ask the planner for a plan, re-asking once if its output does not validate (ADR 0035's
    #    rule). Escalate if that fails too, charging the failed calls to the budget.
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

    # 2. Charge the call before the plan is judged: a plan the guards reject was still billed,
    #    and the budget may over-report but must never under-report (ADR 0015's rule).
    run_state = run_state.model_copy(
        update={"budget": accrue_structured_call(run_state.budget, call, model)}
    )
    plan = call.result.output

    def refuse(reason: str) -> tuple[RunState, RunState]:
        return run_state, _escalate_remediation(run_state, at, reason)

    # 3. Escalate unless both tools are in the registry and each sits at the tier its job needs:
    #    the action must be Tier-1, and the verify call must be read-only.
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
    # 4. Escalate if the plan leaves out a field naming what to act on. An absent argument is
    #    filled from the schema's default, so the call would hit that, not this incident.
    absent = _absent_resource_args(plan)
    if absent:
        return refuse(
            "plan rejected before execution: resource argument(s) not "
            f"named by the plan: {', '.join(absent)}. An omitted resource "
            "argument is filled from the platform's input-schema default "
            "at wire time, so the call would target that default's "
            "resource instead of this incident's."
        )
    # 5. Ask again, rather than escalate, when an id is the wrong shape or was never read
    #    (ADR 0030's rule). Before step 6, since a mangled id makes a good verify leg look wrong.
    malformed = _malformed_resource_args(plan)
    if malformed:
        return run_state, _argument_refusal(plan, run_state, "malformed", malformed)
    unsourced = _unsourced_resource_args(plan, _evidence_value_corpus(run_state))
    if unsourced:
        return run_state, _argument_refusal(plan, run_state, "unsourced", unsourced)
    # 6. Escalate if the verify call names a different resource than the action does: reading
    #    something the action never touched reports healthy whatever the action did.
    misdirected = _misdirected_verify_args(plan)
    if misdirected:
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

    The comparison ADR 0056 refuses a repeat on. ``idempotency_key`` is dropped from both sides,
    since it is already a function of (incident, tool, args).
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

    Off the ledger rather than a new state field: ``make_remediate`` records each executed action
    with the exact wired arguments it sent, so the ledger already answers this.
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

    The alert plus JSON result summaries, EXCLUDING values the same call's own arguments supplied:
    an echo would launder a hallucinated probe argument into the corpus (B-08).
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

    ``wire_arguments`` FILLS a field carrying a default rather than refusing it, so a verify leg
    that omits ``consumer_group`` probes ``"worker-dispatcher"`` whatever the action restarted.
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

    Only enforced when both legs name resources; ``_unobserved_action_resource`` covers the rest
    (ADR 0025). VALUES must line up, not field names.
    """
    action_values = _resource_values(plan.action_tool, plan.action_arguments)
    verify_values = _resource_values(plan.verify_tool, plan.verify_arguments)
    if not action_values or not verify_values:
        return []
    return sorted(verify_values - action_values)


def _unobserved_action_resource(plan: RemediationPlan) -> tuple[VerifyProbe, ...]:
    """The probes that WOULD observe this action, when the plan picked none.

    Empty means the plan is fine; a non-empty tuple is the refusal and its own steer. Inert with
    no ``VERIFY_PROBE_FOR_ACTION`` entry, no acted-on value, or a probe already picked.
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
            # This read observes the resource without being able to name it in an
            # argument, so choosing this tool at all is the whole requirement.
            return ()
        observed = plan.verify_arguments.get(probe.argument_field)
        if isinstance(observed, str) and observed in acted:
            return ()
    return probes


def _rows_read_for(run_state: RunState, source: SourceRow) -> set[str]:
    """Every resource id this run has actually SEEN a row for, per one source.

    Deliberately narrower than ``_evidence_value_corpus``, which holds ids the alert merely
    echoed: only the listing's own rows count, and only the row's identifying field.
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

    ``None`` means the plan is fine. Only the FIRST declared source is reported, but an id counts
    as read if ANY declared source carried it.
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

    A missing key, a JSON ``null``, a non-string and whitespace all read as NOT narrowed, on the
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

    ``None`` means the plan is fine. ANY covering source admits the plan; the FIRST is reported.
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

    Refuses rather than escalates, like ``_refuse_plan``, naming the readings already in evidence
    so the planner can re-plan onto the slice it looked at.
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
    shapes ``ALERT_SUBJECT_PROBES`` can produce (ADR 0031).
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

    Walks ``SOURCE_ROW_FOR_ACTION`` rather than a second table. ``None`` leaves the guard inert
    rather than inventing a probe nobody declared.
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

    ``None`` is a VALUE, not a lookup miss: absent means never read, mapped to ``None`` means read
    and unclassified. A later reading wins, because a fence changes the hint.
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

    Not ``SourceRow``, because this read's response says which resource it is a view OF, so a node
    id in it is a claim about the alerted object rather than about the world at large.
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


# Which kinds of alert subject have a reading that lists other nodes beneath them. Keyed on the
# subject's own read tool, so a consumer-group alert is unaffected by any chain reading a run holds.
GRAPH_VIEW_FOR_SUBJECT: Final[dict[str, GraphView]] = {
    "get_dag_state": GraphView("get_dag_state", "seed_id", "nodes", "id"),
}


def _graph_nodes_in_evidence(
    evidence: Sequence[EvidenceEntry], subject: AlertSubject
) -> frozenset[str]:
    """Every node id this run has read in a graph view rooted at ``subject``.

    Empty means "no licence". The ``subject_field`` comparison is what rules out a reading of a
    DIFFERENT graph: the suite seeds three stuck chains, and one is not evidence about another.
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

    Derived from ``SOURCE_LISTING_FOR_ACTION``'s ``ListingScope`` pairs (ADR 0031), so the read
    side and the act side of one dimension cannot drift apart.
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

    ``None`` means the plan is fine. NOT inert for an action naming no resource at all under a
    RESOURCE subject, which is the live-run shape ADR 0032 was written from.
    """
    # 1. Work out what the alert is about, whether that is one resource or a whole slice of the
    #    queue, and which resources this plan's action would actually touch.
    subject = alert_subject(run_state.alert)
    if subject is None:
        return None
    kind = _subject_kind(subject)
    acted = _resource_values(plan.action_tool, plan.action_arguments)
    rendered = f"{plan.action_tool}({json.dumps(plan.action_arguments, sort_keys=True)})"

    # 2. The alert is about one named resource: the plan is fine if its action names that same
    #    resource.
    if kind is SubjectKind.RESOURCE:
        if subject.value in acted:
            return None
        # 3. It is also fine if the action names a node beneath that resource in a reading THIS
        #    run holds (ADR 0070's rule); a node of some other chain is still refused.
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

    # 4. The alert is about a slice of the dead-letter queue instead: collect the rows this run's
    #    own listing put in that slice, which is what the action is allowed to touch.
    source = _row_source_for_subject(subject)
    if source is None:
        return None
    decisions = _row_decisions_in_evidence(run_state.evidence, source)
    wanted: str | None = None if kind is SubjectKind.UNCLASSIFIED else subject.value
    in_slice = sorted(row for row, hint in decisions.items() if hint == wanted)
    slice_names = ", ".join(in_slice) if in_slice else None

    # 5. The slice is the rows the platform never classified. Those can only be acted on by
    #    explicit id: a null `remediation_hint` on the read means "every category", not these rows.
    if kind is SubjectKind.UNCLASSIFIED:
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

    # 6. The slice is a named category: the plan is fine if the action replays that category by
    #    name, or names only rows this run's listing placed in it.
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

    ``None`` means the plan may proceed, and it is inert wherever no reading can answer. A newest
    reading that reads recovered is O-29's "if already healthy, do not act".
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
        # Opens with a shouted banner because this sentence becomes the run's
        # `escalation_reason`, and its first words are what an on-call engineer reads.
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

    Two entries, in that order: the escalation marker must stay last, because
    ``briefing._terminal_marker`` reads it as the reason.
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

    Refuses rather than escalates, like its four siblings: the repair needs no new read, since the
    subject is in the alert and the rows are in evidence the planner was already shown.
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

    Deliberately NOT terminal: the action is correct and only the evidence for it is missing, so
    naming the probe is usually enough.
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

    Refuses rather than escalates, like ``_refuse_plan``: a batch replay carrying one listed id
    and one unlisted one is repaired by dropping the second.
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

    The cheap half of ADR 0030, run BEFORE the evidence check: "not the shape of a job id" points
    at the characters. A zero-filled block is still valid hex, so shape and provenance are a pair.
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

    Reuses ``SOURCE_ROW_FOR_ACTION``, not ``_evidence_value_corpus``, which would make the offer
    a wall of noise. Empty means the caller makes no offer.
    """
    seen: set[str] = set()
    for source in SOURCE_ROW_FOR_ACTION.get(tool, ()):
        seen |= _rows_read_for(run_state, source)
    return tuple(sorted(seen))


def _did_you_mean(value: str, candidates: Sequence[str]) -> str | None:
    """The one candidate a rejected value was probably a slip of, or ``None``.

    Prefix-based, because the slips this exists for keep the opening characters, and
    single-match-only, because offering two is a wrong claim. NEVER a correction.
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
        # Having nothing to offer is itself the advice: the planner has to go and read
        # the rows. If it asks again without doing so, the next refusal escalates.
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

    Refuses rather than escalates (ADR 0030): a live run quoted the right id in its rationale,
    briefing and judge reasoning, and only the arguments dict was wrong.
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
    # Refusals are taken out of the evidence list and printed in full at the end: evidence lines
    # are cut to 200 characters, which would truncate a refusal the planner must act on.
    refusals = [e for e in run_state.evidence if e.tool_name in _PLAN_REFUSAL_MARKERS]
    # A record of a failed attempt is taken out for the same reason, and rendered by the same
    # function that writes it into the investigation planner's context (ADR 0056's rule).
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
    # Each tool is described in the platform contract's own words, because the planner writes
    # both the action and what it expects the verify read to show from these descriptions.
    tier_1_tools = sorted(name for name in TOOL_REGISTRY if tier_of(name) is Tier.TIER_1)
    tier_1_dump = "\n".join(_tool_context_block(name) for name in tier_1_tools)
    read_tools = sorted(name for name in TOOL_REGISTRY if tier_of(name) is Tier.READ)
    read_dump = "\n".join(_tool_context_block(name) for name in read_tools)
    # The alert goes in verbatim: the planner must copy resource names out of real platform
    # values rather than retype them from a summary.
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

    Executes the plan's action under an agent-generated idempotency key: success → VERIFYING, any
    failure → ESCALATED. ``action_timeout_seconds`` overrides the client default for this call.
    """

    def transition_remediate(run_state: RunState, at: datetime) -> RunState:
        # 1. Load the stored plan back into a model, escalating if it is missing or no longer
        #    valid: it crossed a checkpoint as a plain dict and nothing else re-checked it.
        try:
            plan = _load_plan(run_state)
        except ValidationError as err:
            return _escalate_remediation(run_state, at, f"stored plan invalid: {err}")
        if plan is None:
            return _escalate_remediation(
                run_state, at, "REMEDIATING entered with no remediation_plan"
            )

        # 2. Build the idempotency key and serialize the arguments. Entering REMEDIATING again
        #    builds the SAME key, so the platform replays its stored answer instead of re-acting.
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

        # 3. Execute the action. A call that never landed and a call the platform refused both
        #    mean nothing changed, so neither one uses up a remediation attempt.
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

        # 4. Parse the response. A success reply means the action DID run, so an unreadable one
        #    still uses up an attempt, unlike the two failures in step 3.
        try:
            output_summary = _summarize_output(spec.output_model, result.content)
        except (ValueError, ValidationError) as err:
            return _escalate_remediation(
                run_state,
                at,
                f"remediation output parse failed ({plan.action_tool}): {err}",
                attempted_tool=plan.action_tool,
                attempted_arguments=arguments,
                executed=True,
            )

        # 5. Record the executed call on the ledger under its own tool name, charge one tool call
        #    and one remediation attempt, and move the run to VERIFYING.
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

    Three deterministic preconditions in this order: the cap, the budget, somewhere else to go.
    Without the third, reinvestigation could only reach the plan ``_repeated_attempt`` refuses.
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
        # Deliberately not the key ``attempted_tool``: to the grader that key means "a call the
        # platform refused", while this call did execute and is on the ledger under its own name.
        arguments={
            "attempt": run_state.remediation_attempts,
            "of": max_attempts,
            # Which cause this attempt was aimed at, so the resolve gate can tell a cause
            # the run acted on from one it has only named (ADR 0059's rule).
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

    ``hypotheses`` carry over so the planner RE-RANKS given the failure, and the ledger is
    untouched so the second attempt spends from the first's. The spent plan is dropped.
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

    Probe, judge against the plan's expectation, re-probe until ``probe_attempts`` is spent. An
    attempt that did not end the incident reinvestigates or escalates (ADR 0056, ADR 0006).
    """

    def transition_verify(run_state: RunState, at: datetime) -> RunState:
        # 1. Load the stored plan back into a model and serialize the verify call's arguments,
        #    escalating if the plan is missing, no longer valid, or its arguments do not fit.
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

        # 2. State carried across the polls below. `at_attempt` starts as the transition's own
        #    time; the last reading and reasoning end up in the failed-attempt record.
        at_attempt = at
        last_reading = ""
        last_reasoning = ""
        for attempt in range(probe_attempts):
            # 3. Escalate before sleeping if the budget is spent, except on the very first poll:
            #    ADR 0006 allows one verify read over budget, not one per polling attempt.
            if attempt > 0:
                if run_state.budget.is_exhausted:
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

            # 4. Make the verify read and parse it, escalating on a failed call, a refusal or
            #    a response that does not fit the tool's output model.
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

            # 5. Ask the judge whether that reading meets what the plan expected. A judge call
            #    that failed was still billed, so charge it before escalating.
            try:
                judge_call = judge_verification(
                    llm_client,
                    plan=plan,
                    probe_summary=probe_summary,
                    action_summary=_action_result_of(run_state, plan),
                    model=model,
                )
            except (ValueError, ValidationError, LLMError) as err:
                run_state = run_state.model_copy(
                    update={"budget": accrue_llm_error(run_state.budget, err, model)}
                )
                return _escalate_remediation(
                    run_state, at_attempt, f"{VERIFY_JUDGE_INVALID}: {err}"
                )

            # 6. Put the reading and the verdict on the evidence ledger, each tagged with which
            #    poll it was, so a reader can tell poll 2 of 4 from poll 4 of 4.
            judgment = judge_call.result.output
            last_reading = probe_summary
            last_reasoning = judgment.reasoning
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
            # Charged per poll, not once for the whole transition, and through
            # ``accrue_structured_call`` because a re-asked judgment is two billed calls.
            new_budget = accrue_structured_call(run_state.budget, judge_call, model).model_copy(
                update={"tool_calls_used": run_state.budget.tool_calls_used + 1}
            )
            # Evidence and budget build up across the polls, so an escalation at the end
            # carries every reading the run took, not just the last.
            run_state = run_state.model_copy(
                update={
                    "budget": new_budget,
                    "evidence": (*run_state.evidence, probe_entry, judge_entry),
                    "updated_at": at_attempt,
                }
            )
            # 7. Send this verdict to the console immediately, not when polling ends: a step that
            #    polls for minutes showing nothing looks to a watcher like a hung run.
            if planner_log is not None:
                planner_log.verdict(
                    hypotheses=tuple(run_state.hypotheses),
                    verdict=judgment.verdict,
                    reasoning=judgment.reasoning,
                    attempt=attempt + 1,
                    of=probe_attempts,
                )
            if judgment.verdict == "verified":
                # 8. The action worked — but some Tier-1 tools only stabilize. A pause verifies
                #    and still leaves the chain stuck, so it never resolves (ADR 0026's rule).
                try:
                    policy = resolution_class_of(plan.action_tool)
                except PolicyCoverageError as err:
                    # Fail closed: a Tier-1 tool nobody classified is a safety decision
                    # nobody made, so escalate rather than assume it resolved anything.
                    return _escalate_remediation(run_state, at_attempt, str(err))
                if policy.resolution is Resolution.STABILIZES:
                    stabilized = _stabilized_reason(plan, policy.rationale)
                    # A stabilizing action never resolves the incident, but it may go back to
                    # INVESTIGATING once before handing off to a human (ADR 0056's rule).
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
                        # No `executed=True` here: REMEDIATING already charged the action, and
                        # naming it now only puts it on the briefing's `attempted_action`.
                        attempted_tool=plan.action_tool,
                        attempted_arguments=plan.action_arguments,
                    )
                # 9. One question left, about the incident rather than the action: has the alerted
                #    condition cleared, and is any second cause still unaddressed (ADR 0059)?
                condition = _uncleared_alert_condition(plan, run_state) or (
                    _unaddressed_second_cause(plan, run_state)
                )
                if condition is not None:
                    # Same shape as the stabilizer branch above: the action worked and the
                    # incident is not over, so the run may earn one reinvestigation.
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
                        # As in the stabilizer branch: already charged, and naming it stops
                        # the briefing recommending the same action again.
                        attempted_tool=plan.action_tool,
                        attempted_arguments=plan.action_arguments,
                    )
                # 10. Verified, the alerted condition is clear and no cause is left: RESOLVED.
                return run_state.with_state(IncidentState.RESOLVED, at_attempt)

        # 11. Every poll came back ``not_verified``: take one more remediation attempt if one is
        #     left, otherwise hand the incident off (ADR 0056's rule).
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
        # A run on its first attempt escalates with no added reason, exactly as it did before
        # retries existed, so every older canned run still produces identical output.
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

    A function, because WP-6.3's calibration must use the same prompt, renderer, schema and repair
    the run does: a second copy in ``evals/`` is the drift that produced INC-002.
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

    Public since WP-6.3, so the calibration asks in exactly the bytes a run would.
    ``action_summary`` is the action's own response: a delayed replay is unjudgeable without it.
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

    It lands on ``EscalationBriefing.escalation_reason``, so it must carry three things without
    reading as a failure: the action worked, the incident is not over, and which resource is left.
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
# Fixing some of the dead-letter queue does not resolve an alert that named no one row


def _dead_letter_actions() -> frozenset[str]:
    """Every Tier-1 action that acts on dead-letter rows — derived, not declared.

    The union of the three maps that already answer it, ``HINT_ROUTED_TOOLS`` included because
    `mark_dlq_permanent` is inert in the other two. Pinned, so it cannot empty silently.
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

    Read off ADR 0056's attempt records, so the fact travels with the ledger. ``not_verified`` and
    ``verified_unresolved`` are not here: one changed nothing, the other is re-asked per plan.
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

    Name OR category, because ``target_hypothesis`` is a free string the corpus spells both ways.
    One spelling would call an already-remediated cause unaddressed.
    """
    return hypothesis.name in attempted or hypothesis.category.value in attempted


def _standing_causes(run_state: RunState, plan: RemediationPlan) -> tuple[Hypothesis, ...]:
    """Causes this run still ranks at the bar it acts on and has NOT acted on (ADR 0059).

    The run's OWN ranking at the loop's own threshold, so hedging below the bar costs nothing. A
    cause an attempt already targeted counts as addressed however the ranking still scores it.
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

    ``_uncleared_alert_condition``'s question asked of the run's own diagnosis, so it is live
    whatever the alert names. Two ways to be unfinished: an earlier stabilizer, or a standing cause.
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

    The run as it stood when the action was chosen. Slicing there matters: a post-action page
    answers "what did the action do?", not "what was the alert about?". ``None`` is inert.
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

    Narrowed on nothing, and the page IS the queue (``total`` equal to the rows returned). No
    usable ``total`` is accepted: this is a contradiction test, not an invention.
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

    Provable only: the action NAMED the row by id, or narrowed to the slice the listing classified
    it on. Un-provable is unaddressed, and "addressed" never claims the row is FIXED.
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


# What a row the run did not act on still needs, phrased for the human who reads the briefing.
# Keyed on the same `remediation_hint` values ``HINT_ROUTED_TOOLS`` uses, and kept in step with it.
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
    """The escalation reason for a verified action that left the condition standing. Opens with
    ADR 0026's ``STABILIZED, NOT RESOLVED``: a stabilize-only OUTCOME reads the same way."""
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

    ``None`` means RESOLVED is admissible. Inert wherever another guard owns the question — a
    subject-bearing alert (ADR 0032), a non-DLQ action, a whole queue fully addressed.
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

    ``attempted_tool`` records a Tier-1 call that was MADE and refused, which SAFETY grades red;
    ``executed=True`` charges one tool call and one attempt. The reason becomes the briefing's.
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
