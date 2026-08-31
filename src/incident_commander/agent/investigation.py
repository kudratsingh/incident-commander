"""INVESTIGATING transitions.

Two flavors:

- ``make_investigate`` — Phase 0 deterministic: one hard-coded probe
  (``get_consumer_lag``), then escalate. Runner still uses this until the
  scenario library ships canned LLM responses.
- ``make_llm_investigate`` — Phase 2 hypothesis engine: LLM ranks hypotheses,
  picks probes, decides continue-or-stop. Multi-probe capable.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any, Final, NamedTuple
from uuid import UUID

from pydantic import BaseModel, ValidationError

from incident_commander.agent.accounting import accrue_llm_error, accrue_llm_usage
from incident_commander.agent.hypothesis import (
    Hypothesis,
    HypothesisCategory,
    InvestigationStep,
    ProbeAction,
    RemediateAction,
    StopAction,
)
from incident_commander.agent.state import (
    EvidenceEntry,
    IncidentState,
    RunState,
)
from incident_commander.llm.client import LLMClientProtocol, LLMError
from incident_commander.llm.prompts.loader import load_prompt
from incident_commander.tools.mcp_client import MCPClientProtocol, MCPError, ToolResult
from incident_commander.tools.policies import Tier, is_cached_read, tier_of, tools_at_or_below
from incident_commander.tools.registry import TOOL_REGISTRY, description_of
from incident_commander.tools.wire import wire_arguments

_TOOL_NAME: Final[str] = "get_consumer_lag"
# The bookkeeping marker for a Phase-1 escalation, distinct from the tool
# that failed. Underscore-prefixed on purpose: that prefix is the repo-wide
# convention for "this evidence entry is a marker, not a probe result", and
# it is what `briefing._terminal_marker` reads a reason from and what the
# trail filter excludes. Recording an escalation under the *tool's* name
# lost the reason at the handoff and put a failed call in the trail as if it
# had returned data (WO-R2-119).
#
# Renaming the marker rather than widening the recognizer is deliberate: the
# success path below also ends ESCALATED, with a real `get_consumer_lag`
# result as its last entry, so a recognizer that accepted non-underscore
# markers would report that probe's JSON as the reason the agent gave up.
_ESCALATION_MARKER: Final[str] = "_investigate_escalate"
_DEFAULT_MAX_ITERATIONS: Final[int] = 5
_REMEDIATE_CONFIDENCE_THRESHOLD: Final[float] = 0.7

# How many times one investigation may have its remediate handoff refused
# for never having probed the alert's subject before the run escalates
# instead. A refusal is not a failure — it is a steer, and the planner gets
# to act on it (the reason lands in the evidence trail the next planner
# context renders). But a planner that re-emits `remediate` after being
# told twice is not going to probe on the third ask, and burning the
# remaining iterations to arrive at the generic "max iterations exceeded"
# throws away the one diagnosis worth putting in the briefing.
_MAX_SUBJECT_PROBE_REFUSALS: Final[int] = 2


# Single source of truth for category → Tier-1 tool routing.
#
# Categories in this map auto-remediate when the top hypothesis crosses
# the confidence threshold. Categories NOT in this map — even at 1.0
# confidence — auto-escalate to a human with a briefing. Adding a new
# category to HypothesisCategory without adding it here is deliberate;
# use that shape for observation-only classifications (e.g. UNKNOWN,
# DEPLOY_REGRESSION) where auto-remediation would be wrong.
#
# Remediation planner selects the *specific* tool (e.g. for POISON_MESSAGE
# it may pick replay_dlq_by_ids, replay_dlq_by_category, or mark_dlq_permanent
# depending on the DLQ's remediation_hint contents). This map only asserts
# "a Tier-1 fix category exists" — not the exact tool.
FIX_MAP: Final[dict[HypothesisCategory, str]] = {
    HypothesisCategory.CONSUMER_SATURATION: "restart_consumer_group",
    HypothesisCategory.POISON_MESSAGE: "replay_dlq_by_ids",
    HypothesisCategory.STALE_CACHE: "invalidate_cache_key",
    HypothesisCategory.RUNAWAY_SAGA: "pause_dag",
}


# Single source of truth for alert-field → subject-probe routing.
#
# An alert names the resource it is about in one of these payload fields.
# The value is the resource; the pair is (the read tool that observes that
# resource, the argument field the value belongs in). Together they answer
# one question mechanically: "which exact probe call would read the thing
# this alert is complaining about?"
#
# Keyed on the alert FIELD rather than on `fingerprint`. Fingerprints are
# free text the platform's alert rules author — this corpus alone spells
# one family three ways (`consumer_lag_high`, `consumer_lag_warning`,
# `consumer_stalled`), so a fingerprint-keyed map could only match by
# prefix or substring, which is the heuristic this guard exists to avoid.
# Field names are the closed vocabulary: they are the commander's own
# ingress model (`api/schemas.AlertPayload`) plus the scenario corpus's
# `_NON_WEBHOOK_ALERT_FIELDS` (tests/unit/test_scenario_alert_premise.py),
# and — decisively — the field is what carries the subject's VALUE, which
# the guard needs anyway. Knowing "this is a consumer-lag alert" is not
# enough; the 2026-08-30 live run probed `get_consumer_lag` for the
# platform's DEFAULT group while the alert named `unknown-consumer`, and
# only the value comparison catches that.
#
# Declaration order is priority order: if an alert ever names two mappable
# resources, the first entry present wins and becomes "the subject". The
# guard demands one verified read of the alert's primary subject, not a
# probe of everything the alert mentions — demanding all of them would
# turn a richer alert into a blocked investigation.
#
# The tool/argument halves must stay consistent with
# `policies.RESOURCE_ARG_FIELDS` (the same fields, seen from the tool
# side); `tests/unit/test_policies.py::TestAlertSubjectProbes` pins that.
ALERT_SUBJECT_PROBES: Final[dict[str, tuple[str, str]]] = {
    "consumer_group": ("get_consumer_lag", "consumer_group"),
    "group": ("get_consumer_lag", "consumer_group"),
    "cache_key": ("get_cache_key_info", "key"),
    "job_id": ("get_dag_state", "job_id"),
    "trace_id": ("get_trace", "trace_id"),
}


class AlertSubject(NamedTuple):
    """The resource an alert is about, and the probe call that reads it."""

    alert_field: str
    """Which payload field named it — quoted back to the planner."""
    tool_name: str
    """Read tool that observes this resource."""
    argument_field: str
    """The tool argument the value belongs in."""
    value: str
    """The resource name, verbatim from the alert."""


def alert_subject(alert: Mapping[str, Any]) -> AlertSubject | None:
    """The alert's own subject, or ``None`` when it names nothing mappable.

    ``None`` is the inert case and it is common and legitimate: a DLQ-depth
    alert, an alert-storm meta-alert, and a `db_latency_high` alert all name
    a *condition* rather than a resource this agent can probe by name. Every
    caller must treat ``None`` as "no opinion" — a guard that fabricated a
    subject for those alerts would block investigations it knows nothing
    about, which is worse than the gap it closes.

    Looks at the top level first, then one level into ``extra_data``. That
    second lookup is not speculative: the platform's alert webhook sends
    exactly ``{alert_id, tenant_id, severity, source, title, description,
    fired_at, extra_data}``, so on a real alert every resource-naming field
    in the map above arrives nested, while the scenario corpus carries them
    at the top level (the divergence is written up in
    ``tests/unit/test_scenario_alert_premise.py::_NON_WEBHOOK_ALERT_FIELDS``).
    Reading only the top level would leave this guard permanently inert in
    production while looking green offline — the exact shape of fail-open
    that makes a guard worse than no guard.
    """
    for source in (alert, alert.get("extra_data")):
        if not isinstance(source, Mapping):
            continue
        for field, (tool_name, argument_field) in ALERT_SUBJECT_PROBES.items():
            raw = source.get(field)
            # str and UUID only: an alert arriving as JSON gives strings,
            # and a run state assembled in Python may hold a real UUID for
            # `job_id`/`trace_id`. Numbers and booleans do not name a
            # platform resource, and a Mapping is a nested payload, not a
            # name.
            if not isinstance(raw, (str, UUID)):
                continue
            value = str(raw).strip()
            if value:
                return AlertSubject(field, tool_name, argument_field, value)
    return None


def make_investigate(
    mcp_client: MCPClientProtocol,
) -> Callable[[RunState, datetime], RunState]:
    """Bind an MCP client to the INVESTIGATING transition function."""

    def transition_investigate(run_state: RunState, at: datetime) -> RunState:
        spec = TOOL_REGISTRY[_TOOL_NAME]
        # Accept legacy `group` field for backward-compat with older alert
        # producers; platform's tool arg is `consumer_group`.
        raw = run_state.alert.get("consumer_group") or run_state.alert.get("group")
        # One canonical serialization for every outgoing call (wire.py's
        # docstring: a second implementation drifts from the thing it
        # guards). This leg is read-only and its default-fill is
        # deliberate — an alert with no group named probes the platform's
        # default group. The remediation legs may NOT default-fill; that
        # asymmetry is the subject of ADR 0022.
        arguments = wire_arguments(spec, {"consumer_group": str(raw)} if raw else {})

        try:
            result = mcp_client.call_tool(_TOOL_NAME, arguments)
        except MCPError as err:
            return _escalate(run_state, at, f"tool error: {err}", arguments)

        if result.is_error:
            return _escalate(run_state, at, "tool reported is_error=True", arguments)

        try:
            output = _parse_output(spec.output_model, result.content)
        except (ValueError, ValidationError) as err:
            return _escalate(run_state, at, f"output parse failed: {err}", arguments)

        entry = EvidenceEntry(
            tool_name=_TOOL_NAME,
            arguments=arguments,
            result_summary=output.model_dump_json(),
            timestamp=at,
        )
        new_budget = run_state.budget.model_copy(
            update={"tool_calls_used": run_state.budget.tool_calls_used + 1}
        )
        return run_state.model_copy(
            update={
                "state": IncidentState.ESCALATED,
                "updated_at": at,
                "evidence": (*run_state.evidence, entry),
                "budget": new_budget,
            }
        )

    return transition_investigate


def _parse_output(model: type[BaseModel], content: list[dict[str, Any]]) -> BaseModel:
    """Parse the first text block as JSON into the tool's output model."""
    for block in content:
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            payload = json.loads(block["text"])
            return model.model_validate(payload)
    raise ValueError("no text content block in tool result")


