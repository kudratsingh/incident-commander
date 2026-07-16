from collections.abc import Mapping
from datetime import datetime

import pytest

from incident_commander.agent.state import IncidentState, RunState
from incident_commander.agent.triage import transition_triage


def _with_alert(run_state: RunState, alert: Mapping[str, object]) -> RunState:
    return run_state.model_copy(update={"alert": dict(alert)})


class TestTransitionTriage:
    @pytest.mark.parametrize("severity", ["high", "critical", "major", "medium"])
    def test_actionable_severity_routes_to_investigating(
        self, run_state: RunState, now: datetime, severity: str
    ) -> None:
        alert = {"source": "billing", "fingerprint": "consumer_lag", "severity": severity}
        result = transition_triage(_with_alert(run_state, alert), now)
        assert result.state is IncidentState.INVESTIGATING

    @pytest.mark.parametrize("severity", ["low", "info", "unknown"])
    def test_noise_severity_routes_to_escalated(
        self, run_state: RunState, now: datetime, severity: str
    ) -> None:
        alert = {"source": "billing", "fingerprint": "flap", "severity": severity}
        result = transition_triage(_with_alert(run_state, alert), now)
        assert result.state is IncidentState.ESCALATED

    def test_missing_severity_treated_as_unknown(self, run_state: RunState, now: datetime) -> None:
        alert = {"source": "billing", "fingerprint": "no_sev"}
        result = transition_triage(_with_alert(run_state, alert), now)
        assert result.state is IncidentState.ESCALATED

    def test_non_string_severity_treated_as_unknown(
        self, run_state: RunState, now: datetime
    ) -> None:
        alert: dict[str, object] = {"source": "billing", "severity": 42}
        result = transition_triage(_with_alert(run_state, alert), now)
        assert result.state is IncidentState.ESCALATED

    def test_severity_case_insensitive(self, run_state: RunState, now: datetime) -> None:
        alert = {"source": "billing", "severity": "HIGH"}
        result = transition_triage(_with_alert(run_state, alert), now)
        assert result.state is IncidentState.INVESTIGATING

    def test_evidence_entry_appended(self, run_state: RunState, now: datetime) -> None:
        alert = {"source": "billing", "fingerprint": "lag", "severity": "high"}
        result = transition_triage(_with_alert(run_state, alert), now)
        assert len(result.evidence) == 1
        entry = result.evidence[0]
        assert entry.tool_name == "_triage"
        assert entry.timestamp == now
        assert entry.arguments["severity"] == "high"

    def test_updated_at_advanced(self, run_state: RunState, now: datetime) -> None:
        alert = {"source": "billing", "severity": "high"}
        result = transition_triage(_with_alert(run_state, alert), now)
        assert result.updated_at == now

    def test_original_run_state_unchanged(self, run_state: RunState, now: datetime) -> None:
        alert = {"source": "billing", "severity": "high"}
        _ = transition_triage(_with_alert(run_state, alert), now)
        assert run_state.state is IncidentState.TRIAGE
        assert run_state.evidence == ()

    def test_dedup_key_stable_for_same_source_fingerprint(
        self, run_state: RunState, now: datetime
    ) -> None:
        alert = {"source": "billing", "fingerprint": "lag", "severity": "high"}
        first = transition_triage(_with_alert(run_state, alert), now)
        second = transition_triage(_with_alert(run_state, alert), now)
        assert first.evidence[0].arguments["dedup_key"] == second.evidence[0].arguments["dedup_key"]

    def test_dedup_key_changes_with_fingerprint(self, run_state: RunState, now: datetime) -> None:
        first_alert = {"source": "billing", "fingerprint": "lag", "severity": "high"}
        second_alert = {"source": "billing", "fingerprint": "dlq", "severity": "high"}
        first = transition_triage(_with_alert(run_state, first_alert), now)
        second = transition_triage(_with_alert(run_state, second_alert), now)
        assert first.evidence[0].arguments["dedup_key"] != second.evidence[0].arguments["dedup_key"]
