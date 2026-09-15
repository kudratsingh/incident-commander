---
status: accepted
date: 2026-09-15
supersedes: none
amends: 0013, 0018, 0020
---

# 37. A scenario's fault is a plan, and the plan is put back; exits 9 and 10

## Context

A scenario declared its fault as one optional `chaos_setup: ChaosHook`, fired once by
`evals/runner.py` before the agent's client was built. Three things that arrangement could
not say, and one it said wrongly.

**It cannot say "two faults".** The benchmark is growing towards multi-fault (Level 5) and
cascading (Level 6) worlds. One field holds one hook, and a cascade is not a set — its
second fault is only the fault it claims to be if the first one has already landed. Order
is part of the declaration, and there was nowhere to declare it.

**It cannot say "wait".** Chaos is not instantaneous. `PreconditionProbe.attempts` /
`delay_seconds` already poll per probe, which covers a lag number that climbs over the
platform's metrics interval. It does not cover a whole plan whose second-order effect
takes platform loop ticks to appear before *any* probe is worth making.

**It cannot say how the world is put back.** There was no teardown path at all. Every
shipped hook is undone today by `ttl_seconds` expiring or by `make eval-reset`, and that
has been enough while every fault was one hook with a TTL. It stops being enough the
moment a plan seeds something whose compensator is a second tool call.

And the thing it said wrongly: **a seeding failure was graded.** `run_scenario` raised, the
suite caught it, and `_crashed_result` produced a `ScenarioOutcome` carrying a
`GradeReport` with `passed=False`. That row entered `failed`, entered the pass rate, and
entered every number derived from the report — so a run the agent never made described the
agent. This is precisely the mistake [the precondition split](../eval-methodology.md)
exists to prevent one step later: `bb1fa70abb4c` graded FAIL and the report said the agent
fixed the wrong thing, when the right thing could not be made to exist.

## Decision

**A scenario's fault is a `ChaosPlan`: `setup` hooks in declared order, `teardown` hooks,
and one `settle_seconds` wait.** Legacy `chaos_setup` is accepted unchanged and normalized
to `ChaosPlan(setup=(hook,))` at load, so no YAML moved and none has to. The two spellings
may not both appear on one scenario, and a plan with no `setup` is refused: a teardown-only
plan compensates a fault nobody seeded, and an empty plan reads as "seeds chaos" to a human
while the predicate says otherwise.

Every hook in a plan, setup and teardown alike, goes through the existing `ChaosHook`
validator against `contracts/platform-tools.snapshot.json` — closed name set, arguments
checked against that entry's `inputSchema`. There is no second, weaker validator for the
teardown half, because that would be a second door into the same write+chaos principal.

**One normalized reader, one predicate.** `Scenario.chaos` returns the plan whichever way
the YAML spelled it; `Scenario.seeds_chaos` answers "does this touch the chaos surface".
Every gate reads those rather than `chaos_setup`: the ADR 0018 smoke refusal, the ADR 0020
mutating-scenario refusal, and the `chaos:invoke` principal guard. A plan-declaring scenario
leaves the legacy field `None`, so a gate reading it directly would silently stop counting a
whole class of scenario — a regression that fails nothing and shows up as a paid run
grading two scenarios against one shared world.

**Runner order:** validate at load → setup in order, stopping at the first failure → record
each hook's result in evaluator-only metadata → wait `settle_seconds` → preconditions →
abort before any model call on an unmet premise, naming which → run the agent → grade →
teardown in a `finally`.

**Setup failure means the world is invalid: the agent is not graded.** The scenario produces
no `GradeReport` at all. It lands in the new `RunReport.ungraded` list with the failing hook
and the platform's own refusal name (out of `error.data.error_code`, so
`poison_fixture_name_in_use` reads as "reset the world" rather than as flakiness), and it is
absent from `total`, `passed` and `failed` alike. The suite keeps running. The invocation
exits **9**.

**Teardown failure is a different event from an agent failure.** The run's grade stands —
it was produced in a valid world — and what is wrong is the *shared environment* the next
run would inherit. Both facts are recorded: `ScenarioOutcome.teardown_error` beside the
grade, and, because the damage reaches the next invocation, a latch on disk at
`evals/.chaos-teardown-block.json`. While that file exists, every `--live` run is refused
with exit **10**, before settings load, before the guards, before any spend. Offline runs
are untouched. Clearing it is `make eval-reset PURGE_IDEMPOTENCY=1` followed by
`uv run python -m evals.runner --clear-chaos-block` — two steps, because the reset is what
restores the world and the second command only records that it happened.