def _escalate(
    run_state: RunState, at: datetime, reason: str, arguments: dict[str, Any]
) -> RunState:
    entry = EvidenceEntry(
        tool_name=_ESCALATION_MARKER,
        arguments=arguments,
        result_summary=reason,
        timestamp=at,
    )
    return run_state.model_copy(
        update={
            "state": IncidentState.ESCALATED,
            "updated_at": at,
            "evidence": (*run_state.evidence, entry),
        }
    )


# ---------------------------------------------------------------------------
# Phase 2: LLM-driven multi-probe investigation loop.


def make_llm_investigate(
    mcp_client: MCPClientProtocol,
    llm_client: LLMClientProtocol,
    model: str,
    max_iterations: int = _DEFAULT_MAX_ITERATIONS,
    reprobe_attempts: int = 0,
    reprobe_delay_seconds: float = 0.0,
    sleep: Callable[[float], None] = time.sleep,
) -> Callable[[RunState, datetime], RunState]:
    """Bind clients + model to the Phase 2 INVESTIGATING transition.

    Each iteration: the LLM ranks hypotheses and either proposes a probe or
    says stop. Budget is checked before every LLM call and every tool call;
    exhaustion escalates immediately. ``max_iterations`` guards against loops.

    ``reprobe_attempts`` is the investigation-side twin of ADR 0006's verify
    polling (ADR 0009): when a probe of a declared-cached tool (see
    ``policies.CACHED_READ_FRESHNESS_SECONDS``) kills a fixable hypothesis
    that was at or above the remediate threshold, the loop re-reads that
    same probe after ``reprobe_delay_seconds`` before accepting the
    contradiction — at most ``reprobe_attempts`` times per tool per run.
    Default 0 preserves canned behavior byte-identically; the runner wires
    it live, where a cached reading can predate the fault entirely.
    """

    def transition_llm_investigate(run_state: RunState, at: datetime) -> RunState:
        last_probe: ProbeAction | None = None
        reprobes_spent: dict[str, int] = {}
        subject = alert_subject(run_state.alert)
        refusals_spent = 0
        for _ in range(max_iterations):
            if run_state.budget.is_exhausted:
                return _escalate_investigation(run_state, at, "budget exhausted mid-investigation")

            prior_hypotheses = run_state.hypotheses
            try:
                run_state, step = _plan_next_step(run_state, at, llm_client, model)
            except (ValueError, ValidationError, LLMError) as err:
                # ``_plan_next_step`` accrues on its way out, so a call that
                # raises accrues nothing — and this is the run's hottest LLM
                # call, made once per investigation iteration. Charge what the
                # failed call already billed before escalating (ADR 0015).
                run_state = run_state.model_copy(
                    update={"budget": accrue_llm_error(run_state.budget, err, model)}
                )
                return _escalate_investigation(run_state, at, f"planner output invalid: {err}")

            killed = _cached_probe_contradiction(prior_hypotheses, step.hypotheses, last_probe)
            if (
                killed is not None
                and last_probe is not None
                and reprobes_spent.get(last_probe.tool_name, 0) < reprobe_attempts
            ):
                # Do not act on this step yet — the hypothesis died on the
                # word of a possibly-stale sensor. Re-read it fresh first;
                # the next planner iteration sees both readings and decides.
                reprobes_spent[last_probe.tool_name] = (
                    reprobes_spent.get(last_probe.tool_name, 0) + 1
                )
                run_state = _note_freshness_reprobe(
                    run_state, at, last_probe, killed, reprobe_delay_seconds
                )
                sleep(reprobe_delay_seconds)
                if run_state.budget.is_exhausted:
                    return _escalate_investigation(
                        run_state, at, "budget exhausted before freshness re-probe"
                    )
                run_state = _execute_probe(run_state, at, mcp_client, last_probe)
                if run_state.state is IncidentState.ESCALATED:
                    return run_state
                continue

            action = step.next_action
            if isinstance(action, StopAction):
                return _finalize(run_state, at, action.reason)
            if isinstance(action, RemediateAction):
                # Structural guard: the LLM emitted `remediate`, but we
                # verify the top hypothesis actually qualifies before
                # handing off to PLANNING. Two conditions:
                #   1. category is a key in FIX_MAP (auto-remediable)
                #   2. confidence >= threshold
                # Either condition failing → escalate with a clear reason.
                # The Pydantic schema already prevented invalid *categories*;
                # this catches the "correct category, insufficient confidence"
                # or "prompt drift emitted remediate for an unmapped category"
                # cases at the state-machine level.
                top = step.hypotheses[0]
                if top.category not in FIX_MAP:
                    return _finalize(
                        run_state,
                        at,
                        (
                            f"planner emitted remediate for category "
                            f"{top.category.value!r} which has no Tier-1 fix; "
                            "escalating"
                        ),
                    )
                if top.confidence < _REMEDIATE_CONFIDENCE_THRESHOLD:
                    return _finalize(
                        run_state,
                        at,
                        (
                            f"planner emitted remediate but top confidence "
                            f"{top.confidence:.2f} is below threshold "
                            f"{_REMEDIATE_CONFIDENCE_THRESHOLD}; escalating"
                        ),
                    )
                # Third structural guard: you may not remediate an incident
                # whose own alerted signal nobody has read. Unlike the two
                # above, this one REFUSES rather than escalates — the run is
                # not over, the planner is simply sent back with the probe it
                # skipped named for it (WO behaviour fix, 2026-08-30).
                if subject is not None and not _alert_subject_probed(run_state, subject):
                    if refusals_spent >= _MAX_SUBJECT_PROBE_REFUSALS:
                        return _finalize(
                            run_state,
                            at,
                            (
                                f"planner emitted remediate {refusals_spent + 1} times "
                                f"without ever probing the alert's subject "
                                f"({subject.alert_field}={subject.value!r}); the alerted "
                                "signal is still unread, so no remediation can be shown "
                                "to address it; escalating"
                            ),
                        )
                    refusals_spent += 1
                    run_state = _refuse_handoff(run_state, at, subject)
                    continue
                return _handoff_to_planning(run_state, at, action.reason)

            # ProbeAction — tool_name is Literal-validated at schema time,
            # so this branch only runs on tools that were in the read-tier
            # slice of TOOL_REGISTRY when the module loaded. The extra
            # runtime check catches the (rare) case where the registry
            # drifts after startup.
            if action.tool_name not in TOOL_REGISTRY:
                return _escalate_investigation(
                    run_state, at, f"planner proposed unknown tool: {action.tool_name}"
                )

            if run_state.budget.is_exhausted:
                return _escalate_investigation(run_state, at, "budget exhausted before probe")

            run_state = _execute_probe(run_state, at, mcp_client, action)
            if run_state.state is IncidentState.ESCALATED:
                # Probe failed; already escalated with the reason.
                return run_state
            last_probe = action

        return _escalate_investigation(run_state, at, f"max iterations ({max_iterations}) exceeded")

    return transition_llm_investigate


