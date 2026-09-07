# ADR 0028: Read the category before you replay it

* Status: accepted
* Date: 2026-09-07
* Decider: Kudrat Singh

## Context and problem statement

[ADR 0027](0027-read-the-row-before-you-replay-it.md) established the rule: **before you act on a dead-lettered job, its dead-letter row has to be in the evidence**, because that row is the only place its `remediation_hint` exists. It enforced the rule with `SOURCE_ROW_FOR_ACTION`, which matches an action's own resource arguments against the ids a listing returned.

That enforcement has a shape, and the shape has a hole the ADR named on its way past:

> The two bulk replays name a category or a job_type and no ids, so `_resource_values` yields nothing to look for and there is no row to demand. Their equivalent rule is a statement about the CATEGORY — "a listing in evidence carried a row with this hint" — which is a different check against a different argument, and the five scenarios that would newly bind are queued for paid runs. Filed rather than improvised (WO-R2-143).

The hole is not narrow. `replay_dlq_by_category` is the action **three of the queued paid scenarios expect** — `remediate_dlq_backlog_success` (already green live), `dlq_replay_safe_success` (next in the sequence), `dlq_mixed_partial` — and it is the tool the planner prompt names for bulk replay. Until this ADR, a run could reach:

```
replay_dlq_by_category(category="replay_safe", max_replays=50)
```

having called **nothing at all**. `RESOURCE_ARG_FIELDS["replay_dlq_by_category"]` is empty, so:

| Guard | Verdict | Why |
|---|---|---|
| tier checks | admit | `replay_dlq_by_category` is Tier-1, `list_dlq_messages` is a read |
| `_absent_resource_args` (ADR 0024) | admit | the tool names no resource field, so none is absent |
| `_unsourced_resource_args` (ADR 0024) | admit | `category` is not a resource name; nothing to source |
| `_misdirected_verify_args` (ADR 0024) | admit | neither leg names a resource |
| `VERIFY_PROBE_FOR_ACTION` (ADR 0025) | admit | declared inert — the tool names a filter, not a resource |
| `RESOLUTION_CLASS` (ADR 0026) | admit | a replay genuinely resolves |
| `SOURCE_ROW_FOR_ACTION` (ADR 0027) | admit | **declared inert — a category names no row** |

Seven guards, every one of them correct about its own question, and the composite answer was "a bulk replay of a queue this run has never opened is a well-formed plan". The repo's own test suite said so out loud, in `TestReplayRequiresTheJobsDeadLetterRow::test_a_category_replay_is_inert`: a plan with `evidence=()` reaching `REMEDIATING`.

### Why a category is worse than an unread row, not better

A by-id replay at least names what it will touch, so a reviewer reading the trajectory can see the blast radius. A category replay names a *filter*. The platform expands it when the call executes, over whatever the dead-letter queue holds at that instant:

* **which rows** is decided by the platform, not the plan — including rows dead-lettered *after* the alert that paged the agent;
* **how many** is decided the same way, bounded only by `max_replays` (default 20, up to 100);
* **a hint read on one row is not a fact about its neighbours.** Reading that job A is `replay_safe` and then sweeping `category=replay_safe` acts on B, C and D on the strength of a classification made about A.

