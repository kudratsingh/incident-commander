"""Scenario schema. A scenario is a triggering alert plus a scored expectation.

Scenarios drive the eval runner: the runner starts a run from the alert, drives
the state machine to a terminal state, and calls the grader with the scenario's
``expectation``. Canned tool responses let the runner exercise the agent offline
against a fake platform — one response per tool name is enough for Phase 0's
one-probe shape; more elaborate matching lands with multi-probe scenarios.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, field_validator

from evals.graders.deterministic import ScenarioExpectation
from incident_commander.api.schemas import AlertPayload
from incident_commander.tools.mcp_client import ToolResult

_SNAPSHOT_PATH: Final = (
    Path(__file__).resolve().parents[2] / "contracts" / "platform-tools.snapshot.json"
)
# Since v0.4.9 the platform stamps every chaos tool's description with its
# blast radius (platform app/mcp/chaos.py:79). Selecting on that prefix is
# the same structural filter tests/unit/test_registry.py uses to exclude
# chaos tools from TOOL_REGISTRY — hand-lists of chaos tools have drifted
# three times in this repo's history, so nothing here is hand-listed.
_CHAOS_PREFIX: Final = "[chaos:"
# Chaos tools the platform registers but the commander deliberately does not
# use. ``seed_dlq_messages`` is deferred, flag-off platform work that stays
# out of this repo entirely — not in TOOL_REGISTRY, not in a ``chaos_setup``,
# not in a scenario. It is absent from the pinned 26-tool snapshot today and
# the post-campaign rebless will add it; excluding it here by construction
# means that rebless cannot silently widen the closed set.
_DEFERRED_CHAOS_TOOLS: Final = frozenset({"seed_dlq_messages"})


def _chaos_names_from_snapshot(payload: object) -> frozenset[str]:
    """Chaos tool names in a parsed ``tools/list`` snapshot, minus deferrals."""
    if not isinstance(payload, dict):
        return frozenset()
    tools = payload.get("tools", [])
    if not isinstance(tools, list):
        return frozenset()
    names = {
        str(tool["name"])
        for tool in tools
        if isinstance(tool, dict)
        and isinstance(tool.get("name"), str)
        and str(tool.get("description", "")).startswith(_CHAOS_PREFIX)
    }
    return frozenset(names - _DEFERRED_CHAOS_TOOLS)


@lru_cache(maxsize=1)
def chaos_tool_names() -> frozenset[str]:
    """The closed set of chaos hooks a scenario may declare.

    Derived from the committed contract snapshot, read lazily and cached:
    ``evals`` is imported at unit-test collection time and this must not
    cost a file read per scenario. The snapshot is always present in a
    checkout; a missing one is a broken checkout, and the resulting
    ``FileNotFoundError`` says so more usefully than a silent empty set.
    """
    return _chaos_names_from_snapshot(json.loads(_SNAPSHOT_PATH.read_text()))


class ChaosHook(BaseModel):
    """Declarative chaos-hook invocation the runner fires before a live run.

    Moves the "which chaos hook seeds this scenario" mapping from operator
    memory (or the sibling ``make chaos-*`` targets) into the scenario file
    itself. Only invoked when the run is live (``use_live_mcp`` is true AND
    ``PLATFORM_MCP_URL`` is a real endpoint); canned runs ignore it entirely
    since the canned tool responses already encode the broken state.

    ``name`` is a CLOSED SET, not a free string. Chaos seeding runs under
    ``settings.platform_token`` — the full write+chaos principal — and
    ``ChaosClient.call`` forwards the name verbatim as a ``tools/call``, so
    an unconstrained name lets a scenario YAML execute any platform tool,
    Tier-1 writes included, under that principal (S-03).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, description="Platform hook name, e.g. `inject_latency`.")
    arguments: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _name_is_a_registered_chaos_tool(cls, value: str) -> str:
        allowed = chaos_tool_names()
        if value not in allowed:
            raise ValueError(
                f"{value!r} is not a chaos tool. chaos_setup runs under the full "
                f"write+chaos principal, so its name is a closed set: "
                f"{', '.join(sorted(allowed))}. To add a hook, land it on the platform, "
                f"bump the pinned digest in demo/compose.yml, and re-bless the snapshot "
                f"with `make snapshot` (docs/runbook.md) — this list is derived from "
                f"contracts/platform-tools.snapshot.json, never hand-edited."
            )
        return value


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
