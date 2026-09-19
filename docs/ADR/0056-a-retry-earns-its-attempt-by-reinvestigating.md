# ADR 0056: A retry earns its attempt by reinvestigating — and never by re-sending

* Status: accepted
* Date: 2026-09-18
* Decider: Kudrat Singh (WO-R3-226, WP-10.1)
* Supersedes: [ADR 0008](0008-single-attempt-remediation.md) (single-attempt remediation). Closes
  WO-R2-155.
* Related: [ADR 0002](0002-hand-rolled-state-machine.md) (the transition graph this edge joins),
  [ADR 0006](0006-verification-is-a-polling-window.md) (why the eventual-consistency case is not a
  retry), [ADR 0016](0016-incident-identity-and-single-flight.md) (one run per incident, which the
  second attempt stays inside), [ADR 0026](0026-a-stabilizer-is-not-a-resolution.md) (a verified
  stabilizer still escalates), [ADR 0030](0030-a-mis-transcribed-resource-id-buys-one-re-ask.md)
  and [ADR 0032](0032-the-action-must-address-the-alerts-subject.md) (the plan-time guards a second
  attempt still faces), [ADR 0033](0033-a-human-required-chain-root-is-fenced-then-escalated.md)
  (fence then escalate, unchanged)

## Context and problem statement

ADR 0008 deleted the `VERIFYING → PLANNING` edge and took single-attempt remediation as a deliberate
posture rather than a gap. It was right about the edge and explicit about what would bring the
decision back: "reintroduce this decision when we're ready to build it properly; see the deferred
design below." That deferred design named the shape — `VERIFYING → INVESTIGATING`, previous-attempt
context in the planner prompt, a cap of two, paired eval scenarios — and this record implements it.

Two things force the reopening now. The capability ladder's levels 5 and 8 are unreachable while the
agent may act once: both describe an agent whose first action fails and whose second is chosen
differently. And the adaptive strategy's strongest trigger — "the first action failed, spend more
inference" — cannot exist until the run can act twice.

The risk ADR 0008 named is real and unchanged: **an agent that acts again autonomously after its
first action demonstrably failed is a worse risk profile.** So the question this record answers is
not "should the agent retry" but "what makes a second attempt a different attempt".

## Decision

`ALLOWED_TRANSITIONS[VERIFYING]` gains `INVESTIGATING` — never `PLANNING`. `MAX_REMEDIATION_ATTEMPTS`
(default 2, ceiling 3) is a `Settings` field. The edge is taken when an attempt has ended without
ending the incident — a `not_verified` verdict, a verified stabilizer (ADR 0026), or a verified
action that left the alerted condition standing (WO-R2-164) — **and** all three preconditions below
hold. Otherwise the run escalates exactly as it did before this record.

### The three preconditions, in order

1. **The cap.** `remediation_attempts < MAX_REMEDIATION_ATTEMPTS`. At the cap the run escalates with
   a reason naming it ("no fix converged after N Tier-1 attempts"), and the `make_llm_plan` guard
   holds the same number as a backstop.
2. **The budget.** Budgets are NOT reset. A run that cannot fund an action and its verify out of
   what is left escalates instead, with the verdict that stands.
3. **Somewhere else to go.** At least one ranked hypothesis other than the attempted one carries a
   `FIX_MAP` category. With none, the only plan a reinvestigation could reach is the one the
   identical-attempt guard refuses, so the outcome is already known and a planner call would buy
   nothing.

The third precondition is the one that keeps this from being the shape ADR 0008 rejected, and it is
also why every scenario written before this record behaves identically: each ends on a single ranked
hypothesis, the one it just acted on.

### The four structures that make a second attempt a different attempt

