"""Per-scenario JSONL tracer for eval runs.

Every LLM call and every MCP tool call is captured as one JSON line in
``<EVAL_TRACE_DIR>/<scenario>.jsonl``. Enable by exporting ``EVAL_TRACE_DIR``
before running ``make eval-live`` (``make eval-live`` sets it by default).

Each line has ``kind`` in {``llm``, ``llm_error``, ``mcp``, ``mcp_error``,
``scenario_start``, ``scenario_end``} and includes the full request +
response payloads for the LLM/MCP variants — enough to reconstruct the
model's reasoning end-to-end.

Design note: tracer callbacks are plumbed directly through ``LLMClient``
and ``MCPClient`` (no wrapper class) so we capture the raw Anthropic
response before parsing, not just the parsed structured output.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass
class JsonlTracer:
    """Append-only JSONL writer scoped to one scenario run."""

    path: Path

    def __post_init__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Truncate on scenario start so re-runs don't concatenate.
        self.path.write_text("")

    def write(self, record: dict[str, Any]) -> None:
        record.setdefault("timestamp", datetime.now(UTC).isoformat())
        with self.path.open("a") as f:
            f.write(json.dumps(record, default=str) + "\n")

    def llm_hook(self, role: str) -> Callable[[dict[str, Any]], None]:
        """Return a tracer callable to pass to ``LLMClient(tracer=...)``."""

        def hook(payload: dict[str, Any]) -> None:
            self.write({"kind": "llm", "role": role, **payload})

        return hook

    def mcp_hook(self) -> Callable[[dict[str, Any]], None]:
        """Return a tracer callable to pass to ``MCPClient(tracer=...)``."""

        def hook(payload: dict[str, Any]) -> None:
            kind = "mcp_error" if "error" in payload else "mcp"
            self.write({"kind": kind, **payload})

        return hook


def tracer_for(scenario_name: str, base_dir: Path) -> JsonlTracer:
    """Build a JsonlTracer whose file is named after the scenario."""
    return JsonlTracer(path=base_dir / f"{scenario_name}.jsonl")
