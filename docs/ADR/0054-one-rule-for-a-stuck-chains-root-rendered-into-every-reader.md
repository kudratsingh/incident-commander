# ADR 0054: A stuck chain's root is routed by its own row — one rule, rendered into every reader

* Status: accepted
* Date: 2026-09-17
* Decider: Kudrat Singh (owner decision O-19, WO-R3-263)
* Related: [ADR 0027](0027-read-the-row-before-you-replay-it.md) (read the row first),
  [ADR 0026](0026-a-stabilizer-is-not-a-resolution.md) (why the run escalates after a fence),
  [ADR 0031](0031-an-alerted-dlq-category-is-the-incident.md) (the alert names its subject),
  [ADR 0034](0034-when-a-rows-hint-and-its-error-disagree-the-error-wins.md) (the precedence this
  rule carries), [ADR 0040](0040-a-ground-truth-is-a-statement-about-one-world.md) (a label is
  about one world), `../../../context/INCIDENTS.md` INC-002 (why every reader gets the rule)

## Context and problem statement

WO-R3-261 wrote a root-cause label for all 41 scenarios by reading each world before opening its
canned planner script. Two things the corpus could not say came out of that exercise, and both are
statements about what the live agent is told rather than about the harness.

**Gap 1 — a world with no label.** `trace_investigation`'s trace holds one failed `report_gen` job
whose error reads "OOM during PDF generation (200MB report)", `retry_count` 2. The fault is known
exactly. `HypothesisCategory` had no member for a worker running out of memory, so the honest label
was `unknown` — and `unknown` does not mean "a cause with no box"; the planner prompt defines it as
"can't classify with confidence" and the enum's own docstring as "the LLM couldn't classify the root
cause into any known category". A human handed that label goes looking for evidence that is already
sitting in the trace.

**Gap 2 — three statements about one incident, two of them true.** For a stuck dependency chain:

* `FIX_MAP[RUNAWAY_SAGA]` was `replay_dlq_by_ids`, unconditionally;
* both planner prompts routed a `human_required` chain root to `mark_dlq_permanent` (WO-R2-160, the
  user's reversal of the old chain-root exception);
* `saga_stuck` forbids `replay_dlq_by_ids`, requires the fence, and expects `escalated`.

Nothing held them together. `TestFixMapMatchesTheSuite` — the check written *for* this class of
drift after PR #173 — was scoped to scenarios expecting `resolved`, on the sound reasoning that an
escalate-only scenario forbids every Tier-1 tool on purpose. So the one scenario in the corpus that
proves the disagreement was the one scenario the check could not see.

## Decision

### 1. `resource_exhaustion` joins the taxonomy, escalate-only

A worker or job that ran out of memory, CPU or disk. Appended to `HypothesisCategory` so every value
already written into a run archive, a trajectory or a `ground_truth.root_causes` keeps its spelling
and its position. Outside `FIX_MAP` and not as a staging post: nothing on this platform's Tier-1
surface raises a memory limit, resizes a worker or reclaims a disk, so there is no fix for it to be
promoted to. `trace_investigation`'s ground truth moves from `unknown` to `resource_exhaustion` and
its canned planner's concluding step moves with it (fixture-authoritative: the script says what the
world says). Its FIRST step keeps `unknown`, which is true — nothing has been read yet.

### 2. A stuck chain's root is routed by that root's own dead-letter row

Not by the chain view, and not by the category. `RUNAWAY_SAGA` joins `HINT_ROUTED_CATEGORIES`, where
`POISON_MESSAGE` already sat: `FIX_MAP`'s value names the common case, and the specific tool comes
from `HINT_ROUTED_TOOLS` keyed on the row's `remediation_hint`. The routing itself did not have to
move — `human_required` already routed to the fence and `replay_safe` to a by-id replay, which is
what the saga pair grades. What moved is the admission that a chain root is a dead-letter row like
any other.

### 3. The rule is ONE sentence, held in ONE place, and rendered into every reader

This is the condition the owner attached to Gap 2, and it is INC-002's prevention clause: *any rule
about how a piece of evidence may be read is given to every reader of that evidence, in the same
change.* INC-002 is what half a rule cost — cmd #218 told the briefing writer that a filtered read
proves only its own slice, the judge was not told, and the judge then made the overclaim the writer
had just been forbidden and scored an honest briefing 0.0.

The sentence lives in `src/incident_commander/llm/prompts/shared_rules.py` as
`STUCK_CHAIN_ROOT_RULE`:

> A stuck dependency chain is routed by its dead-lettered root's own dead-letter row and never by
> the chain view: when that row reads permanent — the hint is `human_required`, or the
> `error_message` names bad data or a schema its producer must fix, which outranks a replay-safe
> label — the root is fenced with `mark_dlq_permanent` and the run escalates with the chain still
> stuck, because a fence drains nothing; when the row reads replay-safe, the root is replayed
> immediately by its own id with `replay_dlq_by_ids`, and the resolver then promotes the descendants
> that were waiting.

