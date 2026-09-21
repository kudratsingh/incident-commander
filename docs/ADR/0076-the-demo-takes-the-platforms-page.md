# ADR 0076: The demo takes the platform's page, and the DLQ world is one the platform can honestly page for

* Status: accepted
* Date: 2026-09-21
* Decider: Kudrat Singh (WO-R3-339; owner decisions O-35 and O-36, 2026-09-20 20:55)
* Implements the commander half of platform ADR 0039 (the platform pages on its own metric; an
  episode raises once), pinned by the v0.6.18 digest in `demo/compose.yml`
* Builds on [ADR 0075](0075-a-states-timestamp-is-the-moment-it-was-entered.md)
  (`--world-already-faulted` — one fault, fired once, which is what makes this decision's
  repeat question moot), [ADR 0069](0069-a-rehearsal-is-a-third-provenance-not-a-live-run.md)
  (the free mode both rehearsals run in),
  [ADR 0074](0074-once-the-ranking-is-settled-the-planner-acts-or-hands-off.md) (the demo
  runner reads the world under the smoke principal),
  [ADR 0013](0013-run-provenance-is-part-of-the-eval-result.md) (the provenance record
  `alert_source` and `alert_id` join), [ADR 0031](0031-an-alerted-dlq-category-is-the-incident.md) and
  [ADR 0032](0032-the-action-must-address-the-alerts-subject.md) (what an
  alert's SUBJECT is, which is what the match reads), and platform ADR 0038 (the lab's own
  label on a probe the lab makes)
* Amends the deferral in [ADR 0010](0010-scenario-owned-dlq-fixtures.md): its `seed_dlq_messages`
  hook becomes declarable here, and its inter-scenario empty-DLQ baseline stays deferred

## Context and problem statement

The owner's fourth live take (2026-09-20, run `1f14d3f6…`, $0.25) was green and still not
watchable. ADR 0075 fixed the timestamps, the live planner reporting and the double injection.
The coordinator's read of the full audit stream found one more thing, and it was not a bug in
either repo:

> The alert the agent triaged is synthesized by the scenario YAML (`alert:` block, fingerprint
> `consumer_stalled`), not raised by the platform: `list_active_alerts` read 3 seeded alerts
> before, during and after the fault. **The platform never paged anyone.**

The demo's sentence is "jobs pile up, the platform pages, the agent responds". The middle clause
was a fixture written by the same file that grades the run — which is not a presentation problem.
A benchmark whose alert and whose answer key come from one document cannot show that the agent
answered a page; it can only show that it answered this document.

The owner decided both halves on 2026-09-20 (O-36: the platform raises the alert; O-35: its lag
clock runs at 5 s on the demo stack, because nobody watching a demo waits a minute for a number
to move). Platform v0.6.18 shipped the platform's half — two rules on the metrics tick,
`consumer_stalled` and `dlq_depth_warning`, deduped per episode, with `alert.raised` /
`alert.resolved` audit rows the agent is allowed to see. This ADR is the commander's half, and
the two decisions in it that constrain future work.

## Decision 1 — a run may be paged by the platform, and the row says which

`evals/runner.py` takes `--alert-from-platform`. With it, a run does not read the scenario's
`alert:` block at all: after the preconditions prove the fault is in the world, it polls
`list_active_alerts` until a row carrying this scenario's alert appears, and starts the run from
**that row's `extra_data` verbatim**.

Five things about that are decisions rather than implementation:

**The payload is verbatim, and the id travels beside it.** No merge with the YAML block, no key
added, no re-keying. A payload this harness had a hand in assembling would be the old arrangement
with an extra step in it. The alert's id and `fired_at` are printed and recorded in provenance
(`alert_id`), and reach no prompt — so a report can be joined to the `alert.raised` audit row
that started it, which is the claim O-36 makes and the one thing a fingerprint cannot identify.

