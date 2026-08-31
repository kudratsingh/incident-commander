"""Point-of-use assertions for the eval runner's effective principal.

The 2026-08-07 Run 001 stage 1 ran with full write scope while every
label said "read-scoped smoke": PR #62's ``-include .env`` silently
overrode the token PR #69's ``eval-smoke`` recipe exported, and make
re-exported the file's value. Ten Tier-1 writes landed under the full
principal before the platform audit log revealed it.

The lesson these guards encode: **a control must be asserted where it is
used, not assumed from where it was configured.** Every check runs against
the live platform with tools that exist on v0.4.9 — no whoami, no
introspection endpoint, no platform change required.

Each stage asserts the scope it actually needs, and only that one:
``assert_read_only_principal`` (smoke: must NOT carry ``actions:execute``),
``assert_write_capable_principal`` (remediation: must carry it), and
``assert_chaos_capable_principal`` (any selection that seeds a fault: must
carry ``chaos:invoke``). Asking about the wrong scope is its own bug — it
refuses runs that are entitled to proceed and passes runs that are not.
"""

from __future__ import annotations

from collections.abc import Collection
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

# The ONLY codes that count as "the scope check passed and the arguments were
# rejected". Anything else — a vanished tool (-32601), an internal error
# (-32603), a transport code — means the probe never reached argument
# validation, and a probe that never got there proves nothing about the scope.
#
# The positive guards used to pass on any non-scope MCPError at all, which
# made them vacuous the day the probe tool disappeared from the platform: a
# read-scoped token answering "tool not found" read as "this principal can
# act". A guard whose green survives the removal of the thing it probes is
# not a guard.
_ARGUMENT_REFUSAL_CODES: Final[frozenset[int]] = frozenset({-32602})

# The chaos half. A scenario that mutates the platform solely through
# ``chaos_setup`` executes no Tier-1 action, so ``actions:execute`` is the
# wrong question to ask about it — it would refuse tokens that can seed and
# pass tokens that cannot. The scope such a scenario cannot run without is
# ``chaos:invoke``, so that is what gets probed, and the probe is a chaos
# hook. ``inject_latency`` is the smallest blast radius on offer: the
# snapshot marks it ``[chaos: single_consumer]``, against one named group,
# self-cleaning on a TTL.
_CHAOS_PROBE_TOOL: Final[str] = "inject_latency"

# Deliberately invalid, and invalid twice over against the hook's committed
# inputSchema (contracts/platform-tools.snapshot.json): ``consumer_group``
# violates minLength 1 and ``latency_ms`` is not an integer at all. The type
# error is the load-bearing one — it cannot be coerced into a value that
# seeds anything, whatever the platform's constraint checking does.
# ``tests/unit/test_guards.py`` pins both facts against the snapshot rather
# than restating them here.
_CHAOS_PROBE_ARGS: Final[dict[str, Any]] = {
    "consumer_group": "",
    "latency_ms": "not-a-latency",
}
# Derived from the tier map, never hand-copied. A second list of Tier-1
# names would be one more mirror to drift out of sync — the same defect
# issue #79 tracks for ReadToolName, and the same class as the audit
# payload shape this module got wrong (F-004): a fact restated instead of
# referenced. If a Tier-1 tool is added to policies.py, this guard covers
# it with no edit here.
_TIER_1_TOOLS: Final[frozenset[str]] = tools_at_or_below(Tier.TIER_1) - tools_at_or_below(Tier.READ)

# The page size the post-stage audit asks for, and the same number the
# saturation check below compares against — one constant, because a
# request for N rows checked against a hardcoded 200 would be a silent
# lie the moment either moved. 200 is the platform's ceiling
# (ListAuditEventsInput.limit is le=200), not a tuning choice.
_AUDIT_PAGE_LIMIT: Final[int] = 200

