# ADR 0075: A state's timestamp is the moment it was entered; the planner's thinking is reported as it happens

* Status: accepted
* Date: 2026-09-20
* Decider: Kudrat Singh (WO-R3-337, the owner's fourth live take)
* Amends: [ADR 0068](0068-the-agent-reports-its-run-and-reporting-is-never-a-tool-call.md) (what a
  report is sent on) and [ADR 0072](0072-a-run-report-carries-one-step-and-an-older-platform-gets-fewer-fields.md)
  (what one carries), through [ADR 0074](0074-once-the-ranking-is-settled-the-planner-acts-or-hands-off.md)
  decision 6, which made the STEPS live and left the thinking between them queued
* Related: [ADR 0002](0002-hand-rolled-state-machine.md) (the loop owns the transitions, which is
  why the stamp belongs to it), [ADR 0015](0015-wall-clock-and-usd-budget-meters.md) (`_accrue_wall_time`'s second
  clock reading, which this decision shares), [ADR 0036](0036-the-planner-call-is-the-only-strategy-seam.md)
  (strategies propose, the loop decides — the reason the thinking is observed where the loop
  accepts it), [ADR 0038](0038-the-agents-view-of-a-scenario-is-an-allow-list-projection.md) and platform ADR 0038 (the
  lab's own label on a probe the lab makes with the agent's token),
  [ADR 0069](0069-a-rehearsal-is-a-third-provenance-not-a-live-run.md) (the mode the demo
  rehearses in), [ADR 0013](0013-run-provenance-is-part-of-the-eval-result.md) (the provenance record `chaos_seeded_by`
  joins)

## Context and problem statement

The owner took the demo a fourth time on 2026-09-20 (`make demo-live MODE=consumer_outage
LIVE=1`, run `1f14d3f6-768c-5f5a-8409-eb13cd414103`, archive `6f657c6ca613`, $0.25). The run was
GREEN — the planner acted, ADR 0074 held — and the page was still not watchable while it
happened. Four facts, all read off the platform's own record rather than re-derived:

1. **`phase_history` was a lie about durations.** triage 19:48.584, investigating 19:48.658,
   **planning 19:48.667**, remediating 20:10.597. The page showed "investigating · 10 ms" and
   "planning · 21.9 s". The run in fact investigated from 19:48.67 to 20:10.6: two reads at
   19:56 and 20:04, three planner calls. Every transition stamp was the time the transition
   FUNCTION was entered, because `agent/loop.py` reads the clock once per iteration and the
   whole investigation happens inside one `dispatch(run_state, now)`.
2. **The hypotheses arrived in one burst at 20:10.6.** Steps reported live (ADR 0074 decision 6
   works), but a report carries `hypotheses` and the reporter sends one on a transition — and
   no transition happens during a 22-second investigation. Three planner rankings existed; one
   was ever visible.
3. **"4 steps reported · 5 calls the platform recorded — the two do not agree"** was false. The
   fifth agent-principal call was the runner's own precondition probe
   (`evals/preconditions.py`, the demo's step 4 "prove the premise"), which WO-R3-335 labelled
   the guards and the world audit but not this.
4. **The fault fired twice.** `chaos.tool_invoked kill_consumer` at 03:18:05 from
   `scripts/demo_live.py` step 3, and again at 03:19:48 from `evals/runner.py`'s
   `_seed_chaos_plan` when step 5 started the agent. The console anchors "the fault" on the
   take's newest `chaos.*` row, so "injected 08:19:48", "T+ 43.9 s", the chart's F marker and
   "agent acting 58 s" were all measured from a re-arm 1 minute 43 seconds after the real fault.
   `demo_live.py`'s own docstring called the double fire "safe".

The thread through all four: **an event's time is a fact, and four different places were
recording something else in its place.**

## Decision

### 1. The loop stamps the state it holds, with the reading it already takes

`run_to_completion` re-reads the clock after `dispatch` returns and stamps `updated_at` with
that reading (`loop._stamp_entered`). `ALLOWED_TRANSITIONS` has no self-loop, so a dispatch that
returned always produced a state that was entered exactly then — there is no case where a
transition knows a better time for "when the new state was entered" than the moment control came
back to the loop.

**One place in the loop, not twenty call sites, and the alternative was considered.** The work
order offered a second shape: pass `clock` into the transition functions and read it at the point
of decision. Rejected for two reasons. It changes the `Transition` signature and every function
that implements it, to record a fact that is not a transition's fact at all. And a stamp each
transition takes for itself is a stamp twenty functions can drift on — which is the shape of the
bug, not its fix. The loop owns it.

The reading is the one `_accrue_wall_time` was already taking after the transition ("read again so
a terminal transition records its own duration"), so the honest stamp costs no extra clock read.
That the wall meter and the phase stamp disagreed about which reading to use is the whole defect:
they were always meant to be the same fact.

The transition's own evidence entry is restamped with it under three conditions that keep it to
the row recording the exit: the entry was APPENDED by this dispatch, it still carries the
iteration's start time, and it is an underscore-prefixed bookkeeping marker (the repo-wide
convention for a row that is the loop talking rather than a call the platform answered). **A real
tool entry is deliberately left alone** — a read at 19:56 stays at 19:56, because its timestamp is
about the read.

### 2. The reasoning has a seam of its own, and the loop writes to it where it accepts a ranking

A tool call has a client seam (`ToolCallLog` off `MCPClient`'s tracer hook). A planner ranking has
none: it is an LLM call, so it exists nowhere but the run's own state, which is why it could only
travel on a transition report. `agent/thinking.py` is the seam — `PlannerLog`, with
`ToolCallLog.subscribe`'s shape, and `RunReporter` subscribes in its constructor beside the other.

**The write point is the loop, one line, where `plan_next_step` returns.** That is the single
place every strategy's proposal passes through, so `baseline`, `reflection`'s revised step, both
`best_of_n` arms, `candidate_selector`, `search` and `adaptive` are all covered and none of them
learns that telemetry exists (ADR 0036's line: strategies propose, the loop decides, and
telemetry is not a decision). It is written BEFORE the loop's own guards look at the step, because
what is recorded is what the planner decided — a refusal is its own row on the evidence trail, and
a refused proposal is still thinking a person watching should see.

Each observation leaves as one report carrying:

* the run's state as the last transition reported it (still `investigating`), for ADR 0072's
  reason — a terminal state closes the run, so an intermediate report may not carry a new state;
* `hypotheses` and `current_hypothesis` from the observation's OWN ranking, not from the last
  checkpoint's. That staleness is the fourth take's finding 2 in miniature;
* one step of `kind: "report"`, `tool: investigation_planner | reflection | verify_judge`,
  `arguments: {ranking (top 5), next_action, reason}`, and a `result_excerpt` that is one
  readable sentence ("top consumer_saturation 0.85 → probe get_consumer_lag").

The verify judge's verdict is written the same way, from `remediation.make_llm_verify`, for the
same reason one layer along: a verify leg can poll for minutes behind the platform's 60-second lag
clock, and a page that shows nothing for those minutes reads as a hung run.

**A `report` step is not a call.** It touches no budget (nothing in the reporter ever did), it is
counted apart from the tool steps in the run's summary, and it is never the run's `last_step` —
`last_step.kind` has two members and `report` is not one of them, so writing it there would refuse
the whole report on v0.6.15's input model and narrow every later report of the run. `seq` comes
from the same counter the tool steps use, so the ledger's order is the order the run made its
moves in: a ranking, the probe it chose, the next ranking.

**An observation nobody can send is dropped and counted, not buffered.** Two windows: before the
first transition report there is no state to stamp a report with, and on a narrowed platform there
is no step field to carry one. In both the ranking still reaches the console on the next
transition report, which has carried `hypotheses` since WO-R3-329 — so the loss is the row's
timing, not the information. A buffer here would need a second place that knows about `seq`.

### 3. A probe the lab makes to prove the premise says so

`evals/runner.py`'s precondition reads go out wrapped in `LabProbeClient` with the EVALUATOR's
credential and a per-probe reason, `precondition: <tool> proves <what its `expect` asserts>`
(`preconditions.probe_label`). Same seam as WO-R3-335, same rule from platform ADR 0038: a probe
the lab must make on the agent's token is labelled by the lab, and the label needs the lab's own
credential.

The reason is DERIVED from the probe's `expect` rather than written on the schema, because a
written description is a second source of truth that drifts, and `expect` is what the probe
actually asserts. It is truncated to the platform's 200-character bound here, since the platform
REFUSES an over-long reason rather than trimming it, and a refused label is an unlabelled row —
the failure this exists to prevent.

After this, an `agent.tool_invoked` row exists only for the agent's own calls, so the console's
counts line has nothing left to disagree about.

### 4. One fault, fired once, by whoever is driving the demo

`evals/runner.py` takes `--world-already-faulted`: it skips `_seed_chaos_plan` and the settle
wait, still runs the precondition probes, and records `provenance.chaos_seeded_by = "demo_live"`
so a grader reading the chaos audit knows the one row it finds sits BEFORE the run rather than
that a hook went missing. `scripts/demo_live.py` passes it in step 5 on both paths (the paid one
through `make eval-live WORLD_ALREADY_FAULTED=1`), and its docstring stops calling the double fire
safe: it is safe for the WORLD — measured, and `kill_consumer` genuinely re-arms — and wrong for
the RECORD, which is the half that sentence left out.

**The premise is still checked, deliberately.** Whether the fault is there is the one question
this flag does not get to answer; all it says is who manufactured it. And because the premise IS
checked, the flag counts as `chaos_seeded` for INC-003's rule
(`root_cause.label_describes_this_world`): that rule asks whether the world was MANUFACTURED, and
its "live but nothing seeded" case is the smoke pass — a run against a healthy world. Without
that, the demo's own rehearsal would hold back its ROOT_CAUSE grade saying "a live run that seeded
no fault", which would be false and would make the flag quietly change what the run measures.

**A self-expiring fault may not be seeded elsewhere.** A temporal template's evaluator timeline
(WP-14.1, ADR 0062) is read off the seeding record's own `expires_at`, so a run that seeded
nothing has none. Refused as a SETUP failure rather than degraded, so the row is ungraded instead
of scored against a recovery clock nobody measured.

**Teardown is skipped with the seeding, for symmetry.** A compensator undoes what its setup hook
did, and this run's setup did nothing. Firing them anyway would put a SUCCESSFUL
`chaos.tool_invoked` row in the take for a hook nobody asked for, and a console that reads a
successful chaos row as a fault would draw it as one. The demo owns the world it broke and puts it
back in its own wind-down step.

## Considered alternatives

**Pass the clock into the transitions (decision 1's sibling).** Rejected above: twenty call sites
for a fact only the loop can observe, and twenty places to drift.

**Restamp every evidence entry the transition appended.** That would move a read taken at 19:56 to
20:10 and make the ledger lie in the other direction. The rule is narrow on purpose.

**Let the reporter read the ranking off the run state when a thinking report goes out.** That is
what it already does through `_last_state`, and it is precisely the staleness finding 2 describes:
mid-transition, the state's ranking is the one the last checkpoint held. The observation carries
its own.

**Report reflection's initial ranking as well as its revision.** The loop sees only the step the
arm accepted, and the accepted step IS the revision. Reaching for the intermediate draft would
mean a write point inside `strategies/reflection.py`, which is the one thing
`TestStrategiesHoldNoExecutionPolicy` and ADR 0036 exist to prevent. What the run accepted is also
the honest thing to show. Reported as a divergence from the work order's wording.

**Invent a `reason` for a `probe` step so every thinking row reads alike.** `ProbeAction` has no
`reason` field — that is a real property of the schema — so the row carries `null` and the
sentence ends at the tool. A rendered guess at why the model probed would be model output this
project did not receive.

**Buffer a thinking observation nobody could send.** Decision 2: the information is not lost, and
a second owner of `seq` is a worse trade than a timing gap in the one window before the run's
first transition report.

**Leave the double injection and fix it in the console.** The console change lands too (WO-R3-336
item 7: a fault is the take's FIRST successful injection and never a probe), and it is not
sufficient: two real injections in one take mean the world was re-armed under a running demo, and
a reader picking the first row would still be reading a take whose record says the fault happened
twice.

## Consequences

Positive:

* Every duration on the page is a duration. An investigation that took 22 seconds reads as 22
  seconds, and `planning` is stamped after the last planner call rather than before the first.
* The three rankings the fourth take produced are three rows as they are decided, with the
  confidence that ranking held, so "what does it think now" is answerable while it runs.
* One `agent.tool_invoked` row per call the agent made, so the counts line has no false
  disagreement to report.
* One `chaos.tool_invoked` per hook per take, so everything anchored on it is anchored on the
  fault.
* The whole of decision 2 is one line in the loop, one line in the verify leg, and one module; no
  strategy changed, and `Transition` did not.

Negative:

* **A thinking report sits inside a transition**, between an LLM call and the loop's decision
  about it, and costs up to the reporter's 5-second timeout per ranking against a slow platform.
  The same trade ADR 0074 made for the step reports, one seam further in, and `PlannerLog`
  swallows everything for the same reason.
* **A thinking report carries the budget as of the last transition.** ADR 0074's consequence,
  unchanged: the hypotheses are now fresh and the budget meter can still be one call behind.
* **`reflection`'s critique is invisible.** Only the revised step is reported; the run's own
  trace and `StepRecord` still hold both.
* **A run whose first planner call precedes its first transition report loses that row's
  timing.** The ranking arrives at the next transition, as it did before.
* **`--world-already-faulted` trusts its caller.** Nothing in the runner can see whether a hook
  fired, so the provenance field is taken on trust — and a run started with the flag against an
  unfaulted world fails its precondition, loudly, which is the honest failure.
* **`provenance.chaos_seeded_by` is a new optional field.** Archived reports parse (the default is
  `None`), and `baseline_report`'s "every field is answered" check now names it as a field whose
  ABSENCE is the answer.
* `evals/fakes.py`'s `CannedMCPClient` gained the lab-probe parameters, because the premise reads
  go through `LabProbeClient` and a fake that refused the label would fail every offline
  precondition test on a signature rather than on behaviour. It records them, so the label is
  assertable offline.

## More information

* Implemented by WO-R3-337: `_stamp_entered` in `agent/loop.py`; `agent/thinking.py`
  (`PlannerLog`, `ObservedThinking`, `ThinkingAction`); `_report_thinking`, `_thinking_step`,
  `ranked_of`/`top_of` and the `last_step` guard in `agent/run_reporting.py`; the write point plus
  `_thinking_action`/`_action_reason` in `agent/investigation.py`; the verdict write in
  `agent/remediation.py`; `probe_label` in `evals/preconditions.py`; `_precondition_reader`,
  `--world-already-faulted`, `EXTERNAL_CHAOS_SEEDER` and `RunProvenance.chaos_seeded_by` in
  `evals/runner.py` (plus the `or world_already_faulted` on INC-003's
  `label_describes_this_world` call and the derived-TTL refusal beside the skip);
  `_ABSENCE_IS_AN_ANSWER` in `evals/baseline_report.py`; the step-5 flag and the rewritten
  docstring in `scripts/demo_live.py`; `WORLD_ALREADY_FAULTED` in the `eval-live` recipe.
* Red-before/green-after, offline and free: `tests/unit/test_loop.py::
  TestAStatesTimestampIsTheMomentItWasEntered` (4 of its 5 red on `main`),
  `tests/unit/test_run_reporting.py::TestThePlannersThinkingIsReportedAsItHappens`,
  `tests/unit/test_llm_investigation.py::TestTheLoopRecordsTheRankingItAccepts`,
  `tests/unit/test_remediation.py::TestTheVerifyVerdictIsReportedWhenTheJudgeReturns`,
  `tests/unit/test_lab_probe.py::TestThePremiseReadsAreTheLabs`,
  `tests/unit/test_runner.py::TestTheWorldMayAlreadyBeFaulted` and
  `tests/unit/test_demo_live.py::TestTheFaultIsFiredOnceByThisScript`.
* The take this is written from is run `1f14d3f6-768c-5f5a-8409-eb13cd414103`, archive
  `6f657c6ca613`, and the coordinator's reading of its audit stream in
  `audit-ws/.coordination/briefs/brief-demo-v4.md`.
* Measured on the v0.6.17 stack, free, `AUTO=1`, both demo modes: each take's audit stream holds
  exactly ONE fault row (`chaos.tool_invoked`, `outcome: success`, no `lab_probe_reason`) —
  `kill_consumer` at 04:16:32 and `poison_message` at 04:19:15 — with the principal guards'
  `inject_latency` rows carrying their label beside them, and every world-audit and premise read
  arriving as `lab.probe`. `phase_history` on `5baf113c` runs triage → investigating → planning
  at .248 / .268 / .310 while the two planner rankings report at .278 and .302: PLANNING is
  stamped AFTER the last planner call, which is the claim, and before ADR 0075 it carried .268.
* No platform change: the step enum has carried `report` since v0.6.16, and the lab-probe field
  and header since v0.6.17. `tools/list` is unchanged.
* Not done here and not needed for the fix: no live or paid run. The owner's fifth take is what
  measures whether the page is watchable, and it is the owner's to authorise.
* The console's half of all of this is WO-R3-336.
