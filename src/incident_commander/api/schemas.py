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
    # The DLQ half of "an alert names its subject": the platform's hint vocabulary
    # (`replay_safe`, `wait_and_replay`, `human_required`), typed because
    # `investigation.ALERT_SUBJECT_PROBES` reads it. `None` = no category at all
    # (whole-queue depth, mixed DLQ), and the subject guard is inert then.
    remediation_hint: str | None = None
    # The THIRD way a DLQ alert names its subject, because the other two cannot
    # (ADR 0032): `remediation_hint: null` means "no category", not "the null
    # category" (ADR 0031), and explicit-null is indistinguishable from absent
    # downstream since `app.py` builds the alert with `payload.model_dump()`.
    # `unclassified` is the only value `investigation.ALERT_SUBJECT_PROBES`
    # recognises, and it demands the UNFILTERED listing. Loose (`str | None`, not
    # a `Literal`) because an unrecognised scope word must leave the guard inert,
    # never 422 a real alert (invariant 5, fail open).
    dlq_scope: str | None = None


class IngestResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    incident_id: UUID = Field(description="The run id spawned for this alert.")


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    details: dict[str, Any] = Field(default_factory=dict)
