"""Fakes for exercising the agent offline against scripted platform responses."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from incident_commander.tools.mcp_client import MCPError, ToolResult


class CannedMCPClient:
    """Structural ``MCPClientProtocol`` fake — returns pre-scripted responses.

    A tool maps to either one ``ToolResult`` (returned on every call — the
    Phase 0 shape) or a sequence of them, consumed in order with the last
    one repeating once exhausted. Sequences exist for scenarios whose
    canned data must change across the run — e.g. ``get_dag_state`` reads
    ``paused=false`` during investigation and ``paused=true`` on the
    post-``pause_dag`` verify probe (v0.4.9 enforced-pause semantics).
    The last-one-repeats rule keeps extra probes (verify polling, freshness
    re-probes) from crashing a canned run.
    """

    def __init__(self, responses: Mapping[str, ToolResult | Sequence[ToolResult]]) -> None:
        self._queues: dict[str, list[ToolResult]] = {}
        for name, value in responses.items():
            if isinstance(value, ToolResult):
                self._queues[name] = [value]
            else:
                queue = list(value)
                if not queue:
                    raise ValueError(f"empty canned response sequence for tool: {name}")
                self._queues[name] = queue
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call_tool(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        timeout_seconds: float | None = None,
    ) -> ToolResult:
        self.calls.append((name, dict(arguments)))
        if name not in self._queues:
            raise MCPError(-32601, f"no canned response for tool: {name}")
        queue = self._queues[name]
        return queue.pop(0) if len(queue) > 1 else queue[0]
