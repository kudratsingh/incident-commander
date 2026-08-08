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
    """One audit row in the PLATFORM's real shape.

    Taken from v0.4.9's AuditEventEntry, not from what the guard happened
    to expect. The first version of these tests built {"items": [...]},
    a container key the platform never emits, so all four audit tests
    passed against a payload that could not occur and the guard was a
    no-op in production (F-004).
    """
    return {
        "id": "aud_" + tool[:6] + when.strftime("%H%M%S"),
        "action": "agent.tool_invoked",
        "principal_type": "service_account",
        "principal_id": "c12fd570-3ff4-42ce-a935-61086396df3c",
        "resource_type": None,
        "resource_id": None,
        "request_id": None,
        "created_at": when.isoformat(),
        "extra_data": {"tool_name": tool, "outcome": outcome},
    }


def _result(items: list[dict[str, Any]]) -> ToolResult:
    """The platform's envelope: {"total": N, "events": [...]}."""
    return ToolResult(
        content=[{"type": "text", "text": json.dumps({"total": len(items), "events": items})}]
    )


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


class TestAuditPayloadShape:
    """The guard must read the platform's shape, and fail closed on any other.

    F-004: `_parse_events` read `payload["items"]` while v0.4.9 emits
    `{"total": N, "events": [...]}`, so it returned zero events on every
    real call — no violations, no exception, "zero successful Tier-1
    actions", exit 0. The check built to catch the Run 001 token bug could
    not have caught it. Parsing nothing must fail like an unreadable audit,
    never like a clean one.
    """

    _SINCE = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)

    def _envelope(self, payload: dict[str, Any] | list[Any]) -> _Client:
        return _Client(ToolResult(content=[{"type": "text", "text": json.dumps(payload)}]))

    def test_reads_the_platform_events_key(self) -> None:
        row = _audit("restart_consumer_group", "success", self._SINCE + timedelta(minutes=1))
        client = self._envelope({"total": 1, "events": [row]})
        with pytest.raises(PrincipalGuardError, match="restart_consumer_group"):
            assert_no_tier1_successes(client, self._SINCE)

    def test_legacy_items_key_is_not_silently_accepted_as_empty(self) -> None:
        # The exact regression: a payload keyed "items" must NOT parse as
        # zero events and report a clean stage.
        row = _audit("mark_dlq_permanent", "success", self._SINCE + timedelta(minutes=1))
        with pytest.raises(PrincipalGuardError):
            assert_no_tier1_successes(self._envelope({"items": [row]}), self._SINCE)

    def test_unrecognized_shape_raises_rather_than_returning_empty(self) -> None:
        with pytest.raises(PrincipalGuardError, match="unrecognized payload shape"):
            assert_no_tier1_successes(self._envelope({"rows": []}), self._SINCE)

    def test_bare_list_payload_raises(self) -> None:
        with pytest.raises(PrincipalGuardError, match="unrecognized payload shape"):
            assert_no_tier1_successes(self._envelope([]), self._SINCE)

    def test_no_text_block_raises(self) -> None:
        client = _Client(ToolResult(content=[]))
        with pytest.raises(PrincipalGuardError, match="no text content block"):
            assert_no_tier1_successes(client, self._SINCE)

    def test_well_formed_empty_audit_is_a_genuine_pass(self) -> None:
        # total=0/events=[] is the platform saying "nothing happened" —
        # distinguishable from "we could not read the payload".
        assert (
            assert_no_tier1_successes(self._envelope({"total": 0, "events": []}), self._SINCE) == []
        )
