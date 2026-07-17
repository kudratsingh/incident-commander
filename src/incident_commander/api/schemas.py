"""HTTP request/response models. Alert content is untrusted (invariant 4)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class AlertPayload(BaseModel):
    """Loose validation over the platform's alert shape.

    Fields we care about are typed; unknown fields are preserved (``extra="allow"``)
    so a platform-side field addition doesn't drop payload content on the floor.
    Content is still treated as untrusted evidence inside the state machine.
    """

    model_config = ConfigDict(extra="allow")

    source: str
    severity: str = "unknown"
    fingerprint: str | None = None
    group: str | None = None


class IngestResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    incident_id: UUID = Field(description="The run id spawned for this alert.")


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    details: dict[str, Any] = Field(default_factory=dict)
