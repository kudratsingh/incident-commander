"""Fakes for exercising the agent offline against scripted platform responses."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from incident_commander.tools.mcp_client import MCPError, ToolResult


class CannedMCPClient:
    """Structural ``MCPClientProtocol`` fake — returns pre-scripted responses."""

    def __init__(self, responses: Mapping[str, ToolResult]) -> None:
        self._responses = dict(responses)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call_tool(self, name: str, arguments: Mapping[str, Any]) -> ToolResult:
        self.calls.append((name, dict(arguments)))
        if name not in self._responses:
            raise MCPError(-32601, f"no canned response for tool: {name}")
        return self._responses[name]
