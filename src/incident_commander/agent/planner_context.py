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

#: How an evidence line begins when the ids are shown to the model. Named once, so the prompt
#: and the tests that read it back cannot disagree.
EVIDENCE_ID_PREFIX: Final[str] = "evidence_id="

#: The ledger row name for a remediation attempt that did not end the incident. Named in this
#: low-level module because several modules read it, and a second spelling would stop matching.
ATTEMPT_FAILED_MARKER: Final[str] = "_remediation_attempt_failed"

#: The ledger row name for a plan that passed every guard, carrying the cause it aims at. Read
#: together with the row above to tell a cause the run acted on from one it merely named.
PLAN_MARKER: Final[str] = "_planner_plan"

#: The ledger row name for one verification verdict, whose summary reads "<verdict>: <reasoning>"
#: and whose arguments say which poll of how many it was. Three modules read it, so one spelling.
VERIFY_JUDGE_MARKER: Final[str] = "_verify_judge"

#: The heading above that block, in the exact words the model is shown.
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
            # Records of failed attempts are left out here and printed in full at the end,
            # where nothing truncates them.
            if entry.tool_name == ATTEMPT_FAILED_MARKER:
                continue
            # The id comes first on the line, so a model looking for something to cite finds
            # it in the same place every time.
            cited = f"{EVIDENCE_ID_PREFIX}{entry.evidence_id} " if show_evidence_ids else ""
            lines.append(f"  - {cited}[{entry.tool_name}] {entry.result_summary}")
    else:
        lines.append("Evidence so far: (none)")
    lines.append("")
    # Only read tools are listed: the investigation planner proposes a remediation for the
    # loop to gate, and never calls an action tool itself.
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
