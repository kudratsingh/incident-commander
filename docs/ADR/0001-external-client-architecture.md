# ADR 0001: Deploy the Incident Commander as an external client of the Incident Platform

* Status: accepted
* Date: 2026-07-15
* Decider: Kudrat Singh

## Context and problem statement

Incident Commander is an autonomous AI SRE agent that investigates and remediates incidents on the Incident Platform. The agent needs telemetry reads, admin actions such as restarting consumers, replaying DLQs, and rolling back deploys, and an approval path for risky actions. The structural question is where the agent lives relative to the platform: inside the platform codebase and process, beside it in one repository, or outside it entirely as an independent product. The answer determines whether the safety model is enforced or merely described.

## Decision drivers

* The trust boundary must be real. Tiered actions, allowlists, and blast-radius limits only mean something if the agent is a constrained principal the platform can refuse.
* The safety model must be externally enforceable. Authorization cannot depend on the agent process behaving well.
* Release cadences differ. Prompt, policy, model, and eval changes should ship without a platform deployment, and platform PRs should not wait on paid eval runs.
* The AI SRE category deploys this way. Cleric, Resolve, and Traversal connect to customer environments through observability and admin surfaces, not from inside customer codebases. Matching that shape keeps the door open to a second target environment later.
* Portfolio legibility. Two repositories tell two distinct stories: a distributed systems platform and an agentic AI product.

## Considered options

1. In-process module inside incident-platform
2. Monorepo with two deployable services and a hard import boundary
3. Separate repository, external client through network contracts only (chosen)
4. External client with direct read access to platform datastores

## Decision outcome

Option 3. Incident Commander lives in its own repository and communicates with the platform exclusively through the platform's MCP server and versioned REST endpoints.

The decision has five parts:

1. **Independent principal.** The agent authenticates as a service account with explicitly scoped tokens (telemetry:read, incidents:read, actions:propose, actions:execute) and per-principal rate limits, through the same authorization layer as any operator or integration.
2. **Independent release cycle.** Model, prompt, policy, and evaluation changes deploy without touching the platform. Each repo owns its own CI economics.
3. **Reusable product boundary.** The tool layer is written so additional environments can be supported later through adapters. Pointing a second MCP client (Claude Code) at the platform is standing proof the surface is agent-agnostic.
4. **No internal imports.** No shared code, no direct connections to the platform's Postgres, Redis, or Kafka. Communication happens only over versioned network contracts, pinned by image digest with a generated contract snapshot diffed in CI. Mechanics live in ADR 0007.
5. **Platform-owned enforcement.** The platform makes the final authorization decision on every call. Approvals are platform objects with parameter-hash binding, detailed in ADR 0003. The agent's own policy registry is a first filter, never the security boundary.

One invariant is promoted into this record because it is architectural: the agent augments the paging path and never gates it. If the model API or the agent is down, alerts page humans through the normal platform route. No human page ever waits on the agent.

### Why the alternatives lose

**Option 1, in-process module.** The agent inherits the platform's full permissions, so every tier, allowlist, and budget becomes advisory. A bug or an injected instruction inside the process has god-mode access. The harness would be decoration.

**Option 2, monorepo with a boundary.** Preserves the trust model if the no-imports rule holds, but the rule is enforced by discipline rather than by the repository boundary, CI economics stay entangled, and the agent work is invisible at the repo-card level. This remains the documented fallback if two-repo friction becomes unsustainable for a solo developer. The fallback is never option 1.

**Option 4, direct datastore reads.** Even limited Postgres grants bypass application-level tenant authorization, idempotency, validation, and audit. It also makes the agent's capability surface unenumerable, which breaks eval design. If ad hoc queries become necessary later, the path is a platform-mediated run_readonly_query tool against a read replica with statement allowlisting, timeouts, and row caps. Deferred, not planned.

### Consequences

Positive:

* The safety model is enforced by infrastructure rather than promised by prompts, and safety metrics are graded from the platform's immutable audit log rather than the agent's self-report.
* The agent's capability surface is exactly the published tool contract, which makes evals, threat modeling, and interview narration tractable.
* Any new capability requires a deliberate platform PR, so the agent can never invent operational powers.

Negative:

* Cross-cutting changes need two PRs, and local development runs both services. Accepted as useful friction.
* Contract drift between repos is a live risk. Mitigated by digest pinning and snapshot diffing in CI, per ADR 0007.
* Some type definitions exist twice, generated on the agent side rather than imported. Accepted to preserve the boundary.

Revisit trigger: sustained solo-development friction that measurably slows phase delivery reopens the monorepo fallback, option 2, with the import boundary enforced by tooling.

## More information

CLAUDE.md invariants 1, 2, 5, and 6 restate this decision as daily working rules. Platform-side implementation lands as the wave 1 PR sequence: principals and scopes, audit log, MCP scaffold.