**The match is fingerprint plus subject, and never `source`.** The platform's rows read
`kafka:consumer_lag` and `dlq:threshold`; the corpus writes `platform.kafka` and `platform.dlq`.
A match on source finds nothing, and the failure mode is the worst kind — a run waiting out its
whole bound with the right alert in front of it. The subject is read through
`investigation.alert_subject`, this repo's one declaration of what an alert is ABOUT (the agent's
own subject guard routes off the same function), and what is compared is the RESOURCE it names —
the read that observes it, the argument the value belongs in, and the value — never which payload
field spelled it. The platform's lag alert carries `consumer_group` *and* `group` holding the same
value because the receiver reads whichever it finds, while a scenario writes one of them; both
route to `get_consumer_lag(consumer_group=…)`. Comparing field names was written first and was
red on the first test that ran: the right alert did not match itself.

**A fingerprint is required, and a subject is not.** With no fingerprint the first alert in the
tenant becomes the run's page, and the tenant is never empty — three seeded fixtures. With no
subject the fingerprint alone is the honest match: a whole-queue depth page names a condition
rather than a resource, and there is nothing narrower to ask.

**The wait is bounded, and running out is UNGRADED rather than failed.** `ChaosSetupFailed`, the
same class as a hook that did not fire: an alert the platform never raised is a world that does
not hold the run's premise, and grading the agent for a pre-run event is what plan 01 § 4
forbids. The bound is expressed in seconds (120) and not in ticks, because the tick is a
deployment setting now — a bound counted in ticks would be 30 seconds here and half an hour on a
stack somebody misconfigured.

**The read is the evaluator's, under the read-only principal, labelled as the lab's.** ADR 0074's
rule (an observation of the world is made by the token that cannot change it) and platform
ADR 0038's (an unlabelled read on a service account lands as `agent.tool_invoked` inside the take,
and the demo page then counts it as a call the agent made and never reported — finding F4). An
unset `PLATFORM_SMOKE_TOKEN` refuses; it never falls back to the agent's own token.

**What does not change: the grade.** The graders key on the terminal state, the platform's audit
log and the readings the run took. None of those is the alert. The canned and eval scenarios keep
their YAML alerts (O-36 says so explicitly), the flag is refused against a recorded world and
against a canned fallback, and `make eval-reg` runs exactly as before.

## Decision 2 — the DLQ demo world is a replay-safe backlog, in its own scenario, seeded by the one hook that can write one

The platform's DLQ rule names the category carried by the rows ABOVE the seeded baseline of four.
`remediate_dlq_backlog_success` injects one `poison_message` row and that row is UNCLASSIFIED on
purpose (v0.6.3 stopped the lab lying about it — the whole subject of WO-R2-166). So the alert the
platform can honestly raise for that world reads `dlq_scope: unclassified` with
`remediation_hint: null`, and the honest action there is to read and fence the row. That is a
correct scenario and a correct incident. It is the opposite of the owner's demo story, which is
that the queue frees up while you watch.

Three consequences, each chosen over an alternative that was tried first:

**A new scenario, not a bent one.** `demo_dlq_replay_safe_backlog` is demo-only (a live scenario,
`family: dlq`, graded like any other). `remediate_dlq_backlog_success` could not accept the new
world without changes: its premise pins a five-row queue, its evidence claims are keyed to two
specific row ids, and `replayed` sums to exactly 1. Every one of those would have had to move, and
they are where the 2026-09-08 re-derivation lives. The corpus grows by one rather than losing a
graded world.

**`seed_dlq_messages` becomes declarable, and ADR 0010's baseline flip stays deferred.** It is the
only hook whose `remediation_hint` accepts `replay_safe`: `create_bad_data_job`'s enum is
`human_required`/`unclassified` by design, because the row it writes carries a permanent data
fault and a replay-safe label on it would be the lab lying; `create_mislabeled_dlq_job` writes
exactly that contradiction on purpose, for the scenario about wrong classifications. So a world
where the platform can honestly page for a replayable backlog is reachable through this hook and
no other. It was excluded from the commander's closed set with the rest of ADR 0010's commander
half — a packet that flips the inter-scenario DLQ baseline to empty, which changes every
scenario. Admitting the hook changes nothing about that baseline: the seeded four rows stay, the
world audit still wants a DLQ total of 4, and the hook's rows are DELETEd by the next reset. The
exclusion MECHANISM stays and stays tested, because the next deferred hook has to be excludable
the same way.

