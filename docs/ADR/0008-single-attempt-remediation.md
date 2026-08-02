# ADR 0008: Single-attempt remediation — delete the VERIFYING→PLANNING retry edge

* Status: accepted
* Date: 2026-08-01
* Decider: Kudrat Singh

## Context and problem statement

`ALLOWED_TRANSITIONS[VERIFYING]` shipped with a `PLANNING` successor from Phase 6 day one. The intent was "if the fix didn't verify, re-plan and try again." No code path actually took the edge in the six months since — every `not_verified` verdict escalated. The FIX_PLAN v2 audit surfaced two adjacent artifacts of the same never-implemented feature: the reachability was assumed by an attempts-cap guard in `make_llm_plan`, and REMEDIATING carried a `_action_already_executed` reconciliation short-circuit that was meant to make re-entry safe. Both were defending against an execution path the state machine never actually produces.

The choice is whether to (a) implement the retry loop that would justify the edge, or (b) remove the edge and its dependent code and take the position deliberately.

## Decision drivers

* **Verify polling already ate the useful case.** [ADR 0006](0006-verification-is-a-polling-window.md) turned VERIFYING into a bounded polling window. The scenario "fix worked but wasn't visible yet" — the honest use case for a retry edge — is now handled inside a single attempt. What's left is retrying when the fix genuinely didn't work.
* **The useful shape of retry is not what this edge produces.** The edge re-enters PLANNING with an unchanged evidence ledger and unchanged hypothesis ranking, so the planner re-derives essentially the same plan and re-fires the same action against a system it already mutated once. That's not a second attempt — it's the same attempt with worse justification. A useful retry targets hypothesis #2 or a different `FIX_MAP` action, with prior-failure context in the planner prompt. That routes through `VERIFYING → INVESTIGATING`, not `VERIFYING → PLANNING`, and needs prompt work, budget semantics, and its own eval scenarios. Not a cleanup task.
* **Single-attempt-then-escalate is the correct safety posture for a Tier-1-action agent, not a limitation.** A failed remediation is information: the agent's model of the incident is wrong. The production-correct response is a human with a briefing — which is already the flagship feature. An agent that acts again autonomously after its first action demonstrably failed is a different, worse risk profile. Framing this as "retry not implemented yet" would read as a gap; framing it as an explicit posture reads as judgment.
* **The reconciliation branch is triply condemned.** `_action_already_executed` can't be kept in either world:
  1. Its documented crash window (checkpoint after tool call, before state advance) doesn't actually occur — evidence and state land in one atomic checkpoint.
  2. Its tool-name matcher would silently skip a legitimate second attempt if the retry loop ever went live (it would treat *any* prior invocation of the same tool name in the same incident as "already done").
  3. The problem it guards against is already solved one layer down. A crash-resume that re-executes REMEDIATING re-sends the same idempotency key and gets the platform's cached response — proven live by [`tests/integration/test_idempotency_contract.py`](../../tests/integration/test_idempotency_contract.py) and covered by ADR 0010 on the platform side. Client-side execute-once reconciliation was redundant defense against something the wire contract handles by design.

## Considered options

1. Implement retry-with-reinvestigation: keep the VERIFYING→PLANNING edge, extend REMEDIATING's reconciliation to route around it, add planner-prompt failure context, add eval scenarios for the retry path, extend budget semantics. Ship as a phased feature.
2. Delete the edge, the attempts-cap enforcement path that assumed the edge could be taken, and the `_action_already_executed` reconciliation branch (chosen).

## Decision outcome

Option 2. Concrete changes:

