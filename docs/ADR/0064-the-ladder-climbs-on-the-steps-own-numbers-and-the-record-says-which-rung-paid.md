# ADR 0064: The ladder climbs on the step's own numbers, and the record says which rung paid

* Status: accepted
* Date: 2026-09-19
* Decider: Kudrat Singh

## Context

Plan 02 § 15 (WP-13.2) asks for the strategy that composes everything above it:
`baseline → best_of_n_enumerated(4) → candidate_selector → search or escalate`, driven entirely by
[ADR 0061](0061-a-threshold-is-declared-with-the-split-it-was-set-on.md)'s thresholds, and the
accuracy/cost Pareto against the fixed arms. Plan 00 § 7 item 10 is the claim it exists to test, and
it is two-sided: **easy scenarios stay cheap AND hard scenarios show headroom.** Either half alone is
worthless — a ladder that climbs on everything is `baseline` plus overhead, and a ladder that never
climbs is `baseline`.

Four facts shape the design.

**The cheap half has to be falsifiable.** "Easy cases are cheap" is the kind of sentence a report can
print about an arm that quietly made three calls per step. The only version of it worth having is a
count on the record, produced by the arm itself, that a test can read: the extra calls beyond the
control group's one.

**Two of the four rungs are whole strategies.** `best_of_n_enumerated` and `search` are shipped arms
with their own records, their own sinks, their own failure types and their own bounds. A ladder that
re-implemented either would be measuring a second implementation, and plan 02 § 4's seam exists so it
does not have to.

**The top rung cannot always run.** [ADR 0060](0060-a-branch-reads-through-the-loop-and-spends-the-runs-own-ledger.md)
bounds `search` to recorded mode: a branch reads the world, and in a live world each branch would read
a world that had already moved. Plan 02 § 15's own wording — "search **or escalate**" — is what the
ladder does about that.

**One of the seven signals is not about this step.** `remediation_attempt_failed` counts the attempt
records the remediation loop appends ([ADR 0056](0056-a-retry-earns-its-attempt-by-reinvestigating.md)),
and they stay on the ledger for the rest of the run. No rung can clear it.

## Decision

**1. The ladder is a sequence in code, and its second rung's N is not a knob.** `CLIMB` is
`(baseline, best_of_n_enumerated, candidate_selector)` with the tail resolved per step;
`LADDER_N = 4` is plan 02 § 265's number, declared in `agent/strategies/adaptive.py` rather than read
from `StrategyKnobs`. `BEST_OF_N=1` would turn the second rung into a second `baseline` call and the
report would still say `adaptive`. What ran is stamped into `strategy_config` instead.

**2. Rung 0 is the control group's call, verbatim.** `investigation._plan_next_step` — the same
function `baseline` and `reflection` call — so an easy step is the control group: the same prompt, the
same rendered context with no evidence ids ([ADR 0044](0044-evidence-ids-are-rendered-for-the-arms-whose-schema-cites-them.md)),
the same emitted step, the same accrued state, and the same `llm_calls` on the record. A test asserts
that equality against `BaselineStrategy` on the same turn rather than describing it.

**3. A rung is entered only because the rung below it fired.** After each rung the policy is
evaluated again — on the run as that rung left it, with the reading that rung can supply: nothing at
rung 0, the candidate set at rung 1, the set plus the selector's own `uncertainty` at rung 2. A signal
nothing could measure is UNMEASURED, never not-fired (ADR 0061 § 7). If nothing fires, the climb
stops and that rung's step is what the loop gets.

**4. The selector rung selects over the set the rung below already paid for.** It is one
`select_candidate` call and `step_for_selection`, not `CandidateSelectorStrategy.plan_next_step`,
which would generate a second set. The ladder's cost is therefore strictly additive: one planner
call, then one more, then one selector call, then a walk.

**5. Every rung transition is on the `StepRecord`.** `LadderRecord` carries the resolved ladder, the
rung the step terminated on, `extra_llm_calls`, whether `search` was reachable, the live value of
every threshold, and one `RungRecord` per rung with `entered_because` (the rung below's fired
signals), `fired`, `unmeasured`, the reasons in words, its own token/dollar/tool-call deltas, and the
`climbed`/`emitted` flags. Exactly one rung per step is `emitted`. This is what lets the report say
what fraction of runs escalated at each rung (plan 03 § 173) instead of averaging it away.

**6. The tail is `search` where a branch may read, and `escalate` where it may not.** `escalate` is
not a strategy: it buys no inference and emits a `StopAction` naming the signals still firing and the
reason the walk was unavailable, which the loop turns into an escalation with a briefing. The
alternative — falling back to the selector rung's step — would report a laddered number for a run
that silently skipped its top rung. `search` itself remains refused outside recorded mode at the
edge; `adaptive` is not refused, because ending at `escalate` is the plan's own other branch.

**7. A signal no rung can clear buys the climb and does not decide the tail.** `UNCLEARABLE` holds
`remediation_attempt_failed`. It escalates the ladder — that is plan 02 § 15's intent, and the
strongest evidence available that a diagnosis was wrong — but the tail is decided by
`fired − UNCLEARABLE`. Otherwise every step after one failed attempt takes the tail unconditionally,
which off recorded mode means the reinvestigation ADR 0056 granted is stopped on the step it was
granted for. The still-firing signal is on the record as `ladder.unclearable`, so the stop is
auditable rather than invisible.