def _plan_next_step(
    run_state: RunState,
    at: datetime,
    llm_client: LLMClientProtocol,
    model: str,
) -> tuple[RunState, InvestigationStep]:
    """One planner LLM call. Updates budget + hypotheses + updated_at."""
    result = llm_client.call(
        system_prompt=load_prompt("investigation_planner"),
        user_message=_format_planner_context(run_state),
        output_model=InvestigationStep,
        model=model,
    )
    new_budget = accrue_llm_usage(run_state.budget, result, model)
    updated = run_state.model_copy(
        update={
            "budget": new_budget,
            "hypotheses": result.output.hypotheses,
            "updated_at": at,
        }
    )
    return updated, result.output


def _execute_probe(
    run_state: RunState,
    at: datetime,
    mcp_client: MCPClientProtocol,
    action: ProbeAction,
) -> RunState:
    """Call the tool the planner picked. On any failure, escalate with the reason."""
    spec = TOOL_REGISTRY[action.tool_name]
    # Runtime tier guard (B-06): the ReadToolName Literal keeps the schema
    # read-only, but only tier_of() catches a READ→TIER_1 reclassification
    # made in policies.py after the Literal was hand-listed — the live agent
    # token carries actions:execute, so the platform would permit the call.
    # Placed here so BOTH call sites are covered: the main probe branch and
    # the freshness re-probe (ADR 0009). Mirrors make_llm_plan's
    # defense-in-depth tier checks.
    if tier_of(action.tool_name) is not Tier.READ:
        return _escalate_investigation(
            run_state,
            at,
            f"planner proposed non-read tool as probe: {action.tool_name} "
            f"(tier={tier_of(action.tool_name).value})",
        )
    try:
        # Same canonical serialization the remediation legs use: mode="json"
        # so UUID/datetime fields become str/isoformat (httpx.json can't
        # encode raw UUID objects), and no second copy of the rule to drift.
        arguments = wire_arguments(spec, action.arguments)
    except ValidationError as err:
        return _escalate_investigation(
            run_state, at, f"probe arguments invalid for {action.tool_name}: {err}"
        )

    try:
        result = mcp_client.call_tool(action.tool_name, arguments)
    except MCPError as err:
        return _escalate_investigation(run_state, at, f"tool error ({action.tool_name}): {err}")

    if result.is_error:
        return _escalate_investigation(
            run_state, at, f"tool reported is_error=True ({action.tool_name})"
        )

    try:
        summary = _summarize_probe(spec.output_model, result)
    except (ValueError, ValidationError) as err:
        return _escalate_investigation(
            run_state, at, f"output parse failed ({action.tool_name}): {err}"
        )

    entry = EvidenceEntry(
        tool_name=action.tool_name,
        arguments=arguments,
        result_summary=summary,
        timestamp=at,
    )
    new_budget = run_state.budget.model_copy(
        update={"tool_calls_used": run_state.budget.tool_calls_used + 1}
    )
    return run_state.model_copy(
        update={
            "evidence": (*run_state.evidence, entry),
            "budget": new_budget,
            "updated_at": at,
        }
    )


