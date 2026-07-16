"""INVESTIGATING transition: probe the platform, escalate with findings.

Phase 0 shape: one hard-coded probe (``get_consumer_lag``), then escalate to a
human. The multi-probe hypothesis-ranking loop is Phase 2.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from typing import Any, Final

from pydantic import BaseModel, ValidationError

from incident_commander.agent.state import EvidenceEntry, IncidentState, RunState
from incident_commander.tools.mcp_client import MCPClientProtocol, MCPError
from incident_commander.tools.registry import TOOL_REGISTRY

_TOOL_NAME: Final[str] = "get_consumer_lag"


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