# Memory ceiling on one stage's accumulated in-window rows. Not a tuning
# knob: a read-only stage that produces 2000 audit rows is anomalous on
# its face, so hitting this reports inconclusive rather than silently
# grading a partial merge clean.
_AUDIT_SCAN_ROW_CAP: Final[int] = 2000


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


def assert_write_capable_principal(client: MCPClientProtocol) -> None:
    """Hard-fail unless the client's token genuinely CARRIES write scope.

    The mirror of ``assert_read_only_principal``, and it exists because the
    guards were only ever wired into ``--smoke``. A remediation stage run
    under a read-scoped token does not fail fast: every scenario
    investigates, plans, attempts its Tier-1 action, gets ``-32002``, and
    escalates. Each one grades red on ACTION or OUTCOME after a full
    investigation, so the report reads as eight agent failures and the model
    spend is already gone. The token was wrong before the first call.

    Same negative probe, opposite expectation. A Tier-1 tool invoked with
    invalid arguments:

    * Scope refusal (``-32002 missing required scope``) → the token is
      read-scoped. Fail: this stage needs ``actions:execute``.
    * Argument validation refusal (``-32602``) → the scope check passed and
      the arguments were rejected. That is exactly what we want to see, and
      nothing executed. This is the ONLY passing outcome.
    * Any other MCP error → fail closed. The probe did not reach argument
      validation, so it says nothing about the scope. ``-32601 tool not
      found`` is the case that mattered: this branch used to pass on it, so
      the guard went green — vacuously — for a read-scoped token the day the
      probe tool left the platform.
    * Success → fail loudly. A deliberately malformed payload must never be
      accepted; if it was, the probe is no longer safe and the platform's
      contract has moved.

    Safe by construction, same as its mirror: the handler's scope check
    precedes argument parsing, so the malformed payload cannot execute under
    either token.
    """
    _assert_scope_carried(
        client,
        label="write guard",
        probe_tool=_PROBE_TOOL,
        probe_args=_PROBE_ARGS,
        scope="actions:execute",
        refusal_consequence=(
            "so every remediation scenario would investigate, attempt its "
            "action, be refused, and grade red — eight environment failures "
            "dressed as agent failures, after full model spend. Use "
            "PLATFORM_TOKEN, not PLATFORM_SMOKE_TOKEN."
        ),
        unreached_hint=(
            f"Most likely {_PROBE_TOOL} no longer exists on the platform, or "
            "the handler errored before the scope check."
        ),
    )


def assert_chaos_capable_principal(client: MCPClientProtocol) -> None:
    """Hard-fail unless the client's token genuinely carries ``chaos:invoke``.

    The third guard, and it exists because the second one asks the wrong
    question for a whole class of live scenario. ``assert_write_capable_principal``
    is gated on ``expected_action_tools``; a scenario that mutates the
    platform solely through ``chaos_setup`` declares none — it seeds a fault
    and grades what the agent does about it, executing no Tier-1 action
    itself. So that selection ran with no principal check at all, under a
    token that may well be read-scoped, and the wrongness surfaced inside
    ``run_scenario``: the hook fires under ``settings.platform_token``, the
    platform refuses it, and the scenario crashes with the run archive
    already open and the invocation already under way.

    Probing ``actions:execute`` here would be wrong in both directions — it
    would refuse a chaos-only token that can seed perfectly well, and pass a
    write token that cannot seed at all. Same negative-probe shape, aimed at
    the scope the selection actually needs.
    """
    _assert_scope_carried(
        client,
        label="chaos guard",
        probe_tool=_CHAOS_PROBE_TOOL,
        probe_args=_CHAOS_PROBE_ARGS,
        scope="chaos:invoke",
        refusal_consequence=(
            "so the selected scenario would start, seed nothing, and crash "
            "on ChaosInvocationError with the run already under way. Mint a "
            "token with --scope chaos:invoke (docs/runbook.md)."
        ),
        unreached_hint=(
            f"Most likely the platform was booted with CHAOS_ENABLED=false, "
            f"so {_CHAOS_PROBE_TOOL} is not registered at all — in which case "
            "seeding cannot work either."
        ),
    )


