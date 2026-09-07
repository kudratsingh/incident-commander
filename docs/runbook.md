# Runbook

Operating the agent itself: setup, day-to-day, live eval workflow, kill switch.

## Environment prerequisites

- **Docker Desktop** — needed for `demo/compose.yml` and integration tests
- **uv** — Python package manager. Install: https://docs.astral.sh/uv/
- **`.env` with real values** — copy `.env.example` and fill in. Do NOT commit `.env`.

Required keys:
```
ANTHROPIC_API_KEY=sk-ant-api03-...
PLATFORM_MCP_URL=http://localhost:8001/mcp
PLATFORM_REST_URL=http://localhost:8000
PLATFORM_TOKEN=sa_...               # minted with make bootstrap-token
PLATFORM_WEBHOOK_SECRET=<from platform config>
DATABASE_URL=postgresql://...       # agent's own DB, separate from platform
```

## Day-to-day commands

```bash
make setup             # uv sync + pre-commit hooks + pull pinned platform image
make check             # ruff + mypy strict
make test              # unit + integration (containers auto-managed)
make eval              # offline eval suite (canned data, no tokens spent)
make trace-report      # regenerate human-readable trajectory files
```

All source-code work should stay green on `make check && make test && make eval` before push.

## Live eval workflow

Live eval spends tokens and (for remediation scenarios) mutates the platform. Two-step procedure:

### 1. Seed the platform state you want to test

For remediation scenarios to prove something real, the platform needs to be broken in a way the agent can detect and fix. Use `scripts/chaos_setup.py` (via the `make chaos-*` targets):

```bash
# Consumer lag scenarios — restart_consumer_group is the fix
make chaos-kill-consumer

# DLQ scenarios — replay_dlq_by_category / replay_dlq_by_ids is the fix
# (replay_dlq_messages was demoted to legacy by the v0.4.0 categorization
# tools; no scenario expects it, so watching for it is watching for a call
# the agent will never make. mark_dlq_permanent is the human_required path.)
make chaos-poison

# Cache scenarios — invalidate_cache_key is the fix
make chaos-saturate

# Investigation scenarios (bad-deploy correlation)
make chaos-bad-deploy
```

Each hook self-cleans on TTL (5–10 minutes). Chaos setup requires the platform booted with `CHAOS_ENABLED=true` (default in `demo/compose.yml`) and a token with `chaos:invoke` scope.

`PLATFORM_MCP_URL` and `PLATFORM_TOKEN` in `.env` are enough — the `chaos-*` recipes hand them to the script themselves (WO-R2-89). Until then they did not: make's `-include .env` sets a *make* variable, the script reads `os.environ`, and nothing bridged the two, so every one of these targets aborted with "PLATFORM_MCP_URL and PLATFORM_TOKEN must be set (env or --flag)" no matter how correct your `.env` was. Because make now expands these values, keep them **unquoted** in `.env` (make keeps the quotes; dotenv strips them).

If your service-account token was minted without chaos scope, regenerate:
```bash
uv run python scripts/bootstrap_agent_token.py --scope chaos:invoke
```

### 2. Run the live eval

```bash
make eval-live ONLY=remediate_consumer_lag_success
```

`ONLY=` is not optional, and it names scenarios by **full scenario name**.

A bare `make eval-live` is refused at Makefile parse time with **exit 2**,
before anything runs, and `python -m evals.runner --live` without `--only` is
refused by the runner with the same exit 2 — before the settings load, so it
depends on nothing. An unfiltered `--live` is the whole suite against one
shared platform, with real spend and no reset between scenarios; run one at a
time with a reset between, the full protocol is below.

(Until 2026-09 neither refusal existed. A bare `make eval-live` was stopped
only by the exit-8 canned-only gate, which fires because six scenarios declare
no live leg — a fact about `evals/scenarios/`, not about the command, and one
that would stop being true the moment those six gained a live leg.)

A live `ONLY=` pattern must be a scenario's full name, and a pattern that is
not one is refused with **exit 2** listing the scenarios it would have matched.
This is load-bearing, not pedantry: `ONLY=dlq_backlog` used to select
`dlq_backlog` *and* `remediate_dlq_backlog_success`, the read-only one drained
the seeded `replay_safe` pool before the remediation was graded, and the ADR
0020 gate stayed quiet because only one of the two mutates. A name that is a
prefix of a longer name still selects itself alone — exact match wins — so
`ONLY=dlq_backlog` runs `dlq_backlog`. Comma-separated exact names still work.
`--smoke` and offline `make eval ONLY=` keep substring matching (`SMOKE_ONLY`
is a documented substring override, and neither path spends or shares state).

Trace files land in `evals/traces/*.jsonl`; the formatter turns them into readable stepwise trajectories in `evals/reports/human/*.txt`.

**Cost:** roughly $0.05 per read-only scenario, $0.07 per remediation scenario. Current suite of 38 (~32 live: 24 read-only, 8 remediation) is ~$1.70 of tokens end to end — but never in one invocation, for the reason above. A smoke pass is ~$1.15 of that; the remediation scenarios are the rest, paid one run at a time.

**Side effects:** remediation scenarios fire real Tier-1 mutations against the platform. Idempotent — repeat runs with the same `(incident_id, tool, args)` hash return the cached result. But the *first* run of a scenario does apply changes.

### 3. Restore state (optional)

Chaos effects TTL-expire on their own. If you want to accelerate cleanup:
```bash
make chaos-restore     # clears leftover kill/latency flags on worker-dispatcher
```

**Verifying the consumer actually came back — ask Kafka, not the lag metric.**
The lag number in Redis is written by a separate background loop that keeps
refreshing while the consumer is dead, so "the lag key got a fresh write" is a
fake-green check: a dead consumer passes it. The real test is whether the
group has a live member:

```bash
# In the platform's Redpanda container:
rpk group describe worker-dispatcher
```

Healthy = a live member in the group, holding all 6 partitions, with lag
draining to 0. A corpse cannot pass this — Kafka won't report a member that
isn't there. Caveat: wait 10–15 seconds after the restore before running it,
or you'll catch the group mid-rejoin (`PreparingRebalance`) and think it
failed when it didn't.

## Live eval protocol (post-hardening)

Written after the Phase-6 seven-run live eval that produced the five-bucket noise-source taxonomy (see [`docs/lessons/live-eval-noise-sources.md`](lessons/live-eval-noise-sources.md)). Run one scenario at a time with an explicit reset between them.

### Pre-run checklist

