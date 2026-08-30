# ADR 0022: Connection-pool sizing and the run-concurrency ceiling

* Status: accepted
* Date: 2026-08-30
* Decider: Kudrat Singh
* Amends: [ADR 0016](0016-incident-identity-and-single-flight.md) — adds the resource accounting
  0016 never specified. It does **not** revise 0016's decisions; the lease mechanism, the identity
  derivation, and the resume rules all stand exactly as accepted.

## Context and problem statement

ADR 0016 reasoned carefully about lock lifetime versus *connection* lifetime — it is one of its
named decision drivers — and concluded that a run's lifetime "is therefore exactly expressible as
one connection checkout." That is true, and it is the design. What 0016 never asked is the next
question: **how many of those checkouts can exist at once, and how big is the thing they are
checked out from?**

Nobody had answered it, because nobody had chosen the pool. `api/app.py` built its engine with
`create_engine(str(settings.database_url))` and no pool arguments at all, which means SQLAlchemy's
defaults: `QueuePool`, `pool_size=5`, `max_overflow=10`, `pool_timeout=30`. Fifteen connections
and a thirty-second wait, selected by a library author who knew nothing about this workload.

Put the two together and the arithmetic is bleak:

* A run pins **one** connection for its entire life. Not for a query — for the run, bounded only
  by `BUDGET_MAX_SECONDS`, which defaults to 1800.
* That same run needs **a second** connection every time it checkpoints, and it checkpoints after
  every state transition.
* So at fifteen concurrent incidents, fifteen lease connections hold the entire pool, and every
  one of those runs then blocks for thirty seconds trying to check out a connection that only
  another lease holder could release — and none of them will, because releasing it is precisely
  what they are all waiting to be able to do.

That is hold-and-wait with no preemption: a deadlock in the textbook sense, reached with a
perfectly healthy Postgres, an idle CPU, and no bug in any query. The crash rail that exists to
record the failure (`_record_run_failure`) needs a connection too, so the incident that wedges the
agent is also the one least likely to leave an explanation behind.

The verifier found the failure worse than "runs stall", and the second half is why this ADR covers
the API layer as well. `ingest_alert` is `async def` but was calling synchronous Postgres directly
on the event loop — `derive_incident_id`'s generation walk (up to 64 indexed loads) and the ingress
checkpoint. One thread runs every coroutine in the process. So during pool exhaustion each of those
calls parks the *entire event loop* for the pool timeout, and `/health` — which has nothing to do
with Postgres — stops answering. An agent that is merely waiting on its database reports as dead,
and whatever watches `/health` restarts it, destroying the in-flight runs whose pinned connections
were the only thing worth preserving. The signal inverts exactly when it is needed.

## Decision drivers

* **The lease is not the bug.** ADR 0016 chose the advisory lock deliberately, over a lease table,
  with pre-authorized switch triggers. None of those triggers has fired. Holding a connection for
  the run is the design working as specified; what was missing was any accounting for what that
  costs. This ADR does the accounting.
* **A bigger pool only moves the cliff.** Any fixed pool has a concurrency at which this recurs.
  Sizing without bounding is not a fix, it is a larger number to be surprised by later.
* **Invariant 5 decides the overload behavior, not throughput.** CLAUDE.md: the agent augments the
  incident response path and never gates it. Whatever happens above capacity must not make alert
  delivery look failed and must not make a human page wait.
* **The failure must be loud.** An agent silently declining to investigate is indistinguishable
  from an agent that investigated and found nothing — the worst possible ambiguity during an
  incident.
* **Misconfiguration must not be able to reintroduce the deadlock.** If the ceiling is a free knob,
  the first operator who raises it during a busy hour rebuilds the bug. Per
  [architecture principle 3](../architecture-principles.md), the structural fix beats documenting
  the constraint.
* **`evals/runner.py` must not move.** It never goes through the ingress path and takes no lease
  ([ADR 0011](0011-campaign-eval-freeze.md) freezes the suite), so admission must live on the
  ingress/background-task path only.

## Considered options

**Bounding concurrency**

1. Do nothing; raise `pool_size` (status quo plus a bigger number).
2. A run-admission semaphore whose ceiling is *derived* from the pool size (**chosen**).
3. A free-standing `AGENT_MAX_CONCURRENT_RUNS` knob, independent of the pool.
4. A work queue with N worker threads, runs queued when all workers are busy.

**Behavior above the bound**

5. Shed: log at WARNING, leave the alert at its durable TRIAGE checkpoint, do not investigate
   (**chosen**).
6. Queue the run until a slot frees.
7. Return 503 from `/alerts` so the platform emitter retries.
8. Block on the semaphore inside the background task.

**Checkpoint write cost**

9. Read the version and INSERT in one transaction on one connection (**chosen**).
10. Leave the two sequential checkouts as they are.

## Decision outcome

Options 2, 5, and 9.

### The pool is stated, and the ceiling is derived from it

