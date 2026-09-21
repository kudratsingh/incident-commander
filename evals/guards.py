"""Point-of-use assertions for the eval runner's effective principal.

Run 001 stage 1 ran with full write scope while every label said "read-scoped smoke" (F-001),
so **a control is asserted where it is used**, and each stage probes the one scope it needs.
Chaos-blindness is load-bearing: the platform hides the ``chaos.%`` rows — the answer key —
only from principals that genuinely lack the scope (O-4). Each probe also SAYS it is the
lab's (``lab_principal_token``, platform ADR 0038), or its row reads as the agent's (F4).
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

# Deliberately invalid arguments. The handler checks scope BEFORE parsing them, so the two
# outcomes are distinguishable and neither can execute.
_PROBE_ARGS: Final[dict[str, Any]] = {
    "job_id": "00000000-0000-0000-0000-000000000000-INVALID",
    "reason": "",
    "idempotency_key": "x",
}

_SCOPE_REFUSAL_CODE: Final[int] = -32002

# The only JSON-RPC codes that mean "the platform accepted this principal and then rejected the
# arguments". Any other code means the call never got as far as argument checking, so it says
# nothing about the scope: treating one as a pass is how these guards once went green on a
# probe tool that no longer existed.
_ARGUMENT_REFUSAL_CODES: Final[frozenset[int]] = frozenset({-32602})

# The probe tool for the chaos half. A scenario that only seeds a fault executes no Tier-1
# action, so asking whether the token can act is the wrong question there. ``inject_latency``
# is used because it is the least destructive hook: one named group, and it expires by itself.
_CHAOS_PROBE_TOOL: Final[str] = "inject_latency"

# Invalid twice over against the hook's committed inputSchema (minLength 1, and a type error
# that cannot be coerced into anything that seeds). ``test_guards.py`` pins both.
_CHAOS_PROBE_ARGS: Final[dict[str, Any]] = {
    "consumer_group": "",
    "latency_ms": "not-a-latency",
}

# The one scope the agent under test must never hold. With it the platform serves the agent the
# chaos audit rows, so the agent could read which fault was injected against which resource
# seconds before its own alert — the answer key. Spelled once, because this probe and the
# platform's own ``hidden_audit_action_prefixes`` check have to agree on the exact string.
_AGENT_FORBIDDEN_SCOPE: Final[str] = "chaos:invoke"
# Derived from the tier map rather than written out again here: a second hand-written list of
# Tier-1 tool names is one more copy that can fall out of step with the first.
_TIER_1_TOOLS: Final[frozenset[str]] = tools_at_or_below(Tier.TIER_1) - tools_at_or_below(Tier.READ)

# The largest page the platform will serve (its ``ListAuditEventsInput.limit`` is capped at 200).
# This is the platform's number, not a tuning choice, and both the request and the "was the page
# full?" check below use it.
_AUDIT_PAGE_LIMIT: Final[int] = 200

# How many audit rows one stage may hold in memory before this guard stops merging them. Not a
# tuning knob: 2000 rows out of a read-only stage is already abnormal, so reaching it makes the
# stage report "cannot tell" instead of "clean".
_AUDIT_SCAN_ROW_CAP: Final[int] = 2000


# The sentence each probe sends along with itself, so the platform records it in the audit log
# as a lab probe (``lab.probe``) instead of as the agent invoking a tool. Each one names what
# its probe proves, because this text is what an operator reads out of the log months later.
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

    No credential means no label rather than a refused call. The offline fakes and the
    pre-v0.6.17 path pass ``None``.
    """
    return (reason, lab_principal_token) if lab_principal_token else (None, None)


