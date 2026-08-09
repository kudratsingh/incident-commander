# ADR 0017: The eval run archive is written incrementally, and report.json is its completion marker

* Status: accepted
* Date: 2026-08-09
* Decider: Kudrat Singh

## Context and problem statement

CLAUDE.md invariant 9 makes eval evidence append-only, and `evals/runs/<invocation_id>/` is the
durable record it names — the flat `evals/{reports,trajectories,briefings}/` paths are pointers to
the most recent run, refreshed in place by design. WO-C4-01 removed the `.gitignore` entry that
had kept that record untracked, so archives are now commit-able. What remained was *when* the
archive gets written.

At HEAD it was written all at once, after the suite. `run_all` accumulated `ScenarioResult`s in a
list and returned; `main` then called `archive_run` — the first write of any report, trajectory or
briefing. `run_all` catches `Exception` only (deliberately: widening it would swallow Ctrl-C into
a synthetic crash row), so a `KeyboardInterrupt` at scenario 30 of 37 escaped past the archive call
and 30 scenarios of paid live evidence were discarded at process exit (audit findings S-05, A-14).
A `SIGKILL` or an OOM did the same. That is exactly the F-002/F-003 shape invariant 9 exists to
prevent, reintroduced one layer up: the runner protected the archive from *overwrites* while
leaving it entirely unwritten for the whole duration of the run.

Two adjacent gaps in the same writer. The archive carried no traces (S-07), so joining an archived
run to the prompts and responses that produced it went through the flat, gitignored,
multi-invocation `evals/traces/<scenario>.jsonl` — the one artifact a fresh clone does not have.
And `report.json` was the *first* file `archive_run` wrote (aggregate first, then per-scenario
files), so a crash mid-archive left a directory that looked complete and was not (S-08).

## Decision drivers

* A run that has been paid for — in dollars, in platform side effects, in wall time — must leave
  behind everything it completed, whatever kills it. Losing 30 scenarios to a Ctrl-C is the same
  loss as deleting them.
* The archive is the *evidence*, and evidence must be self-describing: a reader (human or script)
  must be able to tell a complete run from a truncated one without external context.
* Exclusive-create (`open("x")`) is the load-bearing discipline of the F-002 fix, pinned by
  `TestRunArchiveIsAppendOnly`. Nothing added here may create a path that silently overwrites.
* The flat trace file is the only record of a scenario killed *mid*-scenario. Whatever the archive
  does with traces, it must never open that file for writing.
* No new failure mode may be introduced into the eval writer while the runner cannot be executed
  (ADR 0011 freeze) — the design has to be provable from unit tests against `tmp_path`.

## Considered options

