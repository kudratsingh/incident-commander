from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from incident_commander.agent.state import RunState
from incident_commander.api.app import create_app
from incident_commander.api.hmac_verify import sign
from incident_commander.config import Settings
from incident_commander.persistence.memory import InMemoryCheckpointer


def _test_settings(**overrides: Any) -> Settings:
    defaults: dict[str, Any] = {
        "anthropic_api_key": SecretStr("sk-ant-test"),
        "judge_model": "claude-haiku-4-5",
        "platform_mcp_url": "https://mcp.local",
        "platform_rest_url": "https://api.local",
        "platform_token": SecretStr("svc-token"),
        "platform_webhook_secret": SecretStr("hmac-secret"),
        "database_url": "postgresql://u:p@localhost:5432/db",
    }
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)  # type: ignore[call-arg]


@pytest.fixture
def spawned_runs() -> list[RunState]:
    return []


@pytest.fixture
def client(spawned_runs: list[RunState]) -> TestClient:
    settings = _test_settings()
    ckpt = InMemoryCheckpointer()

    def capture(run: RunState, _s: Settings, _c: object) -> None:
        spawned_runs.append(run)

    app = create_app(settings=settings, checkpointer=ckpt, run_task=capture)
    return TestClient(app)


class TestHealth:
    def test_ok(self, client: TestClient) -> None:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok", "details": {}}


class TestIngestAlert:
    def _signed_post(
        self,
        client: TestClient,
        alert: dict[str, Any],
        secret: str = "hmac-secret",
    ) -> Any:
        body = json.dumps(alert).encode()
        return client.post(
            "/alerts",
            content=body,
            headers={
                "X-Signature-256": sign(body, secret),
                "Content-Type": "application/json",
            },
        )

    def test_returns_202_and_incident_id(
        self, client: TestClient, spawned_runs: list[RunState]
    ) -> None:
        response = self._signed_post(
            client,
            {"source": "billing", "severity": "high", "group": "billing-consumer"},
        )
        assert response.status_code == 202
        payload = response.json()
        incident_id = UUID(payload["incident_id"])
        assert len(spawned_runs) == 1
        assert spawned_runs[0].incident_id == incident_id
        assert spawned_runs[0].alert["group"] == "billing-consumer"

    def test_rejects_missing_signature(
        self, client: TestClient, spawned_runs: list[RunState]
    ) -> None:
        response = client.post(
            "/alerts",
            json={"source": "billing"},
        )
        assert response.status_code == 401
        assert spawned_runs == []

    def test_rejects_wrong_signature(
        self, client: TestClient, spawned_runs: list[RunState]
    ) -> None:
        response = self._signed_post(client, {"source": "billing"}, secret="wrong-secret")
        assert response.status_code == 401
        assert spawned_runs == []

    def test_rejects_tampered_body(self, client: TestClient, spawned_runs: list[RunState]) -> None:
        original = json.dumps({"source": "billing", "severity": "info"}).encode()
        tampered = json.dumps({"source": "billing", "severity": "critical"}).encode()
        response = client.post(
            "/alerts",
            content=tampered,
            headers={
                "X-Signature-256": sign(original, "hmac-secret"),
                "Content-Type": "application/json",
            },
        )
        assert response.status_code == 401
        assert spawned_runs == []

    def test_rejects_malformed_json(self, client: TestClient, spawned_runs: list[RunState]) -> None:
        body = b"not json at all"
        response = client.post(
            "/alerts",
            content=body,
            headers={
                "X-Signature-256": sign(body, "hmac-secret"),
                "Content-Type": "application/json",
            },
        )
        assert response.status_code == 422
        assert spawned_runs == []

    def test_missing_source_rejected(
        self, client: TestClient, spawned_runs: list[RunState]
    ) -> None:
        response = self._signed_post(client, {"severity": "high"})
        assert response.status_code == 422
        assert spawned_runs == []

    def test_extra_fields_preserved_in_alert(
        self, client: TestClient, spawned_runs: list[RunState]
    ) -> None:
        response = self._signed_post(
            client,
            {
                "source": "billing",
                "severity": "high",
                "trace_id": "abc123",
                "labels": {"team": "payments"},
            },
        )
        assert response.status_code == 202
        run = spawned_runs[0]
        assert run.alert["trace_id"] == "abc123"
        assert run.alert["labels"] == {"team": "payments"}
