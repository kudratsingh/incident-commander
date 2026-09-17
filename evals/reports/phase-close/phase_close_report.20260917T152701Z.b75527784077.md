# Phase 2 close report

**CLOSING** — every run in scope was made under the benchmark model role

**DRAFT** — 2 live re-run(s) are pending the owner's go (`remediate_stale_cache_success`, `remediate_dlq_backlog_success`). Their fixes are merged and their worlds are ready, but nothing has been measured on them yet, so every number below that depends on those scenarios is the number BEFORE the fix. Adding the two archive ids to this phase's scope and re-running `make phase-close-report` writes the final version beside this one.

Protocol: docs/plans/research-buildout-v2.1/03_EVAL_RESEARCH_PLAN.md section 14. Work order: WO-R3-195 (WP-2.6). Scope: REDUCED close, owner decisions O-14 (benchmark model) and O-20 (scope, option A). DRAFT: two live re-runs are pending the owner's go.

Zero live invocations and zero LLM calls produced this document. Every number was read out of a committed archive; the only thing executed is the `grep` in section 2.

## What this close does and does not claim

**It claims:**

- The 41-scenario canned corpus passes under the benchmark model role, names the right root cause on all 32 rows that carry a label, and moves nothing against either baseline that this phase did not itself add.
- On the three seeded live legs — one per injected-fault family — the agent named the right root cause every time. 3/3 is the first VALID live diagnosis number this project has.
- The leak boundary holds on live evidence, and one bucket that was not empty in Phase 1 is empty now: no platform tool response in any of the 71 trajectories this phase produced contains `chaos`, a root-cause label or a fixture name, and our own prompts no longer contain the word either (cmd #258).
- Invariant 2 is visible in evidence: four `remediate` handoffs in the read-only pass crossed the agent's own gates; three of them reached the platform and it refused every one — `missing required scope: actions:execute` — after which each run escalated with a briefing.
- The cost of a run can be attributed to a prompt role for the first time (WP-2.3). Of the $2.677354 this close cost, the investigation planner spent three quarters of it.

**It does not claim:**

- That the agent resolves these scenarios reliably. Three seeded legs at one rep each: one resolved, one escalated holding the correct diagnosis, one acted correctly and failed an evidence claim. Pass rate on the seeded legs is 1 of 3.
- A live root-cause accuracy over the corpus. Three graded rows is three rows, and the read-only pass adds none: a scenario that seeds no fault is not the world its label describes (ADR 0040).
- `root cause: 11/18 correct (61%)` — the figure the read-only archive still carries. It is withdrawn (INC-003) and appears in this document only as the number being withdrawn.
- That either merged fix works. Neither has been run live. `remediate_stale_cache_success` and `remediate_dlq_backlog_success` stand in this report exactly as they ran.
- That the canned 32/32 says anything about the model. The canned planner replies are fixtures; that number is about the corpus and its labels agreeing.
- That the phase's bill of $2.677354 is comparable to plan 03 section 11's ~$20 phase-close row. It is a different, smaller sweep.

Phase 2 closes on this sample, not on the plan's full sweep, and it closes as a DRAFT because two of its three seeded legs have merged fixes that have not been measured. The differences are listed as deviations at the end, each with the decision or the fact behind it.

## 1. Sweep results

**Canned sweep `b75527784077`** — 41/41 passed, 0 failed, judged 41, judge mean 0.8591463414634146, degraded 34. Model role(s): benchmark; agent model(s): claude-sonnet-4-6.

What the gate says about it:

- `no changes vs baseline`
- `closing: every run in latest was made under the benchmark model role`
- `cross-model refusal: none — baseline and sweep name one agent model`

**Live legs** — 1/3 passed, in the order the owner released them.

| # | Archive | Scenario | Result | Judge | Agent tool calls | Commander | Platform |
|---|---|---|---|---|---|---|---|
| 1 | `759e198cdd27` | `remediate_consumer_lag_success` | PASS (resolved) | 0.95 | 9 | `b85ddbc` | `sha256:8f3cbcff426a` |
| 2 | `648a32f2339d` | `remediate_stale_cache_success` | **FAIL** (escalated) | 0.875 | 5 | `b85ddbc` | `sha256:8f3cbcff426a` |
| 3 | `fc896b25a09c` | `remediate_dlq_backlog_success` | **FAIL** (resolved) | 1.0 | 3 | `b85ddbc` | `sha256:8f3cbcff426a` |

- `759e198cdd27` — GREEN, judge 0.95, root cause correct. Read the lag trend with the time fields platform v0.6.7 added, went 0.75 -> 0.82 across five planner steps, restarted `worker-dispatcher`, and kept probing through three `not_verified` readings until the fourth showed lag falling 50 -> 30 -> 11 -> 0. The verify loop, not the first reading, is what made this green.
- `648a32f2339d` — RED. Root cause CORRECT (`stale_cache`) at every one of five planner steps, and no `remediate` step at any of them: confidence drifted 0.75 -> 0.72 -> 0.65 -> 0.55 -> 0.55, crossed below the 0.7 bar at step 3, and the run escalated on the five-step budget. The sensor is the cause — `get_cache_key_info` returned key shape and size but nothing that says the entry is stale, so more probing could only lower confidence. Platform v0.6.8 (plat #209) now reports `records_referenced` / `records_found` (healthy 3/3, stale 3/0); commander re-pinned in cmd #269.
- `fc896b25a09c` — RED on EVIDENCE, and correct on everything else: OUTCOME, ACTION, SAFETY and ROOT_CAUSE all pass, judge 1.00. The agent replayed exactly the one `replay_safe` row by id and verified the slice was empty — but it never called `list_dlq_messages` unfiltered, so it never saw the poison row it left behind. The runner tagged this `grader-brittleness`; owner decision O-21 went the other way: the claim is right, the agent must read the whole queue once before replaying part of it. ADR 0041's guard (WO-R3-268) is the fix; the re-run has not been bought.

**Live root cause on the seeded legs: 3/3 correct.** A ground truth is a statement about one world (ADR 0040). These legs SEED the fault their label describes, so their diagnosis is gradable; the read-only pass seeds nothing by construction, so 16 of its rows report 'not graded' rather than red, and it cannot contribute a live root-cause number at all.

**Read-only live pass `0db6fe722f7c`** — 27 scenarios in one invocation under the read-only token; 16 reached the live platform and 21 made live model calls. It seeds no fault, so it is a live read of the ordinary world: the leak hunt, the cost profile and the platform's refusal of every Tier-1 call that reached it all come from it. Its own root-cause grade was INVALID and was re-graded offline (INC-003, ADR 0040).

The archive still carries `root cause: 11/18 correct (61%)`. That figure is WITHDRAWN and is not quoted as a result anywhere in this document. What replaces it is the offline re-grade under ADR 0040, `evals/reports/regrades/regrade_report.20260917T133824Z.0db6fe722f7c.json`:

| | As archived | Re-graded |
|---|---|---|
| Scenarios passing | 20/27 | 26/27 |
| ROOT_CAUSE graded | 18 | 2 |
| ROOT_CAUSE correct | 11 | 2 |
| ROOT_CAUSE not graded — wrong world | 0 | 16 |

6 verdicts changed and no other dimension moved. The re-grade verified all 82 files of the archive unchanged by sha256 before reading them, and the archive itself was not touched (invariant 9). Still failing after the re-grade: `consumer_lag_missing_group` (evidence).

A re-graded root-cause accuracy covers only the rows whose world carries their label — 2 of 27 here. A live root-cause number is not recoverable from a pass that seeded no fault, and this document does not offer one.

All five archives are committed in this repository — the canned sweep by this PR, on Phase 1's precedent, because a report that reads an untracked archive can only be regenerated on the machine that ran it. Every number above is read out of them and none is typed in.

## 2. Leak hunt

65 terms in three families, all derived rather than typed: `chaos`; every `HypothesisCategory` value; every scenario name and every chaos hook name in the corpus. Matching is case-insensitive substring, so a term glued to something else still counts — and the false positives that produces are adjudicated below by name rather than filtered away.

**The gating grep** — WP-1.5's own acceptance wording, over all 71 trajectories this phase produced (41 canned + 27 read-only live + 3 seeded live):

```console
$ grep -rIni -e chaos evals/runs/759e198cdd27/trajectories evals/runs/648a32f2339d/trajectories evals/runs/fc896b25a09c/trajectories evals/runs/0db6fe722f7c/trajectories evals/runs/b75527784077/trajectories
# exit status 1 (0 matching line(s))
```

Same files, split by who authored each ledger row — because a trajectory is full of root-cause labels by design (they are the agent's own hypotheses), and the only thing that can fail the phase is a term arriving in a **platform tool response**:

| Bucket | Hits |
|---|---|
| platform responses the agent read | {'unknown': 2} |
| text the agent or the harness wrote | {'consumer_lag_high': 9, 'consumer_saturation': 93, 'db_pool_saturation': 1, 'db_query_latency': 3, 'deploy_regression': 28, 'downstream_dependency': 1, 'no_fault': 8, 'persistent_data_bug': 15, 'poison_message': 55, 'redis_saturation': 4, 'runaway_saga': 8, 'stale_cache': 14, 'transient_dependency': 23, 'unknown': 38} |

Every hit in what the agent read, adjudicated:

- `unknown` x2 in `get_consumer_lag` (consumer_lag_missing_group.json): `{"consumer_group":"unknown-consumer","lag":null,"lag_known":false,"source":"unrecognized","cache_key":"kafka:consumer_la`

**Adjudication.**

- `unknown` — The substring `unknown` inside the consumer group id `unknown-consumer` — the platform's own value for a group it does not recognise, and the entire subject of `consumer_lag_missing_group`. A substring collision with the `unknown` root-cause label, not a leaked label.

Unadjudicated hits: none. **Leak-hunt verdict: PASS.** No `chaos` token, no root-cause label and no fixture name reaches the agent from the platform in any of the 71 trajectories.

**Where `chaos` does appear**, and which side of the boundary each occurrence is on. The traces are the wider record — they hold our own prompts and the evaluator's bookkeeping as well as the wire:

```console
$ grep -rIoin -e chaos evals/runs/759e198cdd27/traces/remediate_consumer_lag_success.jsonl evals/runs/648a32f2339d/traces/remediate_stale_cache_success.jsonl evals/runs/fc896b25a09c/traces/remediate_dlq_backlog_success.jsonl evals/runs/0db6fe722f7c/traces
evals/runs/759e198cdd27/traces/remediate_consumer_lag_success.jsonl:2:chaos
evals/runs/759e198cdd27/traces/remediate_consumer_lag_success.jsonl:2:chaos
evals/runs/648a32f2339d/traces/remediate_stale_cache_success.jsonl:2:chaos
evals/runs/fc896b25a09c/traces/remediate_dlq_backlog_success.jsonl:2:chaos
# exit status 0 (4 matching line(s))
```

| Author | `chaos` | What it is | Gating? |
|---|---|---|---|
| platform response | 0 | bytes a platform tool returned to the agent | **yes** |
| commander prompt | 0 | **empty.** Phase 1 found one hit here — our own `remediation_planner.md` said "...lives in the description of a chaos tool you never see". Removed in cmd #258 (WO-R3-255), and these runs are the evidence that it is gone. | no |
| harness record | 4 | the evaluator's `chaos_setup` trace record and the `chaos:kill:...` key it wrote. Never shown to the agent. | no |
| agent output | 0 | what the model wrote, plus its own ledger replayed into the next request | no |

The gating bucket is **empty in every live run**: nothing the agent read from the platform contained the word. So is the commander-prompt bucket, which was not empty at the Phase 1 close — the last `chaos` token on the agent's side of the boundary went out with cmd #258, and these 30 live traces are where that is checked rather than claimed.

Per-run counts for all 65 terms are in the JSON companion under `sections.leak_hunt.traces_by_author.per_run`.

## 3. Gate-crossing audit

Three structural gates stand between a planner's `remediate` step and a Tier-1 call: the category must be a key in FIX_MAP, the top confidence must be at or above the bar, and the alert's own subject must already have been probed. The first two escalate when they fail; the third refuses and re-steers, which is why a refusal is not a failed run.

The bar is 0.7. FIX_MAP: `consumer_saturation`, `poison_message`, `runaway_saga`, `stale_cache`.

Across the three seeded live legs: 12 planner steps, 2 `remediate` steps emitted, 2 crossed into planning, 0 refused and re-steered, 1 escalation. Every crossing is tabulated below with the two numeric gates it had to pass. So is leg 2, which crossed nothing on any of its five steps: on two of them every gate was open and the planner probed anyway, and on the other three its own confidence had already fallen below the bar — the table gives the confidence at each step so the drift is visible rather than described. The read-only pass is audited after the legs: it is read-only by token, not by intention, and it did try to act.

### `759e198cdd27` — `remediate_consumer_lag_success` -> resolved (expected resolved)

5 planner step(s); 1 `remediate` emitted, 1 crossed, 0 refused; 4 step(s) where every gate was open and the planner probed instead.

| Step | Top hypothesis | Category | Confidence | In FIX_MAP | >= bar | Emitted | Disposition |
|---|---|---|---|---|---|---|---|
| 1 | worker-dispatcher-stalled-lag-climbing | `consumer_saturation` | 0.75 | yes | yes | `probe` | no handoff attempted (probe step) |
| 2 | worker-dispatcher-lag-climbing-30-sustained | `consumer_saturation` | 0.75 | yes | yes | `probe` | no handoff attempted (probe step) |
| 3 | worker-dispatcher-lag-climbing-30-and-rising | `consumer_saturation` | 0.82 | yes | yes | `probe` | no handoff attempted (probe step) |
| 4 | worker-dispatcher-lag-climbing-30-and-rising | `consumer_saturation` | 0.82 | yes | yes | `probe` | no handoff attempted (probe step) |
| 5 | worker-dispatcher-lag-climbing-30-and-rising | `consumer_saturation` | 0.82 | yes | yes | `remediate` | CROSSED into planning |

Plan: `restart_consumer_group({"consumer_group": "worker-dispatcher"})` targeting `worker-dispatcher-lag-climbing-30-and-rising`, verified with `get_consumer_lag`.

Executed: `restart_consumer_group` (1 call(s)).

Verify: **not_verified**, **not_verified**, **not_verified**, **verified**.

### `648a32f2339d` — `remediate_stale_cache_success` -> escalated (expected resolved)

5 planner step(s); 0 `remediate` emitted, 0 crossed, 0 refused; 2 step(s) where every gate was open and the planner probed instead.

| Step | Top hypothesis | Category | Confidence | In FIX_MAP | >= bar | Emitted | Disposition |
|---|---|---|---|---|---|---|---|
| 1 | cache-miss-spike-hot-key-cache:jobs:worker-dispatcher:hot_set | `stale_cache` | 0.75 | yes | yes | `probe` | no handoff attempted (probe step) |
| 2 | cache-miss-spike-worker-dispatcher-hot-set | `stale_cache` | 0.72 | yes | yes | `probe` | no handoff attempted (probe step) |
| 3 | cache-miss-spike-worker-dispatcher-hot-set | `stale_cache` | 0.65 | yes | no | `probe` | no handoff attempted (probe step) |
| 4 | cache-miss-spike-worker-dispatcher-hot-set | `stale_cache` | 0.55 | yes | no | `probe` | no handoff attempted (probe step) |
| 5 | cache-miss-spike-hot-set-key-worker-dispatcher | `stale_cache` | 0.55 | yes | no | `probe` | no handoff attempted (probe step) |

Escalation: `max iterations (5) exceeded`.

Justified by ground truth? NOT justified. The scenario's ground truth expects 'resolved', and on 2 planner step(s) the top hypothesis was both in FIX_MAP and at or above the 0.7 bar — every gate was open and the planner chose `probe` anyway. Not laziness either: the diagnosis was RIGHT and the agent's confidence in it fell — 0.75, 0.72, 0.65, 0.55, 0.55 — because `get_cache_key_info` could not show the key was stale. The same world went green that morning (`ee183c85429c`) at the same bar, which is what makes this a boundary and not a behaviour. Fixed in platform v0.6.8 (WO-R3-267); the re-run has not been bought.

### `fc896b25a09c` — `remediate_dlq_backlog_success` -> resolved (expected resolved)

2 planner step(s); 1 `remediate` emitted, 1 crossed, 0 refused; 1 step(s) where every gate was open and the planner probed instead.

| Step | Top hypothesis | Category | Confidence | In FIX_MAP | >= bar | Emitted | Disposition |
|---|---|---|---|---|---|---|---|
| 1 | dlq-replay-safe-messages-need-replay | `poison_message` | 0.7 | yes | yes | `probe` | no handoff attempted (probe step) |
| 2 | bulk-api-sync-upstream-timeout-replay-safe | `poison_message` | 0.92 | yes | yes | `remediate` | CROSSED into planning |

Plan: `replay_dlq_by_ids({"job_ids": ["fc8d2a03-23b3-5371-9acb-46443c73baa5"]})` targeting `bulk-api-sync-upstream-timeout-replay-safe`, verified with `list_dlq_messages`.

Executed: `replay_dlq_by_ids` (1 call(s)).

Verify: **verified**.

### `0db6fe722f7c` — the read-only pass

4 `remediate` handoff(s) crossed the loop's own gates; the loop refused and re-steered 1 more. Tier-1 calls the platform refused: 3. Tier-1 calls that succeeded: 0.

Where a call was actually made, the PLATFORM refused it — `MCP error -32002: missing required scope: actions:execute`, because the smoke pass runs under the read-only token. That is invariant 2 in evidence rather than in a design document: the agent's policy registry is a first filter, and the authorization decision is the platform's. Every one of these runs then escalated with a briefing, which is invariant 7's shape for a blocked action. Note the fourth crossing never reached the platform at all — it ran out of tool-call budget first, which is a different stop and is labelled as one.

- `consumer_lag_analytics_critical` — stopped by the platform: remediation tool error (restart_consumer_group): MCP error -32002: missing required scope: actions:execute
- `consumer_lag_orders_high` — stopped by the platform: remediation tool error (restart_consumer_group): MCP error -32002: missing required scope: actions:execute
- `multi_probe_hypothesis_evolution` — stopped by the platform: remediation tool error (restart_consumer_group): MCP error -32002: missing required scope: actions:execute
- `redis_saturation` — stopped by the agent's own budget: insufficient tool-call budget for action+verify (remaining=1); escalating without executing

## 4. Budget profile diff — benchmark vs development

Owner decision O-14 keeps BENCHMARK_MODEL at `claude-sonnet-4-6`, which is also DEVELOPMENT_MODEL. Both roles resolve to the same model id, so a benchmark-vs-development token or tool-call diff is not small — it is definitionally zero, and printing a table of zeroes would read like a measurement. What Phase 2 adds instead is the breakdown WP-2.3 built: the same money, split by the prompt role that spent it, which is the column a strategy comparison in Phase 6 will actually move.

| Archive | Scenario | LLM calls | Planner iterations | Wire tool calls | Billed tool calls | Tokens | Wall s |
|---|---|---|---|---|---|---|---|
| `759e198cdd27` | `remediate_consumer_lag_success` | 12 | 5 | 17 | 9 | 89069 | 168.07542 |
| `648a32f2339d` | `remediate_stale_cache_success` | 7 | 5 | 6 | 5 | 58163 | 0.057253 |
| `fc896b25a09c` | `remediate_dlq_backlog_success` | 6 | 2 | 5 | 3 | 43487 | 22.496284 |

Every run stayed inside every cap: highest USD 0.237300 of 1.00, highest tokens 89 069 of 200 000, highest tool calls 9 against the 13 the remediation scenarios seed, highest start-to-finish 248.5 s of the 600 s cap. `mcp_calls_in_trace` counts more calls than `agent_tool_calls_billed` because the runner's own precondition poll and the post-action verify probe are on the wire but are not the agent's budget. One reading here should NOT be trusted: the ledger's `wall_seconds_used` is 0.057 s on `648a32f2339d`, which cannot be true of a run that made seven model calls — the meter is not advanced on every path out of the loop. Section 7 takes wall time from the trace records instead, and the defect is in the follow-ups.

**Where the money goes, per prompt role** — across every live run in scope. This is the column WP-2.3 exists to produce; before it, a run could say what it cost but not which role spent it.

| Role | Charged to the agent's ledger | Calls | Tokens | USD | Model time (s) |
|---|---|---|---|---|---|
| `investigation_planner` | yes | 91 | 927396 | 2.000379 | 789.1 |
| `remediation_planner` | yes | 5 | 91359 | 0.324482 | 35.3 |
| `briefing_writer` | changed mid-phase (cmd #264) | 30 | 62066 | 0.250254 | 159.9 |
| `briefing_judge` | no — evaluator | 30 | 71141 | 0.092197 | 71.8 |
| `verification_judge` | yes | 5 | 8006 | 0.010042 | 8.1 |

Rolled up across every live run in scope, sorted by spend. `charged_to_ledger` is false for the EVALUATOR's roles: the briefing judge grades the run and is not part of it, so its dollars are real but are not the agent's budget. It reads `mixed` for `briefing_writer` because the answer changed inside this phase — cmd #264 decided the writer's prose is the agent's own and charged it — so the read-only pass, which ran before that commit, and the three legs, which ran after it, disagree. The column is the truth about the runs, not a rule. `elapsed_ms` is time inside the model call, deliberately not the run's wall clock — the loop also probes and waits.

Budget trips: none. Per-role LLM call counts are in the JSON companion under `sections.budget_profile_diff.per_run[].llm_calls_by_role`.

## 5. Judge calibration

**0 reruns required.** Plan 03 section 9 requires a calibration rerun for every judge the phase TOUCHED. Phase 2 touched none: no judge prompt, no rubric, no judge model pin. The two commits a reader might suspect are not judge changes — cmd #243 (WO-R2-174, in Phase 1) gave the judges the same bounded one-re-ask repair the planner call sites already had, on OUR OWN malformed output, and cmd #264 (WO-R3-260) charged the briefing WRITER to the run ledger and timed the planner call. Neither changes what a judge is asked or how its answer is scored, and the sweep agrees: all 41 rows judged, judge mean 0.8591463414634146, identical to the blessed baseline and to the Phase 1 close.

Evidence: Three checks, each re-runnable from this repository. (1) `git log 4d023e6..HEAD -- src/incident_commander/llm/prompts/` — one commit, cmd #258, and it edits the planner prompt, not a judge. (2) `git log 4d023e6..HEAD -- evals/graders/` — two commits, cmd #255 (the ROOT_CAUSE dimension, deterministic) and cmd #265 (ADR 0040's world scoping, deterministic); neither is a judge file. (3) `git show --stat f572108` (cmd #264) — it touches `llm/client.py`, `agent/accounting.py`, `agent/briefing_enrichment.py`, `agent/investigation.py`, `agent/strategies/` and `evals/runner.py`, and no prompt or grader at all. The digests below are the arithmetic form of (1): they are the same two values the Phase 1 close report recorded.

Judge prompt digests at assembly time:

- `briefing_judge.md` — `sha256:838a5ee5de6081c32ef1b7aba35aefe0ddd83826e841af2ca831ba76f4692719`
- `verification_judge.md` — `sha256:6d55bbfb6efebdaa6b5b032839094c9cf7ec0547377df74fcd595ffb9b93d1e3`

## 6. Baseline delta

Phase 0 baseline cited by id: `baseline_report.20260915T132421Z.ad634a458e5e`. The committed artifact `artifacts.newest('baseline_report')` resolves is `baseline_report.20260915T132550Z.2408b07ef532.json`; its offline leg is archive `2408b07ef532`. See deviation D8.

| Against | Regressions | Improvements | New | Dropped | Dropped dims | Vacated | Differing rows | Judge mean delta |
|---|---|---|---|---|---|---|---|---|
| blessed regression baseline (evals/reports/baseline.json, cmd #238) | 0 | 0 | 0 | 0 | 0 | 0 | 41 | 0.0 |
| Phase 0 baseline offline leg (2408b07ef532) | 0 | 0 | 0 | 0 | 0 | 0 | 41 | 0.0 |

Against the blessed regression baseline, 41 row(s) differ and every one of them differs in the same way: a dimension that did not exist in the baseline — `root_cause` (x41) — is now present and passing. Nothing else on those rows moved: same pass/fail, same terminal state, same tool-call count, same judge score. Unexplained differences: 0.

Against the Phase 0 baseline offline leg, 41 row(s) differ and every one of them differs in the same way: a dimension that did not exist in the baseline — `root_cause` (x41) — is now present and passing. Nothing else on those rows moved: same pass/fail, same terminal state, same tool-call count, same judge score. Unexplained differences: 0.

No unexplained movement, so nothing blocks the phase — but unlike Phase 1 the answer is not 'zero movement'. The gate's own axes are all clean on both baselines: no regression, no improvement, no new or dropped scenario, no dropped dimension, no vacated assertion, judge mean identical to the last digit, degraded count identical. What did move is every one of the 41 rows, in exactly one way: they now carry a ROOT_CAUSE dimension the baselines have no column for, and it passes on all 41. That is this phase's own new measurement appearing, not the corpus behaving differently — the ruler grew a mark, the thing being measured did not move — and the report says so by classifying the rows rather than by asserting it. Read honestly, all of this is still a statement about the CANNED corpus: the same fixtures replayed through the same graders under the same model id give the same answer. It is not evidence about the live world; the seeded legs are.

**The numbers this phase produced that no baseline can be compared to:**

| Number | Value | Sample size | Read it how |
|---|---|---|---|
| Canned root-cause accuracy, benchmark role | **32/32 (100%)** | 32 of the 41 scenarios; the other 9 declare no label on purpose | The canned suite is scripted — the planner's replies are fixtures, so this measures that the labels and the scripts agree, not that the agent diagnoses well. It is a floor for the harness, not a score for the model. |
| Live root-cause accuracy on the seeded legs | **3/3 (100%)** | 3 runs, 1 rep each, 3 fault families | Three samples. Far too small to generalise from — a single different trajectory would make it 2/3 — and the three scenarios were chosen one per fault family, not at random. It is the first VALID live diagnosis number this project has, and that is all it is. |

These have no Phase 0 counterpart to move against: the dimension and the labels that produce them both landed in this phase. They are stated with their sample size rather than left out, and every one of those samples is too small to generalise from.

Cost per scenario cannot move against either baseline: both baselines and this sweep are canned runs, and a canned run makes no billable call — every row's `usd_used` is 0.000000 on both sides. The live cost numbers are in section 7, and this phase is the first that can break them down by role.

## 7. Spend line

| # | Archive | Scenario | USD | Agent-loop s | Start to finish s | Tokens |
|---|---|---|---|---|---|---|
| 1 | `759e198cdd27` | `remediate_consumer_lag_success` | 0.237300 | 168.075 | 248.519 | 89069 |
| 2 | `648a32f2339d` | `remediate_stale_cache_success` | 0.145759 | 0.057 | 57.668 | 58163 |
| 3 | `fc896b25a09c` | `remediate_dlq_backlog_success` | 0.136289 | 22.496 | 31.005 | 43487 |
| — | `0db6fe722f7c` | read-only pass, 27 scenarios | 1.851162 | 212.36 | 1122.108 | 844715 |

**Live total: $2.370510**, 402.988 s of agent loop and 1459.299 s from each run's first trace record to its last. The canned sweep cost $0.000000. Free by construction: a canned run replays recorded tool responses through a canned LLM client, so nothing billable leaves the machine. The wall figure is the sum of the 41 rows' own ledgers, not the operator's clock.

**The bill for this close is $2.677354**: $2.370510 charged to the agents' own ledgers plus $0.306844 the eval harness spent grading them. Two totals, because there are two answers. `live_total_usd` is the sum of the runs' own budget ledgers — what the AGENT spent, and the number invariant 7 caps. `live_total_usd_including_evaluator` adds the eval harness's own briefing judge, which is real money off the same key but is not the agent's budget and would fail the ledger reconciliation if it were folded in. The second is the bill.

Against plan 03 section 11:

- **Planned:** remediation scenario: ~$0.10-0.20, 2-3 min with reset  
  **Actual:** 3 seeded live remediation runs, mean $0.173116 per run and 112 s mean start to finish. The mean is inside the band on both axes; one leg is not. `759e198cdd27` cost $0.237300 and ran 248 s, because it kept re-probing through three `not_verified` readings while it waited for the lag to fall. That is the loop doing the right thing, and the band is what needs widening if verify-and-wait becomes normal.
- **Planned:** read-only live scenario: ~$0.09, ~80 s, ~7 LLM calls  
  **Actual:** 27 read-only scenarios in one invocation for $2.145804 including the evaluator's judge — about $0.079 each, against a planned $0.09 — over 18.7 minutes, about 42 s each against a planned 80 s. Under the planned figure on both, and the only line of this close that compares like with like against plan 03's table.
- **Planned:** phase-close sweep (~40 scenarios x 3 reps): ~$15 per phase, +$5 live  
  **Actual:** $2.677354 in total. The reduction is decision O-20, not an underspend: recorded mode does not exist until Phase 3, so the 41-scenario sweep ran canned and free; three remediation scenarios ran live at one rep instead of twelve at three; and the read-only stage is one pass, not a matrix.
- **Planned:** per-scenario budget: 1.00 USD / 25 calls / 200 000 tokens / 600 s  
  **Actual:** No cap was approached on any of the 4 live invocations. Highest USD 0.237300 of 1.00; highest tokens 89 069 of 200 000; highest tool calls 9, against the 13 the remediation scenarios seed (the scenario cap is tighter than plan 03's 25); highest start-to-finish 248.5 s of 600.

The closed live-eval campaign cost about $7.20 and the Phase 1 close added $0.635. This close adds $2.677354, of which $2.370510 is the agents' own ledgers. Total wall clock across the 4 live invocations: 24.3 minutes of run time, operator time and world resets excluded because they are not in any archive.

## Deviations from the protocol as written

- **D1 — Reduced scope (owner decision O-20, 2026-09-17).** Plan 03 section 14 step 1 asks for every scenario touched or added in the phase in recorded mode at 3 reps, plus live for every scenario with a remediation leg. What ran: all 41 scenarios canned at 1 rep, one live read-only pass over 27 scenarios, and 3 of the 12 remediation-leg scenarios live at 1 rep - one per injected-fault family (kill_consumer, create_stale_cache, poison_message). The owner chose option A of `.coordination/PHASE2-CLOSE-PLAN.md` and kept the plan order.
- **D2 — Recorded-world mode still does not exist, so there is no 3-rep sweep.** It arrives in Phase 3 (WP-3.1). The sweep leg is canned at one rep, exactly as in Phase 1. Saying so is the point - a silent substitution would make every later phase's comparison unreadable. One rep also means no variance figure: the only repeated live scenario in this whole project is the one that went green then red.
- **D3 — The read-only live pass cannot yield a live root-cause number, by construction.** `make eval-smoke` runs read-only scenarios against the live stack and seeds no fault. A ground truth is a statement about ONE world (ADR 0040), so 16 of the 18 labelled rows in that pass report 'not graded - the label describes a world the run did not have'. The pass is still evidence: it is a live leak hunt over 27 trajectories, a live cost profile, and the run in which the PLATFORM refused every Tier-1 call that reached it. It is not a diagnosis measurement and this report does not use it as one.
- **D4 — Two of the three seeded legs were not green on their one rep. This is a DRAFT.** `remediate_stale_cache_success` (648a32f2339d) diagnosed correctly and never acted; `remediate_dlq_backlog_success` (fc896b25a09c) acted correctly and failed one evidence claim. Both fixes are merged - platform v0.6.8 plus commander cmd #269 for the first, ADR 0041's whole-queue guard (WO-R3-268) for the second - and NEITHER has been re-run live, because a live run needs the owner's explicit go and readiness is not authorization. Until those two archives exist, this close reports the runs it has.
- **D5 — Judge calibration was not rerun, and that is the protocol's own answer.** Plan 03 section 14 step 5 scopes the rerun to 'every judge the phase touched'. Phase 2 touched none, and section 5 shows the three checks that establish it rather than asserting it: the judge prompt digests are byte-identical to the ones the Phase 1 close recorded, no commit since that close touches a prompt other than the planner's (cmd #258), and the two commits a reader might suspect - cmd #243 (output repair) and cmd #264 (ledger and timing) - touch neither a prompt nor a grader. Step 5 is therefore legitimately empty, and says so rather than being omitted (WO-R3-195's own finding 7).
- **D6 — The read-only pass was bought on a recommendation that did not hold (INC-003).** The coordinator recommended it as 'the first live root-cause number' without checking which world the ground-truth labels described. $2.15 bought a figure - `root cause: 11/18 correct (61%)` - that had to be withdrawn, and seven false reds on a paid archive. The fix (WO-R3-265, ADR 0040) and the offline re-grade are in section 1; the rule is in LESSONS, 2026-09-17: before buying a run to measure X, check that the expected values for X were written about the world that run will be in. The money is in section 7 either way.
- **D7 — The canned sweep archive is committed with this report.** Offline archives are untracked by convention, but a report that reads an untracked archive can only be regenerated on the machine that ran it, and the regeneration test is what makes this document checkable. `b75527784077` was copied byte-identical from the locked original, verified file by file with sha256, and committed on its own - the same precedent as Phase 1's `32ae38f6b38b`.
- **D8 — The Phase 0 baseline artifact still exists twice; this report names both.** Unchanged since the Phase 1 close. WO-R3-189 cites `baseline_report.20260915T132421Z.ad634a458e5e`. That file is on disk in the main checkout but was never committed, and it sorts 89 seconds OLDER than its committed twin, so `artifacts.newest('baseline_report')` resolves the other one. Section 6 reads the committed, resolver-visible artifact and names both ids; their recorded numbers are identical, so the delta is the same either way.

## Follow-ups this close leaves open

- **WO-R3-266** (open) — Four EVIDENCE claims on the read-only path assert the world's CONTENTS, not the agent's conduct - the same class as INC-003 one layer down. `consumer_lag_missing_group` is the one still red after the re-grade.
- **WO-R3-267** (fix merged; re-run pending the owner's go) — The stale-cache sensor. FIXED: platform v0.6.8 reports `records_referenced` / `records_found`, commander re-pinned in cmd #269. What is left is the live re-run.
- **WO-R3-268 / O-21** (PR open at the time this draft was assembled; re-run pending the owner's go) — The agent must read the whole dead-letter queue once before replaying part of it (owner decision O-21, option 1). ADR 0041's guard.
- **O-19** (open — needs the owner) — Taxonomy and routing (WO-R3-263): no category for resource exhaustion, and `RUNAWAY_SAGA` routed by prompt hint but by FIX_MAP in code. Both change what the live agent is told.
- **FINDING — the agent-loop wall meter** (open — needs a work-order id) — `648a32f2339d`'s ledger records `wall_seconds_used` 0.057 s for a run that made seven model calls over 58 s of trace. The meter is not advanced on every path out of the loop, so the ledger's wall figure is a lower bound, not a measurement. Section 7 reports wall time from the trace records instead. No work order filed - raised by this close.
- **O-8** (open) — `bad_deploy`'s alert source vs the reset predicate — untouched by this close.

## What is still owed — why this is a draft

Each of these has a merged fix and a ready world, and neither has been measured. Nothing below may be treated as run until its archive id is in this report's scope:

- **`remediate_stale_cache_success`** — supersedes `648a32f2339d`. Platform v0.6.8 makes the cache-key reading say how many records the entry names and how many the database holds (healthy 3/3, stale 3/0); commander re-pinned in cmd #269 and the stack is up on it. Waiting on the owner's explicit go — readiness is not authorization.
- **`remediate_dlq_backlog_success`** — supersedes `fc896b25a09c`. ADR 0041's guard (WO-R3-268) requires an unfiltered `list_dlq_messages` before any DLQ replay, which is owner decision O-21 option 1. Waiting on the owner's explicit go — readiness is not authorization.

To produce the final version: add those two archive ids to this phase's `live_legs`, drop the matching `PendingRerun` entries, and run `make phase-close-report PHASE=2 WRITE=1`. The status flips to FINAL because it is computed from the scope, and the new document is written beside this one under a new timestamp — this draft is never overwritten (invariant 9).

## Was an INCIDENTS.md row filed?

**Yes** INC-003, filed 2026-09-17 BEFORE the fix, as the protocol requires. The read-only pass reported seven reds that were the evaluation being wrong about the agent, not the agent being wrong: canned-world labels applied to an unseeded live world. It is closed by cmd #265 and ADR 0040, and the offline re-grade is in section 1. The two red seeded legs get NO incident row: `648a32f2339d` graded red on OUTCOME with the agent's own trajectory agreeing (it never emitted `remediate`), and `fc896b25a09c`'s EVIDENCE red was carried to the owner as a grader-brittleness candidate and decided the other way (O-21) — the claim was right. In both cases the grader was right about the run.

