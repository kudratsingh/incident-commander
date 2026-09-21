"""HTTP request/response models. Alert content is untrusted (invariant 4)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class AlertPayload(BaseModel):
    """Loose validation over the platform's alert shape.

    Known fields are typed; unknown ones are preserved (``extra="allow"``) so a platform-side
    addition drops no content. Still untrusted evidence inside the state machine.
    """

    model_config = ConfigDict(extra="allow")

    source: str
    severity: str = "unknown"
    fingerprint: str | None = None
    group: str | None = None
    # The platform's hint vocabulary (`replay_safe`, `wait_and_replay`, `human_required`),
    # read by `investigation.ALERT_SUBJECT_PROBES`. `None` = no category, so the guard is inert.
    remediation_hint: str | None = None
    # The THIRD way a DLQ alert names its subject (ADR 0032), because `remediation_hint: null`
    # means "no category", not "the null category" (ADR 0031). `unclassified` is the only value
    # `ALERT_SUBJECT_PROBES` reads; loose, not a `Literal`, so an unknown word fails open.
    dlq_scope: str | None = None


class IngestResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    incident_id: UUID = Field(description="The run id spawned for this alert.")


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    details: dict[str, Any] = Field(default_factory=dict)
