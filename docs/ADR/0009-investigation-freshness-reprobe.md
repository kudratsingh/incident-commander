# ADR 0009: Cached-read contradictions get a fresh re-probe before the hypothesis dies

* Status: accepted
* Date: 2026-08-03
* Decider: Kudrat Singh

## Context and problem statement

[ADR 0006](0006-verification-is-a-polling-window.md) fixed the verify side of eventual consistency: VERIFYING re-reads its probe over a bounded window instead of judging one instant read. The 2026-08-03 live campaign surfaced the same failure one state earlier, on the investigation side, where no such window exists.

The trace: `remediate_consumer_lag_success` injected consumer latency at T+0. At T+10s the investigation planner's first probe, `get_consumer_lag`, returned `lag: 0` — the platform serves that number from a 60-second cache, so the reading predated the fault. The planner had opened with `consumer_saturation` at 0.75 — correct, actionable, above the remediate threshold — and the stale zero killed it on the next iteration. The agent then wandered to the fixture DLQ, built a `poison_message` story at 0.82, and fired a DLQ replay against an incident whose actual fault was consumer latency. One stale sensor reading converted a correct diagnosis into a confident wrong remediation.

The investigation loop trusted a single instant read of a declared-cached metric at the exact moment that reading was most likely to be stale: seconds after the fault began, which live is precisely when alerts fire.

## Decision drivers

* A cached reading taken inside its staleness window can predate the fault entirely. Freshness is a per-tool property the platform declares; the agent should exploit that declaration, not ignore it.
* The damage mode is specific: an *actionable* hypothesis (category in `FIX_MAP`, confidence ≥ the remediate threshold) dying on stale evidence changes what the agent *does*, not just what it reports. Protecting the handoff decision is the point.
* Canned runs are the regression substrate. They are instant-consistent, and an extra probe would consume an extra scripted planner response — the canned suite must remain byte-identical by default.
* The mechanism must be structural (loop-level interception), not prose. A prompt line saying "distrust cached reads" is documentation, not enforcement (architecture principle 1).
* Cost must be bounded: at most one extra probe + one extra planner iteration per cached tool per run.

## Considered options

1. Prompt guidance — tell the planner cached tools may be stale and to re-probe when surprised.
2. Structural re-probe interceptor in the investigation loop, knob-gated, canned-default-off (chosen).
3. Platform-side cache bypass — a `fresh=true` parameter on cached read tools.

## Decision outcome

Option 2. `make_llm_investigate` gains `reprobe_attempts` (default 0) and `reprobe_delay_seconds`, mirroring ADR 0006's shape. After each planner iteration, the loop checks a tight trigger:

* the last executed probe's tool is in `policies.CACHED_READ_FRESHNESS_SECONDS` (today: `get_consumer_lag`, 60s), AND
* the prior top hypothesis was actionable — category in `FIX_MAP` and confidence ≥ the remediate threshold (0.7), AND
* the fresh planner output dropped that category below the threshold or dropped it entirely.

When all three hold and the per-tool re-probe allowance is unspent, the loop does **not** act on the planner's step. It records a `_freshness_reprobe` evidence entry naming the killed hypothesis and the delay, sleeps, re-executes the same probe, and lets the next planner iteration see both readings side by side. The planner — not the interceptor — decides what the pair of readings means. Budget is charged for the extra probe and the extra planner call; the trigger fires at most `reprobe_attempts` times per tool per run.

`Settings` exposes `INVESTIGATE_REPROBE_ATTEMPTS` (default 0, `le=3`) and `INVESTIGATE_REPROBE_DELAY_SECONDS` (default 20s). The eval runner wires them only when live MCP is available, exactly as it does for verify polling.

Scoped to `FIX_MAP` categories deliberately: unmapped categories escalate to a human regardless of confidence, so a stale read there changes the briefing's shading, not the agent's actions. Revisit if a briefing-quality case appears.

### Why the alternatives lose

**Prompt guidance.** The campaign's second attempt showed the planner *correctly* trusting a fresh `lag: 2` reading — the model's instinct to believe the probe is right and shouldn't be diluted with generalized suspicion. The failure is structural (reading age vs fault age), so the fix belongs in the loop, where "this tool is cached and it just killed an actionable hypothesis" is checkable without trusting the model to remember prose. Principle 3: structural fix first.

**Platform-side cache bypass.** Honest fix for the staleness itself, but it moves cost to the platform (a fresh Kafka admin read per call is why the cache exists), requires a platform PR and contract rev, and still leaves the agent trusting single reads of any *other* cached tool added later. May still land someday as a complement; it is not the agent-side posture.

### Consequences

Positive:

* A correct actionable diagnosis now survives one stale sensor reading — the exact class that produced the campaign's wrong-remediation trace.
* Both readings land in evidence with an explanatory `_freshness_reprobe` marker, so briefings and trajectories show *why* the loop paused, and graders can assert on it.
* Canned suite untouched (default 0 attempts); the knob follows the exact ADR 0006 pattern operators already know.

Negative:

* Live incidents where the contradiction is real (the fault genuinely cleared) pay one extra probe, one delay, and one extra planner iteration. Bounded by the per-tool allowance and sized by `reprobe_delay_seconds` to the declared staleness window.
* `CACHED_READ_FRESHNESS_SECONDS` is agent-side knowledge that duplicates a platform property. Mitigation: single source in `policies.py` with the declared window recorded beside each entry; if the platform ever publishes freshness in tool metadata, generate the map from the contract snapshot instead.

Revisit trigger: if the platform publishes per-tool freshness in `tools/list` metadata, or grows a `fresh=true` bypass, reopen this ADR and derive the map (or delete it) from the contract.

## More information

Campaign evidence: `evals/traces/remediate_consumer_lag_success.jsonl` (2026-08-03 attempt 1 — stale-kill; attempt 2 — fresh `lag: 2` correctly trusted). Related: [ADR 0006](0006-verification-is-a-polling-window.md) (verify-side twin), `docs/lessons/live-eval-noise-sources.md` (eventual-consistency bucket). Implemented in `src/incident_commander/agent/investigation.py` (`_cached_probe_contradiction`, `_note_freshness_reprobe`), `src/incident_commander/tools/policies.py` (`CACHED_READ_FRESHNESS_SECONDS`).
