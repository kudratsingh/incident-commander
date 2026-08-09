# ADR 0016: Incident identity, single-flight lease, and crash-resume semantics

* Status: accepted
* Date: 2026-08-09
* Decider: Kudrat Singh

## Context and problem statement

[ADR 0002](0002-hand-rolled-state-machine.md) chose a hand-rolled state machine over LangGraph on
the argument that durability is the load-bearing feature and should be owned outright. It then
promised three specific mechanics (lines 31-33): resumption loads the latest checkpoint, a crash
resumes from the last committed transition, and "a single-flight lease per incident id,
implemented as a Postgres advisory lock or lease table, guarantees one live run per incident."
None of the three was ever built (audit finding B-05). At HEAD, every `POST /alerts` mints a fresh
`uuid4` (`agent/factory.py:21` via `api/app.py:142`); `agent/triage.py:20-24` computes a
`dedup_key` and files it as evidence that nothing consults; `grep` for lease or advisory in `src/`
returns nothing; and `Checkpointer.load`'s only production caller is the crash rail added by
WO-C5-03 (`api/app.py:235`) — `PostgresCheckpointer.reconcile` is itself uncalled.

The consequences are concrete. Platform alert delivery is at-least-once, so an ordinary webhook
retry runs two complete investigations against the same fault, doubling LLM and tool spend. Worse,
if two runs ever shared an incident id, `build_idempotency_key` (`agent/remediation.py:114-125`)
hashes the incident id, so two concurrent runs on *different* ids produce two *different*
idempotency keys for the same fix and the platform executes both — the wire contract that ADR 0008
relies on for crash safety only dedupes *within* an incident identity. And a crashed run leaves a
non-terminal checkpoint that nothing ever picks up.

This ADR decides the three coupled questions — what an incident *is*, who is allowed to run one,
and what happens on re-entry — before any of it is implemented. Identity constrains the lease key,
and the lease is what makes resume safe, so they cannot be decided separately.

## Decision drivers

* **ADR 0002 is a promise with no implementation.** This ADR implements it; it does not revise or
  contradict it. The mechanics chosen below are the two ADR 0002 named (advisory lock, latest
  checkpoint), not new ones.
* **Duplicate delivery is the normal case, not the attack case.** The platform emitter retries
  (incident-platform `backend/app/services/alerts.py`), so identity has to absorb honest
  redelivery quietly. [ADR 0014](0014-webhook-signature-v2.md) shipped a process-local TTL cache
  as the explicitly-labelled stopgap for this and named durable dedupe as B-05's territory.
* **The eval harness must not move.** `evals/runner.py:338` calls `start_run` directly and depends
  on a fresh `uuid4` per scenario; 37 canned scenarios and the committed baseline are frozen
  ([ADR 0011](0011-campaign-eval-freeze.md)). Identity derivation therefore cannot become
  `start_run`'s default behavior.
* **A deterministic id must not over-collapse.** `dedup_key` hashes `(source, fingerprint)`, and
  `fingerprint` is `str | None` in `AlertPayload`. If every fingerprint-less alert from one source
  mapped to one id, an entire alert stream would merge into a single incident forever. The
  non-deterministic fallback is load-bearing, not a nicety.
* **`pg_advisory_lock` is session-scoped.** Under a SQLAlchemy connection pool, a lock taken on a
  connection that is then returned to the pool is silently released — the classic trap. Whichever
  lease is chosen must have an explicit answer for lock lifetime versus connection lifetime.
* **Resume must not double-execute a Tier-1 action.** It cannot: re-entering REMEDIATING re-sends
  the same deterministic idempotency key and gets the platform's cached response, proven live by
  [`tests/integration/test_idempotency_contract.py`](../../tests/integration/test_idempotency_contract.py)
  and settled by [ADR 0008](0008-single-attempt-remediation.md). That existing contract is the
  reason resume is safe to build now.
