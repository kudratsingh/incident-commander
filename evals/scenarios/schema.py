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


class ChaosHook(BaseModel):
    """Declarative chaos-hook invocation the runner fires before a live run.

    Moves the "which chaos hook seeds this scenario" mapping from operator
    memory (or the sibling ``make chaos-*`` targets) into the scenario file
    itself. Only invoked when the run is live (``use_live_mcp`` is true AND
    ``PLATFORM_MCP_URL`` is a real endpoint); canned runs ignore it entirely
    since the canned tool responses already encode the broken state.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, description="Platform hook name, e.g. `inject_latency`.")
    arguments: dict[str, Any] = Field(default_factory=dict)


class Scenario(BaseModel):
    """One eval scenario. Loaded from YAML, validated at load time."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    description: str = ""
    tags: tuple[str, ...] = ()
    alert: AlertPayload
    expectation: ScenarioExpectation
    # One response per tool (served on every call), or a list consumed in
    # order with the last repeating — for scenarios whose canned platform
    # state changes mid-run (e.g. get_dag_state paused false→true across
    # a pause_dag action; v0.4.9 enforced-pause semantics).
    canned_tool_responses: dict[str, ToolResult | tuple[ToolResult, ...]] = Field(
        default_factory=dict
    )
    canned_llm_responses: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    # When True the runner ignores ``canned_tool_responses`` and builds a real
    # ``MCPClient`` against ``settings.platform_mcp_url``. Scenarios using this
    # flag are skipped by ``make eval`` when the URL is still the offline
    # placeholder — ``make eval-live`` (or an env with a real URL) runs them.
    use_live_mcp: bool = False
    # When True the runner ignores ``canned_llm_responses`` and builds a real
    # ``LLMClient`` against ``settings.anthropic_api_key`` for the planner,
    # briefing writer, and judge. Scenarios using this flag are skipped when
    # the API key is the offline placeholder. Non-deterministic — regression
    # gate does not apply.
    use_live_llm: bool = False
    # Optional chaos hook to fire before the run in live mode. Puts scenario
    # setup in the scenario file instead of in operator memory (see the
    # live-eval noise-source lessons doc, "shared mutable environment").
    chaos_setup: ChaosHook | None = None
