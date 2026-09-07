# The paid live-eval sequence, 2026-08-30 → 09-07

The record of the run sequence that took `remediate_consumer_lag_success` from
"never passed live" to the first fully-green live remediation in the project's
history, and of everything that went wrong on the way there.

Written for the operator running the *next* paid sequence. It is deliberately
not a changelog: the PRs are linked, but what is worth your time is the shape of
the mistakes, because six of the ten entries below were manufactured by the
harness, the protocol, or the operator — not by the agent under test. A run that
fails for one of those reasons costs money and teaches nothing. Two more (§8,
§9) were manufactured by the agent's own steering and cost nothing, because they
were caught by reading rather than by spending.

Read [the pre-run checklist in the runbook](../runbook.md) before you spend
anything. If you only read one thing here, read §1.

---

## 1. The protocol error that cost ~$2 and produced a stage of false failures

**Date:** 2026-08-30. **Stage:** the read-only pass.

The read-only stage has exactly one supported invocation:

```bash
make eval-smoke
```

That is not what was run. The coordinator hand-rolled an equivalent instead — a
manually assembled `ONLY=` list, executed under the **write-scoped** token
rather than the read-scoped `PLATFORM_SMOKE_TOKEN`. Every property that makes
the stage read-only was supplied by `make eval-smoke`, and every one of them was
therefore absent:

| What `make eval-smoke` provides | What the hand-rolled invocation did |
|---|---|
| Read-scoped token — a Tier-1 call **403s** at the platform | Write-scoped token — Tier-1 calls **succeeded** |
| Selection derived from the YAMLs (no chaos, no action tool) | A typed substring list |
| Exit-6 refusal if the selection carries chaos | No such gate on the `--live` path |

The result: agents **remediated during a stage labelled read-only**. This is not
inference from grades — the trajectories show real Tier-1 writes landing against
the platform. Scenarios were scored against a world that earlier scenarios in
the same pass had already mutated, so the reds are artifacts of the invocation
rather than findings. About $2, and a stage whose results had to be discarded
entirely.

> **On the count.** The campaign gotchas ledger records "7 false failures". The
> archive it refers to, `e72b5ffb9df0`, records **23 scenarios selected from a
> hand-typed 22-pattern list, 12 pass / 11 fail**. Where the two disagree,
> **trust the archive** — a ledger entry is a memory of a run, the archive is
> the run. The discrepancy is itself worth noticing: a summary written from
> recollection drifted from its own evidence inside a week, which is the same
> failure mode as §2's wrong `failure_class` label. Numbers get re-derived from
> archives, never quoted from prose.

The substring list did the rest of the damage. `ONLY=dlq_backlog` matched
`remediate_dlq_backlog_success` too, smuggling a mutating scenario into the
read-only stage. **ADR 0020's one-mutating-scenario gate passed**, correctly and
uselessly: only one of the two selected scenarios mutates, so `len(mutating) > 1`
was False. The gate was never designed to catch a widened selection; it counts
mutators, and one is allowed.

> **RULE: a paid run executes the runbook's exact commands.** Not an equivalent,
> not an inlined version, not "the same thing but filtered". If the runbook's
> command does not do what you need, that is a finding to bring to the user
> *before* spending — the deviation is the thing to discuss, not the workaround.

### The meta-lesson, which is the more expensive one

This exact trap was **already documented**. `audit-ws/PHASE_7.md:323`, written
2026-08-11 — nineteen days earlier — describes hand-rolled stage invocations and
substring selection as a known way to corrupt a read-only pass. It was read by
nobody at the moment it mattered.

> **A recorded lesson is worthless unless the protocol routes the operator to it
> at decision time.** Writing it down is not the control. The control is a step
> in the procedure that makes you read it while you still have the choice.

That is the entire reason the runbook's pre-run checklist now opens by pointing
at the gotchas ledger explicitly, as a numbered step with a link, rather than
trusting that a diligent operator will go looking. This document is subject to
the same rule: if you are reading it because you went looking, the checklist has
already failed.

---

## 2. The read-only pass, done correctly

**Command:** `make eval-smoke`. **Archive:** `cde5a14485c3`.

**25/26 green, `degraded_count: 0`, exit 0.**

The `degraded 0` is worth pausing on. Before PR #176 a run could finish with
scenarios silently degraded to canned responses and still exit 0 — the audit
window was checkpointed *backwards*, so the exit-5 mask hid exactly the failures
you most needed to see. PR #176's forward checkpointing is why this run's exit
code means something. An exit 0 from before that change is not evidence.

