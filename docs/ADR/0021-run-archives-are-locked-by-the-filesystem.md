# ADR 0021: Run archives are locked by the filesystem, not by discipline

* Status: accepted
* Date: 2026-08-21
* Decider: Kudrat Singh

## Context and problem statement

CLAUDE.md invariant 9 makes eval run data append-only, and ADR 0017 built the writer that honors
it: exclusive-create on every file, `report.json` last as the completion marker. But every one of
those protections binds only the runner's own hands. On disk, nothing refuses anything. An audit
found `chflags uchg` applied in exactly one place in the workspace — `context/pack.sh`, session
archives only — while under `evals/runs/`, 0 of 371 files carried any protection at all. A routine
cleanup of a retired second checkout then came within one command of destroying 195 run files that
existed nowhere else: four complete runs, two of them 37 scenarios each.

The `context/` convention (context/README.md, "Archives cannot be deleted, and that is enforced
rather than requested") already solved this exact problem for session archives: written read-only,
flagged user-immutable, unlock is a rare documented act. It even cites invariant 9 as its model —
"except here it is enforced by the filesystem instead of by discipline." Run archives are the
invariant's *namesake* artifact and had the weaker half of that sentence.

## Decision drivers

* The threats that remain after ADR 0017 are all external to the runner: `rm -rf`, `git clean
  -fd` (untracked archives are exactly what it deletes), a cleanup script pointed at the wrong
  checkout. Discipline does not compose across sessions, agents, and reflexes.
* A killed run's partial rows are first-class evidence (ADR 0017) and cost real money; they must
  not wait for a finalize that will never come before gaining protection.
* The flat trace files under `evals/traces/` are APPENDED to by later invocations (the F-002
  fix). Anything a future run must write to must never be locked.
* CI is Linux: no `chflags`, no `uchg`. The lock must degrade to `chmod` there and must be
  assertable in unit tests on either platform.
* A filesystem that refuses `chmod`/`chflags` (network mounts) must not turn a completed —
  possibly paid — run into a crashed one. By lock time the bytes are durable; the lock is
  enforcement on top of evidence, never a precondition for it.

## Considered options

1. Keep discipline + docs, add a runbook warning (status quo, louder).
2. Lock the whole `runs/<id>/` tree once, at finalize.
3. Lock each file the moment its write completes; seal the directory tree at finalize (chosen).

## Decision outcome

**Option 3.** Two moments of enforcement, matched to the two moments ADR 0017 defined:

1. **Per file, at archive time.** `_archive_trajectory`, `_archive_briefing` and
   `_archive_trace_slice` call `_lock_path` the moment their exclusive-create write completes:
   write bits cleared (`chmod a-w`), then `uchg` where the platform supports it (guarded by
   `hasattr(os, "chflags")`). A run killed at scenario 30 of 37 leaves 30 scenarios of
   individually locked evidence — protected the instant it was durable, not the instant the
   suite happened to finish.
2. **Per tree, at finalize.** `finalize_archive` writes `report.json` — the marker that says
   "nothing will ever write here again" — and then locks everything under `runs/<id>/`
   bottom-up, directories last. This is the exact earliest moment directory locks are safe:
   `uchg` and `a-w` on a directory refuse new entries, which mid-run would refuse the archive
   its own remaining writes.

`_lock_path` is best-effort and idempotent: an already-immutable path is skipped (re-`chmod` of a
`uchg` file is itself EPERM), and any `OSError` is printed (`archive lock skipped (...)`) and
swallowed. Locking failure downgrades enforcement, never evidence.

A partial archive's *directories* stay writable forever — there is no process left to seal them,
and it costs nothing: every invocation gets a fresh `invocation_id` and every file is
exclusive-create, so no legitimate writer ever targets an existing run directory. On macOS the
per-file `uchg` still refuses deletion of partial rows outright; on Linux, partial rows are
read-only but deletable until finalize seals the parent — the gap is confined to killed runs on
non-Darwin dev machines, and closing it would require locking directories a live run still needs.

The deliberate unlock is documented in docs/runbook.md ("Completed archives are locked on disk"),
mirroring context/README.md: `chflags -R nouchg` then `chmod -R u+w`, rare and announced.
Unlocking to *edit* run data is invariant 9's definition of a bug; migration and a future
retention ADR are the legitimate uses.

### Why the alternatives lose

**Option 1 (louder discipline)** is the arrangement that already failed. The near-miss ran under
a constitution that says "never destroyed" in bold; the command that nearly destroyed 195 files
was not malicious, it was routine. Warnings do not intercept `git clean`.

**Option 2 (finalize-only)** leaves every killed run's evidence — precisely the runs whose
evidence is irreplaceable, since re-running cannot reproduce a crashed attempt — with zero
protection for its entire existence. It also leaves a live run's already-archived scenarios
unprotected for the duration of the suite, which for a 37-scenario live campaign is hours. The
per-file lock costs two syscalls per file and removes both windows.

### Consequences

Positive:

* `rm -f`, truncation, and overwrite of any archived file are refused by the kernel on both
  platforms; on macOS, `git clean -xfd` and `rm -rf` of whole archives are refused too — the
  same four-row refusal table context/README.md verifies for session archives.
* A re-run colliding with an existing run id still fails loudly at `mkdir(exist_ok=False)` /
  exclusive-create, before any spend (ADR 0017); the seal is now a second wall behind that
  guard, pinned by `TestCompletedArchiveIsLocked`.
* The lock is asserted where it is used (architecture principle: controls asserted at point of
  use): unit tests assert missing write bits and refused writes on any platform, and the `uchg`
  layer specifically where `os.chflags` exists.

Negative:

* Committed archives are read-only in every checkout; a `git checkout` that must *replace* an
  archived file (it never legitimately must) would fail. Acceptable: that failure is the
  invariant working. Note git does not preserve `uchg` across clones — a fresh clone's
  protection is its git history, not flags; the flags protect the machine where the run
  happened, which is where the irreplaceable uncommitted copy lives.
* Anything that deletes eval workspaces wholesale — including this repo's own tests if they ever
  finalized an archive into pytest's `tmp_path` without unlocking — must now use the documented
  unlock first. The unit suite ships a `_unlock_tree` teardown for exactly this.
* On exotic filesystems the lock may silently not hold (logged, not fatal). The floor is the
  status quo ante; the ceiling is real enforcement. Never worse, usually better.

Revisit trigger: a retention/compression ADR for `evals/runs/` growth (it inherits the unlock
path defined here), or a platform where even `chmod` is refused becoming a supported dev
environment.

## More information

The near-miss: 2026-08-21 audit of a retired second checkout — 195 run files, four complete runs,
one command from erasure; the audit that found `uchg` applied in exactly one place. Related:
CLAUDE.md invariant 9 (this is its filesystem half), ADR 0017 (the writer this seals, and the
partial-archive semantics the per-file lock protects), context/README.md (the convention this
ports, including the unlock etiquette), study/findings.md F-002/F-003 (the losses that made the
rule), docs/runbook.md §"Completed archives are locked on disk" (the operator view).