* **The crash rail already exists.** WO-C5-03 landed the ingress checkpoint (`api/app.py:144-165`)
  and the terminal FAILED record (`_record_run_failure`, `api/app.py:217-259`). Resume has a
  checkpoint to load and a terminal marker that tells it when not to.

## Considered options

**Incident identity**

1. `uuid4` per delivery (status quo).
2. `uuid5(fixed namespace, dedup_key)` with a `uuid4` fallback when no fingerprint is present
   (**chosen**).
3. A durable dedupe table mapping `dedup_key → incident_id`, allocated under a unique constraint.

**Recurrence cutoff** (a fingerprint that fires again after its incident closed)

4. Terminal-state cutoff: walk a deterministic generation chain, skipping generations whose run
   already reached a terminal state (**chosen**).
5. Time-bucket: fold `floor(now / bucket)` into the `uuid5` input.

**Single-flight lease**

6. `pg_try_advisory_lock` on one pinned connection held for the run's lifetime (**chosen**).
7. Lease table with an expiry column and a reaper (**pre-authorized fallback**, condition below).
8. No lease — rely on deterministic identity alone.

**Crash resume**

9. Resume from the latest **non-terminal** checkpoint; terminal (including FAILED) is not
   resumable; AWAITING_APPROVAL is out of scope (**chosen**).
10. Always start fresh from TRIAGE, ignoring any prior checkpoint.
11. Resume from any latest checkpoint, FAILED included.

## Decision outcome

Options 2, 4, 6, and 9. The concrete shape follows; it is pinned to this level of detail so the
implementing PR does not re-decide anything.

### Identity: `uuid5` over the triage dedup key, opt-in at ingress

The namespace is a fixed constant, checked into the source and never regenerated:

```
_INCIDENT_NAMESPACE = UUID("d0f7dd54-e4fc-49f6-b507-f4becc6886a3")
```

The `_dedup_key` blake2b in `agent/triage.py:20-24` is promoted to a shared public
`dedup_key(alert)` and imported by both `transition_triage` and the new derivation helper. It is
not duplicated — one hash, one definition, so the evidence entry TRIAGE records and the id the
incident carries can never drift.

`derive_incident_id(alert)` lands in `agent/factory.py` beside `start_run`:

* If `alert["fingerprint"]` is absent, `None`, or an empty/whitespace-only string → return
  `uuid4()`. The check is on the **raw fingerprint field**, never on the hash: `dedup_key` happily
  hashes `"prometheus|"` into a stable value, and returning that stable value would collapse every
  fingerprint-less alert from that source into one immortal incident.
* Otherwise → the generation walk in the next section, whose generation 0 is
  `uuid5(_INCIDENT_NAMESPACE, dedup_key(alert))`.

`start_run`'s signature is unchanged: `incident_id: UUID | None = None`, defaulting to `uuid4()`.
Derivation is **opt-in at the ingress call site only** — `ingest_alert` in `api/app.py` passes a
derived id; nothing else does. `evals/runner.py:338`, the unit suites, and every test that
constructs a `RunState` keep minting `uuid4` and are untouched by this ADR's implementation. There
is no Settings flag: a flag would be a second code path to test with no caller asking for it, and
the call-site choice is already the narrowest possible opt-in.

### Recurrence cutoff: a deterministic generation chain over terminal state

A fingerprint that fires again next week, after its incident RESOLVED, is a new incident. The
cutoff is terminal state, and it is made deterministic by generation-numbering the chain rather
than by falling back to `uuid4` on recurrence:

```
gen 0: uuid5(NS, dedup_key)
gen n: uuid5(NS, f"{dedup_key}|{n}")            for n >= 1
```

At ingress, walk `n = 0, 1, 2, …`: load the latest snapshot for the candidate id; if it is absent,
that id is the incident (fresh); if it is **non-terminal**, that id is the incident (join —
duplicate delivery or a crashed run to resume); if it is **terminal**, advance to `n+1`. The walk
is capped at `_MAX_RECURRENCE_GENERATIONS = 64`; on exhaustion, log at WARNING and return
`uuid4()` so a pathological flapping fingerprint degrades to today's behavior rather than looping.

