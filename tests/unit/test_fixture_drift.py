"""The canned-vs-live comparison, exercised offline against synthetic payloads.

The live half lives in ``tests/integration/test_canned_fixtures_match_live.py``
and needs a platform. Everything about *what counts as drift* is decided here,
where it can be tested without one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from evals.fixture_drift import CannedCall, Drift, canned_calls, compare
from evals.fixture_drift_ledger import (
    _JUSTIFIED,
    FIXTURE_DEFECT,
    POST_FAULT,
    classify,
    context_of,
    defect_count,
    dump_ledger,
    load_entries,
    load_ledger,
)
from evals.fixture_probe import UnseededPlatformError, assert_seeded
from evals.scenarios.loader import load_scenarios
from incident_commander.tools.mcp_client import ToolResult

_SCENARIOS_DIR = Path(__file__).resolve().parents[2] / "evals" / "scenarios"


def _call(tool: str, payload: dict[str, Any], *, scenario: str = "s") -> CannedCall:
    return CannedCall(scenario=scenario, tool=tool, arguments={}, payload=payload)


def _kinds(drifts: list[Drift]) -> set[str]:
    return {d.kind for d in drifts}


class TestValueDrift:
    def test_identical_payloads_do_not_drift(self) -> None:
        payload = {"consumer_group": "billing-consumer", "lag": 15000}
        assert compare(_call("get_consumer_lag", payload), payload) == []

    def test_the_lag_fixture_that_started_this(self) -> None:
        """The motivating defect, pinned as a test.

        ``consumer_lag_high`` asserted 1200 against a live 0 for months and
        the suite stayed green, because nothing compared the two.
        """
        canned = {"consumer_group": "worker-dispatcher", "lag": 1200}
        live = {"consumer_group": "worker-dispatcher", "lag": 0}
        drifts = compare(_call("get_consumer_lag", canned), live)
        assert [d.kind for d in drifts] == ["value"]
        assert drifts[0].path == "lag"
        assert drifts[0].canned == 1200
        assert drifts[0].live == 0

    def test_a_real_type_change_is_reported_as_type(self) -> None:
        drifts = compare(
            _call("get_consumer_lag", {"lag": 15000}),
            {"lag": "15000"},
        )
        assert _kinds(drifts) == {"type"}

    def test_null_against_a_number_is_a_value_disagreement_not_a_type_one(self) -> None:
        """`null` is a legal value here, and treating it as a type made the
        ledger flake: a freshly booted platform reports `lag: null` for
        worker-dispatcher until the 60s metrics loop first runs, then `0`.
        Same fixture defect, two ledger keys, depending on timing."""
        as_null = compare(_call("get_consumer_lag", {"lag": 15000}), {"lag": None})
        as_zero = compare(_call("get_consumer_lag", {"lag": 15000}), {"lag": 0})
        assert _kinds(as_null) == {"value"}
        assert [d.key for d in as_null] == [d.key for d in as_zero]

    def test_declared_volatile_field_is_checked_for_type_only(self) -> None:
        # get_redis_health.ping_latency_ms moves between any two honest reads.
        canned = {"ok": True, "ping_latency_ms": 0.5}
        live = {"ok": True, "ping_latency_ms": 12.7}
        assert compare(_call("get_redis_health", canned), live) == []

    def test_a_volatile_field_still_drifts_on_a_type_change(self) -> None:
        drifts = compare(
            _call("get_redis_health", {"ok": True, "ping_latency_ms": 0.5}),
            {"ok": True, "ping_latency_ms": "fast"},
        )
        assert _kinds(drifts) == {"type"}

    def test_lag_is_deliberately_not_volatile(self) -> None:
        # The value is the whole subject of the lag scenarios. If it were
        # declared volatile the guard would go quiet on its own motivating case.
        drifts = compare(
            _call("get_consumer_lag", {"lag": 1200}),
            {"lag": 0},
        )
        assert drifts, "lag must not be treated as a volatile gauge"


class TestShapeDrift:
    def test_field_the_platform_returns_and_no_fixture_models(self) -> None:
        drifts = compare(
            _call("get_deploy_history", {"total": 1, "entries": [{"version": "v1"}]}),
            {"total": 1, "entries": [{"version": "v1", "revision": "abc1234"}]},
        )
        assert [d.kind for d in drifts] == ["live_only_field"]
        assert drifts[0].path == "entries[].revision"

    def test_field_the_fixture_invents(self) -> None:
        drifts = compare(
            _call("get_consumer_lag", {"lag": 0, "invented": 1}),
            {"lag": 0},
        )
        assert [d.kind for d in drifts] == ["canned_only_field"]
        assert drifts[0].path == "invented"

    def test_nested_shape_is_compared(self) -> None:
        drifts = compare(
            _call("list_dlq_messages", {"items": [{"triage": {"summary": "x"}}]}),
            {"items": [{"triage": {"summary": "x", "root_cause_category": "bad_input"}}]},
        )
        assert [d.path for d in drifts] == ["items[].triage[].root_cause_category"]


class TestValueDomain:
    """Rows may differ; the values in them must still be ones the platform emits."""

    def test_enum_value_the_platform_never_emits(self) -> None:
        drifts = compare(
            _call("get_dag_state", {"nodes": [{"status": "succeeded"}]}),
            {"nodes": [{"status": "completed"}, {"status": "running"}]},
        )
        assert [d.kind for d in drifts] == ["not_live_reachable"]
        assert drifts[0].canned == "succeeded"

    def test_a_value_present_in_any_live_row_is_reachable(self) -> None:
        # The fixture's row need not be the live row — only its values must
        # be values the platform can produce.
        canned = {"items": [{"remediation_hint": "human_required"}]}
        live = {
            "items": [
                {"remediation_hint": "replay_safe"},
                {"remediation_hint": "human_required"},
            ]
        }
        assert compare(_call("list_dlq_messages", canned), live) == []

    def test_pinned_id_that_exists_in_no_live_row(self) -> None:
        drifts = compare(
            _call("list_dlq_messages", {"items": [{"id": "cccccccc-3333"}]}),
            {"items": [{"id": "fc8d2a03-23b3-5371-9acb-46443c73baa5"}]},
        )
        assert [d.kind for d in drifts] == ["not_live_reachable"]

    def test_unbounded_text_is_exempt(self) -> None:
        # Free prose has no value domain to be a member of.
        canned = {"items": [{"error_message": "stripe api timeout"}]}
        live = {"items": [{"error_message": "ConnectionTimeout: upstream SMTP relay"}]}
        assert compare(_call("list_dlq_messages", canned), live) == []

    def test_timestamps_inside_rows_are_exempt(self) -> None:
        canned = {"items": [{"created_at": "2026-07-30T10:00:00Z"}]}
        live = {"items": [{"created_at": "2026-08-14T03:35:00.019523Z"}]}
        assert compare(_call("list_dlq_messages", canned), live) == []

    def test_an_empty_live_list_is_one_finding(self) -> None:
        # `trace_investigation` pins a trace id the platform returns nothing
        # for. That is one fact — the rows do not exist — and reporting it
        # once beats reporting it per field the fixture happened to write.
        drifts = compare(
            _call("get_trace", {"jobs": [{"status": "failed"}]}),
            {"jobs": []},
        )
        assert [d.kind for d in drifts] == ["no_live_rows"]

    def test_null_canned_values_are_not_domain_checked(self) -> None:
        canned = {"items": [{"remediation_hint": None}]}
        live = {"items": [{"remediation_hint": "replay_safe"}]}
        assert compare(_call("list_dlq_messages", canned), live) == []


class TestCannedCallDerivation:
    """Arguments come from the scenario's own canned planner, not a hand table."""

    def test_every_shipped_canned_tool_has_derivable_arguments(self) -> None:
        # `canned_tool_responses` is keyed by tool name only, so the fixture
        # does not record the call it answers. If a scenario's canned planner
        # stops naming a tool the fixture covers, the drift check would probe
        # it with `{}` and quietly compare against the wrong world.
        scenarios = load_scenarios(_SCENARIOS_DIR)
        calls = canned_calls(scenarios)
        by_scenario = {s.name: s for s in scenarios}
        missing = [
            f"{c.scenario}:{c.tool}"
            for c in calls
            if not c.arguments and _expects_arguments(by_scenario[c.scenario], c.tool)
        ]
        assert missing == [], f"no derivable arguments for: {missing}"

    def test_sequenced_fixtures_yield_one_call_per_entry(self) -> None:

        scenarios = [s for s in load_scenarios(_SCENARIOS_DIR) if _is_sequenced(s)]
        assert scenarios, "no sequenced fixture in the suite — the case lost its subject"
        for scenario in scenarios:
            indices = [c.index for c in canned_calls([scenario])]
            assert max(indices) >= 1, f"{scenario.name}: sequenced entries collapsed"

    def test_error_fixtures_are_not_probed(self) -> None:
        from evals.graders.deterministic import ScenarioExpectation
        from evals.scenarios.schema import Scenario
        from incident_commander.agent.state import IncidentState
        from incident_commander.api.schemas import AlertPayload

        scenario = Scenario(
            name="err",
            alert=AlertPayload(source="s"),
            expectation=ScenarioExpectation(
                name="err", expected_terminal_state=IncidentState.ESCALATED
            ),
            canned_tool_responses={
                "get_consumer_lag": ToolResult(content=[], is_error=True),
            },
        )
        # A fixture that models a transport failure has no payload to compare.
        assert canned_calls([scenario]) == ()


