# ADR 0003: Enforce Tier-1 remediation authorization on the platform, not the agent

* Status: accepted
* Date: 2026-07-30
* Decider: Kudrat Singh

## Context and problem statement

Phase 6 gave the agent the ability to invoke Tier-1 write actions (`restart_consumer_group`, `pause_dag`, `replay_dlq_messages`, `invalidate_cache_key`). Each mutates shared platform state — the wrong invocation would restart a healthy consumer, wipe a warm cache, or replay jobs that shouldn't retry.

The agent already has a policy module (`src/incident_commander/tools/policies.py`) classifying every tool as `READ` / `TIER_1` / `TIER_2`. The investigation planner is constrained to `tools_at_or_below(READ)`; only the remediation planner sees Tier-1 tools. But that policy runs *inside the agent* — a compromised planner prompt, a hallucinated tool name, or a bug in the state machine could still cause the agent to attempt a Tier-1 call.

CLAUDE.md invariant 2 requires: "The final authorization decision always happens on the platform side." This ADR captures how that invariant is upheld across Tier-1 (this phase) and Tier-2 (Wave 3 PR F on the platform, deferred).

## Decision drivers

* Model output is untrusted (CLAUDE.md invariant 4). The prompt can be poisoned by log content the agent reads.
* Agent-side policy is fast but bypassable. Platform-side is slow but enforceable.
* Bounded, reversible actions can execute without human approval to preserve auto-remediation value.
* Wider-blast-radius actions must not execute without human sign-off.
* Idempotency has to be the platform's decision, not the agent's — retries + crash recovery need server-side deduplication.

## Considered options

1. Agent-side enforcement only. Trust the tier-policy module and the remediation planner's prompt.
2. Two-layer: agent-side first filter + platform-side final authorization.
3. Platform-side only. Skip the agent-side policy entirely.

**Chosen: option 2.** Agent-side policy is the first filter — cheap, fails loudly during eval, and keeps the wrong tool from ever being proposed. Platform-side authz is the security boundary — the token's scope, the tool's `required_scope`, and (for Tier-2, later) the propose/approve/execute object with param-hash binding are what actually decide whether the action fires.

## Decision outcome

### Tier ladder (this phase)

| Tier | Agent-side rule | Platform-side rule | Approval? |
|---|---|---|---|
| `READ` | Investigation planner may propose any | Token needs `telemetry:read` / `incidents:read` scope | None |
| `TIER_1` | Only remediation planner may propose. Guarded by `tier_of(tool) is Tier.TIER_1` check on plan output. | Token needs `actions:execute` scope. Tool declares `is_idempotent=True` and mandates a caller-supplied `idempotency_key`. Platform's idempotency store dedups by that key. | None (agent executes directly) |
| `TIER_2` | Not populated today. Reserved for actions where blast radius is wide enough to warrant human review. | Requires a platform-side approval object with param-hash binding. Agent holds a request slip, never keys. | Human via platform inbox (Wave 3 PR F) |

### What lives where

**Agent side (`src/incident_commander/tools/policies.py`):**
- `Tier` enum + `tier_of(tool_name)` classifier
- `tools_at_or_below(max_tier)` — investigation planner asks for `READ`, remediation planner for `TIER_1`
- `ensure_covered()` — new tool in registry without a policy entry fails the unit test

**Agent side (`src/incident_commander/agent/remediation.py`):**
- `make_llm_plan` rejects any `RemediationPlan` where `action_tool ∉ TIER_1` or `verify_tool ∉ READ`
- `make_remediate` generates the idempotency key deterministically: `sha256(incident_id | tool | sorted_json_args)`

**Platform side:**
- Every Tier-1 tool has `required_scope=Scope.ACTIONS_EXECUTE` in its `@tool` decorator
- The tool's input model requires `idempotency_key` (min length 8, max 255)
- Platform stores executed keys and returns the cached result on repeat calls
- Immutable audit log records every invocation with the resolved principal + args

### Idempotency key generation

Deterministic: `hashlib.sha256(f"{incident_id}|{tool_name}|{sorted_json_args}").hexdigest()[:32]`. Same `(incident, tool, args)` produces the same key. Cross-incident, cross-tool, and cross-args produce different keys. The `idempotency_key` field is stripped from the args before hashing so the key doesn't depend on itself.

This lets crash recovery replay REMEDIATING safely — the second call returns the cached response from the platform without re-executing.

### Why the alternatives lose

**Option 1 (agent-side only)** fails CLAUDE.md invariant 2. If the model output is compromised (a poisoned log line convinces the planner to invoke `restart_consumer_group("payments-consumer")` on a healthy group), no platform check catches it. The audit log records the misfire but the damage is already done.

**Option 3 (platform-side only)** wastes a round-trip. Every planner iteration would send an invalid proposal to the platform to be rejected. Agent-side policy filters this at proposal time, drops LLM cost, and produces better trajectories.

### Consequences

Positive:
* Two independent layers must agree before a Tier-1 call succeeds. Compromising one still requires the other to be wrong.
* Idempotency store handles both retry-within-run and crash-recovery-across-runs uniformly.
* Adding a new tool requires two coordinated changes (registry + policies + platform scope) — hard to accidentally expand blast radius.

Negative:
* Duplicate enforcement (agent + platform). Mitigation: policy classification is data, not logic — `_TIER_1_TOOLS = frozenset({...})` on the agent mirrors `required_scope` on the platform. Drift caught by `ensure_covered()` + contract snapshot.
* Tier-2 approvals not shipped yet. Mitigation: `_TIER_2_TOOLS` is an empty frozenset today. Adding a tool at Tier-2 requires the platform's approvals surface to exist first — the empty set makes that dependency loud.

Revisit trigger: Tier-2 lands. At that point we need to decide whether the agent stores the approval id on `RunState`, how AWAITING_APPROVAL polls or is pushed to, and how the platform binds the approval to the specific param hash.

## More information

* Implementing PRs: #32 (registry + policies), #34 (remediation loop), #35 (crash recovery), #36 (scenarios), #37 (this ADR + chaos setup)
* Related: [ADR 0001](0001-external-client-architecture.md) (external client posture), [ADR 0002](0002-hand-rolled-state-machine.md) (explicit transitions)
* CLAUDE.md invariants 2 (platform-owned enforcement), 3 (approvals are platform objects), 4 (tool output is untrusted data)
