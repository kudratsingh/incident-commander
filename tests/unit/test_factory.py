from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from pydantic import SecretStr

from incident_commander.agent.factory import start_run
from incident_commander.agent.state import IncidentState
from incident_commander.config import Settings


def _test_settings(**overrides: Any) -> Settings:
    defaults: dict[str, Any] = {
        "anthropic_api_key": SecretStr("sk-ant-test"),
        "judge_model": "claude-haiku-4-5",
        "platform_mcp_url": "https://mcp.local",
        "platform_rest_url": "https://api.local",
        "platform_token": SecretStr("svc"),
        "platform_webhook_secret": SecretStr("hmac"),
        "database_url": "postgresql://u:p@localhost:5432/db",
    }
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)  # type: ignore[call-arg]


class TestStartRun:
    def test_produces_triage_state(self, now: datetime) -> None:
        run = start_run({"source": "billing"}, _test_settings(), now)
        assert run.state is IncidentState.TRIAGE

    def test_budget_seeded_from_settings(self, now: datetime) -> None:
        settings = _test_settings(
            budget_max_tool_calls=7,
            budget_max_tokens=999,
            budget_max_seconds=60,
            budget_max_usd=Decimal("1.23"),
        )
        run = start_run({"source": "billing"}, settings, now)
        assert run.budget.max_tool_calls == 7
        assert run.budget.max_tokens == 999
        assert run.budget.max_wall_seconds == 60
        assert run.budget.max_usd == Decimal("1.23")

    def test_alert_copied_not_aliased(self, now: datetime) -> None:
        alert: dict[str, object] = {"source": "billing", "severity": "high"}
        run = start_run(alert, _test_settings(), now)
        alert["severity"] = "mutated-after"
        assert run.alert["severity"] == "high"

    def test_created_and_updated_at_match(self, now: datetime) -> None:
        run = start_run({"source": "billing"}, _test_settings(), now)
        assert run.created_at == now
        assert run.updated_at == now

    def test_incident_id_auto_generated_when_omitted(self, now: datetime) -> None:
        run = start_run({"source": "billing"}, _test_settings(), now)
        assert isinstance(run.incident_id, UUID)

    def test_incident_id_respected_when_provided(self, now: datetime) -> None:
        given = uuid4()
        run = start_run({"source": "billing"}, _test_settings(), now, incident_id=given)
        assert run.incident_id == given

    def test_evidence_starts_empty(self, now: datetime) -> None:
        run = start_run({"source": "billing"}, _test_settings(), now)
        assert run.evidence == ()

    def test_no_pending_approval(self, now: datetime) -> None:
        run = start_run({"source": "billing"}, _test_settings(), now)
        assert run.pending_approval_id is None
