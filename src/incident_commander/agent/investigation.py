"""INVESTIGATING transitions.

``make_investigate``: Phase-0 deterministic ``get_consumer_lag`` probe, then escalate.
``make_llm_investigate``: the Phase-2 hypothesis loop.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from datetime import datetime
from enum import StrEnum
from typing import Any, Final, NamedTuple
from uuid import UUID

from pydantic import BaseModel, ValidationError

from incident_commander.agent.accounting import accrue_llm_error, accrue_structured_call
from incident_commander.agent.hypothesis import (
    Hypothesis,
    HypothesisCategory,
    InvestigationStep,
    ProbeAction,
    RemediateAction,
    StopAction,
)
from incident_commander.agent.planner_context import format_planner_context
from incident_commander.agent.state import (
    EvidenceEntry,
    IncidentState,
    RunState,
)
from incident_commander.agent.strategies.protocol import InvestigationStrategy, StrategyContext
from incident_commander.agent.strategies.records import PlannerCall, StepSink
from incident_commander.llm.client import LLMClientProtocol, LLMError
from incident_commander.llm.prompts.loader import load_prompt
from incident_commander.llm.prompts.shared_rules import STUCK_CHAIN_ROOT_RULE
from incident_commander.llm.repair import (
    INVESTIGATION_PLANNER_INVALID,
    call_with_output_repair,
)
from incident_commander.tools.mcp_client import MCPClientProtocol, MCPError, ToolResult
from incident_commander.tools.policies import Tier, is_cached_read, tier_of
from incident_commander.tools.registry import TOOL_REGISTRY
from incident_commander.tools.wire import wire_arguments

_TOOL_NAME: Final[str] = "get_consumer_lag"
# Bookkeeping marker for a Phase-1 escalation, distinct from the tool that failed. The
# underscore is the repo-wide marker convention `briefing._terminal_marker` reads (WO-R2-119).
_ESCALATION_MARKER: Final[str] = "_investigate_escalate"
# Marker for ADR 0041's refusal, distinct from `_handoff_refused` (ADR 0031/0032):
# a run can collect one of each, naming different missing reads.
_WHOLE_QUEUE_REFUSED_MARKER: Final[str] = "_handoff_refused_unlisted_queue"
_DEFAULT_MAX_ITERATIONS: Final[int] = 5
_REMEDIATE_CONFIDENCE_THRESHOLD: Final[float] = 0.7

# How many remediate handoffs may be refused for never probing the alert's subject before
# the run escalates instead. A refusal steers the planner, but a third ask would not land.
_MAX_SUBJECT_PROBE_REFUSALS: Final[int] = 2

# Same, for never having read the dead-letter queue whole (ADR 0041). ONE where the sibling
# above is two: `list_dlq_messages()` has no argument to get wrong, so a re-ask adds nothing.
_MAX_WHOLE_QUEUE_REFUSALS: Final[int] = 1


# Single source of truth for category → Tier-1 tool routing: categories here auto-remediate
# above the confidence threshold, categories absent always escalate. The value names the
# common case (the remediation planner picks the specific tool) but is load-bearing — the
# planner prompt is written FROM it, and `tests/unit/test_policies.py::TestFixMapMatchesTheSuite`
# checks a category's steered tool is not one its scenarios forbid.
FIX_MAP: Final[dict[HypothesisCategory, str]] = {
    HypothesisCategory.CONSUMER_SATURATION: "restart_consumer_group",
    HypothesisCategory.POISON_MESSAGE: "replay_dlq_by_ids",
    HypothesisCategory.STALE_CACHE: "invalidate_cache_key",
    # Replay the dead-lettered root, NOT `pause_dag` — a pause never un-sticks a chain and
    # blocks the replay. Hint-routed since WO-R3-263, so the root's own row picks the tool.
    HypothesisCategory.RUNAWAY_SAGA: "replay_dlq_by_ids",
}


# Categories whose SPECIFIC tool comes from the platform's per-row `remediation_hint` rather
# than FIX_MAP's value: `human_required` rows route to `mark_dlq_permanent`, `replay_safe`
# ones to an immediate replay — same category, both correct.
#
# `RUNAWAY_SAGA` joined on 2026-09-17 (WO-R3-263, owner decision O-19, ADR 0054): a chain's
# root is a dead-letter row like any other, and `TestFixMapMatchesTheSuite` missed the
# disagreement while scoped to `resolved` scenarios. Pinned with `HINT_ROUTED_TOOLS` and
# `stuck_chain_root_rule()` by `tests/unit/test_policies.py::TestStuckChainRootRule`.
HINT_ROUTED_CATEGORIES: Final[frozenset[HypothesisCategory]] = frozenset(
    {HypothesisCategory.POISON_MESSAGE, HypothesisCategory.RUNAWAY_SAGA}
)


# The routing the set above defers to: per-row `remediation_hint` → the Tier-1 tools it
# admits. The vocabulary is the alerted DLQ SLICE, named by `AlertPayload.remediation_hint`
# or `AlertPayload.dlq_scope`; `unclassified` joined on 2026-09-08 after live run
# `a0aa257bf865` showed a null hint IS a finding (ADR 0032). Sets, not single values,
# because two hints genuinely admit two tools.
#
# NOT read at runtime — only the prompt obeys it, so this map's job is to be the single
# source the prompt is written FROM (architecture-principles rule 2) and to be checkable by
# `tests/unit/test_policies.py::TestHintRoutedToolsMatchTheSuite`, which also covers the
# escalate-but-acts scenarios `TestFixMapMatchesTheSuite` cannot see (WO-R2-140, WO-R2-160).
HINT_ROUTED_TOOLS: Final[dict[str, frozenset[str]]] = {
    "replay_safe": frozenset({"replay_dlq_by_ids", "replay_dlq_by_category"}),
    "wait_and_replay": frozenset({"replay_dlq_by_ids", "replay_dlq_by_category"}),
    # One tool; the fence is `Resolution.STABILIZES`, so success is a handoff (WO-R2-140).
    "human_required": frozenset({"mark_dlq_permanent"}),
    # Keyed on `AlertPayload.dlq_scope`, not a row hint: an unclassified row has none.
    "unclassified": frozenset({"mark_dlq_permanent"}),
}

# WHEN THE LABEL AND THE EVIDENCE DISAGREE, THE ROUTING ABOVE DOES NOT APPLY.
#
# The user's ruling (WO-R2-167, 2026-09-08): when a row's `remediation_hint` and its
# `error_message` disagree, the ERROR wins, the row is not replayed, and the disagreement is
# reported in the briefing. A separate constant rather than a fifth `HINT_ROUTED_TOOLS` entry
# because the contradiction is a property of a ROW, and a per-slice map cannot hold it.
#
# NOT READ AT RUNTIME, by decision (ADR 0034): a plan-time refusal keyed on error text would
# derive a control from tool output, which CLAUDE.md invariant 4 forbids. Enforcement is the
# prompt rule this constant sources, exact grading in `dlq_mislabeled_replay_safe`, the free
# `make world-dossier` lint, and `tests/unit/test_policies.py::TestHintRoutedToolsMatchTheSuite`.
CONTRADICTED_HINT_TOOLS: Final[frozenset[str]] = frozenset({"mark_dlq_permanent"})


def stuck_chain_root_rule() -> str:
    """The stuck-chain conditional, in the words every reader of it is given.

    Read from ``llm/prompts/shared_rules.py`` rather than spelled here, so a fourth copy
    cannot drift (ADR 0054). ``HINT_ROUTED_CATEGORIES`` and ``HINT_ROUTED_TOOLS`` are its
    structural half; ``TestStuckChainRootRule`` holds the two together.
    """
    return STUCK_CHAIN_ROOT_RULE


# The read that shows a dead-letter row, and the two arguments that narrow it. Held here
# because `remediation.py` imports THIS module; pinned against `DLQ_ROW_SOURCE` and
# `SOURCE_LISTING_FOR_ACTION` by `test_policies.py::TestWholeQueueReadBeforeDlqAction`.
DLQ_LISTING_TOOL: Final[str] = "list_dlq_messages"

# `limit` and `offset` are NOT here: paging a listing is not slicing it.
DLQ_LISTING_FILTERS: Final[frozenset[str]] = frozenset({"remediation_hint", "job_type"})


# Every Tier-1 tool that replays or fences a dead-letter row.
#
# Declared, not derived: `mark_dlq_permanent` is DELIBERATELY inert in
# `SOURCE_ROW_FOR_ACTION` and `SOURCE_LISTING_FOR_ACTION` (WO-R2-144), so deriving would
# silently drop the fence — half of what ADR 0041 is about.
# `test_policies.py::TestWholeQueueReadBeforeDlqAction` keeps this a superset of what those
# maps tie to the listing.
DLQ_ACTION_TOOLS: Final[frozenset[str]] = frozenset(
    {
        "replay_dlq_by_ids",
        "replay_dlq_by_category",
        "replay_dlq_messages",
        "mark_dlq_permanent",
    }
)


def _dlq_acting_categories() -> frozenset[HypothesisCategory]:
    """Hypothesis categories whose Tier-1 route can reach a dead-letter action.

    Derived rather than hand-listed, because a handoff knows only the top hypothesis's
    CATEGORY: the union of ``FIX_MAP[category]`` and, for a ``HINT_ROUTED_CATEGORIES``
    member, every tool in ``HINT_ROUTED_TOOLS``. Today ``POISON_MESSAGE`` and
    ``RUNAWAY_SAGA``; the guard stays inert on the rest, which have no queue to read.
    """
    hint_routed = frozenset(tool for tools in HINT_ROUTED_TOOLS.values() for tool in tools)
    acting = set()
    for category, mapped_tool in FIX_MAP.items():
        reachable = {mapped_tool}
        if category in HINT_ROUTED_CATEGORIES:
            reachable |= hint_routed
        if reachable & DLQ_ACTION_TOOLS:
            acting.add(category)
    return frozenset(acting)


DLQ_ACTING_CATEGORIES: Final[frozenset[HypothesisCategory]] = _dlq_acting_categories()


class SubjectMatch(StrEnum):
    """How a probe is judged to have read the subject.

    ``EQUALS`` (the default) counts a probe whose argument carries the subject's exact
    value. ``UNFILTERED`` is the inverse claim, for the one subject the platform cannot
    express as a filter: ``ListDlqMessagesInput.remediation_hint = null`` means "every
    category", so the whole-queue page is the only read that shows a null-hint row. The same
    null means "nobody classified this" on a returned ROW — live run ``a0aa257bf865`` is what
    reading the two as one fact costs.
    """

    EQUALS = "equals"
    UNFILTERED = "unfiltered"


class SubjectProbe(NamedTuple):
    """The read call that observes one kind of alert subject."""

    tool_name: str
    """Read tool that observes this resource or slice."""
    argument_field: str
    """The tool argument the subject's value belongs in — or, under
    ``SubjectMatch.UNFILTERED``, the argument that must NOT be narrowed."""
    match: SubjectMatch = SubjectMatch.EQUALS
    """How ``_alert_subject_probed`` judges a candidate probe."""
    admissible_values: frozenset[str] | None = None
    """The alert values this entry recognises, or ``None`` for "any non-empty
    string" — which is right wherever the value IS the resource, since the
    value is then compared against the probe's own argument and a wrong one
    simply fails to match. An ``UNFILTERED`` entry MUST declare its
    vocabulary: there the value is a word rather than a name and nothing
    downstream compares it, so an unrecognised string would otherwise mean
    "unclassified" by default."""


# Single source of truth for alert-field → subject-probe routing: the value is the resource,
# the pair is (the read tool that observes it, the argument the value belongs in).
#
# Keyed on the alert FIELD rather than on `fingerprint`, which is free text one family spells
# three ways; the fields are `api/schemas.AlertPayload` plus the corpus's
# `_NON_WEBHOOK_ALERT_FIELDS` (tests/unit/test_scenario_alert_premise.py), and the field is
# what carries the VALUE — the 2026-08-30 live run probed the default group while the alert
# named `unknown-consumer`, and only the value comparison catches that. Declaration order is
# priority order: the first entry present is "the subject". The tool/argument halves stay
# consistent with `policies.RESOURCE_ARG_FIELDS`, pinned by
# `tests/unit/test_policies.py::TestAlertSubjectProbes`.
#
# TWO ENTRIES ARE NOT RESOURCES. `remediation_hint` names a SLICE, admissible because
# `remediation.SOURCE_LISTING_FOR_ACTION`'s `ListingScope` names the same slice on both sides
# of the read/act boundary — and because leaving it out cost `dlq_wait_and_replay_success`
# (archive `06e14be3e7b1`): the agent read the queue unfiltered, reasoned correctly about all
# four rows, and escalated because its scope was four rows instead of two (ADR 0008). It is
# LAST, so a resource always outranks a slice, and the read it demands also licenses a
# same-category replay under ADR 0028. `dlq_scope="unclassified"` is the third shape — the
# ABSENCE of a filter, `SubjectMatch.UNFILTERED`, a separate field per ADR 0032.
ALERT_SUBJECT_PROBES: Final[dict[str, SubjectProbe]] = {
    "consumer_group": SubjectProbe("get_consumer_lag", "consumer_group"),
    "group": SubjectProbe("get_consumer_lag", "consumer_group"),
    "cache_key": SubjectProbe("get_cache_key_info", "key"),
    "job_id": SubjectProbe("get_dag_state", "job_id"),
    "trace_id": SubjectProbe("get_trace", "trace_id"),
    "remediation_hint": SubjectProbe("list_dlq_messages", "remediation_hint"),
    # LAST: an alert naming both is about the category. The admissible-value set is
    # required — an UNFILTERED probe never compares the value, so any string would pass.
    "dlq_scope": SubjectProbe(
        "list_dlq_messages",
        "remediation_hint",
        SubjectMatch.UNFILTERED,
        frozenset({"unclassified"}),
    ),
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
    match: SubjectMatch = SubjectMatch.EQUALS
    """How a probe qualifies as having read it — see ``SubjectMatch``."""


def alert_subject(alert: Mapping[str, Any]) -> AlertSubject | None:
    """The alert's own subject, or ``None`` when it names nothing mappable.

    ``None`` is the inert case and it is legitimate — a meta-alert or a whole-queue depth
    alert names a condition, not a probeable resource (``dlq_backlog`` and
    ``dlq_mixed_partial`` are the corpus's witnesses). Every caller must read it as "no
    opinion".

    Looks at the top level, then one level into ``extra_data``: a real webhook alert nests
    every resource field, while the corpus carries them at the top level
    (``tests/unit/test_scenario_alert_premise.py::_NON_WEBHOOK_ALERT_FIELDS``). Top level
    only would leave this guard inert in production while looking green offline.
    """
    for source in (alert, alert.get("extra_data")):
        if not isinstance(source, Mapping):
            continue
        for field, probe in ALERT_SUBJECT_PROBES.items():
            raw = source.get(field)
            # str and UUID only: JSON gives strings, Python a real UUID for
            # `job_id`/`trace_id`. Nothing else names a resource.
            if not isinstance(raw, (str, UUID)):
                continue
            value = str(raw).strip()
            if not value:
                continue
            # A declared vocabulary recognises only itself; unrecognised is the inert
            # case, not an error — the platform may name a scope before we can read it.
            if probe.admissible_values is not None and value not in probe.admissible_values:
                continue
            return AlertSubject(field, probe.tool_name, probe.argument_field, value, probe.match)
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
        # One canonical serialization for every outgoing call (wire.py). Read-only, so
        # the default-fill is deliberate; the remediation legs may NOT (ADR 0024).
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
    """End the probe leg at ESCALATED, filing the reason under the marker tool name."""
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


def _control_group() -> InvestigationStrategy:
    """``baseline``, imported at call time to keep one seam from being a cycle.

    ``strategies.baseline`` imports ``_plan_next_step`` from this module, so this module
    cannot import the strategy registry at import time.
    """
    from incident_commander.agent.strategies.registry import default_strategy

    return default_strategy()


def make_llm_investigate(
    mcp_client: MCPClientProtocol,
    llm_client: LLMClientProtocol,
    model: str,
    max_iterations: int = _DEFAULT_MAX_ITERATIONS,
    reprobe_attempts: int = 0,
    reprobe_delay_seconds: float = 0.0,
    sleep: Callable[[float], None] = time.sleep,
    strategy: InvestigationStrategy | None = None,
    record_step: StepSink | None = None,
    selector_llm_client: LLMClientProtocol | None = None,
) -> Callable[[RunState, datetime], RunState]:
    """Bind clients + model to the Phase 2 INVESTIGATING transition.

    Each iteration the LLM ranks hypotheses and either probes or stops; budget is checked
    before every LLM and tool call, and ``max_iterations`` guards the loop.

    ``reprobe_attempts`` is the investigation-side twin of ADR 0006's verify polling
    (ADR 0009): when a declared-cached probe (``policies.CACHED_READ_FRESHNESS_SECONDS``)
    kills a fixable hypothesis at or above the threshold, re-read it after
    ``reprobe_delay_seconds`` first. Default 0 keeps canned behaviour byte-identical.

    ``strategy`` makes the one planner call (plan 02 § 4, WP-0.2); ``None`` is ``baseline``,
    resolved through the registry because the eval runner wires ``INFERENCE_STRATEGY`` at the
    edge. Everything around the call — the ``FIX_MAP`` gate, the 0.7 threshold, the
    subject-probe refusal, the ADR-0009 re-probe, ``_execute_probe`` and its tier re-check —
    stays here: strategies propose, this loop decides.

    ``selector_llm_client`` is the ``candidate_selector`` role's client (WP-6.2), separate so
    accounting meters the two roles apart. ``record_step`` takes each ``StepRecord``; ``None``
    means nobody is recording (the tracer is opt-in via ``EVAL_TRACE_DIR``), but the record is
    built either way.
    """
    chosen: Final[InvestigationStrategy] = strategy if strategy is not None else _control_group()

    def transition_llm_investigate(run_state: RunState, at: datetime) -> RunState:
        last_probe: ProbeAction | None = None
        reprobes_spent: dict[str, int] = {}
        subject = alert_subject(run_state.alert)
        refusals_spent = 0
        whole_queue_refusals_spent = 0
        for iteration in range(max_iterations):
            if run_state.budget.is_exhausted:
                return _escalate_investigation(run_state, at, "budget exhausted mid-investigation")

            prior_hypotheses = run_state.hypotheses
            try:
                # Third value is the ``StepRecord``, already written to ``record_step``.
                # The loop never reads it: research data must not change the run.
                run_state, step, _ = chosen.plan_next_step(
                    run_state,
                    at,
                    StrategyContext(
                        llm_client=llm_client,
                        model=model,
                        iteration=iteration,
                        config=chosen.config,
                        record_step=record_step,
                        selector_llm_client=selector_llm_client,
                    ),
                )
            except (ValueError, ValidationError, LLMError) as err:
                # ``_plan_next_step`` accrues on the way out, so a raising call
                # accrued nothing. Charge what it billed (ADR 0015).
                run_state = run_state.model_copy(
                    update={"budget": accrue_llm_error(run_state.budget, err, model)}
                )
                return _escalate_investigation(
                    run_state, at, f"{INVESTIGATION_PLANNER_INVALID}: {err}"
                )

            killed = _cached_probe_contradiction(prior_hypotheses, step.hypotheses, last_probe)
            if (
                killed is not None
                and last_probe is not None
                and reprobes_spent.get(last_probe.tool_name, 0) < reprobe_attempts
            ):
                # The hypothesis died on a possibly-stale sensor: re-read it first.
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
                # Structural guard before handing off to PLANNING: the category must be
                # a key in FIX_MAP and confidence must clear the threshold, or escalate.
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
                # Third guard: no remediation of an incident whose alerted signal nobody
                # has read. REFUSES rather than escalates — the planner gets another turn.
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
                # Fourth guard, the mirror of the third (ADR 0041): no replaying or fencing
                # PART of a dead-letter queue nobody has read WHOLE. After the subject check,
                # so a run missing both reads hears about its own incident first. Refuses
                # rather than escalates, and this is the only place it can be made — PLANNING
                # proposes actions and never probes.
                if top.category in DLQ_ACTING_CATEGORIES and not _whole_queue_listed(run_state):
                    if whole_queue_refusals_spent >= _MAX_WHOLE_QUEUE_REFUSALS:
                        return _finalize(
                            run_state,
                            at,
                            (
                                f"planner emitted remediate "
                                f"{whole_queue_refusals_spent + 1} times for "
                                f"{top.category.value!r} without ever reading the "
                                f"dead-letter queue unfiltered; a replay or fence "
                                f"decided from a filtered page is a decision taken "
                                f"without seeing the rows it leaves behind; escalating"
                            ),
                        )
                    whole_queue_refusals_spent += 1
                    run_state = _refuse_whole_queue_handoff(run_state, at, top.category)
                    continue
                return _handoff_to_planning(run_state, at, action.reason)

            # ProbeAction — tool_name is Literal-validated at schema time; this check
            # catches a registry that drifted after startup.
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
) -> tuple[RunState, InvestigationStep, PlannerCall]:
    """One planner LLM call — plus one bounded repair if it does not parse.

    ADR 0035: a ``record_output`` payload the schema rejects is a harness event. Both legs
    accrue; a second failure raises ``OutputRepairExhausted``.

    The third return value is the call's own measurements (tokens, trace-record id, context
    size, elapsed time — WP-2.1, WO-R3-260). The loop never reads it.
    """
    system_prompt = load_prompt("investigation_planner")
    # A local, not inline: the step record measures what was sent, and a second
    # render could differ from the string the model saw.
    user_message = format_planner_context(run_state)
    call = call_with_output_repair(
        llm_client,
        system_prompt=system_prompt,
        user_message=user_message,
        output_model=InvestigationStep,
        model=model,
    )
    new_budget = accrue_structured_call(run_state.budget, call, model)
    updated = run_state.model_copy(
        update={
            "budget": new_budget,
            "hypotheses": call.result.output.hypotheses,
            "updated_at": at,
        }
    )
    result = call.result
    measured = PlannerCall(
        record_id=result.record_id,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cache_read_tokens=result.cache_read_tokens,
        cache_creation_tokens=result.cache_creation_tokens,
        context_chars=len(system_prompt) + len(user_message),
        # The client's own measurement — a stopwatch here would also time
        # the accrual below and call it model time.
        elapsed_ms=result.elapsed_ms,
    )
    return updated, result.output, measured


def _execute_probe(
    run_state: RunState,
    at: datetime,
    mcp_client: MCPClientProtocol,
    action: ProbeAction,
) -> RunState:
    """Call the tool the planner picked. On any failure, escalate with the reason."""
    spec = TOOL_REGISTRY[action.tool_name]
    # Runtime tier guard (B-06): only tier_of() catches a READ→TIER_1 reclassification made
    # after the Literal was hand-listed. Here, so both call sites are covered (ADR 0009).
    if tier_of(action.tool_name) is not Tier.READ:
        return _escalate_investigation(
            run_state,
            at,
            f"planner proposed non-read tool as probe: {action.tool_name} "
            f"(tier={tier_of(action.tool_name).value})",
        )
    try:
        # Same canonical serialization the remediation legs use — no second copy.
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

    Triggers when a declared-cached probe drops a ``FIX_MAP`` hypothesis from at/above the
    remediate threshold to below it — the re-probe exists to protect that handoff.
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

    Matching is on the tool AND the argument value, never the tool alone: on 2026-08-30 the
    agent answered a ``group="unknown-consumer"`` alert with ``get_consumer_lag`` and no
    group, which ``wire_arguments`` default-fills to ``worker-dispatcher``. Evidence records
    the WIRED arguments, so the fill is visible here. Values compare exactly after
    ``strip()`` — the campaign's own failure values are substrings of the true ones
    (``remediation._unsourced_resource_args``).

    Under ``SubjectMatch.UNFILTERED`` the comparison inverts: the qualifying probe is the one
    that did NOT narrow. Absent key, explicit ``null``, non-string and whitespace all read as
    unfiltered, the same collapse ``remediation._scope_value`` makes for ADR 0028.
    """
    for entry in run_state.evidence:
        if entry.tool_name != subject.tool_name:
            continue
        raw = entry.arguments.get(subject.argument_field)
        narrowed = str(raw).strip() if isinstance(raw, (str, UUID)) else ""
        if subject.match is SubjectMatch.UNFILTERED:
            if not narrowed:
                return True
            continue
        if narrowed == subject.value:
            return True
    return False


