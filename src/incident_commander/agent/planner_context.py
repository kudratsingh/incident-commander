"""What an investigation planner is shown — one renderer, every strategy.

Moved out of ``agent/investigation.py`` by WP-5.2 so the loop and every strategy share one
rendering and no execution policy lives here. Read-tier tools only, pinned by
``tests/unit/test_planner_context.py``. ``show_evidence_ids`` defaults off, keeping
``baseline``'s prompt byte-identical; the best-of-N arm's id column is ADR 0042 / ADR 0044.
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

#: The ledger entry ``agent/remediation.py`` writes for an attempt that did not end the
#: incident (ADR 0056). Named HERE, in the lower module, because both planner contexts —
#: this one and the remediation planner's — must pull it out of the evidence dump and
#: render it whole, and a second spelling of the name is how one of them stops matching.
ATTEMPT_FAILED_MARKER: Final[str] = "_remediation_attempt_failed"

#: The ledger entry ``agent/remediation.py`` writes when a plan clears its guards, carrying the
#: cause that plan targets. Named here for the same reason as the marker above: ``agent/
#: incidents.py`` reads both to tell a cause this run acted on from one it only named.
PLAN_MARKER: Final[str] = "_planner_plan"

#: How that block is headed, in the words the model reads.
ALREADY_ATTEMPTED_HEADING: Final[str] = "Already attempted in this incident — do NOT repeat:"


def render_already_attempted(evidence: Sequence[EvidenceEntry]) -> str:
    """The "Already attempted" block, or ``""`` when nothing has been attempted.

    Rendered LAST and never truncated, for the same reason a plan refusal is: it is an
    instruction about this run's own earlier decision, and a cut sentence is worse than
    none. Empty for every run that has made no failed attempt, which is why adding this
    left every pre-ADR-0056 prompt byte-identical.
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
            # Attempt records are pulled out and rendered whole at the end, like the
            # remediation planner's refusal block.
            if entry.tool_name == ATTEMPT_FAILED_MARKER:
                continue
            # The id goes first, so a model scanning for something to cite finds it
            # at a fixed offset on every line.
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
