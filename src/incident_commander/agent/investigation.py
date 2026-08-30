"""INVESTIGATING transitions.

Two flavors:

- ``make_investigate`` — Phase 0 deterministic: one hard-coded probe
  (``get_consumer_lag``), then escalate. Runner still uses this until the
  scenario library ships canned LLM responses.
- ``make_llm_investigate`` — Phase 2 hypothesis engine: LLM ranks hypotheses,
  picks probes, decides continue-or-stop. Multi-probe capable.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any, Final

from pydantic import BaseModel, ValidationError

from incident_commander.agent.accounting import accrue_llm_error, accrue_llm_usage
from incident_commander.agent.hypothesis import (
    Hypothesis,
    HypothesisCategory,
    InvestigationStep,
    ProbeAction,
    RemediateAction,
    StopAction,
)
from incident_commander.agent.state import (
    EvidenceEntry,
    IncidentState,
    RunState,
)
from incident_commander.llm.client import LLMClientProtocol, LLMError
from incident_commander.llm.prompts.loader import load_prompt
from incident_commander.tools.mcp_client import MCPClientProtocol, MCPError, ToolResult
from incident_commander.tools.policies import Tier, is_cached_read, tier_of, tools_at_or_below
from incident_commander.tools.registry import TOOL_REGISTRY, description_of
from incident_commander.tools.wire import wire_arguments

_TOOL_NAME: Final[str] = "get_consumer_lag"
# The bookkeeping marker for a Phase-1 escalation, distinct from the tool
# that failed. Underscore-prefixed on purpose: that prefix is the repo-wide
# convention for "this evidence entry is a marker, not a probe result", and
# it is what `briefing._terminal_marker` reads a reason from and what the
# trail filter excludes. Recording an escalation under the *tool's* name
# lost the reason at the handoff and put a failed call in the trail as if it
# had returned data (WO-R2-119).
#
# Renaming the marker rather than widening the recognizer is deliberate: the
# success path below also ends ESCALATED, with a real `get_consumer_lag`
# result as its last entry, so a recognizer that accepted non-underscore
# markers would report that probe's JSON as the reason the agent gave up.
_ESCALATION_MARKER: Final[str] = "_investigate_escalate"
_DEFAULT_MAX_ITERATIONS: Final[int] = 5
_REMEDIATE_CONFIDENCE_THRESHOLD: Final[float] = 0.7


# Single source of truth for category → Tier-1 tool routing.
#
# Categories in this map auto-remediate when the top hypothesis crosses
# the confidence threshold. Categories NOT in this map — even at 1.0
# confidence — auto-escalate to a human with a briefing. Adding a new
# category to HypothesisCategory without adding it here is deliberate;
# use that shape for observation-only classifications (e.g. UNKNOWN,
# DEPLOY_REGRESSION) where auto-remediation would be wrong.
#
# Remediation planner selects the *specific* tool (e.g. for POISON_MESSAGE
# it may pick replay_dlq_by_ids, replay_dlq_by_category, or mark_dlq_permanent
# depending on the DLQ's remediation_hint contents). This map only asserts
# "a Tier-1 fix category exists" — not the exact tool.
FIX_MAP: Final[dict[HypothesisCategory, str]] = {
    HypothesisCategory.CONSUMER_SATURATION: "restart_consumer_group",
    HypothesisCategory.POISON_MESSAGE: "replay_dlq_by_ids",
    HypothesisCategory.STALE_CACHE: "invalidate_cache_key",
    HypothesisCategory.RUNAWAY_SAGA: "pause_dag",
}


def make_investigate(
    mcp_client: MCPClientProtocol,
) -> Callable[[RunState, datetime], RunState]:
    """Bind an MCP client to the INVESTIGATING transition function."""

    def transition_investigate(run_state: RunState, at: datetime) -> RunState:
        spec = TOOL_REGISTRY[_TOOL_NAME]
        # Accept legacy `group` field for backward-compat with older alert
        # producers; platform's tool arg is `consumer_group`.
        raw = run_state.alert.get("consumer_group") or run_state.alert.get("group")
        # One canonical serialization for every outgoing call (wire.py's
        # docstring: a second implementation drifts from the thing it
        # guards). This leg is read-only and its default-fill is
        # deliberate — an alert with no group named probes the platform's
        # default group. The remediation legs may NOT default-fill; that
        # asymmetry is the subject of ADR 0022.
        arguments = wire_arguments(spec, {"consumer_group": str(raw)} if raw else {})

        try:
            result = mcp_client.call_tool(_TOOL_NAME, arguments)
        except MCPError as err:
            return _escalate(run_state, at, f"tool error: {err}", arguments)

        if result.is_error:
            return _escalate(run_state, at, "tool reported is_error=True", arguments)

        try:
            output = _parse_output(spec.output_model, result.content)
        except (ValueError, ValidationError) as err:
            return _escalate(run_state, at, f"output parse failed: {err}", arguments)

        entry = EvidenceEntry(
            tool_name=_TOOL_NAME,
            arguments=arguments,
            result_summary=output.model_dump_json(),
            timestamp=at,
        )
        new_budget = run_state.budget.model_copy(
            update={"tool_calls_used": run_state.budget.tool_calls_used + 1}
        )
        return run_state.model_copy(
            update={
                "state": IncidentState.ESCALATED,
                "updated_at": at,
                "evidence": (*run_state.evidence, entry),
                "budget": new_budget,
            }
        )

    return transition_investigate


def _parse_output(model: type[BaseModel], content: list[dict[str, Any]]) -> BaseModel:
    """Parse the first text block as JSON into the tool's output model."""
    for block in content:
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            payload = json.loads(block["text"])
            return model.model_validate(payload)
    raise ValueError("no text content block in tool result")


