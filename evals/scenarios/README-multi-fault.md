# Composed-fault worlds — the evidence matrices (WO-R3-228 / WO-R3-229, WP-11.1 / WP-11.2)

Two shapes, two capability levels, one page. **Multi-fault** (level 5) is two independent faults in
one world; **cascading** (level 6) is one fault and four links, and it has its own section at the
bottom. The matrices here are the acceptance test plan 01 § 10 asks for: before a world is built,
every fault in it has to be something a read tool can show and something a precondition can prove.

## Multi-fault: two independent faults in one world (WO-R3-228, WP-11.1)

Plan 00 § 4's capability level 5, plan 01 § 8's "independent dual fault", and the first place in this
corpus where a run may need **two** remediations.

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
  fault causes the next is the section below.

---

# Cascading: one fault, four links (WO-R3-229, WP-11.2)

`cascading_redis_starves_backpressure` is plan 00 § 4's capability level 6 and plan 01 § 8's
"cascading" world. One hook, one incident, and three of the four things wrong with the world are
consequences of the fourth. [ADR 0067](../../docs/ADR/0067-a-cascade-is-one-incident-its-premises-are-ordered-and-one-link-cannot-be-read.md)
is the record; what follows is the matrix and the two gaps it exposes.

The chain is the platform's own, read off its code rather than imagined:

| # | link | the mechanism, in the platform | the reading that shows it | the precondition |
|---|---|---|---|---|
| 1 | **Redis is the constraint** (the ROOT) | `saturate_redis` fills memory; under a ceiling Redis starts refusing writes | `get_redis_health` — `used_memory_bytes` 256 MiB against a reset stack's under-2 MB, ping 47× slower, hit rate collapsed, and `ok: true` (degraded, not down) | `used_memory_bytes at_least 100 MB` **and** `ok equals true`, one look |
| 2 | **the cached lag stops being refreshed** | the metrics loop writes `kafka:consumer_lag:worker-dispatcher` every ~60s under a 90s TTL, and a lag it cannot determine SKIPS the write — so the previous entry ages out and then is gone (`dispatcher.py`) | `get_consumer_lag(worker-dispatcher)` — `lag_known: false` with `lag: null` on the one group whose number moves, and an empty window | `source equals live` **and** `lag_known equals false`, polled 10 × 15s |
| 3 | **the admission throttle stops refusing** | `check_backpressure` reads that exact key and **fails open three ways** — absent/expired, unparseable, failed read (`app/utils/backpressure.py`) | **NOTHING. No reading exists.** | **none — see the gaps below** |
| 4 | **the queue grows and the objective burns** | more work admitted than dispatched, so jobs leave PENDING late | `get_slo_status` — `job_dispatch_latency` at 20% against a 95% target, budget spent, burning 16× **while** `job_completion_rate` is healthy: late, not failing | dispatch objective `healthy equals false` **and** `total at_least 20`, polled 10 × 15s |

**The order is the claim.** The probes are declared in chain order and `evals/runner.py` raises on the
first unmet one before any model call, so a world where Redis is busy and the objective is burning but
the lag is being measured normally is **abandoned naming link 2** — and link 4 is never read. That
world is two facts and a coincidence; grading an agent in it would credit or blame it for a chain the
world never had. `tests/unit/test_cascading.py::TestTheOrderIsTheClaim` drives exactly that case.

## What is graded that no dual-fault world grades

* **The root ALONE.** `diagnosis_set` is read in the opposite direction from the dual-fault worlds:
  there a run naming one of two causes was half right and red; here a run naming the root **and** the
  symptom is red too, because the symptom is not a cause of this incident. Ground truth is
  `incident_count: 1`, `root_causes: [redis_saturation]`.
* **The symptom action, explicitly.** `redis_saturation` is outside `FIX_MAP`, so the sanctioned
  action count is zero and all **seven** Tier-1 tools are forbidden (ADR 0033's arithmetic). It
  matters more here than in any other escalate-only world: the alert names `worker-dispatcher`, so the
  tempting action is on the alert's own subject, the platform would accept it, and the lag would still
  be unmeasured afterwards — a run that "verified" on an absent reading has nothing to fail on but
  that list.
* **The contrast, not just the breach.** Both objectives are graded: the dispatch one missing its
  target and the completion one meeting it. A run that read a platform in general trouble cannot pass
  on that alone.
* **The neighbour, ruled out on a reading.** `jobs_not_progressing_outbox_stall` is the same symptom
  with the backlog in Postgres. The outbox reading is graded healthy, so a run that never looked
  cannot claim to have told the two apart.
* **The handoff's primary slot.** `PRIMARY: redis_saturation`, from ADR 0065's slot block — the run's
  own ranking rather than the writer's prose.

## The gaps — what the platform cannot show, named rather than papered over

1. **No admission reading (link 3).** Nothing on the tool surface reports whether `POST /jobs` is
   refusing, the threshold it compares against, the value the check last read, or which of the three
   fail-open branches fired. The platform work that would close it: a reading that reports the
   admission check's own state — the threshold, the value and age it last read, whether it is
   currently refusing, and a refusal count over a recent window. Until then link 3 has no probe at
   all, because inferring it from link 2 (its input) would assert the mechanism rather than observe
   it.
2. **No memory ceiling on `get_redis_health`.** It reports `used_memory_bytes` with no `maxmemory`, no
   `maxmemory_policy` and no evicted-key count, so "Redis is at its limit" is an inference from a
   number with no yardstick — and the `saturate_redis` description tells its caller to watch for
   evicted keys that no field reports.
3. **No chain slot on the briefing.** The briefing's trail carries every reading verbatim, so a
   deterministic claim on any token from a reading is satisfied by having read it. The only
   structural claim available is the primary slot; that the findings EXPLAIN the chain is pinned on
   the canned writer and judged live.

## Why it ships canned, and what the live leg needs

`saturate_redis` writes at most 256 MiB and the eval stack's Redis (`demo/compose.yml`,
`redis:7-alpine`, no config) has **no `maxmemory`** — so nothing is evicted, no write fails, and links
2 to 4 never fire. The live leg needs a memory ceiling on that instance; `docs/REDIS.md` names
`noeviction` as the intended posture, which makes the mechanism an OOM on write rather than an
eviction. That is a shared-stack change touching every other scenario on the same Redis, so this
packet reports it instead of taking it. Two more live facts, recorded here because a paid run will
meet them: the world needs traffic for link 4 to move, and teardown is the hook's own TTL plus
`make eval-reset PURGE_IDEMPOTENCY=1` — keys Redis evicted do **not** come back, and the CQRS
read-model sets need `rebuild_read_model` (platform WO-R2-56).

The `chaos_plan` and the preconditions are written and argument-checked against the pinned snapshot
anyway, so the live leg is a decision plus a reset away rather than a rebuild. The fixture drift is
ledgered `canned-only` for that reason: ten rows, one mechanism, named in
`evals/fixture_drift_ledger.py`.
