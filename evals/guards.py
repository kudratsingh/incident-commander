"""Point-of-use assertions for the eval runner's effective principal.

Run 001 stage 1 ran with full write scope while every label said "read-scoped
smoke" (F-001), so: **a control is asserted where it is used, not assumed from
where it was configured.** Each stage probes the one scope it needs —
``assert_read_only_principal`` (no ``actions:execute``),
``assert_write_capable_principal`` (carries it, and NOT ``chaos:invoke``),
``assert_chaos_capable_principal`` (the evaluator's client carries ``chaos:invoke``).
The agent's chaos-blindness is load-bearing: the platform hides the ``chaos.%``
audit rows only from principals that genuinely lack the scope, so a token holding
it reads the answer key out of ``list_audit_events`` (owner decision O-4).

Three of the four probes must wear the AGENT's token — that is what they prove — so
since platform v0.6.17 each one also SAYS it is the lab's (``lab_principal_token``,
platform ADR 0038). Without the label the platform writes ``agent.tool_invoked`` and
the demo page reads a run nobody made (finding F4, WO-R3-335).
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from datetime import datetime
from typing import Any, Final

from pydantic import ValidationError

from incident_commander.tools.mcp_client import (
    LabProbeCapableClient,
    LabProbeRefused,
    MCPError,
)
from incident_commander.tools.policies import Tier, tools_at_or_below
from incident_commander.tools.registry import TOOL_REGISTRY, AuditEventEntry

# Any Tier-1 tool works as the probe; mark_dlq_permanent is the cheapest.
_PROBE_TOOL: Final[str] = "mark_dlq_permanent"

# Deliberately invalid arguments. The handler checks scope BEFORE parsing them, so
# a token without actions:execute is refused on scope and one with it fails argument
# validation — two distinguishable outcomes, neither of which can execute.
_PROBE_ARGS: Final[dict[str, Any]] = {
    "job_id": "00000000-0000-0000-0000-000000000000-INVALID",
    "reason": "",
    "idempotency_key": "x",
}

_SCOPE_REFUSAL_CODE: Final[int] = -32002

# The ONLY codes meaning "scope check passed, arguments rejected". Anything else —
# a vanished tool (-32601), an internal error, a transport code — never reached
# argument validation and proves nothing. The positive guards used to pass on any
# non-scope MCPError, so they went vacuously green when the probe tool vanished.
_ARGUMENT_REFUSAL_CODES: Final[frozenset[int]] = frozenset({-32602})

# The chaos half: a ``chaos_setup``-only scenario executes no Tier-1 action, so
# ``actions:execute`` is the wrong question about it. ``inject_latency`` is the
# smallest blast radius on offer — one named group, self-cleaning on a TTL.
_CHAOS_PROBE_TOOL: Final[str] = "inject_latency"

# Invalid twice over against the hook's committed inputSchema: ``consumer_group``
# violates minLength 1 and ``latency_ms`` is not an integer at all. The type error is
# load-bearing — it cannot be coerced into a value that seeds anything.
# ``tests/unit/test_guards.py`` pins both against the snapshot.
_CHAOS_PROBE_ARGS: Final[dict[str, Any]] = {
    "consumer_group": "",
    "latency_ms": "not-a-latency",
}

# The scope the AGENT principal must never carry (owner decision O-4). Named once,
# because the negative probe and the platform's own ``hidden_audit_action_prefixes``
# predicate must key on the same string.
_AGENT_FORBIDDEN_SCOPE: Final[str] = "chaos:invoke"
# Derived from the tier map, never hand-copied: a second list of Tier-1 names is one
# more mirror to drift (the F-004 class — a fact restated instead of referenced).
_TIER_1_TOOLS: Final[frozenset[str]] = tools_at_or_below(Tier.TIER_1) - tools_at_or_below(Tier.READ)

# One constant for both the page request and the saturation check below. 200 is the
# platform's ceiling (ListAuditEventsInput.limit is le=200), not a tuning choice.
_AUDIT_PAGE_LIMIT: Final[int] = 200

# Memory ceiling on one stage's in-window rows. Not a knob: 2000 audit rows from a
# read-only stage is anomalous, so hitting this reports inconclusive rather than
# grading a partial merge clean.
_AUDIT_SCAN_ROW_CAP: Final[int] = 2000


# Every probe below is a call the LAB makes on a service account's token — three of
# them on the AGENT's, on purpose, because what they prove is what that token can and
# cannot do. Since platform v0.6.17 the lab says so on the call itself (``_lab_probe``
# + ``X-Lab-Principal``, platform ADR 0038) and the platform writes ``lab.probe``
# instead of ``agent.tool_invoked``; without it these rows read as a run nobody made
# (demo finding F4). The reason is one short sentence naming what the probe proves,
# because it is what an operator reads in the audit stream months later.
_READ_ONLY_PROBE_REASON: Final[str] = (
    "principal guard: proves this token cannot execute a Tier-1 action"
)
_WRITE_PROBE_REASON: Final[str] = (
    "principal guard: proves the agent token can execute a Tier-1 action"
)
_CHAOS_BLIND_PROBE_REASON: Final[str] = "principal guard: proves the agent token cannot seed chaos"
_CHAOS_CAPABLE_PROBE_REASON: Final[str] = (
    "principal guard: proves the evaluator token can seed chaos"
)
_AUDIT_SCAN_REASON: Final[str] = "principal guard: reads the audit window for Tier-1 successes"


class PrincipalGuardError(RuntimeError):
    """The effective principal is not the one the run requires."""


def _label(reason: str, lab_principal_token: str | None) -> tuple[str | None, str | None]:
    """The label pair to send: the reason rides with the lab's credential or not at all.

    No credential means no label — an unlabelled probe on a platform that would have
    refused a credential-less one, not a call sent twice. Callers with a token pass it;
    the offline fakes and the pre-v0.6.17 path pass None.
    """
    return (reason, lab_principal_token) if lab_principal_token else (None, None)


def assert_read_only_principal(
    client: LabProbeCapableClient, *, lab_principal_token: str | None = None
) -> None:
    """Hard-fail unless the client's token genuinely lacks write scope.

    Negative probe on a Tier-1 tool with invalid arguments: a ``-32002`` scope
    refusal passes, anything else fails before a scenario runs. Safe by
    construction — the scope check precedes argument parsing.
    """
    _assert_scope_absent(
        client,
        label="read-only guard",
        probe_tool=_PROBE_TOOL,
        probe_args=_PROBE_ARGS,
        scope="actions:execute",
        carried_consequence=(
            "which is write scope — refusing to run smoke scenarios under a "
            "write-capable principal. Use PLATFORM_SMOKE_TOKEN, not "
            "PLATFORM_TOKEN."
        ),
        lab_probe_reason=_READ_ONLY_PROBE_REASON,
        lab_principal_token=lab_principal_token,
    )


def assert_write_capable_principal(
    client: LabProbeCapableClient, *, lab_principal_token: str | None = None
) -> None:
    """Hard-fail unless the client's token genuinely CARRIES write scope.

    The mirror of ``assert_read_only_principal``: only an argument refusal
    (``-32602``) passes, a scope refusal fails, and anything else fails closed.
    Without it a read-scoped remediation stage grades every scenario red on ACTION
    after a full investigation — environment failures dressed as agent failures,
    after the spend. Then ``assert_chaos_blind_principal``, because the right
    principal here is two claims (owner decision O-4).
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
        lab_probe_reason=_WRITE_PROBE_REASON,
        lab_principal_token=lab_principal_token,
    )
    assert_chaos_blind_principal(client, lab_principal_token=lab_principal_token)


