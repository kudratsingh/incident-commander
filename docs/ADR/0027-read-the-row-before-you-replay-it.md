# ADR 0027: Read the row before you replay it

* Status: accepted
* Date: 2026-09-07
* Decider: Kudrat Singh

## Context and problem statement

`remediate_runaway_saga_success` was staged and held for the user's go. Its correct trajectory, as ADR 0026 left it, is three moves: probe `get_dag_state`, see a `dead_letter` root holding `waiting` descendants, replay that root with `replay_dlq_by_ids`.

The user read that trajectory before releasing the money and asked the question nobody in the loop had asked: **how does the agent know that root is safe to replay?**

It does not. `get_dag_state`'s node model is five fields:

```
id, type, status, retry_count, created_at
```

There is no `remediation_hint` and no `error_message` among them. So `"status": "dead_letter"` tells the agent that the root **stopped** the chain, and says nothing whatsoever about whether restarting it is safe. Replaying a job on that evidence is a coin flip dressed as a diagnosis, and one of its faces is re-running a poison payload that will dead-letter again — or re-running a job the platform's own classifier fenced off for a human.

The answer exists. It lives in exactly one place: the job's row in `list_dlq_messages`, which carries the `remediation_hint` the platform's triage wrote. That tool's own description states the rule this ADR enforces:

> A null hint is UNKNOWN, not replay-safe: do not feed those to a categorised replay. Read the error, then replay by explicit id, or fence it with `mark_dlq_permanent`.

An **unread** row is strictly less evidence than a null hint.

### Why every existing guard admitted it

This is the part worth carrying forward, because the plan that concerned the user passes four separate structural checks:

| Guard | Verdict | Why |
|---|---|---|
| tier checks | admit | `replay_dlq_by_ids` is Tier-1, `get_dag_state` is a read |
| `_absent_resource_args` (ADR 0024) | admit | both legs name their resource |
| `_unsourced_resource_args` (ADR 0024) | admit | the job id **is** platform-produced — the alert carries it, and `get_dag_state` echoes it as `seed_id` and as a node `id` |
| `_misdirected_verify_args` (ADR 0024) | admit | same value on both legs |
| `VERIFY_PROBE_FOR_ACTION` (ADR 0025) | admit | `get_dag_state` genuinely observes what a replay changes |
| `RESOLUTION_CLASS` (ADR 0026) | admit | `replay_dlq_by_ids` genuinely resolves |

Every one of them is correct. Together they establish that the agent is acting on **the right object, named honestly, and can check its own work**. None of them asks whether acting on that object is a good idea, and `_unsourced_resource_args` is the near-miss that makes the gap easy to overlook: it proves the platform uttered the id, which reads a great deal like "the platform told us about this job" and means only "this string is not a hallucination".

### The class, stated generally

The guard family so far answers three questions: *did anyone read what this alert is about* (`ALERT_SUBJECT_PROBES`), *is this action aimed at that thing* (ADR 0024), *can anyone see what it changed* (ADR 0025), and *is a verified success worth anything* (ADR 0026). All four can be satisfied by a run that never established whether the change should be made at all.

**Some actions have a precondition that is a fact about the resource, not about the plan** — and the only way to have that fact is to have read it.

## Decision drivers

