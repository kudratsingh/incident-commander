# ADR 0025: A verify leg must be able to observe the action

* Status: accepted
* Date: 2026-09-07
* Decider: Kudrat Singh

## Context and problem statement

[ADR 0024](0024-plan-arguments-name-their-resource.md) made a remediation plan name its resource on both legs, and added `_misdirected_verify_args`: when both legs name resources, the verify leg may not name one the action left alone. That check is **inert when either leg names no resource**, and 0024 recorded the inertness as a deliberate escape hatch, in as many words:

> the check is inert unless both legs name resources, so the escape hatch is a resource-free verify tool

It also named the case it meant to bless: "`invalidate_cache_key` verified by `get_redis_health`".

The hatch was too wide, and the paid run of 2026-09-07 (archive `7acd2b441961`) is what walked through it.

`remediate_stale_cache_success`. Chaos planted the 90-byte stale value. The agent probed the exact key with `get_cache_key_info` (`exists: true`), formed the right hypothesis, and invalidated exactly that key — `deleted: true`, the correct key, no collateral write. ARGUMENT, SAFETY, BUDGET, ACTION and EVIDENCE all green. Then the remediation planner chose `verify_tool = get_redis_health`.

`get_redis_health` returns **server-wide** counters. Nothing in the eval world reads `cache:jobs:worker-dispatcher:hot_set`, so the deletion could not move them, and the traffic that does move them belongs to everything else on the box:

| verify poll | `keyspace_hits` | `keyspace_misses` |
|---|---|---|
| 1 | 209 | 438376 |
| 2 | 209 | 439202 |
| 3 | 209 | 440022 |
| 4 | 209 | 440858 |
| 5 | 209 | 441659 |
| 6 | 209 | 442480 |

Hits frozen at exactly 209 for the entire window. The judge answered `not_verified` six times, each time correctly, and the agent escalated. **OUTCOME red, and the agent was right.** The plan asked a question the world could not answer.

Two things made this reachable rather than exotic:

* **The scenario's own description told it to.** *"verify probes get_redis_health again and expects the miss trend to reverse."*
* **So did the planner prompt.** *"Invalidate cache → verify with `get_redis_health` (miss rate should recover)."*

The agent followed its instructions exactly. This is the same family as the 2026-08-12 "the fault could not be manufactured" finding, one step later in the loop: there the scenario asserted a fault the world could not produce, here it asserted a *recovery* the world could not produce.

