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


#: Who may be credited with a recovery, and what a run says when nobody may (owner decision
#: O-29, 2026-09-19; ADR 0071, amending ADR 0062 and building on ADR 0009's re-probe). ONE
#: sentence for the planner that decides to act, the fix table that picks the action and the
#: judge that grades the report, because the failure it prevents is exactly INC-002's shape:
#: a run told to refuse credit and a judge that marks the refusal down. It names the
#: DISCRIMINATOR (the pair of readings, before and after, never how clean the action's own
#: response looked), both arms, and both sentences VERBATIM — ``agent/attribution.py`` holds
#: the two strings and a test pins that these are them.
ATTRIBUTION_RULE: Final[str] = (
    "A recovery belongs to your action only when the last reading you took of that resource "
    "BEFORE acting showed the fault present and your reading after it shows the fault gone, "
    "because a reading says what a resource is and never who changed it: so re-read the "
    "resource immediately before you act, and when that reading already shows the fault gone, "
    "do not act at all — take no Tier-1 action, report `the issue cleared on its own before I "
    "could act`, and escalate, because the cause is unknown and may recur; when an action has "
    "already been taken with no fault-present reading immediately behind it and the resource "
    "now reads healthy, the honest report is `recovered, but I cannot confirm my action caused "
    "it` and the run escalates on that too; and a run whose readings do carry the pair may say "
    "plainly that its action fixed it."
)


#: How MANY times a "re-read before X" rule asks to be satisfied (INC-004, 2026-09-20; ADR
#: 0073, amended by ADR 0074). Held here for a reason this module has not had before: the two
#: readers are two RULES in the same file rather than two prompts. `investigation_planner.md`
#: states the freshness re-read (ADR 0009) and the attribution re-read (ADR 0071) as separate
#: bullets, a model applied both as "take one more confirming read" on every step, and a clause
#: appended to one of them would leave the other saying what it said. One sentence written
#: once, rendered into both, so neither can drift and a later edit cannot reach only half the
#: demand. The STRUCTURAL half is `investigation._probe_withdrawn`, which takes `probe` out of
#: the schema for that step; this sentence is what makes the narrowing predictable rather than
#: a surprise.
#:
#: ADR 0074 rewrote the tail. ADR 0073's version promised that "a probe of some OTHER tool
#: stays open", which was true of that guard and was exactly what the third live take did with
#: it: refused a third reading of the consumer group, then read the DLQ, then the circuit
#: breakers, then asked for the group a fourth time. A prompt may not promise a move the schema
#: withdraws, so the sentence now says what the narrowed step really offers.
CONFIRMING_READ_BOUND_RULE: Final[str] = (
    "One fresh reading that shows the fault is the whole demand of this rule — a second is "
    "not more evidence, it is the same evidence and one step you cannot get back — so once "
    "your top hypothesis has held at or above the remediate threshold in a category with a "
    "Tier-1 fix for two steps running and your own newest reading of the alerted resource is "
    "fresh and shows the fault present, the state machine takes `probe` out of the schema for "
    "your next step entirely, names that refusal on the evidence trail, and leaves you "
    "`remediate` and `stop`: act on the answer you have already ranked, or hand off with a "
    "reason naming what you would need to SEE to act, because a reading that would not change "
    "a ranking that settled is a step spent to arrive where you already are."
)


#: Every shared rule, by the key a prompt file names it with.
SHARED_RULES: Final[dict[str, str]] = {
    "chain_node_action": CHAIN_NODE_ACTION_RULE,
    "stuck_chain_root": STUCK_CHAIN_ROOT_RULE,
    "unresolved_remainder": UNRESOLVED_REMAINDER_RULE,
    "attribution": ATTRIBUTION_RULE,
    "confirming_read_bound": CONFIRMING_READ_BOUND_RULE,
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
