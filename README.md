# Incident Commander

An autonomous on-call agent that investigates and remediates production incidents on the [Incident Platform](https://github.com/kudratsingh/incident-platform) — and an eval harness built to prove whether it actually does.

The agent is an **external client**. It reaches the platform only through the platform's MCP tool server and versioned REST endpoints, authenticated with a scoped service-account token. Authorization, tenant isolation, rate limits, idempotency, approvals and audit are all enforced on the platform side, not here. If the agent needs a capability the platform does not expose, the answer is a platform pull request that adds a tool, never a bypass. See [ADR 0001](docs/ADR/0001-external-client-architecture.md).

## What it does

An alert arrives — by signed webhook, or by polling as a fallback. From there the agent runs one incident to a terminal state:

1. **Triage** — is this actionable, or is it noise?
2. **Investigate** — rank hypotheses, pick the most discriminating next tool call, update the evidence ledger, stop when the evidence is decisive or the budget is spent.
3. **Plan** — propose a remediation, classified by risk tier.
4. **Act and verify** — execute under an agent-generated idempotency key, then poll for evidence that the action worked ([ADR 0006](docs/ADR/0006-verification-is-a-polling-window.md)). One attempt per incident ([ADR 0008](docs/ADR/0008-single-attempt-remediation.md)).
5. **Resolve or escalate** — an escalation always carries a written briefing, never a silent stop.

The loop is a hand-rolled explicit state machine, not a framework graph ([ADR 0002](docs/ADR/0002-hand-rolled-state-machine.md)). Every transition is checkpointed to Postgres, so a run resumes after a crash, and one incident can only be worked by one run at a time ([ADR 0016](docs/ADR/0016-incident-identity-and-single-flight.md)).

## The rules it cannot talk its way out of

Five of the invariants in [`CLAUDE.md`](CLAUDE.md) matter most to anyone reading the code:

- **Tool output is untrusted data.** Log lines, DLQ payloads and error strings are evidence to reason about, never instructions to follow. Risk tier is never derived from model output or tool output.
- **The audit log is ground truth.** Safety is graded from the platform's immutable audit records, not from the agent's own account of what it did. An agent cannot grade itself honest.
- **Budgets are hard limits.** Tool calls, tokens, wall clock and dollars all have ceilings. Exhausting one escalates with a briefing; it never continues quietly.
- **Eval artifacts are append-only.** No run record is ever truncated or overwritten — on disk the completed archives are locked read-only ([ADR 0021](docs/ADR/0021-run-archives-are-locked-by-the-filesystem.md)). This rule exists because a tracer once cleared each scenario's file on construction and erased a paid run's first attempt with its own re-run.
- **Fail open on paging.** If the agent is down, alerts page humans by the normal route. No human page ever waits on an agent.

The full safety contract — the tier ladder, what each Tier-1 action can touch, the kill switch, the budget meters — is in [`docs/safety-model.md`](docs/safety-model.md).

## Where it stands

**Phase 6, with one exit criterion open.**

| Shipped | State |
|---|---|
| Read-only investigation loop, hypothesis ranking, evidence ledger | live |
| The full read tool surface and a typed registry generated from the platform contract | live |
| Tier-1 auto-remediation: plan → execute → verify, under idempotency keys | live |
| Crash recovery from Postgres checkpoints, single-flight lease per incident | live |
| Escalation briefings, deterministic and LLM-enriched | live |
| Eval harness: 41 scenarios, deterministic graders, a pinned LLM judge, a regression gate | live |
| **Tier-2 propose / approve / execute against platform approval objects** | **not built** |

`_TIER_2_TOOLS` is deliberately empty and `AWAITING_APPROVAL` is a stub handler: the approvals subsystem is a platform-side deliverable that has not landed, so no Tier-2 tool ships here. Everything the agent can execute today is Tier-1 and idempotent.

Phases 4 (memory), 5 (skills and sub-agents), 7 (adversarial hardening) and 8 (observability, cost dashboards, end-to-end tier) have no implementation yet. `CLAUDE.md` has the phase plan and the exit criteria for each.

## The eval harness

The harness is the point as much as the agent is. It was built before the behaviour and it gates changes to it.

- **41 scenarios** across 11 families — consumer lag, DLQ handling, cache and Redis, traces, workflows, deploys, incidents, Postgres, tool faults, noise, and harness control. 29 run against a live platform, 12 replay canned fixtures.
- **Ground truth is hidden from the agent.** A scenario declares the fault it injects and the remediation that resolves it; what the agent sees is an allow-list projection of that scenario ([ADR 0038](docs/ADR/0038-the-agents-view-of-a-scenario-is-an-allow-list-projection.md)).
- **Graders are deterministic first** — root-cause match, action safety read from the platform audit log, budget adherence, escalation correctness. A pinned LLM judge grades only the soft qualities: is the briefing useful, is the reasoning coherent.
- **A committed baseline, and a gate.** `evals/reports/baseline.json` holds the blessed result: 41 of 41 passing. `make eval-reg` re-runs the suite and fails on a regression; `.github/workflows/evals.yml` runs it on any PR that touches prompts, tools, policies, the model pins, the contract snapshot or the harness itself.
- **Every run leaves a record.** Traces, trajectories, briefings and reports are versioned per invocation and never overwritten.

[`docs/eval-methodology.md`](docs/eval-methodology.md) is the long form: scenario taxonomy, grader design, metric definitions, judge pinning.

## Getting started

```bash
make setup    # uv sync --all-groups
make check    # ruff + ruff format --check + mypy
make test     # unit + integration
```

`make help` lists every target. Nothing above needs a platform, a Docker daemon or an API key.

To run the agent against a real platform:

```bash
make demo             # bring the pinned platform stack up. Compose only — no eval, no scenario
make bootstrap-token  # mint the service-account tokens and print the .env lines
make eval-reg         # the offline suite and the regression gate
```

`make demo` is documented in [`demo/README.md`](demo/README.md), including what `demo-down` keeps and what `demo-destroy` deletes. Running a **live** scenario spends real money and is one deliberate act at a time: [`docs/runbook.md`](docs/runbook.md) has the procedure and it is meant to be followed exactly.

## How it talks to the platform

The agent's tool registry declares **21 tools** — 14 read tools (consumer lag, DLQ contents, traces, deploy history, DAG state, Redis and Postgres health, incidents, alerts, audit events, cache key info, outbox status) and 7 Tier-1 actions (`restart_consumer_group`, `replay_dlq_messages`, `replay_dlq_by_ids`, `replay_dlq_by_category`, `pause_dag`, `invalidate_cache_key`, `mark_dlq_permanent`).

`contracts/platform-tools.snapshot.json` holds **30**. The extra ten are the platform's lab hooks — the fault injectors an evaluator uses to build a scenario's world. The agent never registers them, and since platform v0.6.5 its token cannot call them either.

That snapshot is generated, never hand-edited. The contract test in CI pulls the pinned platform image by digest, starts it, fetches the live tool schemas and diffs them: a tool that disappears, a parameter that becomes required, an enum that changes or a response field that changes type fails the build. The platform is pinned to **v0.6.7 by digest** in `demo/compose.yml`, which is the single source of truth for that pin.

### Three principals, and the split is load-bearing

An eval run uses three separate service-account tokens:

| Token | Who it is | Holds |
|---|---|---|
| `PLATFORM_TOKEN` | the agent under test | reads + `actions:execute` — and **not** `chaos:invoke` |
| `PLATFORM_CHAOS_TOKEN` | the evaluator that seeds, verifies and resets the world | reads + `chaos:invoke` |
| `PLATFORM_SMOKE_TOKEN` | the read-only smoke pass and traffic generator | reads only |

The agent must not be able to fire the lab, and — less obviously — must not be able to *read that the lab fired*. The platform withholds the `chaos.` audit stream from any principal without `chaos:invoke`, which means a single all-scope token would have made that protection inert. It did, for six releases: `list_audit_events` told an investigating agent exactly which hook had injected its fault, and with what arguments.

## Layout

```text
src/incident_commander/
├── agent/          the state machine: triage, investigation, remediation, briefing
│   └── strategies/ the planner-call seam — strategies propose, the loop decides (ADR 0036)
├── llm/            Anthropic client, structured outputs, bounded repair, versioned prompts
├── tools/          MCP client, typed registry, tier policy, contract diff, wire format
├── persistence/    Postgres checkpointer, single-flight lease, pool sizing
├── api/            FastAPI: webhook ingress with HMAC verification, health, run inspection
└── config.py       every setting, with the reasoning for the ones that bite

evals/              the harness: scenarios, graders, runner, regression gate, world audit
contracts/          the platform tool snapshot, generated and diffed in CI
docs/               ADRs, methodology, safety model, runbook, lessons — see docs/README.md
study/              dated findings and pre-registered predictions from the paid runs
demo/               compose file pinning the platform by digest, and how to drive it
```

## Documentation

- [`CLAUDE.md`](CLAUDE.md) — the project constitution: invariants, architecture, phase plan, conventions. Start here if you are going to change something.
- [`docs/README.md`](docs/README.md) — one line per document.
- [`docs/ADR/README.md`](docs/ADR/README.md) — all 39 decision records with their statuses.
- [`docs/safety-model.md`](docs/safety-model.md) — the enforceable safety contract.
- [`docs/eval-methodology.md`](docs/eval-methodology.md) — how the harness measures, and why those measures.
- [`docs/runbook.md`](docs/runbook.md) — operating the agent: live runs, re-pins, rollbacks, the kill switch.
- [`study/findings.md`](study/findings.md) — a dated record of every reliability finding the paid runs produced, each naming the control that failed and the fix that makes it non-repeatable.

## Configuration

Copy `.env.example` to `.env` and fill it in. It is annotated variable by variable. Never commit `.env`.

## License

No license is granted at this time. The code is published for reading and review only; all rights are reserved. You may not use, copy, modify or redistribute it without written permission from the author.
