"""Reading rules several prompts must state in the same words, held once.

A rule about how a piece of evidence may be read belongs to every reader of
that evidence, given in the same change — the planner that acts on it, the
routing table that encodes it, and the judge that grades what was written
about it. That sentence is INC-002's prevention clause, and INC-002 is what
half a rule costs: cmd #218 told the briefing WRITER that a filtered read
proves only its own slice, the judge was never told, and the judge then made
the overclaim the writer had just been forbidden to make and scored an honest
briefing 0.0.

The obvious way to give three readers one rule is to type it into three
markdown files, and that is the shape this module exists to refuse. Three
copies drift one edit at a time, and nothing in a diff shows that the copy in
the judge's rubric no longer says what the planner's does — which is exactly
how ``FIX_MAP``'s value and the remediation prompt disagreed for the whole
life of PR #173 (``agent/investigation.py``'s own comment records it).

So a shared rule is written here, once, and each prompt file writes
``{{rule:<key>}}`` where it belongs. ``loader.load_prompt`` renders the
placeholder as it serves the file, so every caller — the investigation loop,
the remediation planner, the best-of-N strategies, and the eval harness's
briefing judge — receives the identical sentence without any of them knowing
this module exists. The snapshot suite hashes prompts *as the loader serves
them*, so a rule edit moves every hash it reaches and a reviewer sees the
blast radius in the diff.

**Why the text is in Python rather than in a markdown file of its own.**
CLAUDE.md's rule is that prompts live in versioned ``prompts/*.md`` files with
snapshot tests, and this module does not weaken it: whole prompts still live
there, and this holds fragments that several of them share. The routing code
has to be able to name the rule it encodes (``investigation.py`` imports
``STUCK_CHAIN_ROOT_RULE`` beside ``HINT_ROUTED_CATEGORIES``) without reading a
file, and ``available_prompts()`` enumerates ``*.md`` — a fragment file there
would present as a loadable prompt that no role ever loads. Recorded as a
narrow, named exception in ADR 0054.
"""

from __future__ import annotations

import re
from typing import Final

#: What a prompt file writes where a shared rule belongs. Deliberately
#: unlike anything a prompt says in earnest, so the sweep in
#: ``test_prompts_snapshot.py`` that fails on an *unrendered* placeholder in a
#: served prompt cannot be satisfied by a coincidence.
PLACEHOLDER: Final[re.Pattern[str]] = re.compile(r"\{\{rule:([a-z0-9_]+)\}\}")


class UnknownSharedRuleError(RuntimeError):
    """A prompt file asked for a shared rule this module does not hold.

    Raised while the prompt is being served, not at import, because that is
    when the request is made — and raised rather than left in place, because a
    literal ``{{rule:typo}}`` reaching a model is a prompt with a hole in it
    where a load-bearing rule should be.
    """


#: The conditional routing for a stuck dependency chain (owner decision O-19,
#: 2026-09-17; ADR 0054). ONE sentence, because the thing being prevented is
#: three paraphrases, and a paraphrase is what a second sentence becomes.
#:
#: It names four things its three readers each need: the DISCRIMINATOR (the
#: root's own dead-letter row, never the chain view — ``get_dag_state`` carries
#: no hint and no error text, so ``"status": "dead_letter"`` says the root
#: stopped the chain and nothing about whether restarting it is safe), the two
#: ARMS with the tool each routes to, the PRECEDENCE when a row's hint and its
#: error disagree (ADR 0034 — the error wins, one way only), and what the fence
#: does NOT do. The last clause is the judge's half: a briefing that reports
#: the chain still stuck after a fence is grounded, and a judge that has not
#: been told a fence drains nothing reads that honesty as a contradiction.
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
