"""A dirty or unreadable world must never print a passing audit."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest
from pydantic import SecretStr

from evals import dossier
from evals import world_audit as audit
from evals.guards import PrincipalGuardError
from evals.runner import _eval_defaults
from evals.scenarios.loader import ScenarioLoadError, load_scenario
from evals.scenarios.schema import PreconditionField, PreconditionProbe
from incident_commander.tools.mcp_client import MCPError, ToolResult

#: The moment the canned SLO readings below were taken; the budget clears 24 h after it at most.
_MEASURED_AT = "2026-10-10T11:30:05.123456Z"
_API_LATENCY = (
    "api_latency_db_query",
    "api_latency_downstream",
    "api_latency_healthy_control",
    "api_latency_redis",
)


def _objective(objective_id: str, failed: int, total: int, target: float) -> dict[str, object]:
    """One objective row as get_slo_status returns it, with the platform's own arithmetic."""
    rate = failed / total if total else 0.0
    budget = 100.0 if not total else max(-100.0, (1 - rate / (1 - target)) * 100)
    return {
        "id": objective_id,
        "target": target,
        "window_hours": 24,
        "total": total,
        "failed": failed,
        "current_success_rate": 1 - rate,
        "budget_remaining_pct": budget,
        "burn_rate": rate / (1 - target),
        "healthy": 1 - rate >= target,
        "fast_burn": rate / (1 - target) >= 14.4,
    }


def _slo_reading(
    *, dispatch_failed: int, completion_failed: int = 0, measured_at: object = _MEASURED_AT
) -> dict[str, object]:
    """A get_slo_status answer: the completion objective first, as the platform declares them."""
    return {
        "measured_at": measured_at,
        "objectives": [
            _objective("job_completion_rate", completion_failed, 253, 0.99),
            _objective("job_dispatch_latency", dispatch_failed, 271, 0.95),
        ],
        "total": 2,
        "fast_burn_threshold": 14.4,
    }


#: The real corpus loader, kept before any fixture swaps it for the session's cached copy.
_LOAD_CORPUS_DEMANDS = audit._corpus_demands


@pytest.fixture(scope="session")
def corpus_demands() -> tuple[audit.BudgetDemand, ...]:
    """The corpus's budget checks, loaded once rather than on every ``main()`` call."""
    demands, warnings = _LOAD_CORPUS_DEMANDS()
    assert not warnings
    return demands


@pytest.fixture
def world(
    monkeypatch: pytest.MonkeyPatch, corpus_demands: tuple[audit.BudgetDemand, ...]
) -> dict[str, object]:
    payloads: dict[str, object] = {
        "list_dlq_messages": {
            "total": 4,
            "items": [
                {"id": f"row-{i}", "remediation_hint": "replay_safe", "fenced_at": None}
                for i in range(4)
            ],
        },
        "list_active_alerts": {"total": 3},
        "get_consumer_lag": {"lag": 0, "lag_known": True},
        "get_cache_key_info": {"exists": True, "size": 120},
        "get_dag_state": {"paused": False, "nodes": [{"status": "completed"}]},
        "search_traces": {
            "matches": [
                {"trace_id": "0e24ca29-1d47-57e9-b898-4d79bb6da981"},
                {"trace_id": "edeeb994-56d2-53e6-88fd-8af47e695dbc"},
            ]
        },
        "get_slo_status": _slo_reading(dispatch_failed=0),
    }
    client = Mock()
    # ``**kwargs`` because every read now goes through ``LabProbeClient``, which adds
    # the lab label and the credential to each call (WO-R3-335); the answers are the
    # same, and ``tests/unit/test_lab_probe.py`` is where the label itself is asserted.
    client.call_tool.side_effect = lambda tool, args, **_kwargs: ToolResult(
        content=[{"type": "text", "text": json.dumps(payloads[tool])}]
    )
    settings = _eval_defaults().model_copy(update={"platform_smoke_token": SecretStr("read-test")})
    monkeypatch.setattr(audit, "Settings", lambda: settings)
    monkeypatch.setattr(audit, "make_client", lambda settings, *, token: client)
    monkeypatch.setattr(audit, "assert_read_only_principal", lambda client, **_kwargs: None)
    monkeypatch.setattr(audit, "chaos_key_count", lambda: (0, "none"))
    monkeypatch.setattr(audit, "process_count", lambda: (0, "none"))
    monkeypatch.setattr(audit, "_corpus_demands", lambda: (corpus_demands, []))
    return payloads


