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

from datetime import datetime
from typing import Any, Final

from pydantic import ValidationError

from incident_commander.tools.mcp_client import MCPClientProtocol, MCPError
from incident_commander.tools.policies import Tier, tools_at_or_below
from incident_commander.tools.registry import TOOL_REGISTRY, AuditEventEntry

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
# Derived from the tier map, never hand-copied. A second list of Tier-1
# names would be one more mirror to drift out of sync — the same defect
# issue #79 tracks for ReadToolName, and the same class as the audit
# payload shape this module got wrong (F-004): a fact restated instead of
# referenced. If a Tier-1 tool is added to policies.py, this guard covers
# it with no edit here.
_TIER_1_TOOLS: Final[frozenset[str]] = tools_at_or_below(Tier.TIER_1) - tools_at_or_below(Tier.READ)


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
    except Exception as err:  # noqa: BLE001 — fail closed, deliberately
        # Transport blip, unknown response shape, anything at all: an
        # unverified guard is an unmet precondition, not a warning. A
        # safety check that shrugs on an unexpected error is the bypass
        # F-001 is about.
        raise PrincipalGuardError(
            "read-only guard: could not verify the principal "
            f"({type(err).__name__}: {err}). Failing closed — the run does "
            "not proceed on an unverified control."
        ) from err
    raise PrincipalGuardError(
        "read-only guard: the negative probe was NOT refused on scope "
        f"(result: {str(result)[:200]}). The token carries write scope — "
        "refusing to run smoke scenarios under a write-capable principal."
    )


def assert_no_tier1_successes(
    client: MCPClientProtocol,
    since: datetime,
) -> list[AuditEventEntry]:
    """Fail if the platform audit records any successful Tier-1 call since ``since``.

    Graded from the platform's own audit log — the ground truth that
    caught the token bug — rather than from the agent's trajectory. An
    agent cannot grade itself honest (CLAUDE.md invariant 6).

    Returns the offending rows (empty on success) so callers can report
    them; raises ``PrincipalGuardError`` when any are found.
    """
    try:
        result = client.call_tool(
            "list_audit_events",
            {"action": "agent.tool_invoked", "principal_type": "service_account", "limit": 200},
        )
        events = _parse_events(result)
    except PrincipalGuardError:
        raise
    except Exception as err:  # noqa: BLE001 — fail closed, deliberately
        # An audit query we couldn't run proves nothing. Inconclusive is
        # a failure, not a pass.
        raise PrincipalGuardError(
            "post-stage audit could not be read "
            f"({type(err).__name__}: {err}); treating as a failure — an "
            "unverifiable stage is not a clean stage."
        ) from err
    violations = [
        e
        for e in events
        if _tool_of(e) in _TIER_1_TOOLS and _outcome_of(e) == "success" and e.created_at >= since
    ]
    if violations:
        summary = ", ".join(
            f"{_tool_of(e)}@{e.created_at.isoformat()} by {e.principal_id}" for e in violations[:5]
        )
        raise PrincipalGuardError(
            f"read-only stage executed {len(violations)} successful Tier-1 "
            f"action(s) per the platform audit log: {summary}. "
            "Read-only means read-only."
        )
    return violations


def _parse_events(result: Any) -> list[AuditEventEntry]:
    """Parse the tool result through the REGISTRY'S typed output model.

    Not a hand-rolled dict walk. The first version of this function read
    ``payload["items"]``; v0.4.9 emits ``{"total": N, "events": [...]}``,
    so it silently returned an empty list on every real call and the
    assertion below passed unconditionally — the guard built to catch the
    Run 001 token bug could not have caught it (F-004). The correct shape
    was already encoded, typed, in ``registry.ListAuditEventsOutput``, one
    import away.

    Parsing through the registry model means the contract test and the
    registry-consistency test now also protect this guard: if the platform
    changes the audit payload, CI fails before a run does. An unrecognized
    payload raises rather than yielding zero events — parsing nothing must
    fail closed, exactly like an unreadable audit.
    """
    spec = TOOL_REGISTRY["list_audit_events"]
    for block in getattr(result, "content", []) or []:
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            try:
                parsed = spec.output_model.model_validate_json(block["text"])
            except ValidationError as err:
                raise PrincipalGuardError(
                    "post-stage audit returned an unrecognized payload shape "
                    f"({err.error_count()} validation error(s) against "
                    f"{spec.output_model.__name__}); refusing to report a clean "
                    "stage from a payload we could not read."
                ) from err
            events: list[AuditEventEntry] = list(parsed.events)  # type: ignore[attr-defined]
            return events
    raise PrincipalGuardError(
        "post-stage audit response contained no text content block; "
        "treating as unreadable rather than as zero events."
    )


def _tool_of(event: AuditEventEntry) -> str:
    extra = event.extra_data or {}
    return str(extra.get("tool_name", ""))


def _outcome_of(event: AuditEventEntry) -> str:
    extra = event.extra_data or {}
    return str(extra.get("outcome", ""))
