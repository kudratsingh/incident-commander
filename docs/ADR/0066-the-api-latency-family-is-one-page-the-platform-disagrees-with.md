# ADR 0066: The `api_latency` family is one page the platform disagrees with, and its fifth world is unbuildable

* Status: accepted
* Date: 2026-09-19
* Decider: Kudrat Singh

## Context

Plan 01 § 7.3 and plan 04 WP-8.5 ask for the corpus's Family A: five worlds behind one
"latency above SLO" alert — `db_query`, `db_pool`, `downstream`, `redis` and a healthy
control — with `get_slo_status` confirming or refuting the page. WO-R3-221's rationale
says why it is the family that most needs `get_slo_status` and the one where a healthy
control is most valuable, "because 'the alert is stale noise' is a genuine operator
answer".

Four of the five ship. Everything below is either a decision that fell out of a
measurement or a decision to stop and record rather than to soften a claim, and the
readings are in
[`evals/scenarios/README-api-latency.md`](../../evals/scenarios/README-api-latency.md),
which is the family's acceptance test.

Three facts shaped the design, and each one contradicted something the plan assumed.

**The platform cannot page on the objective the plan names.** WO-R3-216 did close: the
demo stack runs the SLO evaluator at its default interval, and `demo/compose.yml`
deliberately no longer pins `SLO_EVALUATION_INTERVAL_SECONDS` to 0. But the evaluator
raises an Alert only on a ≥14.4× burn of one of the platform's own two objectives, and
`job_dispatch_latency` never burns in any world of this family — measured 0 failed of
154, 155, 186 and 221 dispatches across the four recordings. Even in the world where a
dependency is down, jobs are dispatched on time and fail afterwards, so the objective
that breaches is `job_completion_rate` and not the latency one.

**On a quiet world `get_slo_status` reads `total: 0`, which its own description names as
an absence of evidence rather than health.** The evaluator computes both objectives over
a rolling 24 h of the `jobs` table and skips rows carrying the `eval_fixture` /
`seeded_fixture` markers (WO-R2-132), so a reset eval world has nothing in the window at
all. A control asserting "the budget is intact" over that reading would be asserting it
over no samples — the strongest possible form of INC-003's mistake.

**A held connection pool is invisible on the agent's read surface.** Seeded at the
schema's maximum with traffic running, `get_postgres_health` answered the healthy
control's reading field for field, and `get_slo_status`, `get_consumer_lag` and
`get_outbox_status` did not move either. That is § 4.

## Decision

### 1. The alert is SCENARIO-AUTHORED, it says so in its `source`, and it is refuted in every world

The page is `source: monitoring.slo`, `fingerprint:
job_dispatch_latency_above_objective`, byte-identical in all four worlds (ADR 0051 rule
1). It stands in for an external monitor watching the platform's own
`job_dispatch_latency` objective — which is what pages an on-call in every real
deployment — and it does **not** claim the platform detected anything, because the
platform cannot (see Context). An alert that claimed otherwise would be the failure
LESSONS 2026-09-08 records: an alert must not claim what the platform does not know.

`get_slo_status` is then the platform's own reading of the same number, and it refutes
the page in all four worlds. That is deliberate and it is the family's subject. A page
each world confirms would decide the answer before any probe, which is ADR 0051's first
rejected option; a page the platform disagrees with makes "is this real?" the first
question. The interesting result is that *the page is not real* and *nothing is wrong*
turn out to be different answers three times out of four.

**Rejected:** authoring the page against `job_completion_rate`, the objective that does
breach in the `downstream` world. It would make one world's page platform-corroborated
and the other three's a fabrication about a different objective, which is either four
different alerts or one alert that lies in three worlds.

### 2. The alert names an objective, so the subject guard is inert BY DESIGN

No payload field is a key in `investigation.ALERT_SUBJECT_PROBES`, so `alert_subject`
returns `None` and cmd #177's mandatory-first-probe machinery has no opinion. This is
the documented legitimate case — "a meta-alert or a whole-queue depth alert names a
condition, not a probeable resource" — with `dlq_backlog` and `dlq_mixed_partial` as the
corpus's existing witnesses, and the recorder says so in its own notes.

**Rejected:** adding an `objective` key to `ALERT_SUBJECT_PROBES` mapping to
`get_slo_status`. It would make the first probe structural, which is tempting, and it
would also make the guard's admissible-value set a list of objective ids that has to
move with the platform's `SLOS` map — a cross-repo coupling for one family's
convenience. The cost of the decision is real and is stated in the README: the family's
first probe is a judgement the agent makes rather than a guard it passes.

### 3. Sustained `make traffic` is the family's premise, and it is asserted rather than documented

Every world's `expected_precondition` asserts `get_slo_status.objectives[].total ≥ 1`,
and every world's graded claims assert it again beside the intactness claim, so the
statement is "there is evidence and it says the budget is whole" rather than "nothing
contradicts me". A run on a quiet world abandons before any model call.