Three prompt files write `{{rule:stuck_chain_root}}` where it belongs, and `load_prompt` expands the
placeholder as it serves the file:

| Reader | Where | What it needs the rule for |
|---|---|---|
| the planner | `investigation_planner.md`, Rules | both arms are `remediate`; `stop` leaves the only available action untaken |
| the fix table | `remediation_planner.md`, "Stuck dependency chains" | which of the two tools this root gets, and how to verify it |
| the judge | `briefing_judge.md`, groundedness | a briefing that says the chain is still stuck after a fence is GROUNDED |

Because the expansion happens in the loader, no caller opts in and none can opt out: the
investigation loop, the remediation planner, both best-of-N strategies and `evals/graders/llm_judge.py`
each keep the single `load_prompt` call they already had. **No change was needed in
`evals/graders/llm_judge.py` at all** — the judge's rubric reaches it through the same loader.

### 4. `TestFixMapMatchesTheSuite` is scoped to every forbidden set that is a decision

`resolved`, or `escalated` while still requiring an action. Derived from the expectation, never
declared, so a scenario cannot opt in or out. Escalate-only scenarios with no expected action stay
excluded for the original reason, which is still right.

## Why the shared sentence is Python and not a `prompts/*.md` file

CLAUDE.md's rule is that prompts live in versioned `prompts/*.md` files with snapshot tests, and
this does not weaken it: whole prompts still live there, snapshot-hashed, and `shared_rules.py`
holds only fragments that several of them share. Two reasons it is not a markdown file:
`available_prompts()` enumerates `*.md`, so a fragment there would present as a loadable prompt that
no role ever loads; and the routing code has to be able to name the rule it encodes
(`investigation.stuck_chain_root_rule()` sits beside `HINT_ROUTED_CATEGORIES`) without reading a
file. The snapshot suite hashes prompts **as the loader serves them**, so editing the one sentence
moves all three hashes at once and a reviewer sees the whole blast radius in the diff — which is the
property the indirection exists for.

## Rejected alternatives

* **Three hand-typed copies.** Passes an identity check on the day it is typed. It is what
  `FIX_MAP`'s value and the remediation prompt did for the whole life of PR #173, and what INC-002
  did to a briefing. `test_every_reader_delegates_the_rule_rather_than_copying_it` fails on a copy
  even when the copy is correct.
* **Enforcing the rule at plan time from the error text.** Rejected in ADR 0034 and still rejected:
  it derives a control from tool output (CLAUDE.md invariant 4) and fails open on any wording outside
  its vocabulary. Enforcement stays exact grading plus the free `make world-dossier` lint.
* **Promoting `resource_exhaustion` into `FIX_MAP` later.** There is no Tier-1 tool to promote it to.
* **Making `FIX_MAP[RUNAWAY_SAGA]` conditional.** One map value cannot be two answers; that is what
  `HINT_ROUTED_CATEGORIES` exists to express.

## Consequences

* Three prompt hashes move by construction, and a taxonomy row and a `stop`-rule entry move with
  them. Named in the PR body.
* `make eval-reg`: 41 of 41, `root cause: 32/32 correct (100%)`, "no changes vs baseline". Exactly
  one dimension row in the whole sweep differs from the pre-change sweep — `trace_investigation`'s
  ROOT_CAUSE detail, `diagnosed unknown; ground truth unknown` → `diagnosed resource_exhaustion;
  ground truth resource_exhaustion`, still passing. The baseline was NOT re-blessed.
* Two frozen-evidence documents stopped regenerating byte for byte, and neither was rewritten. A
  close report derives its leak-hunt vocabulary from `HypothesisCategory` and prints judge-prompt
  digests "at assembly time"; the committed re-grade of archive `0db6fe722f7c` quotes today's
  ground-truth label on a row ADR 0040 does not grade. The tests now name those values explicitly
  and pin every other line to the byte. Same class as the corpus-size gate of cmd #284.
* The live effect on the agent is unmeasured: three prompt edits, no paid run. One re-run of
  `trace_investigation` and one of each saga scenario are in
  `.coordination/DEFERRED-PAID-RUNS.md`.

## How to verify

* `tests/unit/test_prompts_snapshot.py::TestSharedRulesReachEveryReader` — one sentence, three
  readers, served identically, delegated rather than copied, no unexpanded placeholder anywhere, and
  the reader set is exactly the three the decision names.
* `tests/unit/test_policies.py::TestStuckChainRootRule` — the rule's words against
  `HINT_ROUTED_TOOLS`, the saga pair graded on both arms from each root's own row, and a chain root
  with a permanent error reaching no replay tool.
* `tests/unit/test_policies.py::TestFixMapMatchesTheSuite` — red on the old routing, naming
  `saga_stuck`, and green only once the category is hint-routed.
