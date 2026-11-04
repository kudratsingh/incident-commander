# Incident Commander — Autonomous AI SRE

An **autonomous AI SRE and incident-response agent** that performs hypothesis-driven investigation, risk-tiered remediation and post-action verification on the [Incident Platform](https://github.com/kudratsingh/incident-platform). Its explicit state machine combines evidence-led tool selection, Postgres checkpoints, single-flight incident leases, bounded budgets and scoped **MCP** access.

**Best-of-N generation, candidate selection, reflection, bounded search and adaptive inference** share the same execution and safety boundaries. A chaos-driven evaluation harness checks outcomes through deterministic graders and platform audit evidence, connecting inference-time compute research with recoverable incident operations. The agent remains an external client: authorization, tenant isolation and idempotency are enforced by the platform.

## The incident loop

```text
Alert → triage → investigate → plan → remediate → verify → resolve
                    ↖ evidence gathering          ↘ escalate with briefing
```

- **Investigation:** ranked hypotheses, an evidence ledger and the next useful probe, bounded by tool, token, time and dollar budgets.
- **Execution:** Tier-1 actions under idempotency keys, with one remediation attempt per incident.
- **Verification:** a polling window checks the action's effect. Stabilizing an incident is reported separately from resolving its cause.
- **Recovery:** every state transition is checkpointed to Postgres, with a single-flight lease per incident.
- **Escalation:** budget exhaustion, unsupported actions and unresolved evidence produce a written handoff. Human paging does not wait for the agent.

The state machine is explicit Python. Tools and risk tiers are allowlisted; retrieved logs, payloads and error strings cannot grant authority. [Architecture decision](docs/ADR/0001-external-client-architecture.md) · [Safety model](docs/safety-model.md).

## What is implemented

| Area | Current implementation |
|---|---|
| Agent | Signed-webhook ingress, polling fallback, investigation, Tier-1 remediation, verification and escalation |
| Persistence | Checkpoints, incident identity, crash recovery and single-flight leases |
| Tools | Typed registry generated from the platform contract; read tools and bounded Tier-1 actions |
| Inference strategies | Baseline, enumerated/sampled best-of-N, candidate selection, bounded reflection, replay-world search and adaptive allocation |
| Evaluation | Scenario runner, deterministic graders, pinned judge support, regression gate and append-only artifacts |
| Tier-2 approvals | Not implemented: no Tier-2 tools; the approval state remains a stub |

The strategy implementations share the same loop and safety boundaries. Their presence is not a claim that additional inference improves outcomes; each needs its own controlled evaluation.

## Evaluation and evidence

The committed regression baseline reports **41/41 scenarios passing**, with **34 marked degraded**. It uses offline/canned execution; this is a harness regression result, **not a 100% live-agent remediation rate**. The [baseline artifact](evals/reports/baseline.json) records execution mode, degradation and provenance per scenario.

The live evaluation path separately injects faults into a pinned platform stack and checks the resulting world. Deterministic graders assess outcome, evidence, action safety, budget adherence and escalation. The platform's audit records establish which actions occurred; the agent does not grade its own safety from its narrative. A model judge covers softer briefing qualities.

Agent, evaluator and read-only smoke traffic use separate service-account principals. Fault injectors and their audit stream are withheld from the agent under test. Completed traces, trajectories and reports are versioned rather than overwritten.

[Evaluation methodology](docs/eval-methodology.md) explains the metrics and limits. [Study findings](study/findings.md) records failures found during live runs, including a supposedly read-only stage using the wrong principal and a rerun erasing earlier evidence.

## Run the local checks

Install [uv](https://docs.astral.sh/uv/), then:

```bash
make setup
make check
make test
make eval-reg
```

These targets run local lint/types, unit/integration tests and the offline regression suite. They do not require a live platform or paid model calls. Environment and sandbox assumptions are documented in [CLAUDE.md](CLAUDE.md) and the [runbook](docs/runbook.md).

To bring up the pinned platform demonstration:

```bash
make demo
make bootstrap-token
```

Follow [demo/README.md](demo/README.md) to configure the scoped tokens. Live agent/model evaluation is a separate, explicitly funded procedure in the [runbook](docs/runbook.md). The demo pins both platform and console images; it is not evidence of a public production deployment.

## Code and documentation

| Area | Entry point |
|---|---|
| State machine and incident loop | [src/incident_commander/agent](src/incident_commander/agent/) |
| Inference strategy implementations | [agent/strategies](src/incident_commander/agent/strategies/) |
| MCP transport, registry and tier policy | [tools](src/incident_commander/tools/) |
| Checkpoints and leases | [persistence](src/incident_commander/persistence/) |
| Scenario harness and contract snapshot | [evals](evals/), [contracts](contracts/) |
| Design decisions and operational guide | [ADR index](docs/ADR/README.md), [runbook](docs/runbook.md), [docs index](docs/README.md) |

Durable memory, reusable skill/subagent orchestration, Tier-2 approval execution and later hardening milestones remain outside the completed scope. The phase plan in [CLAUDE.md](CLAUDE.md) distinguishes them from shipped work.

## License

All rights reserved. Published for reading and review; no license to use, copy, modify or redistribute is granted.
