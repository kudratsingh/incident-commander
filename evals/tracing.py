"""Per-scenario JSONL tracer for eval runs.

Every LLM call and every MCP tool call is captured as one JSON line in
``<EVAL_TRACE_DIR>/<scenario>.jsonl``. Enable by exporting ``EVAL_TRACE_DIR``
before running ``make eval-live`` (``make eval-live`` sets it by default).

Each line has ``kind`` in {``llm``, ``llm_error``, ``mcp``, ``mcp_error``,
``scenario_start``, ``scenario_end``} plus an ``invocation_id`` that
groups the records written by one runner invocation, and includes the
full request +
response payloads for the LLM/MCP variants — enough to reconstruct the
model's reasoning end-to-end.

Design note: tracer callbacks are plumbed directly through ``LLMClient``
and ``MCPClient`` (no wrapper class) so we capture the raw Anthropic
response before parsing, not just the parsed structured output.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass
class JsonlTracer:
    """Append-only JSONL writer scoped to one scenario run.

    **Never truncates.** Until 2026-08-07 ``__post_init__`` cleared the file
    "so re-runs don't concatenate" — which silently deleted the previous
    attempt's records for that scenario. Run 001's killed first attempt
    (13 scenarios) was erased in full by its own re-run, and the loss only
    surfaced when trace-derived cost came in ~1.9x under the console
    (study/findings.md F-002). Concatenation was never the problem;
    *indistinguishable* concatenation was. Every record now carries
    ``invocation_id`` and ``invocation_started_at``, so attempts stay
    separable while the history stays intact.
    """

    path: Path
    invocation_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    invocation_started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def __post_init__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, record: dict[str, Any]) -> None:
        record.setdefault("timestamp", datetime.now(UTC).isoformat())
        record.setdefault("invocation_id", self.invocation_id)
        record.setdefault("invocation_started_at", self.invocation_started_at)
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