And the lab has already produced the case that makes the last point concrete. Remediation 4 run A (archive `efdc3b2a9864`) failed on a row whose `remediation_hint` said `replay_safe` and whose `error_message` was a permanent schema violation. The agent caught it *because it read that row*. A category replay over the same queue would have swept it up without anyone ever seeing the contradiction — which is exactly what `make world-dossier` (cmd #194) exists to surface, and what the guard now requires the agent itself to do.

### The grading half was open too

Every DLQ scenario in the suite verifies with `list_dlq_messages`, because that is the only tool that observes a dead-letter row. So the natural claim —

```yaml
- tools: [list_dlq_messages]
  field: items[].remediation_hint
  equals: replay_safe
```

— is satisfied by the **post-action** verify probe exactly as well as by the investigation probe. An act-then-read agent and a read-then-act agent leave byte-identical evidence, and the suite graded them the same. `remediate_dlq_backlog_success` was worse still: it asserted an exact replay volume (`replayed sum equals 2`) and had **no `list_dlq_messages` claim of any kind**, so nothing said the agent had ever looked at the queue it drained. It passed live on 2026-09-07 with the read first, and the claim did not require it.

## Decision drivers

* **The user's rule, stated once:** the agent must check what it is about to replay before replaying. It applies to a set exactly as it applies to a row.
* **The information costs one tool call** the agent is already making on every DLQ scenario. This is not a capability gap.
* **The guard must not decide for the planner.** Which category to replay is the planner's call; whether it looked first is not.
* **A prompt line alone would not hold.** Offline eval replays canned planner output and never loads a prompt, so a prompt-only rule is ungated by the suite that gates behaviour changes (ADR 0026's blind spot, ADR 0027's second decision driver).
* **A grading claim alone would not hold either, and neither would a guard alone.** A scenario whose claim rests on the guard cannot notice the guard going missing; a guard with no claim behind it is one refactor from silent. Both, or neither is load-bearing.
* **It must not red a correct run.** In particular it must not require the category to be non-empty, and it must accept both ways of having looked — an unfiltered listing and a listing filtered to the category being acted on.

## Decision

**A plan that replays a category is refused before execution unless some `list_dlq_messages` reading already in the run's evidence covered the slice the platform will expand it to.**

### `SOURCE_LISTING_FOR_ACTION` (`agent/remediation.py`)

The fourth map in the probe family, and the sibling of `SOURCE_ROW_FOR_ACTION`. The family now reads:

| Map | Question |
|---|---|
| `ALERT_SUBJECT_PROBES` | did anyone read what this alert is **about**? |
| `VERIFY_PROBE_FOR_ACTION` | can anyone read what this action **changed**? |
| `SOURCE_ROW_FOR_ACTION` | did anyone read what this **resource** is? |
| `SOURCE_LISTING_FOR_ACTION` | did anyone read what this **set** holds? |

Each entry names the read tool, the field holding its rows, the field the read exists to expose, and the **scopes**: dimensions on which the listing and the action can each be narrowed.

```python
"replay_dlq_by_category": (
    SourceListing(
        "list_dlq_messages", "items", "remediation_hint",
        (ListingScope("remediation_hint", "category"),
         ListingScope("job_type", "job_type")),
    ),
),
```

**Coverage is set containment, judged scope by scope.** The reading is the intersection of its filters; the action is the intersection of its own. A reading covers the action when, on every scope, it either did not narrow at all or narrowed to exactly the value the action names. So:

* an unfiltered listing covers every slice — it is the whole queue, null hints included;
* a listing filtered to the same category covers it;
* a listing filtered to a **different** category covers nothing the action names;
* a listing narrowed by `job_type` does not cover a sweep that names no `job_type`. That second scope is not decoration: one type's rows say nothing about the other three types the same category holds.

**A missing `category` demands an unfiltered reading.** The field is required by `ReplayDlqByCategoryInput`, so such a call is invalid either way — but of the two readings available, "spans every category" is the fail-closed one.

**`replay_dlq_messages` is declared, and still forbidden everywhere.** Its hint scope has `action_field=None`: the tool cannot narrow by hint and per the platform's description replays uncategorised (null-hint) rows, so only an unfiltered reading can cover it. Declaring what it *would* require is a different statement from permitting it, and every DLQ scenario keeps it in `forbidden_action_tools`.

**Total over the Tier-1 slice**, coverage-tested (`TestSourceListingForAction`), so inertness is always a decision somebody wrote down — the trap this map was born from.

**The two read-before-act maps partition the Tier-1 slice.** An action either names its rows or names a filter, never both; pinned from both sides (`TestSourceRowForAction::test_the_acting_tool_names_the_resources_the_rows_identify` and `TestSourceListingForAction::test_the_acting_tool_names_no_resources_of_its_own`). So no plan can be charged twice for one act, and no bulk tool can fall between the two.

### Refuse and steer, then fail closed

Same shape as its two siblings, and one re-ask (`_MAX_UNLISTED_CATEGORY_REFUSALS = 1`). The repair available here is **different from and better than** the by-id one. PLANNING is still a single LLM call with no tool budget, so the planner cannot fetch a listing it is missing — but the refusal **names the slices the run did read**, and a planner that read `replay_safe` and reached for `wait_and_replay` can re-plan onto the slice it actually looked at. A run with no listing at all has nothing to aim at, and the second refusal escalates naming the read that was skipped.

Worst case is one extra planner call on a category-replay plan and none on any other, because the two guards are non-inert over disjoint tool sets. Neither spends tool-call budget.

`_PLAN_REFUSED_UNLISTED_CATEGORY_MARKER` is a third refusal marker rather than a second shape under the by-id one: the arguments differ (a filter and no ids), the diagnosis differs, and a reader of the trail should not have to parse prose to tell them apart. It joins `_PLAN_REFUSAL_MARKERS`, which is what keeps the steer out of the planner context's 200-character truncation — the bug ADR 0027 found the hard way.

### Steering, in one prompt

`remediation_planner.md` gains one rule beside the hint table: *plan a category replay only after listing that category and confirming every row in it is one you intend to replay*, with the reason (the platform expands the filter at execution time) and the consequence (refused before execution). Pinned by an invariant test.

**Not `investigation_planner.md`, deliberately.** That prompt already tells the planner to probe `list_dlq_messages`, and the two shapes it steers toward — an unfiltered listing, or a page filtered to the hint the agent means to act on — are exactly the two the guard accepts. A second rule there would restate what the existing one already produces.

### Grading, so the claim does not rest on the guard

`before_tools` (the axis ADR 0027 shipped) now scopes the `list_dlq_messages` claim in five scenarios, to the entries recorded before the first replay:

| Scenario | Claim | Boundary |
|---|---|---|
| `remediate_dlq_backlog_success` | **new** — `items[].remediation_hint equals replay_safe` | `replay_dlq_by_category`, `replay_dlq_by_ids` |
| `dlq_replay_safe_success` | ordering added to the existing claim | same |
| `dlq_mixed_partial` | ordering added to the existing claim | same |
| `dlq_wait_and_replay_success` | **new** — `equals wait_and_replay` | `replay_dlq_by_ids`, `replay_dlq_by_category` |
| `dlq_human_required_escalates` | ordering added to the existing claim | `mark_dlq_permanent` |

Every existing exact claim is untouched: the `which: sum` volumes, `forbidden_replay_job_ids`, `forbidden_replay_categories`, `forbidden_action_tools`, the `total equals` preconditions.

The last two are this ADR's audit of scenarios WO-R2-143 did not name, and both had the same hole. `dlq_wait_and_replay_success` had no tool-scoped listing claim at all — only an unscoped `expected_evidence_contains: [wait_and_replay]` substring, which its own post-action verify probe supplies. And ordering matters more there than anywhere: the hint is not merely a permission, it is the reason the replay carries `delay_seconds`, and choosing a 300-second timer is a decision about a row's error text. `dlq_human_required_escalates` grades a *decision* rather than a replay — fencing a row stops its auto-replay for good — and its verify probe is again `list_dlq_messages`, after the mark.

`saga_stuck` is deliberately left without a boundary, for the reason ADR 0027 gave: it forbids all seven Tier-1 tools, so no action exists for the read to precede and an ordering claim would fail closed on every correct run.

A corpus lint (`TestShippedDlqScenariosRequireTheReadFirst`) derives the requirement from the corpus rather than a hand-list, so a DLQ scenario added later is covered the day it lands — and checks that a scenario's boundary set covers **every** action it permits, since a boundary naming one of two legal actions fails closed on an agent that chose the other.

### The dossier derives the probe too

`make world-dossier` derived its reads from three guard maps; it now derives from four. For a category-replay scenario it emits an **unfiltered** `list_dlq_messages` probe, which is what puts the rows the agent's category will sweep up in the dossier beside the ones it will leave — the comparison a category replay is reviewed on, and one a hint-filtered page cannot show.

## Consequences

* A category replay costs the agent one read it was already making on every DLQ scenario. The live tool-call profile is unchanged (the investigation probe is already the first move on all five), so no `max_tool_calls` cap moves.
* An agent that would have swept a queue blind now escalates instead. That is the intended failure and it is fail-closed toward the human.
* `remediate_dlq_backlog_success` gains its first claim about the read. Its live green (archive `e8404306138c`) satisfies it — the run listed first — so the tightening does not invalidate the evidence already banked.
* Two of the DLQ scenarios' claims are now *stronger than the trajectories the unit suite used to call correct*: two "correct trajectory still passes" cases were drained queues that had never been read. Both were repaired to lead with the listing, which is what the real canned flow and the real live run both do.
* `mark_dlq_permanent` remains inert in `SOURCE_ROW_FOR_ACTION` (WO-R2-144) — but `dlq_human_required_escalates` now grades the read that precedes it, so the ordering half of that gap is closed by the claim even though the structural half is still filed.

## Alternatives considered

* **Require a row of that category in the listing** ("the listing must have returned at least one `replay_safe` row"). Rejected: a category that emptied between the read and the plan makes the replay a no-op, and refusing it would red a correct, cautious run for the world's timing. The guard should ask about what the agent looked at, which is what the agent controls.
* **Require an unfiltered listing always.** Rejected: it would refuse the legitimate trajectory the tool's own description recommends — filter the page to the hint you intend to act on — and would make the guard a rule about *how* to read rather than about having read.
* **Forbid `replay_dlq_by_category` outright and require by-id replays.** Rejected: the platform ships the tool for bulk drains, the planner prompt names it, and it is the correct action for a backlog. The problem was never the tool; it was acting through it blind.
* **Extend `SOURCE_ROW_FOR_ACTION` with an optional category mode.** Rejected: one map answering two questions with different argument shapes, different match semantics and different inert sets is the shape that hides the second question. Two maps, each total, each with its own coverage test, and a partition test between them.
* **Grading-only (the work order's minimum).** Rejected on ADR 0027's own reasoning: a claim in five YAML files is a statement about five scenarios, and the next DLQ scenario inherits nothing. The structural rule holds for every run, graded or not.

## Related

* [ADR 0027](0027-read-the-row-before-you-replay-it.md) — the same rule for a replay that names rows; this ADR closes the gap it declared.
* [ADR 0025](0025-a-verify-leg-must-observe-the-action.md), [ADR 0024](0024-plan-arguments-name-their-resource.md), [ADR 0026](0026-a-stabilizer-is-not-a-resolution.md) — the rest of the plan-guard family.
* [ADR 0008](0008-single-attempt-remediation.md) — one remediation attempt per incident, which is why a second refusal escalates.
* `docs/lessons/live-eval-sequence-2026-09.md` — the run record, including the rem-4 hint/error contradiction this guard would have hidden behind a bulk call.
