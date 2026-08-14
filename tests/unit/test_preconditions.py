"""Preconditions: establishing a scenario's premise before grading the agent.

The failure this closes is a paid one. `bb1fa70abb4c` graded FAIL and the
report said the agent fixed the wrong thing; the truth was that the thing it
was told about could not be made to exist. Nothing distinguished "the agent
was wrong" from "the world was never broken", so the run described the agent
and was believed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from evals.preconditions import resolve, unmet
from evals.scenarios.loader import load_scenarios
from evals.scenarios.schema import PreconditionField, PreconditionProbe, Scenario

_SCENARIOS_DIR = Path(__file__).resolve().parents[2] / "evals" / "scenarios"

_DLQ_PAYLOAD: dict[str, Any] = {
    "total": 4,
    "items": [
        {"id": "a", "remediation_hint": "replay_safe"},
        {"id": "b", "remediation_hint": "human_required"},
        {"id": "c", "remediation_hint": None},
    ],
}


def _probe(**fields: Any) -> PreconditionProbe:
    return PreconditionProbe(
        tool=fields.pop("tool", "list_dlq_messages"),
        expect=fields.pop("expect"),
        **fields,
    )


class TestResolve:
    def test_top_level_field(self) -> None:
        assert resolve(_DLQ_PAYLOAD, "total") == [4]

    def test_descends_into_a_list(self) -> None:
        assert resolve(_DLQ_PAYLOAD, "items[].remediation_hint") == [
            "replay_safe",
            "human_required",
            None,
        ]

    def test_missing_key_resolves_to_nothing(self) -> None:
        assert resolve(_DLQ_PAYLOAD, "absent") == []

    def test_missing_key_inside_rows_resolves_to_nothing(self) -> None:
        assert resolve(_DLQ_PAYLOAD, "items[].absent") == []

    def test_a_row_missing_the_field_is_skipped_not_fatal(self) -> None:
        payload = {"items": [{"hint": "x"}, {}]}
        assert resolve(payload, "items[].hint") == ["x"]

    def test_nested_object_inside_a_row(self) -> None:
        payload = {"items": [{"triage": {"category": "bad_input"}}]}
        assert resolve(payload, "items[].triage.category") == ["bad_input"]

    def test_a_string_is_not_treated_as_a_sequence(self) -> None:
        # Guards the obvious bug: str is a Sequence, so a naive descend would
        # explode "abc" into three characters.
        assert resolve({"name": "abc"}, "name[]") == []


class TestUnmet:
    def test_satisfied_by_any_row_is_met(self) -> None:
        probe = _probe(
            expect=[PreconditionField(path="items[].remediation_hint", equals="human_required")]
        )
        assert unmet(probe, _DLQ_PAYLOAD) == []

    def test_no_row_satisfies_is_unmet(self) -> None:
        probe = _probe(
            expect=[PreconditionField(path="items[].remediation_hint", equals="wait_and_replay")]
        )
        failures = unmet(probe, _DLQ_PAYLOAD)
        assert len(failures) == 1
        assert "remediation_hint" in failures[0]
        assert "wait_and_replay" in failures[0]

    def test_absent_path_is_unmet_and_says_so(self) -> None:
        probe = _probe(expect=(PreconditionField(path="lag", at_least=1),))
        failures = unmet(probe, _DLQ_PAYLOAD)
        assert "nothing at 'lag'" in failures[0]

    def test_numeric_threshold(self) -> None:
        met = _probe(expect=(PreconditionField(path="total", at_least=4),))
        unmet_probe = _probe(expect=(PreconditionField(path="total", at_least=5),))
        assert unmet(met, _DLQ_PAYLOAD) == []
        assert unmet(unmet_probe, _DLQ_PAYLOAD) != []

    def test_a_zero_reading_does_not_satisfy_at_least_one(self) -> None:
        # The lag case: a healthy group reads 0, and the scenario needs a
        # backlog. This is the assertion that stops a run against a world
        # where the fault has not landed yet.
        probe = _probe(tool="get_consumer_lag", expect=(PreconditionField(path="lag", at_least=1),))
        assert unmet(probe, {"lag": 0}) != []
        assert unmet(probe, {"lag": 1200}) == []

    def test_a_null_reading_does_not_satisfy_at_least_one(self) -> None:
        # A group the platform cannot resolve reads null, which is neither a
        # backlog nor a healthy zero — and must not pass as either.
        probe = _probe(tool="get_consumer_lag", expect=(PreconditionField(path="lag", at_least=1),))
        assert unmet(probe, {"lag": None}) != []

    def test_every_failing_field_is_reported(self) -> None:
        probe = _probe(
            expect=(
                PreconditionField(path="total", at_least=99),
                PreconditionField(path="items[].remediation_hint", equals="nope"),
            )
        )
        assert len(unmet(probe, _DLQ_PAYLOAD)) == 2


class TestProbeSchema:
    """A precondition establishes what is; it must never make it so."""

    def test_write_tool_rejected(self) -> None:
        with pytest.raises(ValidationError, match="may only read"):
            PreconditionProbe(
                tool="replay_dlq_by_category",
                expect=(PreconditionField(path="replayed", at_least=1),),
            )

    def test_chaos_tool_rejected(self) -> None:
        # Chaos hooks are not in TOOL_REGISTRY at all, so they fail earlier —
        # but the point is the same: seeding is chaos_setup's job, and a
        # precondition that seeded would be verifying its own handiwork.
        with pytest.raises(ValidationError, match="not a registered tool"):
            PreconditionProbe(
                tool="kill_consumer", expect=(PreconditionField(path="ok", equals=True),)
            )

    def test_read_tool_accepted(self) -> None:
        probe = PreconditionProbe(
            tool="get_consumer_lag", expect=(PreconditionField(path="lag", at_least=1),)
        )
        assert probe.tool == "get_consumer_lag"

    def test_at_least_one_assertion_required(self) -> None:
        # A probe with nothing to assert is a tool call that proves nothing.
        with pytest.raises(ValidationError):
            PreconditionProbe(tool="get_consumer_lag", expect=())

    def test_exactly_one_comparator_per_field(self) -> None:
        with pytest.raises(ValidationError, match="exactly one of"):
            PreconditionField(path="lag", at_least=1, equals=5)

    def test_defaults_to_a_single_look(self) -> None:
        probe = PreconditionProbe(
            tool="get_consumer_lag", expect=(PreconditionField(path="lag", at_least=1),)
        )
        assert probe.attempts == 1
        assert probe.delay_seconds == 0.0


def _shipped() -> list[Scenario]:
    return list(load_scenarios(_SCENARIOS_DIR))


# Remediation scenarios with no precondition, and why. Each entry is a claim
# that the fault is not observable through any READ tool — not that nobody
# got around to it. Adding a name here needs that justification; removing one
# needs a precondition.
_JUSTIFIED_WITHOUT_PRECONDITION: dict[str, str] = {
    # create_stale_cache writes a Redis key. No read tool exposes cache keys —
    # get_redis_health returns server stats only, and invalidate_cache_key is
    # the Tier-1 write this scenario exists to test. The fault is real and
    # unobservable, which is a gap in the platform's read surface, not here.
    "remediate_stale_cache_success": "no read tool exposes a Redis key",
    # use_live_mcp: false — it never runs live, and preconditions are about
    # the live world. Its canned responses ARE its premise.
    "remediate_verify_fails": "canned-only scenario; never runs live",
}


class TestRemediationScenarioCoverage:
    """Stage 1's fifth criterion: every remediation scenario has one, or a reason."""

    @staticmethod
    def _remediation_scenarios() -> list[Scenario]:
        return [s for s in _shipped() if s.expectation.expected_action_tools]

    def test_every_remediation_scenario_is_accounted_for(self) -> None:
        missing = [
            s.name
            for s in self._remediation_scenarios()
            if not s.expected_precondition and s.name not in _JUSTIFIED_WITHOUT_PRECONDITION
        ]
        assert missing == [], (
            f"remediation scenarios with no precondition and no recorded reason: {missing}. "
            "A live scenario that cannot say what must be true before it runs cannot "
            "distinguish an agent failure from a fault that was never manufactured."
        )

    def test_the_exception_list_has_no_dead_entries(self) -> None:
        # An exception for a scenario that now HAS a precondition, or that no
        # longer exists, is a suppression nobody can discharge.
        names = {s.name for s in _shipped()}
        with_precondition = {s.name for s in _shipped() if s.expected_precondition}
        stale = sorted(
            name
            for name in _JUSTIFIED_WITHOUT_PRECONDITION
            if name not in names or name in with_precondition
        )
        assert stale == [], f"remove these from the exception list: {stale}"

    def test_preconditions_only_probe_read_tools(self) -> None:
        from incident_commander.tools.policies import Tier, tier_of

        offenders = [
            f"{s.name}:{p.tool}"
            for s in _shipped()
            for p in s.expected_precondition
            if tier_of(p.tool) is not Tier.READ
        ]
        assert offenders == []

    def test_the_lag_precondition_waits_for_the_metrics_interval(self) -> None:
        """The one precondition that must poll, pinned so it cannot be flattened.

        kill_consumer stops the consumer at once, but the platform recomputes
        lag on a 60s interval, so the number stays 0 for up to a minute after
        seeding. A single look would fail a correctly-seeded world.
        """
        scenario = next(s for s in _shipped() if s.name == "remediate_consumer_lag_success")
        probe = next(p for p in scenario.expected_precondition if p.tool == "get_consumer_lag")
        assert probe.attempts * probe.delay_seconds >= 60.0
