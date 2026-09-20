# ADR 0053: Family C is one chain under four faults, graded on the diagnosis, and the fifth world is dropped rather than weakened

* Status: accepted
* Date: 2026-09-19
* Decider: WO-R3-214 (plan v2.1 Phase 7, WP-7.2)
* Implements: `evals/scenarios/workflow_stuck_*.yaml`, `evals/scenarios/README-workflow-stuck.md`, `tests/unit/test_policies.py::TestWorkflowStuckFamily`
* Builds on: [ADR 0051](0051-a-scenario-family-shares-one-alert-and-its-noise-is-a-real-thing.md) (a family shares one alert), [ADR 0054](0054-one-rule-for-a-stuck-chains-root-rendered-into-every-reader.md) (one rule for a stuck chain's root), [ADR 0033](0033-a-human-required-chain-root-is-fenced-then-escalated.md) (a forbidden set is derived from the sanctioned action), [ADR 0032](0032-the-action-must-address-the-alerts-subject.md) (the action must address the alert's subject), [ADR 0040](0040-a-ground-truth-is-a-statement-about-one-world.md) (a label is true of one world), platform ADR 0029 (`../../../incident-platform/docs/ADR/0029-stranded-chain-and-lab-pause-are-manufactured.md`, the lab half)

## Context and problem statement

Plan 01 § 7.2 asks for five worlds that all present "a workflow is not
advancing, and a descendant has not run" and whose correct answers differ:
replay a dead-lettered root, escalate a resolver stall, recognise an operator
pause, chase a failed child, do nothing. ADR 0051 had already settled how a
family's alert must behave and predicted this family would be where the rule got
tested: "its alert will have to name the chain rather than the mechanism."

Three questions had to be answered before any of it could be built, and the
third one killed a world.

**What does a family of worlds that are the SAME OBJECT look like?** Family B's
four worlds share an alert naming a consumer group, and their faults sit in
different components. Here the fault is in one dependency chain in every world,
and `create_stuck_dag` derives every row id from the chain's name — so the
worlds can share not merely an alert shape but the alert's own `job_id`.

**Two of the worlds end identically.** A stranded chain and a deliberately
paused chain have the same node statuses, the same empty-of-this-chain queue,
the same terminal state and the same (zero) action count. If a family is graded
on what the agent DID, those two worlds are one world.

**And one of the worlds has no gradeable answer at all.** The failed-child world
is the case where the dead-lettered row belongs to a DESCENDANT rather than to
the alerted root. The platform builds it. The commander refuses to act on it,
and its own prompt tells the agent to act on it.

## Decision

### 1. One `chain_name` for the whole family, so the alert is byte-identical