def _expects_arguments(scenario: Any, tool: str) -> bool:
    """Does this tool take arguments at all in this scenario's canned planner?"""
    for step in scenario.canned_llm_responses.get("investigation_planner", []):
        action = step.get("next_action") or {}
        if action.get("tool_name") == tool:
            return bool(action.get("arguments"))
    return False


def _is_sequenced(scenario: Any) -> bool:
    return any(not isinstance(v, ToolResult) for v in scenario.canned_tool_responses.values())


class TestLedgerRatchet:
    def _drift(self, scenario: str = "s", path: str = "lag") -> Drift:
        return Drift(scenario=scenario, tool="get_consumer_lag", path=path, kind="value")

    def test_recorded_drift_is_not_new(self) -> None:
        drift = self._drift()
        new, stale = classify([drift], frozenset({drift.key}))
        assert new == () and stale == ()

    def test_unrecorded_drift_is_new(self) -> None:
        drift = self._drift()
        new, _ = classify([drift], frozenset())
        assert new == (drift,)

    def test_recorded_drift_that_stopped_occurring_is_stale(self) -> None:
        fixed = self._drift(path="cache_key").key
        new, stale = classify([], frozenset({fixed}))
        assert new == () and stale == (fixed,)

    def test_key_ignores_the_observed_values(self) -> None:
        """A wobbling gauge must not force a re-bless.

        If the key carried the values, every drift on a moving number would
        read as new drift, the ledger would be re-blessed constantly, and a
        genuinely new fixture defect would ride in on one of those blesses.
        """
        a = Drift(scenario="s", tool="t", path="p", kind="value", canned=1, live=2)
        b = Drift(scenario="s", tool="t", path="p", kind="value", canned=1, live=9)
        assert a.key == b.key

    def test_missing_ledger_is_the_strictest_reading(self, tmp_path: Path) -> None:
        assert load_ledger(tmp_path / "absent.json") == frozenset()

    def test_dump_then_load_round_trips(self, tmp_path: Path) -> None:
        path = tmp_path / "ledger.json"
        drifts = [self._drift(), self._drift(path="cache_key")]
        assert dump_ledger(drifts, path) == 2
        assert load_ledger(path) == {d.key for d in drifts}

    def test_dump_is_deterministic_and_deduplicated(self, tmp_path: Path) -> None:
        path = tmp_path / "ledger.json"
        drift = self._drift()
        twice = Drift(**{**drift.__dict__, "live": 99})
        assert dump_ledger([drift, twice, drift], path) == 1


