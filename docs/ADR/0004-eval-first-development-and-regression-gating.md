# ADR 0004: Build the eval harness before the behavior, gate PRs against a committed baseline

* Status: accepted
* Date: 2026-07-30
* Decider: Kudrat Singh

## Context and problem statement

Agent behavior drifts silently. A prompt tweak that "seems better" can wreck a scenario the reviewer wasn't looking at. A tool schema change can cascade into a state-machine bug that only fires on one alert shape. Without a fast, deterministic feedback loop, the reviewer has to hold every scenario in their head to spot the regression — which nobody can.

CLAUDE.md invariant 8 says: "Evals gate behavior changes. Any PR that touches prompts, tool definitions, policy tiers, memory retrieval, or the pinned model must pass the regression eval suite before merge." This ADR captures how we make that enforceable.

## Decision drivers

* Behavior changes without eval verification are the cheapest way to break the product.
* An eval suite that runs only on demand won't get run. CI must be the enforcement point.
* Offline eval must be cheap enough (deterministic, no LLM calls) to run on every PR without spending tokens.
* Live eval must exist, be periodic, and produce artifacts the reviewer can inspect — offline-only proves canned data, not real platform interaction.
* The bar can't be "run some evals and eyeball the diff." It must be automatic: score, compare, block.

## Considered options

1. Manual test protocol per PR — reviewer runs eval scripts, eyeballs results.
2. Eval suite in CI, no baseline — surfaces regressions in aggregate scores but doesn't compare against history.
3. Eval suite + baseline + regression gate — every metric drop against the committed baseline blocks the PR until explained or fixed.

**Chosen: option 3.** Every PR that touches `src/agent/prompts/`, `src/tools/`, `evals/graders/`, or model configuration runs the offline eval suite and diffs against `evals/reports/baseline.json`. A metric drop beyond threshold fails the check.

## Decision outcome

### Baseline as truth

`evals/reports/baseline.json` is the source of truth for "how the agent scored on the day this was blessed." Committing a baseline change is a deliberate act — the reviewer sees the metric delta in the PR diff.

- `make baseline` regenerates `latest.json` and copies it to `baseline.json`. Only run when the metric change is intentional.
- `make eval-reg` runs the full offline suite and diffs against baseline. Fails on regression.
- CI runs `make eval-reg` on every PR that touches behavior surfaces.

### What's in the baseline

Per-scenario metrics:
- Terminal state (must match `expected_terminal_state`)
- Evidence corpus hits (`expected_evidence_contains`)
- Tool-call budget (`max_tool_calls`)
- Action tool called (`expected_action_tool` — Phase 6 addition)
- Judge scores on `groundedness` + `actionability` (informational, not gate)

Aggregate:
- Total scenarios / passed / failed
- Judge mean overall
- Judge useful count

### Live eval as a separate signal

`make eval-live` runs the same suite against the real platform + Anthropic. It is:
- Not gated by CI (spends money, non-deterministic)
- Run manually before major PR merges + nightly (planned Phase 8)
- Traced in full JSONL — every LLM call, every MCP call, request + response

Live-eval discoveries feed back into the scenario suite. When a scenario passes offline but fails live because canned data no longer matches platform reality, the offline canned data gets regenerated — not the scenario expectations.

### The DLQ finding (2026-07-30) — why eval-first pays off

First real live-eval remediation run surfaced a design gap invisible to offline eval:
- Scenario expected `replay_dlq_messages` on any DLQ-high alert
- Real DLQ contents were persistent bugs (bad data + downstream outage), not poison messages
- Agent correctly refused to force a wrong fix; escalated with a well-graded briefing
- Judge scored 0.90 despite the scenario being "failed"

Offline eval would never have caught this because the canned DLQ data was tuned to match the expected outcome. Live eval showed the design assumption was wrong. See [`docs/eval-methodology.md#case-study-dlq-categorization-discovery`](../eval-methodology.md#case-study-dlq-categorization-discovery).

The response is scenario-shape work (split into categorized variants) + platform-side work (richer DLQ triage). But the discovery itself is the win. Eval is the tool that made the wrong assumption visible.

### Why the alternatives lose

**Option 1 (manual)** is what everyone does before this ADR. It fails because reviewer attention is finite and eval scenarios grow faster than any one person can track. The one time somebody skips the manual step is the day a regression ships.

**Option 2 (CI, no baseline)** catches "did all 33 scenarios pass" but misses "did the aggregate judge score drop 15 points." A subtle prompt change might keep all scenarios passing but silently downgrade briefing quality across the board. Baseline diffing catches that; presence-only CI doesn't.

### Consequences

Positive:
* Behavior changes come with visible metric diffs. The reviewer can't miss a regression.
* Baseline is a live document of "what we know works." New contributors read baseline scores to understand the current bar.
* Live-eval findings systematically feed back into scenarios — the suite grows more realistic over time.

Negative:
* Baseline maintenance is real work. Every legitimate behavior change bumps baseline, which needs review to distinguish "improvement" from "moved the goalposts to hide a regression." Mitigation: PR reviewers required to look at baseline diffs when they appear.
* Offline eval can't catch live-only regressions (platform contract drift, LLM behavior shifts against real evidence). Mitigation: `make eval-live` before every major merge + nightly full run (Phase 8).
* Some regressions are borderline (a scenario flips from pass to fail because the LLM made a defensible-but-different call). Mitigation: judge scores as tiebreaker + reviewer discretion codified as "if judge overall > baseline overall, the pass/fail flip is likely a scenario-design issue, not an agent regression."

Revisit trigger: baseline maintenance becomes noise-heavy (reviewers rubber-stamp baseline updates without looking). At that point we either shrink the sensitive-surface list (fewer PRs trigger regression checks) or partition scenarios into "must-not-regress" vs "informational."

## More information

* Related ADRs: [ADR 0001](0001-external-client-architecture.md), [ADR 0002](0002-hand-rolled-state-machine.md), [ADR 0003](0003-platform-enforced-tier-policy.md)
* Implementing PRs: original harness (Phase 1), regression gating (CI workflow `evals.yml`), Phase 6 ACTION dimension (#36), scenario filter (#38)
* Related doc: [docs/eval-methodology.md](../eval-methodology.md)
* CLAUDE.md invariant 8 (evals gate behavior changes), invariant 6 (audit log is ground truth for safety metrics — separate but complementary)
