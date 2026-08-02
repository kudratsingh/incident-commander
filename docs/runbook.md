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

## Live eval protocol (post-hardening)

Written after the Phase-6 seven-run live eval that produced the five-bucket noise-source taxonomy (see [`docs/lessons/live-eval-noise-sources.md`](lessons/live-eval-noise-sources.md)). Run one scenario at a time with an explicit reset between them.

```bash
make demo && make bootstrap-token
export VERIFY_PROBE_ATTEMPTS=4 VERIFY_PROBE_DELAY_SECONDS=20

# Scenarios that declare chaos_setup in the YAML seed themselves — no
# separate `make chaos-*` call needed. remediate_consumer_lag_success
# uses inject_latency; remediate_dlq_backlog_success uses poison_message.
uv run python -m evals.runner --live --only remediate_consumer_lag_success

# Reset between scenarios. Today eval-reset only clears the consumer-
# group kill+latency flags — a full seed reset (idempotency records,
# DLQ pool, chaos:* keys) is blocked on platform-owned
# scripts/reset_eval_state.py landing.
make eval-reset

# Then one fault → one scenario → reset, for each remaining scenario.
# Do NOT batch until the platform reset script is enforceable.
```

Environment variable knobs for the live path (see [ADR 0006](ADR/0006-verification-is-a-polling-window.md)):

| Var | Default | Live-recommended | Meaning |
|---|---|---|---|
| `VERIFY_PROBE_ATTEMPTS` | 1 | 4 | Bounded polling window on VERIFYING. Default keeps canned runs single-probe. |
| `VERIFY_PROBE_DELAY_SECONDS` | 15 | 20 | Delay between polling attempts. Size to the slowest verify probe's freshness. |

The `remediate_stale_cache_success` scenario is currently **not winnable live** — it needs a chaos hook to seed `cache:jobs:worker-dispatcher:hot_set` before the agent runs, and no such hook exists. Skip it in the live pass until the platform ships the hook or the scenario is flagged offline-only.

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