def test_clean_world_prints_rows_and_passes(
    world: dict[str, object], capsys: pytest.CaptureFixture[str]
) -> None:
    assert audit.main(["--roots", "root-a,root-b"]) == 0
    output = capsys.readouterr().out
    assert "WORLD AUDIT: PASS" in output
    assert '"id": "row-3"' in output
    assert "chain root-a paused" in output
    assert "chain root-b paused" in output


@pytest.mark.parametrize(
    ("tool", "payload", "label"),
    [
        ("list_dlq_messages", {"total": 5, "items": []}, "DLQ total"),
        ("list_dlq_messages", {"total": 4, "items": []}, "DLQ listing complete"),
        ("list_dlq_messages", {"total": 4, "items": [{"id": "a"}] * 4}, "DLQ unclassified rows"),
        (
            "list_dlq_messages",
            {"total": 4, "items": [{"remediation_hint": "safe", "fenced_at": "now"}] * 4},
            "DLQ fenced rows",
        ),
        ("list_active_alerts", {"total": 6}, "active alerts"),
        ("get_consumer_lag", {"lag": 0, "lag_known": False}, "lag_known"),
        ("get_consumer_lag", {"lag": False, "lag_known": True}, "worker-dispatcher lag"),
        ("get_cache_key_info", {"exists": False, "size": 120}, "hot_set exists"),
        ("get_cache_key_info", {"exists": True, "size": 90}, "hot_set size"),
        ("get_dag_state", {"paused": True, "nodes": [1]}, "chain root paused"),
        ("get_dag_state", {"paused": False, "nodes": []}, "chain root present"),
        ("list_active_alerts", None, "active alerts"),
    ],
)
def test_dirty_world_exits_nonzero_and_names_failure(
    world: dict[str, object],
    tool: str,
    payload: object,
    label: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    world[tool] = payload
    assert audit.main(["--roots", "root"]) == 1
    assert f"[FAIL] {label}" in capsys.readouterr().out


def test_same_baseline_implementation_and_reading_types() -> None:
    assert dossier.audit_baseline is audit.audit_baseline
    assert dossier.BaselineLine is audit.BaselineLine
    assert dossier.chaos_key_count is audit.chaos_key_count
    assert dossier.read is audit.read


@pytest.mark.parametrize(
    "payload",
    [
        {"matches": []},  # aged fixtures: no rows remain inside since_hours=1
        {"matches": [{"trace_id": "0e24ca29-1d47-57e9-b898-4d79bb6da981"}]},
        {"matches": [{"trace_id": "unrelated-a"}, {"trace_id": "unrelated-b"}]},
        {"matches": [{"trace_id": "0e24ca29-1d47-57e9-b898-4d79bb6da981"}] * 2},
        {"matches": [{"trace_id": []}]},
        {"matches": [None]},
        {"matches": "unreadable"},
        {},
        None,
    ],
)
def test_stale_or_unreadable_traces_fail_with_reset_instruction(
    world: dict[str, object], payload: object, capsys: pytest.CaptureFixture[str]
) -> None:
    world["search_traces"] = payload
    assert audit.main([]) == 1
    output = capsys.readouterr().out
    assert "[FAIL] seeded failed traces inside probe window" in output
    assert "make eval-reset" in output


def test_fresh_traces_pass_in_shared_dossier_baseline(world: dict[str, object]) -> None:
    client = Mock()
    client.call_tool.side_effect = lambda tool, args: ToolResult(
        content=[{"type": "text", "text": json.dumps(world[tool])}]
    )
    lines = dossier.audit_baseline(client)
    line = next(line for line in lines if line.name == "seeded failed traces inside probe window")
    assert line.passed
    client.call_tool.assert_any_call("search_traces", {"status": "failed", "since_hours": 1})
    world["search_traces"] = {"matches": []}
    lines = dossier.audit_baseline(client)
    line = next(line for line in lines if line.name == "seeded failed traces inside probe window")
    assert not line.passed
    assert "make eval-reset" in line.observed


@pytest.mark.parametrize("transport_error", [False, True])
def test_failed_trace_read_cannot_pass(world: dict[str, object], transport_error: bool) -> None:
    def reply(tool: str, args: object) -> ToolResult:
        if tool == "search_traces" and transport_error:
            raise MCPError(-32000, "trace read failed")
        return ToolResult(
            content=[{"type": "text", "text": json.dumps(world[tool])}],
            is_error=tool == "search_traces",
        )

    client = Mock()
    client.call_tool.side_effect = reply
    lines = audit.audit_baseline(client)
    line = next(line for line in lines if line.name == "seeded failed traces inside probe window")
    assert not line.passed
    assert "make eval-reset" in line.observed


def test_trace_baseline_matches_scenario_probe_and_fixture() -> None:
    scenario = load_scenario(
        Path(__file__).resolve().parents[2] / "evals/scenarios/failed_traces_scan.yaml"
    )
    result = scenario.canned_tool_responses["search_traces"]
    assert isinstance(result, ToolResult)
    payload = json.loads(result.content[0]["text"])
    assert {row["trace_id"] for row in payload["matches"]} == audit.BASELINE_FAILED_TRACE_IDS
    planner = scenario.canned_llm_responses["investigation_planner"]
    assert isinstance(planner, list)
    assert planner[0]["next_action"]["arguments"] == {
        "status": "failed",
        "since_hours": audit.TRACE_PROBE_WINDOW_HOURS,
    }


def test_baseline_constants_match_runbook() -> None:
    text = (Path(__file__).resolve().parents[2] / "docs/runbook.md").read_text()
    for label, expected in (
        ("DLQ total", audit.BASELINE_DLQ_TOTAL),
        ("Active alerts", audit.BASELINE_ACTIVE_ALERTS),
        ("Redis `chaos:*` keys", audit.BASELINE_CHAOS_KEYS),
        ("DLQ unclassified rows", audit.BASELINE_UNCLASSIFIED),
        ("DLQ fenced rows", audit.BASELINE_FENCED),
        ("`worker-dispatcher` lag", audit.BASELINE_LAG),
        ("`hot_set` size", audit.BASELINE_HOT_SET_SIZE),
        ("`traffic_loop` / `evals.runner` processes", audit.BASELINE_PROCESSES),
    ):
        match = re.search(r"\| " + re.escape(label) + r" \| \*\*(\d+)\*\*", text)
        assert match is not None, label
        assert int(match[1]) == expected


def test_write_principal_fails_before_audit(
    world: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(client: object, **_kwargs: object) -> None:
        raise PrincipalGuardError("write token")

    monkeypatch.setattr(audit, "assert_read_only_principal", refuse)
    spy = Mock(side_effect=AssertionError("must not read on an unverified principal"))
    monkeypatch.setattr(audit, "audit_world", spy)
    assert audit.main([]) == 3
    spy.assert_not_called()


def test_missing_read_token_never_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(audit, "Settings", _eval_defaults)
    spy = Mock(side_effect=AssertionError("must not construct a client"))
    monkeypatch.setattr(audit, "make_client", spy)
    assert audit.main([]) == 3
    spy.assert_not_called()


@pytest.mark.parametrize("scanner", ["chaos_key_count", "process_count"])
@pytest.mark.parametrize("count", [None, 1])
def test_failed_or_dirty_scans_fail(
    world: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    scanner: str,
    count: int | None,
) -> None:
    monkeypatch.setattr(audit, scanner, lambda: (count, "scan result"))
    assert audit.main([]) == 1


@pytest.mark.parametrize("scanner", ["chaos_key_count", "process_count"])
def test_subprocess_error_cannot_look_like_zero(
    monkeypatch: pytest.MonkeyPatch,
    scanner: str,
) -> None:
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 2, "", "failed")
    )
    assert getattr(audit, scanner)()[0] is None


