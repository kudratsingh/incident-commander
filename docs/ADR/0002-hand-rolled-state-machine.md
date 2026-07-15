# ADR 0002: Hand-rolled state machine for the agent loop instead of LangGraph

* Status: accepted
* Date: 2026-07-15
* Decider: Kudrat Singh

## Context and problem statement

The agent's control flow is an incident lifecycle: triage, an investigation loop, remediation planning, a tier gate, a remediation loop with verification, and resolution, with escalation reachable from every state. The loop must checkpoint after every transition, pause for approvals that may arrive hours later, survive process crashes, hold a single-flight lease per incident, and reconcile against the platform audit log before re-planning after a crash. Something has to own transitions and durability. The question is whether that something is a framework or code I write.

## Decision drivers

* The topology is static. States and edges are fixed at design time. The intelligence lives inside states (hypothesis ranking, probe selection, plan generation), not in dynamic graph structure. Frameworks earn their keep on dynamic topologies, parallel branches, and streaming orchestration, none of which this loop needs.
* Durability is the load-bearing feature. Checkpoint-per-transition, resume-on-approval, crash reconciliation, and leasing map directly onto Postgres rows and the saga and outbox patterns already running on the platform. A framework checkpointer inserts its own schema and abstractions between me and the exact mechanics this project exists to demonstrate.
* Portfolio coverage. arxiv-research-agent already demonstrates LangGraph fluency on a five-agent graph. This repo demonstrates that the loop itself is understood. Together they answer both "have you used the frameworks" and "do you need them."
* Dependency surface. Fewer dependencies, cleaner mypy --strict, no framework version churn in a repo whose CI gates on behavior regressions.
* Debuggability. Transitions as plain functions are unit-testable in isolation, and a trajectory is just the ordered list of persisted transitions.

## Considered options

1. Framework-managed graph (LangGraph, known from arxiv-research-agent)
2. Durable execution engine (Temporal-class workflow runtime)
3. Hand-rolled explicit state machine with Postgres checkpointing (chosen)

## Decision outcome

Option 3. The orchestrator in src/agent/orchestrator.py is an explicit state machine:

* States are an enum: TRIAGE, INVESTIGATING, PLANNING, AWAITING_APPROVAL, REMEDIATING, VERIFYING, RESOLVED, ESCALATED, plus terminal failure states.
* Each transition is a function that takes the typed run state (src/agent/state.py, Pydantic) and returns the next state plus effects. Budget checks run at every loop boundary.
* A checkpoint row is written transactionally after every transition. Resumption loads the latest checkpoint, so an approval arriving hours later simply unblocks AWAITING_APPROVAL, and a crash resumes from the last committed transition.
* On resume after a crash during remediation, the first step is reconciliation: read the platform audit log to learn whether the last proposed action actually landed before planning anything new.
* A single-flight lease per incident id, implemented as a Postgres advisory lock or lease table, guarantees one live run per incident.
* Sub-agents, such as the phase 5 log diver, are function calls inside a state that return compressed findings. They are not graph nodes. The topology stays static.

### Why the alternatives lose

**LangGraph.** Nothing here is beyond it, and its checkpointer would work. But for a static topology the graph API is indirection without payoff, the checkpointer abstracts exactly the durability mechanics this project needs to expose, and the dependency brings version churn into a behavior-gated CI. Proven elsewhere in the portfolio, deliberately not used here.

**Durable execution engine.** Temporal-class runtimes solve checkpoint, resume, and retry for real, and they are a credible industry answer for production agent loops. Rejected at solo scale: heavyweight infrastructure for one service, and it hides the same mechanics the framework would.

### Consequences

Positive:

* Full ownership of the durability story, which is the part interviews probe hardest.
* Transitions are trivially unit-testable, and trajectories fall out of the checkpoint table for free.
* Minimal dependency surface in the most behavior-sensitive repo I own.

Negative:

* I own the concurrency and resume edge cases a framework would handle. Mitigation: dedicated integration tests that kill the process mid-remediation, deliver a late approval, and race two alerts for one incident.
* No free ecosystem tooling for visualizing runs. Mitigation: OTel spans per transition and the trajectory store carry observability.
* Reinvention risk if the state machine grows clever. Mitigation: the machine stays small and dumb, and intelligence lives in the engines it calls.

Revisit trigger: if the topology ever needs to become dynamic, for example parallel hypothesis branches investigated concurrently, reopen this decision and reevaluate LangGraph before writing a scheduler by hand.

## More information

CLAUDE.md, Tech stack and standing decisions, restates this choice. The phase 0 walking skeleton implements the minimal version: three states, one transition cycle, one checkpoint table.
