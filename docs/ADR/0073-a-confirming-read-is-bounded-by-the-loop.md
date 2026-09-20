# ADR 0073: A confirming read is bounded by the loop, not by the prompt

* Status: accepted
* Date: 2026-09-20
* Decider: Kudrat Singh (WO-R3-331, INC-004)
* Related: [ADR 0009](0009-investigation-freshness-reprobe.md) (re-read the alerted signal before you
  conclude — the first of the two rules this bounds),
  [ADR 0071](0071-attribution-is-graded-on-the-agents-own-pre-action-read.md) (re-read the
  resource immediately before you act — the second),
  [ADR 0032](0032-the-action-must-address-the-alerts-subject.md) and
  [ADR 0033](0033-a-human-required-chain-root-is-fenced-then-escalated.md) (the refusal shape
  this copies: a marker on the ledger, a refusal that names itself, a narrowed choice),
  [ADR 0041](0041-read-the-whole-queue-before-you-replay-part-of-it.md) (the sibling that
  DEMANDS a read, and the reason this one can never bind `list_dlq_messages`),
  [ADR 0054](0054-one-rule-for-a-stuck-chains-root-rendered-into-every-reader.md) (one rule
  held once and rendered into every reader),
  [ADR 0065](0065-a-briefing-names-its-remainder-in-a-slot-not-in-its-prose.md) (the handoff
  states a fact in a slot rather than leaving it to prose),
  [ADR 0036](0036-the-planner-call-is-the-only-strategy-seam.md) (strategies propose, the loop
  decides), INC-004 in `audit-ws/context/INCIDENTS.md`

## Context and problem statement

The owner's second live demo take (`make demo-live MODE=consumer_outage LIVE=1`, archive
`91617a423b63`, $0.21) killed the `worker-dispatcher` consumer, watched its lag climb 20 → 39,
and got a run that did nothing. All five `investigation_planner` calls ranked
`consumer_saturation` first — 0.80, 0.72, 0.82, 0.85, 0.82 — every one of them at or above the
0.7 remediate threshold, in a category `FIX_MAP` gives a Tier-1 fix. Every one of them emitted
`probe`: three re-reads of the same lag, then `list_dlq_messages`, then `get_circuit_breakers`.
The loop hit `max iterations (5)`, escalated with no action, and the briefing recommended
checking an SMTP relay it had no evidence for. The world, the harness, the reporter and the
console all worked.

Two prompt rules produced it, and both are correct rules:

* **ADR 0009's freshness rule** — "before you conclude, re-read the alerted signal", because one
  static reading of a moving metric is not evidence that it stopped moving.
* **ADR 0071's pre-action rule** — "re-read the resource immediately before you act", because a
  recovery belongs to an action only when the reading before it showed the fault present.

Neither says HOW MANY TIMES. A model applied both as "take one more confirming read" on every
step, which satisfies each of them at every step, forever. Confidence never dipped below the
bar, so ADR 0009's re-probe never fired either — the one existing mechanism that watches a
cached read is armed by a hypothesis DYING, and nothing here died.

Three things follow, and all three are why this record exists.

**Nothing structural bounded the number of reads.** Every other refusal in the loop is about a
read that is MISSING (ADR 0032's alerted subject, ADR 0041's whole queue). There was no shape in
the state machine for a read that is redundant, so the only bound available was the iteration
budget — which is not a bound on confirming reads, it is the thing they consume.

**The canned suite could not catch it.** A scripted planner emits `remediate` on cue, so 63
scenarios all reached the gate. The failure lives in what the model does with a rule, and a
corpus with no scenario about that has no opinion about it.

**The escalation reason named no cause.** `max iterations (5) exceeded` is true and says nothing.
The briefing writer was handed that sentence plus a trail containing the platform's four resting
DLQ rows, one of which mentions an SMTP connection refusal — so the writer had a silence to fill
and filled it with the most concrete thing in front of it. That is the same failure as INC-001's
secondary finding one layer up: a fact the handoff does not state is a fact the writer may
invent around.

