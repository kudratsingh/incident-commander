# Pre-registered predictions — clean-baseline rerun (Run 001)

Date: 2026-08-07

Each line is falsifiable against Run 001's committed artifacts (traces,
trajectories, `latest.json` failure_class buckets, platform audit log).

1. **#65 freshness re-probe:** zero incidents where a cached read kills an
   actionable (FIX_MAP, ≥0.7) hypothesis that a fresh re-read would have
   sustained — stale-read hypothesis abandonment ≈ 0 (was the direct cause
   of the campaign's wrong-remediation trace).
2. **#66 evidence-sourced args:** zero Tier-1 action failures caused by
   re-typed resource names (platform refusals of malformed keys/ids) —
   typo-class action failures = 0 (was 1/1 on stale_cache live).
3. **#64 equivalence graders:** zero ACTION-dimension failures where the
   agent achieved the scenario's intended effect via a same-effect sibling
   tool — grader-bucket fails on correct behavior ≈ 0 (was 2 in the
   campaign: dlq_backlog, wait_and_replay).
4. **Kill-based consumer_lag:** `remediate_consumer_lag_success` passes
   live — the fault is now manufacturable (was 0/3 across the campaign's
   latency-based attempts).
5. **Enforced pause:** `remediate_runaway_saga_success` passes live with
   the verify judge citing `paused=true` — the effect is now observable
   (was 0/2; both prior attempts escalated on an invisible pause).
6. **Read-scoped smoke token:** zero Tier-1 writes in the platform audit
   log during stage 1 — smoke-stage Tier-1 writes = 0, enforced by scope
   rather than scenario selection (the campaign's read-only pass fired one).

Pre-registered before Run 001 (clean-baseline rerun, pinned v0.4.9).
Full study charter lands post-run.
