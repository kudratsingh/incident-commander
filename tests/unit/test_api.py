from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from incident_commander.agent.factory import start_run
from incident_commander.agent.state import IncidentState, RunState
from incident_commander.api import app as app_module
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


@pytest.fixture(autouse=True)
def _fresh_replay_cache() -> Iterator[None]:
    """Isolate the module-level webhook replay cache between tests."""
    app_module._replay_cache.clear()
    yield
    app_module._replay_cache.clear()


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

    def _platform_post(
        self,
        client: TestClient,
        alert: dict[str, Any],
        *,
        timestamp: str | None = None,
        secret: str = "hmac-secret",
    ) -> Any:
        """POST shaped exactly like the platform emitter (alerts.py:111-115):
        X-Alert-Signature carries the body-only HMAC, X-Alert-Timestamp is
        epoch milliseconds."""
        body = json.dumps(alert).encode()
        if timestamp is None:
            timestamp = str(int(time.time() * 1000))
        return client.post(
            "/alerts",
            content=body,
            headers={
                "X-Alert-Signature": sign(body, secret),
                "X-Alert-Timestamp": timestamp,
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

    def test_platform_emitter_shaped_request_accepted(
        self, client: TestClient, spawned_runs: list[RunState]
    ) -> None:
        # The platform has always sent X-Alert-Signature (+ X-Alert-Timestamp,
        # epoch ms), never X-Signature-256. This assertion would have caught
        # the header mismatch that 401'd every real delivery.
        response = self._platform_post(
            client,
            {"source": "billing", "severity": "high", "group": "billing-consumer"},
        )
        assert response.status_code == 202
        assert len(spawned_runs) == 1
        assert spawned_runs[0].alert["group"] == "billing-consumer"

    def test_duplicate_delivery_spawns_exactly_one_run(
        self, client: TestClient, spawned_runs: list[RunState]
    ) -> None:
        alert = {"source": "billing", "severity": "high", "group": "billing-consumer"}
        timestamp = str(int(time.time() * 1000))
        first = self._platform_post(client, alert, timestamp=timestamp)
        second = self._platform_post(client, alert, timestamp=timestamp)
        # Replay suppressed but still 202: the platform emitter treats any
        # >=400 as delivery failure, and redelivery is legitimate
        # at-least-once behavior, not an error.
        assert first.status_code == 202
        assert second.status_code == 202
        UUID(second.json()["incident_id"])
        assert len(spawned_runs) == 1

    def test_stale_timestamp_rejected(
        self, client: TestClient, spawned_runs: list[RunState]
    ) -> None:
        stale = str(int((time.time() - 100_000) * 1000))
        platform_shaped = self._platform_post(client, {"source": "billing"}, timestamp=stale)
        assert platform_shaped.status_code == 401
        # The skew window binds to X-Alert-Timestamp whichever header carried
        # the MAC; a stale delivery signed into the legacy header fails too.
        body = json.dumps({"source": "billing"}).encode()
        legacy_shaped = client.post(
            "/alerts",
            content=body,
            headers={
                "X-Signature-256": sign(body, "hmac-secret"),
                "X-Alert-Timestamp": stale,
                "Content-Type": "application/json",
            },
        )
        assert legacy_shaped.status_code == 401
        assert spawned_runs == []

    def test_future_timestamp_rejected(
        self, client: TestClient, spawned_runs: list[RunState]
    ) -> None:
        future = str(int((time.time() + 100_000) * 1000))
        response = self._platform_post(client, {"source": "billing"}, timestamp=future)
        assert response.status_code == 401
        assert spawned_runs == []

    def test_non_numeric_timestamp_rejected(
        self, client: TestClient, spawned_runs: list[RunState]
    ) -> None:
        response = self._platform_post(client, {"source": "billing"}, timestamp="not-a-number")
        assert response.status_code == 401
        assert spawned_runs == []

    def test_legacy_signature_header_still_accepted(
        self, client: TestClient, spawned_runs: list[RunState]
    ) -> None:
        # Back-compat regression guard (green before and after the fix):
        # pre-fix tools sign into X-Signature-256 with no timestamp header.
        response = self._signed_post(
            client,
            {"source": "billing", "severity": "high", "group": "billing-consumer"},
        )
        assert response.status_code == 202
        assert len(spawned_runs) == 1

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


class TestKillSwitch:
    """AGENT_ENABLED=false gates the state machine, never the webhook
    (docs/safety-model.md#kill-switch, docs/runbook.md#kill-switch).

    The assertion that would have caught finding B-03: the documented env
    var must actually stop investigation runs from being spawned.
    """

    def _post_alert(self, client: TestClient, alert: dict[str, Any]) -> Any:
        body = json.dumps(alert).encode()
        return client.post(
            "/alerts",
            content=body,
            headers={
                "X-Alert-Signature": sign(body, "hmac-secret"),
                "X-Alert-Timestamp": str(int(time.time() * 1000)),
                "Content-Type": "application/json",
            },
        )

    def test_disabled_records_triage_run_without_spawning(
        self, spawned_runs: list[RunState], caplog: pytest.LogCaptureFixture
    ) -> None:
        ckpt = InMemoryCheckpointer()

        def capture(run: RunState, _s: Settings, _c: object) -> None:
            spawned_runs.append(run)

        app = create_app(
            settings=_test_settings(agent_enabled=False),
            checkpointer=ckpt,
            run_task=capture,
        )
        with caplog.at_level(logging.WARNING, logger="incident_commander.api.app"):
            response = self._post_alert(
                TestClient(app),
                {"source": "billing", "severity": "high", "group": "billing-consumer"},
            )
        assert response.status_code == 202
        incident_id = UUID(response.json()["incident_id"])
        assert spawned_runs == []
        recorded = ckpt.load(incident_id)
        assert recorded is not None
        assert recorded.state is IncidentState.TRIAGE
        assert recorded.alert["group"] == "billing-consumer"
        assert any("AGENT_ENABLED=false" in r.getMessage() for r in caplog.records)

    def test_disabled_checkpoint_failure_still_acknowledges(
        self, spawned_runs: list[RunState], caplog: pytest.LogCaptureFixture
    ) -> None:
        """Fail-open posture: a disabled agent must never turn webhook
        ingestion into delivery failures, even with the run store down."""

        class _ExplodingCheckpointer(InMemoryCheckpointer):
            def write(self, run_state: RunState) -> None:
                raise RuntimeError("run store unavailable")

        def capture(run: RunState, _s: Settings, _c: object) -> None:
            spawned_runs.append(run)

        app = create_app(
            settings=_test_settings(agent_enabled=False),
            checkpointer=_ExplodingCheckpointer(),
            run_task=capture,
        )
        with caplog.at_level(logging.ERROR, logger="incident_commander.api.app"):
            response = self._post_alert(TestClient(app), {"source": "billing"})
        assert response.status_code == 202
        UUID(response.json()["incident_id"])
        assert spawned_runs == []
        assert any(
            "AGENT_ENABLED=false" in r.getMessage() and r.levelno == logging.ERROR
            for r in caplog.records
        )


def _boom(*_args: Any, **_kwargs: Any) -> RunState:
    raise RuntimeError("boom")


class TestFailureRail:
    """B-04: a crashing background run must still leave a terminal record.

    ``test_api.py`` injects ``run_task=capture`` everywhere else, so the real
    ``_run_investigation`` body was never exercised — which is exactly why the
    missing failure rail survived. Monkeypatching ``run_to_completion`` is the
    honest seam: the Phase-0 transitions wired by ``create_app`` always
    terminate at ESCALATED, so no realistic flow reaches a raising transition.
    """

    def _run(self) -> RunState:
        return start_run(
            {"source": "billing", "severity": "high", "group": "billing-consumer"},
            _test_settings(),
            datetime.now(UTC),
        )

    def _post_alert(self, client: TestClient, alert: dict[str, Any]) -> Any:
        body = json.dumps(alert).encode()
        return client.post(
            "/alerts",
            content=body,
            headers={
                "X-Alert-Signature": sign(body, "hmac-secret"),
                "X-Alert-Timestamp": str(int(time.time() * 1000)),
                "Content-Type": "application/json",
            },
        )

    def test_crash_writes_terminal_failed_checkpoint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The assertion that would have caught B-04: a raising run still
        yields a terminal checkpoint carrying the error as evidence."""
        ckpt = InMemoryCheckpointer()
        run = self._run()
        monkeypatch.setattr(app_module, "run_to_completion", _boom)

        with pytest.raises(RuntimeError, match="boom"):
            app_module._run_investigation(run, _test_settings(), ckpt)

        recorded = ckpt.load(run.incident_id)
        assert recorded is not None
        assert recorded.state is IncidentState.FAILED
        failures = [e for e in recorded.evidence if e.tool_name == "_run_failure"]
        assert len(failures) == 1
        assert "boom" in failures[0].result_summary
        assert failures[0].arguments["error_type"] == "RuntimeError"

    def test_ingress_writes_triage_checkpoint_before_202(
        self, spawned_runs: list[RunState]
    ) -> None:
        """Initial-checkpoint-at-ingress contract: the run row is durable
        before the 202, independent of the background task ever running."""
        ckpt = InMemoryCheckpointer()

        def capture(run: RunState, _s: Settings, _c: object) -> None:
            spawned_runs.append(run)

        app = create_app(settings=_test_settings(), checkpointer=ckpt, run_task=capture)
        response = self._post_alert(
            TestClient(app),
            {"source": "billing", "severity": "high", "group": "billing-consumer"},
        )

        assert response.status_code == 202
        incident_id = UUID(response.json()["incident_id"])
        assert len(spawned_runs) == 1
        recorded = ckpt.load(incident_id)
        assert recorded is not None
        assert recorded.state is IncidentState.TRIAGE
        assert recorded.alert["group"] == "billing-consumer"

    def test_crash_after_terminal_state_appends_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A crash after the outcome was decided must not overwrite it with
        FAILED — the terminal record already tells the true story."""
        ckpt = InMemoryCheckpointer()
        run = self._run()
        ckpt.write(run.with_state(IncidentState.ESCALATED, datetime.now(UTC)))
        monkeypatch.setattr(app_module, "run_to_completion", _boom)

        with pytest.raises(RuntimeError, match="boom"):
            app_module._run_investigation(run, _test_settings(), ckpt)

        history = ckpt.history(run.incident_id)
        assert len(history) == 1
        assert history[-1].state is IncidentState.ESCALATED

    def test_failure_write_error_still_reraises_the_original(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The realistic crash vector IS the run store, so the rail is
        best-effort: it logs and lets the ORIGINAL exception carry the stack."""

        class _ExplodingCheckpointer(InMemoryCheckpointer):
            def write(self, run_state: RunState) -> None:
                raise OSError("run store unavailable")

        run = self._run()
        monkeypatch.setattr(app_module, "run_to_completion", _boom)

        with (
            caplog.at_level(logging.ERROR, logger="incident_commander.api.app"),
            pytest.raises(RuntimeError, match="boom"),
        ):
            app_module._run_investigation(run, _test_settings(), _ExplodingCheckpointer())

        assert any(
            "FAILED" in r.getMessage() and r.levelno == logging.ERROR for r in caplog.records
        )

    def test_terminal_run_logs_a_briefing(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Invariant 7 needs a producer on the service path: render_briefing's
        only caller used to be evals/runner.py."""
        ckpt = InMemoryCheckpointer()
        run = self._run()
        final = run.with_state(IncidentState.ESCALATED, datetime.now(UTC))
        monkeypatch.setattr(app_module, "run_to_completion", lambda *_a, **_k: final)

        with caplog.at_level(logging.INFO, logger="incident_commander.api.app"):
            app_module._run_investigation(run, _test_settings(), ckpt)

        record = next(r for r in caplog.records if "incident terminal" in r.getMessage())
        briefing_json: str = record.__dict__["briefing"]
        assert str(run.incident_id) in briefing_json
        assert IncidentState.ESCALATED.value in briefing_json