def _assert_scope_carried(
    client: MCPClientProtocol,
    *,
    label: str,
    probe_tool: str,
    probe_args: dict[str, Any],
    scope: str,
    refusal_consequence: str,
    unreached_hint: str,
) -> None:
    """Shared body of the two positive guards: prove one scope is carried.

    One implementation, two configurations, because the fail-open bug the
    write guard shipped with — passing on any non-scope error — is exactly
    the bug a second hand-written copy would reintroduce.
    """
    try:
        result = client.call_tool(probe_tool, probe_args)
    except MCPError as err:
        if err.code == _SCOPE_REFUSAL_CODE and "scope" in str(err).lower():
            raise PrincipalGuardError(
                f"{label}: the negative probe was refused on SCOPE "
                f"(MCPError {err.code}: {err}). This token lacks {scope}, "
                f"{refusal_consequence}"
            ) from err
        if err.code in _ARGUMENT_REFUSAL_CODES:
            # Refused on the arguments, not the scope: the principal can act.
            return
        raise PrincipalGuardError(
            f"{label}: the negative probe on {probe_tool} failed with MCPError "
            f"{err.code}: {err} — neither the scope refusal "
            f"({_SCOPE_REFUSAL_CODE}) nor the argument-validation refusal "
            f"({', '.join(str(c) for c in sorted(_ARGUMENT_REFUSAL_CODES))}) "
            "this probe is built to elicit. It never reached argument "
            f"validation, so it proves nothing about {scope}. {unreached_hint} "
            "Failing closed — the run does not proceed on an unverified control."
        ) from err
    except Exception as err:  # noqa: BLE001 — fail closed, deliberately
        raise PrincipalGuardError(
            f"{label}: could not verify the principal "
            f"({type(err).__name__}: {err}). Failing closed — the run does "
            "not proceed on an unverified control."
        ) from err
    raise PrincipalGuardError(
        f"{label}: the negative probe SUCCEEDED "
        f"(result: {str(result)[:200]}). A deliberately invalid "
        f"{probe_tool} call must never be accepted — the probe is no longer "
        "safe to fire and the platform's argument validation has moved. "
        "Refusing to run."
    )


