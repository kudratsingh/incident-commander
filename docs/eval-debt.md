# Eval-debt ledger

During the campaign eval freeze
([ADR 0011](ADR/0011-campaign-eval-freeze.md)), behavior-surface PRs merge without
pre-merge eval evidence. This ledger is where each one records its debt.

Every PR that touches an invariant-8 surface (prompts under
`src/incident_commander/llm/prompts/`, tool definitions/registry, policy tiers, memory
retrieval, pinned model config in `config.py`, `contracts/platform-tools.snapshot.json`,
`evals/scenarios/**` expectation changes) appends exactly one row here, in that same PR.
The table is append-only by convention: rows are never edited or removed. A row's number N
is its 1-based position in the table, cited by the PR's eval-impact line as
`EVAL FROZEN per ADR 0011 — not run; debt row N`. Dates are `YYYY-MM-DD`; the
post-campaign observable is the falsifiable check the restart run must confirm.

At the post-campaign eval restart, this ledger is walked row by row before the new
baseline is blessed — it is the answer to "what is the first eval run actually
validating?"

| date | PR | WO id(s) | surface touched | what changed | post-campaign observable |
|---|---|---|---|---|---|
| 2026-08-09 | #94 | WO-C5-04, WO-C5-05 | LLM boundary: InvestigationStep schema, probe execution path, evidence-value corpus | hypothesis ranking normalized at the schema boundary (confidence-descending, stable); probes tier-checked at runtime (non-read tool → escalate); corpus drops LLM-authored arguments and same-call result echoes | post-campaign live run shows no remediate/escalate gate mis-ordering and no evidence-sourcing false accept/reject; offline canned suite stays 37/37 (canned rankings are pre-sorted, so offline outcomes are unchanged — `test_shipped_scenarios_pass`) |
| 2026-08-09 | #96 | WO-C1-02 | regression gate exit policy (`evals/regression.py` main) + Makefile ONLY guards on `eval-reg`/`baseline` | gate exits 1 on dropped baseline scenarios, exits 2 refusing a filtered (`only_patterns`) latest.json, warns without gating on `degraded_count` mismatch/unknown; `make eval-reg ONLY=` and `make baseline ONLY=` refuse at parse time before the eval prerequisite | restart-protocol harness self-check (before the gate guards anything): in a throwaway worktree, offline `make eval ONLY=x` then `make eval-reg` exits 2 refusing the filtered report, and `make eval-reg ONLY=x` / `make baseline ONLY=x` refuse at parse time with nothing run; run-2's gate passes only on a full 37-scenario report |
| 2026-08-09 | #99 | WO-C6-04 | tool definitions/registry (`GetConsumerLagInput`) | agent-side input validation loosened to match the snapshot: removed `min_length=1` on `consumer_group` (validation-only — the default value is unchanged and `min_length` is never serialized, so outgoing wire bytes for defaulted calls are unchanged) | Phase-6 rebless input-model diff shows zero drift on all 26 shared tools, and live `get_consumer_lag` probes on arbitrary group names return `lag:null` without agent-side rejection |
| 2026-08-09 | #102 | WO-C3-01 | `evals/scenarios/**` expectations: `budget.max_tool_calls` on the nine remediation-class scenarios | grading caps for the nine scenarios that declare `expected_action_tools` raised 8/12 → 13 to match the documented live polling profile (2 probes + 1 ADR-0009 re-probe + 1 action + up to 6 ADR-0006 verify polls = 10 calls); strict loosening, runtime `BudgetLedger` ceiling untouched | all nine recalibrated scenarios pass the BUDGET dimension at live knobs (`VERIFY_PROBE_ATTEMPTS=6`, `INVESTIGATE_REPROBE_ATTEMPTS=1`) with `tool_calls_used <= 13` |
| 2026-08-09 | #108 | WO-C3-02 | runtime budget enforcement + pinned per-model price map (`src/incident_commander/llm/pricing.py`, `agent/**`) | `wall_seconds_used` and `usd_used` gained their first writers, so all four invariant-7 dimensions are now trippable (wall accrues from `created_at`, USD from a pinned in-repo rate map); `tokens_used` became total volume incl. cache-creation/cache-read, which it previously dropped | live run reports non-zero `wall_seconds_used` and `usd_used` in its report and briefing (both were structurally `0.0`/`"0"`), and no scenario trips BUDGET on the wall or dollar dimension at the documented knobs (1800s / $5.00); offline canned suite unchanged (`CannedLLMClient` reports zero usage — `test_shipped_scenarios_pass`) |
