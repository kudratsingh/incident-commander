"""Reads a run must have made before the loop accepts its verdict (ADR 0078).

The alert's breadth sets the burden of proof, and the scenario declares it: a page that names no
single resource lists the reads a verdict owes. ``investigation.py`` enforces exactly that list.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, model_validator

from incident_commander.agent.hypothesis import ReadToolName
from incident_commander.tools.registry import TOOL_REGISTRY

#: The consumer group that promotes a chain's waiting children once every parent has completed.
#: A stuck chain whose own rows are healthy is stalled HERE, so it is the one resource outside the
#: chain's own reading that a chain verdict must read (ADR 00XX) and a chain action may name.
CHAIN_RESOLVER_GROUP: Final[str] = "dependency-resolver"


class VerdictCondition(StrEnum):
    """Which verdicts a required read applies to."""

    # The verdict's top hypothesis is `no_fault`: "nothing is wrong" must have looked everywhere.
    NO_FAULT = "no_fault"
    # The alert names a dependency chain (a `job_id`): any verdict about it, escalation included.
    STUCK_CHAIN = "stuck_chain"
    # Every verdict, whatever it says.
    ANY = "any"


class RequiredReading(BaseModel):
    """One read the loop refuses a verdict without, and the verdicts it applies to."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool: ReadToolName
    when: VerdictCondition
    # The arguments the read must be MADE WITH, compared as they go on the wire (ADR 00XX). Empty
    # means any call of the tool counts. A read of another resource is not this read: the
    # resolver's lag is not the dispatcher's, though one tool answers both.
    arguments: Mapping[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _arguments_are_ones_the_tool_takes(self) -> RequiredReading:
        """A pinned argument the tool does not accept could never be made, so it is refused here.

        The read would be owed forever and every verdict refused, which reads as an agent fault.
        """
        spec = TOOL_REGISTRY.get(str(self.tool))
        accepted = set(spec.input_model.model_fields) if spec is not None else set()
        unknown = sorted(set(self.arguments) - accepted)
        if unknown:
            raise ValueError(
                f"{self.tool} takes no argument named {unknown}, so a read pinned on it could "
                f"never be made and every verdict would be refused. It takes: {sorted(accepted)}."
            )
        blank = sorted(name for name, value in self.arguments.items() if not value.strip())
        if blank:
            raise ValueError(f"pinned argument(s) {blank} of {self.tool} are blank")
        return self

    def rendered(self) -> str:
        """The read as the planner is told to make it: ``tool`` or ``tool(name=value, …)``."""
        if not self.arguments:
            return str(self.tool)
        pinned = ", ".join(f"{name}={value}" for name, value in sorted(self.arguments.items()))
        return f"{self.tool}({pinned})"

    def made_with(self, arguments: Mapping[str, Any]) -> bool:
        """Whether a call carrying ``arguments`` (wired) names every pinned value exactly."""
        for name, value in self.arguments.items():
            seen = arguments.get(name)
            if not isinstance(seen, str) or seen.strip() != value.strip():
                return False
        return True
