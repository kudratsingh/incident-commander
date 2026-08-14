"""Factories that build a fresh ``RunState``, and the ingress incident identity.

``start_run`` is the state constructor; ``derive_incident_id`` is the ADR 0016
identity rule the webhook ingress opts into. They are deliberately separate —
see ``start_run``.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime
from typing import Final
from uuid import UUID, uuid4, uuid5

from incident_commander.agent.orchestrator import Checkpointer
from incident_commander.agent.state import BudgetLedger, IncidentState, RunState
from incident_commander.agent.triage import dedup_key
from incident_commander.config import Settings

_log = logging.getLogger(__name__)

# Fixed forever (ADR 0016). Regenerating it would re-point every existing
# fingerprint at a different incident id and silently un-dedupe the fleet.
_INCIDENT_NAMESPACE: Final[UUID] = UUID("d0f7dd54-e4fc-49f6-b507-f4becc6886a3")

# Cap on the recurrence walk: a pathological flapping fingerprint degrades to
# a fresh uuid4 rather than looping over an unbounded generation chain.
_MAX_RECURRENCE_GENERATIONS: Final[int] = 64


def derive_incident_id(alert: Mapping[str, object], checkpointer: Checkpointer) -> UUID:
    """Deterministic incident id for an alert carrying ``(source, fingerprint)``.

    ADR 0016. Generation 0 is ``uuid5(namespace, dedup_key(alert))``; a
    generation whose run already reached a terminal state is closed, so the
    walk advances to ``uuid5(namespace, f"{dedup_key}|{n}")`` — a fingerprint
    that fires again after its incident resolved opens a NEW incident, while
    every at-least-once redelivery of the same occurrence still resolves to the
    same id. An absent, empty, or whitespace-only fingerprint declines to
    dedupe at all and returns ``uuid4()``.

    Opt-in at the ingress call site only: ``start_run`` keeps minting ``uuid4``
    by default so ``evals/runner.py`` and every test that builds a ``RunState``
    stay isolated per scenario.
    """
    raw_fingerprint = alert.get("fingerprint")
    if not isinstance(raw_fingerprint, str) or not raw_fingerprint.strip():
        # Keyed on the RAW field, never on the hash: ``dedup_key`` hashes
        # "billing|" into a perfectly stable value, and returning it would
        # collapse every fingerprint-less alert from that source into one
        # immortal incident.
        return uuid4()

    key = dedup_key(alert)
    for generation in range(_MAX_RECURRENCE_GENERATIONS):
        candidate = uuid5(_INCIDENT_NAMESPACE, key if generation == 0 else f"{key}|{generation}")
        latest = checkpointer.load(candidate)
        if latest is None or not latest.state.is_terminal:
            # Absent → fresh incident. Non-terminal → join it: a duplicate
            # delivery, or a crashed run for the single-flight lease to resume.
            return candidate
    _log.warning(
        "recurrence chain for dedup key %s exhausted %d generations; "
        "falling back to a non-deterministic incident id",
        key,
        _MAX_RECURRENCE_GENERATIONS,
    )
    return uuid4()


def start_run(
    alert: Mapping[str, object],
    settings: Settings,
    at: datetime,
    incident_id: UUID | None = None,
    *,
    max_tool_calls: int | None = None,
) -> RunState:
    """Build a fresh TRIAGE-state run with a BudgetLedger seeded from settings.

    The ``uuid4`` default is load-bearing and must stay (ADR 0016): every
    caller other than the webhook ingress — ``evals/runner.py`` above all —
    depends on a distinct incident per invocation. Folding
    ``derive_incident_id`` in here would collapse the canned scenarios that
    share a ``(source, fingerprint)`` pair onto one checkpointer history.

    ``max_tool_calls`` overrides ``settings.budget_max_tool_calls`` for this
    run only (ADR 0019). The eval runner passes the scenario's declared cap,
    so the agent is bounded by — and told about — the budget the scenario
    actually allots it, instead of the fleet default. Ingress does not pass
    it: a production incident is bounded by configuration, not by a caller.

    A ``max_tool_calls`` of 0 is deliberately ignored, and the setting
    default stands. ``BudgetLedger.is_exhausted`` is ``used >= max``, so a
    zero ledger is born exhausted and ``run_to_completion`` escalates before
    TRIAGE ever classifies the alert — the run would end before doing the
    thing such a scenario exists to observe. A cap of 0 means "a correct run
    makes no tool call", which is a claim about the outcome and is graded
    post-hoc by the BUDGET dimension; it is not a runtime ceiling that this
    ledger can express.
    """
    return RunState(
        incident_id=incident_id or uuid4(),
        state=IncidentState.TRIAGE,
        alert=dict(alert),
        budget=BudgetLedger(
            max_tool_calls=max_tool_calls or settings.budget_max_tool_calls,
            max_tokens=settings.budget_max_tokens,
            max_wall_seconds=settings.budget_max_seconds,
            max_usd=settings.budget_max_usd,
        ),
        created_at=at,
        updated_at=at,
    )
