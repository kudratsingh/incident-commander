"""Reading rules several prompts must state in the same words, held once.

A rule about reading evidence belongs to every reader of it — planner, routing table, judge —
in the same change; INC-002 is what half a rule cost. Each prompt file writes ``{{rule:<key>}}``,
which ``loader.load_prompt`` renders while serving, so the pinned snapshot hashes move with it.
Python, not a ``prompts/*.md``, so ``investigation.py`` can import it: ADR 0054's exception.
"""

from __future__ import annotations

import re
from typing import Final

#: What a prompt file writes where a shared rule belongs; unlike
#: earnest prompt text, for ``test_prompts_snapshot.py``'s sweep.
PLACEHOLDER: Final[re.Pattern[str]] = re.compile(r"\{\{rule:([a-z0-9_]+)\}\}")


class UnknownSharedRuleError(RuntimeError):
    """A prompt file asked for a shared rule this module does not hold.

    Raised while serving: a ``{{rule:typo}}`` reaching a model is a hole
    in the prompt.
    """


#: The conditional routing for a stuck dependency chain (owner decision O-19,
#: 2026-09-17; ADR 0054, with ADR 0034 for error-outranks-hint). ONE sentence,
#: because what is prevented is three paraphrases. It names the DISCRIMINATOR
#: (the root's own dead-letter row, never ``get_dag_state``'s chain view), the
#: two ARMS with their tools, that precedence, and what the fence does NOT do.
STUCK_CHAIN_ROOT_RULE: Final[str] = (
    "A stuck dependency chain is routed by its dead-lettered root's own "
    "dead-letter row and never by the chain view: when that row reads "
    "permanent — the hint is `human_required`, or the `error_message` names "
    "bad data or a schema its producer must fix, which outranks a replay-safe "
    "label — the root is fenced with `mark_dlq_permanent` and the run "
    "escalates with the chain still stuck, because a fence drains nothing; "
    "when the row reads replay-safe, the root is replayed immediately by its "
    "own id with `replay_dlq_by_ids`, and the resolver then promotes the "
    "descendants that were waiting."
)


#: How the briefing's structured remainder is read, by the writer that must name it and the
#: judge that grades whether it did (WP-11.3, ADR 0065). ONE sentence for both, because the
#: failure it prevents is the judge marking down the honesty the writer is required to show
#: (INC-002). It quotes the block's own heading, which ``agent/briefing.py`` renders.
UNRESOLVED_REMAINDER_RULE: Final[str] = (
    "The run context carries a structured remainder — the block headed `Remaining (not "
    "addressed by this run):` — which is computed from the run's own ranking and its own "
    "attempts rather than written by anyone, so every cause listed there is still open and "
    "is grounded by the block alone: `findings` must name each of them as remaining, none of "
    "them may be called cleared, addressed, fixed or resolved however well a verified action "
    "worked, and when the block is absent this run left no such remainder and none may be "
    "invented."
)


#: WHICH node of a chain an action may name (WO-R3-284, ADR 0070, amending ADR 0032).
#: The other half of ADR 0070: the guard now admits a node of the alerted chain, and a
#: guard that admits what no prompt asks for is half a rule — INC-002's failure, and the
#: reason ADR 0053 § 4 dropped a world rather than ship one side of it. ONE sentence,
#: because what is prevented is three paraphrases. It names the DISCRIMINATOR (the alerted
#: job's own chain reading), the two admissible arms, and the three shapes that are never
#: targets.
CHAIN_NODE_ACTION_RULE: Final[str] = (
    "An action about a dependency chain names a node the alerted job's own "
    "`get_dag_state` reading names — the alerted job itself, or, when that job "
    "completed and the chain's one dead-letter row belongs to a descendant in "
    "that same reading, that descendant — because the reading is what makes a "
    "node part of the incident you were paged for, so an id no reading of the "
    "alerted chain carries, a node of a different chain, and a dead-letter row "
    "sitting in the queue that the chain view does not name are each outside "
    "this incident and are never targets."
)


#: Every shared rule, by the key a prompt file names it with.
SHARED_RULES: Final[dict[str, str]] = {
    "chain_node_action": CHAIN_NODE_ACTION_RULE,
    "stuck_chain_root": STUCK_CHAIN_ROOT_RULE,
    "unresolved_remainder": UNRESOLVED_REMAINDER_RULE,
}


def render(text: str) -> str:
    """Expand every ``{{rule:<key>}}`` in ``text``. Unknown keys raise."""

    def _substitute(match: re.Match[str]) -> str:
        key = match.group(1)
        rule = SHARED_RULES.get(key)
        if rule is None:
            raise UnknownSharedRuleError(
                f"prompt asks for shared rule {key!r}, which is not in "
                f"SHARED_RULES (have: {sorted(SHARED_RULES)}). Add the rule to "
                f"llm/prompts/shared_rules.py or fix the placeholder — a rule "
                f"the loader cannot expand would reach the model as literal "
                f"'{match.group(0)}'."
            )
        return rule

    return PLACEHOLDER.sub(_substitute, text)
