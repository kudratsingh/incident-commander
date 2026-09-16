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
from evals.scenarios.loader import load_scenario
from incident_commander.tools.mcp_client import MCPError, ToolResult


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
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
    }
    client = Mock()
    client.call_tool.side_effect = lambda tool, args: ToolResult(
        content=[{"type": "text", "text": json.dumps(payloads[tool])}]
    )
    settings = _eval_defaults().model_copy(update={"platform_smoke_token": SecretStr("read-test")})
    monkeypatch.setattr(audit, "Settings", lambda: settings)
    monkeypatch.setattr(audit, "make_client", lambda settings, *, token: client)
    monkeypatch.setattr(audit, "assert_read_only_principal", lambda client: None)
    monkeypatch.setattr(audit, "chaos_key_count", lambda: (0, "none"))
    monkeypatch.setattr(audit, "process_count", lambda: (0, "none"))
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
    def refuse(client: object) -> None:
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
