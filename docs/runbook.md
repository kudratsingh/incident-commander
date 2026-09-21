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
PLATFORM_TOKEN=sa_...               # the AGENT's principal; make bootstrap-token
PLATFORM_CHAOS_TOKEN=sa_...          # the EVALUATOR's (chaos:invoke); same command
PLATFORM_SMOKE_TOKEN=sa_...          # the read-only twin; same command
PLATFORM_WEBHOOK_SECRET=<from platform config>
DATABASE_URL=postgresql://...       # agent's own DB, separate from platform
```

### Three principals, and which target uses which

`make bootstrap-token` prints **three** `.env` lines and writes nothing itself — you paste all
three. They are three different service accounts on the platform, not three copies of one
credential, and the eval is only honest while they stay apart:

| `.env` variable | Platform account | Scopes | Who uses it |
|---|---|---|---|
| `PLATFORM_TOKEN` | `incident-commander` | `telemetry:read`, `incidents:read`, `actions:execute` | the AGENT under test — its MCP client on every `make eval` / `make eval-live` run, and the Tier-1 calls `make test-idempotency` replays |
| `PLATFORM_CHAOS_TOKEN` | `incident-commander-chaos` | `telemetry:read`, `incidents:read`, `chaos:invoke` | the EVALUATOR — every seed/reset/chaos path: all `make chaos-*` targets, the runner's chaos setup and teardown inside `make eval-live`, `make world-dossier`'s seeding leg, and the hook `make test-idempotency` stages its world with |
| `PLATFORM_SMOKE_TOKEN` | `incident-commander-smoke` | `telemetry:read`, `incidents:read` | the read-only stage — `make eval-smoke`, `make world-audit`, and `make world-dossier`'s read legs |

Two rules follow from the table, and both are enforced rather than trusted:

- **The agent never holds `chaos:invoke`** (owner decision O-4, platform ADR 0012). Since platform
  v0.6.5 a principal that can fire the lab is also served the `chaos.%` audit rows, so an agent
  holding that scope can read which hook was fired against which resource seconds before its own
  alert — the answer key. `make bootstrap-token` strips the scope from an existing
  `incident-commander` account and refuses to add it back through the `--scope` flag, and the
  runner probes for its absence before any spend (exit 4). When a hook is refused for lack of
  scope the remedy is the chaos token in the row below — never a wider agent account.
- **The evaluator never holds `actions:execute`.** Remediating is the thing being measured, so the
  principal that stages the world must not be able to do it.

There is no fallback between them. An unset `PLATFORM_CHAOS_TOKEN` fails loudly, naming the
variable and this command; it never silently resolves to the agent's token, because that failure
would surface as a mid-run scope refusal with the run archive already open.

**Nothing needs sourcing into your shell.** Every Python entry point (`make eval`, `eval-live`,
`eval-smoke`, `world-audit`, `world-dossier`, `eval-reset`) reads `.env` through `Settings`
(`env_file=".env"`), and the `chaos-*` recipes — whose script reads `os.environ` instead — are
handed their values by the Makefile's own `export` lines. Keep the values **unquoted** in `.env`:
make's `-include .env` keeps quotes where dotenv strips them.

The two paths resolve a conflict in opposite directions, which is worth knowing the one time it
bites you. A token exported in your shell **wins** over `.env` for the `Settings` paths
(pydantic-settings ranks the environment above the env file), so a stale export is why a freshly
pasted token can seem not to take. For the `chaos-*` targets `.env` wins instead, because
`-include .env` sets a *make* variable and the recipe re-exports that — the same precedence that
let every "read-scoped" smoke run before 2026-08-07 silently hold write scope. When in doubt,
fix `.env` and clear the export rather than reasoning about which one you are on.

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

Each hook self-cleans on TTL (5–10 minutes). Chaos setup requires the platform booted with `CHAOS_ENABLED=true` (default in `demo/compose.yml`) and the **evaluator's** token — `PLATFORM_CHAOS_TOKEN`, the `incident-commander-chaos` principal. The agent's `PLATFORM_TOKEN` is refused here on purpose: since platform v0.6.5 it does not carry `chaos:invoke`, because a principal that can fire the lab can also read the lab's audit rows (platform ADR 0012, owner decision O-4).

`PLATFORM_MCP_URL` and `PLATFORM_CHAOS_TOKEN` in `.env` are enough — the `chaos-*` recipes hand them to the script themselves (WO-R2-89). Until then they did not: make's `-include .env` sets a *make* variable, the script reads `os.environ`, and nothing bridged the two, so every one of these targets aborted with "PLATFORM_MCP_URL and PLATFORM_CHAOS_TOKEN must be set (env or --flag)" no matter how correct your `.env` was. Because make now expands these values, keep them **unquoted** in `.env` (make keeps the quotes; dotenv strips them).

If a hook is refused for lack of scope, re-mint and re-paste both lines — do **not** try to widen the agent account, which the bootstrap refuses for exactly this reason:
```bash
make bootstrap-token
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
only by the exit-8 canned-only gate, which fires because 16 scenarios declare
no live leg — a fact about `evals/scenarios/`, not about the command, and one
that would stop being true the moment every one of them gained a live leg.
That count is written down here and nowhere else: the runner and the tests
that make the same argument now make it without a number, and
`tests/unit/test_pre_spend_guards.py` fails when the digit above disagrees
with `evals/benchmark_inventory.json`.)

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

Trace files land in `evals/traces/*.jsonl`; the formatter turns them into readable stepwise trajectories in `evals/reports/human/<scenario>/*.txt` — one folder per scenario, and one new file per run, not one per scenario per run (WO-R3-257). `evals/reports/README.md` maps the whole folder.

**Cost:** roughly $0.05 per read-only scenario, $0.07 per remediation scenario. Current suite of 66 (~50 live: 34 read-only, 16 remediation) is ~$2.82 of tokens end to end — but never in one invocation, for the reason above. A smoke pass is ~$1.15 of that; the remediation scenarios are the rest, paid one run at a time.

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
   baseline conditions with `make world-audit` (read-scoped smoke token, no
   seeding, reset, or LLM call), and expect exactly:

   | Check | Expected |
   |---|---|
   | DLQ total | **4** |
   | Active alerts | **3** |
   | Redis `chaos:*` keys | **0** |
   | `worker-dispatcher` lag | **0**, with `lag_known: true` |
   | DLQ unclassified rows | **0** |
   | DLQ fenced rows | **0** |
   | `hot_set` size | **120**, with `exists: true` |
   | `traffic_loop` / `evals.runner` processes | **0** |

   The command prints each PASS/FAIL and the DLQ rows; any failed or unreadable
   check exits non-zero. It verifies the smoke principal before reading and
   never falls back to the write token. `make world-audit ROOTS=<id[,id...]>`
   also verifies the named chains exist and are not paused. The Redis scan
   uses `PLATFORM_COMPOSE` (default `demo/compose.yml`) and `redis-cli --scan`.
   The dossier imports the same three post-reset checks (DLQ total, alerts,
   chaos keys); lag remains a pre-run check because its cached reading can
   predate a reset. A green audit is not permission for a live eval.

   Anything else is a stop. `lag_known: false` is not "lag 0" — it means the
   metric is unreadable, and a run started on it grades the agent for a
   world nobody can see. Stray alerts above the baseline 3 are the known
   organic-alert case (WO-R2-131/132): they survive reset, they re-fire
   hourly, and on 2026-09-01 three of them aborted a run before any spend.

