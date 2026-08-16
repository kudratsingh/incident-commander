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
    assert_write_capable_principal,
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


_OUR_AGENT_SA = "c12fd570-3ff4-42ce-a935-61086396df3c"
_OUR_SMOKE_SA = "9b21a4de-71c0-4d0e-b0a1-0f0f5a2c8e11"
_SOMEONE_ELSES_SA = "00000000-dead-beef-0000-1111deadbeef"


def _audit(
    tool: str,
    outcome: str,
    when: datetime,
    principal_id: str = _OUR_AGENT_SA,
) -> dict[str, Any]:
    """One audit row in the PLATFORM's real shape.

    Taken from v0.4.9's AuditEventEntry, not from what the guard happened
    to expect. The first version of these tests built {"items": [...]},
    a container key the platform never emits, so all four audit tests
    passed against a payload that could not occur and the guard was a
    no-op in production (F-004).
    """
    return {
        "id": "aud_" + tool[:6] + when.strftime("%H%M%S%f"),
        "action": "agent.tool_invoked",
        "principal_type": "service_account",
        "principal_id": principal_id,
        "resource_type": None,
        "resource_id": None,
        "request_id": None,
        "created_at": when.isoformat(),
        "extra_data": {"tool_name": tool, "outcome": outcome},
    }


def _result(items: list[dict[str, Any]], total: int | None = None) -> ToolResult:
    """The platform's envelope: {"total": N, "events": [...]}.

    ``total`` is the platform's COUNT over the SAME filter with no limit
    applied (repositories/audit.py:86), while ``events`` is capped at
    ``limit`` — so a caller-supplied ``total`` larger than ``len(items)``
    is the server itself saying it withheld rows.
    """
    return ToolResult(
        content=[
            {
                "type": "text",
                "text": json.dumps(
                    {"total": len(items) if total is None else total, "events": items}
                ),
            }
        ]
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

    def test_row_with_unparseable_created_at_fails_closed(self) -> None:
        # Covered by the typed parse since F-004: AuditEventEntry declares
        # created_at as a required datetime, so a row the guard cannot place
        # in time can never reach the window comparison and be silently
        # dropped from it. Pinned here so a future loosening of the model
        # (created_at: str | None) cannot quietly restore that hole.
        row = _audit("mark_dlq_permanent", "success", self._SINCE + timedelta(minutes=1))
        row["created_at"] = "not-a-timestamp"
        with pytest.raises(PrincipalGuardError, match="unrecognized payload shape"):
            assert_no_tier1_successes(self._envelope({"total": 1, "events": [row]}), self._SINCE)


class TestSaturatedAuditPage:
    """A-13: one page of at most 200 rows is not a scan of the window.

    ``list_audit_events`` exposes no offset and no created_after (the
    handler hardcodes offset=0, limit is capped at 200), so the guard
    cannot page. Rows come back created_at DESC, which means the ONLY
    proof the whole [since, now] window was seen is that the oldest row on
    the page predates ``since``. Without that proof the guard must fail
    closed: a busy stage otherwise pushes its Tier-1 successes past row
    200 and the assertion that replaced the manual F-001 query reports a
    clean stage.
    """

    _SINCE = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)

    def _page(self, count: int, *, oldest_before_since: bool = False) -> list[dict[str, Any]]:
        rows = [
            _audit("get_consumer_lag", "success", self._SINCE + timedelta(seconds=i + 1))
            for i in range(count)
        ]
        if oldest_before_since:
            rows[-1] = _audit("get_consumer_lag", "success", self._SINCE - timedelta(minutes=5))
        return rows

    def test_full_page_still_inside_the_window_fails_closed(self) -> None:
        # Exactly the A-13 blindness: 200 in-window rows, none of them a
        # Tier-1 success, so the violations list is empty — but rows 201+
        # are unreachable and may hold the successes the guard exists for.
        client = _Client(_result(self._page(200), total=417))
        with pytest.raises(PrincipalGuardError, match="saturated"):
            assert_no_tier1_successes(client, self._SINCE)

    def test_full_page_whose_oldest_row_predates_since_is_a_genuine_pass(self) -> None:
        # The window WAS fully scanned: the page reaches back past `since`,
        # so nothing in-window can be hiding behind it. Saturation must not
        # false-fail a covered window.
        client = _Client(_result(self._page(200, oldest_before_since=True), total=417))
        assert assert_no_tier1_successes(client, self._SINCE) == []

    def test_server_reported_total_above_the_page_fails_closed(self) -> None:
        # The platform's `total` is a COUNT over the same filter with no
        # limit (repositories/audit.py:86), so total > len(events) is the
        # server itself saying it withheld rows — honest even if the page
        # cap ever moves off 200.
        client = _Client(_result(self._page(3), total=64))
        with pytest.raises(PrincipalGuardError, match="inconclusive"):
            assert_no_tier1_successes(client, self._SINCE)

    def test_short_page_is_conclusive(self) -> None:
        # total == len(events) and the page is not at the cap: every row
        # matching the filter came back, so the window is fully covered
        # even though no row predates `since`.
        client = _Client(_result(self._page(3)))
        assert assert_no_tier1_successes(client, self._SINCE) == []

    def test_saturated_page_still_names_the_violations_it_can_see(self) -> None:
        # Inconclusive outranks clean, but a violation already visible on
        # the page is the more actionable signal and must not be swallowed
        # by the saturation message.
        rows = self._page(199)
        rows.append(_audit("mark_dlq_permanent", "success", self._SINCE + timedelta(minutes=1)))
        client = _Client(_result(rows, total=900))
        with pytest.raises(PrincipalGuardError, match="mark_dlq_permanent"):
            assert_no_tier1_successes(client, self._SINCE)

    def test_the_guard_asks_for_the_page_size_it_checks_against(self) -> None:
        # The saturation check compares against a constant; if the request
        # asked for a different limit the comparison would be meaningless.
        client = _Client(_result([]))
        assert assert_no_tier1_successes(client, self._SINCE) == []
        assert client.calls[0][1]["limit"] == 200


