"""INVESTIGATING transitions.

``make_investigate``: Phase-0 deterministic ``get_consumer_lag`` probe, then escalate.
``make_llm_investigate``: the Phase-2 hypothesis loop.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
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
from incident_commander.agent.incidents import incident_slots
from incident_commander.agent.planner_context import format_planner_context
from incident_commander.agent.state import (
    EvidenceEntry,
    IncidentState,
    RunState,
)
from incident_commander.agent.strategies.names import StrategyName
from incident_commander.agent.strategies.protocol import (
    BranchProbeOutcome,
    BranchProber,
    InvestigationStrategy,
    StrategyContext,
)
from incident_commander.agent.strategies.records import PlannerCall, StepRecord, StepSink
from incident_commander.agent.thinking import (
    PLANNER_TOOL,
    REFLECTION_TOOL,
    PlannerLog,
    ThinkingAction,
)
from incident_commander.llm.client import LLMClientProtocol, LLMError
from incident_commander.llm.prompts.loader import load_prompt
from incident_commander.llm.prompts.shared_rules import STUCK_CHAIN_ROOT_RULE
from incident_commander.llm.repair import (
    INVESTIGATION_PLANNER_INVALID,
    OutputNotOffered,
    call_with_output_repair,
    sum_usage,
    usage_of,
)
from incident_commander.tools.mcp_client import MCPClientProtocol, MCPError, ToolResult
from incident_commander.tools.policies import (
    CACHED_READ_FRESHNESS_SECONDS,
    Tier,
    is_cached_read,
    tier_of,
)
from incident_commander.tools.registry import TOOL_REGISTRY
from incident_commander.tools.wire import wire_arguments

_TOOL_NAME: Final[str] = "get_consumer_lag"
# Bookkeeping marker for a Phase-1 escalation, distinct from the tool that failed. The
# underscore is the repo-wide marker convention `briefing._terminal_marker` reads (WO-R2-119).
_ESCALATION_MARKER: Final[str] = "_investigate_escalate"
# ADR 0041's refusal, distinct from `_handoff_refused` (ADR 0031/0032): a run can collect one
# of each, naming different missing reads.
_WHOLE_QUEUE_REFUSED_MARKER: Final[str] = "_handoff_refused_unlisted_queue"
# ADR 0073's refusal, and the only one of the three that refuses a READ rather than a handoff.
_CONFIRMING_READ_REFUSED_MARKER: Final[str] = "_probe_refused_confirming_read"
_DEFAULT_MAX_ITERATIONS: Final[int] = 5
# The bar a hypothesis must clear before the loop acts on it. Public because three other
# readers need the SAME number (`remediation.py`'s resolve gate, ADR 0059; the slots, ADR 0065).
REMEDIATE_CONFIDENCE_THRESHOLD: Final[float] = 0.7

# Remediate handoffs refusable for never probing the alert's subject before the run escalates
# instead. A refusal steers the planner, but a third ask would not land.
_MAX_SUBJECT_PROBE_REFUSALS: Final[int] = 2

# Same, for never having read the dead-letter queue whole (ADR 0041). ONE, because
# `list_dlq_messages()` has no argument to get wrong, so a re-ask adds nothing.
_MAX_WHOLE_QUEUE_REFUSALS: Final[int] = 1

# Readings of the alert's subject allowed before a further one is refused (ADR 0073): the
# investigation, then the confirming re-read. INC-004 is what an unbounded "re-read" cost.
_CONFIRMING_READS_ALLOWED: Final[int] = 2

# Consecutive steps that must rank the same actionable answer first before the guard arms. TWO:
# a ranking that did not move, where a run whose top is still changing may read again.
_SETTLED_RANKING_STEPS: Final[int] = 2

# Further reads refusable before the run escalates instead. Two, like the subject-probe sibling:
# the first names the moves that remain, the second allows for a mis-read, a third is not landing.
_MAX_CONFIRMING_READ_REFUSALS: Final[int] = 2


# Single source of truth for category → Tier-1 tool routing: categories here auto-remediate above
# the confidence threshold, absent ones always escalate. Load-bearing — the planner prompt is
# written FROM it, and `TestFixMapMatchesTheSuite` checks each steered tool against the suite.
FIX_MAP: Final[dict[HypothesisCategory, str]] = {
    HypothesisCategory.CONSUMER_SATURATION: "restart_consumer_group",
    HypothesisCategory.POISON_MESSAGE: "replay_dlq_by_ids",
    HypothesisCategory.STALE_CACHE: "invalidate_cache_key",
    # Replay the dead-lettered root, NOT `pause_dag` — a pause never un-sticks a chain and
    # blocks the replay. Hint-routed since WO-R3-263, so the root's own row picks the tool.
    HypothesisCategory.RUNAWAY_SAGA: "replay_dlq_by_ids",
}


# Categories whose SPECIFIC tool comes from the platform's per-row `remediation_hint` rather than
# FIX_MAP's value: `human_required` rows route to `mark_dlq_permanent`, `replay_safe` ones to a
# replay. `RUNAWAY_SAGA` joined under ADR 0054, pinned by `TestStuckChainRootRule`.
HINT_ROUTED_CATEGORIES: Final[frozenset[HypothesisCategory]] = frozenset(
    {HypothesisCategory.POISON_MESSAGE, HypothesisCategory.RUNAWAY_SAGA}
)


# The routing the set above defers to: per-row `remediation_hint` → the Tier-1 tools it admits;
# `unclassified` is keyed on the alerted SLICE, because a null hint IS a finding (ADR 0032).
# NOT read at runtime — the prompt is written FROM it (`TestHintRoutedToolsMatchTheSuite`).
HINT_ROUTED_TOOLS: Final[dict[str, frozenset[str]]] = {
    "replay_safe": frozenset({"replay_dlq_by_ids", "replay_dlq_by_category"}),
    "wait_and_replay": frozenset({"replay_dlq_by_ids", "replay_dlq_by_category"}),
    # One tool; the fence is `Resolution.STABILIZES`, so success is a handoff (WO-R2-140).
    "human_required": frozenset({"mark_dlq_permanent"}),
    # Keyed on `AlertPayload.dlq_scope`, not a row hint: an unclassified row has none.
    "unclassified": frozenset({"mark_dlq_permanent"}),
}

# WHEN THE LABEL AND THE EVIDENCE DISAGREE, THE ROUTING ABOVE DOES NOT APPLY: the ERROR wins, the
# row is not replayed, and the briefing reports it (WO-R2-167). NOT read at runtime (ADR 0034) — a
# refusal keyed on error text would derive a control from tool output, which invariant 4 forbids.
CONTRADICTED_HINT_TOOLS: Final[frozenset[str]] = frozenset({"mark_dlq_permanent"})


def stuck_chain_root_rule() -> str:
    """The stuck-chain conditional, in the words every reader of it is given.

    Read from ``llm/prompts/shared_rules.py`` rather than spelled here, so a fourth copy cannot
    drift (ADR 0054); ``TestStuckChainRootRule`` holds it to its structural half.
    """
    return STUCK_CHAIN_ROOT_RULE


# The read that shows a dead-letter row, and the two arguments that narrow it. Here because
# `remediation.py` imports this module; pinned by `TestWholeQueueReadBeforeDlqAction`.
DLQ_LISTING_TOOL: Final[str] = "list_dlq_messages"

# `limit` and `offset` are NOT here: paging a listing is not slicing it.
DLQ_LISTING_FILTERS: Final[frozenset[str]] = frozenset({"remediation_hint", "job_type"})


# Every Tier-1 tool that replays or fences a dead-letter row. Declared, not derived:
# `mark_dlq_permanent` is deliberately inert in the source maps, so deriving would silently drop
# the fence — half of what ADR 0041 is about.
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

    Derived rather than hand-listed, because a handoff knows only the top hypothesis's CATEGORY.
    The guard stays inert on categories with no queue to read.
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

    ``EQUALS`` wants the subject's exact value in the argument. ``UNFILTERED`` is the inverse, for
    a subject no filter can express: ``remediation_hint = null`` means "every category".
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


# Single source of truth for alert-field → subject-probe routing. Keyed on the alert FIELD because
# the field carries the VALUE; declaration order is priority order, and the last two entries are
# SLICES (ADR 0028, ADR 0032) so they rank below every resource. Pinned by `TestAlertSubjectProbes`.
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

    ``None`` is legitimate and every caller reads it as "no opinion". Looks at the top level and
    one level into ``extra_data``, because a real webhook nests every resource field.
    """
    for source in (alert, alert.get("extra_data")):
        if not isinstance(source, Mapping):
            continue
        for field, probe in ALERT_SUBJECT_PROBES.items():
            raw = source.get(field)
            # str and UUID only: JSON gives strings, Python a real UUID for `job_id`/`trace_id`.
            if not isinstance(raw, (str, UUID)):
                continue
            value = str(raw).strip()
            if not value:
                continue
            # Unrecognised is the inert case, not an error: the platform may name a scope
            # before we can read it.
            if probe.admissible_values is not None and value not in probe.admissible_values:
                continue
            return AlertSubject(field, probe.tool_name, probe.argument_field, value, probe.match)
    return None