Work through this **before spending anything**. It is ordered: each step is
cheaper than the one after it, and every item exists because skipping it once
cost real money. The 2026-08→09 sequence spent ~$2 on a stage whose results
had to be thrown away, and produced seven red scenarios that were all
harness artifacts — see [`docs/lessons/live-eval-sequence-2026-09.md`](lessons/live-eval-sequence-2026-09.md).

1. **Read the gotchas ledger.** `## Gotchas ledger` in the campaign repo's
   `docs/00-RESUME-HERE.md` (`audit-ws`, a sibling checkout — it is not in
   this repo, so there is no relative link to follow). Read it **now**, not
   after something breaks. This step is here because a recorded lesson that
   the procedure does not route you to is worth nothing: the hand-rolled
   read-only stage that cost ~$2 on 2026-08-30 had been described in that
   ledger's own repo nineteen days earlier, and nobody read it at the moment
   the choice was made. Also skim the lessons doc linked above.

2. **Run the runbook's exact commands.** Not an equivalent, not an inlined
   version, not "the same thing but filtered". `make eval-smoke` is the
   read-only stage; a hand-assembled `ONLY=` list under the write token is
   not, and the difference is invisible until the trajectories show Tier-1
   writes. If the documented command will not do what you need, that is a
   finding to raise with the user **before** spending, not a thing to work
   around.

3. **Start from a known world**: either a fresh stack (`make demo`) or an
   explicit `make eval-reset`. After any `docker compose down -v`, re-mint
   and re-paste the tokens (see the bring-up block below).

4. **Audit the world against the seeded baseline.** A green reset does not
   prove a clean world — reset only undoes what the eval seeds. Check all
   four, and expect exactly:

   | Check | Expected |
   |---|---|
   | DLQ total | **4** |
   | Active alerts | **3** |
   | Redis `chaos:*` keys | **0** |
   | `worker-dispatcher` lag | **0**, with `lag_known: true` |

   Anything else is a stop. `lag_known: false` is not "lag 0" — it means the
   metric is unreadable, and a run started on it grades the agent for a
   world nobody can see. Stray alerts above the baseline 3 are the known
   organic-alert case (WO-R2-131/132): they survive reset, they re-fire
   hourly, and on 2026-09-01 three of them aborted a run before any spend.

5. **Traffic only where the scenario needs it.** `make traffic` is required
   for `remediate_consumer_lag_success` and for nothing else. Every other
   remediation scenario seeds its fault whole. Running traffic during an
   unrelated scenario adds load the scenario did not ask for.

6. **Hold the machine awake, and run in the background.** A paid run needs
   its own untimed `caffeinate -dims`; the harness's `caffeinate -i -t 300`
   is a five-minute timer, shorter than a single scenario. And a foreground
   run dies to the 10-minute command timeout, wasting the spend — long runs
   go in the background, always.

7. **Reset after** the scenario, not just before it.

8. **On any failure: STOP.** Investigate before running the next scenario.
   A second run against a world the first one left dirty cannot be
   interpreted, and two consecutive different-looking failures almost
   always mean shared state rather than two bugs. Bucket the failure before
   opening a code file, and check whether the harness, not the agent,
   produced it.