### The one red is a real finding, and the archive mislabels it

`consumer_lag_missing_group` failed. **This is a genuine agent defect, not
harness noise.** The alert named a specific consumer group; the agent called
`get_consumer_lag` without a group argument, so the schema default sent the
probe to `worker-dispatcher` — a consumer nobody had complained about — which
came back healthy. It then noticed an unrelated critical billing alert, chased
that instead, and escalated on it. It never probed the group the alert was about.

The archive's auto-label says:

```
failure_class: grader-brittleness
```

**That label is wrong, and this document is the override.** Grader brittleness
means the behavior was right and the assertion was too tight. Here the behavior
was wrong: the agent did not investigate the resource it was paged for. The
label was assigned by the classifier's heuristics, not by review.

**Do not edit the archive to correct it.** Archives are append-only and
immutable (invariant 9, ADR 0017) precisely so that what a run recorded stays
what it recorded; a corrected archive is a destroyed one. Overrides live in
`study/findings.md` and here. Whenever you read a `failure_class` off an
archive, check it against the override record before you act on it.

---

## 3. `remediate_consumer_lag_success`: four runs to green

The arc worth studying, because each run failed for a different *kind* of
reason, and only one of the four was the agent's fault.

### Run A — `4779f94faa3c` — the agent fixed the wrong thing

Lag was climbing on a genuinely killed consumer. The agent probed lag, opened
the DLQ, and anchored on **the four rows the platform seeds into every world**:
*"Top hypothesis confirmed: DLQ contains 4 messages."* It replayed one
`replay_safe` row, verified that replay, and resolved at 4 of 13 tool calls.
`restart_consumer_group` — the fix for the fault it was paged for — was never
called.

Note why this is seductive rather than stupid: a non-empty DLQ is the resting
state of a busy queue. An agent that treats "the DLQ has entries" as a finding
will find one on every incident, forever, and every one of them will look
confirmed.

The grader caught it on ACTION and EVIDENCE. **Fix → PR #177:**
- `ALERT_SUBJECT_PROBES` — the alert's own subject must be probed. Deliberately
  **value-matched** (the probe must carry the value the alert named, not merely
  call the right tool), **refuse-not-escalate** (a missed subject is a planner
  error to correct, not an incident outcome to grade), and **inert on
  subjectless alerts** so it cannot punish scenarios with nothing to name.
- Three planner prompt rules binding the chosen remediation to the alerted fault.

### Run B — `adcdcadd94a3` — the harness manufactured the failure

The agent escalated on a lag reading that appeared static at 4. Given its
evidence, that was the **honest and correct** call.

The evidence was a lie produced by a knob. `INVESTIGATE_REPROBE_DELAY_SECONDS`
was 20; the platform's lag cache holds ~60s. Both the read and its freshness
re-probe landed inside the *same cached generation* and returned the identical
number, so the re-probe — whose only job is to detect staleness — confirmed the
stale value instead. A re-probe shorter than the staleness window is worse than
no re-probe, because it launders a stale reading into a corroborated one.

**Fix → PR #178:** reprobe delay 20 → **75** (must straddle 60, not fit inside
it), and the precondition raised to `lag >= 20` over `10×15s` so a run cannot
start against a backlog too thin to reason about.

> Before you grade an agent red, ask what the harness showed it. Run B's
> trajectory is indistinguishable from good reasoning, because it *was* good
> reasoning.

### Run C — aborted before spending, on purpose

The pre-run world audit found **three stray `SLO fast burn` alerts** that had
survived `make eval-reset`. The run was aborted before any model call.

Two things came out of it, and both are positive results:

- The `incident_app` path **refused** to resolve the strays. That refusal is
  correct: it is R2-129's fail-closed RLS doing exactly its job. Positive
  evidence for a control is rare and worth recording — most controls are only
  ever observed not-failing.
- They were hand-resolved as superuser, and **WO-R2-131** was filed, because
  needing a superuser to clean up after a reset means the reset is incomplete.

The lesson is the abort itself. The audit cost nothing; a run started against
three phantom alerts would have cost money and produced an uninterpretable
result.

### Run D — `16ae3c7a4c9d` — green

PR #179. **Green on all five dimensions, 9 of 13 tool calls. The first fully
green live remediation in the project's history.**

Four runs; one agent defect (A), one harness defect (B), one world defect (C),
one success (D).

---

## 4. The fresh world alerts on itself

**Observed 2026-09-07.** A world that had *just* booted, with no chaos seeded
and no run in flight, raised a **critical SLO fast-burn alert within five
minutes** — every single time.