class FaultPresentReading(NamedTuple):
    """How one read tool says "the fault this resource was paged for is present NOW".

    Deliberately NOT ``attribution.RecoveredReading`` (ADR 0073): the opposite direction, where a
    stale zero is no trap, and that module imports this one so a shared map needs a third home.
    """

    tool_name: str
    """Read tool that observes the resource."""
    argument_field: str
    """The argument carrying the resource name — the same field ``SubjectProbe`` names,
    checked equal to it before the entry is used, so this map cannot describe a reading of
    something else."""
    field: str
    """The field in the reading that answers it."""
    recovered_value: object
    """The value of that field which means the fault is GONE. Expressed this way round
    because "present" is everything else, and enumerating everything else is how a
    predicate stops being total."""
    known_field: str | None
    """A boolean field that must read ``True`` before the field above means anything, or
    ``None`` where the reading is always a measurement. ``lag: null, lag_known: false`` is
    the platform saying it has no measurement, which is not a fault and not a recovery."""
    why: str
    """What makes this reading a fault-present one, quoted into the refusal so a human is
    never asked to take the predicate on trust."""


#: "This reading shows the fault still present". TOTAL over every read tool
#: ``ALERT_SUBJECT_PROBES`` names; ``None`` is a DECLARED inert entry, never an omission, pinned
#: by ``TestTheFaultPresentReadingMap``.
FAULT_PRESENT_READING: Final[dict[str, FaultPresentReading | None]] = {
    # The backlog is still there. `lag_known` guards the measurement's existence and `age_seconds`
    # makes it current: "present" has to be about NOW to be a reason not to look again.
    "get_consumer_lag": FaultPresentReading(
        "get_consumer_lag",
        "consumer_group",
        "lag",
        0,
        "lag_known",
        "the platform reports the group's own backlog with the age of the measurement, so a "
        "non-zero lag inside its freshness window is the alerted condition happening now",
    ),
    # INERT: a key's presence is not a fault. Staleness is a judgement over three fields and a
    # second reading, and `exists: true` would arm this guard on every healthy key.
    "get_cache_key_info": None,
    # INERT: a chain's fault is WHICH node is dead-lettered (ADR 0070), and the routing read is
    # the root's own dead-letter row (ADR 0054), so a bound here would bind the wrong read.
    "get_dag_state": None,
    # INERT: a trace records work that already finished; two reads return the same record.
    "get_trace": None,
    # INERT (INC-001, INC-002): an absence proves only that page, a fenced row is still listed
    # (ADR 0033), and ADR 0041 REQUIRES this read — a bound here would collide with it.
    "list_dlq_messages": None,
}


