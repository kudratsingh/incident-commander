# Phase 1 close report

**CLOSING** — every run in scope was made under the benchmark model role

Protocol: docs/plans/research-buildout-v2.1/03_EVAL_RESEARCH_PLAN.md section 14. Work order: WO-R3-189 (WP-1.7). Scope: REDUCED close, owner decisions O-14 (benchmark model) and O-15 (scope).

Zero live invocations and zero LLM calls produced this document. Every number was read out of a committed archive; the only thing executed is the `grep` in section 2.

## What this close does and does not claim

**It claims:**

- The 41-scenario canned corpus passes under the benchmark model role and moves nothing against either baseline.
- On three live remediation scenarios, one per injected-fault family, the agent found the fault, crossed the remediation gate on a hypothesis that was in FIX_MAP and above the 0.7 bar, acted once on the right resource, and verified it.
- WP-1.5's acceptance holds on live evidence: no platform tool response any of these runs read contains `chaos`, any root-cause label, or any fixture name.
- The one live red of this phase is understood, root-caused to a sensor that could not show time passing, fixed in platform v0.6.7, and re-run green.

**It does not claim:**

- That the agent is stable on these scenarios. One rep is one sample of a trajectory; the 2026-08-31 green on scenario 1 turned red on the same model, and the difference was an accident of confidence (LESSONS, 2026-09-17).
- That the remaining nine remediation-leg scenarios behave the same way live. They were not run in this phase.
- That the eight live greens of the earlier campaign say anything about the leak. They ran under the OLD all-scope agent token; they show the corpus runs live and nothing more.
- Anything about `bad_deploy` or owner decision O-8.
- That the phase's spend of $0.635054 is comparable to plan 03 section 11's ~$20 phase-close row. It is a different, smaller sweep.

Phase 1 closed on this sample, not on the plan's full sweep. The differences are listed as deviations at the end, each with the decision or the fact behind it.

## 1. Sweep results

**Canned sweep `32ae38f6b38b`** — 41/41 passed, 0 failed, judged 41, judge mean 0.8591463414634146, degraded 34. Model role(s): benchmark; agent model(s): claude-sonnet-4-6.

What the gate says about it:

- `no changes vs baseline`
- `closing: every run in latest was made under the benchmark model role`
- `cross-model refusal: none — baseline and sweep name one agent model`

**Live legs** — 3/4 passed, in the order the owner released them.

| # | Archive | Scenario | Result | Judge | Agent tool calls | Commander | Platform |
|---|---|---|---|---|---|---|---|
| 1 | `42000dfda188` | `remediate_consumer_lag_success` | **FAIL** (escalated) | 0.8999999999999999 | 5 | `d513d8e` | `sha256:29af4263bfcc` |
| 2 | `845bdae22195` | `remediate_consumer_lag_success` | PASS (resolved) | 0.935 | 5 | `a2b9636` | `sha256:8f3cbcff426a` |
| 3 | `ee183c85429c` | `remediate_stale_cache_success` | PASS (resolved) | 0.925 | 4 | `5a7a423` | `sha256:8f3cbcff426a` |
| 4 | `47abb70a2b9e` | `remediate_dlq_backlog_success` | PASS (resolved) | 1.0 | 4 | `c7f00e8` | `sha256:8f3cbcff426a` |