* The information exists and costs one tool call. This is not a capability gap; it is a read the agent was told it did not need.
* A prompt line alone would not hold. Offline eval replays canned planner output and never loads a prompt (ADR 0026's second blind spot), so a prompt rule about a live-only surface is ungated by the suite that exists to gate behaviour changes.
* The rule must not decide *for* the planner. `wait_and_replay` warrants a deferred replay and `replay_safe` an immediate one; both are legitimate, and a structural rule admitting only one value would refuse every correct `wait_and_replay` plan in the suite.
* It must be inert where it does not apply. No listing classifies a cache key or a consumer group.

## Decision

**A plan that replays a dead-lettered job is refused before execution unless that job's own dead-letter row is already in the run's evidence.**

### `SOURCE_ROW_FOR_ACTION` (`agent/remediation.py`)

The third map in the probe family, and the one that asks about the past: *which read must have SEEN this resource before an action may touch it?* Each entry names the read tool, the field holding its rows, the field within a row that identifies the resource, and the field the read exists to expose.

```python
"replay_dlq_by_ids": (SourceRow("list_dlq_messages", "items", "id", "remediation_hint"),),
```

TOTAL over the Tier-1 slice, like both its siblings and for the same reason: an empty tuple is a *declared* inert entry and `tests/unit/test_policies.py::TestSourceRowForAction` fails on any Tier-1 tool with no entry at all, so a replay tool shipped next year cannot inherit "of course it may act on an unread row" by silence.

The check compares the action's own resource arguments (`RESOURCE_ARG_FIELDS`, so a batch contributes every id) against the ids found in rows of matching evidence entries. Deliberately narrower than `_evidence_value_corpus`, which collects every string the platform ever uttered — that corpus is what makes an alert-supplied id look sourced.

**It requires the ROW, never a particular hint VALUE.** Reading is structural; deciding is the prompt's job and the scenario's claim.

### Refuse and steer, then fail closed

Same posture as ADR 0025: the first bad plan is refused, not escalated, and the planner is re-asked with the unread ids and the exact read named. The second escalates without executing.

The repair available here is narrower than ADR 0025's, and this is worth stating plainly because it is the honest limit of the mechanism: **PLANNING is a single LLM call with no tool budget, so the planner cannot go and fetch the row it is missing.** What it can do — and the only repair the re-ask is for — is drop the ids it has no row for, which is how a batch carrying one listed job and one unlisted one gets fixed. A planner with no row for any of its ids has nothing to re-plan toward, and the run escalates naming the read that was skipped.

That escalation is the intended failure, not a defect: fail closed toward the human rather than replay a job whose classification nobody looked at. Keeping the run green is the **investigation planner's** job, which is why the rule lands in both prompts (below) and not only in the one the guard interrupts.

### Steering, in two prompts

* **`investigation_planner.md`** — the planner that owns the probes learns that a dead-lettered job is not remediable until its dead-letter row has been read, and what each of the four readings means. This is what makes the guard's happy path reachable.
* **`remediation_planner.md`** — the *Stuck dependency chains* section (ADR 0026's) is extended rather than replaced. It now states that the chain view carries no hint, how to reach the row (filter by hint, or page until the id appears), and the decision: `replay_safe` or a transient error → replay by explicit id; `human_required`, a bad-data / schema / poison error, or **no hint at all** → do not replay, fence with `mark_dlq_permanent` only if the operator's intent is to stop retries, else escalate naming the root.

ADR 0026's prompt said the opposite — *"You do not need the DLQ listing to act here"* — with an invariant test pinning it. True about the cheapest trajectory, false about the safe one. Both are reversed here, and the replacement test says why.

### Grading, so the claim does not rest on the guard

The guard is a thing under test; a scenario whose claim rests on it cannot notice it going missing. Two grader axes were added so the scenario can state the claim independently:

* **`where` (a `RowSelector`)** scopes a comparator to the rows a selector picks out. Without it, "the root's row says replay_safe" can only be written as two any-row assertions that two *different* rows satisfy — and the seeded listing always contains a genuinely `replay_safe` row, so the pair is green for a world in which the root itself is `human_required`.
* **`before_tools`** restricts an assertion to entries recorded before the first call to a named tool, making ordering expressible. `list_dlq_messages` is also the natural post-replay verify probe, so without it a run that replayed first and read afterwards carries exactly the evidence a correct one does.

## Consequences

* `remediate_runaway_saga_success` gains one read (9 → 10 live calls against a cap of 13; the margin absorbs it) and two claims: the root's row read `replay_safe`, before the replay.
* **`saga_stuck` finally has a discriminator.** Its chaos hook now seeds `remediation_hint: human_required`, so the escalate-only twin differs from the resolve twin in something the agent can read. Before this, the two expected opposite behaviour from byte-identical evidence and a green run measured the planner's temperament. ADR 0026 identified this and left it as the user's call; this is that call taken, and it is cheap because the hook already supported the argument.
* **The category replays are not covered.** `replay_dlq_by_category` names a filter and no ids, so there is nothing to look up; the equivalent rule is a statement about the category, against a different argument. Five scenarios queued for paid runs would newly bind, so it is filed (WO-R2-143) rather than improvised. Their laziest passing trajectory still permits reading the hint *after* acting.
* **`mark_dlq_permanent` is left inert** though its entry could be filled today. Fencing is the conservative direction — it stops auto-replay rather than re-running anything — so acting on an unread row there cannot cause the harm this ADR is about. Filed as WO-R2-144.
* A run whose investigation skipped the read now escalates instead of replaying. That is the point, and it is a behaviour change that will show up as an escalation in a live run whose steering did not take.

## Alternatives considered

**Refuse the handoff in the investigation loop instead** (where `ALERT_SUBJECT_PROBES` sits), so the loop can still make the read and the run stays green. Rejected as the *primary* mechanism: at handoff time the plan does not exist, so the guard would have to infer which job will be replayed from `FIX_MAP[category]` plus the alert's `job_id` — true for the saga case and false in general. The prompt rule reaches the same planner without an inference, and the plan guard states the rule where the ids are actually known.

**Require the hint to equal `replay_safe`.** Rejected: it would refuse every correct `wait_and_replay` plan and would move the planner's decision into the policy layer, which is where ADR 0026 says such decisions specifically do not belong.

**Prompt only.** Rejected under architecture-principles rule 3, and by ADR 0026's own evidence: offline eval never loads a prompt, so a prompt-only rule is invisible to the gate.

## Related

* ADR 0024 — plan arguments name their resource (the guard family this joins).
* ADR 0025 — a verify leg must observe the action (same refuse-and-steer posture).
* ADR 0026 — a stabilizer is not a resolution (the prompt section this extends, and the `saga_stuck` question this answers).