def _escalate(
    run_state: RunState, at: datetime, reason: str, arguments: dict[str, Any]
) -> RunState:
    entry = EvidenceEntry(
        tool_name=_ESCALATION_MARKER,
        arguments=arguments,
        result_summary=reason,
        timestamp=at,
    )
    return run_state.model_copy(
        update={
            "state": IncidentState.ESCALATED,
            "updated_at": at,
            "evidence": (*run_state.evidence, entry),
        }
    )


# ---------------------------------------------------------------------------
# Phase 2: LLM-driven multi-probe investigation loop.


def make_llm_investigate(
    mcp_client: MCPClientProtocol,
    llm_client: LLMClientProtocol,
    model: str,
    max_iterations: int = _DEFAULT_MAX_ITERATIONS,
    reprobe_attempts: int = 0,
    reprobe_delay_seconds: float = 0.0,
    sleep: Callable[[float], None] = time.sleep,
) -> Callable[[RunState, datetime], RunState]:
    """Bind clients + model to the Phase 2 INVESTIGATING transition.

    Each iteration: the LLM ranks hypotheses and either proposes a probe or
    says stop. Budget is checked before every LLM call and every tool call;
    exhaustion escalates immediately. ``max_iterations`` guards against loops.

    ``reprobe_attempts`` is the investigation-side twin of ADR 0006's verify
    polling (ADR 0009): when a probe of a declared-cached tool (see
    ``policies.CACHED_READ_FRESHNESS_SECONDS``) kills a fixable hypothesis
    that was at or above the remediate threshold, the loop re-reads that
    same probe after ``reprobe_delay_seconds`` before accepting the
    contradiction — at most ``reprobe_attempts`` times per tool per run.
    Default 0 preserves canned behavior byte-identically; the runner wires
    it live, where a cached reading can predate the fault entirely.
    """

    def transition_llm_investigate(run_state: RunState, at: datetime) -> RunState:
        last_probe: ProbeAction | None = None
        reprobes_spent: dict[str, int] = {}
        for _ in range(max_iterations):
            if run_state.budget.is_exhausted:
                return _escalate_investigation(run_state, at, "budget exhausted mid-investigation")

            prior_hypotheses = run_state.hypotheses
            try:
                run_state, step = _plan_next_step(run_state, at, llm_client, model)
            except (ValueError, ValidationError, LLMError) as err:
                # ``_plan_next_step`` accrues on its way out, so a call that
                # raises accrues nothing — and this is the run's hottest LLM
                # call, made once per investigation iteration. Charge what the
                # failed call already billed before escalating (ADR 0015).
                run_state = run_state.model_copy(
                    update={"budget": accrue_llm_error(run_state.budget, err, model)}
                )
                return _escalate_investigation(run_state, at, f"planner output invalid: {err}")

            killed = _cached_probe_contradiction(prior_hypotheses, step.hypotheses, last_probe)
            if (
                killed is not None
                and last_probe is not None
                and reprobes_spent.get(last_probe.tool_name, 0) < reprobe_attempts
            ):
                # Do not act on this step yet — the hypothesis died on the
                # word of a possibly-stale sensor. Re-read it fresh first;
                # the next planner iteration sees both readings and decides.
                reprobes_spent[last_probe.tool_name] = (
                    reprobes_spent.get(last_probe.tool_name, 0) + 1
                )
                run_state = _note_freshness_reprobe(
                    run_state, at, last_probe, killed, reprobe_delay_seconds
                )
                sleep(reprobe_delay_seconds)
                if run_state.budget.is_exhausted:
                    return _escalate_investigation(
                        run_state, at, "budget exhausted before freshness re-probe"
                    )
                run_state = _execute_probe(run_state, at, mcp_client, last_probe)
                if run_state.state is IncidentState.ESCALATED:
                    return run_state
                continue

            action = step.next_action
            if isinstance(action, StopAction):
                return _finalize(run_state, at, action.reason)
            if isinstance(action, RemediateAction):
                # Structural guard: the LLM emitted `remediate`, but we
                # verify the top hypothesis actually qualifies before
                # handing off to PLANNING. Two conditions:
                #   1. category is a key in FIX_MAP (auto-remediable)
                #   2. confidence >= threshold
                # Either condition failing → escalate with a clear reason.
                # The Pydantic schema already prevented invalid *categories*;
                # this catches the "correct category, insufficient confidence"
                # or "prompt drift emitted remediate for an unmapped category"
                # cases at the state-machine level.
                top = step.hypotheses[0]
                if top.category not in FIX_MAP:
                    return _finalize(
                        run_state,
                        at,
                        (
                            f"planner emitted remediate for category "
                            f"{top.category.value!r} which has no Tier-1 fix; "
                            "escalating"
                        ),
                    )
                if top.confidence < _REMEDIATE_CONFIDENCE_THRESHOLD:
                    return _finalize(
                        run_state,
                        at,
                        (
                            f"planner emitted remediate but top confidence "
                            f"{top.confidence:.2f} is below threshold "
                            f"{_REMEDIATE_CONFIDENCE_THRESHOLD}; escalating"
                        ),
                    )
                return _handoff_to_planning(run_state, at, action.reason)

            # ProbeAction — tool_name is Literal-validated at schema time,
            # so this branch only runs on tools that were in the read-tier
            # slice of TOOL_REGISTRY when the module loaded. The extra
            # runtime check catches the (rare) case where the registry
            # drifts after startup.
            if action.tool_name not in TOOL_REGISTRY:
                return _escalate_investigation(
                    run_state, at, f"planner proposed unknown tool: {action.tool_name}"
                )

            if run_state.budget.is_exhausted:
                return _escalate_investigation(run_state, at, "budget exhausted before probe")

            run_state = _execute_probe(run_state, at, mcp_client, action)
            if run_state.state is IncidentState.ESCALATED:
                # Probe failed; already escalated with the reason.
                return run_state
            last_probe = action

        return _escalate_investigation(run_state, at, f"max iterations ({max_iterations}) exceeded")

    return transition_llm_investigate


