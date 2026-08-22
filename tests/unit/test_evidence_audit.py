"""The cross-satisfiability audit, and the CI guard it powers.

``failed_traces_scan`` passed the trusted 26/26 live run without ever
calling ``search_traces``: the agent probed ``list_dlq_messages`` and
``get_deploy_history``, escalated, and the scenario's one evidence assert
— the substring ``trace`` — was satisfied because DLQ rows carry a
``trace_id`` field. The scenario exists to prove the agent scans failed
traces, and it proved nothing (context/INDEX.md, 2026-08-16 dress
rehearsal). The existing hygiene rule ("assert a field name, never a
value") permits the whole class, because field names recur across tools.

``evals/evidence_audit.py`` closes the class mechanically: a token in
``expected_evidence_contains`` that could appear in the recorded output
of two or more tools cannot prove which tool ran, and must instead be a
tool-scoped ``expected_evidence_fields`` assert. The suite-wide test at
the bottom is the lasting guard — the next scenario written with a
cross-satisfiable token fails here, in CI, instead of going green for
the wrong reason in a paid live run.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from evals.evidence_audit import (
    audit_evidence_scoping,
    reachable_field_names,
    rendered_canned_evidence,
    satisfiable_by,
)
from evals.graders.deterministic import GradeDimension, GradeReport, ScenarioExpectation, grade
from evals.scenarios.loader import load_scenarios
from evals.scenarios.schema import Scenario
from incident_commander.agent.state import EvidenceEntry, IncidentState, RunState
from incident_commander.tools.registry import (
    GetConsumerLagOutput,
    ListDlqMessagesOutput,
    SearchTracesOutput,
)

_SCENARIOS_DIR = Path(__file__).resolve().parents[2] / "evals" / "scenarios"

# ListDlqMessagesOutput-shaped, as `_summarize_probe` would record it — the
# rendering that satisfied `failed_traces_scan`'s old `trace` substring live.
_DLQ_WITH_TRACE_IDS = (
    '{"total":1,"items":[{"id":"aaaaaaaa-0000-0000-0000-000000000001",'
    '"type":"send_email","retry_count":3,"created_at":"2026-08-16T10:00:00Z",'
    '"trace_id":"trace-1a2b"}]}'
)


def _scenario(
    name: str,
    canned: dict[str, Any],
    tokens: tuple[str, ...] = (),
) -> Scenario:
    return Scenario.model_validate(
        {
            "name": name,
            "alert": {"source": "test", "severity": "high"},
            "canned_tool_responses": canned,
            "expectation": {
                "name": name,
                "expected_terminal_state": "escalated",
                "expected_evidence_contains": list(tokens),
            },
        }
    )


def _text_result(text: str, *, is_error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "is_error": is_error}


def _dim(report: GradeReport, dimension: GradeDimension) -> Any:
    return next(d for d in report.dimensions if d.dimension == dimension)


class TestReachableFieldNames:
    def test_top_level_fields_are_reachable(self) -> None:
        names = reachable_field_names(GetConsumerLagOutput)
        assert {"consumer_group", "lag", "cache_key"} <= names

    def test_nested_row_fields_are_reachable_through_lists(self) -> None:
        # The exact reach that made `trace` cross-satisfiable: trace_id lives
        # two levels down, inside items[].
        assert "trace_id" in reachable_field_names(ListDlqMessagesOutput)
        assert "trace_id" in reachable_field_names(SearchTracesOutput)


class TestRenderedCannedEvidence:
    def test_fixture_is_rendered_the_way_the_runtime_records_it(self) -> None:
        # extra fields are dropped by the output model (extra="ignore"), so a
        # raw-fixture substring match would over-approximate what evidence
        # can contain; the rendered corpus must not.
        raw = (
            '{"consumer_group":"g","lag":5,"cache_key":"kafka:consumer_lag:g",'
            '"internal_debug_note":"never-recorded"}'
        )
        corpus = rendered_canned_evidence([_scenario("s", {"get_consumer_lag": _text_result(raw)})])
        assert len(corpus["get_consumer_lag"]) == 1
        assert "never-recorded" not in corpus["get_consumer_lag"][0]
        assert '"lag":5' in corpus["get_consumer_lag"][0]

    def test_error_and_malformed_fixtures_are_excluded(self) -> None:
        # The runtime records both as bookkeeping escalations, never as tool
        # evidence — so neither can satisfy an evidence substring.
        corpus = rendered_canned_evidence(
            [
                _scenario(
                    "s",
                    {
                        "get_consumer_lag": _text_result(
                            '{"consumer_group":"g","lag":1,"cache_key":"k"}', is_error=True
                        ),
                        "search_traces": _text_result("{ not json"),
                    },
                )
            ]
        )
        assert corpus == {}

    def test_corpus_is_suite_wide_not_per_scenario(self) -> None:
        # The DLQ fixture that satisfied failed_traces_scan live belongs to
        # other scenarios entirely; the audit must see across files.
        other = _scenario("other", {"list_dlq_messages": _text_result(_DLQ_WITH_TRACE_IDS)})
        bare = _scenario("bare", {})
        corpus = rendered_canned_evidence([other, bare])
        assert "trace-1a2b" in corpus["list_dlq_messages"][0]


class TestAuditFlagsTheDefectClass:
    def test_the_failed_traces_scan_token_is_flagged(self) -> None:
        # The original defect, reconstructed: `trace` is satisfiable by the
        # DLQ listing (and get_trace) as well as by search_traces.
        scenarios = [
            _scenario(
                "failed_traces_scan_shape",
                {"search_traces": _text_result('{"matches":[]}')},
                tokens=("trace",),
            ),
            _scenario("other", {"list_dlq_messages": _text_result(_DLQ_WITH_TRACE_IDS)}),
        ]
        violations = audit_evidence_scoping(scenarios)
        assert len(violations) == 1
        assert "failed_traces_scan_shape" in violations[0]
        assert "'trace'" in violations[0]
        assert "list_dlq_messages" in violations[0]
        assert "search_traces" in violations[0]
        assert "expected_evidence_fields" in violations[0]

    def test_bookkeeping_prose_tokens_stay_legal(self) -> None:
        # "planner stop" targets a bookkeeping entry; no tool corpus carries
        # it, and zero satisfiers is not ambiguity.
        scenarios = [_scenario("noise_shape", {}, tokens=("planner stop",))]
        assert audit_evidence_scoping(scenarios) == []

    def test_tokens_unique_to_one_tool_stay_legal(self) -> None:
        # `keyspace` only appears in get_redis_health's field names; a
        # single-satisfier token still proves which tool ran.
        scenarios = [_scenario("redis_shape", {}, tokens=("keyspace",))]
        assert audit_evidence_scoping(scenarios) == []

    def test_satisfiable_by_reports_field_name_reach_without_fixtures(self) -> None:
        # Field-name skeletons alone are enough to flag: every rendering of a
        # tool's output carries its field names, fixtures or not.
        from incident_commander.tools.registry import TOOL_REGISTRY

        field_names = {
            name: reachable_field_names(spec.output_model) for name, spec in TOOL_REGISTRY.items()
        }
        tools = satisfiable_by("trace", field_names, {})
        assert {"get_trace", "list_dlq_messages", "search_traces"} <= tools


class TestFailedTracesScanRegression:
    """The live wrong-green, replayed against the shipped expectation."""

    def _shipped_scenario(self) -> Scenario:
        return next(s for s in load_scenarios(_SCENARIOS_DIR) if s.name == "failed_traces_scan")

    def _run(self, run_state: RunState, evidence: tuple[EvidenceEntry, ...]) -> RunState:
        return run_state.model_copy(update={"state": IncidentState.ESCALATED, "evidence": evidence})

    def test_a_run_that_never_called_search_traces_grades_red(
        self, run_state: RunState, now: datetime
    ) -> None:
        # Exactly the evidence the live run recorded: DLQ rows (with
        # trace_id) and deploy history — no search_traces call.
        evidence = (
            EvidenceEntry(
                tool_name="list_dlq_messages",
                arguments={},
                result_summary=_DLQ_WITH_TRACE_IDS,
                timestamp=now,
            ),
            EvidenceEntry(
                tool_name="get_deploy_history",
                arguments={},
                result_summary='{"total":0,"entries":[],"source":"deploy_markers"}',
                timestamp=now,
            ),
        )
        old_expectation = ScenarioExpectation(
            name="failed_traces_scan",
            expected_terminal_state=IncidentState.ESCALATED,
            expected_evidence_contains=("trace",),
        )
        run = self._run(run_state, evidence)
        # The pre-sweep expectation graded this run green — the wrong-reason
        # pass the 2026-08-16 dress rehearsal caught.
        assert _dim(grade(run, old_expectation), GradeDimension.EVIDENCE).passed is True
        # The shipped, tool-scoped expectation refuses it.
        shipped = self._shipped_scenario().expectation
        result = _dim(grade(run, shipped), GradeDimension.EVIDENCE)
        assert result.passed is False
        assert "search_traces" in result.detail

    def test_a_run_that_did_call_search_traces_grades_green(
        self, run_state: RunState, now: datetime
    ) -> None:
        scenario = self._shipped_scenario()
        rendering = rendered_canned_evidence([scenario])["search_traces"][0]
        evidence = (
            EvidenceEntry(
                tool_name="search_traces",
                arguments={},
                result_summary=rendering,
                timestamp=now,
            ),
        )
        run = self._run(run_state, evidence)
        assert _dim(grade(run, scenario.expectation), GradeDimension.EVIDENCE).passed is True


class TestShippedScenariosAreScoped:
    """The lasting guard: the committed suite carries no cross-satisfiable token."""

    def test_no_expected_evidence_token_is_cross_satisfiable(self) -> None:
        violations = audit_evidence_scoping(load_scenarios(_SCENARIOS_DIR))
        assert violations == [], "\n" + "\n".join(violations)
