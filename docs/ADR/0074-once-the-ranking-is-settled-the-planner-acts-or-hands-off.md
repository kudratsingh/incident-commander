# ADR 0074: Once the ranking is settled, the planner acts or hands off

* Status: accepted
* Date: 2026-09-20
* Decider: Kudrat Singh (WO-R3-332, INC-004, the owner's third live take)
* Amends: [ADR 0073](0073-a-confirming-read-is-bounded-by-the-loop.md) (the bound this one moves
  from a refusal into the schema)
* Related: [ADR 0009](0009-investigation-freshness-reprobe.md) and
  [ADR 0071](0071-attribution-is-graded-on-the-agents-own-pre-action-read.md) (the two re-read
  rules the bound sizes), [ADR 0032](0032-the-action-must-address-the-alerts-subject.md) /
  [ADR 0033](0033-a-human-required-chain-root-is-fenced-then-escalated.md) (the refusal shape
  both guards copy), [ADR 0035](0035-one-bounded-re-ask-for-our-own-invalid-output.md) (the one
  bounded re-ask, and the one failure that is now not re-asked),
  [ADR 0036](0036-the-planner-call-is-the-only-strategy-seam.md) (strategies propose, the loop
  decides — the reason the narrowing travels on the context),
  [ADR 0072](0072-a-run-report-carries-one-step-and-an-older-platform-gets-fewer-fields.md) (the
  reporting shape this one makes live), [ADR 0068](0068-the-agent-reports-its-run-and-reporting-is-never-a-tool-call.md)
  (reporting is fail-open and never a tool call), INC-004 in `audit-ws/context/INCIDENTS.md`

## Context and problem statement

ADR 0073 shipped on 2026-09-20 and the owner re-took the demo the same afternoon
(`make demo-live MODE=consumer_outage LIVE=1`, archive `f8a13135c6a1`, run
`2e2f95fb-422c-5f03-9614-32ed39109009`, $0.21). The guard fired exactly as designed: two
readings of `worker-dispatcher` landed and the third was refused. Then the planner probed
`list_dlq_messages`, then `get_circuit_breakers`, then asked for the lag a fourth time. Five
steps, `consumer_saturation` first at 0.75 → 0.82 the whole way — above the 0.7 remediate
threshold, in a category `FIX_MAP` gives a Tier-1 fix — and no action. The escalation reason was
honest this time (ADR 0073's second half worked), and the run still did nothing.

**The guard bounded the wrong thing, or rather it bounded one thing and left the class open.** A
refusal that arrives after the planner has chosen can only say "not that"; the planner's next
move is still its own, and `probe` was still on the menu. The model took the menu. This is the
generalisation of INC-004's own rule for the ledger: a rule that leaves an unbounded way to
satisfy itself will be satisfied that way forever, and "re-read the SUBJECT" was only one such
way.

Three more findings from the same take are closed here because they are the same demo being
unwatchable, and they are decisions rather than repairs:

* **The AGENT phase row never advanced live.** `phase_history` read triage 15:17:59.9,
  investigating 15:17:59.9, escalated 15:17:59.9, and all four steps plus the terminal state
  arrived at 15:18:40 in one burst carrying their original timestamps. The events were right; the
  reporter queued them to the next transition.
* **The action ledger was flooded with the runner's own reads under the AGENT principal**, every
  three seconds, so the page could not tell them from the agent's four.
* **Step 3 took 106 seconds behind a bare spinner** while the platform's 60-second lag clock
  caught up.

## Decision

### 1. The bound is in the schema of the next call, not in a refusal after it

`investigation._probe_withdrawn` is asked BEFORE every planner call. When

1. the top hypothesis has been a `FIX_MAP` category at or above
   `REMEDIATE_CONFIDENCE_THRESHOLD` for `_SETTLED_RANKING_STEPS` (2) planner steps running, and
2. a declared reading of the alert's own subject can say the fault is present
   (`FAULT_PRESENT_READING`), and this run has taken at least one, and
3. the NEWEST of those readings says so and is FRESH (ADR 0009's own window, on the reading's own
   `age_seconds`),

then that call is made with `hypothesis.without_probe(<step model>)`: the same model with
`next_action` re-annotated to `Literal["remediate"] | Literal["stop"]`, the `probe` variant gone
from the discriminator and `ProbeAction` gone from `$defs`. There is no door to take, so no model
can take it.

**What is gone from ADR 0073's condition is the COUNT of readings, and that is deliberate.** That
guard refused the THIRD reading of the subject; this one withdraws the move once the run holds
ONE fresh reading that shows the fault. The reason is ADR 0073's own prompt clause, which was
already the honest rule: *one fresh reading that shows the fault is the whole demand*. One such
reading satisfies ADR 0009 (the conclusion rests on a current measurement, not a static one) and
ADR 0071 (the reading immediately behind the action shows the fault present), so a run holding one
has everything either rule asks for. The clause and the structure now say the same thing.

**ADR 0073's guard stays, and it is not dead code.** `_confirming_read_exhausted` is
`_probe_withdrawn` plus the two clauses only a proposal can answer — the probe reads the subject
again, and the run has already taken two such readings — and it is reachable on exactly one kind
of step: the one where the streak COMPLETES. The narrowing is computed before the call and the
streak is updated by it, so a step that takes the ranking from one settled step to two can propose
a third reading no narrowed schema ever saw. Both paths share one refusal record, one counter and
one cap.

### 2. The refusal is recorded before the call it narrows

`_refuse_confirming_read` is written to the evidence trail FIRST, so the narrowed call's own
context carries the reason its choice is smaller than it was. A choice that shrinks silently is a
model guessing, and guessing for five steps is what this record is about. The words are ADR 0073's
words, with one sentence rewritten: that refusal promised "a probe of a DIFFERENT tool is still
open", which is precisely what the take did with it. It now says no probe is offered this step, of
this resource or any other, and asks a `stop` to name what the run would need to SEE to act.

The same sentence is in the schema, on `next_action`'s `description`
(`hypothesis.SETTLED_CHOICE_DESCRIPTION`) — on the field and never in a class docstring, because a
docstring on a model whose schema reaches a prompt is a silent schema change (CLAUDE.md), and this
one is meant to be read.

One record per narrowed call, including the call that then complies: what it records is that the
loop withdrew a move, which is true whatever the planner did next. So a run refused twice and then
handed off carries three records and two spent refusals, and the cap counts ASKS.

### 3. A `stop` there is an escalation, and the loop still never acts on its own

`stop` ends the run at ESCALATED through the existing path, carrying the planner's own reason. And
the loop still does not promote a withdrawn probe into a `remediate`: ADR 0073 decision 2 holds
unchanged, for the same reason — a Tier-1 write the planner never proposed would be the loop
reaching through ADR 0036's seam, and "the agent restarted a consumer group because its own guard
fired" is not a sentence this project wants to be able to say. The planner acts or hands off; the
loop makes sure it cannot do neither indefinitely, and cannot spend five steps discovering that.

### 4. The narrowing reaches every strategy, through the context

`StrategyContext.offer_probe` is a boolean the LOOP sets, and `StrategyContext.step_model(model)`
is how a strategy renders it: `model` as it was, or `without_probe(model)`. Every planner call site
goes through it — `baseline`, `reflection` (both of its calls, so a critique cannot win the move
back), `best_of_n_sampled`, `best_of_n_enumerated` (whose own `CandidateStep<n>` is what gets
narrowed, N-bound intact), `adaptive`'s rung 0, and the two selector arms through their generators.
`without_probe` derives the narrowed model from the model handed in for exactly that reason: a
narrowing that only knew `InvestigationStep` would leave every other arm able to probe where the
loop said it may not — F1 one layer down.

Two arms assemble their emitted step in Python from a selected candidate (`candidate_selector`,
`search`), so the schema cannot bind what they build from its answer. The loop therefore refuses a
probe that arrives while the move is withdrawn, whoever produced it. Two enforcement points, and
the second is not redundant: the first is what a real model meets, the second is what makes the
guarantee true of the loop rather than of the strategies.

### 5. A withdrawn move is refused, never re-asked

`StructuredOutput.output_refused(error)` is a new hook, `False` by default;
`ProbeWithdrawn.output_refused` answers `True` for a `union_tag_invalid` on the tag `probe`
(read off the structured errors and through `__cause__`, because `LLMClient` wraps a
`ValidationError` in `LLMOutputError` per ADR 0007). `call_with_output_repair` asks the model and
raises `OutputNotOffered` instead of re-asking.

ADR 0035's re-ask is for output nobody can read, and its turn says exactly that ("your output was
invalid"). A `probe` under this schema was perfectly readable and the move was withdrawn, so the
re-ask would send the wrong sentence, pay for a second call to be told the same thing, and —
against a scripted planner — consume the NEXT step's answer as this step's correction. The hook
lives on the MODEL so that the knowledge of what a model offers stays with the model and every
caller that passes a narrowed schema gets the behaviour without knowing it exists.

### 6. The reporter reports each step when it happens

`ToolCallLog` gains one subscriber and `RunReporter` subscribes in its constructor, so a step
report goes out inside the tracer hook that fires when the tool call returns — which is where the
event is. `_pending` no longer builds steps from the evidence ledger when a tool log is wired (it
would be the same call a second time, under a second `seq`); it carries the verify verdicts, which
are LLM calls and exist nowhere but the ledger. Transitions report at the transition, as they
already did. Two windows keep the old queued path, and both are honest: before the first
transition report there is no state to stamp a step with, and on a platform that refused the
widened fields there is no step field to send. The only batching left is the one the platform
forces — a terminal state closes the run, so the terminal report goes LAST (ADR 0072 unchanged).

Reporting from inside `call_tool` is safe in the one way that matters and was checked: the tracer
hook fires after the round trip, the reporter skips its own two tools (so a report cannot report
itself), and the reporter swallows every failure — invariant 5, and more sharply here than
anywhere else in that module, because a raising observer would turn telemetry into a failed tool
call in the middle of an incident.

### 7. The demo runner wears the read-only token, always

`Settings.require_smoke_token()` is `require_chaos_token`'s twin and refuses in one line:
`scripts/demo_live.py` builds every client through one `_smoke_client()`, and an unset
`PLATFORM_SMOKE_TOKEN` fails the demo rather than falling back to the agent's principal. The
fallback is what wrote the runner's polling into the audit log as `agent.tool_invoked`, where the
page cannot tell it from the agent's own reads. The traffic subprocess is handed the same token in
its environment rather than trusting `make`'s `-include .env` to be true of whatever environment
the script was started in. And both waits print the platform's 60-second lag clock with the last
sample's own age, because a spinner over a 106-second wait reads as a hang.

## Considered alternatives

**Keep the refusal and add "and do not probe anything else" to the prompt.** Rejected on
`docs/architecture-principles.md` § 3 and on the evidence: the prompt rule ADR 0073 added was
already correct and already followed, and the model still found the unbounded way. A rule a model
can satisfy forever is the class of bug; the fix is to stop offering the move.

**Refuse every probe once ANY reading of the subject exists.** That is this decision without the
settled-ranking clause, and it would fire on a run whose top hypothesis is still moving — the case
where another reading is the most useful thing available. The streak is what distinguishes
"confirming" from "investigating".

**Keep ADR 0073's read count (withdraw only after two readings).** Rejected in decision 1: it
would leave the F1 door open for one more step in the common case (one reading of the subject, then
two probes elsewhere), and the count was never the rule — freshness was.

**Let the loop promote the withdrawn probe into a `remediate`.** Rejected again, ADR 0073 decision
2's reasoning verbatim. It is also the one change here that a prompt edit could not undo.

**Re-ask the model once when it asks for the withdrawn move.** Rejected in decision 5: it is the
wrong sentence, and against the canned suite it is actively destructive.

**Make the new scenario end in a verified remediation** (the work order's acceptance text:
"green after — the narrowed schema forces remediate on step 3"). **Impossible in one canned
scenario, and the proof is short.** A scripted planner's answer to step N is fixed, so the
narrowing changes what happens at step N only if that answer is a `probe`; if it is a `remediate`,
both runs behave identically and the scenario discriminates nothing. And a step whose scripted
answer IS a probe cannot produce an action at that step: the refusal is not re-asked (decision 5),
so no second payload is drawn, and the run's next answer is the next step's. Therefore no canned
script can be red before and remediate-after at the same step. The post-fix run of
`planner_probes_elsewhere_forever` escalates, deliberately, and the resolved path through the
narrowing is covered where it can be — `tests/unit/test_llm_investigation.py::
TestProbeWithdrawnOnceTheRankingIsSettled::test_the_schema_of_the_settled_step_offers_only_remediate_or_stop`
and `::test_the_withdrawn_move_is_refused_rather_than_re_asked` both end in PLANNING. Reported as
a divergence, the same way ADR 0073 reported the same acceptance text.

## Consequences

Positive:

* A settled ranking costs one step: the planner acts or hands off. The probes that bought nothing
  in the take — a DLQ listing, a breaker read, a fourth lag read — are three steps and three
  audit rows that no longer happen.
* The prompt and the structure say the same thing. ADR 0073's clause promised a probe of another
  tool stayed open; the schema now matches the sentence, in both directions.
* An operator watching the page sees each step as it happens, with the run's own timestamps, and
  the ledger holds the agent's reads and nothing else.
* The narrowing is a property of the LOOP, not of a strategy: every arm gets it, and the two arms
  that assemble steps in Python are covered by the loop's own refusal.

Negative:

* **The guard is still inert wherever a reading cannot say the fault is present** — every subject
  but the consumer group today (`FAULT_PRESENT_READING` is unchanged and still total over
  `ALERT_SUBJECT_PROBES`). The equivalent failure on a cache key or a chain would not be caught.
* **A planner that ignores the steer still escalates.** Three narrowed calls and the run hands off.
  The bound makes the run cheaper and the handoff honest; it cannot make a model decide.
* **A `probe` under the narrowed schema no longer gets ADR 0035's re-ask.** That is the decision,
  and it means a live model whose only fault was reaching for a withdrawn move spends its refusal
  allowance rather than a repair.
* **A step report now leaves inside the agent's own tool call.** The total latency is what it was
  (the same reports, spread out instead of batched), but it sits in the call path rather than
  between transitions, and a slow platform costs the run up to the reporter's 5-second timeout per
  call rather than per transition.
* **A step reported mid-transition carries the hypotheses and the budget as of the last
  transition.** That is the honest stamp for an event that happened then, and it means the
  console's budget meter can be one call behind until the transition lands.
* `planner_confirms_forever`'s TRAJECTORY changes although every graded dimension of it is
  identical: its refused steps are now failed planner calls rather than successful ones, so that
  run records three fewer `StepRecord`s and three fewer billed LLM calls (zero-cost, canned). The
  proof is dimension by dimension below.
* The pre-fix run of the new scenario is labelled `grader-brittleness` by the runner's failure
  classifier — INC-001's signature, and here a true negative for ADR 0073's own reason: only
  EVIDENCE fails, and the claims are about the loop's own records.

## More information

* Implemented by WO-R3-332: `_probe_withdrawn`, the pre-call refusal and the `OutputNotOffered`
  arm in `agent/investigation.py`; `SettledNextAction`, `ProbeWithdrawn`, `without_probe`,
  `asked_for_a_probe` and `SETTLED_CHOICE_DESCRIPTION` in `agent/hypothesis.py`;
  `output_refused` in `llm/structured.py`; `OutputNotOffered` in `llm/repair.py`;
  `offer_probe` + `step_model` in `agent/strategies/protocol.py` and one line in each arm;
  the live step report in `agent/run_reporting.py`; `require_smoke_token` in `config.py`;
  `_smoke_client`, `_smoke_env` and the lag-clock waits in `scripts/demo_live.py`;
  `CONFIRMING_READ_BOUND_RULE` rewritten in `llm/prompts/shared_rules.py` (one prompt hash moves,
  `investigation_planner`, as in ADR 0073); `evals/scenarios/planner_probes_elsewhere_forever.yaml`.
* Red-before/green-after, offline and free: `make eval` with the narrowing OUT gives 64/65 —
  report `report.20260920T160802Z.96ec83165997.json`, archive `96ec83165997`, the new scenario red
  on EVIDENCE with every new signal missing and the last lag reading 52s old. With it IN, 65/65 —
  report `report.20260920T160715Z.c04a41cc33b8.json`, archive `c04a41cc33b8`, 2 tool calls and the
  last reading 8s old. A dimension-by-dimension diff of the two reports: **64 scenarios identical
  in every dimension, 1 differing — the new one.**
* The take this is written from is archive `f8a13135c6a1`, human report
  `evals/reports/human/remediate_consumer_lag_success/remediate_consumer_lag_success.20260920T151849Z.0d7ada16b6e9.txt`.
* Not done here and not needed for the fix: no live or paid run. The owner's fourth take is what
  measures whether a real planner takes the narrowed choice, and it is the owner's to authorise.
* F4 (the evaluator's own probes labelled as the agent's) and F5 (the reset clearing the lag
  history) are the platform's, in WO-R3-333, and the console's half of all of this is WO-R3-334.
