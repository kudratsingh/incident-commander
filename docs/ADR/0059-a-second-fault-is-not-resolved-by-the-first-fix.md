# ADR 0059: A second fault is not resolved by the first fix — and a diagnosis may name two causes

* Status: accepted
* Date: 2026-09-19
* Decider: Kudrat Singh (WO-R3-228, WP-11.1)
* Amends: [ADR 0056](0056-a-retry-earns-its-attempt-by-reinvestigating.md) (the retry edge, whose
  second attempt could resolve over a stabilizer the first attempt had recorded) and WO-R3-191's
  one-label reading of the ROOT_CAUSE dimension (`docs/eval-methodology.md` § "Root cause is
  measured separately from outcome")
* Related: [ADR 0026](0026-a-stabilizer-is-not-a-resolution.md) (a verified stabilizer still
  escalates), [ADR 0032](0032-the-action-must-address-the-alerts-subject.md) (why a dual-fault
  alert must name no subject when two resources are acted on), [ADR 0037](0037-a-scenarios-fault-is-a-plan-and-the-plan-is-put-back.md)
  (the two-hook chaos plan these worlds are built from), [ADR 0038](0038-ground-truth-is-evaluator-only.md)
  (ground truth is evaluator-only), [ADR 0025](0025-a-verify-leg-must-be-able-to-observe-the-action.md)
  (each action verified on its own resource)

## Context and problem statement

Capability level 5 is two independent simultaneous faults in one world. Building the first two
templates for it (WO-R3-228) hit two walls that no amount of scenario authoring gets past, because
both are in the code the scenarios run on.

**1. A verified action ends the run, whatever else the run just said was broken.**
`make_llm_verify` asks one question after a `verified` verdict — "is the alerted condition
cleared?" — and that question is `_uncleared_alert_condition`, which is inert unless the alert names
no subject AND the action is a dead-letter action AND a whole-queue reading shows rows nobody
addressed (WO-R2-164). Everything else goes straight to RESOLVED. So a run that fixed one of two
faults, with the second ranked at 0.8 in its own final ranking, resolved the incident. That is the
shape scenario 8 run A was red for — "a partial action never ends resolved" — one level up from the
dead-letter queue: the leftover is not a row, it is a fault.

ADR 0056 got half of this right and said so: a stabilized attempt carries "STABILIZED, NOT RESOLVED"
onto the ledger "so reinvestigating cannot lose it". Nothing stopped the *next* attempt resolving
over it, and ADR 0026's rule is unconditional.

**2. A single-label diagnosis can never be right about a two-cause world.** WO-R3-191 read the
diagnosis as `RunState.hypotheses[0]` alone and recorded the consequence honestly: on a two-cause
world a single-diagnosis run "scores precision 1.00, recall 0.50, F1 0.67 and still reds". That was
an accurate statement about a corpus with no multi-fault scenario in it. As a grading rule it makes
every level-5 scenario permanently red, so the ladder's fifth rung could be built and never passed.

The reason WO-R3-191 chose one label is the one that matters and has to survive: **counting the rest
of a ranking pays for hedging.** "The correct cause appeared anywhere in the candidate set" is
pass@k, a different metric.

## Decision

### 1. The resolve gate: another cause the run asserts and has not acted on

`make_llm_verify` asks `_unaddressed_second_cause` after `_uncleared_alert_condition` returns
`None`. It returns a `ConditionMiss` — the existing "verified, and the incident is not over" shape,
which either reinvestigates (ADR 0056's three preconditions) or escalates with the reason — when
either holds:

* **An earlier attempt in this run stabilized.** Any `_remediation_attempt_failed` record carrying
  the verdict `verified_stabilizer`. ADR 0026's rule is unconditional, so a later attempt's success
  does not convert a stabilization into a resolution. `not_verified` is deliberately excluded: that
  attempt changed nothing, which is why `retry_second_hypothesis_succeeds` still resolves on its
  second attempt.
* **A cause is still asserted and has not been acted on.** Some hypothesis in the current ranking
  at or above `REMEDIATE_CONFIDENCE_THRESHOLD` (0.7 — the same number the loop uses to decide a
  hypothesis is actionable), other than any hypothesis an attempt in this run targeted.

"Acted on" is read off the attempt records, which now carry `target_hypothesis`, matched by
hypothesis NAME or category value because `RemediationPlan.target_hypothesis` is a free string and
the corpus spells it both ways. A cause the run has already fixed stays out of the gate however
confidently the ranking still scores it — otherwise a run that fixed both faults could never say it
had.

The gate is live whatever the alert names, unlike its dead-letter sibling. A second independent
fault is not something an alert can name, so scoping the question to a subjectless alert would make
it inert exactly where it is needed.

### 2. The diagnosed set: the top label plus every other cause asserted at the bar

`evals/graders/root_cause.py::diagnosis_set` is what `ROOT_CAUSE` scores: `hypotheses[0]`'s
category, plus the category of every other hypothesis in the final ranking at or above the same
0.7 bar. The arithmetic is unchanged — exact set is the pass condition, precision/recall/F1 are
reported partial credit.

The anti-hedging property survives because the bar is the one the loop acts on: a hypothesis below
it is a cause the agent is *considering* and costs nothing, while putting a second cause at or above
it is an assertion that costs precision when it is wrong. A run cannot pad the set for free, and
a wrong second cause at 0.7 turns an exact match into a 0.67 F1 red.

**Proven inert on the corpus that predates it:** no scenario shipped before WO-R3-228 ranks two
hypotheses at or above 0.7 in any single planner step, so `diagnosis_set` returns the same singleton
`final_diagnosis` did for all 53 of them. That is a test
(`tests/unit/test_policies.py::TestTheDiagnosisSetIsInertOnSingleFaultWorlds`), not a claim: it
scans every canned planner script, so a scenario that changes it fails there rather than moving a
grade quietly.

### What does not change

No gate is relaxed and no new autonomy is granted. `MAX_REMEDIATION_ATTEMPTS` is still 2, the
identical-attempt refusal, the subject guard, the read-before-act guards and the argument guards all
run on every attempt exactly as before, and the cap still ends a run that cannot converge. The gate
only ever moves a run from RESOLVED towards a human — never the other way.

## Considered alternatives

**Leave the loop alone and say it in the grading only.** The scenario's expectation can require both
actions, so a one-fix run reds. But the loop resolves after the first verified action, so the
*correct* trajectory is unreachable and the scenario grades nothing but its own impossibility. The
order's own finding said "the grading must forbid it"; the grading cannot conjure the second attempt.

**Ask "is another cause standing?" without the confidence bar** (any other ranked hypothesis with a
`FIX_MAP` category, which is what ADR 0056's third precondition already computes). Rejected: every
run ends with a runner-up on the ranking, so this escalates correctly-scoped single-fault runs —
`retry_second_hypothesis_succeeds` would stop at the cap instead of resolving. The bar is what turns
"considered" into "asserted".

**Read the diagnosis from the `StepRecord` stream or from the briefing's primary/secondary fields.**
That is WP-11.3's multi-incident representation and it is the better long-term home. It is not
available here: records reach the trace store only under `EVAL_TRACE_DIR`, which `make eval` does
not set, so a grader reading them could fail nothing in the offline suite (the same reason
WO-R3-191 refused them).

**Grade a multi-fault world on the top-N labels where N is the size of the ground truth.** Rejected:
the grader would be reading the answer key to decide how much of the answer to look at.

**Declare no `ground_truth` on the dual-fault templates until WP-11.3 lands.** Rejected: it would
delete the measurement the packet exists to make, and the schema would then also lose
`incident_count: 2`.

## Consequences

Positive:

* Level 5 is reachable: `dual_fault_dlq_and_consumer_lag` makes two Tier-1 remediations in one run,
  each verified on its own resource, and resolves; `dual_fault_consumer_lag_and_bad_deploy` fixes
  the one fault it can and escalates naming the other.
* "A partial action never ends resolved" is now structural for faults, not only for dead-letter
  rows, and the escalation names which cause is left and at what confidence.
* ADR 0026's rule holds across the retry edge instead of only within one attempt.
* The root-cause metric can express a multi-cause answer, so plan 03 § 7.1's set arithmetic has a
  real consumer and reward v0's set-F1 term (plan 03 § 12) is no longer measuring one label.

Negative:

* **The gate can escalate a run that would previously have resolved**, if the run asserts a second
  cause at the bar and cannot act on it. That is the intended behaviour and it is also a change in
  what "resolved" counts, so any future scenario whose planner ranks two causes high must mean it.
* One hole stays open and is named rather than hidden: an attempt that ended `verified_unresolved`
  with dead-letter rows still outstanding is not re-asked about those rows when a LATER attempt
  verifies, because the attempt record does not carry the rows it left behind. No scenario reaches
  it today (both templates keep the dead-letter side whole), and closing it means recording the
  unaddressed ids on the attempt record. Filed as a follow-up work order.
* The 0.7 bar is now load-bearing in three places (the remediate decision, this gate, the diagnosed
  set). It is one constant, `investigation.REMEDIATE_CONFIDENCE_THRESHOLD`, read by all three — but
  moving it now moves grades as well as behaviour.

## More information

* Implemented by WO-R3-228 (WP-11.1): `_unaddressed_second_cause`, `_standing_causes`,
  `_stabilized_earlier` and `target_hypothesis` on the attempt record in `agent/remediation.py`;
  `diagnosis_set` in `evals/graders/root_cause.py`, read by `_grade_root_cause`.
* Scenarios: `dual_fault_dlq_and_consumer_lag`, `dual_fault_consumer_lag_and_bad_deploy`, and the
  matrix in `evals/scenarios/README-multi-fault.md`.
* Both are CANNED. The live legs — and PROTOCOL step 5's fault-world content review run on the
  COMPOSED world rather than on each hook — are WP-11.4's and deferred.
