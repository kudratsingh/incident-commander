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


#: Every shared rule, by the key a prompt file names it with.
SHARED_RULES: Final[dict[str, str]] = {
    "stuck_chain_root": STUCK_CHAIN_ROOT_RULE,
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
