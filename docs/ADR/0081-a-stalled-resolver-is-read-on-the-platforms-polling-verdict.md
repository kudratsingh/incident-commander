# ADR 0081: A stalled resolver is read and graded on the platform's `polling` verdict, and a paused backstop never explains it

* Status: accepted
* Date: 2026-10-10
* Decider: Kudrat Singh (owner decision O-51 "ok go", WO-R3-374, INC-008 addendum)
* Amends: [ADR 0080](0080-a-stalled-resolver-is-read-before-the-verdict-and-repaired.md) —
  decision 4's evidence, verify and precondition claims on the resolver reading, and decision 5's
  shared sentence and the two category docstrings it names. Everything else in 0080 stands: the
  three required reads, the pinned group, the routing, the subject guard, the repair itself.
* Related: [ADR 0054](0054-one-rule-for-a-stuck-chains-root-rendered-into-every-reader.md) (one
  shared sentence, every reader), [ADR 0053](0053-family-c-is-one-chain-under-four-faults.md) (the
  `workflow_stuck` family), platform ADR 0041 and its 2026-10-10 amendment (v0.6.25: `polling` and
  `poll_interval_seconds` on `get_consumer_lag`), INC-008 and its addendum in
  `audit-ws/context/INCIDENTS.md`, LESSONS 2026-10-10 "a readable fact is not a legible one"

## Context

