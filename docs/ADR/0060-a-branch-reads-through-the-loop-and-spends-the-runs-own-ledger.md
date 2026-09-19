# ADR 0060: A search branch reads through the loop, and spends the run's own ledger

* Status: accepted
* Date: 2026-09-19
* Decider: Kudrat Singh

## Context

Plan 02 § 14 (WP-12.1) asks for `search`: a bounded shallow walk over **evidence-gathering
decisions**. Depth ≤ 2, branch factor ≤ 3, a node of
`(hypothesis set, evidence snapshot ref, proposed probe, score, accumulated cost)`, a score of
`selector_confidence − tool_cost − token_cost − safety_risk` (02 § 257), recorded-world mode only,
no world-changing action inside a branch, and the chosen path continuing through the normal
handoff.

Four facts shape the design.

**A branch has to read.** That is what makes it a branch: two candidates that would probe different
things are two different next steps, and the only way to find out which one pays is to take the
reading. So `search` is the first strategy that needs the world, and
[ADR 0036](0036-the-planner-call-is-the-only-strategy-seam.md) says a strategy replaces exactly one
call and carries none of the policy around it — no tier map, no wire serializer, no client.

**A moving world makes the comparison meaningless.** Decision C13 in the plan's log: in a live
world, branch two reads a world that branch one has already let move on, so the two nodes are not
comparable and the walk measures drift. A canned world is worse in a quieter way — canned responses
are per-tool sequences rather than per-call, so a second branch asking the same tool would be
served the first branch's answer and the walk would read a world that never existed.

**The tool-call budget is never multiplied.** Plan 02 § 8: token and dollar ceilings scale per
strategy through `TOKEN_BUDGET_MULTIPLIER`, and tool calls deliberately do not, "because probing the
world is the thing strategies compete on". So every branch competes with the chosen path for one
ceiling. That constraint *is* the experiment: exploring that could mint its own budget would be
free, and a free search tells us nothing about whether search is worth it.

**A bound in prose fails open.** The bounds that have held in this repo are objects and validators —
`MAX_OUTPUT_REPAIRS`, `_MAX_SUBJECT_PROBE_REFUSALS`, `max_iterations`, and
[ADR 0055](0055-one-revision-pass-per-step-spent-by-a-token-not-promised-by-a-prompt.md)'s
`RevisionPass`. A depth bound stated in a prompt, or held only by the shape of a loop somebody may
later edit, is not a bound.

## Decision

**1. A branch reads through a prober the LOOP builds, not through a client the strategy holds.**
`investigation.make_branch_prober(mcp_client)` returns a `BranchProber`:
`(RunState, ProbeAction) -> BranchProbeOutcome`. Behind it, on the loop's side of the seam, are the
`tier_of` re-check, `wire_arguments`, the MCP client and the ledger accrual; in front of it a
strategy can ask for one read and can name nothing else. `StrategyContext.branch_prober` is the
only non-LLM field on the context, and `tests/unit/test_strategies.py::
TestStrategiesHoldNoExecutionPolicy` still pins the whole field set, the forbidden imports and the
forbidden gate names — `strategies/search.py` names none of them.

**2. A non-read inside a branch is refused by its tier, not by a name list.** The prober classifies
with `policies.tier_of` and refuses anything above `Tier.READ` **without calling the client**, which
is the same runtime guard `_execute_probe` makes (B-06) for the same reason: a tool reclassified
`READ → TIER_1` after `ReadToolName` was hand-listed would otherwise slip through. The schema is the
other half — `ProbeAction.tool_name` is a `ReadToolName` literal, so a candidate cannot propose an
action tool at all. A refusal is recorded on the branch and the walk continues; it never escalates
the run, because a branch is explored and the run's state is the chosen path's business.

**3. Recorded mode only, refused twice and never degraded.** `search` refuses when
`ctx.branch_prober is None`, before any call, with `SEARCH_IS_RECORDED_MODE_ONLY` — which says why
and names `make world-record`. The eval runner refuses earlier and harder: `run_all` refuses the
**whole invocation** when `INFERENCE_STRATEGY=search` and any scenario has no recording, and
`run_scenario` refuses a direct call before a client exists or a hook fires. The prober is wired in
recorded mode alone, so the refusal is structural rather than a line of documentation.

