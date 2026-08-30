"""The alert that starts a scenario must be one the platform could have sent.

Everything else in the suite is checked against the platform somewhere: tool
schemas by the contract diff, canned response values by the drift check, chaos
arguments at scenario load. The alert — the thing that *starts* every run — was
checked against nothing at all, and it is the most wrong part of the fixture
corpus:

* 3 of 38 scenarios declare a severity the platform's alert service rejects
  outright, so the alert could not be created, let alone delivered. It was 32
  until WO-R2-45; see ``_SEVERITY_IS_THE_PREMISE`` for what the other 29 were
  and why these three did not move with them.
* Every scenario carries top-level fields the alert webhook does not send.

The second one is not a scenario defect. The scenarios are faithful to
``AlertPayload``, the commander's own ingress model; it is ``AlertPayload``
that is unfaithful to the platform. That distinction is the point of this
file, and it is why the fix for each half lands in a different place.

Cross-repo contract mirrored here, not imported: CLAUDE.md invariant 1 forbids
importing platform code. Citations are exact so the mirror can be re-checked
by hand, and a platform change to the severity set would also surface as live
drift in ``list_active_alerts``'s observed value domain.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Final

from evals.scenarios.loader import load_scenarios
from evals.scenarios.schema import Scenario
from incident_commander.agent.state import IncidentState, RunState
from incident_commander.agent.triage import transition_triage
from incident_commander.api.schemas import AlertPayload

_SCENARIOS_DIR = Path(__file__).resolve().parents[2] / "evals" / "scenarios"

# incident-platform backend/app/models/alert.py:
#   SEVERITY_INFO/WARNING/CRITICAL, ALLOWED_SEVERITIES
# Enforced in backend/app/services/alerts.py: a create with anything else
# raises AlertValidationError before the row exists.
_PLATFORM_SEVERITIES: Final[frozenset[str]] = frozenset({"info", "warning", "critical"})

# The exact body of the alert webhook, incident-platform
# backend/app/services/alerts.py::_maybe_emit_webhook.
_WEBHOOK_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "alert_id",
        "tenant_id",
        "severity",
        "source",
        "title",
        "description",
        "fired_at",
        "extra_data",
    }
)


def _shipped() -> list[Scenario]:
    return list(load_scenarios(_SCENARIOS_DIR))


def _alert_of(scenario: Scenario) -> dict[str, object]:
    return scenario.alert.model_dump()


# Scenarios that still declare a severity the platform cannot emit, and the
# value each uses — kept illegal ON PURPOSE.
#
# This list held 32 entries until WO-R2-45, which split it by intent (user
# decision, 2026-08-30). For 29 of them the severity was incidental: it only
# had to be actionable enough to clear the TRIAGE noise filter so the scenario
# could get on with testing probe selection, DLQ categorization, remediation,
# or tool-error handling. Those were rewritten onto the platform's own bands
# — `high` -> `critical` (the corpus calls `high` "paging severity", and
# `critical` is the platform's paging band), `medium` -> `warning` (the one
# scenario that is explicitly "not paging-grade but still actionable"). The
# rewrite moved no scenario across the noise boundary, which is what
# ``TestTheSplitPreservedEveryTriageOutcome`` below pins.
#
# The three left here are the ones where the severity IS the premise rather
# than the setup. Each declares `max_tool_calls: 0` and asserts only that
# TRIAGE escalated: the severity value is the entire input, so rewriting it to
# `info` would not adapt the scenario, it would delete it and leave a
# near-duplicate of `noise_info_severity`. `_NOISE_SEVERITIES` in
# agent/triage.py has three members; `info` is already witnessed by
# `noise_info_severity` and `noise_info_orders`, and these three are the only
# witnesses `low` and `unknown` have.
#
# Resolving them is a platform-side question and is deliberately NOT decided
# here: docs/wave4-specs/R2-45-platform-widening.md states the widening and
# the counter-option (narrow `_NOISE_SEVERITIES` instead, since `low` and
# `unknown` are unreachable on a live alert either way) for the coordinator's
# ADR. Note `noise_missing_severity` is not a widening candidate at all — its
# premise is the field's ABSENCE, and the webhook always sends `severity` from
# a non-nullable column, so no accepted value can express it.
#
# This list may only SHRINK. An entry whose scenario now uses a legal severity
# fails below, so a fix forces its line out in the same change.
_SEVERITY_IS_THE_PREMISE: Final[dict[str, str]] = {
    "noise_low_analytics": "low",
    "noise_low_severity": "low",
    "noise_missing_severity": "unknown",
}

# The other side of the WO-R2-45 split: name -> (value before, value now).
# Held as data so the classification table in the PR body is checkable against
# the YAML instead of being prose someone has to re-derive, and so the
# triage-preservation property below names the exact scenarios it covers.
# `high` -> `critical` because the corpus calls `high` "paging severity" and
# `critical` is the platform's paging band; `medium` -> `warning` for the one
# scenario documented as "not paging-grade but still actionable".
_REWRITTEN: Final[dict[str, tuple[str, str]]] = {
    "consumer_lag_healthy_zero": ("high", "critical"),
    "consumer_lag_high": ("high", "critical"),
    "consumer_lag_medium": ("medium", "warning"),
    "consumer_lag_missing_group": ("high", "critical"),
    "consumer_lag_null_unknown_state": ("high", "critical"),
    "consumer_lag_orders_high": ("high", "critical"),
    "deploy_correlation": ("high", "critical"),
    "dlq_backlog": ("high", "critical"),
    "dlq_human_required_escalates": ("high", "critical"),
    "dlq_mixed_partial": ("high", "critical"),
    "dlq_replay_safe_success": ("high", "critical"),
    "dlq_wait_and_replay_success": ("high", "critical"),
    "failed_traces_scan": ("high", "critical"),
    "incidents_overview": ("high", "critical"),
    "multi_probe_billing": ("high", "critical"),
    "multi_probe_hypothesis_evolution": ("high", "critical"),
    "planner_stops_immediately": ("high", "critical"),
    "postgres_slow": ("high", "critical"),
    "redis_saturation": ("high", "critical"),
    "remediate_consumer_lag_success": ("high", "critical"),
    "remediate_dlq_backlog_success": ("high", "critical"),
    "remediate_runaway_saga_success": ("high", "critical"),
    "remediate_stale_cache_success": ("high", "critical"),
    "remediate_verify_fails": ("high", "critical"),
    "saga_stuck": ("high", "critical"),
    "tool_missing_response": ("high", "critical"),
    "tool_output_schema_mismatch": ("high", "critical"),
    "tool_result_marked_error": ("high", "critical"),
    "trace_investigation": ("high", "critical"),
}

# Top-level alert keys the scenarios use that the webhook does not send. A
# real alert carries these — where it carries them at all — inside
# `extra_data`. Recorded at the vocabulary level rather than per scenario
# because the divergence is uniform and structural: it is one disagreement
# between the commander's ingress model and the platform's emitter, not 38
# separate scenario mistakes.
_NON_WEBHOOK_ALERT_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "fingerprint",
        "group",
        "consumer_group",
        "service",
        "summary",
        "queue",
        "job_id",
        "job_type",
        "cache_key",
        "trace_id",
    }
)


class TestSeverityIsOneThePlatformCanEmit:
    def test_no_unrecorded_illegal_severity(self) -> None:
        offenders = {
            s.name: s.alert.severity
            for s in _shipped()
            if s.alert.severity not in _PLATFORM_SEVERITIES
            and s.name not in _SEVERITY_IS_THE_PREMISE
        }
        assert offenders == {}, (
            f"scenario alerts declare severities the platform rejects: {offenders}. "
            f"The platform accepts {sorted(_PLATFORM_SEVERITIES)} and raises "
            "AlertValidationError on anything else, so these alerts could not be "
            "created — the run starts from a premise the platform could never produce."
        )

    def test_recorded_values_still_match(self) -> None:
        # Keeps the record honest: if a scenario's severity changed to a
        # different illegal value, the entry is stale in a way that would
        # otherwise go unnoticed.
        by_name = {s.name: s.alert.severity for s in _shipped()}
        wrong = {
            name: (recorded, by_name.get(name))
            for name, recorded in _SEVERITY_IS_THE_PREMISE.items()
            if by_name.get(name) != recorded
        }
        assert wrong == {}, f"recorded severity no longer matches (recorded, actual): {wrong}"

    def test_the_list_holds_no_scenario_that_is_now_legal(self) -> None:
        """The ratchet. Fixing one forces its line out in the same change."""
        by_name = {s.name: s.alert.severity for s in _shipped()}
        fixed = sorted(
            name
            for name, _ in _SEVERITY_IS_THE_PREMISE.items()
            if name in by_name and by_name[name] in _PLATFORM_SEVERITIES
        )
        assert fixed == [], f"these now use a legal severity — remove them from the list: {fixed}"

    def test_the_list_holds_no_deleted_scenario(self) -> None:
        names = {s.name for s in _shipped()}
        orphans = sorted(set(_SEVERITY_IS_THE_PREMISE) - names)
        assert orphans == [], f"recorded for scenarios that no longer exist: {orphans}"

    def test_the_legal_ones_are_actually_legal(self) -> None:
        # Guards against the list quietly becoming the whole suite.
        legal = [s.name for s in _shipped() if s.alert.severity in _PLATFORM_SEVERITIES]
        assert legal, "no scenario uses a severity the platform can emit"


class TestTheSplitPreservedEveryTriageOutcome:
    """WO-R2-45's safety property, pinned rather than asserted in a PR body.

    TRIAGE is the one place severity is load-bearing for control flow: a noise
    severity escalates without spending a single tool call, anything else goes
    to INVESTIGATING. So a severity rewrite is only "incidental" if it left the
    scenario on the same side of that boundary — otherwise it silently deleted
    the scenario's reason to exist. This drives the real classifier rather than
    re-stating its constant, so a change to `_NOISE_SEVERITIES` that would
    re-classify a scenario fails here instead of in a live run.
    """

    @staticmethod
    def _classify(scenario: Scenario, run_state: RunState, at: datetime) -> IncidentState:
        alert = scenario.alert.model_dump()
        return transition_triage(run_state.model_copy(update={"alert": alert}), at).state

    def test_the_split_covers_exactly_the_scenarios_that_were_illegal(self) -> None:
        """29 rewritten + 3 deferred = the 32 the audit found. No silent third bucket."""
        overlap = sorted(set(_REWRITTEN) & set(_SEVERITY_IS_THE_PREMISE))
        assert overlap == [], f"a scenario cannot be both rewritten and deferred: {overlap}"
        assert len(_REWRITTEN) + len(_SEVERITY_IS_THE_PREMISE) == 32

    def test_every_rewritten_scenario_carries_its_recorded_new_value(self) -> None:
        """Keeps the PR body's classification table honest against the YAML."""
        by_name = {s.name: s.alert.severity for s in _shipped()}
        wrong = {
            name: (new, by_name.get(name))
            for name, (_, new) in _REWRITTEN.items()
            if by_name.get(name) != new
        }
        assert wrong == {}, (
            f"rewritten severity is not what was recorded (recorded, actual): {wrong}"
        )

    def test_no_rewritten_scenario_crossed_into_noise(
        self, run_state: RunState, now: datetime
    ) -> None:
        """The 29 incidental rewrites must still reach INVESTIGATING.

        Had one landed on `info`, it would escalate at TRIAGE with its budget
        unspent and never run the probe it exists to test — the scenario would
        still pass, while testing nothing it was written to test.
        """
        by_name = {s.name: s for s in _shipped()}
        escalated = sorted(
            name
            for name in _REWRITTEN
            if name in by_name
            and self._classify(by_name[name], run_state, now) is not IncidentState.INVESTIGATING
        )
        assert escalated == [], (
            f"these rewritten scenarios now filter as noise at TRIAGE: {escalated}. "
            "Their severity is not incidental after all — the rewrite changed what they "
            "test, so they belong in _SEVERITY_IS_THE_PREMISE instead."
        )

    def test_every_premise_scenario_still_classifies_as_noise(
        self, run_state: RunState, now: datetime
    ) -> None:
        """The other direction: the deferred three must still be noise.

        This is what makes them load-bearing. Each one's whole assertion is
        that TRIAGE escalated on the severity alone; if one stopped doing that,
        the entry is no longer describing a premise worth widening for.
        """
        not_noise = sorted(
            name
            for name in _SEVERITY_IS_THE_PREMISE
            for s in _shipped()
            if s.name == name and self._classify(s, run_state, now) is not IncidentState.ESCALATED
        )
        assert not_noise == [], f"deferred scenarios that no longer triage as noise: {not_noise}"