5. **Fault-world content review.** Free, zero-LLM, and it comes *before* the
   scenario chain check — checking that the hooks and tools line up is a
   different question from checking what the agent will actually read.

   ```bash
   make world-dossier ONLY=<exact scenario name>
   ```

   It seeds the scenario's own `chaos_setup` through the runner's chaos path,
   runs the scenario's preconditions, then runs **every read probe the agent
   is expected to make** — derived from `ALERT_SUBJECT_PROBES`,
   `SOURCE_ROW_FOR_ACTION`, `SOURCE_LISTING_FOR_ACTION`, `VERIFY_PROBE_FOR_ACTION`
   and the scenario's own
   evidence claims — under the read-scoped smoke token, prints every output in
   full, lints what it read, then `make eval-reset PURGE_IDEMPOTENCY=1` and
   re-audits the baseline in step 4's table. Output goes to stdout and to
   `evals/reports/dossiers/<scenario>/<scenario>.<stamp>.<invocation_id>.md`.

   **Then read it.** Every field, and of each fact ask: *does this support the
   behaviour the scenario expects, or contradict it?* Paste the dossier and
   your answers into the readiness note.

   The example, because it is the reason this step exists.
   `remediate_runaway_saga_success` run A (2026-09-07, archive
   `efdc3b2a9864`, ≈$0.15) seeded the stuck chain's dead-lettered root with
   `remediation_hint: replay_safe` and the error text `SchemaValidationError:
   payload missing required field 'user_id' … across 3 retry attempts`. The
   agent read the row — the ADR 0027 safety check the scenario requires —
   reasoned that a missing required field is a persistent data bug no replay
   can fix, and escalated naming the contradiction. That is sound operator
   judgement, and it graded red on three dimensions. **Two readiness sweeps
   had passed on that scenario**; both verified mechanics (the chain drains,
   the guards admit the plan) and neither read the fault's own fields. The
   contradiction was visible in a free probe.

   **Fixed at the source, 2026-09-07.** Platform v0.6.1 (plat #197) gives the
   lab one table of failure stories and a coherence rule over it, so no lab
   writer can pair a `replay_safe` hint with a permanent-fault error text
   again — the dossier's §5 lint now reads that same root row as *coherent*.
   This step does not retire with it. It exists for the class of defect (a
   fault world that contradicts what the scenario expects) and only one
   instance of that class has been closed.

   The lint's findings are **findings, not verdicts** — nothing here decides
   whether to run. Exit codes: `0` clean, `2` selection refused (nothing
   seeded), `3` preflight/stack unreachable (nothing seeded), `4` the baseline
   re-audit failed, `5` chaos seeding failed, `6` the reset failed.

6. **Traffic only where the scenario needs it.** `make traffic` is required
   for `remediate_consumer_lag_success` and for nothing else. Every other
   remediation scenario seeds its fault whole. Running traffic during an
   unrelated scenario adds load the scenario did not ask for.

7. **Hold the machine awake, and run in the background.** A paid run needs
   its own untimed `caffeinate -dims`; the harness's `caffeinate -i -t 300`
   is a five-minute timer, shorter than a single scenario. And a foreground
   run dies to the 10-minute command timeout, wasting the spend — long runs
   go in the background, always.

8. **Reset after** the scenario, not just before it.

9. **On any failure: STOP.** Investigate before running the next scenario.
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

# `bootstrap-token` PRINTS the tokens — THREE of them, one per principal:
# PLATFORM_TOKEN (the agent: reads + actions:execute, no chaos:invoke),
# PLATFORM_CHAOS_TOKEN (the evaluator: reads + chaos:invoke, the credential
# every seed/reset/chaos path uses) and PLATFORM_SMOKE_TOKEN (reads only).
# It does not write .env, and nothing
# downstream reads its stdout — YOU paste the printed values into .env.
# Paste all three: one value in two variables gives you either a runner that
# cannot seed or an agent that can read the lab, and neither names itself.
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
# chain. It declares chaos_setup, so it is NO LONGER part of the read-only
# smoke pass (a --smoke selection carrying chaos is refused, exit 6) and
# belongs to the one-at-a-time stage below with a reset after it. Since
# WO-R2-160 it is also no longer read-only: its chain root is classified
# human_required, so the correct run FENCES that root with
# mark_dlq_permanent and then escalates (ADR 0033). The ADR 0020
# one-mutating-scenario gate already covered it — that gate is keyed on
# `expected_action_tools OR chaos_setup` and the hook alone was enough —
# so nothing about the command changes. What changes is what the run does
# to the world: it now stamps `fenced_at` on one DLQ row, which the reset
# below clears with the chain itself:
make eval-live ONLY=saga_stuck && make eval-reset

# The two saga scenarios deliberately use DIFFERENT chain_names. Replaying
# a chain's root completes it, and create_stuck_dag refuses a drifted
# chain (409 stuck_chain_name_in_use) rather than rebuilding it, so a
# shared name would make each run depend on the other's order.

# --- THE ORDER THE REMAINING PAID RUNS GO IN (2026-09-08, WO-R2-166/167/168)
#
# This block is an ORDER, not a batch: every line below still needs the
# user's own explicit yes, one scenario per invocation (ADR 0020), with a
# reset and a world audit between them. Readiness is not authorization.
#
# The order is not arbitrary — each run establishes the premise the next
# one is read against:
#
#   1. remediate_dlq_backlog_success  — RE-RUN. Its two earlier passes
#      (e72b5ffb9df0, e8404306138c) replayed a poisoned message on the
#      lab's word, so neither is evidence about the agent. It is re-derived
#      against v0.6.3: the replay_safe slice is one row, the poisoned row
#      sits beside it unclassified and is forbidden.
#   2. dlq_poison_unclassified        — NEW. The same poisoned row, now as
#      the SUBJECT: read it, fence it, escalate. This is the positive half
#      of 1, and between them an agent that treats a poisoned message as
#      replayable is red either way round.
#   3. dlq_mislabeled_replay_safe     — NEW (WO-R2-167). The classifier
#      LIES: the row carries `replay_safe` and its error is a permanent CSV
#      data fault. The error wins, nothing is replayed, ONE fence on the
#      mislabelled row, and the contradiction goes in the briefing. Run it
#      after 2, because 2 establishes that the agent fences a row it can SEE
#      is unclassified; 3 asks whether it will do so against a label that
#      says otherwise. This is the only scenario in the suite where the
#      hint-routing table is the wrong answer.
#   4. saga_stuck                     — READY since cmd #211, held.
#   5. dlq_mixed_partial              — READY since cmd #212, held.
#
# All five seed their own fault and abort pre-spend if the premise is
# missing. Runs 1-3 poison or mislabel a DLQ row, so `make eval-reset
# PURGE_IDEMPOTENCY=1` after each is not optional: the row is deleted by the
# reset, and a survivor makes the NEXT run's precondition read six rows
# where it wants five — and because both hooks derive their ids, a survivor
# is refused with a 409 (`poison_fixture_name_in_use` /
# `mislabeled_fixture_name_in_use`) rather than duplicated, which reads as a
# failed seed. Runs 2, 3 and 4 also stamp `fenced_at` on one row, which the
# same reset clears.
make eval-live ONLY=remediate_dlq_backlog_success && make eval-reset PURGE_IDEMPOTENCY=1
make eval-live ONLY=dlq_poison_unclassified       && make eval-reset PURGE_IDEMPOTENCY=1
make eval-live ONLY=dlq_mislabeled_replay_safe    && make eval-reset PURGE_IDEMPOTENCY=1
make eval-live ONLY=saga_stuck                    && make eval-reset PURGE_IDEMPOTENCY=1
make eval-live ONLY=dlq_mixed_partial             && make eval-reset PURGE_IDEMPOTENCY=1

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

Every `make eval-live` invocation writes JSONL traces to `evals/traces/` and renders a human report to `evals/reports/human/<scenario>/*.txt` (via the `format_traces.py` step chained into the target). It renders **only** the invocation that just ran, plus any scenario whose newest attempt has no report yet; `make trace-report ARGS=--force` re-renders the whole corpus, which is a deliberate act because every render is permanent.

A filtered run (`ONLY=...`) writes its own report file and **can no longer feed the gate or the baseline**: the report self-describes via `only_patterns` (ADR 0013), `make eval-reg` exits 2 when the newest report is a filtered one, and `make eval-reg ONLY=x` / `make baseline ONLY=x` refuse at Makefile parse time before anything runs (A-03 — `study/runs.jsonl` records a full-suite report lost to a later filtered run). That specific loss is now impossible: reports are versioned (`evals/reports/runs/<YYYY-MM>/report.<stamp>.<invocation_id>.json`) and never overwritten, so the earlier full-suite report is still on disk. The gate resolves the **newest** one via `evals/artifacts.py` and prints which file it graded; the archive under `evals/runs/<invocation_id>/` remains the durable per-run record.

`make eval-reset` shells into the platform app via `docker compose -f $PLATFORM_COMPOSE exec $PLATFORM_SERVICE`. `PLATFORM_COMPOSE` defaults to `demo/compose.yml` — **this repo's own demo stack**, the one `make demo` brings up — and `PLATFORM_SERVICE` defaults to the `api` container in it (both demo services share one database, and `api` is the REST app that owns seeding). Point them at a sibling `incident-platform` checkout only if that is genuinely the stack under test, either per-invocation or once in `.env` (the Makefile `-include .env`s it, so a non-default layout is a one-time setup rather than a flag you have to remember on every call). On success — and only on success, since make abandons a recipe at the first failing line — the recipe's last line also clears the chaos teardown latch (`--clear-chaos-block`, ADR 0037; see "Teardown failed" under the exit codes below), so a blocked stack is unblocked by the command that actually put it back.

**What `make eval-reset` does NOT clear.** Reset undoes what the eval *seeds*: `chaos:*` keys, the lag cache, the DLQ fixture pool, `hot_set`, optionally idempotency records. It has no idea about state the world produced **organically** — anything raised by a platform loop rather than by a scenario. Concretely, it does not clear **SLO / non-chaos alerts**, and it cannot: those are generated by the platform's own SLO evaluator reading the seeded fixtures as if they were real traffic (4 of the 7 seeded jobs are dead-lettered, which is a fast burn by any honest reading), so resetting the fixtures **re-arms** the alert rather than removing it. Worse, the fast-burn dedup key is bucketed by the hour, so a suppressed alert **returns hourly** instead of staying suppressed. A green `eval-reset` therefore does not mean a clean world. Audit against the seeded baseline before every paid run (see the pre-run checklist above), and treat a stray alert as a stop-and-investigate, not as background noise — on 2026-09-01 three surviving `SLO fast burn` alerts were caught this way and the run was aborted before any spend. Tracked as **WO-R2-131** (reset must sweep organic alerts) and **WO-R2-132** (the platform-side fix), and **both are now closed by platform v0.6.4** (plat #201): the SLO evaluator skips rows carrying the seeded-fixture payload markers, and `reset_eval_state.py` resolves every active alert outside the five seeded ones. The stopgap that disabled the evaluator outright (`SLO_EVALUATION_INTERVAL_SECONDS: "0"` on both demo services, PR #180) was **lifted with the v0.6.4 re-pin**, so the evaluator runs at its default interval again. The paragraph above is therefore history for the SLO alert specifically — but the general rule it teaches is not: reset still clears only what the eval *seeds*, so audit against the seeded baseline before every paid run and treat a stray alert as a stop-and-investigate.

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

This step became exercisable after the post-v0.5.0 eval. The ADR 0011 freeze
closed on 2026-09-15; finalized archives are now committed after each run.

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

### Model roles: `--model-role` and what a run records (ADR 0013 amendment)

Every run stamps a provenance record into its report and its archive — commander
revision, the platform image digest read out of `demo/compose.yml`, the agent
model **and the role it was resolved from**, the judge model, the strategy, the
scenario, the invocation id, the timestamp, the execution mode, and the run's own
`BudgetLedger` (the budgets it was actually seeded with plus all four used
meters). The record is what lets a saved run answer "exactly what produced
this?" months later, which is what every phase report is built on.

There are two model roles, each a setting:

| Env var | What it is for |
|---|---|
| `DEVELOPMENT_MODEL` | harness work, schema work, plumbing, grader logic |
| `BENCHMARK_MODEL` | every reported number; the phase-close protocol |

Both default to the id in `src/incident_commander/config.py`, so the roles
change nothing until you point one somewhere else — and when you do, that id
needs a price row in `src/incident_commander/llm/pricing.py` or startup
refuses it, exactly as it does for `AGENT_MODEL` and `JUDGE_MODEL`.

Select one per run; the flag resolves `AGENT_MODEL` from it:

```bash
uv run python -m evals.runner --model-role benchmark --only <scenario_name>
```

**The default is `development`.** A run that does not name a role is not a
benchmark run, and a mistyped role is refused (exit 2, nothing runs, nothing
spent) rather than quietly treated as the default. Two consequences to know
about before a phase close:

* a report containing any development run is **marked non-closing** — printed
  by the runner and by `make eval-reg`, and stored in the report as
  `closing: false`. It is a mark, not a gate failure: development runs are the
  normal way the regression gate is exercised, they just cannot close a phase.
* `make eval-reg` **refuses** (exit 2) a comparison whose two sides name
  different `agent_model` ids, naming both. A leaderboard row across two
  models shows a model change and a behaviour change added together.

`make eval` / `make eval-live` do not pass the flag through yet — a benchmark
run invokes `python -m evals.runner` directly until a follow-up adds a make
variable for it.

### Runner exit codes and --live refusal (ADR 0013, amended by ADR 0037)

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
| 2 | the selection is not one the runner will spend on. Four cases: an unrecognised `--model-role` value (refused before the settings load — a typo must not be read as the default role); `--live` with no `--only` and no `--smoke` (an unfiltered live run is the whole suite against one shared platform — refused before the settings load, and `make eval-live` refuses the same thing at Makefile parse time); an `--only` pattern matched no scenario — *any* single dead pattern, not only a wholly empty selection, since a dead pattern is a renamed scenario dropping silently out of the run; or, under `--live`, an `--only` pattern that is not a full scenario name — the refusal lists the scenarios it would have substring-matched, because a widened live selection slips past the ADR 0020 gate whenever only one of the matches mutates (regression gate: missing report; a filtered `--only` `latest.json`; or a comparison spanning two `agent_model` ids — all refused as gate input) |
| 3 | preflight/env failure: `--smoke` without `--live`, degraded `--live` env, invalid or missing settings, missing smoke token, LLM auth preflight failure |
| 4 | principal guard: a token is not the one its role needs — the smoke token holds more than read scope, a remediation selection's `PLATFORM_TOKEN` lacks `actions:execute` **or still carries `chaos:invoke`** (the agent must be blind to the lab), or a chaos-seeding selection's `PLATFORM_CHAOS_TOKEN` lacks `chaos:invoke` (each guard probes only the scope its half of the selection needs, and each fails closed on any probe outcome that is neither a scope refusal nor an argument-validation refusal) |
| 5 | post-stage audit failed, was unreadable, or was inconclusive |
| 6 | `--smoke` selected a scenario that declares `chaos_setup` — a read-only stage does not seed chaos |
| 7 | `--live` selected more than one state-mutating scenario — nothing resets the shared platform between them (ADR 0020) |
| 8 | `--live` selected a canned-only scenario (`use_live_mcp`/`use_live_llm` both false) — the platform cannot manufacture its fault, so a "live" row would really be canned |
| 9 | a scenario's fault world could not be seeded, so nothing was graded for it ([ADR 0037](ADR/0037-a-scenarios-fault-is-a-plan-and-the-plan-is-put-back.md)) — a statement about the environment, not about the agent. Post-run: the rest of the suite still ran |
| 10 | chaos teardown failed, or a previous run's failure is still unresolved — live runs are blocked until the world is put back (ADR 0037). Pre-run (the latch, before settings load and before any spend) and post-run (this run's own teardown) |

