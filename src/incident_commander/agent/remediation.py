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
import time
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any, Final, Literal, NamedTuple

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from incident_commander.agent.accounting import accrue_llm_error, accrue_llm_usage
from incident_commander.agent.hypothesis import ReadToolName
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

# Evidence marker for a refused plan. Underscore-prefixed per the repo-wide
# convention, so the briefing's evidence trail and the grader's "tools
# called" set both exclude it — a refusal is bookkeeping, not a probe, and
# it spends no tool-call budget.
_PLAN_REFUSED_MARKER: Final[str] = "_plan_refused"


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

    Three resource-argument guards run before anything is wired, all
    escalating pre-execution (ADR 0024):

    - ``_absent_resource_args`` — every resource-naming field on both
      legs must be present, or the registry default silently picks the
      resource for us.
    - ``_unsourced_resource_args`` — its value must be one the platform
      itself produced (copy, don't re-type).
    - ``_misdirected_verify_args`` — the verify probe must not name a
      resource the action left alone.

    A fourth guard, ``_unobserved_action_resource``, then asks whether the
    verify leg observes the acted-on resource *at all* (ADR 0025). It
    REFUSES rather than escalates — the plan is re-asked once with the
    required probe named — because unlike the three above, the action it
    chose is right and only its evidence is missing.
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
        while True:
            run_state, outcome = _plan_once(run_state, at, llm_client, model, top.name)
            if isinstance(outcome, RunState):
                return outcome
            plan = outcome

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
) -> tuple[RunState, RemediationPlan | RunState]:
    """One planner call plus every guard that ESCALATES on failure.

    Returns ``(run_state, plan)`` when the plan survives, or
    ``(run_state, escalated_run_state)`` when it does not — the caller
    checks the type. Split out of ``transition_plan`` when the verify-target
    guard made that function a loop: the seven checks below are all terminal,
    so keeping them inline would have meant seven ``return`` statements
    inside a ``while`` whose other exit is a ``break``, which is exactly the
    shape that grows a bug the next time someone adds a check.
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
    unsourced = _unsourced_resource_args(plan, _evidence_value_corpus(run_state))
    if unsourced:
        # Copy, don't re-type: a resource name the platform never uttered
        # is a hallucination risk, not a plan. The live campaign watched
        # the planner drop `cache:jobs:` off an alert-provided key; only
        # the platform's prefix allowlist stopped the call.
        return refuse(
            "plan rejected before execution: resource argument(s) not "
            f"evidence-sourced: {', '.join(unsourced)}. Resource names must "
            "be copied verbatim from the alert or tool results."
        )
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
    refusals = [e for e in run_state.evidence if e.tool_name == _PLAN_REFUSED_MARKER]
    evidence_dump = "\n".join(
        f"  - [{e.tool_name}] {e.result_summary[:200]}"
        for e in run_state.evidence
        if e.tool_name != _PLAN_REFUSED_MARKER
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
