# Live eval noise sources — a taxonomy

Written after the seven-run Phase-6 live eval that produced [FIX_PLAN v2](https://github.com/kudratsingh/incident-commander/pull/48). Reference for future "why is this run failing differently every time?" investigations. If a failed run doesn't fit one of the five buckets below, add a bucket rather than debugging blind.

## The three-noise-source framing (headline)

Across seven live runs the same shape kept repeating: a resolved-looking run in one scenario, a `not_verified` in another, a `CRASH` in a third — and every failure looked like a novel bug. It wasn't. Three independent failure modes were stacking on top of a state-mutation cascade underneath:

1. **Transport flakiness** in the two external clients (MCP, LLM). One 429 mid-run reads as a novel scenario bug when it's the same rate-limit dance.
2. **Eventual consistency** between an action and the probe used to verify it. The fix was correct; the probe was reading a 60s-cached value taken ~1s after the action.
3. **Shared mutable environment** left by previous runs. Dead consumers from prior chaos, drained fixture pools, immortal idempotency records. Pass rate decays across runs even when the code doesn't change.

Every failure got labeled "the new bug" until we sorted the runs by bucket and the distribution named the layer. **Bucket before you debug.**

## The five-bucket taxonomy

| Noise source | Signature in a run | Handling (now in code/process) |
|---|---|---|
| Transport flakiness (429s, timeouts, 4xx) | scenario `CRASH`, different scenario each run | wrap at client boundary → graded escalation; retry 429/5xx with `Retry-After` ([ADR 0007](../ADR/0007-transport-errors-are-domain-errors.md)) |
| Eventual consistency (60s lag cache, commit-after-response) | correct fix, judge returns `not_verified` on one instant read | verify-with-deadline polling; per-tool freshness declared in tool description ([ADR 0006](../ADR/0006-verification-is-a-polling-window.md)) |
| Shared mutable environment (dead consumers, drained fixtures, immortal idempotency records) | pass rate decays across runs; each failure looks novel | supervisor (platform); TTLs on idempotency + chaos; reset protocol; one-fault-one-scenario |
| LLM variance (extra probes, judge phrasing sensitivity) | `BUDGET` fails a resolved run; verdict flips on same evidence | caps calibrated with ≥30% margin; expectations state platform truth |
| Grader brittleness (serialized-JSON counts) | `EVIDENCE` fails a correct behavior | presence over counts; field asserts, not string fragments |

## Debugging discipline: bucket first

Before opening a code file on a failing run, classify. The signal you want is in `evals/traces/<scenario>.jsonl` + the `evals/reports/human/<scenario>.txt` render — not in the newest stack trace.

```
CRASH?                                     → transport bucket
                                             (look for MCPError/LLMError; check retry counts)
tool.is_error=True?                        → environment or platform bucket
                                             (drained pool? dead consumer? fixture reset needed?)
judge=not_verified with correct action?    → eventual-consistency bucket
                                             (raise VERIFY_PROBE_ATTEMPTS; check tool's freshness declaration)
BUDGET failed on a resolved run?           → LLM-variance bucket
                                             (recalibrate max_tool_calls with ≥30% margin)
EVIDENCE failed on a correct behavior?     → grader-brittleness bucket
                                             (relax the assert to presence, not count)
```

If two consecutive runs fail differently, you are almost certainly in the **environment bucket**: some resource from the previous run persisted. Reset and re-run one scenario in isolation before you touch code. Environment-caused failures are indistinguishable from novel bugs from the code side.

## The 7-run war story, briefly

Run 1: `remediate_consumer_lag_success` escalated with `not_verified`. Fix looked correct. Judged the cache.

Run 2: `remediate_consumer_lag_success` **crashed** with a raw `httpx.ConnectError`. Same scenario, different symptom, one flaky 429 apart.

Run 3: `remediate_dlq_backlog_success` escalated with `BUDGET`. Cap was 5; run took 6 (extra probe). Look-alike of Run 1 in aggregate, unrelated in cause.

Run 4: `remediate_stale_cache_success` failed with `is_error=True` on the read. Chaos hook that seeds the cache key had never been implemented — the scenario was unwinnable live, not broken. **Environment bucket.**

Run 5: `dlq_wait_and_replay_success` correct action + platform said `scheduled=2`, judge said `not_verified`. Judge's expectation said "the DLQ list should shrink"; platform's actual behavior is to hold the timer end-to-end and drain on the promote loop's next tick. **Grader had wrong assumption baked in.**

Run 6: `remediate_consumer_lag_success` (again) — same run made pass. Then a follow-up run in a different scenario made `chaos-restore` return `kill_key_cleared=true` — but the consumer was still dead. Turned out `restore-consumer-cli-invocation-01` was a hard-coded idempotency key: platform returned the *first* call's cached body, no delete happened. **Shared mutable environment.**

Run 7: Investigation: multiple scenarios showing "dead consumer" symptoms with no chaos-kill in their setup. Traced back to a *phantom supervisor* on the platform side: `restart_consumer_group` was documented to restart the consumer, but the supervisor that would do the actual re-spawn was never implemented. Every eval left a permanently-dead consumer, making subsequent runs look novel.

Five noise sources, one cascade underneath. The postmortem is in the [platform repo](../../CLAUDE.md#documentation) as `docs/postmortems/0002-phantom-supervisor.md`.

## What each source demands, generalized

**Transport flakiness** demands wrapping at the client boundary. Rule: business logic sees only `MCPError` / `LLMError` — never raw SDK types. Enforced by [ADR 0007](../ADR/0007-transport-errors-are-domain-errors.md) and client tests that assert the domain-exception surface.

**Eventual consistency** demands per-tool freshness declaration + a bounded verify-polling window. Rule: never judge an effect on one instant read of an eventually-consistent probe. Enforced by [ADR 0006](../ADR/0006-verification-is-a-polling-window.md) and `probe_attempts` / `probe_delay_seconds` config.

**Shared mutable environment** demands setup/teardown protocols. Rule: any eval that mutates shared state defines setup + teardown; runs start from a known baseline; injected faults self-clean (TTL) or are reset explicitly. Enforcement WIP: `Scenario` schema will require the hooks; `make eval-reset` for the reset protocol; supervisor on the platform side prevents the dead-consumer cascade at its source. Until then, one-fault-one-scenario is the manual protocol.

**LLM variance** demands calibrated caps with margin. Rule: `max_tool_calls` = expected live path + ≥30% margin, never a tight cap that a single extra probe breaks. Grader expectations must state what the platform *actually does*, not the mental model of what it should do. Enforced by the amended [eval-methodology](../eval-methodology.md#grader-calibration-rules).

**Grader brittleness** demands presence-over-counts. Rule: `expected_evidence_contains` items assert that a *field* or *concept* is present, not that a specific serialized JSON fragment appears. Assert `scheduled`, not `"scheduled":2`. Enforced by the amended [eval-methodology](../eval-methodology.md#grader-calibration-rules).

## Checklist for a live-eval PR

Before opening a PR that changes anything that could touch a live run:

- [ ] Every new tool declares its freshness in its description (instant / seconds / ~1min / manual-refresh).
- [ ] Every new scenario has `max_tool_calls` ≥ expected live path × 1.3, and `expected_evidence_contains` asserts presence of fields, not serialized counts.
- [ ] Every new chaos hook has a matching compensating action + a test that proves the pair round-trips.
- [ ] Every new external client's public methods raise only that client's own domain exception; SDK types stay behind the boundary.
- [ ] If the scenario mutates state, it declares its own reset / TTL story.

## Related documents

- [ADR 0006 — Verification is a bounded polling window, not an instant read](../ADR/0006-verification-is-a-polling-window.md)
- [ADR 0007 — Wrap all transport failures as domain errors at the client boundary](../ADR/0007-transport-errors-are-domain-errors.md)
- [`docs/eval-methodology.md#grader-calibration-rules`](../eval-methodology.md#grader-calibration-rules) — the calibration rules generalized
- [`docs/runbook.md#live-eval-protocol-post-hardening`](../runbook.md#live-eval-protocol-post-hardening) — the post-patch operating protocol
- [Phase-6 hardening lessons (earlier)](phase-6-hardening.md) — the "structural fix vs prompt tweak" precedent that shaped how these fixes were made
