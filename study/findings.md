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
