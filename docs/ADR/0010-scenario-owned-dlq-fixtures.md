# ADR 0010: Scenario-owned DLQ fixtures — the inter-scenario baseline is an empty DLQ

* Status: proposed (cross-repo — platform review requested before any implementation)
* Date: 2026-08-03
* Decider: Kudrat Singh

## Context and problem statement

The platform's eval seed maintains a global 4-row DLQ fixture pool (2× `csv_upload`/`human_required`, 2× `bulk_api_sync`/`wait_and_replay`), restored by `reset_eval_state.py` between scenarios. Every live scenario therefore runs against a DLQ that always has content, whatever the scenario is about.

The 2026-08-03 campaign showed what that costs. Three separate runs pivoted onto the fixture rows when their actual subject was invisible or absent:

1. `consumer_lag_high` (read-only smoke): live lag was healthy, so the agent investigated until it found the fixture DLQ, built a `poison_message` story, and fired a real Tier-1 `replay_dlq_by_category` — RESOLVED where the scenario expected ESCALATED.
2. `dlq_human_required_escalates`: the scenario assumes a human_required-only DLQ; the mixed fixture hints (2 human_required + 2 wait_and_replay) made escalation the reasonable read of an ambiguous queue, failing an expectation written for a clean one.
3. `remediate_consumer_lag_success` attempt 1: after a stale lag read killed the correct hypothesis, the fixture DLQ supplied the confident wrong alternative (0.82) that became a wrong Tier-1 replay.

The fixture pool functions as an attractive nuisance: always present, always plausible, never the scenario's subject. The agent behaved reasonably each time — an SRE who finds a populated DLQ while chasing a ghost will look at it. The lab, not the agent, is supplying the red herring.

## Decision drivers

* One-fault-one-scenario is the standing protocol; a standing global fixture violates it *by design* — it is a second fault present in every scenario.
* Scenario expectations must be writable against a known DLQ state. "Whatever the pool holds today, minus what previous runs replayed or marked" is not a known state (fixture rows carried `updated_at` stamps from three prior days of runs).
* DLQ-subject scenarios (`dlq_*`, `remediate_dlq_backlog`) genuinely need seeded rows — but rows whose types, hints, and counts the scenario itself declares, the way `chaos_setup` already works for latency and cache faults (PR #54 baked seeding into scenario YAML for exactly this reason).
* Cross-repo boundary: the commander's scenario schema and runner own *declaring* fixtures; the platform owns *creating* them (seed hooks run inside the platform's trust boundary, per ADR 0001).

## Considered options

1. Status quo: global fixture pool + reset sweep between scenarios.
2. Scenario-owned DLQ fixtures: baseline between scenarios is an **empty DLQ**; any scenario that needs DLQ content declares it in its own setup hooks (chosen, pending platform review).
3. Per-scenario tenant isolation: each scenario runs in a fresh tenant with its own empty everything.

## Decision outcome

Option 2. Concretely, split across the two repos:

**Platform side (review requested):**
* `reset_eval_state.py` gains an empty-DLQ baseline mode (or makes it the default): the sweep removes *all* DLQ rows rather than restoring the 4-row pool.
* A `seed_dlq_messages` chaos/seed hook (name per platform convention) that creates N DLQ rows with declared `job_type`, `remediation_hint`, and error text, TTL-scoped or reset-swept like other chaos artifacts. The existing fixture pool becomes the hook's canned default payload rather than a standing installation.

**Commander side (this repo, after platform lands):**
* `Scenario.chaos_setup` generalizes to a list (`setup_hooks`) so a DLQ scenario can declare both its fault and its fixtures; DLQ-subject scenarios declare their rows explicitly.
* Scenario expectations for `dlq_*` scenarios rewritten against their declared fixtures (counts and hints become knowable again).
* The runbook's baseline check ("4 fixture DLQ rows") becomes "DLQ empty".

### Why the alternatives lose

**Status quo.** The reset sweep restores *consistency* but not *relevance*: every non-DLQ scenario still runs with someone else's fixtures in frame. The campaign produced three exhibits in one night; the cost is recurring and it lands on the scenarios' credibility (wrong-reason FAILs and wrong-reason PASSes both).

**Per-scenario tenant isolation.** The clean-room answer, and the platform's multi-tenancy could support it — but it multiplies seed time per scenario, complicates the SA token story (scope per tenant per scenario), and solves a problem the empty-DLQ baseline already solves for the only shared surface that has bitten. Revisit if other shared surfaces (traces, deploy history) start contaminating scenarios the same way.

### Consequences

Positive:

* Non-DLQ scenarios lose their standing red herring; investigation quality becomes attributable to the agent instead of the furniture.
* DLQ scenario expectations become exact (declared rows in, graded outcomes against those rows) instead of calibrated to a drifting pool.
* The seeding model becomes uniform: everything a scenario needs, the scenario declares — no ambient state.

Negative:

* Cross-repo sequencing: platform hook + reset change land first, then commander scenarios migrate. Until then the status quo stands (this ADR stays `proposed`).
* Scenarios that *want* a noisy environment (distractor-resistance tests — arguably worth having after the campaign!) must now declare their distractors explicitly. That is a feature, but it is also more YAML.
* `chaos_setup` schema change (single hook → list) touches every scenario file mechanically.

Revisit trigger: if a distractor-resistance scenario family lands, it may justify a shared "noise pack" hook rather than per-scenario declarations; reopen then.

## More information

Campaign evidence: `evals/traces/consumer_lag_high.jsonl`, `evals/traces/dlq_human_required_escalates.jsonl`, `evals/traces/remediate_consumer_lag_success.jsonl` (2026-08-03). Related: PR #54 (chaos_setup in scenario YAML — same principle, fault side), platform `scripts/reset_eval_state.py` + reset-sweep #90, `docs/lessons/live-eval-noise-sources.md` (shared-mutable-environment bucket).