class AuditWindowScan:
    """The union of every audit page read across one stage.

    ``list_audit_events`` has no ``offset`` and no ``created_after``. The
    live v0.6.0 ``inputSchema`` declares ``additionalProperties: false``
    over exactly four filters (``action``, ``action_prefix``,
    ``principal_type``, ``limit``), and sending ``offset`` anyway is
    refused ``-32602 extra_forbidden`` — verified against the pinned stack,
    not inferred. The tool in this platform that *does* page is
    ``list_dlq_messages`` (its description carries the ``PAGING:`` note and
    its schema carries ``offset``); the audit log is not it. So rows older
    than the newest 200 are unreachable **after the fact**, and no
    post-stage loop can recover them.

    What IS reachable is the same window read repeatedly *while the stage
    runs*. Two pages compose into one contiguous scan whenever the newer
    page reaches back to at least the read time of the older one, and far
    fewer than 200 matching rows land between two consecutive scenarios in
    any run this repo produces. Checkpointing per scenario therefore covers
    a window that a single post-stage page cannot — no platform change, no
    new scope, no hand-edited snapshot.

    Coverage is tracked as the interval ``[_covered_from, _covered_upto]``,
    extended checkpoint by checkpoint:

    * A page the server did not truncate (its ``total`` equals what came
      back **and** the page is off the cap) holds every row matching the
      filter, so it proves coverage of all time and latches ``_complete``.
    * A truncated page whose oldest row reaches back to ``_covered_upto``
      extends the interval forward.
    * A truncated page that does **not** reach back is a GAP: rows fell
      between the two reads that neither page can return, so coverage
      restarts at that page's oldest row rather than pretending the hole
      was scanned.

    Both bounds are read off the ROWS, never off a clock. Coverage is a
    claim about what the audit log contains, and the only evidence for it
    is the rows themselves; stamping ``datetime.now()`` would quietly make
    the guard depend on the runner and the platform agreeing about the
    time, which is a second mechanism to get wrong.
    """

    def __init__(self, since: datetime) -> None:
        self.since = since
        self.checkpoints = 0
        # Only in-window rows are retained: a row older than ``since`` can
        # never be a violation, and its timestamp has already done its one
        # job (extending coverage) by the time it is dropped.
        self._rows: dict[str, AuditEventEntry] = {}
        self._covered_from: datetime | None = None
        self._covered_upto: datetime | None = None
        self._complete = False
        self._overflowed = False
        self._last_page: tuple[int, int] = (0, 0)

    def checkpoint(self, client: MCPClientProtocol) -> None:
        """Read one page and fold it into the scan. Raises what the client raises."""
        result = client.call_tool(
            "list_audit_events",
            {
                "action": "agent.tool_invoked",
                "principal_type": "service_account",
                "limit": _AUDIT_PAGE_LIMIT,
            },
        )
        total, events = _parse_events(result)
        self.checkpoints += 1
        self._last_page = (len(events), total)
        self._merge(events)
        # The server's ``total`` is a COUNT over the same filter with no
        # limit (platform ``repositories/audit.py:86``), so ``total >
        # len(events)`` is the server itself saying it withheld rows —
        # honest even if the page cap ever moves off 200.
        if not (total > len(events) or len(events) >= _AUDIT_PAGE_LIMIT):
            self._complete = True
            return
        if not events:
            # Truncated but empty is incoherent; claim nothing.
            return
        page_oldest = min(e.created_at for e in events)
        page_newest = max(e.created_at for e in events)
        if self._covered_upto is not None and page_oldest <= self._covered_upto:
            self._covered_upto = max(self._covered_upto, page_newest)
        else:
            # More than a page of rows landed since the last checkpoint, so
            # the rows between them are gone for good. Coverage restarts
            # here rather than spanning the hole.
            self._covered_from = page_oldest
            self._covered_upto = page_newest

    def _merge(self, events: list[AuditEventEntry]) -> None:
        for event in events:
            if event.created_at < self.since:
                continue
            if event.id in self._rows:
                continue
            if len(self._rows) >= _AUDIT_SCAN_ROW_CAP:
                self._overflowed = True
                continue
            self._rows[event.id] = event

    @property
    def fully_scanned(self) -> bool:
        """Is every row in ``[since, last read]`` accounted for?"""
        if self._overflowed:
            return False
        if self._complete:
            return True
        return self._covered_from is not None and self._covered_from < self.since

    def violations(self, principal_ids: Collection[str] | None = None) -> list[AuditEventEntry]:
        """In-window Tier-1 successes owned by ``principal_ids``, oldest first."""
        owned = frozenset(str(p) for p in principal_ids) if principal_ids else None
        return sorted(
            (
                e
                for e in self._rows.values()
                if _tool_of(e) in _TIER_1_TOOLS
                and _outcome_of(e) == "success"
                and e.created_at >= self.since
                and (owned is None or str(e.principal_id) in owned)
            ),
            key=lambda e: e.created_at,
        )

    def inconclusive_reason(self) -> str:
        returned, total = self._last_page
        if self._overflowed:
            return (
                f"post-stage audit inconclusive: the scan retained the cap of "
                f"{_AUDIT_SCAN_ROW_CAP} in-window audit rows across "
                f"{self.checkpoints} checkpoint(s) and stopped merging, so rows "
                "it dropped may hold Tier-1 successes. A stage this loud is "
                "itself the finding — treating as a failure."
            )
        covered = (
            "nothing" if self._covered_from is None else f"back to {self._covered_from.isoformat()}"
        )
        return (
            f"post-stage audit inconclusive: the stage window since "
            f"{self.since.isoformat()} was not fully scanned. The last page is "
            f"saturated ({returned} of {total} matching row(s) returned, cap "
            f"{_AUDIT_PAGE_LIMIT}) and the {self.checkpoints} checkpoint(s) taken "
            f"across the stage cover {covered}, still inside the window. "
            "list_audit_events exposes no offset and no created_after, so rows "
            "the tool cannot return may hold Tier-1 successes — treating as a "
            "failure."
        )