Generation-numbering rather than "recurrence → `uuid4`" is deliberate: with `uuid4`, the *first*
delivery of a recurrence gets a random id and its at-least-once redeliveries each get another one,
so dedupe would be lost exactly for recurring alerts — the ones most likely to be retried. The
chain keeps every delivery of the same recurrence resolving to the same id.

The walk costs one indexed `load` per closed generation; `idx_run_snapshots_incident_version`
(`alembic/versions/c362fc714309_run_snapshots.py:40-44`) makes each a single index seek, and the
generation count for a real fingerprint is small.

### Ingress: derive, checkpoint only when new, always spawn

Three pinned rules at `api/app.py`, all inside `ingest_alert` between payload validation and the
202:

1. **Derive before `start_run` (L142).** `start_run(payload.model_dump(), settings, now,
   incident_id=derive_incident_id(payload.model_dump()))`.
2. **The ingress checkpoint (L144-165) is written only if no snapshot exists for that id.** This is
   a correctness requirement, not an optimization. `run_snapshots` is append-only and `load`
   returns the highest version, so appending a fresh TRIAGE snapshot on top of an in-flight run
   sitting at INVESTIGATING would make `load` return TRIAGE and hand the resume path a state that
   has lost all its evidence. Guarding the write on `checkpointer.load(incident_id) is None`
   preserves WO-C5-03's durability-before-acknowledgement guarantee (the first delivery always
   writes) and keeps the fail-open `try/except` posture exactly as it is — a lookup failure is
   treated like a write failure: logged, still 202.
3. **The background task is still spawned on every accepted delivery** (subject to the
   `AGENT_ENABLED` kill switch, unchanged). The lease, not the ingress, decides who runs. A
   duplicate arriving while a run is live loses the lease and returns immediately; a duplicate
   arriving after the owning process died finds the lock already released and resumes the run.
   Making ingress decide "is anything live?" would require exactly the liveness knowledge only the
   lock has.

Both 202s return the same derived `incident_id` — the observable dedupe contract.

Two simultaneous first deliveries race: both derive the same id, both see no snapshot, both write
an ingress TRIAGE row, both spawn. `PostgresCheckpointer.write`'s `IntegrityError`
retry-with-fresh-version (`persistence/postgres.py:38-60`) absorbs the version collision, and
exactly one task then wins the lease. The outcome is one redundant TRIAGE row and **zero**
duplicate investigations. Note this corrects the WO's expectation that the lease demotes that
retry to belt-and-suspenders: the two ingress writes happen *outside* the lease, so the retry stays
a live requirement at ingress. Its comment should say so rather than claim the lease covers it.

### Lease: `pg_try_advisory_lock` on one pinned connection

A new `src/incident_commander/persistence/lease.py` exposes a single context manager:

```python
@contextmanager
def incident_lease(engine: Engine, incident_id: UUID) -> Iterator[bool]:
    """Yield True iff this process holds the single-flight lease for the incident."""
```

Mechanics, all pinned:

* One connection, checked out for the whole `with` block via `engine.connect()`, is the lease's
  lifetime. SQLAlchemy's pool will not hand a checked-out connection to anyone else, so the
  session that holds the lock is the session that runs the incident. Nothing inside the block may
  take its own connection for the lock.
* Acquire: `SELECT pg_try_advisory_lock(hashtext(:incident_id)::bigint)` with the incident id as
  text. `try`, never the blocking form: a second delivery must return in milliseconds, not park a
  thread on a lock for the length of an investigation.
* Release in a `finally`: `SELECT pg_advisory_unlock(hashtext(:incident_id)::bigint)`, then close
  the connection. The explicit unlock is belt-and-braces — returning the connection to the pool
  does not reliably reset session locks — and closing it is the real guarantee.
* Yield `False` and take no lock when acquisition fails. The caller logs and returns.