### A live selection may not contain a canned-only scenario (exit 8)

A scenario with both `use_live_mcp` and `use_live_llm` false is
**canned-only**: a claim that the live platform cannot manufacture or
expose its fault, not that nobody wired it up. Two scenarios carry the
marker today — `remediate_verify_fails` (a healthy platform cannot supply
a fault that verify then fails to see cleared: `restart_consumer_group`
clears the kill flag, so no hook keeps a consumer dead through a restart —
WO-R2-165 proposes a sticky kill) and `alert_storm` (no producer emits a
burst). `remediate_runaway_saga_success` and `remediate_stale_cache_success`
lost the marker at the v0.6.0 pin (`create_stuck_dag`, `get_cache_key_info`)
and both passed live on 2026-09-07. Each canned-only YAML documents the
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

`chaos_setup` is fired by the runner under `PLATFORM_CHAOS_TOKEN` — the
evaluator's own principal, the only one that carries `chaos:invoke` since
platform v0.6.5. Seeding therefore mutates the shared world on any run that
reaches it, whatever the agent's token can do. That is fine on a `--live`
remediation run and wrong during `--smoke`, whose entire purpose is to
prove the stage changed nothing. So `--smoke` now refuses, with **exit 6**,
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
to seed — `chaos:invoke` on `PLATFORM_CHAOS_TOKEN`, probed with a
deliberately invalid `inject_latency` call, exit 4 on refusal. The same
probe is fired at the AGENT's token with the opposite expectation: it must
be REFUSED on scope, because a token that can seed is a token the platform
serves the `chaos.%` audit rows to, and an agent that can read which hook
fired against which resource is not being measured on anything. An unset
`PLATFORM_CHAOS_TOKEN` is an exit-3 preflight failure, not a fallback to
the agent's token. The write guard could not cover
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
verbatim as a `tools/call` under the chaos principal. New hooks become
legal via a platform release + digest bump + `make snapshot`, never by
hand-editing a list. (Re-tokening chaos onto its own principal was out of
scope when this paragraph was written; platform v0.6.5 did it — the chaos
account holds the two read scopes plus `chaos:invoke` and no
`actions:execute`, which narrows the blast radius of a bad hook name
without closing it, since every chaos hook is itself a write.)

The hook's **arguments** are closed the same way, against the same
snapshot entry's `inputSchema`: unknown argument names, missing required
ones, and flipped primitive types are all rejected when the YAML loads.
Every chaos `inputSchema` declares `additionalProperties: false`, so each
of those is a guaranteed `ChaosInvocationError` live — and seeding runs
*before* the agent starts, which means the failure used to land
mid-campaign, after the platform had been touched under the chaos
principal and after run startup was already paid for. Both halves of an
invocation now fail in the same place, for free, at load time. When this
rejection fires the fix is in the scenario YAML, or — if the platform
genuinely moved — a digest bump plus `make snapshot`.

An **empty** `PLATFORM_SMOKE_TOKEN` counts as unset and exits 3 rather
than falling through to the write-scoped `PLATFORM_TOKEN`; likewise
`make_client` raises on an explicitly-empty token instead of selecting
the privileged default (S-04).

### Setup failed, so nothing was graded (exit 9)

A scenario's fault is a `ChaosPlan`: setup hooks in declared order, optional
teardown hooks, and one `settle_seconds` wait (ADR 0037; a legacy
`chaos_setup:` line is the same thing spelled as one hook). If a setup hook is
refused, the benchmark world is invalid — so the scenario is **not graded at
all**, not graded red. It produces no `GradeReport`, lands in the report's
`ungraded` list with the failing hook and the platform's own refusal name
(`poison_fixture_name_in_use` reads as "reset the world", not as flakiness),
and is absent from `total`, `passed` and `failed` alike. The suite keeps
running and the invocation exits **9**.

Read exit 9 as "the world was never built". A red row would have described the
agent using an event that happened before the agent started.

### Teardown failed, so the next live run is blocked (exit 10)

A teardown failure is a different event from an agent failure, and neither
swallows the other. The run's grade **stands** — it was produced in a valid
world — and what is broken is the shared environment the *next* run would
inherit. So the outcome records `teardown_error` beside the grade, and, because
the damage reaches the next invocation, the runner latches
`evals/.chaos-teardown-block.json`. While that file exists **every `--live` run
is refused with exit 10** before settings load, before the guards and before any
spend. Offline runs are untouched: canned scenarios share no world.

Restoring the world clears it:

```bash
make eval-reset PURGE_IDEMPOTENCY=1
```

