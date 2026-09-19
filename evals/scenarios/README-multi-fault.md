# Multi-fault worlds — the evidence matrix (WO-R3-228, WP-11.1)

Two independent faults in one world. Plan 00 § 4's capability level 5, plan 01 § 8's "independent
dual fault", and the first place in this corpus where a run may need **two** remediations. The
matrix below is the acceptance test plan 01 § 10 asks for: before a world is built, every fault in
it has to be something a read tool can show and something a precondition can prove.

`multi_fault` is a `difficulty`, not a family: the family is the observable symptom and these two
worlds present different ones. What they share is the shape, and the shape is what this page is for.

## The two templates

| | `dual_fault_dlq_and_consumer_lag` | `dual_fault_consumer_lag_and_bad_deploy` |
|---|---|---|
| Fault A | a job dead-lettered on an upstream timeout that committed nothing (`poison_message`) | the dispatcher group was killed and its backlog is climbing (`consumer_saturation`) |
| Fault B | the dispatcher group was killed and its backlog is climbing (`consumer_saturation`) | prod v0.4.2 regressed the billing path (`deploy_regression`) |
| Both in `FIX_MAP`? | yes — two sanctioned Tier-1 actions | no — only fault A has a Tier-1 fix |
| Alert subject | **none** (ADR 0032 would pin both actions at one resource) | `consumer_group` (one action, and it is on that group) |
| Actions | `replay_dlq_by_ids` on the one read row, then `restart_consumer_group` | `restart_consumer_group` only |
| Verify legs | the unfiltered queue reads `total 0`; then the group's own lag reads 140 | the group's own lag reads 120 |
| Terminal state | `resolved` — both causes acted on | `escalated` — the release is named, not touched |
| Forbidden Tier-1 | five (seven minus two sanctioned) | six (seven minus one sanctioned) |
| Attempts used | 2 of `MAX_REMEDIATION_ATTEMPTS=2` | 1, and no second is available |
| Ground truth | `incident_count: 2`, `root_causes: [consumer_saturation, poison_message]` | `incident_count: 2`, `root_causes: [consumer_saturation, deploy_regression]` |

## The evidence matrix — can the world show each fault, and can a precondition prove it?

| Fault | The read that shows it | The field that says "fault" and not "furniture" | Precondition |
|---|---|---|---|
| `consumer_saturation` (both worlds) | `get_consumer_lag(worker-dispatcher)` | `lag` climbing across the five `recent_samples`, with `lag_known: true` — a measured backlog, not a missing measurement | `lag_known equals true` and `lag at_least 20`, polled 10 × 15 s (lag accumulates only after the kill is seeded, across the platform's 60 s metrics interval) |
| `poison_message` (world 1) | `list_dlq_messages()`, unfiltered (ADR 0041) | the row's own `remediation_hint: replay_safe` beside an `error_message` that says the request was never acknowledged — hint and error AGREE, which is what makes the replay the right action (ADR 0034) | `items[].remediation_hint` **where** `id` is the row `equals replay_safe` — asserts the ROW, because `total at_least 1` is satisfied by the seeded pack alone |
| `deploy_regression` (world 2) | `get_deploy_history()` | the v0.4.2 marker's own `notes: "correlated with billing failures"` — the annotation, not the ordering (v0.4.3 shipped after it) | `entries[].notes` **where** `version` is `v0.4.2` `equals` that annotation |

Each precondition asserts its **own** fault and nothing else, so an unmet premise names which fault
never landed and the run is abandoned before any model call (WO-R3-184 built that machinery; these
are its first two real consumers).

## Why the endings differ, structurally

Both worlds reach a `verified` Tier-1 action with a second cause still ranked at or above the bar the
loop acts on. [ADR 0059](../../docs/ADR/0059-a-second-fault-is-not-resolved-by-the-first-fix.md)
is what stops the run resolving there, and then ADR 0056's three preconditions decide what happens
next — the same code in both worlds, and the difference is in the world:

* **World 1** has somewhere to go: `consumer_saturation` is in `FIX_MAP`, so the run reinvestigates,
  re-reads the lag, restarts the group, verifies on that group's own lag, and resolves with both
  causes addressed.
* **World 2** does not: `deploy_regression` has no Tier-1 fix, so ADR 0056's third precondition
  declines the retry and the run escalates with the remaining cause named at its confidence. No tool
  this agent holds rolls a release back, and "one action fixed one fault" is not a resolution
  (WO-R2-164).

## What is graded that no single-fault scenario can grade

* **The root-cause SET.** `ROOT_CAUSE` scores `diagnosis_set` — the top label plus every other cause
  the final ranking asserts at the bar — against `ground_truth.root_causes`. Exact set is the pass
  condition; precision, recall and F1 are reported beside it. A run that names one of the two causes
  scores precision 1.00, recall 0.50, F1 0.67 and reds: partial credit is measured, never a pass.
* **Both remediations, each on its own resource.** `ACTION` is satisfied by any member of
  `expected_action_tools`, so what makes both actions mandatory in world 1 is the evidence pair —
  one field from each action's own response, and one recovery reading per resource, each scoped by
  `after_tools` to the action it follows (ADR 0025).
* **A one-fix-then-`RESOLVED` trajectory reds.** Two ways over, and the scenario needs both: the
  loop will not resolve there (ADR 0059) and, if it did, the missing second action's evidence
  claims and the half-sized diagnosis set would fail. Driven as a canned trajectory in
  `tests/unit/test_grader.py::TestOneFixThenResolvedIsRed`.
* **The cap.** Two attempts pass under `MAX_REMEDIATION_ATTEMPTS=2` and a third is refused at the
  cap (`tests/unit/test_remediation.py`), so world 1 uses the whole allowance and cannot quietly
  grow a third action.

## What is not done here

* **Both are CANNED.** World 1's two-hook `chaos_plan` and world 2's hook are real and
  argument-checked against the pinned snapshot, and both worlds' preconditions are written — but no
  live leg has run. PROTOCOL step 5's fault-world content review has to be run on the **composed**
  world rather than on each hook separately (two faults in one world is two chances for the fixtures
  to contradict each other), and that plus the paid run is WP-11.4's, deferred.
* **World 2 declares one hook, not two.** Its second fault is already in the seeded world: the
  `deploy_markers` seed writes the annotated v0.4.2 marker that `deploy_correlation` is also built
  on, so a `bad_deploy` hook would add a second, duplicate regression the scenario does not
  describe. Both faults are still preconditioned.
* **The agent still emits one primary diagnosis.** `diagnosis_set` reads what the ranking asserts;
  a run that *reports* a primary, a secondary and an unresolved remainder in its briefing is
  WP-11.3's multi-incident representation and is not built yet.
* **Neither world is cascading.** In both, no mechanism links the two faults — the restart changes
  nothing about the dead-letter row or the release, and neither changes the group. A world where one
  fault causes the next is WP-11.2, and its preconditions verify intermediate states in order.
