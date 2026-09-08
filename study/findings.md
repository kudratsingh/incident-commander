# Study findings

Running record of reliability findings. Each entry names the control that
failed, the evidence that settled it, and the fix that makes the failure
non-repeatable.

## F-001 — Controls must be asserted at point of use, not assumed downstream

**Date:** 2026-08-07 (Run 001, stage 1)

**What happened.** Stage 1 was labelled and believed to be read-only:
`make eval-smoke` exported the read-scoped `PLATFORM_TOKEN` (PR #69), the
smoke service account carried exactly `telemetry:read, incidents:read`,
and a live probe of that token was correctly refused. Yet the stage
executed ten successful Tier-1 calls (`mark_dlq_permanent` ×7,
`restart_consumer_group` ×3).

**Evidence that settled it.** The platform audit log — not the agent's
trajectory, not the trace summaries. Every successful row carried the
**full** principal (`incident-commander`); the only smoke-principal row in
the table was an `unauthorized` refusal from a manual probe. That single
query eliminated three plausible stories at once (platform scope-check
bug, over-scoped smoke SA, trace mislabelling of denials) and identified a
fourth nobody had proposed.

**Root cause.** `-include .env` (PR #62, added so `PLATFORM_COMPOSE` would
persist) overrides environment variables supplied to a make recipe, and
make re-exports the *file's* value to the sub-process. PR #69's token
export was silently defeated. Two independently-correct changes composed
into a broken control, and nothing between them ever checked.

**Why it went unnoticed.** Every layer that could have caught it was
looking at configuration rather than behavior: the SA had the right
scopes, the recipe named the right variable, the runbook described the
right intent. The only artifact that reflected what actually happened was
the platform's audit log, and nothing read it.

**The lesson.** *A control that isn't verified where it is used is not a
control — it is a belief about configuration.* Read-only-ness must be
established by the principal being unable to write at the moment of the
run, not by having configured a token that cannot write.

**Fix.** Two point-of-use assertions, no platform changes (v0.4.9 exposes
no whoami/introspection tool, and the freeze holds):

1. **Negative probe at startup** — smoke mode invokes a Tier-1 tool with
   deliberately invalid arguments and requires a *scope* refusal. The
   platform checks scope before parsing arguments, so the probe cannot
   execute under either token, and the two outcomes are distinguishable: a
   validation error means the token has write scope → hard-fail before any
   scenario runs.
2. **Post-stage audit assertion** — query `list_audit_events` for the
   stage window and fail if any Tier-1 tool shows `outcome=success`. This
   automates exactly the evidence that caught the bug.

Structurally, the principal now comes from config (`--smoke` selects
`PLATFORM_SMOKE_TOKEN` inside the runner) instead of from shell variable
inheritance, and a regression test pins make's precedence behavior so the
mechanism cannot quietly return.

**Third instance tonight.** The same shape appeared twice more: platform
#92's sys.path fix existing on master but untagged (the control existed,
the artifact in use didn't have it), and idempotent replay returning
`already_marked: true` — a response indistinguishable from a fresh write
unless you check the audit. In all three, the artifact in use diverged
from the artifact described, and only ground truth at the point of use
told them apart.

**Related.** `evals/guards.py`, `tests/unit/test_make_token_precedence.py`,
CLAUDE.md invariant 6 (audit log is ground truth), ADR 0003 (platform-side
enforcement is the boundary — it held; the agent-side plumbing did not).


## F-002 — A derived metric that silently lost its own data

**Date:** 2026-08-07 (Run 001, stage 1)

**What happened.** Throughout Run 001 I reported spend as "~$1.9 of the
$4 ceiling." The console's actual for that UTC day was **$4.53** — 2.4x
the number I was steering by, and **above the $4 ceiling the run was
authorized under**. Nobody knew until the operator read the console.

**Two separate errors, and the second is the interesting one.**

*The reported figure was never computed.* I applied the runbook's
per-scenario rule of thumb (~$0.05 read-only) written during the July
campaign, against a suite whose prompts, tool descriptions, and
verify-polling depth had all since grown. Summing the surviving traces
with real per-model rates — Sonnet $3/$15 per MTok, Haiku $1/$5, cache
writes at 1.25x input, cache reads at 0.1x — gives **$2.43**.

*The traces themselves were incomplete, by construction.*
`JsonlTracer.__post_init__` called `self.path.write_text("")`, commented
"truncate on scenario start so re-runs don't concatenate." Run 001's
first attempt was killed after 13 scenarios; the re-run covered the same
27 and **erased all 13 of those records in full**. The exact-timestamp
reconstruction is unambiguous: the trace directory holds one contiguous
window (10:49:55–11:25:13 UTC) and nothing at all from the killed
attempt's 10:19–10:31.

**How the mechanism was identified — and how I got it wrong first.**
Traced Sonnet was 1.86x under console and traced Haiku 1.89x under: two
models, different rates, different cache mixes, skewed identically. That
eliminates a pricing-formula error (which would skew per-model) and
implicates missing calls. I then guessed *which* missing calls, and
picked wrong — I claimed cache-write usage was under-reported, citing
188k cache-reads against 2.4k cache-writes as impossible. It is not
impossible; it is exactly what prompt caching looks like when one early
call writes a prefix that twenty-six later scenarios read. The operator
found the real cause by reading `tracing.py`. **The elimination argument
was sound and the mechanism was a guess dressed as a conclusion.**

**The lesson — same family as F-001.** F-001 was an unverified
*capability* claim; this is an unverified *measurement*. Both were
plausible, internally consistent, and never checked against the system
that actually knew. Worse here: the writer destroyed the evidence needed
to check it, so the shortfall could only ever surface from outside.
A budget ceiling enforced against a self-reported estimate is not a
ceiling.

**Fix.**
- `evals/tracing.py` no longer truncates. Every record carries
  `invocation_id` + `invocation_started_at`, so re-runs stay separable
  without deletion — concatenation was never the problem,
  *indistinguishable* concatenation was. The old test asserting
  truncation is inverted, with the reason inline, plus regression tests
  that a second invocation preserves the first.
- `LLMClient.call` traces **before** parsing. A response that bills and
  then fails to parse (no `record_output` block, `max_tokens`
  truncation) previously left no record at all; it is now written with
  `parse_failed: true`.
- `scripts/estimate_cost.py` replaces the heuristic with token
  arithmetic at correct per-model and cache rates, groups by
  `invocation_id`, and labels itself a lower bound every time it runs.
- `study/runs.jsonl` records trace-derived **and** console actual per
  window, with the ratio. Console is authoritative for property 6.

**Unrecoverable:** Run 001's killed first attempt. Its ~13 scenarios are
gone from the trace record permanently; its cost exists only inside the
console day total.


## P-001 — When an artifact's consumer changes, re-audit its producer

**Named practice, promoted from three failures in one campaign.**

Each of these was a different bug with the same shape:

| Failure | Producer written for | Consumer it acquired | What broke |
|---|---|---|---|
| Trace truncation (F-002) | debug output — "don't concatenate re-runs" is *correct* for eyeballing one scenario | cost ledger and study evidence | Deleting the prior attempt went from tidy to destructive the moment someone summed the file |
| Estimate vs console ceiling (F-002) | a July per-scenario rule of thumb, fine as a rough sizing hint | the enforcement input for a hard $4 spend ceiling | A hint became a control without ever being re-derived |
| Read-only smoke token (F-001) | a token selection mechanism, correct when written | the *guarantee* that a stage cannot write | Nothing re-checked it at the point the guarantee was relied on |

None was a coding error. In every case the producer kept doing exactly
what it was built to do, and the *demand placed on its output* changed
underneath it. Truncation is reasonable for a debug log and unacceptable
for a ledger; a heuristic is reasonable for sizing and unacceptable for
enforcement; a config value is reasonable for wiring and unacceptable as
a safety guarantee. The bug is never visible in the producer's own diff —
it appears only when you ask what the output is now being trusted to do.

**The practice.** When you begin using an existing artifact for a new
purpose — especially a purpose that involves *counting it, enforcing
against it, or citing it as evidence* — go read the code that produces
it, before you trust it. Ask three questions:

1. **Is it complete?** Can it drop, truncate, or overwrite records? Under
   what conditions — a re-run, a crash, an unset env var?
2. **Is it precise enough for the new use?** A number good enough to
   eyeball is not automatically good enough to enforce a ceiling.
3. **Is it verified where it is used,** or only where it was configured?
   (This is F-001's rule; P-001 is the reason you'd think to ask.)

**Review-checklist line** (add to the PR checklist in
`docs/architecture-principles.md`):

> - [ ] If this PR starts using an existing artifact for a new purpose —
>   counting it, enforcing against it, grading from it, or citing it as
>   evidence — I read the producer and confirmed it is complete and
>   precise enough for that use. (P-001)

**Why a checklist line and not a memory.** The three failures above span
five weeks and were each committed by someone who knew the individual
facts. Nobody forgot that traces were debug output; nobody re-asked what
that implied once the traces became the cost record. Enforced at review,
the question gets asked by the process rather than remembered by a
person.

### Run 001 residuals — permanent

- **Attempt 1 (13 scenarios, 2026-08-07 10:19–10:31 UTC) is unrecoverable
  from traces.** Its cost is known only as part of the console day total.
  Best estimate ~$1.2, derived from the re-run's $0.09/scenario — an
  inference, not a measurement.
- **~$0.9 of the day's $4.53 is permanently unattributable.** The records
  that would have resolved it were deleted by the producer being audited.
  This number does not get to shrink later; it is what the failure cost
  in evidence, and it is recorded so no future analysis quietly rounds it
  away.
- **Run 001 exceeded its authorized $4 ceiling** and the overage was
  detected by the operator reading the console, not by any control in the
  system.


## F-003 — The audit for a data-destruction defect destroyed data while it ran

**Date:** 2026-08-08

**What happened.** While fixing F-002's trace truncation, the routine
offline commands used to verify the fix — `make eval`, `make eval-reg`,
run repeatedly and free of charge — silently erased **Run 001's live
trajectories**. `write_trajectories` keyed files on scenario name alone
and wrote with `Path.write_text`, so each offline run overwrote the paid
live run's record. Every file now in `evals/trajectories/` is stamped
2026-08-08T05:20–05:21Z. The 2026-08-07 live trajectories are gone.

The traces from that same run survived only because the tracer had been
made append-only the day before. **The two artifact stores had identical
defects; one had been fixed, and the fix was not carried across.**

**The shape of it.** The verification step for a data-loss fix was itself
a data-loss event, executed in the same working tree that held the only
copy. The command that did it was free, routine, and reflexively safe —
"just re-run the offline suite" — which is precisely why it was run
several times without a second thought. Nothing about `make eval`
announces that it overwrites evidence.

**Fix.** `evals/runner.py` gains `archive_run`: every invocation mints one
`invocation_id` (shared with the tracer, so a trajectory and its trace can
be joined) and writes `report.json`, `trajectories/`, and `briefings/`
into an immutable `evals/runs/<invocation_id>/`, **before** refreshing the
flat paths. Every archive file is opened with exclusive-create — a path
collision fails loudly instead of deleting. The flat
`evals/{reports,trajectories,briefings}` paths remain as pointers to the
latest run, so every existing consumer is unchanged.

Two further confirmed writers fixed in the same pass:

- **`make demo-down` was `docker compose down -v`**, and `demo/compose.yml`
  declared no named volumes. Stopping the demo stack deleted the
  platform's Postgres volume — including the **immutable audit log that
  CLAUDE.md invariant 6 makes the ground truth for grading safety**, the
  service accounts, and the fixture state. Now: named volumes
  (`demo_pgdata`, `demo_redisdata`), `demo-down` preserves data, and a
  separate `demo-destroy` requires `CONFIRM=1`. Stopping a stack is not a
  destructive act.
- `write_report` / `write_briefings` shared the overwrite pattern. Both
  are largely derived (rebuildable from traces), which is why the audit's
  verifiers split on severity — but both are now archived per invocation
  regardless, because "derived today" is exactly the assumption P-001 says
  will stop holding.

**Standing practice (new).** *Irreplaceable run artifacts are archived
outside the working tree before any fix cycle touches their writers.*
Debugging a writer means running it; running it is the hazard. Before this
fix cycle continued, `evals/{traces,trajectories,reports,briefings}` and
`study/` were copied to `~/eval-archive/<date>-pre-writer-fix/` — 157
files, outside the repo, unaffected by anything the fix does. Do that
first, every time, and confirm the copy exists before editing.

**Relationship to the other findings.** F-001: a control asserted nowhere.
F-002: a measurement checked against nothing. F-003: a fix whose
verification loop consumed the evidence it was meant to protect. P-001
names the common root — but F-003 adds a sharper corollary: **when the
defect is in a writer, the act of testing the fix is itself the failure
mode.** Back up first.

**Unrecoverable.** Run 001's live trajectories, like its attempt-1 traces,
are gone permanently. They will not be regenerated: a re-run costs money
and produces a different run, which would be a fabrication, not a
recovery.


## F-004 — The guard against unverified controls was itself unverified

**Date:** 2026-08-08

**What happened.** F-001's fix added a post-stage assertion: query the
platform audit log and fail the stage if any Tier-1 tool shows
`outcome=success`. It was the automation of the exact evidence that had
caught the Run 001 token bug.

It could not have caught the Run 001 token bug. `_parse_events` read
`payload["items"]`. Platform v0.4.9 emits `{"total": N, "events": [...]}`.
The `.get("items", ...)` fallback returned `[]` on every real call, so the
assertion found no violations, raised nothing, printed *"post-stage audit:
zero successful Tier-1 actions during the smoke stage"*, and exited 0 —
**unconditionally, whatever the platform actually recorded.**

**Why the tests didn't catch it.** All four audit tests built their
fixture as `{"items": [...]}` — a container key the platform never emits.
They validated the guard against the shape the guard assumed. Both sides
of the test agreed with each other and neither agreed with reality.

**Why the live verification didn't catch it either.** I ran the guard
against the real platform and it printed a clean pass. It was a clean
window: nothing had written a Tier-1 row. *"Parsed nothing"* and *"found
nothing"* produce identical output, and I never constructed the case that
distinguishes them. **A guard verified only on windows where it should
pass is not verified.**

**The sharpest detail.** The correct shape was already in the repository,
typed, one import away: `registry.ListAuditEventsOutput` declares
`total: int` and `events: list[AuditEventEntry]`, generated from the
contract snapshot and protected by both the contract test and the
registry-consistency test. I hand-rolled a dict walk *past* the model that
encodes the truth. Not an unavailable fact — a bypassed one.

**Fix.**
- `_parse_events` parses through `TOOL_REGISTRY["list_audit_events"].output_model`.
  The shape can no longer be wrong without CI failing first: the contract
  test and registry-consistency test now protect this guard too.
- An unrecognized payload, or a response with no text block, **raises**.
  Parsing nothing fails closed, exactly like an unreadable audit; a
  well-formed `{"total": 0, "events": []}` remains a genuine pass, and the
  two are now distinguishable.
- Test fixtures rebuilt from the platform's real envelope including
  `total`, with explicit cases that a legacy `items` payload and any
  unrecognized shape both raise rather than reporting a clean stage.
- `_TIER_1_TOOLS` was a second hand-copied list of the seven Tier-1 names;
  it is now derived from the tier map. Same defect class — a fact restated
  instead of referenced.

**Empirically verified, the only way that proves anything:** a real Tier-1
action was executed under the full token, and the assertion was run over a
window bracketing it. It failed, naming the tool, timestamp, and
principal. A window starting one second later passed. Before the fix, the
same window returned zero violations.

**Fourth instance of one root.** F-001: a control asserted nowhere.
F-002: a measurement reconciled against nothing. F-003: a fix whose
verification consumed its own evidence. F-004: a verifier whose tests
encoded the assumption rather than the contract. P-001 named the producer/
consumer version of this; F-004 adds the test-shaped one:

> **A test that constructs its own fixture cannot validate a contract.**
> If the shape comes from outside the process, the fixture must come from
> the contract — the snapshot, the typed model, or a captured real
> response — never from what the code under test expects to see. And a
> guard must be exercised on the case where it should FAIL before it is
> trusted on the case where it should pass.

## F-005 — An archive's auto-assigned failure_class is a guess, not a verdict

**Date:** 2026-09-02 (read-only pass, archive `cde5a14485c3`)

**Scope.** This entry is the **override record** for one label. It does not
change the archive, and it must not: run archives are append-only and
immutable (invariant 9, ADR 0017). A corrected archive is a destroyed one —
the value of the record is that it says what the run said at the time.

**What happened.** `make eval-smoke` finished 25/26 green, `degraded_count: 0`,
exit 0. The single red, `consumer_lag_missing_group`, was auto-labelled by the
archive writer:

```
failure_class: grader-brittleness
```

**The label is wrong.** "Grader brittleness" is the bucket for *correct
behavior failed by an over-tight assertion* — the taxonomy's fifth bucket
(`docs/lessons/live-eval-noise-sources.md`). It carries an implicit
instruction: relax the assert. Applied here that instruction would have
deleted a real finding.

The behavior was not correct. The alert named a specific consumer group; the
agent called `get_consumer_lag` with no group argument, so the schema default
sent the probe to `worker-dispatcher` — a consumer nobody had complained
about — which returned healthy. It then noticed an unrelated critical billing
alert, chased that, and escalated on it. **It never probed the resource the
alert was about.** The correct classification is an agent defect (see F-006).

**Root cause of the mislabel.** The classifier assigns `failure_class` from
the *shape* of the failing dimension — an EVIDENCE-family failure on a run
that reached an accepted terminal state looks exactly like grader brittleness
from the outside. It has no way to ask whether the terminal state was reached
for the right reason, which is the only question that separates the two
buckets. The heuristic is not defective; it is being read as an authority it
was never able to be.

**Why it matters more than one label.** A wrong `failure_class` is
self-erasing. It routes the reader to "loosen the assertion", and loosening
the assertion makes the red disappear, which then confirms the label. Two of
the five buckets (grader-brittleness, LLM-variance) have this property: acting
on them destroys the evidence that would have refuted them.

**The lesson.** *A generated label is an input to triage, not the output of
it.* Any `failure_class` read off an archive is a hypothesis about a run, and
it must be checked against the trajectory before it is acted on — especially
when it recommends weakening a check.

**Fix.**
- This entry is the durable override; `docs/lessons/live-eval-sequence-2026-09.md`
  §2 carries the operator-facing version, and `docs/eval-methodology.md`
  reconciles the "so the scenario passed" narrative with the later red.
- The reds that matter are re-read against their trajectories before any
  assertion is relaxed. No archive was edited.


## F-006 — The alert's own subject was never required to be probed

**Date:** 2026-08-30 (two live runs), fix landed 2026-09-03

**What happened.** Two runs, in different stages, failed the same way and both
reached a terminal state the grader accepted.

- `consumer_lag_missing_group` (read-only): probed the schema-default consumer
  instead of the one the alert named, found it healthy, and escalated on an
  unrelated billing alert it noticed along the way.
- `remediate_consumer_lag_success` (run A, `4779f94faa3c`): a real killed
  consumer with climbing lag. The agent anchored on the **four DLQ rows the
  platform seeds into every world** — *"Top hypothesis confirmed: DLQ contains
  4 messages"* — replayed one `replay_safe` row, verified that replay, and
  resolved at 4 of 13 calls. `restart_consumer_group` was never called.

**The common defect.** The investigation planner under-weighted the alert's
own subject. It neither reliably probed the resource the alert named, nor
required its chosen remediation to address the alerted fault, and it concluded
while the alerted signal was still unexplained. One run is the first half, the
other the second. Both are the same missing premise: *the thing the alert is
about is the thing you have to read.*

**Why run A is seductive rather than stupid.** A non-empty DLQ is the resting
state of a busy queue. An agent that treats "the DLQ has entries" as a finding
will find one on every incident, forever, and each one will look confirmed.
The seeded baseline of 4 rows guarantees it.

**Why the graders were green.** Every dimension the grader scored was a
property of the *outcome*: terminal state, evidence fields present, budget,
action tool, safety. Nothing asserted a **relationship** between the alert and
the investigation. "Did it call `get_consumer_lag`" is green for the first run;
"did it read the consumer the alert named" is not — and only the second
question is worth asking. The suite measured that the agent did things, not
that it did them about the right resource.

**The lesson.** *An outcome-shaped grader cannot detect a correct answer
reached about the wrong subject.* At least one assertion must bind the
investigation to the incident's own referent, or every scenario is passable by
finding some other real problem.

**Fix (PR #177).**
- `ALERT_SUBJECT_PROBES` maps alert fields to the probe that must read them.
  Deliberately **value-matched** — the probe must carry the value the alert
  named, not merely call the right tool, since calling the tool with a default
  argument is the exact defect. **Refuse-not-escalate** — a missed subject is a
  planner error to correct before execution, not an incident outcome to grade.
  **Inert on subjectless alerts**, so it cannot punish scenarios with no
  resource to name.
- Three planner prompt rules binding the chosen remediation to the alerted
  fault.
- Consequence, recorded so it is not misread as a regression:
  `consumer_lag_missing_group` now grades **red** (F-005). The behavior did not
  change; the assertion did.

**Fifth instance of one root.** F-001 asserted a control nowhere; F-002
reconciled a measurement against nothing; F-003's verification consumed its own
evidence; F-004's tests encoded the assumption rather than the contract. F-006
is the grader-shaped one:

> **A check that never names the subject verifies only that something
> happened.** If the artifact under test is *about* a particular resource, the
> assertion must reference that resource by value — otherwise a correct-looking
> trajectory about an unrelated problem passes, and the suite's green is a
> statement about activity rather than about correctness.

---

## F-007 — A slip and a wrong belief were given the same disposition

**Where it bit.** Live paid run `5c8895771fbd`, `dlq_wait_and_replay_success`,
2026-09-07. Red on outcome, action and evidence. ~$0.11.

**What happened.** The remediation planner listed the DLQ filtered to
`wait_and_replay`, grouped the two rows by the dependency each named rather than
by their shared hint, derived a 300-second delay from the row that stated no
wait, wrote the derivation into `action_rationale`, and picked a verify leg with
the one expectation in the suite that is satisfied by nothing happening. Then it
emitted the second job id as `97d91272-0000-0000-0000-000000000000` for a row
whose id is `97d91272-9774-5b8e-980b-f0d2fa6ed619` — first block correct,
remainder zero-filled. The evidence-sourcing guard refused the plan, correctly,
and the run escalated **after one planner call**. Nothing executed.

**Why it is a finding and not a bad roll.** The same run quotes the correct id in
three other places: its own `action_rationale`, the escalation briefing it wrote
(twice, in full, with the instruction to use "the exact IDs returned by
`list_dlq_messages`"), and the briefing judge's reasoning, which scored
groundedness 1.0. The model held the right value and copied it out wrong once.

**The defect.** Seven guards run on a remediation plan. Three refuse and re-ask
once; three escalate on the first offence. The code's own justification for the
split read:

> ...the three argument guards ... escalate because a mis-named resource means
> the planner is reasoning about the wrong object rather than merely checking the
> right object the wrong way.

That is a claim about *why* an argument is wrong, inferred from the fact *that*
it is wrong. The two causes are distinguishable — a run whose briefing and
rationale name the right resource did not misunderstand it — and the guard did
not distinguish them, so it applied the disposition for the worse cause to both.
The cheap repair was available and unused: PLANNING is one LLM call with no tool
budget, and every candidate the planner could want was already in the evidence
it had read one call earlier.

**Two traps found while fixing it, both of which would have shipped a
correct-looking fix that was wrong.**

1. *A mangled id fails more than one guard, and the other guard's advice is
   harmful here.* The unread-row guard (ADR 0027) also refuses this plan — no
   listing carries a row for an id that does not exist — and its steer says to
   **drop** the ids you have no row for. Obeyed, it converts a two-row delayed
   replay into a one-row one, failing the scenario's `scheduled equals 2` exactly
   as escalating did. When several guards can fire on one defect, the ORDER in
   which they are diagnosed decides what the agent does next, and only one of the
   orders is right.
2. *The obvious structural fix does not cover the observed case.* "Validate the
   id as a UUID at parse time" is worth doing and catches truncation, wrong
   length and non-hex — but `97d91272-0000-0000-0000-000000000000` is canonical
   8-4-4-4-12 hex. Shape and provenance are different questions. A fix that reads
   as complete because it is structural is still incomplete.

**The other half: three tests that were green for the wrong reason.** Changing
the disposition exposed them. `TestEvidenceSourcedArgs`' three rejection tests
each built a fake LLM client with **one** canned plan and asserted the run
ESCALATED. Under the new behaviour the plan is refused, the fake has nothing
left, and the run escalates on `planner LLM invalid: no more canned responses` —
so all three stayed green while the guard under test decided nothing. Verified
empirically before fixing them, not assumed.

**Seventh instance of one root, and it is the test-shaped sibling of F-001.**
F-001 asserted a control nowhere; F-004's tests encoded the assumption rather
than the contract; F-006's grader never named the subject. This one:

> **A test that asserts a terminal state without asserting its cause passes for
> any cause.** A one-response fake plus an assertion on the outcome is a test of
> the fake, not of the code under test. Where a state is reachable by more than
> one path — and an escalation always is — the assertion has to name which path,
> or the test survives the removal of the thing it was written to protect.

**And the finding-shaped half:**

> **A guard that infers WHY an output is wrong from the fact THAT it is wrong
> will mis-dispose one of the two causes.** "The argument is not evidence-sourced"
> is compatible with a hallucination and with a copying slip; the run itself
> carries the discriminator (does the prose agree with the arguments?). Where the
> two causes want different dispositions and the check cannot tell them apart,
> the safe default is the one that does not end the run — an offer costs one LLM
> call and a wrong escalation costs the whole scenario.

**Fix.** ADR 0030: the two argument-shape guards refuse and re-ask once, with the
ids the run actually read enumerated back and a `did you mean <id>?` when the
rejected value shares its first block with exactly one candidate (and no claim
when it shares one with two). The harness offers candidates and never
substitutes one — a run that repeats the mangled id escalates with no stored
plan. `UUID_RESOURCE_FIELDS` is derived from `format: "uuid"` in each tool's own
input schema rather than hand-listed. One prompt line, hashed and invariant-
tested, including the clause that the ellipsis-abbreviated ids in the prompt's
own worked example are for reading only — that example sits in the very section
that produced the mangled plan.

**Filed, not done:** evidence-row handles. The harness labels each recorded row
(`dlq#2`), the planner selects labels, the harness resolves label → id on the
wire, and no UUID is ever re-typed. That removes the surface rather than guarding
it; it invalidates every canned plan fixture at once, so it wants its own PR and
its own paid run.

---

## F-008 — The alert named a slice; the agent scoped itself to the collection

**Where it bit.** Live paid run `06e14be3e7b1`, `dlq_wait_and_replay_success`,
2026-09-07. Red on outcome, evidence, action and safety. One tool call, one
planner turn, zero plans. ~$0.06.

**What happened.** The agent called `list_dlq_messages` unfiltered, got the four
seeded rows, and classified all four correctly — one `replay_safe` upstream
timeout, two `wait_and_replay` rows on two different dependencies, one
`human_required` CSV parse error — naming the right remedy for each. Then it
stopped:

> The DLQ contains 4 entries with mixed remediation hints that cannot be handled
> by a single Tier-1 action […] A human operator must triage each entry
> individually.

The briefing judge scored groundedness **0.95**. Every id, error type, hint and
retry count in the escalation matches the tool output exactly.

**The defect is the scope, and it is upstream of the model.** The alert was
`{"source": "platform.dlq", "severity": "critical", "fingerprint":
"dlq_depth_warning_wait_replay", "group": null}`. The incident is the
wait_and_replay backlog: two rows, one deferred replay, exactly one Tier-1
action. The only thing in the payload that said so was the substring
`wait_replay` inside a free-text fingerprint — and `ALERT_SUBJECT_PROBES` is
keyed on the field rather than on `fingerprint` precisely because that string is
free text nobody validates. So the agent set its scope from the queue it could
observe rather than from the incident it was paged for.

**Why a true sentence became an escalation.** ADR 0008 allows one attempt with
one action. With a four-row scope and one action available, there is no partial
fix to fall back to, so the correct behaviour *given that scope* is to hand off.

> **An over-wide scope does not degrade into a partial fix; it converts into an
> escalation.** The single-action limit is not the defect — it is what makes an
> under-specified alert expensive. Where a run's stated reason for escalating is
> that the work did not fit in one action, suspect the scope before the
> reasoning, and ask what the smallest scope that would have explained the alert
> was. (WO-R2-155 already records the single-action limit as the live constraint;
> this is the first run where it converted a specification gap into a red.)

**Why the previous green does not count as evidence.** Run A of the same
scenario (`5c8895771fbd`, F-007) scoped itself correctly and unprompted on the
same world — its first call was
`list_dlq_messages(remediation_hint="wait_and_replay")`. Nothing changed between
the two runs except sampling.

> **A behaviour that appears when the model happens to read a fingerprint the
> way you hoped is not a behaviour the harness has.** F-006's lesson was that a
> check which never names the subject verifies only that something happened.
> This is its twin one level up: an *alert* that never names its subject
> specifies only that something is wrong. A trajectory that got the scope right
> is evidence the scope was guessable, not that it was given.

**Why no grader could have caught it.** Every dimension was red, so the suite
reported the failure loudly — but nothing in the trajectory, the briefing or the
judge score identifies the *cause*. An escalation that names four
correctly-classified rows reads as diligence, and `failure_class` came back
`unclassified`. The discriminator is not in the run at all; it is in the alert
the run started from. Same shape as F-005: the archive cannot tell you whether
the question was well posed.

**Fix.** ADR 0031. Structural first: `AlertPayload.remediation_hint` carries the
category, and `ALERT_SUBJECT_PROBES` gains `remediation_hint →
list_dlq_messages.remediation_hint` so the handoff guard requires the scoped
listing by value — an unfiltered page wires the argument to `None` and does not
satisfy it. Prompt second: an alerted category *is* the incident, and a mixed
queue is never a reason to escalate. The remediation planner's contradicting
"pick the most impactful action" mixed-DLQ steer is replaced by an ordered rule,
because on this exact alert it pointed at the one `replay_safe` row beside the
subject.

**Two properties of the fix worth reusing.**

> **A guard that costs no tool call is usually asking for something an existing
> guard already needed.** The scoped listing the subject guard demands is the
> same reading that COVERS a same-category replay under ADR 0028, so the two
> guards share one call.

> **When a structural fix makes a class of failure unreachable, check whether it
> also made the accompanying prompt rule untestable.** `dlq_mixed_partial` keeps
> no category on purpose: it is the only scenario left that can fail "a mixed
> queue is not an escalation". Completing the set would have made the suite
> assert the fix rather than measure it.

**Filed, not done.** The platform's DLQ-depth alert producer does not emit the
category, so the guard is inert on real production alerts until it does — inert
being the correct failure mode, which is why this ships first. Recorded as a
test with a docstring rather than a silent gap
(`test_the_dlq_category_field_is_one_of_them_and_is_a_filed_platform_gap`).

## F-009 — A contradiction recorded three times, and the terminal state that was carrying the whole claim

**Date:** 2026-09-08 (no run; caught by reading, before spend). WO-R2-140.

**What happened.** `evals/scenarios/dlq_human_required_escalates.yaml` — the
scenario whose name, description and steering all say *escalate* — asserted
`expected_terminal_state: resolved`, and had since PR #112 on 2026-08-09. It was
next in the paid sequence.

**Evidence that settled it.** Not a run. The platform's own handler:
`mark_dlq_permanent` sets `remediation_hint = human_required` and writes an audit
row, and its description says "Doesn't change job.status — the entry stays in
DLQ". Three readings already agreed with the name and the fourth was the one the
grader reads. Confirming the reply's shape needed one offline run
(`already_marked: true`, `previous_hint: human_required`) and no money.

**Root cause of the delay, which is the interesting part.** The contradiction was
found by ADR 0026's `RESOLUTION_CLASS` work on 2026-09-07 and deliberately not
fixed — recorded instead in ADR 0026's consequences, in a comment at the map
entry, and in a filed work order — because flipping the classification reddens a
scenario queued for a paid run, and a builder does not quietly re-point what a
queued scenario measures. **A contradiction between a scenario and the code is
not always a defect to fix; sometimes it is a decision to route.** Recording it
beside the code is what let it survive until the person spending the money could
take it.

**The failure the fix nearly introduced.** With `resolved`, the lazy trajectory —
read the row, see `human_required`, escalate having fenced nothing — failed on
OUTCOME for free. With `escalated` it reaches the expected terminal state. So
flipping the enum without grading the action would have made the scenario
consistent and strictly weaker.

> **When you change a scenario's expected outcome, ask what the old outcome was
> carrying that nothing else now carries.** Here: the action itself. It is now
> `expected_action_tools` plus a universal `equals` on the fenced `job_id`, which
> fails closed when no fence fired.

**Fix.** `mark_dlq_permanent` is `Resolution.STABILIZES` (ADR 0026's open
classification, resolved): a verified fence escalates with the
`STABILIZED, NOT RESOLVED` briefing naming the row. The scenario expects
`escalated` and grades the fence, the row's own pre-fence hint (`where`-selected
by id), the reply's `previous_hint`, and the briefing text. Both prompts were
re-steered — the investigation planner's rule literally ended
"`human_required` … means `stop`", which is the failing trajectory in the prompt,
and the hypothesis-category table routed a CSV parse error to
`persistent_data_bug`, which auto-escalates before any fence.

**Two limits stated rather than papered over.**

> **When the platform exposes no observation of an action, say so in the scenario
> instead of writing an assertion that implies one.** The fence has no observable:
> no field separates a row an operator fenced from one the classifier categorised,
> and on an already-classified row — every row the agent knows to fence — the
> handler writes nothing at all, not even the audit row. The scenario grades the
> tool's reply plus the row still being listed, and its comments say that is a
> claim about the decision, not about an effect. WO-R2-158 (platform),
> WO-R2-159 (the grader has no `after_tools`).

> **A new scenario shape can fall outside an existing corpus check without either
> being wrong.** `TestFixMapMatchesTheSuite` is scoped to scenarios expecting
> `resolved`, because escalate-only scenarios forbid every Tier-1 tool on purpose.
> This is the first scenario expecting `escalated` that nevertheless requires an
> action, so a prompt routing `human_required` at a tool it forbids would have
> been invisible there. `HINT_ROUTED_TOOLS` plus
> `TestHintRoutedToolsMatchTheSuite` closes it, keyed on the ALERT's own hint —
> which is also what keeps it silent on `saga_stuck`, whose subject is a chain and
> which forbids the fence deliberately.

## F-010 — The subject guard checked the read and never the act

**Date:** 2026-09-08, live run `a0aa257bf865` (`dlq_human_required_escalates`,
≈$0.12). Red on outcome, action, evidence and safety; budget green at 3 of 13
calls. Fix: ADR 0032.

**What happened.** The agent listed the dead-letter queue unfiltered — the
correct read, and the only one that shows a row the platform's classifier has
not touched. It found `3971a293…`, read its parse error, and wrote in its own
plan rationale:

> The human_required and unclassified csv_upload entries (3971a293 and any
> null-hint row) **must not be touched by auto-replay and are left for human
> review.** Replaying the replay_safe slice now is the right first action.

It then called `replay_dlq_by_category(category="replay_safe")`, replaying the
seeded `fc8d2a03` row the scenario forbids acting on, verified that slice empty,
and RESOLVED. Its briefing says *"leaving four unresolved"* and was scored
**1.0 groundedness / 1.0 actionability** by the judge.

**Why this is F-006's other half, not a new species.** F-006 produced the
alert-subject probe guard (PR #177): the alert's subject must have been PROBED
before a `remediate` handoff. That is a claim about a READ, and nothing anywhere
required the ACTION to target the same thing. So the very run F-006 was written
from — `adcdcadd94a3`, `remediate_consumer_lag_success` run A — was only half
fixed. Re-read against the archive:

| | `adcdcadd94a3` (2026-08-31) | `a0aa257bf865` (2026-09-08) |
|---|---|---|
| alert names | `consumer_group: worker-dispatcher` | an unclassified dead-letter row |
| subject probe made | yes, `get_consumer_lag(worker-dispatcher)` → lag 17 | yes, unfiltered `list_dlq_messages()` |
| plan | `replay_dlq_by_category(replay_safe)` | `replay_dlq_by_category(replay_safe)` |
| subject named in the plan | nowhere | nowhere |
| terminal state | resolved | resolved |

The same plan, under two different alerts, eight days apart, one of them the run
that motivated the guard the other one walked through.

**What made it invisible.** Every existing plan guard was satisfied, and the
second run satisfied ADR 0028's read-before-act coverage check *by
construction*: an unfiltered listing covers every slice, so the run had read
strictly more than the guard required. There is no amount of reading that makes
a wrong-target action right, which is the thing the guard family had not yet
said.

**Three things worth keeping.**

> **A 1.0 judge score is a claim about the prose, not about the incident.** This
> briefing earned full marks while naming four unresolved rows, one of them the
> row the run existed to handle. Groundedness measures whether the sentences
> match the evidence; a run that acts on the wrong thing and describes it
> accurately scores perfectly. Both live runs in this family scored ≥0.95.

> **A rule with an unstated precondition will be applied wherever it fits.**
> "Act on the safest slice first" is correct for a bare queue-depth alert on a
> mixed queue, which is what it was written for. The prompt did not say so, the
> agent supplied the precondition itself — *"The alert named no specific
> category, so the mixed-queue rule applies"* — and an alert whose subject the
> harness could not express looked exactly like an alert with no subject.

> **An inert guard is a decision or a gap, and the two are distinguishable.** It
> is a DECISION when the alert names a condition nothing can probe by name — a
> queue's depth, an alert-storm meta-alert. It is a GAP when the alert is about
> something specific for which the payload has no field. This scenario's YAML
> argued the first at length (three sound arguments, one wrong conclusion) and
> filed the residue as WO-R2-161; it was the second, and finding out cost a live
> run.

**The fix, and what it deliberately leaves open.** ADR 0032 adds a fifth plan
guard requiring the action to target the alert's subject — resource, category,
or the unclassified slice — refusing and re-asking once, escalating on the
second offence naming the subject. `AlertPayload.dlq_scope: unclassified` is a
new field rather than a reading of `remediation_hint: null`, because
`payload.model_dump()` makes explicit-null and absent the same object by the
time the state machine sees them, so a key-presence check would be inert offline
and non-inert on every production alert. The RESOLVED-honesty question for a
subject-less mixed queue is answered in the ADR and filed as WO-R2-164 rather
than encoded: it flips `dlq_mixed_partial`'s expected terminal state, which is a
spend decision on a queued scenario and contradicts an accepted ADR.

## F-011 — The carve-out that made the passing trajectory the lazy one

**Where:** `evals/scenarios/saga_stuck.yaml`; both planner prompts;
[ADR 0026](../docs/ADR/0026-a-stabilizer-is-not-a-resolution.md)'s `saga_stuck`
consequence. Found by reading, not by a run — WO-R2-160, decided by the user on
2026-09-08. Cost: $0.

**What it was.** cmd #205 settled `human_required` DLQ rows as *fence, then
escalate*, and carved out one exception in the same change: a `human_required`
row that is the ROOT of a stuck chain was to be escalated with nothing touched,
on the reasoning that a `platform.dag` alert makes the chain the incident and
replaying the root is the human's decision, so a fence would record the opposite
disposition against a decision already handed over.

The carve-out was written carefully, pinned by a prompt test in both planners,
stated in the ADR's consequences, and left to the user rather than taken quietly.
It was still wrong, and the thing that makes it worth writing down is *how* it
was wrong: not a missing fact, but two decisions collapsed into one.

**The premise.** The exception treats the fence and the replay as alternatives.
The platform does not: a fenced row is still replayable **by explicit id** —
only category scans and the default bulk sweep skip fenced rows. ADR 0026 quotes
that sentence and draws the opposite conclusion from it. So the fence forecloses
nothing the human is being asked to decide, and the thing it *does* foreclose is
the next operator's `replay_dlq_by_category` sweep re-running a payload that
cannot succeed — which is not a decision anyone was making.

**What the carve-out cost the scenario, which is the part worth generalising.**
With every Tier-1 tool forbidden, the correct-behaviour claim was "touch
nothing", so the LAZY trajectory — probe the chain, read the row, escalate — was
the *passing* one. `saga_stuck` had already been repaired once for grading
temperament rather than reasoning (cmd #192 gave it the `human_required`
discriminator, so the escalation rests on something the agent reads); the
carve-out left the second half of that defect standing. A run that read nothing
past the chain and stopped still passed on four of five dimensions and could
only fail on the evidence claim.

The rule that falls out, now in `docs/eval-methodology.md`: **derive the
forbidden set from the sanctioned action, never from the terminal state.** An
escalating scenario forbids all seven Tier-1 tools when its correct action count
is zero, and six when it is one. Terminal state is not a proxy for "did nothing".

**The measurement that settled the stabilizer half.** Before writing the claims,
`get_dag_state` on the chain root was read against the pinned v0.6.2 stack
immediately before and immediately after a real `mark_dlq_permanent`, and the two
responses were **byte-identical** — root `dead_letter` at `retry_count: 3`,
descendant `waiting`, `paused: false`. The fence stamps the DLQ row and touches
the chain not at all. That is the difference between asserting a stabilizer
repairs nothing and knowing it, and it is why the run escalates and why the
briefing must say `STABILIZED, NOT RESOLVED`.

**Two things the fix deliberately did not do.**

*It did not put the hint in the alert.* Adding `remediation_hint: human_required`
to the `platform.dag` alert was the cheap way to make the existing corpus check
(`TestHintRoutedToolsMatchTheSuite`, keyed on the alert's own hint) reach this
scenario. It would also have handed the agent the discriminator cmd #192 created
precisely so the escalation would rest on a READ. The check was widened instead:
it now also selects a scenario whose alert names a resource and whose graded
evidence pins that resource's row hint, narrowed to the routed tools that can
name a resource — ADR 0032's own rule reused rather than a second hand-written
list.

*It did not assert a post-fence chain read.* It is the scenario's whole point and
it is still not gradeable: a plan has one verify tool, `fenced_at` lives only in
the DLQ listing, and any `get_dag_state` claim would therefore be read off the
pre-action investigation probe under every `which` — a statement about the world
before the fence dressed as one about the world after it, which is the
cross-satisfiable fake-green this suite has corrected twice already. Said in the
YAML rather than papered over; the briefing carries the claim instead.

**Full record:** [ADR 0033](../docs/ADR/0033-a-human-required-chain-root-is-fenced-then-escalated.md),
[`docs/lessons/live-eval-sequence-2026-09.md` §15](../docs/lessons/live-eval-sequence-2026-09.md).