**4. Depth and branch are structural; config may ask for less, never more.**
`MAX_SEARCH_DEPTH = 2` and `MAX_BRANCH_FACTOR = 3` live in `agent/search.py`. `SearchWalk` refuses a
larger request at construction — refuses rather than clamps, so a row never prints a bound the walk
did not run under — and hands out one `BranchAllowance` per node whose `spend()` raises on the
fourth branch, while `descend()` raises on the third level. `SEARCH_DEPTH` / `SEARCH_BRANCH` exist
because plan 03 § 8's matrix compares branch 2 with branch 3, and both are bounded above in
`Settings` as well. The walk is a `for` over `range(depth_allowed)`; the tokens are redundant today,
which is the point.

**5. The caps are shared, and they are enforced at the ledger.** One `BudgetLedger` carrier for the
whole step: every call and every branch read lands in it *before* the next branch is considered, so
the strategy never counts anything itself. A branch is taken only while `room_for_a_branch` says the
ledger can afford it **plus a reserve**: one tool call held back for the chosen path's own probe, and
a token reserve equal to the largest single call the step has already paid for — measured, not
guessed. When the reserve bites, the branch is recorded as refused with that reason and
`pruned_by_ledger` counts it, so "search was capped" and "search ran out of ideas" are different
findings in the record.

**6. The score is plan 02 § 257, kept as its four terms.** `selector_confidence` is the selector's
own score for the candidate it points at. `tool_cost` and `token_cost` are the fraction of the
shared ceiling the *path* has spent, weighted 0.25 each. `safety_risk` is the selector's
`uncertainty`, weighted 0.5 and charged **only against a node that would commit** (`select`):
gathering more evidence carries no risk, and acting while unsure is the risk. Uncertainty is not
folded into the confidence term, because a number built from two others multiplied together cannot
say which one decided the path. The weights are **declared, not tuned** — tuning them needs the paid
sweep (WP-12.2) and may never be done on the holdout (plan 03 § 16) — and they are stamped into
`strategy_config`.

**7. The walk is `(generator, selector)` at every node, so nothing is re-implemented.** A node is
expanded by one `best_of_n_enumerated` call with `N = branch`, which yields up to three candidates
and so up to three distinct reads; each branch then costs one read and one `candidate_selector` call
over the same set with the new reading in the ledger. The frontier is one node per level — best-first
with a beam of one — so depth 2 is at most two expansions. Two candidates naming the same
`tool(arguments)` are one decision (INC-002), and the second is recorded as a duplicate rather than
paying for an answer already in hand.

**8. The chosen path hands off exactly as `candidate_selector` does.** `step_for_selection` is now a
module-level function both arms call, so the emitted `InvestigationStep` has one shape, and the
`FIX_MAP` gate, the 0.7 threshold, ADR 0041's whole-queue refusal, ADR 0032's subject guard and
`_execute_probe`'s tier re-check all run on it unchanged. **Exploring is not authorization** (plan
02 § 18). The returned state carries the chosen path's evidence and the **whole walk's** ledger: a
branch not taken leaves its cost behind, and its reading behind with it.

**9. Every node is on the record, or the arm cannot be measured.** `StepRecord.search` carries the
bounds, the levels used, branches taken and refused, `pruned_by_ledger`, the chosen branch id, and
one `BranchRecord` per node with its evidence snapshot ref, the read it took *with its arguments*,
the read it would take next, its own ledger cost and all four score terms. The run's accounting
gains `search_branches` and `search_branch_tool_calls`, which reach the report row beside the run's
own `tool_calls`, so a reader can see what exploring cost against the ceiling it was spent from.

## Alternatives rejected

**Give the strategy the MCP client.** The shortest path, and it would have put the tier check,
`wire_arguments` and the ledger accrual inside `strategies/`, where a later edit could reach an
action tool. The prober is the same capability narrowed to one verb.

**Let a branch propose reads without taking them, and score the proposals.** Free, and it needs no
recorded world — but it is not search. Scoring a probe you have not run is what the selector already
does at the root; the walk exists to find out what the reading *said*.

