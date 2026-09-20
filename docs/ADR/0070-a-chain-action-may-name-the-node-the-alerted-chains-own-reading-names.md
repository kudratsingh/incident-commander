# ADR 0070: A chain action may name the node the alerted chain's own reading names

* Status: accepted
* Date: 2026-09-19
* Decider: WO-R3-284 (coordinator-filed from the WP-7.2 builder's divergence, 2026-09-18)
* **Amends [ADR 0032](0032-the-action-must-address-the-alerts-subject.md)** — the RESOURCE
  row of its target table gains a second admissible shape. Nothing in ADR 0032 is
  withdrawn: every plan it refused is still refused except the one shape named below.
* Closes the gap [ADR 0053](0053-family-c-is-one-chain-under-four-faults.md) § 4 filed, and
  ships the world that record dropped.
* Related: [ADR 0054](0054-one-rule-for-a-stuck-chains-root-rendered-into-every-reader.md)
  (one rule, every reader), [ADR 0033](0033-a-human-required-chain-root-is-fenced-then-escalated.md)
  (a forbidden set is derived from the sanctioned action),
  [ADR 0041](0041-read-the-whole-queue-before-you-replay-part-of-it.md) (the whole queue
  first), [ADR 0027](0027-read-the-row-before-you-replay-it.md) (read the row first),
  `../../../context/INCIDENTS.md` INC-002 (why half a rule is the failure)

## Context and problem statement

ADR 0032 closed a hole two paid live runs had walked through: a run read exactly the right
resource and remediated something else, twice, and reported RESOLVED both times. Its guard
requires a Tier-1 action to TARGET the alert's subject, and under `SubjectKind.RESOURCE`
the target test is value equality — `subject.value in acted`.

That test is right for a consumer group, a cache key and a trace. It is wrong for a
dependency chain, and WP-7.2 measured the consequence rather than reasoning about it.

The `workflow_stuck` family is one chain under four faults, and ADR 0053 § 1 makes every
world share one alert down to its `job_id`: the chain's ROOT. The plan's fifth world is
`create_stuck_dag(root_status="completed", failed_step=1)` — the root succeeded, descendant
1 is dead-lettered, the rest are `waiting`, exactly one dead-letter row and it belongs to a
descendant. The answer the plan wanted is the `human_required` arm of ADR 0054's rule:
fence the failed descendant, then escalate with the chain still stuck.

```
mark_dlq_permanent(job_id=<step-1>)      -> REFUSED (kind=resource)
replay_dlq_by_ids(job_ids=[<step-1>])    -> REFUSED (kind=resource)
mark_dlq_permanent(job_id=<root>)        -> allowed  (and the root has no row to fence)
```

**And the commander's own prompt told the agent to take the refused action.**
`investigation_planner.md`'s `human_required` rule: "emit `remediate`, and the remediation
planner fences that row with `mark_dlq_permanent` … `stop` here looks like the safe answer
and is the weaker one." So the steering pointed at the fence and the guard forbade it —
INC-002's half-a-rule shape, and the reason ADR 0053 § 4 dropped the world outright instead
of shipping either dishonest half: as an acting world it grades red on every run whatever
the agent does, and as an escalate-only world it grades the agent green for declining what
its own steering demands and pins a forbidden set contradicting the prompt.

ADR 0053 § 4 also wrote down what the repair would have to be, and this record is that
repair: "the guard would have to admit a resource that the alerted subject's OWN READING
names as part of the same DAG, and the prompt would have to say which node of a chain an
action may name. Half of that fix is worse than none."

The gap is general and predates the family: **any alert naming a DAG root whose non-root
node is dead-lettered has this shape.** No scenario exercised it, which is why nobody had
found it.

## Decision

**Both halves, in one change, or neither.**

### 1. The RESOURCE target test gains an evidence-grounded second shape

ADR 0032's table row becomes:

| Subject kind | The action targets it when |
|---|---|
| **RESOURCE** | the subject's value is among the action's own resource values (unchanged), **or** every resource the action names is a node of the graph the subject roots, per a reading of that graph THIS RUN ALREADY HOLDS |

The second clause is the whole of the change, and every word of it is load-bearing.

**"per a reading this run already holds"** — the admission is evidence-grounded, never
derived from the plan's own text. `_graph_nodes_in_evidence` walks `run_state.evidence`,
which is the same place the sibling arms read their DLQ rows from
(`_row_decisions_in_evidence`). An id the planner invented has no licence, and neither does
a correct id the run never read: a run that has not read the chain is sent to read it.

**"the graph the subject roots"** — scoped by the reading's own echo of what it is a view
of, `get_dag_state.seed_id` compared against the subject's value. Without that comparison
the guard would admit a node of ANY chain the run happened to read, and this suite seeds
three stuck chains (`workflow-stuck-eval`, `saga-stuck-eval`, `runaway-saga-eval`) whose
shapes are identical.

**"every resource the action names"** — the `off_graph` set must be empty, exactly as the
CATEGORY and UNCLASSIFIED arms require an empty `off_slice`. The family's queue holds four
replayable-looking rows belonging to other incidents, so a batch of
`[<a chain node>, <furniture>]` is refused: the chain half does not buy the sweep.

**Declared, not hard-coded.** `GRAPH_VIEW_FOR_SUBJECT` maps a SUBJECT'S PROBE TOOL to a
`GraphView(tool_name, subject_field, nodes_field, id_field)`, which is `SourceRow`'s
sibling for a read whose response is a graph rather than a list of independent rows. Today
one entry: `get_dag_state → (seed_id, nodes, id)`. Keying on the subject's probe tool is
what makes the admission **inert for every other subject**: a `consumer_group` alert with a
chain reading in evidence still refuses a DLQ replay, which is live run `adcdcadd94a3`'s
exact shape. The field names are checked against the pinned contract's `outputSchema`,
because a renamed response field would otherwise make the admission silently inert — and an
inert admission reads exactly like the refusal it replaced.

**The equality arm is untouched, deliberately.** A batch naming the subject AND furniture
passed this guard before and still does; the sweep is caught by
`forbidden_replay_job_ids`, by ADR 0041's whole-queue rule and by the graders. Tightening
it is a separate safety decision, and making it silently inside a widening is how a guard's
scope drifts.

**The refusal says what is now admissible.** When the action misses, the steer names the
nodes the run HAS read of the alerted chain, or says the run holds no reading of it at all
and to read it first, and names the ids that are not among them. A widened guard whose
refusal still describes the old rule spends re-asks teaching the planner the wrong lesson.

### 2. One shared sentence says which node an action may name

`llm/prompts/shared_rules.py` gains `CHAIN_NODE_ACTION_RULE`, written into three prompt
files as `{{rule:chain_node_action}}` and expanded by `load_prompt` as it serves them —
ADR 0054's mechanism, unchanged:

> An action about a dependency chain names a node the alerted job's own `get_dag_state`
> reading names — the alerted job itself, or, when that job completed and the chain's one
> dead-letter row belongs to a descendant in that same reading, that descendant — because
> the reading is what makes a node part of the incident you were paged for, so an id no
> reading of the alerted chain carries, a node of a different chain, and a dead-letter row
> sitting in the queue that the chain view does not name are each outside this incident and
> are never targets.

| Reader | Where | What it needs the rule for |
|---|---|---|
| the planner | `investigation_planner.md`, Rules | "the job I was paged for is `completed`" is not a healthy reading and not a `stop` |
| the fix table | `remediation_planner.md`, "Stuck dependency chains" | every instruction there applies to the descendant's id and its own row, unchanged |
| the judge | `briefing_judge.md`, groundedness | a briefing whose action named a different id than the alert is GROUNDED when the alerted chain's reading lists it |

**The reader set is exactly ADR 0054's**, and a test asserts the two lists are equal rather
than merely each correct. The routing rule says which TOOL a chain's dead-letter row gets;
this one says which NODE it is aimed at. A reader given one and not the other is answering
an incomplete question, which is the same failure one level up from INC-002.

### 3. The fifth world ships

`workflow_stuck_downstream_child_failed`: `create_stuck_dag(chain_name=workflow-stuck-eval,
root_status=completed, failed_step=1, remediation_hint=human_required,
child_age_seconds=2820)`, under the family's one alert, field for field. Root `completed`,
upstream `completed`, descendant 1 `dead_letter` with `human_required` on a schema error,
descendant 2 `waiting` behind it. Sanctioned action: one `mark_dlq_permanent` on the
DESCENDANT, then `escalated` with the chain still stuck — the fence is
`Resolution.STABILIZES` (ADR 0026), so the escalation is the state machine's own, and ADR
0041's whole-queue read comes first. Ground truth `poison_message`: the label has to be the
family's fifth distinct answer (ADR 0053's one-alert test requires one answer per world),
and it is the honest one — the chain is held by a payload that cannot be processed, not by
a node that stopped stepping, and `poison_message` is already in `HINT_ROUTED_CATEGORIES`
so the fence is a routed action rather than one the steering never reaches.