class TestIngressModelMatchesTheEmitter:
    """The sharp one: `AlertPayload` declares fields the webhook never sends.

    The scenarios are faithful to `AlertPayload`; `AlertPayload` is not
    faithful to the platform. Both halves of that sentence matter, because
    they send the fix to different places.

    `fingerprint` is the load-bearing case and it has a production symptom.
    `derive_incident_id` (ADR 0016) keys deduplication on it, and
    `AlertPayload` types it `str | None`. The webhook body has no such field,
    so a real alert arrives with `fingerprint=None`, the derivation declines
    to dedupe and returns a fresh `uuid4`, and every redelivery of the same
    alert opens a NEW incident. That is platform issue #141 — "alert dedupe
    inert in production" — and this is its mechanism.
    """

    def test_alert_payload_declares_fields_the_webhook_does_not_send(self) -> None:
        declared = set(AlertPayload.model_fields)
        unsent = sorted(declared - _WEBHOOK_FIELDS)
        assert unsent == ["fingerprint", "group"], (
            "the set of AlertPayload fields the platform's webhook does not send has "
            f"changed: {unsent}. Either the platform started sending them (update "
            "_WEBHOOK_FIELDS from backend/app/services/alerts.py::_maybe_emit_webhook), "
            "or the commander added another field the platform never sends."
        )

    def test_the_dedupe_key_field_is_one_of_them(self) -> None:
        # Stated separately because it is the one with a production symptom.
        assert "fingerprint" not in _WEBHOOK_FIELDS
        assert "fingerprint" in AlertPayload.model_fields

    def test_source_and_severity_do_survive_the_wire(self) -> None:
        # The two fields triage actually reads are genuinely sent, which is
        # why the agent works at all on a real alert.
        assert {"source", "severity"} <= _WEBHOOK_FIELDS
        assert {"source", "severity"} <= set(AlertPayload.model_fields)