#: The field a platform reading carries its own measurement age in (v0.6.7, WO-R3-254). Named
#: because the freshness test below and the refusal that quotes the number must spell it once.
READING_AGE_FIELD: Final[str] = "age_seconds"


def _same_value(value: object, expected: object) -> bool:
    """Value equality that does not let ``0`` satisfy ``False`` (S-20's lesson). Spelled again
    rather than imported from ``attribution``, because that module imports this one."""
    if isinstance(expected, bool):
        return value is expected
    return value == expected


def _parsed_reading(entry: EvidenceEntry) -> Mapping[str, Any] | None:
    """One evidence entry's summary as the mapping the platform returned, or ``None``."""
    try:
        parsed = json.loads(entry.result_summary)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, Mapping) else None


def reads_fault_present(entry: EvidenceEntry, reading: FaultPresentReading) -> bool | None:
    """Whether one reading shows the fault present. ``None`` when it cannot say.

    ``None`` is load-bearing in the safe direction: an unparseable summary, a missing field or a
    ``known_field`` that is not ``True`` all leave the guard inert rather than refusing a read.
    """
    parsed = _parsed_reading(entry)
    if parsed is None or reading.field not in parsed:
        return None
    if reading.known_field is not None and parsed.get(reading.known_field) is not True:
        return None
    return not _same_value(parsed[reading.field], reading.recovered_value)


def reading_is_fresh(entry: EvidenceEntry, tool_name: str) -> bool:
    """Whether one reading is current enough to be acted on (ADR 0009).

    A tool with no declared staleness window is measured at call time. A cached read must carry an
    age inside the window; an unreported age answers ``False``, the inert direction.
    """
    window = CACHED_READ_FRESHNESS_SECONDS.get(tool_name)
    if window is None:
        return True
    parsed = _parsed_reading(entry)
    if parsed is None:
        return False
    age = parsed.get(READING_AGE_FIELD)
    if isinstance(age, bool) or not isinstance(age, (int, float)):
        return False
    return 0 <= age <= window


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
    """``baseline``, imported at call time: ``strategies.baseline`` imports ``_plan_next_step``
    from this module, so a module-level import of the registry would be a cycle."""
    from incident_commander.agent.strategies.registry import default_strategy

    return default_strategy()


