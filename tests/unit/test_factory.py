import logging
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4, uuid5

import pytest
from pydantic import SecretStr

from incident_commander.agent.factory import (
    _INCIDENT_NAMESPACE,
    _MAX_RECURRENCE_GENERATIONS,
    derive_incident_id,
    start_run,
)
from incident_commander.agent.state import IncidentState, RunState
from incident_commander.agent.triage import dedup_key
from incident_commander.config import Settings
from incident_commander.persistence.memory import InMemoryCheckpointer


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


class TestStartRunStaysNonDeterministic:
    """Regression guard for the eval runner's per-scenario isolation.

    ``evals/runner.py`` calls ``start_run(scenario.alert.model_dump(), ...)``
    with no ``incident_id`` and depends on a fresh ``uuid4`` per scenario. If
    derivation were ever moved into ``start_run`` (ADR 0016 forbids it), every
    canned scenario sharing a ``(source, fingerprint)`` pair would collapse
    onto one incident id, merging their checkpointer histories and corrupting
    trajectories and graders.
    """

    def test_repeated_identical_alerts_get_fresh_ids(self, now: datetime) -> None:
        alert = {"source": "billing", "fingerprint": "kafka-lag-spike", "severity": "high"}
        first = start_run(alert, _test_settings(), now)
        second = start_run(alert, _test_settings(), now)
        assert first.incident_id != second.incident_id

    def test_derivation_is_not_reachable_without_a_checkpointer(self, now: datetime) -> None:
        # ``derive_incident_id`` needs a checkpointer to walk the recurrence
        # chain, so it cannot be wired into ``start_run``'s default without
        # changing that signature — this pins the shape, not just the values.
        alert = {"source": "billing", "fingerprint": "kafka-lag-spike"}
        run = start_run(alert, _test_settings(), now)
        assert run.incident_id != uuid5(_INCIDENT_NAMESPACE, dedup_key(alert))


def _terminal_snapshot(incident_id: UUID, at: datetime) -> RunState:
    return start_run(
        {"source": "billing", "fingerprint": "kafka-lag-spike"},
        _test_settings(),
        at,
        incident_id=incident_id,
    ).with_state(IncidentState.RESOLVED, at)


class TestDeriveIncidentId:
    """ADR 0016: uuid5 over the triage dedup key, with a generation chain
    walking past terminal recurrences and a uuid4 fallback."""

    def test_same_source_and_fingerprint_derive_the_same_id(self) -> None:
        ckpt = InMemoryCheckpointer()
        alert = {"source": "billing", "fingerprint": "kafka-lag-spike", "severity": "high"}
        # A redelivery differs in fields outside the identity pair.
        redelivery = {"source": "billing", "fingerprint": "kafka-lag-spike", "severity": "critical"}
        assert derive_incident_id(alert, ckpt) == derive_incident_id(redelivery, ckpt)

    def test_generation_zero_is_uuid5_over_the_dedup_key(self) -> None:
        alert = {"source": "billing", "fingerprint": "kafka-lag-spike"}
        derived = derive_incident_id(alert, InMemoryCheckpointer())
        assert derived == uuid5(_INCIDENT_NAMESPACE, dedup_key(alert))

    def test_different_fingerprints_derive_different_ids(self) -> None:
        ckpt = InMemoryCheckpointer()
        first = derive_incident_id({"source": "billing", "fingerprint": "lag"}, ckpt)
        second = derive_incident_id({"source": "billing", "fingerprint": "dlq"}, ckpt)
        assert first != second

    def test_different_sources_derive_different_ids(self) -> None:
        ckpt = InMemoryCheckpointer()
        first = derive_incident_id({"source": "billing", "fingerprint": "lag"}, ckpt)
        second = derive_incident_id({"source": "checkout", "fingerprint": "lag"}, ckpt)
        assert first != second

    @pytest.mark.parametrize("alert", [{}, {"fingerprint": None}, {"fingerprint": ""}])
    def test_absent_fingerprint_is_unique_per_call(self, alert: dict[str, Any]) -> None:
        ckpt = InMemoryCheckpointer()
        payload = {"source": "billing", **alert}
        assert derive_incident_id(payload, ckpt) != derive_incident_id(payload, ckpt)

    def test_whitespace_fingerprint_is_unique_per_call(self) -> None:
        ckpt = InMemoryCheckpointer()
        payload: dict[str, object] = {"source": "billing", "fingerprint": "   "}
        assert derive_incident_id(payload, ckpt) != derive_incident_id(payload, ckpt)

    def test_fallback_keys_on_the_raw_field_not_the_hash(self) -> None:
        # ``dedup_key`` happily hashes "billing|" into a stable value; returning
        # it would collapse every fingerprint-less alert from one source into a
        # single immortal incident. The check must be on the raw field.
        payload: dict[str, object] = {"source": "billing", "fingerprint": ""}
        derived = derive_incident_id(payload, InMemoryCheckpointer())
        assert derived != uuid5(_INCIDENT_NAMESPACE, dedup_key(payload))

    def test_non_terminal_prior_run_joins_the_same_incident(self, now: datetime) -> None:
        ckpt = InMemoryCheckpointer()
        alert = {"source": "billing", "fingerprint": "kafka-lag-spike"}
        first = derive_incident_id(alert, ckpt)
        ckpt.write(
            start_run(alert, _test_settings(), now, incident_id=first).with_state(
                IncidentState.INVESTIGATING, now
            )
        )
        assert derive_incident_id(alert, ckpt) == first

    def test_terminal_prior_run_opens_the_next_generation(self, now: datetime) -> None:
        ckpt = InMemoryCheckpointer()
        alert = {"source": "billing", "fingerprint": "kafka-lag-spike"}
        first = derive_incident_id(alert, ckpt)
        ckpt.write(_terminal_snapshot(first, now))
        recurrence = derive_incident_id(alert, ckpt)
        assert recurrence != first
        assert recurrence == uuid5(_INCIDENT_NAMESPACE, f"{dedup_key(alert)}|1")

    def test_recurrence_of_a_recurrence_is_still_deterministic(self, now: datetime) -> None:
        ckpt = InMemoryCheckpointer()
        alert = {"source": "billing", "fingerprint": "kafka-lag-spike"}
        for _ in range(2):
            ckpt.write(_terminal_snapshot(derive_incident_id(alert, ckpt), now))
        third = derive_incident_id(alert, ckpt)
        assert third == uuid5(_INCIDENT_NAMESPACE, f"{dedup_key(alert)}|2")
        # Deterministic, not random: the redelivery of a recurrence must
        # resolve to the same id or dedupe is lost exactly for flapping alerts.
        assert derive_incident_id(alert, ckpt) == third

    def test_cap_exhaustion_falls_back_to_uuid4(
        self, now: datetime, caplog: pytest.LogCaptureFixture
    ) -> None:
        ckpt = InMemoryCheckpointer()
        alert = {"source": "billing", "fingerprint": "flapping"}
        key = dedup_key(alert)
        chain = {uuid5(_INCIDENT_NAMESPACE, key)} | {
            uuid5(_INCIDENT_NAMESPACE, f"{key}|{n}")
            for n in range(1, _MAX_RECURRENCE_GENERATIONS + 1)
        }
        for incident_id in chain:
            ckpt.write(_terminal_snapshot(incident_id, now))

        with caplog.at_level(logging.WARNING, logger="incident_commander.agent.factory"):
            derived = derive_incident_id(alert, ckpt)

        assert derived not in chain
        assert derive_incident_id(alert, ckpt) != derived
        assert any("recurrence" in record.getMessage() for record in caplog.records)