`create_pooled_engine` (`src/incident_commander/persistence/pool.py`) states every pool parameter
rather than inheriting one. `pool_pre_ping` is on because a lease connection can sit idle for the
length of an investigation — ample time for a middlebox or a Postgres restart to kill it — and
without the ping the run discovers that at its next checkpoint, as a crash.

The ceiling is arithmetic, not judgement:

```
capacity          = DB_POOL_SIZE + DB_MAX_OVERFLOW              = 10 + 10 = 20
for_runs          = capacity - DB_INGEST_RESERVED_CONNECTIONS   = 20 -  4 = 16
max concurrent    = for_runs // connections-per-run             = 16 //  2 =  8
```

**Connections per run is 2**, and that number is load-bearing enough to be a named constant in
`config.py` with the ceiling computed from it: one pinned lease connection, plus at most one
transient checkout for a checkpoint load or write. It is 2 rather than 3 only because of option 9
below, and `tests/integration/test_pool_capacity.py` counts pool checkout events during a write to
keep it honest — if a future change makes a write take two connections again, the constant becomes
a lie and that test fails rather than the ceiling silently becoming unsafe.

The reservation of 4 covers the work that is *not* a run: ingress identity derivation and its
conditional checkpoint write, plus the crash rail. Those are deliberately treated as headroom
rather than bounded, and the distinction is the point of the whole ADR: **short-lived checkouts
that are always released cause contention, never deadlock.** Only the lease holds a connection
across an unbounded wait, so only runs need a hard bound. Ingest under contention waits at most
`DB_POOL_TIMEOUT_SECONDS` and then takes the fail-open path that already exists.

At the chosen numbers the worst case fits with the reservation intact: 8 runs × 2 connections = 16,
plus 4 reserved = 20 = capacity. Every admitted run can hold its lease *and* write a checkpoint
simultaneously without touching the reservation. `tests/unit/test_run_admission.py` asserts that
inequality directly rather than asserting the number 8, so re-tuning the knobs cannot quietly break
the property.

`DB_POOL_TIMEOUT_SECONDS` drops from SQLAlchemy's implicit 30 to 10. With admission bounded, a
checkout that waits at all means genuine contention rather than deadlock, and 30 seconds of it is
three budget-minutes of an incident spent in a queue.

### `AGENT_MAX_CONCURRENT_RUNS` may only lower the ceiling

The knob exists — there are good reasons to want fewer concurrent runs than the pool allows, all of
them about LLM spend rather than connections — but a `model_validator` on `Settings` refuses at
construction any value above the derived ceiling, and refuses any pool too small to serve a single
run. The process does not start. This is deliberate: the alternative failure mode is a
ten-second stall under load during an incident, which is the failure this ADR removes, arriving
later and disguised as something else. Startup is the only honest time to find out, and the error
names the knobs that fix it.

### Above the bound, the run is shed — logged, recorded, not investigated

Invariant 5 forces this. The delivery was already acknowledged 202 and the alert is already durably
recorded at TRIAGE by the ingress write, so the platform pages a human off that alert whether or
not this agent ever looks at it. Overload therefore degrades the agent, never the page.

The alert does not vanish. It leaves two marks: a WARNING naming the ceiling and the incident id,
and a TRIAGE-state run that never advances — the same visible residue the kill switch leaves, and
found the same way. Deliberately no *extra* checkpoint write records the shed: that would mean
asking for a connection at the exact moment connections are the scarce resource, adding load to the
overload path in order to describe it.

Admission is taken **before** the lease, and both are held for the whole run. The order is not
incidental — taking the lease is what pins the connection, so a run that is going to be refused has
to be refused before it holds one. A bound applied after the lease would be a bound on nothing.

### Synchronous DB calls move off the event loop

`derive_incident_id` and the ingress checkpoint are wrapped in `run_in_threadpool`. They are still
synchronous and still take pooled connections; they simply no longer do it on the one thread that
answers `/health`. This is the narrow, complete fix for the finding as filed. It is not a general
async rework of the persistence layer — see below.

### Checkpoint writes take one connection

`PostgresCheckpointer.write` used to read the next version on its own connection, return it, and
then take a second one for the INSERT. Two checkouts and a window between them in which another
writer could claim the version just read. Both now happen inside one `begin()`. This halves the
checkout traffic of a run that is already holding a lease connection, closes the race, and is what
makes connections-per-run 2.

Worth stating precisely, because the finding described it imprecisely: the two checkouts were
**sequential, not simultaneous** — the old code never held two connections at once for a single
write. The deadlock claim survives the correction intact, because the fatal pairing was never
"write takes two" but "lease holds one *while* write needs one".

## Why the alternatives lose

**Raise `pool_size` and stop (option 1).** Buys a larger N at which the identical deadlock occurs,
and buys it invisibly. The problem is unbounded hold-and-wait, and no pool size bounds it.

