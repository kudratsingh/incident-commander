"""Per-scenario JSONL tracer for eval runs.

Every LLM and MCP call becomes one line in ``<EVAL_TRACE_DIR>/<scenario>.jsonl``, carrying a
``TraceKind``, an ``invocation_id`` and the full request and response — the raw Anthropic
response, captured before parsing.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any


class TraceKind(StrEnum):
    """Every ``kind`` a trace record can carry, and who writes it.

    Every member needs a step formatter or is a scenario boundary, enforced by
    ``tests/unit/test_format_traces.py::TestEveryKindRenders``; a new kind has to
    land here first. ``StrEnum``, so a record read back from JSONL compares equal.
    """

    #: One completed LLM call, request + raw response (``LLMClient``).
    LLM = "llm"
    #: One LLM call that was billed (or attempted) and did not return —
    #: an exhausted 429, a dropped connection (``LLMClient._trace_error``).
    LLM_ERROR = "llm_error"
    #: One completed MCP tool call, arguments + result (``MCPClient``).
    MCP = "mcp"
    #: One MCP tool call that raised instead of returning (``MCPClient``).
    MCP_ERROR = "mcp_error"
    #: The world-state check that decides whether the scenario's premise
    #: was ever manufactured (``runner._assert_preconditions``).
    PRECONDITION = "precondition"
    #: The chaos hook a live scenario fires to seed its fault (``runner``).
    CHAOS_SETUP = "chaos_setup"
    #: One planner step as ``agent.strategies.records.StepRecord`` wrote it: candidates, step,
    #: rankings either side, bill (plan 02 § 7). Evaluator-side, never read back into a run.
    STEP = "step"
    #: Scenario boundaries: the header and footer of one invocation.
    SCENARIO_START = "scenario_start"
    SCENARIO_END = "scenario_end"


@dataclass
class JsonlTracer:
    """Append-only JSONL writer scoped to one scenario run.

    **Never truncates.** Clearing the file on construction erased Run 001's killed
    first attempt in full (F-002). Every record carries ``invocation_id`` and
    ``invocation_started_at``, so attempts stay separable and history stays intact.
    """

    path: Path
    invocation_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    invocation_started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def __post_init__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, record: dict[str, Any]) -> None:
        # ``record_id`` identifies ONE record inside an invocation: a repaired call writes two,
        # the second naming the first as ``repair_of`` (ADR 0035). ``LLMClient`` mints its own.
        record.setdefault("record_id", uuid.uuid4().hex[:12])
        record.setdefault("timestamp", datetime.now(UTC).isoformat())
        record.setdefault("invocation_id", self.invocation_id)
        record.setdefault("invocation_started_at", self.invocation_started_at)
        with self.path.open("a") as f:
            f.write(json.dumps(record, default=str) + "\n")

    def llm_hook(self, role: str) -> Callable[[dict[str, Any]], None]:
        """Return a tracer callable to pass to ``LLMClient(tracer=...)``.

        A payload carrying ``error`` becomes an ``llm_error``; without that an
        exhausted 429 left a silent gap where billed work happened (invariant 9).
        """

        def hook(payload: dict[str, Any]) -> None:
            kind = TraceKind.LLM_ERROR if "error" in payload else TraceKind.LLM
            self.write({"kind": kind, "role": role, **payload})

        return hook

    def mcp_hook(self) -> Callable[[dict[str, Any]], None]:
        """Return a tracer callable to pass to ``MCPClient(tracer=...)``."""

        def hook(payload: dict[str, Any]) -> None:
            kind = TraceKind.MCP_ERROR if "error" in payload else TraceKind.MCP
            self.write({"kind": kind, **payload})

        return hook


def tracer_for(scenario_name: str, base_dir: Path) -> JsonlTracer:
    """Build a JsonlTracer whose file is named after the scenario."""
    return JsonlTracer(path=base_dir / f"{scenario_name}.jsonl")