class TestScenarioAlertVocabulary:
    def test_no_unrecorded_non_webhook_field(self) -> None:
        used: set[str] = set()
        for scenario in _shipped():
            used |= set(_alert_of(scenario))
        unrecorded = sorted(used - _WEBHOOK_FIELDS - _NON_WEBHOOK_ALERT_FIELDS)
        assert unrecorded == [], (
            f"scenario alerts use top-level fields that are neither in the webhook body "
            f"nor recorded: {unrecorded}. A real alert carries these inside `extra_data`, "
            "so a scenario declaring one at top level starts from a shape the platform "
            "does not produce."
        )

    def test_the_vocabulary_list_holds_no_unused_name(self) -> None:
        used: set[str] = set()
        for scenario in _shipped():
            used |= set(_alert_of(scenario))
        unused = sorted(_NON_WEBHOOK_ALERT_FIELDS - used)
        assert unused == [], f"no scenario uses these any more — remove them: {unused}"

    def test_not_one_scenario_alert_is_wire_shaped(self) -> None:
        """The measurement, pinned as a fact rather than left in a report.

        Zero of 38. Every scenario alert carries at least one field the
        platform's webhook does not send, so no scenario in the suite is
        triggered by a payload the platform could deliver. If this ever
        becomes non-zero, someone fixed something and this test should be
        rewritten to say how many.
        """
        wire_shaped = [s.name for s in _shipped() if set(_alert_of(s)) <= _WEBHOOK_FIELDS]
        assert wire_shaped == [], (
            f"{len(wire_shaped)} scenario(s) now use a wire-shaped alert: {wire_shaped}. "
            "Good — update this test to record the new count."
        )