def _plan_next_step(
    run_state: RunState,
    at: datetime,
    llm_client: LLMClientProtocol,
    model: str,
) -> tuple[RunState, InvestigationStep]:
    """One planner LLM call. Updates budget + hypotheses + updated_at."""
    result = llm_client.call(
        system_prompt=load_prompt("investigation_planner"),
        user_message=_format_planner_context(run_state),
        output_model=InvestigationStep,
        model=model,
    )
    new_budget = accrue_llm_usage(run_state.budget, result, model)
    updated = run_state.model_copy(
        update={
            "budget": new_budget,
            "hypotheses": result.output.hypotheses,
            "updated_at": at,
        }
    )
    return updated, result.output


def _execute_probe(
    run_state: RunState,
    at: datetime,
    mcp_client: MCPClientProtocol,
    action: ProbeAction,
) -> RunState:
    """Call the tool the planner picked. On any failure, escalate with the reason."""
    spec = TOOL_REGISTRY[action.tool_name]
    # Runtime tier guard (B-06): the ReadToolName Literal keeps the schema
    # read-only, but only tier_of() catches a READ→TIER_1 reclassification
    # made in policies.py after the Literal was hand-listed — the live agent
    # token carries actions:execute, so the platform would permit the call.
    # Placed here so BOTH call sites are covered: the main probe branch and
    # the freshness re-probe (ADR 0009). Mirrors make_llm_plan's
    # defense-in-depth tier checks.
    if tier_of(action.tool_name) is not Tier.READ:
        return _escalate_investigation(
            run_state,
            at,
            f"planner proposed non-read tool as probe: {action.tool_name} "
            f"(tier={tier_of(action.tool_name).value})",
        )
    try:
        # Same canonical serialization the remediation legs use: mode="json"
        # so UUID/datetime fields become str/isoformat (httpx.json can't
        # encode raw UUID objects), and no second copy of the rule to drift.
        arguments = wire_arguments(spec, action.arguments)
    except ValidationError as err:
        return _escalate_investigation(
            run_state, at, f"probe arguments invalid for {action.tool_name}: {err}"
        )

    try:
        result = mcp_client.call_tool(action.tool_name, arguments)
    except MCPError as err:
        return _escalate_investigation(run_state, at, f"tool error ({action.tool_name}): {err}")

    if result.is_error:
        return _escalate_investigation(
            run_state, at, f"tool reported is_error=True ({action.tool_name})"
        )

    try:
        summary = _summarize_probe(spec.output_model, result)
    except (ValueError, ValidationError) as err:
        return _escalate_investigation(
            run_state, at, f"output parse failed ({action.tool_name}): {err}"
        )

    entry = EvidenceEntry(
        tool_name=action.tool_name,
        arguments=arguments,
        result_summary=summary,
        timestamp=at,
    )
    new_budget = run_state.budget.model_copy(
        update={"tool_calls_used": run_state.budget.tool_calls_used + 1}
    )
    return run_state.model_copy(
        update={
            "evidence": (*run_state.evidence, entry),
            "budget": new_budget,
            "updated_at": at,
        }
    )