- `42000dfda188` — RED. Correct top hypothesis at 0.75 for all five planner steps and never a `remediate` step: three lag re-reads inside 25 s returned the same cached 29, so the agent could not see the metric move. Root cause WO-R3-254 — the alert says lag is CLIMBING and the only sensor returned one undated number. Fixed in platform v0.6.7 (plat #204): `get_consumer_lag` now returns `measured_at`, `age_seconds` and `recent_samples`. Commander re-pinned in cmd #245.
- `845bdae22195` — GREEN on the re-run, on platform v0.6.7. Read the trend 0 -> 16 -> 35 in one call, went 0.75 -> 0.82, restarted `worker-dispatcher`, verify passed on the second reading. The root fix is proven by this run, not argued.
- `ee183c85429c` — GREEN. Read the named hot key, dipped to 0.65 and probed Redis health rather than acting on a dip, came back at 0.75, invalidated exactly that key, verified on the key's own state (`exists: false`).
- `47abb70a2b9e` — GREEN. Replayed exactly the one `replay_safe` row by id and left the unclassified poison row alone. Ran only after WO-R3-251 (plat #203, v0.6.6) took `(chaos poison_message on topic ...)` out of the DLQ error text, which would otherwise have failed this scenario's own leak check.

All five archives are committed in this repository; every number above is read out of them and none is typed in.

## 2. Leak hunt

65 terms in three families, all derived rather than typed: `chaos`; every `HypothesisCategory` value; every scenario name and every chaos hook name in the corpus. Matching is case-insensitive substring, so a term glued to something else still counts — and the false positives that produces are adjudicated below by name rather than filtered away.

**The gating grep** — WP-1.5's own acceptance wording, over all 45 trajectories this phase produced (4 live + 41 canned):

```console
$ grep -rIni -e chaos evals/runs/42000dfda188/trajectories evals/runs/845bdae22195/trajectories evals/runs/ee183c85429c/trajectories evals/runs/47abb70a2b9e/trajectories evals/runs/32ae38f6b38b/trajectories
# exit status 1 (0 matching line(s))
```

Same files, split by who authored each ledger row — because a trajectory is full of root-cause labels by design (they are the agent's own hypotheses), and the only thing that can fail the phase is a term arriving in a **platform tool response**:

| Bucket | Hits |
|---|---|
| platform responses the agent read | {'unknown': 2} |
| text the agent or the harness wrote | {'consumer_saturation': 54, 'deploy_regression': 6, 'no_fault': 2, 'persistent_data_bug': 12, 'poison_message': 41, 'redis_saturation': 4, 'runaway_saga': 8, 'stale_cache': 17, 'transient_dependency': 7, 'unknown': 13} |

Every hit in what the agent read, adjudicated:

- `unknown` x2 in `get_consumer_lag` (consumer_lag_missing_group.json): `{"consumer_group":"unknown-consumer","lag":null,"lag_known":false,"source":"unrecognized","cache_key":"kafka:consumer_la`

**Adjudication.**

- `unknown` — The substring `unknown` inside the consumer group id `unknown-consumer` — the platform's own value for a group it does not recognise, and the entire subject of `consumer_lag_missing_group`. A substring collision with the `unknown` root-cause label, not a leaked label.

Unadjudicated hits: none. **Leak-hunt verdict: PASS.** No `chaos` token, no root-cause label and no fixture name reaches the agent from the platform in any of the 45 trajectories.

**Where `chaos` does appear**, and which side of the boundary each occurrence is on. The traces are the wider record — they hold our own prompts and the evaluator's bookkeeping as well as the wire:

```console
$ grep -rIoin -e chaos evals/runs/42000dfda188/traces/remediate_consumer_lag_success.jsonl evals/runs/845bdae22195/traces/remediate_consumer_lag_success.jsonl evals/runs/ee183c85429c/traces/remediate_stale_cache_success.jsonl evals/runs/47abb70a2b9e/traces/remediate_dlq_backlog_success.jsonl
evals/runs/42000dfda188/traces/remediate_consumer_lag_success.jsonl:2:chaos
evals/runs/42000dfda188/traces/remediate_consumer_lag_success.jsonl:2:chaos
evals/runs/845bdae22195/traces/remediate_consumer_lag_success.jsonl:2:chaos
evals/runs/845bdae22195/traces/remediate_consumer_lag_success.jsonl:2:chaos
evals/runs/845bdae22195/traces/remediate_consumer_lag_success.jsonl:18:chaos
evals/runs/ee183c85429c/traces/remediate_stale_cache_success.jsonl:2:chaos
evals/runs/ee183c85429c/traces/remediate_stale_cache_success.jsonl:10:chaos
evals/runs/47abb70a2b9e/traces/remediate_dlq_backlog_success.jsonl:2:chaos
evals/runs/47abb70a2b9e/traces/remediate_dlq_backlog_success.jsonl:13:chaos
# exit status 0 (9 matching line(s))
```

| Author | `chaos` | What it is | Gating? |
|---|---|---|---|
| platform response | 0 | bytes a platform tool returned to the agent | **yes** |
| commander prompt | 3 | our own `remediation_planner.md`: "...lives in the description of a chaos tool you never see". Our text, naming no fault. WO-R3-255. | no |
| harness record | 6 | the evaluator's `chaos_setup` trace record and the `chaos:kill:...` key it wrote. Never shown to the agent. | no |
| agent output | 0 | what the model wrote, plus its own ledger replayed into the next request | no |

The gating bucket is **empty in every live run**: nothing the agent read from the platform contained the word. That is WP-1.5's acceptance, claimed here on live evidence rather than on the test that motivated it.

Per-run counts for all 65 terms are in the JSON companion under `sections.leak_hunt.traces_by_author.per_run`.

## 3. Gate-crossing audit

Three structural gates stand between a planner's `remediate` step and a Tier-1 call: the category must be a key in FIX_MAP, the top confidence must be at or above the bar, and the alert's own subject must already have been probed. The first two escalate when they fail; the third refuses and re-steers, which is why a refusal is not a failed run.

The bar is 0.7. FIX_MAP: `consumer_saturation`, `poison_message`, `runaway_saga`, `stale_cache`.

Across the four live runs: 15 planner steps, 4 `remediate` steps emitted, 3 crossed into planning, 1 refused and re-steered, 1 escalation. Every crossing is tabulated below with the two numeric gates it had to pass, and the red run's five non-crossings are tabulated the same way.

### `42000dfda188` — `remediate_consumer_lag_success` -> escalated (expected resolved)

5 planner step(s); 0 `remediate` emitted, 0 crossed, 0 refused; 5 step(s) where every gate was open and the planner probed instead.

| Step | Top hypothesis | Category | Confidence | In FIX_MAP | >= bar | Emitted | Disposition |
|---|---|---|---|---|---|---|---|
| 1 | worker-dispatcher-stalled-lag-climbing | `consumer_saturation` | 0.75 | yes | yes | `probe` | no handoff attempted (probe step) |
| 2 | worker-dispatcher-lag-29-climbing-stalled | `consumer_saturation` | 0.75 | yes | yes | `probe` | no handoff attempted (probe step) |
| 3 | worker-dispatcher-lag-29-climbing-stalled | `consumer_saturation` | 0.75 | yes | yes | `probe` | no handoff attempted (probe step) |
| 4 | worker-dispatcher-lag-29-stalled | `consumer_saturation` | 0.75 | yes | yes | `probe` | no handoff attempted (probe step) |
| 5 | worker-dispatcher-lag-29-stalled | `consumer_saturation` | 0.75 | yes | yes | `probe` | no handoff attempted (probe step) |

Escalation: `max iterations (5) exceeded`.

Justified by ground truth? NOT justified. The scenario's ground truth expects 'resolved', and on 5 planner step(s) the top hypothesis was both in FIX_MAP and at or above the 0.7 bar — every gate was open and the planner chose `probe` anyway. Not laziness in the grader's sense either: the agent could not see the metric move (WO-R3-254).

### `845bdae22195` — `remediate_consumer_lag_success` -> resolved (expected resolved)

3 planner step(s); 1 `remediate` emitted, 1 crossed, 0 refused; 2 step(s) where every gate was open and the planner probed instead.

| Step | Top hypothesis | Category | Confidence | In FIX_MAP | >= bar | Emitted | Disposition |
|---|---|---|---|---|---|---|---|
| 1 | worker-dispatcher-stalled-lag-climbing | `consumer_saturation` | 0.75 | yes | yes | `probe` | no handoff attempted (probe step) |
| 2 | worker-dispatcher-lag-35-climbing | `consumer_saturation` | 0.75 | yes | yes | `probe` | no handoff attempted (probe step) |
| 3 | worker-dispatcher-lag-climbing-35-in-2min | `consumer_saturation` | 0.82 | yes | yes | `remediate` | CROSSED into planning |

Plan: `restart_consumer_group({"consumer_group": "worker-dispatcher"})` targeting `worker-dispatcher-lag-climbing-35-in-2min`, verified with `get_consumer_lag`.

Executed: `restart_consumer_group` (1 call(s)).

Verify: **not_verified**, **verified**.

### `ee183c85429c` — `remediate_stale_cache_success` -> resolved (expected resolved)

3 planner step(s); 1 `remediate` emitted, 1 crossed, 0 refused; 1 step(s) where every gate was open and the planner probed instead.

| Step | Top hypothesis | Category | Confidence | In FIX_MAP | >= bar | Emitted | Disposition |
|---|---|---|---|---|---|---|---|
| 1 | hot-set-cache-key-missing-or-expired | `stale_cache` | 0.75 | yes | yes | `probe` | no handoff attempted (probe step) |
| 2 | hot-set-cache-miss-spike-worker-dispatcher | `stale_cache` | 0.65 | yes | no | `probe` | no handoff attempted (probe step) |
| 3 | cache-miss-spike-worker-dispatcher-hot_set | `stale_cache` | 0.75 | yes | yes | `remediate` | CROSSED into planning |

Plan: `invalidate_cache_key({"key": "cache:jobs:worker-dispatcher:hot_set"})` targeting `cache-miss-spike-worker-dispatcher-hot_set`, verified with `get_cache_key_info`.

Executed: `invalidate_cache_key` (1 call(s)).

Verify: **verified**.

### `47abb70a2b9e` — `remediate_dlq_backlog_success` -> resolved (expected resolved)

4 planner step(s); 2 `remediate` emitted, 1 crossed, 1 refused; 2 step(s) where every gate was open and the planner probed instead.

| Step | Top hypothesis | Category | Confidence | In FIX_MAP | >= bar | Emitted | Disposition |
|---|---|---|---|---|---|---|---|
| 1 | dlq-depth-warning-replay-safe-messages | `poison_message` | 0.7 | yes | yes | `probe` | no handoff attempted (probe step) |
| 2 | mixed-dlq-5-rows-replay-safe-wait-human-unclassified | `poison_message` | 0.92 | yes | yes | `remediate` | REFUSED and re-steered |
| 3 | dlq-replay-safe-slice-upstream-timeout | `poison_message` | 0.85 | yes | yes | `probe` | no handoff attempted (probe step) |
| 4 | replay-safe-bulk-api-sync-upstream-timeout | `poison_message` | 0.95 | yes | yes | `remediate` | CROSSED into planning |

Plan: `replay_dlq_by_ids({"job_ids": ["fc8d2a03-23b3-5371-9acb-46443c73baa5"]})` targeting `replay-safe-bulk-api-sync-upstream-timeout`, verified with `list_dlq_messages`.

Executed: `replay_dlq_by_ids` (1 call(s)).

Verify: **verified**.

## 4. Budget profile diff — benchmark vs development

Owner decision O-14 keeps BENCHMARK_MODEL at `claude-sonnet-4-6`, which is also DEVELOPMENT_MODEL. Both roles resolve to the same model id, so a benchmark-vs-development token or tool-call diff is not small — it is definitionally zero, and printing a table of zeroes would read like a measurement. What can be reported is the profile itself, per run, so the next phase (or the next model) has something to diff against.

| Archive | Scenario | LLM calls | Planner iterations | Wire tool calls | Billed tool calls | Tokens | Wall s |
|---|---|---|---|---|---|---|---|
| `42000dfda188` | `remediate_consumer_lag_success` | 7 | 5 | 12 | 5 | 57094 | 90.232063 |
| `845bdae22195` | `remediate_consumer_lag_success` | 8 | 3 | 14 | 5 | 55622 | 155.613375 |
| `ee183c85429c` | `remediate_stale_cache_success` | 7 | 3 | 5 | 4 | 52636 | 35.335445 |
| `47abb70a2b9e` | `remediate_dlq_backlog_success` | 8 | 4 | 6 | 4 | 68283 | 51.840843 |

Every run stayed inside every cap. `mcp_calls_in_trace` counts more calls than `agent_tool_calls_billed` because the runner's own precondition poll and the post-action verify probe are on the wire but are not the agent's budget.

Budget trips: none. Per-role LLM call counts are in the JSON companion under `sections.budget_profile_diff.per_run[].llm_calls_by_role`.

## 5. Judge calibration

**0 reruns required.** Plan 03 section 9 requires a calibration rerun for every judge the phase TOUCHED. Phase 1 touched no judge prompt and no rubric. The one judge-side change in the phase is WO-R2-174 (cmd #243), which gave the verification judge and the eval briefing judge the same bounded output repair the three planner call sites already had: one re-ask on OUR OWN malformed output, same cap, same wrapper. It changes what happens when a reply fails schema validation, not what the judge is asked or how its answer is scored — and the canned sweep shows it: all 41 rows judged, judge mean identical to the blessed baseline to the last digit.

Evidence: `git log <phase0 baseline>..HEAD -- src/incident_commander/llm/prompts/` is empty; the only commit under `evals/graders/` is cmd #243 and it touches `evals/graders/llm_judge.py` alone.

Judge prompt digests at assembly time:

- `briefing_judge.md` — `sha256:838a5ee5de6081c32ef1b7aba35aefe0ddd83826e841af2ca831ba76f4692719`
- `verification_judge.md` — `sha256:6d55bbfb6efebdaa6b5b032839094c9cf7ec0547377df74fcd595ffb9b93d1e3`

## 6. Baseline delta

Phase 0 baseline cited by id: `baseline_report.20260915T132421Z.ad634a458e5e`. The committed artifact `artifacts.newest('baseline_report')` resolves is `baseline_report.20260915T132550Z.2408b07ef532.json`; its offline leg is archive `2408b07ef532`. See deviation D5.

| Against | Regressions | Improvements | New | Dropped | Dropped dims | Vacated | Differing rows | Judge mean delta |
|---|---|---|---|---|---|---|---|---|
| blessed regression baseline (evals/reports/baseline.json, cmd #238) | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0.0 |
| Phase 0 baseline offline leg (2408b07ef532) | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0.0 |

Zero movement, on both baselines and on every axis the gate measures: no regression, no improvement, no new or dropped scenario, no dropped dimension, no vacated assertion, and not one of the 41 rows differs in pass/fail, terminal state, tool-call count, per-dimension result or judge score. There is therefore no unexplained movement to block the phase. Read honestly, that is a statement about the CANNED corpus: the same fixtures replayed through the same graders under the same model id give the same answer, which is what a regression baseline is for. It is not evidence about the live world; the live legs are.

Cost per scenario cannot move against either baseline: both baselines and this sweep are canned runs, and a canned run makes no billable call — every row's `usd_used` is 0.000000 on both sides. The live cost numbers are in section 7.

## 7. Spend line

| # | Archive | Scenario | USD | Agent-loop s | Start to finish s | Tokens |
|---|---|---|---|---|---|---|
| 1 | `42000dfda188` | `remediate_consumer_lag_success` | 0.134020 | 90.232 | 157.99 | 57094 |
| 2 | `845bdae22195` | `remediate_consumer_lag_success` | 0.157086 | 155.613 | 190.011 | 55622 |
| 3 | `ee183c85429c` | `remediate_stale_cache_success` | 0.153772 | 35.335 | 44.424 | 52636 |
| 4 | `47abb70a2b9e` | `remediate_dlq_backlog_success` | 0.190176 | 51.841 | 66.425 | 68283 |

**Live total: $0.635054**, 333.021 s of agent loop and 458.849 s from each run's first trace record to its last. The canned sweep cost $0.000000. Free by construction: a canned run replays recorded tool responses through a canned LLM client, so nothing billable leaves the machine. The wall figure is the sum of the 41 rows' own ledgers, not the operator's clock.

Against plan 03 section 11:

- **Planned:** remediation scenario: ~$0.10-0.20, 2-3 min with reset  
  **Actual:** 4 live remediation runs, $0.635054 total, mean $0.158764 per run; 115 s mean start to finish. Inside the band on cost; faster than the band on time because the band includes the world reset, which is operator wall clock and not in any archive.
- **Planned:** phase-close sweep (~40 scenarios x 3 reps): ~$15 per phase, +$5 live  
  **Actual:** $0.635054. The reduction is decision O-15, not an underspend: recorded mode does not exist until Phase 3, so the 41-scenario sweep ran canned and free; three remediation scenarios ran live at one rep instead of twelve at three; and there was no read-only live stage.
- **Planned:** per-scenario budget: 1.00 USD / 25 calls / 200 000 tokens / 600 s  
  **Actual:** No cap was approached. Highest USD 0.190176 of 1.00; highest tokens 68 283 of 200 000; highest tool calls 5 of the 13 these scenarios seed (the scenario cap is tighter than plan 03's 25); highest wall 155.6 s of 600.

The closed live-eval campaign cost about $7.20 in total. This close adds $0.635054 to that.

## Deviations from the protocol as written

- **D1 — Reduced scope (owner decision O-15, 2026-09-16).** Plan 03 section 14 step 1 asks for every scenario touched or added in the phase in recorded mode at 3 reps, plus live for every scenario with a remediation leg. What ran: all 41 scenarios canned at 1 rep, and 3 of the 12 remediation-leg scenarios live at 1 rep - one per injected-fault family (kill_consumer, create_stale_cache, poison_message). There was no separate read-only live stage.
- **D2 — Recorded-world mode does not exist yet.** It arrives in Phase 3 (WP-3.1). Phase 1 therefore cannot run 'recorded mode, 3 reps' and does not pretend to: the sweep leg is canned. Saying so is the point - a silent substitution would make every later phase's comparison unreadable.
- **D3 — Live run 1's world reset was not the runbook's exact chain.** The runbook chains `make eval-live ... && make eval-reset`, so the reset runs only if the run succeeded. The wrapper used for run 1 (archive 42000dfda188) reset unconditionally. Disclosed to the owner at the time; runs 2, 3 and 4 used the exact chain. No evidence was lost - the archive was written before the reset - but the deviation is recorded rather than smoothed over.
- **D4 — No live scenario fires `bad_deploy`, so owner decision O-8 is not informed.** O-8 asks whether renaming `bad_deploy`'s alert source breaks `make eval-reset` (the reset predicate matches `source LIKE 'chaos:%'`). Nothing in this close exercises that path, so this close says nothing about it. O-8 stays open.
- **D5 — The Phase 0 baseline artifact exists twice; this report names both.** WO-R3-189 cites `baseline_report.20260915T132421Z.ad634a458e5e`. That file is on disk in the main checkout but was never committed, and it sorts 89 seconds OLDER than its committed twin, so `artifacts.newest('baseline_report')` resolves the other one. Section 6 reads the committed, resolver-visible artifact and names both ids; their recorded numbers are identical (41/41, judge mean 0.8591463414634146, degraded 34), so the delta is the same either way.

## Follow-ups this close leaves open

- **WO-R3-253** (open) — Prose gloss for the generated remediation table (from WO-R2-177).
- **WO-R3-255** (open) — The commander's own remediation-planner prompt says '...lives in the description of a chaos tool you never see'. It is our text, not the platform's, and it names no fault - but it is the last `chaos` token on the agent's side of the boundary.
- **O-8** (open) — `bad_deploy`'s alert source vs the reset predicate — untouched by this close.

## Was an INCIDENTS.md row filed?

**No.** No grader or judge drift was found. The one red run graded RED on OUTCOME with the agent's own trajectory agreeing — it never emitted `remediate` and never acted — so the grader was right and `context/INCIDENTS.md` (which records the times the EVALUATION was wrong about the agent) gets no row. The cause was the agent plus a sensor that could not show time passing, and it is filed as WO-R3-254 with the platform fix already shipped.

