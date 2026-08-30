"""Tier-1 canned fixtures against the tools' output models — offline.

The live drift check excludes every Tier-1 fixture by construction, because
probing `pause_dag` to see what it returns would pause a DAG. That left the
nine Tier-1 recordings checked by nothing: the one class of fixture that can
invent a field and never be contradicted. The committed tool snapshot
answers the half that needs no platform.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from evals.fixture_drift import CannedCall, canned_calls
from evals.fixture_shape import (
    MISSING_REQUIRED_FIELD,
    TYPE,
    UNDECLARED_FIELD,
    check_call,
    check_calls,
    load_output_schemas,
    write_tier_calls,
)
from evals.scenarios.loader import load_scenarios

_SCENARIOS_DIR = Path(__file__).resolve().parents[2] / "evals" / "scenarios"


def _tier_one_calls() -> tuple[CannedCall, ...]:
    return write_tier_calls(canned_calls(load_scenarios(_SCENARIOS_DIR)))


def _call(tool: str, payload: dict[str, Any]) -> CannedCall:
    return CannedCall(scenario="s", tool=tool, arguments={}, payload=payload)


class TestTheShippedTierOneFixtures:
    def test_every_tier_one_fixture_matches_its_output_model(self) -> None:
        defects = check_calls(_tier_one_calls())
        assert [d.describe() for d in defects] == []

    def test_there_are_tier_one_fixtures_to_check(self) -> None:
        # Guards the check against passing because it looked at nothing —
        # the failure mode of every filter-based guard.
        tools = {call.tool for call in _tier_one_calls()}
        assert {"pause_dag", "mark_dlq_permanent"} <= tools

    def test_the_snapshot_describes_every_tier_one_tool_a_fixture_answers(self) -> None:
        schemas = load_output_schemas()
        undescribed = sorted({c.tool for c in _tier_one_calls()} - set(schemas))
        assert undescribed == []


class TestFabricatedTierOneFixtures:
    """What the check would have caught, on payloads built to be wrong."""

    def _schema(self, tool: str) -> Any:
        return load_output_schemas()[tool]

    def test_an_invented_field_is_flagged(self) -> None:
        defects = check_call(
            _call(
                "pause_dag",
                {
                    "root_job_id": "j1",
                    "pause_key": "dag:paused:j1",
                    "ttl_seconds": 600,
                    "accepted": True,
                    "paused_until": "2026-07-30T10:10:00Z",
                },
            ),
            self._schema("pause_dag"),
        )
        assert [(d.path, d.kind) for d in defects] == [("paused_until", UNDECLARED_FIELD)]

    def test_a_missing_required_field_is_flagged(self) -> None:
        defects = check_call(
            _call("pause_dag", {"root_job_id": "j1", "pause_key": "dag:paused:j1"}),
            self._schema("pause_dag"),
        )
        assert sorted(d.path for d in defects) == ["accepted", "ttl_seconds"]
        assert {d.kind for d in defects} == {MISSING_REQUIRED_FIELD}

    def test_a_wrong_type_is_flagged(self) -> None:
        defects = check_call(
            _call(
                "pause_dag",
                {
                    "root_job_id": "j1",
                    "pause_key": "dag:paused:j1",
                    "ttl_seconds": "600",
                    "accepted": True,
                },
            ),
            self._schema("pause_dag"),
        )
        assert [(d.path, d.kind) for d in defects] == [("ttl_seconds", TYPE)]

    def test_a_nullable_field_accepts_null(self) -> None:
        # `previous_hint` is `str | null`; a fixture recording the
        # never-classified case is legal and must not be reported.
        defects = check_call(
            _call(
                "mark_dlq_permanent",
                {
                    "job_id": "j1",
                    "previous_hint": None,
                    "remediation_hint": "human_required",
                    "already_marked": False,
                },
            ),
            self._schema("mark_dlq_permanent"),
        )
        assert defects == []

    def test_a_boolean_is_not_accepted_where_a_number_is_declared(self) -> None:
        # `True == 1` in Python, and a laxer check would let a fixture claim
        # a boolean ttl. JSON Schema says a bool is not an integer.
        defects = check_call(
            _call(
                "pause_dag",
                {
                    "root_job_id": "j1",
                    "pause_key": "dag:paused:j1",
                    "ttl_seconds": True,
                    "accepted": True,
                },
            ),
            self._schema("pause_dag"),
        )
        assert [(d.path, d.kind) for d in defects] == [("ttl_seconds", TYPE)]

    def test_rows_inside_a_list_are_checked_against_their_own_model(self) -> None:
        # replay_dlq_by_ids returns a list of ReplayResult behind a $ref.
        defects = check_call(
            _call(
                "replay_dlq_by_ids",
                {
                    "requested": 1,
                    "replayed": 1,
                    "scheduled": 0,
                    "failed": 0,
                    "results": [{"id": "j1", "ok": True, "invented_row_field": 1}],
                },
            ),
            self._schema("replay_dlq_by_ids"),
        )
        assert [(d.path, d.kind) for d in defects] == [
            ("results[].invented_row_field", UNDECLARED_FIELD)
        ]

    def test_a_value_the_platform_cannot_produce_is_NOT_caught(self) -> None:
        """The named remaining hole, pinned so nobody assumes otherwise.

        Both defects this check was built alongside were VALUES in correctly
        shaped fields — a fabricated Redis namespace, and a flag
        contradicting the field beside it. A JSON Schema of plain strings
        and booleans cannot express either, and no offline check can. They
        needed a person reading the platform's code.
        """
        defects = check_call(
            _call(
                "pause_dag",
                {
                    "root_job_id": "j1",
                    "pause_key": "chaos:dag_pause:j1",
                    "ttl_seconds": 600,
                    "accepted": True,
                },
            ),
            self._schema("pause_dag"),
        )
        assert defects == []