def _summarize_probe(output_model: type[BaseModel], result: ToolResult) -> str:
    """Parse the tool's typed output and return its compact JSON summary."""
    output = _parse_output(output_model, result.content)
    return output.model_dump_json()


def _cached_probe_contradiction(
    prior: tuple[Hypothesis, ...],
    updated: tuple[Hypothesis, ...] | list[Hypothesis],
    last_probe: ProbeAction | None,
) -> Hypothesis | None:
    """Return the fixable high-prior hypothesis a cached read just killed, if any.

    Trigger requires all of:
    - the last executed probe reads from a declared staleness window,
    - the prior top hypothesis was actionable (category in ``FIX_MAP``)
      and at/above the remediate threshold,
    - the fresh planner output dropped that category below the threshold
      (or dropped it entirely).

    Scoped to FIX_MAP categories deliberately: the re-probe exists to
    protect the remediate handoff decision. Unmapped categories escalate
    to a human either way.
    """
    if last_probe is None or not is_cached_read(last_probe.tool_name):
        return None
    if not prior:
        return None
    prior_top = prior[0]
    if prior_top.category not in FIX_MAP:
        return None
    if prior_top.confidence < _REMEDIATE_CONFIDENCE_THRESHOLD:
        return None
    surviving = max(
        (h.confidence for h in updated if h.category == prior_top.category),
        default=0.0,
    )
    if surviving >= _REMEDIATE_CONFIDENCE_THRESHOLD:
        return None
    return prior_top