`_run_investigation` (`api/app.py:186-214`) becomes: open the lease → if not acquired, log at INFO
that another worker owns the incident and return → otherwise run the resume-or-fresh logic below
inside the lease. The existing `try/except` failure rail wraps the whole thing unchanged, so a
crash inside the lease still writes the terminal FAILED record.

**Why the advisory lock, and why sticky connections hold today.** A run executes entirely inside
one Starlette `BackgroundTask`: synchronous, single-threaded, start to finish in the process that
accepted the webhook, with no queue hop, no thread handoff, and no `await` that could return the
connection mid-run. Its lifetime is therefore exactly expressible as one connection checkout. The
lock is per-database, not per-process, so it already fences *across* processes and replicas that
share the agent's Postgres — the sticky-connection requirement is about the worker's internals,
not about being single-process. And crash release is free: when the process dies, the TCP session
dies, and Postgres drops the lock. A lease table would need a TTL, a reaper, and a clock-skew
story to get the same property, and every one of those is a new failure mode.

**When to switch to the lease table (option 7).** The switch is pre-authorized, no new ADR
required, when any of these becomes true:

* A connection pooler in transaction-pooling mode (PgBouncer et al.) is introduced between the app
  and Postgres. Session-level advisory locks break silently there. This is the likeliest trigger
  and the one to watch.
* Run execution moves off the request process into a task queue or worker pool, so the run can be
  picked up by a process other than the one that accepted the alert, or migrated mid-run.
* The loop becomes async in a way that yields the connection between transitions.
* Operators need to *see* who holds a lease, or to expire one administratively, rather than
  inferring it from `pg_locks`.

If it switches, the table is `incident_leases(incident_id PK, owner TEXT, acquired_at,
expires_at)`, acquisition is an upsert conditional on `expires_at < now()`, and the run heartbeats.
The lease *contract* — try-acquire, log-and-return on loss, held across `run_to_completion` — does
not change; only the mechanism does.

### Resume: latest non-terminal checkpoint, nothing more

Inside the lease, before running:

```
latest = checkpointer.load(run.incident_id)
if latest is None:                          -> run fresh from `run`
elif latest.state.is_terminal:              -> log, return          (RESOLVED / ESCALATED / FAILED)
elif latest.state is AWAITING_APPROVAL:     -> log, return          (out of scope, below)
else:                                       -> run_to_completion(latest, ...)   (resume)
```

That is the whole of resume: load the latest checkpoint and continue the loop from it. It does not
consult the platform audit log and does not reconcile — ADR 0008 deleted client-side
reconciliation, and `PostgresCheckpointer.reconcile`'s audit-log ambition stays unrevived. The
idempotency wire contract carries re-execution safety.

**FAILED is not resumable.** FAILED is the crash rail's terminal record (`_record_run_failure`),
written precisely because a run died in a way nobody handled. Resuming it would put a
deterministically-crashing run into an unbounded redelivery-driven retry loop with no operator
signal, and `is_terminal` already includes FAILED, so `run_to_completion` would raise
`TerminalStateError` on entry (`agent/loop.py:54-57`) if it were tried. A FAILED incident needs a
human, or a new alert — which, per the generation chain, opens a new incident cleanly.

**Tier-2 / AWAITING_APPROVAL resume is out of scope for this ADR.** No Tier-2 tools ship, and
`TRANSITIONS[AWAITING_APPROVAL]` is still `_stub("await_approval")`
(`agent/orchestrator.py:82-89`), so resuming into it would raise `NotImplementedError` and the
failure rail would convert a merely-waiting incident into a FAILED one. Approval-bound resume is
its own design and needs things this ADR deliberately does not invent: a trigger (nobody re-invokes
`_run_investigation` when an approval lands hours later — that wants a poller or a platform
callback, i.e. an ingress this repo does not have), a binding between the approval object and the
checkpoint, and expiry semantics for approvals that never arrive. The explicit log-and-return
branch above exists so the scope-out is enforced in code rather than reached by accident; it is the
first thing the Tier-2 phase deletes.

