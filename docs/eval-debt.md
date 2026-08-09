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
