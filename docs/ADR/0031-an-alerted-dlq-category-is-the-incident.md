# ADR 0031: An alerted DLQ category is the incident, and the alert says which

* Status: accepted
* Date: 2026-09-07
* Decider: Kudrat Singh

## Context and problem statement

Live run `06e14be3e7b1` (`dlq_wait_and_replay_success`, archived under `evals/runs/`) is the second run of the seventh paid remediation scenario, taken after ADR 0030 fixed the id-transcription slip that killed run A. It failed differently, and this time nothing the agent said was false.

One tool call, one planner turn, zero plans. The agent called `list_dlq_messages` **unfiltered**, got the four seeded rows, and reasoned about all of them correctly — one `replay_safe` upstream timeout, two `wait_and_replay` rows on two different dependencies, one `human_required` CSV parse error. It even named the right remedy for each. Then it stopped:

> The DLQ contains 4 entries with mixed remediation hints that cannot be handled by a single Tier-1 action: (1) fc8d2a03 — bulk_api_sync, replay_safe (upstream timeout, idempotent, safe to replay immediately); (2) f030f975 — csv_upload, human_required (bad CSV data at row 15,382, non-retryable, data producer must fix); (3) af67d1b1 — bulk_api_sync, wait_and_replay (rate-limited, replay after 120s quota window); (4) 97d91272 — bulk_api_sync, wait_and_replay (SMTP unavailable, replay after smtp.mailer.internal recovers). […] A human operator must triage each entry individually.

The briefing judge scored groundedness 0.95. Every id, error type, hint and retry count matches the tool output exactly. The scenario graded red on four of five dimensions — outcome, evidence, action, safety — because it never planned anything.

### The escalation is a consequence of the scope, not of the reasoning

Read the quoted sentence as a claim about the world and it is true: four rows in three categories cannot be fixed by one call. Read it as a decision and it is wrong, and the reason it is wrong is upstream of the model.

The alert this run started from was:

```json
{"source": "platform.dlq", "severity": "critical",
 "fingerprint": "dlq_depth_warning_wait_replay", "group": null}
```

The incident is the **wait_and_replay backlog** — two rows, one deferred replay, exactly one Tier-1 action. The only thing in the payload that said so was the substring `wait_replay` inside a free-text fingerprint. The agent set its own scope from the queue it could see rather than from the incident it was paged for, got a four-row scope, and then [ADR 0008](0008-single-attempt-remediation.md)'s single-attempt limit did what it is designed to do: with one action available and a scope needing three, there is no partial fix to fall back to, so the run hands off to a human.

That interaction is worth stating plainly, because it will recur wherever scope is inferred rather than given:

> **An over-wide scope does not degrade into a partial fix. It converts into an escalation.**

Run A of the same scenario (`5c8895771fbd`) scoped itself to the wait slice correctly and unprompted. Nothing changed between the runs except the model's sampling. A behaviour that appears when a model happens to read a fingerprint string the way we hoped is not a behaviour the harness has.

### Why not parse the fingerprint