def _summarize_probe(output_model: type[BaseModel], result: ToolResult) -> str:
    """Parse the tool's typed output and return its compact JSON summary."""
    output = _parse_output(output_model, result.content)
    return output.model_dump_json()


def _cached_probe_contradiction(
    prior: tuple[Hypothesis, ...],
    updated: tuple[Hypothesis, ...] | list[Hypothesis],
    last_probe: ProbeAction | None,
) -> Hypothesis | None:
    """Return the fixable high-prior hypothesis a cached read just killed, if any.

    Trigger requires all of:
    - the last executed probe reads from a declared staleness window,
    - the prior top hypothesis was actionable (category in ``FIX_MAP``)
      and at/above the remediate threshold,
    - the fresh planner output dropped that category below the threshold
      (or dropped it entirely).

    Scoped to FIX_MAP categories deliberately: the re-probe exists to
    protect the remediate handoff decision. Unmapped categories escalate
    to a human either way.
    """
    if last_probe is None or not is_cached_read(last_probe.tool_name):
        return None
    if not prior:
        return None
    prior_top = prior[0]
    if prior_top.category not in FIX_MAP:
        return None
    if prior_top.confidence < _REMEDIATE_CONFIDENCE_THRESHOLD:
        return None
    surviving = max(
        (h.confidence for h in updated if h.category == prior_top.category),
        default=0.0,
    )
    if surviving >= _REMEDIATE_CONFIDENCE_THRESHOLD:
        return None
    return prior_top