def _whole_queue_listed(run_state: RunState) -> bool:
    """True when this run read the dead-letter queue with no slice filter on it.

    ADR 0041's whole check, and deliberately a statement about the CALL rather than the rows:
    a probe that failed never reaches the ledger, and the rule is about what the agent looked
    at. "Unfiltered" is judged exactly as ``remediation._scope_value`` judges it — missing
    key, explicit ``null``, non-string or whitespace. Paging is not narrowing (``limit`` and
    ``offset`` are not in ``DLQ_LISTING_FILTERS``), so a page-at-a-time walk has read it whole.
    """
    for entry in run_state.evidence:
        if entry.tool_name != DLQ_LISTING_TOOL:
            continue
        if any(_narrowed_on(entry.arguments, field) for field in DLQ_LISTING_FILTERS):
            continue
        return True
    return False


def _narrowed_on(arguments: Mapping[str, Any], field: str) -> bool:
    """Whether one call narrowed on one filter argument."""
    value = arguments.get(field)
    return isinstance(value, str) and bool(value.strip())


def _refuse_whole_queue_handoff(
    run_state: RunState, at: datetime, category: HypothesisCategory
) -> RunState:
    """Refuse a dead-letter handoff and steer the planner at the whole-queue read.

    Same shape as ``_refuse_handoff``: the state stays INVESTIGATING and the reason is
    rendered into the next planner context. It names the call and what the read is FOR —
    "read the queue" alone makes a planner re-read the page it has (ADR 0031).
    Underscore-prefixed, so the briefing trail and the grader's tool set exclude it.
    """
    reason = (
        f"handoff refused: this run is about to take a dead-letter action "
        f"(top hypothesis category {category.value!r}), and nothing in the "
        f"evidence trail has read the dead-letter queue unfiltered. Call "
        f"{DLQ_LISTING_TOOL}() with no "
        f"{' and no '.join(sorted(DLQ_LISTING_FILTERS))} filter before "
        f"remediating — paging it with limit/offset is fine. A listing narrowed "
        f"to one category or one job type does not count: it cannot show the "
        f"rows that sit beside the ones you are acting on, and a row the "
        f"platform has not classified carries no category at all, so no "
        f"filtered page selects it. Read the whole queue once, then act on the "
        f"slice you were paged for and name the rest in the briefing."
    )
    entry = EvidenceEntry(
        tool_name=_WHOLE_QUEUE_REFUSED_MARKER,
        arguments={
            "category": category.value,
            "required_tool": DLQ_LISTING_TOOL,
            "required_unfiltered_arguments": sorted(DLQ_LISTING_FILTERS),
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


def _refuse_handoff(run_state: RunState, at: datetime, subject: AlertSubject) -> RunState:
    """Refuse a remediate handoff and steer the planner at the probe it skipped.

    NOT a terminal transition: the state stays INVESTIGATING and the refusal is rendered
    into the next planner context, mirroring ``remediation.make_llm_plan``'s plan guards.
    Underscore-prefixed, so the briefing trail and the grader's tool set exclude it.
    """
    if subject.match is SubjectMatch.UNFILTERED:
        instruction = (
            f"Call {subject.tool_name} with NO {subject.argument_field} filter "
            f"before remediating — the whole-queue page is the only read that "
            f"shows rows the platform has not classified, because "
            f"{subject.argument_field}=null on that call means 'every category' "
            f"rather than 'the uncategorised ones'. A call to "
            f"{subject.tool_name} that narrowed to some category does not "
            "count: the rows this incident is about are the ones no category "
            "contains."
        )
    else:
        instruction = (
            f"Call {subject.tool_name} with "
            f"{subject.argument_field}={subject.value!r} before remediating. A call "
            f"to {subject.tool_name} that did not carry that exact value does not "
            "count — an unfiltered or default-filled read observes something else."
        )
    reason = (
        f"handoff refused: this incident's alert names "
        f"{subject.alert_field}={subject.value!r}, and no probe in the evidence "
        f"trail has read it. {instruction} "
        "Other incidents, alerts, and DLQ entries visible in the evidence are "
        "context, not this incident's subject — remediating one of those leaves "
        "the alerted signal unexplained."
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
