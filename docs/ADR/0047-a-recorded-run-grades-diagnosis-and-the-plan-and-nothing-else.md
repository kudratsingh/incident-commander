# ADR 0047: A recorded run grades diagnosis and the plan, and a recorded result is not evidence until the drift check passes

* Status: accepted
* Date: 2026-09-17
* Decider: WO-R3-198 (plan v2.1 Phase 3, WP-3.3)

## Context and problem statement

[ADR 0043](0043-a-recording-is-keyed-by-what-the-agent-sends.md) made a recorded world and [ADR 0046](0046-a-replay-answers-the-call-that-was-made-at-the-clock-it-is-replayed-at.md) made the client that replays it. This record is about the mode those two exist for, and the mode is a bargain with two halves that have to be stated together.

The half that pays: a recorded run costs one seeding and can then be repeated as many times as wanted, in parallel, against **the same world**. [ADR 0020](0020-one-mutating-scenario-per-live-invocation.md) serialises live mutating scenarios one per invocation, and no two live runs see the same world anyway — the dead-letter clocks move, lag is a cached gauge, the traffic generator writes rows. "Strategy A beat strategy B" measured across two live runs is measured across two worlds. Phases 5, 6, 9, 12 and 13 are all paired comparisons, so without this mode they are unaffordable and, worse, unsound.

The half that has to be paid for: **a recorded run cannot observe an effect.** It has no state to change, no audit log, and nothing that could tell a correct action from a refused one. And unlike a canned run, it looks like a live run from the outside — real model, real platform responses, a real trajectory — so a report that did not say which of its numbers mean anything would be read as if all of them did. The failure this prevents by name is a green SAFETY dimension: "zero unauthorized actions" asserted by a run that could not have taken one, folded into a phase-close report beside numbers that were actually measured. This project has already had one invalid number reported and withdrawn (INC-003), and the cost of that was a $2.15 archive re-graded and a "61%" retracted.

There is a third problem, quieter than both: a recording does not decay visibly. It is a committed JSON file that loads and replays perfectly forever, long after the platform release or fixture change that made the world it describes stop existing.

## Decision

**1. `--mode recorded [--world <id>]` is a third execution mode, stamped as one.**

`ExecutionMode.RECORDED` in the provenance record. A recorded run: builds a `RecordedMCPClient` over one recording, forces the live MCP leg **off** whatever `PLATFORM_MCP_URL` holds, seeds nothing, settles nothing, polls no precondition, tears nothing down, and is parallel-safe because there is no shared world to serialise over. The path to the recording **is** the mode — `run_scenario(recorded_world=<path>)` — so there is no separate flag that could disagree with it.

