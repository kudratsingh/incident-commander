# ADR 0065: A briefing names its remainder in a slot, not in its prose

* Status: accepted
* Date: 2026-09-19
* Decider: Kudrat Singh (WO-R3-230, WP-11.3)
* Amends: [ADR 0059](0059-a-second-fault-is-not-resolved-by-the-first-fix.md) — its "read the
  diagnosis from the briefing's primary/secondary fields" alternative, recorded there as the
  better long-term home and unavailable at the time, is what this record builds
* Related: [ADR 0026](0026-a-stabilizer-is-not-a-resolution.md) (a verified stabilizer still
  escalates), [ADR 0054](0054-one-rule-for-a-stuck-chains-root-rendered-into-every-reader.md)
  (one rule held once and rendered into every reader), [ADR 0038](0038-ground-truth-is-evaluator-only.md)
  (the slots are the AGENT's own claims, never the answer key), [ADR 0036](0036-the-planner-call-is-the-seam.md)
  (strategies propose; the bar and the gates stay in the loop), INC-001 and INC-002 in
  `audit-ws/context/INCIDENTS.md`

## Context and problem statement

Capability level 5 is two independent faults in one world. WO-R3-228 (ADR 0059) made such a run
reachable and gradable: the loop no longer resolves over a cause it still asserts, and
`ROOT_CAUSE` scores the asserted SET. What it left is the handoff. A run that fixes one fault and
escalates says which cause is left **in prose** — in the escalation reason the loop happens to
write, and in whatever the briefing writer chooses to put in `findings`. Three things follow from
that, and all three are the reason this packet exists.

**A remainder a writer may omit is a remainder that will be omitted.** WO-R2-164's rule is
"stabilize what one action can, then escalate with a briefing naming every remaining row and what
each needs". As prose that is advice. The failure it was written for is already on the record: a
RESOLVED briefing said the remaining rows were "cleared or addressed" while four remained
(INC-001's secondary finding). One fault fixed instead of one row replayed is the same shape one
level up.

**A claim about the remainder had nothing structural to land on.** `expect_briefing_contains`
searches a corpus assembled by `_briefing_corpus`, and a scenario that wants to assert "the
handoff names the cause it did not fix" could only assert on the writer's sentence or on the
loop's escalation reason. The first is phrasing, which `docs/eval-methodology.md` forbids
asserting on; the second exists only on the escalation path.

**Two derivations of "what this run asserts" were about to exist.** ADR 0059 computes the
standing causes inside `remediation.py` for its resolve gate and the diagnosed set inside
`evals/graders/root_cause.py` for `ROOT_CAUSE`. Adding a third for the briefing would be three
places where the bar, the "already acted on" reading and the tie-breaks could drift, and a drift
there moves grades rather than behaviour.

## Decision

### 1. The slots are structural, and they are one projection

`agent/incidents.py` holds `IncidentSlots`: `primary`, `secondary`, `unresolved_extra`, each slot
carrying the agent's own category, name and confidence plus whether an attempt in this run aimed
at it. `incident_slots` derives them from a ranking and an evidence ledger:

* **primary** — the top of the ranking, whatever its confidence. The same reading
  `final_diagnosis` has always had, so a run's primary and its graded label cannot disagree.
* **secondary** — every OTHER hypothesis at or above `investigation.REMEDIATE_CONFIDENCE_THRESHOLD`
  (0.7). Below the bar is a cause the agent is *considering*, and ADR 0059's anti-hedging
  argument is exactly why the bar and not the ranking length is the boundary.
* **unresolved_extra** — of those, every one that no attempt in this run targeted. "Targeted" is
  read off the ledger markers that carry `target_hypothesis`: the plan that cleared its guards
  (`_planner_plan`) and each failed attempt (`_remediation_attempt_failed`, ADR 0056), matched by
  hypothesis NAME or category value because `RemediationPlan.target_hypothesis` is a free string
  and the corpus spells it both ways.

`EscalationBriefing.incidents` carries them, filled by `render_briefing` from `RunState` — so it
is filled on every run, including the ones whose writer says nothing, and including the ones with
no LLM enrichment at all. `render_incidents` is the one rendering both LLM readers are shown,
beside `render_trail`.

**The remainder is not "extra" in the sense of "beyond the primary".** A run that acted on
nothing lists its primary there too, because a human handed a run that diagnosed something and
did nothing about it needs to be told that, and "extra" relative to an action that never happened
is a distinction only the harness cares about.

### 2. Every reader learns the slots in this change (INC-002)

* **The deterministic grader.** `_briefing_corpus` includes the rendered block, so
  `expect_briefing_contains` can assert on a remaining cause and be satisfied by the run's own
  state rather than by prose. The claim is proven satisfiable by a correct run before shipping —
  INC-001's rule — with `findings`, `recommendation` and the escalation reason all empty.
* **`ROOT_CAUSE`.** `diagnosis_set` is now `incidents_of(run).categories`: the set the grader
  scores IS the decomposition the handoff names. Same arithmetic, same numbers, one derivation.
* **The briefing judge and the briefing writer.** One sentence in `llm/prompts/shared_rules.py`
  (`{{rule:unresolved_remainder}}`, ADR 0054's mechanism) tells the writer to name every listed
  cause and forbids calling any of them cleared, and tells the judge that naming them is
  GROUNDED — because the block is run state, not speculation. The rule quotes the block's own
  heading and a test pins that it still matches what the code renders. Both snapshot hashes move
  in this PR, deliberately.
* **The trace.** `StepRecord.incidents` carries the same projection per planner step, stamped by
  the loop (`_with_incident_slots`) rather than by any of the five strategies, so all of them emit
  one record shape and none of them learns the bar (ADR 0036). `None` there means nobody computed
  the slots, never "this step asserted nothing".

### 3. RESOLVED is not admissible while an unresolved-extra remains

`OUTCOME` fails a run that ended `RESOLVED` while its slots carry a remainder, even where the
scenario expects `resolved`, and the detail names each cause left and its confidence. This is the
grading side of ADR 0059's resolve gate: the loop stops a run reaching that state, and if a loop
change ever reopened the hole the suite would red instead of grading it green.

The check is asked only of a run that addressed something. `RESOLVED` is reachable only through a
verified remediation attempt (`remediation.py`'s single `with_state(RESOLVED)`), so that is the
whole population the loop's own gate asks about, and the scoping keeps the rule off hand-built
run states no loop can produce — an assertion nothing could have satisfied is the INC-001 shape.

## Considered alternatives

**Leave it in the writer's prose and grade the prose.** Rejected on the record already: a claim
on phrasing is brittle by the suite's own rule, and the honesty in question is exactly what a
writer under a token budget drops first.

**Make the slots disjoint — primary, then secondary, then a remainder that excludes both.** It
reads tidier and hides the case that matters most: a run that acted on nothing would have an
empty remainder, which is the "briefing omits the remainder" failure with a schema around it.

**Put the remainder in the escalation reason only.** It is already there on the escalation path
(ADR 0059 writes it), which is why the two dual-fault scenarios pass today. It is absent on every
other path, including a resolved run, and it is one string rather than a set a grader can read.

**Let a strategy fill `StepRecord.incidents`.** Five call sites, five chances to drift, and every
one of them would have to import the 0.7 bar — which `tests/unit/test_strategies.py::
TestStrategiesHoldNoExecutionPolicy` refuses, correctly.

**Grade `RESOLVED` inadmissible whenever a remainder exists, with no "addressed something"
precondition.** Stricter, and no real run reaches the difference, because the only path to
RESOLVED runs through a verified action. What it would reach is hand-built fixtures, where it
would red runs for a rule the loop never had a chance to satisfy.

**Have `agent/incidents.py` import the bar itself.** It cannot: the loop imports this module to
stamp its records, so the module cannot import the loop back. The bar is a required parameter and
its two appliers — `briefing.incidents_of` for the handoff and the grader,
`investigation._with_incident_slots` for the trace — both read the one constant.

## Consequences

Positive:

* A run that fixes one fault and leaves another cannot hand off without naming what is left, and
  the naming is checkable rather than judged.
* "A partial action never ends resolved" is now enforced on both sides — the loop's gate and the
  grade — and the grade says which cause was left.
* One derivation of "what this run asserts", read by the briefing, the judge, `ROOT_CAUSE` and the
  trace. Plan 03 § 7.1's set arithmetic and plan 02 § 7's step record now describe the same object.
* The judge can no longer mark a briefing down for honesty about a cause no probe names, which is
  INC-002's failure in its newest possible form.

Negative:

* **The briefing writer's and judge's contexts grew a block**, so a live briefing is written
  against slightly more context than before. Every context for a run with no ranking is
  byte-identical, which is why no canned grade moved, but the live prompts did change.
* **`OUTCOME` can now fail a scenario that expects `resolved`.** That is the point, and it means a
  future scenario whose planner ranks two causes high and fixes one must expect `escalated`.
* A third field on the briefing is a third thing the archive carries. Older archives have no
  `incidents` key and read back as empty slots, which is correct for them (the projection did not
  exist) and is the same default-forward convention `DimensionResult.applicable` uses.
* Two archived live rows (`e72b5ffb9df0`, `dlq_backlog` and `failed_traces_scan`, 2026-09-08)
  carry a resolved state with an unaddressed asserted cause. Both are ALREADY graded red on
  OUTCOME for the same underlying error ("expected escalated, got resolved"), so nothing in the
  evidence tree changes verdict; whether to re-grade them offline under this rule is a follow-up,
  not this PR's to take.

## More information

* Implemented by WO-R3-230 (WP-11.3): `agent/incidents.py`; `incidents_of`, `render_incidents`
  and `EscalationBriefing.incidents` in `agent/briefing.py`; the block in
  `agent/briefing_enrichment.py::_format_context` and `evals/graders/llm_judge.py::
  format_briefing_context`; `UNRESOLVED_REMAINDER_RULE` in `llm/prompts/shared_rules.py` and the
  two prompt files; `_briefing_corpus` and `_inadmissible_resolution` in
  `evals/graders/deterministic.py`; `diagnosis_set` in `evals/graders/root_cause.py`;
  `StepRecord.incidents` and `_with_incident_slots`.
* `PLAN_MARKER` joins `ATTEMPT_FAILED_MARKER` in `agent/planner_context.py` for the reason the
  comment there already gives: a second spelling of a marker name is how one reader stops matching.
* WO-R2-170 (open) asks whether a briefing claim should be scoped to the AUTHORED prose rather
  than the whole handoff. The slots are template, not prose: if that split lands, a claim on a
  remaining cause belongs on the deterministic side with `escalation_reason` and
  `attempted_action`, and `_briefing_corpus` says so in a comment.
* The live legs stay deferred under standing instruction O-22: no paid run measures whether a real
  writer and judge use the block as the rule asks. That is WP-11.4's, with the rest of Phase 11's
  close.