* `ALLOWED_TRANSITIONS[VERIFYING] = {RESOLVED, ESCALATED, FAILED}`. The `PLANNING` successor is removed and replaced with a comment pointing at this ADR.
* `_action_already_executed` and its short-circuit branch in `transition_remediate` are deleted. The transition's docstring names the crash-recovery contract explicitly: on resume, `build_idempotency_key(incident, tool, args)` produces a deterministic key, the platform's idempotency store returns the cached response, and the effect does not double.
* The attempts-cap check in `make_llm_plan` is reframed as an *invariant guard*, not a soft limit. Under the current graph it should be unreachable (PLANNING is only entered from INVESTIGATING, where `remediation_attempts == 0`). If it ever fires, either the graph was mutated without updating this ADR or someone constructed a `RunState` directly bypassing dispatch. The escalation reason names the invariant explicitly ("invariant violation (ADR 0008): PLANNING reached with remediation_attempts=N"), and a matching test constructs the impossible state so the guard stays enforced rather than decorative (Practice 8 from `docs/lessons/live-eval-noise-sources.md`).
* A Practice-12 test in `test_orchestrator.py` asserts the exact shape of `ALLOWED_TRANSITIONS[VERIFYING]`. A well-intentioned "let's just add retry" PR flips this test, forcing the conversation before the loop is silently restored.
* No scenario or grader changes. The live path's terminal states were already `RESOLVED` and `ESCALATED`; the removed edge was unreachable.

### Why the alternatives lose

**Implement retry-with-reinvestigation.** The right shape (VERIFYING → INVESTIGATING with prior-failure context in the prompt, targeting a different hypothesis or `FIX_MAP` action, with paired eval scenarios that prove the retry is *distinct* from the first attempt) is a designed multi-phase feature, not a cleanup. Reintroduce this decision when we're ready to build it properly; see the deferred design below.

### Consequences

Positive:

* Three defensive code paths (dead edge, tool-name matcher, attempts-cap-as-limit) collapse to one explicit invariant guard with a matching test.
* The crash-recovery contract is now stated in one place — the wire contract, verified live by the idempotency-contract test — instead of duplicated between agent and platform.
* An honest safety posture: the agent acts once, then hands off. That's the story the flagship feature tells, and now it's the story the code tells.

Negative:

* No autonomous retry when a Tier-1 action fails. Human intervention is required for every not-verified outcome. Mitigation: verify polling ([ADR 0006](0006-verification-is-a-polling-window.md)) absorbs the eventual-consistency case that would otherwise look like a failed action.
* The invariant guard could turn into decoration if nobody notices the test that enforces it. Mitigation: the guard's escalation reason contains "ADR 0008" as a searchable string, and the Practice-12 test on `ALLOWED_TRANSITIONS[VERIFYING]` asserts the exact successor set.

Revisit trigger: when a phase deliberately introduces retry-with-reinvestigation (see below), reopen this ADR and mark it superseded rather than edit it.

## Deferred design: retry-with-reinvestigation

If a later phase brings back multi-attempt remediation, the shape should be:

* **Edge**: `VERIFYING → INVESTIGATING`, not `VERIFYING → PLANNING`. The signal from a `not_verified` verdict is "our understanding of the incident is wrong," so the loop should gather new evidence, not re-plan against stale evidence.
* **Planner prompt**: on the second entry to PLANNING, include a `previous_attempts:` block naming each failed action + its verify probe result, so the planner is constrained to pick a *different* target hypothesis or a *different* action tool. Without this the loop degenerates to same-plan-fires-again.
* **Budget**: reserve action + verify headroom per attempt at INVESTIGATING entry, not just at PLANNING (see [ADR 0006](0006-verification-is-a-polling-window.md) for the headroom pattern).
* **Attempts cap**: keep at 2 total (initial + one retry), enforced at the INVESTIGATING → PLANNING transition, escalate at cap with a "no fix converged after N attempts" briefing.
* **Eval scenarios**: at least one scenario per Tier-1 hypothesis where attempt 1 (wrong hypothesis) fails to verify and attempt 2 (right hypothesis, forced by prompt exclusion of attempt-1's target) succeeds. Without this, the retry loop isn't tested.
* **Reconciliation**: still not needed on the agent side. The idempotency wire contract already dedupes retries transparently.

Cite this ADR from the reintroducing PR rather than editing it.

## More information

- Deletes the reconciliation branch and dead edge added in Phase 6.
- Amends the crash-recovery description in [ADR 0002](0002-hand-rolled-state-machine.md) — the reconciliation story is now the platform's idempotency dedup, not client-side evidence-log matching.
- Cross-references [ADR 0006](0006-verification-is-a-polling-window.md) (verify polling as the useful case), ADR 0010 on the platform side (idempotency lifecycle), and [`tests/integration/test_idempotency_contract.py`](../../tests/integration/test_idempotency_contract.py) (live wire proof).
- CLAUDE.md Phase 6 line updated to name single-attempt as the posture.