def _with_incident_slots(sink: StepSink | None, run_state: RunState) -> StepSink | None:
    """Stamp each ``StepRecord`` with the causes that step named, and the remainder (WP-11.3).

    Here rather than in the five strategies, so none of them learns the bar (ADR 0036). The
    ranking is the record's own; the attempts are the ledger as it stood when the step was planned.
    """
    if sink is None:
        return None

    def stamped(record: StepRecord) -> None:
        sink(
            replace(
                record,
                incidents=incident_slots(
                    hypotheses=record.hypothesis_state_after,
                    evidence=run_state.evidence,
                    bar=REMEDIATE_CONFIDENCE_THRESHOLD,
                ),
            )
        )

    return stamped


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
    critic_llm_client: LLMClientProtocol | None = None,
    branch_prober: BranchProber | None = None,
    planner_log: PlannerLog | None = None,
) -> Callable[[RunState, datetime], RunState]:
    """Bind clients + model to the Phase 2 INVESTIGATING transition.

    Each iteration the LLM ranks hypotheses and either probes or stops. ``strategy`` makes the one
    planner call; every gate around it stays here — strategies propose, the loop decides (ADR 0036).
    """
    chosen: Final[InvestigationStrategy] = strategy if strategy is not None else _control_group()
    # Which LLM role a console row is filed under. ``reflection``'s step is the REVISED one, so
    # naming it for the arm names the call that produced the ranking.
    thinking_tool: Final[str] = (
        REFLECTION_TOOL if chosen.name == StrategyName.REFLECTION.value else PLANNER_TOOL
    )

    def transition_llm_investigate(run_state: RunState, at: datetime) -> RunState:
        last_probe: ProbeAction | None = None
        reprobes_spent: dict[str, int] = {}
        subject = alert_subject(run_state.alert)
        refusals_spent = 0
        whole_queue_refusals_spent = 0
        confirming_refusals_spent = 0
        # Consecutive steps that ranked the same actionable answer first (ADR 0073). A streak,
        # never a total: a run whose ranking moved has earned another reading.
        settled_steps = 0
        for iteration in range(max_iterations):
            if run_state.budget.is_exhausted:
                return _escalate_investigation(run_state, at, "budget exhausted mid-investigation")

            # ADR 0074, and BEFORE the call, so this call's context carries why its choice shrank:
            # a settled ranking over a fresh fault-present reading makes another read worthless.
            withdrawn = (
                _probe_withdrawn(run_state, subject, settled_steps) if subject is not None else None
            )
            if withdrawn is not None and subject is not None:
                run_state = _refuse_confirming_read(
                    run_state, at, subject, *withdrawn, settled_steps
                )

            prior_hypotheses = run_state.hypotheses
            try:
                # Third value is the ``StepRecord``, already written to ``record_step``; the loop
                # never reads it, because research data must not change the run.
                run_state, step, _ = chosen.plan_next_step(
                    run_state,
                    at,
                    StrategyContext(
                        llm_client=llm_client,
                        model=model,
                        iteration=iteration,
                        config=chosen.config,
                        record_step=_with_incident_slots(record_step, run_state),
                        selector_llm_client=selector_llm_client,
                        critic_llm_client=critic_llm_client,
                        branch_prober=branch_prober,
                        # The narrowing reaches every arm through the context; no arm decides it.
                        offer_probe=withdrawn is None,
                    ),
                )
            except (ValueError, ValidationError, LLMError) as err:
                # ``_plan_next_step`` accrues on the way out, so a raising call accrued
                # nothing. Charge what it billed (ADR 0015).
                run_state = run_state.model_copy(
                    update={"budget": accrue_llm_error(run_state.budget, err, model)}
                )
                # The planner asked for the probe the narrowed schema withdrew (ADR 0074): not a
                # reason to escalate, so it keeps its turn under ADR 0073's own cap.
                if (
                    isinstance(err, OutputNotOffered)
                    and withdrawn is not None
                    and subject is not None
                ):
                    if confirming_refusals_spent >= _MAX_CONFIRMING_READ_REFUSALS:
                        return _finalize(
                            run_state,
                            at,
                            _refusals_exhausted_reason(
                                run_state, subject, confirming_refusals_spent
                            ),
                        )
                    confirming_refusals_spent += 1
                    continue
                return _escalate_investigation(
                    run_state, at, f"{INVESTIGATION_PLANNER_INVALID}: {err}"
                )

            # ADR 0075, and the ONE place a ranking is accepted. Written before the loop's guards
            # look at the step, because a refused proposal is still thinking worth watching.
            if planner_log is not None:
                planner_log.ranking(
                    tool=thinking_tool,
                    hypotheses=tuple(step.hypotheses),
                    next_action=_thinking_action(step.next_action),
                    reason=_action_reason(step.next_action),
                )

            settled_steps = settled_steps + 1 if _ranks_an_actionable_answer(step.hypotheses) else 0

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
                if top.confidence < REMEDIATE_CONFIDENCE_THRESHOLD:
                    return _finalize(
                        run_state,
                        at,
                        (
                            f"planner emitted remediate but top confidence "
                            f"{top.confidence:.2f} is below threshold "
                            f"{REMEDIATE_CONFIDENCE_THRESHOLD}; escalating"
                        ),
                    )
                # Third guard: no remediation of an incident whose alerted signal nobody has
                # read. REFUSES rather than escalates, so the planner gets another turn.
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
                # Fourth guard, the mirror of the third (ADR 0041): no replaying or fencing PART
                # of a queue nobody read WHOLE. Here, not in PLANNING, which never probes.
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

            # Fifth guard, and the only one that refuses a READ (ADR 0073, INC-004): the bound is
            # here, where the reads are counted, because a model can satisfy "re-read before you
            # conclude" forever. Either arm fires; see each one's own docstring.
            confirming = withdrawn or (
                _confirming_read_exhausted(run_state, subject, action, settled_steps)
                if subject is not None
                else None
            )
            if confirming is not None and subject is not None:
                if confirming_refusals_spent >= _MAX_CONFIRMING_READ_REFUSALS:
                    return _finalize(
                        run_state,
                        at,
                        _refusals_exhausted_reason(run_state, subject, confirming_refusals_spent),
                    )
                confirming_refusals_spent += 1
                if withdrawn is None:
                    # ADR 0073's path writes its refusal now, because only the proposal revealed
                    # it; the narrowed path already wrote one before the call.
                    run_state = _refuse_confirming_read(
                        run_state, at, subject, *confirming, settled_steps
                    )
                continue

            if run_state.budget.is_exhausted:
                return _escalate_investigation(run_state, at, "budget exhausted before probe")

            run_state = _execute_probe(run_state, at, mcp_client, action)
            if run_state.state is IncidentState.ESCALATED:
                # Probe failed; already escalated with the reason.
                return run_state
            last_probe = action

        return _escalate_investigation(
            run_state, at, _iterations_exhausted_reason(run_state, max_iterations)
        )

    return transition_llm_investigate


