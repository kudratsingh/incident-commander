"""The alert that starts a scenario must be one the platform could have sent.

Everything else is checked against the platform somewhere; the alert was not. Two
defects: 3 of 38 scenarios declare a severity the platform rejects (WO-R2-45), and
every scenario carries top-level fields the webhook does not send — the second is
``AlertPayload``'s fault, not the corpus's. Contract mirrored, not imported (invariant 1).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Final

from evals.scenarios.loader import load_scenarios
from evals.scenarios.schema import Scenario
from incident_commander.agent.investigation import SubjectMatch, alert_subject
from incident_commander.agent.state import IncidentState, RunState
from incident_commander.agent.triage import transition_triage
from incident_commander.api.schemas import AlertPayload

_SCENARIOS_DIR = Path(__file__).resolve().parents[2] / "evals" / "scenarios"

# ALLOWED_SEVERITIES (platform app/models/alert.py); anything else is rejected.
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


# Severities the platform cannot emit, kept illegal ON PURPOSE: the only
# witnesses `low` and `unknown` have; WO-R2-45 rewrote the other 29 (R2-45 spec).
_SEVERITY_IS_THE_PREMISE: Final[dict[str, str]] = {
    "noise_low_analytics": "low",
    "noise_low_severity": "low",
    "noise_missing_severity": "unknown",
}

# The other side of the WO-R2-45 split: name -> (value before, value now),
# held as data so the classification is checkable against the YAML.
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

# Top-level alert keys the scenarios use that the webhook does not send — a real
# alert carries them inside `extra_data`. `remediation_hint` is not emitted yet.
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
        "remediation_hint",
        # ADR 0032: the positive statement `remediation_hint: null` cannot make.
        "dlq_scope",
        # WO-R3-202 / ADR 0051: the `jobs_not_progressing` noise variant carries the
        # running release. NOT in `ALERT_SUBJECT_PROBES` — a release needs no probe.
        "deploy_version",
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
        # Keeps the record honest: a severity changed to a different illegal value.
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

    TRIAGE is where severity drives control flow: a noise severity escalates without
    spending a tool call, anything else goes to INVESTIGATING. A `_NOISE_SEVERITIES`
    change fails here rather than in a live run.
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

        On `info` one escalates at TRIAGE, budget unspent, still passing.
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

        Each one asserts only that TRIAGE escalated on the severity.
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

    `fingerprint` is the load-bearing case: `derive_incident_id` (ADR 0016) keys dedupe
    on it, the webhook body has no such field, so a real alert arrives with None, the
    derivation returns a fresh `uuid4` and every redelivery opens a NEW incident (plat #141).
    """

    def test_alert_payload_declares_fields_the_webhook_does_not_send(self) -> None:
        declared = set(AlertPayload.model_fields)
        unsent = sorted(declared - _WEBHOOK_FIELDS)
        assert unsent == ["dlq_scope", "fingerprint", "group", "remediation_hint"], (
            "the set of AlertPayload fields the platform's webhook does not send has "
            f"changed: {unsent}. Either the platform started sending them (update "
            "_WEBHOOK_FIELDS from backend/app/services/alerts.py::_maybe_emit_webhook), "
            "or the commander added another field the platform never sends."
        )

    def test_the_dlq_category_field_is_one_of_them_and_is_a_filed_platform_gap(self) -> None:
        """`remediation_hint` joined the list on 2026-09-07, deliberately.

        Added to `AlertPayload` knowing the platform does not send it: the commander READS it
        (`ALERT_SUBJECT_PROBES` → `list_dlq_messages`) and the guard is inert in production.
        """
        assert "remediation_hint" not in _WEBHOOK_FIELDS
        assert "remediation_hint" in AlertPayload.model_fields
        assert AlertPayload(source="platform.dlq").remediation_hint is None

    def test_the_unclassified_scope_field_is_one_of_them_and_the_same_filed_gap(self) -> None:
        """`dlq_scope` joined the list on 2026-09-08, with the same posture.

        A SEPARATE field, not a reading of `remediation_hint: null`: `model_dump()` materialises
        every declared field, so an omitted key and an explicit null are the same object.
        """
        assert "dlq_scope" not in _WEBHOOK_FIELDS
        assert "dlq_scope" in AlertPayload.model_fields
        assert AlertPayload(source="platform.dlq").dlq_scope is None
        # The collapse itself, asserted rather than described.
        omitted = AlertPayload(source="platform.dlq").model_dump()
        explicit = AlertPayload(source="platform.dlq", remediation_hint=None).model_dump()
        assert omitted == explicit, (
            "an omitted `remediation_hint` and an explicit null no longer dump "
            "identically. If pydantic gained a way to distinguish them here, ADR 0032's "
            "reason for a separate field is worth re-reading before adding another."
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

        Zero of 38: every scenario alert carries at least one field the webhook does not send.
        """
        # `exclude_none=True` asks about the scenario's YAML, not AlertPayload's field list: plain
        # model_dump() always emits `fingerprint` and `group`, so the test was vacuous (WO-R2-102).
        wire_shaped = [
            s.name
            for s in _shipped()
            if set(s.alert.model_dump(exclude_none=True)) <= _WEBHOOK_FIELDS
        ]
        assert wire_shaped == [], (
            f"{len(wire_shaped)} scenario(s) now use a wire-shaped alert: {wire_shaped}. "
            "Good — update this test to record the new count."
        )


# The seeded human_required DLQ row, computed as seed_eval_fixtures.py derives fixture ids.
_LIVE_HUMAN_REQUIRED: Final[str] = "f030f975-974e-5ce3-aa6b-444136507d86"

# Tools a scenario may name as its intended replay. `replay_dlq_messages` is absent: it
# sweeps the whole tenant and has no job_ids argument for forbidden_replay_job_ids to see.
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


class TestTheUnclassifiedDlqAlertNamesTheScopeNotACategory:
    """`dlq_human_required_escalates` carries no `remediation_hint` but does carry `dlq_scope`.

    The incident is a row the platform's triage has not classified, and no listing selects
    unclassified rows, so the category stays null (ADR 0032). Live run `a0aa257bf865` cost the
    old inert reading: with no subject, nothing required the ACTION to be about that row.
    """

    _SCENARIO: Final[str] = "dlq_human_required_escalates"
    _CHAOS_ROW: Final[str] = "3971a293-3f5b-55eb-b835-649d685801a7"
    _SEEDED_HUMAN_REQUIRED: Final[str] = "f030f975-974e-5ce3-aa6b-444136507d86"

    def _scenario(self) -> Scenario:
        return {s.name: s for s in _shipped()}[self._SCENARIO]

    def test_the_alert_carries_no_category(self) -> None:
        assert self._scenario().alert.remediation_hint is None, (
            f"{self._SCENARIO}: the alert names a category. Its incident is a row "
            "nothing has classified, so any category it named would be one the "
            "platform's own classifier did not assign."
        )

    def test_the_alert_names_the_unclassified_scope_instead(self) -> None:
        assert self._scenario().alert.dlq_scope == "unclassified", (
            f"{self._SCENARIO}: the alert no longer names the unclassified scope. "
            "Without it the subject guard is inert here, and an inert subject is what "
            "admitted live run a0aa257bf865's plan — a category replay of the "
            "replay_safe slice under an alert about a row nothing had classified."
        )

    def test_the_subject_is_the_unfiltered_listing_not_a_filtered_one(self) -> None:
        """The derived subject, and the shape of the probe it demands.

        The unfiltered page is the only read that shows this row, so the subject resolves to
        `list_dlq_messages` under `SubjectMatch.UNFILTERED`.
        """
        subject = alert_subject(_alert_of(self._scenario()))
        assert subject is not None
        assert (subject.alert_field, subject.tool_name, subject.argument_field) == (
            "dlq_scope",
            "list_dlq_messages",
            "remediation_hint",
        )
        assert subject.match is SubjectMatch.UNFILTERED

    def test_a_category_scoped_listing_would_not_contain_this_incident(self) -> None:
        """The reason the field cannot simply be added back.

        Read off the scenario's own canned pre-fence listing. A `human_required` alert would
        make the required first probe the one listing that hides the fault.
        """
        scenario = self._scenario()
        canned = scenario.canned_tool_responses["list_dlq_messages"]
        assert isinstance(canned, tuple), (
            f"{self._SCENARIO}: the listing fixture is a sequence — the row changes "
            "across the fence and both sides are graded"
        )
        pre = json.loads(canned[0].content[0]["text"])
        rows = {row["id"]: row for row in pre["items"]}
        assert rows[self._CHAOS_ROW]["remediation_hint"] is None
        assert rows[self._CHAOS_ROW]["fenced_at"] is None

        human_required = [
            row_id for row_id, row in rows.items() if row["remediation_hint"] == "human_required"
        ]
        assert human_required == [self._SEEDED_HUMAN_REQUIRED], (
            "the human_required slice of this world is the seeded furniture row alone; "
            f"a category-scoped alert would send the agent to {human_required} and never "
            f"show it {self._CHAOS_ROW}"
        )

    def test_the_fence_is_observable_across_the_two_recordings(self) -> None:
        """The re-seed's whole point, asserted on the fixture itself.

        Through v0.6.1 a fence on this row changed nothing any read could see; the pre/post
        pair is what makes it observable on the ROW.
        """
        scenario = self._scenario()
        canned = scenario.canned_tool_responses["list_dlq_messages"]
        assert isinstance(canned, tuple) and len(canned) == 2
        pre, post = (json.loads(c.content[0]["text"]) for c in canned)
        before = next(r for r in pre["items"] if r["id"] == self._CHAOS_ROW)
        after = next(r for r in post["items"] if r["id"] == self._CHAOS_ROW)
        assert (before["remediation_hint"], before["fenced_at"]) == (None, None)
        assert after["remediation_hint"] == "human_required"
        assert after["fenced_at"] is not None
        assert after["fenced_by"] is not None
        # The row stays in the queue: the platform documents the mark as not
        # changing job.status, so an ABSENT row would be the failure.
        assert pre["total"] == post["total"] == 5
