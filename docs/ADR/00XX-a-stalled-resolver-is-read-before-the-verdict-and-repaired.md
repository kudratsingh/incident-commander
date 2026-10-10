# ADR 00XX: A stuck chain's resolver is read before any verdict, and a stopped one is repaired

* Status: accepted
* Date: 2026-10-10
* Decider: Kudrat Singh (owner rulings O-48 "fix" and O-49 "a", WO-R3-372, INC-008)
* Related: [ADR 0053](0053-family-c-is-one-chain-under-four-faults.md) (the `workflow_stuck`
  family, whose `resolver_stall` world this record turns from a handoff into a repair),
  [ADR 0078](0078-the-alerts-breadth-sets-the-burden-of-proof.md) (the required-reads mechanism,
  extended here to a read pinned to one resource), [ADR 0032](0032-the-action-must-address-the-alerts-subject.md)
  and [ADR 0070](0070-a-chain-action-may-name-the-node-the-alerted-chains-own-reading-names.md)
  (the subject guard, amended here a second time), [ADR 0054](0054-one-rule-for-a-stuck-chains-root-rendered-into-every-reader.md)
  (one shared sentence, every reader), [ADR 0056](0056-a-retry-earns-its-attempt-by-reinvestigating.md)
  (an identical second call is refused), [ADR 0077](0077-resolved-needs-a-reading-taken-after-the-action.md)
  (RESOLVED needs a post-action reading), platform ADR 0041 (v0.6.24: the resolver's poll time
  and the background loops are readable), INC-008 in `audit-ws/context/INCIDENTS.md`

## Context

`workflow_stuck_resolver_stall` seeds one chain — root and parent `completed`, two children
`waiting` for 47 minutes, nothing dead-lettered, nothing paused — and holds it with two hooks:
the `dependency-resolver` consumer group is stopped and the `resume_unblocked_waiting` sweep
that backstops it is paused. Its ground truth is `resolver_stall`; its nearest neighbour label is
`saga_coordinator_stall`. Four live runs answered it 1 right in 4 on the SAME eight reads
(INC-008): nothing the agent could read separated the two components, so the label was a coin
flip. The owner chose to fix the world rather than retry (O-48).

Platform v0.6.24 (platform ADR 0041) made both halves readable.
`get_consumer_lag(consumer_group="dependency-resolver")` now answers `source: live` and carries
`last_poll_at` / `last_poll_age_seconds`; a polling consumer's poll age stays within a few
seconds of `age_seconds`, a stopped one's grows one second per second — while its LAG reads 0,
known and fresh, exactly like a healthy idle resolver's. `get_control_loops` reports every
background loop, `resume_unblocked_waiting` among them, with `paused` and its expiry. The
commander re-pinned to it in cmd #361.

With the resolver readable, the owner ruled the scenario a repair (O-49, "a"): the graded action
is `restart_consumer_group(dependency-resolver)`, verified by the resolver polling again; the
paused sweep has no tool, so it is named in the briefing with its expiry and left to run out.

## Decision

### 1. The family reads the resolver BY NAME and the loops before any verdict

`FAMILY_REQUIRED_BEFORE_VERDICT["workflow_stuck"]` becomes three reads, all `when: stuck_chain`:
the whole dead-letter queue (ADR 0078, unchanged), `get_consumer_lag` pinned to
`consumer_group: dependency-resolver`, and `get_control_loops`. The five worlds inherit it, so the
declaration is identical in all five and names no world's answer.

`RequiredReading` gains `arguments` — the values a read must be MADE WITH, compared as they go on
the wire — because one tool answers for every consumer group and the dispatcher's lag is not the
resolver's. An unknown or blank argument is refused when the scenario loads (a read that can
never be made would refuse every verdict). The loop's narrowed step still binds only the TOOL
(`only_probes`); the group is checked by the loop before the call goes out, costs a refusal like
any other probe that pays nothing, and the refusal names the exact read
(`get_consumer_lag(consumer_group=dependency-resolver) counts only with exactly
consumer_group='dependency-resolver'`). The ledger row's `missing_reads` renders a pinned read
with its arguments and an unpinned one exactly as before, so every existing archive and test
reads the same.

