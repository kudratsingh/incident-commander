# ADR 0018: The runner's exit-code contract is 0–6; exit 6 is chaos seeding refused under `--smoke`

* Status: accepted
* Date: 2026-08-10
* Decider: Kudrat Singh

## Context and problem statement

[ADR 0013](0013-run-provenance-is-part-of-the-eval-result.md) enumerated the runner's exit-code
contract as codes 0–5 and made that table "the full contract in one place". It was, when written.

It stopped being complete during the same campaign. WO-C6-05 (the P-002 slice, findings S-03/S-04)
closed the hole where the read-only smoke stage performed chaos seeding through the FULL
write+chaos principal — the stage whose entire purpose is to prove read-only. The fix refuses the
run outright rather than seeding: when any selected scenario declares a `chaos_setup` under
`--smoke`, the runner exits **6** before preflight, before the principal guard, and before any
spend. That was the right shape for the fix, but it left the repository asserting two different
things about the same contract: `docs/runbook.md`'s exit table documents 6, and ADR 0013's table
stops at 5.

The cost of leaving that split is concrete and lands on a human, not a machine. Phase 7's
red-reading procedure triages a live run mechanically from its exit code. An operator holding
ADR 0013 as the enumeration meets an exit 6 that the contract does not mention, and the natural
readings are all wrong: an unknown code looks like a crash, or like a harness bug, or gets
pattern-matched onto 5 ("post-stage audit") which points debugging at the audit log instead of at
the scenario selection that actually caused it.

Accepted ADRs in this repo are never edited after acceptance (CLAUDE.md Definition of Done item 4).
So the question is not whether to correct 0013, but how to record the extension without rewriting it.

## Decision drivers

* The exit-code contract must have exactly one authoritative enumeration a human can trust.
* Accepted ADRs are immutable; superseded decisions are marked, never rewritten.
* Exit 6 is already shipped and merged behavior (PR #116) — this ADR documents reality, it does
  not propose a change.
* Phase 7's operator needs the complete table *before* the first live run, not after.

## Considered options

1. **Edit ADR 0013's table in place.** Rejected outright: it violates the never-edit rule, and it
   would make the git history claim 0013 always knew about a code that post-dated it by six PRs.
2. **Supersede ADR 0013 entirely with a new ADR.** Rejected as disproportionate. 0013's substance —
   run provenance is part of the eval result, `--live` refuses a degraded env, the gate consumes
   the new fields — is untouched and correct. Superseding it would retire a still-live decision to
   fix one row of one table, and would orphan the many references to 0013 across the runbook, the
   eval-debt ledger and PR bodies.
3. **Leave the runbook as the sole record of exit 6.** Rejected: it is what we have today, and it
   is precisely the drift this ADR exists to close. The runbook is the operational record, but ADR
   0013 is cited as *the* contract; two records disagreeing is how the Phase 7 operator gets
   misled.
4. **Extend 0013 with a narrow ADR that owns the delta.** Chosen.

## Decision outcome

**ADR 0013's exit-code contract is EXTENDED, not superseded.** Codes 0–5 keep exactly the meanings
0013 gave them. This ADR adds code 6 and restates the complete table so that one document can be
read as the whole contract.

| Code | Meaning | Emitted by |
|---|---|---|
| 0 | all selected scenarios passed | runner; regression gate clean |
| 1 | ≥1 scenario failed (or regression detected) | runner post-run; regression gate |
| 2 | nothing to compare: no scenario matched `--only`; missing/incomparable report | runner; regression gate |
| 3 | preflight/env failure: smoke-without-live, degraded `--live` env, invalid/missing settings, missing smoke token, LLM auth preflight | runner, pre-run |
| 4 | principal guard: smoke token holds more than read scope | runner, pre-run |
| 5 | post-stage audit failed or unreadable | runner, post-run |
| **6** | **chaos seeding requested under `--smoke`: a selected scenario declares `chaos_setup`** | **runner, pre-run** |

Exit 6 is emitted after `--only` filtering and before preflight, the principal guard and any spend.
Placing it after filtering is deliberate: `SMOKE_ONLY` is `.env`-overridable, so the gate has to
judge the scenarios actually selected rather than the ones the Makefile would have selected.

> **Amended by WO-R2-123.** The `SMOKE_ONLY`/`SMOKE_EXCLUDE` lists this ADR describes no longer
> exist. `make eval-smoke` passes no `--only` by default, and the runner derives the selection from
> `Scenario.in_smoke_pass`, which excludes a chaos-declaring scenario by construction. Nothing about
> the placement decision changes — `SMOKE_ONLY` survives as an operator override and still arrives
> as `--only`, so the gate must still judge the selection rather than the tree. What changes is the
> reachability: the default path can no longer produce the condition, and the override is the one
> channel that can.

**How to read exit 6.** It is a *selection* problem, never an environment or platform problem. The
smoke stage was asked to run a scenario that mutates state through the chaos surface. The fix is to
correct the scenario selection (`SMOKE_ONLY` / `--only`), not to re-mint tokens, not to touch the
platform, and never to widen the smoke principal. The documented flow — a bare `make eval-smoke` —
derives a selection that excludes the three chaos-declaring `remediate_*` scenarios and therefore
cannot trip this.

## Consequences

* The exit-code contract has one complete enumeration again: this ADR. ADR 0013 remains accepted
  and correct for codes 0–5 and for everything else it decided; readers arriving at 0013 for the
  contract are pointed here by this ADR's number being the later one, and by `docs/runbook.md`,
  which carries the same seven rows.
* Phase 7's red-reading tree gains a seventh branch, and it is the cheapest one to action: exit 6
  never means the fix regressed.
* A future code 7+ extends this ADR the same way, rather than editing it.

## More information

* Shipped by WO-C6-05 in PR #116 (`9d73718`); the gate lives in `evals/runner.py`'s `main()`,
  upstream of preflight.
* `docs/runbook.md`'s exit table is the operational record and already lists all seven codes.
* Related: [ADR 0013](0013-run-provenance-is-part-of-the-eval-result.md) (the contract this
  extends), [ADR 0011](0011-campaign-eval-freeze.md) (the freeze under which WO-C6-05 merged
  without an eval run).
* Raised during the Phase 6 re-sync as a documentation-coherence defect found by the campaign's own
  completion review, not by a failure.

---

**Amended by [ADR 0020](0020-one-mutating-scenario-per-live-invocation.md):** the contract is now 0–7, and exit 7 refuses a live selection holding more than one state-mutating scenario. Exit 4 also widened — it now covers the *write* principal guard on a live remediation stage, not only the read-only guard under `--smoke`.