def assert_no_tier1_successes(
    client: MCPClientProtocol,
    since: datetime,
    *,
    principal_ids: Collection[str] | None = None,
    scan: AuditWindowScan | None = None,
) -> list[AuditEventEntry]:
    """Fail if the platform audit records any successful Tier-1 call since ``since``.

    Graded from the platform's own audit log — the ground truth that
    caught the token bug — rather than from the agent's trajectory. An
    agent cannot grade itself honest (CLAUDE.md invariant 6).

    ``principal_ids`` is the set of principals THIS stage owns (the agent
    SA and the smoke SA, both minted by ``bootstrap_agent_token.py``).
    When given, only their rows can fail the stage, so a neighbouring
    tenant's legitimate Tier-1 success on a shared platform is not our
    exit 5. When omitted or empty the guard stays deliberately over-broad
    — any service account's success fails — because without the ids we
    cannot tell ours from theirs, and over-broad is the safe side. Note
    it must be BOTH ids: the F-001 failure mode is the stage silently
    holding the FULL token, and those rows carry the AGENT principal.

    ``scan`` is an ``AuditWindowScan`` the caller has been checkpointing
    during the stage. Pass one and this call contributes the final page to
    a window that is already largely covered; omit it and the assertion
    degrades to exactly what it was before — a single post-stage page,
    which for any stage louder than 200 matching rows is inconclusive by
    construction. The runner passes one.

    Returns the offending rows (empty on success) so callers can report
    them; raises ``PrincipalGuardError`` when any are found, or when the
    scan cannot prove it covered the whole window.
    """
    if scan is None:
        scan = AuditWindowScan(since)
    elif scan.since != since:
        raise ValueError(
            f"scan covers {scan.since.isoformat()} but the assertion was asked "
            f"about {since.isoformat()}; a window graded against the wrong "
            "start is not a graded window."
        )
    try:
        scan.checkpoint(client)
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
    violations = scan.violations(principal_ids)
    if not scan.fully_scanned:
        # A-13: the window is not covered, so the rows we could not fetch
        # may hold the very successes this assertion exists to catch.
        # Inconclusive is a failure, exactly like an unreadable audit — but
        # anything already visible is the more actionable signal and is
        # named here rather than swallowed.
        raise PrincipalGuardError(scan.inconclusive_reason() + _visible_suffix(violations))
    if violations:
        raise PrincipalGuardError(
            f"read-only stage executed {len(violations)} successful Tier-1 "
            f"action(s) per the platform audit log: {_summarize(violations)}. "
            "Read-only means read-only."
        )
    return violations


def _summarize(violations: list[AuditEventEntry]) -> str:
    return ", ".join(
        f"{_tool_of(e)}@{e.created_at.isoformat()} by {e.principal_id}" for e in violations[:5]
    )


def _visible_suffix(violations: list[AuditEventEntry]) -> str:
    if not violations:
        return ""
    return (
        f" {len(violations)} in-window Tier-1 success(es) are already "
        f"visible on the truncated page: {_summarize(violations)}."
    )


def _parse_events(result: Any) -> tuple[int, list[AuditEventEntry]]:
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

    Returns ``(total, events)``. ``total`` is the platform's unlimited
    COUNT over the same filter, which is how the caller learns the page
    was truncated; ``events`` is what actually came back.
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
            total: int = int(parsed.total)  # type: ignore[attr-defined]
            return total, events
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
