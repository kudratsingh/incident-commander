from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine
from sqlalchemy.exc import OperationalError

from incident_commander.agent.factory import derive_incident_id, start_run
from incident_commander.agent.state import EvidenceEntry, IncidentState, RunState
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
def checkpointer() -> InMemoryCheckpointer:
    return InMemoryCheckpointer()


@pytest.fixture
def client(spawned_runs: list[RunState], checkpointer: InMemoryCheckpointer) -> TestClient:
    settings = _test_settings()

    def capture(run: RunState, _s: Settings, _c: object) -> None:
        spawned_runs.append(run)

    app = create_app(settings=settings, checkpointer=checkpointer, run_task=capture)
    return TestClient(app)


class _UnreachableStore(InMemoryCheckpointer):
    """A run store that is up as far as the process knows and down in fact."""

    def load(self, incident_id: UUID) -> RunState | None:
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))


class _SlowStore(InMemoryCheckpointer):
    """A run store that answers, eventually — the pool-exhaustion shape."""

    def load(self, incident_id: UUID) -> RunState | None:
        time.sleep(5)
        return None


class TestHealth:
    def test_ok(self, client: TestClient) -> None:
        response = client.get("/health")
        assert response.status_code == 200
        # The probe is reported, not implied: "ok" with an empty details bag
        # was the answer a completely non-functional agent used to give.
        assert response.json() == {"status": "ok", "details": {"datastore": "ok"}}

    def test_reports_degraded_when_the_datastore_is_down(self) -> None:
        # The finding: /health returned ok unconditionally, so an agent that
        # could not read or write a single run reported healthy to whatever
        # watches it — and the lease/pool availability work would be graded
        # against that answer.
        app = create_app(
            settings=_test_settings(),
            checkpointer=_UnreachableStore(),
            run_task=lambda *_: None,
        )
        response = TestClient(app).get("/health")
        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "degraded"
        assert "OperationalError" in body["details"]["datastore"]

    def test_reports_degraded_when_the_probe_does_not_answer_in_time(self) -> None:
        # A store that hangs is the pool-exhaustion shape, and it is the one a
        # health check must not inherit: waiting for it makes the endpoint as
        # unavailable as the thing it is reporting on.
        app = create_app(
            settings=_test_settings(health_probe_timeout_seconds=0.05),
            checkpointer=_SlowStore(),
            run_task=lambda *_: None,
        )
        started = time.monotonic()
        response = TestClient(app).get("/health")
        assert response.status_code == 503
        assert response.json()["status"] == "degraded"
        assert time.monotonic() - started < 4, "the endpoint waited for the hung probe"


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

    def test_absurdly_large_timestamp_is_refused_not_crashed(
        self, client: TestClient, spawned_runs: list[RunState]
    ) -> None:
        # `int` is unbounded, so 10**400 parses fine and then overflowed the
        # float conversion in the skew comparison — an OverflowError outside
        # the `except ValueError`, i.e. an unauthenticated caller getting a
        # 500 out of the ingress. It is a client error like every other
        # unusable timestamp, and 401 is the code the two neighbouring
        # rejections (non-numeric, out-of-window) already use.
        response = self._platform_post(client, {"source": "billing"}, timestamp=str(10**400))
        assert 400 <= response.status_code < 500, "an unusable header must not be a server error"
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