ADR 0080 made the stranded world's label observable: since platform v0.6.24 the agent can read the
`dependency-resolver` group's poll time and the background loops. It graded the world on that
number — `last_poll_age_seconds at_least 20` before the restart, `at_most 15` after — and wrote the
shared rule in the same terms ("`last_poll_age_seconds` far above its `age_seconds` means the
resolver stopped polling").

The first paid run on it (archive `daea4943f3a5`, $0.33, the INC-008 addendum) made every read the
family owes. The resolver read `lag 0, lag_known true, source live, age_seconds 3,
last_poll_age_seconds 39` — dead for half a minute — and the planner wrote "the dependency-resolver
is polling (lag=0, last_poll_age=39s) … somewhat elevated but not conclusive". Then
`get_control_loops` showed `resume_unblocked_waiting paused: true, 851 s to expiry`, a crisp fact
with a clock, and the planner anchored on it: `dag_paused` 0.75, then `saga_coordinator_stall`
0.85, escalated, nothing restarted. A number the reader must threshold against another number, with
no stated cadence, lost to a boolean with an expiry.

The owner ruled the fix structural (O-51): the platform derives the verdict. v0.6.25 (platform ADR
0041 amendment, WO-R3-373) adds `polling` — `false` when, at the metrics pass's own look, the
consumer had not polled for more than 5 × `poll_interval_seconds` (2.0 s, so a 10 s line) — and the
tool description says "a dead consumer reads `polling: false` while `lag`, `lag_known`, `source` and
`age_seconds` can all still look healthy". The commander re-pinned to it in cmd #364, and every
canned `get_consumer_lag` reading carries the field. This record is the commander's half: what the
model is told, and what the world is graded on.

## Decision

### 1. The rule the model reads names the verdict and says which way it points

`shared_rules.STALLED_CHAIN_RULE` (`{{rule:stalled_chain}}`, the same four readers — both planners,
the briefing writer, the briefing judge) is rewritten, still one sentence:

* read `polling` on `get_consumer_lag(consumer_group="dependency-resolver")` FIRST;
* `polling: false` means the resolver has stopped, whatever its `lag`, `lag_known`, `source` and
  `age_seconds` say; `last_poll_age_seconds` is only the number behind the verdict;
* that is `resolver_stall`, fixed with `restart_consumer_group` on `dependency-resolver` and
  verified by a reading taken after the restart that says `polling: true`;
* `get_control_loops` reading `resume_unblocked_waiting` `paused: true` is the backstop held too,
  and **a paused backstop never explains a stalled primary consumer** — it is not the cause while
  the resolver reads `polling: false`, and it is not `dag_paused`, which is the chain's own
  `get_dag_state` `paused: true` — so the resolver is still restarted and the held sweep is named
  with its `paused_expires_in_seconds` and left to expire;
* only when the resolver reads `polling: true` and the sweep runs is `saga_coordinator_stall` what
  remains.

The planner's category table says the same in three rows (`resolver_stall` reads `polling: false`
whatever its lag says; `saga_coordinator_stall` never while the resolver reads `polling: false`;
`dag_paused` is the chain's own pause, not a loop), and the remediation planner writes its verify
expectation as `polling: true` on a reading taken after the restart. The three category docstrings
in `hypothesis.py` say it in the same terms. Four prompt hashes move, once.

The words are aimed at the two mistakes the archive shows, in the order it made them: the number
read as "polling", and the paused loop read as the cause (first as `dag_paused`, a label about a
different field entirely, then as the coordinator).

### 2. The world is graded on the verdict

`workflow_stuck_resolver_stall`'s three resolver claims change; nothing else 0080 graded moves.

| Claim | Before (0080) | After |
|---|---|---|
| Evidence, before the restart | `last_poll_age_seconds at_least 20` | `polling equals false` |
| Verify, last reading after the restart | `last_poll_age_seconds at_most 15` | `any_of`: `polling equals true` / `last_poll_age_seconds at_most 15` |
| Precondition | `source: live`, `last_poll_age_seconds at_least 20` | the same, plus `polling equals false` |

**Every correct shape, and why each claim holds for it (PROTOCOL step 4).** There is one call shape
on each side of the action — `get_consumer_lag` on the resolver (`call_arguments`), cut at the
restart (`before_tools` / `after_tools`) — so the question is which fields describe it honestly.

* **Before the restart, the verdict alone.** Every correct run reads the resolver after the
  precondition has proved it dead, and nothing revives it before the restart, so `polling: false`
  holds on every such reading. The age threshold is NOT kept beside it as an `any_of`: it is not a
  second shape of "dead" but a proxy for this stack's 5 s metrics pass. At the platform's default
  60 s pass a living consumer reads ages up to about 70 with `polling: true`, so as an alternative
  the age would admit a resolver that never stopped — a disjunction with it loosens the claim.
* **After the restart, the verdict or a fresh poll.** The age member stays, as the brief asked,
  because there it is honest: the precondition proves the frozen poll time at least 20 s old before
  the run starts and it only ages from there, so an age of 15 or less on a post-restart reading can
  only be a poll the restarted consumer made. And it can only err the safe way — on a slow pass a
  polling consumer's age can exceed 15 (a correct run failing that member), which the verdict
  member covers. A run that resolves on the frozen reading (`polling: false`, age 20 and climbing)
  fails both members. The verdict member is listed first; the grader names which member held.
* **The precondition keeps the age** for exactly that reason: it is what makes the verify group's
  age member honest. It gains the verdict so the world is refused before any spend if the platform
  ever reads the killed resolver as polling.

`which: last` and the cut at the action are unchanged from 0080, as is the measured timing: the poll
taken the instant after the restart still reads the frozen verdict (`polling: false`), the next one
reads `polling: true` (platform ADR 0041 amendment, "while the supervisor restarts a consumer it is
not polling").

### 3. The fixtures and the recordings carry the verdict

The canned readings already carry both fields (written by the platform's own rule at the re-pin,
cmd #364): `polling: false` on resolver_stall's investigation read and on its first verify poll,
`true` on the second, `true` on the resolver reading of each of the four siblings. The five family
worlds are re-recorded on v0.6.25 so each recording carries the field as the platform reported it,
and each is drift-checked against the live stack: all five CLEAN in
`world_drift_table.20261010T190122Z.0f08672ecce9`. No canned value moved — every recording agrees
with the reading the re-pin wrote by the platform's rule.

## Considered alternatives

**Keep both age claims and only change the words.** The words are what failed in the archive, so
they had to change; but a world graded on a threshold the model is told NOT to use would grade a
correct run on a different fact from the one it was taught to read, and at a slower metrics pass it
would grade a restart of a healthy consumer as correct.

**A loop refusal: refuse `saga_coordinator_stall` or `dag_paused` while the newest resolver reading
says `polling: false`.** The structural form of decision 1, and it would make the paid re-run's
result less dependent on the words. Not built here: the order is the rule and the claims (O-51,
WO-R3-374's fix shape), the platform's verdict field is the structural half of this fix, and a new
refusal is its own decision with its own failure modes (a stale reading taken before a real
recovery). It is the next step if the re-run misreads the verdict too. Today the wrong label cannot
PASS — it ends ESCALATED with OUTCOME, ROOT_CAUSE and ACTION red, and a `remediate` on the
coordinator has no Tier-1 route — but it is not refused.

**Grade the verify on the verdict alone.** Strictly tighter, and also honest. Not taken because the
age member is honest after the restart (above) and the brief asked for it to stay as an `any_of`;
dropping it would change nothing about any run this world can produce on the demo stack.

## Consequences

* **No canned grade moves.** The canned world already carried the verdict; the scenario's own
  script resolves with every dimension passing before and after. `make eval-reg` stays 67/67.
* **Two grading outcomes change, both on readings this world does not produce today** (pinned by
  `tests/unit/test_resolver_stall_polling.py`): a restart of a resolver whose pre-action reading
  says `polling: true` at an age of 40 fails EVIDENCE now and passed before; a verify reading of
  `polling: true` at an age of 18 passes now and failed before.
* **The paid re-run is unchanged in cost and command** (≈ $0.35, its own yes; O-52 pre-authorises
  it once after this merges): `make eval-live ONLY=workflow_stuck_resolver_stall
  MODEL_ROLE=benchmark`.
* **The INC-008 archives stay as graded.** Finished trajectories are not re-graded against a claim
  they did not meet.

## How to verify

* `tests/unit/test_resolver_stall_polling.py` — the claims, the two grading outcomes above, the
  frozen reading failing both members, the scripted planner that concludes `saga_coordinator_stall`
  or `dag_paused` with `polling: false` in hand (cannot pass) and the one that restarts and
  verifies (passes), the table rows, the docstrings, the canned readings and the v0.6.25 recordings.
* `tests/unit/test_prompts_snapshot.py::TestTheStalledChainRuleReachesEveryReader` — the rule reads
  `polling` first and says a paused backstop never explains a stalled primary consumer.
* Rehearsal archive `26c28af9321b` (live platform v0.6.25, scripted planner, $0): the pre-action
  resolver read `polling: false` (age 20 against 2), verify poll 1 the frozen `polling: false`
  (`not_verified`), poll 2 `polling: true` (age 1); RESOLVED in 7 tool calls, every dimension
  PASS, the verify group satisfied by its verdict member.
* The five v0.6.25 recordings and their drift checks: table
  `world_drift_table.20261010T190122Z.0f08672ecce9` (5 CLEAN, 12 NOT RUN).