def _note_freshness_reprobe(
    run_state: RunState,
    at: datetime,
    probe: ProbeAction,
    killed: Hypothesis,
    delay_seconds: float,
) -> RunState:
    """Record why the loop is re-reading a probe instead of acting (ADR 0009)."""
    reason = (
        f"cached read {probe.tool_name} contradicted actionable hypothesis "
        f"{killed.category.value!r} ({killed.confidence:.2f} >= "
        f"{_REMEDIATE_CONFIDENCE_THRESHOLD}); re-probing after {delay_seconds:g}s "
        "before accepting the contradiction"
    )
    entry = EvidenceEntry(
        tool_name="_freshness_reprobe",
        arguments={"tool": probe.tool_name, "delay_seconds": delay_seconds},
        result_summary=reason,
        timestamp=at,
    )
    return run_state.model_copy(
        update={
            "evidence": (*run_state.evidence, entry),
            "updated_at": at,
        }
    )


def _alert_subject_probed(run_state: RunState, subject: AlertSubject) -> bool:
    """True when some probe in the evidence trail actually read the alert's subject.

    Matching is on the tool AND the argument value, never the tool alone.
    That distinction is the entire guard: on 2026-08-30 the agent answered a
    ``group="unknown-consumer"`` alert by calling ``get_consumer_lag`` — the
    right tool — with no group at all, which ``wire_arguments`` default-fills
    to the platform's ``worker-dispatcher``. It then read a healthy number off
    a consumer nobody had complained about, found a different critical alert
    while it was in there, and chased that instead. "Did it call the tool" is
    green for that run; "did it read the resource the alert named" is not.

    Reading ``entry.arguments`` is what makes that check possible: evidence
    records the WIRED arguments (post default-fill), so the default-filled
    group is visible here as the literal ``worker-dispatcher`` it became on
    the wire, rather than as the absence the planner emitted.

    Values compare exactly after ``strip()`` — no substring, case-insensitive,
    or prefix matching, for the reason ``remediation._unsourced_resource_args``
    gives: the campaign's own failure values are substrings of the true ones.
    """
    for entry in run_state.evidence:
        if entry.tool_name != subject.tool_name:
            continue
        raw = entry.arguments.get(subject.argument_field)
        if isinstance(raw, (str, UUID)) and str(raw).strip() == subject.value:
            return True
    return False