**A free-standing concurrency knob (option 3).** Two numbers that must agree, with nothing checking
that they do — architecture principle 2 names this exact shape as a bug source. The deadlock would
return the first time someone raised one without the other, during the busy hour that motivated the
change.

**Queue the excess (options 4, 6, 8).** A slot is held for up to `BUDGET_MAX_SECONDS`, so queueing
means an alert waiting up to half an hour to be *looked at* — operationally indistinguishable from
dropping it, except that nothing says so, and by the time the run starts its evidence is stale and
its wall-clock budget is spent. Option 8 is worse still: background tasks run in Starlette's
threadpool, so blocking on the semaphore consumes a threadpool worker per waiting alert, and the
thing that eventually starves is the pool that serves `/health`. Refusing immediately is the honest
answer and the safe one.

**503 from `/alerts` (option 7).** The most tempting, and it inverts invariant 5. The platform
emitter treats anything >= 400 as a failed delivery and retries, so an agent at capacity would be
answered with *more* traffic — a retry storm aimed at the component least able to absorb it, which
is how a busy agent becomes a down one. It also makes the agent's saturation visible in the
platform's delivery-failure metrics as if the platform were broken.

**Leave the two-checkout write (option 10).** Makes connections-per-run 3, cutting the ceiling from
8 to 5 for the same pool, and leaves the version-read race open for no benefit.

## Consequences

Positive:

* The deadlock is unreachable by construction rather than unlikely: admitted runs can never
  collectively demand more connections than the pool holds, and the arithmetic is enforced at
  startup rather than documented.
* The pool has chosen numbers with a written rationale, so the next person changing them can see
  what the ceiling is derived from and what breaks.
* A slow database no longer stops `/health`, so the liveness signal reports on liveness.
* One less connection checkout per checkpoint, and the version-read race is closed.

Negative:

* **Throughput is now explicitly capped at 8 concurrent incidents per process**, where before it
  was nominally unbounded — though "unbounded" meant "deadlocks at 15", so the real capacity went
  up, not down. Above 8, alerts are recorded but not investigated. Mitigation: the WARNING names
  the ceiling and the levers; raising `DB_POOL_SIZE`/`DB_MAX_OVERFLOW` lifts it, and horizontal
  replicas multiply it (the lease is per-database, so it already fences across processes).
* **Shedding is a real reduction in service**, chosen over the alternatives, not over doing better.
  A dropped-on-the-floor investigation is only acceptable because invariant 5 guarantees the human
  page is not on this path.
* **The ceiling is per process, the pool is per process, and Postgres' `max_connections` is not.**
  Eight replicas at these defaults is 160 connections. Nothing here checks that against the server,
  and the next person to add replicas has to. Called out in the runbook.
* **`run_in_threadpool` moves the blocking rather than removing it.** The DB calls are still
  synchronous and now occupy anyio's threadpool workers (40 by default). That is the right trade —
  a blocked worker is not a blocked event loop — but it is a bound worth knowing about, and it is
  why the pool timeout came down to 10s.
* **The full async persistence rework is deliberately not done here.** It means an async engine, an
  async checkpointer, an async lease whose advisory lock still has to stay pinned to one connection
  across awaits, and re-verifying every ADR 0016 guarantee on top of it — a change to the durability
  substrate, and much too large to ride along with a deadlock fix. `run_in_threadpool` addresses the
  filed finding (the event loop stalls) completely. Revisit when the persistence layer goes async
  for its own reasons, which is also ADR 0016's third lease-table switch trigger, so the two should
  be reconsidered together.

Revisit trigger: any of ADR 0016's four lease-table switch conditions firing (a lease table changes
connections-per-run to 1 and this whole ceiling relaxes); the run loop becoming async; adding
replicas without checking Postgres `max_connections`; or the WARNING appearing in normal operation,
which means the ceiling is genuinely too low rather than protective.

## More information

* Amends [ADR 0016](0016-incident-identity-and-single-flight.md), which chose the lease and
  reasoned about lock-vs-connection lifetime but never about pool size. 0016 is not superseded and
  none of its decisions change. Its "when to switch to the lease table" conditions remain the
  governing test for replacing the mechanism; this ADR's changes are all *around* the lease and
  none of them meets one.
* Implements the resource half of the finding pair in `docs/04`: the lease pinning a connection for
  the run (`persistence/lease.py`), and synchronous Postgres on the async ingress path
  (`api/app.py`).
* Operator documentation: `docs/runbook.md` ("Connection pool and run capacity"),
  `docs/safety-model.md` ("Fail-open on paging"), `.env.example`.
* Tests: `tests/integration/test_pool_capacity.py` (pool exhaustion under concurrent runs, checkout
  accounting, shed-not-wedge), `tests/unit/test_run_admission.py` (ceiling arithmetic, startup
  refusals, semaphore semantics), `tests/unit/test_ingest_event_loop.py` (`/health` answers while an
  ingest is stalled on the database).
