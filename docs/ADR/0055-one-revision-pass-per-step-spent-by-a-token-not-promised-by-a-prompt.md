# ADR 0055: One revision pass per step, spent by a token — not promised by a prompt

* Status: accepted
* Date: 2026-09-18
* Decider: Kudrat Singh

## Context

Plan 02 § 13 (WP-9.1) asks for `reflection`: the cheapest of the buildout's revision strategies and
the one most likely to be net-negative. One bounded pass per planner step — initial
`InvestigationStep` → critic prompt → revised step — with both steps recorded, and a report of
cases fixed versus cases harmed.

Three facts shape the design.

**The seam is fixed.** [ADR 0036](0036-the-planner-call-is-the-only-strategy-seam.md)
says a strategy replaces exactly one call, the planner call, and carries none of the policy around
it. `reflection` is one more strategy on that seam, beside `baseline`, the two best-of-N arms and
`candidate_selector`. It widens nothing.

**An unbounded critic loop is the failure mode that matters.** "Critique and re-plan" has no natural
stopping point. A critic asked to find problems will find them, and a revised step gives it fresh
material. The cheap strategy becomes the most expensive one, and a budget ceiling turns a strategy
experiment into a measurement of where the ceiling was.

**A prompt-stated bound fails open.** Every bound this repo has tried to hold in prompt text has
eventually been crossed by a model that had a reason. The bounds that held are objects and
validators: `MAX_OUTPUT_REPAIRS` ([ADR 0035](0035-a-parse-failure-of-our-own-output-is-a-harness-event.md)),
`_MAX_SUBJECT_PROBE_REFUSALS` ([ADR 0032](0032-the-action-must-address-the-alerts-subject.md)),
`max_iterations`.

There is a fourth fact, from the hub's lessons ledger rather than from this repo. On 2026-09-17 a
decision quoted a platform behaviour and then concluded the opposite of what the quote said. A
critic prompt does that by construction the moment it lists a contradiction and approves the step
anyway, and a critique like that is worse than no critique: it costs tokens and licenses a wrong
answer.

## Decision

**1. One pass per step, and the pass is a token that can be spent once.**

`agent/reflection.RevisionPass` holds `allowed = MAX_REVISION_PASSES = 1` and a `spent` counter.
`spend()` raises `ReflectionCapExceeded` on the second call. `ReflectionStrategy.plan_next_step` is
straight-line — one `critique_step` call site, one `revise_step` call site, neither inside a loop —
and takes the pass before revising. The token is redundant *today*, which is the point: a later edit
that wrapped the revision in a retry would raise rather than bill a third planner call.
`ReflectionCapExceeded` is a `RuntimeError` and not an `LLMError`, so the loop's escalation arm does
**not** absorb it: a breached cap is a defect in the strategy, not an incident outcome.

**2. The cap is not configurable.** No `Settings` field and no `StrategyKnobs` field reaches it. A
bound an operator can raise is not a bound. What ran is stamped instead: `strategy_config` carries
`passes: 1` and `cap: "structural"`, and every `StepRecord.revision` carries `passes_used` beside
`passes_allowed`, so a reader of an archive never has to know this release's constant.

**3. The critic critiques; the planner re-plans.** The critic returns findings and a verdict, never a
step. The revised step comes from a **second planner call** using `investigation_planner.md` plus a
short addendum (`investigation_planner_revision.md`) and the same `InvestigationStep` schema. So a
reflection step is at most **two planner-role calls and one critic call**, and the revised step meets
the `FIX_MAP` gate, the 0.7 threshold, [ADR 0041](0041-read-the-whole-queue-before-you-replay-part-of-it.md)'s
whole-queue refusal, ADR 0032's subject guard and `_execute_probe`'s tier re-check exactly as any
other step does. **Revision is not authorization** (plan 02 § 18).

**4. The verdict follows the findings, enforced by a validator.** `StepCritique` accepts exactly two
combinations: `keep` with every finding list empty and `missing_probe` null, or `revise` with at
least one finding named. A `keep` beside a named contradiction is refused, so the "quote a fact and
conclude the opposite" shape is unreachable rather than discouraged; ADR 0035 re-asks once and the
rejected leg is billed. A `revise` with nothing named is refused too — it would hand the planner a
demand with no content.

**5. The critic's reach is the schema's.** `missing_probe` is typed `ReadToolName | None`, so the
critic cannot name an action tool at all, and contradictions carry an `EvidenceRef`, so
"the ledger contradicts this" resolves to a real entry ([ADR 0042](0042-a-candidates-evidence-reference-is-resolved-by-a-validator.md)'s
validator, reused). The critic is shown the planner's own context — a step cannot be faulted for what
the planner was never shown — with evidence ids on, because a grounded citation is unaskable without
them.