# The objectives read (WO-R3-367, INC-007): a budget under 100 WARNs and never fails the audit.


def _summary(output: str) -> str:
    return next(line for line in output.splitlines() if line.startswith("WORLD AUDIT:"))


def test_whole_budgets_print_one_row_each_and_no_warning(
    world: dict[str, object], capsys: pytest.CaptureFixture[str]
) -> None:
    assert audit.main([]) == 0
    output = capsys.readouterr().out
    assert "[INFO] SLO job_completion_rate: 0 failed of 253, budget_remaining_pct 100" in output
    assert "[INFO] SLO job_dispatch_latency: 0 failed of 271, budget_remaining_pct 100" in output
    assert "healthy True" in output
    assert "[WARN]" not in output
    assert _summary(output) == "WORLD AUDIT: PASS"


def test_spent_dispatch_budget_warns_naming_the_family_and_the_clear_time(
    world: dict[str, object], capsys: pytest.CaptureFixture[str]
) -> None:
    world["get_slo_status"] = _slo_reading(dispatch_failed=28)
    assert audit.main([]) == 0
    output = capsys.readouterr().out
    assert "[PASS] DLQ total" in output
    assert "[INFO] SLO job_dispatch_latency: 28 failed of 271, budget_remaining_pct -100" in output
    assert "healthy False" in output
    warnings = [line for line in output.splitlines() if line.startswith("[WARN]")]
    assert len(warnings) == 1
    [warning] = warnings
    assert warning.startswith("[WARN] SLO job_dispatch_latency budget:")
    assert "api_latency (" + ", ".join(_API_LATENCY) + ")" in warning
    # No contract tool says when the late jobs were created, so the clear time is the latest
    # it can be: the reading's own moment plus the 24 h window.
    assert "no later than 2026-10-11 11:30 UTC" in warning
    assert _MEASURED_AT in warning
    summary = _summary(output)
    assert summary.startswith("WORLD AUDIT: PASS")
    assert "WARN" in summary
    assert "api_latency (" in summary
    assert "no later than 2026-10-11 11:30 UTC" in summary