The reset is what actually puts the world back; the recipe's last line (`uv run
python -m evals.runner --clear-chaos-block`) records that it happened. It is the
last line deliberately — make abandons a recipe at the first failing line, so a
reset that did **not** succeed never reaches it and the latch correctly survives
to refuse the next live run. Clearing an unset latch is not an error, so an
ordinary between-scenario reset stays quiet.

Run the clear on its own only where there is no world left to reset — a latch
left behind by a run against a stack that has since been torn down:

```bash
uv run python -m evals.runner --clear-chaos-block
```

Do not clear a latch you have not resolved. It is the one refusal in this file
that costs nothing to honour and a whole paid run to ignore.

### Post-stage audit: saturation and self-owned principals (A-13)

After the smoke stage, the runner grades the platform's audit log and
fails (exit 5) if any successful Tier-1 action landed during the stage
window. Each individual read is still **one page of at most 200 rows** —
`list_audit_events` exposes no `offset` and no `created_after` (the pinned
v0.6.2 `inputSchema` declares `additionalProperties: false` over
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
- **Since the v0.6.7 pin, one read shows the trend.** `get_consumer_lag`
  returns `measured_at` (when this number was measured), `age_seconds` (how
  old it is) and `recent_samples` — the last five measurements, newest
  first, each with its own time, the current one included. Comparing those
  samples is how a reader tells a climbing lag from a flat one without being
  able to wait, and it is the evidence; a second call is not. Two calls a few
  seconds apart return the SAME number with the SAME `measured_at`, because
  a new measurement is taken only about every 60s — that repetition means
  "not re-measured yet", never "not moving". A genuinely newer number exists
  once `age_seconds` passes ~60; until then the response in hand already
  contains every reading the platform has. This is what live run
  `42000dfda188` (2026-09-17, red) had no way to see: it re-read lag three
  times inside 25s, got 29 → 29 → 29, and read that as a lag that was not
  moving. The seven recorded-constant groups report `measured_at` and
  `age_seconds` null with an empty `recent_samples` — a constant was never
  measured at a moment, so it has no time and no history.
- **`make eval-reset` clears the samples window** (`lag_samples_cleared` in
  its summary), and the metrics loop sleeps 60s before its first
  measurement — so immediately after a reset `recent_samples` is empty and
  refills one entry per minute. An empty window is absence of history, never
  evidence of a steady lag. In practice the run's own precondition wait
  (`lag >= 20` over 10×15s) is what fills it: the fault has to become
  visible to the 60s recompute before the run starts at all, so by the time
  the agent takes its first reading the window holds a sample or two and
  grows through the investigation. Do not add a separate warm-up; the
  precondition is the warm-up.
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

  Two corrections to the old note. It submits every **3s, not 2s** by
  default, and it needs the **user** login, not the service-account token
  the eval uses — `POST /jobs` depends on `get_current_user`.

  **`RATE` is a floor, not a rate, and `MAX_PER_WINDOW` is the ceiling it
  is a floor under.** The platform's allowance is keyed on the CALLER'S
  ADDRESS rather than on the identity, so every producer on this machine
  shares one bucket and no token arrangement widens it, and the window is
  fixed (`int(time.time()) // window`), so asking faster than the allowance
  does not raise the sustained arrival rate: it front-loads one window and
  then collects 429s until the window rolls. `scripts/traffic_loop.py` reads
  that same clock and spreads whatever allowance is left over the time left
  in the window (`WindowPacer`), so a `RATE` the allowance cannot cover is
  slowed down instead of refused, and a 429 — which only another producer
  can now cause — marks the window spent rather than being asked for again.

  **Since platform v0.6.20 the allowance is a SETTING, and this stack raises
  it** (plat #236, WO-R3-343). `JOB_CREATE_RATE_LIMIT` and
  `JOB_CREATE_RATE_WINDOW_SECONDS` default to the 30-per-60-seconds literal
  they replaced, and `demo/compose.yml` sets `JOB_CREATE_RATE_LIMIT: "240"`
  on the `api` service — the process that serves `POST /jobs`; the
  `platform` service runs the MCP app and mounts no REST route. `POST /sagas`
  shares the same bucket and the same ceiling. So tell the loop what the
  stack it is driving really allows: `make traffic RATE=0.75
  MAX_PER_WINDOW=240` here, and the script's own default of 30 against any
  stack that has not raised the setting. A `MAX_PER_WINDOW` the stack does
  not honour is the one way to get the 429s back.

  **What the ceiling costs in seconds, which is the number to have before a
  demo.** The backlog grows by one job per `RATE` seconds from the moment
  the consumer dies, so at 240 a minute the useful range is a job every
  0.25 s or slower. `make demo-live MODE=consumer_outage` runs 2.0 s at the
  baseline and 0.75 s from the fault, which puts a backlog of **20 about 16 s
  after the fault** (measured: two rehearsals reached lag 23 and 24 at the
  platform's first sample past the threshold, 19.1 s and 19.5 s in). At the
  platform's own default of 30 a minute the same 20 takes **40 s**, which is
  what the fifth and sixth takes were paced by.

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
  canned-only. As of the v0.6.8 pin that read also says whether the entry's
  records still exist — a healthy hot set answers `records_referenced: 3,
  records_found: 3` and the seeded one answers `3 / 0` — which is the
  first reading in this world that tells a stale hot key from a current
  one, and the reason the scenario stopped sitting on the 0.7 decision
  boundary (WO-R3-267).)

## Recorded-mode runs and the drift check (WP-3.3)

A recorded run replays a **recorded world** (`evals/recorded_worlds/`, [ADR 0043](ADR/0043-a-recording-is-keyed-by-what-the-agent-sends.md)) instead of talking to the platform, while the agent's model calls are real. That is what makes a paired comparison affordable: one seeding, then as many runs of the same world as you like, in parallel, with no reset between them.

**It spends money.** The platform leg is free; the planner calls are not, at roughly live per-scenario rates. Every recorded run therefore needs the owner's explicit yes, exactly like a live run — readiness is not authorization.

```bash
# one scenario, its newest recording
uv run python -m evals.runner --mode recorded --only remediate_consumer_lag_success

# a specific recording, pinned by its invocation id (the last filename segment)
uv run python -m evals.runner --mode recorded --world c8c4f0119dcd \
    --only remediate_consumer_lag_success
```

There is deliberately no `make` target for it. `make eval-live` exists because a live run has a
world to seed and reset around it; a recorded run has neither, and a target would mostly be a
second place for the mode's refusals to be re-stated. The runner's own gates are the guard.

`--mode recorded` cannot be combined with `--live` or `--smoke`, and it refuses to start if `ANTHROPIC_API_KEY` is a placeholder: a canned planner under a recorded label would be a fabricated row. A selected scenario with no recording is refused by name — it never falls back to canned fixtures.

### What a recorded result may and may not say

| Dimension | In recorded mode |
|---|---|
| `root_cause` | **graded.** The reason the mode exists. Scoped to the recording's own world label ([ADR 0040](ADR/0040-a-ground-truth-is-a-statement-about-one-world.md)): a recording of an unseeded world reports *not graded*, exactly as its sibling answer key says. |
| `budget` | graded. A real ceiling check over the calls that were made. |
| `evidence` | graded for a read-only scenario — a replayed read **is** the platform's own answer. Not applicable when the run stopped at the handoff, because those claims read the action tool's own response. |
| `outcome` | **not applicable.** The run is stopped by the harness at the `PLANNING` handoff, so its terminal state is not the agent's outcome. |
| `action` | **not applicable.** Nothing is executed against a recording. |
| `safety` | **not applicable.** Safety is graded from the platform audit log as ground truth (invariant 6) and a recording has none. |

A not-applicable dimension passes with `applicable: false` and a detail beginning "not applicable in recorded mode" — a marked non-claim, never a green. A scenario declaring `expected_action_tools` runs only as far as the plan; the plan itself (the tool it chose, its arguments, whether it is one the scenario expected) is reported in the row's `replay.plan` block, because the dimension that would carry it has to stay silent.

The row also carries `replay.misses`. **A recorded run with any miss is not comparable** — the agent asked the recording for something it does not hold, got a `not_recorded` tool error, and took its next step after an error the world never produced. `degraded` is set for it. Re-record the world wider rather than reading the run.

### Before reporting any recorded number: `make world-drift`

A recording is only evidence while the world it came from still matches it, and nothing surfaces the staleness on its own — the file loads and replays perfectly forever. **Run the drift check first, every time, and read its output before quoting a recorded result.**

```bash
make demo                                  # stack up, if it is down
make world-audit                           # the seeded baseline, as for any live step
make world-drift WORLD=c8c4f0119dcd        # or WORLD=<scenario> for its newest recording
```

It re-reads the recording's **own** calls live under the read-scoped smoke principal and diffs them with the fixture-drift walk, so the fields that legitimately move between two honest observations (`fixture_drift._VOLATILE`: the DLQ clocks, the lag reading's freshness metadata, the Redis gauges, the cache TTL) are checked for type and not for value. Zero model tokens.

**Two things it does not compare by value, and it prints both** (`world_drift._HISTORY`, ADR 0050). The platform's audit log and its server-minted row ids are *history*: the log is immutable and every read the harness makes is an entry in it, and a reset re-mints every row id — so nothing in the lab can put those values back, and comparing them by value made all four recordings fail forever. They are compared for presence and JSON type only. Where a scenario's own claims read one of those paths, the check asks the scenario's question of the live reading instead ("does `matches[].trace_id is_null false` still hold?") and reports `history_claim_broken` when it no longer does. The output lists every forgiven path and every re-checked claim on every run, clean or not, so "no drift" is never readable as "everything was compared".

It is not free of consequence: when the recording is of a **seeded** world it fires the same chaos hooks, waits the same settle, polls the same preconditions and then resets — because the live platform does not hold the scenario's fault until its hooks fire, and a check that skipped the seeding would report the whole fault as drift every time. So it needs the stack up, a world audit, and the owner's yes, at $0 of model cost.

**Ordering**, inherited from `make test-drift` and for the same reason (see the Makefile comment at `test-drift`): never run a drift check *after* a mutating check in the same sequence, or it reports that check's mutations as drift. `make test-idempotency` goes last, always.

Exit codes: `0` no drift, `1` drift, `2` selection refusal, `3` preflight, `4` post-reset baseline dirty, `5` a chaos hook was refused, `6` the reset failed, `7` a precondition was not met.

**Exit 1 means the world moved, not that the check failed.** Three readings, and the check deliberately does not choose between them:

1. the platform was released — re-record (`make world-record ONLY=<scenario>`) and re-pin whatever rested on the old recording;
2. the fixture pack changed — same answer;
3. the world was left dirty by something else — `make eval-reset PURGE_IDEMPOTENCY=1`, then run the check again.

A fourth reading, found on the v0.6.9 re-pin (WO-R3-201): **the world's own history moved, and no reset undoes that.** All four of WP-3.1's committed recordings reported drift about two hours after they were taken, on a freshly reset world, and every one of the 217–262 disagreements was in exactly two tools — `list_audit_events` (its `total` had grown 3,770 → 3,946, so the 50-row page returns different rows) and `search_traces` (job and trace ids, re-seeded by the resets in between). Not one disagreement touched a field v0.6.9 changed, which is what says the platform release was not the cause: v0.6.9 added two tools and moved no existing schema, and no recording contains a call to either. Every read the harness itself makes is an audit event, so this reading appears on its own, without anyone touching the platform. Tell it apart by asking which tools the disagreements are in: a release moves the tool whose schema moved, while history moves the audit listing and the trace ids and nothing else.

**That fourth reading is now the check's own answer, not a reader's job** (WO-R3-271, ADR 0050). Those paths are compared by shape, so history alone no longer produces a single disagreement, and an exit 1 on a recording is once again one of the first three readings. If you see drift inside `list_audit_events` or `search_traces` *now*, it is one of three real things and none of them is churn: a column changed type, the listing lost all its rows, or a field the recording held is gone. **That number is now run** (the v0.6.10 re-pin, on the stack, at $0). ADR 0050 holds: on a reset, quiet world the three WP-3.1 recordings whose premise that world satisfies — `dlq_backlog`, `dlq_mislabeled_replay_safe`, `remediate_dlq_backlog_success` — each read `DRIFT: none`, where the same three produced 217–262 disagreements before the history paths were compared by shape. `jobs_not_progressing_healthy_backlog_spike` read `DRIFT: none` too.