class TestIngressBodyCap:
    """An unauthenticated caller must not be able to make this process buffer.

    ``/alerts`` reads the whole body before the HMAC check can reject anyone —
    it has to, because the signature covers the body — so the only place a cap
    can protect the process is ahead of the route. Both shapes are covered:
    a declared Content-Length over the cap (refused without reading a byte)
    and an undeclared/chunked body (refused the moment the running total
    passes the cap, so lying about the length buys nothing).
    """

    _CAP = 1024

    @pytest.fixture
    def capped_client(self, spawned_runs: list[RunState]) -> TestClient:
        def capture(run: RunState, _s: Settings, _c: object) -> None:
            spawned_runs.append(run)

        app = create_app(
            settings=_test_settings(webhook_max_body_bytes=self._CAP),
            checkpointer=InMemoryCheckpointer(),
            run_task=capture,
        )
        return TestClient(app)

    def test_oversized_unsigned_post_is_refused_and_never_reaches_the_route(
        self, capped_client: TestClient, spawned_runs: list[RunState]
    ) -> None:
        response = capped_client.post(
            "/alerts",
            content=b"x" * (self._CAP * 4),
            headers={"Content-Type": "application/json"},
        )
        # 413 and not 401 is the whole assertion: 401 would mean the body was
        # buffered and handed to the signature check, which is the memory the
        # cap exists to refuse to spend on an unauthenticated caller.
        assert response.status_code == 413
        assert spawned_runs == []

    def test_a_declared_length_over_the_cap_is_refused_before_the_body_arrives(
        self, capped_client: TestClient
    ) -> None:
        # The body sent here is two bytes; only the declaration is oversized.
        # A 413 therefore proves the refusal came from the header alone.
        response = capped_client.post(
            "/alerts",
            content=b"{}",
            headers={"Content-Type": "application/json", "Content-Length": str(self._CAP * 100)},
        )
        assert response.status_code == 413

    def test_an_undeclared_body_is_capped_as_it_streams(
        self, capped_client: TestClient, spawned_runs: list[RunState]
    ) -> None:
        def chunks() -> Iterator[bytes]:
            for _ in range(8):
                yield b"x" * self._CAP

        # A generator body makes httpx use chunked transfer-encoding, so there
        # is no Content-Length to check and the running count is the only
        # thing standing between the caller and the buffer.
        response = capped_client.post(
            "/alerts", content=chunks(), headers={"Content-Type": "application/json"}
        )
        assert response.status_code == 413
        assert spawned_runs == []

    def test_a_body_under_the_cap_is_untouched(
        self, capped_client: TestClient, spawned_runs: list[RunState]
    ) -> None:
        # Green before and after: the cap must not become a second, quieter
        # way for a legitimate delivery to fail.
        body = json.dumps({"source": "billing", "group": "billing-consumer"}).encode()
        assert len(body) < self._CAP
        response = capped_client.post(
            "/alerts",
            content=body,
            headers={
                "X-Alert-Signature": sign(body, "hmac-secret"),
                "X-Alert-Timestamp": str(int(time.time() * 1000)),
                "Content-Type": "application/json",
            },
        )
        assert response.status_code == 202
        assert len(spawned_runs) == 1

    def test_the_cap_does_not_touch_bodyless_routes(self, capped_client: TestClient) -> None:
        assert capped_client.get("/health").status_code == 200