**The double TRIAGE snapshot stays benign.** `api/app.py:149-152` documents that ingress writes a
TRIAGE row and `run_to_completion` writes another on entry (`agent/loop.py:58-59`), so the log
normally carries two identical TRIAGE rows. Under this design the fresh path becomes: ingress
writes TRIAGE v0, the task loads it, sees TRIAGE (non-terminal), and resumes from it — which is
byte-identical in content to the in-memory `run` it would otherwise have used — and
`run_to_completion` writes TRIAGE v1 as before. Same two rows, same order, same content; resume
keys on the latest version's *state*, and both rows carry TRIAGE, so the branch taken is identical
either way. Resuming from a later state adds one duplicate row of that state for the same reason,
equally benign. No skip flag is plumbed through the loop, and `evals/runner.py`'s trajectories are
untouched because the runner never goes through this path.

### Why the alternatives lose

**`uuid4` per delivery (option 1).** The status quo, and the finding. Every webhook retry is a full
second investigation, and two runs on the same fault produce two different idempotency keys, so the
one mechanism protecting against double Tier-1 execution is bypassed by construction.

**Durable dedupe table (option 3).** Correct, and more machinery than the problem needs: a second
table, a migration, and a mapping row whose lifecycle (when does a `dedup_key` stop pointing at an
old incident?) reintroduces exactly the recurrence question the generation chain answers with
arithmetic. A deterministic function needs no storage and no cleanup, and it is what ADR 0002
implied. The fallback path already covers the case a table would handle better (no fingerprint) by
declining to dedupe at all.

**Time-bucket recurrence (option 5).** Cleaner across restarts on paper — no reads at all — and
wrong on the boundary: a live incident whose alert re-fires across a bucket edge splits into two
incidents mid-investigation, which is the failure mode this ADR exists to prevent, while a
recurrence *within* a bucket after resolution still merges into the closed incident. The bucket
size would be a guess about incident duration; terminal state is the fact itself.

**No lease (option 8).** Deterministic identity alone gets two duplicate deliveries onto one
incident id and then lets both runs write to one append-only history, interleaving two writers'
snapshots into a single version sequence — a worse artifact than two separate runs, and the exact
shape B-05 describes. Identity without a lease converts a duplication bug into a corruption bug.

**Lease table with expiry (option 7).** The pre-authorized fallback, not a rejection; see the
switch conditions above. It loses today only on cost: a table, a migration, a heartbeat, a reaper,
and a clock-skew story to replicate the crash release that Postgres gives away for free with a
session lock.

**Always fresh from TRIAGE (option 10).** Discards a crashed run's evidence and re-spends the
entire investigation budget to rebuild it, and, because a redelivery would restart a *live* run's
state from scratch, it needs the lease to do more work rather than less. It also leaves ADR 0002's
resume promise unimplemented while looking like it was addressed.

**Resume FAILED too (option 11).** Turns the crash rail's terminal record into a retry trigger.
The realistic crash vector is the checkpointer itself, so the runs most likely to be FAILED are
exactly the ones most likely to fail again immediately, and every at-least-once redelivery would
re-arm the loop.

### Consequences

Positive:

* ADR 0002's durability trio stops being aspirational. The checkpoint log, the lease, and the
  resume entrypoint that CLAUDE.md line 220 lists as a Phase 6 exit criterion all become real, and
  the criterion becomes true rather than aspirational.
* Webhook retries stop costing a full investigation each, durably and across restarts and
  replicas — which is what [ADR 0014](0014-webhook-signature-v2.md)'s process-local replay cache
  explicitly could not do. That cache's revisit trigger ("WO-C5-08 landing durable dedupe") is what
  this ADR discharges; it can be retired in a follow-up once this lands, or kept as a cheap
  pre-filter that costs one dict lookup.
