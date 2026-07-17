"""Hypothesis engine models.

The LLM investigation loop outputs an ``InvestigationStep`` per iteration:
ranked ``Hypothesis`` list plus a discriminated-union ``NextAction`` (probe
another tool, or stop and escalate). Schema is authoritative — the ``LLMClient``
forces the model to satisfy it via ``tool_choice=record_output``.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Hypothesis(BaseModel):
    """One candidate root cause with a confidence score and reasoning."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str = Field(min_length=1)


class ProbeAction(BaseModel):
    """Call a tool from the registry to gather more evidence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["probe"] = "probe"
    tool_name: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)


class StopAction(BaseModel):
    """Enough evidence — hand off to a human."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["stop"] = "stop"
    reason: str = Field(min_length=1)


NextAction = Annotated[ProbeAction | StopAction, Field(discriminator="kind")]


class InvestigationStep(BaseModel):
    """One iteration of the investigation loop."""

    model_config = ConfigDict(extra="forbid")

    hypotheses: tuple[Hypothesis, ...] = Field(min_length=1)
    next_action: NextAction