## Decision

### 1. The bound is structural and it lives in the loop

`investigation._confirming_read_exhausted` refuses a probe when ALL of the following hold, and
every clause is load-bearing:

1. **The ranking has settled.** The top hypothesis has been a `FIX_MAP` category at or above
   `REMEDIATE_CONFIDENCE_THRESHOLD` for `_SETTLED_RANKING_STEPS` (2) planner steps in a row. One
   step is a ranking; two in a row is a ranking that did not move. The comparison is the
   remediate gate's own (`>=`), so the streak counts exactly the steps on which a `remediate`
   would have been let through. Any step that breaks it resets the count to zero — it is a
   streak, never a total, because a run whose answer is still changing has earned another read.
2. **The probe would read the ALERT'S OWN SUBJECT again** — the same tool with the same value,
   judged by `subject_reads`, the one matching rule ADR 0032's guard also uses. Judged on the
   WIRED arguments, so omitting `consumer_group` (which `wire_arguments` default-fills to the
   alerted group) is not a way past the bound.
3. **The run has already taken `_CONFIRMING_READS_ALLOWED` (2) readings of it**, so the refused
   one is the THIRD. The first read IS the investigation of the alerted signal; the second is the
   confirming re-read both prompt rules ask for; the third is the one no rule asks for.
4. **A declared reading of that subject says the fault is present, the newest one does, and it is
   FRESH.** `FAULT_PRESENT_READING` holds the predicate per read tool and `reading_is_fresh`
   applies ADR 0009's own window (`CACHED_READ_FRESHNESS_SECONDS`) to the reading's own
   `age_seconds`.

The refusal is a refusal and not an escalation, in ADR 0032/0033's shape: a marker on the ledger
(`_probe_refused_confirming_read`, underscore-prefixed so the trail and the grader's tool set
exclude it), a reason that names itself and quotes the reading it is refusing to repeat, and the
state left at INVESTIGATING so the planner keeps its turn. `_MAX_CONFIRMING_READ_REFUSALS` (2)
bounds the refusals themselves, the way `_MAX_SUBJECT_PROBE_REFUSALS` does: a third ask means the
steer is not landing, and the run escalates with the reason in decision 3.

**The refusal offers a narrowed choice, and that is not decoration.** It names `remediate` and
`stop` as the moves that remain for that resource, and says a probe of a DIFFERENT tool is still
open. A refusal that only said "not that read" would leave the planner to guess, and guessing for
five steps is what this record is about.

### 2. The loop refuses; it does not act

The loop knows enough to act — the category, the confidence and the fault-present reading are
exactly the remediate gate's inputs. It still does not. A Tier-1 write the planner never proposed
would be the loop reaching through the seam ADR 0036 draws, and "the agent restarted a consumer
group because its own guard fired" is not a sentence this project wants to be able to say. The
planner acts or hands off; the loop makes sure it cannot do neither indefinitely.

The consequence is worth stating plainly: a scripted planner that never decides still ends the
run escalated. What the bound buys is the budget the redundant reads would have spent, and a
handoff that says what the run concluded.

### 3. `FAULT_PRESENT_READING` is its own map, total over the subject probes

Keyed on the READ tool, with `None` a declared inert entry carrying its reason, and
`tests/unit/test_llm_investigation.py::TestTheFaultPresentReadingMap` pinning totality over every
tool `ALERT_SUBJECT_PROBES` names — so a new alert subject arrives as a decision rather than as
silence. One active entry today (`get_consumer_lag`: `lag_known` true and `lag` not 0), which is
the honest state of it.

It is deliberately NOT `attribution.RECOVERED_READING`, and the asymmetry is the point:

* **Opposite direction, opposite risk.** `RECOVERED_READING` declares `get_consumer_lag` INERT
  because a cached zero may be a measurement taken before the fault existed — on 2026-08-03 a
  stale zero killed a correct diagnosis and bought a wrong remediation. A reading that says "the
  fault is here", inside its own freshness window, carries no such trap: a backlog nobody
  measured does not report as 39. "Recovered" needs a pair of readings; "still broken" needs one
  fresh one.