def assert_chaos_blind_principal(
    client: LabProbeCapableClient, *, lab_principal_token: str | None = None
) -> None:
    """Hard-fail unless the AGENT's token genuinely lacks ``chaos:invoke``.

    Carrying it is a read of the answer key, not merely a wider grant:
    ``list_audit_events`` would return the hook and its arguments, stamped seconds
    before the alert (divergence G3, platform ADR 0012's 2026-09-15 amendment, O-4).
    Called by ``assert_write_capable_principal``, and by the runner for a live
    selection that seeds chaos without declaring a Tier-1 action.
    """
    _assert_scope_absent(
        client,
        label="agent chaos-blindness guard",
        probe_tool=_CHAOS_PROBE_TOOL,
        probe_args=_CHAOS_PROBE_ARGS,
        scope=_AGENT_FORBIDDEN_SCOPE,
        carried_consequence=(
            "so the platform will serve it the chaos.% audit rows — the agent "
            "can read which hook was fired against which resource seconds "
            "before its own alert, and no diagnosis on this run means "
            "anything. This is PLATFORM_CHAOS_TOKEN pasted into "
            "PLATFORM_TOKEN, or an incident-commander account the bootstrap "
            "has not re-scoped yet: run `make bootstrap-token` and paste the "
            "PLATFORM_TOKEN line it prints."
        ),
        lab_probe_reason=_CHAOS_BLIND_PROBE_REASON,
        lab_principal_token=lab_principal_token,
    )