Corpus 62 → 63; the `workflow_stuck` family is five worlds, two of which sanction an
action.

## What this deliberately does NOT do

**It does not widen the CATEGORY or UNCLASSIFIED arms.** Both are about a listing of
independent rows, where "part of the same object" has no meaning; `GraphView` exists
precisely because a graph read makes a claim a listing cannot.

**It does not add a second graph tool.** `get_trace` is also a subject probe and its
response also has structure, but a trace's spans are not resources a Tier-1 tool acts on,
so an entry for it would be admission with nothing to admit. The map is one row and the
test that pins it is what makes adding a second a decision.

**It does not widen `SOURCE_ROW_FOR_ACTION["mark_dlq_permanent"]`** — WO-R2-144 stays open,
as it did under ADR 0032. The new world's fence is aimed at a row the run read anyway,
because ADR 0041 requires the unfiltered queue read before any dead-letter action.

**It does not change what a fence achieves.** It stops a later bulk or category sweep from
re-running a job that cannot succeed, and it repairs nothing; the chain is still stuck and
the run still escalates.

## Alternatives considered

* **Give the fifth world an alert naming the descendant.** ADR 0053's own rejected
  alternative, and it still works mechanically. Rejected for the reason recorded there: it
  costs the family's one-alert property, making `job_id` the single field that differs
  between worlds with different answers — ADR 0051 rule 2's own prohibition.