* **Different question.** That map answers "may this run claim the recovery?" for a briefing and a
  grade. This one answers "is another reading worth a step?" for the loop, before anything is
  claimed.
* **Different import direction.** `agent/attribution.py` imports `agent/investigation.py`, so a
  shared map would have to live in a third module that neither the loop nor the grader owns.

Every "cannot say" answers `None` or `False` and leaves the loop exactly as it was: a reading that
shows the fault gone, one whose age the platform did not report, one outside its window, an
unparseable summary, a subject no entry covers. The guard stops a read that is certainly
redundant; it does not ration reads.

### 4. The exhausted-iteration reason names the ranking, and the briefing carries it in a slot

`_iterations_exhausted_reason` opens with the words it has always opened with —
`max iterations (N) exceeded`, which archives, reports and one test match on — and then names the
top hypothesis with its category and confidence, whether that answer was one the run could have
acted on, and the reads it spent its steps on, counted per tool. It closes with the sentence the
writer needs: that ranking is this run's own conclusion, and no cause outside it was established.
It reaches the handoff through `EscalationBriefing.escalation_reason`, which is ADR 0065's shape —
a fact the template states rather than a fact the writer may choose to state. The same sentence is
reused by the refusal cap's escalation, written once (`_ranking_sentence`,
`_reads_taken_sentence`).

### 5. The prompt clause is secondary, and it is one sentence in two places

`{{rule:confirming_read_bound}}` in `llm/prompts/shared_rules.py`: one fresh reading that shows
the fault is the whole demand, a second is not more evidence, and the state machine refuses a
third. It is rendered into BOTH re-read rules in `investigation_planner.md` and nowhere else —
the first shared rule whose two readers are two RULES rather than two prompts. A clause appended
to one bullet would have left the other saying what it said before INC-004, which is INC-002's
half a rule inside a single file. Exactly one prompt hash moves.

### 6. The canned scenario is the regression, and it reproduces the red

`planner_confirms_forever` (family `harness_control`, difficulty `control`, canned): the alert is
the live take's own, the readings are v0.6.7-shaped with their ages, and the scripted planner
probes the alerted group at all five steps with the live run's own confidences. Before the guard
it takes five readings and escalates with nothing done; after it, two readings land, the third
and fourth asks are refused, and the run hands off naming what it concluded. `max_tool_calls` is
6 rather than 2 deliberately: a budget that stopped the old run would make BUDGET the
discriminator and hide what the scenario is about, so both runs have room to spare and the guard
is the only difference between them.

## Considered alternatives

**Tighten the prompt only** — "take ONE confirming read". Rejected on
`docs/architecture-principles.md` § 3: a rule a model can satisfy forever is the class of bug,
and adding a number to the same prose is the band-aid. It also fails the specific way this
failure failed: the two rules were already correct and already followed.

**Let the loop promote the refused probe into a `remediate`.** Tempting — it would make the demo
end in a restart — and rejected in decision 2. It puts a Tier-1 write behind a guard rather than
behind a planner decision, and it is the one change here that could not be undone by a prompt
edit.

**Bound the total number of probes per run.** That is the iteration budget, which already exists
and is the thing being spent. A smaller one would refuse legitimate investigations of the
scenarios that need four and five reads.

**Refuse EVERY repeat read of the subject after the first.** ADR 0009 and ADR 0071 both ask for a
second, so this would refuse the read the prompt demands and produce the mirror failure: a run
steered into acting on one static reading of a moving metric.

**Count reads instead of steps (drop the settled-ranking clause).** It would fire on a run whose
top hypothesis is still moving, which is the case where a third reading is the most useful thing
available. The streak is what distinguishes "confirming" from "investigating".

