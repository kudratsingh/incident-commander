# ADR 0033: A `human_required` chain root is fenced, then escalated

- Status: accepted
- Date: 2026-09-08
- Deciders: repository owner (WO-R2-160)
- Supersedes: the "What was deliberately NOT changed: `saga_stuck`" consequence of
  [ADR 0026](0026-a-stabilizer-is-not-a-resolution.md), and the `saga_stuck` exemption noted in
  [ADR 0028](0028-read-the-category-before-you-replay-it.md)
- Related: [ADR 0027](0027-read-the-row-before-you-replay-it.md) (the read this decision acts on),
  [ADR 0032](0032-the-action-must-address-the-alerts-subject.md) (why the action targets the root)

## Context

[ADR 0026](0026-a-stabilizer-is-not-a-resolution.md) classified `mark_dlq_permanent` as
`Resolution.STABILIZES` and the user settled WO-R2-140 as **fence, then escalate**: a dead-lettered
row the platform classified `human_required` is fenced out of auto-replay with
`mark_dlq_permanent`, and the run escalates because a fenced row is not a fixed one.

That ADR carved out one exception and said so out loud, because it was a real question rather than
an oversight:

> **What was deliberately NOT changed: `saga_stuck`.** Its chain root is seeded `human_required`
> (cmd #192) and it forbids every Tier-1 tool including the fence. … A `platform.dag` alert makes
> the *chain* the incident: replaying that root by id is how the chain drains, the platform permits
> it … and it is the exact question being escalated — so fencing the root records "never
> auto-replay this" against a decision the human has been handed.

The reasoning is ADR 0032's — act on the alert's subject — and the carve-out was left to the user
as WO-R2-160 rather than taken inside a PR, because re-pointing a scenario's expected behaviour is
the class of decision ADR 0026 refused to take quietly the first time.

## Decision

**A dead-lettered job classified `human_required` is fenced and then escalated, wherever that job
sits — including when it is the root of a stuck dependency chain.** There is no chain-root
exception.

The escalation still names the root, quotes its dead-letter row, and says the chain is still stuck.
The replay that would drain the chain remains the human's decision; the fence does not make it.

## Why the carve-out's premise was wrong

The exception rested on the fence and the replay being alternatives. They are not.

1. **A fence forecloses nothing the human is being asked to decide.** The platform permits a replay
   of a fenced row **by explicit id** — only category scans and the default bulk sweep skip fenced
   rows, which is the sentence ADR 0026 quoted while drawing the opposite conclusion from it. The
   un-stick a person may well intend, once the payload is fixed, is still exactly one
   `replay_dlq_by_ids` call away.
2. **Leaving it unfenced forecloses something nobody is deciding.** An unfenced `human_required`
   root is in scope for the next operator's `replay_dlq_by_category` sweep, which will re-run a
   payload that cannot succeed and burn another attempt on it. Nobody weighed that; it just
   happens.
3. **The disposition a fence records is the platform's own, not a new one.** On this root
   `previous_hint` comes back `human_required`: the classifier had already said the job needs a
   human. The fence adds a timestamp and an audit row, not an opinion.

So the two decisions are on different axes — "should this chain be replayed" (the human's) and
"should a bulk sweep be free to replay it in the meantime" (not a decision anyone was making) —
and the exception collapsed them into one.

## What the fence does NOT do, measured rather than argued

`get_dag_state` on the chain root, read before the fence and after it against the pinned v0.6.2
stack on 2026-09-08, came back **byte-identical**: root `dead_letter` at `retry_count: 3`,
descendant `waiting`, `paused: false`. The fence stamps the DLQ row and touches the chain not at
all.

That is the whole reason the terminal state is `escalated` rather than `resolved`, and it is why
the briefing has to carry `STABILIZED, NOT RESOLVED`, the root id and the error text. It is also
why `pause_dag` stays forbidden here: it is stabilize-only *and* it blocks the by-id replay the
briefing recommends (`find_blocking_pause`).

## Consequences

**`saga_stuck` becomes an escalating scenario that requires an action**, the second in the corpus
after `dlq_human_required_escalates`. `expected_action_tools: [mark_dlq_permanent]`, with a
universal `equals` pin on the root's `job_id`; six Tier-1 tools forbidden instead of seven.

**The lazy trajectory flips colour, and that is the point.** While the action count was zero, "read
the chain, read the row, escalate having touched nothing" was the *passing* run. It now fails
ACTION and SAFETY while still reaching the expected terminal state. The general rule, written into
`docs/eval-methodology.md`: derive the forbidden set from the sanctioned action, never from the
terminal state.

**An ordering claim became writable.** ADR 0027 and ADR 0028 both recorded that `saga_stuck` could
carry no `before_tools` boundary, because a boundary naming a tool that must never fire fails
closed on every correct run. There is an action now, so the root's own hint must be read
**before** the fence. `consumer_lag_high` remains the corpus's witness for "no action, therefore no
ordering claim".

**The fence is verified on `fenced_at`, not on the `human_required` page.** This root was already
classified, so `list_dlq_messages(remediation_hint="human_required")` returns it whether or not
anything was fenced — the no-observable problem cmd #205 hit, which plat #198 (WO-R2-158) fixed by
adding `fenced_at`/`fenced_by`. Both prompts and the scenario now verify on the stamp. The
precondition pins the root's `fenced_at` as null before the run, so a non-null read is necessarily
post-fence and the ordering comes free from the value.

**Deliberately NOT asserted: a post-fence chain read.** A plan has one verify tool and `fenced_at`
lives only in the DLQ listing, so any `get_dag_state` claim would grade the pre-action
investigation probe under every `which` — a statement about the world *before* the fence dressed as
one about the world after it. The claim is carried by the briefing instead.

**The corpus check widened rather than the alert.** `HINT_ROUTED_TOOLS` already routed
`human_required` to `mark_dlq_permanent`; what had to change was
`TestHintRoutedToolsMatchTheSuite`'s reach, which was keyed on the alert's own `remediation_hint`
and therefore silent on a `platform.dag` alert. It now also selects a scenario whose alert names a
RESOURCE and whose graded evidence pins that resource's row hint, narrowed to routed tools that can
name a resource (this ADR's sibling rule, ADR 0032, reused rather than restated — a category replay
names a filter, not a row).

**The alert deliberately does not carry the hint.** Putting `remediation_hint: human_required` on
the alert would have been the cheap way to make the existing check reach this scenario, and it
would have handed the agent the discriminator cmd #192 created so the escalation would rest on
something the agent *reads*. The twin scenarios must stay separable only by that read.

**The cap moves 11 → 13.** Requiring an action puts the scenario in the remediation class, where
the polling profile (2 probes + 1 ADR-0009 re-probe + 1 action + up to 6 ADR-0006 verify polls =
10) and the ≥30% margin apply. Under ADR 0019 the cap is also the runtime ceiling, so 11 would have
truncated the verify loop rather than grading a correct run red.

**No prompt exception survives.** Both planners carried the carve-out in prose; both now route a
`human_required` row to the fence uniformly and describe the chain case as a different *briefing*,
not a different plan.

## Alternatives considered

**Keep the carve-out (escalate-only).** Rejected by the user. It leaves the poisoned root exposed
to the next bulk sweep, and it makes the corpus's only stuck-chain escalation scenario one whose
passing trajectory is to do nothing — which grades temperament, the very defect cmd #192's
discriminator was added to remove.

**Fence and resolve.** Refused by ADR 0026 and unchanged here: the payload is still wrong, the root
is still dead, and the descendants are still waiting. Reporting that incident closed would be
false.

**Pause the chain as well as fencing the root.** Rejected: `pause_dag` self-expires, changes
nothing about the stopped node, and blocks the replay the human is being asked to make.