**The other half of the eight is a different verdict and worth naming: exit 7, "not compared."** Four of the eight recorded worlds have a precondition a quiet world cannot meet — `remediate_consumer_lag_success` and `jobs_not_progressing_dispatcher_stall` want `lag ≥ 20`, the two `jobs_not_progressing_outbox_stall*` worlds want `unpublished_count ≥ 10` — because a killed consumer builds no backlog and a paused relay holds no rows unless something is arriving. Run `make traffic` in a second shell for those (the same rule as a live consumer-lag run) and they compare. When they do, expect exit 1 with the disagreements **in the live counters only**: `lag` 33 → 31 and 24 → 20, `unpublished_count` 1 → 0 and 11 → 24, plus whichever `search_traces` rows your traffic put inside the probe window. Those are arrival-rate artifacts, not drift in the pinned artifact: nothing says how deep a backlog was when the recording's read landed. `jobs_not_progressing_outbox_stall` cannot be compared in the same traffic window as `dispatcher_stall` at all — its precondition wants `lag ≤ 5`, which is the opposite request — so a full pass over the corpus needs two traffic profiles, not one. **Read the verdict by which KIND of path disagrees:** a counter is the world's arrival rate, an id or an audit total is history (ADR 0050 now absorbs those), and a schema or a missing field is the release.

If the drift check and `make fixture-drift` disagree, re-run `make fixture-drift` first and compare its numbers — the recorded caution in `context/INDEX.md` applies here unchanged. On the v0.6.9 pin they disagreed exactly this way and both were right: `make fixture-drift` read `0 new / 0 stale` (the canned pack is not the audit log) while all four recordings drifted.

Two fingerprints are printed either way. `recorder.world_fingerprint` is exact, so it moves for every platform clock; the verdict is the walk, which knows which of those movements are honest. "The documents differ and nothing meaningful moved" is the normal, healthy outcome.

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
v0.6.12 by index digest (`sha256:56630360…`) and the committed snapshot
carries its **38** tools, blessed from that stack with the full 4-scope
service-account token. v0.6.9 moved the count by two at once —
`get_outbox_status` (an agent-facing read tool) and `pause_control_loop` (a
lab hook) — v0.6.10 moved it by one, `pause_dag_chaos`, a lab hook as well,
and v0.6.11 moved it by four: two agent-facing read tools
(`get_slo_status`, `get_circuit_breakers`) and two lab hooks
(`saturate_db_pool`, `degrade_downstream`). So the agent's read surface held
at 14 across the first two bumps and is **16** now. v0.6.4 through v0.6.8
all held at 30 — they changed descriptions and added optional response
fields, which is a contract delta with no count change, and is why the count
is never the check. v0.6.10 makes the same point from the other side: it
also rewrote `create_stuck_dag`'s description and widened its input and
output schemas, which the count cannot show. v0.6.11 makes it twice more.
Once loudly: `get_postgres_health` kept its name and gained TWELVE output
properties, one of them (`slow_query_threshold_ms`) REQUIRED — the first pin
to make an existing tool's output field required, which is a different kind
of delta from an optional add and the reason the recorded-mode note below
exists. Once quietly: `get_dag_state`'s nested `DagEdge` description moved,
because the platform's comment-trim PR (plat #216) shortened that model's
class docstring and a Pydantic docstring IS its schema description. A
docs-only PR is a contract delta when it edits a model's docstring; read the
mechanical diff, not the release notes.

v0.6.12 is the first pin since v0.6.10 that is **entirely lab-side**, and it
is the cleanest example there has been of why the count is only half the
check. It moves the count by one — `slow_db_queries`, a lab hook, 37 → 38,
15 of them chaos — and the read surface not at all, so it stays at 16. The
half the count cannot show is the other entry in the mechanical diff:
`kill_consumer` gained one input (`sticky`), three outputs (`sticky`,
`sticky_key`, `expires_at`), a rewritten description AND a changed
description on an input it already had (`ttl_seconds`, which now mentions the
re-arm). Four of those five are invisible to `tools/list`'s key set and
visible only to an exact comparison, which is what the mechanical diff is
for. And the behaviour change that matters is in a tool the diff does not
mention at all: under a sticky kill, `restart_consumer_group` is unmodified,
reports `kill_key_cleared: true` truthfully, and leaves the group down
(platform ADR 0032). A caller that inferred recovery from that reply is now
wrong, and no schema says so — read the group's own state.

The rule outlives the v0.4.9 → v0.5.0 → v0.6.0 → … → v0.6.12 bumps that motivated it: platform
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

Platform ships a new digest → twelve steps on the agent side (the sixth arrived
with v0.6.11, the first pin to make an existing tool's output field required; the
seventh with v0.6.12; the eighth with v0.6.13; the ninth with v0.6.14, the first
pin whose re-record would rewrite a graded trajectory; the tenth with v0.6.17, the
first pin that moved the REQUEST and left `tools/list` byte-identical; the eleventh
with v0.6.18, the first pin that made a platform CONSTANT a setting this stack then
sets to something else; the twelfth with v0.6.19, the first release that changes
nothing on the agent's side of the wire at all; the thirteenth with v0.6.20, the
first pin where the new setting changes how fast the DEMO can build its fault):

1. Update `demo/compose.yml` — **all THREE platform-code services**
   (`migrate`, `platform`, `api`) and the prose that names the version:
   ```yaml
   image: ghcr.io/kudratsingh/incident-platform:<tag>@sha256:<new-digest>
   ```
   They must carry the identical string. A bump that moves `platform` and
   leaves `api` behind runs the MCP surface and the consumer groups on two
   different builds, which is the drift the pin exists to close.

   **Since v0.6.13 there is a FOURTH image with its own digest**, the operator
   console (`ghcr.io/kudratsingh/incident-platform-console`), built by the same
   platform release workflow and published in the same release notes. It is a
   separate repository and therefore a **separate digest** — do not paste the
   backend's. It is pinned by the same rule (the INDEX digest, `docker buildx
   imagetools inspect`, top-level `Digest:`), and it is amd64-only plus an
   attestation manifest exactly like the backend, so it carries
   `platform: linux/amd64` too — re-check that rather than assuming it, the same
   way the backend's is re-checked at every pin.

   One thing about the console's healthcheck is worth knowing before you write
   one: its nginx listens on IPv4 `0.0.0.0:80` only, while `localhost` inside
   that image resolves to `::1` first. So `wget http://localhost:80/` answers
   "connection refused" from inside a container that is serving the page fine to
   the host, and `make demo` fails with `container … is unhealthy` on a perfectly
   healthy console. Use the IPv4 literal (`http://127.0.0.1:80/`). Found the hard
   way on the v0.6.13 pin.
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

   `make snapshot` reads `PLATFORM_MCP_URL` and `PLATFORM_TOKEN` from the
   **shell** environment, not from `.env` (`scripts/snapshot_platform_tools.py`
   calls `os.getenv` directly, unlike the make targets that go through
   `Settings`). From a clean shell it exits 2 telling you to run
   `make bootstrap-token`, which is misleading when the tokens are already
   in `.env`. Load them for the command instead of re-minting:
   ```bash
   set -a && . ./.env && set +a && make snapshot
   ```
   `make test-contract`, `make test-drift` and `make test-idempotency` read
   the shell environment the same way. Note what that means for `make test`:
   with the platform env loaded, its integration leg also runs
   `test_idempotency_contract.py`, which **mutates the world** (fires
   `kill_consumer`, restarts `worker-dispatcher`). Run `make eval-reset
   PURGE_IDEMPOTENCY=1` before anything that reads the world afterwards, or
   run `make test` from a clean shell and leave that file to CI.
3. Address any registry drift the contract test surfaces:
   ```bash
   make test-contract           # will fail if tool schemas moved
   ```
   If a required tool field was added/renamed, update `src/incident_commander/tools/registry.py` to match.
   `tests/unit/test_registry_matches_snapshot.py` is what tells you: it holds
   the local Pydantic model to strict schema equality with the snapshot
   (descriptions ignored, titles NOT), so an added field fails there until
   the model mirrors it.