Because `ALERT_SUBJECT_PROBES` was keyed on the alert **field** rather than on `fingerprint` for reasons that have not changed (cmd #177): fingerprints are free text authored by the platform's alert rules, this corpus alone spells one family three ways, and a fingerprint-keyed map could only match by prefix or substring — the heuristic the guard exists to avoid. `remediate_dlq_backlog_success` makes the point concrete: it is a `replay_safe` incident whose fingerprint is the generic `dlq_depth_warning`, a string `dlq_backlog` also uses for a whole-queue alert about no category at all.

Decisively, the field is what carries the **value** the guard compares against, and the value comparison is the whole guard.

## Decision

**A DLQ alert that fires on one remediation category says so in a payload field, and that category is the incident's subject: the first discriminating read is the hint-filtered listing, and a `remediate` handoff is refused until that exact read is in evidence.**

Three parts.

1. **`AlertPayload.remediation_hint: str | None`** (`api/schemas.py`). Typed rather than left to `extra="allow"`, because it is load-bearing. Values are the platform's own DLQ vocabulary — the same strings `list_dlq_messages.remediation_hint` filters on and `replay_dlq_by_category.category` acts on. `None` is correct for a whole-queue depth alert and for a genuinely mixed one.

2. **`ALERT_SUBJECT_PROBES["remediation_hint"] = ("list_dlq_messages", "remediation_hint")`** (`agent/investigation.py`), **last** in declaration order so a named resource always outranks a slice. The existing guard then does the rest unchanged: value-matched, refuses rather than escalates, inert when the alert names nothing.

3. **Two prompt rules** (`llm/prompts/investigation_planner.md`): when the alert names a category, that category is the incident and rows in other categories are context; and a mixed queue is never a reason to escalate. The remediation planner's mixed-DLQ routing said "pick the most impactful action. If replay_safe entries exist, replay those" — which, on this exact alert, steers *away* from the alerted slice toward the one `replay_safe` row sitting beside it. That steer is replaced by an ordered rule: the alerted category first, impact only as the tiebreak when the alert named none.

### A slice is not a resource, and the admissibility rule is derived

The other four entries in the map name a resource. `remediation_hint` names a partition of a queue, and `TestAlertSubjectProbes` previously required every subject argument to be in `RESOURCE_ARG_FIELDS` — with a docstring saying why: a probe aimed at `limit` or `since_hours` would be asking the planner to prove something about a filter.

That protection is kept, and not by adding an exception. A slice is admissible as a subject **only where the platform names the same dimension on both sides of the read/act boundary** — the value filters the listing *and* narrows what an action touches. `SOURCE_LISTING_FOR_ACTION` already records exactly that pairing for ADR 0028's coverage check:

```python
ListingScope("remediation_hint", "category")
```

so the admissible set is a projection of an existing map rather than a second hand-written list (architecture-principles rule 2). `limit` and `offset` page a listing and narrow no action on any dimension, so no `ListingScope` pairs them with an action field and they can never qualify. `TestAlertSubjectProbes::test_the_slice_arm_is_not_vacuous` pins the derivation so the day the platform stops pairing them is the day this entry stops being admissible, loudly.

### It composes with ADR 0028 rather than costing a second read

A reading filtered to exactly the slice an action names **covers** that action under ADR 0028. So the probe this ADR demands is the same call that licenses a same-category replay: the subject read and the read-before-act read are one tool call, and no scenario pays for the overlap.

### One scenario deliberately keeps no category

`dlq_mixed_partial` carries no `remediation_hint`, on purpose, and this is the part most likely to be "tidied up" later.

The structural fix answers a category-scoped alert. The prompt rule beside it — *a mixed queue is never a reason to escalate; act on the safe slice and name the rest* — states something no alert field can, and it needs a scenario that can **fail** it.

> **Amended 2026-09-08 — the rule reads "never a reason to escalate WITH NOTHING DONE", and a partial action ends the run in escalation.** The sentence above is left as accepted; this line is the pointer, not a rewrite. What it got right is the SCOPE half (act on the safe slice, name the rest) and that half is unchanged. What it did not distinguish is *escalating instead of acting* from *acting and then handing over*: it reads as though the first is the only failure, which made RESOLVED the implied terminal state for a partial fix. WO-R2-164, decided by the user, says a partial action on a subject-less alert never resolves. See "Amendment: a partial action does not resolve the incident" under Consequences. `dlq_mixed_partial` now expects `escalated`. Give `dlq_mixed_partial` a category and every DLQ alert in the corpus becomes category-scoped, the mixed-queue rule becomes untestable, and the suite asserts the fix instead of measuring it. Its claims already say what the rule prescribes and needed no edit: replays sum to exactly 1, the listing is read first, and the human_required id, both wait ids, the `wait_and_replay` category and the bulk tool are all forbidden.

`dlq_backlog` also keeps none, for a simpler reason: it is a read-only scenario about queue **depth**, with no `expected_action_tools` at all, so the handoff guard is unreachable and a category would misdescribe the premise.

## Consequences

* **The guard is inert on production DLQ alerts until the platform emits the category.** The commander now reads a field the platform's DLQ-depth alert producer does not send. That is the intended shipping order — `alert_subject` returns `None` without it, so a real DLQ alert investigates exactly as it does today — and it is recorded as a test with a docstring (`test_the_dlq_category_field_is_one_of_them_and_is_a_filed_platform_gap`) rather than left as a silent gap. The platform-side ask is one field on the alert the depth rule already emits. When it arrives inside `extra_data`, nothing here changes: `alert_subject` already reads one level in.
* **Four scenarios gained a second investigation probe.** The unfiltered page stays first — it is context for the briefing, and it is the reading `evals/fixture_drift.py` compares the canned recording against (`_planner_arguments` takes the first probe of each tool) — with the scoped page after it. No fixture was re-recorded and the drift ledger is untouched.
* **`remediate_dlq_backlog_success` now states a premise its claims already had.** Since cmd #184 it graded exactly the two `replay_safe` rows; the alert now says `replay_safe` out loud. Its generic fingerprint is left alone deliberately, as the corpus's own argument for keying on the field.
* **A category-scoped alert cannot be answered by the whole-queue page.** An unfiltered listing wires `remediation_hint` to `None`, so it does not satisfy the guard. This is the intended reading rather than a technicality — the unfiltered page is what run `06e14be3e7b1` read before it stopped — and the refusal reason says so in as many words, because a planner that reads "no probe has read it" after having called the tool will otherwise re-read the same page.
* **What this does not do.** It does not decide *which* action a category warrants; the remediation planner's hint routing does that. It does not make a mixed queue actionable in one call — ADR 0008 still allows one action — it makes the alerted slice the one that gets it. And it does not touch the `human_required` fence: reading a category is not replaying it, and every replay tool stays forbidden in `dlq_human_required_escalates`.

### Amendment: a partial action does not resolve the incident (2026-09-08, WO-R2-164)

The bullets above are left exactly as accepted; this section records the one consequence of this ADR that turned out to be wrong, and how it was settled.

**Decided by the user: on an alert that names no subject, a partial action stabilizes what one call can reach and the run ESCALATES with every remaining row named and the action each one needs. RESOLVED is admissible only when the alerted condition is cleared.**

What this ADR framed, and where the framing failed. The rule quoted above was written against live run `06e14be3e7b1`, whose failure was *escalating with nothing done* — so "a mixed queue is never a reason to escalate" is the correction that run needed, and it is right. But it collapsed two questions into one sentence. *Which slice do I act on?* is answered here. *Is the incident over once I have?* was not asked, and the sentence's shape supplied the answer RESOLVED by implication. [ADR 0032](0032-the-action-must-address-the-alerts-subject.md) noticed it, set out the design, and deliberately did not encode it — flipping `dlq_mixed_partial`'s terminal state is a spend decision on a queued paid scenario and it contradicts this ADR, both of which made it the user's call rather than a builder's.

Live run `a0aa257bf865` is the failure in full. One replay of one row out of five, verified, RESOLVED — with a briefing that says *"leaving four unresolved"* and scores 1.0 for groundedness. Nothing in it is false. The row it was paged for is still in the queue, unfenced, with nothing recorded against it, and nobody is being woken.

**Three options, and the middle one was taken.**

| | | |
|---|---|---|
| **A** | keep `resolved` for the actionable slice, and record why | Rejected. It is `a0aa257bf865`'s shape: a true briefing under a terminal state that contradicts it. RESOLVED is what stops a human being paged. |
| **B** | stabilize what one action reaches, escalate naming the remainder and each row's disposition | **Taken.** Honest under ADR 0008 exactly as it stands, and it costs no extra tool call — the rows are already in evidence. |
| **C** | let one incident carry several Tier-1 calls against disjoint groups, so a mixed queue can actually be cleared | Deferred to **WO-R2-155**. A much larger change: it revises ADR 0008's single-attempt invariant, and the guards would have to apply per call. Under it some of B's escalations become resolutions, which is a better outcome and not a cheaper one. |

**What "cleared" means, precisely, because a rule about honesty cannot itself be vague.** For a subject-less DLQ alert the alerted condition is the queue the run read, and a row counts as **addressed by this run** when the one executed Tier-1 action named it — by id (`replay_dlq_by_ids.job_ids`, `mark_dlq_permanent.job_id`) or by the slice it narrowed to (`replay_dlq_by_category.category` matching the row's own hint in the listing) — so it was replayed, scheduled, or fenced. Addressed is not *fixed*: a scheduled replay has not run and a fence repairs nothing. It is "this run took a decision about the row and recorded it", which is the strongest claim a briefing can honestly carry. Every other row in the listing is unaddressed, and one unaddressed row is enough to escalate.

Four properties of the implementation, each answering a way it could have been got wrong:

* **It runs at the one `RESOLVED` transition, beside ADR 0026's check, not at plan time.** A partial action is a legitimate plan — refusing it would leave the safe row dead-lettered for nothing. How much of the condition it covered is a fact about the run, knowable only afterwards.
* **It reuses the stabilizer path's wording.** The escalation opens `STABILIZED, NOT RESOLVED`, the string ADR 0026 minted for a stabilize-only TOOL. This is a stabilize-only OUTCOME, and it is the same message to the same reader; a second phrasing would make every scenario claim on it tool-specific for no gain.
* **It is inert wherever the alert names a subject.** ADR 0032's guard already refuses any plan aimed elsewhere, so a run that executed addressed what it was paged for. Asking again there would escalate every correctly-scoped run over the furniture beside its incident — the opposite of what this ADR decided, which the amendment narrows rather than reverses.
* **The link that makes the queue the incident is the agent's own plan, never the alert's text.** Under a subject-less alert, choosing a dead-letter action *is* the run saying the queue is what it was paged for. No fingerprint is parsed and no `source` string is read — the heuristic this ADR refused twice stays refused.

**And a run that only read slices does not get to claim the queue is clear.** "Everything I looked at is addressed" is satisfiable by looking away: read `list_dlq_messages(remediation_hint="replay_safe")`, replay that category, resolve. So the condition set is built only from readings that narrowed on none of the declared `ListingScope` dimensions and whose `total` equals the rows they returned — the same "did not narrow" comparison `SubjectMatch.UNFILTERED` makes for the unclassified subject, plus a page-completeness check. A run that read the queue only through filters or partial pages escalates too, saying so.

Revisit trigger: WO-R2-155 landing. Under Option C a run could address every slice, at which point some of these escalations should resolve and this check would go quiet on its own — it asks about coverage, not about count.

## Alternatives considered

* **Parse the fingerprint.** Rejected above: substring matching on free text is the heuristic `ALERT_SUBJECT_PROBES` was built to replace, and the field carries the value the guard needs anyway.
* **Let the planner scope itself and grade the result.** This is the status quo, and it is what produced a pass in run A and an escalation in run B from the same world. Scope inferred from a queue listing is a property of sampling, not of the harness.
* **Relax ADR 0008 to allow one action per category.** A much larger change, and it fixes the wrong thing: the run did not need three actions, it needed the right scope for one. Multi-action remediation stays deferred (WO-R2-155 remains on file as the record that the single-action limit is what converts a wide scope into an escalation).

  **Re-examined 2026-09-08 (WO-R2-164), and still deferred — but the reasoning above only holds for an alert that names a slice.** It is exactly right for `06e14be3e7b1`: that run needed one scope, not three actions. For a genuinely subject-less mixed queue there is no scope to narrow to, so the single-action limit is not incidental — it is what makes a complete fix unreachable, and the honest end is a partial fix plus a handoff (Option B, taken; see the amendment under Consequences). **Option C is the multi-action design, and it is the follow-up rather than the answer here: `WO-R2-155`, a plan declaring up to K Tier-1 calls against disjoint resource groups, executed as one attempt, guards applied per call, verified once.** The user's decision on 2026-09-08 was "B now, C later", in those terms. What C would buy is real: a run that replays the safe row, schedules both wait rows and fences the poisoned one has cleared the queue and may honestly resolve, and the check this amendment adds would go quiet on such a run by itself.
* **Add `remediation_hint` to `RESOURCE_ARG_FIELDS`.** Rejected. It would make the subject test pass by redefining a filter as a resource, and `_unsourced_resource_args` would then start demanding that a category string be evidence-sourced — a different guard, changed by accident, for a value that comes from the alert.
* **Give every DLQ scenario a category.** Rejected: it deletes the only witness for the mixed-queue rule. See above.
