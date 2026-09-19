# ADR 0067: A cascade is one incident, its premises are ordered, and one link cannot be read

* Status: accepted
* Date: 2026-09-19
* Decider: Kudrat Singh (WO-R3-229, WP-11.2)
* Related: [ADR 0059](0059-a-second-fault-is-not-resolved-by-the-first-fix.md) (the diagnosed
  SET, which a cascade reads in the opposite direction), [ADR 0033](0033-a-human-required-chain-root-is-fenced-then-escalated.md)
  (the forbidden set derives from the sanctioned action — here, none),
  [ADR 0037](0037-a-scenarios-fault-is-a-plan-and-the-plan-is-put-back.md) (the plan form and its
  settle wait), [ADR 0038](0038-the-agents-view-of-a-scenario-is-an-allow-list-projection.md) (the chain is the
  evaluator's record), [ADR 0065](0065-a-briefing-names-its-remainder-in-a-slot-not-in-its-prose.md)
  (the briefing's primary slot, which is what a chain claim can actually land on), plan 01 § 8,
  plan 00 § 4's capability level 6

## Context

Plan 01 § 8 specifies the cascading world in one sentence and a warning: "Redis degraded → lag
telemetry stale → backpressure weakened → API admits more → Kafka backlog grows. **Not 'two hooks at
once': preconditions must verify the intermediate states in order.** Ground truth names the root
(`redis_saturation`) and the chain."

The chain is real in this platform, not a figure of speech. The worker's metrics loop measures
`worker-dispatcher`'s consumer lag every ~60s and caches it in Redis under a 90s TTL; `POST /jobs`
reads exactly that cached entry through `check_backpressure`, which **fails open three ways** —
absent or expired key, unparseable value, failed read. And a lag the loop cannot determine is not
written as a null: the write is SKIPPED, so the previous entry ages out and then is simply gone
(platform `dispatcher.py`, `app/utils/backpressure.py`). A Redis that cannot serve that write
therefore removes the only throttle on admission, and the dispatch queue grows behind it.

Three things about building a benchmark world on that chain forced decisions.

**One incident, or four?** Every link is a true statement about something wrong with the platform.
Three of them are consequences.

**Which links can a precondition assert?** The suite's rule is that a scenario's premise is proven
before any model call and the run is abandoned if it is false (WO-R3-184). A cascade has four
premises, and one of them — the admission throttle no longer refusing — has **no agent-visible
reading at all**. Nothing on the tool surface reports the admission decision, the threshold it
compares against, the value it last read, or which fail-open branch fired.

**Can the fault be seeded?** `saturate_redis` writes at most 256 MiB of keys. The eval stack's Redis
(`demo/compose.yml`, `redis:7-alpine`, no config) has no `maxmemory`, so nothing is evicted and no
write fails. The mechanism the chain runs through needs a memory ceiling to reach.

## Decision

### 1. A cascade is ONE incident whose root is the whole answer

`ground_truth.incident_count: 1` with `root_causes: [redis_saturation]`, and the four links in
`causal_chain`. This is the opposite reading of ADR 0059's dual-fault worlds, on purpose: there, a
run that named one of two causes was half right and graded red on the SET; here, a run that names
the root **and** the symptom is also red on the set, because the symptom is not a cause of this
incident. `diagnosis_set` needs no change — the arithmetic is the same and the world is different,
which is what a ground-truth label is for.

`causal_chain` is recorded and **not graded**. It is prose about a mechanism, and there is no honest
way to score a free-text chain against a ranking of labels; what it does is make the world reviewable
and give the export a faithful record ([ADR 0038](0038-the-agents-view-of-a-scenario-is-an-allow-list-projection.md) keeps both
evaluator-only).

### 2. The order of the preconditions is the claim, and the runner already enforces it

One probe per observable link, declared in chain order, each asserting its OWN link:

| link | state | reading | precondition |
|---|---|---|---|
| 1 | Redis is the constraint (the root) | `get_redis_health` | `used_memory_bytes at_least 100 MB`, `ok equals true` |
| 2 | the cached lag measurement is not being refreshed | `get_consumer_lag(worker-dispatcher)` | `source equals live`, `lag_known equals false`, polled 10 × 15s |
| 3 | the admission throttle stops refusing | **no reading exists** | none — see § 3 |
| 4 | the queue grows and the dispatch objective burns | `get_slo_status` | dispatch objective `healthy equals false` with `total at_least 20`, polled 10 × 15s |

`evals/runner.py::_assert_preconditions` iterates the declared tuple and raises on the first unmet
probe, before any model call. That is exactly the semantics a cascade needs, so **no code changed** —
what changed is that the order now carries meaning, and a test pins it: a world where link 1 and link
4 hold while link 2 does not is abandoned naming link 2, and link 4 is never read. A later link
passing cannot rescue an earlier one, because the same three readings in any other order are three
facts about a platform rather than a chain, and grading an agent in that world would credit or blame
it for a chain the world never had.

The corollary is that **no probe may assert a link another probe already read**. A second look at the
cached lag standing in for link 3 would be the cross-satisfiable premise `PreconditionField.where`
exists to prevent, one level up: two assertions, one underlying fact, and a premise that reads
stronger than it is.

### 3. A link nobody can read is a stated gap, not a probe

Link 3 gets no precondition and no evidence claim. It is named in the scenario header, in
`evals/scenarios/README-multi-fault.md` (with the platform reading that would close it) and in the
work order's report. The alternative — inferring it from link 2, which is its input — would assert
the mechanism rather than observe it.

This also bounds what the scenario grades about the ROOT's own reading. `get_redis_health` reports
`used_memory_bytes` and no ceiling, no eviction policy and no evicted-key count, so "Redis is at its
limit" is an inference from a number with no yardstick beside it. The precondition asserts the
footprint the hook writes (evaluator-side, where the ceiling is known); the agent's own inference
rests on memory, ping latency and the hit rate, as `redis_saturation` already does.

### 4. The handoff's checkable claim is the primary slot, not the chain

`expect_briefing_contains: ["PRIMARY: redis_saturation"]`. ADR 0065's slot block is rendered from the
run's own final ranking, so the claim is satisfied by what the agent asserted rather than by the
writer's prose, and a run that ranks the symptom on top fails it.

"The briefing explains the chain" is deliberately NOT a deterministic claim. The briefing corpus
contains every reading verbatim (the trail renders each tool's own JSON), so any token drawn from a
reading is satisfied by having READ it — a claim on the cache key or on the objective's name would go
green on an agent that never connected them. What remains is the writer's phrasing, which
`docs/eval-methodology.md` forbids asserting on. So the chain-naming requirement is carried by the
canned writer (pinned in `tests/unit/test_cascading.py`) and by the briefing judge, and making it
structural would mean a chain slot on the briefing — a commander change this packet reports rather
than makes.

### 5. The template ships canned, and the blocker is named

`use_live_mcp: false`, with the reason in the file: the hook cannot degrade a Redis that has no
memory ceiling. The live leg needs one on the eval instance (`docs/REDIS.md` names `noeviction` as
the intended posture, which makes the mechanism an OOM on write rather than an eviction), and that is
a shared-stack decision with a blast radius over every other scenario on that instance. The
`chaos_plan` and the preconditions are written and argument-checked anyway, so the live leg is a
decision plus a reset away rather than a rebuild.

## Considered alternatives

**Seed the intermediate states with a second hook** — `pause_control_loop('metrics')` stops the lag
cache being refreshed today, no memory ceiling required. Rejected: plan 01 § 8 forbids exactly this
("not two hooks at once"), and it would move the root. A world where the telemetry loop is paused has
`redis_saturation` nowhere in it, and the agent would be graded for following a chain the harness
built rather than one the platform produced. Recorded here because it is the obvious shortcut and the
next builder will think of it.

**Precondition link 3 on the cached lag it reads.** Rejected in § 2: it asserts link 2 twice and
reports a premise the world never showed.

**Grade the `causal_chain` against the agent's reasoning text.** Rejected: an LLM judge over a
free-text chain is exactly the soft grading the methodology confines to the judge's own dimensions,
and a deterministic substring match on a mechanism is phrasing.

**Call the world `multi_fault` and reuse the dual-fault README.** Rejected: `multi_fault` means
several independent faults, and "a cascade is one incident with several causes" is already
`GroundTruth`'s own wording. The `cascading` rung is plan 03 § 3's and this is its first occupant;
the evidence matrix joins the composed-world README beside the dual-fault one.

**Give the cascade its own family.** Rejected for now: the family is the observable SYMPTOM, and a
one-member family puts a name in a closed enum ahead of the worlds that fill it (the enum's own
rule). The world sits in `cache_redis` beside `redis_saturation` — the same root read two ways, seen
directly there and through what it starves here — and the sentence saying so is in the scenario and
in the README.

## Consequences

Positive:

* The corpus has a level-6 world, and its premise is a chain rather than a coincidence: the ordered
  probes make "this world is the cascade" a checkable statement, and an unmet link names itself.
* The failure mode plan 01 § 8 and LESSONS 2026-09-17 both warn about is graded from three directions
  at once — the root alone in `diagnosis_set`, all seven Tier-1 tools forbidden, and the handoff's
  primary slot — so treating the symptom cannot be green.
* Two missing platform readings are now written down with the scenario that needs them, rather than
  discovered during a paid run: the admission throttle's own state, and Redis's ceiling / eviction
  counters.

Negative:

* **A four-link chain ships with three probes.** The gap is stated, not closed, and until the
  platform reading exists no run can prove link 3 happened — the scenario's own header says so.
* **No live leg, and the blocker is in the shared stack.** The ledgered fixture drift is
  `canned-only` for that reason, and a reader of the ledger will see ten rows whose mechanism is "the
  chain cannot be seeded here yet".
* **The fixtures are the premise.** Every number in the SLO reading is the platform's own arithmetic
  at counts nobody has observed, so the first live leg may move them — which is what the drift ledger
  is for, and the rows say so.