def test_spent_completion_budget_names_only_the_control_that_reads_it(
    world: dict[str, object], capsys: pytest.CaptureFixture[str]
) -> None:
    world["get_slo_status"] = _slo_reading(dispatch_failed=0, completion_failed=1)
    assert audit.main([]) == 0
    [warning] = [line for line in capsys.readouterr().out.splitlines() if "[WARN]" in line]
    assert warning.startswith("[WARN] SLO job_completion_rate budget:")
    assert "api_latency (api_latency_healthy_control)" in warning


@pytest.mark.parametrize("measured_at", [None, "not a time"])
def test_unreadable_clock_still_warns_with_the_rule(
    world: dict[str, object], capsys: pytest.CaptureFixture[str], measured_at: object
) -> None:
    world["get_slo_status"] = _slo_reading(dispatch_failed=3, measured_at=measured_at)
    assert audit.main([]) == 0
    [warning] = [line for line in capsys.readouterr().out.splitlines() if "[WARN]" in line]
    assert "24 h after the last failure in its window" in warning
    assert "no later than" not in warning


@pytest.mark.parametrize("failure", ["transport", "is_error", "no objectives"])
def test_a_failed_slo_read_says_so_and_the_rest_still_reports(
    world: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: str,
) -> None:
    client = Mock()
    monkeypatch.setattr(audit, "make_client", lambda settings, *, token: client)

    def reply(tool: str, args: object, **_kwargs: object) -> ToolResult:
        if tool == "get_slo_status":
            if failure == "transport":
                raise MCPError(-32000, "slo read failed")
            payload = {"measured_at": _MEASURED_AT} if failure == "no objectives" else {}
            return ToolResult(
                content=[{"type": "text", "text": json.dumps(payload)}],
                is_error=failure == "is_error",
            )
        return ToolResult(content=[{"type": "text", "text": json.dumps(world[tool])}])

    client.call_tool.side_effect = reply
    assert audit.main(["--roots", "root-a"]) == 0
    output = capsys.readouterr().out
    assert "[PASS] DLQ total" in output
    assert "[PASS] chain root-a paused" in output
    assert '"id": "row-3"' in output
    [warning] = [line for line in output.splitlines() if "[WARN]" in line]
    assert warning.startswith("[WARN] SLO budgets: unreadable")
    assert "api_latency (" + ", ".join(_API_LATENCY) + ")" in warning
    assert "WARN" in _summary(output)