def assert_chaos_capable_principal(
    client: LabProbeCapableClient, *, lab_principal_token: str | None = None
) -> None:
    """Hard-fail unless the client's token genuinely carries ``chaos:invoke``.

    A ``chaos_setup``-only scenario declares no ``expected_action_tools``, so the
    write guard never fired for it and the wrongness surfaced inside
    ``run_scenario``, with the archive open. Probing ``actions:execute`` here would
    be wrong both ways. Runs on the EVALUATOR's client (``PLATFORM_CHAOS_TOKEN``):
    the agent's token must FAIL this probe, the runner's must pass it.
    """
    _assert_scope_carried(
        client,
        label="chaos guard",
        probe_tool=_CHAOS_PROBE_TOOL,
        probe_args=_CHAOS_PROBE_ARGS,
        scope="chaos:invoke",
        refusal_consequence=(
            "so the selected scenario would start, seed nothing, and crash "
            "on ChaosInvocationError with the run already under way. This is "
            "the evaluator's own principal: run `make bootstrap-token` and "
            "paste the PLATFORM_CHAOS_TOKEN line it prints."
        ),
        unreached_hint=(
            f"Most likely the platform was booted with CHAOS_ENABLED=false, "
            f"so {_CHAOS_PROBE_TOOL} is not registered at all — in which case "
            "seeding cannot work either."
        ),
        lab_probe_reason=_CHAOS_CAPABLE_PROBE_REASON,
        lab_principal_token=lab_principal_token,
    )


