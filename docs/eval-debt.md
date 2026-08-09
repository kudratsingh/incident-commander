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
