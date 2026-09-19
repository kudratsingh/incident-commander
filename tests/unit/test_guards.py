"""Point-of-use principal guards (Run 001 stage-1 token bug)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import pytest

from evals.guards import (
    _AGENT_FORBIDDEN_SCOPE,
    _CHAOS_PROBE_ARGS,
    _CHAOS_PROBE_TOOL,
    _PROBE_ARGS,
    _PROBE_TOOL,
    AuditWindowScan,
    PrincipalGuardError,
    assert_chaos_blind_principal,
    assert_chaos_capable_principal,
    assert_no_tier1_successes,
    assert_read_only_principal,
    assert_write_capable_principal,
)
from evals.scenarios.schema import chaos_argument_errors, chaos_tool_names
from incident_commander.tools.mcp_client import MCPError, ToolResult
from incident_commander.tools.policies import Tier, tier_of


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


class _ByTool:
    """One answer per tool name, because the agent guard now asks two things.

    ``assert_write_capable_principal`` fires a Tier-1 probe then a chaos probe and the
    pass condition is OPPOSITE: Tier-1 refused on arguments, chaos refused on scope.
    """

    def __init__(self, behavior: dict[str, ToolResult | Exception]) -> None:
        self._behavior = behavior
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call_tool(
        self, name: str, arguments: Any, *, timeout_seconds: float | None = None
    ) -> ToolResult:
        self.calls.append((name, dict(arguments)))
        try:
            behavior = self._behavior[name]
        except KeyError:  # pragma: no cover - a test wired the wrong tool
            raise AssertionError(f"no behavior configured for {name!r}") from None
        if isinstance(behavior, Exception):
            raise behavior
        return behavior


#: The principal the split is meant to produce: can act, cannot seed.
_SPLIT_AGENT: Final[dict[str, ToolResult | Exception]] = {
    _PROBE_TOOL: MCPError(-32602, "invalid tool arguments"),
    _CHAOS_PROBE_TOOL: MCPError(-32002, "missing required scope: chaos:invoke"),
}


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

    From v0.4.9's AuditEventEntry: the first version built {"items": [...]}, which the
    platform never emits, so the guard was a no-op (F-004).
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

    ``total`` counts the same filter unlimited (audit.py:86), so a bigger ``total``
    means withheld rows.
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

    F-004: `_parse_events` read `payload["items"]` while v0.4.9 emits `events`, so it
    returned zero events on every real call and exited 0.
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
        # Covered by the typed parse since F-004: AuditEventEntry requires created_at, so a
        # row the guard cannot place in time cannot be dropped silently.
        row = _audit("mark_dlq_permanent", "success", self._SINCE + timedelta(minutes=1))
        row["created_at"] = "not-a-timestamp"
        with pytest.raises(PrincipalGuardError, match="unrecognized payload shape"):
            assert_no_tier1_successes(self._envelope({"total": 1, "events": [row]}), self._SINCE)


class TestSaturatedAuditPage:
    """A-13: one page of at most 200 rows is not a scan of the window.

    ``list_audit_events`` has no offset and no created_after, so the guard cannot
    page. Rows come DESC, so the only proof the whole window was seen is
    that the oldest row predates ``since``; without it, fail closed.
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
        # Exactly the A-13 blindness: 200 in-window rows, and rows 201+ unreachable.
        client = _Client(_result(self._page(200), total=417))
        with pytest.raises(PrincipalGuardError, match="saturated"):
            assert_no_tier1_successes(client, self._SINCE)

    def test_full_page_whose_oldest_row_predates_since_is_a_genuine_pass(self) -> None:
        # The page reaches back past `since`, so the window was fully scanned.
        client = _Client(_result(self._page(200, oldest_before_since=True), total=417))
        assert assert_no_tier1_successes(client, self._SINCE) == []

    def test_server_reported_total_above_the_page_fails_closed(self) -> None:
        # `total` counts the same filter unlimited, so total > len(events) means withheld rows.
        client = _Client(_result(self._page(3), total=64))
        with pytest.raises(PrincipalGuardError, match="inconclusive"):
            assert_no_tier1_successes(client, self._SINCE)

    def test_short_page_is_conclusive(self) -> None:
        # total == len(events) below the cap: every matching row came back.
        client = _Client(_result(self._page(3)))
        assert assert_no_tier1_successes(client, self._SINCE) == []

    def test_saturated_page_still_names_the_violations_it_can_see(self) -> None:
        # A violation visible on the page must not be swallowed by saturation.
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

    The filter set is {agent SA, smoke SA} — NOT the smoke SA alone: the F-001 failure
    this guard exists for is the stage running under the FULL agent token.
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
        # The F-001 shape: the "read-scoped" stage wrote under the agent token.
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

    Every principal check was gated on --smoke, so the remediation stage ran unguarded.
    """

    def test_a_scope_refusal_fails_the_guard(self) -> None:
        # The exact wrong-token case: PLATFORM_SMOKE_TOKEN in the remediation
        # stage. This is the outcome the read-only guard treats as success.
        client = _Client(MCPError(-32002, "missing required scope: actions:execute"))
        with pytest.raises(PrincipalGuardError, match="lacks\\s+actions:execute"):
            assert_write_capable_principal(client)

    def test_an_argument_refusal_plus_a_chaos_scope_refusal_passes(self) -> None:
        # The post-v0.6.5 agent principal and the ONLY passing shape: Tier-1 refused on
        # arguments, chaos refused on scope.
        client = _ByTool(dict(_SPLIT_AGENT))
        assert_write_capable_principal(client)  # no raise
        assert [name for name, _ in client.calls] == [_PROBE_TOOL, _CHAOS_PROBE_TOOL]

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

    def test_a_token_that_can_also_seed_chaos_fails_the_guard(self) -> None:
        """The leak, caught before a single model call.

        A principal that passes BOTH probes on arguments is the pre-v0.6.5 four-scope token:
        it can act, and the platform serves it the `chaos.%` audit rows.
        """
        client = _ByTool(
            {
                _PROBE_TOOL: MCPError(-32602, "invalid tool arguments"),
                _CHAOS_PROBE_TOOL: MCPError(-32602, "latency_ms: not an integer"),
            }
        )
        with pytest.raises(PrincipalGuardError, match="chaos:invoke"):
            assert_write_capable_principal(client)

    def test_a_vanished_probe_tool_fails_closed(self) -> None:
        """ "Tool not found" is not proof that the principal can act.

        The guard used to pass on ANY non-scope MCP error, so a renamed
        ``mark_dlq_permanent`` makes it green vacuously for a read-scoped token.
        """
        client = _Client(MCPError(-32601, f"Unknown tool: {_PROBE_TOOL}"))
        with pytest.raises(PrincipalGuardError, match="-32601"):
            assert_write_capable_principal(client)

    def test_an_internal_platform_error_fails_closed(self) -> None:
        client = _Client(MCPError(-32603, "internal server error"))
        with pytest.raises(PrincipalGuardError, match="Failing closed"):
            assert_write_capable_principal(client)

    def test_a_scope_shaped_code_without_the_word_scope_fails_closed(self) -> None:
        # -32002 with no scope named is neither refusal, so fail closed owns it.
        client = _Client(MCPError(-32002, "upstream timeout"))
        with pytest.raises(PrincipalGuardError):
            assert_write_capable_principal(client)

    def test_the_probe_is_a_tier_1_tool(self) -> None:
        # The probe only distinguishes the scopes because it needs
        # actions:execute. A read tool would be refused by neither token.
        assert tier_of(_PROBE_TOOL) is Tier.TIER_1


class TestChaosBlindPrincipal:
    """The negative half of the split: the AGENT must not be able to seed.

    Not blast radius: the platform hides every ``chaos.%`` audit row from principals
    without ``chaos:invoke``, so the scope is a read of the answer key — hook name and
    arguments, stamped seconds before the alert. Asserted at the point of use (F-001).
    """

    def test_a_scope_refusal_passes(self) -> None:
        client = _Client(MCPError(-32002, "missing required scope: chaos:invoke"))
        assert_chaos_blind_principal(client)  # no raise
        assert client.calls[0][0] == _CHAOS_PROBE_TOOL

    def test_an_argument_refusal_means_the_scope_is_carried_and_fails(self) -> None:
        # The pre-split four-scope signature: past the scope check, refused on arguments.
        client = _Client(MCPError(-32602, "latency_ms: Input should be a valid integer"))
        with pytest.raises(PrincipalGuardError, match="chaos:invoke"):
            assert_chaos_blind_principal(client)

    def test_a_successful_probe_fails_loudest(self) -> None:
        client = _Client(ToolResult(content=[{"type": "text", "text": "{}"}]))
        with pytest.raises(PrincipalGuardError, match="chaos:invoke"):
            assert_chaos_blind_principal(client)

    def test_an_unexpected_error_fails_closed(self) -> None:
        # A probe that never reached the handler proves nothing either way,
        # and an unverified control is an unmet precondition.
        client = _Client(RuntimeError("connection reset"))
        with pytest.raises(PrincipalGuardError, match="Failing closed"):
            assert_chaos_blind_principal(client)

    def test_a_missing_chaos_tool_fails_closed(self) -> None:
        # CHAOS_ENABLED=false: the hook is not registered, so the refusal says nothing
        # about the token.
        client = _Client(MCPError(-32601, f"Unknown tool: {_CHAOS_PROBE_TOOL}"))
        with pytest.raises(PrincipalGuardError, match="Failing closed"):
            assert_chaos_blind_principal(client)

    def test_the_message_names_the_credential_to_fix(self) -> None:
        # The remedy is re-minting: `make bootstrap-token` refuses this scope.
        client = _Client(MCPError(-32602, "invalid arguments"))
        with pytest.raises(PrincipalGuardError) as exc:
            assert_chaos_blind_principal(client)
        message = str(exc.value)
        assert "PLATFORM_CHAOS_TOKEN" in message
        assert "make bootstrap-token" in message

    def test_it_probes_the_scope_the_platform_filter_keys_on(self) -> None:
        # Same string the platform's audit filter tests for, or it is only tidiness.
        assert _AGENT_FORBIDDEN_SCOPE == "chaos:invoke"

    def test_the_two_chaos_guards_are_exact_opposites(self) -> None:
        """One platform response, two verdicts — one per principal.

        The same probe with inverted expectations is what makes "two different
        principals" checkable.
        """
        scope_refused = _Client(MCPError(-32002, "missing required scope: chaos:invoke"))
        assert_chaos_blind_principal(scope_refused)  # the agent: correct
        with pytest.raises(PrincipalGuardError):
            assert_chaos_capable_principal(scope_refused)  # the runner: wrong

        args_refused = _Client(MCPError(-32602, "latency_ms: not an integer"))
        assert_chaos_capable_principal(args_refused)  # the runner: correct
        with pytest.raises(PrincipalGuardError):
            assert_chaos_blind_principal(args_refused)  # the agent: wrong


class TestChaosCapablePrincipal:
    """The scope a chaos-only scenario actually needs is ``chaos:invoke``.

    Such a scenario declares no ``expected_action_tools``, so the write guard never
    fired; probing ``actions:execute`` would refuse a selection entitled to run.
    """

    def test_a_scope_refusal_fails_the_guard(self) -> None:
        client = _Client(MCPError(-32002, "missing required scope: chaos:invoke"))
        with pytest.raises(PrincipalGuardError, match="chaos:invoke"):
            assert_chaos_capable_principal(client)

    def test_an_argument_refusal_passes_the_guard(self) -> None:
        client = _Client(MCPError(-32602, "latency_ms: Input should be a valid integer"))
        assert_chaos_capable_principal(client)  # no raise
        assert client.calls[0][0] == _CHAOS_PROBE_TOOL

    def test_a_successful_probe_fails_loudly(self) -> None:
        client = _Client(ToolResult(content=[{"type": "text", "text": "{}"}]))
        with pytest.raises(PrincipalGuardError, match="SUCCEEDED"):
            assert_chaos_capable_principal(client)

    def test_a_missing_chaos_tool_fails_closed(self) -> None:
        # CHAOS_ENABLED=false is the common cause; refusing before spend is the answer.
        client = _Client(MCPError(-32601, f"Unknown tool: {_CHAOS_PROBE_TOOL}"))
        with pytest.raises(PrincipalGuardError, match="CHAOS_ENABLED"):
            assert_chaos_capable_principal(client)

    def test_an_unexpected_error_fails_closed(self) -> None:
        client = _Client(RuntimeError("connection reset"))
        with pytest.raises(PrincipalGuardError, match="Failing closed"):
            assert_chaos_capable_principal(client)

    def test_the_probe_hook_is_one_the_platform_declares(self) -> None:
        # Derived from contracts/platform-tools.snapshot.json, so a retired hook fails in CI.
        assert _CHAOS_PROBE_TOOL in chaos_tool_names()

    def test_the_probe_arguments_cannot_seed_anything(self) -> None:
        # The safety claim of a negative probe: this invocation is rejected by the
        # platform's own committed input schema.
        assert chaos_argument_errors(_CHAOS_PROBE_TOOL, _CHAOS_PROBE_ARGS)

    def test_the_write_probe_arguments_are_not_reused(self) -> None:
        # Two scopes, two probes. Firing the Tier-1 probe to test chaos scope
        # is the naive fix this guard exists to avoid.
        assert _CHAOS_PROBE_TOOL != _PROBE_TOOL
        assert _CHAOS_PROBE_ARGS != _PROBE_ARGS

    def test_chaos_scope_does_not_answer_for_write_scope(self) -> None:
        """A token with chaos:invoke and no actions:execute must fail the
        write guard and pass the chaos guard from the same platform."""
        chaos_refused = _Client(MCPError(-32002, "missing required scope: chaos:invoke"))
        with pytest.raises(PrincipalGuardError):
            assert_chaos_capable_principal(chaos_refused)
        args_refused = _Client(MCPError(-32602, "invalid arguments"))
        assert_chaos_capable_principal(args_refused)  # no raise


class _SequenceClient:
    """Serves a different page per call, the way a live audit log does.

    ``_Client`` replays one fixed answer forever, which cannot express the
    thing under test: the log GROWS between checkpoints, and rows that were
    on an earlier page scroll off a later one.
    """

    def __init__(self, pages: list[ToolResult]) -> None:
        self._pages = pages
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call_tool(
        self, name: str, arguments: Any, *, timeout_seconds: float | None = None
    ) -> ToolResult:
        self.calls.append((name, dict(arguments)))
        # Past the end, keep serving the final page — the post-stage read
        # sees the same log the last checkpoint did.
        return self._pages[min(len(self.calls) - 1, len(self._pages) - 1)]


class TestCheckpointedWindowScan:
    """B2: one page of 200 is not a scan, and the fix is not `offset`.

    ``list_audit_events`` takes only ``action`` / ``action_prefix`` / ``principal_type``
    / ``limit`` under ``additionalProperties: false``, so the window cannot be paged
    backwards — but it can be covered forwards, by reading it while the stage runs.
    """

    _SINCE = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)
    _BEFORE = [
        _audit(
            "get_consumer_lag",
            "success",
            datetime(2026, 8, 9, 11, 50, tzinfo=UTC) + timedelta(seconds=i),
        )
        for i in range(80)
    ]

    def _in_window(
        self, first: int, count: int, *, tool: str = "get_consumer_lag"
    ) -> list[dict[str, Any]]:
        return [
            _audit(tool, "success", self._SINCE + timedelta(seconds=i))
            for i in range(first, first + count)
        ]

    def _page(self, rows: list[dict[str, Any]], total: int) -> ToolResult:
        """Newest 200, created_at DESC — the platform's own ordering."""
        newest = sorted(rows, key=lambda r: r["created_at"], reverse=True)[:200]
        return _result(newest, total=total)

    def test_one_post_stage_page_is_inconclusive_the_old_behaviour(self) -> None:
        # RED-BEFORE: 250 in-window rows saturate the final page, so it cannot reach past
        # `since` and a correct, clean stage exits 5.
        final = self._page(self._BEFORE + self._in_window(1, 250), total=330)
        with pytest.raises(PrincipalGuardError, match="inconclusive"):
            assert_no_tier1_successes(_Client(final), self._SINCE)

    def test_checkpoints_cover_a_window_one_page_cannot(self) -> None:
        # GREEN-AFTER: a mid-stage checkpoint saw the older half, and the pages overlap.
        mid = self._page(self._BEFORE + self._in_window(1, 120), total=200)
        final = self._page(self._BEFORE + self._in_window(1, 250), total=330)
        client = _SequenceClient([mid, final])
        scan = AuditWindowScan(self._SINCE)
        scan.checkpoint(client)
        assert assert_no_tier1_successes(client, self._SINCE, scan=scan) == []
        assert scan.checkpoints == 2

    def test_a_success_that_scrolled_off_the_last_page_is_still_caught(self) -> None:
        # The early Tier-1 success is row 240-something by the end, named only via a checkpoint.
        early_violation = _audit(
            "mark_dlq_permanent", "success", self._SINCE + timedelta(seconds=5)
        )
        mid = self._page(self._BEFORE + [early_violation] + self._in_window(10, 110), total=200)
        final = self._page(self._BEFORE + [early_violation] + self._in_window(10, 250), total=331)
        # It really is off the final page: the one-page guard cannot see it.
        with pytest.raises(PrincipalGuardError) as unpaged:
            assert_no_tier1_successes(_Client(final), self._SINCE)
        assert "mark_dlq_permanent" not in str(unpaged.value)

        client = _SequenceClient([mid, final])
        scan = AuditWindowScan(self._SINCE)
        scan.checkpoint(client)
        with pytest.raises(PrincipalGuardError, match="mark_dlq_permanent"):
            assert_no_tier1_successes(client, self._SINCE, scan=scan)

    def test_a_gap_between_checkpoints_is_not_coverage(self) -> None:
        # Checkpoints too far apart: the rows in the hole are gone, so fail closed.
        mid = self._page(self._BEFORE + self._in_window(1, 120), total=200)
        final = self._page(self._in_window(151, 200), total=600)
        client = _SequenceClient([mid, final])
        scan = AuditWindowScan(self._SINCE)
        scan.checkpoint(client)
        with pytest.raises(PrincipalGuardError, match="inconclusive"):
            assert_no_tier1_successes(client, self._SINCE, scan=scan)

    def test_scan_graded_against_a_different_since_is_refused(self) -> None:
        # A window from one stage start asserted against another grades wrong.
        scan = AuditWindowScan(self._SINCE)
        with pytest.raises(ValueError, match="not a graded window"):
            assert_no_tier1_successes(
                _Client(_result([])), self._SINCE + timedelta(hours=1), scan=scan
            )

    def test_checkpoints_ask_for_the_page_size_the_scan_checks_against(self) -> None:
        client = _SequenceClient([_result([])])
        AuditWindowScan(self._SINCE).checkpoint(client)
        assert client.calls[0][0] == "list_audit_events"
        assert client.calls[0][1]["limit"] == 200