def assert_read_only_principal(
    client: LabProbeCapableClient, *, lab_principal_token: str | None = None
) -> None:
    """Hard-fail unless the client's token genuinely lacks write scope.

    Negative probe on a Tier-1 tool with invalid arguments: only a ``-32002`` scope refusal
    passes. Safe by construction, because the scope check precedes argument parsing.
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

    The mirror of ``assert_read_only_principal``: only an argument refusal (``-32602``)
    passes. Without it a read-scoped remediation stage grades every scenario red on ACTION
    after full spend. Also asserts chaos-blindness: the right principal is two claims (O-4).
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

    Carrying it reads the answer key: ``list_audit_events`` would return the hook and its
    arguments, stamped seconds before the alert (G3, platform ADR 0012 amended, O-4).
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

    For a ``chaos_setup``-only scenario, which declares no ``expected_action_tools`` and so
    never reached the write guard. Runs on the EVALUATOR's client (``PLATFORM_CHAOS_TOKEN``):
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

    Only a ``-32002`` scope refusal passes, and each other outcome says why it failed:
    "the token carries chaos:invoke" and "chaos is switched off" send an operator to
    different files. Shared, not copied — a second copy reintroduces the fail-open bug.
    """
    reason, token = _label(lab_probe_reason, lab_principal_token)
    # 1. Make the one deliberately invalid call, labelled as the lab's own probe.
    try:
        result = client.call_tool(
            probe_tool, probe_args, lab_probe=reason, lab_principal_token=token
        )
    # 2. If the platform rejected the LABEL, that is a bug in how this request was built and says
    #    nothing about the scope, so give up rather than quietly retrying without the label.
    except LabProbeRefused:
        raise
    except MCPError as err:
        # 3. The platform refused the call because this token lacks the scope. That is exactly
        #    what the guard set out to prove, so the check passes here.
        if err.code == _SCOPE_REFUSAL_CODE and "scope" in str(err).lower():
            return
        # 4. The platform got as far as checking the arguments, which means it accepted the
        #    principal: the token DOES carry the scope, and the run must not start.
        if err.code in _ARGUMENT_REFUSAL_CODES:
            raise PrincipalGuardError(
                f"{label}: the negative probe on {probe_tool} was refused on its "
                f"ARGUMENTS (MCPError {err.code}: {err}), which means the scope "
                f"check passed. The token carries {scope}, {carried_consequence}"
            ) from err
        # 5. Any other error code means the call never reached argument checking, so it proves
        #    nothing about the scope: raise, and the caller refuses to start the run.
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
    # 6. Anything else at all leaves the question unanswered, which counts as a failure: a safety
    #    check that shrugs and lets the run continue is the hole this guard exists to close.
    except Exception as err:  # noqa: BLE001 — fail closed, deliberately
        raise PrincipalGuardError(
            f"{label}: could not verify the principal "
            f"({type(err).__name__}: {err}). Failing closed — the run does "
            "not proceed on an unverified control."
        ) from err
    # 7. And if the invalid call SUCCEEDED, the platform accepted arguments it should have
    #    rejected: the token holds the scope and the probe is no longer safe to fire, so raise.
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
    # 1. Make the same deliberately invalid call, labelled as the lab's own probe.
    try:
        result = client.call_tool(
            probe_tool, probe_args, lab_probe=reason, lab_principal_token=token
        )
    # 2. A rejected LABEL matters more here than in the negative guard: an argument refusal is
    #    this guard's PASS, so a label refusal carrying the same code would read as "can act".
    except LabProbeRefused:
        raise
    except MCPError as err:
        # 3. The platform refused on scope, so this token cannot act. That is this guard's
        #    failure: raise, and the caller refuses to start the run rather than grading it red.
        if err.code == _SCOPE_REFUSAL_CODE and "scope" in str(err).lower():
            raise PrincipalGuardError(
                f"{label}: the negative probe was refused on SCOPE "
                f"(MCPError {err.code}: {err}). This token lacks {scope}, "
                f"{refusal_consequence}"
            ) from err
        # 4. The platform got as far as checking the arguments, which means it accepted the
        #    principal: the token can act, so return and let the run start.
        if err.code in _ARGUMENT_REFUSAL_CODES:
            return
        # 5. Any other error code means the call never reached argument checking, so it proves
        #    nothing about the scope: raise, and the caller refuses to start the run.
        raise PrincipalGuardError(
            f"{label}: the negative probe on {probe_tool} failed with MCPError "
            f"{err.code}: {err} — neither the scope refusal "
            f"({_SCOPE_REFUSAL_CODE}) nor the argument-validation refusal "
            f"({', '.join(str(c) for c in sorted(_ARGUMENT_REFUSAL_CODES))}) "
            "this probe is built to elicit. It never reached argument "
            f"validation, so it proves nothing about {scope}. {unreached_hint} "
            "Failing closed — the run does not proceed on an unverified control."
        ) from err
    # 6. Anything else at all leaves the question unanswered, which counts as a failure here too:
    #    the run does not start on a control nobody managed to check.
    except Exception as err:  # noqa: BLE001 — fail closed, deliberately
        raise PrincipalGuardError(
            f"{label}: could not verify the principal "
            f"({type(err).__name__}: {err}). Failing closed — the run does "
            "not proceed on an unverified control."
        ) from err
    # 7. And if the invalid call SUCCEEDED, the platform's argument checking has moved and this
    #    probe is no longer safe to fire at all, so raise instead of reporting a pass.
    raise PrincipalGuardError(
        f"{label}: the negative probe SUCCEEDED "
        f"(result: {str(result)[:200]}). A deliberately invalid "
        f"{probe_tool} call must never be accepted — the probe is no longer "
        "safe to fire and the platform's argument validation has moved. "
        "Refusing to run."
    )


