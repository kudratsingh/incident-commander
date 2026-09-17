"""What an investigation planner is shown — one renderer, every strategy.

Moved out of ``agent/investigation.py`` by WP-5.2, unchanged in what it
produces for the control group, and the move is the point rather than a
tidy-up.

``tests/unit/test_strategies.py::TestStrategiesHoldNoExecutionPolicy::
test_the_only_thing_taken_from_the_loop_is_the_planner_call`` allows a
strategy module to import exactly one name from ``investigation.py``:
``_plan_next_step``. That guard is what keeps the ``FIX_MAP`` gate, the 0.7
threshold, the subject-probe refusal and the tier re-check in the loop where
every strategy is subject to them. A strategy that renders its own planner
context — which every strategy past ``baseline`` must, because its output
schema is not ``InvestigationStep`` — would either have to import a second
name from the loop (a second seam) or copy the rendering (two contexts that
drift, which makes a strategy comparison meaningless).

So the rendering lives here: shared instrumentation, imported by the loop and
by every strategy, holding no execution policy at all. Plan 02 § 17 and
``docs/eval-methodology.md`` § "Context handling is instrumentation, held
constant across strategies" are the rule this module now implements structurally
— there is one function, and ``planner_context_chars`` on every ``StepRecord``
measures the string it returned.

The tool block is read-tier only (``tools_at_or_below(Tier.READ)``), which is
the property that makes importing this from a strategy safe: the most a
strategy can learn from it is which read probes exist, which is what the
planner has always been shown. ``tests/unit/test_planner_context.py`` asserts
that directly, so the guard is on the rendering rather than on anyone's
intentions.

Evidence ids (WP-5.2)
---------------------

``show_evidence_ids`` is the one behavioural knob, and it defaults to **off**.

WP-5.1 shipped ``EvidenceRef`` (ADR 0042): a candidate's citation is an
``evidence_id`` from the run's ledger, and a ref that names no entry fails
validation. It left one thing unfinished, recorded in its own module docstring
— this renderer showed ``- [tool_name] result_summary`` and **no id at all**,
so a planner asked to cite one had never seen one. A best-of-N candidate set
cannot be grounded until the ids are on the page.

They are rendered for the strategies that ask for them and for no one else,
and the reason is that the alternative could not be measured. Showing ids to
``baseline`` changes the bytes of the prompt the campaign's live runs were made
with. On the canned suite that is provably inert — the fake client replays a
scripted payload and never reads the prompt — but the canned suite is therefore
also unable to detect the change, and the only instrument that could is a paid
live run, which this packet is not permitted to make (owner instruction O-22).
A control group whose prompt moved by an amount nobody measured is no longer
the control the eight green live runs were made with, so ``baseline`` keeps its
exact bytes and the id column is part of what the best-of-N arm *is*.

That is a confound and it is named rather than hidden: the best-of-N arm's
context differs from ``baseline``'s by one ``evidence_id=<uuid> `` prefix per
evidence line. ADR 0044 records the decision, what it costs, and the one
experiment that would settle it. Every arm's ``strategy_config`` stamps
``evidence_ids_rendered`` so no artifact can report a comparison without
saying which side of it saw ids.
"""

from __future__ import annotations

import json
from typing import Final

from incident_commander.agent.state import RunState
from incident_commander.tools.policies import Tier, tools_at_or_below
from incident_commander.tools.registry import TOOL_REGISTRY, description_of

#: How one evidence line opens when ids are rendered. Named so the prompt
#: addendum that tells the model to cite them, and the test that asserts they
#: are there, both spell it the same way.
EVIDENCE_ID_PREFIX: Final[str] = "evidence_id="


def format_planner_context(run_state: RunState, *, show_evidence_ids: bool = False) -> str:
    """What the investigation planner is shown.

    The alert, the budget left, the evidence gathered so far, and the
    read-only probes it may pick from.

    ``show_evidence_ids`` puts each evidence entry's ``evidence_id`` in front
    of its line, for a strategy whose output schema cites them (ADR 0042,
    ADR 0044). Default off, so the string handed to ``baseline`` is byte-for-byte
    the one it has always been handed.
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
            # The id goes first, before the tool name, so a model scanning for
            # something to cite finds it at a fixed offset on every line rather
            # than after a summary of unbounded length.
            cited = f"{EVIDENCE_ID_PREFIX}{entry.evidence_id} " if show_evidence_ids else ""
            lines.append(f"  - {cited}[{entry.tool_name}] {entry.result_summary}")
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