The mechanism is a collision between two correct components. The SLO evaluator
counts real job outcomes; the eval fixture seeder plants 7 jobs of which **4 are
dead-lettered**. A 4/7 failure rate is a fast burn by any honest reading. The
evaluator was right; its input was scenery.

Two properties made it worse than a one-off:

- **It re-fires.** The dedup key (`_fast_burn_dedup_key`) is bucketed by hour,
  so the alert returns every hour rather than being suppressed once.
- **`make eval-reset` cannot clear it.** Reset knows about chaos and seeded
  fixtures. This alert is neither — it is organically produced by a platform
  loop reading data reset itself puts back. Resetting re-arms it.

**Mitigation → PR #180:** `SLO_EVALUATION_INTERVAL_SECONDS: "0"` on **both**
demo services, disabling the loop in the eval world. Scenarios deliver their own
alert payloads, so nothing of value is lost. **WO-R2-132** tracks the real
platform-side fix — a demo-compose value is a workaround, not a repair.

Generalize past the SLO evaluator: **reset clears what it seeds, and anything
the world grows on its own outlives it.** Audit against the baseline before a
run rather than trusting a clean reset exit code.

---

## 5. `git add -A` swept 342 files into a 6-line PR

PR #178 changed six lines of the runbook. It was committed from the **main
checkout** with `git add -A`, which also swept in **three untracked run archives
— 342 files, ~42,000 lines** — that happened to be sitting in `evals/runs/`.

Run archives are large, and they are *supposed* to be untracked until someone
decides to commit them; `evals/runs/` is deliberately not gitignored (invariant
9), which is exactly what makes `-A` dangerous there rather than merely noisy. A
six-line documentation change became unreviewable, and the archives entered git
through a change that had nothing to do with them.

> **RULE: `git add <explicit paths>`, never `git add -A`.** Run archives are
> committed deliberately, on their own branch, with their own message
> (`eval: archive live run <invocation_id>`) — never as a side effect.

The habit that prevents it: do documentation work in a **worktree**, so the main
checkout's untracked archives are not even visible to your `git add`.

---

## 6. Two harness facts that cost time

### `make bootstrap-token` only *prints* tokens

It does not write `.env`, and nothing downstream reads its stdout. The operator
copies the printed values into `.env` by hand. A run that skips the paste fails
with an authentication error that reads like a scope problem.

The trap is **volume wipes**. `docker compose down -v` destroys the database the
tokens were minted against, so every previously issued token is invalid — even
though `.env` still holds a plausible-looking one. The symptom is `Invalid
token` on the drift check, immediately after a bring-up that looked clean.
**After any `down -v`: re-run `make bootstrap-token` and re-paste.**

### `caffeinate -i -t 300` is a five-minute timer

The harness wraps some commands in `caffeinate -i -t 300`. That is a 300-second
hold — shorter than a single live scenario, and it does not prevent display
sleep. A paid run left under it can lose its machine mid-spend.

A paid run needs its own untimed hold, started before the run and held for the
whole sequence:

```bash
caffeinate -dims
```

Combine with the standing rule that **long runs go in the background**, never in
the foreground where a 10-minute command timeout can kill them and waste the
spend.

---

## 7. The verification the world could not answer

**Archive `7acd2b441961`, 2026-09-07. `remediate_stale_cache_success`.**

**OUTCOME red. Every other dimension green. The agent was right.**

