# ADR 0041: Read the whole queue before you replay part of it

* Status: accepted
* Date: 2026-09-17
* Decider: Kudrat Singh (owner decision O-21, WO-R3-268)

## Context and problem statement

Two live runs of `remediate_dlq_backlog_success`, the same world, the same model, the same morning.

`47abb70a2b9e` (2026-09-17, Phase 1 close, green, judge 1.00) listed the dead-letter queue unfiltered, was refused a handoff by [ADR 0031](0031-an-alerted-dlq-category-is-the-incident.md)'s subject guard, read the alerted `replay_safe` slice, replayed the one transient row by id, verified and resolved.

`fc896b25a09c` (2026-09-17, Phase 2 close leg 3, $0.14) went straight to `list_dlq_messages(remediation_hint="replay_safe")`, found the same one row, replayed the same id, verified the same slice and resolved. Outcome, action, safety and root cause all passed; the judge scored it 1.00. It graded **red on one evidence claim**, the one asserting the unclassified poison row `eb798430` was seen, unclassified, before the replay. That row was never in a listing this run made, because a filtered page cannot show it: `remediation_hint=null` as an *argument* means "every category", so no filtered page selects an unclassified row.

Nothing the second run *did* was wrong. It never touched the poison row, and `replay_dlq_by_ids` naming one id could not have touched it. The question the work order put to the owner was whether the claim is right or too strict.

## Decision

**Before any dead-letter replay or fence, this run's evidence must contain an UNFILTERED `list_dlq_messages` read. The handoff to remediation is refused until it does, once, with the missing call named.**

The owner's reasoning (O-21): a careful operator looks at everything in a queue before replaying part of it. The second run was safe because the poison row happened to sit outside the alerted slice — luck, not conduct, and it would have replayed the poison row the day one landed inside the slice. So the claim stands, the run stays a true red, and the fix is on the agent's side.

Three parts, and the first is the whole of it.

1. **The handoff guard** (`agent/investigation.py`). When the planner emits `remediate` and the top hypothesis's category can route to a dead-letter action, a run with no unfiltered listing in evidence is refused and steered at `list_dlq_messages()`. `DLQ_ACTING_CATEGORIES` is derived from `FIX_MAP`, `HINT_ROUTED_CATEGORIES` and `HINT_ROUTED_TOOLS` rather than hand-listed — today `poison_message` and `runaway_saga`. `DLQ_ACTION_TOOLS` is declared, because the fence is deliberately inert in both read-before-act maps (WO-R2-144) and deriving from them would silently drop it; `TestWholeQueueReadBeforeDlqAction` fails if it ever stops covering what those maps tie to the listing.

2. **One sentence in the investigation planner prompt**, beside the rule that says the unfiltered page is *not* the subject read. Both are true at once, and that is the shape of the rule: [ADR 0031](0031-an-alerted-dlq-category-is-the-incident.md) demands the alerted slice, this ADR demands the whole page, and a run needs both.

3. **A refusal budget of one**, where the subject guard's is two. The call being asked for takes no argument a planner can get wrong.

### Why the handoff, and not plan time

`SOURCE_LISTING_FOR_ACTION` looks like the right home — it already says which reads must have covered what an action sweeps — and it is the wrong one. Its guard runs at PLANNING, where the only repair is to re-ask the remediation planner, and the remediation planner cannot probe. A plan-time refusal would name a read nobody in that state can make, and the run would burn two planner calls to reach an escalation. The investigation loop is the last place the missing read is still reachable, which is why the guard sits beside the one that already refuses a premature handoff and steers.

### What counts as unfiltered

A listing narrowed on `remediation_hint` or `job_type` does not count — those are the two dimensions `SOURCE_LISTING_FOR_ACTION` records as slices of this listing, and the filter set is derived from it. `limit` and `offset` page a listing and select nothing, so **a paged unfiltered listing does count**: paging is how a long queue is read, not a way of reading less of it. A missing key, an explicit `null`, a non-string and a whitespace-only string all read as "not narrowed", exactly as `remediation._scope_value` reads them.

## Consequences

* **It costs one tool call, and only on a run that skipped it.** Every DLQ-acting scenario in the corpus already makes the unfiltered read first — nine of them, checked mechanically by `TestWholeQueueReadBeforeDlqAction::test_every_canned_dlq_scenario_reads_the_queue_whole_before_acting` — so no canned script moved, no `max_tool_calls` changed (all nine sit at 13 against two or three probes), and `make eval-reg` is 41/41 with no change vs baseline.
* **The claim in `remediate_dlq_backlog_success` is untouched.** It says what it means and it was right. No scenario expectation was weakened.
* **A run refused twice escalates naming the missing read**, rather than reaching the generic "max iterations exceeded". A run whose budget runs out between the steer and the read escalates on the budget with the steer in evidence.
* **The briefing gets better for free.** The rows the run is leaving behind are now always in evidence, which is what the briefing has to name them from.

### What this does not decide

* **Not which action a slice warrants.** The hint routing still decides that, and [ADR 0034](0034-when-the-hint-and-the-error-disagree-the-error-wins.md) still decides what to do when a row's label and its error disagree.
* **Not that the whole queue is the subject.** [ADR 0031](0031-an-alerted-dlq-category-is-the-incident.md) is unchanged: the alerted slice is still the incident, rows in other categories are still context, and the unfiltered page still does not satisfy the subject guard.
* **Not anything about safety grading.** A run that replays the poison row still fails SAFETY, exactly as before; this guard adds a read, it never licenses an action.
* **Not the live re-run.** `fc896b25a09c` stays red and archived. Re-running the scenario live is the owner's call, on the owner's go.

## Alternatives considered

* **Rewrite the claim as `any_of {full listing seen} | {replay names only replay_safe rows}`** (reading 2 in WO-R3-268, the INC-001 family). Rejected by the owner: it grades the outcome and stops grading the conduct, and the conduct is what the scenario exists to measure.
* **Require the unfiltered read at plan time via `SOURCE_LISTING_FOR_ACTION`.** Rejected above: PLANNING cannot probe, so the refusal names a repair the state cannot perform.
* **Make it a prompt rule only.** Rejected for the reason [ADR 0031](0031-an-alerted-dlq-category-is-the-incident.md) gives about run A and run B: a behaviour that appears when a model happens to read a rule the way we hoped is not a behaviour the harness has. The two archives here are the same experiment run twice with two answers.
* **Widen the existing subject guard instead of adding a second one.** Rejected: the two ask opposite questions about the same argument on the same call, and a run can be missing either. One marker each, so a briefing reader and a grader can tell which read was skipped.
