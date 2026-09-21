"""Report a run's progress to the platform so a human can watch it (ADR 0068).

Fail-open and never a tool call: a refusal is logged, swallowed, never charged (invariant 5). One
report per EVENT as it happens, and a shape refusal narrows to ``NARROW_FIELDS`` once (ADR 0074).
"""

from __future__ import annotations

import json
import logging
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Literal, NamedTuple
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from incident_commander.agent.briefing import EscalationBriefing
from incident_commander.agent.hypothesis import Hypothesis
from incident_commander.agent.orchestrator import Checkpointer
from incident_commander.agent.planner_context import (
    ATTEMPT_FAILED_MARKER,
    VERIFY_JUDGE_MARKER,
)
from incident_commander.agent.state import EvidenceEntry, RunState
from incident_commander.agent.thinking import ObservedThinking, PlannerLog
from incident_commander.tools.mcp_client import MCPClientProtocol, MCPError
from incident_commander.tools.policies import Tier, tier_of

_LOG: Final = logging.getLogger(__name__)

#: The two platform tools this module calls. Deliberately NOT in ``TOOL_REGISTRY``: a registry
#: entry is what puts a tool on the planner's page.
REPORT_RUN_TOOL: Final = "report_agent_run"
REPORT_BRIEFING_TOOL: Final = "report_agent_briefing"

#: Both of them, for anything that has to leave the reporter's own traffic out of a stream
#: (``ToolCallLog``, which would otherwise report its reports as steps, forever).
REPORT_TOOLS: Final[frozenset[str]] = frozenset({REPORT_RUN_TOOL, REPORT_BRIEFING_TOOL})

#: Timeout for one report, well under the client's 30 s default: a platform slow to accept a
#: report must cost the run seconds, not minutes.
_REPORT_TIMEOUT_SECONDS: Final = 5.0

#: How long a prose blob may be, matching ``report_agent_briefing``'s own ``maxLength``.
#: Truncated here rather than refused there, because a refusal loses the whole briefing.
_MAX_PROSE_CHARS: Final = 20_000

#: How much reasoning one hypothesis, plan or verdict may carry (WO-R3-328): the tool refuses an
#: over-long string, and a console panel shows a sentence.
_MAX_EXCERPT_CHARS: Final = 280

#: And how much of what a tool ANSWERED one step may carry — longer, because the audit row
#: records that a read happened and never what it returned.
_MAX_RESULT_EXCERPT_CHARS: Final = 400

#: The platform's caps on the short identifying fields. Over the limit they are REFUSED rather than
#: truncated (plat #230), and ``Hypothesis.name`` is free-form model output, so this is a real path.
_MAX_NAME_CHARS: Final = 128
_MAX_CATEGORY_CHARS: Final = 64
#: Same reasoning, for a verdict string the platform leaves open.
_MAX_VERDICT_CHARS: Final = 64
#: And for a step's outcome. 64, not 128: this module said 128 until
#: ``TestTheMirrorMatchesTheContract`` compared it with the snapshot, and a transport-error
#: outcome is easily over 64.
_MAX_OUTCOME_CHARS: Final = 64

#: JSON-RPC's "Invalid params". MEASURED on platform v0.6.15: an undeclared field comes back as a
#: JSON-RPC ERROR with this code, not as a 200 carrying ``isError``, so reading only the
#: ``isError`` path left a whole rehearsal's reports failing and the console empty.
_INVALID_PARAMS: Final = -32602

#: What a refusal must name before it is read as "this run cannot be reported" rather than "this
#: platform does not know these fields yet": a shape refusal is recoverable, a finished run is not.
_RUN_LEVEL_REFUSALS: Final[frozenset[str]] = frozenset(
    {
        "agent_run_already_finished",
        "agent_run_not_found",
        "agent_run_briefing_already_recorded",
    }
)

#: Exactly the input fields platform v0.6.15 declares for ``report_agent_run``. The fallback
#: payload KEEPS these rather than dropping the new ones, so a field added later cannot leak in
#: by being forgotten; ``tests/unit/test_run_reporting.py`` pins the set against the snapshot.
NARROW_FIELDS: Final[tuple[str, ...]] = (
    "run_id",
    "state",
    "at",
    "alert_id",
    "run_label",
    "current_hypothesis",
    "last_step",
)


# What a report may contain — a local MIRROR of the tool's input schema, never the contract, so a
# reporter bug surfaces as a log line rather than a platform refusal mid-demo. These schemas reach
# no prompt and no tool definition, which is why docstrings are safe on them.


class _CurrentHypothesis(BaseModel):
    """``current_hypothesis`` — the leading explanation, in v0.6.15's three fields.

    Deliberately WITHOUT ``reasoning_excerpt``: ``HypothesisReport`` forbids unknown keys, and
    the reasoning travels on the ranked list instead.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(max_length=128)
    category: str | None = Field(default=None, max_length=64)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class _RankedHypothesis(BaseModel):
    """One entry of the ranked ``hypotheses`` list, which does carry its reasoning."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(max_length=128)
    category: str | None = Field(default=None, max_length=64)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    reasoning_excerpt: str | None = Field(default=None, max_length=_MAX_EXCERPT_CHARS)