def _thinking_action(action: ProbeAction | RemediateAction | StopAction) -> ThinkingAction:
    """The planner's move, in the two fields a console thinking row draws (ADR 0075). ``kind`` is
    the schema's own discriminator, so a move this module never saw still renders."""
    tool = action.tool_name if isinstance(action, ProbeAction) else None
    return ThinkingAction(kind=action.kind, tool=tool)


def _action_reason(action: ProbeAction | RemediateAction | StopAction) -> str | None:
    """Why the planner chose that move. ``None`` for a probe, which carries no reason field."""
    reason = getattr(action, "reason", None)
    return reason if isinstance(reason, str) else None


def _plan_next_step(
    run_state: RunState,
    at: datetime,
    llm_client: LLMClientProtocol,
    model: str,
    output_model: type[InvestigationStep] = InvestigationStep,
) -> tuple[RunState, InvestigationStep, PlannerCall]:
    """One planner LLM call — plus one bounded repair if it does not parse (ADR 0035).

    ``output_model`` is the step schema for THIS call; where the loop withdrew the probe, a
    ``probe`` under it raises ``OutputNotOffered`` with no re-ask (ADR 0074).
    """
    system_prompt = load_prompt("investigation_planner")
    # A local, not inline: the step record measures the string that was SENT.
    user_message = format_planner_context(run_state)
    call = call_with_output_repair(
        llm_client,
        system_prompt=system_prompt,
        user_message=user_message,
        output_model=output_model,
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
        # The client's own measurement: a stopwatch here would time the accrual as model time.
        elapsed_ms=result.elapsed_ms,
        # Every leg this call billed. A strategy making a SECOND call in the same step needs it,
        # or the first call's bill goes uncharged when the second fails (ADR 0045).
        billed_usage=sum_usage(*(usage_of(err) for err in call.failures), result),
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
    # Runtime tier guard (B-06): only tier_of() catches a READ→TIER_1 reclassification made after
    # the Literal was hand-listed. Here, so both call sites are covered (ADR 0009).
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


#: Why a branch's read did not happen, as ``BranchProbeOutcome.refused`` carries it. Named so a
#: test asserts the guard's own marker rather than that a string contains a word (F-007).
BRANCH_PROBE_NOT_A_READ: Final[str] = "not a read tool"
BRANCH_PROBE_UNKNOWN_TOOL: Final[str] = "not in the tool registry"
BRANCH_PROBE_BAD_ARGUMENTS: Final[str] = "arguments invalid"
BRANCH_PROBE_TOOL_ERROR: Final[str] = "tool error"
BRANCH_PROBE_UNPARSED: Final[str] = "output parse failed"


def make_branch_prober(mcp_client: MCPClientProtocol) -> BranchProber:
    """The read-only prober a search branch gathers evidence through (WP-12.1, ADR 0060).

    Built HERE, so the tier re-check, the wire, the client and the accrual stay in the loop: a
    strategy is handed this, never a client. Refuses instead of escalating.
    """

    def probe(run_state: RunState, action: ProbeAction) -> BranchProbeOutcome:
        spec = TOOL_REGISTRY.get(action.tool_name)
        if spec is None:
            return BranchProbeOutcome(run_state=run_state, refused=BRANCH_PROBE_UNKNOWN_TOOL)
        # The same runtime guard `_execute_probe` makes (B-06), and the reason a branch can never
        # act: "no world-changing action inside a branch" is not a promise about what is proposed.
        if tier_of(action.tool_name) is not Tier.READ:
            return BranchProbeOutcome(
                run_state=run_state,
                refused=(
                    f"{BRANCH_PROBE_NOT_A_READ}: {action.tool_name} is "
                    f"{tier_of(action.tool_name).value}. A branch gathers evidence; it never "
                    "changes the world, and a recorded world has no state to change and no "
                    "audit log to grade the change from (invariant 6)"
                ),
            )
        try:
            arguments = wire_arguments(spec, action.arguments)
        except ValidationError as err:
            return BranchProbeOutcome(
                run_state=run_state, refused=f"{BRANCH_PROBE_BAD_ARGUMENTS} ({err})"
            )
        try:
            result = mcp_client.call_tool(action.tool_name, arguments)
        except MCPError as err:
            return BranchProbeOutcome(
                run_state=run_state, refused=f"{BRANCH_PROBE_TOOL_ERROR}: {err}"
            )
        if result.is_error:
            return BranchProbeOutcome(
                run_state=run_state, refused=f"{BRANCH_PROBE_TOOL_ERROR}: is_error=True"
            )
        try:
            summary = _summarize_probe(spec.output_model, result)
        except (ValueError, ValidationError) as err:
            return BranchProbeOutcome(
                run_state=run_state, refused=f"{BRANCH_PROBE_UNPARSED}: {err}"
            )
        entry = EvidenceEntry(
            tool_name=action.tool_name,
            arguments=arguments,
            result_summary=summary,
            timestamp=run_state.updated_at,
        )
        # The read is charged to the run's OWN ledger, the one the chosen path spends from:
        # that shared ceiling is what makes exploring a trade-off (plan 02 § 8).
        return BranchProbeOutcome(
            run_state=run_state.model_copy(
                update={
                    "evidence": (*run_state.evidence, entry),
                    "budget": run_state.budget.model_copy(
                        update={"tool_calls_used": run_state.budget.tool_calls_used + 1}
                    ),
                }
            )
        )

    return probe


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
    if prior_top.confidence < REMEDIATE_CONFIDENCE_THRESHOLD:
        return None
    surviving = max(
        (h.confidence for h in updated if h.category == prior_top.category),
        default=0.0,
    )
    if surviving >= REMEDIATE_CONFIDENCE_THRESHOLD:
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
        f"{REMEDIATE_CONFIDENCE_THRESHOLD}); re-probing after {delay_seconds:g}s "
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
    """True when some probe in the evidence trail actually read the alert's subject."""
    return bool(subject_reads(run_state, subject))


def subject_reads(run_state: RunState, subject: AlertSubject) -> tuple[EvidenceEntry, ...]:
    """Every entry in the trail that read the alert's subject, in ledger order.

    On the tool AND the WIRED argument value, never the tool alone, because an omitted
    ``consumer_group`` is default-filled. A SET: ADR 0032 asks if it is empty, ADR 0073 counts it.
    """
    return tuple(
        entry
        for entry in run_state.evidence
        if entry.tool_name == subject.tool_name and _names_the_subject(entry.arguments, subject)
    )


def _names_the_subject(arguments: Mapping[str, Any], subject: AlertSubject) -> bool:
    """Whether one call's WIRED arguments read the subject. See ``subject_reads``."""
    raw = arguments.get(subject.argument_field)
    narrowed = str(raw).strip() if isinstance(raw, (str, UUID)) else ""
    if subject.match is SubjectMatch.UNFILTERED:
        return not narrowed
    return narrowed == subject.value


def _probe_would_read_the_subject(action: ProbeAction, subject: AlertSubject) -> bool:
    """Whether the probe the planner just proposed would read the alert's subject again.

    Judged on the WIRED arguments, so omitting ``consumer_group`` is not a way past the bound.
    Invalid arguments answer ``False``: such a call never reaches the platform.
    """
    if action.tool_name != subject.tool_name:
        return False
    spec = TOOL_REGISTRY.get(action.tool_name)
    if spec is None:
        return False
    try:
        arguments = wire_arguments(spec, action.arguments)
    except ValidationError:
        return False
    return _names_the_subject(arguments, subject)


def _ranks_an_actionable_answer(hypotheses: Sequence[Hypothesis]) -> bool:
    """Whether this step's top hypothesis is one the loop would act on.

    The remediate gate's own two conditions, in its own comparison, so the streak counts exactly
    the steps on which a ``remediate`` would have been let through.
    """
    if not hypotheses:
        return False
    top = hypotheses[0]
    return top.category in FIX_MAP and top.confidence >= REMEDIATE_CONFIDENCE_THRESHOLD


def _probe_withdrawn(
    run_state: RunState,
    subject: AlertSubject,
    settled_steps: int,
) -> tuple[EvidenceEntry, FaultPresentReading] | None:
    """The reading that makes ANY further read pointless this step, or ``None`` (ADR 0074).

    The ranking held one actionable answer for ``_SETTLED_RANKING_STEPS`` and the subject's NEWEST
    declared reading shows the fault and is fresh — ADR 0009 and ADR 0071 satisfied at once.
    """
    if settled_steps < _SETTLED_RANKING_STEPS:
        return None
    reading = FAULT_PRESENT_READING.get(subject.tool_name)
    # Keyed by tool, so the entry must also describe a reading of THIS subject's resource.
    if reading is None or reading.argument_field != subject.argument_field:
        return None
    reads = subject_reads(run_state, subject)
    if not reads:
        return None
    newest = reads[-1]
    if reads_fault_present(newest, reading) is not True:
        return None
    if not reading_is_fresh(newest, subject.tool_name):
        return None
    return newest, reading


def _confirming_read_exhausted(
    run_state: RunState,
    subject: AlertSubject,
    action: ProbeAction,
    settled_steps: int,
) -> tuple[EvidenceEntry, FaultPresentReading] | None:
    """The reading that makes a further read of the subject pointless, or ``None`` (ADR 0073).

    ``_probe_withdrawn``'s clauses plus two only a proposal can answer: it would read the alert's
    OWN subject again, and ``_CONFIRMING_READS_ALLOWED`` readings are already in hand.
    """
    withdrawn = _probe_withdrawn(run_state, subject, settled_steps)
    if withdrawn is None:
        return None
    if not _probe_would_read_the_subject(action, subject):
        return None
    if len(subject_reads(run_state, subject)) < _CONFIRMING_READS_ALLOWED:
        return None
    return withdrawn


def _refuse_confirming_read(
    run_state: RunState,
    at: datetime,
    subject: AlertSubject,
    newest: EvidenceEntry,
    reading: FaultPresentReading,
    settled_steps: int,
) -> RunState:
    """Refuse a further reading and narrow the planner's choice to the two moves left.

    NOT terminal: the state stays INVESTIGATING, under an underscore marker. It names the remaining
    moves, because a refusal that only said "not that read" cost five steps in INC-004.
    """
    taken = len(subject_reads(run_state, subject))
    age = (_parsed_reading(newest) or {}).get(READING_AGE_FIELD)
    measured = f" measured {age:g}s ago" if isinstance(age, (int, float)) else ""
    top = run_state.hypotheses[0]
    reason = (
        f"probe refused: this run has already read {subject.alert_field}="
        f"{subject.value!r} {taken} times, and your top hypothesis has been "
        f"{top.category.value!r} at or above the {REMEDIATE_CONFIDENCE_THRESHOLD} "
        f"remediate threshold — a category with a Tier-1 fix — for {settled_steps} steps in "
        f"a row. The newest of those readings, "
        f"{reading.tool_name}({reading.argument_field}={subject.value!r}) -> "
        f"{newest.result_summary}, shows the fault present{measured}: "
        f"{reading.why}. One fresh reading that shows the fault is the whole demand of "
        f"'re-read before you conclude' and of 'read the resource again before you act' — a "
        f"second is not more evidence, it is the same evidence and one step you cannot get "
        f"back. Decide on what you have: emit `remediate` to act on "
        f"{top.category.value!r}, or `stop` to hand off with the readings you took, naming in "
        f"the reason what you would need to SEE to act. No probe is offered this step — not "
        f"of this resource and not of any other tool: a reading that would not change a "
        f"ranking this settled is a step spent to arrive where you already are."
    )
    entry = EvidenceEntry(
        tool_name=_CONFIRMING_READ_REFUSED_MARKER,
        arguments={
            "alert_field": subject.alert_field,
            "refused_tool": subject.tool_name,
            "refused_value": subject.value,
            "reads_already_taken": taken,
            "settled_steps": settled_steps,
            "remaining_decisions": ["remediate", "stop"],
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


def _refusals_exhausted_reason(
    run_state: RunState, subject: AlertSubject, refusals_spent: int
) -> str:
    """Why a run that was offered ``remediate`` or ``stop`` twice is handed off instead.

    One text for both of ADR 0074's paths, opening with the words the corpus matches on
    (``N times after being refused``): a reason that names no cause is one a writer fills in.
    """
    return (
        f"planner asked to re-read {subject.alert_field}={subject.value!r} "
        f"{refusals_spent + 1} times after being refused: {_ranking_sentence(run_state)}, and "
        f"{_reads_taken_sentence(run_state.evidence)}. A further reading of a resource this "
        f"run has already read is not more evidence, and no step will be spent on one; "
        f"escalating with the readings it has"
    )


def _ranking_sentence(run_state: RunState) -> str:
    """What this run ranked first, and whether it was something the loop could have acted on.

    One sentence for the two escalations that must not leave a reader guessing (ADR 0073): INC-004's
    reason said only "max iterations (5) exceeded" and the briefing recommended something else.
    """
    if not run_state.hypotheses:
        return "this run produced no ranking, so it names no cause"
    top = run_state.hypotheses[0]
    standing = (
        (
            f"at or above the {REMEDIATE_CONFIDENCE_THRESHOLD} remediate threshold in a "
            f"category with a Tier-1 fix, so this run ended holding an answer it could "
            f"have acted on"
        )
        if _ranks_an_actionable_answer(run_state.hypotheses)
        else "not an answer this run could have acted on autonomously"
    )
    return (
        f"the top hypothesis was {top.category.value!r} / {top.name!r} at confidence "
        f"{top.confidence:.2f}, which is {standing}"
    )


def _reads_taken_sentence(evidence: Sequence[EvidenceEntry]) -> str:
    """The reads this run spent its steps on, counted per tool. Underscore markers are excluded
    as everywhere else, so this counts calls the platform actually answered."""
    counted = Counter(entry.tool_name for entry in evidence if not entry.tool_name.startswith("_"))
    if not counted:
        return "no read was taken"
    listed = ", ".join(
        f"{tool} x{count}" if count > 1 else tool for tool, count in sorted(counted.items())
    )
    return f"the reads it took were {listed}"


def _iterations_exhausted_reason(run_state: RunState, max_iterations: int) -> str:
    """Why the loop ran out of steps, with the ranking it ran out holding (ADR 0073).

    Opens with the words archives, reports and one test match on; what follows is what INC-004
    showed was missing — the ranking is this run's conclusion, and a cause outside it is not.
    """
    return (
        f"max iterations ({max_iterations}) exceeded with no action taken: "
        f"{_ranking_sentence(run_state)}, and "
        f"{_reads_taken_sentence(run_state.evidence)}. That ranking is this run's own "
        f"conclusion — no cause outside it was established by those reads."
    )


def _whole_queue_listed(run_state: RunState) -> bool:
    """True when this run read the dead-letter queue with no slice filter on it.

    ADR 0041's check, and about the CALL rather than the rows: the rule is what the agent looked
    at. Paging is not narrowing, so a page-at-a-time walk has read it whole.
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

    Same shape as ``_refuse_handoff``. It names the call and what the read is FOR — "read the
    queue" alone makes a planner re-read the page it already has (ADR 0031).
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

    NOT terminal: the state stays INVESTIGATING and the refusal is rendered into the next planner
    context under an underscore marker, so the briefing trail and the grader exclude it.
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