The probe that would have answered was already on the ledger. `get_cache_key_info` shipped in v0.6.0 (plat #146, tenant-scoped by plat #182) and its own registry comment says why:

> a suspect entry can be checked before remediation and confirmed gone after, instead of the agent inferring both from the deletion's own return value

The agent used it for the "before" and not the "after".

## Decision drivers

* **A verification that cannot fail is not a verification, and neither is one that cannot pass.** Both are the same defect — the probe's reading is independent of whether the fix worked — and only the second is loud. The first grades green.
* **`deleted: true` is the action's own testimony.** Invariant 6 says the audit log is ground truth precisely because a component's self-report is not. A verify leg exists to be the *independent* reading; one that observes a different system is a self-report with extra steps.
* **The failure is expensive and silent in the other direction.** This run failed honestly. The same hole with a *noisy* server-wide metric produces the opposite: a counter that happens to move for unrelated reasons, a judge that answers `verified`, and RESOLVED on an un-fixed system. We saw the benign polarity first by luck.
* **Architecture principle 3.** The prompt line was wrong and had to change, but a prompt fix alone leaves the class open — principle 1 in one line: an instruction is documentation, not enforcement.
* **Architecture principle 4.** The LLM was not wrong here in any respect. The design assumed a resource-free verify was always a legitimate choice. That assumption was the bug.

## Considered options

1. Fix the prompt line and the scenario description; leave the guards alone.
2. Require the verify leg to name a resource whenever the action does.
3. Map each Tier-1 action to the read tool that observes its resource, and refuse a verify leg that is not one of them (chosen).
4. Grade it only in the scenario — assert the post-remediation key state and let the corpus catch it.

## Decision outcome

**Option 3**, plus the prompt and scenario corrections from option 1 and the grading from option 4, which are complements rather than alternatives.

`VERIFY_PROBE_FOR_ACTION` (`agent/remediation.py`) maps a Tier-1 action to the read calls that can observe what it changes. `_unobserved_action_resource` refuses a plan whose verify leg is none of them.

| Action | Observed by |
|---|---|
| `invalidate_cache_key.key` | `get_cache_key_info.key` |
| `restart_consumer_group.consumer_group` | `get_consumer_lag.consumer_group` |
| `pause_dag.root_job_id` | `get_dag_state.job_id` |
| `replay_dlq_by_ids.job_ids` | `list_dlq_messages`, or `get_dag_state.job_id` when the id is a DAG root |
| `mark_dlq_permanent.job_id` | `list_dlq_messages`, or `get_dag_state.job_id` |
| `replay_dlq_by_category` | *(declared inert — names a category, not a resource)* |
| `replay_dlq_messages` | *(declared inert — same)* |

Three properties carry the design.

**It is the complement of `_misdirected_verify_args`, not a replacement.** That guard asks "does the verify leg name a resource the action did NOT touch?"; this one asks "is there a read that observes what the action DID touch, and did the plan use it?". The first is inert when the verify leg names nothing; the second is what that inertness leaves open. Together: verify the right resource, and verify it at all. 0024's check is unchanged and still runs first.

**It refuses; it does not escalate.** Same shape as `ALERT_SUBJECT_PROBES` in the investigation loop. The three argument guards escalate because a mis-named resource means the planner is reasoning about the wrong object and the run has nothing to salvage. Here the action is correct and only the evidence for it is missing — naming the probe is usually enough. A `_plan_refused` marker is appended, the planner is asked again with the required call spelled out, and a second refusal escalates naming the gap. The marker is underscore-prefixed, so it stays out of the briefing trail and the grader's called-tools set, and it spends no tool-call budget; the re-ask's planner tokens are charged like any other call (ADR 0015).

The steer is rendered **whole** into the next planner context, in its own trailing block, rather than through the evidence dump — which truncates at 200 characters, shorter than a refusal naming a probe, an argument and a cache key. A steer that arrives cut mid-sentence is not a steer.

**The map is total over the Tier-1 slice.** An empty tuple is a *declared* inert entry; an absent one would be a silent hole. `tests/unit/test_policies.py::TestVerifyProbeForAction` fails on any Tier-1 tool without an entry, with a message saying which decision is missing — the same shape as `TestResourceArgFieldsCoverage`, for the reason 0024 already gave: "the narrowness is a trap, not a comfort."

### The prompt and the scenario

Both said the wrong thing, and both are the reason the planner chose as it did. The prompt now carries a rule under an invariant test — *"verify by re-reading the resource you acted on; a server-wide health number is not evidence about one key, group or job"* — and `Invalidate cache → verify with get_cache_key_info` in place of the inverted line. `remediate_stale_cache_success` verifies with `get_cache_key_info(key)` expecting `exists == false`, records the post-delete world in its fixture honestly, and states in the YAML that nothing in this environment repopulates the key — which is exactly why absence is the right signal and a recovering hit rate is not.

The scenario also **grades the verify signal**, which it never did: the old verify leg produced nothing assertable, so the run that surfaced this finding was green on EVIDENCE. `which: last`, because `get_cache_key_info` is read before and after the deletion and `any` would be satisfied by the investigation probe alone — that is, by a run that never remediated.

### Why the alternatives lose

**Prompt and scenario only.** They are necessary and they are not sufficient. This is principle 3's exact case: the prompt line was the proximate cause, so fixing it feels like fixing the bug, but the next unobservable verify leg will be one nobody wrote a line about. The prompt is now the steering half of a structural rule rather than the rule itself.

**Require the verify leg to name a resource whenever the action does.** Simpler, and wrong in both directions. It refuses `replay_dlq_by_ids` verified by `list_dlq_messages` — correct today, and the platform's only way to observe DLQ rows — while admitting `invalidate_cache_key` verified by `get_dag_state(job_id=<the key>)`, which names a resource and observes nothing. Naming is not observing; the map is what knows the difference.

**Grade it in the scenario only.** The corpus catches what the corpus covers. This exact plan was legal for every scenario and for production traffic, where there is no expectation file at all. Scenario grading is how we *notice*; the guard is how the agent stops doing it. Both shipped.

### Consequences

Positive:

* A plan that cannot observe its own effect is unreachable, and the refusal names the call that would.
* Refusal is pre-execution: no Tier-1 side effect, no misleading ledger entry, `remediation_attempts` still 0, and the incident is left recoverable rather than half-remediated.
* The first refusal costs one planner call and usually fixes itself, so the common case is a correction rather than an escalation a human has to read.
* The two verification polarities are now both closed: 0024 stopped a probe reading a healthy bystander and resolving; this stops a probe reading a signal that cannot move at all.

Negative:

* **A correct plan verified through an unmapped-but-valid read is refused.** If a future read tool observes a cache key better than `get_cache_key_info`, it is refused until it is in the map. Accepted: the map is one line, the coverage test names it, and the alternative is the hole this ADR closes. Mitigation: the refusal says exactly which probe to use.
* **The map is a claim about the platform that can go stale.** A retiered or renamed tool would have the guard demanding a call nobody can make. `TestVerifyProbeForAction` cross-checks every entry against the registry, the read tier and `RESOURCE_ARG_FIELDS`, which is the same protection `ALERT_SUBJECT_PROBES` has.
* **PLANNING can now make two LLM calls.** Bounded at one re-ask, budget-gated before it (an exhausted ledger escalates instead), and charged; but the transition is no longer strictly single-call, which the budget profile in the live runbook should keep in mind.
* **`list_dlq_messages` is accepted on tool identity alone**, because it takes no row argument — so a DLQ replay is verified more weakly than a cache invalidation. That is the platform's observability, not a choice made here. Revisit if a per-row read ships.

Revisit trigger: a Tier-1 action whose only honest verification is a *related* resource rather than its own — at which point the map needs a declared relation rather than a value match, the same trigger 0024 recorded for `_misdirected_verify_args`.

## More information

Implemented in `src/incident_commander/agent/remediation.py` (`VERIFY_PROBE_FOR_ACTION`, `VerifyProbe`, `_unobserved_action_resource`, `_refuse_plan`, `_probe_options`). Tests: `tests/unit/test_remediation.py::TestVerifyLegObservesTheAction`, `tests/unit/test_policies.py::TestVerifyProbeForAction`, `tests/unit/test_prompts_snapshot.py::TestRemediationPlannerInvariants`, `tests/unit/test_grader.py::TestStaleCacheGradesWhichKeyWasDeleted`.

Extends [ADR 0024](0024-plan-arguments-name-their-resource.md) — that ADR stands; the escape-hatch consequence it recorded is narrowed here. Related: [ADR 0008](0008-single-attempt-remediation.md) (one attempt, so the single verification must be able to succeed), [ADR 0006](0006-verification-is-a-polling-window.md) (polling cannot rescue a signal that will never move — six polls of a frozen counter is what that looks like), [docs/lessons/live-eval-sequence-2026-09.md §7](../lessons/live-eval-sequence-2026-09.md), [docs/safety-model.md](../safety-model.md#the-verify-leg-must-observe-the-action).
