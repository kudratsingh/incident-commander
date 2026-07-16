"""TRIAGE transition: classify a fresh alert as actionable or noise."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from hashlib import blake2b
from typing import Final

from incident_commander.agent.state import EvidenceEntry, IncidentState, RunState

_NOISE_SEVERITIES: Final[frozenset[str]] = frozenset({"info", "low", "unknown"})


def _severity(alert: Mapping[str, object]) -> str:
    raw = alert.get("severity")
    return raw.lower() if isinstance(raw, str) else "unknown"


def _dedup_key(alert: Mapping[str, object]) -> str:
    """Stable fingerprint over (source, fingerprint). Untrusted input; str-coerced."""
    source = str(alert.get("source", ""))
    fingerprint = str(alert.get("fingerprint", ""))
    return blake2b(f"{source}|{fingerprint}".encode(), digest_size=16).hexdigest()


def transition_triage(run_state: RunState, at: datetime) -> RunState:
    """Route an alert: noise-severity → ESCALATED (human), otherwise → INVESTIGATING."""
    severity = _severity(run_state.alert)
    dedup_key = _dedup_key(run_state.alert)
    next_state = (
        IncidentState.ESCALATED if severity in _NOISE_SEVERITIES else IncidentState.INVESTIGATING
    )
    entry = EvidenceEntry(
        tool_name="_triage",
        arguments={"severity": severity, "dedup_key": dedup_key},
        result_summary=f"severity={severity} classified as {next_state.value}",
        timestamp=at,
    )
    return run_state.model_copy(
        update={
            "state": next_state,
            "updated_at": at,
            "evidence": (*run_state.evidence, entry),
        }
    )
