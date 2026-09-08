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
    # The DLQ half of "an alert names its subject" (`group` is the consumer
    # half). A dead-letter alert that fires on ONE remediation category names
    # that category here, and the value is the platform's own DLQ vocabulary —
    # `replay_safe`, `wait_and_replay`, `human_required`, the same strings
    # `list_dlq_messages.remediation_hint` filters on and
    # `replay_dlq_by_category.category` acts on. Typed rather than left to
    # `extra="allow"` because it is load-bearing: it is the field
    # `investigation.ALERT_SUBJECT_PROBES` reads to derive the scoped listing
    # a category-scoped DLQ incident must be investigated through.
    #
    # `None` is the correct value for a whole-queue depth alert, and the
    # subject guard is inert then — see `alert_subject`. A mixed DLQ is not a
    # category, so it leaves this unset rather than picking one.
    remediation_hint: str | None = None


class IngestResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    incident_id: UUID = Field(description="The run id spawned for this alert.")


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    details: dict[str, Any] = Field(default_factory=dict)
