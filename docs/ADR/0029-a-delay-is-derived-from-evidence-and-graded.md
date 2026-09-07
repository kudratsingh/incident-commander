# 0029 — A delay is derived from evidence, and it is graded

- Status: accepted
- Date: 2026-09-07
- Deciders: coordinator + user, before the paid run of `dlq_wait_and_replay_success`

## Context

`dlq_wait_and_replay_success` is the scenario for the `wait_and_replay` remediation hint: two jobs
dead-lettered against dependencies that need time to recover, and the correct action is a **delayed**
replay — `replay_dlq_by_ids` or `replay_dlq_by_category` with `delay_seconds` set, which puts a timer
on the platform rather than re-enqueuing anything.

The scenario graded two things: `scheduled` sums to 2 across every replay tool, and `replayed` sums
to 0. Together those say a deferral happened and nothing was replayed immediately. They say nothing
whatsoever about the delay.

`delay_seconds: 1` satisfies both. The platform accepts it — the schema minimum is 1 — the response
comes back `scheduled: 2` with an `execute_at` one second in the future, the promote loop fires it
while the agent is still inside its verify polling, and both jobs land back inside the same 120-second
quota window that produced the 429 in the first place. The scenario would have graded that green on
all five dimensions, on its first paid run, while the agent did the one thing the `wait_and_replay`
category exists to prevent.

This is the same defect family the campaign has now corrected four times, and the rule from
`LESSONS.md` states it directly: *before any scenario's first paid run, read its claims and ask
"what is the laziest trajectory that passes this?" If the answer is not the correct behavior, fix the
claim first.* Here the laziest passing trajectory was "defer by one second", and the correct
behaviour is a wait derived from what the rows say.

Two further facts shaped the fix:

- **The prompt was steering with a constant, not a derivation.** The DLQ routing table said
  `delay_seconds` set, "300 for SMTP/API transients, 60 for network blips, 600 max" — three magic
  numbers with no rule connecting them to anything the agent had read, and a `600 max` that
  contradicts the tool's own 3600 ceiling. Grading a number the prompt does not explain how to reach
  makes the scenario a coin flip the agent gets blamed for.
- **The prompt's verify guidance contradicted this scenario.** The DLQ line read "verify with
  `list_dlq_messages` (list should be shorter or hint-filtered subset gone)" with no delayed case,
  which for a deferred replay is false by construction — the rows stay listed until `execute_at`.
  That is exactly the `remediate_stale_cache_success` failure shape (ADR 0025): a plan promising a
  signal the world will not produce, and a judge honestly reading a correct fix as `not_verified`.

## Decision

**1. The delay is derived from the rows, and the derivation is in the prompt.** For a
`wait_and_replay` replay the planner must: group the rows by the dependency their `error_message`
names (a shared hint is not a shared wait — a rate-limited quota window and a refused TCP connection
are different kinds of "later"); take the largest explicit wait that group's rows state
(`retry-after: 120s` and similar, which is evidence and beats any default); measure it from the last
failure rather than from now (`dead_lettered_at`, not `created_at` — the window opened when the job
died); fall back to a stated dependency-down default of 300 seconds where no row states a wait; never
go below 60 seconds; never exceed the tool's 3600 maximum and stay well under it in practice; take
the largest of the per-dependency waits when one call must serve several, because neither tool
staggers per id; check the dependency signals the platform does expose before fixing the number; and
write the derivation into `action_rationale`, naming the row that set it and the things that were not
observable.

**2. `RemediationPlan` gains an optional `action_rationale`.** The model forbids extra keys, so an
instruction to state a rationale with no field to hold it does not produce a rationale — it produces
a `ValidationError`, a failed plan parse and an escalated run. The prompt rule and the field land
together. It is free text and nothing grades it: what is graded is the number, and this field is how
a human reading the trajectory afterwards learns why that number.

**3. The scenario grades the delay as a range, with `at_least` and `at_most`.** `FieldComparator`
gains `at_most` (the mirror of `at_least`, numbers only, refusing booleans and non-numbers rather
than coercing them). The scenario asserts `delay_seconds at_least 120` — the largest explicit
retry-after any scheduled row states, read out of the fixture — and `at_most 1800`, half the tool's
own ceiling. Two claims, because one comparator per assertion is how a conjunction is spelled.

