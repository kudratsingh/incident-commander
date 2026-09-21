"""Fakes for exercising the agent offline against scripted platform responses."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from incident_commander.tools.mcp_client import MCPError, ToolResult


class CannedMCPClient:
    """Structural ``MCPClientProtocol`` fake — returns pre-scripted responses.

    A tool maps to one ``ToolResult`` (returned always) or a sequence consumed in
    order, the last one repeating so verify polling and re-probes do not crash.

    It satisfies ``LabProbeCapableClient`` as well, because the runner's premise reads go
    through ``LabProbeClient`` since ADR 0075 and a fake that refused the label would fail
    every offline precondition test on a signature rather than on behaviour.
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
        #: The lab label each call carried, in call order (ADR 0075). ``(None, None)`` for the
        #: agent's own reads. Recorded rather than ignored because the label is the thing under
        #: test wherever this fake stands in for the transport — a fake that swallowed it would
        #: make "the premise reads are the lab's" unprovable offline.
        self.labels: list[tuple[str | None, str | None]] = []

    def call_tool(
        self,
        name: str,
        arguments: Mapping[str, Any],
        *,
        timeout_seconds: float | None = None,
        lab_probe: str | None = None,
        lab_principal_token: str | None = None,
    ) -> ToolResult:
        self.calls.append((name, dict(arguments)))
        self.labels.append((lab_probe, lab_principal_token))
        if name not in self._queues:
            raise MCPError(-32601, f"no canned response for tool: {name}")
        queue = self._queues[name]
        return queue.pop(0) if len(queue) > 1 else queue[0]
