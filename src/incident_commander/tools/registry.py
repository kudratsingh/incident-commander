"""Typed schemas for platform MCP tools (Wave 1 subset, hand-written for Phase 0).

Later this gets generated from ``contracts/platform-tools.snapshot.json``, but for
Phase 0 the one read tool is hand-coded so the shape is legible in review.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final

from pydantic import BaseModel, ConfigDict, Field


class GetConsumerLagInput(BaseModel):
    """Input schema for the platform's ``get_consumer_lag`` tool."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    group: str = Field(min_length=1)


class GetConsumerLagOutput(BaseModel):
    """Output schema for the platform's ``get_consumer_lag`` tool."""

    model_config = ConfigDict(extra="ignore", frozen=True)
    group: str
    lag: int = Field(ge=0)
    timestamp: datetime


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