class _LastStep(BaseModel):
    """``last_step`` — v0.6.15's shape, kept as it is and still sent on every report."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["read", "action"]
    tool: str = Field(max_length=128)
    at: datetime | None = None


class _Step(BaseModel):
    """One ``step`` — appended by the platform, so exactly one travels per report."""

    model_config = ConfigDict(extra="forbid")

    #: The contract's bound is ``>= 0``; this reporter's own first step is 1.
    seq: int = Field(ge=0)
    # ``report`` is the platform's word for the agent TELLING the operator something rather than
    # a call it made (ADR 0075). It is never the run's ``last_step``, whose kind has no member.
    kind: Literal["read", "action", "report"]
    tool: str = Field(max_length=128)
    arguments: dict[str, Any] = Field(default_factory=dict)
    result_excerpt: str | None = Field(default=None, max_length=_MAX_RESULT_EXCERPT_CHARS)
    outcome: str | None = Field(default=None, max_length=_MAX_OUTCOME_CHARS)
    latency_ms: int | None = Field(default=None, ge=0)
    at: datetime | None = None


class _Plan(BaseModel):
    """``plan`` — what the run decided to do, sent once per distinct plan."""

    model_config = ConfigDict(extra="forbid")

    action_tool: str = Field(max_length=128)
    action_arguments: dict[str, Any] = Field(default_factory=dict)
    target_hypothesis: str | None = Field(default=None, max_length=128)
    rationale_excerpt: str | None = Field(default=None, max_length=_MAX_EXCERPT_CHARS)


class _Verification(BaseModel):
    """``verification`` — one verify poll's verdict, or one attempt's closing verdict."""

    model_config = ConfigDict(extra="forbid")

    verdict: str = Field(max_length=64)
    reasoning_excerpt: str | None = Field(default=None, max_length=_MAX_EXCERPT_CHARS)
    attempt: int | None = Field(default=None, ge=1)
    of: int | None = Field(default=None, ge=1)


class _Budget(BaseModel):
    """``budget`` — the ledger as a meter, so the console can show what is left."""

    model_config = ConfigDict(extra="forbid")

    tool_calls_used: int = Field(ge=0)
    #: ``null`` is the contract's "no limit" (plat #230), which this reporter never sends:
    #: invariant 7 gives every run an explicit ceiling, so there is always a number.
    tool_calls_max: int | None = Field(default=None, ge=0)
    tokens_used: int = Field(ge=0)
    usd_used: float = Field(ge=0.0)
    wall_seconds: float = Field(ge=0.0)


class _RunReport(BaseModel):
    """The whole ``report_agent_run`` payload, as WO-R3-328 defines it."""

    model_config = ConfigDict(extra="forbid")

    run_id: UUID
    state: str
    at: datetime | None = None
    alert_id: UUID | None = None
    run_label: str | None = Field(default=None, max_length=128)
    current_hypothesis: _CurrentHypothesis | None = None
    last_step: _LastStep | None = None
    hypotheses: list[_RankedHypothesis] | None = None
    plan: _Plan | None = None
    verification: _Verification | None = None
    step: _Step | None = None
    budget: _Budget | None = None


# ---------------------------------------------------------------------------
# What the client saw — the only place latency, outcome and raw output exist.


@dataclass(frozen=True, slots=True)
class ObservedCall:
    """One tool call as the MCP client measured it.

    ``arguments`` are the WIRED ones — the bytes the platform hashed — because a console showing
    an action must show the value that was sent.
    """

    tool: str
    arguments: dict[str, Any]
    result_excerpt: str | None
    #: ``ok``, ``refused`` (a 200 carrying ``isError``) or ``error: <type>: <message>``. Three
    #: outcomes, not a boolean: "the platform said no" and "the call never landed" differ.
    outcome: str
    latency_ms: int | None
    at: datetime


def _now() -> datetime:
    """The wall clock, as ``ToolCallLog``'s default stamp for an observed call."""
    return datetime.now(UTC)