The CLI refuses the combinations that would let a recorded row be something else: `--mode recorded` with `--live` or `--smoke` (a recorded run's platform is a file; there is no live stage for it to be part of), `--world` without `--mode recorded`, an unrecognised `--mode` value, a selected scenario with **no recording** (never a canned fallback — the row would claim a world it never replayed), and a placeholder `ANTHROPIC_API_KEY` (a canned planner under a recorded label is a fabricated measurement). A pinned `--world` with a wider selection is refused too: one world is one scenario's world.

Recorded mode reads the **real** environment, and that is the whole reason it costs money. The platform is replayed; the planner calls are not. Every recorded run with a real model is a paid run and needs the owner's explicit yes, exactly like a live run.

**2. A recorded run grades diagnosis and the plan. OUTCOME, ACTION and SAFETY are reported not-applicable — never as passes.**

| Dimension | Recorded mode |
|---|---|
| `root_cause` | **graded.** The reason the mode exists (plan 03 § 52). World-scoped by ADR 0040: the booleans come off the **recording's own label**, not off the run's live flags, so a recording of an unseeded world reports *not graded* exactly as its sibling answer key says. |
| `budget` | graded. A ledger is a fact about any run that happened. |
| `evidence` | graded for a read-only run — a replayed read **is** the platform's own answer. Not applicable when the run was truncated (see 3). |
| `outcome` | **not applicable.** The harness stops the run, so the terminal state is not the agent's outcome (plan 02 § 189). |
| `action` | **not applicable.** Nothing is executed against a recording. |
| `safety` | **not applicable.** Safety is graded from the platform audit log as ground truth (invariant 6), and a recording has none. |

A not-applicable dimension **passes** with `DimensionResult.applicable = False` and a detail beginning "not applicable in recorded mode: …". Passing is deliberate: `GradeReport.passed` is an `all()`, so a red would fail every run in the mode for a reason that is not about the agent — the same argument `_grade_root_cause` makes for a not-graded verdict. What makes it safe is that the green is **marked twice**: structurally, by a flag a reader branches on, and textually, by a detail `is_vacuous_detail` recognises, so a recorded report gated against a canned baseline fires the vacated-assertion check instead of passing it.

`MODE_APPLICABLE_DIMENSIONS` is the boundary, enforced in `grade()` rather than trusted: a mode may declare OUTCOME, ACTION, SAFETY and EVIDENCE inapplicable and nothing else. **ROOT_CAUSE above all** — a mode allowed to mark diagnosis inapplicable would turn every run in it green with no verdict inside, and diagnosis is the only thing this mode exists to measure.

**3. A scenario with `expected_action_tools` stops at the `PLANNING` handoff, and the plan is reported rather than graded as an action.**

`RecordedHandoff` replaces the `REMEDIATING` **and** `AWAITING_APPROVAL` transitions on **every** recorded run — not only on the scenarios that declare an action. The declaration says what a run is graded on; what a run *does* is the agent's choice, and an agent that reaches the remediate handoff on a read-only scenario would otherwise hand a Tier-1 call to the replay client. ADR 0046 refuses that call, which is correct and is also a crashed scenario. Replacing the transition makes the refusal *unreachable* rather than merely right: in recorded mode there is no transition that could execute anything.

"Grade the plan" (04:117) lands in the row's `replay.plan` block — the planned action tool, its arguments, the verify tool, and whether the tool is one the scenario expected — **not** in the ACTION dimension, which has to stay silent. A green ACTION in this mode would be indistinguishable from an executed action that worked.

**EVIDENCE is not applicable on a truncated run, and this is a stated divergence from the order's scope** (WO-R3-198 names three dimensions). Two facts force it. Those scenarios' evidence claims read the action tool's **own response** — `remediate_consumer_lag_success` asserts `kill_key_cleared` on `restart_consumer_group`, which no replay can produce. And the handoff writes an evidence entry of its own, so leaving EVIDENCE graded would let harness prose satisfy a scenario's substring claim. Grading it would be red for a reason that is the mode rather than the agent, which is the mis-attribution `INCIDENTS.md` exists to record.

**4. A miss makes a recorded run non-comparable, and the row says so.**

The replay's counters travel on the outcome as `replay` — which recording, its world fingerprint, the replay clock and offset, answered / missed / refused, the recording's coherence findings, and the plan. `degraded` is set when the replay missed or refused anything: the agent took its next step after a tool error the world never produced, so the trajectory is not a trajectory through that world. `degraded` is deliberately **not** set merely because a run replayed a platform — that is the mode, and `execution_mode` is where a reader learns it.

The coherence lints are not re-run: they ran at record time and their findings travel inside the recording (ADR 0043). They are **printed on every recorded run**, because "a recorded world that contradicts itself is a fixture defect, not a benchmark" (plan 02 § 187) and a finding nobody sees is a finding nobody acts on. A finding is not a verdict and does not refuse the run — the dossier's own rule, for the reason its lint docstring gives.

**5. `make world-drift WORLD=<id>` is a required step before any recorded number is reported.**

It re-reads the recording's **own** calls live under the read-scoped principal and diffs them with `evals/fixture_drift.py`'s walk — reused, not reimplemented, because that module already knows which fields legitimately move between two honest observations (`_VOLATILE`) and a second opinion about that would be a second answer to "is this difference real". Zero model tokens. It seeds the recording's hooks when the recording is of a seeded world, because the live platform does not hold the scenario's fault until they fire, and a check that skipped the seeding would report the whole fault as drift every time; it resets afterwards. It inherits `make test-drift`'s ordering constraint unchanged: never after a mutating check.

Exit 1 means **the world moved**, not that the check failed, and the check deliberately does not choose between the three readings (a platform release, a fixture-pack change, a dirty world). Both fingerprints are printed either way: `world_fingerprint` is exact and moves for every platform clock, so it is not the verdict — "the documents differ and nothing meaningful moved" is the normal, healthy outcome.

The step is written into `docs/runbook.md` and asserted by a test, because a check nobody runs is a check that does not exist.

## What was rejected

**A seventh grade dimension for the plan.** The enum is read back against the committed baseline and every archived report, and the plan comparison is one boolean; a schema change across every historical row buys nothing that the `replay.plan` block does not already give a paired comparison.

**Grading the PLANNED action in the ACTION dimension.** It would be a green ACTION on a run that executed nothing — the exact number this record exists to keep out of a report.

**Marking a not-applicable dimension as failed.** Every recorded run would fail, for a reason that is not about the agent, and the roll-up would be unreadable. Marked-and-passing plus a refusal boundary is the honest shape.

**Letting recorded mode fall back to canned fixtures for a scenario with no recording.** That is the `--live` canned-only refusal (exit 8) in a new place: a canned green here is a statement about fixtures, not about a replayed world.

**Truncating only the scenarios that declare `expected_action_tools`.** The declaration is about grading, not about behaviour. A read-only scenario whose agent chooses to remediate would reach the replay client's Tier-1 refusal and crash.

**Writing the drift check's fresh read as a new recording.** A recording is evidence and writing one is `make world-record`'s deliberate act, not a side effect of a check.

## Consequences

* Every later phase's paired comparison has a mode to run in, and a recorded row carries, in one place, both what it measured and what it could not.
* A recorded run of a scenario with an action leg cannot tell you whether the action would have worked. That is not a gap to close later: it is what a replay is. The live confirmation subset (03 § 55 — at least one instance per template confirmed live before a claim is reported) is the other half of every recorded claim, and it is a paid run.
* `DimensionResult` grew `applicable`, defaulting to `True`. Archived reports and the committed baseline keep parsing, and the default falls in the direction that cannot hide coverage: a forgotten flag reads as a real claim.
* The drift check is a live, zero-model operation with an owner's go behind it. A recording whose drift check has never run is a recording whose results should not be quoted — including the four recordings this repo carries today, whose first drift run is a deferred item.
* Nothing in this packet was run with a real model. Recorded mode is proved end to end against the committed recordings with the canned planner, at zero spend; the first real recorded sweep and the Phase 3 close are deferred paid runs.
