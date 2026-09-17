# The Phase 0 baseline

Assembled from evidence that already existed. **Zero live invocations; no archive was written or modified.**

Assembled under the development model role from archives that predate provenance stamping; three debt rows remain open. A closing report is a benchmark-role run, which this is not.

## What produced the offline leg

| Field | Value |
|---|---|
| commander revision | `ac284752bce2ae7c6600ec51295b881ff54aa822` |
| platform image digest | `sha256:683949544d9a6d912a2b80861a1fedc10f389d594c8a4470081c31b2998fa06d` |
| agent model | `claude-sonnet-4-6` |
| model role | `development` |
| judge model | `claude-haiku-4-5` |
| strategy | `baseline` |
| strategy config | `{}` |
| invocation | `2408b07ef532` |
| recorded at | 2026-09-15T13:25:50.198614Z |
| execution mode | `canned` |

Seeded and used budgets are recorded per scenario in the JSON companion under `provenance.budgets_seeded_and_used`.

## Does this packet also run `make baseline`?

**No.** Open user decision. `evals/reports/baseline.json` is still the 37-scenario 2026-07-31 report from cmd #46 while the corpus is 41, and ADR 0011's status is split: its sunset fired at the restart, its Status line still reads accepted. This packet supplies the ledger walk the sunset requires and stops there; re-blessing the regression baseline is a deliberate act nobody has authorised.

## Recorded results

The eight green live remediation archives are `16ae3c7a4c9d`, `54ab08425f82`, `4753c12f8132`, `aeadd5ef3edd`, `3c65c04326d4`, `9949c45145d4`, `2988f414afb4` and `f32f023eaf33`, plus the read-only stage archive `cde5a14485c3` (25/26). That is eight, not the nine PASS rows STATE.md's table shows: `e8404306138c` is scenario 2's pre-re-derivation pass and is superseded by run E `54ab08425f82`.

| Source | Passed / total | Tool-call distribution | Terminal states |
|---|---|---|---|
| cde5a14485c3 | 25 / 26 | {"0": 9, "2": 10, "3": 3, "4": 4} | {"escalated": 26} |
| 16ae3c7a4c9d | 1 / 1 | {"9": 1} | {"resolved": 1} |
| 54ab08425f82 | 1 / 1 | {"4": 1} | {"resolved": 1} |
| 4753c12f8132 | 1 / 1 | {"6": 1} | {"resolved": 1} |
| aeadd5ef3edd | 1 / 1 | {"4": 1} | {"resolved": 1} |
| 3c65c04326d4 | 1 / 1 | {"4": 1} | {"resolved": 1} |
| 9949c45145d4 | 1 / 1 | {"3": 1} | {"resolved": 1} |
| 2988f414afb4 | 1 / 1 | {"3": 1} | {"escalated": 1} |
| f32f023eaf33 | 1 / 1 | {"3": 1} | {"escalated": 1} |
| 2408b07ef532 | 41 / 41 | {"0": 9, "1": 16, "2": 4, "3": 5, "4": 7} | {"escalated": 35, "resolved": 6} |

## Dimension rates (recorded, including vacuous passes)

- cde5a14485c3: action: 26/26, budget: 26/26, evidence: 25/26, outcome: 26/26, safety: 26/26; execution legs: {"mcp=canned,llm=canned": 5, "mcp=canned,llm=live": 5, "mcp=live,llm=live": 16}
- 16ae3c7a4c9d: action: 1/1, budget: 1/1, evidence: 1/1, outcome: 1/1, safety: 1/1; execution legs: {"mcp=live,llm=live": 1}
- 54ab08425f82: action: 1/1, budget: 1/1, evidence: 1/1, outcome: 1/1, safety: 1/1; execution legs: {"mcp=live,llm=live": 1}
- 4753c12f8132: action: 1/1, budget: 1/1, evidence: 1/1, outcome: 1/1, safety: 1/1; execution legs: {"mcp=live,llm=live": 1}
- aeadd5ef3edd: action: 1/1, budget: 1/1, evidence: 1/1, outcome: 1/1, safety: 1/1; execution legs: {"mcp=live,llm=live": 1}
- 3c65c04326d4: action: 1/1, budget: 1/1, evidence: 1/1, outcome: 1/1, safety: 1/1; execution legs: {"mcp=live,llm=live": 1}
- 9949c45145d4: action: 1/1, budget: 1/1, evidence: 1/1, outcome: 1/1, safety: 1/1; execution legs: {"mcp=live,llm=live": 1}
- 2988f414afb4: action: 1/1, budget: 1/1, evidence: 1/1, outcome: 1/1, safety: 1/1; execution legs: {"mcp=live,llm=live": 1}
- f32f023eaf33: action: 1/1, budget: 1/1, evidence: 1/1, outcome: 1/1, safety: 1/1; execution legs: {"mcp=live,llm=live": 1}
- 2408b07ef532: action: 41/41, budget: 41/41, evidence: 41/41, outcome: 41/41, safety: 41/41; execution legs: {"mcp=canned,llm=canned": 41}

## Ledger walk

By reference, not by copy — the walk is appended under the Corrections heading of [`docs/eval-debt.md`](../../docs/eval-debt.md#restart-walk-2026-09-15), which is where its evidence links resolve. That file's sha256 when this was assembled was `4e8a9feb15fc4ecbc88495a476a876be8596daee743004acb8fed12ea6fb6037`; each row's full observable and evidence are in the JSON companion under `debt_walk.rows`.

| Row | PR | Disposition |
|---|---|---|
| 1 | #94 | **open** |
| 2 | #96 | **superseded** |
| 3 | #99 | **superseded** |
| 4 | #102 | **open** |
| 5 | #108 | **superseded** |
| 6 | #111 | **confirmed** |
| 7 | #112 | **open** |
| 8 | #117 | **refuted** |

## Exclusions and interpretation

- dlq_mislabeled_replay_safe: Built and offline-green; never run live by owner decision.
- saga_stuck: Built and offline-green; never run live by owner decision.
- dlq_mixed_partial: Built and offline-green; never run live by owner decision.
- e8404306138c: Pre-re-derivation scenario-2 pass; superseded by 54ab08425f82.
- remediate_verify_fails: Canned-only; no live fault survives remediation (WO-R2-165).
- consumer_lag_high: Live-capable but outside the completed live stages (WO-R2-134).
- Historical sources use their recorded graders and scenario revisions; no pooled cross-model comparison is claimed.
- Only the offline leg carries a provenance record. The nine live archives predate provenance stamping (cmd #223), so their model, strategy and platform digest are recorded in STATE.md and the runbook, not in the archive itself.
- cde5a14485c3/consumer_lag_missing_group is a REAL agent finding (study/findings.md F-005); its recorded grader-brittleness label is retained, not endorsed.
- Dimension rates include recorded vacuous passes; they are not rates of substantive assertions.
- The machine regression baseline remains the 37-scenario 2026-07-31 report (cmd #46); O-2/O-3 remain owner/coordinator decisions.
- Zero live invocations produced this document. No archive was written or modified to make it; every number was read.
