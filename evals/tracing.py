"""Per-scenario JSONL tracer for eval runs.

Every LLM call and every MCP tool call is captured as one JSON line in
``<EVAL_TRACE_DIR>/<scenario>.jsonl``. Enable by exporting ``EVAL_TRACE_DIR``
before running ``make eval-live`` (``make eval-live`` sets it by default).

Each line has a ``kind`` from ``TraceKind`` below plus an ``invocation_id``
that groups the records written by one runner invocation, and includes the
full request + response payloads for the LLM/MCP variants — enough to
reconstruct the model's reasoning end-to-end.

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
from enum import StrEnum
from pathlib import Path
from typing import Any


class TraceKind(StrEnum):
    """Every ``kind`` a trace record can carry, and who writes it.

    This is the enumeration the human renderer is held to: every member
    must have a step formatter (or be one of the two scenario boundaries
    the header and footer render), enforced by
    ``tests/unit/test_format_traces.py::TestEveryKindRenders``. Before that
    guard, ``llm_error`` and ``precondition`` — two kinds the harness has
    written all along — reached the report as ``STEP N — unknown kind=…``,
    a raw JSON dump of exactly the records a reader is looking for.

    Every write goes through a member rather than a bare string, so a new
    kind cannot be introduced without landing here first — and landing here
    is what makes the renderer's coverage test fail until it can render it.

    ``StrEnum``, so ``json.dumps`` writes the plain string and a record read
    back from JSONL compares equal to the member.
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
    #: Scenario boundaries: the header and footer of one invocation.
    SCENARIO_START = "scenario_start"
    SCENARIO_END = "scenario_end"


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
        """Return a tracer callable to pass to ``LLMClient(tracer=...)``.

        Mirrors ``mcp_hook``'s discrimination: a payload carrying ``error``
        is an ``llm_error``. ``llm_error`` was in this module's documented
        kind set from the start and nothing ever wrote one, so an exhausted
        429 or a dropped connection left a silent gap exactly where billed
        work had happened — the trace showed the call before it and the call
        after it, and nothing in between (invariant 9's concern, one layer
        down: an artifact that omits a failure is a lower bound presented as
        a record).
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
