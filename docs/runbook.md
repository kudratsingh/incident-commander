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

# Probe knobs for the live path. eval-live sets EVAL_TRACE_DIR inline,
# but exporting it here means every direct `evals.runner` invocation
# also gets traced.
export EVAL_TRACE_DIR=evals/traces \
       VERIFY_PROBE_ATTEMPTS=6 \
       VERIFY_PROBE_DELAY_SECONDS=20

# 1) Smoke pass FIRST — read-only scenarios catch any wire-shape
#    surprise from the current pin before you spend on a Tier-1
#    remediation attempt. ~$1 of tokens. Runs under the read-scoped
#    PLATFORM_SMOKE_TOKEN, so "read-only" is enforced by the platform
#    (a Tier-1 attempt 403s and grades as an escalation), not by the
#    scenario list. Override the list with SMOKE_ONLY= if needed.
make eval-smoke

# 2) Remediation scenarios, one at a time, with reset between.
#    Each scenario declares its own chaos_setup in the YAML (PR #54).
make eval-live ONLY=remediate_consumer_lag_success && make eval-reset
make eval-live ONLY=remediate_dlq_backlog_success  && make eval-reset
make eval-live ONLY=remediate_stale_cache_success  && make eval-reset

# After any consumer restart — the agent's restart_consumer_group or a
# manual restore — confirm liveness with `rpk group describe
# worker-dispatcher` (see "Restore state" above), never the lag metric.

# Do NOT drop the reset between scenarios. The one-fault-one-scenario
# protocol exists because shared platform state between runs was the
# single largest source of noise in the seven-run audit — see the
# lessons doc's third bucket, "shared mutable environment".
```

Every `make eval-live` invocation writes JSONL traces to `evals/traces/` and renders per-scenario human reports to `evals/reports/human/*.md` (via the `format_traces.py` step chained into the target).

A filtered run (`ONLY=...`) still overwrites `evals/reports/latest.json`, but the report now self-describes via `only_patterns` (ADR 0013) and **can no longer feed the gate or the baseline**: `make eval-reg` exits 2 on a filtered `latest.json`, and `make eval-reg ONLY=x` / `make baseline ONLY=x` refuse at Makefile parse time before anything runs (A-03 — `study/runs.jsonl` records a full-suite `latest.json` lost to a later filtered run). The archive under `evals/runs/<invocation_id>/` remains the durable record for filtered runs; the flat `latest.json` is only a pointer to the most recent one.

`make eval-reset` shells into the platform's `app` container via `docker compose -f $PLATFORM_COMPOSE exec` — defaults to `../incident-platform/docker-compose.yml`. If the platform repo isn't a sibling checkout, set `PLATFORM_COMPOSE` either per-invocation or once in `.env` (the Makefile `-include .env`s it, so a non-sibling layout is a one-time setup rather than a flag you have to remember on every call). Getting this wrong fails loudly on an exit-2 guard before anything runs — it can't half-reset. Pass `PURGE_IDEMPOTENCY=1` to also `DELETE` idempotency_records (24h TTL from platform ADR 0010 handles the common case; opt-in purge for guaranteed-fresh cache).

Environment variable knobs for the live path (see [ADR 0006](ADR/0006-verification-is-a-polling-window.md)):

| Var | Default | Live-recommended | Meaning |
|---|---|---|---|
| `VERIFY_PROBE_ATTEMPTS` | 1 | 6 | Bounded polling window on VERIFYING. Default keeps canned runs single-probe. 6 proved out in the 2026-08-03 campaign; size scenario caps for it. |
| `VERIFY_PROBE_DELAY_SECONDS` | 15 | 20 | Delay between polling attempts. Size to the slowest verify probe's freshness. |
| `INVESTIGATE_REPROBE_ATTEMPTS` | 0 | 1 | Investigation-side freshness re-probe ([ADR 0009](ADR/0009-investigation-freshness-reprobe.md)): when a cached read kills a fixable hypothesis at ≥0.7, re-read it fresh before accepting. Default 0 keeps canned runs byte-identical. |
| `INVESTIGATE_REPROBE_DELAY_SECONDS` | 20 | 20+ | Delay before the freshness re-read. Size to the cached tool's declared staleness window (lag cache: 60s). |

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
status (only `-x` concerns ignored ones), so an uncommitted archive is
still one `git clean -fd` away from erasure. The commit is the durability.

Offline `make eval` / `make eval-reg` / `make baseline` invocations also
leave untracked `evals/runs/<id>/` directories behind. Leaving them
untracked is acceptable; deleting them is not (invariant 9).

This step first becomes exercisable at the post-v0.5.0 eval — under the
ADR 0011 freeze nothing runs, so no archive is committed until then.

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
| 2 | no scenario matched `--only` (regression gate: missing report, or a filtered `--only` `latest.json` — refused as gate input) |
| 3 | preflight/env failure: `--smoke` without `--live`, degraded `--live` env, invalid or missing settings, missing smoke token, LLM auth preflight failure |
| 4 | principal guard: the smoke token holds more than read scope |
| 5 | post-stage audit failed or was unreadable |

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
- Traffic: **modest suffices** — the standard 1-job/2s loop against a
  dead consumer (service rate 0) builds real backlog. No soak
  engineering, no heavy bursts (the rate limiter would eat them anyway). As of platform v0.4.7 the previously-blocked `remediate_stale_cache_success` uses the new `create_stale_cache` chaos hook and is winnable live.

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

**Run contract tests ONLY against the pinned demo stack until further
notice.** Platform master currently serves one more tool than the v0.4.9
tag (a dormant, flag-off addition), and no new tag lands until the
clean-baseline rerun completes. A contract check against the dev stack
will therefore fail **by design** — that is master drift, not drift in
the pinned artifact, and it must not trigger a snapshot rebless from the
dev stack. Bless snapshots from the pinned stack only.

## Bumping the pinned platform image

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
