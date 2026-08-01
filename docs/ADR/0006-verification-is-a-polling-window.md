# ADR 0006: Verification is a bounded polling window, not an instant read

* Status: accepted
* Date: 2026-07-31
* Decider: Kudrat Singh

## Context and problem statement

The VERIFYING transition executes a plan's verify probe against the platform and asks a judge LLM whether the observed state matches the plan's expectation. In offline canned runs this is instant-consistent — one probe returns the truth. Against a live platform it is not: `get_consumer_lag` reads a 60s-cached metric, DB-effect commits land just after the tool's response is serialized, and downstream indexes catch up on the next tick. During the seven-run live eval that produced this ADR the same behavior — correct action, correct target, correct expectation — was graded `not_verified` because the single probe fired ~1s after the action returned. The transition was judging the cache, not the fix.

Compounding this: the loop's per-step budget short-circuit was severing the same atomic unit. A run that had executed a Tier-1 action could exhaust its budget on the verify probe and be escalated with the action landed but unverified. That is worse than one extra probe over budget — the whole safety story rests on "an executed remediation is always verified or explicitly escalated with the fact declared."

## Decision drivers

* Live probes are eventually consistent; freshness is a per-tool property, not a global assumption.
* An executed-but-unverified Tier-1 action is a safety regression. VERIFYING must always run to completion once REMEDIATING has committed.
* Canned/offline runs are the regression-test substrate for the entire suite. Their instant-consistent behavior must remain the default so 37 canned scenarios keep passing byte-identically.
* The polling window is a live-eval knob, not a code-path change: same transition, same evidence shape, same escalation semantics — just re-read the probe before giving up.

## Considered options

1. Move VERIFYING to a background scheduler that reruns the transition on a timer (real durable polling with checkpoints between attempts).
2. Push retry into the probe tool itself (the MCP client retries the read until the platform reports "fresh").
3. Bounded polling loop inside the VERIFYING transition, configurable per-run, single-probe by default (chosen).

## Decision outcome

Option 3. The `make_llm_verify` factory accepts `probe_attempts` (default 1) and `probe_delay_seconds` (default 0). When `probe_attempts > 1` and the judge returns `not_verified`, the transition sleeps and re-probes, accumulating each probe + judgment into the evidence ledger and the budget ledger. On the first `verified` verdict it transitions to RESOLVED. If every attempt returns `not_verified`, the transition escalates carrying the full probe history.

The runner wires the setting only when `live_mcp_available` is true. Canned runs pass `probe_attempts=1`, preserving one-probe behavior. `Settings` exposes `VERIFY_PROBE_ATTEMPTS` (default 1, `le=10`) and `VERIFY_PROBE_DELAY_SECONDS` (default 15s) as environment overrides.

Two paired changes protect the action+verify atomicity:

* `run_to_completion` exempts VERIFYING from the budget short-circuit. The docstring names why: an executed-but-unverified action violates the safety story more than one extra over-budget probe.
* `make_llm_plan` refuses to enter REMEDIATING with fewer than 2 tool calls of headroom (action + at least one verify). It escalates before spending planner tokens or executing anything.

Together these are one atomic multi-step operation reserved up front and never severed mid-unit.

### Why the alternatives lose

**Background scheduler.** Real durable polling with checkpoints between probe attempts is the right answer for a very long verify window (minutes to hours). At the current probe cadence (a 60s cache, one action to verify, seconds not hours) it is over-engineered — a new scheduler process, a new checkpoint edge, a new reconciliation case on resume. Revisit if the verify window ever needs to survive process restarts.

**Retry inside the tool client.** Would hide the polling from the state machine and from evidence. The transition should own the "did the fix take" judgment, not the transport. Also: freshness is per-tool (60s for lag cache, near-instant for DB reads); pushing the window down loses that per-tool sizing.

### Consequences

Positive:

* Live probes get a real chance to reflect the fix. The verified/not_verified distinction now grades behavior, not a race with the cache.
* Executed Tier-1 actions always terminate in verified or explicit-escalation-with-evidence.
* Canned suite is unchanged (default `probe_attempts=1`), and 37/37 offline scenarios still pass byte-identically.
* Evidence and budget accumulate across probes, so an escalation report carries the full polling story.

Negative:

* The polling window runs inside one transition dispatch. No checkpoint lands mid-poll, so a crash during a live verify window loses that partial evidence. Mitigation: the window is bounded (default 3 attempts × 15s = 45s); crash recovery re-plans from the last checkpoint, and the reconciler already reads the platform audit before re-executing anything.
* Live budget consumption per incident goes up (one extra probe per verified action on average). Mitigation: the eval budget caps were recalibrated in the sibling PR (c) with ≥30% margin.

Revisit trigger: if the verify window ever needs to exceed one dispatch cycle (e.g. verifying a change that takes minutes to propagate), promote to Option 1.

## More information

Amends ADR 0002's VERIFYING semantics — see the "Consequences" section there for the atomicity guarantee this ADR now underwrites. Implemented in `src/incident_commander/agent/remediation.py:make_llm_verify`, `src/incident_commander/agent/loop.py:run_to_completion`, `src/incident_commander/agent/remediation.py:make_llm_plan` (headroom check), and `src/incident_commander/config.py`. Regression tests in `tests/unit/test_phase6_hardening.py`.
