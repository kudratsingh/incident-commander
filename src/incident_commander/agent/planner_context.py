"""What an investigation planner is shown — one renderer, every strategy.

Moved out of ``agent/investigation.py`` by WP-5.2 so the loop and every strategy share one
rendering and no execution policy lives here. Read-tier tools only, pinned by
``tests/unit/test_planner_context.py``. ``show_evidence_ids`` defaults off, keeping
``baseline``'s prompt byte-identical; the best-of-N arm's id column is ADR 0042 / ADR 0044.
"""

from __future__ import annotations

import json
from typing import Final

from incident_commander.agent.state import RunState
from incident_commander.tools.policies import Tier, tools_at_or_below
from incident_commander.tools.registry import TOOL_REGISTRY, description_of

#: How an evidence line opens when ids are rendered. Named so prompt and test agree.
EVIDENCE_ID_PREFIX: Final[str] = "evidence_id="


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
            # The id goes first, so a model scanning for something to cite finds it
            # at a fixed offset on every line.
            cited = f"{EVIDENCE_ID_PREFIX}{entry.evidence_id} " if show_evidence_ids else ""
            lines.append(f"  - {cited}[{entry.tool_name}] {entry.result_summary}")
    else:
        lines.append("Evidence so far: (none)")
    lines.append("")
    # Read tools only (Tier.READ) — the planner emits RemediateAction rather than acting.
    lines.append(format_tool_block())
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
