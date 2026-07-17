"""Scenario schema. A scenario is a triggering alert plus a scored expectation.

Scenarios drive the eval runner (Phase 1b): the runner starts a run from the
alert, drives the state machine to a terminal state, and calls the grader with
the scenario's ``expectation``. Canned tool responses (needed to run the agent
offline against a fake platform) are a runner concern and live outside this
schema so scenario files stay small and readable.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from evals.graders.deterministic import ScenarioExpectation
from incident_commander.api.schemas import AlertPayload


class Scenario(BaseModel):
    """One eval scenario. Loaded from YAML, validated at load time."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    description: str = ""
    tags: tuple[str, ...] = ()
    alert: AlertPayload
    expectation: ScenarioExpectation
