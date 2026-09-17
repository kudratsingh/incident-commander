# Documentation map

One line per document, so you can find the right one without opening five.

[`CLAUDE.md`](../CLAUDE.md) at the repo root is the constitution — invariants, architecture, phase plan, conventions — and is the thing to read before changing anything. [`README.md`](../README.md) is the front door. Everything longer lives here.

## How it works, and what it is allowed to do

| Document | What it answers |
|---|---|
| [safety-model.md](safety-model.md) | The enforceable safety contract: trust boundaries, the READ / Tier-1 / Tier-2 ladder, what each action can touch, budgets, the kill switch, fail-open behaviour |
| [architecture-principles.md](architecture-principles.md) | The rules that came out of past pull requests — structural fixes over prose, single source of truth for mappings. Read before touching schemas, prompts or the state machine |
| [eval-methodology.md](eval-methodology.md) | How the harness measures: scenario taxonomy, grader design, metric definitions, judge pinning, the regression gate |
| [runbook.md](runbook.md) | Operating the agent: live-eval procedure, bumping the pinned platform image, rollbacks, the kill switch |

## Decisions

| Document | What it answers |
|---|---|
| [ADR/](ADR/) | Why the agent is shaped the way it is. [ADR/README.md](ADR/README.md) indexes all 46 with their statuses and records which record later amended which |

## History — read as a record, not as current state

These are dated. They describe what was true when they were written, and they are kept that way on purpose.

| Document | What it answers |
|---|---|
| [lessons/phase-6-hardening.md](lessons/phase-6-hardening.md) | A bug shipped in Phase 2, hidden for four months, and why the fix was a schema tightening rather than a prompt tweak |
| [lessons/live-eval-noise-sources.md](lessons/live-eval-noise-sources.md) | The taxonomy of why live runs fail differently every time. Read this first if a run is failing in a new way |
| [lessons/live-campaign-2026-08-03.md](lessons/live-campaign-2026-08-03.md) | What one night of live evaluation actually bought, ranked |
| [lessons/live-eval-sequence-2026-09.md](lessons/live-eval-sequence-2026-09.md) | The paid run sequence, written for whoever runs the next one. Six of its ten entries were failures the harness or the operator manufactured, not the agent |
| [lessons/parallel-agent-campaigns.md](lessons/parallel-agent-campaigns.md) | What breaks when several agents share one checkout |
| [eval-debt.md](eval-debt.md) | The append-only ledger of behaviour-surface changes that merged during the campaign eval freeze, and the walk that dispositioned every row when the freeze closed |

## Planned, and not written yet

Named here so a reader knows they are absent by schedule rather than by oversight. Each is an exit criterion for its phase in `CLAUDE.md`.

- `threat-model.md` — injection surfaces, mitigations, and the mapping to the adversarial scenario suite (Phase 7).
- `memory-design.md` — what episodic memory stores, how it is retrieved, and the forgetting policy (Phase 4).
- `interview-map.md` — component to skill mapping (Phase 8).

## Elsewhere in the repo

- [`study/findings.md`](../study/findings.md) — a dated record of every reliability finding the paid runs produced. Each entry names the control that failed, the evidence that settled it, and the fix that makes the failure non-repeatable.
- [`study/predictions.md`](../study/predictions.md) — predictions pre-registered before the clean-baseline rerun, deliberately left as written.
- [`demo/README.md`](../demo/README.md) — bringing the platform stack up, what each teardown target keeps or deletes, and what is pinned.
- [`context/`](../context/) — the per-session history convention. `context/INDEX.md` is the map; the archives it indexes are gitignored and absent from a clone.
