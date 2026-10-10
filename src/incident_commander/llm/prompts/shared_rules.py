"""Reading rules several prompts must state in the same words, held once (INC-002).

Each file writes ``{{rule:<key>}}``, expanded by ``loader.load_prompt`` while serving, so the
pinned snapshot hashes move with a rule. Python, so ``investigation.py`` can import it (ADR 0054).
"""

from __future__ import annotations

import re
from typing import Final

#: What a prompt file writes where a shared rule belongs: ``{{rule:<key>}}``, lower-case key.
#: ``test_prompts_snapshot.py`` sweeps every prompt for these and checks each key is known here.
PLACEHOLDER: Final[re.Pattern[str]] = re.compile(r"\{\{rule:([a-z0-9_]+)\}\}")


class UnknownSharedRuleError(RuntimeError):
    """A prompt file asked for a shared rule this module does not hold.

    Raised while serving: a ``{{rule:typo}}`` reaching a model is a hole in the prompt.
    """


#: What to do about a dependency chain that stopped: fence the dead-lettered root out of replay,
#: or replay it. The root's OWN dead-letter row decides, never the chain view (ADR 0034, ADR 0054).
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


#: What holds a stuck chain whose own rows are healthy, and how each reading of it is acted on:
#: the resolver's `polling` verdict is read first and decides, a paused backstop loop never
#: explains a stalled resolver, the resolver is restarted, the paused sweep is named and left to
#: expire, and the coordinator is only what remains once both are ruled out (INC-008 and its
#: addendum, O-49, O-51, ADR 0080, ADR 0081). Same words to every reader.
STALLED_CHAIN_RULE: Final[str] = (
    "A stuck dependency chain whose own nodes are healthy — nothing in it dead-lettered, "
    "the chain not paused, children `waiting` behind parents that `completed` — is held "
    "by what promotes it, so read `polling` on "
    '`get_consumer_lag(consumer_group="dependency-resolver")` first: `polling: false` '
    "means the resolver has stopped, whatever its `lag`, `lag_known`, `source` and "
    "`age_seconds` say (a dead resolver reads lag 0, known and fresh, exactly like a "
    "healthy idle one, and `last_poll_age_seconds` is only the number behind the "
    "verdict), which is `resolver_stall`, fixed with `restart_consumer_group` on "
    "`dependency-resolver` and verified by a reading taken after the restart that says "
    "`polling: true`; `get_control_loops` reading the `resume_unblocked_waiting` loop as "
    "`paused: true` means the sweep that backstops the resolver is held too, and a paused "
    "backstop never explains a stalled primary consumer — it is not the cause while the "
    "resolver reads `polling: false`, and it is not `dag_paused`, which is the chain's own "
    "`get_dag_state` `paused: true` — so the resolver is still restarted, and the held "
    "sweep, which no tool lifts, is named in the report with its "
    "`paused_expires_in_seconds` and left to expire; and only when the resolver reads "
    "`polling: true` and that sweep is running is `saga_coordinator_stall` what remains, "
    "a label no reading confirms."
)


#: How to read the list of causes a run did NOT address: everything in it is still open, and no
#: run may call any of them fixed. The briefing writer and the judge get the same words (ADR 0065).
UNRESOLVED_REMAINDER_RULE: Final[str] = (
    "The run context carries a structured remainder — the block headed `Remaining (not "
    "addressed by this run):` — which is computed from the run's own ranking and its own "
    "attempts rather than written by anyone, so every cause listed there is still open and "
    "is grounded by the block alone: `findings` must name each of them as remaining, none of "
    "them may be called cleared, addressed, fixed or resolved however well a verified action "
    "worked, and when the block is absent this run left no such remainder and none may be "
    "invented."
)


#: WHICH job of a dependency chain an action may name: only one the alerted job's own
#: ``get_dag_state`` reading lists (ADR 0070, amending ADR 0032). Code enforces it too.
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


#: When a run may say its own action fixed the incident: only with a reading that showed the fault
#: just before acting and one that shows it gone after (ADR 0071). ``agent/attribution.py`` agrees.
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


#: How many times the "re-read before you act" rules have to be satisfied: once. A second reading
#: of the same resource is the same evidence, so the machine then withdraws ``probe`` (ADR 0073).
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
    "stalled_chain": STALLED_CHAIN_RULE,
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