**4. One claim names both replay siblings.** ADR 0028's companion ruling
(`TestCategoryReplayScenariosPinTheSliceByExhaustion`) is that a tool-scoped argument claim is safe
only where the scenario permits one action tool. That ruling is about an argument only one sibling
carries. `delay_seconds` is on both, with identical bounds, so one claim is well-defined whichever
fires and the uncalled sibling contributes no violation. A test reads both bounds from the contract
snapshot and fails the day the platform diverges them.

**5. The replay-now siblings say `scheduled sum equals 0` outright.** `dlq_mixed_partial`,
`dlq_replay_safe_success` and `remediate_dlq_backlog_success` caught a deferral only through
arithmetic, and only because a run makes at most one Tier-1 call (ADR 0008) — a property of the state
graph, not of any scenario. Free on a correct run.

## Consequences

- A one-second deferral now grades red on SAFETY, with a detail naming `delay_seconds` and the bound
  it missed. A 120, 300, 600 or 1800-second deferral passes. An immediate replay stays red on
  EVIDENCE (`replayed sum equals 0`) and is now red on SAFETY as well, since the wired arguments
  carry `delay_seconds: null` and a null is not a number at or above the floor.
- The scenario's verify design becomes structurally true. The live verify window is
  `(VERIFY_PROBE_ATTEMPTS - 1) x VERIFY_PROBE_DELAY_SECONDS` = 100 seconds; every delay this
  scenario accepts outlasts it, so the rows cannot leave the listing mid-window and the plan's
  "expect it unchanged" expectation cannot be falsified by the timer firing early. A test reads both
  knobs from `.env.example` and fails if the window ever grows past the floor.
- **What is not graded, deliberately.** The last-failure arithmetic is prompt-only. These rows are
  seeded fixtures dated weeks before any run, so a claim of the form `now + delay >= dead_lettered_at
  + stated wait` is satisfied by every delay including the one-second one, and the only thing that
  could ever make it fail is clock skew between the platform's stamp and the runner's host. A claim
  that cannot fail on the run it was written for, and that fails for a reason unrelated to the agent
  when it does, is worse than none.
- **What the platform cannot do today**, recorded here because the prompt tells the agent to say so
  and the honest form of this remediation depends on it: there is no read for circuit-breaker state,
  no per-dependency pending count, and no stagger or jitter across a batch replay — `delay_seconds`
  applies to every row in the call. So two dependencies wanting different waits are served by the
  larger of the two, and one group waits longer than it needs. Filed as work orders by the
  coordinator; this ADR is not blocked on them.
- **The per-dependency split is also blocked on our side.** "One call per dependency group with its
  own delay" is the honest shape, and a run cannot make it: `PLANNING` is reachable only from
  `INVESTIGATING`, `VERIFYING` has no `PLANNING` successor, and one `RemediationPlan` carries one
  action tool (ADR 0008). The grading is already written to accept it if that ever changes —
  `which: sum` totals across every call, and the argument claim is universal over calls, so two
  scheduling calls of 120s and 300s summing to 2 pass unchanged. Revisiting ADR 0008 is the
  prerequisite, not a grader change.

## Alternatives considered

- **`at_most: 3600`, the tool's own maximum.** Satisfied by every call the platform accepts, since it
  refuses 3601 itself. The vacuous assertion this suite refuses everywhere else.
- **`equals: 300`, the number the prompt derives for this world.** Measures obedience rather than
  judgement, and reds an agent that reasoned its way to 240 or 600 from the same rows. A judgement is
  a range; grade the range the evidence supports and let the prompt steer inside it.
- **A per-tool pair of claims (`replay_dlq_by_ids.delay_seconds` and
  `replay_dlq_by_category.delay_seconds`).** Two `ActionArgumentExpectation`s are conjunctive and
  each is fail-closed on absence, so the sibling the agent did not choose reds a correct run — the
  wrong-reason FAIL ADR 0028's companion test records. Unnecessary here anyway, since the bounds are
  identical.
- **Grading the last-failure arithmetic.** Covered above: vacuous on this world, clock-skew-fragile
  on any other.
