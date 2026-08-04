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
from collections.abc import Callable
from datetime import datetime
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from incident_commander.agent.hypothesis import ReadToolName
from incident_commander.agent.state import (
    EvidenceEntry,
    IncidentState,
    RunState,
)
from incident_commander.llm.client import LLMClientProtocol, LLMError
from incident_commander.llm.prompts.loader import load_prompt
from incident_commander.tools.mcp_client import MCPClientProtocol, MCPError
from incident_commander.tools.policies import RESOURCE_ARG_FIELDS, Tier, tier_of
from incident_commander.tools.registry import TOOL_REGISTRY
from incident_commander.tools.wire import wire_arguments

# Every Tier-1 tool the platform exposes today. Kept hand-listed for the
# same reason as ``ReadToolName`` in hypothesis.py — Pydantic Literals need
# literal string args at import time. Drift caught by a unit test that
# compares to ``tools_at_or_below(Tier.TIER_1) - tools_at_or_below(Tier.READ)``.
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
        try:
            result = llm_client.call(
                system_prompt=load_prompt("remediation_planner"),
                user_message=_format_plan_context(run_state, top.name),
                output_model=RemediationPlan,
                model=model,
            )
        except (ValueError, ValidationError, LLMError) as err:
            return _escalate_remediation(run_state, at, f"planner LLM invalid: {err}")

        plan = result.output

        if plan.action_tool not in TOOL_REGISTRY:
            return _escalate_remediation(
                run_state, at, f"planner picked unknown action tool: {plan.action_tool}"
            )
        if tier_of(plan.action_tool) is not Tier.TIER_1:
            return _escalate_remediation(
                run_state,
                at,
                f"planner picked non-Tier-1 action: {plan.action_tool} "
                f"(tier={tier_of(plan.action_tool).value})",
            )
        if plan.verify_tool not in TOOL_REGISTRY:
            return _escalate_remediation(
                run_state, at, f"planner picked unknown verify tool: {plan.verify_tool}"
            )
        if tier_of(plan.verify_tool) is not Tier.READ:
            return _escalate_remediation(
                run_state,
                at,
                f"verify tool must be read-only, got tier={tier_of(plan.verify_tool).value}",
            )
        unsourced = _unsourced_resource_args(plan, _evidence_value_corpus(run_state))
        if unsourced:
            # Copy, don't re-type: a resource name the platform never uttered
            # is a hallucination risk, not a plan. The live campaign watched
            # the planner drop `cache:jobs:` off an alert-provided key; only
            # the platform's prefix allowlist stopped the call.
            return _escalate_remediation(
                run_state,
                at,
                "plan rejected before execution: resource argument(s) not "
                f"evidence-sourced: {', '.join(unsourced)}. Resource names must "
                "be copied verbatim from the alert or tool results.",
            )

        tokens = result.input_tokens + result.output_tokens
        new_budget = run_state.budget.model_copy(
            update={"tokens_used": run_state.budget.tokens_used + tokens}
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
                "budget": new_budget,
                # Stored as dict so state.py stays free of remediation-loop imports.
                # REMEDIATING + VERIFYING re-validate via ``_load_plan``.
                "remediation_plan": plan.model_dump(mode="json"),
                "evidence": (*run_state.evidence, entry),
                "updated_at": at,
            }
        )

    return transition_plan


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

    Sources: the alert payload, prior tool-call arguments, and tool result
    summaries that parse as JSON. Bookkeeping entries (prose summaries)
    are skipped deliberately — LLM prose is not a source of truth for
    resource names.
    """
    corpus: set[str] = set()
    _collect_strings(dict(run_state.alert), corpus)
    for entry in run_state.evidence:
        _collect_strings(entry.arguments, corpus)
        try:
            parsed = json.loads(entry.result_summary)
        except (ValueError, TypeError):
            continue
        _collect_strings(parsed, corpus)
    return corpus


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
    evidence_dump = "\n".join(
        f"  - [{e.tool_name}] {e.result_summary[:200]}" for e in run_state.evidence
    )
    tier_1_tools = sorted(name for name in TOOL_REGISTRY if tier_of(name) is Tier.TIER_1)
    tier_1_dump = "\n".join(
        f"  - {name}: input_schema={json.dumps(TOOL_REGISTRY[name].input_model.model_json_schema())}"  # noqa: E501
        for name in tier_1_tools
    )
    read_tools = sorted(name for name in TOOL_REGISTRY if tier_of(name) is Tier.READ)
    read_dump = "\n".join(
        f"  - {name}: input_schema={json.dumps(TOOL_REGISTRY[name].input_model.model_json_schema())}"  # noqa: E501
        for name in read_tools
    )
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
                run_state, at, f"remediation tool error ({plan.action_tool}): {err}"
            )

        if result.is_error:
            return _escalate_remediation(
                run_state, at, f"remediation tool reported is_error=True ({plan.action_tool})"
            )

        try:
            output_summary = _summarize_output(spec.output_model, result.content)
        except (ValueError, ValidationError) as err:
            return _escalate_remediation(
                run_state,
                at,
                f"remediation output parse failed ({plan.action_tool}): {err}",
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

        for attempt in range(probe_attempts):
            if attempt > 0:
                sleep(probe_delay_seconds)

            try:
                result = mcp_client.call_tool(plan.verify_tool, arguments)
            except MCPError as err:
                return _escalate_remediation(
                    run_state, at, f"verify tool error ({plan.verify_tool}): {err}"
                )
            if result.is_error:
                return _escalate_remediation(
                    run_state, at, f"verify tool reported is_error=True ({plan.verify_tool})"
                )

            try:
                probe_summary = _summarize_output(spec.output_model, result.content)
            except (ValueError, ValidationError) as err:
                return _escalate_remediation(
                    run_state, at, f"verify output parse failed ({plan.verify_tool}): {err}"
                )

            try:
                judgment_result = llm_client.call(
                    system_prompt=load_prompt("verification_judge"),
                    user_message=_format_verify_context(plan, probe_summary),
                    output_model=VerificationJudgment,
                    model=model,
                )
            except (ValueError, ValidationError, LLMError) as err:
                return _escalate_remediation(run_state, at, f"verify judge LLM invalid: {err}")

            judgment = judgment_result.output
            probe_entry = EvidenceEntry(
                tool_name=plan.verify_tool,
                arguments=arguments,
                result_summary=probe_summary,
                timestamp=at,
            )
            judge_entry = EvidenceEntry(
                tool_name="_verify_judge",
                arguments={"expectation": plan.verify_expectation},
                result_summary=f"{judgment.verdict}: {judgment.reasoning}",
                timestamp=at,
            )
            new_budget = run_state.budget.model_copy(
                update={
                    "tool_calls_used": run_state.budget.tool_calls_used + 1,
                    "tokens_used": (
                        run_state.budget.tokens_used
                        + judgment_result.input_tokens
                        + judgment_result.output_tokens
                    ),
                }
            )
            # Accumulate evidence + budget across polling attempts so an
            # eventual escalation carries the full probe history.
            run_state = run_state.model_copy(
                update={
                    "budget": new_budget,
                    "evidence": (*run_state.evidence, probe_entry, judge_entry),
                    "updated_at": at,
                }
            )
            if judgment.verdict == "verified":
                return run_state.with_state(IncidentState.RESOLVED, at)

        return run_state.with_state(IncidentState.ESCALATED, at)

    return transition_verify


def _format_verify_context(plan: RemediationPlan, probe_summary: str) -> str:
    return (
        f"Remediation attempted: {plan.action_tool}({json.dumps(plan.action_arguments)})\n"
        f"Target hypothesis: {plan.target_hypothesis}\n\n"
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


def _escalate_remediation(run_state: RunState, at: datetime, reason: str) -> RunState:
    """Escalate from any remediation state with the failure reason on evidence."""
    entry = EvidenceEntry(
        tool_name="_remediation_escalate",
        arguments={"from_state": run_state.state.value, "reason": reason},
        result_summary=reason,
        timestamp=at,
    )
    return run_state.model_copy(
        update={
            "state": IncidentState.ESCALATED,
            "evidence": (*run_state.evidence, entry),
            "updated_at": at,
        }
    )