```bash
# Bring-up. `make demo` now stops after compose-up (no embedded eval).
# Expect FIVE long-running healthy containers (postgres, redis, redpanda,
# platform=MCP on 8001, api=REST+consumers on 8000) plus two exited
# one-shots (migrate, redpanda-init). Three healthy containers means the
# pre-completion compose — no consumers, no consumer_lag scenarios.
make demo

# `bootstrap-token` PRINTS the tokens. It does not write .env, and nothing
# downstream reads its stdout — YOU paste the printed values into .env.
# Skipping the paste fails later with an auth error that reads like a
# scope problem. And note what invalidates them: `docker compose down -v`
# destroys the database they were minted against, so every previously
# issued token is dead even though .env still holds a plausible-looking
# one (symptom: `Invalid token` on the drift check, right after a
# bring-up that looked clean). After any `down -v`, re-mint and re-paste.
make bootstrap-token

# Tracing. eval-live sets EVAL_TRACE_DIR inline, but exporting it here
# means every direct `evals.runner` invocation also gets traced. The live
# probe knobs (VERIFY_PROBE_ATTEMPTS=6 etc.) no longer need exporting: a
# .env copied from .env.example ships them, and a --live run still at the
# canned-equivalent values prints a preflight warning (see the knobs table
# below).
export EVAL_TRACE_DIR=evals/traces

# 1) Smoke pass FIRST — read-only scenarios catch any wire-shape
#    surprise from the current pin before you spend on a Tier-1
#    remediation attempt. ~$1 of tokens. Runs under the read-scoped
#    PLATFORM_SMOKE_TOKEN, so "read-only" is enforced by the platform
#    (a Tier-1 attempt 403s and grades as an escalation), not by the
#    scenario list.
#    WHICH scenarios run is derived from the YAMLs, not listed anywhere:
#    every scenario that seeds no chaos and expects no action tool, minus
#    any that declares a `smoke_exclusion:` reason of its own. The run
#    prints the count and every hold-back it honoured — read those, they
#    are its own record of what it covered. See docs/eval-methodology.md,
#    "The read-only smoke pass".
#    Override with SMOKE_ONLY= (command line or .env) to run a subset,
#    e.g. re-checking one scenario against a new pin. The override can only
#    NARROW the derived selection, never widen it: it goes through --only,
#    so both refusals still apply to it: a pattern that
#    matches no scenario refuses the whole selection (exit 2, and the
#    per-pattern counts say which), and a selection holding any scenario
#    outside the derived set — chaos_setup, expected_action_tools, or a
#    declared smoke_exclusion — refuses with exit 6, naming each scenario
#    and its reason. An override cannot smuggle a chaos-seeding OR a
#    write-declaring scenario into the read-only stage.
make eval-smoke

# 2) Remediation scenarios, one at a time, with reset between.
#    Each scenario declares its own chaos_setup in the YAML (PR #54).
#    This is now ENFORCED, not remembered: the runner refuses a --live
#    selection holding more than one state-mutating scenario and exits 7
#    before any spend (ADR 0020). The old `eval-live-remediation` batch
#    target is deleted — it selected nine of them at once, against one
#    shared platform, with no reset between.
#
#    The reset is what makes one-at-a-time work, so check it is pointed at
#    the stack under test: `make eval-reset` echoes the compose file and
#    service it resets. Defaults are demo/compose.yml + api; a .env that
#    overrides PLATFORM_COMPOSE at the platform's own dev stack cleans a
#    different database and reports success.
#    TRAFFIC PREREQUISITE — remediate_consumer_lag_success ONLY.
#    Start `make traffic` in a second terminal and KEEP IT RUNNING for the
#    whole scenario. Lag is arrival minus service: the runner's chaos_setup
#    supplies the service half (kill_consumer), and `make traffic` is the
#    only producer of the arrival half. Without it the backlog stays at 0,
#    the precondition burns its full 10x15s and aborts, and the run reports
#    a fault that was never manufactured.
#
#    Do NOT try to bank a head start first. `make traffic UNTIL_LAG=30`
#    before launching the runner builds NOTHING: the consumer is still
#    alive and healthy at that point, so it services every job as fast as
#    the loop submits it and lag stays flat at 0 — the command simply never
#    returns. Lag can only accumulate AFTER the runner seeds the kill, which
#    happens inside the run. The raised precondition IS the head start: it
#    waits up to 2.5 minutes for the backlog to build past 20 while traffic
#    pumps. Start traffic, then start the run, and let the precondition wait.
#
#    The other scenarios below need no producer — their faults are seeded
#    whole (DLQ rows, a hot cache key, a stuck chain) rather than accumulated.
make eval-live ONLY=remediate_consumer_lag_success && make eval-reset
make eval-live ONLY=remediate_dlq_backlog_success  && make eval-reset

make eval-live ONLY=remediate_stale_cache_success   && make eval-reset
make eval-live ONLY=remediate_runaway_saga_success && make eval-reset

# The last two joined this list at the v0.6.0 pin (wave-10), because the
# platform capability each was waiting on shipped. They self-seed like the
# others and each aborts pre-spend if its premise is missing:
#   * remediate_stale_cache_success — create_stale_cache writes the hot
#     key, and get_cache_key_info (plat #146) now reads it back, which is
#     what the precondition asserts. Verify RE-READS THAT KEY with
#     get_cache_key_info and expects exists=false. It used to re-read
#     get_redis_health, and the 2026-09-07 paid run is why it no longer
#     does: nothing in this world reads that key, so the server-wide
#     keyspace counters cannot move when it is deleted (hits frozen at
#     209 across six polls). The verification was unpassable by
#     construction and the agent escalated, correctly. ADR 0025 now
#     refuses such a plan before it executes.
#   * remediate_runaway_saga_success — create_stuck_dag (plat #148) builds
#     a genuinely stuck chain: completed upstream parent, dead-lettered
#     root, descendants held in `waiting` behind it. The remediation is
#     replay_dlq_by_ids on the ROOT, which is the platform's own tested
#     un-stick path; pause_dag only stabilizes and is no longer graded as
#     a fix by any scenario.
# saga_stuck also became live-capable in the same change, on its own
# chain. It is a read-only escalation scenario, but it now declares
# chaos_setup, so it is NO LONGER part of the read-only smoke pass (a
# --smoke selection carrying chaos is refused, exit 6) and belongs to the
# one-at-a-time stage below with a reset after it:
make eval-live ONLY=saga_stuck && make eval-reset

# The two saga scenarios deliberately use DIFFERENT chain_names. Replaying
# a chain's root completes it, and create_stuck_dag refuses a drifted
# chain (409 stuck_chain_name_in_use) rather than rebuilding it, so a
# shared name would make each run depend on the other's order.

# alert_storm and remediate_verify_fails are NOT in this list: they are
# canned-only (use_live_mcp/use_live_llm false in the YAML, with the
# reason and the unblocking platform change commented above the flags),
# because the live platform cannot manufacture their faults — the platform
# has three alert producers and none of them emits a burst, and nothing
# seeds a consumer group that stays dead. Selecting one under --live is
# refused with exit 8 before any spend; they run (and must stay green) in
# the offline suite instead.

# After any consumer restart — the agent's restart_consumer_group or a
# manual restore — confirm liveness with `rpk group describe
# worker-dispatcher` (see "Restore state" above), never the lag metric.