The dossier derives the required reads as expected probes, and its value pool takes the pinned
group from the declaration — so every family world's recording carries the resolver read, not
only the world whose chaos plan names the group.

### 2. `resolver_stall` routes to `restart_consumer_group`

`FIX_MAP[RESOLVER_STALL] = "restart_consumer_group"`: the first category promoted out of WP-1.6's
escalate-only set. That rule said a promotion is "its own packet — it needs the scenario that
grades the action"; `tests/unit/test_policies.py::TestEveryNewCategoryIsEscalateOnly` now keeps
a `_PROMOTED` table and holds each entry to it (the category routes at the tool, a scenario with
that ground truth resolves with that tool as its sanctioned action and grades the argument).
`saga_coordinator_stall` stays escalate-only: no reading shows the coordinator, so a restart of
it could never be verified.

### 3. A chain alert's action may name the resolver, once the run has read it

The alert names a chain (`job_id`), so ADR 0032's guard refused any action not aimed at that
chain, and ADR 0070 widened it only to the chain's own nodes. The resolver is not a node.
`remediation.CHAIN_SERVICES_FOR_SUBJECT` declares, for a `get_dag_state` subject, ONE service:
`restart_consumer_group(consumer_group="dependency-resolver")`, admitted only when this run holds
a `get_consumer_lag(consumer_group="dependency-resolver")` reading. Any other group — the
dispatcher, `saga-coordinator` — is still refused, read or not, and an alert about a consumer
group gains nothing (the table is keyed on the subject's own read).

The rehearsal found a second gate (archive `d70a7429f182`): B-08 drops a reading's echo of its own
argument from the evidence value corpus, and the resolver's name reaches a run ONLY as that echo,
so the restart was refused as "unsourced". The declared name is the commander's own constant,
not a value a model typed, so `_declared_services_read` adds it to the corpus once the run has
read the service; a model's retyping (`dependency_resolver`) is still unsourced.

### 4. The scenario becomes a repair; the other four worlds keep their verdicts

`workflow_stuck_resolver_stall` now expects `resolved`, one `restart_consumer_group` with
`consumer_group` exactly `dependency-resolver` (graded universally; "exactly once" is that claim
plus ADR 0056's refusal of an identical second call), the other six Tier-1 tools forbidden, the
four furniture rows still in `forbidden_replay_job_ids`. Its evidence claims gain the resolver
read BEFORE the action (`last_poll_age_seconds at_least 20`, the precondition's own threshold),
the loop read (`resume_unblocked_waiting` `paused: true`), the restart's `kill_key_cleared`, and
the verify claim: the resolver's `last_poll_age_seconds at_most 15` on the LAST reading after the
restart. The briefing must name `resume_unblocked_waiting`. The precondition gains the resolver
reading (polling stopped) and the loop reading (paused). Ground truth, causal chain included, is
unchanged: the label was always right, the reading was missing.

Every correct verify shape (PROTOCOL step 4): only one read can observe the action
(`VERIFY_PROBE_FOR_ACTION`, enforced by the plan guard), so there is one CALL shape and no
`any_of` to write. What varies is the timing — measured on the rehearsal, the poll taken the
instant after the restart still reads the frozen value (20 s) because the supervisor re-spawns
the consumer on a 2 s tick and the next 5 s metrics pass publishes the poll time; the poll 20 s
later reads 2. `which: last` with `after_tools` grades whichever poll the run verified on, and a
run that resolves on the frozen reading fails it.

What a resolved run does NOT claim is that the chain drained: the parent's `job.completed` was
consumed before the chain was built, so a restarted resolver has nothing that promotes step-1, and
the children move when the sweep's pause expires. RESOLVED means the component the agent can
repair is repaired and verified; the briefing carries the rest.

The other four worlds keep their verdicts, because the reads that turned this world into a repair
say nothing new about theirs: in each the resolver reads polling and no loop is paused (their new
canned readings are transcribed from the v0.6.24 recordings), so `runaway_saga` still routes to a
replay of the dead root, `poison_message` to a fence of the dead descendant, `dag_paused` to a
handoff naming a pause no tool lifts, and `no_fault` to a handoff with nothing done. The family's
"one chain, five faults" property (ADR 0053) is untouched: same alert, same declaration, five
answers. Each canned script makes the two new reads before its verdict, and each grade is unchanged.

### 5. One shared sentence, four readers

`shared_rules.STALLED_CHAIN_RULE` (`{{rule:stalled_chain}}`, ADR 0054's mechanism) says, in one
sentence, what holds a chain whose own rows are healthy, which reading shows each half, that the
resolver is restarted and verified on its poll age, that the paused sweep is named with its expiry
and left, and that `saga_coordinator_stall` is only what remains once both are ruled out. Its
readers are the investigation planner, the remediation planner, the briefing writer and the
briefing judge (INC-002: every reader of a reading gets the same rule). The planner's category
table gives `resolver_stall` its fix, and the stuck-chain verdict sentence names the two new
reads. The two category docstrings in `hypothesis.py` say the same thing.

### 6. Closed phase reports keep the routing of record

`phase_close_report` read `FIX_MAP` from today's tree, so promoting a category would have
rewritten the Phase 1 and Phase 2 close documents (invariant 9). `PhaseScope.fix_map_of_record`
pins the map those runs met (`FIX_MAP_BEFORE_O49`); a later phase defaults to today's map.

## Considered alternatives

**Keep the scenario escalate-only and only require the reads.** It would fix the coin flip, but
the owner ruled the repair (O-49): with the resolver readable, a stopped consumer the agent can
restart and verify is a Tier-1 fix, and grading a handoff there would grade the agent for not
acting on an answer it can prove.

**Require the reads only for a resolver-or-coordinator verdict.** Narrower, and it would leave
the declaration different from what O-44 part 3 set: a page that could be any of five faults
requires the full sweep before ANY verdict. The reads cost two calls; every world's budget covers
them.

**Admit any consumer group the run has read.** Rejected: the subject guard exists because two
live runs read the right resource and acted on another; "any group I looked at" would readmit
that. One declared service, keyed on the chain subject, is the narrowest rule that lets the
repair through.

**A disjunction of verify shapes.** There is one call shape; an `any_of` with a second, invented
member would loosen the claim, which PROTOCOL step 4 forbids.

## Consequences

* **One canned grade moves by design**: `workflow_stuck_resolver_stall` goes `escalated` →
  `resolved` (all dimensions pass both before and after). Every other row is unchanged. The
  baseline is not re-blessed here; the coordinator blesses it.
* **A live run of the family spends two more reads.** Every world's `max_tool_calls` covers them;
  `resolver_stall`'s rises 8 → 17 for the action and up to six verify polls.
* **The four INC-006/INC-008 archives stay as graded.** The rule is on the agent and the
  scenario; finished trajectories are not re-graded against a world they did not see.
* **The paid re-run is deferred** (≈ $0.25, its own yes): `make eval-live
  ONLY=workflow_stuck_resolver_stall MODEL_ROLE=benchmark`.

## How to verify

* `tests/unit/test_resolver_stall_repair.py` — the pinned read, the admission and its limits, the
  declared-name sourcing, the red (the coin flip concludes with no action when only the queue is
  owed) and the green (refused, then the reads, the restart, the verify, RESOLVED), the recordings
  and the dossier.
* `tests/unit/test_required_reads.py`, `test_policies.py::TestEveryNewCategoryIsEscalateOnly`,
  `test_prompts_snapshot.py::TestTheStalledChainRuleReachesEveryReader`.
* `make eval-reg` — 67/67; only `workflow_stuck_resolver_stall`'s terminal state moves.
* Rehearsal archive `11b24360e455` (live platform, scripted planner, $0): the reads, one restart,
  verify poll 1 frozen (20 s), poll 2 polling (2 s), RESOLVED, every dimension PASS.