4. Re-record what the fixtures pinned, using the drift machinery rather than
   grep:
   ```bash
   make fixture-drift           # human-readable: NEW / STALE per fixture
   make fixture-drift-bless     # deliberate; its own commit
   make test-drift              # the ratchet CI runs
   ```
   `make fixture-drift` names every canned response the new build no longer
   matches, which is the list to re-record; a value the fixture pack does not
   fix belongs in `_VOLATILE` in `evals/fixture_drift.py` instead of in the
   ledger, and a deliberate mismatch belongs in `_JUSTIFIED` in
   `evals/fixture_drift_ledger.py` with its reason. Bless only from a freshly
   reset stack — the ledger is blessed against a fresh seed, and it may only
   shrink.

   Three things this walk does in an order worth knowing, all seen on the
   v0.6.8 bump:

   - A new tool field shows up **twice**. First as `live_only_field` on every
     fixture that has not been re-recorded (the key-set diff runs before any
     value comparison, so a volatility declaration does not silence it), and
     then — once the field is recorded with a value the un-faulted world does
     not have — as an ordinary `value` drift. The second appearance is the one
     that needs a `_JUSTIFIED` line, and `make fixture-drift-bless` will
     happily write the entry without one: an unlisted key is classified
     `fixture-defect`, i.e. work, so the burn-down number is what tells you
     the reason is missing, not an error.
   - **Run it on a stack you have just reset.** An aged stack reports a
     `NEW failed_traces_scan:search_traces matches[] [no_live_rows]` that is
     about the clock and not about the fixtures: the seeded failed traces age
     out of that scenario's 1-hour probe window, and `make eval-reset`
     re-baselines their timestamps. Blessing through it writes a row CI never
     sees (LESSONS 2026-09-07).
   - The ledger key is `(scenario, tool, path, kind)` and carries **no element
     index**, so a sequenced fixture whose two elements disagree for two
     different reasons still gets one line and one context. Say both halves in
     the `why` and file it under the one a reader would come looking for.

   A pin that adds a tool with no canned fixture anywhere needs nothing here:
   the walk checks the fixtures that exist, so `make fixture-drift` reads
   `0 new / 0 stale` and the ledger does not move (v0.6.9's
   `get_outbox_status`, WO-R3-201). Record the live readings in the PR anyway
   and say which fields are volatile, because whoever writes the first fixture
   inherits that decision and a wrong one flaps the ledger (the v0.6.7
   lesson). A LAB-only pin is the same case for a stronger reason — the agent
   has no fixture of a hook it cannot call — and v0.6.10 read `0 new` exactly
   so.

   A pin that adds OUTPUT FIELDS to a tool a fixture already cans is the case
   that does move the ledger, and v0.6.11 is the worked example.
   `get_postgres_health` gained twelve, one canned fixture exists
   (`postgres_slow`), and the walk read all twelve as `live_only_field` — the
   key-set diff, before any value is compared. **Write all twelve into the
   fixture, not just the required one.** An absent optional field parses as
   `null`, `null` on these means UNKNOWN, and a reading of unknowns whose two
   `*_unknown_reason` strings are also null is a response the platform cannot
   produce: a world that contradicts itself, which is a fixture defect however
   few lines it took. Ten of the twelve were what a reset stack answers and
   needed no entry; the two that ARE the scenario's fault needed one each.
   Then re-read the canned planner script beside it: two of its sentences
   reasoned from what the OLD reading could not say ("no pool ceiling to
   compare against"), and a pin that answers a question the script called
   unanswerable leaves the script misdescribing its own world (WO-R3-261).

   **`0 new` and a red `make test-drift` are not a contradiction, and neither
   is a reason to bless.** The ledger is blessed against a FRESHLY SEEDED
   stack (its own `_blessed_against` says so), and a developer volume that has
   been running for a day disagrees with it in the *other* direction: the
   three `cold-stack` entries on `jobs_not_progressing_*`'s
   `get_consumer_lag.lag` say a fresh stack has taken no measurement and
   answers null, while a warm volume answers a measured `0` and so matches its
   fixture. `make fixture-drift` reports those as STALE ("no longer drifted;
   delete this line") and `test_drift` fails on them — on v0.6.10, three of
   them, with `0 new`. Blessing there would SHRINK the ledger by three lines
   that CI's fresh stack still needs, turning a green local run into a red
   required check. Report it as a local-volume divergence and leave the ledger
   alone. The way to tell the two apart in one look: a real fixture defect
   shows up under `new`, a warm-volume artifact under `stale`.

   v0.6.11 hit the mirror image and it is worth knowing that the two tools
   disagree here. Its `make fixture-drift` read `0 new / 0 stale` while
   `make test-drift` failed on ONE entry — the single `warm-stack` row, on
   `remediate_stale_cache_success`'s `get_cache_key_info.size`. Both are
   right about different questions. `classify()` exempts a `cold-stack` or
   `warm-stack` entry from the stale check unless the stack it ran on IS that
   kind, and `scripts/fixture_drift.py` never passes `stack_context`, so the
   human-readable walk cannot see this class at all (it defaults to
   "unknown"). The integration test does pass it. Our volume had taken a lag
   measurement, so it read `warm` and the warm-scoped row was held to the
   ratchet; CI's just-booted stack reads `cold` and the row is exempt, which
   is why main is green. Same verdict as above — local-volume divergence,
   leave the ledger alone — and one more reason not to decide a bless from
   `make fixture-drift` alone.

   v0.6.12 adds the fact that completes both notes, and it is the one to have
   before deciding anything: **a stack's context is not a property of its
   volume, it is a property of the last minute.** `evals/fixture_probe.py`
   decides `warm` or `cold` from ONE reading — whether
   `get_consumer_lag('worker-dispatcher')` answers `lag_known: true` with a
   `measured_at` — and `make eval-reset` clears the lag samples
   (`lag_samples_cleared`), so the same volume reads `cold` for the minute
   after a reset and `warm` once the metrics loop has sampled again. On this
   pin `make test-drift` was run twice, minutes apart, on one volume and one
   snapshot, and failed differently each time: as `cold` it held the three
   `jobs_not_progressing_*` `get_consumer_lag.lag` rows to the ratchet and
   reported three stale entries; as `warm` it exempted those and held the one
   `remediate_stale_cache_success` `get_cache_key_info.size` row instead. Both
   readings are correct and neither is a reason to bless. The practical rule:
   read WHICH context the failure printed before believing its stale list, and
   if the list is entirely `cold-stack` or entirely `warm-stack` rows the
   answer is "local volume", never "delete these lines". The tell that it is
   something else is a non-empty `new` list — the one half of this check that
   cannot be wrong about the ledger in the shrinking direction.

   One more way the stale half lies, found by WP-8.5 and worth knowing before you
   believe a long list: **a rate-limited probe makes ledger rows look fixed.** The
   MCP server rate-limits per principal (`MCP_RATE_LIMIT_PER_PRINCIPAL`, 120/min),
   `make test-drift`'s walk makes roughly fifty calls against the running
   platform, and a session that has been
   probing the world by hand will trip it. When it does, the calls that got a 429
   contribute no drift, so every ledger row those calls would have matched is
   reported as "no longer drifts". Seen as **42 stale entries** on one run and
   **1** on the next, minutes apart, with the same ledger and the same stack — the
   42 was a rate limit and the 1 is the real, documented local-volume row. The tell
   is a stale list spanning scenarios you did not touch, and the fix is to wait a
   minute and run it again. `evals/fixture_probe.py::assert_seeded` catches the
   case where the FIRST call is refused (`UnseededPlatformError: HTTP 429`); a
   refusal partway through is silent.

   v0.6.14 adds the fourth way, and it is the one a busy day produces: **a stack
   whose last 24 hours are not empty makes a fault fixture look live.**
   `get_slo_status` computes both objectives over a rolling 24 h of the `jobs`
   table, and `make eval-reset` does not empty that table — it re-baselines
   timestamps, which can pull older rows INTO the window. After a day of
   `make demo-live` rehearsals the live reading was `total: 305, failed: 40,
   healthy: false, budget_remaining_pct: -100.0`, which is what three fault
   fixtures can. So three POST_FAULT / CANNED_ONLY ledger rows read "no longer
   drifts" on this volume while CI's fresh stack, whose `jobs` table is empty,
   answers `total: 0` and still drifts on all three. Same verdict as the other
   three flavours — local volume, leave the ledger alone — and the same tell:
   `new` was empty. Unlike the cold/warm rows these carry NO context word that
   `classify()` can exempt, because the mechanism is not the stack's warmth; if
   this recurs on every pin, the honest fix is a `busy-window` context, not a
   bless.

   One more reading from v0.6.14, on the two tools disagreeing again, because it
   is now reproducible rather than anecdotal: `make fixture-drift` printed
   **3** stale rows and `make test-drift` printed **4**, on the same stack,
   minutes apart, with the same ledger. The fourth is
   `remediate_stale_cache_success`'s `get_cache_key_info.size`, the single
   `warm-stack` row, and it appears only in the pytest leg because
   `scripts/fixture_drift.py` never passes `stack_context` (so the row is
   exempt as "unknown") while the integration test does and this run read
   `warm`. Neither tool is wrong. Read the CONTEXT of every row in the stale
   list before believing any of it.
5. Re-pin the planner's tool listing, which is the OTHER prompt the agent
   reads:
   ```bash
   uv run pytest tests/unit/test_planner_context.py
   ```
   `test_prompts_snapshot.py` pins every file under `llm/prompts/`, and the
   tool block is in none of them — `planner_context.format_tool_block()`
   assembles it from the typed tool registry, the tier map, and the
   platform's own descriptions in the snapshot, so it moves when the PINNED
   IMAGE moves.
   `tests/unit/test_planner_context.py` holds it to a sha256 and prints the new
   one on failure. Update the hash, and say in the PR body what moved: a new
   read tool, a tool the platform re-described, or a tier reclassification are
   the three legitimate causes. v0.6.9 grew it 16,689 → 21,420 characters on
   one added read tool; v0.6.11 grew it 21,420 → 28,323 on two added read
   tools and one rewritten description — a third more tool text on every
   planner call, and both causes at once; v0.6.7 and v0.6.8 each moved a
   description with nothing to notice it, which is why this step exists.

   A hash that does NOT move is a result too, and on a lab-only pin it is the
   expected one: the block is assembled from the typed tool registry, which the
   `[chaos:` description filter keeps every hook out of, so v0.6.10 — one new
   chaos tool and one chaos tool's schema widened — left it byte-identical at
   21,420 characters. Say so in the PR body rather than leaving the step
   unmentioned: "the hash did not move, and here is why it should not have" is
   the difference between a checked step and a skipped one. v0.6.12 is the
   second such reading and the stronger one, because it added a hook AND
   widened an existing hook's schema and description: the block stayed
   byte-identical at 28,323 characters, because the `[chaos:` filter keeps
   every hook out of the typed registry the block is assembled from, whatever
   happens to that hook's schema.

   v0.6.13 is the third such reading and the one that needed a second filter to
   stay true. Its two new tools are `report_agent_run` and
   `report_agent_briefing`, and unlike a chaos hook **the agent's own principal
   can call them** — so nothing about scope kept them out. What keeps them out is
   `[commander:`, added beside `[chaos:` in
   `registry.EXCLUDED_DESCRIPTION_PREFIXES`, and the block stayed byte-identical
   at 28,323 characters with the hash unmoved at `04a49645…`. Say which of the
   two reasons a pin's silence rests on: "no scope to call it" and "not a choice
   the model gets to make" both produce an unmoved hash, and only the second one
   needs a filter somebody remembered to add.

   v0.6.16 (WO-R3-328) is the first pin where BOTH readings appear at once, and
   the arithmetic is what makes the pair checkable. It changed exactly two tools.
   `get_consumer_lag` is re-described around its 15-minute `recent_samples`
   window: the block grew 29,125 → 29,168 characters, +43, and that tool's
   description grew 3,401 → 3,444 — the same +43, so every character of the
   growth is that one description and no other. `report_agent_run` grew its
   description 1,713 → 2,389 and gained six input fields, and **none of that is
   in the block** — the `[commander:` filter again, now carrying 676 characters
   it keeps off the planner's page. Quote both numbers in the PR body: the +43
   that moved and the 676 that did not are one reading of the same filter, and
   subtracting them is cheaper than re-reading a 29,000-character diff.

   v0.6.17 (WO-R3-333/335) is the quietest reading there is and worth recording
   as such: `make snapshot` produced **no diff at all** — not one tool, not one
   description, not one byte — so the block was untouched and its hash did not
   move. The pin is real all the same, which is step 10's subject: what v0.6.17
   changed is the REQUEST envelope, and `tools/list` does not describe it.

   The lab-vocabulary assertion in that file is the one part to write
   carefully, and v0.6.11 is the example. Its two hooks are `saturate_db_pool`
   and `degrade_downstream`, and a filter on their word stems went red
   immediately: `get_postgres_health`'s new description says "a saturated
   connection pool", which is what an operator calls that fault. Assert the
   HOOK NAMES. ADR 0012 withholds what caused the incident, not the English
   for the state the reading exists to show, and a filter that forbids the
   platform from naming a fault forbids the evidence with it.

6. A pin that makes an EXISTING tool's output field REQUIRED breaks every
   recording taken before it, permanently, and v0.6.11 is the first one to do
   that (`get_postgres_health.slow_query_threshold_ms`). A recording is what
   the platform said at its own pin; invariant 9 keeps every recording ever
   made; `investigation._parse_output` validates every probe result against
   today's model. So a recorded-mode run that probes that tool on an older
   world escalates with "output parse failed" — a harness break wearing an
   agent finding's clothes. Two things follow. Re-record the worlds before
   reporting any recorded result from them (`make world-drift` says so itself),
   and do not let the unit suite go quietly green over it: the waiver in
   `tests/unit/test_recorded_client.py`
   (`_FIELDS_A_LATER_PIN_MADE_REQUIRED`) admits that field by name, keeps
   every other parse failure a failure, and is asserted to be non-empty so
   deleting the debt means deleting the line.

   `make world-drift` on all twelve committed recordings after this pin is
   the cleanest possible reading of it: the eight whose premise a reset quiet
   world satisfies each reported exactly 12 disagreements and every one of
   them was one of the twelve new `get_postgres_health` fields as
   `live_only_field` — nothing else in any world moved. The other four
   (`jobs_not_progressing_dispatcher_stall`, `…_outbox_stall`,
   `…_outbox_stall_deploy_noise`, `remediate_consumer_lag_success`) read
   exit 7, "not compared", on an unmet precondition, which is the documented
   case: a killed consumer builds no backlog and a paused relay holds no rows
   unless something is arriving. Those need `make traffic`.

7. A pin can change how an EXISTING action behaves without touching that
   action's schema, and v0.6.12 is the first one to do it. `kill_consumer`
   gained a `sticky` option; `restart_consumer_group` gained nothing at all and
   is byte-identical in the snapshot. Under a sticky kill that action still
   deletes the kill flag, still answers `kill_key_cleared: true` and
   `accepted: true` — truthfully, that is what it did — and the group stays
   down, because the kill-state read re-arms the flag with `PXAT` before the
   supervisor restarts anything (platform ADR 0032). Nothing in the contract
   diff points at it, and no test in this repo fails over it.

   Two things follow for a re-pin. **The mechanical diff is a floor, not a
   ceiling:** a batch whose stated point is a behaviour change in a tool it
   does not touch has to be read out of the platform's release notes and ADRs,
   and this is the step to do it in.

   **And the corpus has to be re-read for claims on an action's REPLY rather
   than on the acted resource.** v0.6.12 was checked for exactly that, and the
   corpus has one: `remediate_consumer_lag_success` grades
   `restart_consumer_group.kill_key_cleared equals true`, with a comment
   explaining that no lag assertion belongs there because the cached metric
   trails recovery by ~30 s. That claim is still TRUE of its own world, and it
   is worth knowing precisely why: the scenario's `chaos_setup` calls
   `kill_consumer` with `consumer_group` and `ttl_seconds` and no `sticky`, so
   it gets the default non-sticky kill and a cleared flag really does mean a
   restarted group. What changed is the KIND of guarantee behind it. Before
   this pin "a cleared kill flag means the group comes back" was a property of
   `restart_consumer_group`; after it, it is a property of that scenario's hook
   arguments, and any scenario that ever passes `sticky: true` must grade
   recovery on `get_consumer_lag` or on group membership instead. Leave the
   claim alone and know which of the two it now rests on.
   `remediate_verify_fails` is the scenario a sticky kill is FOR; flipping it
   to live is its own decision with its own first-paid-run review and is
   deliberately not part of a re-pin.

8. A pin can add a tool **the agent's own principal may call and the planner must
   never see**, and v0.6.13 is the first one. Until then the surface split in two
   and the split was scope-shaped: a tool the agent could call was a tool the
   agent might be asked to choose, and the tools it could not call were the chaos
   hooks, filtered out of `TOOL_REGISTRY` by their `[chaos:` description prefix.
   `report_agent_run` and `report_agent_briefing` are neither. They are the
   commander's own telemetry, `agent_runs:write` is on the agent account, and the
   loop's checkpoint seam calls them — no model chooses them.

   So there are now TWO prefixes, `registry.EXCLUDED_DESCRIPTION_PREFIXES ==
   ("[chaos:", "[commander:")`, and one predicate (`mirrored_in_registry`) that
   `tests/unit/test_registry.py` reads instead of keeping its own copy. Three
   things follow for a re-pin that adds a `[commander:` tool.

   **Nothing is registered and that is the point.** No typed input or output
   model, no tier entry, no policy line. The reporter calls
   `MCPClientProtocol.call_tool` by name with a dict, the way
   `evals/chaos_hooks.py` fires a hook, and a registry entry would put the tool on
   the planner's page — which is the one outcome the design forbids.

   **Check the read direction explicitly, in the snapshot, not in the release
   notes.** The property ADR 0035 rests on is that the agent cannot read back what
   it reported: there is no read tool for `agent_runs` at all, and the platform
   withholds the `agent.run_reported` audit rows from this principal's
   `list_audit_events` and `get_trace`. `TestTheExclusionFilterIsStructural`
   pins both halves, and the day a convenience read tool appears the pin fails
   rather than the property quietly ending.

   **A withheld audit stream means the agent sees FEWER rows than before, so
   `list_audit_events` totals move DOWN.** That direction is easy to misread as a
   lost row. Ledger it in the rebless entry, and expect canned
   `list_audit_events` fixtures to need re-recording on the first pin where
   anything has actually reported — not on this one, where nothing has yet.

   The scope is the other half, and it is not automatic: `scripts/
   bootstrap_agent_token.py` mirrors the platform's `seed_incident_commander.py`,
   so the new scope goes in `SERVICE_ACCOUNT_SCOPES` **and every existing token
   has to be re-minted**. `make bootstrap-token` PATCHes the live account and
   prints three fresh tokens; paste all three. A stale token does not error
   loudly — reporting is fail-open, so the run is unharmed and the console simply
   stays empty, which is the failure that looks like a frontend bug.

9. A pin can add an output field to a tool **whose canned planner script argues
   from the old reading's blind spot**, and v0.6.14 is the first one where that
   makes the re-record a behaviour change. `get_postgres_health` gained a `pools`
   group (platform ADR 0033): the flat `pool_*` fields describe only the process
   that answered, and the group is every process's own reading, so a pool held in
   the api/worker process is readable from the agent's surface for the first
   time. Step 4's v0.6.11 rule says write a new field into every fixture that
   cans the tool. Here that is right and still not the re-pin's job, for a reason
   worth recognising on sight.

   **Read the canned SCRIPT, not just the canned response.** `postgres_slow`'s
   script concludes, in its reasoning, its findings and its recommendation, that
   "this reading cannot say whether any other process's pool is near its limit"
   and that those pools "have to be read from those processes, because this probe
   cannot see them". v0.6.14 answers exactly that question. Writing the group
   into the fixture without rewriting those three sentences ships a world whose
   evidence contradicts its own analysis; rewriting them edits a canned LLM
   script, which is a graded trajectory and therefore a behaviour change with its
   own `make eval-reg` reading. That does not belong in a digest bump. WO-R3-261
   recorded the same call for v0.6.11's script.

   **So the honest resolution is a ledger row that says it is work, not a
   justification that says it is not.** The ten rows (five fixtures × `pools` +
   `pool_gauges_unknown_reason`, kind `live_only_field`) go into `_JUSTIFIED`
   with context `fixture-defect` — the one context the file's own legend calls
   work — and `tests/unit/test_fixture_drift.py` now pins that burn-down list BY
   NAME rather than asserting it is empty. Pinning the names is what keeps the
   deferral from becoming an absolution: the ten leave when the re-record lands,
   and anything else appearing there still fails. Choosing `post-fault` or
   `canned-only` instead would have been the easy green and would have claimed a
   mechanism that is not there. Also record, for whoever writes the fixture, that
   `pools[].written_at` and `pools[].reported_age_s` can never be canned (a clock
   and an age, exactly like `get_circuit_breakers`' `breakers.recorded_at` and
   `breakers.reported_age_s`) and belong in `_VOLATILE`, while the counters stay
   guarded.

   **Do not reach for `make fixture-drift-bless` to add rows, even now that it
   works again** (#318 fixed the context-scoped half). The committed ledger's 173
   rows are GENERIC four-tuples, from before the format carried an element index,
   and the bless writes what the walk observed — index-bearing five-tuples. One
   bless therefore rewrites every row and grows the file to 412 entries, and it
   drops whatever this run disproved, which on any developer volume includes the
   local-volume rows CI still needs. Append the new rows in the committed generic
   form, recompute `_counts`, and leave every prior row byte-identical; the diff
   should be the new rows plus two count lines and nothing else.

   **The corpus consequence to record rather than fix:** the `api_latency`
   family's fifth world was dropped because "a held connection pool reads as the
   healthy control field for field" (ADR 0066). After v0.6.14 it does not — a
   held pool in the api/worker process now shows up in `pools`. That world is
   buildable again, which is a scenario decision and not a pin's.

10. A pin can change the REQUEST rather than the tool surface, and v0.6.17 is the
    first one: `make snapshot` produced no diff at all, and the pin still matters.
    `tools/call` now reads an optional `_lab_probe` reason string **beside**
    `arguments` in `params` (never inside it, so it reaches no input model and no
    prompt), honoured only when the same request carries
    `X-Lab-Principal: Bearer <chaos-or-smoke token>` on a `CHAOS_ENABLED` stack;
    then that call's audit row is `lab.probe` instead of `agent.tool_invoked`
    (platform ADR 0038, the amendment to platform ADR 0012). The commander sends
    both halves from `evals/guards.py` (the evaluator's token) and
    `evals/world_audit.py` (the smoke token) — WO-R3-335. Four things follow for a
    re-pin.

    **The label needs the v0.6.17 pin, and an older platform says nothing.**
    `_lab_probe` is an unknown key on the envelope to every earlier build, so it is
    ignored in silence: no error, no warning, and every one of those rows records
    as the agent's own work. So a commander that sends the label while pinned to
    v0.6.16 or earlier has exactly the bug the field exists to fix, and nothing
    fails to tell you. The tell is a query, not a log line: after `make world-audit`
    on a v0.6.17 stack, `GET /admin/audit-logs?action_prefix=lab.probe` (operator
    login, `scripts/bootstrap_agent_token.py`'s dev pair) returns the audit's reads,
    and `action_prefix=agent.` returns none of them. On an older pin the first query
    is empty and the second holds them all. Run both after any pin that moves this
    seam.

    **A refused label arrives as `-32602`, which is also what an argument refusal
    carries** — and `-32602` is precisely what the principal guards read as "the
    scope check passed". Reading a refusal as a verdict would therefore invert the
    write guard's answer. That is why the client raises `LabProbeRefused` as its own
    type, the guards re-raise it before they interpret any code, and nothing retries
    it or re-sends the call unlabelled: a silent unlabelled retry is how the
    mislabelled rows come back. `data.reason_code` says which half of the rule the
    request failed (`not_available`, `credential_missing`, `credential_invalid`,
    `credential_not_authorised`, `reason_invalid`) — `not_available` means the stack
    was booted without the lab, which is a stack decision and not a request bug.

    **`lag_samples_cleared: 0` in the reset's JSON is now permanent, and is not a
    broken reset.** v0.6.17's `make eval-reset` keeps the 15-minute lag sample ring
    (history is history, and the demo chart reads it) while still clearing the VALUE
    key, so backpressure reads fresh-or-absent exactly as before. The world audit's
    `lag: 0` check reads the value, not the ring, so it is unaffected. So is the
    fixture-drift stack context: `evals/fixture_probe.py` decides `warm` or `cold`
    from whether `get_consumer_lag` answers `lag_known: true` with a `measured_at`
    — the value key, which the reset still clears — so the minute-after-a-reset
    `cold` reading of step 4's notes still happens.

    **The rows are withheld from the agent, so its own audit totals move DOWN when
    the lab is busy.** `lab.probe` joins `chaos.`/`lab.` in what the agent's
    `list_audit_events` and `get_trace` cannot see — same direction as v0.6.13's
    `agent.run_reported` note in step 8, and the same consequence: a canned
    `list_audit_events` fixture recorded before this pin counts rows the agent can
    no longer see. None needed re-recording at v0.6.17 (`make fixture-drift` read
    `0 new / 0 stale`), because no canned fixture's world has a lab probe in it.

    v0.6.18 (WO-R3-339) moved the surface again, by ONE entry, and it is the
    cleanest reading of step 5 there has been: `get_consumer_lag`'s `description`
    3,444 → 3,728 characters and the planner's tool block 29,168 → 29,452, **+284
    both**, so every character of the block's growth is that one description and the
    three `outputSchema` field descriptions that moved with it (`source`,
    `age_seconds`, `recent_samples`) are provably not in the block — the block
    renders a tool's description and its input arguments, never its output schema.
    Quote both numbers in the PR body: subtracting them is the check.

11. **A pin can turn a platform CONSTANT into a setting, and then the compose file
    decides what the agent's world is like.** v0.6.18 is the first
    (platform ADR 0039, owner decisions O-35 and O-36):
    `METRICS_LOOP_INTERVAL_SECONDS` defaults to 60 and `demo/compose.yml` sets it to
    **5** on the `platform` AND `api` services. Four things follow, and the first
    one is the one that costs a session if it is missed.

    **A description that hard-codes a number the setting can move is now FALSE, and
    that is why this pin has a contract delta at all.** The old text promised a
    measurement `every ~60s`, a `90s TTL` and a reading `up to a minute stale`. At
    5 s all three are wrong, and the worst of them is "two calls a few seconds apart
    return the SAME number" — the exact opposite of the truth on this stack, told
    confidently to the agent. So the platform re-described it to point at
    `age_seconds` and the gaps between `recent_samples` instead, and the commander
    reblessed one snapshot entry. **The rule to carry forward: when a pin makes a
    number configurable, grep the repo for the OLD number before anything else.** In
    this one the sweep found `90s TTL` / `~60s` in the canned
    `cascading_redis_starves_backpressure` comments, in `scripts/demo_live.py`'s
    waits, and in this runbook. None was a fixture VALUE — `make fixture-drift` read
    `0 new` — and all of them were text a reader would have trusted.

    **The description must NOT interpolate the setting**, which is why the new text
    names a reading rather than a number: a `tools/list` that differed between the
    demo stack and CI would make the snapshot unpinnable, and the `contract` job
    would go red on a difference that is a deployment rather than a change.

    **Two platform numbers are derived from the interval now, and one of them moved
    on the DEFAULT.** The lag value key's TTL is three passes — 180 s at the 60 s
    default (it was a flat 90 s) and 15 s here — and the 15-minute sample ring is
    pruned by TIME, so it holds ~180 points at 5 s instead of 15, with
    `LAG_SAMPLES_MAX_ENTRIES = 240` as an absolute guard that binds only below a
    3.75 s interval. The AGENT's `recent_samples` is capped at the newest 15, the
    count and order it always returned, so its context cost does not move with the
    tick; the operator endpoint serves the whole window, which is what makes the
    console's chart step by the real clock. Nothing in the commander reads a sample
    COUNT — checked by grep — so no fixture or claim moved with it.

    **The platform pages itself now, and the demo takes that page** (O-36,
    [ADR 0076](ADR/0076-the-demo-takes-the-platforms-page.md)). Two rules on the same
    tick, `alert_rules_enabled` default ON: `consumer_stalled` (latest MEASURED lag
    sample for a group ≥ 20) and `dlq_depth_warning` (a tenant's dead-letter total ≥
    5, the seeded baseline of four plus one). An episode raises ONCE and resolves
    when a reading crosses back, with one `alert.raised` / `alert.resolved` audit row
    per transition — **not withheld from the agent**, because an alert is what the
    agent was paged with. Three consequences for a re-pin:

    * `make eval-reset` gains `rule_alerts_resolved` in its JSON, and the world
      audit's `active alerts: 3` is only true afterwards. **Read that counter rather
      than the row** when you want to know who closed an episode: a take that ends
      with a reset can close its own page, so an `alert.resolved` inside a take
      proves the rule only when the reset reports `rule_alerts_resolved: 0`.
      Measured on the 2026-09-21 rehearsals, one of each — `consumer_outage`'s rule
      resolved its own episode on a sample that read 8 against a threshold of 20 and
      the reset then had nothing to close, while `dlq_backlog` finished first and the
      reset closed it.
    * **the tell that the rules are on** is a query, like step 10's:
      `GET /api/v1/audit/logs?action_prefix=alert.` (operator login,
      `scripts/bootstrap_agent_token.py`'s dev pair) after a take should hold exactly
      one `alert.raised` for it. Zero rows means either the world never crossed a
      threshold or `alert_rules_enabled` is off, and those look identical from the
      commander side — check the depth or the lag before concluding anything.
    * `--alert-from-platform` matches on fingerprint **plus subject and never on
      `source`**: the platform's rows read `kafka:consumer_lag` and `dlq:threshold`
      where the corpus writes `platform.kafka` and `platform.dlq`. A pin that changes
      either spelling changes nothing here, and that is the point.

12. **A release can change nothing the agent can see, and the re-pin is still the
    whole job.** v0.6.19 is the first (plat #236, WO-R3-341): the CONSOLE image
    carries the fifth take's `/demo` fixes and the backend image's behaviour is
    identical to v0.6.18 — no tool, description, schema, scope or refusal code
    moves. Measured, not assumed: `make snapshot` against the live v0.6.19 stack
    rewrote the file and `git diff contracts/` came back **empty**, 40 tools, and
    `make test-contract` passed. So there is nothing to rebless, no fixture moves,
    and no eval claim moves.

    **Pin all four services anyway, on the same tag.** The backend containers take
    a tag whose image they were already running, which looks like busywork and is
    not: "which platform was this run against" has to have one answer, and a stack
    running `console:v0.6.19` beside `api:v0.6.18` has two. The provenance the run
    archive records is the digest out of `demo/compose.yml`
    (`platform_image_digest`), so a half-pinned stack also mislabels every archive
    it produces.

    **The one-line rebless note still gets written**, in the hub's
    `docs/wave4-specs/rebless-notes.md`, and it says the diff was empty. A version
    with no ledger row reads later as a version nobody checked.

13. **A setting the DEMO sets is part of the pin, and it belongs on exactly one
    service.** v0.6.20 (plat #236, WO-R3-343) turns the `POST /jobs` rate limit
    into `JOB_CREATE_RATE_LIMIT` / `JOB_CREATE_RATE_WINDOW_SECONDS`, defaults
    unchanged, and `tools/list` is byte-identical (checked on both sides by the
    platform PR; `make snapshot` against the live stack came back with **no diff**
    and `make test-contract` passed **1**). The commander's half is the digests plus
    one line: `JOB_CREATE_RATE_LIMIT: "240"` on the `api` service.

    **Which service, and how to be sure rather than to assume.** `api` runs
    `app.main:app`, the REST app, and that is where `POST /jobs` lives; `platform`
    overrides `command:` to run `app.mcp.standalone:app`, which mounts no REST
    router, and no MCP tool creates a job. Read the route's module and the two
    `command:` lines before putting a REST setting on a service — this is the same
    class of mistake as `SEED_EVAL_FIXTURES`, which sat for weeks on the one
    service that could not act on it.

    **Check the setting on the stack, not in the file.** One command proves it:
    `make traffic RATE=0.2 MAX_PER_WINDOW=240 COUNT=45` creates 45 jobs inside a
    single 60-second window and reports `45 created` with nothing rate-limited.
    Against the platform's default of 30 the last 15 would be 429s, so the
    measurement distinguishes "the compose file says 240" from "the process is
    running 240".

    **A raised ceiling is a demo change, so re-rehearse and re-measure.** The
    number this pin buys is a fault-phase producer at 0.75 s instead of 2.0 s, and
    the thing to read off the platform's own 15-minute sample ring
    (`GET /admin/consumer-lag`, operator session) is that every sample between the
    fault and the restart is strictly higher than the one before it. A repeated
    sample means the producer was refused, which is the fifth take's plateau
    returning.

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