**6. The reviser gets `baseline`'s context bytes.** `format_planner_context` is called with
`show_evidence_ids` **off** for both planner turns, so the only difference from the control group's
turn is the two appended blocks. A cited id is rendered into the revision turn as the ledger *line*
it names, derived from the ledger rather than restated
([ADR 0047](0047-a-recorded-run-grades-diagnosis-and-the-plan-and-nothing-else.md)'s rule). Per
[ADR 0044](0044-evidence-ids-are-rendered-for-the-arms-whose-schema-cites-them.md) the arm stamps
`evidence_ids_rendered: false` and, separately, `critic_sees_evidence_ids: true`.

**7. Both steps are recorded, or the strategy cannot be measured.** `StepRecord.revision` carries
`initial_step`, the verdict, the findings, the cited ids, whether a revision ran, and the two pass
counts; `emitted_step` is what the run acted on. `evals/reflection_metrics.py` reads them and reports
FIXED / HARMED / UNCHANGED_CORRECT / UNCHANGED_WRONG / NOT_REVISED / NOT_GRADED per step and for the
deciding step, plus the paired across-arm comparison against `baseline` by `WorldKey`
(plan 03 § 12), with added tokens and added tool calls by family and difficulty. **Cases harmed is a
line of its own in every summary**, never only a net.

**8. The critic is its own metered role.** `reflection_critic` has its own client on
`StrategyContext` and its own `accounting.meter` label, so "added tokens" separates the critique from
the planning. It is charged to the run's ledger: it decides whether the run re-plans, so it is the
agent's own cost, like the selector and unlike the briefing judge.

## Alternatives rejected

**The critic emits the revised step (one extra call instead of two).** Cheaper, and it was the first
sketch. Rejected: the critic would then need the category table, the tool list and the action rules —
a second copy of `investigation_planner.md`, which would drift, and an arm comparison that had become
a comparison of prompts. The addendum pattern from
[ADR 0044](0044-evidence-ids-are-rendered-for-the-arms-whose-schema-cites-them.md) exists for exactly
this and is reused.

**A configurable pass count (`REFLECTION_PASSES`), defaulting to 1.** Rejected: the bound is the
safety property. A default is not a cap, and the first operator with a hypothesis would raise it.
Plan 02 § 13's "HARD CAP" is read as meaning what it says.

**Only a contradiction forces a revision; the softer findings are advisory.** Rejected as a
judgement about seriousness that nobody can calibrate. `keep` means "I found nothing" is a rule a
reader can check. It does make revisions common and reflection expensive, and that is a *finding*,
which is why added tokens are reported by family.

**Deriving the pass's effect from the arm comparison alone.** Rejected: the across-arm number cannot
say whether a wrong answer was wrong before the pass or because of it. Recording both steps is what
makes harm attributable, and it is the reason `StepRecord` grew a field rather than the report
growing an inference.

## Consequences

* A revised reflection step costs about three planner-sized calls where `baseline` costs one. The
  ledger is seeded by `TOKEN_BUDGET_MULTIPLIER` (WP-2.4) and nowhere else, so an arm metered against
  a one-call ceiling would read as the strategy failing rather than as the ceiling biting.
* `RunState.hypotheses` gains a fifth writer. It is still one write per planner call, and the write
  is the revised step's — the one the run acted on — so `final_diagnosis` stays sound
  (`tests/unit/test_grader.py::test_only_a_planner_call_writes_the_ranking`).
* `StrategyContext` gains a second optional LLM client. That is the same kind of widening WP-6.2
  made and no more: an MCP client, a tool registry or the run itself would let a strategy act, and
  none of those is there.
* `PlannerCall` gains `billed_usage`, so a strategy whose SECOND call fails can charge the first
  one's bill ([ADR 0045](0045-a-sampled-step-is-one-samples-and-every-draw-is-charged.md)'s trap one
  layer up). Unread by `baseline`.
* Two new prompt files, two new snapshot hashes. `investigation_planner.md`'s hash does **not**
  move, which is what makes `reflection` the control group plus a pass.
* The canned suite is untouched: `INFERENCE_STRATEGY` defaults to `baseline`, no canned scenario
  declares a `reflection_critic` queue, and `make eval-reg` reports 49 of 49 with no grade moved.
* **Not measured yet.** Cases fixed and harmed against a real model need a paid arm sweep
  (WP-9.2), which standing instruction O-22 forbids. The machinery, the fixtures of each case and
  the offline arithmetic are here; the number is a deferred row.

## Links

* Plan `docs/plans/research-buildout-v2.1/` — 02 § 13 (the strategy), § 17 (probe efficiency and
  context size), § 18 (strategy safety), 03 § 7 and § 12, 04 Phase 9.
* Work order WO-R3-223 (WP-9.1). WP-9.2 is the analysis and phase close.
* [ADR 0036](0036-the-planner-call-is-the-only-strategy-seam.md)
  (the seam), [ADR 0035](0035-a-parse-failure-of-our-own-output-is-a-harness-event.md) (the bounded re-ask this
  copies), [ADR 0042](0042-a-candidates-evidence-reference-is-resolved-by-a-validator.md) (grounded
  citations), [ADR 0044](0044-evidence-ids-are-rendered-for-the-arms-whose-schema-cites-them.md)
  (which arms see ids; the addendum pattern),
  [ADR 0045](0045-a-sampled-step-is-one-samples-and-every-draw-is-charged.md) (every billed leg is
  charged), [ADR 0048](0048-a-selector-is-constrained-by-its-schema-not-by-a-temperature.md) (selection
  is not authorization; the shape this follows),
  [ADR 0015](0015-wall-clock-and-usd-budget-meters.md) (the ledger).
* `audit-ws/context/LESSONS.md`, 2026-09-17: "Quoting a fact and drawing the opposite conclusion
  from it is a real failure mode." Decision 4 above is the structural answer to it.