class TestDurableIncidentIdentity:
    """ADR 0016: the incident id is derived from the triage dedup key, so an
    at-least-once redelivery lands on the SAME incident — durably, unlike the
    process-local replay cache (ADR 0014) that only suppresses byte-identical
    redeliveries within the skew window.

    Every test here defeats that cache deliberately (a cleared cache or a body
    that differs outside the identity pair), so a green result can only come
    from derivation. ``len(spawned_runs) == 2`` is the proof: the replay branch
    returns before spawning, so two spawns mean both deliveries ran the full
    ingress path.
    """

    def _post(self, client: TestClient, alert: dict[str, Any]) -> Any:
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

    def test_redelivery_after_replay_cache_loss_keeps_the_incident_id(
        self, client: TestClient, spawned_runs: list[RunState]
    ) -> None:
        alert = {"source": "billing", "severity": "high", "fingerprint": "kafka-lag-spike"}
        first = self._post(client, alert)
        # The cache is process-local and lost on restart; durable dedupe is
        # exactly the guarantee it could not make.
        app_module._replay_cache.clear()
        second = self._post(client, alert)

        assert (first.status_code, second.status_code) == (202, 202)
        assert first.json()["incident_id"] == second.json()["incident_id"]
        assert len(spawned_runs) == 2, "both deliveries must reach the spawn path"
        assert spawned_runs[0].incident_id == spawned_runs[1].incident_id

    def test_dedupe_holds_when_the_body_changes_outside_the_identity_pair(
        self, client: TestClient, spawned_runs: list[RunState]
    ) -> None:
        # Different bodies → different signatures → the replay cache cannot be
        # what makes this pass. Identity keys on (source, fingerprint) only.
        first = self._post(
            client, {"source": "billing", "severity": "high", "fingerprint": "kafka-lag-spike"}
        )
        second = self._post(
            client,
            {
                "source": "billing",
                "severity": "critical",
                "fingerprint": "kafka-lag-spike",
                "annotations": {"redelivery": 2},
            },
        )
        assert first.json()["incident_id"] == second.json()["incident_id"]
        assert len(spawned_runs) == 2

    def test_redelivery_writes_a_single_ingress_checkpoint(
        self, client: TestClient, checkpointer: InMemoryCheckpointer
    ) -> None:
        alert = {"source": "billing", "severity": "high", "fingerprint": "kafka-lag-spike"}
        incident_id = UUID(self._post(client, alert).json()["incident_id"])
        app_module._replay_cache.clear()
        redelivery = self._post(client, alert)
        assert UUID(redelivery.json()["incident_id"]) == incident_id
        assert len(checkpointer.history(incident_id)) == 1

    def test_redelivery_does_not_append_triage_over_an_inflight_run(
        self, client: TestClient, checkpointer: InMemoryCheckpointer
    ) -> None:
        """The corruption case the conditional ingress write exists for.

        ``run_snapshots`` is append-only and ``load`` returns the highest
        version, so an unconditional TRIAGE append on top of an in-flight
        INVESTIGATING run would hand the resume path an evidence-stripped
        state.
        """
        alert = {"source": "billing", "severity": "high", "fingerprint": "kafka-lag-spike"}
        incident_id = UUID(self._post(client, alert).json()["incident_id"])
        inflight = checkpointer.load(incident_id)
        assert inflight is not None
        checkpointer.write(inflight.with_state(IncidentState.INVESTIGATING, datetime.now(UTC)))

        app_module._replay_cache.clear()
        redelivery = self._post(client, alert)

        assert UUID(redelivery.json()["incident_id"]) == incident_id
        latest = checkpointer.load(incident_id)
        assert latest is not None
        assert latest.state is IncidentState.INVESTIGATING
        assert [snap.state for snap in checkpointer.history(incident_id)] == [
            IncidentState.TRIAGE,
            IncidentState.INVESTIGATING,
        ]

    def test_different_fingerprints_open_different_incidents(
        self, client: TestClient, spawned_runs: list[RunState]
    ) -> None:
        first = self._post(client, {"source": "billing", "fingerprint": "kafka-lag-spike"})
        second = self._post(client, {"source": "billing", "fingerprint": "dlq-growth"})
        assert first.json()["incident_id"] != second.json()["incident_id"]
        assert len(spawned_runs) == 2

    def test_fingerprintless_alerts_do_not_collapse_per_source(
        self, client: TestClient, spawned_runs: list[RunState]
    ) -> None:
        # The load-bearing fallback: without it an entire fingerprint-less
        # alert stream from one source would merge into one immortal incident.
        first = self._post(client, {"source": "billing", "severity": "high"})
        second = self._post(client, {"source": "billing", "severity": "critical"})
        assert first.json()["incident_id"] != second.json()["incident_id"]
        assert len(spawned_runs) == 2

    def test_recurrence_after_a_terminal_run_opens_a_new_incident(
        self, client: TestClient, checkpointer: InMemoryCheckpointer
    ) -> None:
        alert = {"source": "billing", "severity": "high", "fingerprint": "kafka-lag-spike"}
        first = UUID(self._post(client, alert).json()["incident_id"])
        resolved = checkpointer.load(first)
        assert resolved is not None
        checkpointer.write(resolved.with_state(IncidentState.RESOLVED, datetime.now(UTC)))

        app_module._replay_cache.clear()
        second = UUID(self._post(client, alert).json()["incident_id"])
        assert second != first
        assert checkpointer.load(second) is not None

        # The recurrence is a deterministic generation, not a fresh uuid4:
        # its own redeliveries must keep landing on it, or dedupe would be
        # lost exactly for the alerts most likely to be retried.
        app_module._replay_cache.clear()
        third = UUID(self._post(client, alert).json()["incident_id"])
        assert third == second


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
        FAILED — the terminal record already tells the true story.

        Two layers hold this now. ADR 0016's resume gate refuses to re-run a
        terminal incident at all, so the crashing body is never reached; and
        ``_record_run_failure``'s own terminal check, exercised directly below,
        still guards a crash that happens after the loop wrote a terminal
        snapshot internally.
        """
        ckpt = InMemoryCheckpointer()
        run = self._run()
        ckpt.write(run.with_state(IncidentState.ESCALATED, datetime.now(UTC)))
        monkeypatch.setattr(app_module, "run_to_completion", _boom)

        app_module._run_investigation(run, _test_settings(), ckpt)
        app_module._record_run_failure(run, ckpt, RuntimeError("boom"), held_lease=True)

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

    def test_logged_briefing_carries_the_reason_and_the_attempted_action(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """R2-38: the service path is where a real on-call reads the handoff.

        The reason lived only on an underscore-prefixed marker that the
        briefing filtered out, so this log line used to describe an
        escalation without saying why — or that a Tier-1 action had already
        fired, which is the half an on-call can act on destructively.
        """
        from incident_commander.agent.briefing import EscalationBriefing

        ckpt = InMemoryCheckpointer()
        run = self._run()
        at = datetime.now(UTC)
        final = run.model_copy(
            update={
                "state": IncidentState.ESCALATED,
                "updated_at": at,
                "evidence": (
                    EvidenceEntry(
                        tool_name="_remediation_escalate",
                        arguments={
                            "from_state": "remediating",
                            "reason": "remediation output parse failed",
                            "attempted_tool": "restart_consumer_group",
                            "attempted_arguments": {"consumer_group": "billing-consumer"},
                        },
                        result_summary="remediation output parse failed (restart_consumer_group)",
                        timestamp=at,
                    ),
                ),
            }
        )
        monkeypatch.setattr(app_module, "run_to_completion", lambda *_a, **_k: final)

        with caplog.at_level(logging.INFO, logger="incident_commander.api.app"):
            app_module._run_investigation(run, _test_settings(), ckpt)

        record = next(r for r in caplog.records if "incident terminal" in r.getMessage())
        briefing = EscalationBriefing.model_validate_json(record.__dict__["briefing"])
        assert briefing.escalation_reason == (
            "remediation output parse failed (restart_consumer_group)"
        )
        assert briefing.attempted_action is not None
        assert briefing.attempted_action.tool == "restart_consumer_group"
        assert briefing.attempted_action.arguments == {"consumer_group": "billing-consumer"}


class TestResumeGate:
    """ADR 0016: inside the lease, the background task loads the latest
    checkpoint and continues from it instead of always re-running from TRIAGE.

    No engine is wired here, so these exercise the resume branch alone; the
    lease itself needs real Postgres (``tests/integration/test_single_flight``).
    """

    def _run(self) -> RunState:
        return start_run(
            {"source": "billing", "severity": "high", "fingerprint": "kafka-lag-spike"},
            _test_settings(),
            datetime.now(UTC),
        )

    def _recorder(
        self, seen: list[RunState]
    ) -> Callable[..., RunState]:  # pragma: no cover - trivial
        def record(state: RunState, **_kwargs: Any) -> RunState:
            seen.append(state)
            return state.with_state(IncidentState.ESCALATED, datetime.now(UTC))

        return record

    def test_no_checkpoint_runs_the_state_it_was_handed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[RunState] = []
        run = self._run()
        monkeypatch.setattr(app_module, "run_to_completion", self._recorder(seen))

        app_module._run_investigation(run, _test_settings(), InMemoryCheckpointer())

        assert [state.incident_id for state in seen] == [run.incident_id]
        assert seen[0].state is IncidentState.TRIAGE

    def test_non_terminal_checkpoint_is_resumed_with_its_evidence(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The crash-resume contract: a run that died mid-investigation
        continues from its checkpoint rather than re-spending the budget to
        rebuild evidence it already has."""
        ckpt = InMemoryCheckpointer()
        run = self._run()
        ckpt.write(run)
        crashed = run.with_state(IncidentState.INVESTIGATING, datetime.now(UTC)).model_copy(
            update={
                "evidence": (
                    EvidenceEntry(
                        tool_name="get_consumer_lag",
                        arguments={"consumer_group": "worker-dispatcher"},
                        result_summary="lag=12000",
                        timestamp=datetime.now(UTC),
                    ),
                )
            }
        )
        ckpt.write(crashed)
        seen: list[RunState] = []
        monkeypatch.setattr(app_module, "run_to_completion", self._recorder(seen))

        app_module._run_investigation(run, _test_settings(), ckpt)

        assert seen[0].state is IncidentState.INVESTIGATING
        assert [entry.tool_name for entry in seen[0].evidence] == ["get_consumer_lag"]

    def test_terminal_checkpoint_is_not_resumed(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        ckpt = InMemoryCheckpointer()
        run = self._run()
        ckpt.write(run.with_state(IncidentState.RESOLVED, datetime.now(UTC)))
        monkeypatch.setattr(app_module, "run_to_completion", _boom)

        with caplog.at_level(logging.INFO, logger="incident_commander.api.app"):
            app_module._run_investigation(run, _test_settings(), ckpt)

        assert len(ckpt.history(run.incident_id)) == 1
        assert any("terminal" in r.getMessage() for r in caplog.records)

    def test_awaiting_approval_is_not_resumed(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Tier-2 resume is scoped out (ADR 0016): AWAITING_APPROVAL's
        transition is still a stub, so resuming into it would raise
        NotImplementedError and the failure rail would turn a merely-waiting
        incident into a FAILED one."""
        ckpt = InMemoryCheckpointer()
        run = self._run()
        ckpt.write(run.with_state(IncidentState.AWAITING_APPROVAL, datetime.now(UTC)))
        monkeypatch.setattr(app_module, "run_to_completion", _boom)

        with caplog.at_level(logging.INFO, logger="incident_commander.api.app"):
            app_module._run_investigation(run, _test_settings(), ckpt)

        latest = ckpt.load(run.incident_id)
        assert latest is not None
        assert latest.state is IncidentState.AWAITING_APPROVAL
        assert any("awaiting_approval" in r.getMessage() for r in caplog.records)


def _pool_exhausted(_engine: object, _incident_id: UUID) -> Iterator[bool]:
    """A lease attempt that dies before it can answer — the realistic loser.

    ``incident_lease`` checks a connection out of the pool and runs
    ``pg_try_advisory_lock`` on it. Under the ADR 0022 bound, a pool with no
    free connection (or a Postgres blip) raises here, and the process learns
    nothing about who owns the incident — least of all that it does.
    """
    raise OSError("connection pool exhausted")


@contextmanager
def _granted_lease(_engine: object, _incident_id: UUID) -> Iterator[bool]:
    yield True


def _escalates(state: RunState, **kwargs: Any) -> RunState:
    """A run that terminates, checkpointing its outcome as the real loop does."""
    final = state.with_state(IncidentState.ESCALATED, datetime.now(UTC))
    checkpointer = cast("InMemoryCheckpointer", kwargs["checkpointer"])
    checkpointer.write(final)
    return final


class TestCrashRailIsLeaseAware:
    """R2-40: only the lease HOLDER may write the terminal FAILED record.

    The rail wraps admission as well as the run, so any exception on the way
    to the lease — pool exhaustion above all — lands in the same ``except``.
    Without a holder check it stamps FAILED on an incident this process never
    owned, and per ADR 0016 that record is non-resumable: the worker that does
    hold the lease has its run killed off from the outside, and because a
    closed generation-0 makes ``derive_incident_id`` walk on, the next
    redelivery forks a second investigation of one fault.
    """

    def _alert(self) -> dict[str, Any]:
        return {"source": "billing", "severity": "high", "fingerprint": "kafka-lag-spike"}

    def _contested(self) -> tuple[InMemoryCheckpointer, RunState]:
        """One incident, mid-investigation, owned by a worker that is not us."""
        ckpt = InMemoryCheckpointer()
        alert = self._alert()
        run = start_run(alert, _test_settings(), datetime.now(UTC), derive_incident_id(alert, ckpt))
        ckpt.write(run.with_state(IncidentState.INVESTIGATING, datetime.now(UTC)))
        return ckpt, run

    def test_a_worker_that_never_held_the_lease_records_no_failure(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        ckpt, run = self._contested()
        before = [state.state for state in ckpt.history(run.incident_id)]
        monkeypatch.setattr(app_module, "incident_lease", _pool_exhausted)

        with (
            caplog.at_level(logging.WARNING, logger="incident_commander.api.app"),
            pytest.raises(OSError, match="connection pool exhausted"),
        ):
            app_module._run_investigation(
                run, _test_settings(), ckpt, engine=cast("Engine", object())
            )

        after = ckpt.history(run.incident_id)
        assert [state.state for state in after] == before
        assert not any(state.state is IncidentState.FAILED for state in after)
        assert not any(
            entry.tool_name == "_run_failure" for state in after for entry in state.evidence
        )
        assert any("lease" in record.getMessage() for record in caplog.records)

    def test_the_holder_finishes_and_no_second_incident_is_derived(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The consequence the rail's spurious FAILED buys: a forked incident."""
        ckpt, run = self._contested()
        monkeypatch.setattr(app_module, "incident_lease", _pool_exhausted)

        with pytest.raises(OSError):
            app_module._run_investigation(
                run, _test_settings(), ckpt, engine=cast("Engine", object())
            )

        # Generation 0 is still open, so the next redelivery of this alert
        # joins the live run instead of forking a second investigation of one
        # fault. A spurious FAILED closes it and this returns a new uuid.
        assert derive_incident_id(self._alert(), ckpt) == run.incident_id

        # And the holder, resuming under its own lease, still completes.
        monkeypatch.setattr(app_module, "incident_lease", _granted_lease)
        monkeypatch.setattr(app_module, "run_to_completion", _escalates)
        app_module._run_investigation(run, _test_settings(), ckpt, engine=cast("Engine", object()))

        latest = ckpt.load(run.incident_id)
        assert latest is not None
        assert latest.state is IncidentState.ESCALATED

    def test_the_holder_still_records_its_own_crash(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The B-04 rail itself must survive the holder check: a crash under a
        HELD lease is exactly the case FAILED exists to record."""
        ckpt, run = self._contested()
        monkeypatch.setattr(app_module, "incident_lease", _granted_lease)
        monkeypatch.setattr(app_module, "run_to_completion", _boom)

        with pytest.raises(RuntimeError, match="boom"):
            app_module._run_investigation(
                run, _test_settings(), ckpt, engine=cast("Engine", object())
            )

        latest = ckpt.load(run.incident_id)
        assert latest is not None
        assert latest.state is IncidentState.FAILED