class TestSelfOwnedPrincipals:
    """A-13's other half: a shared platform's other tenants are not us.

    Any service account's in-window Tier-1 success currently fails the
    stage, so a neighbouring principal's legitimate remediation is a false
    exit 5. The filter set is {agent SA, smoke SA} — NOT the smoke SA
    alone: the F-001 failure mode this guard was built for is the stage
    silently running under the FULL agent token, and those rows carry the
    AGENT principal_id.
    """

    _SINCE = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)
    _OURS = frozenset({_OUR_AGENT_SA, _OUR_SMOKE_SA})

    def _tier1_by(self, principal_id: str) -> _Client:
        return _Client(
            _result(
                [
                    _audit(
                        "mark_dlq_permanent",
                        "success",
                        self._SINCE + timedelta(minutes=2),
                        principal_id=principal_id,
                    )
                ]
            )
        )

    def test_foreign_principal_does_not_fail_our_stage(self) -> None:
        client = self._tier1_by(_SOMEONE_ELSES_SA)
        assert assert_no_tier1_successes(client, self._SINCE, principal_ids=self._OURS) == []

    def test_our_agent_principal_still_fails_the_stage(self) -> None:
        # The F-001 shape: the "read-scoped" stage wrote under the full
        # agent token. Filtering to the smoke SA alone would make the guard
        # blind to its own reason for existing.
        client = self._tier1_by(_OUR_AGENT_SA)
        with pytest.raises(PrincipalGuardError, match="mark_dlq_permanent"):
            assert_no_tier1_successes(client, self._SINCE, principal_ids=self._OURS)

    def test_our_smoke_principal_still_fails_the_stage(self) -> None:
        client = self._tier1_by(_OUR_SMOKE_SA)
        with pytest.raises(PrincipalGuardError, match="mark_dlq_permanent"):
            assert_no_tier1_successes(client, self._SINCE, principal_ids=self._OURS)

    def test_unconfigured_fails_on_any_service_account(self) -> None:
        # Deliberately over-broad default: without the .env ids we cannot
        # tell ours from theirs, and over-broad is the safe side.
        client = self._tier1_by(_SOMEONE_ELSES_SA)
        with pytest.raises(PrincipalGuardError, match="mark_dlq_permanent"):
            assert_no_tier1_successes(client, self._SINCE, principal_ids=None)

    def test_empty_configuration_is_treated_as_unconfigured(self) -> None:
        client = self._tier1_by(_SOMEONE_ELSES_SA)
        with pytest.raises(PrincipalGuardError, match="mark_dlq_permanent"):
            assert_no_tier1_successes(client, self._SINCE, principal_ids=frozenset())

    def test_foreign_principal_cannot_mask_our_own_violation(self) -> None:
        # Filtering narrows who fails the stage; it must not drop OUR row
        # just because a foreign row sorts ahead of it.
        client = _Client(
            _result(
                [
                    _audit(
                        "restart_consumer_group",
                        "success",
                        self._SINCE + timedelta(minutes=3),
                        principal_id=_SOMEONE_ELSES_SA,
                    ),
                    _audit(
                        "pause_dag",
                        "success",
                        self._SINCE + timedelta(minutes=1),
                        principal_id=_OUR_AGENT_SA,
                    ),
                ]
            )
        )
        with pytest.raises(PrincipalGuardError, match="pause_dag") as excinfo:
            assert_no_tier1_successes(client, self._SINCE, principal_ids=self._OURS)
        assert "restart_consumer_group" not in str(excinfo.value)


