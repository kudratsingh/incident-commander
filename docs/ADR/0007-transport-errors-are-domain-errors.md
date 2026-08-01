# ADR 0007: Wrap all transport failures as domain errors at the client boundary

* Status: accepted
* Date: 2026-07-31
* Decider: Kudrat Singh

## Context and problem statement

`MCPClient` and `LLMClient` sit at trust boundaries between the agent's business logic and two external systems (the platform's MCP endpoint and the Anthropic API). Before this ADR they leaked their transport-layer exceptions upward: `httpx.RequestError`, `httpx.HTTPStatusError`, `anthropic.APIConnectionError`, and `anthropic.APIStatusError` could all reach a state-machine transition. In the seven-run Phase-6 live eval that produced this ADR, one 429 mid-run and one flaky `httpx.ConnectError` each surfaced as a raw stack trace, killed the scenario, and looked like a novel bug — three times in different scenarios before we recognized the pattern.

The transitions had no way to distinguish "transport blew up, escalate with a graded reason" from "this is a policy violation, escalate for a different reason." Every transport hiccup crashed the eval.

## Decision drivers

* Business logic should reason about *what happened to the incident*, not about SDK types. Raw `httpx` / `anthropic` exceptions in a transition are a leak of transport concerns into the state machine.
* A crashed scenario is an eval-infrastructure bug by definition — it produces no evidence, no briefing, no trajectory. An escalation-with-reason is a graded outcome.
* Retry policy (network, 5xx, 429 + Retry-After) belongs with the transport; escalation policy belongs with the transition. Mixing the two makes retries invisible to trajectories and escalations invisible to metrics.
* Two adjacent classes of failure — permanent client errors (bad request, auth) and transient server/rate errors — need different behavior: fail fast vs. retry then wrap. They must not both leak the same raw exception.

## Considered options

1. Catch raw exceptions at every call site in the transitions.
2. Use a global exception handler in `run_to_completion` that maps SDK types to domain types before the transition sees them.
3. Wrap at the client boundary — the client's public methods raise only its own domain exception (`MCPError` / `LLMError`) (chosen).

## Decision outcome

Option 3. `MCPClient` and `LLMClient` are the only places `httpx` and `anthropic` types are handled. Every public call returns success or raises the client's own domain exception; no other exception type crosses the boundary.

* **`MCPClient`**: transport errors → `MCPError(-32000, "transport error after N attempts: ...")`. Non-2xx (except retried 5xx/429) → `MCPError(-32000, "HTTP <status> ...")`. Non-JSON bodies → `MCPError(-32700, ...)`. Retry policy: 5xx and 429 retried with backoff, honoring numeric `Retry-After` when the response carries one; network errors retried with backoff; everything else fails fast.
* **`LLMClient`**: 4xx (except 429) → `LLMError("LLM API error <status>: ...")` immediately (retry can't help). 429 + 5xx + connection errors retried with backoff, honoring numeric `Retry-After` when present. Persistent transport failure after `max_attempts` → `LLMError("LLM transport failure after N attempts: ...")` with `from last_exc` to preserve the chain.
* Transitions catch `MCPError` and `LLMError`, call `_escalate_remediation` (or the transition's local escalate helper), and the run terminates with `ESCALATED` and evidence explaining why.

Enforcement: client unit tests assert the public surface raises only the domain exception, including the retry-then-succeed and retry-then-give-up paths for 429 + 5xx + network errors.

### Why the alternatives lose

**Catch at every call site.** Would work, would drift. Every new transition or every new tool-call path becomes another chance to forget the `try/except`. The class of bug the seven-run eval surfaced — transport errors crashing the scenario — was exactly the case where a transition forgot.

**Global handler in `run_to_completion`.** Centralizes the mapping but hides it from the transition's own contract. A transition would look like it could raise `httpx.ConnectError` even though something upstream would rewrap it. Structural confusion, and it forces the state machine to know about transport types it should never see.

### Consequences

Positive:

* Transitions can be written under one exception invariant: "if `MCPError` or `LLMError`, escalate with the reason string." That invariant is testable.
* Retries stay hidden from evidence (they're transient by definition); only the terminal outcome carries into the briefing.
* Adding a new tool client (e.g. a memory backend, an OTel exporter that agents call) inherits the same discipline by construction — its own domain error type, wrapped at the boundary.
* Enforceable at the linter level (no `httpx` / `anthropic` imports outside `tools/` + `llm/`), which prevents drift.

Negative:

* The original SDK exception is now one `.__cause__` away instead of directly at the point of failure. Mitigation: `from last_exc` on every raise preserves the chain, and structured logging in the client records the original type + message before wrapping.
* Two clients duplicate the "retry with Retry-After" logic. Small enough to keep inline; extract to a shared helper only if a third client needs it.

Revisit trigger: if we add a third external client, extract the retry+wrap logic into a shared `RetryPolicy` helper rather than triple-implementing it.

## More information

Implementing PR: [#48](https://github.com/kudratsingh/incident-commander/pull/48). Lives in `src/incident_commander/tools/mcp_client.py:MCPClient._call` and `src/incident_commander/llm/client.py:LLMClient.call`. Regression coverage in `tests/unit/test_mcp_client.py::TestRetries` and `tests/unit/test_llm_client.py::TestRetries`. Related lesson: [`docs/lessons/live-eval-noise-sources.md`](../lessons/live-eval-noise-sources.md) (the *transport flakiness* noise source).