* **Reinvestigation, not re-planning.** The edge goes to `INVESTIGATING`, so the second plan is made
  from evidence gathered *after* the failure. Re-entering `PLANNING` with the ledger that produced
  the failure is "the same attempt with worse justification" (ADR 0008's words).
* **The identical-attempt refusal, structural and never prompted.** A plan whose `(tool, wired
  arguments)` pair this run already executed is refused before execution and the run escalates
  naming the refusal. The comparison is on the WIRED form, because `wire_arguments` default-fills
  every omitted optional — two plans one `ttl_seconds` apart on paper are one call on the wire.
  `idempotency_key` is excluded from both sides: it is a deterministic function of (incident, tool,
  arguments), so an identical re-send would carry the first attempt's key and the platform would
  replay its cached response — a perfect success that did nothing (LESSONS 2026-09-07). Refusing the
  repeat is what makes "mint a fresh key" true by construction rather than by care.
* **The failed attempt on the ledger.** A structured `_remediation_attempt_failed` entry records the
  tool, its arguments, the verify probe, the reading and the verdict, and both planner contexts pull
  it out of the evidence dump and render it whole under "Already attempted in this incident — do NOT
  repeat:". It also reaches the briefing's `escalation_reason`, because the investigation trail
  filters underscore markers and a human must be told about a Tier-1 write already made on their
  system.
* **Hypotheses carry over.** The ranking is not cleared, so the planner is asked to re-rank given the
  failure rather than to start over. The spent plan IS cleared: the next `PLANNING` writes its own.

### What does not change

No gate is relaxed. The Tier policy, the approval model, the subject guard (ADR 0032), the
read-before-act guards (ADR 0027/0028), the argument guards (ADR 0030) and the `FIX_MAP` check all
run on the second plan exactly as on the first — the preconditions above are necessary, not
sufficient, and `stabilizer_then_reinvestigate` is the witness: its alternative hypothesis is one
ADR 0032 would refuse to act on, and the run stops instead of acting. A verified stabilizer still
escalates (ADR 0026) and a `human_required` chain root is still fenced then escalated (ADR 0033);
what the edge adds is that the stabilized run may look once more before handing off, carrying
"STABILIZED, NOT RESOLVED" onto the ledger and into the briefing so reinvestigating cannot lose it.

## Considered alternatives

**Keep ADR 0008.** Rejected by the ladder: two capability levels and the adaptive strategy's best
trigger are unreachable without the edge, and the deferred design already said how to build it.

**`VERIFYING → PLANNING`, as the graph shipped in Phase 6.** Rejected for ADR 0008's own reason: the
planner would re-derive essentially the same plan from an unchanged ledger.

**Prompt the planner not to repeat itself.** Rejected by `docs/architecture-principles.md` — default
to the structural fix. The prompt says what a good second attempt looks like; only the guard makes a
bad one impossible, and the guard is what a reviewer can check.

**Enforce the cap only at `PLANNING`.** Rejected because the escalation reason a human reads would
then be an invariant message rather than "no fix converged after two attempts", and `VERIFYING` is
where both verdicts are known. The `PLANNING` guard keeps the same number as a backstop.

**Reset the budget for the second attempt.** Rejected: invariant 7 makes budgets hard limits per
incident, and a per-attempt ledger would let a retrying run spend twice what a first-attempt run may.

## Consequences

Positive:

* The agent can act twice when — and only when — the second action targets something different, and
  the whole path is deterministic: three preconditions, one refusal, one cap.
* A human reading an escalation now sees every attempt that was made, which the single-attempt
  posture never had to say.
* `MAX_REMEDIATION_ATTEMPTS=1` restores ADR 0008's behaviour exactly, so the posture is a setting
  rather than a rewrite.

Negative:

* **This is a safety-surface change: the agent may now perform two Tier-1 writes without a human.**
  The mitigations are the ones above, and the honest statement of residual risk is in
  `docs/safety-model.md`.
* The four scenarios that exercise the edge are CANNED. A first action that lands and still does not
  clear the fault needs a chaos variant that survives the fix (WP-10.0, WO-R2-165), which the
  platform does not have — so the edge has never run against a live world. WP-10.2 is that
  confirmation and it is deferred, not done.
* A live retry scenario will need a tool-call cap admitting two ADR 0006 verify windows (~19 rather
  than 13). The canned caps are the class minimum today; WP-10.2 sets the live one.
* The ceiling of 3 is arbitrary in one direction: a third attempt has no scenario justifying it, and
  raising the knob without one would buy autonomy no eval measures.

## More information

* Implemented by WO-R3-226 (WP-10.1): the edge in `agent/orchestrator.py`, the cap and both guards
  in `agent/remediation.py`, the rendering in `agent/planner_context.py`, the briefing in
  `agent/briefing.py`, `MAX_REMEDIATION_ATTEMPTS` in `config.py`.
* Scenarios: `retry_second_hypothesis_succeeds`, `retry_identical_refused`, `retry_cap_escalates`,
  `stabilizer_then_reinvestigate` — each asserting the guard's own marker or reason rather than a
  terminal state (F-007).
* Both ADR 0008 pins moved with it: `tests/unit/test_orchestrator.py` and
  `tests/unit/test_grader.py::TestTheOneActionClaimIsStructuralNotGraded`, the second of which now
  pins the three structures that replaced "at most one call" as the graph property its scenarios
  lean on.
* `llm/prompts/remediation_planner.md` cited ADR 0008 twice for "you get one Tier-1 call". Both
  citations now name this record and say the true thing: a PLAN is one Tier-1 call.