def _assert_scope_absent(
    client: LabProbeCapableClient,
    *,
    label: str,
    probe_tool: str,
    probe_args: dict[str, Any],
    scope: str,
    carried_consequence: str,
    lab_probe_reason: str,
    lab_principal_token: str | None,
) -> None:
    """Shared body of the two negative guards: prove one scope is NOT carried.

    Only a ``-32002`` scope refusal passes. The other outcomes fail for different
    REASONS and each says so, because "the token carries chaos:invoke" and "chaos is
    switched off" send an operator to different files. Shared, not copied: the
    fail-open bug that made the write guard vacuous is what a second copy reintroduces.
    """
    reason, token = _label(lab_probe_reason, lab_principal_token)
    try:
        result = client.call_tool(
            probe_tool, probe_args, lab_probe=reason, lab_principal_token=token
        )
    except LabProbeRefused:
        # A refused LABEL is a request bug, not a verdict about the scope: the call
        # never ran, and its -32602 is the code the clause below reads as "the scope
        # check passed". Loud, unretried, and never re-sent unlabelled — a silent
        # unlabelled retry is exactly how F4's rows became the agent's.
        raise
    except MCPError as err:
        if err.code == _SCOPE_REFUSAL_CODE and "scope" in str(err).lower():
            return
        if err.code in _ARGUMENT_REFUSAL_CODES:
            raise PrincipalGuardError(
                f"{label}: the negative probe on {probe_tool} was refused on its "
                f"ARGUMENTS (MCPError {err.code}: {err}), which means the scope "
                f"check passed. The token carries {scope}, {carried_consequence}"
            ) from err
        raise PrincipalGuardError(
            f"{label}: the negative probe on {probe_tool} failed with MCPError "
            f"{err.code}: {err} — neither the scope refusal "
            f"({_SCOPE_REFUSAL_CODE}) this probe is built to elicit nor the "
            "argument-validation refusal "
            f"({', '.join(str(c) for c in sorted(_ARGUMENT_REFUSAL_CODES))}) "
            "that would prove the opposite. It never reached argument "
            f"validation, so it proves nothing about {scope}. Failing closed — "
            "the run does not proceed on an unverified control."
        ) from err
    except Exception as err:  # noqa: BLE001 — fail closed, deliberately
        # Anything at all: an unverified guard is an unmet precondition, not a
        # warning. A safety check that shrugs is the bypass F-001 is about.
        raise PrincipalGuardError(
            f"{label}: could not verify the principal "
            f"({type(err).__name__}: {err}). Failing closed — the run does "
            "not proceed on an unverified control."
        ) from err
    raise PrincipalGuardError(
        f"{label}: the negative probe on {probe_tool} SUCCEEDED "
        f"(result: {str(result)[:200]}). A deliberately invalid call must never "
        f"be accepted, and it was not refused on scope: the token carries "
        f"{scope}, {carried_consequence}"
    )


