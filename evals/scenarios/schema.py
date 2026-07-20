"""Scenario schema. A scenario is a triggering alert plus a scored expectation.

Scenarios drive the eval runner: the runner starts a run from the alert, drives
the state machine to a terminal state, and calls the grader with the scenario's
``expectation``. Canned tool responses let the runner exercise the agent offline
against a fake platform — one response per tool name is enough for Phase 0's
one-probe shape; more elaborate matching lands with multi-probe scenarios.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from evals.graders.deterministic import ScenarioExpectation
from incident_commander.api.schemas import AlertPayload
from incident_commander.tools.mcp_client import ToolResult


class Scenario(BaseModel):
    """One eval scenario. Loaded from YAML, validated at load time."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    description: str = ""
    tags: tuple[str, ...] = ()
    alert: AlertPayload
    expectation: ScenarioExpectation
    canned_tool_responses: dict[str, ToolResult] = Field(default_factory=dict)
    canned_llm_responses: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    # When True the runner ignores ``canned_tool_responses`` and builds a real
    # ``MCPClient`` against ``settings.platform_mcp_url``. Scenarios using this
    # flag are skipped by ``make eval`` when the URL is still the offline
    # placeholder — ``make eval-live`` (or an env with a real URL) runs them.
    use_live_mcp: bool = False