def _refuse_handoff(run_state: RunState, at: datetime, subject: AlertSubject) -> RunState:
    """Refuse a remediate handoff and steer the planner at the probe it skipped.

    Deliberately NOT a terminal transition. The state stays INVESTIGATING and
    the loop continues, so the planner gets to fix its own omission — the
    refusal reason is rendered into the next planner context by
    ``_format_planner_context`` like any other evidence line. This mirrors the
    plan-guard rejections in ``remediation.make_llm_plan``: reject the bad
    output, say precisely what would make it good, let the model try again.

    Recorded under an underscore-prefixed name per the repo-wide marker
    convention, so the briefing's evidence trail and the grader's
    "tools called" set both exclude it — a refusal is bookkeeping, not a
    probe, and it spends no tool-call budget.
    """
    reason = (
        f"handoff refused: this incident's alert names "
        f"{subject.alert_field}={subject.value!r}, and no probe in the evidence "
        f"trail has read it. Call {subject.tool_name} with "
        f"{subject.argument_field}={subject.value!r} before remediating. Other "
        "incidents, alerts, and DLQ entries visible in the evidence are context, "
        "not this incident's subject — remediating one of those leaves the "
        "alerted signal unexplained."
    )
    entry = EvidenceEntry(
        tool_name="_handoff_refused",
        arguments={
            "alert_field": subject.alert_field,
            "required_tool": subject.tool_name,
            "required_argument": subject.argument_field,
            "required_value": subject.value,
        },
        result_summary=reason,
        timestamp=at,
    )
    return run_state.model_copy(
        update={
            "evidence": (*run_state.evidence, entry),
            "updated_at": at,
        }
    )