**Give the guard a `RECOVERED_READING`-style entry and share one map.** Rejected in decision 3:
the two maps answer different questions in opposite directions, with `get_consumer_lag` active in
one and deliberately inert in the other, and sharing would force the import cycle.

**Make the scenario end in a verified remediation** (the work order's acceptance text).
Impossible in one canned scenario, and the proof is short: canned tool responses are a queue per
TOOL, `restart_consumer_group` must verify with `get_consumer_lag`
(`remediation.VERIFY_PROBE_FOR_ACTION`), and the guard's whole effect is that the post-fix run
takes FEWER lag reads than the pre-fix one. So the post-fix verify and the pre-fix third probe
are the same queue element, and it would have to read "fault present" for the red to be honest
and "drained" for the verify to pass. Reported as a divergence; the resolved path through the
same guard is covered by
`tests/unit/test_llm_investigation.py::TestConfirmingReadBound::test_a_remediate_after_the_refusal_still_hands_off`.

## Consequences

Positive:

* A run cannot spend its whole budget confirming an answer it already has. Two readings, then it
  acts or it hands off.
* The two re-read rules keep their demands and gain a size. "Re-read before X" is now a bounded
  instruction everywhere it appears.
* An exhausted iteration budget hands a human the ranking and the reads rather than an arithmetic
  fact, so a briefing writer with nothing to say has nothing to invent.
* The suite has a scenario about what a model does with a rule, which is the class of failure the
  canned corpus was blind to.

Negative:

* **The guard is inert wherever a reading cannot say the fault is present** — every subject but
  the consumer group today, and any canned fixture without `age_seconds`. That is the safe
  direction and it is also a real limit: the equivalent failure on a cache key or a chain would
  not be caught. Each new entry is a decision with its own reasoning, which is what the totality
  test is for.
* **A planner that ignores the steer still escalates.** The bound makes the run honest and
  cheaper; it cannot make a model decide.
* **One more state in the investigation loop** (a streak and a refusal counter), and one more
  refusal a planner may meet. Both are local to `transition_llm_investigate`.
* **The `investigation_planner` prompt grew**, so a live planner is called with slightly more
  context than before. No canned grade moved, and the corpus is 64 with the new scenario as the
  only delta.
* The pre-fix run of the new scenario is labelled `grader-brittleness` by the runner's failure
  classifier (outcome, action and safety pass; only EVIDENCE fails), which is INC-001's signature
  and here is a true negative: the claims are about the loop's own refusal records, and the
  trajectory shows the third read happening. Worth knowing before that label is trusted.

## More information

* Implemented by WO-R3-331: `_confirming_read_exhausted`, `_refuse_confirming_read`,
  `FAULT_PRESENT_READING`, `reads_fault_present`, `reading_is_fresh`, `subject_reads`,
  `_ranks_an_actionable_answer`, `_ranking_sentence`, `_reads_taken_sentence` and
  `_iterations_exhausted_reason` in `agent/investigation.py`; `CONFIRMING_READ_BOUND_RULE` in
  `llm/prompts/shared_rules.py` and two renderings in `llm/prompts/investigation_planner.md`;
  `evals/scenarios/planner_confirms_forever.yaml`.
* Red-before/green-after on the canned scenario: archive `c3382dd5e330` FAILS without the guard
  (5 tool calls, `age_seconds` last = 11), archive `ad5b2848cc88` PASSES with it (2 tool calls,
  `age_seconds` last = 3). Same five planner steps either way.
* The live archive this is written from is `91617a423b63`, with the human-readable report at
  `evals/reports/human/remediate_consumer_lag_success/remediate_consumer_lag_success.20260920T124047Z.1166d6798eba.txt`.
* INC-004's rule for the ledger, which generalises past this guard: **a rule that says "re-read
  before X" needs a structural bound on how many times, or the model will satisfy it forever.**
* Not done here and not needed for the fix: no live or paid run. The owner's re-take of
  `make demo-live MODE=consumer_outage LIVE=1` is what measures whether a real planner takes the
  narrowed choice, and it is the owner's to authorise.