1. Leave the bulk archive; wrap `main`'s `run_all` call in `try/finally` so a partial result set is
   archived on the way out (the audit's own sketch for S-05).
2. Write each scenario's artifacts into a staging directory `evals/runs/.tmp-<id>/` and
   `os.rename` it into place as the final atomic step (the audit's own sketch for S-08).
3. Stream each scenario's artifacts into `evals/runs/<invocation_id>/` as it completes, with
   `report.json` written last as the completion marker (chosen).

## Decision outcome

**Option 3.** The archive is built incrementally, and its completion is a fact on disk.

### 1. The run directory is created before the first scenario

`main` computes `target = _RUNS_DIR / invocation_id` and creates `target`, `target/trajectories`,
`target/briefings` (and `target/traces` when `EVAL_TRACE_DIR` is set) with
`mkdir(exist_ok=False)` — before `run_all`, after every preflight refusal. An invocation-id
collision therefore fails loudly with a `FileExistsError` before a single tool call is paid for,
rather than landing two runs in one directory. A refused `--live` run still creates nothing.

### 2. Each scenario streams its own evidence

`run_all` takes `on_result: Callable[[ScenarioResult], None] | None`, invoked once per scenario on
both the success path and the `_crashed_result` path, *after* the result is appended and *outside*
the `except Exception` block — so an archive failure aborts the suite loudly instead of being
recorded as a scenario crash. `main` passes `archive_scenario`, which exclusive-creates
`trajectories/<scenario>.json`, `briefings/<scenario>.json`, and the trace slice below.

The narrow `except Exception` in `run_all` is unchanged and stays narrow. Streaming is what makes
it safe: the interrupt still terminates the suite, it just no longer takes the evidence with it.

### 3. The archive carries its own trace slice

When `EVAL_TRACE_DIR` is set and `<trace_dir>/<scenario>.jsonl` exists, `archive_scenario` reads
it and copies the lines whose parsed `invocation_id` equals this run's into
`traces/<scenario>.jsonl`. Records belonging to other invocations, records with no
`invocation_id` (pre-invocation-id vintage — the same convention `scripts/estimate_cost.py` uses),
and unparseable lines (a killed run can leave a half-written final line) are excluded. The flat
file is opened read-only on every path: it remains the canonical incremental record, and it is the
only evidence of a scenario killed *during* its own execution, before `archive_scenario` fires.

A consequence to state plainly, since it is a cost: committed archives now contain full prompts and
responses and are therefore substantially larger. That is the point — an archived run without its
traces cannot be re-read. If repository size later becomes a real constraint, the answer is a
future ADR on retention or compression; it is explicitly **not** license to re-ignore
`evals/runs/`, which is the failure this cluster just finished undoing.

### 4. `report.json` is written last, and its presence is the completion marker

`finalize_archive(target, report)` exclusive-creates `report.json` and nothing else, and it is the
last write into the archive — after the final scenario, before the flat pointers are refreshed.
The semantics are therefore:

* `evals/runs/<id>/report.json` **present** — the suite finished; the aggregate is authoritative.
* `report.json` **absent** — the run was killed, crashed, or is still in flight. The per-scenario
  files present are first-class evidence of the scenarios that did complete. They are not garbage
  and must not be cleaned up (invariant 9 covers them).

`archive_run` keeps its exact signature as the one-shot equivalent for callers holding a complete
run in memory, and now composes the same helpers in the same order, so it too writes `report.json`
last.

### Why the alternatives lose

**Option 1 (`try/finally` bulk archive)** protects against exactly the failures Python is still
alive to handle. `finally` does not run on `SIGKILL`, on an OOM kill, on a power loss, or on a
`os._exit`; and a live suite is long enough that those are the realistic kills, not the polite
ones. It is also strictly weaker than streaming even where it does run: it archives at the end,
so everything between the last scenario and the crash is still a window in which the whole run is
in memory and nowhere else. Streaming makes the window one scenario wide, which is the smallest it
can be without changing what a scenario is. `try/finally` buys nothing streaming does not already
provide.

**Option 2 (tmp-dir + rename)** solves the completion-marker problem — a directory that exists is
complete, because the rename is atomic — but it solves it by making killed runs invisible. Once
archiving is incremental, the evidence of a killed run is precisely what we want to keep, and this
option strands it under a dot-prefixed `.tmp-<id>` name that reads as garbage, is skipped by every
glob over `evals/runs/*`, and is the first thing a cleanup script deletes. It buys atomicity we do
not need: `report.json`-written-last already distinguishes complete from partial with one
`exists()` check, without a second name for the same directory and without rename-at-end machinery
that must itself be crash-safe. Rejected on the same principle that motivates the whole ADR —
partial archives are evidence, not garbage.

### Consequences

Positive:

* A Ctrl-C, crash, or hard kill mid-suite now costs at most the in-flight scenario. Everything
  already completed is on disk, in the archive, with its traces.
* Complete-versus-partial is decidable from the filesystem, by one `exists()` check, forever.
* An archived run is now self-contained: report, trajectories, briefings and the prompts and
  responses that produced them, all under one committed directory.
* An invocation-id collision fails before any spend rather than after the suite.

Negative:

* Committed archives grow considerably, because trace slices carry full payloads. Mitigation: the
  slice is filtered to one invocation (not a copy of the accumulating flat file), and retention is
  a future ADR if it bites.
* An archive write now happens between scenarios, so a bug in the writer can abort a live suite
  mid-run — where previously it could only fail after everything had already run. Mitigation:
  every write is exclusive-create into a directory created empty at run start, so the only
  realistic failure is a genuine id collision, which must abort; and that path is unit-pinned.
* `SIGKILL` itself, and a genuine multi-scenario live run, are not reproducible in pytest. The
  claim that a hard kill costs at most the in-flight scenario rests on the per-scenario write
  ordering, which *is* proven in unit tests; its first real-world confirmation is the
  post-freeze campaign run.
* Under ADR 0011 the new writer does not execute at all until that run, so this ADR's live
  acceptance is deferred with it.

Revisit trigger: `evals/runs/` growth becoming an actual repository problem (retention/compression
ADR), or a consumer needing to distinguish "killed" from "still in flight" — both are currently
"no `report.json`", and telling them apart would need a start marker, which nothing needs today.

## More information

Findings S-05 and A-14 (in-memory-until-the-end archive), S-07 (archive omits traces), S-08
(`report.json` written first). Work order WO-C4-02; depends on WO-C4-01 (`evals/runs/`
un-gitignored, PR #90).

Related: CLAUDE.md invariant 9 (append-only eval evidence — this is its writer),
`study/findings.md` F-002/F-003 (the trace truncation and the erased Run 001 trajectories that
motivate the whole discipline), ADR 0011 (the eval freeze that defers live acceptance), ADR 0013
(run provenance — the archived `report.json` this marker completes), and `docs/runbook.md`
§"Commit the run archive".