The `downstream` world needs traffic for a second and simpler reason: with no
`bulk_api_sync` jobs arriving, nothing calls the degraded endpoints and the breaker never
trips.

**Rejected:** running only the `downstream` world under traffic. Then
`get_slo_status.total` differs between that world and the other three *because of the
harness*, not because of the fault — a lab artefact the agent could discriminate on,
which is the shape ADR 0012 exists to prevent.

### 4. `api_latency_db_pool` is DROPPED, because the platform cannot show the fault

With `saturate_db_pool(connections=10)` — the schema's cap — and sustained traffic,
`get_postgres_health` answered `pool_checked_out: 1`, `pool_overflow: 0`,
`pool_wait_timeouts_1m: 0`, `longest_active_query_ms: null`,
`active_queries_over_slow_threshold: 0`: the healthy control's reading, field for field.
Across six reads at twelve-second intervals, `get_slo_status` stayed at 100 % of both
budgets with 0 failed, `get_consumer_lag` stayed 0, and `get_outbox_status` read 0 or 1
unpublished with the relay ticking under a second.

So the world is distinguished from the control on **no** agent-visible field, which fails
the family's first acceptance requirement. Three facts make that structural:

1. The `pool_*` fields describe the process that ANSWERED the call — the MCP server, a
   separate process with its own pool (platform ADR 0006) — while the hook holds the
   API/worker process's pool. The hook's own description says "a pool reading taken there
   does not show this fault", and platform ADR 0030 leaves the gap open explicitly.
2. `MIN_FREE_CONNECTIONS` is 4 and the clamp is deliberate, so the background loops slow
   rather than stop (platform ADR 0031). At the traffic rate the platform admits — the
   submission rate limit is 30/60 s, so `traffic_loop`'s 2 s interval is already at the
   ceiling — they keep up completely.
3. WO-R3-289 is the gauge that closes it: a per-process pool reading published the way
   breaker state is. It is open, and it is this world's prerequisite rather than a
   nice-to-have.

Shipping it anyway would mean grading a scenario on a reading its fault does not move —
"a signal the lab cannot produce is a fixture defect" (LESSONS 2026-09-07, rem 3 run A,
≈$0.15), caught this time before any spend — or grading `db_pool_saturation` in a world
whose every reading says nothing is wrong, which is ADR 0040 and INC-003. Both are worse
than four worlds.

**Rejected:** grading it on the downstream consequences of a held pool
(`get_slo_status`'s dispatch-latency objective breaching, `get_consumer_lag` climbing,
`get_outbox_status` ageing), which is what the dispatch brief restated the world as.
Every one of those was measured and none of them moves. **Also rejected:** raising the
traffic rate until they do — the platform rate-limits submissions at 30/60 s and refusing
that limit is exactly the "routed around a safety prompt" behaviour LESSONS forbids.

`DB_POOL_SATURATION` stays in `HypothesisCategory`, unused. That is honest: it is a fault
this platform has and cannot yet show, and deleting the label would hide the gap.

### 5. The signature the lab produces is a pair, and its floor is the platform's threshold

WP-8.2 specified the `db_query` world as "`p95_query_ms_1m` high, `pool_wait_timeouts_1m`
~0". `p95_query_ms_1m` is null in every response this platform can produce and always
will be (platform ADR 0030, O-28, divergence H6). `api_latency_db_query` therefore grades
the null, so the restatement is recorded in the scenario rather than left as a silence,
and grades the signature that does exist: `longest_active_query_ms` past the platform's
500 ms threshold **with** `active_queries_over_slow_threshold ≥ 1`, while
`pool_wait_timeouts_1m` is 0 and `pool_checked_out` is normal.

The floor is 500 — the threshold — and not 1000, and that cost a red run to establish. The
first draft claimed `at_least: 1000.0` on five samples reading 1008.8 to 1972.7 ms; a
sixth recording read **772.4** and graded a correct run RED on EVIDENCE with outcome,
action and safety passing, which is the INC-001 signature. The hook's guarantee is only
that the longest running query is past the threshold; `query_ms` is a per-query duration
on a one-second poll, not a floor on what a reader catches. **A claim written for the
samples that have been seen rather than for the guarantee the lab makes is grader drift
even when every sample so far satisfied it.**

### 6. The claims are bounds, clamps and booleans — never rates

`get_slo_status` and `get_circuit_breakers` arrived with v0.6.11 and this is the first
family to use them, so nothing had said which of their fields are readings and which are
values. First pass: 12, 10, 9 and 60 `world-drift` disagreements, every one a clock, a
monotonic counter or a ratio over one. `fixture_drift._VOLATILE` now declares, with a
reason each and with what is deliberately left OUT:

* `get_slo_status`: `measured_at`, `objectives.total`, `objectives.failed`,
  `objectives.current_success_rate`, `objectives.burn_rate`.
* `get_circuit_breakers`: `measured_at`, `breakers.recorded_at`,
  `breakers.reported_age_s`, `breakers.last_state_change_at`,
  `breakers.seconds_since_state_change`, `breakers.last_failure_at`,
  `breakers.failure_count`.
