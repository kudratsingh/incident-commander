"""What an investigation planner is shown — one renderer for every strategy, no execution policy.

Read-tier tools only, pinned by ``tests/unit/test_planner_context.py``. ``show_evidence_ids``
defaults off so ``baseline``'s prompt bytes hold (ADR 0042 / ADR 0044).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Final

from incident_commander.agent.state import EvidenceEntry, RunState
from incident_commander.tools.policies import Tier, tools_at_or_below
from incident_commander.tools.registry import TOOL_REGISTRY, description_of

#: How an evidence line opens when ids are rendered. Named so prompt and test agree.
EVIDENCE_ID_PREFIX: Final[str] = "evidence_id="

#: Ledger entry for an attempt that did not end the incident (ADR 0056). Named here, in the
#: lower module, because a second spelling is how one of its readers stops matching.
ATTEMPT_FAILED_MARKER: Final[str] = "_remediation_attempt_failed"

#: Ledger entry for a plan that cleared its guards, carrying the cause it targets. Read with
#: the marker above to tell a cause this run acted on from one it only named.
PLAN_MARKER: Final[str] = "_planner_plan"

#: Ledger entry for one verify poll's verdict, ``"<verdict>: <reasoning>"`` with
#: ``{attempt, of}`` on its arguments. Three readers, so one spelling.
VERIFY_JUDGE_MARKER: Final[str] = "_verify_judge"

#: How that block is headed, in the words the model reads.
ALREADY_ATTEMPTED_HEADING: Final[str] = "Already attempted in this incident — do NOT repeat:"


def render_already_attempted(evidence: Sequence[EvidenceEntry]) -> str:
    """The "Already attempted" block, or ``""`` when nothing has been attempted.

    Rendered last and never truncated: it is an instruction about this run's own earlier
    decision, and a cut sentence is worse than none.
    """
    attempts = [entry for entry in evidence if entry.tool_name == ATTEMPT_FAILED_MARKER]
    if not attempts:
        return ""
    lines = [ALREADY_ATTEMPTED_HEADING, *(f"  - {entry.result_summary}" for entry in attempts)]
    return "\n".join(lines)


def format_planner_context(run_state: RunState, *, show_evidence_ids: bool = False) -> str:
    """The alert, the budget left, the evidence so far, and the read-only probes.

    ``show_evidence_ids`` prefixes each line with its ``evidence_id`` (ADR 0042/0044),
    off by default so ``baseline``'s prompt bytes hold.
    """
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
            # Attempt records are pulled out and rendered whole at the end.
            if entry.tool_name == ATTEMPT_FAILED_MARKER:
                continue
            # Id first, so a model scanning for something to cite finds it at a fixed offset.
            cited = f"{EVIDENCE_ID_PREFIX}{entry.evidence_id} " if show_evidence_ids else ""
            lines.append(f"  - {cited}[{entry.tool_name}] {entry.result_summary}")
    else:
        lines.append("Evidence so far: (none)")
    lines.append("")
    # Read tools only (Tier.READ) — the planner emits RemediateAction rather than acting.
    lines.append(format_tool_block())
    attempted = render_already_attempted(run_state.evidence)
    if attempted:
        lines.extend(("", attempted))
    return "\n".join(lines)


def format_tool_block() -> str:
    """The read-only probe listing, exactly as the planner is shown it.

    Depends on ``TOOL_REGISTRY`` and the pinned platform image, never on the run, so
    ``tests/unit/test_planner_context.py`` pins it by hash like the authored prompts.
    """
    lines = ["Available tools (read-only probes):"]
    for name in sorted(tools_at_or_below(Tier.READ)):
        spec = TOOL_REGISTRY[name]
        schema = spec.input_model.model_json_schema()
        lines.append(f"  - {name}: {_indented_description(name)}")
        lines.append(f"    input_schema={json.dumps(schema, sort_keys=True)}")
    return "\n".join(lines)


def _indented_description(tool_name: str) -> str:
    """Platform-authored tool description, indented for the context block.

    Verbatim from the contract snapshot (``registry.description_of``) — load-bearing.
    """
    text = description_of(tool_name)
    if not text:
        return "(no description)"
    return text.replace("\n", "\n    ")