**Teardown is compensators where they exist, plus a bounded TTL, plus the authoritative
reset.** A non-empty `teardown` is therefore not required. Several shipped hooks have no
compensating tool on the platform (`kill_consumer` has no `revive_consumer`), so a schema
demanding one would be satisfied by fiction. What is guaranteed is that a declared teardown
is a real validated invocation and that it runs — on a clean return, on an agent crash, on
an unmet precondition, and after a failed seed.

**The exit-code contract is now 0–10.** Restated whole, the way ADR 0018 restated it:

| Code | Meaning | Emitted by |
|---|---|---|
| 0 | all selected scenarios passed | runner; regression gate clean |
| 1 | ≥1 scenario failed (or regression detected) | runner post-run; regression gate |
| 2 | nothing to compare: no scenario matched `--only`; missing/incomparable report; unfiltered `--live`; bad `--model-role` | runner; regression gate |
| 3 | preflight/env failure | runner, pre-run |
| 4 | principal guard: the token holds the wrong scope for the stage | runner, pre-run |
| 5 | post-stage audit failed or unreadable | runner, post-run |
| 6 | chaos seeding requested under `--smoke` | runner, pre-run |
| 7 | more than one state-mutating scenario in a live selection | runner, pre-run |
| 8 | a canned-only scenario in a live selection | runner, pre-run |
| **9** | **a scenario's fault world could not be seeded; nothing was graded** | **runner, post-run** |
| **10** | **chaos teardown failed (or a prior failure is unresolved): live runs blocked** | **runner, pre-run and post-run** |

Ordering at the end of a run is by blast radius, widest first: 10 outranks 9 outranks 1,
because only 10 reaches the next invocation, and 9 is the one whose natural misreading
("the agent failed") is exactly what did not happen. All three land *after* the archive and
the report are on disk.

## Consequences

**ADR 0020 is unchanged, and the packet's own regression risk is that it would not be.** A
`ChaosPlan` with two hooks is still ONE scenario: it seeds one world and is reset once, and
exit 7 still refuses a selection holding more than one mutating scenario. ADR 0020's
"alternatives considered" rejected a teardown framework as a *substitute* for that
isolation, and this does not make it one — teardown can only undo what it knows about, and
the seeded `replay_safe` row is consumed by the agent, not by a hook. Two tests pin both
directions: a lone two-hook plan runs, and a two-hook plan beside a remediation scenario is
refused with exit 7.

**Reports gain a third number.** "Passed, failed, never ran" — and the third can no longer
be got by subtraction, which is the point. `RunReport.ungraded` defaults to empty so every
archived report and the committed baseline keep parsing, the same back-compatibility
precedent as `degraded_count` and `closing`.

**Setup results are now durable.** `ScenarioOutcome.chaos_hooks` records what each hook did,
including the hook's own response, so the archive answers "what world was this graded in?"
without a trace file — previously answerable only when tracing happened to be on.
Evaluator-only: it is never put on `RunState`, on the evidence ledger, or into a prompt.

**The latch is operational state, not evidence.** It has two transitions and lives outside
`evals/runs/`; invariant 9 covers the append-only record, which is the report row, not the
latch. Its clearing is a separate runner invocation rather than a step inside
`make eval-reset` because the Makefile was owned by a concurrent editor when this landed;
folding the clear into that recipe is a one-line follow-up and would not change this
decision.

**A future code 11+ extends this ADR the same way** ADR 0018 and ADR 0020 were extended,
rather than editing it.

## Alternatives considered

**Rename `chaos_setup` to `chaos_plan`.** Rejected. `Scenario` is `extra="forbid"`, so a
rename is a 40-YAML migration plus a simultaneous edit of every reader —
`evals/dossier.py`, `evals/inventory.py`, `evals/fixture_drift.py` — for no behavioural
gain. Additive plus a normalizing property keeps the corpus untouched and gives downstream
readers one shape to migrate to at their own pace.

**Grade a setup failure red and mark it.** Rejected: any marking still leaves the row in
the failed count unless every reader is taught about the mark, and the readers include the
regression gate, the baseline and the archived reports. Absence is the only representation
a naive reader cannot get wrong.

**Abort the whole suite on a setup failure.** Rejected: it would discard the aggregate
report for the scenarios that had already run, and under ADR 0020 a live invocation holds
at most one mutating scenario anyway, so aborting buys nothing the ungraded row does not.

**An in-memory teardown flag.** Rejected outright — the thing it protects is the *next*
process. A flag that dies with the run protects nothing.

**Raise from the teardown.** Rejected: on a crashed run it would replace the agent's own
cause with the janitor's, and on a clean run it would throw away a valid grade to report an
environment problem. Both facts are kept, side by side.
