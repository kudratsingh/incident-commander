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

# DLQ scenarios — replay_dlq_messages is the fix
make chaos-poison

# Cache scenarios — invalidate_cache_key is the fix
make chaos-saturate

# Investigation scenarios (bad-deploy correlation)
make chaos-bad-deploy
```

Each hook self-cleans on TTL (5–10 minutes). Chaos setup requires the platform booted with `CHAOS_ENABLED=true` (default in `demo/compose.yml`) and a token with `chaos:invoke` scope.

If your service-account token was minted without chaos scope, regenerate:
```bash
uv run python scripts/bootstrap_agent_token.py --scope chaos:invoke
```

### 2. Run the live eval

```bash
make eval-live
```

This runs the full scenario suite against the live platform + live LLM. Trace files land in `evals/traces/*.jsonl`; the formatter turns them into readable stepwise trajectories in `evals/reports/human/*.txt`.

**Cost:** roughly $0.05 per read-only scenario, $0.07 per remediation scenario. Current suite of 33 (~29 live) is ~$1.60/run.

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

```bash
# Bring-up. `make demo` now stops after compose-up (no embedded eval).
# Expect FIVE long-running healthy containers (postgres, redis, redpanda,
# platform=MCP on 8001, api=REST+consumers on 8000) plus two exited
# one-shots (migrate, redpanda-init). Three healthy containers means the
# pre-completion compose — no consumers, no consumer_lag scenarios.
make demo
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
#    scenario list. Override the list with SMOKE_ONLY= if needed —
#    but a smoke selection may not contain a chaos-declaring scenario
#    (exit 6, see "Runner exit codes" below), so keep the remediate_*
#    scenarios out of it.
#    Every --only pattern must match at least one scenario: one that
#    matches none is a renamed scenario that would silently drop out of
#    the pass, so the runner refuses the whole selection (exit 2) and
#    prints the match count per pattern. Read those counts — they are
#    the run's own record of what it actually covered. Which scenarios
#    the pass owes coverage to is derived from the YAMLs and enforced
#    offline by tests/unit/test_smoke_only_coverage.py; see
#    docs/eval-methodology.md, "The read-only smoke pass".
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
make eval-live ONLY=remediate_consumer_lag_success && make eval-reset
make eval-live ONLY=remediate_dlq_backlog_success  && make eval-reset

# remediate_stale_cache_success, remediate_runaway_saga_success and
# remediate_verify_fails are NOT in this list: they are canned-only
# (use_live_mcp/use_live_llm false in the YAML, with the reason and the
# unblocking platform change commented above the flags), because the live
# platform cannot manufacture their faults — create_stale_cache writes a
# Redis key no read tool can observe, and no chaos hook builds a runaway
# DAG. Selecting one under --live is refused with exit 8 before any
# spend; they run (and must stay green) in the offline suite instead.

# After any consumer restart — the agent's restart_consumer_group or a
# manual restore — confirm liveness with `rpk group describe
# worker-dispatcher` (see "Restore state" above), never the lag metric.

# Do NOT drop the reset between scenarios. The one-fault-one-scenario
# protocol exists because shared platform state between runs was the
# single largest source of noise in the seven-run audit — see the
# lessons doc's third bucket, "shared mutable environment".
```

Every `make eval-live` invocation writes JSONL traces to `evals/traces/` and renders per-scenario human reports to `evals/reports/human/*.txt` (via the `format_traces.py` step chained into the target).

A filtered run (`ONLY=...`) still overwrites `evals/reports/latest.json`, but the report now self-describes via `only_patterns` (ADR 0013) and **can no longer feed the gate or the baseline**: `make eval-reg` exits 2 on a filtered `latest.json`, and `make eval-reg ONLY=x` / `make baseline ONLY=x` refuse at Makefile parse time before anything runs (A-03 — `study/runs.jsonl` records a full-suite `latest.json` lost to a later filtered run). The archive under `evals/runs/<invocation_id>/` remains the durable record for filtered runs; the flat `latest.json` is only a pointer to the most recent one.

`make eval-reset` shells into the platform's `app` container via `docker compose -f $PLATFORM_COMPOSE exec` — defaults to `../incident-platform/docker-compose.yml`. If the platform repo isn't a sibling checkout, set `PLATFORM_COMPOSE` either per-invocation or once in `.env` (the Makefile `-include .env`s it, so a non-sibling layout is a one-time setup rather than a flag you have to remember on every call). Getting this wrong fails loudly on an exit-2 guard before anything runs — it can't half-reset. Pass `PURGE_IDEMPOTENCY=1` to also `DELETE` idempotency_records (24h TTL from platform ADR 0010 handles the common case; opt-in purge for guaranteed-fresh cache).

Environment variable knobs for the live path (see [ADR 0006](ADR/0006-verification-is-a-polling-window.md)):

| Var | Default | Live-recommended | Meaning |
|---|---|---|---|
| `VERIFY_PROBE_ATTEMPTS` | 1 | 6 | Bounded polling window on VERIFYING. Default keeps canned runs single-probe. 6 proved out in the 2026-08-03 campaign; size scenario caps for it. |
| `VERIFY_PROBE_DELAY_SECONDS` | 15 | 20 | Delay between polling attempts. Size to the slowest verify probe's freshness. |
| `INVESTIGATE_REPROBE_ATTEMPTS` | 0 | 1 | Investigation-side freshness re-probe ([ADR 0009](ADR/0009-investigation-freshness-reprobe.md)): when a cached read kills a fixable hypothesis at ≥0.7, re-read it fresh before accepting. Default 0 keeps canned runs byte-identical. |
| `INVESTIGATE_REPROBE_DELAY_SECONDS` | 20 | 20+ | Delay before the freshness re-read. Size to the cached tool's declared staleness window (lag cache: 60s). |

`.env.example` now ships the live-recommended values for these knobs uncommented (canned/offline runs are unaffected — the runner forces single-probe and no-reprobe whenever the platform is a placeholder), and a `--live` run that still has them at canned-equivalent values prints a preflight warning. This table stays the source of record.

All Tier-1 remediation scenarios now self-seed via `chaos_setup:` in their YAML — no separate `make chaos-*` step needed for the live pass.

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
| 2 | an `--only` pattern matched no scenario — *any* single dead pattern, not only a wholly empty selection, since a dead pattern is a renamed scenario dropping silently out of the run (regression gate: missing report, or a filtered `--only` `latest.json` — refused as gate input) |
| 3 | preflight/env failure: `--smoke` without `--live`, degraded `--live` env, invalid or missing settings, missing smoke token, LLM auth preflight failure |
| 4 | principal guard: the smoke token holds more than read scope |
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
before preflight or any spend, if any *selected* scenario declares
`chaos_setup`. The check runs after `--only` filtering, so an
`SMOKE_ONLY=` override cannot smuggle a chaos scenario in. There is no
opt-out flag: a scenario that seeds chaos is not a smoke scenario. The
default `SMOKE_ONLY` list already excludes the three `remediate_*`
scenarios, so `make eval-smoke` is unaffected; an unfiltered
`python -m evals.runner --live --smoke` selects the whole suite and is
refused.

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

After the smoke stage, the runner re-reads the platform's audit log and
fails (exit 5) if any successful Tier-1 action landed during the stage
window. That read is **one page of at most 200 rows**:
`list_audit_events` exposes no `offset` and no `created_after`, and the
platform handler hardcodes `offset=0`, so the window cannot be paged from
this repo. Two consequences operators need to know:

* **A saturated page is inconclusive, not clean.** Rows come back newest
  first, so the only proof the whole window was scanned is that the
  oldest row on the page predates the stage start. If the page is at the
  200 cap (or the platform's `total` says rows were withheld) and it
  still does not reach back past the stage start, the guard raises and
  the runner exits 5 — the unreachable rows could hold the very Tier-1
  successes it is looking for. **This is by design** (inconclusive ≠
  clean, invariant 6): it is not an agent bug. Re-run the smoke stage in
  a quieter window, or reduce concurrent `agent.tool_invoked` traffic on
  the tenant. Any violation already visible on the truncated page is
  named in the same message.
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

Real pagination is a **cross-repo follow-up**: it needs a platform PR
adding `created_after` / `offset` / `principal_id` to `list_audit_events`,
a platform release, a pin bump, and a snapshot regen from the pinned
stack — never a hand edit of `contracts/platform-tools.snapshot.json`.

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
- Traffic: **required, and it now exists.** "The standard 1-job/2s loop"
  described here since 2026-08-04 was never a thing you could run — no
  such script existed in either repo, so lag stayed at 0 and the scenario
  asserted a fault that could not be made. `make traffic` is it.

  Run it in a second terminal BEFORE seeding `kill_consumer`:

  ```bash
  make traffic                      # until Ctrl-C
  make traffic UNTIL_LAG=1500       # stop once the backlog is deep
  ```

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
  `lag >= 1` over 6×15s and aborts before any model call, reporting that
  the fault was never manufactured. (`remediate_stale_cache_success` is
  not winnable live despite the v0.4.7 `create_stale_cache` hook: the
  hook writes one Redis key that no read tool can observe, so the
  miss-rate collapse the scenario grades never exists on the platform.
  It is canned-only — a `--live` selection is refused with exit 8 —
  until a platform `get_cache_key_info` read tool ships and is pinned.)

## Debugging one scenario

The per-scenario trace file is the fastest path:
```bash
# Full trace (14 records for redis_saturation)
cat evals/traces/redis_saturation.jsonl | jq .

# Just the LLM outputs (hypotheses + next actions)
cat evals/traces/redis_saturation.jsonl | jq 'select(.kind=="llm") | .output'

# Just the tool calls with results
cat evals/traces/redis_saturation.jsonl | jq 'select(.kind=="mcp") | {tool_name, arguments, result}'

# Human-readable stepwise version
open evals/reports/human/redis_saturation.txt
```

For deeper introspection, `evals/trajectories/<scenario>.json` has every `RunState` checkpoint (state, evidence, hypotheses over time).

## Contract-test target (constraint in force)

**Run contract tests ONLY against the pinned demo stack.** The pin is
v0.5.0 by digest and the committed snapshot carries its 27 tools, blessed
from that stack with the full 4-scope service-account token.

The rule outlives the v0.4.9 → v0.5.0 bump that motivated it: platform
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
   docker compose -f demo/compose.yml up -d --wait
   make snapshot                # writes contracts/platform-tools.snapshot.json
   ```
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
- Open `evals/reports/human/<scenario>.txt` — every planner iteration is timestamped, with system prompt + user message + parsed output.
- Compare against `evals/trajectories/<scenario>.json` for state-machine transitions.
- The briefing (`evals/briefings/<scenario>.json`) is the final human-facing artifact.

**Symptom:** contract test fails after a platform bump.
- The diff between `contracts/platform-tools.snapshot.json` and the live `tools/list` output tells you what moved. Add/rename registry fields to match, or revert the platform bump if the change is unexpected.

**Symptom:** the agent picked the wrong Tier-1 action.
- Look at the `remediation_planner` LLM record in the trace file. The `output` field has `target_hypothesis` + `action_tool` + `verify_expectation`.
- If the mapping is wrong, tune `src/incident_commander/llm/prompts/remediation_planner.md` and re-run the affected scenario. Regenerate the prompt hash in `tests/unit/test_prompts_snapshot.py`.

## Escalation from the agent

Terminal state `ESCALATED` means the state machine reached a handoff point + a briefing was generated. Today the briefing lives in `evals/briefings/<scenario>.json` (offline) or the trajectory store (live). No paging integration ships yet — the notification rail is a planned follow-up.

Every escalation carries a `_planner_escalate` or `_remediation_escalate` evidence entry with the reason. Read the trajectory JSON to see why the agent handed off.
