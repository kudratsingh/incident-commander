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
from collections.abc import Callable
from datetime import datetime
from typing import Any, Final

from pydantic import BaseModel, ValidationError

from incident_commander.agent.hypothesis import (
    InvestigationStep,
    ProbeAction,
    StopAction,
)
from incident_commander.agent.state import (
    BudgetLedger,
    EvidenceEntry,
    IncidentState,
    RunState,
)
from incident_commander.llm.client import LLMClientProtocol
from incident_commander.llm.prompts.loader import load_prompt
from incident_commander.tools.mcp_client import MCPClientProtocol, MCPError, ToolResult
from incident_commander.tools.registry import TOOL_REGISTRY

_TOOL_NAME: Final[str] = "get_consumer_lag"
_DEFAULT_MAX_ITERATIONS: Final[int] = 5


def make_investigate(
    mcp_client: MCPClientProtocol,
) -> Callable[[RunState, datetime], RunState]:
    """Bind an MCP client to the INVESTIGATING transition function."""

    def transition_investigate(run_state: RunState, at: datetime) -> RunState:
        spec = TOOL_REGISTRY[_TOOL_NAME]
        group = str(run_state.alert.get("group", "unknown"))
        arguments = spec.input_model.model_validate({"group": group}).model_dump()

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
        tool_name=_TOOL_NAME,
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
) -> Callable[[RunState, datetime], RunState]:
    """Bind clients + model to the Phase 2 INVESTIGATING transition.

    Each iteration: the LLM ranks hypotheses and either proposes a probe or
    says stop. Budget is checked before every LLM call and every tool call;
    exhaustion escalates immediately. ``max_iterations`` guards against loops.
    """

    def transition_llm_investigate(run_state: RunState, at: datetime) -> RunState:
        for _ in range(max_iterations):
            if run_state.budget.is_exhausted:
                return _escalate_investigation(run_state, at, "budget exhausted mid-investigation")

            try:
                run_state, step = _plan_next_step(run_state, at, llm_client, model)
            except (ValueError, ValidationError) as err:
                return _escalate_investigation(run_state, at, f"planner output invalid: {err}")

            action = step.next_action
            if isinstance(action, StopAction):
                return _finalize(run_state, at, action.reason)

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
    new_budget = _add_tokens(run_state.budget, result.input_tokens + result.output_tokens)
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
    try:
        arguments = spec.input_model.model_validate(action.arguments).model_dump()
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


def _add_tokens(budget: BudgetLedger, tokens: int) -> BudgetLedger:
    return budget.model_copy(update={"tokens_used": budget.tokens_used + tokens})


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
    lines.append("Available tools:")
    for name in sorted(TOOL_REGISTRY):
        spec = TOOL_REGISTRY[name]
        schema = spec.input_model.model_json_schema()
        lines.append(f"  - {name}: input_schema={json.dumps(schema, sort_keys=True)}")
    return "\n".join(lines)
