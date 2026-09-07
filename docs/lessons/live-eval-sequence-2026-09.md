# The paid live-eval sequence, 2026-08-30 → 09-07

The record of the run sequence that took `remediate_consumer_lag_success` from
"never passed live" to the first fully-green live remediation in the project's
history, and of everything that went wrong on the way there.

Written for the operator running the *next* paid sequence. It is deliberately
not a changelog: the PRs are linked, but what is worth your time is the shape of
the mistakes, because five of the seven entries below were manufactured by the
harness, the protocol, or the operator — not by the agent under test. A run that
fails for one of those reasons costs money and teaches nothing.

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

**The through-line.** Five of these seven are failures of *procedure and
environment*, not of the agent — only rows 2 and 3 are genuine agent defects,
and they are the same defect. The scoreboard was reporting on the harness and
attributing it to the model. Before you accept a red live result, establish that
the world was clean, the invocation was the runbook's, and the knobs let the
agent see the truth — in that order. Only then is the result about the agent.