def _note_freshness_reprobe(
    run_state: RunState,
    at: datetime,
    probe: ProbeAction,
    killed: Hypothesis,
    delay_seconds: float,
) -> RunState:
    """Record why the loop is re-reading a probe instead of acting (ADR 0009)."""
    reason = (
        f"cached read {probe.tool_name} contradicted actionable hypothesis "
        f"{killed.category.value!r} ({killed.confidence:.2f} >= "
        f"{_REMEDIATE_CONFIDENCE_THRESHOLD}); re-probing after {delay_seconds:g}s "
        "before accepting the contradiction"
    )
    entry = EvidenceEntry(
        tool_name="_freshness_reprobe",
        arguments={"tool": probe.tool_name, "delay_seconds": delay_seconds},
        result_summary=reason,
        timestamp=at,
    )
    return run_state.model_copy(
        update={
            "evidence": (*run_state.evidence, entry),
            "updated_at": at,
        }
    )


def _finalize(run_state: RunState, at: datetime, reason: str) -> RunState:
    """Planner said stop. Transition to ESCALATED with the reason in evidence."""
    entry = EvidenceEntry(
        tool_name="_planner_stop",
        arguments={"reason": reason},
        result_summary=f"planner stop: {reason}",
        timestamp=at,
    )
    return run_state.model_copy(
        update={
            "state": IncidentState.ESCALATED,
            "updated_at": at,
            "evidence": (*run_state.evidence, entry),
        }
    )


def _escalate_investigation(run_state: RunState, at: datetime, reason: str) -> RunState:
    """Escalation path for LLM loop failures (budget, invalid output, tool errors)."""
    entry = EvidenceEntry(
        tool_name="_planner_escalate",
        arguments={"reason": reason},
        result_summary=reason,
        timestamp=at,
    )
    return run_state.model_copy(
        update={
            "state": IncidentState.ESCALATED,
            "updated_at": at,
            "evidence": (*run_state.evidence, entry),
        }
    )


def _handoff_to_planning(run_state: RunState, at: datetime, reason: str) -> RunState:
    """Planner said the top hypothesis is remediable. Hand off to PLANNING."""
    entry = EvidenceEntry(
        tool_name="_planner_remediate",
        arguments={"reason": reason},
        result_summary=f"planner handoff to PLANNING: {reason}",
        timestamp=at,
    )
    return run_state.model_copy(
        update={
            "state": IncidentState.PLANNING,
            "updated_at": at,
            "evidence": (*run_state.evidence, entry),
        }
    )


def _format_planner_context(run_state: RunState) -> str:
    remaining_calls = max(run_state.budget.max_tool_calls - run_state.budget.tool_calls_used, 0)
    remaining_tokens = max(run_state.budget.max_tokens - run_state.budget.tokens_used, 0)
    lines = [
        f"Alert: {json.dumps(dict(run_state.alert), sort_keys=True)}",
        f"Budget remaining: tool_calls={remaining_calls}, tokens={remaining_tokens}",
        "",
    ]
    if run_state.evidence:
        lines.append("Evidence so far:")
        for entry in run_state.evidence:
            lines.append(f"  - [{entry.tool_name}] {entry.result_summary}")
    else:
        lines.append("Evidence so far: (none)")
    lines.append("")
    # Investigation planner sees read tools only (Tier.READ). Tier-1 tools
    # are executed by the REMEDIATING transition; the planner emits a
    # RemediateAction to hand off, it does not call them directly.
    lines.append("Available tools (read-only probes):")
    for name in sorted(tools_at_or_below(Tier.READ)):
        spec = TOOL_REGISTRY[name]
        schema = spec.input_model.model_json_schema()
        lines.append(f"  - {name}: {_indented_description(name)}")
        lines.append(f"    input_schema={json.dumps(schema, sort_keys=True)}")
    return "\n".join(lines)


def _indented_description(tool_name: str) -> str:
    """Platform-authored tool description, indented for the context block.

    Verbatim from the contract snapshot (see ``registry.description_of``).
    These are load-bearing: freshness windows, delayed-replay semantics,
    and observable effects live here, and the planner can only reason
    about them if it reads them.
    """
    text = description_of(tool_name)
    if not text:
        return "(no description)"
    return text.replace("\n", "\n    ")
