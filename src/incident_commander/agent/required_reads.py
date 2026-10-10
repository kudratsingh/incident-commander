"""Reads a run must have made before the loop accepts its verdict (ADR 0078).

The alert's breadth sets the burden of proof, and the scenario declares it: a page that names no
single resource lists the reads a verdict owes. ``investigation.py`` enforces exactly that list.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from incident_commander.agent.hypothesis import ReadToolName


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