def _finalize(run_state: RunState, at: datetime, reason: str) -> RunState:
    """Planner said stop. Transition to ESCALATED with the reason in evidence."""
    entry = EvidenceEntry(
        tool_name="_planner_stop",
        arguments={"reason": reason},
        result_summary=f"planner stop: {reason}",
        timestamp=at,
    )
    return run_state.model_copy(
        update={
            "state": IncidentState.ESCALATED,
            "updated_at": at,
            "evidence": (*run_state.evidence, entry),
        }
    )


def _escalate_investigation(run_state: RunState, at: datetime, reason: str) -> RunState:
    """Escalation path for LLM loop failures (budget, invalid output, tool errors)."""
    entry = EvidenceEntry(
        tool_name="_planner_escalate",
        arguments={"reason": reason},
        result_summary=reason,
        timestamp=at,
    )
    return run_state.model_copy(
        update={
            "state": IncidentState.ESCALATED,
            "updated_at": at,
            "evidence": (*run_state.evidence, entry),
        }
    )


def _handoff_to_planning(run_state: RunState, at: datetime, reason: str) -> RunState:
    """Planner said the top hypothesis is remediable. Hand off to PLANNING."""
    entry = EvidenceEntry(
        tool_name="_planner_remediate",
        arguments={"reason": reason},
        result_summary=f"planner handoff to PLANNING: {reason}",
        timestamp=at,
    )
    return run_state.model_copy(
        update={
            "state": IncidentState.PLANNING,
            "updated_at": at,
            "evidence": (*run_state.evidence, entry),
        }
    )


def _format_planner_context(run_state: RunState) -> str:
    remaining_calls = max(run_state.budget.max_tool_calls - run_state.budget.tool_calls_used, 0)
    remaining_tokens = max(run_state.budget.max_tokens - run_state.budget.tokens_used, 0)
    lines = [
        f"Alert: {json.dumps(dict(run_state.alert), sort_keys=True)}",
        f"Budget remaining: tool_calls={remaining_calls}, tokens={remaining_tokens}",
        "",
    ]
    if run_state.evidence:
        lines.append("Evidence so far:")
        for entry in run_state.evidence:
            lines.append(f"  - [{entry.tool_name}] {entry.result_summary}")
    else:
        lines.append("Evidence so far: (none)")
    lines.append("")
    # Investigation planner sees read tools only (Tier.READ). Tier-1 tools
    # are executed by the REMEDIATING transition; the planner emits a
    # RemediateAction to hand off, it does not call them directly.
    lines.append("Available tools (read-only probes):")
    for name in sorted(tools_at_or_below(Tier.READ)):
        spec = TOOL_REGISTRY[name]
        schema = spec.input_model.model_json_schema()
        lines.append(f"  - {name}: {_indented_description(name)}")
        lines.append(f"    input_schema={json.dumps(schema, sort_keys=True)}")
    return "\n".join(lines)


def _indented_description(tool_name: str) -> str:
    """Platform-authored tool description, indented for the context block.

    Verbatim from the contract snapshot (see ``registry.description_of``).
    These are load-bearing: freshness windows, delayed-replay semantics,
    and observable effects live here, and the planner can only reason
    about them if it reads them.
    """
    text = description_of(tool_name)
    if not text:
        return "(no description)"
    return text.replace("\n", "\n    ")