class AuditWindowScan:
    """The union of every audit page read across one stage.

    ``list_audit_events`` has no ``offset`` and no ``created_after``, so rows older than the
    newest 200 are unreachable after the fact and reads DURING the stage have to compose.
    Coverage is ``[_covered_from, _covered_upto]``, restarted when a truncated page does not
    reach back to it. Both bounds come off the ROWS, so the guard needs no clock.
    """

    def __init__(self, since: datetime, *, lab_principal_token: str | None = None) -> None:
        self.since = since
        # This scan's own reads are the lab's as well, so they carry the same label. Without it
        # the rows this guard writes while reading would show up in the log as the agent's work.
        self._lab_principal_token = lab_principal_token
        self.checkpoints = 0
        # Only rows inside the stage's own time window are kept. A row older than the window
        # cannot be a violation, and its timestamp has already widened the covered range by the
        # time it is dropped, so nothing is lost by not storing it.
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
        # ``total`` is the server's unlimited count over the same filter, so ``total`` being
        # larger than the page is the server telling us it held rows back. That reading stays
        # correct even if the platform's page cap moves off 200.
        if not (total > len(events) or len(events) >= _AUDIT_PAGE_LIMIT):
            self._complete = True
            return
        if not events:
            # The server said it withheld rows and then returned none, which cannot both be
            # true. Return without extending the covered range, so this claims nothing.
            return
        page_oldest = min(e.created_at for e in events)
        page_newest = max(e.created_at for e in events)
        if self._covered_upto is not None and page_oldest <= self._covered_upto:
            self._covered_upto = max(self._covered_upto, page_newest)
        else:
            # More than one page of rows landed since the last checkpoint, so the rows in
            # between can never be fetched. Start the covered range again at this page rather
            # than claiming to cover the gap.
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
    ``principal_ids`` are the principals THIS stage owns, BOTH of them, and omitting them
    leaves the guard deliberately over-broad. Without ``scan``, a single post-stage page is
    inconclusive above 200 rows.
    """
    # 1. Take the caller's scan if it covers this exact window, or start one; a scan that began
    #    at a different moment would be graded against a window it never watched.
    if scan is None:
        scan = AuditWindowScan(since, lab_principal_token=lab_principal_token)
    elif scan.since != since:
        raise ValueError(
            f"scan covers {scan.since.isoformat()} but the assertion was asked "
            f"about {since.isoformat()}; a window graded against the wrong "
            "start is not a graded window."
        )
    # 2. Read one last page and fold it in. If that read fails, the audit log could not be
    #    checked at all, so raise: an unverifiable stage is not a clean stage.
    try:
        scan.checkpoint(client)
    except PrincipalGuardError:
        raise
    except Exception as err:  # noqa: BLE001 — fail closed, deliberately
        raise PrincipalGuardError(
            "post-stage audit could not be read "
            f"({type(err).__name__}: {err}); treating as a failure — an "
            "unverifiable stage is not a clean stage."
        ) from err
    violations = scan.violations(principal_ids)
    # 3. If any part of the window went unread, raise: the rows nobody could fetch may hold the
    #    very Tier-1 successes this looks for. The message names the ones already visible too.
    if not scan.fully_scanned:
        raise PrincipalGuardError(scan.inconclusive_reason() + _visible_suffix(violations))
    # 4. The window was fully read, so the audit log can answer: raise if it recorded any
    #    successful Tier-1 action by this stage's principals, and return the empty list if not.
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

    Never a hand-rolled dict walk: the first version read ``payload["items"]`` against a
    ``{"total", "events"}`` payload, returned zero events on every real call and passed the
    assertion unconditionally (F-004). ``total`` is the unlimited COUNT, which is how the
    caller learns the page was truncated.
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
