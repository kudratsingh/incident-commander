# ADR 0077: RESOLVED needs a reading taken after the action, inside the threshold

* Status: accepted
* Date: 2026-09-27
* Decider: Kudrat Singh (WO-R3-353, INC-005, the owner's seventh live take)
* Amends: [ADR 0006](0006-verification-is-a-polling-window.md) — "on the first `verified`
  verdict it transitions to RESOLVED" now holds only for alerts that are not metric-shaped
* Related: [ADR 0009](0009-investigation-freshness-reprobe.md) (a cached reading can predate what
  it is used to judge), [ADR 0071](0071-attribution-is-graded-on-the-agents-own-pre-action-read.md)
  (credit needs a reading after the action; this record makes the loop ask for one),
  [ADR 0073](0073-a-confirming-read-is-bounded-by-the-loop.md) (the canned-queue argument reused
  in decision 4), [ADR 0075](0075-a-states-timestamp-is-the-moment-it-was-entered.md) (the verdict
  row the console shows), [ADR 0076](0076-the-demo-takes-the-platforms-page.md) (the platform's
  page, which carries the threshold), INC-005 in `audit-ws/context/INCIDENTS.md`

## Context

The owner's seventh take (`consumer_outage`, archive `a6ad732b145f`, run `8ef22e0d…`, $0.18) was
graded PASS on every dimension. The agent restarted `worker-dispatcher` at 08:52:11.03 and read its
lag 48 ms later: **55 and climbing**, `measured_at` 08:52:08.30 — three seconds before the restart.
The verification judge answered `verified`, reasoning that `recent_samples` showed "a clear decline
from 55 down to 0". The platform lists samples newest first; the judge read them oldest first. The run
went RESOLVED on poll 1 of 6. The world did recover — the platform resolved its own page 0.3 s later —
so the outcome was right by coincidence, and every grader passed.

Three things let that happen, and each needs its own fix:

1. **RESOLVED rested on one model call.** Nothing in the loop asked whether the reading the judge was
   shown could show the action's effect at all. A reading measured before the action cannot.
2. **The judge was handed a list whose order is a convention.** Raw JSON, newest first, stated
   nowhere a reader would see it.
3. **The graders key on the terminal state and the audit outcome.** A run that resolves on a wrong
   verdict passes whenever the world happens to recover.

## Decision

### 1. Two shapes of expectation, and a structural gate on one of them

An alert is **metric-shaped** when its subject is a number with a threshold: the subject's read tool
has an entry in `post_action.METRIC_READING` **and** the alert states `threshold`. The platform's own
pages do (`consumer_stalled` carries `lag`, `measured_at` and `threshold`; ADR 0076 starts the demo
from that payload verbatim). Everything else keeps the judge-decides path of ADR 0006 unchanged — a
scheduled replay reports `scheduled` and an `execute_at` rather than showing an effect, a cache
invalidation reports `deleted: true`, and a chain's state is not a number.

For a metric-shaped alert, **the verify loop leaves VERIFYING for RESOLVED only when a reading of the
alerted subject measured after the action is inside the threshold.** "Inside" is the platform's own
rule: the page fires at or above the threshold and resolves below it. A judge `verified` without such a
reading is written to the ledger as a `_verify_reading_gate` row — `verified_on_stale_reading`,
`verified_above_threshold` or `verified_without_a_reading`, with the reading, its time and the action's
time — and the loop polls again under the existing attempts and delay. When the polls run out the run
ESCALATES with a reason naming the last reading and its time. It does not reinvestigate: a second action
would not make a reading newer. A `not_verified` is unchanged — the gate can only withhold RESOLVED,
never grant it.

**"Measured after the action" is judged on the platform's clock.** The action's time is the agent's
clock; `measured_at` is the platform's. The reading also carries `age_seconds`, so
`measured_at + age_seconds` is the platform's "now" when it served the reading, and the action's time
on the platform's clock is that minus the agent-clock seconds between the action and the read. The
test reduces to *the reading's age is shorter than the time since the action*, which needs no agreement
between the two clocks — the commander is an external client (invariant 1) and must not assume one.
The age is whole seconds rounded down, so a reading measured under a second before the action can pass;
it then still has to be inside the threshold, which a reading of the fault is not.

`METRIC_READING` is total over every read tool an alert subject can name, and `None` is a declared
entry with its reason (ADR 0071's shape). One active entry today, `get_consumer_lag`. The DLQ listing is
inert on purpose: it is read at call time, so it cannot be older than the action, and a scheduled replay
leaves its rows listed until the timer fires. No Tier-1 action verifies with `get_circuit_breakers` and
no alert names a breaker, so there is no entry to write.

### 2. The judge never sees a raw sample list

`post_action.judge_view` builds the verify reading the judge is shown: the reading without its
`recent_samples`, then the samples **sorted by `measured_at`, oldest first**, each with its time, then
`Trend: {first, last, direction, samples_after_action}` and the action's time on the platform's clock.
`first` and `last` are the oldest and newest values; `direction` is how the newest sample moved from the
one before it (oldest-to-newest would call a window that climbed and recovered `flat`);
`samples_after_action` counts samples measured after the action. Order comes from the timestamps, never
from list position. A reading with no sample list is shown byte-identical to before.

`verification_judge.md` gains one rule saying how that block is laid out and that a sample measured before
the action cannot show what the action did. **One prompt hash moves**, `verification_judge`'s. This is the
secondary fix: decision 1 holds whatever the judge says.

### 3. The evidence grade asks the same question of a finished run

`evals/graders/deterministic.py`'s EVIDENCE dimension fails a **RESOLVED** run on a metric-shaped alert
whose last reading of the alerted subject predates its action or is not below the threshold, with a detail
naming both times. It calls the same `gate_miss` the loop calls, so the loop and the grade cannot disagree
about what counts. On a passing run the detail says which reading held. Inert on every other run, so no
existing detail string moved.

### 4. The canned reproduction

`verify_judge_reads_history_backwards` (`harness_control`, `control`, canned): the take's own platform
page, its own two readings byte for byte, its own plan and its own judge verdict, plus a third reading of
0 measured after the restart. Before this record the run resolves on poll 1 with the stale 55 as its last
reading. After it, poll 1's `verified` is refused as `verified_on_stale_reading`, poll 2 reads the 0, and
the run resolves on it. RESOLVED either way, so the claims are on the reading and the refusal, not the
terminal state (F-007).

A canned run polls once, which would turn the fixed run into an escalation. The scenario schema gains
`canned_verify_polls` (default 1, evaluator-only), and a canned run polls that many times with no wait
between polls — canned answers do not change with time. This scenario is the only one that sets it.

## Considered alternatives

**Fix the prompt only** — tell the judge the order. Rejected on `docs/architecture-principles.md` § 3: the
order was already a fact a careful reader could infer, and the take shows a model can miss it. The prompt
change lands, and it is the second line of defence.

**Compare `measured_at` to the agent's clock directly.** Right on one host, wrong the moment the agent and
the platform disagree about the time, and meaningless in a canned world whose fixtures are dated months
ago. The age-based form is the same comparison without the shared-clock assumption.

**Let the gate apply to every alert naming a consumer group, with `lag == 0` as the threshold when none is
given.** It would move the four canned scenarios that resolve on a lag reading
(`remediate_consumer_lag_success`, `jobs_not_progressing_dispatcher_stall`,
`retry_second_hypothesis_succeeds`, `dual_fault_dlq_and_consumer_lag`): their post-restart readings carry
an age of 1 to 12 seconds and are served instantly, so by their own numbers they were measured before the
action, and two of them resolve on 120 and 140. Those are real weaknesses of the fixtures, not of this
rule, and fixing them is a separate order (below). Scoping to alerts that state a threshold keeps every existing grade unmoved **by construction**,
and covers exactly the pages the platform raises and the demo runs on.

**Reinvestigate when the polls run out.** ADR 0056's edge is for an action that did not work. Here the
action may well have worked and the run could not see it yet; a second Tier-1 write on that basis is the
risk ADR 0008 named.

**Make DLQ depth metric-shaped too.** The platform's depth page does carry a threshold. But the verify read
is usually a filtered slice whose `total` is not the depth, and a delayed replay leaves rows listed by
design — the gate would refuse correct runs. Declared inert with that reason.

## Consequences

* **INC-005's trajectory can no longer resolve.** Offline re-grade of `a6ad732b145f` with the new grader:
  archived PASS, re-graded FAIL on EVIDENCE — "lag 55 measured at 08:52:08.296763 was measured before
  restart_consumer_group ran at 08:52:10.248781 on the platform's clock (08:52:11.030919 on the agent's);
  and 55 is not below the alert's threshold 20". Nothing was re-run.
* **A live run on the platform's page now takes at least one more poll.** The first verify read lands
  milliseconds after the action and is nearly always older than it, so the demo's run reads twice and
  resolves one `VERIFY_PROBE_DELAY_SECONDS` later. Rehearsed on the v0.6.21 stack: poll 1 refused on lag
  23 measured before the restart, poll 2 read lag 0 measured 18 s after it, RESOLVED.
* **The scripted judge in `remediate_consumer_lag_success` has one verdict per live poll** (six), because a
  rehearsal now polls more than once. A canned run still reads only the first.
* **The hand-written corpus is not covered.** Its lag alerts carry no threshold, so the gate is inert on
  them and every existing grade is unmoved. The honest follow-up is to give those alerts the platform's
  threshold and their post-action fixtures an age the verify can believe — which will move those scenarios
  and is a decision for the owner, not a side effect of this record.
* The judge's context for a lag reading is longer (one line per sample). Other readings are unchanged.

## How to verify

* `tests/unit/test_post_action.py` — the loop (stale → polls again → resolves on the fresh reading; polls
  exhausted → escalates naming the reading; above threshold refused; no threshold → today's path; the
  console gets the gate's verdict), the judge's view (no raw list, oldest first by timestamp, the trend on
  the take's reading and after a recovery, the action's time, other readings byte-identical), the grade
  (the take's trajectory fails naming both times; fresh passes; inert when escalated or without a
  threshold), the map's totality, and the scenario's knob. 17 of its 30 tests fail on the pre-change code.
* `make eval-reg` — 67/67, no pre-existing dimension moved; the new scenario is reported NEW.
* `make regrade-archive ARCHIVE=a6ad732b145f RUNS_DIR=<checkout with the archive>/evals/runs` — PASS → FAIL.