def test_a_warning_never_hides_a_failed_baseline(
    world: dict[str, object], capsys: pytest.CaptureFixture[str]
) -> None:
    world["list_active_alerts"] = {"total": 6}
    world["get_slo_status"] = _slo_reading(dispatch_failed=28)
    assert audit.main([]) == 1
    summary = _summary(capsys.readouterr().out)
    assert summary.startswith("WORLD AUDIT: FAIL")
    assert "WARN" in summary


def test_an_unreadable_corpus_warns_and_the_audit_still_runs(
    world: dict[str, object], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def broken(_directory: Path) -> list[object]:
        raise ScenarioLoadError(Path("evals/scenarios/x.yaml"), "half-edited")

    monkeypatch.setattr(audit, "load_scenarios", broken)
    monkeypatch.setattr(audit, "_corpus_demands", _LOAD_CORPUS_DEMANDS)
    world["get_slo_status"] = _slo_reading(dispatch_failed=28)
    assert audit.main([]) == 0
    output = capsys.readouterr().out
    assert "[WARN] scenario corpus: unreadable" in output
    assert "[WARN] SLO job_dispatch_latency budget:" in output


def test_the_families_come_from_the_corpus_preconditions(
    corpus_demands: tuple[audit.BudgetDemand, ...],
) -> None:
    demands = corpus_demands
    by_objective: dict[str, set[tuple[str, str]]] = {}
    for objective in ("job_dispatch_latency", "job_completion_rate"):
        row = {"id": objective}
        by_objective[objective] = {(d.family, d.scenario) for d in demands if d.reads(row)}
    assert by_objective["job_dispatch_latency"] == {("api_latency", name) for name in _API_LATENCY}
    assert by_objective["job_completion_rate"] == {("api_latency", "api_latency_healthy_control")}


def test_a_precondition_the_reading_still_meets_is_not_named() -> None:
    lenient = audit.BudgetDemand(
        scenario="lenient",
        family="api_latency",
        probe=PreconditionProbe(
            tool="get_slo_status",
            expect=(PreconditionField(path="objectives[].budget_remaining_pct", at_least=0.0),),
        ),
    )
    client = Mock()
    client.call_tool.return_value = ToolResult(
        content=[{"type": "text", "text": json.dumps(_slo_reading(dispatch_failed=1))}]
    )
    lines = audit.audit_slo_budgets(client, (lenient,))
    [warning] = [line for line in lines if line.warn]
    assert "lenient" not in warning.detail
    assert "none" in warning.detail