# Do NOT drop the reset between scenarios. The one-fault-one-scenario
# protocol exists because shared platform state between runs was the
# single largest source of noise in the seven-run audit — see the
# lessons doc's third bucket, "shared mutable environment".
```

Every `make eval-live` invocation writes JSONL traces to `evals/traces/` and renders per-scenario human reports to `evals/reports/human/*.txt` (via the `format_traces.py` step chained into the target).

A filtered run (`ONLY=...`) writes its own report file and **can no longer feed the gate or the baseline**: the report self-describes via `only_patterns` (ADR 0013), `make eval-reg` exits 2 when the newest report is a filtered one, and `make eval-reg ONLY=x` / `make baseline ONLY=x` refuse at Makefile parse time before anything runs (A-03 — `study/runs.jsonl` records a full-suite report lost to a later filtered run). That specific loss is now impossible: reports are versioned (`evals/reports/report.<stamp>.<invocation_id>.json`) and never overwritten, so the earlier full-suite report is still on disk. The gate resolves the **newest** one via `evals/artifacts.py` and prints which file it graded; the archive under `evals/runs/<invocation_id>/` remains the durable per-run record.

`make eval-reset` shells into the platform app via `docker compose -f $PLATFORM_COMPOSE exec $PLATFORM_SERVICE`. `PLATFORM_COMPOSE` defaults to `demo/compose.yml` — **this repo's own demo stack**, the one `make demo` brings up — and `PLATFORM_SERVICE` defaults to the `api` container in it (both demo services share one database, and `api` is the REST app that owns seeding). Point them at a sibling `incident-platform` checkout only if that is genuinely the stack under test, either per-invocation or once in `.env` (the Makefile `-include .env`s it, so a non-default layout is a one-time setup rather than a flag you have to remember on every call).

**What `make eval-reset` does NOT clear.** Reset undoes what the eval *seeds*: `chaos:*` keys, the lag cache, the DLQ fixture pool, `hot_set`, optionally idempotency records. It has no idea about state the world produced **organically** — anything raised by a platform loop rather than by a scenario. Concretely, it does not clear **SLO / non-chaos alerts**, and it cannot: those are generated by the platform's own SLO evaluator reading the seeded fixtures as if they were real traffic (4 of the 7 seeded jobs are dead-lettered, which is a fast burn by any honest reading), so resetting the fixtures **re-arms** the alert rather than removing it. Worse, the fast-burn dedup key is bucketed by the hour, so a suppressed alert **returns hourly** instead of staying suppressed. A green `eval-reset` therefore does not mean a clean world. Audit against the seeded baseline before every paid run (see the pre-run checklist above), and treat a stray alert as a stop-and-investigate, not as background noise — on 2026-09-01 three surviving `SLO fast burn` alerts were caught this way and the run was aborted before any spend. Tracked as **WO-R2-131** (reset must sweep organic alerts) and **WO-R2-132** (the platform-side fix); the current mitigation is `SLO_EVALUATION_INTERVAL_SECONDS: "0"` on both demo services in `demo/compose.yml` (PR #180), which disables the loop in the eval world only.

Getting this wrong does **not** reliably fail. The exit-2 guard only checks that `$PLATFORM_COMPOSE` is a file that exists, so a stale `PLATFORM_COMPOSE=../incident-platform/docker-compose.yml` in `.env` — the value this runbook itself used to give — passes the guard, resets the platform's dev Postgres and Redis, prints success, and leaves the stack you are actually evaluating untouched. The recipe echoes the compose file and service it is about to reset for exactly this reason: read that line, do not trust the exit code. The guard catches a missing file, nothing more. Pass `PURGE_IDEMPOTENCY=1` to also `DELETE` idempotency_records (24h TTL from platform ADR 0010 handles the common case; opt-in purge for guaranteed-fresh cache). Only the literal `1` enables it: the gate used to be make's `$(if ...)`, which asks whether the value is a non-empty string rather than whether it is true, so `PURGE_IDEMPOTENCY=0` — and `no`, and `false` — deleted the rows (WO-R2-89).

Environment variable knobs for the live path (see [ADR 0006](ADR/0006-verification-is-a-polling-window.md)):

| Var | Default | Live-recommended | Meaning |
|---|---|---|---|
| `VERIFY_PROBE_ATTEMPTS` | 1 | 6 | Bounded polling window on VERIFYING. Default keeps canned runs single-probe. 6 proved out in the 2026-08-03 campaign; size scenario caps for it. |
| `VERIFY_PROBE_DELAY_SECONDS` | 15 | 20 | Delay between polling attempts. Size to the slowest verify probe's freshness. |
| `INVESTIGATE_REPROBE_ATTEMPTS` | 0 | 1 | Investigation-side freshness re-probe ([ADR 0009](ADR/0009-investigation-freshness-reprobe.md)): when a cached read kills a fixable hypothesis at ≥0.7, re-read it fresh before accepting. Default 0 keeps canned runs byte-identical. |
| `INVESTIGATE_REPROBE_DELAY_SECONDS` | 20 | 75 | Delay before the freshness re-read. Must **straddle** the cached tool's staleness window, not merely be shorter than it (lag cache: 60s → 75). |

`.env.example` now ships the live-recommended values for these knobs uncommented (canned/offline runs are unaffected — the runner forces single-probe and no-reprobe whenever the platform is a placeholder), and a `--live` run that still has them at canned-equivalent values prints a preflight warning. This table stays the source of record.

**Why the reprobe delay is 75 and not 20** (live run 2026-08-31, "run B" —
[`docs/lessons/live-eval-sequence-2026-09.md`](lessons/live-eval-sequence-2026-09.md)).
A re-probe exists to answer "is this reading stale?", which it can only do by
landing in a *different* cached generation than the first read. At 20s both
probes fell inside the same ~60s cached reading of the consumer lag and
returned the identical number. The agent read `lag=4` twice, concluded the
metric was genuinely static, and — correctly, on the evidence it had —
escalated instead of remediating. The graded failure was manufactured
entirely by this knob: nothing was wrong with the agent, the platform, or the
scenario. A delay shorter than the staleness window turns the freshness
re-probe into a second copy of the first read, which is worse than no
re-probe at all, because it launders a stale value into a confirmed one.
75 > 60 guarantees the second read crosses a refresh boundary.

All Tier-1 remediation scenarios now self-seed via `chaos_setup:` in their YAML — no separate `make chaos-*` step needed for the live pass.

That covers **chaos, not traffic**, and the difference is easy to miss because the sentence above reads like "no manual setup at all." `chaos_setup:` breaks something; it does not generate load. One scenario, `remediate_consumer_lag_success`, needs a fault that is *accumulated* rather than seeded — consumer lag is arrival minus service, and killing the consumer only removes the service side. Nothing in the YAML produces the arrival side, so `make traffic` must be running in another terminal for the duration of that run (see the traffic prerequisite in the protocol block above). Every other remediation scenario seeds its fault whole and needs no producer.

### Commit the run archive (invariant 9)

After every live campaign, commit its archive:

```bash
git checkout -b eval/<slug>
git add evals/runs/<invocation_id>
git commit -m "eval: archive live run <invocation_id>"
```

`evals/runs/` is no longer gitignored, but un-gitignoring alone is NOT
durability: `git clean -fd` deletes untracked files regardless of ignore
status (only `-x` concerns ignored ones). Since ADR 0021 a finalized
archive is also locked on disk (see §"Completed archives are locked on
disk" below), which makes that `git clean` refuse locally — but locked is
not backed up: the flag stops deletion, not disk failure, and flags do not
travel through git. The commit is still the durability.

Offline `make eval` / `make eval-reg` / `make baseline` invocations also
leave untracked `evals/runs/<id>/` directories behind. Leaving them
untracked is acceptable; deleting them is not (invariant 9).

This step first becomes exercisable at the post-v0.5.0 eval — under the
ADR 0011 freeze nothing runs, so no archive is committed until then.

### Partial archives: no `report.json` means the run was killed ([ADR 0017](ADR/0017-eval-run-archive-lifecycle.md))

The archive is written **incrementally**. `evals/runs/<invocation_id>/` is
created before the first scenario, each scenario's trajectory, briefing and
trace slice land as that scenario finishes, and `report.json` is the **last**
file written. So:

| On disk | Means |
|---|---|
| `runs/<id>/report.json` present | the suite finished; the aggregate report is authoritative |
| `runs/<id>/report.json` absent | the run was killed, crashed, or is still in flight |

A directory without `report.json` is **not** garbage: the per-scenario files
under it are first-class evidence of the scenarios that did complete, they cost
real money, and invariant 9 covers them exactly as it covers a finished run.
Commit them the same way — the commit message is the place to say the run was
interrupted. A Ctrl-C mid-suite now loses at most the in-flight scenario.

List the incomplete archives:

```bash
for d in evals/runs/*/; do [ -f "$d/report.json" ] || echo "PARTIAL $d"; done
```

`runs/<id>/traces/<scenario>.jsonl` holds this invocation's slice of the flat
`evals/traces/<scenario>.jsonl` — the flat file accumulates every invocation
forever and is gitignored, so the archived slice is what makes a committed run
re-readable. The runner only ever **reads** the flat file; it stays the
canonical incremental record and is the only trace of a scenario killed
part-way through, before its archive write fires. Trace slices carry full
prompts and responses, so committed archives are correspondingly large — that
is deliberate (ADR 0017).

Re-using an invocation id fails loudly: the run directory is created with
`exist_ok=False` and every file inside is opened exclusive-create, so a
collision raises before a single tool call is spent instead of overwriting the
earlier run. Should any future path reach a finalized directory anyway, the
lock below is the second wall: a sealed archive refuses new files too.

### Completed archives are locked on disk ([ADR 0021](ADR/0021-run-archives-are-locked-by-the-filesystem.md))

Invariant 9, enforced by the filesystem rather than requested of the reader —
the same convention `context/README.md` applies to session archives. The
runner locks in two moments:

- **As each scenario lands**, its trajectory, briefing and trace-slice files
  are made read-only (`chmod a-w`) and, where the platform supports file
  flags (macOS), user-immutable (`chflags uchg`). A killed run's partial rows
  are therefore already protected — evidence the instant it is durable.
- **When `report.json` lands**, the whole `runs/<id>/` tree is sealed,
  directories included. From that moment `rm -rf`, truncation, overwrite,
  `git clean -fd`, and new-file creation inside it are all refused.

On Linux (CI) there is no `uchg`; the write-bit removal still refuses all of
the above for a *finalized* archive, because unlink needs write permission on
the parent directory and the seal removes it. The one platform gap: a
*partial* archive's rows on Linux are read-only but deletable until the seal
lands — on macOS `uchg` refuses even that.

The lock is best-effort by design: a filesystem that refuses `chmod` or
`chflags` gets a logged `archive lock skipped (...)` line and the run
completes normally. The evidence is already written by lock time; enforcement
must never cost a paid run its report.

The flat `evals/traces/*.jsonl` files are **never** locked — later
invocations append to them (F-002's fix). Only the per-run slice inside the
archive is sealed.

**To unlock one on purpose** — rare, deliberate, announced:

```bash
chflags -R nouchg evals/runs/<invocation_id>   # macOS only; no-op elsewhere
chmod -R u+w evals/runs/<invocation_id>
```

Unlocking in order to *edit* run data is invariant 9's definition of a bug.
The legitimate reasons are migration to other storage and whatever a future
retention ADR decides. If an archive does get removed, say so where the next
reader will look (the commit message, or the run ledger): a gap that
announces itself is fine; a gap that looks like it was never there is not.

### Runner exit codes and --live refusal (ADR 0013)

`--live` now **refuses to run against an env that would degrade any selected
scenario to canned**: a placeholder `PLATFORM_MCP_URL` (`eval.local`) or an
empty/placeholder `ANTHROPIC_API_KEY` (what a verbatim `.env.example` copy
gives you) exits 3 before a single scenario runs — no tool calls, no spend.
Likewise `--smoke` without `--live` exits 3 (smoke-without-live would run the
whole suite canned under placeholder settings), and a broken/missing `.env`
under `--live` exits 3 with a labeled `PREFLIGHT FAIL (env)` line instead of a
pydantic traceback. There is no opt-out flag. Plain offline runs (no `--live`)
are unaffected: canned fallback is their intended mode, they still exit 0, and
the degradation is now recorded in the report (`degraded_count` in
`latest.json`) rather than only printed.

| Exit | Meaning |
|---|---|
| 0 | all selected scenarios passed |
| 1 | ≥1 scenario failed (regression gate: regression detected, or a baseline scenario dropped from latest) |
| 2 | the selection is not one the runner will spend on. Three cases: `--live` with no `--only` and no `--smoke` (an unfiltered live run is the whole suite against one shared platform — refused before the settings load, and `make eval-live` refuses the same thing at Makefile parse time); an `--only` pattern matched no scenario — *any* single dead pattern, not only a wholly empty selection, since a dead pattern is a renamed scenario dropping silently out of the run; or, under `--live`, an `--only` pattern that is not a full scenario name — the refusal lists the scenarios it would have substring-matched, because a widened live selection slips past the ADR 0020 gate whenever only one of the matches mutates (regression gate: missing report, or a filtered `--only` `latest.json` — refused as gate input) |
| 3 | preflight/env failure: `--smoke` without `--live`, degraded `--live` env, invalid or missing settings, missing smoke token, LLM auth preflight failure |
| 4 | principal guard: the token is not the one the selection needs — the smoke token holds more than read scope, or a remediation selection lacks `actions:execute`, or a chaos-seeding selection lacks `chaos:invoke` (each guard probes only the scope its half of the selection needs, and each fails closed on any probe outcome that is neither a scope refusal nor an argument-validation refusal) |
| 5 | post-stage audit failed, was unreadable, or was inconclusive |
| 6 | `--smoke` selected a scenario that declares `chaos_setup` — a read-only stage does not seed chaos |
| 7 | `--live` selected more than one state-mutating scenario — nothing resets the shared platform between them (ADR 0020) |
| 8 | `--live` selected a canned-only scenario (`use_live_mcp`/`use_live_llm` both false) — the platform cannot manufacture its fault, so a "live" row would really be canned |

### A live selection may not contain a canned-only scenario (exit 8)

A scenario with both `use_live_mcp` and `use_live_llm` false is
**canned-only**: a claim that the live platform cannot manufacture or
expose its fault, not that nobody wired it up. Three scenarios carry the
marker today — `remediate_verify_fails` (a healthy platform cannot supply
a fault that verify then fails to see cleared), `remediate_runaway_saga_success`
(the seeded DAG auto-completes within seconds and no chaos hook builds a
runaway chain), and `remediate_stale_cache_success` (`create_stale_cache`
writes a Redis key invisible to every read tool). Each YAML documents the
reason and the platform change that unblocks it directly above the flags.

Without the refusal, `run_scenario` would fall back to canned for such a
scenario even under `--live`, and the row would land in the live report's
pass count as a green that grades fixtures, not the agent. So the runner
refuses the *selection* — after `--only` filtering, before the ADR 0020
mutating gate, before any env probe, guard, or spend. There is no opt-out
flag; re-enabling a scenario means flipping its flags in the YAML once the
platform capability it names has shipped and been pinned. `--smoke` is
exempt by design: its default selection deliberately mixes canned
harness-sanity scenarios (`noise_*`, `tool_*`, ...) with live reads, and
its report is read that way.

### A smoke selection may not seed chaos (S-03)

`chaos_setup` is fired by the runner under `PLATFORM_TOKEN` — the full
write+chaos principal — because chaos needs `chaos:invoke`, which the
read-scoped smoke token does not carry. That is fine on a `--live`
remediation run and wrong during `--smoke`, whose entire purpose is to
prove the stage is read-only. So `--smoke` now refuses, with **exit 6**,
before preflight or any spend, if any *selected* scenario is outside the
derived smoke set — that is, if it declares `chaos_setup`, declares
`expected_action_tools`, or carries a `smoke_exclusion`. The refusal names
each offending scenario and its reason, because the three have three
different repairs. The check runs after `--only` filtering, so an
`SMOKE_ONLY=` override can only **narrow** the derived selection, never
widen it. There is no opt-out flag: a scenario outside the derived set is
not a smoke scenario.

(Until 2026-09 this checked `chaos_setup` alone, which was half the door.
`--only` bypasses the derivation entirely, so an override could re-admit a
scenario the derivation had dropped for declaring `expected_action_tools` —
a graded Tier-1 write inside the stage whose purpose is proving the smoke
token cannot write. Five shipped scenarios are in that shape and all are
reachable by `SMOKE_ONLY=dlq_`.) Since
WO-R2-123 an unfiltered `--smoke` cannot trip it either — the derived
selection admits only chaos-free, action-free scenarios, so the three
`remediate_*` ones are never in it. Exit 6 is now reachable exactly
through the `--only` override, which is the channel it was always for.

The mirror of that rule on the `--live` side: a selection that *does*
declare `chaos_setup` is checked, before any spend, for the scope it needs
to seed — `chaos:invoke`, probed with a deliberately invalid
`inject_latency` call, exit 4 on refusal. The write guard could not cover
this: it is keyed on `expected_action_tools`, and a scenario that mutates
the platform solely through `chaos_setup` declares none, so it used to run
unguarded and discovered its wrong token inside `run_scenario` — hook
refused, scenario crashed, invocation already under way. Probing
`actions:execute` instead would have been wrong in both directions: it
refuses a chaos-capable token that can seed, and passes a write token that
cannot.

Relatedly, a chaos hook name is a **closed set**, validated at scenario
load time against the chaos tools in
`contracts/platform-tools.snapshot.json` (selected by the platform's
`[chaos: ...]` description prefix). A scenario YAML naming any other tool
— a Tier-1 write, say — is rejected at load instead of being forwarded
verbatim as a `tools/call` under the full principal. New hooks become
legal via a platform release + digest bump + `make snapshot`, never by
hand-editing a list. Re-tokening chaos onto a dedicated lower-privilege
principal is deliberately out of scope: it is a platform-side scope
design change, not a commander one.

The hook's **arguments** are closed the same way, against the same
snapshot entry's `inputSchema`: unknown argument names, missing required
ones, and flipped primitive types are all rejected when the YAML loads.
Every chaos `inputSchema` declares `additionalProperties: false`, so each
of those is a guaranteed `ChaosInvocationError` live — and seeding runs
*before* the agent starts, which means the failure used to land
mid-campaign, after the platform had been touched under the write
principal and after run startup was already paid for. Both halves of an
invocation now fail in the same place, for free, at load time. When this
rejection fires the fix is in the scenario YAML, or — if the platform
genuinely moved — a digest bump plus `make snapshot`.

An **empty** `PLATFORM_SMOKE_TOKEN` counts as unset and exits 3 rather
than falling through to the write-scoped `PLATFORM_TOKEN`; likewise
`make_client` raises on an explicitly-empty token instead of selecting
the privileged default (S-04).

### Post-stage audit: saturation and self-owned principals (A-13)

After the smoke stage, the runner grades the platform's audit log and
fails (exit 5) if any successful Tier-1 action landed during the stage
window. Each individual read is still **one page of at most 200 rows** —
`list_audit_events` exposes no `offset` and no `created_after` (the pinned
v0.6.0 `inputSchema` declares `additionalProperties: false` over
`action` / `action_prefix` / `principal_type` / `limit`, and sending
`offset` anyway is refused `-32602 extra_forbidden`; `list_dlq_messages`
is the tool in this platform that pages, not this one). But the window is
no longer graded from that single page.

* **The window is scanned forward, by checkpoint.** The runner reads a
  page after **every scenario** and folds it into one `AuditWindowScan`;
  the post-stage assertion contributes the final page and grades the
  union. Rows that scroll past row 200 by the end of the stage were
  already banked while they were still reachable, so a stage far louder
  than 200 `agent.tool_invoked` rows now grades conclusively. A Tier-1
  success is caught even when the final page can no longer see it.
* **A gap between checkpoints is still inconclusive, not clean.** Two
  pages compose only when the newer one reaches back to the older one's
  newest row. If more than a full page landed between two checkpoints,
  the rows in the hole are unreachable for good, coverage restarts there,
  and the guard raises — exit 5. **This half is by design**
  (inconclusive ≠ clean, invariant 6): it is not an agent bug. So is the
  2000-row ceiling on one stage's retained in-window rows; a read-only
  stage that loud is itself the finding. Any violation already visible is
  named in the same message.
* **A single failed checkpoint is not fatal.** It prints
  `post-stage audit checkpoint skipped (...)` and the stage continues —
  losing one page only narrows coverage, which fails closed on its own at
  the assertion. A run whose log is full of that line has a broken audit
  read, and its guard has quietly degraded to the one-page behaviour
  above; treat it as a defect rather than noise.
* **Violations can be scoped to the principals you own.** Set both
  `PLATFORM_AGENT_PRINCIPAL_ID` and `PLATFORM_SMOKE_PRINCIPAL_ID` (both
  printed by `make bootstrap-token` alongside the tokens) and only those
  two service accounts' Tier-1 successes fail the stage, so a shared
  platform's other principals cannot false-fail it. Both are required —
  the wrong-token failure mode this guard exists for (the "read-scoped"
  stage silently holding `PLATFORM_TOKEN`) writes its rows under the
  **agent** principal, so the smoke id alone would blind the guard to its
  own reason for existing. Leave them unset and the guard stays
  deliberately over-broad: any service account's in-window Tier-1
  success fails the stage.

Backward paging remains a **cross-repo follow-up**, and would still be
worth having: it needs a platform PR adding `created_after` / `offset` /
`principal_id` to `list_audit_events`, a platform release, a pin bump, and
a snapshot regen from the pinned stack — never a hand edit of
`contracts/platform-tools.snapshot.json`. It would collapse the whole
checkpoint mechanism into one query and close the gap case above. Until
then, forward checkpointing is what this repo can do without touching the
platform, and it covers every window the runner actually produces.

### consumer_lag live notes (kill-window experiment, 2026-08-04)

`remediate_consumer_lag_success` seeds `kill_consumer` (not latency — the
per-principal rate limit ≈ latency-degraded service rate made the old
design unwinnable; see the scenario's chaos_setup comment). Facts to
operate by:

- A killed consumer **keeps its group assignment and reports true
  climbing lag** — no eviction, no null. Rising lag is the signal.
- Cache staleness: ~60s for the metric to show the fault, ~30s to show
  the recovery. The **first probe may read a stale 0** — exactly the
  case the ADR 0009 freshness re-probe exists for; run live with
  `INVESTIGATE_REPROBE_ATTEMPTS=1`.
- Supervisor re-spawn after `restart_consumer_group` clears the kill
  flag: **~2.4s**. Verify polling absorbs it easily.
- Reprobe delay must straddle the cache window: `INVESTIGATE_REPROBE_DELAY_SECONDS=75`
  (a 20s reprobe lands inside the same ~60s cached reading and shows a
  static value — live run 2026-08-31 collapsed a correct hypothesis on it).
- **There is no head start to give.** An earlier version of this note said
  to run `UNTIL_LAG=30` before launching the runner and then keep it
  pumping. The first half cannot work: before the runner seeds
  `kill_consumer` the consumer is alive and drains the topic as fast as
  the loop fills it, so lag never leaves 0 and `UNTIL_LAG` waits forever
  against a healthy stack. Lag only accumulates once the run has killed the
  consumer — which happens *inside* the run. Start `make traffic` plain,
  start the run, and let the precondition (lag>=20 over 10×15s) do the
  waiting; that window IS the head start, and it is why the bar could be
  raised from 1 to 20 without making the scenario flaky.
- Traffic: **required, and it now exists.** "The standard 1-job/2s loop"
  described here since 2026-08-04 was never a thing you could run — no
  such script existed in either repo, so lag stayed at 0 and the scenario
  asserted a fault that could not be made. `make traffic` is it.

  Run it in a second terminal BEFORE seeding `kill_consumer`:

  ```bash
  make traffic                      # until Ctrl-C
  make traffic UNTIL_LAG=1500       # stop once the backlog is deep
  ```

  `UNTIL_LAG` reads the backlog over MCP, so it needs `PLATFORM_MCP_URL`
  and `PLATFORM_SMOKE_TOKEN` — from `.env` is enough, the recipe passes
  them through (WO-R2-89; before that it did not, and `UNTIL_LAG` exited 2
  every time). The **smoke** token deliberately: this loop only ever reads
  lag, and giving it the write-scoped token would widen it for nothing.

  Two corrections to the old note. It submits every **3s, not 2s**:
  `jobs:create` is limited to 30/60s and 1-job/2s sits exactly on that
  limit, so half the requests would 429. And it needs the **user** login,
  not the service-account token the eval uses — `POST /jobs` depends on
  `get_current_user`.

  Expect 503s once lag passes 1000. That is not a failure: the platform's
  backpressure check reads `kafka:consumer_lag:worker-dispatcher`, the
  same key the scenario measures, so a 503 is the platform telling you the
  fault is fully built. Lag does not drain while the consumer is dead, so
  the loop keeps going and says so.

  Forgetting it fails safely: the scenario's precondition polls for
  `lag >= 20` over 10×15s and aborts before any model call, reporting that
  the fault was never manufactured. (It was `lag >= 1` over 6×15s until
  PR #178. Both numbers were raised for the same reason: a backlog of 1 is
  indistinguishable from metric jitter, and 6×15s could expire while the
  60s lag recompute was still in flight — a run could start against a
  fault too thin to reason about, or abort on one that was about to
  appear. The YAML is the authority; this sentence tracks it.) (`remediate_stale_cache_success` is
  winnable live as of the v0.6.0 pin: `get_cache_key_info` reads the exact
  key `create_stale_cache` writes, so the scenario's precondition can
  confirm the seeded key is there before any spend. Before that tool
  shipped the fault was real but unobservable and the scenario was
  canned-only.)

## Debugging one scenario

The per-scenario trace file is the fastest path:
```bash
# Full trace (14 records for redis_saturation)
cat evals/traces/redis_saturation.jsonl | jq .

# Just the LLM outputs (hypotheses + next actions)
cat evals/traces/redis_saturation.jsonl | jq 'select(.kind=="llm") | .output'

# Just the tool calls with results
cat evals/traces/redis_saturation.jsonl | jq 'select(.kind=="mcp") | {tool_name, arguments, result}'

# Human-readable stepwise version — reports are versioned, so ask the resolver
# rather than guessing a filename (see docs/eval-methodology.md § Artifacts)
open "$(uv run python -m evals.artifacts newest human redis_saturation)"

# Every render of that scenario, oldest → newest
uv run python -m evals.artifacts versions human redis_saturation
```

For deeper introspection, the newest `evals/trajectories/<scenario>.<stamp>.<inv>.json` has every `RunState` checkpoint (state, evidence, hypotheses over time): `uv run python -m evals.artifacts newest trajectory redis_saturation`.

## Contract-test target (constraint in force)

**Run contract tests ONLY against the pinned demo stack.** The pin is
v0.6.0 by index digest and the committed snapshot carries its 29 tools,
blessed from that stack with the full 4-scope service-account token.

The rule outlives the v0.4.9 → v0.5.0 → v0.6.0 bumps that motivated it: platform
master moves ahead of whatever tag is pinned, so a contract check against
a master-built dev stack can fail **by design**. That is master drift, not
drift in the pinned artifact, and it must never trigger a snapshot rebless
from the dev stack. Bless snapshots from the pinned stack only, and only
through the one-PR flow below.

## Bumping the pinned platform image

**The contract diff now runs in CI on every pull request** (the `contract`
job in `.github/workflows/ci.yml` boots `demo/compose.yml` and runs
`make test-contract`), always against whatever digest is pinned on the
branch. So the three steps below **must land as ONE pull request**: a PR
that bumps the digest without the reblessed snapshot — or blesses a new
snapshot without the digest bump — puts the `contract` job red on itself
and on `main` until the other half lands. Bless the new snapshot locally
from the new pinned stack, then commit the compose bump, the snapshot, and
any registry realignment together.

Platform ships a new digest → three steps on the agent side:

1. Update `demo/compose.yml`:
   ```yaml
   image: ghcr.io/kudratsingh/incident-platform@sha256:<new-digest>
   ```
2. Regenerate the contract snapshot:
   ```bash
   make demo                    # scoped `up --wait`; see below
   make snapshot                # writes contracts/platform-tools.snapshot.json
   ```
   Bring the stack up with `make demo`, not a bare
   `docker compose -f demo/compose.yml up -d --wait`. Compose fails the wait
   when a one-shot service exits during the watch window, and a digest bump
   recreates every one-shot there is (`migrate`, `redpanda-init`), so the
   unscoped form fails here every time. `make demo` scopes `--wait` to the
   five long-running services; `depends_on` still runs the one-shots first.
3. Address any registry drift the contract test surfaces:
   ```bash
   make test-contract           # will fail if tool schemas moved
   ```
   If a required tool field was added/renamed, update `src/incident_commander/tools/registry.py` to match.

## Connection pool and run capacity ([ADR 0022](ADR/0022-connection-pool-sizing-and-the-run-concurrency-ceiling.md))

The agent runs at most **8 concurrent investigations** per process by default. That
number is not a preference — it is derived from the connection pool, because a live
run pins one Postgres connection for its whole duration (the single-flight lease,
[ADR 0016](ADR/0016-incident-identity-and-single-flight.md)) and needs a second one
for every checkpoint write:

| Var | Default | Meaning |
|---|---|---|
| `DB_POOL_SIZE` | 10 | Connections held open. |
| `DB_MAX_OVERFLOW` | 10 | Extra connections opened under burst. |
| `DB_POOL_TIMEOUT_SECONDS` | 10 | Wait for a free connection before giving up. Was SQLAlchemy's implicit 30. |
| `DB_INGEST_RESERVED_CONNECTIONS` | 4 | Held back from the run ceiling for webhook ingress and the crash rail. |
| `AGENT_MAX_CONCURRENT_RUNS` | unset | Lowers the ceiling below what the pool allows. May only lower it. |

```text
ceiling = (DB_POOL_SIZE + DB_MAX_OVERFLOW - DB_INGEST_RESERVED_CONNECTIONS) / 2
        = (10 + 10 - 4) / 2 = 8
```

**Symptom:** `at capacity (8 concurrent runs): incident <id> is recorded in TRIAGE but
will not be investigated` in the log.

That is the agent shedding load, working as designed, not an error. The alert was
acknowledged, is durably recorded at TRIAGE, and humans are paged by the platform
regardless (see [safety-model.md](safety-model.md#fail-open-on-paging)). It is *not*
investigated, and it will not be retried later. Occasional lines during a genuine
alert storm are expected; a steady stream means the ceiling is too low for the load.

To raise it, raise the pool — `DB_POOL_SIZE` and `DB_MAX_OVERFLOW` — and restart.
Settings are frozen and read once at startup. Before raising it much, check the
server side: **this ceiling is per process and so is the pool, but Postgres'
`max_connections` is shared.** Eight replicas at the defaults is 160 connections, and
nothing in the agent checks that for you. Adding replicas is the better lever anyway
— the advisory lock is per-database, so single-flight already holds across processes.

The process refuses to start if the numbers cannot work (a pool too small for one
run, or `AGENT_MAX_CONCURRENT_RUNS` above the derived ceiling). That is deliberate:
the alternative is discovering it as a stall during an incident.

### A run that crashed before it took the lease

**Symptom:** `run for incident <id> crashed (OperationalError) without ever holding
the single-flight lease; not recording FAILED — another worker may own this incident`.

The background task died on its way to the lease, so it never learned who owns the
incident. The usual cause is the pool: taking the lease is the first thing that asks
for a connection, so pool exhaustion or a brief Postgres outage surfaces here first.

The rail deliberately writes nothing. A FAILED record is a statement about a run
*this* process was conducting, and FAILED is terminal and non-resumable
([ADR 0016](ADR/0016-incident-identity-and-single-flight.md)) — so writing one on
someone else's incident would end a live investigation from the outside and, because
the closed generation makes the next redelivery derive a fresh incident id, split one
fault across two investigations paying twice for the same evidence.

What to do: nothing for the incident itself. The worker that holds the lease is
unaffected, and if no worker held it, the alert sits at TRIAGE and the platform has
already paged a human (invariant 5). Treat repeats as a database or pool signal and
read them alongside the capacity section above.

## Kill switch

To pause the agent without stopping the FastAPI ingress (alerts keep flowing to the platform's normal oncall path):

```bash
export AGENT_ENABLED=false
# then restart the agent process
```

The webhook still records incidents; the state machine never advances. Reversible: set back to `true` and restart.

## When something goes wrong in live eval

**Symptom:** `make eval-live` crashes mid-suite.
- Check `evals/traces/*.jsonl` — the last file to be written names the crashed scenario. Its final `scenario_end` record has the error (PR #35 added error recording on crash).
- `run_all` is resilient (per-scenario try/except), so the whole batch should complete even with one crash. If it doesn't, that's a runner bug — file it.

**Symptom:** live eval passes but a specific scenario is doing the wrong thing.
- Open the newest human report — `open "$(uv run python -m evals.artifacts newest human <scenario>)"` — every planner iteration is timestamped, with system prompt + user message + parsed output.
- Compare against the newest trajectory (`... newest trajectory <scenario>`) for state-machine transitions.
- The newest briefing (`... newest briefing <scenario>`) is the final human-facing artifact.
- These files are versioned and never overwritten, so an earlier run's copy is still there: `... versions <kind> <scenario>` lists them oldest → newest.

**Symptom:** contract test fails after a platform bump.
- The diff between `contracts/platform-tools.snapshot.json` and the live `tools/list` output tells you what moved. Add/rename registry fields to match, or revert the platform bump if the change is unexpected.

**Symptom:** the agent picked the wrong Tier-1 action.
- Look at the `remediation_planner` LLM record in the trace file. The `output` field has `target_hypothesis` + `action_tool` + `verify_expectation`.
- If the mapping is wrong, tune `src/incident_commander/llm/prompts/remediation_planner.md` and re-run the affected scenario. Regenerate the prompt hash in `tests/unit/test_prompts_snapshot.py`.

## Escalation from the agent

Terminal state `ESCALATED` means the state machine reached a handoff point + a briefing was generated. Today the briefing lives in `evals/briefings/<scenario>.<stamp>.<invocation_id>.json` (offline; resolve the newest with `python -m evals.artifacts newest briefing <scenario>`) or the trajectory store (live). No paging integration ships yet — the notification rail is a planned follow-up.

Every escalation carries a `_planner_escalate` or `_remediation_escalate` evidence entry with the reason. Read the trajectory JSON to see why the agent handed off.