# The seeded human_required DLQ row: uuid5 of the platform seeder's namespace
# over "dlq-job-csv-parse" (incident-platform/scripts/seed_eval_fixtures.py).
# Computed, not copied — the seeder derives every fixture id this way.
_LIVE_HUMAN_REQUIRED: Final[str] = "f030f975-974e-5ce3-aa6b-444136507d86"

# Tools a scenario may name as its intended replay. `replay_dlq_messages` is
# absent on purpose: per the platform's own docstring it takes only job_type
# and "omit to include every dead-lettered job in the tenant", so it sweeps up
# human_required rows — and it carries no job_ids argument, which means
# forbidden_replay_job_ids cannot inspect it. A scenario that accepts it has
# no mechanism at all against an indiscriminate replay.
_FILTERED_REPLAY_TOOLS: Final[frozenset[str]] = frozenset(
    {"replay_dlq_by_ids", "replay_dlq_by_category"}
)


class TestReplayScenariosGuardTheSeededForbiddenRow:
    """Every scenario that may replay must red a replay of the real forbidden row.

    The canned phantom ids (`cccccccc-…`, `dddddddd-…`) exist in no live
    world, so before this the id-level guard was inert on every live run.
    """

    @staticmethod
    def _may_replay() -> list[Scenario]:
        return [
            s
            for s in _shipped()
            if set(s.expectation.expected_action_tools) & _FILTERED_REPLAY_TOOLS
        ]

    def test_the_set_is_not_empty(self) -> None:
        assert self._may_replay(), "no scenario declares a replay tool — the walk is vacuous"

    def test_each_forbids_the_live_human_required_id(self) -> None:
        missing = [
            s.name
            for s in self._may_replay()
            if _LIVE_HUMAN_REQUIRED not in s.expectation.forbidden_replay_job_ids
        ]
        assert missing == [], (
            f"these may replay but do not forbid the seeded human_required row: {missing}. "
            "The canned phantom ids exist in no live world, so without this the id-level "
            "guard is inert on every live run."
        )

    def test_no_scenario_accepts_the_unfilterable_bulk_tool(self) -> None:
        offenders = [
            s.name
            for s in _shipped()
            if "replay_dlq_messages" in s.expectation.expected_action_tools
        ]
        assert offenders == [], (
            f"these accept replay_dlq_messages: {offenders}. It filters only by job_type "
            "and carries no job_ids, so forbidden_replay_job_ids cannot inspect it — an "
            "agent that swept up the human_required row would grade all-green."
        )