class ToolCallLog:
    """Every tool call the agent made, collected off the ``MCPClient`` tracer hook.

    The one seam that sees a call's wall time, its raw blocks and the calls that FAILED, none of
    which reach ``RunState.evidence``. It swallows its own failures and skips the reporter's tools.
    """

    def __init__(
        self,
        *,
        skip: frozenset[str] = REPORT_TOOLS,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self.skip = skip
        # The tracer record carries a DURATION and no timestamp, so the stamp is taken here,
        # immediately after the call returned.
        self.clock = clock
        self._calls: list[ObservedCall] = []
        #: Who to hand a call to the moment it is observed (ADR 0074). A sink answering ``False``
        #: leaves the call buffered for the next transition.
        self._sink: Callable[[ObservedCall], bool] | None = None

    def subscribe(self, sink: Callable[[ObservedCall], bool]) -> None:
        """Report each call as it returns rather than at the next transition (ADR 0074).

        One subscriber, because one reporter reports one run. The sink must not raise — this runs
        inside ``call_tool`` — and the ``tee`` guard below is the second belt.
        """
        self._sink = sink

    def observe(self, record: Mapping[str, Any]) -> None:
        """Record one traced call. Takes the tracer's own record shape.

        Buffered only when the subscriber says it could not send: a call reported once must not
        sit here too, or the next transition would report it again under a second ``seq``.
        """
        tool = record.get("tool_name")
        if not isinstance(tool, str) or tool in self.skip:
            return
        error = record.get("error")
        result = record.get("result")
        if isinstance(error, str) and error:
            outcome, excerpt = _capped(f"error: {error}", _MAX_OUTCOME_CHARS), None
        else:
            refused = bool(result.get("is_error")) if isinstance(result, Mapping) else False
            outcome = "refused" if refused else "ok"
            excerpt = _first_text_block(result)
        call = ObservedCall(
            tool=tool,
            arguments=dict(record.get("arguments") or {}),
            result_excerpt=_excerpt(excerpt, _MAX_RESULT_EXCERPT_CHARS),
            outcome=outcome,
            latency_ms=_latency_ms(record.get("duration_seconds")),
            at=self.clock(),
        )
        if self._sink is not None and self._sink(call):
            return
        self._calls.append(call)

    def tee(
        self, inner: Callable[[dict[str, Any]], None] | None
    ) -> Callable[[dict[str, Any]], None]:
        """This log beside an existing tracer hook, as one hook.

        The inner hook runs FIRST and unguarded: the JSONL trace is the run's own append-only
        record and must not become second to telemetry.
        """

        def hook(record: dict[str, Any]) -> None:
            if inner is not None:
                inner(record)
            try:
                self.observe(record)
            except Exception as err:  # noqa: BLE001 - a tracer must never fail a tool call
                _LOG.warning("agent-run reporting: dropped one observed call: %s", err)

        return hook

    def drain(self) -> list[ObservedCall]:
        """Take everything recorded so far, leaving the log empty."""
        taken, self._calls = self._calls, []
        return taken

    def forget(self) -> None:
        """Drop what is recorded without reporting it: the scenario's precondition probes go
        through the agent's own client but are the EVALUATOR's reads (ADR 0038)."""
        self._calls.clear()


def _first_text_block(result: object) -> str | None:
    """The text of a tool result's first content block — what the agent actually read."""
    blocks = result.get("content") if isinstance(result, Mapping) else None
    if not isinstance(blocks, Sequence) or isinstance(blocks, str | bytes) or not blocks:
        return None
    first = blocks[0]
    if not isinstance(first, Mapping):
        return None
    text = first.get("text")
    if isinstance(text, str):
        return text
    # A non-text block is still evidence something came back, so it is rendered, not dropped.
    try:
        return json.dumps(first, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return None


def _latency_ms(duration_seconds: object) -> int | None:
    """Milliseconds from the tracer's seconds; ``None`` when it did not measure one."""
    if isinstance(duration_seconds, bool) or not isinstance(duration_seconds, int | float):
        return None
    return max(int(duration_seconds * 1000), 0)


def _excerpt(text: str | None, limit: int) -> str | None:
    """``text`` cut to ``limit`` characters, with the cut made visible.

    ``None`` for nothing and for whitespace: the tool reads absence as "the caller did not say"
    rather than as an empty reading.
    """
    if text is None:
        return None
    collapsed = text.strip()
    if not collapsed:
        return None
    if len(collapsed) <= limit:
        return collapsed
    return f"{collapsed[: limit - 1]}…"


def _capped(text: str, limit: int) -> str:
    """``text`` cut to ``limit``, keeping a value where the platform requires one.

    Never ``None``, unlike ``_excerpt``: these are the REQUIRED short fields, and the platform
    refuses the whole report over a cap — which shows a person nothing instead of slightly less.
    """
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


# ---------------------------------------------------------------------------
# What the console is shown, derived from the run's own state.


def top_hypothesis(run_state: RunState) -> dict[str, Any] | None:
    """The run's leading explanation, in the shape ``report_agent_run`` takes.

    ``hypotheses[0]`` is the ranking's top entry, read the same way by every other module, so the
    console shows what the run is acting on. ``None`` reads as "no explanation yet".
    """
    return top_of(run_state.hypotheses)


def top_of(hypotheses: Sequence[Hypothesis]) -> dict[str, Any] | None:
    """``top_hypothesis`` over a ranking that is not (yet) the run state's.

    Split out for ADR 0075: a thinking report goes out mid-transition, carrying the ranking the
    planner just produced. One renderer either way, or the two would cap differently.
    """
    if not hypotheses:
        return None
    top = hypotheses[0]
    return {
        "name": _capped(top.name, _MAX_NAME_CHARS),
        "category": _capped(top.category.value, _MAX_CATEGORY_CHARS),
        "confidence": top.confidence,
    }


def ranked_hypotheses(run_state: RunState) -> list[dict[str, Any]] | None:
    """Every hypothesis the run holds, in rank order, each with its reasoning excerpt.

    **The ORDER is the ranking** — the platform stores it as sent and never re-sorts (plat #230).
    ``None`` never clears an earlier ranking; an empty LIST would claim it considered nothing.
    """
    return ranked_of(run_state.hypotheses)


def ranked_of(hypotheses: Sequence[Hypothesis]) -> list[dict[str, Any]] | None:
    """``ranked_hypotheses`` over a ranking that is not (yet) the run state's (ADR 0075)."""
    if not hypotheses:
        return None
    return [
        {
            "name": _capped(hypothesis.name, _MAX_NAME_CHARS),
            "category": _capped(hypothesis.category.value, _MAX_CATEGORY_CHARS),
            "confidence": hypothesis.confidence,
            "reasoning_excerpt": _excerpt(hypothesis.reasoning, _MAX_EXCERPT_CHARS),
        }
        for hypothesis in hypotheses
    ]


def last_step(run_state: RunState) -> dict[str, Any] | None:
    """The most recent real call the run made, or ``None`` before its first.

    Underscore markers are excluded as everywhere else, so the step the console shows is one a
    human could look up in the audit log.
    """
    for entry in reversed(run_state.evidence):
        if entry.tool_name.startswith("_"):
            continue
        return {
            "kind": _step_kind(entry.tool_name),
            "tool": entry.tool_name,
            "at": entry.timestamp.isoformat(),
        }
    return None


def _step_kind(tool_name: str) -> str:
    """``read`` when the step only observed, ``action`` when it changed something.

    An unclassifiable tool reports as ``action`` deliberately: ``read`` is the stronger claim,
    and over-reporting a change is the safe direction for a person watching.
    """
    try:
        return "read" if tier_of(tool_name) is Tier.READ else "action"
    except Exception:
        return "action"


def plan_payload(run_state: RunState) -> dict[str, Any] | None:
    """The stored remediation plan, in the shape the console's plan card reads.

    From ``RunState.remediation_plan`` rather than the ``_planner_plan`` marker, which carries the
    target hypothesis only. The excerpt is absent on a scripted plan, which is honest.
    """
    plan = run_state.remediation_plan
    if not isinstance(plan, Mapping):
        return None
    action_tool = plan.get("action_tool")
    if not isinstance(action_tool, str):
        return None
    arguments = plan.get("action_arguments")
    target = plan.get("target_hypothesis")
    rationale = plan.get("action_rationale")
    return {
        "action_tool": _capped(action_tool, _MAX_NAME_CHARS),
        "action_arguments": dict(arguments) if isinstance(arguments, Mapping) else {},
        # The hypothesis NAME, so it is free-form model output and capped like one.
        "target_hypothesis": _capped(target, _MAX_NAME_CHARS) if isinstance(target, str) else None,
        "rationale_excerpt": _excerpt(
            rationale if isinstance(rationale, str) else None, _MAX_EXCERPT_CHARS
        ),
    }


def budget_payload(run_state: RunState) -> dict[str, Any]:
    """The ledger as the console's budget meter reads it (ADR 0015's numbers, unchanged).

    ``usd_used`` goes over the wire as a number rather than as ``Decimal``'s string: the
    ledger's precision is the ledger's, and what a meter needs is a value it can draw.
    """
    budget = run_state.budget
    return {
        "tool_calls_used": budget.tool_calls_used,
        # Always a number: ``null`` is the contract's "no limit", and invariant 7 says no run
        # has one. The scenario's declared cap arrives here (ADR 0019), not the fleet default.
        "tool_calls_max": budget.max_tool_calls,
        "tokens_used": budget.tokens_used,
        "usd_used": round(float(budget.usd_used), 6),
        "wall_seconds": round(budget.wall_seconds_used, 3),
    }


def _verification_from_judge(entry: EvidenceEntry) -> dict[str, Any]:
    """One verify poll's verdict, from the ``_verify_judge`` ledger entry.

    ``result_summary`` is ``"<verdict>: <reasoning>"`` and ``arguments`` carry ``{attempt, of}``,
    the ordinals that tell poll 2/4 from 4/4.
    """
    verdict, separator, reasoning = entry.result_summary.partition(": ")
    return {
        "verdict": _capped(verdict, _MAX_VERDICT_CHARS) if separator else "unknown",
        "reasoning_excerpt": _excerpt(reasoning or entry.result_summary, _MAX_EXCERPT_CHARS),
        "attempt": _ordinal(entry.arguments.get("attempt")),
        "of": _ordinal(entry.arguments.get("of")),
    }


def _verification_from_attempt(entry: EvidenceEntry) -> dict[str, Any]:
    """An attempt's closing verdict, from the ``_remediation_attempt_failed`` entry.

    The only place ``verified_stabilizer`` and ``verified_unresolved`` exist: the judge says
    ``verified`` and ADR 0026 / ADR 0056 then decide the incident did not end.
    """
    verdict = entry.arguments.get("verdict")
    return {
        "verdict": _capped(verdict, _MAX_VERDICT_CHARS)
        if isinstance(verdict, str)
        else "not_verified",
        "reasoning_excerpt": _excerpt(entry.result_summary, _MAX_EXCERPT_CHARS),
        "attempt": _ordinal(entry.arguments.get("attempt")),
        "of": _ordinal(entry.arguments.get("of")),
    }


def _ordinal(value: object) -> int | None:
    """An ``{attempt, of}`` ordinal, or ``None`` when it is not a countable one."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 1 else None


class _Pending(NamedTuple):
    """One thing to report, and the moment it happened."""

    #: ``{"step": {...}}`` or ``{"verification": {...}}`` — the field it travels in.
    payload: dict[str, Any]
    at: datetime


class Delivery(NamedTuple):
    """What one attempted report did: whether it landed, and why it did not."""

    ok: bool
    #: The platform's own words when it refused, as ``isError`` or a JSON-RPC error. ``None``
    #: when the call never landed at all.
    refusal: str | None
    #: Whether narrowing could help — i.e. the platform rejected these ARGUMENTS rather than
    #: the run, the principal or the connection.
    shape_refusal: bool = False


class RunReporter:
    """Reports one run's state, hypotheses, plan, steps, verdicts and budget.

    Every method swallows every failure: a reporting problem must be invisible to the run.
    """

    def __init__(
        self,
        client: MCPClientProtocol,
        *,
        run_id: UUID,
        run_label: str | None = None,
        alert_id: UUID | None = None,
        tool_log: ToolCallLog | None = None,
        planner_log: PlannerLog | None = None,
    ) -> None:
        self._client = client
        self._run_id = str(run_id)
        # `run_label`, NOT `scenario`: the tool's input model forbids unknown fields, so the
        # wrong spelling is a refused report rather than an ignored one.
        self._run_label = run_label
        self._alert_id = str(alert_id) if alert_id is not None else None
        #: Where the steps come from. ``None`` reports everything except them.
        self._tool_log = tool_log
        #: The run as the last report saw it, so a step reported mid-transition carries what was
        #: true when the call was made. ``None`` is the one window where a step is buffered.
        self._last_state: RunState | None = None
        #: Whether the widened fields are still being sent. Latched off by the first refusal
        #: that names no run-level code — an older platform.
        self._widened = True
        #: Monotonic across the run: the console orders the action ledger by it.
        self._seq = 0
        #: How much of the evidence ledger has been turned into reports already.
        self._reported_entries = 0
        #: The state the last report carried, which is the state the run was in while it
        #: made the calls this report is about to describe.
        self._state_reported: str | None = None
        #: The plan as last sent, so a second attempt's different plan is reported and the
        #: same plan is not re-sent on every report after it.
        self._plan_sent: str | None = None
        #: Reports that failed, for the run's own summary line. A count, not a rail: it exists so
        #: "the console was empty" has an answer other than a broken frontend.
        self.failures: list[str] = []
        #: Why the widened fields were dropped, once. NOT in ``failures``: a refused report that
        #: then landed narrow is one failure, not two.
        self.narrowed_because: str | None = None
        self.reports_sent = 0
        self.steps_sent = 0
        self.thinking_sent = 0
        self.verifications_sent = 0
        self.briefing_sent = False
        if tool_log is not None:
            # ADR 0074: the hook fires when a call returns, so that is when its step is reported.
            # Subscribed here, not by the runner, so no caller gets queued behaviour by omission.
            tool_log.subscribe(self._report_call)
        if planner_log is not None:
            # ADR 0075, for the same reason: the loop writes a ranking the moment it accepts one.
            planner_log.subscribe(self._report_thinking)

    @property
    def run_id(self) -> str:
        """The id every report in this run shares — printed so an operator can find it."""
        return self._run_id

    @property
    def widened(self) -> bool:
        """Whether this run is still reporting the WO-R3-328 fields."""
        return self._widened

    def report(self, run_state: RunState) -> None:
        """Report where the run is now, and anything that happened since the last report.

        One call per pending verdict, plus the run's own state on the last of them. Upsert by
        ``run_id``, so repeating a state appends nothing to the platform's phase history.
        """
        # 1. Publish the state a live step will stamp itself with, before the pending list is
        #    built: a call made during the NEXT transition belongs to that transition.
        self._last_state = run_state
        # 2. What has happened since the last report.
        pending = self._pending(run_state)
        if not self._widened:
            # A narrow payload carries no step, so N of them would say the same thing N times.
            pending = pending[-1:]
        # 3. Nothing pending: one report carrying the state alone.
        if not pending:
            self._deliver(self._payload(run_state, item=None, final=True), narrow_retry=True)
            return
        # 4. Otherwise one report per item, the run's own state riding on the last of them.
        for position, item in enumerate(pending, start=1):
            final = position == len(pending)
            if not self._widened and not final:
                # The narrowing latched mid-report, and everything this item carries is a field
                # the narrow form drops. Skipped rather than sent as a duplicate state report.
                continue
            self._deliver(self._payload(run_state, item=item, final=final), narrow_retry=final)

    def report_briefing(self, briefing: EscalationBriefing, *, prose: str | None = None) -> None:
        """Report the finished handoff. Once per run; a second call is refused by design."""
        if self.briefing_sent:
            # Refused here rather than at the platform: a local guard keeps the log honest
            # about which call was the real one, and the 409 it would earn is not news.
            _LOG.debug("run %s: briefing already reported; not sending a second", self._run_id)
            return
        arguments: dict[str, Any] = {
            "run_id": self._run_id,
            "briefing": briefing.model_dump(mode="json"),
        }
        if prose:
            arguments["prose"] = prose[:_MAX_PROSE_CHARS]
        if self._call(REPORT_BRIEFING_TOOL, arguments).ok:
            self.briefing_sent = True

    # -- reporting one call, the moment it returns ---------------------------

    def _report_call(self, call: ObservedCall) -> bool:
        """Report one observed call as its own step, now (ADR 0074).

        ``False`` means "not sent, keep it" — no state to stamp yet, or a narrowed platform — and
        the next transition's report picks it up. Never raises: it runs inside the tracer hook.
        """
        state = self._last_state
        if state is None or not self._widened:
            return False
        try:
            item = self._step_of(call)
            # Intermediate: the state is the one the run was in while it made this call, and
            # `at` is the call's own moment, never the report's.
            self._deliver(self._payload(state, item=item, final=False), narrow_retry=False)
        except Exception as err:  # noqa: BLE001 - telemetry may never fail a tool call
            self._note(f"{REPORT_RUN_TOOL}: reporting a live step failed: {err}")
        return True

    def _report_thinking(self, thinking: ObservedThinking) -> bool:
        """Report one accepted ranking or verdict as its own ``report`` step, now (ADR 0075).

        The ranking on the payload is the observation's OWN, because ``_last_state`` still holds
        the one from before this call. ``False`` leaves it to the next transition report.
        """
        state = self._last_state
        if state is None or not self._widened:
            return False
        try:
            item = self._thinking_step(thinking)
            payload = self._payload(state, item=item, final=False)
            # The ranking this call produced, in both fields a console panel reads.
            payload["hypotheses"] = ranked_of(thinking.hypotheses)
            payload["current_hypothesis"] = top_of(thinking.hypotheses)
            self._deliver(payload, narrow_retry=False, may_narrow=False)
        except Exception as err:  # noqa: BLE001 - telemetry may never fail a transition
            self._note(f"{REPORT_RUN_TOOL}: reporting the run's thinking failed: {err}")
        return True

    # -- assembling one report ----------------------------------------------

    def _pending(self, run_state: RunState) -> list[_Pending]:
        """Everything since the last report that has not already been reported, in order.

        The verdicts come from the evidence ledger, the only place they exist. Since ADR 0074 the
        steps are normally already sent; what is left is what the live path could not.
        """
        # 1. The ledger slice nobody has reported yet, and whatever the client seam buffered.
        entries = run_state.evidence[self._reported_entries :]
        self._reported_entries = len(run_state.evidence)
        observed = deque(self._tool_log.drain() if self._tool_log is not None else [])
        live_steps = self._tool_log is not None
        pending: list[_Pending] = []
        # 2. Walk the slice: a verdict is a verification, a real call is a step.
        for entry in entries:
            if entry.tool_name == VERIFY_JUDGE_MARKER:
                pending.append(
                    _Pending({"verification": _verification_from_judge(entry)}, entry.timestamp)
                )
                continue
            if entry.tool_name == ATTEMPT_FAILED_MARKER:
                pending.append(
                    _Pending({"verification": _verification_from_attempt(entry)}, entry.timestamp)
                )
                continue
            if entry.tool_name.startswith("_"):
                continue
            if live_steps:
                # Its step went out when the call returned; a second here is the same call
                # under a second `seq`.
                continue
            pending.append(self._step(entry, _take(observed, entry.tool_name)))
        # 3. Anything the live path could not send, in the order the calls happened.
        pending.extend(self._step_of(call) for call in observed)
        return pending

    def _step(self, entry: EvidenceEntry, call: ObservedCall | None) -> _Pending:
        """One ledger entry as a step, enriched with what the client measured."""
        if call is not None:
            return self._step_of(call)
        # No observed call: an untraced client, or a resumed run whose earlier calls happened
        # in another process. The ledger's own summary is the honest excerpt.
        return self._new_step(
            tool=entry.tool_name,
            arguments=dict(entry.arguments),
            result_excerpt=_excerpt(entry.result_summary, _MAX_RESULT_EXCERPT_CHARS),
            outcome="ok",
            latency_ms=None,
            at=entry.timestamp,
        )

    def _step_of(self, call: ObservedCall) -> _Pending:
        return self._new_step(
            tool=call.tool,
            arguments=call.arguments,
            result_excerpt=call.result_excerpt,
            outcome=call.outcome,
            latency_ms=call.latency_ms,
            at=call.at,
        )

    def _thinking_step(self, thinking: ObservedThinking) -> _Pending:
        """One accepted ranking or verdict as a ``report``-kind step (ADR 0075).

        ``seq`` comes from the same counter the tool steps use, so the ledger holds the order the
        run made its moves in. ``latency_ms`` is absent: that time is on the ``StepRecord``.
        """
        return self._new_step(
            tool=thinking.tool,
            arguments={
                "ranking": thinking.ranking(),
                "next_action": thinking.action(),
                "reason": thinking.reason,
            },
            result_excerpt=_excerpt(thinking.sentence(), _MAX_RESULT_EXCERPT_CHARS),
            outcome="ok",
            latency_ms=None,
            at=thinking.at,
            kind="report",
        )

    def _new_step(
        self,
        *,
        tool: str,
        arguments: dict[str, Any],
        result_excerpt: str | None,
        outcome: str,
        latency_ms: int | None,
        at: datetime,
        kind: str | None = None,
    ) -> _Pending:
        self._seq += 1
        return _Pending(
            {
                "step": {
                    # Monotonic across the run, and a REPEATED seq is a no-op on the platform
                    # (plat #230), so a retried report cannot double a row.
                    "seq": self._seq,
                    "kind": _step_kind(tool) if kind is None else kind,
                    "tool": _capped(tool, _MAX_NAME_CHARS),
                    "arguments": arguments,
                    "result_excerpt": result_excerpt,
                    "outcome": outcome,
                    "latency_ms": latency_ms,
                    "at": at.isoformat(),
                }
            },
            at,
        )

    def _payload(
        self, run_state: RunState, *, item: _Pending | None, final: bool
    ) -> dict[str, Any]:
        """One report's arguments: the run as it is, plus at most one pending item.

        ``final`` carries the run's NEW state; earlier items carry the state it was in while it
        made those calls, which keeps a terminal transition's backlog reportable.
        """
        # 1. The state and the stamp: an intermediate report carries the state the run was in
        #    while it made the call, and the call's own moment.
        state = run_state.state.value
        if not final and self._state_reported is not None:
            state = self._state_reported
        at = run_state.updated_at if (final or item is None) else item.at
        # 2. What the console draws from the run itself.
        payload: dict[str, Any] = {
            "run_id": self._run_id,
            "state": state,
            # The agent's own clock for WHEN it moved: omitting it stamps the moment the report
            # landed, a different fact.
            "at": at.isoformat(),
            "current_hypothesis": top_hypothesis(run_state),
            "last_step": last_step(run_state),
            "hypotheses": ranked_hypotheses(run_state),
            "budget": budget_payload(run_state),
        }
        # 3. The one pending item, and the old ``last_step`` field walked forward with it.
        if item is not None:
            payload.update(item.payload)
            step = item.payload.get("step")
            # A ``report`` step never becomes ``last_step``, and could not: v0.6.15's
            # ``last_step.kind`` has no such member, so writing it would refuse the report.
            if step is not None and step["kind"] != "report":
                # The old field walks forward with the new one, so either reader sees one call.
                payload["last_step"] = {
                    "kind": step["kind"],
                    "tool": step["tool"],
                    "at": step["at"],
                }
        # 4. The plan, once per distinct plan, on the report that carries the new state.
        if final:
            plan = plan_payload(run_state)
            if plan is not None and json.dumps(plan, sort_keys=True) != self._plan_sent:
                payload["plan"] = plan
        # 5. The identifying fields, on EVERY report: the tool fills them in once and never
        #    clears them, so repeating costs nothing and a dropped first report is recoverable.
        if self._run_label is not None:
            payload["run_label"] = _capped(self._run_label, _MAX_NAME_CHARS)
        if self._alert_id is not None:
            payload["alert_id"] = self._alert_id
        return payload

    # -- sending it ---------------------------------------------------------

    def _deliver(
        self, payload: dict[str, Any], *, narrow_retry: bool, may_narrow: bool = True
    ) -> None:
        """Send one report, narrowing once if this platform does not know the new fields.

        ``narrow_retry`` is False where the narrow form carries only a state the next report sends.
        ``may_narrow`` is False for a thinking report, where latching punishes one row (ADR 0075).
        """
        # 1. The widened form, while this platform still accepts one.
        if self._widened:
            body = self._validated(payload)
            if body is not None:
                delivery = self._call(REPORT_RUN_TOOL, body)
                if delivery.ok:
                    self._accept(body)
                    return
                # 2. A transport, scope or run-level failure: fewer fields help none of them.
                if not delivery.shape_refusal:
                    return
                if not may_narrow:
                    return
                self._narrow(f"the platform refused the widened report: {delivery.refusal}")
            else:
                if not may_narrow:
                    return
                self._narrow("the reporter's own widened payload failed local validation")
            if not narrow_retry:
                return
        # 3. The narrow form, so the state at least lands.
        narrow = self._validated(_narrow_payload(payload))
        if narrow is None:
            return
        if self._call(REPORT_RUN_TOOL, narrow).ok:
            self._accept(narrow)

    def _validated(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        """``payload`` if it matches the tool's input schema, else ``None`` and a log line.

        The ORIGINAL dict is sent: the model is a gate, not a serializer, so no field is
        silently renamed or re-typed on the way out.
        """
        try:
            _RunReport.model_validate(payload)
        except ValidationError as err:
            self._note(f"{REPORT_RUN_TOOL}: the reporter built an invalid payload: {err}")
            return None
        return payload

    def _accept(self, body: Mapping[str, Any]) -> None:
        """Book what the platform accepted, so the summary line is about what landed."""
        self.reports_sent += 1
        self._state_reported = str(body["state"])
        step = body.get("step")
        if isinstance(step, Mapping):
            # Counted apart, because "N steps reported" is compared against the platform's
            # ``agent.tool_invoked`` rows and a thinking row is not a call (WO-R3-336).
            if step.get("kind") == "report":
                self.thinking_sent += 1
            else:
                self.steps_sent += 1
        if "verification" in body:
            self.verifications_sent += 1
        plan = body.get("plan")
        if plan is not None:
            self._plan_sent = json.dumps(plan, sort_keys=True)

    def _narrow(self, why: str) -> None:
        """Latch the fallback and say so once — never once per call."""
        if not self._widened:
            return
        self._widened = False
        self.narrowed_because = why
        _LOG.warning(
            "run %s: %s. Falling back to the fields platform v0.6.15 accepts "
            "(state, hypothesis, last step) for the rest of this run; the console's agent "
            "panel will be thin until the commander is re-pinned to a platform that "
            "declares the widened input. The run itself is unaffected.",
            self._run_id,
            why,
        )

    def _call(self, tool: str, arguments: Mapping[str, Any]) -> Delivery:
        """One report. Says whether it landed and how it did not; never raises.

        BOTH refusal routes are read: a 200 carrying ``isError``, and ``_INVALID_PARAMS``, which is
        what an older platform answers. ``Exception``, not ``BaseException``, so Ctrl-C still works.
        """
        try:
            result = self._client.call_tool(
                tool, arguments, timeout_seconds=_REPORT_TIMEOUT_SECONDS
            )
        except MCPError as err:
            self._note(f"{tool}: MCPError: {err}")
            # Only "invalid params" is a shape refusal. A 403 and a transport failure arrive as
            # ``MCPError`` too, and fewer fields would only hide a missing scope.
            return Delivery(False, str(err), err.code == _INVALID_PARAMS)
        except Exception as err:  # noqa: BLE001 - fail-open is the whole contract
            self._note(f"{tool}: {type(err).__name__}: {err}")
            return Delivery(False, None)
        if result.is_error:
            # A tool-level refusal is a 200 with `isError`, so reading only the transport counts
            # a refused report as delivered (C-02).
            refusal = _error_text(result)
            self._note(f"{tool}: platform refused the report: {refusal}")
            return Delivery(False, refusal, _is_shape_refusal(refusal))
        return Delivery(True, None)

    def _note(self, detail: str) -> None:
        """Record and log one failed report, at WARNING — visible, never fatal."""
        self.failures.append(detail)
        _LOG.warning(
            "run %s: agent-run reporting failed (the run is unaffected): %s",
            self._run_id,
            detail,
        )


def _take(observed: deque[ObservedCall], tool: str) -> ObservedCall | None:
    """Pull the first observed call for ``tool``, or ``None`` when none was recorded.

    By NAME as well as order, so a mismatch cannot silently attach one call's latency to
    another call's row.
    """
    for position, call in enumerate(observed):
        if call.tool == tool:
            del observed[position]
            return call
    return None


def _narrow_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """``payload`` reduced to the fields platform v0.6.15 declares — by KEEPING
    ``NARROW_FIELDS``, so a field added to the widened form later cannot leak in."""
    return {name: payload[name] for name in NARROW_FIELDS if name in payload}


def _is_shape_refusal(refusal: str) -> bool:
    """Whether a refusal reads as "this platform does not know those fields".

    Decided by what the refusal does NOT say: a run-level code (finished, unknown, already
    briefed) is not helped by narrowing. Everything else earns one narrow retry.
    """
    lowered = refusal.lower()
    return not any(code in lowered for code in _RUN_LEVEL_REFUSALS)


def _error_text(result: Any) -> str:
    """The text blocks of an errored tool result, for the log line."""
    blocks = getattr(result, "content", None) or []
    parts = [
        str(block["text"])
        for block in blocks
        if isinstance(block, dict) and isinstance(block.get("text"), str)
    ]
    return " ".join(parts)[:400] or "<no content>"


class ReportingCheckpointer:
    """The loop's checkpoint seam with a run report hung off it.

    A ``Checkpointer`` decorator rather than a hook on ``run_to_completion``, so telemetry needed no
    change to ``agent/loop.py``. It implements that Protocol and nothing more.
    """

    def __init__(self, inner: Checkpointer, reporter: RunReporter) -> None:
        self._inner = inner
        self._reporter = reporter

    def load(self, incident_id: UUID) -> RunState | None:
        return self._inner.load(incident_id)

    def write(self, run_state: RunState) -> None:
        """Checkpoint first, report second. The order IS the fail-open guarantee: reversed, a slow
        report stands between a transition and its durable record, and a crash loses the run."""
        self._inner.write(run_state)
        self._reporter.report(run_state)


def summarize(reporter: RunReporter | None) -> str:
    """One line about what reporting did: the run id an operator needs to find the record, and
    any failures, rather than leaving an empty console unexplained."""
    if reporter is None:
        return "agent-run reporting: off"
    parts = [
        f"agent-run reporting: run {reporter.run_id}",
        f"{reporter.reports_sent} report(s) accepted",
        f"{reporter.steps_sent} step(s)",
        f"{reporter.thinking_sent} thinking step(s)",
        f"{reporter.verifications_sent} verification(s)",
        f"briefing {'sent' if reporter.briefing_sent else 'NOT sent'}",
    ]
    if not reporter.widened:
        parts.append("NARROWED to the pre-v0.6.16 fields (see the log)")
    if reporter.failures:
        parts.append(f"{len(reporter.failures)} FAILED (first: {reporter.failures[0]})")
    return ", ".join(parts)