* Concurrent runs against one fault can no longer fire two different idempotency keys for the same
  fix, closing the gap between ADR 0008's single-attempt posture and what the ingress actually
  permitted.
* ADR 0002's promised integration test — "race two alerts for one incident," line 52, never
  written — finally has a subject.

Negative:

* `hashtext` is 32-bit, so two distinct incident ids can hash to one lock key and serialize against
  each other. The failure is always toward *false serialization*, never false parallelism: the
  loser logs and returns, leaving its alert durably recorded at TRIAGE with no investigation.
  Probability is a birthday collision over *concurrently held* locks only (locks release at run
  end), so ~1e-6 at 100 simultaneous live incidents. Mitigation if ever observed (a TRIAGE-stranded
  incident with a "lease not acquired" log and no live owner): key the lock on the first 8 bytes of
  the incident UUID as a signed int8 instead of `hashtext`, a one-line change in `lease.py` with no
  contract change.
* The identity walk adds one indexed `load` per closed generation to every accepted delivery,
  on the ingress path before the 202. Mitigation: the index makes each a single seek, the walk is
  capped at 64, and the existing fail-open `try/except` already covers a store that is down.
* A duplicate delivery arriving while a run is live now returns 202 with the *live* incident's id
  and spawns a task that immediately exits. Mitigation: that is the intent; the log line at lease
  loss is the observability, and the task exits before any client is constructed.
* Ingress writes remain outside the lease, so the `IntegrityError` retry in
  `PostgresCheckpointer.write` stays load-bearing rather than becoming belt-and-suspenders.
  Mitigation: keep the retry and say so in its comment; the race costs one redundant TRIAGE row.
* Deterministic ids mean an incident id is now *predictable* from `(source, fingerprint)` by anyone
  who knows the namespace constant. Mitigation: incident ids are not capabilities — the ingress
  authenticates with HMAC (ADR 0014) and nothing authorizes on knowledge of an id.

Revisit trigger: any of the four lease-table switch conditions above becoming true (transaction-
pooled PgBouncer is the one to watch), or Tier-2 tools shipping — at which point AWAITING_APPROVAL
resume needs its own ADR and the log-and-return branch here is deleted.

## More information

* Implements, does not contradict, [ADR 0002](0002-hand-rolled-state-machine.md) lines 31-33 —
  the advisory-lock lease and latest-checkpoint resume it named and never built. ADR 0002 is not
  superseded; it stays accepted and this ADR is its implementation record.
* Compatible with [ADR 0008](0008-single-attempt-remediation.md): resume re-enters REMEDIATING with
  the same deterministic idempotency key and gets the platform's cached response, which is ADR
  0008's stated crash-recovery contract, so resume does not create a second attempt. Nothing here
  adds a transition edge, touches `ALLOWED_TRANSITIONS`, or revives `_action_already_executed` or
  `PostgresCheckpointer.reconcile`'s audit-log ambition; single-flight makes ADR 0008's
  one-attempt-per-incident posture enforceable rather than merely intended.
* Closes the loop [ADR 0014](0014-webhook-signature-v2.md) left open: its "durable fingerprint
  dedupe" rejected-alternative and its revisit trigger both point here.
* Finding B-05 (audit, Medium); work order WO-C5-08, which depends on WO-C5-03 (ingress checkpoint
  and terminal FAILED rail, [PR #101](https://github.com/kudratsingh/incident-commander/pull/101))
  for the checkpoint resume loads and the terminal record it refuses to resume.
* Implementing PR: `feat/incident-identity-lease` — `agent/factory.py` (`derive_incident_id`),
  `agent/triage.py` (promote `dedup_key`), `api/app.py` (`ingest_alert`, `_run_investigation`),
  new `persistence/lease.py`, and the "race two alerts for one incident" integration test ADR 0002
  promised.
* `docs/safety-model.md`'s crash-recovery section gains the resume story, and CLAUDE.md line 220's
  "single-flight lease per incident" becomes a statement of fact, when that PR lands — not before.