class TestCommittedLedger:
    def test_the_committed_ledger_parses_and_is_not_empty(self) -> None:
        ledger = load_ledger()
        assert ledger, "the committed known-drift ledger is empty or unreadable"

    def test_every_entry_names_a_shipped_scenario(self) -> None:
        # A ledger line for a scenario that no longer exists is a suppression
        # nobody can ever discharge.
        shipped = {s.name for s in load_scenarios(_SCENARIOS_DIR)}
        orphans = sorted({key[0] for key in load_ledger()} - shipped)
        assert orphans == [], f"ledger records drift for missing scenarios: {orphans}"

    def test_ledger_file_is_sorted_and_unique(self) -> None:
        from evals.fixture_drift_ledger import LEDGER_PATH

        rows = json.loads(LEDGER_PATH.read_text())["known_drift"]
        keys = [(r["scenario"], r["tool"], r["path"], r["kind"]) for r in rows]
        assert keys == sorted(set(keys)), (
            "ledger is unsorted or has duplicates — regenerate with `make fixture-drift-bless`"
        )


class TestEmptyLiveList:
    """One finding, not one per field.

    The first CI run of this check hit an unseeded platform and turned a
    single fact — "the platform has no rows here" — into 64 per-field
    findings, which made the result depend on how wide each fixture happened
    to be rather than on what was wrong.
    """

    def test_empty_live_list_reports_one_drift(self) -> None:
        drifts = compare(
            _call(
                "list_dlq_messages",
                {"total": 2, "items": [{"id": "a", "type": "x", "retry_count": 1}]},
            ),
            {"total": 0, "items": []},
        )
        row_drifts = [d for d in drifts if d.path.startswith("items")]
        assert [d.kind for d in row_drifts] == ["no_live_rows"]
        assert row_drifts[0].path == "items[]"

    def test_an_empty_canned_list_against_live_rows_is_shape_drift_not_no_rows(self) -> None:
        drifts = compare(
            _call("list_dlq_messages", {"items": []}),
            {"items": [{"id": "a"}]},
        )
        assert "no_live_rows" not in {d.kind for d in drifts}

    def test_both_empty_is_agreement(self) -> None:
        assert compare(_call("search_traces", {"matches": []}), {"matches": []}) == []


