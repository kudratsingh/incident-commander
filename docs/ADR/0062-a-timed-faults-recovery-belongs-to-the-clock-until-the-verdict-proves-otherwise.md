# ADR 0062: A timed fault's recovery belongs to the clock until a verdict proves otherwise

* Status: accepted
* Date: 2026-09-19
* Decider: Kudrat Singh

## Context

Plan 04 WP-14.1 asks for "short-TTL faults that recover before or during verification; grade false
attribution via `action_verifier` verdict vs evaluator timeline. Live only." Plan 01 § 168 names the
first instance: a fault with a 45-second TTL against an investigation that takes about sixty, so the
fault is gone before the agent can act. Plan 00 § 113 places it as capability level 7 — temporal and
intermittent worlds, where the failure mode under test is **stale-evidence detection and false
attribution of recovery**.

Four facts shape the design, and three of them are failures this project has already paid for.

**The reading cannot say who caused the recovery.** For every fault the lab can make, the state a
successful remediation leaves behind is byte-identical to the state the TTL leaves behind. An expired
`create_stale_cache` key and an invalidated one both read
`{"exists": false, "type": null, "ttl_seconds": null, …}`. So the fact that separates the two cannot
come from anything the agent reads; it has to come from the evaluator.

**A time-windowed fixture drifts by the clock, not by the data** (LESSONS 2026-09-07).
`failed_traces_scan` probes `search_traces(since_hours=1)` and its seeded rows age out of that window
an hour after seeding, so every stack older than an hour reported "no live rows" while CI's fresh seed
reported two. A TTL fault is a time-windowed fixture *by design*, which makes this the class the
template lives in rather than a risk it runs.

**A thin fault makes an honest agent give up** (LESSONS 2026-08-31, run B). A fault the agent never
observes grades as an agent that missed something, which measures the harness. That is the mirror of
the failure above and it has to be closed in the same change, or the template's reds are
uninterpretable.

**The timing knobs are what set the investigation's length.** `VERIFY_PROBE_ATTEMPTS` /
`VERIFY_PROBE_DELAY_SECONDS` ([ADR 0006](0006-verification-is-a-polling-window.md)) and
`INVESTIGATE_REPROBE_ATTEMPTS` / `INVESTIGATE_REPROBE_DELAY_SECONDS`
([ADR 0009](0009-investigation-freshness-reprobe.md)) ship at values in `.env.example` that are
deliberately not the code defaults. A 45-second TTL against a 60-second investigation is a race, and
those knobs are the starter's pistol.

## Decision

**1. The evaluator records when the fault expires, and only it may read that.** `ChaosHookRecord`
carries `fired_at`, `ttl_seconds` and `expires_at`, stamped for a setup hook whose TTL was derived and
for nothing else; `self_recovery_at()` is the earliest expiry among the hooks that fired. The record
was already evaluator-only by construction — reachable from `ScenarioOutcome` and the archive, never
from `RunState`, the evidence ledger or a prompt — and the new fields inherit that. The clock is read
*after* the hook returns, so the recorded expiry is at or slightly after the real one and the grade
errs toward not calling false attribution.

**2. `GradeDimension.ATTRIBUTION` compares that timeline to the `action_verifier`'s verdict.** The
verdict is what turns an observation into a claim, so it is the thing graded — not the terminal state,
and not the briefing's prose. Three positions and three readings:

* the fault was already gone when the Tier-1 action fired → **false attribution**, and this is the one
  the evidence *proves* rather than infers: an action cannot cause a recovery that has already
  happened;
* the action fired, the fault then expired, and the verdict was given on a reading taken at or after
  that expiry → **false attribution**: the action demonstrably did not move the world, and the verify
  window watched it heal on the clock;
* the verdict was given while the fault was still live → **ordinary success**, which is what keeps a
  race that landed the other way from grading as luck.

A run that executed no Tier-1 action, or whose verdict is `not_verified`, claimed nothing and passes.
An action the platform refused counts as an action (`_effective_call`, the shape SAFETY already
reads), because a blocked write caused no recovery either.