Chaos planted the 90-byte stale value. The agent probed the exact key
(`get_cache_key_info` → `exists: true, size: 90`, which is the chaos write
and not the seeder's 120-byte one), invalidated exactly that key
(`deleted: true`), and then verified with `get_redis_health`.

Nothing in the lab reads `cache:jobs:worker-dispatcher:hot_set`. So the
deletion could not move a server-wide counter, and the counters that did
move belong to other traffic:

```
poll 1  keyspace_hits 209   keyspace_misses 438376
poll 6  keyspace_hits 209   keyspace_misses 442480
```

**Hits frozen at exactly 209 for six consecutive polls.** The judge
answered `not_verified` six times, correctly, and the agent escalated —
with a briefing the judge scored 1.0 for groundedness.

The agent did what it was told. The scenario's own description said
*"verify probes get_redis_health again and expects the miss trend to
reverse"*, and the planner prompt said *"Invalidate cache → verify with
`get_redis_health` (miss rate should recover)"*. **Both instructions were
wrong**, and the run is what it looks like when an agent follows a wrong
instruction competently.

This is §3-Run-B's shape with the polarity flipped. There the harness
showed the agent a stale number and it reasoned correctly to a wrong
conclusion. Here the harness asked for a number that could not exist and
it reasoned correctly to an honest give-up. Both times the trajectory is
indistinguishable from good reasoning **because it was good reasoning**.

> The archive auto-labels it `failure_class: eventual-consistency`. **That
> label is wrong and this is the override**, the second one this document
> carries after §2's. Eventual consistency means the signal arrives late.
> This signal never arrives at any horizon, because no traffic exists to
> produce it. More polling was never the answer.

### The same finding as 2026-08-12, on the other side of the loop

The August finding was *"the fault could not be manufactured"* — a
scenario asserting a premise the world could not produce. This is the
recovery-side twin: a scenario asserting an *outcome* the world could not
produce. Worth naming as one family, because the next instance will be
neither a fault nor a recovery but some third thing the lab cannot show.

**The rule: a verify signal must be a state the world can reach and hold,
not a trend it would need traffic to produce.** `exists: false` is
reachable and stable. "The miss rate recovers" needs somebody to read the
key, and in this environment nobody does.

### Note which polarity we were handed

This run failed **honestly** — the counter could not move in the right
direction, so the judge said no. The identical hole with a metric that
drifts favourably gives the opposite: `verified`, and RESOLVED on a system
nobody fixed. We saw the loud version first by luck, not by design.

**Fix → [ADR 0025](../ADR/0025-a-verify-leg-must-observe-the-action.md):**
`VERIFY_PROBE_FOR_ACTION` refuses a plan whose verify leg cannot observe
the resource the action changed (refuse-and-steer, like
`ALERT_SUBJECT_PROBES`; second refusal escalates); the scenario verifies
`get_cache_key_info(key)` for `exists == false` and grades it; the prompt
carries the rule under an invariant test.

### The audit that came with it

All nine remediation scenarios' verify legs were checked against what the
lab can actually change. One more defect, in
`dlq_wait_and_replay_success`: its **description** said *"Verify confirms
the ids are out of the active DLQ"* while its own `verify_expectation`
said the opposite and was right — the platform holds the timer, so the
entries stay dead-lettered until `execute_at`. Fixed in the same PR.

Five DLQ scenarios have a separate and lesser problem, recorded but not
fixed here: their `verify_expectation` claims the listing shrinks, their
canned fixture returns the same rows unchanged, and the canned judge
returns `verified` regardless. The verify *design* is sound — live, a
replay really does drain those rows — so this is fixture unfaithfulness
rather than an unobservable signal, and re-recording five fixtures is its
own PR.

> **Read your scenario's description as an instruction, because that is
> what it becomes.** Both defects this audit found were in prose that
> nothing executes and nothing tests. The description is not commentary on
> the scenario; via the prompt and the canned plan it is the thing the
> agent is graded for obeying.

---

## 8. The fix that fixed nothing, caught before the money

**`remediate_runaway_saga_success`, 2026-09-07. No archive: nothing ran.**

The scenario was staged and held for the user's go. A read-only pre-spend sweep
read the steering the agent would actually follow — the prompt, `FIX_MAP`, and
the pinned tool descriptions — rather than the machinery around it, and found
that every one of them pointed at `pause_dag`.

That scenario **forbids** `pause_dag`, and has since PR #173, for a reason
stronger than "it does not help": the platform refuses to replay any job inside
a paused DAG (`find_blocking_pause`). Pausing first makes the real fix return
`ok: false` per id. #173 redesigned the scenario around replaying the
dead-lettered root and never touched the prompt or the map.

**Every guard would have admitted the pause.** Right tier, real tools, resource
named on both legs and evidence-sourced, and §7's brand-new
`VERIFY_PROBE_FOR_ACTION` maps `pause_dag` → `get_dag_state.job_id` — correctly,
because `get_dag_state` genuinely observes what a pause changes.

Then the judge. The platform's own description of `get_dag_state` says:

> This is the verification surface for pause_dag — a successful pause reads as
> `paused=true` with children still in `waiting`.

Hand that reading to a judge holding "children should stop advancing" and the
verdict is `verified`, honestly. **RESOLVED, on a chain as stuck as it started,
with all five dimensions green** — and stuck again ten minutes later when the
pause TTL lapsed and the held children promoted back behind the same dead root.

### Note which polarity we were handed, again

§7's run failed *loudly*, because its verify signal could not move. This one
would have failed *silently*: the signal moves exactly as promised, and the
promise is about the wrong thing. That is three entries in this document now
turning on the same axis — what the harness makes observable — and this is the
first where the observable was working perfectly and still meant nothing.

### Why 38/38 could not have caught it

Two blind spots that compound, and both are worth carrying to the next repo:

* **`FIX_MAP`'s values are never read at runtime.** Only its keys are. A wrong
  value is invisible to every test.
* **Offline eval never loads a prompt.** Canned runs replay recorded planner
  output. The prompt is a live-only surface, so the suite that exists to gate
  behaviour changes is blind to the file that most directly causes them.

> **A green regression suite is not evidence about a prompt.** Before a paid
> run, read the prompt the live agent will actually load, against the scenario
> it will be graded by — the suite cannot do it for you.

### The rule

**Ask what a verified success is worth, not only whether it can be verified.**
Concretely, alongside "what is the laziest trajectory that passes this": *if the
expected action succeeded perfectly and its effect then expired, would the
incident be back?* If yes, it is a stabilizer, and the honest terminal state is
`escalated`.

**Fix →** [ADR 0026](../ADR/0026-a-stabilizer-is-not-a-resolution.md):
`RESOLUTION_CLASS` classifies every Tier-1 action resolve-or-stabilize (total
over the slice, coverage-tested), and the one `RESOLVED` transition escalates a
verified stabilizer with a briefing that names the root still needing a
decision. `FIX_MAP[RUNAWAY_SAGA]` → `replay_dlq_by_ids`; the prompt gained a
stuck-chain section that routes to replaying the root and counters the two
pinned descriptions by name. `TestFixMapMatchesTheSuite` is the corpus check
that would have caught the drift.

### The open one: nothing the agent can see tells the two saga scenarios apart

Left unfixed deliberately, and this is the part to read before the saga pair's
paid runs. The two scenarios expect **opposite** behaviour — escalate and touch
nothing, versus replay the root — from evidence that is identical:

| | `saga_stuck` | `remediate_runaway_saga_success` |
|---|---|---|
| alert `source` / `severity` / `fingerprint` | `platform.dag` / `critical` / `saga_stalled` | identical |
| alert payload beyond that | `job_id` only | `job_id` only |
| `get_dag_state` root | `dead_letter`, `retry_count: 3` | identical |
| descendants | one `waiting`, one `completed` parent | identical |
| `paused` | `false` | `false` |
| canned hypothesis | `runaway_saga` / `stuck_saga_node` / 0.85 | identical |
| expected terminal state | `escalated`, every Tier-1 tool forbidden | `resolved` via `replay_dlq_by_ids` |

`get_dag_state`'s node model is five fields — `id`, `type`, `status`,
`retry_count`, `created_at`. No `remediation_hint`, no `error_message`, no saga
id (`create_stuck_dag` deliberately sets none, so the saga coordinator never
cancels the descendants). The chain name lives only in `jobs.payload`, which no
non-chaos tool returns.

The **one** thing that differs is the root's `remediation_hint`, and it points
the wrong way. `remediate_runaway_saga_success` seeds `replay_safe`;
`saga_stuck` takes the hook's default, `wait_and_replay`. But `wait_and_replay`
is not "a human should decide" — the platform's own routing, and this repo's
planner prompt, both say it means *replay with `delay_seconds` set*. That is
still a Tier-1 action, and `saga_stuck` forbids all of them. So the only
observable difference justifies a different **action**, not the absence of one.
It is also readable only through `list_dlq_messages`, which takes no job-id
filter — the agent would have to page the DLQ or guess a category.

**This is a scenario-design defect, not a prompt gap**, so no discriminator was
invented for it. Nothing was changed in either scenario.

> **CLOSED by §9 (same day).** The user's read-before-replay requirement made
> the discriminator load-bearing rather than optional: once replaying a root
> requires reading its dead-letter row, the root's hint IS the evidence that
> separates the pair. `saga_stuck` now seeds `remediation_hint: human_required`
> exactly as prescribed below, and both scenarios grade the row they read.
> Everything below is the analysis that produced that fix; read it for the
> reasoning, not for the current state.

The fix, when someone takes it, is one line and the platform points at it
already. `create_stuck_dag`'s `remediation_hint` argument says `human_required`
"makes the chain unrecoverable through the replay guardrails — reserve it for
escalation drills." Seed `saga_stuck` with
`chaos_setup.arguments.remediation_hint: human_required` and its expected
behaviour becomes justified by something the agent can read: a `human_required`
root is one the platform refuses to auto-replay, so escalating is the only move
left. Then, and only then, is "replay only when the root's hint is
`replay_safe`; a `human_required` root escalates" a rule worth putting in the
prompt. It costs `saga_stuck` one extra read (its cap is 11) and it changes a
scenario queued for a paid run, so it is the user's call, not a builder's.

Until that lands, `saga_stuck` grades whether the investigation planner happens
to choose caution on evidence that equally supports acting. Nothing structural
holds it there — `runaway_saga` is in `FIX_MAP`, so the handoff is permitted —
which means a green run is a green *sample*, not a green guarantee.

---

## 9. The fix that was never checked for safety, caught by the user before the money

**`remediate_runaway_saga_success`, 2026-09-07. Still no archive: still nothing ran.**

§8 left this scenario staged with a corrected trajectory — probe `get_dag_state`,
see a `dead_letter` root holding `waiting` descendants, replay that root. Two
readiness sweeps said GO. The user read the trajectory before releasing the
spend and asked the question none of the sweeps had:

> How does the agent know that root is safe to replay?

It does not. `get_dag_state`'s node model is five fields — `id`, `type`,
`status`, `retry_count`, `created_at`. **`remediation_hint` is not one of
them, and neither is `error_message`.** So `"status": "dead_letter"` says the
root *stopped* the chain and says nothing at all about whether restarting it
is safe. The agent was about to re-run a job knowing only that it had failed
three times.

The information exists, in exactly one place: that job's row in
`list_dlq_messages`. And the platform's own description of that tool states
the rule out loud:

> A null hint is UNKNOWN, not replay-safe: do not feed those to a categorised
> replay. Read the error, then replay by explicit id, or fence it with
> `mark_dlq_permanent`.

An **unread** row is strictly less evidence than a null hint.

### Six guards admitted it, and the near-miss is the interesting one

Every structural check the campaign has built passes this plan. Tier checks;
`_absent_resource_args`; `_misdirected_verify_args`; §7's
`VERIFY_PROBE_FOR_ACTION` (`get_dag_state` genuinely observes a replay); §8's
`RESOLUTION_CLASS` (`replay_dlq_by_ids` genuinely resolves). All correct.

The one worth staring at is `_unsourced_resource_args`, which requires a
resource argument to be a value the platform itself produced. The chain root's
id **is** platform-produced: the alert carries it, and `get_dag_state` echoes
it back as `seed_id` and as a node `id`. So the guard says "this string is not
a hallucination" — and reads, to a tired operator, almost exactly like "the
platform told us about this job". Those are very different claims and only the
first was ever being made.

> **Together the guard stack establishes that the agent is acting on the right
> object, named honestly, and can check its own work. Not one of them asks
> whether acting on that object is a good idea.**

### And the prompt was steering *away* from the answer

§8's own fix said so, in a line pinned by an invariant test:

> **You do not need the DLQ listing to act here.** … Do not call
> `list_dlq_messages` merely to satisfy a table.

That sentence is true about the cheapest trajectory and false about the safe
one, and it was written for a good reason: `list_dlq_messages` takes no job-id
filter, so reaching one known row means filtering by hint or paging. §8
measured that cost and decided against paying it, having framed the question
as routing rather than as safety. One extra call against a cap of 13.

### The rule

**A fix that is only justified by the symptom is not justified.** "This node is
dead-lettered" explains why the chain is stuck; it does not license the
remediation. Before an action, ask what would have to be *true of the resource*
for the action to be correct, and then ask which read establishes it. If no
read does, that is an escalation, not an assumption.

The general form, and the one to carry to the next repo: the guard family had
grown four questions — did anyone read what the alert names, is the action
aimed at it, can anyone see what changed, is a verified success worth anything
— and every one of them can be satisfied by a run that never established
whether the change should be made at all.

**Fix →** [ADR 0027](../ADR/0027-read-the-row-before-you-replay-it.md):
`SOURCE_ROW_FOR_ACTION` refuses a by-id replay whose job's dead-letter row is
not in the evidence (refuse-and-steer, total over Tier-1, coverage-tested);
both planner prompts carry the four readings and their outcomes; the scenario
grades that the root's own row read `replay_safe` *before* the replay, which
needed two new grader axes — a row selector and an ordering boundary.

### The honest limitation, stated because it will bite a live run

PLANNING is a single LLM call with no tool budget, so the remediation planner
**cannot fetch the row it is missing**. The refusal's only real repair is
dropping ids it has no row for; otherwise the run escalates naming the skipped
read. That is fail-closed and correct, and it means a live run whose
*investigation* skipped the read will escalate rather than resolve. Keeping a
correct run green is the investigation planner's job, which is why the rule
went into `investigation_planner.md` as well — the prompt is the steering half,
and it is the half the guard cannot substitute for.

### The saga pair finally has a discriminator

§8 recorded that `saga_stuck` and `remediate_runaway_saga_success` expected
**opposite** behaviour from evidence the agent could not tell apart, called it
a scenario-design defect, and left the one-line fix as the user's call because
it changes a scenario queued for a paid run.

That call is taken here. `saga_stuck`'s chaos hook now seeds
`remediation_hint: human_required` — the hook already supported the argument
and its own description reserves that value "for escalation drills". The
escalate-only twin is now escalate-only for a reason the agent can read: the
root's row says a human decides, and the platform refuses to auto-replay it.
Until now a green run there measured whether the planner happened to choose
caution. It costs one extra read against a cap of 11.

### What is still open

`replay_dlq_by_category` names a filter and no ids, so the guard has nothing to
look up: three category-replay scenarios can still read the hint *after* acting
and grade green (WO-R2-143). `mark_dlq_permanent` is deliberately left inert —
fencing stops auto-replay rather than re-running anything, so an unread row
there cannot cause this harm (WO-R2-144). Both would newly bind scenarios
queued for paid runs, which makes them the coordinator's call, not a builder's.

---

## 10. The same rule, the other call shape: a category names rows nobody read

**`dlq_replay_safe_success` and the DLQ set, 2026-09-07. Nothing ran; nothing
had to.**

§9's fix enforces "read the row before you replay it" by matching the plan's
own job ids against the ids a `list_dlq_messages` reading returned. That
enforcement has a shape, and ADR 0027 named the hole on its way past: a
`replay_dlq_by_category` call **has no job ids**. It hands the platform a
filter and the platform picks the rows when the call executes. So
`RESOURCE_ARG_FIELDS` is empty for it, `_resource_values` returns nothing to
look up, and the guard is inert by construction. Filed as WO-R2-143 rather
than improvised, because five queued scenarios would have newly bound.

The hole was not narrow. `replay_dlq_by_category` is the expected action of
**three** of the queued paid scenarios, including `dlq_replay_safe_success` —
the next one in the sequence. Until this landed, a run could reach

```
replay_dlq_by_category(category="replay_safe", max_replays=50)
```

having called nothing at all, and **seven structural guards admitted it**, each
one correct about its own question. The repo's own test suite said so out loud:
`test_a_category_replay_is_inert` asserted that a plan with `evidence=()`
reached REMEDIATING. That test was the red-before.

**Why a category is worse than an unread row, not better.** A by-id replay at
least names what it will touch, so a reviewer reading the trajectory can see
the blast radius. A category names a filter: *which* rows and *how many* are
both decided by the platform at execution time, over whatever the queue holds
at that instant — including rows dead-lettered after the alert that paged the
agent. And a hint read on one row is not a fact about its neighbours: reading
that job A is `replay_safe` and then sweeping `category=replay_safe` acts on B,
C and D on the strength of a classification made about A.

§7's own run is the case that makes this concrete. Remediation 4 run A failed
on a row whose `remediation_hint` said `replay_safe` and whose `error_message`
was a permanent schema violation. **The agent caught it because it read that
row.** A category replay over the same queue would have swept it up and nobody
would have seen the contradiction.

**The grading half was open in the same way, and for a reason worth
remembering.** Every DLQ scenario verifies with `list_dlq_messages`, because
that is the only tool that observes a dead-letter row. So the natural claim —
"some row the agent listed was classified `replay_safe`" — is satisfied by the
**post-action** verify probe exactly as well as by the investigation probe. An
act-then-read agent and a read-then-act agent leave byte-identical evidence.
`remediate_dlq_backlog_success` was the worst of it: it graded an exact replay
volume and had **no `list_dlq_messages` claim of any kind**, so nothing said
the agent had ever looked at the queue it drained. It passed live
(`e8404306138c`) with the read first — and the claim never required it.

**The generalisation.** A guard that enforces a rule by matching *arguments* is
only as complete as the argument shapes it knows about. When the platform
offers two ways to say the same thing — name the rows, or name a filter over
them — closing one is a partition, not a fix, and the open half is where the
next run goes. ADR 0028 makes the two maps a declared partition, total over
Tier-1 from both sides, so a bulk tool shipped next year cannot land between
them.

Fix: ADR 0028 (`SOURCE_LISTING_FOR_ACTION` — coverage, scope by scope, refuse
and steer), `before_tools` ordering on the five DLQ scenarios' listing claims,
one prompt rule, and a fourth derivation in `make world-dossier`. Found by
reading a filed work order against the code rather than by a run, at zero cost.


## Summary: what each failure was actually caused by

| # | Run / event | Looked like | Actually was | Fix |
|---|---|---|---|---|
| 1 | Read-only stage, 08-30 | 11 agent failures (archive `e72b5ffb9df0`) | Protocol deviation (hand-rolled invocation, write token) | Runbook commands verbatim; pre-run checklist |
| 2 | `consumer_lag_missing_group` | grader brittleness (auto-label) | **Real agent defect** — alert subject never probed | PR #177; override recorded here |
| 3 | Run A | plausible remediation | Wrong target — anchored on always-present DLQ rows | PR #177 `ALERT_SUBJECT_PROBES` + planner rules |
| 4 | Run B | agent gave up | Harness knob — reprobe inside the cache window | PR #178 (delay 75, precondition lag>=20) |
| 5 | Run C | dirty world | Incomplete reset; RLS refusal was **correct** | WO-R2-131 |
| 6 | Fresh boot | spurious alert | Evaluator reading seeded fixtures as traffic | PR #180; WO-R2-132 |
| 7 | PR #178 | 6-line docs PR | 342 files swept by `git add -A` | Explicit paths; work in a worktree |
| 8 | `remediate_stale_cache_success` (`7acd2b441961`) | eventual consistency (auto-label) | **Scenario + prompt asked for a signal the world cannot produce** — hits frozen at 209 across six polls | ADR 0025; override recorded in §7 |
| 9 | `remediate_runaway_saga_success` (not run) | a staged, ready scenario | **Prompt + `FIX_MAP` steered at the one tool the scenario forbids**; a verified pause would have graded RESOLVED on a still-stuck chain | ADR 0026; §8 |
| 10 | `remediate_runaway_saga_success` (still not run) | a staged, *corrected*, twice-swept scenario | **The corrected fix was never checked for safety**: the agent would replay a dead-lettered root knowing only that it was dead-lettered, because `get_dag_state` carries no `remediation_hint` — and six guards admitted the plan | ADR 0027; §9 |
| 11 | The DLQ category scenarios (not run) | a rule already closed by ADR 0027 | **The by-id guard is inert for a call that names a filter** — a bulk `replay_dlq_by_category` by a run that had listed nothing was admitted by seven guards, and three scenarios' claims could not tell act-then-read from read-then-act | ADR 0028; §10 |

**The through-line.** Six of these ten are failures of *procedure and
environment*, not of the agent — only rows 2 and 3 are genuine agent defects,
and they are the same defect. The scoreboard was reporting on the harness and
attributing it to the model. Before you accept a red live result, establish that
the world was clean, the invocation was the runbook's, and the knobs let the
agent see the truth — in that order. Only then is the result about the agent.

(The count said "eight" until row 10 landed, and row 11 came after that; each
arrived once the prose was written, which is the five-stale-copies problem in
miniature and is why the counts are corrected in place rather than left to be
re-derived. Read "six of these ten" above as six of eleven.)

Rows 9, 10 and 11 are a third kind, and they are the three that cost nothing:
**steering defects, found by reading the instructions the agent would actually
obey rather than by running it.** All three were caught between "the scenario
is ready" and "the money is released", which is the only window in which a
defect of that kind is free. None would have shown as a red run — row 9 would
have graded RESOLVED, row 10 would probably have graded green too (the world's
chain root really was replay-safe), and row 11 already HAS a green live run
behind it: `remediate_dlq_backlog_success` passed on all five dimensions with
the read first, and nothing in its grading required the read. **A green run
does not establish that the agent had grounds.**

Row 8 adds a fourth question to that list, and it is the one this sequence
kept failing to ask: **could the run have gone green at all?** Rows 4 and 8
are both "the harness made the truth unavailable" — once by showing a stale
number, once by asking for a number that never existed. Two of the ten
turned on a *premise the lab could not satisfy* (row 8 and the 2026-08-12
un-manufacturable fault), and both were written down in prose nobody
executes. Before the money: read the scenario's description and its
`verify_expectation` as instructions, and ask what in this world would move
if the fix worked.

Also note where the archive's own labels landed. **Two of the ten rows
have an auto-assigned `failure_class` this document overrides** (rows 2 and
8), both in the direction of making a real finding look like noise —
"grader-brittleness" and "eventual-consistency" are both ways of saying
*nothing to see here*. The classifier is a heuristic over the trajectory
shape; it has no way to know whether the world could have answered. Never
quote a `failure_class` without checking it here first.
