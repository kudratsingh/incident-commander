"""Point-of-use principal guards (Run 001 stage-1 token bug)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from evals.guards import (
    PrincipalGuardError,
    assert_no_tier1_successes,
    assert_read_only_principal,
)
from incident_commander.tools.mcp_client import MCPError, ToolResult


class _Client:
    def __init__(self, behavior: ToolResult | Exception) -> None:
        self._behavior = behavior
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call_tool(
        self, name: str, arguments: Any, *, timeout_seconds: float | None = None
    ) -> ToolResult:
        self.calls.append((name, dict(arguments)))
        if isinstance(self._behavior, Exception):
            raise self._behavior
        return self._behavior


class TestReadOnlyGuard:
    def test_scope_refusal_passes(self) -> None:
        client = _Client(MCPError(-32002, "missing required scope: actions:execute"))
        assert_read_only_principal(client)  # no raise
        assert client.calls[0][0] == "mark_dlq_permanent"

    def test_validation_error_means_write_scope_and_fails(self) -> None:
        # The exact signature of the Run 001 bug: the handler got PAST the
        # scope check and rejected our deliberately invalid arguments.
        client = _Client(MCPError(-32602, "Invalid params: job_id is not a valid UUID"))
        with pytest.raises(PrincipalGuardError, match="actions:execute"):
            assert_read_only_principal(client)

    def test_success_fails_loudest(self) -> None:
        client = _Client(ToolResult(content=[{"type": "text", "text": "{}"}]))
        with pytest.raises(PrincipalGuardError, match="write scope"):
            assert_read_only_principal(client)

    def test_other_scope_code_still_fails(self) -> None:
        client = _Client(MCPError(-32000, "transport error"))
        with pytest.raises(PrincipalGuardError):
            assert_read_only_principal(client)


def _audit(tool: str, outcome: str, when: datetime) -> dict[str, Any]:
    return {
        "created_at": when.isoformat(),
        "principal_id": "abc",
        "extra_data": {"tool_name": tool, "outcome": outcome},
    }


def _result(items: list[dict[str, Any]]) -> ToolResult:
    return ToolResult(content=[{"type": "text", "text": json.dumps({"items": items})}])


class TestPostStageAudit:
    def _since(self) -> datetime:
        return datetime(2026, 8, 7, 12, 0, tzinfo=UTC)

    def test_clean_window_passes(self) -> None:
        since = self._since()
        client = _Client(
            _result(
                [
                    _audit("get_consumer_lag", "success", since + timedelta(minutes=1)),
                    _audit("restart_consumer_group", "unauthorized", since + timedelta(minutes=2)),
                ]
            )
        )
        assert assert_no_tier1_successes(client, since) == []

    def test_tier1_success_in_window_fails(self) -> None:
        since = self._since()
        client = _Client(
            _result([_audit("mark_dlq_permanent", "success", since + timedelta(minutes=3))])
        )
        with pytest.raises(PrincipalGuardError, match="mark_dlq_permanent"):
            assert_no_tier1_successes(client, since)

    def test_older_tier1_success_is_ignored(self) -> None:
        # Pre-existing state is not this stage's doing — the exact
        # disambiguation the audit query was added to make.
        since = self._since()
        client = _Client(
            _result([_audit("mark_dlq_permanent", "success", since - timedelta(hours=2))])
        )
        assert assert_no_tier1_successes(client, since) == []


class TestFailsClosed:
    """An unverified control is an unmet precondition, never a warning."""

    def test_transport_error_fails_closed(self) -> None:
        client = _Client(MCPError(-32000, "transport error after 3 attempts"))
        with pytest.raises(PrincipalGuardError):
            assert_read_only_principal(client)

    def test_unexpected_exception_type_fails_closed(self) -> None:
        client = _Client(RuntimeError("something nobody predicted"))
        with pytest.raises(PrincipalGuardError, match="Failing closed"):
            assert_read_only_principal(client)

    def test_unreadable_audit_fails_closed(self) -> None:
        client = _Client(MCPError(-32000, "audit read blew up"))
        with pytest.raises(PrincipalGuardError, match="not a clean stage"):
            assert_no_tier1_successes(client, datetime(2026, 8, 7, tzinfo=UTC))

    def test_unparseable_audit_response_fails_closed(self) -> None:
        client = _Client(ToolResult(content=[{"type": "text", "text": "not json"}]))
        with pytest.raises(PrincipalGuardError):
            assert_no_tier1_successes(client, datetime(2026, 8, 7, tzinfo=UTC))


class TestNoOptOut:
    """The guard must have no bypass — that's the whole point of F-001."""

    def test_guard_is_derived_from_platform_reachability_not_a_flag(self) -> None:
        from pathlib import Path

        runner = (Path(__file__).resolve().parents[2] / "evals" / "runner.py").read_text()
        assert "guard_required = smoke and not _is_offline_placeholder" in runner, (
            "the guard condition must derive from whether a real platform is "
            "reachable, not from the --live flag (a second mechanism deciding "
            "whether the first needs checking)"
        )
        assert "if smoke and live:" not in runner

    def test_no_bypass_switch_exists(self) -> None:
        from pathlib import Path

        repo = Path(__file__).resolve().parents[2]
        sources = (repo / "evals" / "runner.py").read_text() + (
            repo / "evals" / "guards.py"
        ).read_text()
        for bypass in ("SKIP_GUARD", "skip_guard", "no_guard", "disable_guard", "--no-guard"):
            assert bypass not in sources, f"bypass switch {bypass!r} must not exist"