def _assert_scope_carried(
    client: LabProbeCapableClient,
    *,
    label: str,
    probe_tool: str,
    probe_args: dict[str, Any],
    scope: str,
    refusal_consequence: str,
    unreached_hint: str,
    lab_probe_reason: str,
    lab_principal_token: str | None,
) -> None:
    """Shared body of the two positive guards: prove one scope is carried.

    One implementation, two configurations, because the fail-open bug the write guard
    shipped with is what a second hand-written copy would reintroduce.
    """
    reason, token = _label(lab_probe_reason, lab_principal_token)
    try:
        result = client.call_tool(
            probe_tool, probe_args, lab_probe=reason, lab_principal_token=token
        )
    except LabProbeRefused:
        # Same reason as the negative guard: here a -32602 is the PASS condition, so a
        # refused label would read as "the principal can act" — the most misleading
        # outcome of the four. Raised as itself.
        raise
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

    ``list_audit_events`` has no ``offset`` and no ``created_after`` (verified:
    sending one is refused ``-32602 extra_forbidden``), so rows older than the newest
    200 are unreachable after the fact. Repeated reads DURING the stage compose:
    coverage is the interval ``[_covered_from, _covered_upto]``, extended when a
    truncated page reaches back to it, restarted at that page's oldest row when it
    does not, latched ``_complete`` by an untruncated page. Both bounds come off the
    ROWS, never off a clock, so the guard needs no agreement about the time.
    """

    def __init__(self, since: datetime, *, lab_principal_token: str | None = None) -> None:
        self.since = since
        # The scan's own reads are the lab's too, on the same token as the stage, so
        # they carry the same label: rows this guard writes while reading must not
        # show up as the agent's work on the demo page.
        self._lab_principal_token = lab_principal_token
        self.checkpoints = 0
        # Only in-window rows are retained: an older row can never be a violation,
        # and its timestamp has already extended coverage by the time it is dropped.
        self._rows: dict[str, AuditEventEntry] = {}
        self._covered_from: datetime | None = None
        self._covered_upto: datetime | None = None
        self._complete = False
        self._overflowed = False
        self._last_page: tuple[int, int] = (0, 0)

    def checkpoint(self, client: LabProbeCapableClient) -> None:
        """Read one page and fold it into the scan. Raises what the client raises."""
        reason, token = _label(_AUDIT_SCAN_REASON, self._lab_principal_token)
        result = client.call_tool(
            "list_audit_events",
            {
                "action": "agent.tool_invoked",
                "principal_type": "service_account",
                "limit": _AUDIT_PAGE_LIMIT,
            },
            lab_probe=reason,
            lab_principal_token=token,
        )
        total, events = _parse_events(result)
        self.checkpoints += 1
        self._last_page = (len(events), total)
        self._merge(events)
        # ``total`` is an unlimited COUNT over the same filter (platform
        # ``repositories/audit.py:86``), so ``total > len(events)`` is the server
        # saying it withheld rows — honest even if the cap moves off 200.
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
            # More than a page landed since the last checkpoint, so the rows between
            # them are gone. Coverage restarts here rather than spanning the hole.
            self._covered_from = page_oldest
            self._covered_upto = page_newest

    def _merge(self, events: list[AuditEventEntry]) -> None:
        """Fold a page's in-window rows into the scan, deduped by id and capped."""
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
                if tool_of(e) in _TIER_1_TOOLS
                and outcome_of(e) == "success"
                and e.created_at >= self.since
                and (owned is None or str(e.principal_id) in owned)
            ),
            key=lambda e: e.created_at,
        )

    def inconclusive_reason(self) -> str:
        """The message explaining why this scan cannot vouch for the whole window."""
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
    client: LabProbeCapableClient,
    since: datetime,
    *,
    principal_ids: Collection[str] | None = None,
    scan: AuditWindowScan | None = None,
    lab_principal_token: str | None = None,
) -> list[AuditEventEntry]:
    """Fail if the platform audit records any successful Tier-1 call since ``since``.

    Graded from the platform's audit log, not the agent's trajectory (invariant 6).
    ``principal_ids`` are the principals THIS stage owns — BOTH the agent and smoke
    SAs, since the F-001 rows carry the AGENT principal — and omitting them leaves
    the guard deliberately over-broad. ``scan`` is the caller's in-stage
    ``AuditWindowScan``; without one a single post-stage page is inconclusive above
    200 rows. Returns the offending rows, or raises ``PrincipalGuardError``.
    """
    if scan is None:
        scan = AuditWindowScan(since, lab_principal_token=lab_principal_token)
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
        # An audit query we couldn't run proves nothing: inconclusive is a failure.
        raise PrincipalGuardError(
            "post-stage audit could not be read "
            f"({type(err).__name__}: {err}); treating as a failure — an "
            "unverifiable stage is not a clean stage."
        ) from err
    violations = scan.violations(principal_ids)
    if not scan.fully_scanned:
        # A-13: the rows we could not fetch may hold the successes this exists to
        # catch, so inconclusive is a failure — and anything already visible is
        # named rather than swallowed.
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
        f"{tool_of(e)}@{e.created_at.isoformat()} by {e.principal_id}" for e in violations[:5]
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

    Not a hand-rolled dict walk: the first version read ``payload["items"]`` against
    a ``{"total", "events"}`` payload, so it returned zero events on every real call
    and the assertion passed unconditionally (F-004). Through the registry model the
    contract test protects this guard too, and an unrecognized payload raises rather
    than yielding nothing. Returns ``(total, events)``; ``total`` is the unlimited
    COUNT, which is how the caller learns the page was truncated.
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


def tool_of(event: AuditEventEntry) -> str:
    """The tool one audit row names. Public: `evals/reward.py` reads rows too."""
    extra = event.extra_data or {}
    return str(extra.get("tool_name", ""))


def outcome_of(event: AuditEventEntry) -> str:
    """Whether the platform served that invocation. `success` or anything else."""
    extra = event.extra_data or {}
    return str(extra.get("outcome", ""))


def arguments_of(event: AuditEventEntry) -> Mapping[str, Any]:
    """The arguments the platform recorded. Untrusted content, read structurally."""
    extra = event.extra_data or {}
    arguments = extra.get("arguments")
    return arguments if isinstance(arguments, Mapping) else {}