**Give each branch its own budget, or multiply the tool ceiling by the branch factor.** Rejected
twice over: plan 02 § 8 says tool calls are not multiplied, and a branch that cannot starve the
chosen path is a branch with no trade-off to measure. The reserve is the narrower answer — the
chosen path keeps one call, and exploring pays for the rest out of the same ceiling.

**A configurable maximum depth and branch (`SEARCH_DEPTH` with no ceiling).** Rejected for ADR
0055's reason: an operator with a hypothesis raises it, and the bound is the safety property. The
knobs go down only.

**Regenerate nothing at depth 2 and branch on the parent's remaining reads.** One call cheaper per
step, and the walk would branch on proposals made before the new reading existed — the opposite of
what a second level is for.

**Weights tuned to make the arm look good offline.** Rejected as unmeasurable and as a holdout
hazard. The four terms are reported separately so WP-12.2 can show which one decided each path,
against a real model, on the owner's go.

## Consequences

* A depth-2, branch-3 step costs up to two generation calls, seven selector calls and six reads
  where `baseline` costs one call and no read. The token ceiling is seeded by
  `TOKEN_BUDGET_MULTIPLIER` (WP-2.4) and nowhere else; the tool ceiling is **not** scaled, so on a
  25-call run the ledger will prune the walk on later iterations — that is a finding about search,
  not a defect, and `pruned_by_ledger` is how a reader sees it.
* `StrategyContext` gains its first non-LLM field. The test that pins the field set was updated
  deliberately, with the argument in it: a loop-owned read-only function is not a client.
* `RunState.hypotheses` gains a sixth writer, still one write per step and still the step the run
  acted on (`tests/unit/test_grader.py::test_only_a_planner_call_writes_the_ranking`).
* The chosen path's evidence ledger holds only the reads on that path, while the budget holds every
  read the walk made. That asymmetry is deliberate and is the definition of search; the readings a
  rejected branch saw are in the trace (`StepRecord.search`) and in the replay client's own call
  list, so nothing is lost — but a grader reading `RunState.evidence` sees what the agent carried
  forward, not everything it looked at.
* `search` is not in any canned scenario and cannot be: the canned suite has no recordings, and the
  runner refuses the arm there. `make eval-reg` is unchanged at 53 of 53 with no grade moved.
* **Not measured yet.** Search versus the selector on the hardest ambiguous and noisy templates,
  with cost and latency beside accuracy, is WP-12.2 — a paid recorded-mode sweep, which standing
  instruction O-22 forbids. The machinery, the bounds and the arithmetic are here; the number is a
  deferred row.

## Links

* Plan `docs/plans/research-buildout-v2.1/` — 02 § 14 and § 257 (the strategy and its score),
  § 8 (budgets per strategy), § 18 (strategy safety), 03 § 8 (the experiment matrix) and § 12
  (paired comparisons), 04 Phase 12, 06 constraint C13 (recorded mode only — the work order cites
  it as "D13").
* Work order WO-R3-232 (WP-12.1). WO-R3-233 (WP-12.2) is the experiment and the phase close.
* [ADR 0036](0036-the-planner-call-is-the-only-strategy-seam.md) (the seam this widens by one
  read-only function), [ADR 0055](0055-one-revision-pass-per-step-spent-by-a-token-not-promised-by-a-prompt.md)
  (the structural-cap pattern), [ADR 0048](0048-a-selector-is-constrained-by-its-schema-not-by-a-temperature.md)
  (the selector whose confidence the score starts from; "a decision about diagnosis is not
  authorization"), [ADR 0046](0046-a-replay-answers-the-call-that-was-made-at-the-clock-it-is-replayed-at.md)
  and [ADR 0047](0047-a-recorded-run-grades-diagnosis-and-the-plan-and-nothing-else.md) (the
  recorded world this runs in, and what it may be graded on),
  [ADR 0042](0042-a-candidates-evidence-reference-is-resolved-by-a-validator.md) (the candidate set
  a branch comes from), [ADR 0045](0045-a-sampled-step-is-one-samples-and-every-draw-is-charged.md)
  (every billed leg is charged, one layer up),
  [ADR 0015](0015-wall-clock-and-usd-budget-meters.md) (the ledger the caps live on).
