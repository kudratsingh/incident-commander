"""Typed schemas for platform MCP tools (Wave 1 subset, hand-written for Phase 0).

Shapes match the live platform's Pydantic input/output models as of the pinned
image (see demo/compose.yml for digest). Contract snapshot testing (Phase 3
follow-up PR) will regenerate these from a live ``tools/list`` and diff in CI —
until then, drift is caught by running scenarios against real MCP.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from pydantic import BaseModel, ConfigDict, Field


class GetConsumerLagInput(BaseModel):
    """Input schema for the platform's ``get_consumer_lag`` tool.

    Only ``worker-dispatcher`` is exposed today; more groups arrive with
    later platform waves. Default matches the platform's default so
    zero-arg calls succeed.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)
    consumer_group: str = Field(default="worker-dispatcher", min_length=1)


class GetConsumerLagOutput(BaseModel):
    """Output schema for the platform's ``get_consumer_lag`` tool.

    ``lag`` is nullable — the platform returns ``null`` when the metrics
    loop's Redis cache is empty or expired (typically <60s after startup
    or a Redis restart).
    """

    model_config = ConfigDict(extra="ignore", frozen=True)
    consumer_group: str
    lag: int | None
    cache_key: str


@dataclass(frozen=True)
class ToolSpec:
    """One entry in the tool registry."""

    name: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]


TOOL_REGISTRY: Final[dict[str, ToolSpec]] = {
    "get_consumer_lag": ToolSpec(
        name="get_consumer_lag",
        input_model=GetConsumerLagInput,
        output_model=GetConsumerLagOutput,
    ),
}