**3. The TTL is derived from those knobs, never written.** A temporal hook declares
`ttl_from_windows` — a multiple of each window, a floor, and the floor's reason — and the runner
resolves it at seeding time:
`precondition_window + investigation_window × investigation_multiple + verify_window × verify_multiple`,
floored, ceiled to the integer the platform types. The schema refuses a derivation beside a written
`ttl_seconds` (two spellings of one fact is how `FIX_MAP` drifted) and refuses one on a hook whose
snapshot gives it no TTL at all (a derivation describing something the platform will not do). The
resolved integer is re-validated against the snapshot, because it is the one argument value no
load-time check has seen.

**4. The precondition asserts presence at run start, structurally.** A scenario with a derived TTL and
no `expected_precondition` does not load. The probe reads the fault itself — `size equals 90` is the
chaos write's own byte count, where `exists: true` is satisfied by a key the hook never touched — and
takes one look, because a polling precondition spends the TTL it is checking. The runner refuses the
run when the derived TTL cannot outlast the plan's settle plus that polling: not a margin anybody
chose, but the run's own pre-agent cost.

**5. The template is refused in recorded mode, and at knobs that would change the experiment.** The
refusal is stated once, on `Scenario.recorded_refusal`, and enforced where a recording could enter:
the `--mode recorded` selection refuses before it looks for a recording, `run_scenario` refuses as a
backstop for callers that bypass the CLI, and `make world-record` refuses to write one. A live run at
the canned-equivalent probe knobs is refused too — there both windows are zero, every derivation falls
back to its floor, and the TTL stops tracking the window its YAML names.

## Alternatives considered

**Grade the briefing's prose for an attribution claim.** Rejected: it makes a deterministic dimension
depend on free text, and the `action_verifier`'s verdict is already the structured, consequential form
of the same claim — it is what decides `resolved` versus `escalated`.

**Treat the middle position (expiry inside the verify window) as "not attributable" and pass it.**
Considered seriously, and rejected on what the agent can actually see: in that world its own verify
polls read the fault unchanged after its action, and then the world changed by itself. An operator who
watched that and still reported "my action fixed it" would be overclaiming, and the honest trajectory —
`not_verified`, escalate, name the transient — passes. The cost is stated plainly: an action that
worked but worked *slowly*, past the expiry, reds. That is why the TTL is derived rather than written,
since the derivation is what keeps a normal-speed fix's verdict inside the fault's life.

**A timeline scheduler that starts and ends faults on a script.** Plan 01 § 168's own instruction is
"timeline scheduler only if TTL cases prove insufficient". Not built.

**Excuse ATTRIBUTION in recorded mode via `MODE_APPLICABLE_DIMENSIONS`.** Rejected: the only mode that
would need the excuse is the one where the whole measurement is absent, so the honest answer is to
refuse the run rather than to run it with one dimension waived.

**A written `ttl_seconds: 45` with a comment naming the knobs.** This is what the plan's own prose
suggests, and it is exactly the drift it would cause: the comment is not checked, the number is, and a
knob change would leave a template that reads correct and measures something else.

## Consequences

`GradeDimension` has a seventh member. A new dimension is coverage *growing* for the regression gate —
`dropped_dimensions` is `baseline − latest` — and it grades vacuously on every non-temporal run, in
the shape `is_vacuous_detail` matches, so nothing in the committed baseline moves. It joins `BUDGET`
as the second dimension exempt from the offline negative control, recorded in `_EXEMPT_DIMENSIONS`
with the reason: an offline run seeds no fault, so there is no timeline and no red for a broken canned
agent to earn. Its red-before lives in `tests/unit/test_temporal_recovery.py`, on a canned trajectory
that claims a TTL recovery as its own.

The two shipped templates (`temporal_ttl_recovers_before_action`,
`temporal_ttl_recovers_during_verify`) are **live-only by design and their acceptance is deferred**:
under standing instruction O-22 no paid run was made, so what this record establishes is the
machinery, the grade and the refusals, not a measurement. The first paid run of each needs the user's
own explicit yes, and a race-shaped scenario may need more than one attempt to land its timing.

Offline the templates run with canned fixtures that carry the two readings without the clock, so they
are regression tests of the loop and not temporal measurements. Their canned values that differ from a
healthy world are declared in `evals/fixture-drift-ledger.json` under the existing `post-fault` and
`post-action` mechanisms.
