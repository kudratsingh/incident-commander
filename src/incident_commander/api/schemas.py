"""The request and response shapes of the agent's own HTTP surface.

Everything in an inbound alert is content someone else wrote, so it is evidence to reason about and
never an instruction to follow, wherever it ends up inside the run (invariant 4).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class AlertPayload(BaseModel):
    """Deliberately loose validation of the platform's alert shape.

    The fields we act on are typed; anything else is kept rather than dropped (``extra="allow"``),
    so a field the platform adds still reaches the investigation instead of disappearing here.
    """

    model_config = ConfigDict(extra="allow")

    source: str
    severity: str = "unknown"
    fingerprint: str | None = None
    group: str | None = None
    # The platform's verdict on the job the alert is about: `replay_safe`, `wait_and_replay` or
    # `human_required`. `investigation.ALERT_SUBJECT_PROBES` reads it to decide what to look at
    # first; null means the platform had no verdict, and that check then simply does nothing.
    remediation_hint: str | None = None
    # A third way an alert about the dead-letter queue can say what it is about (ADR 0032), needed
    # because a null `remediation_hint` means "no verdict", not "the null category" (ADR 0031).
    # `unclassified` is the only value read, and an unknown word is ignored rather than rejected.
    dlq_scope: str | None = None


class IngestResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    incident_id: UUID = Field(description="The run id spawned for this alert.")


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    details: dict[str, Any] = Field(default_factory=dict)
