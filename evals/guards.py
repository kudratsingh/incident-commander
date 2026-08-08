"""Point-of-use assertions for the eval runner's effective principal.

The 2026-08-07 Run 001 stage 1 ran with full write scope while every
label said "read-scoped smoke": PR #62's ``-include .env`` silently
overrode the token PR #69's ``eval-smoke`` recipe exported, and make
re-exported the file's value. Ten Tier-1 writes landed under the full
principal before the platform audit log revealed it.

The lesson these guards encode: **a control must be asserted where it is
used, not assumed from where it was configured.** Both checks run against
the live platform with tools that exist on v0.4.9 — no whoami, no
introspection endpoint, no platform change required.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Final

from incident_commander.tools.mcp_client import MCPClientProtocol, MCPError

# Any Tier-1 tool works as the probe; mark_dlq_permanent is the cheapest
# (no side effect even if it somehow executed — but see below, it can't).
_PROBE_TOOL: Final[str] = "mark_dlq_permanent"

# Deliberately invalid arguments. The platform's tool handler checks scope
# BEFORE parsing arguments, so a token WITHOUT actions:execute is refused
# on scope and a token WITH it fails argument validation — two
# distinguishable outcomes, neither of which can execute the action.
_PROBE_ARGS: Final[dict[str, Any]] = {
    "job_id": "00000000-0000-0000-0000-000000000000-INVALID",
    "reason": "",
    "idempotency_key": "x",
}

_SCOPE_REFUSAL_CODE: Final[int] = -32002
_TIER_1_TOOLS: Final[frozenset[str]] = frozenset(
    {
        "restart_consumer_group",
        "pause_dag",
        "replay_dlq_messages",
        "invalidate_cache_key",
        "replay_dlq_by_ids",
        "replay_dlq_by_category",
        "mark_dlq_permanent",
    }
)


class PrincipalGuardError(RuntimeError):
    """The effective principal is not the one the run requires."""


def assert_read_only_principal(client: MCPClientProtocol) -> None:
    """Hard-fail unless the client's token genuinely lacks write scope.

    Negative probe: invoke a Tier-1 tool with invalid arguments.

    * Scope refusal (``-32002 missing required scope``) → the token is
      read-scoped. Pass.
    * Anything else (validation error, success, unexpected code) → the
      token carries ``actions:execute``. Fail before any scenario runs.

    Safe by construction: the handler's scope check precedes argument
    parsing, so the malformed payload cannot execute under either token.
    """
    try:
        result = client.call_tool(_PROBE_TOOL, _PROBE_ARGS)
    except MCPError as err:
        if err.code == _SCOPE_REFUSAL_CODE and "scope" in str(err).lower():
            return
        raise PrincipalGuardError(
            "read-only guard: expected a scope refusal from the negative "
            f"probe, got MCPError {err.code}: {err}. The token appears to "
            "carry actions:execute — refusing to run smoke scenarios under "
            "a write-capable principal."
        ) from err
    raise PrincipalGuardError(
        "read-only guard: the negative probe was NOT refused on scope "
        f"(result: {str(result)[:200]}). The token carries write scope — "
        "refusing to run smoke scenarios under a write-capable principal."
    )


def assert_no_tier1_successes(
    client: MCPClientProtocol,
    since: datetime,
) -> list[dict[str, Any]]:
    """Fail if the platform audit records any successful Tier-1 call since ``since``.

    Graded from the platform's own audit log — the ground truth that
    caught the token bug — rather than from the agent's trajectory. An
    agent cannot grade itself honest (CLAUDE.md invariant 6).

    Returns the offending rows (empty on success) so callers can report
    them; raises ``PrincipalGuardError`` when any are found.
    """
    result = client.call_tool(
        "list_audit_events",
        {"action": "agent.tool_invoked", "principal_type": "service_account", "limit": 200},
    )
    events = _parse_events(result)
    violations = [
        e
        for e in events
        if _tool_of(e) in _TIER_1_TOOLS
        and _outcome_of(e) == "success"
        and _created_at(e) is not None
        and _created_at(e) >= since  # type: ignore[operator]
    ]
    if violations:
        summary = ", ".join(
            f"{_tool_of(e)}@{_created_at(e)} by {e.get('principal_id')}" for e in violations[:5]
        )
        raise PrincipalGuardError(
            f"read-only stage executed {len(violations)} successful Tier-1 "
            f"action(s) per the platform audit log: {summary}. "
            "Read-only means read-only."
        )
    return violations


def _parse_events(result: Any) -> list[dict[str, Any]]:
    for block in getattr(result, "content", []) or []:
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            payload = json.loads(block["text"])
            items = payload.get("items", payload if isinstance(payload, list) else [])
            return [i for i in items if isinstance(i, dict)]
    return []


def _tool_of(event: dict[str, Any]) -> str:
    extra = event.get("extra_data") or {}
    return str(extra.get("tool_name", "")) if isinstance(extra, dict) else ""


def _outcome_of(event: dict[str, Any]) -> str:
    extra = event.get("extra_data") or {}
    return str(extra.get("outcome", "")) if isinstance(extra, dict) else ""


def _created_at(event: dict[str, Any]) -> datetime | None:
    raw = event.get("created_at")
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