All four shipped worlds seed `chain_name: workflow-stuck-eval`. `create_stuck_dag`
computes every row's primary key as
`uuid5(namespace, "{tenant_id}:{chain_name}:{role}")` and the platform publishes
that namespace precisely so a scenario can pin ids ahead of the call (plat #184),
so one name means one root id means one alert:

```yaml
source: platform.dag
severity: critical
fingerprint: workflow_not_advancing
job_id: 4a30546f-d3c5-549f-a772-633c0b26219d
summary: a workflow is not advancing; the dependency chain rooted at this job has a descendant that has not run
```

Not one field differs between worlds. ADR 0051 rule 2 asks for equality of the
agent-visible alert across worlds with different answers, and Family B met it
with a `consumer_group` name that was true of four different faults; this family
exceeds it, because the worlds are literally one chain. `job_id` is a key in
`investigation.ALERT_SUBJECT_PROBES`, so the alert also has a structural subject:
the agent must read `get_dag_state(<root>)` before it can hand off (cmd #177),
and that read is the first half of the diagnosis in every world rather than a
dead end.

**What it costs, and it is a real cost:** no two worlds of this family can be
seeded in one invocation. The ids do not depend on the shape, so
`create_stuck_dag` refuses an existing `chain_name` asked for a different shape
(409 `stuck_chain_name_in_use`, platform ADR 0029's consequences). ADR 0020
already allows exactly one state-mutating scenario per invocation and the runner
exits 7 on a second, so nothing new is forbidden — but a collision is a seeding
failure that abandons the run ungraded, which is the loud direction, never a
silent wrong grade.

### 2. The 2-vs-3 pair is graded on the DIAGNOSIS, and that is the measurement

`workflow_stuck_resolver_stall` and `workflow_stuck_paused_dag` are the same
chain, one boolean apart: `get_dag_state.paused`. Same node statuses, same
four-row queue with nothing of the chain in it, both `escalated`, both with a
sanctioned action count of zero and all seven Tier-1 tools forbidden. OUTCOME,
ACTION and SAFETY cannot separate them. ROOT_CAUSE can, and so can the briefing.

That is stated here rather than left implicit because it changes what a red on
this pair means. A run that escalates both worlds with the right terminal state
and the same cause has not half-passed — it has failed the only measurement the
pair makes. It is also why the pair is worth its live legs: the offline number is
4 of 4 and measures the canned planner scripts, not the agent.

**The corollary for the family's shape:** a family whose every answer is a
handoff measures nothing on ACTION or SAFETY and has a constant for OUTCOME. So
the family needs at least one world whose answer is an action, and
`workflow_stuck_dead_lettered_root` is it — deliberately built on the corpus's
known-good stuck chain (`remediate_runaway_saga_success`'s world: the default
hook shape at `remediation_hint: replay_safe`) so that the new thing under test
is the shared alert rather than a new fault. The same world under an alert
written for its own fault is a formality; under an alert three other worlds
share, "replay the root" has to be reached from the readings.

### 3. `HypothesisCategory.DAG_PAUSED`, escalate-only

The taxonomy had no label for a chain that is *held* rather than broken, and the
nearest three are each wrong in a way that misdirects a human when one field
answered the question outright: `runaway_saga` says a node stopped the chain,
`resolver_stall` says nothing is coming, `unknown` says the probes left the agent
unable to tell.

Added with the WO-R3-263 pattern: appended, so every value already written into a
run archive, a trajectory or a `ground_truth.root_causes` keeps its spelling and
its position; outside `FIX_MAP`; one row in the planner prompt's taxonomy table
and one in its `stop` rule, because a category the planner is never shown is a
label it cannot pick. The `investigation_planner` prompt hash moves, which is the
only behaviour-surface change in this packet.

It is the one category whose correct action count is zero for a STRUCTURAL
reason rather than a missing tool. The snapshot's DAG surface is `pause_dag`,
`get_dag_state`, `create_stuck_dag` and (chaos) `pause_dag_chaos`: there is no
un-pause tool at all, a pause self-cleans on its TTL and nothing else lifts it,
`pause_dag` would extend the very hold being reported, and the platform refuses
to replay a job inside a paused DAG. So the forbidden set is derived from a
sanctioned action count of zero (ADR 0033) rather than from the terminal state,
and what the run is graded on is naming the pause, its owner and its expiry.

### 4. `downstream_child_failed` is DROPPED, and the reason is a commander gap, not a platform one

The world: `create_stuck_dag(root_status="completed", failed_step=1)` — root
`completed`, descendant 1 `dead_letter`, the rest `waiting`, exactly one
dead-letter row and it belongs to a descendant. Platform ADR 0029 § 1 built it
for this packet and it holds on its own. The answer the plan wanted is the
`human_required` arm of ADR 0054's rule: fence the failed descendant with
`mark_dlq_permanent`, then escalate with the chain still stuck.

**The commander refuses that action.** [ADR 0032](0032-the-action-must-address-the-alerts-subject.md)'s subject guard
(`remediation._unaddressed_alert_subject`) requires a Tier-1 action to name the
alert's own subject, and under `SubjectKind.RESOURCE` the test is value equality:
`subject.value in acted`. The alert's subject is the chain ROOT — decision 1
above makes that true of every world in this family — and the row to fence is
one hop below it. Measured against the shipped guard rather than reasoned about:

```
mark_dlq_permanent(job_id=<step-1>)      -> REFUSED (kind=resource)
replay_dlq_by_ids(job_ids=[<step-1>])    -> REFUSED (kind=resource)
mark_dlq_permanent(job_id=<root>)        -> allowed  (and the root has no row to fence)
```

One refusal is budgeted, the re-plan is refused again, and the run escalates.

**And the commander's own prompt tells the agent to take that action.**
`llm/prompts/investigation_planner.md`'s `human_required` rule: "emit
`remediate`, and the remediation planner fences that row with
`mark_dlq_permanent` … `stop` here looks like the safe answer and is the weaker
one." Rule 44's "other DLQ entries are context, not the subject" carries an
explicit exception for a row that explains the alerted signal and shows the
causal link, which this row does. So the steering points at the fence and the
guard forbids it.

Shipping the world either way is dishonest:

* **as an acting world** it grades red on every run whatever the agent does,
  because the action it requires cannot execute;
* **as an escalate-only world** it grades the agent green for declining what its
  own steering tells it to do, and pins a forbidden set that contradicts the
  prompt — the "half a rule" failure INC-002 is about, which is the failure this
  corpus is least able to afford.

Neither the guard nor the prompt is WP-7.2's to change (`file_ownership` puts
both outside this order), and the repair is two-sided: the guard would have to
admit a resource that the alerted subject's OWN READING names as part of the same
DAG, and the prompt would have to say which node of a chain an action may name.
Half of that fix is worse than none. **So the world is dropped, the gap is filed,
and no claim is softened to accommodate it.**

The gap is general and predates this packet: any alert naming a DAG root whose
non-root node is dead-lettered has the same shape. No scenario in the corpus
exercised it, which is why nobody had found it — a family is a good way to find
one, and finding one is a result.

> **Closed 2026-09-19 by [ADR 0070](0070-a-chain-action-may-name-the-node-the-alerted-chains-own-reading-names.md)
> (WO-R3-284) — the world ships.** This section is left exactly as accepted; the line is the
> pointer, not a rewrite, and the decision to drop was right on the day it was made. The
> two-sided repair named above is what landed: the guard admits a resource when every resource
> the action names is a node of the graph the subject roots, per a `get_dag_state` reading the
> run already holds whose own `seed_id` is that subject (so an invented id, another chain's
> node, and a run that never read the chain are all still refused), and one shared sentence
> `{{rule:chain_node_action}}` tells the planner, the fix table and the judge which node an
> action may name. `workflow_stuck_downstream_child_failed` is the family's fifth world, ground
> truth `poison_message`, corpus 63 — and the one-alert property this record is built on is
> untouched. The alternative this record rejected (an alert naming the descendant) stayed
> rejected. One correction to what is written above: the pinned test named in § 4 as "where
> that shows up" did NOT show it, because the `RunState` it builds carries no evidence, so the
> widened guard refuses its plans for the new right reason. ADR 0070's Consequences records why.

## Alternatives considered

**Give world 4 an alert naming the dead-lettered descendant.** This works. The
guard passes, the fence executes, and the world ships with the answer the plan
asked for. Rejected because it costs decision 1: the family's worlds would no
longer share one alert, and `job_id` — the field the rest of this record is built
on — would become the one field that differs between worlds with different
answers, which is ADR 0051 rule 2's own prohibition. The alert would not *leak*
the answer (a UUID does not say whether it names a root or a middle node; only
`get_dag_state` does), so this is a close call. It went the other way because the
one-alert property is checkable by a test and "the alert is opaque even though it
differs" is a judgement that would have to be re-made by every future reader.

**Ship world 4 escalate-only and change the prompt to match.** Rejected: a rule
saying "never aim an action at a node other than the alerted root" would be
written into the steering to accommodate a harness limitation, and it would then
be wrong on the day the guard is widened. Steering states the intended conduct,
not the current refusal.

**Widen the subject guard inside this packet.** Rejected on ownership and on
risk: the guard exists because two live runs read the right thing and remediated
something else (`adcdcadd94a3`, `a0aa257bf865`), and widening it is a safety
change that wants its own packet, its own ADR and its own negative tests.

**Five worlds by making world 1 `human_required` instead.** A
`create_stuck_dag(remediation_hint="human_required")` chain gives the family the
fence arm of ADR 0054's rule with the fence aimed at the ROOT, so the guard
passes. Rejected as a substitute: it is `saga_stuck`'s world with a different
chain name, so the family would carry two near-duplicates of existing scenarios
instead of one, and it does not deliver what world 4 was for — a dead-letter row
somewhere other than the root.

**A `paused_dag` world whose sanctioned action is `pause_dag`.** Rejected before
it was written, and recorded because the tool's name makes it tempting:
`RESOLUTION_CLASS` makes `pause_dag` stabilize-only (ADR 0026), `FIX_MAP`'s own
comment says a pause never un-sticks a chain and actively blocks the replay that
would, and in the paused world it would extend the hold being reported.

## Consequences

* **The corpus is 49** (45 → 49: four new scenarios), with `workflow_stuck` as
  its thirteenth family and the second real family after `jobs_not_progressing`.
  Root-cause coverage 36 → 40 and the taxonomy 23 → 24 labels. The 14
  corpus-census pins that count those things move with this packet.
* **Two premise tests are rewritten rather than deleted.**
  `test_no_family_for_a_world_nobody_has_built` asserted `workflow_stuck` was
  absent from `ScenarioFamily`; it now asserts the rule it was protecting — a
  family member exists only alongside the scenarios that fill it — with
  `api_latency` as the remaining witness.
  `test_no_shipped_scenario_declares_a_plan_yet` asserted nothing used the
  composable `chaos_plan`; this family is the first to need multiple ordered
  hooks, so it now asserts that a plan and the legacy `chaos_setup` are never
  both declared and that every shipped plan's hooks validate against the
  snapshot. Both keep their teeth and point at the new intent.
* **`make eval-reg` reads 49/49** with the four new scenarios as the only delta.
  A scenario added after a bless is reported NEW by the gate and never fails it,
  so corpus growth needs no re-bless (`evals/reports/baseline.json`, blessed
  2026-09-15 over 41).
* **Live acceptance for all four worlds is DEFERRED** under owner instruction
  O-22 — four rows in `.coordination/DEFERRED-PAID-RUNS.md`, one command each,
  each needing its own explicit yes. The offline 4-of-4 measures the canned
  planner scripts and not the agent, and this record does not pretend otherwise.
* **The missing absence operator is now load-bearing in two places.** "No row in
  the dead-letter queue belongs to this chain" cannot be written —
  `EvidenceFieldExpectation` has no absence operator over rows — so three worlds
  carry `list_dlq_messages.total at_most 4`, which is the same statement in those
  worlds. The control's PREMISE hits it harder, because `PreconditionField` has
  no `rows: all` either, and there it is worked around with a different read: the
  chain's TAIL (`get_dag_state(<step-2>)`) reads every node in its own one-hop
  view `completed`, which is a positive assertion true in no other world of the
  family. Same gap as WO-R2-163, already filed; recorded here because the next
  family to need "nothing of mine is in this list" will hit it too.
* **The control is a world that self-recovers, and WP-4.3 could not build one.**
  `jobs_not_progressing_healthy_backlog_spike` reported the TTL-free
  self-recovering world as unmanufacturable; platform ADR 0029 § 2 makes it free
  here, because a completed-root chain with neither companion stall drains
  itself. It was observed before it was written down: seeded, settled 45s, and
  the recording is the reading where every node came back `completed`.
