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
    # The THIRD way a DLQ alert names its subject, and it exists because the
    # other two cannot express it (ADR 0032).
    #
    # A dead-letter row the platform's classifier has not touched carries
    # `remediation_hint: null`, and "the rows nothing has classified" is a real
    # incident — it is the whole of live run `a0aa257bf865`'s fault. But it is
    # not sayable in `remediation_hint` above, for two independent reasons:
    #
    #   1. **`null` there means "no category", not "the null category".** That
    #      is ADR 0031's accepted reading and it is the right one: the field's
    #      vocabulary is the platform's three hint values, and a whole-queue
    #      depth alert legitimately carries `None`. Overloading `None` to also
    #      mean "and this time I mean it" would make every hintless DLQ alert
    #      in the corpus — `dlq_backlog`, `dlq_mixed_partial` — claim a subject
    #      it does not have.
    #   2. **Explicit-null and absent are indistinguishable downstream, by
    #      construction.** `app.py` builds the run's alert with
    #      `payload.model_dump()`, which materializes every declared field, so
    #      a wire payload that omitted `remediation_hint` and one that sent
    #      `null` both reach `RunState.alert` as present-with-`None`. The two
    #      live archives prove both halves: `remediation_hint` is ABSENT in
    #      `adcdcadd94a3` (2026-08-31, before ADR 0031) and PRESENT-WITH-NULL
    #      in `a0aa257bf865` (2026-09-08), and `dlq_human_required_escalates`'s
    #      own YAML says of its explicit null: "The two are identical to the
    #      guard." Any check keyed on key-presence would therefore be inert
    #      offline for scenarios that omit the key and non-inert for EVERY
    #      production alert of any kind, which is the asymmetry
    #      `alert_subject`'s docstring calls the worst shape of guard.
    #
    # So the positive statement gets its own field. `unclassified` is its only
    # value today and `investigation.ALERT_SUBJECT_PROBES` recognises no other
    # — an unknown scope word leaves the subject guard inert rather than
    # asserting a subject nobody declared a probe for. The probe it does demand
    # is the UNFILTERED listing, because `ListDlqMessagesInput.remediation_hint
    # = null` means "every category" and the whole-queue page is the only read
    # that shows a null-hint row.
    #
    # Typed and loose (`str | None`, not a `Literal`) for the same reason every
    # field here is: an alert is untrusted input on the paging path (invariant
    # 5, fail open). A scope word this commander does not recognise must not
    # 422 a real alert; it must page a human with the alert investigated the
    # way it is today.
    dlq_scope: str | None = None


class IngestResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    incident_id: UUID = Field(description="The run id spawned for this alert.")


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    details: dict[str, Any] = Field(default_factory=dict)