* **Admit any id the run read anywhere.** Rejected: that is ADR 0032's rejected "require
  only that the action be *related* to the subject", and it would admit the four furniture
  rows in this very family's queue, which is the failure the guard exists for.
* **Admit by walking `edges` rather than `nodes`.** Rejected as strictly weaker for the
  same cost: the node list is what the platform says is in this view, while an edge set
  would have to be traversed and the traversal would reach ids the reading does not carry.
* **Admit any node of the chain, unconditionally, with no reading required.** Rejected. It
  is derivable — the ids are `uuid5` of the chain name — but only by the evaluator, never by
  the agent's own guard, and a guard that admits an id on a derivation rather than on a
  reading admits the invented id too.
* **Ship the world escalate-only and change the prompt to match.** ADR 0053's second
  rejected alternative: a rule saying "never aim an action at a node other than the alerted
  root" would encode a harness limitation as intended conduct, and be wrong on the day the
  guard was widened. Which is today.
* **Change the guard and leave the prompts alone.** The exact inverse of the state ADR 0053
  § 4 refused, and the same INC-002 failure: the guard would permit an action nothing asks
  the agent to take, so the measured behaviour would not move and the widening would look
  inert on every live run.

## Consequences

* **Three prompt hashes move**, the same three ADR 0054 moved, and for the same mechanical
  reason — the snapshot hashes the text as the loader serves it. Named in the PR body.
* **One pinned test flips, and it flipped less than its own docstring predicted.**
  `TestWorkflowStuckFamily::test_a_non_root_action_is_refused_so_the_fifth_world_cannot_be_graded`
  said "if the guard is ever widened to admit a node of the alerted DAG, this test is where
  that shows up". It did not: the `RunState` it built carries **no evidence at all**, so the
  widened guard refuses its plans for the new right reason (no reading, no licence) and the
  assertion stayed green. Recorded because the lesson generalises — a pin on a guard's
  refusal has to supply the evidence the admission keys on, or it measures the inert case.
  Its successor supplies the chain reading and asserts both directions.
* **`make eval-reg` reads 63/63** with `workflow_stuck_downstream_child_failed` as the only
  delta and no pre-existing row moved. A scenario added after a bless is reported NEW by the
  gate and never fails it, so corpus growth needs no re-bless.
* **The live effect on the agent is unmeasured.** Two prompt-surface edits and a guard
  widening, no paid run: live acceptance for the new world is one deferred row in
  `.coordination/DEFERRED-PAID-RUNS.md`, needing its own explicit yes. Offline the world
  grades 5 dimensions green against a canned planner script, which measures the script.
* **Every other `workflow_stuck` world is unchanged**, including the four recordings and
  their fixtures. The family's alert is still byte-identical across all five worlds.

## How to verify

* `tests/unit/test_remediation.py::TestAChainNodeActionIsAdmittedByTheChainsOwnReading` —
  the fence and the by-id replay on the descendant admitted; the invented id, another
  chain's node, a run with no reading, and a batch reaching outside the chain each still
  refused; a non-graph subject unaffected by a chain reading; the plan reaching
  `REMEDIATING` through the real `make_llm_plan`; and the table checked against both the
  subject map and the pinned contract's output schema.
* `tests/unit/test_prompts_snapshot.py::TestTheChainNodeActionRuleReachesEveryReader` — one
  sentence, three readers, served identically, delegated rather than copied, the reader set
  equal to ADR 0054's, and the rule naming the read the guard keys on.
* `tests/unit/test_policies.py::TestWorkflowStuckFamily` — five worlds, five answers, one
  alert, two acting worlds, and the successor of the flipped pin.