class TestStartRunToolCallOverride:
    """ADR 0019: the caller may bound one run below the fleet default.

    Before this, every eval scenario ran on ``settings.budget_max_tool_calls``
    regardless of the cap it declared, so the runtime ceiling and the graded
    cap were different numbers — and the investigation planner was told the
    default of 25 in every scenario, including the ones whose entire subject
    is behaviour under a tight budget.
    """

    def test_override_seeds_the_ledger(self, now: datetime) -> None:
        settings = _test_settings(budget_max_tool_calls=25)
        run = start_run({"source": "s"}, settings, now, max_tool_calls=5)
        assert run.budget.max_tool_calls == 5

    def test_absent_override_keeps_the_setting(self, now: datetime) -> None:
        settings = _test_settings(budget_max_tool_calls=25)
        run = start_run({"source": "s"}, settings, now)
        assert run.budget.max_tool_calls == 25

    def test_none_override_keeps_the_setting(self, now: datetime) -> None:
        # A scenario that declares no cap is bounded by configuration.
        settings = _test_settings(budget_max_tool_calls=25)
        run = start_run({"source": "s"}, settings, now, max_tool_calls=None)
        assert run.budget.max_tool_calls == 25

    def test_zero_override_is_ignored(self, now: datetime) -> None:
        """A zero ledger is born exhausted, so it cannot be a runtime ceiling.

        ``is_exhausted`` is ``used >= max``; at max 0 the loop escalates
        before TRIAGE ever classifies the alert, ending the run before it
        does the thing a cap-0 scenario exists to observe. The claim "a
        correct run makes no tool call" is graded post-hoc instead.
        """
        settings = _test_settings(budget_max_tool_calls=25)
        run = start_run({"source": "s"}, settings, now, max_tool_calls=0)
        assert run.budget.max_tool_calls == 25
        assert not run.budget.is_exhausted

    def test_override_does_not_touch_the_other_meters(self, now: datetime) -> None:
        settings = _test_settings(budget_max_tool_calls=25)
        run = start_run({"source": "s"}, settings, now, max_tool_calls=5)
        assert run.budget.max_tokens == settings.budget_max_tokens
        assert run.budget.max_wall_seconds == settings.budget_max_seconds
        assert run.budget.max_usd == settings.budget_max_usd

    def test_override_is_keyword_only(self, now: datetime) -> None:
        # Positional slot 4 is `incident_id` (ADR 0016) and must stay there;
        # a positional cap would silently become an incident id.
        with pytest.raises(TypeError):
            start_run({"source": "s"}, _test_settings(), now, None, 5)  # type: ignore[call-arg]
