# ADR 0068 — the agent reports its run, and reporting is never a tool call

Status: accepted (2026-09-19, WO-R3-314)

## Context

The live demo needs a person to be able to watch an incident run: which phase the agent is
in, what it currently thinks, what it last did, and what it concluded. The agent's own
artefacts cannot supply that. During a run the commander writes only the append-only JSONL
trace and in-memory checkpoints; the trajectory and the briefing render at the end. A
console tailing a file on the operator's laptop is not a product, and the platform — which
the console already reads — knows nothing about the agent's internal state.

Platform v0.6.13 (platform ADR 0035) adds the other half: an `agent_runs` table and two
write tools, `report_agent_run` and `report_agent_briefing`, under a new `agent_runs:write`
scope. **There is deliberately no read tool for `agent_runs`, and the platform withholds the
`agent.run_reported` audit rows from the agent's own principal.** So the agent can say what
it is doing and cannot read back what it said — ADR 0012 holds in both directions, and the
console (a human operator over REST) is the only reader.

That leaves three questions on this side: where the reporting happens, what it is allowed to
cost, and whether the model gets a say in it.

## Decision

**1. Reporting hangs off the checkpoint seam, as a `Checkpointer` decorator.**
`ReportingCheckpointer` wraps the checkpointer `run_to_completion` already writes to on entry
and after every transition. That is the one place in the system that knows a run moved, so
`agent/loop.py` needed no change at all — no new hook argument, no transition aware of
telemetry. The decorator writes the checkpoint **first** and reports **second**, and that
order is the guarantee, not a detail: reversed, a slow or refused report would stand between
a transition and its durable record, and a crash in that window would lose the run's own
history to telemetry.

**2. Reporting is fail-open, and that follows from invariant 5 rather than being a nicety.**
Every failure — transport, 403, timeout, a tool-level refusal carrying `isError` — is logged
at WARNING and swallowed. A run's outcome, evidence ledger and budget are identical whether
the platform answered or was switched off. `KeyboardInterrupt` and `SystemExit` are
deliberately *not* swallowed: an operator stopping a run must win over its telemetry.

**3. A report is not a tool call, and never touches `BudgetLedger`.**
Reports do not count against `max_tool_calls`, cannot exhaust a budget, and leave no entry on
the evidence ledger. The ledger is what the briefing, the deterministic graders and the
reward all read; a report on it would arrive in the investigation trail a human is handed as
though the agent had probed something. The failure this rules out is concrete: a demo run
escalating *because it was being watched*.

**4. The model never chooses to report.** The two tools are absent from `TOOL_REGISTRY`, so
they never reach `format_tool_block()` and cannot be proposed. This needed a second
exclusion prefix, because the existing one did not cover the case. Until v0.6.13 the surface
split in two along scope: a tool the agent could call was a tool it might be asked to
choose, and the tools it could not call were the chaos hooks, filtered out by their
`[chaos:` description prefix. These two are neither — `agent_runs:write` is on the agent
account. So `registry.EXCLUDED_DESCRIPTION_PREFIXES` is now `("[chaos:", "[commander:")`
behind one predicate, `mirrored_in_registry()`, which the registry-coverage test reads
instead of keeping its own copy.

**5. Off unless asked for.** `AGENT_RUN_REPORTING` defaults to false. The default is the
decision: a graded eval must not begin making extra platform calls because a setting
drifted. `make demo-live` is its only caller, and the runner builds a reporter only when
there is also a live platform and an agent principal to report as. Reports go under the
token the agent **acts** with, never the evaluator's chaos token — a report is the agent's
own statement about its own run, and the evaluator making it would be a different claim.

**6. The run id is derived, not random.** `report_agent_run.run_id` is typed `format: uuid`;
the harness's `invocation_id` is `uuid4().hex[:12]`, which is not one. Sending the invocation
id would be refused at the first report and leave the console empty for a reason nobody could
see; sending a fresh `uuid4()` would work and be joinable to nothing. It is a UUID5 over
(invocation id, scenario), so anyone holding the two facts an archive already records can
recompute the id the console holds.

## Consequences

A human can follow a run without the agent gaining a single new thing to read. The console's
phase strip has two independent sources — the platform's own audit log and the agent's own
word — and when they disagree that is information, which is why the run report never
replaces the audit trail.

The cost is an extra platform call per transition on a reported run, off the budget and off
the ledger, plus two `agent.run_reported` audit rows per call that the agent cannot see. That
withholding means the agent's `list_audit_events` totals go **down** on the first pin taken
after anything has reported — the direction most easily misread as a lost row, so it is
recorded in the runbook's bump walk rather than left to be rediscovered.

What this ADR does **not** decide: whether a production deployment should report by default.
The reporter is built for a watched demo and a live operator. Turning it on for a graded run
would add platform calls to the thing being measured, and the honest version of that change
is its own decision with its own ADR.

## Alternatives considered

- **A hook argument on `run_to_completion`.** Rejected: the loop would learn about telemetry
  to gain nothing the checkpoint seam does not already give, and every future caller would
  have to pass it or silently lose reporting.
- **Reporting from inside the transitions.** Rejected: five places to keep in step instead of
  one, and each would need its own fail-open guard.
- **A registered tool the planner may call.** Rejected outright. It would put "tell the human
  what you are doing" in competition with "investigate the incident" inside a budget, and the
  model would sometimes choose wrong. Reporting is the harness's job.
- **REST instead of MCP.** Rejected by invariant 1's shape and by the platform's own design:
  the agent speaks MCP, and the REST twins are the operator's surface.