* `get_postgres_health`: `longest_active_query_ms`,
  `active_queries_over_slow_threshold`.

What is left out is what every world is graded on: `budget_remaining_pct` because it is
CLAMPED (exactly 100.0 while nothing has failed, exactly −100.0 once the budget is spent),
`fast_burn` and `healthy` because they are verdicts, `breakers[].state` because it IS the
downstream world's evidence, `slow_query_threshold_ms` because it is a configured
constant, and every `pool_*` counter because a quiet MCP process answers the same numbers
every time — which is precisely the reading that made § 4's world ungradeable. **A tool
whose every field is volatile can carry no evidence at all**, so each exemption is
paired with a claim that a bound, a clamp or a boolean can carry.

Two fields that move are still graded, as FLOORS: `longest_active_query_ms at_least 500.0`
and `breakers[].failure_count at_least 3`. Volatile as a value, load-bearing as a bound,
which is the shape `get_redis_health.used_memory_bytes` has had since v0.6.0.

### 7. Two `postgres_slow` ledger lines leave, and their coverage moves into claims

Their context was `NO_HOOK`, whose stated premise was "nothing in the lab makes a query
slow". v0.6.12's `slow_db_queries` is precisely that hook, so the premise died in the
release this packet is built on — and independently, § 6 makes the two fields volatile, so
they no longer register as drift. The ratchet requires a stale line to leave in the PR
that made it stale, so both go, and `postgres_slow` gains two graded claims on the same
fields as floors, which is where a gauge's value belongs. `NO_HOOK` itself stays declared
with a note recording why it emptied: **a ledger context whose premise a platform release
can retire has to be re-read at every re-pin**, which the runbook's bump walk now says.

`postgres_slow.yaml` is outside WO-R3-221's declared `file_ownership` and was edited
anyway, because deleting the lines without adding the claims would have left that
scenario with no pin at all on its own fault.

### 8. The control's distractor is a real thing the world holds, and it is not graded

`list_active_alerts` reads three active alerts the seeder writes — two CRITICAL, one of
them a 5xx-rate alert on the api that is the single most plausible thing to mistake for
the page that arrived. None is a hook.

It is **not graded**, and that is the decision rather than an omission. A correct run can
conclude "nothing is wrong with the paged objective" from the four dependency readings
without ever listing the alerts, and PROTOCOL step 4 cuts both ways: a claim that excludes
a correct trajectory is a defect. What is graded is the absence of any action, which is
where anchoring on `billing-consumer` would show up.

## Consequences

`ScenarioFamily` gains `API_LATENCY`, the one member its own docstring named as
deliberately absent "until the scenarios that fill it" — this is that change. The corpus
goes 57 → **61** and the families fourteen → fifteen. No new `HypothesisCategory` member:
all four labels exist already from WO-R3-188, which is the WP-1.6 bet (write the label
with the plan, fill it with the world) paying off.

The family measures ROOT_CAUSE and the briefing. It measures ACTION only as a floor,
because all four categories are outside `FIX_MAP` and all four worlds forbid all seven
Tier-1 tools. That is a genuine limit on what this family can tell you and it is the
reason the control matters most here: it is the corpus's only rung that measures
false-positive resistance against a page that is wrong in a world that is fine.

Live acceptance is DEFERRED under O-22: four legs, each needing `make traffic` beside it
and its own explicit yes. Offline root-cause accuracy on the family is 4 of 4, which
measures the canned planner scripts and not the agent.

One drift is left standing and reported rather than declared away:
`get_outbox_status.unpublished_count` flips 0↔1 between two honest observations of a
traffic-bearing world. It is deliberately not volatile through that tool — it IS
`jobs_not_progressing_outbox_stall`'s evidence — so widening `_VOLATILE` to silence it
here would weaken that family. No world of this family grades it, and it was resolved by
taking a second observation rather than by a declaration.

## Links

* Work order WO-R3-221 (WP-8.5); plan 01 § 7.3, plan 04 WP-8.5, plan 00 § 4 level 0.
* [ADR 0051](0051-a-scenario-family-shares-one-alert-and-its-noise-is-a-real-thing.md) —
  one alert per family, and its noise is a real thing.
* [ADR 0033](0033-a-scenarios-forbidden-set-comes-from-its-sanctioned-action.md) — the
  forbidden set comes from the sanctioned action.
* [ADR 0040](0040-a-root-cause-is-graded-only-in-the-world-its-label-describes.md) — a
  label is graded only in the world it describes.
* [ADR 0025](0025-a-verify-leg-must-observe-the-action.md) — why a family with no action
  has no verify leg to enumerate.
* Platform ADR 0030 (breaker state published; no reading invented to fill a promised
  field), ADR 0031 (the held pool and the degraded dependency), ADR 0034 (a slow query
  manufactured where the server can see it).
* Open follow-up: WO-R3-289, the per-process pool gauge that § 4's world waits on.