**The fault need not be repeat-safe any more, and this world's is not.** `seed_dlq_messages` adds
`count` more rows every time it fires, and no caller can pin the ids it mints. The demo machine
required repeat-safe hooks because "this script fires the plan and the runner fires it again" —
which ADR 0075 retired. Two structural facts now make a repeat unreachable: the walk's step 1 runs
`make eval-reset` and GATES on `make world-audit` before the fault, so a world still carrying the
last take's rows cannot reach the seeding; and step 5 passes `--world-already-faulted`. The
constraint is replaced by a test of those two facts rather than deleted, and the measured
repeat-safety of `poison_message` and `kill_consumer` stays written down.

Because the hook's ids are fresh per run, every claim in the new scenario selects a row by an id
the boot seed owns or asserts a count, and none names a row the hook wrote. Its canned fixtures
are one real recording and can never match a live reading, which is ledgered in
`evals/fixture_drift_ledger.py` rather than exempted.

## Measured, not assumed

Zero-LLM on the v0.6.18 stack, 2026-09-21, before the scenario was written:

* `seed_dlq_messages(replay_safe, count=3)` → depth 7, and the platform raised
  `dlq_depth_warning` in **4.2 s** carrying `remediation_hint: replay_safe`, `dlq_depth: 7`,
  `threshold: 5`, `dlq_scope: null`. The three seeded rows are the three newest in the listing,
  which is what makes them "the rows above the baseline".
* the `replay_safe` slice read four rows (the three seeded plus the boot-seeded `fc8d2a03`);
  one `replay_dlq_by_category(replay_safe)` answered `matched 4, replayed 4, scheduled 0,
  failed 0`; the queue read **3 within 3.1 s** and the platform resolved its own alert inside
  the next tick.
* the replayed rows do not come back — watched for a further 60 s. `process_bulk_api_sync` fails
  a job only when every one of its five simulated endpoints fails, which does not happen on a
  queue nobody is degrading. (The comment in `remediate_dlq_backlog_success` that says a
  replayed row re-dead-letters within ~10 s is about a degraded world, and is left alone.)
* `make eval-reset PURGE_IDEMPOTENCY=1` reported `seeded_dlq_deleted: 3` and
  `rule_alerts_resolved: 1`, and `make world-audit` passed.

## Consequences

* **`RunProvenance` gains two fields.** `alert_source` (`"scenario"` by default, which is a true
  statement about every row ever archived, or `"platform"`) and `alert_id` (nullable). Both are
  derived from the alert OBJECT rather than from the caller's flag, so a run that asked for the
  platform's page and did not get one cannot claim it did. On a crash the source is read off the
  crash — which carries the row when one arrived and nothing when the run died before it — so a
  crash in the seeding records `scenario`, which is what happened. That is deliberately the
  opposite treatment from `chaos_seeded_by`, which IS the caller's instruction, because "a world
  somebody else broke" is true from the first line of a run and "the platform paged this run" is
  not.
* **The demo stack's clock is 5 s and every live scenario shares it** (O-35, `demo/compose.yml`).
  Two platform numbers derive from it rather than being literals — the lag value key's TTL is
  three passes and the sample ring is pruned by time — so a scenario or a comment that documented
  `90s TTL` or `every ~60s` as behaviour is describing a number that no longer exists. Those texts
  are corrected; no fixture value moves, because `make fixture-drift` reports 0 new.
* **`make traffic RATE=<seconds>`**, 0.75 during the fault. `POST /jobs` is rate-limited per
  identity in a FIXED window, so a faster loop does not raise the sustained arrival rate — it
  front-loads the window. That front-load is the point and it is also why 3 stays the default: it
  is what a soak wants and the opposite of what a demo wants.
* **A live acceptance is still deferred.** Both demo modes are rehearsed free (ADR 0069). The
  owner's fifth take, which is the paid one, needs its own explicit yes (PROTOCOL step 0).
* **Not built, and recorded rather than solved:** the webhook. The platform fires its signed
  webhook when `alert_webhook_url` is set, and the commander has an HMAC ingress; wiring one to
  the other so a running agent server is woken by a real page is a Phase-8 e2e item (O-36 says
  so). `--alert-from-platform` is the poll fallback the alert tool's own description recommends
  as second best, and the run it starts is identical either way.