**8. One step, one record.** The `search` rung runs with `record_step` removed from its context and
its `search` block, calls, candidate set and selector record are folded into the ladder's own record.
Two records per step would double every per-step count downstream.

**9. Every rung is gated exactly as `baseline` is.** A rung emits an ordinary `InvestigationStep`, so
the `FIX_MAP` gate, the 0.7 confidence bar, the subject-probe refusal,
[ADR 0041](0041-read-the-whole-queue-before-you-replay-part-of-it.md)'s whole-queue rule, the
ADR-0009 re-probe and `_execute_probe`'s tier re-check all run in `investigation.py`. The module
constructs no action but a `StopAction` — an AST scan asserts it — so the action a rung emits is the
object a planner call produced. **More thinking, not more privilege** (plan 00 § 3.10, plan 02 § 18).

**10. Cost is reported per rung, never as a multiplier.** `adaptive`'s spend sits between
`baseline`'s and the top rung's by construction, so the report prices it from the measured per-rung
costs on the record. The budget multipliers are untouched: tool calls are never multiplied (plan
02 § 8) and the ledger one run is seeded with is still `factory.start_run`'s.

## Considered and rejected

* **The enumerated rung's N from `BEST_OF_N`.** Symmetrical with the fixed arms, and an operator
  might reasonably want 8. Rejected: the ladder's identity is the claim being measured, and a knob
  that can set it to 1 makes `adaptive` indistinguishable from a two-call `baseline` in a report that
  still says `adaptive`. A later packet that wants a second ladder gives it a name.
* **Re-running `candidate_selector` as a rung.** One line instead of two, and it reuses a whole arm.
  Rejected: it regenerates the candidate set, so the third rung would cost two planner calls instead
  of one and the ladder's cost would stop being additive. The selector rung reuses the *call*, which
  is the part that differs.
* **Evaluating the policy before the planner call.** Cheaper — a step could skip rung 0 entirely and
  start at rung 1. Rejected: the thresholds read the ranking, and before the call the ranking is the
  previous step's. The ladder would be spending this step's money on last step's uncertainty, and the
  first step of every run has no ranking at all.
* **Falling back to the selector rung when `search` is unavailable.** Kept every canned run out of
  the escalation rail, and looks conservative. Rejected under decision 6: it hides a missing rung.
* **Letting `remediation_attempt_failed` decide the tail.** Simpler, and arguably right — a failed
  remediation is exactly when a human should look. Rejected under decision 7 because the signal is
  permanently true: the policy would stop being a policy and become "escalate after the first failed
  attempt", which contradicts ADR 0056's whole purpose and would be reported as an adaptive finding.
  The owner can have that behaviour deliberately by setting `UNCERTAINTY_FAILED_ATTEMPT_COUNT` above
  the attempts a run may make, which turns the signal off rather than turning it into the tail.
* **A `ladder` knob (rung order from configuration).** Plan 03 § 8 does list two adaptive variants
  (`baseline → N4 → selector` and `baseline → N4 → search`). Both are reachable today by the mode:
  the second is what recorded mode runs. A configurable order would multiply the arms a report has to
  keep apart, for a variant nobody has asked to measure yet.
* **Rendering evidence ids on the baseline rung too, so every rung sees one context.** Rejected for
  ADR 0044's reason, at the moment that decision is hardest to revisit: it would move the control
  group by an amount only a paid run could measure, and O-22 forbids one. The arm stamps
  `evidence_ids_rendered: "baseline rung no, every rung above it yes"`, and the rung a step
  terminated on is on the record, so a table can split the arm before comparing it.

## Consequences

* `adaptive` is off by default (`INFERENCE_STRATEGY=baseline`), nothing under `evals/` selects it,
  and the canned suite is unmoved — `make eval-reg` stays 53/53 with no grade moved.
* The declared `top1_confidence_floor` (0.75) sits above the loop's 0.7 remediate bar, so under the
  declared defaults an `adaptive` run cannot emit a remediation the gate would refuse: a diagnosis
  between the two lands on the tail rung instead. The gate is unchanged and still runs; it is simply
  no longer the thing that refuses. A test pins both halves.
* After a failed remediation, every later step climbs at least two rungs. That is a real cost, it is
  on the record rung by rung, and the deferred sweep is what prices it.
* **A sweep of this arm has to seed the ledger for its worst case, not its typical one.** A climbing
  step bills up to three calls plus a walk where `baseline` bills one, so an adaptive run metered
  against the control group's token and dollar ceilings can exhaust mid-investigation and escalate —
  and the report would read that as the *strategy* failing rather than as the budget refusing to fund
  it (decision C4, the lesson best-of-N already cost). `TOKEN_BUDGET_MULTIPLIER` and
  `USD_BUDGET_MULTIPLIER` are the instrument, applied once where `factory.start_run` seeds the
  ledger; tool calls stay unmultiplied, because probing the world is what the arms compete on.
* The `search` rung is measurable in recorded mode only, so the frontier's top rung is a
  recorded-world number. Canned and live rows show a three-rung ladder with a stop.
* The aggregate research report gains two sections — the accuracy/cost frontier and the terminating
  rung distribution by difficulty — which say they are not measurable until an archive with an
  `adaptive` arm is in scope. The sweep that produces one is a deferred paid run (O-22), and so is
  the Phase 13 close.
* WP-13.1's consequence that "nothing under `src/` imports the policy module yet" is now closed: this
  arm is its first reader, and the test that pinned the absence pins the reader instead.