class TestWriteCapablePrincipal:
    """The mirror guard, for the one stage that spends money AND mutates.

    Every principal check was gated on --smoke, so the remediation stage ran
    unguarded. A read-scoped token there does not fail fast: each scenario
    investigates, plans, attempts its action, is refused, and grades red —
    eight environment failures dressed as agent failures, after full spend.
    """

    def test_a_scope_refusal_fails_the_guard(self) -> None:
        # The exact wrong-token case: PLATFORM_SMOKE_TOKEN in the remediation
        # stage. This is the outcome the read-only guard treats as success.
        client = _Client(MCPError(-32002, "missing required scope: actions:execute"))
        with pytest.raises(PrincipalGuardError, match="lacks\\s+actions:execute"):
            assert_write_capable_principal(client)

    def test_an_argument_refusal_passes_the_guard(self) -> None:
        # Scope check passed, arguments rejected — nothing executed, and the
        # principal is demonstrably able to act.
        client = _Client(MCPError(-32602, "invalid tool arguments"))
        assert_write_capable_principal(client)  # no raise

    def test_a_successful_probe_fails_loudly(self) -> None:
        # A deliberately invalid Tier-1 call must never be accepted. If it
        # was, the probe is no longer safe to fire.
        client = _Client(ToolResult(content=[{"type": "text", "text": "{}"}]))
        with pytest.raises(PrincipalGuardError, match="SUCCEEDED"):
            assert_write_capable_principal(client)

    def test_an_unexpected_error_fails_closed(self) -> None:
        client = _Client(RuntimeError("connection reset"))
        with pytest.raises(PrincipalGuardError, match="Failing closed"):
            assert_write_capable_principal(client)

    def test_the_two_guards_disagree_on_the_same_response(self) -> None:
        """The pair is the point: each stage requires the opposite token.

        One response, two verdicts — which is what makes running the wrong
        stage with the wrong token detectable at all.
        """
        scope_refused = _Client(MCPError(-32002, "missing required scope"))
        assert_read_only_principal(scope_refused)  # passes
        with pytest.raises(PrincipalGuardError):
            assert_write_capable_principal(scope_refused)  # fails
