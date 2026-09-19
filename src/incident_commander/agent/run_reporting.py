"""Report a run's progress to the platform so a human can watch it (ADR 0068).

The agent reports its own run over two platform tools the model never chooses —
``report_agent_run`` and ``report_agent_briefing``, both stamped ``[commander: telemetry]``
and scoped ``agent_runs:write`` (platform ADR 0035). The platform stores the record and the
operator console reads it; **nothing here is readable by the agent's own principal**, which
is what keeps ADR 0012 intact while a person watches.

Three properties this module exists to hold, in the order they matter:

1. **Reporting is fail-open and never a tool call.** A refused, slow or impossible report
   is logged and swallowed (invariant 5's shape: the agent augments the response path and
   never gates it). It also never touches ``BudgetLedger`` — a report is not an
   investigation step, does not count against ``max_tool_calls``, and cannot push a run
   into escalation. Both halves are pinned by ``tests/unit/test_run_reporting.py``.
2. **It hangs off the checkpoint seam, not off the transitions.** ``ReportingCheckpointer``
   decorates the ``Checkpointer`` the loop already writes to after every transition, so
   there is exactly one place that knows when a run moved, and no transition had to learn
   about telemetry. The checkpoint is written FIRST and the report second, so a reporting
   failure cannot cost a durable checkpoint.
3. **Off unless asked for.** ``evals/runner.py`` builds a reporter only when
   ``AGENT_RUN_REPORTING`` is true, which today only ``make demo-live`` sets.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Final
from uuid import UUID

from incident_commander.agent.briefing import EscalationBriefing
from incident_commander.agent.orchestrator import Checkpointer
from incident_commander.agent.state import EvidenceEntry, RunState
from incident_commander.tools.mcp_client import MCPClientProtocol
from incident_commander.tools.policies import Tier, tier_of

_LOG: Final = logging.getLogger(__name__)

#: The two platform tools this module calls. Named here and nowhere else, and deliberately
#: NOT in ``TOOL_REGISTRY``: a registry entry is what puts a tool on the planner's page
#: (``registry.EXCLUDED_DESCRIPTION_PREFIXES`` is the other half of that decision).
REPORT_RUN_TOOL: Final = "report_agent_run"
REPORT_BRIEFING_TOOL: Final = "report_agent_briefing"

#: Timeout for one report, well under the client's 30 s default. Telemetry waits for
#: nobody: the agent is mid-incident, and a platform slow to accept a report must cost the
#: run seconds, not minutes.
_REPORT_TIMEOUT_SECONDS: Final = 5.0

#: How long a prose blob may be, matching ``report_agent_briefing``'s own ``maxLength``.
#: Truncated here rather than refused there, because a refusal loses the whole briefing.
_MAX_PROSE_CHARS: Final = 20_000


def top_hypothesis(run_state: RunState) -> Mapping[str, Any] | None:
    """The run's leading explanation, in the shape ``report_agent_run`` takes.

    ``hypotheses[0]`` is the ranking's top entry — normalized at the schema boundary and
    read the same way by ``remediation.py``, ``incidents.py`` and ``evals/reward.py``, so
    the console shows the hypothesis the run is actually acting on. ``None`` before the
    first investigation step, which the tool reads as "no explanation yet".
    """
    if not run_state.hypotheses:
        return None
    top = run_state.hypotheses[0]
    return {
        "name": top.name,
        "category": top.category.value,
        "confidence": top.confidence,
    }


def last_step(run_state: RunState) -> Mapping[str, Any] | None:
    """The most recent real call the run made, or ``None`` before its first.

    Underscore-prefixed entries are bookkeeping markers, not calls — the same structural
    filter ``briefing.trail_of`` and ``evals/graders/deterministic.py`` apply, so the step
    the console shows is a step a human could look up in the audit log.
    """
    for entry in reversed(run_state.evidence):
        if entry.tool_name.startswith("_"):
            continue
        return {
            "kind": _step_kind(entry),
            "tool": entry.tool_name,
            "at": entry.timestamp.isoformat(),
        }
    return None


def _step_kind(entry: EvidenceEntry) -> str:
    """``read`` when the step only observed, ``action`` when it changed something.

    Anything the tier map cannot classify is reported as ``action``, deliberately. ``read``
    is the stronger claim — it tells an operator nothing changed — and an unrecognized tool
    cannot support it. Over-reporting a change is the safe direction for a person watching.
    """
    try:
        return "read" if tier_of(entry.tool_name) is Tier.READ else "action"
    except Exception:
        return "action"


class RunReporter:
    """Reports one run's state, hypothesis and last step to the platform.

    Every method swallows every failure. The caller is the loop's checkpoint seam, and a
    reporting problem must be invisible to the run it describes.
    """

    def __init__(
        self,
        client: MCPClientProtocol,
        *,
        run_id: UUID,
        run_label: str | None = None,
        alert_id: UUID | None = None,
    ) -> None:
        self._client = client
        self._run_id = str(run_id)
        # `run_label`, NOT `scenario`: the tool's input model forbids unknown fields
        # (`additionalProperties: false`), so the wrong spelling is a refused report
        # rather than an ignored one.
        self._run_label = run_label
        self._alert_id = str(alert_id) if alert_id is not None else None
        #: Reports that failed, for the run's own summary line. A count, not a rail: it
        #: changes nothing about the run and exists so "the console was empty" has an
        #: answer other than "the frontend is broken".
        self.failures: list[str] = []
        self.reports_sent = 0
        self.briefing_sent = False

    @property
    def run_id(self) -> str:
        """The id every report in this run shares — printed so an operator can find it."""
        return self._run_id

    def report(self, run_state: RunState) -> None:
        """Report where the run is now. Upsert by ``run_id``; safe to repeat."""
        arguments: dict[str, Any] = {
            "run_id": self._run_id,
            "state": run_state.state.value,
            # The agent's own clock for WHEN it moved. Omitting it would have the platform
            # stamp the moment the report landed, which is a different fact.
            "at": run_state.updated_at.isoformat(),
            "current_hypothesis": top_hypothesis(run_state),
            "last_step": last_step(run_state),
        }
        # Sent on every report, not just the first: the tool fills them in once and never
        # clears them, so repeating costs nothing and a dropped first report is recoverable.
        if self._run_label is not None:
            arguments["run_label"] = self._run_label
        if self._alert_id is not None:
            arguments["alert_id"] = self._alert_id
        if self._call(REPORT_RUN_TOOL, arguments):
            self.reports_sent += 1

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
        if self._call(REPORT_BRIEFING_TOOL, arguments):
            self.briefing_sent = True

    def _call(self, tool: str, arguments: Mapping[str, Any]) -> bool:
        """One report. Returns whether it landed; never raises.

        ``Exception`` and not ``BaseException``: a ``KeyboardInterrupt`` or a
        ``SystemExit`` arriving mid-report is the operator stopping the run, and swallowing
        that would make the run unkillable during its own telemetry.
        """
        try:
            result = self._client.call_tool(
                tool, arguments, timeout_seconds=_REPORT_TIMEOUT_SECONDS
            )
        except Exception as err:  # noqa: BLE001 - fail-open is the whole contract
            self._note(f"{tool}: {type(err).__name__}: {err}")
            return False
        if result.is_error:
            # A tool-level refusal is a 200 with `isError`, so reading only the transport
            # would count a refused report as delivered — the `chaos_hooks` C-02 lesson.
            self._note(f"{tool}: platform refused the report: {_error_text(result)}")
            return False
        return True

    def _note(self, detail: str) -> None:
        """Record and log one failed report, at WARNING — visible, never fatal."""
        self.failures.append(detail)
        _LOG.warning(
            "run %s: agent-run reporting failed (the run is unaffected): %s",
            self._run_id,
            detail,
        )


def _error_text(result: Any) -> str:
    """The text blocks of an errored tool result, for the log line."""
    blocks = getattr(result, "content", None) or []
    parts = [
        str(block["text"])
        for block in blocks
        if isinstance(block, dict) and isinstance(block.get("text"), str)
    ]
    return " ".join(parts)[:200] or "<no content>"


class ReportingCheckpointer:
    """The loop's checkpoint seam with a run report hung off it.

    A ``Checkpointer`` decorator rather than a new hook argument on ``run_to_completion``:
    the loop already writes here on entry and after every transition, so this is the one
    seam that knows a run moved, and telemetry needed **no change to ``agent/loop.py`` at
    all**. ``load`` delegates untouched — a resumed run is read from the real store.

    It implements the ``Checkpointer`` Protocol and nothing more. In particular it does not
    forward ``InMemoryCheckpointer.history``, which is not part of that Protocol: the
    runner keeps its own handle on the store it wrapped and reads the trajectory from
    there, so a forwarder here would be a method with no caller.
    """

    def __init__(self, inner: Checkpointer, reporter: RunReporter) -> None:
        self._inner = inner
        self._reporter = reporter

    def load(self, incident_id: UUID) -> RunState | None:
        return self._inner.load(incident_id)

    def write(self, run_state: RunState) -> None:
        """Checkpoint first, report second. The order IS the fail-open guarantee.

        Reversing these two lines would let a slow or refused report stand between a
        transition and its durable record, which is the one thing a checkpointer may never
        do — a crash in that window loses the run's own history to telemetry.
        """
        self._inner.write(run_state)
        self._reporter.report(run_state)


def summarize(reporter: RunReporter | None) -> str:
    """One line about what reporting did, for the runner's own output.

    Says the run id an operator needs to find the record, and names failures rather than
    leaving an empty console unexplained.
    """
    if reporter is None:
        return "agent-run reporting: off"
    parts = [
        f"agent-run reporting: run {reporter.run_id}",
        f"{reporter.reports_sent} report(s) accepted",
        f"briefing {'sent' if reporter.briefing_sent else 'NOT sent'}",
    ]
    if reporter.failures:
        parts.append(f"{len(reporter.failures)} FAILED (first: {reporter.failures[0]})")
    return ", ".join(parts)