class TestSeededPrecondition:
    """A check whose premise was never established reports that, not a result."""

    class _Stub:
        def __init__(self, payloads: dict[str, dict[str, Any]]) -> None:
            self.payloads = payloads
            self.calls: list[str] = []

        def post(self, url: str, **kwargs: Any) -> Any:  # noqa: ARG002
            name = kwargs["json"]["params"]["name"]
            self.calls.append(name)
            body = json.dumps(self.payloads.get(name, {}))
            return _Response({"result": {"content": [{"type": "text", "text": body}]}})

    def test_seeded_platform_passes(self) -> None:
        client = self._Stub({"list_dlq_messages": {"total": 4, "items": [{"id": "a"}]}})
        assert_seeded(client, "http://x/mcp", "tok")  # type: ignore[arg-type]

    def test_unseeded_platform_is_refused(self) -> None:
        client = self._Stub(
            {"list_dlq_messages": {"total": 0, "items": []}, "list_active_alerts": {"alerts": []}}
        )
        with pytest.raises(UnseededPlatformError, match="not\n?\\s*seeded|not seeded"):
            assert_seeded(client, "http://x/mcp", "tok")  # type: ignore[arg-type]

    def test_a_second_witness_can_carry_the_proof(self) -> None:
        # DLQ empty but alerts present is still a seeded world.
        client = self._Stub(
            {
                "list_dlq_messages": {"total": 0, "items": []},
                "list_active_alerts": {"alerts": [{"id": "a"}]},
            }
        )
        assert_seeded(client, "http://x/mcp", "tok")  # type: ignore[arg-type]
        assert client.calls == ["list_dlq_messages", "list_active_alerts"]


class _Response:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


class TestLedgerContext:
    """Not every recorded disagreement is work, and the number has to say so."""

    def test_default_is_that_it_is_work(self) -> None:
        assert context_of(("some_scenario", "some_tool", "some.path", "value")) == (
            FIXTURE_DEFECT,
            "",
        )

    def test_a_justified_entry_carries_its_reason(self) -> None:
        context, why = context_of(("consumer_lag_high", "get_consumer_lag", "lag", "value"))
        assert context == POST_FAULT
        assert "kill_consumer" in why

    def test_the_stale_cache_memory_fixture_is_still_a_defect(self) -> None:
        """The case that rules out inferring context from the scenario.

        `remediate_stale_cache_success` seeds a fault, so a blanket "fault
        scenarios get a pass on value drift" rule would absolve this. But
        `create_stale_cache` writes ONE Redis key; it cannot explain a
        fixture claiming 1.00G of memory in use against a live 1.60M. A rule
        would have deleted this from the work list. It stays work.
        """
        context, _ = context_of(
            ("remediate_stale_cache_success", "get_redis_health", "used_memory_human", "value")
        )
        assert context == FIXTURE_DEFECT

    def test_every_justification_names_a_real_ledger_entry(self) -> None:
        # A justification for something the check no longer reports is a
        # suppression nobody can discharge.
        recorded = load_ledger()
        orphans = sorted(key for key in _JUSTIFIED if key not in recorded)
        assert orphans == [], f"justifications with no matching ledger entry: {orphans}"

    def test_every_justification_names_a_shipped_scenario(self) -> None:
        shipped = {s.name for s in load_scenarios(_SCENARIOS_DIR)}
        orphans = sorted({key[0] for key in _JUSTIFIED} - shipped)
        assert orphans == []

    def test_the_burn_down_number_excludes_the_explained_ones(self) -> None:
        entries = load_entries()
        assert defect_count() == sum(1 for e in entries if e.is_defect)
        assert defect_count() < len(entries), "nothing is explained — the split is not working"

    def test_committed_ledger_counts_match_its_own_header(self) -> None:
        from evals.fixture_drift_ledger import LEDGER_PATH

        payload = json.loads(LEDGER_PATH.read_text())
        counts = payload["_counts"]
        assert counts["recorded"] == len(payload["known_drift"])
        assert counts[FIXTURE_DEFECT] == defect_count()
        assert counts["explained"] == counts["recorded"] - counts[FIXTURE_DEFECT]

    def test_the_original_array_format_still_parses(self, tmp_path: Path) -> None:
        # The committed ledger predates the annotated format; a checkout
        # mid-migration must not read as an empty ledger, which is the
        # strictest possible reading and would fail every run.
        path = tmp_path / "old.json"
        path.write_text(
            json.dumps({"known_drift": [["consumer_lag_high", "get_consumer_lag", "lag", "value"]]})
        )
        assert load_ledger(path) == {("consumer_lag_high", "get_consumer_lag", "lag", "value")}
        assert load_entries(path)[0].context == POST_FAULT
